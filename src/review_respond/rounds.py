"""Rounds: ids, the round table and every transition of the round state table
(docs/interface.md: Rounds, Round states, Run outcomes, Counting). Stage 2.

Every transition runs in one ``lease.transaction()`` (a lost lease aborts with ``lease_lost``
before anything is written): the round row is read under the write lock, the transition checks
the state it starts from, and the row changes only through ``cas_update`` on its ``rev`` and its
state. The comment transitions (``HandledSet`` with the open ``conn``), the PR block and the
caller's record step share that transaction, so a round never ends half-way. A move from a state
the table does not name changes nothing and answers ``Transition(changed=False)`` with the stored
row: that is also how the loser of a race learns it lost. Terminal rounds are immutable.

The lease a caller holds is the PR's ``pr:`` lease or the round's own ``round:`` lease (``_job``
holds both; ``stop``, the reconcile and the sweep hold ``pr:``; the finalizer takes ``round:`` only
through ``Store.try_acquire``, never by writing the row). Nothing here takes a lease.

Seams owned by later issues:

- ``job.py`` (#23) calls ``start``, ``record_throwaway``, ``record_agent``, ``note_started`` and
  ``finish_run`` (``record_job`` is the placement's, #24); its failed PR read increments
  ``prs.read_failures`` (``detect`` does for comment reads) before ``cancel_read_failed``, which
  reads that count and never writes it.
- TODO(#24): job placement, the queued reconcile (``reconcile_lost`` is its write) and the
  finalizer (``finish_run`` is its write); ``show``'s ``finalize`` member stays null until then.
- TODO(#29): ``to_pending(extra=...)`` runs the pending record's insert in the transaction;
  ``settle_pending`` is called inside the record's transaction; ``round_facts.record_open`` is
  derived from the round state ``pending`` (a round stays pending while its record is pending,
  pushing or pushed) until the records table exists.
- TODO(#37): ``post-pending`` and ``post-attention`` (``escalated``, ``failed``, ``job_lost``,
  the third ``read_failed``) are queued by the ``extra`` step of the ending transition
  (``to_escalated``, ``to_failed``, ``to_canceled``, ``finish_run``, ``reconcile_lost``,
  ``cancel_read_failed``, ``cancel_flagged``, ``start``'s ``nothing_to_respond``): it runs in the
  same transaction after the block is set and is told whether this call set it (``new_block``,
  also on ``Transition``); the post is queued only for a new block.
- TODO(#25): ``respond`` clears blocks. The forks rows and ``agent_busy`` are ``lineage``'s (#19).
"""

from __future__ import annotations

import dataclasses
import json
import re
import secrets
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from . import due, lineage
from .audit import ROUND_STATES
from .errors import CliError
from .handled import HandledSet
from .outcomes import (
    ARTIFACT_FAILURES,
    FAILURE_DETAILS,
    FAILURE_REASONS,
    Mapped,
    RunEnd,
    map_run_end,
)
from .store import Lease, cas_update, pr_digest, pr_lease_key, round_lease_key

TERMINAL_STATES: Final = frozenset(
    {"confirmed", "rejected", "expired", "closed", "escalated", "failed", "canceled"}
)
SETTLED_BY_RECORD: Final = ("confirmed", "rejected", "expired", "closed")
CANCEL_REQUESTS: Final = ("stopped", "pr_closed")
MAX_ESCALATION_BYTES: Final = 4096
MAX_ACTIVE_ATTEMPTS: Final = 8
READ_FAILURE_LIMIT: Final = 3  # the third consecutive read_failed sets block failed

_ALPHABET: Final = "abcdefghijklmnopqrstuvwxyz234567"
_ID_RE: Final = re.compile(r"rd-[a-z2-7]{8}")
_PREFIX_RE: Final = re.compile(r"rd-[a-z2-7]{4,8}")
_AGENT_RE: Final = re.compile(r"ag-[a-z2-7]{8}")
_JOB_RE: Final = re.compile(r"jb-[a-z2-7]{8}")
_SHA_RE: Final = re.compile(r"[0-9a-f]{40}")
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
_THROWAWAY_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")

