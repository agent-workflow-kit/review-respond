"""Gateway for review-respond: the single boundary for subprocess, network and clock access.

Every call to an external program (the sibling awkit tools and git) goes through this module, so
tests can substitute fakes (``tests/fakebin/``) and a test guard can prove that nothing else
spawns a process. A test (``tests/test_gateway_boundary.py``) fails when any other module imports
``subprocess`` or opens a network connection. review-respond makes no HTTP call of its own: the
forges are reached only through forgectl and pr-comment.

Rules every wrapper follows:

- commands are argv lists, never a shell string;
- the child environment is exactly the mapping given to ``Gateway`` (callers build it from scratch);
- untrusted or agent-generated text goes through ``stdin`` or a file the caller names, never through
  argv or the environment; a value that is not text for a flag (an id, a path) must not start with
  ``-`` and must not hold a NUL byte, else ``GatewayError`` is raised before anything starts;
- every child starts in its own session, so a timeout signals exactly the process group this
  module created, never anything found by name;
- output is decoded as UTF-8 with ``surrogateescape`` and never raises on bad bytes.

Wrappers return a ``Result`` and never interpret exit codes or error envelopes: mapping a sibling's
failure is the caller's job (see the exit-code mapping in docs/interface.md).
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

PathLike = str | os.PathLike[str]
Timeout = float | None | Literal["default"]  # None: no limit; "default": the gateway default

# The environment names git is allowed to inherit; every other GIT_* variable is dropped.
_GIT_KEEP = frozenset({"GIT_CONFIG_COUNT"})
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_TERM_GRACE = 2.0


class GatewayError(Exception):
    """A call was refused before anything started (a malformed or unsafe argument)."""


class Clock(Protocol):
    """The time source. Tests substitute a manual clock; nothing else reads the system clock."""

    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass(frozen=True)
class Result:
    """What a program printed and how it ended. ``json`` is set when stdout parsed as JSON."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    json: Any = None
    json_error: str | None = None
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def parse_version(text: str) -> tuple[int, int, int] | None:
    """The first ``X.Y.Z`` in ``text`` (a ``--version`` line), or None."""
    match = _VERSION.search(text)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _arg(value: PathLike | int, what: str = "argument") -> str:
    """One argv token that is data for the program, never an option."""
    text = os.fspath(value) if isinstance(value, os.PathLike) else str(value)
    if "\x00" in text:
        raise GatewayError(f"{what} holds a NUL byte")
    if text.startswith("-") and text != "-":
        raise GatewayError(f"{what} starts with '-': refusing to pass it as an option")
    return text


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def _actor_flags(actor: str | None, decision_ids: Sequence[str]) -> list[str]:
    argv: list[str] = []
    if actor is not None:
        argv += ["--actor", _arg(actor, "actor")]
    for decision_id in decision_ids:
        argv += ["--decision-id", _arg(decision_id, "decision id")]
    return argv


def _labels(labels: Sequence[tuple[str, str]]) -> list[str]:
    argv: list[str] = []
    for key, value in labels:
        argv += ["--label", _arg(f"{key}={value}", "label")]
    return argv


