"""The hidden ``_job`` command: one round, from ``queued`` to a recorded outcome (docs/interface.md:
Job, Run outcomes; Stage 2).

``review-respond _job <round id>`` takes the round's lease, re-reads the PR and the comments,
delivers the selected comments, creates the round throwaway, writes the prompt and the permission
profile, forks the PR's base agent (or launches a fresh session), records the agent id, waits for
the run and maps its end onto the round (``outcomes.map_run_end`` through ``Rounds.finish_run``).

Boundaries (what this module does not do):

- Where the job runs (jobsctl or detached), the queued reconcile and the finalizer are #24:
  ``Job`` is written so the finalizer can reuse ``finish_run_end``/``check_done`` later.
- A run that ended ``done`` with a valid artifact stops there (``check_done``): the stored diff
  (TODO(#28)), the pending record, comments ``handled``, round ``pending`` and ``post-pending``
  (TODO(#29), TODO(#37)) are the stored-diff issues'. Until then the round stays ``running``
  with its comments ``delivered``; the job removes its prompt and profile files.
- ``post-attention`` and the other hooks are TODO(#37) (the ``extra`` step of each ending
  transition).
- Throwaway removal and fork teardown of an ended round are TODO(#26); a job that dies leaves
  the temp files of its round for the cleanup (TODO(#26)) and the round for the finalizer (#24).

The job holds the ``round:`` lease for its life. It takes the PR's lease only for the PR and
comment read, the record of that read and ``start`` (``_read_and_start``); every later transition
runs under the ``round:`` lease alone: each is one atomic transaction that checks the lease and is
a compare-and-set on the row's ``rev``, so it needs no ``pr:`` lease (docs/interface.md: Leases;
a ``lease_timeout`` after 60 s leaves the round as stored).

SIGTERM sets a flag only; the job acts on it at the next step boundary and, while it waits for the
agent, within a quarter of a second (the ``wait`` call runs in a thread). A SIGTERM before
``start`` leaves the round ``queued`` (exit 1): the queued reconcile (#24) ends it.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import signal
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any, Final

from . import deps, labels, lineage
from .artifact import FrozenRef, check_path_state, validate_artifact
from .audit import LogWriter
from .config import Config, default_config_path
from .detect import Detection, PrFacts
from .durations import parse_duration
from .errors import CliError
from .gateway import AgentSpawn, Gateway, GatewayError, Result
from .handled import HandledSet, body_digest
from .outcomes import STOP_REASONS, RunEnd, map_run_end
from .output import Context, Outcome
from .prompt import (
    BuiltPrompt,
    PromptComment,
    build_prompt,
    remove_prompt_file,
    sanitize,
    write_prompt_file,
)
from .rounds import TERMINAL_STATES, RoundRow, Rounds, Transition, get_round
from .settings import (
    SessionSettings,
    build_session_settings,
    check_throwaway_root,
    remove_profile_file,
    write_profile_file,
)
from .store import (
    Lease,
    StateDir,
    Store,
    pr_digest,
    pr_lease_key,
    round_lease_key,
)
from .validate import child_env

ACTOR: Final = lineage.ACTOR
# TODO(#40): the marker is the string in the bundled plugin's reference file; the skill is
# written in Stage 6, so this documented constant stands in until then.
SKILL_MARKER: Final = "review-respond:respond-to-review:v1"
PR_LEASE_WAIT: Final = 60.0
MAX_NOTE_BYTES: Final = 8192
# The throwaway's path is not known when the comments are selected (it is created after the
# round starts, the comments being delivered only once the round runs). Selection uses a root of
# this size; the real root's SANITIZED length must not be longer (a longer one fails the round
# before the launch), so the real prompt never outgrows the trial one.
SELECTION_ROOT_BYTES: Final = 1024
WAIT_TICK: Final = 0.25
_SIGNAL_EXITS: Final = (130, 143)


def bundled_plugin_dir() -> Path:
    """The bundled plugin: ``review_respond/plugin`` in an installed wheel, ``plugin/`` in a
    source checkout."""
    here = Path(__file__).resolve().parent
    for candidate in (here / "plugin", here.parent.parent / "plugin"):
        if candidate.is_dir():
            return candidate
    raise CliError("internal", "the bundled plugin directory is missing")


# ---- sibling answers -> RunEnd ----------------------------------------------------------------

# The launch phase of an error envelope: what ended the run before claude was spawned.
_LAUNCH_ERRORS: Final = {
    "escalated": "escalated",
    "vetoed": "vetoed",
    "launch_failed": "not-started",
    "no_run": "not-started",
    "tool_missing": "not-started",
}


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}  # pyright: ignore[reportUnknownVariableType]


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def run_end_of(
    command: str, result: Result, *, started: bool = False
) -> tuple[RunEnd, tuple[str | None, str | None]]:
    """The ``RunEnd`` of a finished ``agent-spawn`` process and the report's (reason, summary).

    A process that timed out or overflowed has no exit code worth mapping (``internal``). The
    cancel flag is not read here: ``Rounds.finish_run`` reads it from the round row.
    """
    doc = _dict(result.json)
    phase = outcome = end_reason = None
    escalation: tuple[str | None, str | None] = (None, None)
    if "error" in doc:
        outcome = _LAUNCH_ERRORS.get(str(_dict(doc["error"]).get("code")))
        phase = None if outcome is None else "launch"
    else:
        started = started or doc.get("first_working_at") is not None
        run = _dict(doc.get("run"))
        if run:
            phase, outcome, end_reason = (
                _text(run.get("phase")),
                _text(run.get("outcome")),
                _text(run.get("end_reason")),
            )
            report = _dict(run.get("report"))
            escalation = (_text(report.get("reason")), _text(report.get("summary")))
    code = None if result.timed_out or result.overflow else result.returncode
    return RunEnd(command, code, phase, outcome, end_reason, started=started), escalation


@dataclass(frozen=True)
class ShownRun:
    """What ``agent-spawn show`` says of an agent: its status, whether it is busy and the last
    final run (None while the run is live or pending)."""

    live: bool
    last: dict[str, Any] | None


def parse_show(result: Result, agent_id: str) -> ShownRun | None:
    """None when the answer cannot be used (a failed call or a document of another shape)."""
    doc = result.json
    if not result.ok or not isinstance(doc, dict):
        return None
    agent = _dict(doc.get("agent"))  # pyright: ignore[reportUnknownMemberType]
    runs = doc.get("runs")  # pyright: ignore[reportUnknownMemberType]
    if agent.get("agent_id") != agent_id or not isinstance(runs, list):
        return None
    live = lineage.agent_busy(agent)
    last: dict[str, Any] | None = None
    if runs:
        outcome = _dict(_dict(runs[-1]).get("outcome"))  # pyright: ignore[reportUnknownArgumentType]
        last = outcome or None
    return ShownRun(live, last)


def end_of_last_run(
    last: dict[str, Any], *, started: bool
) -> tuple[RunEnd, tuple[str | None, str | None]]:
    """The ``RunEnd`` of a ``show``'s last run, mapped like a ``wait`` answer (the run's own
    outcome code is the exit code)."""
    report = _dict(last.get("report"))
    code = last.get("exit_code")
    return (
        RunEnd(
            "wait",
            code if isinstance(code, int) and not isinstance(code, bool) else None,
            _text(last.get("phase")),
            _text(last.get("outcome")),
            _text(last.get("end_reason")),
            started=started or last.get("first_working_at") is not None,
        ),
        (_text(report.get("reason")), _text(report.get("summary"))),
    )


def frozen_ref(last: dict[str, Any]) -> tuple[bool, FrozenRef | None]:
    """(``report.implicit``, ``report.artifact``) of a run; a malformed artifact object is None
    (the round fails ``artifact_missing``: nothing usable was reported)."""
    report = _dict(last.get("report"))
    implicit = report.get("implicit") is True
    art = _dict(report.get("artifact"))
    path, size, sha = art.get("path"), art.get("size"), art.get("sha256")
    if (
        isinstance(path, str)
        and isinstance(size, int)
        and not isinstance(size, bool)
        and isinstance(sha, str)
    ):
        return implicit, FrozenRef(path, size, sha)
    return implicit, None


# ---- the job ----------------------------------------------------------------------------------


def _credential_env(environ: Mapping[str, str], pass_env: list[str]) -> dict[str, str]:
    """The environment of forgectl and wtctl: the allow-listed variables and ``jobs.pass_env``
    (which holds their credential variables)."""
    env = child_env(environ, ())
    for name in pass_env:
        if name in environ:
            env[name] = environ[name]
    return env


def _plain_env(environ: Mapping[str, str], tokens: set[str]) -> dict[str, str]:
    """The environment of agent-spawn (whose children are the agent) and git: the allow-listed
    variables only. Never ``jobs.pass_env`` and never a credential variable."""
    return child_env(environ, tokens)


class Job:
    def __init__(self, ctx: Context, settings: Config, store: Store, round_id: str) -> None:
        state = store.source
        if state is None or ctx.log is None or ctx.audit is None:
            raise CliError("internal", "the job needs the state directory and its log files")
        self.ctx = ctx
        self.cfg = settings
        self.store = store
        self.state: StateDir = state
        self.log: LogWriter = ctx.log
        self.rid = round_id
        tokens = {c.token_env for c in settings.hosts.values() if c.token_env}
        pass_env = list(settings.jobs.pass_env)
        self.gw = Gateway(_credential_env(os.environ, pass_env))
        self.spawn_gw = Gateway(_plain_env(os.environ, tokens))  # agent-spawn and git
        self.spawn: AgentSpawn = self.spawn_gw.agent_spawn
        self.config_dir = os.fspath((ctx.config or default_config_path()).parent)
        self.state_dir = os.fspath(state.path)
        kill_grace = parse_duration(
            settings.jobs.kill_grace, flag="jobs.kill_grace"
        ).total_seconds()
        # interface Job: G = kill_grace - 50 s (at least 5 s); the wait gets kill_grace - 15 s
        self.grace_seconds = int(max(5, kill_grace - 50))
        self.wait_budget = max(1.0, kill_grace - 15)
        self.terminated = False
        self.terminated_at = 0.0  # the spawn gateway's monotonic time of the SIGTERM
        self.started = False  # the run reached ``working`` (Counting)
        self.agent_id: str | None = None
        self.agent_done = False  # the agent's run ended: the temp files can go
        self.root: str | None = None
        self.lease: Lease
        self.rounds: Rounds
        self._pcs: list[PromptComment] = []
        self._note: str | None = None
        self.pr = ""
        self.repo = ""

    # -- plumbing ---------------------------------------------------------------------------

    def _on_signal(self, _signum: int, _frame: FrameType | None) -> None:
        self.terminated_at = self.spawn_gw.clock.monotonic()
        self.terminated = True  # acted on at the next step boundary (never inside a transaction)

    def _row(self) -> RoundRow:
        row = get_round(self.store.conn, self.rid)
        if row is None:
            raise CliError("not_found", "no round matches")
        return row

    def _warn(self, code: str) -> None:
        self.log.warning(code, round=self.rid)

    def run(self) -> int:
        on_event = self.log.lease_callback()
        lease = self.store.try_acquire(round_lease_key(self.rid), on_event=on_event)
        if lease is None:
            return 0  # a live holder has the round
        self.lease = lease
        self.rounds = Rounds(lease, log=self.log)
        previous: Any = None
        installed = False
        try:
            previous = signal.signal(signal.SIGTERM, self._on_signal)
            installed = True
        except ValueError:  # not the main thread (never for the real command)
            pass
        try:
            return self._run(on_event)
        finally:
            try:
                self._cleanup_files()
            finally:
                if installed:
                    signal.signal(signal.SIGTERM, previous)
                lease.release()

    def _cleanup_files(self) -> None:
        """Remove the prompt and profile of the round once nothing needs them: the round ended,
        or the job never launched an agent, or the agent's run ended. A live agent left to the
        finalizer keeps them (TODO(#24): the finalizer removes them when it ends the round)."""
        try:
            row = get_round(self.store.conn, self.rid)
        except Exception:  # the database is unreadable: leave the files for the cleanup
            return
        if row is None or row.state in TERMINAL_STATES or row.agent_id is None or self.agent_done:
            for remove in (remove_prompt_file, remove_profile_file):
                try:
                    remove(self.state, self.rid)
                except (OSError, CliError):
                    self._warn("temp_file_left")

    # -- queued ----------------------------------------------------------------------------

    def _run(self, on_event: Any) -> int:
        row = self._row()
        if row.state != "queued":
            return 0
        if row.cancel_requested_at is not None:
            self.rounds.cancel_flagged(self.rid)  # TODO(#37): post-attention extra
            return 0
        deps.require(self.gw, "forgectl", "wtctl")
        deps.require(self.spawn_gw, "agent-spawn")
        stored = self.store.conn.execute(
            "SELECT repo, digest FROM prs WHERE pr = ?", (row.pr,)
        ).fetchone()
        if stored is None:
            raise CliError("internal", "the round's PR is not stored")
        self.pr, (self.repo, digest) = row.pr, stored
        prepared = self._prepare_local(row, digest)
        if prepared is not None:
            return prepared
        pr_lease = self.store.acquire(
            pr_lease_key(pr_digest(self.pr)),
            wait=PR_LEASE_WAIT,
            contended="lease_timeout",
            on_event=on_event,
        )
        try:
            started = self._read_and_start(pr_lease, row)
        finally:
            pr_lease.release()
        if isinstance(started, int):
            return started
        try:
            return self._running(started)
        except CliError as error:
            if error.code != "lease_lost":
                self._fail_internal()
            raise
        except Exception:  # an OSError of a file write, a bug: the round must not stay running
            self._fail_internal()
            return 1

    def _prepare_local(self, row: RoundRow, digest: str) -> int | None:
        """What needs no sibling: the settings and the stored note. A failure ends the round
        while it is still queued and its comments untouched."""
        try:
            build_session_settings(
                self.cfg, self.repo, config_dir=self.config_dir, state_dir=self.state_dir
            )
        except CliError as error:
            return self._queued_failed("settings_invalid", error)
        if row.note_sha256 is not None:
            try:
                raw = self.state.get_file(digest, row.note_sha256)
                if len(raw) > MAX_NOTE_BYTES:
                    raise CliError("usage", "the stored note is too large")
                self._note = raw.decode("utf-8")
            except CliError as error:
                return self._queued_failed("note_unreadable", error)
            except UnicodeDecodeError:
                return self._queued_failed("note_unreadable")
        return None

    def _queued_failed(self, why: str, error: CliError | None = None) -> int:
        self._warn(why)
        if self._row().cancel_requested_at is not None:
            self.rounds.cancel_flagged(self.rid)  # the stop flag wins; TODO(#37): extra
            return 0
        self.rounds.fail_queued(self.rid)  # TODO(#37): post-attention extra
        if error is not None and error.code == "state_unsafe":
            raise error
        return 1

    # -- the read ----------------------------------------------------------------------------

    def _failures(self) -> int:
        return int(
            self.store.conn.execute(
                "SELECT read_failures FROM prs WHERE pr = ?", (self.pr,)
            ).fetchone()[0]
        )

    def _count_failure(self) -> None:
        with self.lease.transaction() as conn:
            conn.execute(
                "UPDATE prs SET read_failures = read_failures + 1, rev = rev + 1 WHERE pr = ?",
                (self.pr,),
            )

    def _read_failed(self, *, counted_by_detect: bool, before: int) -> int:
        if not counted_by_detect or self._failures() == before:
            self._count_failure()
        self.rounds.cancel_read_failed(self.rid)  # TODO(#37): post-attention extra
        return 0

    def _ref_ok(self, ref: str) -> bool:
        if (
            not ref
            or len(ref) > 1024
            or ref.startswith("-")
            or "@{" in ref
            or any(ord(c) < 32 or c == "\x7f" for c in ref)
        ):
            return False
        try:
            # no credentials: git gets the plain environment
            return self.spawn_gw.git.run(["check-ref-format", "--branch", ref]).returncode == 0
        except GatewayError:
            return False

    def _read_and_start(self, pr_lease: Lease, row: RoundRow) -> int | RoundRow:
        """The fresh PR and comment read and ``queued -> running``. An int is the job's exit code
        (the round ended or was taken away); a row is the running round."""
        det = Detection(
            self.gw,
            pr_lease,
            self.cfg,
            log=self.log,
            on_read=HandledSet(pr_lease).record_read,
        )
        before = self._failures()
        try:
            ref = det.refresh_pr(self.pr)
        except CliError as error:
            if error.code != "lower_failed":
                raise
            return self._read_failed(counted_by_detect=False, before=before)
        facts: PrFacts | None = ref.facts
        if not ref.account_known or facts is None:
            return self._read_failed(counted_by_detect=False, before=before)
        if facts.state != "open":
            self.rounds.cancel_pr_closed(self.rid)
            return 0
        if ref.not_yours or facts.is_fork or not self._ref_ok(facts.head_ref):
            self.rounds.cancel_nothing(self.rid)
            return 0
        try:
            read = det.read_comments(self.pr)
        except CliError as error:
            if error.code == "lower_failed":
                return self._read_failed(counted_by_detect=True, before=before)
            if error.code == "refused" and error.reason == "pr_closed":
                self.rounds.cancel_pr_closed(self.rid)
                return 0
            if error.code == "refused":
                self.rounds.cancel_nothing(self.rid)
                return 0
            raise
        by_id = {c.id: c for c in read.comments}
        picked: list[PromptComment] = []
        for rec in HandledSet(pr_lease).records(self.pr):
            c = by_id.get(rec.id)
            if c is None or not rec.eligible:
                continue
            if rec.state == "unhandled" or (rec.state == "seen" and row.include_existing):
                picked.append(
                    PromptComment(
                        id=rec.id,
                        body=c.body,
                        settle_ts=rec.body_seen_at,
                        author_login=c.author_login,
                        edited=rec.edited,
                        thread_id=c.thread_id,
                        reply_to=c.reply_to,
                        outdated=c.thread_outdated,
                        path=c.path,
                        line=c.line,
                        side=c.side,
                    )
                )
        if not picked:
            self.rounds.cancel_nothing(self.rid)
            return 0
        trial = self._build("/" + "x" * (SELECTION_ROOT_BYTES - 1), picked)
        if trial.status != "ok":
            return self._queued_failed("prompt_over_limit")
        chosen = {i: c for c in picked for i in [c.id]}
        selected = [chosen[i] for i in trial.included]
        if self.terminated:
            return 1  # the round stays queued (TODO(#24): the queued reconcile ends it)
        latest = self._row()
        if latest.cancel_requested_at is not None:
            self.rounds.cancel_flagged(self.rid)
            return 0
        moved = self.rounds.start(
            self.rid,
            [c.id for c in selected],
            head_sha=facts.head_sha,
            expected={c.id: body_digest(c.body) for c in selected},
        )
        after = moved.row
        if not moved.changed or after.state != "running":
            return 0
        self._pcs = [c for c in selected if c.id in after.comment_ids]
        return after

    def _build(self, root: str, comments: list[PromptComment]) -> BuiltPrompt:
        return build_prompt(
            root=root,
            comments=comments,
            note=self._note,
            limit_bytes=self.cfg.limits.prompt_bytes,
        )

    # -- running -----------------------------------------------------------------------------

    def _fail_internal(self) -> None:
        """An unexpected failure after the round started: end it ``failed`` (``internal``)."""
        self._warn("job_internal")
        try:
            self.rounds.to_failed(self.rid, "internal", counted=self.started)
        except Exception:
            self._warn("job_internal_unrecorded")

    def _finish(self, end: RunEnd, escalation: tuple[str | None, str | None] = (None, None)) -> int:
        """``finish_round`` for a run that did not end ``done``: apply the table, answer the
        job's exit code. TODO(#37): the ``post-attention`` extra step."""
        moved: Transition = self.rounds.finish_run(self.rid, end, escalation=escalation)
        self.agent_done = True
        if not moved.changed:
            return 0
        flagged = dataclasses.replace(
            end, cancel_requested=moved.row.cancel_requested_at is not None
        )
        code = map_run_end(flagged).job_exit
        return 1 if code is None else code

    def _failed_internal(self) -> int:
        self.rounds.to_failed(self.rid, "internal", counted=self.started)
        return 1

    def _running(self, row: RoundRow) -> int:
        if self.terminated:
            return self._finish(RunEnd("sigterm", started=self.started))
        created = self._create_throwaway(row)
        if isinstance(created, int):
            return created
        tw_id, root = created
        prompt_file, profile_file, base = self._prepare_session(row, root)
        if self.terminated:
            return self._finish(RunEnd("sigterm", started=self.started))
        if self._row().cancel_requested_at is not None:
            self.rounds.to_canceled(self.rid, counted=False)
            self.agent_done = True
            return 0
        launch = self._launch(row, root, base, prompt_file, profile_file)
        end, esc = run_end_of("launch" if base is None else "resume", launch)
        if end.exit != 0:
            return self._finish(end, esc)
        agent_id = self._record_agent(row, launch, base, tw_id)
        if agent_id is None:
            return self._failed_internal()
        self.started = self.started or end.started
        if end.started:
            self.rounds.note_started(self.rid)
        return self._wait_and_finish(agent_id)

    def _create_throwaway(self, row: RoundRow) -> tuple[str, str] | int:
        assert row.head_sha is not None
        try:
            result = self.gw.wtctl.create_throwaway(
                repo=self.repo,
                ref=row.head_sha,
                labels=labels.throwaway_labels(role="round", round_id=self.rid).labels,
                actor=ACTOR,
            )
            # an answer past the cap is not an exit code of wtctl: `internal`
            code = None if result.overflow else (0 if result.ok else (result.returncode or 1))
        except GatewayError:
            result, code = None, 1
        if code != 0 or result is None:
            self._warn("throwaway_failed")
            return self._finish(RunEnd("wtctl_create", exit=code))
        doc = _dict(result.json)
        record = _dict(doc.get("record"))
        tw_id, path = _text(record.get("id")), _text(record.get("path"))
        if record.get("object_type") != "throwaway" or tw_id is None or path is None:
            self._warn("throwaway_unreadable")
            return self._failed_internal()
        try:
            # recorded before anything is judged: a refused throwaway is left for the cleanup (#26)
            self.rounds.record_throwaway(self.rid, tw_id)
        except ValueError:  # not a plain identifier: nothing to record
            self._warn("throwaway_unreadable")
            return self._failed_internal()
        root = self._usable_root(row, doc, record, path)
        if root is None:
            self._warn("throwaway_unusable")
            return self._failed_internal()
        self.root = root
        return tw_id, root

    def _usable_root(
        self, row: RoundRow, doc: dict[str, Any], record: dict[str, Any], path: str
    ) -> str | None:
        """The throwaway's root when its wtctl record is this round's own and the root is usable,
        else None. Fields the documented record shape leaves null (``ref``, ``base_dir``) are not
        compared."""
        want = labels.throwaway_labels(role="round", round_id=self.rid).labels
        if doc.get("reused") is True or not all(
            labels.has_label(record.get("labels"), k, v) for k, v in want
        ):
            return None
        ref = record.get("ref")
        if ref is not None and ref != row.head_sha:
            return None
        base = record.get("base_dir")
        if isinstance(base, str) and not Path(os.path.realpath(path)).is_relative_to(
            os.path.realpath(base)
        ):
            return None
        try:
            root = check_throwaway_root(path, config_dir=self.config_dir, state_dir=self.state_dir)
        except CliError:
            return None
        if len(sanitize(root).encode()) > SELECTION_ROOT_BYTES:
            return None  # the real prompt could outgrow the one the comments were selected with
        return root

    def _prepare_session(self, row: RoundRow, root: str) -> tuple[Path, Path, str | None]:
        base = row.base_agent_id
        if base is None:
            if not self.cfg.repo_settings(self.repo).respond_fresh:
                raise CliError("internal", "the round has no base agent and no fresh opt-in")
        elif not lineage.is_agent_id(base):
            raise CliError("internal", "the round's base agent id is not valid")
        settings = build_session_settings(
            self.cfg,
            self.repo,
            config_dir=self.config_dir,
            state_dir=self.state_dir,
            throwaway=root,
        )
        built = self._build(root, self._pcs)
        if built.status != "ok" or set(built.included) != {c.id for c in self._pcs}:
            raise CliError("internal", "the prompt no longer fits the delivered comments")
        prompt_file = write_prompt_file(self.state, self.rid, built.text)
        profile_file = write_profile_file(
            self.state, self.rid, settings.profile, config_dir=self.config_dir
        )
        self._settings = settings
        return prompt_file, profile_file, base

    _settings: SessionSettings

    def _launch(
        self, row: RoundRow, root: str, base: str | None, prompt: Path, profile: Path
    ) -> Result:
        fork = labels.fork_labels(pr=self.pr, repo_id=self.repo, round_id=self.rid)
        if fork.warnings:
            self._warn("label_skipped")
        kwargs = self._settings.agent_spawn_kwargs(
            plugin_dir=bundled_plugin_dir(), prompt_file=prompt, permission_profile=profile
        )
        try:
            if base is not None:
                return self.spawn.resume_fork(
                    base, cwd=root, labels=fork.labels, actor=ACTOR, **kwargs
                )
            return self.spawn.launch(
                cwd=root, repo=self.repo, labels=fork.labels, actor=ACTOR, **kwargs
            )
        except GatewayError:
            return Result(argv=(), returncode=1, stdout="", stderr="", timed_out=True)

    def _record_agent(
        self, row: RoundRow, launch: Result, base: str | None, tw_id: str
    ) -> str | None:
        """The agent id of the job's own ``resume``/``launch`` answer (never a search): it must be
        a well-formed id, not the base, and carry this round's label. Recorded (with its forks
        row) before anything waits on it."""
        agent = _dict(_dict(launch.json).get("agent"))
        agent_id = agent.get("agent_id")
        if (
            not lineage.is_agent_id(agent_id)
            or agent_id == base
            or not labels.has_label(agent.get("labels"), "round", self.rid)
        ):
            self._warn("agent_unusable")
            if lineage.is_agent_id(agent_id) and agent_id != base:
                assert isinstance(agent_id, str)
                self._stop(agent_id)  # a session was started: do not leave it running
            return None
        assert isinstance(agent_id, str)
        self.rounds.record_agent(self.rid, agent_id, throwaway_id=tw_id)
        self.agent_id = agent_id
        return agent_id

    # -- wait ----------------------------------------------------------------------------------

    def _stop(self, agent_id: str) -> None:
        """``agent-spawn stop`` of an agent id the job's own ``resume``/``launch`` answered (never a
        searched id)."""
        audit = self.ctx.audit
        assert audit is not None
        attempt = audit.attempted("stop", self.pr, inputs={"round": self.rid})
        try:
            res = self.spawn.stop(agent_id, actor=ACTOR, grace=f"{self.grace_seconds}s")
            code: int | None = res.returncode if 0 <= res.returncode <= 255 else None
            status = "ok" if res.ok else "failed"
        except GatewayError:
            code, status = None, "failed"
        audit.outcome(attempt, "stop", self.pr, {"status": status, "exit_code": code})

    def _wait(self, agent_id: str, deadline: float | None = None) -> Result | None:
        """``agent-spawn wait`` in a thread, so a SIGTERM is acted on while it blocks. ``deadline``
        (the spawn clock's monotonic time) is set when the agent was already stopped; a SIGTERM
        during the wait stops the agent and sets it to the receipt time plus the wait budget. None:
        the wait did not return by the deadline."""
        box: list[Result | BaseException] = []

        def target() -> None:
            try:
                box.append(self.spawn.wait(agent_id))
            except BaseException as exc:
                box.append(exc)

        thread = threading.Thread(target=target, name="wait", daemon=True)
        thread.start()
        clock = self.spawn_gw.clock
        while thread.is_alive():
            thread.join(WAIT_TICK)
            if self.terminated and deadline is None:
                deadline = self.terminated_at + self.wait_budget
                self._stop(agent_id)
            # the stop takes time: the wait may have returned meanwhile
            if deadline is not None and clock.monotonic() > deadline and thread.is_alive():
                self._warn("wait_abandoned")
                return None
        got = box[0]
        if isinstance(got, BaseException):
            if isinstance(got, GatewayError):
                return Result(argv=(), returncode=1, stdout="", stderr="", timed_out=True)
            raise got
        return got

    @staticmethod
    def _is_stop_end(end: RunEnd) -> bool:
        """The wait ended because the agent was stopped (or was signaled), or has no usable
        result: after a SIGTERM that is the job's own ``sigterm`` end."""
        return (
            end.exit is None
            or end.exit in _SIGNAL_EXITS
            or (end.exit == 15 and end.end_reason in STOP_REASONS)
        )

    def _wait_and_finish(self, agent_id: str) -> int:
        deadline: float | None = None
        if self.terminated:
            deadline = self.terminated_at + self.wait_budget
        elif self._row().cancel_requested_at is not None:
            deadline = self.spawn_gw.clock.monotonic() + self.wait_budget
        if deadline is not None:
            self._stop(agent_id)  # a `stop` or a SIGTERM before the agent was recorded
        res = self._wait(agent_id, deadline)
        end: RunEnd | None = None
        esc: tuple[str | None, str | None] = (None, None)
        if res is not None:
            end, esc = run_end_of("wait", res, started=self.started)
        if self.terminated and (end is None or self._is_stop_end(end)):
            return self._finish(RunEnd("sigterm", started=self.started))
        if end is None:
            return self._finish(RunEnd("wait", None, started=self.started))
        if end.exit in _SIGNAL_EXITS:
            shown = parse_show(self._show(agent_id), agent_id)
            if shown is None or shown.live or shown.last is None:
                self._warn("left_running")
                return 0  # the finalizer (#24) maps the run once it ended
            end, esc = end_of_last_run(shown.last, started=self.started)
            return self._after_wait(end, esc, shown)
        if end.exit == 0:
            shown = parse_show(self._show(agent_id), agent_id)
            return self._after_wait(end, esc, shown)
        return self._finish(end, esc)

    def _show(self, agent_id: str) -> Result:
        try:
            return self.spawn.show(agent_id)
        except GatewayError:
            return Result(argv=(), returncode=1, stdout="", stderr="", timed_out=True)

    def _after_wait(
        self,
        end: RunEnd,
        esc: tuple[str | None, str | None],
        shown: ShownRun | None,
    ) -> int:
        if end.exit != 0:
            return self._finish(end, esc)
        return self.check_done(end, shown)

    def check_done(self, end: RunEnd, shown: ShownRun | None) -> int:
        """``finish_round`` for a run that ended ``done`` (the finalizer's too): leave a live
        agent to the finalizer, validate the artifact; a failure ends the round, a valid artifact
        stops here (module docstring: TODO(#28), TODO(#29))."""
        outcome = end.outcome
        if outcome is None and shown is not None and shown.last is not None:
            outcome = _text(shown.last.get("outcome"))
        if outcome not in (None, "done"):
            # exit 0 of wait is the outcome `done`: any other is `internal` (Run outcomes)
            return self._finish(dataclasses.replace(end, outcome=outcome))
        if shown is None:
            self._warn("show_failed")
            return self._failed_internal()
        if shown.live:
            self._warn("left_running")
            return 0
        if shown.last is None:
            return self._finish(dataclasses.replace(end, artifact_failure="artifact_missing"))
        implicit, ref = frozen_ref(shown.last)
        row = self._row()
        result = validate_artifact(
            implicit=implicit,
            artifact=ref,
            delivered_ids=row.comment_ids,
            marker=SKILL_MARKER,
            reply_bytes=self.cfg.limits.reply_bytes,
        )
        if result.ok and result.artifact is not None and self.root is not None:
            result = check_path_state(
                Path(self.root),
                changed=result.artifact.changed_files,
                deleted=result.artifact.deleted_files,
            )
        if not result.ok:
            self._warn(result.failure or "artifact_invalid")
            return self._finish(
                dataclasses.replace(end, artifact_failure=result.failure, detail=result.detail)
            )
        # TODO(#28): build the stored diff in a clean checkout (empty_result, diff_failed).
        # TODO(#29): to_pending with the pending record; then label the throwaway pending=<id>.
        # TODO(#37): post-pending. Until then the round stays running, comments delivered.
        self.agent_done = True
        self._warn("artifact_validated")
        return 0


def handle(ctx: Context, args: argparse.Namespace) -> Outcome:
    from .rounds import is_round_id

    round_id: str = args.round_id
    if not is_round_id(round_id):
        raise CliError("usage", "a round id is rd- plus 8 characters of a-z and 2-7")
    if ctx.settings is None or ctx.store is None:
        raise CliError("internal", "the job needs the config and the state")
    return Outcome(exit_code=Job(ctx, ctx.settings, ctx.store, round_id).run())
