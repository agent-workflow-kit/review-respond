"""`check`: the periodic pass, steps 1 to 3, and `check --pr` (docs/interface.md: check).

Under the `pass` lease it lists your PRs per host, reads their comments (each read under that PR's
lease, taken without waiting), records them in the handled set and evaluates the due rule per PR.
It reports which PRs are due and starts nothing.

Seams owned by later issues:

- Step 4 (#24): a round is started for each due PR, oldest due first (the oldest time an eligible
  comment became unhandled), while `jobs.max_running` queued plus running rounds are not reached;
  a due PR over the cap stays `waiting` (`capacity`), marked `DUE` in the human output. The job is
  placed by `placement.Placement` (jobsctl or detached). TODO(#37): the `pre-round` hooks run
  between the due rule and the round row; a PR whose hooks vetoed or escalated is `waiting`.
- Step 5 (#24): housekeeping reconciles queued rounds, finalizes running rounds whose job is gone
  and resolves held jobs of escalated rounds (`finalizer.py`). The rounds of closed or merged PRs
  are stopped first, by the same function as `stop` (`stop.py`, actor `auto:review-respond`,
  `cancel_reason: pr_closed`) under the PR's lease taken without waiting; TODO(#37):
  `post-attention` `pr_closed`. TODO(#29): expiry and the `remote_moved` comparison; TODO(#26):
  the cleanup of throwaways.
- Before the due rule, a PR with eligible comments and no round or record in progress gets
  `lineage.resolve_base` (agent-spawn `show`/`list`, wtctl `show`): it stores `prs.base_agent` and
  the `agent_busy` observation the rule reads. When agent-spawn cannot be read the PR reports
  `lower_failed`, is not due and nothing is set or cleared for it. Forks marked for teardown are
  torn down at the end of a full pass (`lineage.teardown_pass`); TODO(#37): `teardown_refused`
  fires `post-attention` (it is logged as a warning meanwhile).
- `RoundFacts` come from the rounds table (`rounds.round_facts`); TODO(#29): the open record
  comes from the records table; `pending_state` log events belong to #29.
- Step 6 (hook delivery) arrives with TODO(#37).
"""

from __future__ import annotations

import argparse
import contextlib
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from . import due, lineage, prstate, rounds
from .audit import format_ts
from .config import Config
from .detect import Detection, HostResult, StoredPr, get_pr
from .errors import EXIT_FAILED, EXIT_OK, CliError
from .finalizer import Finalizer
from .gateway import AgentSpawn, Gateway, GatewayError, Wtctl
from .handled import HandledSet
from .output import Context, Outcome, sanitize_line
from .placement import Placement
from .prref import host_of, parse_pr
from .stop import ACTOR as STOP_ACTOR
from .stop import Stopper
from .store import Lease, Store, pass_lease_key, pr_digest, pr_lease_key
from .validate import child_env


@dataclass
class _PrResult:
    pr: str
    state: str
    lease_held: bool = False
    errors: list[str] = field(default_factory=lambda: [])
    due: bool = False
    why: str = ""
    started_round: str | None = None
    oldest_since: float = 0.0  # the oldest time an eligible comment became unhandled (step 4)

    def document(self) -> dict[str, Any]:
        return {
            "pr": self.pr,
            "state": self.state,
            "started_round": self.started_round,
            "lease_held": self.lease_held,
            "errors": list(dict.fromkeys(self.errors)),
        }