class Gateway:
    """Runs programs with a fixed environment and exposes typed wrappers for each sibling."""

    def __init__(
        self,
        env: Mapping[str, str],
        *,
        clock: Clock | None = None,
        on_spawn: Callable[[int], None] | None = None,
        default_timeout: float | None = 120.0,
    ) -> None:
        self.env = dict(env)
        self.clock: Clock = clock or SystemClock()
        self._on_spawn = on_spawn
        self.default_timeout = default_timeout
        self.forgectl = Forgectl(self)
        self.agent_spawn = AgentSpawn(self)
        self.pr_comment = PrComment(self)
        self.wtctl = Wtctl(self)
        self.jobsctl = Jobsctl(self)
        self.claude_session = ClaudeSession(self)
        self.git = Git(self)

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        cwd: PathLike | None = None,
        timeout: Timeout = "default",
        env: Mapping[str, str] | None = None,
    ) -> Result:
        """Run ``argv`` without a shell. ``stdin`` is closed (not inherited) when None."""
        if not argv:
            raise GatewayError("empty argv")
        tokens = tuple(argv)
        if any("\x00" in t for t in tokens):
            raise GatewayError("a NUL byte in argv")
        limit = self.default_timeout if timeout == "default" else timeout
        proc = subprocess.Popen(
            tokens,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=os.fspath(cwd) if cwd is not None else None,
            env=dict(self.env if env is None else env),
            start_new_session=True,
        )
        # start_new_session makes the child its own group leader: pgid == pid.
        pid = proc.pid
        if self._on_spawn is not None:
            self._on_spawn(pid)
        data = stdin.encode("utf-8", "surrogateescape") if stdin is not None else None
        timed_out = False
        try:
            out, err = proc.communicate(data, timeout=limit)
        except subprocess.TimeoutExpired:
            timed_out = True
            out, err = self._stop_group(proc)
        return self._result(tokens, proc.returncode, out, err, timed_out)

    @staticmethod
    def _stop_group(proc: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
        """Signal the process group this module created for ``proc`` and reap it."""
        pid = proc.pid
        for sig, wait in ((signal.SIGTERM, _TERM_GRACE), (signal.SIGKILL, None)):
            try:
                os.killpg(pid, sig)
            except ProcessLookupError:
                pass
            try:
                return proc.communicate(timeout=wait)
            except subprocess.TimeoutExpired:
                continue
        return proc.communicate()

    @staticmethod
    def _result(
        argv: tuple[str, ...], code: int, out: bytes, err: bytes, timed_out: bool
    ) -> Result:
        stdout, stderr = _decode(out), _decode(err)
        doc: Any = None
        problem: str | None = None
        if stdout.strip():
            try:
                doc = json.loads(stdout)
            except ValueError as exc:
                problem = str(exc)
        return Result(argv, code, stdout, stderr, doc, problem, timed_out)

    def version(self, tool: str) -> tuple[int, int, int] | None:
        """``<tool> --version`` parsed to a triple, or None when it does not print one."""
        result = self.run([tool, "--version"], timeout=15.0)
        return parse_version(result.stdout) if result.ok else None


class _Tool:
    prog = ""

    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway

    def _run(
        self,
        args: Sequence[str],
        *,
        stdin: str | None = None,
        cwd: PathLike | None = None,
        timeout: Timeout = "default",
    ) -> Result:
        return self.gateway.run([self.prog, *args], stdin=stdin, cwd=cwd, timeout=timeout)

    def version(self) -> Result:
        return self._run(["--version"], timeout=15.0)


class Forgectl(_Tool):
    """Reads only: forgectl never writes for review-respond."""

    prog = "forgectl"

    def prs(
        self,
        *,
        author: str | None = None,
        repo: str | None = None,
        host: str | None = None,
        state: str | None = None,
        limit: int | None = None,
    ) -> Result:
        args = ["prs"]
        for flag, value in (("--author", author), ("--repo", repo), ("--host", host)):
            if value is not None:
                args += [flag, _arg(value, flag)]
        if state is not None:
            args += ["--state", _arg(state, "state")]
        if limit is not None:
            args += ["--limit", _arg(limit, "limit")]
        return self._run([*args, "--json"])

    def show(self, pr: str) -> Result:
        return self._run(["show", _arg(pr, "pr"), "--json"])

    def comments(self, pr: str) -> Result:
        return self._run(["comments", _arg(pr, "pr"), "--json"])

    def access(self, repo: str, user: str) -> Result:
        return self._run(["access", _arg(repo, "repo"), _arg(user, "user"), "--json"])

    def whoami(self, host: str | None = None) -> Result:
        args = ["whoami"]
        if host is not None:
            args += ["--host", _arg(host, "host")]
        return self._run([*args, "--json"])

    def compare(self, pr: str, from_sha: str, to_sha: str, *, no_diff: bool = True) -> Result:
        args = ["compare"]
        if no_diff:
            args.append("--no-diff")
        args += [_arg(pr, "pr"), "--from", _arg(from_sha, "sha"), "--to", _arg(to_sha, "sha")]
        return self._run([*args, "--json"])


class AgentSpawn(_Tool):
    prog = "agent-spawn"

    def _session_flags(
        self,
        *,
        kind: str,
        mode: str,
        skill: str,
        plugin_dir: PathLike,
        prompt_file: PathLike,
        permission_profile: PathLike,
        sandbox: str,
        setting_sources: Sequence[str],
        env_allowlist: Sequence[str],
        model: str | None,
        timeout: str | None,
    ) -> list[str]:
        """The flags every round passes (docs/interface.md, Session settings)."""
        args = [
            "--kind", _arg(kind, "kind"),
            "--mode", _arg(mode, "mode"),
            "--skill", _arg(skill, "skill"),
            "--plugin-dir", _arg(plugin_dir, "plugin dir"),
            "--prompt-file", _arg(prompt_file, "prompt file"),
            "--permission-profile", _arg(permission_profile, "permission profile"),
            "--sandbox", _arg(sandbox, "sandbox"),
            "--setting-sources", ",".join(_arg(s, "setting source") for s in setting_sources),
        ]  # fmt: skip
        # One flag per name; an empty allowlist is one empty value.
        for name in env_allowlist or [""]:
            args += ["--env-allowlist-only", _arg(name, "env name")]
        if model is not None:
            args += ["--model", _arg(model, "model")]
        if timeout is not None:
            args += ["--timeout", _arg(timeout, "timeout")]
        return [*args, "--no-promote", "--no-implicit-done"]

    def resume_fork(
        self,
        base_agent_id: str,
        *,
        cwd: PathLike,
        kind: str,
        mode: str,
        skill: str,
        plugin_dir: PathLike,
        prompt_file: PathLike,
        permission_profile: PathLike,
        sandbox: str,
        setting_sources: Sequence[str],
        env_allowlist: Sequence[str],
        labels: Sequence[tuple[str, str]],
        actor: str,
        model: str | None = None,
        timeout: str | None = None,
    ) -> Result:
        """``resume <base> --fork`` without ``--wait``: returns when the run has started."""
        args = ["resume", _arg(base_agent_id, "agent id"), "--fork", "--cwd", _arg(cwd, "cwd")]
        args += self._session_flags(
            kind=kind, mode=mode, skill=skill, plugin_dir=plugin_dir, prompt_file=prompt_file,
            permission_profile=permission_profile, sandbox=sandbox,
            setting_sources=setting_sources, env_allowlist=env_allowlist, model=model,
            timeout=timeout,
        )  # fmt: skip
        return self._run([*args, *_labels(labels), "--actor", _arg(actor, "actor"), "--json"])

    def launch(
        self,
        *,
        cwd: PathLike,
        repo: str,
        kind: str,
        mode: str,
        skill: str,
        plugin_dir: PathLike,
        prompt_file: PathLike,
        permission_profile: PathLike,
        sandbox: str,
        setting_sources: Sequence[str],
        env_allowlist: Sequence[str],
        labels: Sequence[tuple[str, str]],
        actor: str,
        model: str | None = None,
        timeout: str | None = None,
    ) -> Result:
        """A fresh launch with no worktree, in the throwaway root, without ``--wait``."""
        args = [
            "launch",
            "--worktree",
            "none",
            "--cwd",
            _arg(cwd, "cwd"),
            "--repo",
            _arg(repo, "repo"),
        ]
        args += self._session_flags(
            kind=kind, mode=mode, skill=skill, plugin_dir=plugin_dir, prompt_file=prompt_file,
            permission_profile=permission_profile, sandbox=sandbox,
            setting_sources=setting_sources, env_allowlist=env_allowlist, model=model,
            timeout=timeout,
        )  # fmt: skip
        return self._run([*args, *_labels(labels), "--actor", _arg(actor, "actor"), "--json"])

    def wait(self, agent_id: str, *, timeout: str | None = None) -> Result:
        args = ["wait", _arg(agent_id, "agent id")]
        if timeout is not None:
            args += ["--timeout", _arg(timeout, "timeout")]
        # The call blocks as long as the run lasts: no gateway timeout of its own.
        return self._run([*args, "--json"], timeout=None)

    def show(self, agent_id: str) -> Result:
        return self._run(["show", _arg(agent_id, "agent id"), "--json"])

    def list(self, *, labels: Sequence[tuple[str, str]] = ()) -> Result:
        args = ["list"]
        for key, value in labels:
            args += ["--label", _arg(f"{key}={value}", "label")]
        return self._run([*args, "--json"])

    def stop(self, agent_id: str, *, actor: str, grace: str | None = None) -> Result:
        args = ["stop", _arg(agent_id, "agent id")]
        if grace is not None:
            args += ["--grace", _arg(grace, "grace")]
        return self._run([*args, "--actor", _arg(actor, "actor"), "--json"])

    def teardown(self, agent_id: str, *, actor: str) -> Result:
        return self._run(
            ["teardown", _arg(agent_id, "agent id"), "--actor", _arg(actor, "actor"), "--json"]
        )


class PrComment(_Tool):
    """The only writer to the forge besides the push."""

    prog = "pr-comment"

    def reply(
        self,
        pr: str,
        *,
        to: str,
        body: str,
        dedupe_key: str,
        actor: str,
        decision_ids: Sequence[str] = (),
    ) -> Result:
        """Post ``body`` (untrusted text, sent on stdin) as a reply to ``to``."""
        args = [
            "reply", _arg(pr, "pr"), "--to", _arg(to, "target"), "--body-file", "-",
            "--dedupe-key", _arg(dedupe_key, "dedupe key"), "--yes",
            *_actor_flags(actor, decision_ids), "--json",
        ]  # fmt: skip
        return self._run(args, stdin=body)

    def resolve(
        self, pr: str, *, threads: Sequence[str], actor: str, decision_ids: Sequence[str] = ()
    ) -> Result:
        if not threads:
            raise GatewayError("resolve needs at least one thread")
        args = ["resolve", _arg(pr, "pr")]
        for thread in threads:
            args += ["--thread", _arg(thread, "thread id")]
        return self._run([*args, "--yes", *_actor_flags(actor, decision_ids), "--json"])


class Wtctl(_Tool):
    prog = "wtctl"

    def create_throwaway(
        self,
        *,
        repo: str,
        ref: str,
        labels: Sequence[tuple[str, str]],
        source: str | None = None,
        actor: str | None = None,
    ) -> Result:
        args = ["create", "--throwaway", "--repo", _arg(repo, "repo"), "--ref", _arg(ref, "ref")]
        if source is not None:
            args += ["--source", _arg(source, "source")]
        return self._run([*args, *_labels(labels), *_actor_flags(actor, ()), "--json"])

    def extract(
        self,
        selector: str,
        *,
        dest: PathLike,
        files_from: PathLike | None = None,
        delete_from: PathLike | None = None,
        actor: str | None = None,
    ) -> Result:
        """Copy the listed files out of a throwaway. The lists are files review-respond wrote."""
        if files_from is None and delete_from is None:
            raise GatewayError("extract needs --files-from or --delete-from")
        args = ["extract", _arg(selector, "selector")]
        if files_from is not None:
            args += ["--files-from", _arg(files_from, "files list")]
        if delete_from is not None:
            args += ["--delete-from", _arg(delete_from, "delete list")]
        return self._run([*args, "--dest", _arg(dest, "dest"), *_actor_flags(actor, ()), "--json"])

    def locate(self, repo: str) -> Result:
        return self._run(["locate", _arg(repo, "repo"), "--json"])

    def remove(self, selector: str, *, actor: str | None = None) -> Result:
        return self._run(
            ["remove", _arg(selector, "selector"), "--yes", *_actor_flags(actor, ()), "--json"]
        )

    def list(self, *, repo: str | None = None, labels: Sequence[tuple[str, str]] = ()) -> Result:
        args = ["list"]
        if repo is not None:
            args += ["--repo", _arg(repo, "repo")]
        return self._run([*args, *_labels(labels), "--json"])

    def show(self, selector: str) -> Result:
        return self._run(["show", _arg(selector, "selector"), "--json"])

    def label_add(
        self, selector: str, labels: Sequence[tuple[str, str]], *, actor: str | None = None
    ) -> Result:
        if not labels:
            raise GatewayError("label add needs at least one label")
        pairs = [_arg(f"{k}={v}", "label") for k, v in labels]
        return self._run(
            ["label", "add", _arg(selector, "selector"), *pairs, *_actor_flags(actor, ()), "--json"]
        )


class Jobsctl(_Tool):
    """Optional: without jobsctl the job runs detached (docs/interface.md, Job)."""

    prog = "jobsctl"

    def add(
        self,
        job_argv: Sequence[str],
        *,
        queue: str,
        unique_key: str,
        cwd: PathLike,
        max_retries: int = 0,
        timeout: str = "none",
        kill_grace: str | None = None,
        labels: Sequence[tuple[str, str]] = (),
        env: Mapping[str, str] | None = None,
        pass_env: Sequence[str] = (),
        actor: str | None = None,
    ) -> Result:
        """Queue a job. ``job_argv`` is review-respond's own command line, never untrusted text."""
        if not job_argv:
            raise GatewayError("a job needs an argv")
        args = [
            "add", "--queue", _arg(queue, "queue"), "--unique-key", _arg(unique_key, "unique key"),
            "--cwd", _arg(cwd, "cwd"), "--max-retries", _arg(max_retries, "max retries"),
            "--timeout", _arg(timeout, "timeout"),
        ]  # fmt: skip
        if kill_grace is not None:
            args += ["--kill-grace", _arg(kill_grace, "kill grace")]
        for name, value in (env or {}).items():
            args += ["--env", f"{_arg(name, 'env name')}={value}"]
        for name in pass_env:
            args += ["--pass-env", _arg(name, "env name")]
        args += _labels(labels)
        args += _actor_flags(actor, ())
        return self._run([*args, "--json", "--", *job_argv])

    def show(self, job_id: str) -> Result:
        return self._run(["show", _arg(job_id, "job id"), "--json"])

    def cancel(self, job_id: str, *, actor: str | None = None) -> Result:
        return self._run(["cancel", _arg(job_id, "job id"), *_actor_flags(actor, ()), "--json"])

    def resolve(self, job_id: str, *, as_: str = "done", actor: str | None = None) -> Result:
        args = ["resolve", _arg(job_id, "job id"), "--as", _arg(as_, "status")]
        return self._run([*args, *_actor_flags(actor, ()), "--json"])


class ClaudeSession(_Tool):
    """Used by ``validate`` only, for the sandbox probe."""

    prog = "claude-session"

    def kinds(self, kind: str = "respond") -> Result:
        return self._run(["kinds", "--kind", _arg(kind, "kind"), "--json"])


class Git(_Tool):
    """git with global and system configuration ignored and no prompts.

    TODO(#27): the hardened recipe (``-c`` overrides, hook and fsmonitor neutralization, the clean
    throwaway only) lives in ``hgit.py`` and builds on ``run``; this wrapper only fixes the
    environment so no call can read the user's git configuration.
    """

    prog = "git"

    def environment(self) -> dict[str, str]:
        env = {
            k: v for k, v in self.gateway.env.items() if not k.startswith("GIT_") or k in _GIT_KEEP
        }
        env.update(
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
            GIT_OPTIONAL_LOCKS="0",
        )
        return env

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: PathLike | None = None,
        stdin: str | None = None,
        timeout: Timeout = "default",
    ) -> Result:
        """Run ``git <args>``. The subcommand must not look like an option."""
        if not args:
            raise GatewayError("git needs a subcommand")
        _arg(args[0], "git subcommand")
        return self.gateway.run(
            ["git", *args], stdin=stdin, cwd=cwd, timeout=timeout, env=self.environment()
        )

    def version(self) -> Result:
        return self.gateway.run(["git", "--version"], timeout=15.0, env=self.environment())
