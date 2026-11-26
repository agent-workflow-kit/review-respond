"""The queued reconcile and the finalizer (docs/interface.md: Round states; Stage 2).

Two recoveries for a round whose ``_job`` is gone:

- ``reconcile_queued``: a ``queued`` round whose job never ran (``check`` crashed before the job was
  submitted, the jobsctl job ended without running ``_job``, the detached process died). Under the
  PR's ``pr:`` lease. A live holder of the round's lease (``_job`` running or about to run) or a
  jobsctl job still ``queued``, ``starting``, ``running`` or ``held`` leaves the round alone.
- ``finalize``: a ``running`` round whose ``round:`` holder is verified gone. The round's lease is
  taken by ``Store.try_acquire`` alone: a live holder (even an expired, stalled one) is never taken
  over. The recorded agent (or, without one, the single labelled candidate that passes the checks)
  is read with ``agent-spawn show`` and its last run maps through the same ``Job.finish`` and
  ``Job.check_done`` ``_job`` runs after its ``wait``.

Ending a round removes the files that belong to it (the prompt, the generated profile, the output
of a detached job). Every transition goes through ``rounds.Rounds`` under the round's lease or the
PR's lease; nothing here writes a round row.

Seams: TODO(#37): the ``post-attention`` ``failed`` / ``job_lost`` / ``escalated`` events of the
ending transitions (``extra=``); TODO(#26): the cleanup of throwaways and forks of an ended round;
TODO(#28), TODO(#29): ``Job.check_done`` leaves a round with a valid artifact ``running`` until the
stored diff and the pending record exist.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Final

from . import labels, lineage
from .config import Config
from .errors import CliError
from .gateway import AgentSpawn, Gateway, GatewayError, Jobsctl, Result, Wtctl
from .job import Job, end_of_last_run, parse_show
from .outcomes import RunEnd
from .output import Context
from .placement import SUBMIT_GRACE_SECONDS, Placement, remove_output
from .rounds import RoundRow, Rounds, Transition, get_round
from .store import Lease, Store, holder_gone, round_lease_key

ACTIVE_JOB_STATUSES: Final = ("queued", "starting", "running", "held")
TERMINAL_JOB_STATUSES: Final = ("done", "failed", "canceled")
MAX_WARNED_IDS: Final = 10
HELD_WINDOW_SECONDS: Final = 86400.0
ACTOR: Final = lineage.ACTOR
_SIGNAL_EXITS: Final = (130, 143)


def holder_alive(store: Store, key: str) -> bool:
    """A lease row exists and its holder is not verified gone (a read; nothing is taken)."""
    row = store.read_lease(key)
    return row is not None and not holder_gone(row.pid, row.pid_start, row.boot_id)


@dataclass(frozen=True)
class Reconciled:
    """What a queued reconcile did: ``waiting`` (the job may still run), ``held`` (a live process
    holds the round), ``resubmitted``, ``ended`` or ``unknown`` (jobsctl could not be asked)."""

    action: str
    round: RoundRow | None = None
    placed: str | None = None


class Finalizer:
    """The recoveries of one command (``check`` or ``show``). ``ctx`` needs the log and audit
    writers (``show`` opens them when it runs a finalizer)."""

    def __init__(
        self, ctx: Context, settings: Config, store: Store, *, placement: Placement | None = None
    ) -> None:
        if ctx.log is None:
            raise CliError("internal", "the finalizer needs the log writer")
        self.ctx = ctx
        self.cfg = settings
        self.store = store
        self.log = ctx.log
        self.on_event = ctx.log.lease_callback()
        tokens = {c.token_env for c in settings.hosts.values() if c.token_env}
        from .validate import child_env

        self.plain = Gateway(child_env(dict(os.environ), tokens))
        self.spawn: AgentSpawn = self.plain.agent_spawn
        self.jobsctl: Jobsctl = self.plain.jobsctl
        self.placement = placement
        self._jobs: dict[str, Job] = {}

    # -- helpers -------------------------------------------------------------------------------

    def _now(self) -> float:
        return self.store.clock.now().timestamp()

    def _step(self, round_id: str, step: str, *, level: str = "info") -> None:
        self.log.emit("finalize", {"round": round_id, "step": step}, level=level)

    def _repo(self, pr: str) -> str:
        found = self.store.conn.execute("SELECT repo FROM prs WHERE pr = ?", (pr,)).fetchone()
        if found is None:
            raise CliError("internal", "the round's PR is not stored")
        return str(found[0])

    def _round_ended(self, row: RoundRow) -> None:
        """Remove what belongs to a round that ended without its own job: its detached job's
        output and its prompt and profile files."""
        state = self.store.source
        if state is not None:
            remove_output(state, row.id, self.log)
        try:
            from .prompt import remove_prompt_file
            from .settings import remove_profile_file

            state = self.store.source
            if state is not None:
                for remove in (remove_prompt_file, remove_profile_file):
                    remove(state, row.id)
        except (OSError, CliError):
            self.log.warning("temp_file_left", round=row.id)

    # -- the queued reconcile ------------------------------------------------------------------

    def reconcile_queued(self, row: RoundRow, pr_lease: Lease) -> Reconciled:
        """``reconcile_queued(r)`` of the interface. ``pr_lease`` is the PR's ``pr:`` lease."""
        if row.state != "queued":
            return Reconciled("waiting")
        if holder_alive(self.store, round_lease_key(row.id)):
            return Reconciled("held")  # _job is running or about to run
        now = self._now()
        if row.job_id is None and row.detached_started_at is None:
            if now - row.created_at < SUBMIT_GRACE_SECONDS:
                return Reconciled("waiting")
            return self._resubmit(row, pr_lease)
        if row.job_id is not None:
            verdict = self._jobsctl_verdict(row)
            if verdict != "gone":
                return Reconciled("unknown" if verdict == "unknown" else "waiting")
        elif row.detached_started_at is not None and (
            now - row.detached_started_at <= SUBMIT_GRACE_SECONDS
        ):
            return Reconciled("waiting")
        return self._end_lost(row)

    def _resubmit(self, row: RoundRow, pr_lease: Lease) -> Reconciled:
        """``check`` crashed between the round row and the submission: submit again (the unique
        key makes jobsctl's add idempotent; a detached job takes the round's lease first, so a
        second start of the same round exits 0)."""
        if self.placement is None:
            raise CliError("internal", "the reconcile needs a placement to submit a job")
        placed = self.placement.submit(Rounds(pr_lease, log=self.log), row)
        self._step(row.id, "agent_missing", level="warn")
        return Reconciled("resubmitted", placed=placed.how)

    def _jobsctl_verdict(self, row: RoundRow) -> str:
        """``active`` (the job may still run), ``gone`` (terminal, or jobsctl no longer knows it)
        or ``unknown`` (jobsctl cannot be asked now: nothing is decided)."""
        assert row.job_id is not None
        try:
            result: Result = self.jobsctl.show(row.job_id)
        except GatewayError:
            self.log.warning("jobsctl_unavailable", round=row.id)
            return "unknown"
        doc = result.json
        if result.returncode == 10 and not result.timed_out and not result.overflow:
            if isinstance(doc, dict) and _error_code(doc) == "not_found":
                return "gone"
        if not result.ok or not isinstance(doc, dict):
            self.log.warning("jobsctl_unreadable", round=row.id)
            return "unknown"
        job: dict[str, Any] = doc  # pyright: ignore[reportUnknownVariableType]
        if job.get("id") != row.job_id:
            self.log.warning("jobsctl_unreadable", round=row.id)
            return "unknown"
        status = job.get("status")
        if status in ACTIVE_JOB_STATUSES:
            return "active"
        if status in TERMINAL_JOB_STATUSES:
            return "gone"
        self.log.warning("jobsctl_unreadable", round=row.id)
        return "unknown"

    def _end_lost(self, row: RoundRow) -> Reconciled:
        lease = self.store.try_acquire(round_lease_key(row.id), on_event=self.on_event)
        if lease is None:
            return Reconciled("held")
        try:
            moved: Transition = Rounds(lease, log=self.log).reconcile_lost(row.id)
        finally:
            lease.release()
        if not moved.changed:
            return Reconciled("waiting", moved.row)
        # TODO(#37): post-attention ``job_lost`` (``extra=``) for a new block
        self._step(row.id, "canceled" if moved.row.state == "canceled" else "failed", level="warn")
        self._round_ended(moved.row)
        return Reconciled("ended", moved.row)

    # -- held jobs of escalated rounds ---------------------------------------------------------

    def resolve_held_jobs(self, only: str | None = None) -> int:
        """``jobsctl resolve --as done`` for the held job of a round the row says ``escalated`` (an
        exit-13 job is held by jobsctl until then, and holds a queue slot). Looks at the rounds
        that ended in the last ``HELD_WINDOW_SECONDS`` (a terminal round cannot carry a mark);
        returns how many it resolved."""
        if self.placement is None or not self.placement.jobsctl_present():
            return 0
        where, args = ("", ()) if only is None else (" AND pr = ?", (only,))
        found = self.store.conn.execute(
            "SELECT id, job_id FROM rounds WHERE state = 'escalated' AND job_id IS NOT NULL"
            f" AND ended_at >= ?{where} ORDER BY ended_at, id",
            (self._now() - HELD_WINDOW_SECONDS, *args),
        ).fetchall()
        resolved = 0
        for round_id, job_id in found:
            try:
                shown = self.jobsctl.show(job_id)
                doc = shown.json
                if not shown.ok or not isinstance(doc, dict) or doc.get("status") != "held":  # pyright: ignore[reportUnknownMemberType]
                    continue
                done = self.jobsctl.resolve(job_id, as_="done", actor=ACTOR)
            except GatewayError:
                self.log.warning("jobsctl_unavailable", round=round_id)
                continue
            if done.ok:
                resolved += 1
                self._step(round_id, "job_resolved")
            else:
                self.log.warning("job_resolve_failed", round=round_id)
        return resolved

    # -- the finalizer -------------------------------------------------------------------------

    def finalize(self, round_id: str) -> str:
        """``busy`` when a live process holds the round's lease, else ``done`` once the round was
        looked at under a lease taken over from a verified-gone holder (it may stay ``running``)."""
        lease = self.store.try_acquire(round_lease_key(round_id), on_event=self.on_event)
        if lease is None:
            return "busy"
        try:
            row = get_round(self.store.conn, round_id)
            if row is not None and row.state == "running":
                self._finalize(row, lease)
        finally:
            lease.release()
        return "done"

    def _finalize(self, row: RoundRow, lease: Lease) -> None:
        job = Job.adopt(
            self.ctx, self.cfg, self.store, row, lease, repo=self._repo(row.pr), root=None
        )
        if row.agent_id is None:
            found = self._find_agent(row, lease, job)
            if found is None:
                return  # the round ended job_lost (or the agent list could not be read)
            row = found
            job.agent_id = row.agent_id
        assert row.agent_id is not None
        try:
            shown_result = self.spawn.show(row.agent_id)
        except GatewayError:
            self.log.warning("show_failed", round=row.id)
            self._step(row.id, "left_running")
            return
        if shown_result.returncode == 10 and not shown_result.timed_out:
            self._ended(job, row, RunEnd("wait", 10, started=row.counted))
            return
        shown = parse_show(shown_result, row.agent_id)
        if shown is None:
            self.log.warning("show_failed", round=row.id)
            self._step(row.id, "left_running")
            return
        if shown.live:
            self._step(row.id, "left_running")
            return
        if shown.last is None:
            # nothing ran or nothing is recorded, and the agent is not busy: its run is lost
            self._ended(job, row, RunEnd("wait", 17, started=row.counted))
            return
        end, esc = end_of_last_run(shown.last, started=row.counted)
        if end.exit is None or end.exit in _SIGNAL_EXITS:
            end = RunEnd("wait", 1, started=end.started)  # a signal is never an outcome: internal
        if end.exit == 0:
            job.root = self._throwaway_root(row)
            job.check_done(end, shown)
            self._step(
                row.id, "pending_recorded" if self._state(row.id) == "pending" else "left_running"
            )
        else:
            job.finish(end, esc)
            self._after_end(row)
        self._cleanup(job, row)

    def _state(self, round_id: str) -> str:
        row = get_round(self.store.conn, round_id)
        return "" if row is None else row.state

    def _ended(self, job: Job, row: RoundRow, end: RunEnd) -> None:
        job.finish(end)
        self._after_end(row)
        self._cleanup(job, row)

    def _after_end(self, row: RoundRow) -> None:
        after = get_round(self.store.conn, row.id)
        if after is None:
            return
        step = {"escalated": "escalated", "canceled": "canceled", "failed": "failed"}.get(
            after.state
        )
        if step is not None:
            self._step(row.id, step, level="info" if step != "failed" else "warn")

    def _cleanup(self, job: Job, row: RoundRow) -> None:
        # the prompt and the profile go once the round ended or its agent's run is over
        job.agent_done = True
        job.cleanup_files()
        state = self.store.source
        if state is not None and self._state(row.id) not in ("running", "queued"):
            remove_output(state, row.id, self.log)

    def _throwaway_root(self, row: RoundRow) -> str | None:
        """The round throwaway's root from its wtctl record (None when it cannot be read)."""
        if row.throwaway_id is None:
            return None
        wt: Wtctl = self.plain.wtctl
        try:
            result = wt.show(row.throwaway_id)
        except GatewayError:
            return None
        doc = result.json
        record = doc.get("record", doc) if isinstance(doc, dict) else None
        path = record.get("path") if isinstance(record, dict) else None
        return path if result.ok and isinstance(path, str) and os.path.isabs(path) else None

    def _find_agent(self, row: RoundRow, lease: Lease, job: Job) -> RoundRow | None:
        """The agent of a round that never recorded one, found by its labels and verified. Returns
        the row with the agent recorded, or None when the round ended or nothing could be read."""
        try:
            listed = self.spawn.list(
                labels=[("round", row.id), ("owner", labels.OWNER)],
            )
        except GatewayError:
            self.log.warning("list_failed", round=row.id)
            return None
        doc = listed.json
        agents = doc.get("agents") if isinstance(doc, dict) else None
        if not listed.ok or not isinstance(agents, list):
            self.log.warning("list_failed", round=row.id)
            return None
        root = self._throwaway_root(row) if row.base_agent_id is None else None
        accepted = [
            a["agent_id"]
            for a in agents  # pyright: ignore[reportUnknownVariableType]
            if self._accept(a, row, root)  # pyright: ignore[reportUnknownArgumentType]
        ]
        if len(accepted) == 1:
            moved = Rounds(lease, log=self.log).record_agent(row.id, accepted[0])
            self._step(row.id, "agent_found")
            return moved.row if moved.changed or moved.row.agent_id else None
        self._step(row.id, "agent_missing", level="warn")
        if len(accepted) > 1:
            shown = ",".join(sorted(accepted)[:MAX_WARNED_IDS])
            self.log.warning("ambiguous_fork", round=row.id, key=shown)
        job.finish(RunEnd("job_gone"))
        self._after_end(row)
        self._cleanup(job, row)
        return None

    def _accept(self, c: object, row: RoundRow, root: str | None) -> bool:
        """A listed agent is this round's fork: not recorded for anything else, this round's
        labels (the list filter is not trusted), and the lineage matches: a fork names the round's
        base as its source, a fresh session is a ``respond`` run in the round's throwaway."""
        if not isinstance(c, dict):
            return False
        agent: dict[str, Any] = c  # pyright: ignore[reportUnknownVariableType]
        agent_id = agent.get("agent_id")
        if (
            not lineage.is_agent_id(agent_id)
            or lineage.is_recorded(self.store.conn, agent_id)
            or agent_id == row.base_agent_id
            or not labels.has_label(agent.get("labels"), "round", row.id)
            or not labels.has_label(agent.get("labels"), "owner", labels.OWNER)
        ):
            return False
        if row.base_agent_id is not None:
            return agent.get("forked_from") == row.base_agent_id
        cwd = agent.get("cwd")
        return (
            root is not None
            and agent.get("run_kind") == "respond"
            and isinstance(cwd, str)
            and cwd == os.path.realpath(root)
        )


def _error_code(doc: dict[str, Any]) -> str | None:
    error = doc.get("error")
    code = error.get("code") if isinstance(error, dict) else None  # pyright: ignore[reportUnknownMemberType]
    return code if isinstance(code, str) else None