@dataclass
class _Report:
    hosts: list[dict[str, Any]] = field(default_factory=lambda: [])
    prs: list[_PrResult] = field(default_factory=lambda: [])
    error: CliError | None = None
    already_running: bool = False
    dry_run: bool = False
    rounds_started: int = 0

    def payload(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "already_running": self.already_running,
            "dry_run": self.dry_run,
            "hosts": self.hosts,
            "prs": [p.document() for p in self.prs],
        }
        if self.error is not None:
            doc["error"] = self.error.body()
        return doc

    def human(self) -> str:
        if self.already_running:
            return "check: another pass is running\n"
        lines: list[str] = []
        for host in self.hosts:
            code = "" if host["ok"] else f" FAILED ({host['code']})"
            extra = "".join(f" [{w}]" for w in host["warnings"])
            lines.append(f"host {host['host']}{code}{extra}")
        for item in self.prs:
            mark = (
                (f" started {item.started_round}" if item.started_round else "")
                + (" DUE" if item.due and item.started_round is None else "")
                + (" lease_held" if item.lease_held else "")
            )
            errors = "".join(f" !{e}" for e in item.errors)
            lines.append(f"{item.pr} {item.state}{mark}{errors}")
        due_count = sum(1 for p in self.prs if p.due and p.started_round is None)
        lines.append(
            f"check: {len(self.prs)} PRs, {self.rounds_started} rounds started, {due_count} due"
        )
        if self.error is not None:
            lines.append(f"error: {self.error.code}: {self.error.message}")
        return "".join(sanitize_line(line) + "\n" for line in lines)


