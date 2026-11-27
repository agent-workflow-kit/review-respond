"""The due rule, the settle clock, the stored PR block and the derived PR state
(docs/interface.md: Settle and round start). Stage 1.

``decide`` and ``derive_state`` are pure functions of ``DueInputs``, a snapshot that ``load_inputs``
reads from the state database. The settle clock only ever sees ``Candidate.settle_ts``, the local
time review-respond first stored a comment's current body hash (``comments.body_seen_at``, an edit
restarts it), and ``Candidate.unhandled_since``, the local time the comment last became unhandled
(``comments.unhandled_since``, an edit of an unhandled comment never moves it; the one-hour ceiling
counts from it, so edits cannot hold a PR in settling); no forge timestamp can reach either.

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

- ``queued``, ``running``, ``counted`` and (until #29) ``record_open`` come from the rounds table
  (``rounds.round_facts``).
- TODO(#29): ``record_open`` (a record pending, pushing or pushed) comes from the records table.
- ``agent_busy`` is ``prs.agent_busy``, the last observation of the base agent; its writer, and
  the writer of ``prs.base_agent``, is ``lineage.resolve_base`` (#19).
- TODO(#37): ``post-attention`` ``round_limit`` fires once when ``apply`` reports ``new_block``.
- ``respond`` (``respond.py``) calls ``clear_block`` with ``human_respond``, ``human_force`` for
  ``round_limit`` (or ``max_rounds_raised``), under the PR's lease.
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
from .store import Lease, cas_update, pass_lease_key, pr_digest, pr_lease_key

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
    """An eligible unhandled comment: its id, its settle timestamp (local first-stored time of its
    current body hash, epoch seconds) and the local time the comment last became unhandled. Without
    ``unhandled_since`` the comment counts as never edited (unhandled since ``settle_ts``)."""

    id: str
    settle_ts: float
    unhandled_since: float | None = None

    @property
    def since(self) -> float:
        return self.settle_ts if self.unhandled_since is None else self.unhandled_since


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
    """Either settle bound has passed. The first: the newest eligible comment (by ``settle_ts``) is
    at least ``settle`` old. The second, a constant: the first-seen time of the oldest is at least
    an hour ago; an edit restarts ``settle_ts`` but never ``unhandled_since``, which only a return
    to ``unhandled`` from another state restarts. A negative settle
    counts as 0; a comment first stored in the future (the clock went backwards) is never old."""
    if math.isnan(i.settle_seconds):
        raise ValueError("settle is not a number")
    settle = max(0.0, i.settle_seconds)
    newest = max(c.settle_ts for c in i.eligible)
    oldest = min(c.since for c in i.eligible)
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
        if block.reason == "hook_vetoed" and (
            block.comment_set_sha256 != comment_set_sha256(c.id for c in i.eligible)
            # an edit restarts settle_ts, so an edited comment is newer than the block
            or any(c.settle_ts > block.set_at for c in i.eligible)
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


def _require_lease(lease: Lease, pr: str) -> None:
    """The block of a PR is written under that PR's lease or the pass lease, never another's."""
    if lease.key not in (pass_lease_key(), pr_lease_key(pr_digest(pr))):
        raise ValueError("the lease is not the pass lease or the lease of this PR")


def set_block_in(
    conn: sqlite3.Connection, pr: str, reason: str, comment_ids: Iterable[str], *, now: float
) -> bool:
    """``set_block`` inside the caller's open transaction (a round's own: the round row, its
    comments and the block change together). The caller holds a lease that ``lease.transaction``
    checked; the rules are those of ``set_block``."""
    if reason not in BLOCK_REASONS:
        raise ValueError("reason is not a block reason")
    row = conn.execute("SELECT rev, block FROM prs WHERE pr = ?", (pr,)).fetchone()
    if row is None or row[1] is not None:
        return False
    doc = {"reason": reason, "set_at": now, "comment_set_sha256": comment_set_sha256(comment_ids)}
    return cas_update(
        conn, "prs", "pr", pr, row[0], {"block": json.dumps(doc, separators=(",", ":"))}
    )


def set_block(lease: Lease, pr: str, reason: str, comment_ids: Iterable[str]) -> bool:
    """Store a block when the PR has none. False when it has one (an episode fires once and a
    block is never replaced) or is unknown. ``lease_lost`` and nothing written without the lease."""
    if reason not in BLOCK_REASONS:
        raise ValueError("reason is not a block reason")
    _require_lease(lease, pr)
    with lease.transaction() as conn:
        return set_block_in(conn, pr, reason, comment_ids, now=lease.store.clock.now().timestamp())


def clear_block(lease: Lease, pr: str, cause: str, *, expect: str | None = None) -> bool:
    """Clear the block by a documented ``cause``; ``ValueError`` when the cause is not one that
    clears the stored reason. ``expect`` makes it a no-op (False) unless the stored reason is
    that one. False when there is no block."""
    if cause not in _ALL_CAUSES:
        raise ValueError("cause is not a documented cause")
    _require_lease(lease, pr)
    with lease.transaction() as conn:
        row = conn.execute("SELECT rev, block FROM prs WHERE pr = ?", (pr,)).fetchone()
        stored = None if row is None else _parse_block(row[1])
        if row is None or stored is None or (expect is not None and stored.reason != expect):
            return False
        if cause not in CLEAR_CAUSES[stored.reason]:
            raise ValueError(f"cause does not clear a {stored.reason} block")
        return cas_update(conn, "prs", "pr", pr, row[0], {"block": None})


def apply(lease: Lease, pr: str, i: DueInputs) -> Applied:
    """``decide`` plus its automatic block transitions, in one lease-checked transaction. Inside
    it the facts the writes rest on are read again: when the stored block is no longer the one
    ``i`` was built from the answer is ``block_changed``, when the records are no longer current
    ``stale_records`` and when the eligible unhandled ids differ ``stale``; nothing is written in
    any of these cases and the caller reads again at the next pass."""
    _require_lease(lease, pr)
    decision = decide(i)
    with lease.transaction() as conn:
        row = conn.execute(
            "SELECT rev, block, records_current FROM prs WHERE pr = ?", (pr,)
        ).fetchone()
        if row is None:
            raise CliError("internal", "the PR is not stored")
        if not row[2]:
            return Applied(False, "stale_records")
        if _parse_block(row[1]) != i.block:
            return Applied(False, "block_changed")
        if [cid for cid, *_ in _eligible_rows(conn, pr)] != [c.id for c in i.eligible]:
            return Applied(False, "stale")
        rev = row[0]
        if decision.clear is not None:
            if not cas_update(conn, "prs", "pr", pr, rev, {"block": None}):
                return Applied(False, "stale")
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
                return Applied(False, "stale")
            new_block = decision.set_block
    return Applied(decision.due, decision.why, new_block, decision.clear)


# ---- from the stored rows ------------------------------------------------------------------------


def _eligible_rows(conn: sqlite3.Connection, pr: str) -> list[tuple[str, float, float]]:
    """(id, settle_ts, unhandled_since) of the eligible unhandled comments, oldest settle first."""
    rows = conn.execute(
        "SELECT comment_id, body_seen_at, coalesce(unhandled_since, first_seen_at) FROM comments"
        " WHERE pr = ? AND state = 'unhandled' AND eligible = 1 ORDER BY body_seen_at, comment_id",
        (pr,),
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def eligible_ids(conn: sqlite3.Connection, pr: str) -> list[str]:
    """The ids of the eligible unhandled comments (the set a block's hash covers)."""
    return [cid for cid, *_ in _eligible_rows(conn, pr)]


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
        eligible=tuple(Candidate(cid, ts, first) for cid, ts, first in _eligible_rows(conn, pr)),
        facts=facts,
    )
