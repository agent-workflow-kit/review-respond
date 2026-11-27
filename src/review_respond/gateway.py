"""Gateway for review-respond: the single boundary for subprocess and network access
and the clock of its process handling.

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
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

PathLike = str | os.PathLike[str]
Timeout = float | None | Literal["default"]  # None: no limit; "default": the gateway default

# The lookbehind keeps a long run of digits from being rescanned from every position.
_VERSION = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)")
VERSION_OUTPUT_CAP = 64 * 1024  # bytes read per stream of a ``--version`` probe
_TERM_GRACE = 2.0  # how long a group gets to exit after SIGTERM
_KILL_WAIT = 2.0  # how long to wait for the pipes after SIGKILL (a member may have escaped)


# Detached children this process started and has not seen end (``Gateway.start_detached``).
_DETACHED: list[subprocess.Popen[bytes]] = []


class GatewayError(Exception):
    """A call was refused before anything started (a malformed or unsafe argument)."""


class SpawnError(GatewayError):
    """The operating system refused to start the program (missing binary, too many arguments)."""


class Clock(Protocol):
    """The time source of the gateway's process handling (waits and timeouts) and of callers that
    take a ``now`` (duration parsing). Tests substitute a manual clock."""

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
    overflow: bool = False  # a stream passed ``max_output``: the program was stopped

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.overflow


def parse_version(text: str) -> tuple[int, int, int] | None:
    """The first ``X.Y.Z`` in ``text`` (a ``--version`` line), or None."""
    match = _VERSION.search(text)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _check_text(text: str, what: str, *, stdin: bool = False) -> None:
    """Refuse a NUL byte or a lone surrogate (it cannot be encoded).

    Stdin is text read back from files: it may carry surrogateescape characters (undecodable
    bytes, U+DC80..U+DCFF) and may hold NUL; any other lone surrogate is refused.
    """
    if "\x00" in text and not stdin:
        raise GatewayError(f"{what} holds a NUL byte")
    try:
        text.encode("utf-8", "surrogateescape" if stdin else "strict")
    except UnicodeEncodeError:
        raise GatewayError(f"{what} is not valid text (a lone surrogate)") from None


def _arg(value: PathLike | int, what: str = "argument") -> str:
    """One argv token that is data for the program, never an option.

    The empty string is allowed here (``--env-allowlist-only ''`` is a documented form); the
    grammars of the callers refuse it where a sibling does.
    """
    text = os.fspath(value) if isinstance(value, os.PathLike) else str(value)
    _check_text(text, what)
    if text.startswith("-") and text != "-":
        raise GatewayError(f"{what} starts with '-': refusing to pass it as an option")
    return text


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


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
        max_output: int | None = None,
    ) -> Result:
        """Run ``argv`` without a shell. ``stdin`` is closed (not inherited) when None.

        With ``max_output`` each output stream is read incrementally and at most that many bytes
        are kept: a stream that passes it stops the whole process group (``Result.overflow``).
        It needs ``stdin=None``.

        Everything is validated before a process starts (``GatewayError``); a refusal by the
        operating system is a ``SpawnError``. Once started, the whole process group is stopped
        and reaped on every way out of this method (a timeout, ``KeyboardInterrupt``, an error
        in ``on_spawn``).
        """
        tokens = self._validate(argv, stdin, cwd, env)
        if max_output is not None and (stdin is not None or max_output < 1):
            raise GatewayError("max_output needs a positive size and no stdin")
        limit = self._timeout(timeout)
        child_env = dict(self.env if env is None else env)
        data = stdin.encode("utf-8", "surrogateescape") if stdin is not None else None
        try:
            proc = subprocess.Popen(
                tokens,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=os.fspath(cwd) if cwd is not None else None,
                env=child_env,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise SpawnError(f"cannot start {tokens[0]!r}: {exc}") from exc
        # start_new_session makes the child its own group leader: pgid == pid.
        timed_out = False
        overflow = False
        try:
            if self._on_spawn is not None:
                self._on_spawn(proc.pid)
            if max_output is not None:
                out, err, timed_out, overflow = self._read_bounded(proc, limit, max_output)
            else:
                try:
                    out, err = proc.communicate(data, timeout=limit)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    out, err = self._stop_group(proc)
        except BaseException:
            if max_output is not None:
                self._stop_bounded(proc, graceful=True)
            else:
                self._stop_group(proc)
            raise
        return self._result(tokens, proc.returncode, out, err, timed_out, overflow)

    def start_detached(
        self,
        argv: Sequence[str],
        *,
        cwd: PathLike,
        out_fd: int,
        env: Mapping[str, str] | None = None,
    ) -> int:
        """Start ``argv`` as a detached process and return its pid without waiting for it.

        The child is a session and process-group leader of its own (nothing a caller or its
        terminal signals reaches it), reads ``/dev/null`` and writes stdout and stderr to
        ``out_fd``; no shell, no inherited descriptor but those, and an environment that is exactly
        ``env`` (default: the gateway's). ``on_spawn`` sees the pid like for any child. The child is
        reaped by a later call of this method if this process is still running by then (a short
        command that exits first leaves it to init): no zombie accumulates in a long-lived caller.
        Raises ``SpawnError`` when the operating system refuses.
        """
        tokens = self._validate(argv, None, cwd, env)
        child_env = dict(self.env if env is None else env)
        for old in [p for p in _DETACHED if p.poll() is not None]:
            _DETACHED.remove(old)
        try:
            proc = subprocess.Popen(
                tokens,
                stdin=subprocess.DEVNULL,
                stdout=out_fd,
                stderr=out_fd,
                cwd=os.fspath(cwd),
                env=child_env,
                start_new_session=True,
                close_fds=True,
            )
        except (OSError, ValueError) as exc:
            raise SpawnError(f"cannot start {tokens[0]!r}: {exc}") from exc
        _DETACHED.append(proc)
        if self._on_spawn is not None:
            self._on_spawn(proc.pid)
        return proc.pid

    def _read_bounded(
        self, proc: subprocess.Popen[bytes], limit: float | None, cap: int
    ) -> tuple[bytes, bytes, bool, bool]:
        """Read both pipes in chunks, keeping at most ``cap`` bytes of each.

        Returns ``(stdout, stderr, timed_out, overflow)``; on a timeout or an overflow the process
        group is stopped and reaped.
        """
        deadline = None if limit is None else time.monotonic() + limit
        streams = {"out": bytearray(), "err": bytearray()}
        sel = selectors.DefaultSelector()
        assert proc.stdout is not None and proc.stderr is not None
        sel.register(proc.stdout, selectors.EVENT_READ, "out")
        sel.register(proc.stderr, selectors.EVENT_READ, "err")
        timed_out = overflow = False
        try:
            while sel.get_map() and not (timed_out or overflow):
                wait = None if deadline is None else deadline - time.monotonic()
                if wait is not None and wait <= 0:
                    timed_out = True
                    break
                for key, _ in sel.select(wait):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        sel.unregister(key.fileobj)
                        continue
                    buf = streams[key.data]
                    room = cap - len(buf)
                    buf += chunk[:room]
                    if len(chunk) > room:
                        overflow = True
                        break
                if not sel.get_map() and not (timed_out or overflow):
                    try:
                        proc.wait(timeout=None if deadline is None else deadline - time.monotonic())
                    except (subprocess.TimeoutExpired, ValueError):
                        timed_out = True
            if timed_out or overflow:
                # the output is capped already: what follows is discarded, never buffered
                self._stop_bounded(proc, graceful=timed_out and not overflow)
        finally:
            sel.close()
            for pipe in (proc.stdout, proc.stderr):
                pipe.close()
        return bytes(streams["out"]), bytes(streams["err"]), timed_out, overflow

    def _timeout(self, timeout: Timeout) -> float | None:
        limit = self.default_timeout if timeout == "default" else timeout
        if limit is None:
            return None
        if isinstance(limit, bool) or not isinstance(limit, int | float):
            raise GatewayError(f"timeout must be a number or None, not {limit!r}")
        if not 0 < limit < float("inf"):
            raise GatewayError(f"timeout must be positive, not {limit!r}")
        return float(limit)

    @staticmethod
    def _validate(
        argv: Sequence[str],
        stdin: str | None,
        cwd: PathLike | None,
        env: Mapping[str, str] | None,
    ) -> tuple[str, ...]:
        if not argv or isinstance(argv, str):
            raise GatewayError("argv must be a non-empty list of strings")
        tokens = tuple(argv)
        for token in tokens:
            if not isinstance(token, str):
                raise GatewayError(f"argv holds a non-string: {token!r}")
            _check_text(token, "argv")
        if not tokens[0]:
            raise GatewayError("the program name is empty")
        if stdin is not None:
            if not isinstance(stdin, str):
                raise GatewayError("stdin must be text")
            _check_text(stdin, "stdin", stdin=True)
        if cwd is not None and not os.path.isdir(cwd):
            raise GatewayError(f"cwd is not a directory: {os.fspath(cwd)!r}")
        for key, value in (env or {}).items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise GatewayError("environment names and values must be strings")
            if not key or "=" in key:
                raise GatewayError(f"bad environment name {key!r}")
            _check_text(key, "environment name")
            _check_text(value, f"environment value of {key}")
        return tokens

    @staticmethod
    def _discard(proc: subprocess.Popen[bytes], seconds: float) -> None:
        """Read and drop the pipes of ``proc`` until both end or ``seconds`` pass (no buffering)."""
        deadline = time.monotonic() + seconds
        with selectors.DefaultSelector() as sel:
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None and not pipe.closed:
                    sel.register(pipe, selectors.EVENT_READ)
            while sel.get_map():
                wait = deadline - time.monotonic()
                if wait <= 0:
                    return
                for key, _ in sel.select(wait):
                    if not os.read(key.fd, 65536):
                        sel.unregister(key.fileobj)

    @classmethod
    def _stop_bounded(cls, proc: subprocess.Popen[bytes], *, graceful: bool) -> None:
        """Stop and reap the group of a ``max_output`` run without ever buffering its output.

        ``graceful`` sends SIGTERM and waits the grace period first (a timeout); otherwise the group
        is killed at once (an overflow, whose output is discarded anyway). Output that arrives
        meanwhile is read and dropped, so a program that floods cannot grow this process.
        """
        pid = proc.pid
        if graceful:
            _signal_group(pid, signal.SIGTERM)
            cls._discard(proc, _TERM_GRACE)
        _signal_group(pid, signal.SIGKILL)
        cls._discard(proc, _KILL_WAIT)
        try:
            proc.wait(timeout=_KILL_WAIT)
        except subprocess.TimeoutExpired:
            pass
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()

    @staticmethod
    def _stop_group(proc: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
        """Signal the process group this module created for ``proc``, reap it, return its output.

        SIGTERM first, SIGKILL after the grace period. Members that ignore SIGTERM or detached their
        output (so the pipes closed when the leader exited) are killed by a last SIGKILL to the
        group. A member that left the group (setsid) may still hold a pipe: the wait for the pipes
        is bounded, and what was read so far is returned.
        """
        pid = proc.pid
        out: bytes | None = None
        err: bytes | None = None
        _signal_group(pid, signal.SIGTERM)
        try:
            out, err = proc.communicate(timeout=_TERM_GRACE)
        except subprocess.TimeoutExpired as exc:
            out, err = exc.stdout, exc.stderr
        # The leader is gone or stuck; either way nothing in the group may outlive the call.
        _signal_group(pid, signal.SIGKILL)
        try:
            out, err = proc.communicate(timeout=_KILL_WAIT)
        except subprocess.TimeoutExpired as exc:
            out, err = exc.stdout or out, exc.stderr or err
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
        try:
            proc.wait(timeout=_KILL_WAIT)
        except subprocess.TimeoutExpired:
            pass
        return out or b"", err or b""

    @staticmethod
    def _result(
        argv: tuple[str, ...],
        code: int,
        out: bytes,
        err: bytes,
        timed_out: bool,
        overflow: bool = False,
    ) -> Result:
        stdout, stderr = _decode(out), _decode(err)
        doc: Any = None
        problem: str | None = None
        if stdout.strip():
            try:
                doc = json.loads(stdout)
            except ValueError as exc:
                problem = str(exc)
            except RecursionError:
                problem = "JSON nested too deeply"
        return Result(argv, code, stdout, stderr, doc, problem, timed_out, overflow)


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
        max_output: int | None = None,
    ) -> Result:
        return self.gateway.run(
            [self.prog, *args], stdin=stdin, cwd=cwd, timeout=timeout, max_output=max_output
        )

    def version(self) -> Result:
        return self.gateway.run(
            [self.prog, "--version"], timeout=15.0, max_output=VERSION_OUTPUT_CAP
        )


# Output bounds for forgectl reads (its output is untrusted): a stream past the cap stops the
# program and the result reports `overflow`.
FORGECTL_OUTPUT_CAP = 8 * 1024 * 1024
FORGECTL_COMMENTS_CAP = 64 * 1024 * 1024
# What ``agent-spawn wait`` and ``show`` and ``wtctl create`` may print: their documents are small;
# a stream that passes it stops the process and the result is ``overflow`` (``internal``).
AGENT_SPAWN_OUTPUT_CAP = 8 * 1024 * 1024
WTCTL_OUTPUT_CAP = 8 * 1024 * 1024
JOBSCTL_OUTPUT_CAP = 8 * 1024 * 1024  # the documents of ``jobsctl`` are small


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
        return self._run([*args, "--json"], max_output=FORGECTL_OUTPUT_CAP)

    def show(self, pr: str) -> Result:
        return self._run(["show", _arg(pr, "pr"), "--json"], max_output=FORGECTL_OUTPUT_CAP)

    def comments(self, pr: str) -> Result:
        return self._run(["comments", _arg(pr, "pr"), "--json"], max_output=FORGECTL_COMMENTS_CAP)

    def access(self, repo: str, user: str) -> Result:
        return self._run(["access", _arg(repo, "repo"), _arg(user, "user"), "--json"])

    def whoami(self, host: str | None = None) -> Result:
        args = ["whoami"]
        if host is not None:
            args += ["--host", _arg(host, "host")]
        return self._run([*args, "--json"], max_output=FORGECTL_OUTPUT_CAP)

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
        return self._run([*args, "--json"], timeout=None, max_output=AGENT_SPAWN_OUTPUT_CAP)

    def show(self, agent_id: str) -> Result:
        return self._run(
            ["show", _arg(agent_id, "agent id"), "--json"], max_output=AGENT_SPAWN_OUTPUT_CAP
        )

    def list(self, *, labels: Sequence[tuple[str, str]] = ()) -> Result:
        args = ["list"]
        for key, value in labels:
            args += ["--label", _arg(f"{key}={value}", "label")]
        return self._run([*args, "--json"])

    def stop(
        self, agent_id: str, *, actor: str, grace: str | None = None, timeout: Timeout = "default"
    ) -> Result:
        """``timeout`` bounds the call: agent-spawn waits up to the grace plus 30 s itself."""
        args = ["stop", _arg(agent_id, "agent id")]
        if grace is not None:
            args += ["--grace", _arg(grace, "grace")]
        return self._run([*args, "--actor", _arg(actor, "actor"), "--json"], timeout=timeout)

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
        return self._run(
            [*args, *_labels(labels), *_actor_flags(actor, ()), "--json"],
            max_output=WTCTL_OUTPUT_CAP,
        )

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
        """Queue a job. ``job_argv`` is review-respond's own command line, never untrusted text.

        ``env`` values travel on argv (``--env NAME=value``), visible to every local user in the
        process list and stored in the job record: they must be non-secret (docs/interface.md
        limits them to ``XDG_*`` paths). A secret goes through ``pass_env``, which names a
        variable jobsctl copies from the environment it runs in.
        """
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
        return self._run([*args, "--json", "--", *job_argv], max_output=JOBSCTL_OUTPUT_CAP)

    def show(self, job_id: str) -> Result:
        """The job's record; only its status is read, so its output tail is not asked for."""
        return self._run(
            ["show", _arg(job_id, "job id"), "--no-output", "--json"],
            max_output=JOBSCTL_OUTPUT_CAP,
        )

    def cancel(self, job_id: str, *, actor: str | None = None) -> Result:
        args = ["cancel", _arg(job_id, "job id"), *_actor_flags(actor, ()), "--json"]
        return self._run(args, max_output=JOBSCTL_OUTPUT_CAP)

    def resolve(self, job_id: str, *, as_: str = "done", actor: str | None = None) -> Result:
        args = ["resolve", _arg(job_id, "job id"), "--as", _arg(as_, "status")]
        return self._run([*args, *_actor_flags(actor, ()), "--json"], max_output=JOBSCTL_OUTPUT_CAP)


class ClaudeSession(_Tool):
    """Used by ``validate`` only, for the sandbox probe."""

    prog = "claude-session"

    def kinds(self, kind: str = "respond") -> Result:
        return self._run(["kinds", "--kind", _arg(kind, "kind"), "--json"])


class Git(_Tool):
    """git with global and system configuration ignored and no prompts.

    ``run`` is for plain subcommands (``git <subcommand> ...``); its first argument must not look
    like an option, which also keeps ``-c``, ``-C`` and ``--exec-path`` out.

    TODO(#27): the hardened recipe (``-c`` overrides, ``-C``, hook and fsmonitor neutralization,
    the clean throwaway only) lives in ``hgit.py``, which builds its own argv and its own
    environment (including any ``GIT_CONFIG_COUNT``/``KEY_n``/``VALUE_n`` set) and calls
    ``Gateway.run`` directly. This wrapper only fixes the environment so no plain call can read
    the user's git configuration.
    """

    prog = "git"

    def environment(self) -> dict[str, str]:
        """The gateway environment without any ``GIT_*`` name, plus the fixed safe settings."""
        env = {k: v for k, v in self.gateway.env.items() if not k.startswith("GIT_")}
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
