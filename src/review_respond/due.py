"""The due rule, the settle clock, the stored PR block and the derived PR state
(docs/interface.md: Settle and round start). Stage 1.

``decide`` and ``derive_state`` are pure functions of ``DueInputs``, a snapshot that ``load_inputs``
reads from the state database. The settle clock only ever sees ``Candidate.settle_ts``, the local
time review-respond first stored a comment's current body hash (``comments.body_seen_at``); no
forge timestamp can reach it.

``prs.block`` is written only here, under a lease and through ``cas_update``: ``set_block`` (only
when the PR has no block) and ``clear_block`` (only by the causes the interface documents for the
reason). ``apply`` runs the automatic part of the due rule (set ``round_limit`` or ``no_agent``,
clear a block whose cause went away) in one lease-checked transaction.

``records_current``: the due rule never starts from comment records it cannot trust. A complete
read stamps ``prs.last_comments_read_at`` before the records are applied, so that stamp is not
proof; ``prs.records_current`` is cleared by that stamp and set again in the transaction that
applies the records (detect.py, handled.py). A PR whose records are not current is not due, and
nothing is set or cleared for it. Its derived state is shown from the records as they are.

Seams owned by later issues (``RoundFacts`` carries them, all defaulting to "none"):

- TODO(#18): ``queued``, ``running`` and ``counted`` come from the rounds table.
- TODO(#29): ``record_open`` (a record pending, pushing or pushed) comes from the records table.
- TODO(#19): ``agent_busy`` and the writer of ``prs.base_agent`` (``resolve_base``).
- TODO(#37): ``post-attention`` ``round_limit`` fires once when ``apply`` reports ``new_block``.
- TODO(#17): ``check`` calls ``load_inputs`` and ``apply`` per PR under the PR lease, and
  ``list`` and ``show`` print ``derive_state``.
- TODO(#25): ``respond`` calls ``clear_block`` with ``human_respond`` or ``human_force``.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from .config import Config
from .durations import duration_ms
from .errors import CliError
from .store import Lease, cas_update

BLOCK_REASONS: Final = (
    "escalated",
    "failed",
    "stopped",
    "round_limit",
    "no_agent",
    "hook_vetoed",
    "hook_escalated",
)
WAITING_REASONS: Final = (*BLOCK_REASONS, "agent_live", "capacity")
PR_STATES: Final = ("idle", "settling", "queued", "running", "pending", "waiting", "closed")
SETTLE_CEILING_SECONDS: Final = 3600.0  # the second settle bound: a constant, not configurable

# Block reason -> the causes that clear it (the "Cleared when" column).
CLEAR_CAUSES: Final[dict[str, frozenset[str]]] = {
    "escalated": frozenset({"human_respond"}),
    "failed": frozenset({"human_respond"}),
    "stopped": frozenset({"human_respond"}),
    "round_limit": frozenset({"human_force", "max_rounds_raised"}),
    "no_agent": frozenset({"agent_found", "human_respond"}),
    "hook_vetoed": frozenset({"human_respond", "comments_changed"}),
    "hook_escalated": frozenset({"human_respond"}),
}
_ALL_CAUSES: Final = frozenset().union(*CLEAR_CAUSES.values())


@dataclass(frozen=True)
class Block:
    reason: str
    set_at: float  # epoch seconds
    comment_set_sha256: str


@dataclass(frozen=True)
class Candidate:
    """An eligible unhandled comment: its id and its settle timestamp (local first-stored time of
    its current body hash, epoch seconds)."""

    id: str
    settle_ts: float


@dataclass(frozen=True)
class RoundFacts:
    queued: bool = False
    running: bool = False
    counted: int = 0
    record_open: bool = False  # a record is pending, pushing or pushed
    agent_busy: bool = False


@dataclass(frozen=True)
class DueInputs:
    host_state: str
    is_fork: bool
    block: Block | None
    records_current: bool
    base_agent: str | None
    respond_fresh: bool
    max_rounds: int
    settle_seconds: float
    now: float
    eligible: tuple[Candidate, ...]
    facts: RoundFacts


@dataclass(frozen=True)
class Decision:
    due: bool
    why: str
    set_block: str | None = None  # a block to store (round_limit or no_agent)
    clear: str | None = None  # the cause that clears the stored block


@dataclass(frozen=True)
class Applied:
    due: bool
    why: str
    new_block: str | None = None  # set by this call: the caller fires post-attention once
    cleared: str | None = None


def comment_set_sha256(ids: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(sorted(ids)).encode("utf-8")).hexdigest()


def settle_seconds(text: str) -> float:
    try:
        return duration_ms(text) / 1000
    except ValueError:
        raise CliError("internal", "a settle value is not a duration") from None


# ---- pure rules ------------------------------------------------------------------------------


def _check_host_state(state: str) -> bool:
    """True when the PR is closed or merged."""
    if state in ("closed", "merged"):
        return True
    if state != "open":
        raise ValueError("host_state is not open, closed or merged")
    return False


def _bounds_passed(i: DueInputs) -> bool:
    """Either settle bound has passed. The first: the newest eligible comment is at least
    ``settle`` old. The second, a constant: the oldest is at least an hour old. A negative settle
    counts as 0; a comment first stored in the future (the clock went backwards) is never old."""
    if math.isnan(i.settle_seconds):
        raise ValueError("settle is not a number")
    settle = max(0.0, i.settle_seconds)
    newest = max(c.settle_ts for c in i.eligible)
    oldest = min(c.settle_ts for c in i.eligible)
    return i.now - newest >= settle or i.now - oldest >= SETTLE_CEILING_SECONDS


def _agent_available(i: DueInputs) -> bool:
    return bool(i.base_agent) or i.respond_fresh


def decide(i: DueInputs) -> Decision:
    """The due rule, condition by condition in the documented order. The returned ``clear`` and
    ``set_block`` are the automatic block transitions that go with the decision."""
    if _check_host_state(i.host_state):
        return Decision(False, "not_open")
    if i.is_fork:
        return Decision(False, "fork")
    if not i.records_current:
        return Decision(False, "stale_records")
    block = i.block
    clear: str | None = None
    if block is not None:
        if block.reason == "hook_vetoed" and block.comment_set_sha256 != comment_set_sha256(
            c.id for c in i.eligible
        ):
            clear = "comments_changed"
        elif block.reason == "round_limit" and i.facts.counted < i.max_rounds:
            clear = "max_rounds_raised"
        elif block.reason == "no_agent" and _agent_available(i):
            clear = "agent_found"
        if clear is not None:
            block = None
    if not i.eligible:
        return Decision(False, "nothing_eligible", clear=clear)
    if i.facts.queued or i.facts.running:
        return Decision(False, "round_active", clear=clear)
    if i.facts.record_open:
        return Decision(False, "record_open", clear=clear)
    if block is not None:
        return Decision(False, "blocked")
    if i.facts.counted >= i.max_rounds:
        return Decision(False, "round_limit", set_block="round_limit", clear=clear)
    if not _agent_available(i):
        return Decision(False, "no_agent", set_block="no_agent", clear=clear)
    if i.facts.agent_busy:
        return Decision(False, "agent_busy", clear=clear)
    if not _bounds_passed(i):
        return Decision(False, "settling", clear=clear)
    return Decision(True, "due", clear=clear)


def derive_state(i: DueInputs) -> tuple[str, str | None]:
    """(state, waiting_reason), the first matching row of the PR state table. A fork PR gets no
    round in 0.x, so it reads as ``idle`` (the documented waiting reasons have none for it)."""
    if _check_host_state(i.host_state):
        return "closed", None
    if i.facts.queued:
        return "queued", None
    if i.facts.running:
        return "running", None
    if i.facts.record_open:
        return "pending", None
    if i.block is not None:
        return "waiting", i.block.reason
    if i.is_fork or not i.eligible:
        return "idle", None
    if i.facts.agent_busy:
        return "waiting", "agent_live"
    if not _bounds_passed(i):
        return "settling", None
    return "waiting", "capacity"


# ---- the stored block --------------------------------------------------------------------------


def _parse_block(text: object) -> Block | None:
    if text is None:
        return None
    try:
        doc = json.loads(text) if isinstance(text, str) else None
        if (
            isinstance(doc, dict)
            and set(doc) == {"reason", "set_at", "comment_set_sha256"}  # pyright: ignore[reportUnknownArgumentType]
            and doc["reason"] in BLOCK_REASONS
            and isinstance(doc["set_at"], int | float)
            and not isinstance(doc["set_at"], bool)
            and isinstance(doc["comment_set_sha256"], str)
        ):
            return Block(doc["reason"], float(doc["set_at"]), doc["comment_set_sha256"])
    except ValueError:
        pass
    raise CliError("internal", "a stored PR block is not valid")


def get_block(conn: sqlite3.Connection, pr: str) -> Block | None:
    row = conn.execute("SELECT block FROM prs WHERE pr = ?", (pr,)).fetchone()
    return None if row is None else _parse_block(row[0])


def set_block(lease: Lease, pr: str, reason: str, comment_ids: Iterable[str]) -> bool:
    """Store a block when the PR has none. False when it has one (an episode fires once and a
    block is never replaced) or is unknown. ``lease_lost`` and nothing written without the lease."""
    if reason not in BLOCK_REASONS:
        raise ValueError("reason is not a block reason")
    with lease.transaction() as conn:
        row = conn.execute("SELECT rev, block FROM prs WHERE pr = ?", (pr,)).fetchone()
        if row is None or row[1] is not None:
            return False
        doc = {
            "reason": reason,
            "set_at": lease.store.clock.now().timestamp(),
            "comment_set_sha256": comment_set_sha256(comment_ids),
        }
        return cas_update(
            conn, "prs", "pr", pr, row[0], {"block": json.dumps(doc, separators=(",", ":"))}
        )


def clear_block(lease: Lease, pr: str, cause: str, *, expect: str | None = None) -> bool:
    """Clear the block by a documented ``cause``; ``ValueError`` when the cause is not one that
    clears the stored reason. ``expect`` makes it a no-op (False) unless the stored reason is
    that one. False when there is no block."""
    if cause not in _ALL_CAUSES:
        raise ValueError("cause is not a documented cause")
    with lease.transaction() as conn:
        row = conn.execute("SELECT rev, block FROM prs WHERE pr = ?", (pr,)).fetchone()
        stored = None if row is None else _parse_block(row[1])
        if row is None or stored is None or (expect is not None and stored.reason != expect):
            return False
        if cause not in CLEAR_CAUSES[stored.reason]:
            raise ValueError(f"cause does not clear a {stored.reason} block")
        return cas_update(conn, "prs", "pr", pr, row[0], {"block": None})


def apply(lease: Lease, pr: str, i: DueInputs) -> Applied:
    """``decide`` plus its automatic block transitions, in one lease-checked transaction. When
    the stored block is no longer the one ``i`` was built from, nothing is written and the PR is
    reported as blocked (the caller reads again at the next pass)."""
    decision = decide(i)
    with lease.transaction() as conn:
        row = conn.execute("SELECT rev, block FROM prs WHERE pr = ?", (pr,)).fetchone()
        if row is None or _parse_block(row[1]) != i.block:
            return Applied(False, "blocked")
        rev = row[0]
        if decision.clear is not None:
            if not cas_update(conn, "prs", "pr", pr, rev, {"block": None}):
                return Applied(False, "blocked")
            rev += 1
        new_block: str | None = None
        if decision.set_block is not None:
            doc = {
                "reason": decision.set_block,
                "set_at": lease.store.clock.now().timestamp(),
                "comment_set_sha256": comment_set_sha256(c.id for c in i.eligible),
            }
            if not cas_update(
                conn, "prs", "pr", pr, rev, {"block": json.dumps(doc, separators=(",", ":"))}
            ):
                return Applied(False, "blocked")
            new_block = decision.set_block
    return Applied(decision.due, decision.why, new_block, decision.clear)


# ---- from the stored rows ------------------------------------------------------------------------


def load_inputs(
    conn: sqlite3.Connection, config: Config, pr: str, *, now: float, facts: RoundFacts
) -> DueInputs:
    row = conn.execute(
        "SELECT repo, host_state, is_fork, base_agent, records_current FROM prs WHERE pr = ?", (pr,)
    ).fetchone()
    if row is None:
        raise CliError("internal", "the PR is not stored")
    repo, host_state, is_fork, base_agent, records_current = row
    settings = config.repo_settings(repo)
    eligible = conn.execute(
        "SELECT comment_id, body_seen_at FROM comments"
        " WHERE pr = ? AND state = 'unhandled' AND eligible = 1 ORDER BY body_seen_at, comment_id",
        (pr,),
    ).fetchall()
    return DueInputs(
        host_state=host_state,
        is_fork=bool(is_fork),
        block=get_block(conn, pr),
        records_current=bool(records_current),
        base_agent=base_agent,
        respond_fresh=settings.respond_fresh,
        max_rounds=settings.max_rounds,
        settle_seconds=settle_seconds(settings.settle),
        now=now,
        eligible=tuple(Candidate(cid, ts) for cid, ts in eligible),
        facts=facts,
    )