_COLUMNS: Final = (
    "id, pr, number, state, rev, trigger_kind, forced, include_existing, note_sha256,"
    " base_agent_id, job_id, detached_started_at, agent_id, throwaway_id, head_sha, comment_ids,"
    " counted, cancel_requested_at, cancel_reason, failure_reason, failure_detail,"
    " escalation_reason, escalation_summary, escalation_cause, warnings, created_at, started_at,"
    " ended_at"
)


def _tampered(what: str) -> CliError:
    return CliError("state_unsafe", f"the state database holds {what} that is not valid")


# ---- ids -----------------------------------------------------------------------------------


def new_round_id() -> str:
    """``rd-`` plus 8 random characters of ``[a-z2-7]`` (40 bits)."""
    return "rd-" + "".join(secrets.choice(_ALPHABET) for _ in range(8))


def is_round_id(text: object) -> bool:
    return isinstance(text, str) and _ID_RE.fullmatch(text) is not None


def resolve_round_id(conn: sqlite3.Connection, text: str) -> str:
    """The id a full id or a unique prefix (4 to 8 characters after ``rd-``) names.

    ``usage`` for a malformed argument, ``not_found`` for none and ``ambiguous`` (exit 21) for
    several rounds.
    """
    if not isinstance(text, str) or not _PREFIX_RE.fullmatch(text):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise CliError("usage", "a round id is rd- plus 4 to 8 characters of a-z and 2-7")
    found = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM rounds WHERE substr(id, 1, ?) = ? ORDER BY id LIMIT 2",
            (len(text), text),
        )
    ]
    if not found:
        raise CliError("not_found", "no round matches")
    if len(found) > 1:
        raise CliError("ambiguous", "the id prefix matches several rounds")
    return found[0]


# ---- rows ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class RoundRow:
    id: str
    pr: str
    number: int
    state: str
    rev: int
    trigger: str
    forced: bool
    include_existing: bool
    note_sha256: str | None
    base_agent_id: str | None
    job_id: str | None
    detached_started_at: float | None
    agent_id: str | None
    throwaway_id: str | None
    head_sha: str | None
    comment_ids: tuple[str, ...]
    counted: bool
    cancel_requested_at: float | None
    cancel_reason: str | None
    failure_reason: str | None
    failure_detail: str | None
    escalation_reason: str | None
    escalation_summary: str | None
    escalation_cause: str | None
    warnings: tuple[str, ...]
    created_at: float
    started_at: float | None
    ended_at: float | None


def _strings(text: str, what: str) -> tuple[str, ...]:
    try:
        doc = json.loads(text)
    except ValueError:
        raise _tampered(what) from None
    if not isinstance(doc, list) or not all(isinstance(x, str) for x in doc):  # pyright: ignore[reportUnknownVariableType]
        raise _tampered(what)
    return tuple(doc)  # pyright: ignore[reportUnknownArgumentType]


def _row(values: Sequence[Any]) -> RoundRow:
    v = list(values)
    if v[3] not in ROUND_STATES:
        raise _tampered("a round state")
    return RoundRow(
        id=v[0], pr=v[1], number=v[2], state=v[3], rev=v[4], trigger=v[5], forced=bool(v[6]),
        include_existing=bool(v[7]), note_sha256=v[8], base_agent_id=v[9], job_id=v[10],
        detached_started_at=v[11], agent_id=v[12], throwaway_id=v[13], head_sha=v[14],
        comment_ids=_strings(v[15], "a round's comment ids"), counted=bool(v[16]),
        cancel_requested_at=v[17], cancel_reason=v[18], failure_reason=v[19],
        failure_detail=v[20], escalation_reason=v[21], escalation_summary=v[22],
        escalation_cause=v[23], warnings=_strings(v[24], "a round's warnings"), created_at=v[25],
        started_at=v[26], ended_at=v[27],
    )  # fmt: skip


def get_round(conn: sqlite3.Connection, round_id: str) -> RoundRow | None:
    found = conn.execute(f"SELECT {_COLUMNS} FROM rounds WHERE id = ?", (round_id,)).fetchone()
    return None if found is None else _row(found)