class _Pass:
    """One `check` run that holds the `pass` lease."""

    def __init__(self, ctx: Context, settings: Config, store: Store, lease: Lease) -> None:
        self.ctx = ctx
        self.settings = settings
        self.store = store
        self.lease = lease
        tokens = {c.token_env for c in settings.hosts.values() if c.token_env}
        self.gateway = Gateway(child_env(dict(os.environ), tokens))
        self.spawn = AgentSpawn(self.gateway)
        self.wtctl = Wtctl(self.gateway)
        self.on_event = ctx.log.lease_callback() if ctx.log is not None else None
        self.leases: dict[str, Lease] = {}  # the PR leases held right now
        self.report = _Report()  # what the pass has found so far (printed if it stops at exit 12)
        self.before = self._states()
        if store.source is None:
            raise CliError("internal", "check needs the state directory")
        self.placement = Placement(
            settings, store.source, self.gateway, config=ctx.config, log=ctx.log
        )
        self.results: dict[str, _PrResult] = {}
        self.fresh: set[str] = set()  # the rounds this pass started: housekeeping leaves them be

    def now(self) -> float:
        return self.store.clock.now().timestamp()

    def _states(self) -> dict[str, str]:
        now = self.now()
        return {
            p.pr: prstate.derive(self.store.conn, self.settings, p.pr, now).state
            for p in prstate.stored_prs(self.store.conn)
        }

    # -- the PR lease ---------------------------------------------------------------------

    @contextlib.contextmanager
    def scope(self, pr: str) -> Iterator[bool]:
        """The PR's lease, taken without waiting (False: another command holds it)."""
        held = self.store.try_acquire(pr_lease_key(pr_digest(pr)), on_event=self.on_event)
        if held is None:
            yield False
            return
        self.leases[pr] = held
        try:
            yield True
        finally:
            self.leases.pop(pr, None)
            held.release()

    def record_read(self, stored: StoredPr, read: Any) -> None:
        # the PR lease serializes the reads of one PR (HandledSet.record_read); TODO(#29):
        # record_open comes from the records table
        HandledSet(self.leases[stored.pr]).record_read(stored, read)

    def detection(self) -> Detection:
        return Detection(
            self.gateway,
            self.lease,
            self.settings,
            log=self.ctx.log,
            on_read=self.record_read,
            pr_scope=self.scope,
        )

    # -- the due rule ---------------------------------------------------------------------

    def evaluate(self, pr: str, pr_lease: Lease | None, errors: list[str]) -> _PrResult:
        """The due rule for one PR under its lease (None: the lease is held elsewhere, the state
        is only derived and nothing is written), then the derived state."""
        self.lease.renew()  # a lost pass lease is exit 1 lease_lost, never a quiet success
        result = _PrResult(pr, "idle", lease_held=pr_lease is None, errors=list(errors))
        self.results[pr] = result
        conn = self.store.conn
        open_pr = get_pr(conn, pr)
        if open_pr is None:
            return result
        if pr_lease is not None and open_pr.host_state == "open":
            if not self.refresh_base(open_pr, pr_lease):
                result.errors.append("lower_failed")
                result.state = prstate.derive(conn, self.settings, pr, self.now()).state
                self._log_state(result)
                return result
            inputs = due.load_inputs(
                conn, self.settings, pr, now=self.now(), facts=rounds.round_facts(conn, pr)
            )
            applied = due.apply(pr_lease, pr, inputs)
            result.due, result.why = applied.due, applied.why
            if applied.due and inputs.eligible:
                result.oldest_since = min(c.since for c in inputs.eligible)
        result.state = prstate.derive(conn, self.settings, pr, self.now()).state
        self._log_state(result)
        return result

    def refresh_base(self, stored: StoredPr, pr_lease: Lease) -> bool:
        """``resolve_base`` for a PR the due rule would reach its agent condition for. False when
        agent-spawn could not be read (nothing is decided for the PR in this pass)."""
        conn = self.store.conn
        if stored.is_fork or not due.eligible_ids(conn, stored.pr):
            return True
        if not conn.execute(
            "SELECT records_current FROM prs WHERE pr = ?", (stored.pr,)
        ).fetchone()[0]:
            return True
        facts = rounds.round_facts(conn, stored.pr)
        if facts.queued or facts.running or facts.record_open:
            return True
        try:
            base = lineage.resolve_base(pr_lease, self.spawn, self.wtctl, self.settings, stored.pr)
        except GatewayError as exc:
            lineage.mark_unreadable(pr_lease, stored.pr)
            if self.ctx.log is not None:
                self.ctx.log.warning("lower_failed", key=str(exc), pr=stored.pr)
            return False
        if self.ctx.log is not None:
            for code in base.warnings:
                self.ctx.log.warning(code, pr=stored.pr)
        return base.kind != "unknown"

    def teardown(self) -> None:
        """Tear down the forks marked for it (the pass lease); a refusal is logged."""
        try:
            done = lineage.teardown_pass(self.lease, self.spawn)
        except GatewayError as exc:
            if self.ctx.log is not None:
                self.ctx.log.warning("teardown_unavailable", key=str(exc))
            return
        if self.ctx.log is not None:
            for item in done:
                if item.refused:  # TODO(#37): post-attention teardown_refused
                    self.ctx.log.warning("teardown_refused", pr=item.pr)

    def _log_state(self, result: _PrResult, why: str | None = None) -> None:
        before = self.before.get(result.pr)
        if self.ctx.log is None or before == result.state:
            return
        self.before[result.pr] = result.state
        self.ctx.log.emit(
            "pr_state",
            {
                "pr": result.pr,
                "from": before,
                "to": result.state,
                "reason": why or result.why or "listed",
            },
        )

    # -- step 4: start the due rounds ------------------------------------------------------

    def _active_rounds(self) -> int:
        return int(
            self.store.conn.execute(
                "SELECT count(*) FROM rounds WHERE state IN ('queued', 'running')"
            ).fetchone()[0]
        )

    def start_rounds(self, only: str | None = None) -> None:
        """Start a round for each due PR, oldest due first, while capacity remains. Each start
        runs under the PR's lease (taken without waiting) and decides again from the stored rows:
        a human command may have changed the PR since it was evaluated."""
        report = self.report
        candidates = sorted(
            (r for r in self.results.values() if r.due and (only is None or r.pr == only)),
            key=lambda r: (r.oldest_since, r.pr),
        )
        for result in candidates:
            self.lease.renew()
            if self._active_rounds() >= self.settings.jobs.max_running:
                break  # over the cap the PR stays `waiting` (capacity)
            with self.scope(result.pr) as free:
                if not free:
                    result.lease_held = True
                    continue
                if self._start_one(result, self.leases[result.pr]):
                    report.rounds_started += 1

    def _start_one(self, result: _PrResult, lease: Lease) -> bool:
        conn = self.store.conn
        inputs = due.load_inputs(
            conn,
            self.settings,
            result.pr,
            now=self.now(),
            facts=rounds.round_facts(conn, result.pr),
        )
        decision = due.decide(inputs)
        if not decision.due or decision.set_block is not None or decision.clear is not None:
            return False  # changed since it was evaluated: the next pass decides
        try:
            self.placement.jobsctl_present()  # a too old jobsctl is found before the round exists
        except CliError as error:
            result.errors.append(error.code)
            return False
        # TODO(#37): the `pre-round` hooks run here; a veto or an escalation creates no round.
        machine = rounds.Rounds(lease, log=self.ctx.log)
        try:
            row = machine.create(result.pr, trigger="check", base_agent_id=inputs.base_agent)
        except CliError as error:
            if error.code == "refused" and error.reason == "round_active":
                return False
            raise
        result.started_round = row.id
        self.fresh.add(row.id)
        try:
            self.placement.submit(machine, row)
        except CliError as error:
            if error.code in ("lease_lost", "state_unsafe", "version_skew"):
                raise
            # the round stays queued: the reconcile submits its job again once it is 2 minutes old
            result.errors.append(error.code)
            if self.ctx.log is not None:
                self.ctx.log.warning("submit_failed", round=row.id, key=error.code)
        result.state = prstate.derive(conn, self.settings, result.pr, self.now()).state
        self._log_state(result, "round_started")
        return True

    # -- step 5: housekeeping ---------------------------------------------------------------

    def housekeeping(self, only: str | None = None) -> None:
        """Reconcile queued rounds, finalize running rounds whose job is gone and resolve held
        jobs of escalated rounds, after stopping the rounds of closed PRs. TODO(#29): end expired
        and closed records and compare the head of a record's PR (`remote_moved`); TODO(#26): the
        cleanup of throwaways and forks. A failure of one round is logged and does not stop the
        others."""
        fin = Finalizer(self.ctx, self.settings, self.store, placement=self.placement)
        conn = self.store.conn
        where, args = ("", ()) if only is None else (" AND pr = ?", (only,))
        self._sweep_closed(only)
        queued = [
            r[0]
            for r in conn.execute(
                f"SELECT id FROM rounds WHERE state = 'queued'{where} ORDER BY created_at, id", args
            )
        ]
        for round_id in (r for r in queued if r not in self.fresh):
            self.lease.renew()
            self._guard(round_id, lambda rid=round_id: self._reconcile(fin, rid))
        running = [
            r[0]
            for r in conn.execute(
                f"SELECT id FROM rounds WHERE state = 'running'{where} ORDER BY created_at, id",
                args,
            )
        ]
        for round_id in running:
            self.lease.renew()
            self._guard(round_id, lambda rid=round_id: fin.finalize(rid))
        self._guard("jobs", lambda: fin.resolve_held_jobs(only))
        self._guard("bases", lambda: self._reset_lost_bases(only))

    def _sweep_closed(self, only: str | None) -> None:
        """Stop the queued and running rounds of closed or merged PRs (``cancel_reason:
        pr_closed``), each under its PR's lease taken without waiting; a held lease is left for the
        next pass. The ``stop`` function is the command's own, with the actor
        ``auto:review-respond`` (Reject, expiry and closed PRs). A round that already carries a
        cancel flag was stopped before: its job, the finalizer or the reconcile ends it, except a
        running round flagged ``pr_closed`` (by an earlier sweep whose ``agent-spawn stop`` may
        have failed): it is stopped again, which is idempotent."""
        if self.ctx.audit is None:
            return
        where, args = ("", ()) if only is None else (" AND r.pr = ?", (only,))
        found = self.store.conn.execute(
            "SELECT r.id, r.pr FROM rounds r JOIN prs p ON p.pr = r.pr"
            " WHERE r.state IN ('queued', 'running') AND p.host_state != 'open'"
            " AND (r.cancel_requested_at IS NULL"
            "      OR (r.state = 'running' AND r.cancel_reason = 'pr_closed'))"
            f"{where} ORDER BY r.created_at, r.id",
            args,
        ).fetchall()
        if not found:
            return
        stopper = Stopper(
            self.settings,
            self.store,
            self.gateway,
            self.placement,
            actor=STOP_ACTOR,
            audit=self.ctx.audit.with_actor(STOP_ACTOR),
            log=self.ctx.log,
        )
        for round_id, pr in found:
            self.lease.renew()
            self._guard(
                f"closed:{round_id}", lambda rid=round_id, p=pr: self._stop_closed(stopper, rid, p)
            )

    def _stop_closed(self, stopper: Stopper, round_id: str, pr: str) -> None:
        with self.scope(pr) as free:
            if free:
                stopper.stop(self.leases[pr], round_id, "pr_closed")

    def _guard(self, what: str, work: Callable[[], object]) -> None:
        try:
            work()
        except CliError as error:
            if error.code in ("lease_lost", "state_unsafe", "version_skew"):
                raise
            if self.ctx.log is not None:
                self.ctx.log.warning("housekeeping_failed", key=f"{what}:{error.code}")
        except GatewayError as exc:
            if self.ctx.log is not None:
                self.ctx.log.warning("housekeeping_failed", key=f"{what}:{type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001 - one hostile answer must not stop the pass
            if self.ctx.log is not None:
                self.ctx.log.warning("housekeeping_failed", key=f"{what}:{type(exc).__name__}")

    def _reconcile(self, fin: Finalizer, round_id: str) -> None:
        row = rounds.get_round(self.store.conn, round_id)
        if row is None or row.state != "queued":
            return
        with self.scope(row.pr) as free:
            if not free:
                return
            row = rounds.get_round(self.store.conn, round_id)
            if row is not None and row.state == "queued":
                fin.reconcile_queued(row, self.leases[row.pr])

    def _reset_lost_bases(self, only: str | None) -> None:
        """A round that ended ``agent_gone`` (its base agent is gone) resets the PR's base, once
        and only while the stored base is still that agent."""
        where, args = ("", ()) if only is None else (" AND r.pr = ?", (only,))
        found = self.store.conn.execute(
            "SELECT DISTINCT r.pr, r.base_agent_id FROM rounds r JOIN prs p ON p.pr = r.pr"
            " WHERE r.state = 'failed' AND r.failure_reason = 'agent_gone'"
            f" AND r.base_agent_id IS NOT NULL AND p.base_agent = r.base_agent_id{where}",
            args,
        ).fetchall()
        for pr, base in found:
            with self.scope(pr) as free:
                if free and lineage.reset_base(self.leases[pr], pr, base):
                    if self.ctx.log is not None:
                        self.ctx.log.warning("base_lost", pr=pr)

    # -- the pass -------------------------------------------------------------------------

    def run(self) -> _Report:
        report = self.report
        try:
            detected = self.detection().run()
        except CliError as error:
            if error.code != "lower_failed":
                raise
            # whoami failed outright with `hosts_to_check` unset: no host list exists. forgectl's
            # own error is kept (`lower_error`) rather than folded into `all_hosts_failed`.
            report.error = error
            return report
        self.lease.renew()
        for host in detected.hosts:
            report.hosts.append(_host_document(host))
            if not host.ok:
                continue
            failed = set(host.failed)
            held = set(host.held)
            for pr in [*host.listed, *host.closed]:
                errors = ["lower_failed"] if pr in failed else []
                if pr in held:
                    report.prs.append(self.evaluate(pr, None, errors))
                    continue
                with self.scope(pr) as free:
                    report.prs.append(self.evaluate(pr, self.leases[pr] if free else None, errors))
        self.start_rounds()
        self.housekeeping()
        self.teardown()
        # TODO(#37): step 6, deliver the queued hook events
        if detected.all_failed:
            report.error = CliError("all_hosts_failed", "no host could be read")
        return report

    def run_one(self, pr: str) -> _Report:
        """`check --pr`: `forgectl show`, then the one comment read, then the due rule."""
        report = self.report
        host = host_of(pr)
        detection = self.detection()
        with self.scope(pr) as free:
            if not free:
                report.hosts.append({"host": host, "ok": True, "code": None, "warnings": []})
                report.prs.append(self.evaluate(pr, None, []))
                return report
            try:
                refreshed = detection.refresh_pr(pr)
            except CliError as error:
                if error.code != "lower_failed":
                    raise
                code = (error.lower or {}).get("code", "unknown")
                if code == "not_found":
                    raise CliError("not_found", "the host does not know this PR") from None
                return self._host_failed(report, host, str(code))
            if refreshed.stored is None and not refreshed.account_known:
                return self._host_failed(report, host, refreshed.account_code or "identity_unknown")
            if refreshed.stored is None:  # not yours: nothing is stored and no read is made
                report.hosts.append({"host": host, "ok": True, "code": None, "warnings": []})
                report.prs.append(_PrResult(pr, "idle", errors=["refused"]))
                return report
            errors: list[str] = []
            stored = refreshed.stored
            if not stored.is_fork and stored.host_state == "open":
                try:
                    detection.read_comments(pr)
                except CliError as error:
                    if error.code != "lower_failed":
                        raise
                    errors.append("lower_failed")
            warnings = ["read_failed"] if errors else []
            report.hosts.append({"host": host, "ok": True, "code": None, "warnings": warnings})
            report.prs.append(self.evaluate(pr, self.leases[pr], errors))
        self.start_rounds(pr)
        self.housekeeping(pr)
        return report

    def _host_failed(self, report: _Report, host: str, code: str) -> _Report:
        if self.ctx.log is not None:
            self.ctx.log.emit("host_failed", {"host": host, "code": code}, level="warn")
        report.hosts.append({"host": host, "ok": False, "code": code, "warnings": []})
        report.error = CliError("all_hosts_failed", "no host could be read")
        return report


def _host_document(host: HostResult) -> dict[str, Any]:
    warnings = list(host.warnings)
    if host.deferred:
        warnings.append("budget_exhausted")
    if host.failed:
        warnings.append("read_failed")
    return {
        "host": host.host,
        "ok": host.ok,
        "code": host.code,
        "warnings": list(dict.fromkeys(warnings)),
    }


def _dry_run(ctx: Context, settings: Config, pr: str | None) -> _Report:
    """Reads only, no sibling call: the stored PRs with their derived state (nothing is listed,
    read or recorded, no lease is taken and no hook runs)."""
    report = _Report(dry_run=True)
    store = ctx.store
    if store is None:
        return report
    now = store.clock.now().timestamp()
    hosts: dict[str, None] = {}
    for stored in prstate.stored_prs(store.conn):
        if pr is not None and stored.pr != pr:
            continue
        if stored.host_state != "open":
            continue
        hosts[stored.host] = None
        state = prstate.derive(store.conn, settings, stored.pr, now).state
        report.prs.append(_PrResult(stored.pr, state))
    report.hosts = [{"host": h, "ok": True, "code": None, "warnings": ["not_read"]} for h in hosts]
    return report


def _finish(ctx: Context, report: _Report, started: str) -> Outcome:
    if ctx.log is not None:
        errors = sum(len(p.errors) for p in report.prs) + sum(
            1 for h in report.hosts if not h["ok"]
        )
        ctx.log.emit(
            "check_pass",
            {
                "started_at": started,
                "finished_at": format_ts(ctx.store.clock.now()) if ctx.store else started,
                "already_running": report.already_running,
                "prs": len(report.prs),
                "rounds_started": report.rounds_started,
                "errors": errors,
            },
            level="warn" if report.error is not None else "info",
        )
    code = EXIT_FAILED if report.error is not None else EXIT_OK
    return Outcome(report.payload(), report.human(), code)


def handle(ctx: Context, args: argparse.Namespace) -> Outcome:
    settings, store = ctx.settings, ctx.store
    if settings is None:
        raise CliError("internal", "check ran without a config")
    pr: str | None = parse_pr(args.pr) if args.pr is not None else None
    if ctx.dry_run:
        report = _dry_run(ctx, settings, pr)
        return Outcome(report.payload(), report.human(), EXIT_OK)
    if store is None:
        raise CliError("internal", "check ran without a state database")
    started = format_ts(store.clock.now())
    on_event = ctx.log.lease_callback() if ctx.log is not None else None
    lease = store.try_acquire(pass_lease_key(), on_event=on_event)
    if lease is None:
        return _finish(ctx, _Report(already_running=True), started)
    try:
        current = _Pass(ctx, settings, store, lease)
        try:
            report = current.run() if pr is None else current.run_one(pr)
        except CliError as error:
            if error.code != "state_unsafe":
                raise
            # found once the pass has started: the result so far, with the error (exit 12)
            report = current.report
            report.error = error
        lease.renew()  # the result is only reported while the pass lease is still ours
    finally:
        lease.release()
    return _finish(ctx, report, started)
