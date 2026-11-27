"""`stop <round id>` and the closed-PR sweep (docs/interface.md: stop; Reject, expiry, closed PRs).

``Stopper.stop`` is the interface's pseudocode, run under the PR's ``pr:`` lease the caller holds:

- a terminal round is a no-op (``changed`` false); a ``pending`` round is refused ``pending_exists``
  (``reject`` ends it);
- the cancel flag is set first (``Rounds.request_cancel``: the first reason stays), so the job sees
  it whatever else happens;
- a queued round: ``jobsctl cancel <job>`` (a refusal ``live`` means the job started: the round is
  left to the job, which reads the flag), a round with no job submitted yet, or a detached job
  that is verifiably gone, ends ``canceled``;
- a running round with a recorded agent: ``agent-spawn stop <agent id> --grace G --actor A``, and
  only for an id in the ``forks`` table; a running round without an agent yet is left to its job,
  which sees the flag before it launches the agent.

Nothing is signalled here and nothing is found by name: the only processes involved are the ones
jobsctl and agent-spawn stop themselves. A failed sibling call leaves the flag set (the round ends
through the job, the finalizer or the queued reconcile) and exits 12 ``lower_failed``.

The sweep (``check`` housekeeping) calls the same function with the actor ``auto:review-respond``
and the reason ``pr_closed``. TODO(#37): ``post-attention`` ``pr_closed`` once per ended round;
TODO(#29): the pending record of a closed PR is ended by ``end_record``, not here.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from typing import Any, Final

from . import lineage
from .audit import AuditLog, LogWriter
from .config import Config
from .durations import parse_duration
from .errors import CliError
from .finalizer import holder_alive
from .gateway import Gateway, GatewayError, Result
from .output import Context, Outcome, sanitize_line
from .placement import SUBMIT_GRACE_SECONDS, Placement
from .prstate import round_object
from .rounds import TERMINAL_STATES, RoundRow, Rounds, Transition, get_round, resolve_round_id
from .store import Lease, Store, pr_digest, pr_lease_key, round_lease_key
from .validate import child_env

ACTOR: Final = lineage.ACTOR
PR_LEASE_WAIT: Final = 30.0  # `stop` waits up to 30 s for the PR's lease, then exits 20 busy
STOP_SLACK_SECONDS: Final = 60.0  # the gateway waits the grace plus agent-spawn's 30 s plus this


def stop_grace_seconds(settings: Config) -> int:
    """``G = jobs.kill_grace`` minus 50 s, at least 5 s (the job's own rule)."""
    kill_grace = parse_duration(settings.jobs.kill_grace, flag="jobs.kill_grace").total_seconds()
    return int(max(5, kill_grace - 50))


@dataclass(frozen=True)
class Stopped:
    """``changed``: this call set the cancel flag or ended the round. ``cancel_requested``: the
    stored round carries the flag after the call. ``row`` is the stored round."""

    row: RoundRow
    changed: bool
    cancel_requested: bool


def lower_of(result: Result, what: str) -> dict[str, Any]:
    """The sibling's own error object, or a placeholder when it printed none."""
    doc = result.json
    error = doc.get("error") if isinstance(doc, dict) else None
    if isinstance(error, dict):
        return dict(error)  # pyright: ignore[reportUnknownArgumentType]
    note = "timed out" if result.timed_out else f"exited {result.returncode}"
    return {"code": "unknown", "message": f"{what} {note}"}


def _error_reason(result: Result) -> str | None:
    doc = result.json
    error = doc.get("error") if isinstance(doc, dict) else None
    reason = error.get("reason") if isinstance(error, dict) else None  # pyright: ignore[reportUnknownMemberType]
    return reason if isinstance(reason, str) else None


def _error_code(result: Result) -> str | None:
    doc = result.json
    error = doc.get("error") if isinstance(doc, dict) else None
    code = error.get("code") if isinstance(error, dict) else None  # pyright: ignore[reportUnknownMemberType]
    return code if isinstance(code, str) else None


def _audit_result(error: CliError) -> dict[str, object]:
    status = "refused" if error.code == "refused" else "failed"
    result: dict[str, object] = {"status": status, "exit_code": error.exit_code}
    if error.reason is not None:
        result["reason"] = error.reason
    if error.lower is not None:
        code = error.lower.get("code")
        if isinstance(code, str):
            result["lower_code"] = code
    return result


class Stopper:
    """The stop function of one command (``stop``, or one ``check``'s sweep)."""

    def __init__(
        self,
        settings: Config,
        store: Store,
        gateway: Gateway,
        placement: Placement,
        *,
        actor: str,
        audit: AuditLog,
        log: LogWriter | None,
    ) -> None:
        self.cfg = settings
        self.store = store
        self.gw = gateway
        self.placement = placement
        self.actor = actor
        self.audit = audit
        self.log = log

    def _warn(self, code: str, round_id: str) -> None:
        if self.log is not None:
            self.log.warning(code, round=round_id)

    def _now(self) -> float:
        return self.store.clock.now().timestamp()

    # -- the function -----------------------------------------------------------------------

    def stop(self, lease: Lease, round_id: str, reason: str = "stopped") -> Stopped:
        """``stop(round)`` of the interface under the PR's lease. ``reason`` is ``stopped`` or
        ``pr_closed`` (the sweep)."""
        conn = self.store.conn
        row = get_round(conn, round_id)
        if row is None:
            raise CliError("not_found", "no round matches")
        settled = self._settled(row)
        if settled is not None:
            return settled
        attempt = self.audit.attempted("stop", row.pr, inputs={"round": row.id})
        try:
            done = self._act(lease, row, reason)
        except CliError as error:
            self.audit.outcome(
                attempt, "stop", row.pr, _audit_result(error), inputs={"round": row.id}
            )
            raise
        status = "ok" if done.changed else "noop"
        self.audit.outcome(
            attempt, "stop", row.pr, {"status": status, "exit_code": 0}, inputs={"round": row.id}
        )
        return done

    def _settled(self, row: RoundRow) -> Stopped | None:
        """A terminal round is a no-op and a pending one is refused; None for an active round."""
        if row.state in TERMINAL_STATES:
            self.audit.decision(
                "stop", row.pr, {"status": "noop", "exit_code": 0}, inputs={"round": row.id}
            )
            return Stopped(row, False, row.cancel_requested_at is not None)
        if row.state == "pending":
            error = CliError(
                "refused",
                "the round is pending confirmation: reject it instead",
                reason="pending_exists",
                details={"round": row.id, "hint": "reject"},
            )
            self.audit.decision("stop", row.pr, _audit_result(error), inputs={"round": row.id})
            raise error
        return None

    def _act(self, lease: Lease, row: RoundRow, reason: str) -> Stopped:
        machine = Rounds(lease, log=self.log)
        flagged = machine.request_cancel(row.id, reason)
        changed = flagged.changed
        row = flagged.row
        if row.state == "queued":
            ended = self._queued(machine, row)
            if ended is not None:
                changed = changed or ended.changed
                row = ended.row
        if row.state == "running":
            self._running(row)
        elif row.state == "pending":
            # the round went pending between the check and the flag: nothing was stopped
            raise CliError(
                "refused",
                "the round is pending confirmation: reject it instead",
                reason="pending_exists",
                details={"round": row.id, "hint": "reject"},
            )
        row = get_round(self.store.conn, row.id) or row
        return Stopped(
            row, changed or row.state in TERMINAL_STATES, row.cancel_requested_at is not None
        )

    # -- a queued round ----------------------------------------------------------------------

    def _queued(self, machine: Rounds, row: RoundRow) -> Transition | None:
        """End a queued round whose job is canceled, never submitted or verifiably gone; leave one
        whose job may still run (it reads the flag). Returns the transition, or None."""
        if row.job_id is not None:
            verdict = self._jobsctl_cancel(row)
            if verdict != "gone":
                return None
        elif row.detached_started_at is not None:
            if holder_alive(self.store, round_lease_key(row.id)):
                return None  # the job is running: it reads the flag
            if self._now() - row.detached_started_at <= SUBMIT_GRACE_SECONDS:
                return None  # it may not have taken its lease yet
        # no job submitted yet, or the detached process is gone: the round ends here
        moved = machine.cancel_flagged(row.id)
        return moved

    def _jobsctl_cancel(self, row: RoundRow) -> str:
        """``gone`` (canceled, or jobsctl no longer knows the job) or ``left`` (the job started, or
        jobsctl cannot be asked: the job reads the flag)."""
        assert row.job_id is not None
        try:
            if not self.placement.jobsctl_present():
                self._warn("jobsctl_unavailable", row.id)
                return "left"
            result = self.gw.jobsctl.cancel(row.job_id, actor=self.actor)
        except GatewayError as exc:
            raise CliError(
                "lower_failed",
                "jobsctl cancel was not run",
                lower={"code": "unknown", "message": str(exc)},
            ) from None
        if result.ok:
            return "gone"
        if result.returncode == 10 and not result.timed_out and _error_code(result) == "not_found":
            return "gone"
        if result.returncode == 20 and not result.timed_out and _error_reason(result) == "live":
            return "left"  # it started: the job reads the flag like a running round
        raise CliError(
            "lower_failed", "jobsctl cancel failed", lower=lower_of(result, "jobsctl cancel")
        )

    # -- a running round ---------------------------------------------------------------------

    def _running(self, row: RoundRow) -> None:
        if row.agent_id is None:
            return  # the job sees the flag before it launches the agent
        if not lineage.is_recorded(self.store.conn, row.agent_id):
            # only an id review-respond itself recorded as a fork is ever stopped
            self._warn("stop_unrecorded_agent", row.id)
            return
        grace = stop_grace_seconds(self.cfg)
        try:
            result = self.gw.agent_spawn.stop(
                row.agent_id,
                actor=self.actor,
                grace=f"{grace}s",
                timeout=grace + 30.0 + STOP_SLACK_SECONDS,
            )
        except GatewayError as exc:
            raise CliError(
                "lower_failed",
                "agent-spawn stop was not run",
                lower={"code": "unknown", "message": str(exc)},
            ) from None
        if result.ok or (
            result.returncode == 10 and not result.timed_out and _error_code(result) == "not_found"
        ):
            return  # stopped, or the agent is gone: the job or the finalizer ends the round
        raise CliError(
            "lower_failed", "agent-spawn stop failed", lower=lower_of(result, "agent-spawn stop")
        )


# ---- the command --------------------------------------------------------------------------------


def _document(
    conn: Any, row: RoundRow, *, dry_run: bool, changed: bool, requested: bool
) -> dict[str, Any]:
    return {
        "dry_run": dry_run,
        "round": round_object(conn, row),
        "changed": changed,
        "cancel_requested": requested,
    }


def _human(row: RoundRow, changed: bool, *, dry_run: bool) -> str:
    if row.state in TERMINAL_STATES and not changed:
        text = f"{row.id} is already {row.state}: nothing to stop"
    elif dry_run:
        text = f"{row.id} is {row.state}: would stop it"
    elif row.state in TERMINAL_STATES:
        text = f"{row.id} stopped ({row.state})"
    else:
        text = f"{row.id} stop requested ({row.state}); it ends when its job sees the flag"
    return sanitize_line(text) + "\n"


def _dry_run(ctx: Context, text: str) -> Outcome:
    store = ctx.store
    if store is None:
        raise CliError("not_found", "no round matches")
    row = get_round(store.conn, resolve_round_id(store.conn, text))
    assert row is not None
    if row.state == "pending":
        raise CliError(
            "refused",
            "the round is pending confirmation: reject it instead",
            reason="pending_exists",
            details={"round": row.id, "hint": "reject"},
        )
    payload = _document(
        store.conn, row, dry_run=True, changed=False, requested=row.cancel_requested_at is not None
    )
    return Outcome(payload, _human(row, False, dry_run=True))


def handle(ctx: Context, args: argparse.Namespace) -> Outcome:
    settings, store = ctx.settings, ctx.store
    if settings is None:
        raise CliError("internal", "stop ran without a config")
    if ctx.dry_run:
        return _dry_run(ctx, args.round_id)
    if store is None or store.source is None or ctx.audit is None:
        raise CliError("internal", "stop needs the state directory and its audit file")
    round_id = resolve_round_id(store.conn, args.round_id)
    row = get_round(store.conn, round_id)
    if row is None:
        raise CliError("not_found", "no round matches")
    tokens = {c.token_env for c in settings.hosts.values() if c.token_env}
    gateway = Gateway(child_env(dict(os.environ), tokens))
    placement = Placement(settings, store.source, gateway, config=ctx.config, log=ctx.log)
    on_event = ctx.log.lease_callback() if ctx.log is not None else None
    lease = store.acquire(
        pr_lease_key(pr_digest(row.pr)),
        wait=PR_LEASE_WAIT,
        contended="busy",
        on_event=on_event,
    )
    try:
        stopper = Stopper(
            settings, store, gateway, placement, actor=ctx.actor, audit=ctx.audit, log=ctx.log
        )
        done = stopper.stop(lease, round_id, "stopped")
        lease.renew()  # the answer is only reported while the lease is still ours
    finally:
        lease.release()
    payload = _document(
        store.conn, done.row, dry_run=False, changed=done.changed, requested=done.cancel_requested
    )
    return Outcome(payload, _human(done.row, done.changed, dry_run=False))