def list_rounds(conn: sqlite3.Connection, pr: str) -> list[RoundRow]:
    """The rounds of a PR, oldest first."""
    return [
        _row(r)
        for r in conn.execute(f"SELECT {_COLUMNS} FROM rounds WHERE pr = ? ORDER BY number", (pr,))
    ]


def round_facts(conn: sqlite3.Connection, pr: str) -> due.RoundFacts:
    """What the due rule and the PR state need from the rounds table. ``record_open`` is true
    while a round of the PR is ``pending`` (TODO(#29): from the records table); ``agent_busy`` is
    the last observation of the base agent (``prs.agent_busy``, written by ``resolve_base``)."""
    queued, running, counted, pending = conn.execute(
        "SELECT coalesce(sum(state = 'queued'), 0), coalesce(sum(state = 'running'), 0),"
        " coalesce(sum(counted), 0), coalesce(sum(state = 'pending'), 0)"
        " FROM rounds WHERE pr = ?",
        (pr,),
    ).fetchone()
    # A read-only open of a database older than the column never migrates: no observation, not busy.
    has_busy = any(r[1] == "agent_busy" for r in conn.execute("PRAGMA table_info(prs)"))
    busy = (
        conn.execute("SELECT agent_busy FROM prs WHERE pr = ?", (pr,)).fetchone()
        if has_busy
        else None
    )
    return due.RoundFacts(
        queued=queued > 0,
        running=running > 0,
        counted=counted,
        record_open=pending > 0,
        agent_busy=bool(busy and busy[0]),
    )


def count_rounds(conn: sqlite3.Connection, pr: str) -> int:
    return int(conn.execute("SELECT count(*) FROM rounds WHERE pr = ?", (pr,)).fetchone()[0])


# ---- the machine ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Transition:
    """``changed`` is False when the round was not in a state the move starts from (or lost the
    race); ``row`` is the stored row after the call. ``new_block`` is True when the call set the
    PR's block (False when none was set or one already existed): the caller queues
    ``post-attention`` only for a new block."""

    changed: bool
    row: RoundRow
    new_block: bool = False


@dataclass(frozen=True)
class _Plan:
    """What a transition writes: the row's columns, the reason logged, whether it ended the round
    (``extra`` runs for an end) and whether it set the PR's block."""

    changes: dict[str, object]
    why: str
    ends: bool = False
    new_block: bool = False


class _Log(Protocol):
    def emit(self, event: str, fields: Mapping[str, object], *, level: str = ...) -> bool: ...


Body = Callable[[sqlite3.Connection, RoundRow], _Plan | None]
RecordStep = Callable[[sqlite3.Connection, RoundRow], None]
# Runs inside the transaction of a transition that ends the round, after the block is set, with
# the stored row and whether this call set the PR's block (post-attention is queued only then).
EndStep = Callable[[sqlite3.Connection, RoundRow, bool], None]


def _scrub(text: str | None) -> str | None:
    """Unpaired surrogates become escapes; the text is cut to 4096 UTF-8 bytes at a character."""
    if text is None:
        return None
    clean = text.encode("utf-8", "backslashreplace")[:MAX_ESCALATION_BYTES]
    return clean.decode("utf-8", "ignore")


class Rounds:
    def __init__(self, lease: Lease, *, log: _Log | None = None) -> None:
        self.lease = lease
        self.log = log
        self._handled = HandledSet(lease)

    # -- plumbing ---------------------------------------------------------------------------

    def _now(self) -> float:
        return self.lease.store.clock.now().timestamp()

    def _check_lease(self, pr: str, round_id: str | None) -> None:
        keys = {pr_lease_key(pr_digest(pr))}
        if round_id is not None:
            keys.add(round_lease_key(round_id))
        if self.lease.key not in keys:
            raise ValueError("the lease is neither the PR's lease nor the round's lease")

    def _emit(self, row: RoundRow, frm: str | None, to: str, why: str) -> None:
        if self.log is not None:
            self.log.emit("round_state", {"round": row.id, "from": frm, "to": to, "reason": why})

    def _run(
        self,
        round_id: str,
        frm: frozenset[str],
        body: Body,
        *,
        rev: int | None = None,
        extra: EndStep | None = None,
    ) -> Transition:
        with self.lease.transaction() as conn:
            row = get_round(conn, round_id)
            if row is None:
                raise CliError("not_found", "no round matches")
            self._check_lease(row.pr, row.id)
            if row.state not in frm or (rev is not None and row.rev != rev):
                return Transition(False, row)
            planned = body(conn, row)
            if planned is None:
                return Transition(False, row)
            if not cas_update(
                conn, "rounds", "id", row.id, row.rev, planned.changes, where={"state": row.state}
            ):
                # cannot happen: the row was read under the write lock of this transaction
                raise CliError("internal", f"round {row.id} changed under the write lock")
            after = get_round(conn, row.id)
            assert after is not None
            if planned.ends and extra is not None:
                extra(conn, after, planned.new_block)
        if after.state != row.state:
            self._emit(after, row.state, after.state, planned.why)
        return Transition(True, after, planned.new_block)

    def _block(self, conn: sqlite3.Connection, row: RoundRow, reason: str) -> bool:
        """Set the PR's block; True when this call set it (an existing block stays)."""
        return due.set_block_in(
            conn, row.pr, reason, due.eligible_ids(conn, row.pr), now=self._now()
        )

    def _end(
        self,
        row: RoundRow,
        to: str,
        counted: bool,
        why: str,
        *,
        new_block: bool = False,
        **more: object,
    ) -> _Plan:
        changes: dict[str, object] = {
            "state": to,
            "ended_at": self._now(),
            "counted": int(row.counted or counted),
            **more,
        }
        return _Plan(changes, why, ends=True, new_block=new_block)

    # -- (none) -> queued ---------------------------------------------------------------------

    def create(
        self,
        pr: str,
        *,
        trigger: str = "check",
        forced: bool = False,
        include_existing: bool = False,
        note_sha256: str | None = None,
        base_agent_id: str | None = None,
    ) -> RoundRow:
        """The round row of a due PR or a human ``respond`` (the ``pre-round`` hooks continued).
        Needs the PR's own lease. ``refused`` ``round_active`` while the PR has a queued or
        running round."""
        if trigger not in ("check", "respond"):
            raise ValueError("trigger is check or respond")
        if note_sha256 is not None and not _SHA256_RE.fullmatch(note_sha256):
            raise ValueError("note_sha256 is 64 lowercase hex characters")
        if base_agent_id is not None and not _AGENT_RE.fullmatch(base_agent_id):
            raise ValueError("base_agent_id is an agent id")
        if self.lease.key != pr_lease_key(pr_digest(pr)):
            raise ValueError("a round is created under the PR's lease")
        with self.lease.transaction() as conn:
            if conn.execute("SELECT 1 FROM prs WHERE pr = ?", (pr,)).fetchone() is None:
                raise CliError("not_found", "no stored PR matches")
            active = conn.execute(
                "SELECT id FROM rounds WHERE pr = ? AND state IN ('queued', 'running')", (pr,)
            ).fetchone()
            if active is not None:
                raise CliError(
                    "refused",
                    "a round of this PR is queued or running",
                    reason="round_active",
                    details={"round": active[0]},
                )
            number = conn.execute(
                "SELECT coalesce(max(number), 0) + 1 FROM rounds WHERE pr = ?", (pr,)
            ).fetchone()[0]
            for _ in range(MAX_ACTIVE_ATTEMPTS):
                rid = new_round_id()
                if conn.execute("SELECT 1 FROM rounds WHERE id = ?", (rid,)).fetchone() is None:
                    break
            else:
                raise CliError("internal", "could not find a free round id")
            conn.execute(
                "INSERT INTO rounds (id, pr, number, state, trigger_kind, forced,"
                " include_existing, note_sha256, base_agent_id, created_at)"
                " VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
                (rid, pr, number, trigger, int(forced), int(include_existing), note_sha256,
                 base_agent_id, self._now()),
            )  # fmt: skip
            row = get_round(conn, rid)
            assert row is not None
        self._emit(row, None, "queued", "created")
        return row

    # -- bookkeeping that is not a change of state ----------------------------------------------

    def record_job(
        self, round_id: str, *, job_id: str | None = None, detached_at: float | None = None
    ) -> Transition:
        """Store the jobsctl job id, or the start time of the detached job (a queued round)."""
        if (job_id is None) == (detached_at is None):
            raise ValueError("give exactly one of job_id and detached_at")
        if job_id is not None and not _JOB_RE.fullmatch(job_id):
            raise ValueError("job_id is a jobsctl job id")
        changes: dict[str, object] = (
            {"job_id": job_id} if job_id is not None else {"detached_started_at": detached_at}
        )
        return self._run(round_id, frozenset({"queued"}), lambda c, r: _Plan(changes, "job"))

    def record_agent(
        self, round_id: str, agent_id: str, *, throwaway_id: str | None = None
    ) -> Transition:
        """Store the agent (and throwaway) of a running round. The same agent id again changes
        nothing; a different one is ``internal`` (an agent id is never replaced)."""
        if not _AGENT_RE.fullmatch(agent_id):
            raise ValueError("agent_id is an agent id")
        if throwaway_id is not None and not _THROWAWAY_RE.fullmatch(throwaway_id):
            raise ValueError("throwaway_id is not a plain identifier")

        def body(conn: sqlite3.Connection, row: RoundRow) -> _Plan | None:
            if row.agent_id is not None:
                if row.agent_id != agent_id:
                    raise CliError("internal", f"round {row.id} already has another agent")
                return None
            # the fork row (state round) is stored with the agent id, in this transaction
            lineage.record_fork(
                conn, pr=row.pr, round_id=row.id, agent_id=agent_id, source=row.base_agent_id,
                now=self._now(),
            )  # fmt: skip
            changes: dict[str, object] = {"agent_id": agent_id}
            if throwaway_id is not None:
                changes["throwaway_id"] = throwaway_id
            return _Plan(changes, "agent")

        return self._run(round_id, frozenset({"running"}), body)

    def record_throwaway(self, round_id: str, throwaway_id: str) -> Transition:
        """Store the round throwaway's wtctl record id (a running round, before the agent: the
        finalizer and the cleanup find the throwaway by it). The same id again changes nothing; a
        different one is ``internal``."""
        if not _THROWAWAY_RE.fullmatch(throwaway_id):
            raise ValueError("throwaway_id is not a plain identifier")

        def body(conn: sqlite3.Connection, row: RoundRow) -> _Plan | None:
            if row.throwaway_id is not None:
                if row.throwaway_id != throwaway_id:
                    raise CliError("internal", f"round {row.id} already has another throwaway")
                return None
            return _Plan({"throwaway_id": throwaway_id}, "throwaway")

        return self._run(round_id, frozenset({"running"}), body)

    def note_started(self, round_id: str) -> Transition:
        """Counting: the run reached ``working``; the round counts toward ``max_rounds``."""

        def body(conn: sqlite3.Connection, row: RoundRow) -> _Plan | None:
            return None if row.counted else _Plan({"counted": 1}, "run_started")

        return self._run(round_id, frozenset({"running"}), body)

    def request_cancel(self, round_id: str, reason: str) -> Transition:
        """``stop``: set the cancel flag of a queued or running round (the first reason stays)."""
        if reason not in CANCEL_REQUESTS:
            raise ValueError("a cancel request is stopped or pr_closed")

        def body(conn: sqlite3.Connection, row: RoundRow) -> _Plan | None:
            if row.cancel_requested_at is not None:
                return None
            return _Plan(
                {"cancel_requested_at": self._now(), "cancel_reason": reason}, "cancel_requested"
            )

        return self._run(round_id, frozenset({"queued", "running"}), body)

    # -- queued -> canceled / failed ------------------------------------------------------------

    def _canceled(
        self, conn: sqlite3.Connection, row: RoundRow, reason: str, counted: bool = False
    ) -> _Plan:
        """End as canceled; a stored cancel reason (a flag set before) wins over ``reason``."""
        final = row.cancel_reason or reason
        if row.state == "running":
            self._handled.release(row.pr, row.id, conn=conn)
        new_block = False
        if final == "stopped":
            new_block = self._block(conn, row, "stopped")
        elif final == "read_failed":
            failures = conn.execute(
                "SELECT read_failures FROM prs WHERE pr = ?", (row.pr,)
            ).fetchone()[0]
            if failures >= READ_FAILURE_LIMIT:
                new_block = self._block(conn, row, "failed")
        return self._end(row, "canceled", counted, final, new_block=new_block, cancel_reason=final)

    def cancel_flagged(self, round_id: str, *, extra: EndStep | None = None) -> Transition:
        """``_job`` finds the cancel flag, or ``stop`` / the closed-PR sweep ends a queued round
        whose job is canceled or never submitted: canceled with the stored reason."""

        def body(conn: sqlite3.Connection, row: RoundRow) -> _Plan | None:
            if row.cancel_requested_at is None or row.cancel_reason is None:
                raise ValueError("the round has no cancel flag")
            return self._canceled(conn, row, row.cancel_reason)

        return self._run(round_id, frozenset({"queued"}), body, extra=extra)

    def _cancel_with(self, round_id: str, reason: str, extra: EndStep | None) -> Transition:
        return self._run(
            round_id,
            frozenset({"queued"}),
            lambda c, r: self._canceled(c, r, reason),
            extra=extra,
        )

    def cancel_pr_closed(self, round_id: str, *, extra: EndStep | None = None) -> Transition:
        return self._cancel_with(round_id, "pr_closed", extra)

    def cancel_nothing(self, round_id: str, *, extra: EndStep | None = None) -> Transition:
        return self._cancel_with(round_id, "nothing_to_respond", extra)

    def cancel_read_failed(self, round_id: str, *, extra: EndStep | None = None) -> Transition:
        """The job's PR or comment read failed. No block until the third consecutive failure of
        the PR (``prs.read_failures``, written by the failed reads, not here)."""
        return self._cancel_with(round_id, "read_failed", extra)

    def reconcile_lost(self, round_id: str, *, extra: EndStep | None = None) -> Transition:
        """The queued reconcile: the job is terminal or gone and the round is still queued."""

        def body(conn: sqlite3.Connection, row: RoundRow) -> _Plan | None:
            if row.cancel_requested_at is not None and row.cancel_reason is not None:
                return self._canceled(conn, row, row.cancel_reason)
            new_block = self._block(conn, row, "failed")
            return self._end(
                row, "failed", False, "job_lost", new_block=new_block, failure_reason="job_lost"
            )

        return self._run(round_id, frozenset({"queued"}), body, extra=extra)

    def fail_queued(self, round_id: str, *, extra: EndStep | None = None) -> Transition:
        """``_job`` cannot build what the round needs and no comment was delivered yet (the
        prompt cannot fit ``limits.prompt_bytes`` even for one comment, the stored note is
        unreadable, the session settings are unsafe): ``queued`` -> ``failed`` (``internal``),
        not counted, block ``failed``. The state table has no such row (a decision for the
        maintainer, see docs/interface.md Round states): without a block a due ``check`` would
        queue the same round again at every pass."""

        def body(conn: sqlite3.Connection, row: RoundRow) -> _Plan | None:
            new_block = self._block(conn, row, "failed")
            return self._end(
                row, "failed", False, "internal", new_block=new_block, failure_reason="internal"
            )

        return self._run(round_id, frozenset({"queued"}), body, extra=extra)

    # -- queued -> running ----------------------------------------------------------------------

    def start(
        self,
        round_id: str,
        comment_ids: Sequence[str],
        *,
        head_sha: str,
        expected: Mapping[str, str] | None = None,
        rev: int | None = None,
        extra: EndStep | None = None,
    ) -> Transition:
        """``_job`` re-read the PR and the comments and selected ``comment_ids``: deliver them
        (``expected`` maps ids to the body hash shown; an edited one is skipped) and go
        ``running``. When nothing could be delivered the round ends canceled
        ``nothing_to_respond`` in the same transaction (``extra`` runs then, and only then). ``rev``
        makes it a strict compare-and-set on the revision the caller decided on."""
        if not isinstance(head_sha, str) or not _SHA_RE.fullmatch(head_sha):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError("head_sha is 40 lowercase hex characters")
        ids = list(dict.fromkeys(comment_ids))
        if not all(isinstance(i, str) and i for i in ids):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError("a comment id is a non-empty string")

        def body(conn: sqlite3.Connection, row: RoundRow) -> _Plan | None:
            delivered = self._handled.deliver(
                row.pr,
                ids,
                round_id=row.id,
                include_seen=row.include_existing,
                expected=expected,
                conn=conn,
            )
            if not delivered:
                return self._canceled(conn, row, "nothing_to_respond")
            conn.execute(
                "UPDATE prs SET read_failures = 0, rev = rev + 1"
                " WHERE pr = ? AND read_failures != 0",
                (row.pr,),
            )
            return _Plan(
                {
                    "state": "running",
                    "head_sha": head_sha,
                    "comment_ids": json.dumps(list(delivered)),
                    "started_at": self._now(),
                },
                "started",
            )

        return self._run(round_id, frozenset({"queued"}), body, rev=rev, extra=extra)

    # -- running -> ... -------------------------------------------------------------------------

    def _pending(self, conn: sqlite3.Connection, row: RoundRow, extra: RecordStep | None) -> _Plan:
        self._handled.mark_handled(row.pr, row.id, conn=conn)
        if extra is not None:
            extra(conn, dataclasses.replace(row, state="pending", counted=True))
        return _Plan({"state": "pending", "counted": 1}, "done")

    def _escalated(
        self,
        conn: sqlite3.Connection,
        row: RoundRow,
        reason: str | None,
        summary: str | None,
        counted: bool,
        cause: str | None,
    ) -> _Plan:
        if cause not in (None, "hook"):
            raise ValueError("cause is hook or none")
        self._handled.release(row.pr, row.id, conn=conn)
        new_block = self._block(conn, row, "escalated")
        return self._end(
            row,
            "escalated",
            counted,
            "escalated",
            new_block=new_block,
            escalation_reason=_scrub(reason),
            escalation_summary=_scrub(summary),
            escalation_cause=cause,
        )

    def _failed(
        self,
        conn: sqlite3.Connection,
        row: RoundRow,
        reason: str,
        detail: str | None,
        counted: bool,
    ) -> _Plan:
        if reason not in FAILURE_REASONS:
            raise ValueError("failure reason is not a closed reason")
        if (reason == "artifact_invalid") != (detail is not None):
            raise ValueError("a detail belongs to artifact_invalid, and artifact_invalid has one")
        if detail is not None and detail not in FAILURE_DETAILS:
            raise ValueError("failure detail is not a closed detail")
        self._handled.release(row.pr, row.id, conn=conn)
        new_block = self._block(conn, row, "failed")
        return self._end(
            row,
            "failed",
            counted,
            reason,
            new_block=new_block,
            failure_reason=reason,
            failure_detail=detail,
        )

    def _running_canceled(self, conn: sqlite3.Connection, row: RoundRow, counted: bool) -> _Plan:
        if row.cancel_requested_at is None or row.cancel_reason is None:
            raise ValueError("the round has no cancel flag")
        return self._canceled(conn, row, row.cancel_reason, counted)

    def to_pending(self, round_id: str, *, extra: RecordStep | None = None) -> Transition:
        """The run ended ``done``, the artifact validated and the stored diff was built: the
        delivered comments are handled and ``extra`` (the pending record's insert) runs in the
        same transaction. TODO(#29), TODO(#37): the record and ``post-pending``."""
        return self._run(round_id, frozenset({"running"}), lambda c, r: self._pending(c, r, extra))

    def to_escalated(
        self,
        round_id: str,
        *,
        reason: str | None,
        summary: str | None,
        counted: bool,
        cause: str | None = None,
        extra: EndStep | None = None,
    ) -> Transition:
        return self._run(
            round_id,
            frozenset({"running"}),
            lambda c, r: self._escalated(c, r, reason, summary, counted, cause),
            extra=extra,
        )

    def to_canceled(
        self, round_id: str, *, counted: bool, extra: EndStep | None = None
    ) -> Transition:
        """The flag is set and the run ended stopped, or the job saw it before the agent."""
        return self._run(
            round_id,
            frozenset({"running"}),
            lambda c, r: self._running_canceled(c, r, counted),
            extra=extra,
        )

    def to_failed(
        self,
        round_id: str,
        reason: str,
        *,
        detail: str | None = None,
        counted: bool = False,
        extra: EndStep | None = None,
    ) -> Transition:
        return self._run(
            round_id,
            frozenset({"running"}),
            lambda c, r: self._failed(c, r, reason, detail, counted),
            extra=extra,
        )

    def finish_run(
        self,
        round_id: str,
        end: RunEnd,
        *,
        escalation: tuple[str | None, str | None] = (None, None),
        extra: EndStep | None = None,
    ) -> Transition:
        """Apply the run-outcome table to a running round (``outcomes.map_run_end``). The cancel
        flag is read from the row, not from ``end``; the detail of ``artifact_invalid`` is
        ``end.detail``. ``extra`` runs when the round ends (not for a run that only started or for
        ``pending``). A signal exit of ``wait`` is a ``ValueError``: the caller re-reads
        ``agent-spawn show`` and maps the last run."""

        def body(conn: sqlite3.Connection, row: RoundRow) -> _Plan | None:
            flagged = dataclasses.replace(end, cancel_requested=row.cancel_requested_at is not None)
            mapped: Mapped = map_run_end(flagged)
            if mapped.to == "reread":
                raise ValueError("a signal exit is never an outcome: re-read agent-spawn show")
            if mapped.to == "running":
                return (
                    None
                    if row.counted or not mapped.counted
                    else _Plan({"counted": 1}, "run_started")
                )
            if mapped.to == "pending":
                return self._pending(conn, row, None)
            if mapped.to == "escalated":
                return self._escalated(
                    conn, row, escalation[0], escalation[1], mapped.counted, mapped.cause
                )
            if mapped.to == "canceled":
                return self._running_canceled(conn, row, mapped.counted)
            assert mapped.failure_reason is not None
            return self._failed(conn, row, mapped.failure_reason, end.detail, mapped.counted)

        return self._run(round_id, frozenset({"running"}), body, extra=extra)

    def settle_pending(self, conn: sqlite3.Connection, round_id: str, to: str) -> bool:
        """``settle_pending`` with the lease's clock; it logs the ``round_state`` event when it
        changed the round (the caller's transaction is the one that commits it)."""
        if not settle_pending(conn, round_id, to, now=self._now()):
            return False
        row = get_round(conn, round_id)
        assert row is not None
        self._emit(row, "pending", to, "settled")
        return True


def settle_pending(conn: sqlite3.Connection, round_id: str, to: str, *, now: float) -> bool:
    """``pending`` follows its record to ``confirmed``, ``rejected``, ``expired`` or ``closed``,
    inside the record's own transaction (the caller holds the lease that transaction checked and
    gives its clock's ``now``; ``Rounds.settle_pending`` does both). False when the round is not
    pending (nothing is written)."""
    if to not in SETTLED_BY_RECORD:
        raise ValueError("a pending round ends confirmed, rejected, expired or closed")
    row = get_round(conn, round_id)
    if row is None:
        raise CliError("not_found", "no round matches")
    if row.state != "pending":
        return False
    return cas_update(
        conn, "rounds", "id", row.id, row.rev, {"state": to, "ended_at": now},
        where={"state": "pending"},
    )  # fmt: skip


__all__ = [
    "ARTIFACT_FAILURES",
    "TERMINAL_STATES",
    "RoundRow",
    "Rounds",
    "Transition",
    "count_rounds",
    "get_round",
    "is_round_id",
    "list_rounds",
    "new_round_id",
    "resolve_round_id",
    "round_facts",
    "settle_pending",
]
