"""The due rule, the settle clock, the stored PR block and the derived PR state
(docs/interface.md: Settle and round start)."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from review_respond import due
from review_respond.config import Config
from review_respond.detect import CommentRead, get_pr
from review_respond.due import Applied, Candidate, Decision, DueInputs, RoundFacts
from review_respond.eligibility import ELIGIBLE, ineligible
from review_respond.errors import CliError
from review_respond.gateway import Gateway
from review_respond.handled import HandledSet
from review_respond.store import (
    Lease,
    Store,
    cas_update,
    open_state_dir,
    pass_lease_key,
    pr_digest,
    pr_lease_key,
)
from tests.harness import builders
from tests.harness.fakes import Fakes
from tests.test_detect import insert_pr, make, script_comments
from tests.test_handled import INSTALLED, rc

NOW = 100_000.0
HOUR = 3600.0


# ---- the pure rule: a due baseline and one mutation per row -----------------------------------


def inputs(**over: Any) -> DueInputs:
    """A PR that is due: open, one eligible comment first stored 10 minutes ago, settle 5m."""
    base = DueInputs(
        host_state="open",
        is_fork=False,
        block=None,
        records_current=True,
        base_agent="agent-1",
        respond_fresh=False,
        max_rounds=3,
        settle_seconds=300.0,
        now=NOW,
        eligible=(Candidate("gh:rc:c1", NOW - 600),),
        facts=RoundFacts(),
    )
    return replace(base, **over)


def block(reason: str, ids: tuple[str, ...] = ("gh:rc:c1",), at: float = NOW) -> due.Block:
    return due.Block(reason, at, due.comment_set_sha256(ids))


def test_the_baseline_is_due() -> None:
    assert due.decide(inputs()) == Decision(True, "due")


# (id, mutation, expected why); every one is "not due" and writes nothing
NOT_DUE: list[tuple[str, dict[str, Any], str]] = [
    ("closed", {"host_state": "closed"}, "not_open"),
    ("merged", {"host_state": "merged"}, "not_open"),
    ("fork", {"is_fork": True}, "fork"),
    ("stale_records", {"records_current": False}, "stale_records"),
    ("nothing_eligible", {"eligible": ()}, "nothing_eligible"),
    ("round_queued", {"facts": RoundFacts(queued=True)}, "round_active"),
    ("round_running", {"facts": RoundFacts(running=True)}, "round_active"),
    ("record_open", {"facts": RoundFacts(record_open=True)}, "record_open"),
    ("agent_busy", {"facts": RoundFacts(agent_busy=True)}, "agent_busy"),
    ("settling", {"eligible": (Candidate("c", NOW - 299.0),)}, "settling"),
    ("blocked", {"block": block("failed")}, "blocked"),
    ("round_limit", {"facts": RoundFacts(counted=3)}, "round_limit"),
    ("no_agent", {"base_agent": None}, "no_agent"),
]
# the rows that store a block as they refuse
SETS_BLOCK = {"round_limit": "round_limit", "no_agent": "no_agent"}


@pytest.mark.parametrize(("name", "mutation", "why"), NOT_DUE, ids=[i for i, *_ in NOT_DUE])
def test_not_due_rows(name: str, mutation: dict[str, Any], why: str) -> None:
    decision = due.decide(inputs(**mutation))
    assert decision == Decision(False, why, set_block=SETS_BLOCK.get(name))


@pytest.mark.parametrize("reason", due.BLOCK_REASONS)
def test_a_stored_block_keeps_the_pr_not_due(reason: str) -> None:
    # the facts that keep round_limit and no_agent in place are part of the row
    held = inputs(block=block(reason), base_agent=None, facts=RoundFacts(counted=3))
    got = due.decide(held)
    assert got.due is False
    assert got.why == "blocked"
    assert got.clear is None
    assert got.set_block is None


def test_round_limit_is_set_when_the_count_reaches_max_rounds() -> None:
    assert due.decide(inputs(facts=RoundFacts(counted=3))) == Decision(
        False, "round_limit", set_block="round_limit"
    )
    assert due.decide(inputs(facts=RoundFacts(counted=2))).due is True
    assert due.decide(inputs(facts=RoundFacts(counted=99))).set_block == "round_limit"


def test_no_agent_is_set_without_an_agent_and_without_the_fresh_opt_in() -> None:
    assert due.decide(inputs(base_agent=None)) == Decision(False, "no_agent", set_block="no_agent")
    assert due.decide(inputs(base_agent=None, respond_fresh=True)) == Decision(True, "due")
    assert due.decide(inputs(base_agent="", respond_fresh=False)).set_block == "no_agent"


def test_round_limit_wins_over_no_agent() -> None:
    got = due.decide(inputs(base_agent=None, facts=RoundFacts(counted=3)))
    assert got.set_block == "round_limit"


def test_the_limit_and_agent_checks_come_after_the_cheap_conditions() -> None:
    # a PR with a round running never gets a block set by the due check
    got = due.decide(inputs(base_agent=None, facts=RoundFacts(counted=3, running=True)))
    assert got == Decision(False, "round_active")
    # nor does a closed or fork PR, nor one with nothing eligible
    assert due.decide(inputs(base_agent=None, host_state="closed")).set_block is None
    assert due.decide(inputs(base_agent=None, is_fork=True)).set_block is None
    assert due.decide(inputs(base_agent=None, eligible=())).set_block is None
    # nor one whose records are not known to be current
    assert due.decide(inputs(base_agent=None, records_current=False)).set_block is None


def test_agent_busy_is_checked_after_the_block_rows() -> None:
    got = due.decide(inputs(base_agent=None, facts=RoundFacts(agent_busy=True)))
    assert got.set_block == "no_agent"


# -- automatic clears --------------------------------------------------------------------------


def test_round_limit_clears_when_max_rounds_is_raised_above_the_count() -> None:
    held = block("round_limit")
    still = due.decide(inputs(block=held, facts=RoundFacts(counted=3)))
    assert still.clear is None and still.why == "blocked"
    raised = due.decide(inputs(block=held, max_rounds=4, facts=RoundFacts(counted=3)))
    assert raised.clear == "max_rounds_raised"
    assert raised.due is True  # the cleared block no longer holds the PR


def test_no_agent_clears_when_an_agent_is_found_again() -> None:
    held = block("no_agent")
    assert due.decide(inputs(block=held, base_agent=None)).clear is None
    found = due.decide(inputs(block=held, base_agent="agent-2"))
    assert found.clear == "agent_found"
    assert found.due is True
    opted = due.decide(inputs(block=held, base_agent=None, respond_fresh=True))
    assert opted.clear == "agent_found"


def test_hook_vetoed_clears_when_the_eligible_set_differs() -> None:
    held = block("hook_vetoed", ("gh:rc:c1",))
    assert due.decide(inputs(block=held)).clear is None
    new = inputs(
        block=held, eligible=(Candidate("gh:rc:c1", NOW - 600), Candidate("gh:rc:c2", NOW - 600))
    )
    assert due.decide(new).clear == "comments_changed"
    gone = inputs(block=held, eligible=(Candidate("gh:rc:other", NOW - 600),))
    assert due.decide(gone).clear == "comments_changed"


def test_hook_vetoed_clears_when_an_unchanged_set_has_an_edited_comment() -> None:
    # the same ids, but one comment's body changed after the block was set (an edit restarts
    # settle_ts, so it is newer than set_at)
    held = block("hook_vetoed", ("gh:rc:c1",), at=NOW - 300)
    edited = inputs(block=held, eligible=(Candidate("gh:rc:c1", NOW - 100, NOW - 900),))
    assert due.decide(edited).clear == "comments_changed"
    untouched = inputs(block=held, eligible=(Candidate("gh:rc:c1", NOW - 300, NOW - 900),))
    assert due.decide(untouched).clear is None  # at exactly set_at: not newer
    older = inputs(block=held, eligible=(Candidate("gh:rc:c1", NOW - 301),))
    assert due.decide(older).clear is None


def test_an_edit_does_not_clear_the_other_block_reasons() -> None:
    held = block("failed", ("gh:rc:c1",), at=NOW - 300)
    got = due.decide(inputs(block=held, eligible=(Candidate("gh:rc:c1", NOW - 100),)))
    assert got.clear is None and got.why == "blocked"


def test_hook_vetoed_set_ignores_order_in_the_id_set() -> None:
    held = block("hook_vetoed", ("b", "a"))
    same = inputs(block=held, eligible=(Candidate("a", NOW - 600), Candidate("b", NOW - 600)))
    assert due.decide(same).clear is None


@pytest.mark.parametrize("reason", ["escalated", "failed", "stopped", "hook_escalated"])
def test_these_blocks_never_clear_by_themselves(reason: str) -> None:
    held = block(reason, ("old",))
    changed = inputs(block=held, base_agent="x", max_rounds=50)
    got = due.decide(changed)
    assert got.clear is None and got.due is False


def test_a_clear_on_a_pr_that_is_not_open_still_does_nothing_else() -> None:
    got = due.decide(inputs(block=block("no_agent"), host_state="closed"))
    assert got.due is False and got.set_block is None


def test_stale_records_never_clear_the_comment_set_block() -> None:
    held = block("hook_vetoed", ("other",))
    got = due.decide(inputs(block=held, records_current=False))
    assert got.clear is None and got.why == "stale_records"


# -- the settle clock ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ages", "settle", "due_expected"),
    [
        ((300.0,), 300.0, True),  # exactly elapsed
        ((299.999,), 300.0, False),
        ((300.001,), 300.0, True),
        ((0.0,), 0.0, True),  # settle 0: due at once
        ((1000.0, 10.0), 300.0, False),  # the newest comment decides the first bound
        ((1000.0, 300.0), 300.0, True),
        ((HOUR, 1.0), 99999.0, True),  # oldest at exactly 1h: second bound
        ((HOUR - 0.001, 1.0), 99999.0, False),
        ((HOUR + 1, 0.0), 10 * HOUR, True),
        ((5.0,), 10**18, False),  # huge settle, young comment
        ((HOUR,), 10**18, True),  # huge settle, the 1h bound still ends it
        ((5.0,), math.inf, False),
        ((HOUR,), math.inf, True),
        ((-50.0,), 300.0, False),  # first stored in the future: the clock went backwards
        ((-50.0, 4000.0), 300.0, True),  # the old one passes the 1h bound
        ((5.0,), -100.0, True),  # a negative settle is a zero settle
        ((-5.0,), -100.0, False),  # ... which still waits for a future-dated comment
    ],
)
def test_settle_bounds(ages: tuple[float, ...], settle: float, due_expected: bool) -> None:
    eligible = tuple(Candidate(f"c{i}", NOW - age) for i, age in enumerate(ages))
    got = due.decide(inputs(eligible=eligible, settle_seconds=settle))
    assert got.due is due_expected
    assert got.why == ("due" if due_expected else "settling")


def test_an_edited_comment_cannot_hold_a_pr_in_settling_beyond_one_hour() -> None:
    # settle_ts keeps restarting with every edit; the ceiling counts from unhandled_since
    def edited(since_age: float) -> DueInputs:
        return inputs(eligible=(Candidate("c", NOW - 10, NOW - since_age),))

    assert due.decide(edited(HOUR - 1)) == Decision(False, "settling")
    assert due.derive_state(edited(HOUR - 1)) == ("settling", None)
    assert due.decide(edited(HOUR)) == Decision(True, "due")
    assert due.derive_state(edited(HOUR)) == ("waiting", "capacity")


def test_an_unhandled_since_in_the_future_is_never_old() -> None:
    future = inputs(eligible=(Candidate("c", NOW - 10, NOW + 50),), settle_seconds=99999.0)
    assert due.decide(future) == Decision(False, "settling")
    assert due.derive_state(future) == ("settling", None)


def test_a_nan_settle_is_refused() -> None:
    with pytest.raises(ValueError, match="settle"):
        due.decide(inputs(settle_seconds=math.nan))


def test_new_comments_cannot_hold_a_pr_in_settling_beyond_one_hour() -> None:
    # a comment every 4 minutes with a 5 minute settle: the first bound never passes
    ids = [Candidate(f"c{i}", NOW - 240 * i) for i in range(30)]  # newest 0s old, oldest 7160s
    got = due.decide(inputs(eligible=tuple(ids)))
    assert got.due is True
    young = [c for c in ids if NOW - c.settle_ts < HOUR]
    assert due.decide(inputs(eligible=tuple(young))).due is False


def test_the_settle_clock_ignores_every_forge_timestamp() -> None:
    # the inputs carry only local first-seen times: nothing else can move the decision
    assert not any("created" in f or "updated" in f for f in Candidate.__dataclass_fields__)


@pytest.mark.parametrize("text", ["5m", "0", "1h", "250ms", "9999999999999d"])
def test_settle_seconds_parses_the_config_duration(text: str) -> None:
    assert due.settle_seconds(text) == pytest.approx(
        {
            "5m": 300.0,
            "0": 0.0,
            "1h": 3600.0,
            "250ms": 0.25,
            "9999999999999d": 9999999999999 * 86400.0,
        }[text]
    )


@pytest.mark.parametrize("text", ["", "5", "-5m", "5 m", "NaN", "5m\n"])
def test_settle_seconds_refuses_other_text(text: str) -> None:
    with pytest.raises(CliError) as err:
        due.settle_seconds(text)
    assert err.value.code == "internal"


# ---- the derived PR state --------------------------------------------------------------------

# (id, mutation, expected state, expected waiting_reason)
STATES: list[tuple[str, dict[str, Any], str, str | None]] = [
    ("closed", {"host_state": "closed"}, "closed", None),
    ("merged", {"host_state": "merged"}, "closed", None),
    ("closed_beats_round", {"host_state": "closed", "facts": RoundFacts(running=True)}, "closed", None),  # noqa: E501
    ("closed_beats_block", {"host_state": "merged", "block": block("failed")}, "closed", None),
    ("queued", {"facts": RoundFacts(queued=True)}, "queued", None),
    ("running", {"facts": RoundFacts(running=True)}, "running", None),
    ("queued_beats_running", {"facts": RoundFacts(queued=True, running=True)}, "queued", None),
    ("pending", {"facts": RoundFacts(record_open=True)}, "pending", None),
    ("running_beats_pending", {"facts": RoundFacts(running=True, record_open=True)}, "running", None),  # noqa: E501
    ("pending_beats_block", {"facts": RoundFacts(record_open=True), "block": block("failed")}, "pending", None),  # noqa: E501
    *[(f"block_{r}", {"block": block(r)}, "waiting", r) for r in due.BLOCK_REASONS],
    ("block_beats_idle", {"block": block("failed"), "eligible": ()}, "waiting", "failed"),
    ("idle", {"eligible": ()}, "idle", None),
    ("fork_is_idle", {"is_fork": True}, "idle", None),
    ("agent_live", {"facts": RoundFacts(agent_busy=True)}, "waiting", "agent_live"),
    ("agent_live_beats_settling", {"facts": RoundFacts(agent_busy=True), "eligible": (Candidate("c", NOW - 1),)}, "waiting", "agent_live"),  # noqa: E501
    ("settling", {"eligible": (Candidate("c", NOW - 1),)}, "settling", None),
    ("settle_exactly_elapsed", {"eligible": (Candidate("c", NOW - 300),)}, "waiting", "capacity"),
    ("one_hour_bound", {"eligible": (Candidate("c", NOW - 1), Candidate("d", NOW - HOUR))}, "waiting", "capacity"),  # noqa: E501
    ("due_is_capacity_waiting", {}, "waiting", "capacity"),
    ("state_ignores_stale_records", {"records_current": False}, "waiting", "capacity"),
    ("state_ignores_round_limit_until_blocked", {"facts": RoundFacts(counted=9)}, "waiting", "capacity"),  # noqa: E501
]  # fmt: skip


@pytest.mark.parametrize(
    ("mutation", "state", "reason"),
    [(m, s, r) for _, m, s, r in STATES],
    ids=[i for i, *_ in STATES],
)
def test_derived_state(mutation: dict[str, Any], state: str, reason: str | None) -> None:
    assert due.derive_state(inputs(**mutation)) == (state, reason)


def test_every_documented_state_and_waiting_reason_is_covered() -> None:
    assert {s for _, _, s, _ in STATES} == set(due.PR_STATES)
    assert {r for *_, r in STATES if r} == set(due.WAITING_REASONS)


def test_an_unknown_host_state_is_refused() -> None:
    with pytest.raises(ValueError, match="host_state"):
        due.derive_state(inputs(host_state="draft"))
    with pytest.raises(ValueError, match="host_state"):
        due.decide(inputs(host_state=""))


# ---- comment_set_sha256 -------------------------------------------------------------------------


def test_comment_set_sha256_is_the_sorted_ids_joined_by_newline() -> None:
    expected = hashlib.sha256(b"a\nb\nc").hexdigest()
    assert due.comment_set_sha256(["c", "a", "b"]) == expected
    assert due.comment_set_sha256([]) == hashlib.sha256(b"").hexdigest()
    # by the definition (ids joined by a newline) these hash equal; comment ids never contain one
    assert due.comment_set_sha256(["a\nb"]) == due.comment_set_sha256(["a", "b"])


def test_comment_set_sha256_handles_unicode_and_hostile_ids() -> None:
    ids = ["gh:rc:é", "gh:rc:'; DROP TABLE prs;--", "\x00", "\U0001f600"]
    got = due.comment_set_sha256(ids)
    assert got == hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()


# ---- the stored block ----------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path, gateway: Gateway) -> Iterator[Store]:
    opened = Store.open(open_state_dir(tmp_path / "state" / "rr"), write=True, close_state=True)
    opened.clock = gateway.clock
    with opened.transaction() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES ('installed_at', ?)", (INSTALLED,))
    yield opened
    opened.close()


@pytest.fixture
def lease(store: Store) -> Iterator[Lease]:
    held = store.try_acquire(pass_lease_key(), heartbeat=None)
    assert held is not None
    yield held
    held.release()


@pytest.fixture
def pr(store: Store) -> str:
    return insert_pr(store, 7, base_agent="agent-1")


def stored_block(store: Store, ref: str) -> Any:
    return store.conn.execute("SELECT block FROM prs WHERE pr = ?", (ref,)).fetchone()[0]


def rev_of(store: Store, ref: str) -> int:
    return store.conn.execute("SELECT rev FROM prs WHERE pr = ?", (ref,)).fetchone()[0]


def test_the_migration_adds_block_base_agent_and_records_current(store: Store, pr: str) -> None:
    columns = {r[1]: r for r in store.conn.execute("PRAGMA table_info(prs)")}
    assert {"block", "base_agent", "records_current"} <= set(columns)
    assert columns["block"][3] == 0  # nullable
    with pytest.raises(Exception, match="CHECK"), store.transaction() as conn:
        conn.execute("UPDATE prs SET block = 'not json'")


@pytest.mark.parametrize(
    "bad",
    [
        '{"reason": "bogus", "set_at": 1, "comment_set_sha256": "x"}',
        '{"set_at": 1}',
        "[]",
        '"escalated"',
        "{}",
        '{"reason": "failed", "set_at": 1, "comment_set_sha256": "x", "extra": 1}',
    ],
)
def test_the_table_refuses_a_block_that_is_not_documented(store: Store, pr: str, bad: str) -> None:
    with pytest.raises(Exception, match="CHECK"), store.transaction() as conn:
        conn.execute("UPDATE prs SET block = ? WHERE pr = ?", (bad, pr))


def test_a_new_pr_has_no_block_no_agent_and_stale_records(store: Store) -> None:
    pr = insert_pr(store, 70)
    assert due.get_block(store.conn, pr) is None
    row = store.conn.execute(
        "SELECT base_agent, records_current FROM prs WHERE pr = ?", (pr,)
    ).fetchone()
    assert tuple(row) == (None, 0)


def test_set_block_stores_the_documented_object_with_cas(
    store: Store, lease: Lease, pr: str, gateway: Gateway
) -> None:
    before = rev_of(store, pr)
    assert due.set_block(lease, pr, "failed", ["gh:rc:b", "gh:rc:a"]) is True
    got = due.get_block(store.conn, pr)
    assert got == due.Block(
        "failed", gateway.clock.now().timestamp(), due.comment_set_sha256(["gh:rc:a", "gh:rc:b"])
    )
    assert rev_of(store, pr) == before + 1
    doc = json.loads(stored_block(store, pr))
    assert set(doc) == {"reason", "set_at", "comment_set_sha256"}


@pytest.mark.parametrize("reason", due.BLOCK_REASONS)
def test_every_reason_can_be_set(store: Store, lease: Lease, pr: str, reason: str) -> None:
    assert due.set_block(lease, pr, reason, []) is True
    block_ = due.get_block(store.conn, pr)
    assert block_ is not None and block_.reason == reason


@pytest.mark.parametrize("reason", ["", "ESCALATED", "capacity", "agent_live", "bogus", "stopped "])
def test_set_block_refuses_an_undocumented_reason(
    store: Store, lease: Lease, pr: str, reason: str
) -> None:
    with pytest.raises(ValueError, match="reason"):
        due.set_block(lease, pr, reason, [])
    assert stored_block(store, pr) is None


def test_set_block_never_overwrites_a_block(store: Store, lease: Lease, pr: str) -> None:
    assert due.set_block(lease, pr, "failed", ["a"]) is True
    first = stored_block(store, pr)
    rev = rev_of(store, pr)
    assert due.set_block(lease, pr, "failed", ["a", "b"]) is False  # same episode: fires once
    assert due.set_block(lease, pr, "escalated", ["a"]) is False
    assert stored_block(store, pr) == first
    assert rev_of(store, pr) == rev


def test_set_block_on_an_unknown_pr_is_false(lease: Lease) -> None:
    assert due.set_block(lease, "forge.test:org/repo#999", "failed", []) is False


def test_set_block_without_the_lease_writes_nothing(store: Store, lease: Lease, pr: str) -> None:
    lease.release()
    with pytest.raises(CliError) as err:
        due.set_block(lease, pr, "failed", [])
    assert err.value.code == "lease_lost"
    assert stored_block(store, pr) is None


def test_clear_block_without_the_lease_writes_nothing(store: Store, lease: Lease, pr: str) -> None:
    assert due.set_block(lease, pr, "failed", []) is True
    held = stored_block(store, pr)
    lease.release()
    with pytest.raises(CliError) as err:
        due.clear_block(lease, pr, "human_respond")
    assert err.value.code == "lease_lost"
    assert stored_block(store, pr) == held


def test_evaluate_without_the_lease_writes_nothing(store: Store, lease: Lease, pr: str) -> None:
    lease.release()
    with pytest.raises(CliError) as err:
        due.apply(lease, pr, inputs(base_agent=None))
    assert err.value.code == "lease_lost"
    assert stored_block(store, pr) is None


CLEARS = {
    "escalated": {"human_respond"},
    "failed": {"human_respond"},
    "stopped": {"human_respond"},
    "round_limit": {"human_force", "max_rounds_raised"},
    "no_agent": {"agent_found", "human_respond"},
    "hook_vetoed": {"human_respond", "comments_changed"},
    "hook_escalated": {"human_respond"},
}
ALL_CAUSES = {c for causes in CLEARS.values() for c in causes}


def test_the_clear_table_matches_the_documented_one() -> None:
    assert {k: set(v) for k, v in due.CLEAR_CAUSES.items()} == CLEARS


@pytest.mark.parametrize(
    ("reason", "cause"), [(r, c) for r, cs in CLEARS.items() for c in sorted(ALL_CAUSES)]
)
def test_a_block_clears_only_by_its_documented_transitions(
    store: Store, lease: Lease, pr: str, reason: str, cause: str
) -> None:
    assert due.set_block(lease, pr, reason, ["a"]) is True
    if cause in CLEARS[reason]:
        assert due.clear_block(lease, pr, cause) is True
        assert stored_block(store, pr) is None
    else:
        with pytest.raises(ValueError, match="cause"):
            due.clear_block(lease, pr, cause)
        assert stored_block(store, pr) is not None


def test_a_plain_human_respond_does_not_clear_round_limit(
    store: Store, lease: Lease, pr: str
) -> None:
    due.set_block(lease, pr, "round_limit", [])
    with pytest.raises(ValueError, match="cause"):
        due.clear_block(lease, pr, "human_respond")


def test_clear_block_refuses_an_unknown_cause(store: Store, lease: Lease, pr: str) -> None:
    due.set_block(lease, pr, "failed", [])
    for cause in ("", "automatic", "HUMAN_RESPOND", "human_respond "):
        with pytest.raises(ValueError, match="cause"):
            due.clear_block(lease, pr, cause)


def test_clear_block_with_no_block_is_false_and_writes_nothing(
    store: Store, lease: Lease, pr: str
) -> None:
    rev = rev_of(store, pr)
    assert due.clear_block(lease, pr, "human_respond") is False
    assert rev_of(store, pr) == rev


def test_clear_block_can_expect_the_reason_it_saw(store: Store, lease: Lease, pr: str) -> None:
    due.set_block(lease, pr, "failed", [])
    assert due.clear_block(lease, pr, "human_respond", expect="escalated") is False
    assert stored_block(store, pr) is not None
    assert due.clear_block(lease, pr, "human_respond", expect="failed") is True


def test_a_blind_detection_bump_does_not_break_the_block_writers(
    store: Store, lease: Lease, pr: str
) -> None:
    with lease.transaction() as conn:  # a listing: rev moves with no content change
        conn.execute("UPDATE prs SET listed_at = 5, rev = rev + 1 WHERE pr = ?", (pr,))
    assert due.set_block(lease, pr, "failed", []) is True
    with lease.transaction() as conn:
        conn.execute("UPDATE prs SET listed_at = 6, rev = rev + 1 WHERE pr = ?", (pr,))
    assert due.clear_block(lease, pr, "human_respond") is True


def test_a_cas_writer_with_a_stale_rev_loses_to_the_block(
    store: Store, lease: Lease, pr: str
) -> None:
    stale = rev_of(store, pr)
    due.set_block(lease, pr, "failed", [])
    with lease.transaction() as conn:
        assert cas_update(conn, "prs", "pr", pr, stale, {"host_state": "closed"}) is False
    row = store.conn.execute("SELECT host_state FROM prs WHERE pr = ?", (pr,)).fetchone()
    assert row[0] == "open"


def test_a_listing_upsert_leaves_the_block_alone(store: Store, lease: Lease, pr: str) -> None:
    due.set_block(lease, pr, "failed", ["a"])
    held = stored_block(store, pr)
    with lease.transaction() as conn:
        conn.execute(
            "UPDATE prs SET head_sha = ?, listed_at = 9, rev = rev + 1 WHERE pr = ?", ("f" * 40, pr)
        )
    assert stored_block(store, pr) == held


def test_get_block_refuses_a_corrupt_stored_block(store: Store, pr: str) -> None:
    store.conn.execute("PRAGMA ignore_check_constraints = ON")
    with store.transaction() as conn:
        conn.execute("UPDATE prs SET block = ? WHERE pr = ?", ('{"reason": "bogus"}', pr))
    store.conn.execute("PRAGMA ignore_check_constraints = OFF")
    with pytest.raises(CliError) as err:
        due.get_block(store.conn, pr)
    assert err.value.code == "internal"


# -- apply: the due check writes only documented transitions --------------------


def seeded(
    store: Store, handled: HandledSet, ref: str, gateway: Gateway, *ids: str, **over: Any
) -> DueInputs:
    """Real comment rows for ``ids`` (default: one), settled for 10 minutes, then the inputs."""
    names = ids or ("gh:rc:c1",)
    read(store, handled, ref, *[rc(n, created="2026-01-01T12:00:01Z") for n in names])
    advance(gateway, 600)
    return replace(load(store, ref, gateway), **over)


def test_apply_sets_round_limit_once_and_reports_the_new_block(
    store: Store, lease: Lease, handled: HandledSet, pr: str, gateway: Gateway
) -> None:
    probe = seeded(store, handled, pr, gateway, facts=RoundFacts(counted=3))
    first = due.apply(lease, pr, probe)
    assert first.due is False and first.new_block == "round_limit"
    held = due.get_block(store.conn, pr)
    assert held is not None and held.reason == "round_limit"
    assert held.comment_set_sha256 == due.comment_set_sha256(["gh:rc:c1"])
    again = due.apply(lease, pr, replace(probe, block=held))
    assert again.new_block is None and again.why == "blocked"  # fires once per episode


def test_apply_sets_no_agent_then_clears_it_when_an_agent_is_found(
    store: Store, lease: Lease, handled: HandledSet, pr: str, gateway: Gateway
) -> None:
    base = seeded(store, handled, pr, gateway, base_agent=None)
    assert due.apply(lease, pr, base).new_block == "no_agent"
    held = due.get_block(store.conn, pr)
    assert held is not None
    got = due.apply(lease, pr, replace(base, block=held, base_agent="agent-9"))
    assert got.due is True and got.cleared == "agent_found"
    assert due.get_block(store.conn, pr) is None


def test_apply_clears_hook_vetoed_when_the_comment_set_changes(
    store: Store, lease: Lease, handled: HandledSet, pr: str, gateway: Gateway
) -> None:
    due.set_block(lease, pr, "hook_vetoed", ["gh:rc:c1"])
    held = due.get_block(store.conn, pr)
    got = due.apply(
        lease, pr, seeded(store, handled, pr, gateway, "gh:rc:c1", "gh:rc:c2", block=held)
    )
    assert got.cleared == "comments_changed"
    assert due.get_block(store.conn, pr) is None


def test_apply_clears_hook_vetoed_when_a_vetoed_comment_is_edited(
    store: Store, lease: Lease, handled: HandledSet, pr: str, gateway: Gateway
) -> None:
    seeded(store, handled, pr, gateway)
    due.set_block(lease, pr, "hook_vetoed", ["gh:rc:c1"])
    advance(gateway, 60)
    read(store, handled, pr, rc("gh:rc:c1", created="2026-01-01T12:00:01Z", body="edited"))
    got = due.apply(lease, pr, load(store, pr, gateway))
    assert got.cleared == "comments_changed"
    assert due.get_block(store.conn, pr) is None


def test_apply_keeps_hook_vetoed_when_nothing_changed(
    store: Store, lease: Lease, handled: HandledSet, pr: str, gateway: Gateway
) -> None:
    seeded(store, handled, pr, gateway)
    due.set_block(lease, pr, "hook_vetoed", ["gh:rc:c1"])
    advance(gateway, 60)
    read(store, handled, pr, rc("gh:rc:c1", created="2026-01-01T12:00:01Z"))
    got = due.apply(lease, pr, load(store, pr, gateway))
    assert got == Applied(False, "blocked")


def test_apply_reports_a_block_that_changed_under_it_as_block_changed(
    store: Store, lease: Lease, handled: HandledSet, pr: str, gateway: Gateway
) -> None:
    # the caller read no block; a human-respond path set one meanwhile, so nothing is set over it
    probe = seeded(store, handled, pr, gateway, base_agent=None)
    due.set_block(lease, pr, "failed", [])
    got = due.apply(lease, pr, probe)
    assert got == Applied(False, "block_changed")
    block_ = due.get_block(store.conn, pr)
    assert block_ is not None and block_.reason == "failed"


def test_apply_reports_a_block_cleared_underneath_as_block_changed(
    store: Store, lease: Lease, handled: HandledSet, pr: str, gateway: Gateway
) -> None:
    due.set_block(lease, pr, "failed", ["gh:rc:c1"])
    probe = seeded(store, handled, pr, gateway, block=due.get_block(store.conn, pr))
    assert due.clear_block(lease, pr, "human_respond") is True
    rev = rev_of(store, pr)
    assert due.apply(lease, pr, probe) == Applied(False, "block_changed")
    assert due.get_block(store.conn, pr) is None
    assert rev_of(store, pr) == rev


def test_apply_writes_nothing_when_the_records_went_stale_after_the_read(
    store: Store, lease: Lease, handled: HandledSet, pr: str, gateway: Gateway
) -> None:
    probe = seeded(store, handled, pr, gateway, base_agent=None)
    with lease.transaction() as conn:  # a new comment read stamped records_current = 0
        conn.execute("UPDATE prs SET records_current = 0, rev = rev + 1 WHERE pr = ?", (pr,))
    rev = rev_of(store, pr)
    assert due.apply(lease, pr, probe) == Applied(False, "stale_records")
    assert stored_block(store, pr) is None
    assert rev_of(store, pr) == rev


def test_apply_writes_nothing_when_a_comment_was_handled_after_the_read(
    store: Store, lease: Lease, handled: HandledSet, pr: str, gateway: Gateway
) -> None:
    probe = seeded(store, handled, pr, gateway, "gh:rc:c1", "gh:rc:c2", base_agent=None)
    handled.deliver(pr, ["gh:rc:c2"], round_id="r-1")
    handled.mark_handled(pr, "r-1")
    rev = rev_of(store, pr)
    assert due.apply(lease, pr, probe) == Applied(False, "stale")
    assert stored_block(store, pr) is None
    assert rev_of(store, pr) == rev


def test_apply_writes_nothing_when_a_comment_arrived_after_the_read(
    store: Store, lease: Lease, handled: HandledSet, pr: str, gateway: Gateway
) -> None:
    probe = seeded(store, handled, pr, gateway, base_agent=None)
    read(
        store, handled, pr,
        rc("gh:rc:c1", created="2026-01-01T12:00:01Z"),
        rc("gh:rc:c2", created="2026-01-01T12:00:02Z"),
    )  # fmt: skip
    assert due.apply(lease, pr, probe) == Applied(False, "stale")
    assert stored_block(store, pr) is None


def test_apply_of_an_unknown_pr_is_an_internal_error(lease: Lease) -> None:
    with pytest.raises(CliError) as err:
        due.apply(lease, "forge.test:org/repo#404", inputs())
    assert err.value.code == "internal"


def test_apply_writes_nothing_for_a_plain_due_or_not_due_pr(
    store: Store, lease: Lease, handled: HandledSet, pr: str, gateway: Gateway
) -> None:
    probe = seeded(store, handled, pr, gateway)
    rev = rev_of(store, pr)
    assert due.apply(lease, pr, probe).due is True
    assert due.apply(lease, pr, replace(probe, facts=RoundFacts(queued=True))).due is False
    assert rev_of(store, pr) == rev


# -- the lease must be this PR's (or the pass lease) --------------------------------


def test_the_block_writers_refuse_a_lease_of_another_pr(store: Store, pr: str) -> None:
    other = store.try_acquire(pr_lease_key(pr_digest("forge.test:org/repo#999")), heartbeat=None)
    assert other is not None
    try:
        with pytest.raises(ValueError, match="lease"):
            due.set_block(other, pr, "failed", [])
        with pytest.raises(ValueError, match="lease"):
            due.clear_block(other, pr, "human_respond")
        with pytest.raises(ValueError, match="lease"):
            due.apply(other, pr, inputs(base_agent=None))
    finally:
        other.release()
    assert stored_block(store, pr) is None


def test_the_block_writers_accept_the_pr_lease_and_the_pass_lease(store: Store, pr: str) -> None:
    own = store.try_acquire(pr_lease_key(pr_digest(pr)), heartbeat=None)
    assert own is not None
    try:
        assert due.set_block(own, pr, "failed", []) is True
        assert due.clear_block(own, pr, "human_respond") is True
    finally:
        own.release()


# ---- from the stored rows ----------------------------------------------------------------------


@pytest.fixture
def handled(lease: Lease) -> HandledSet:
    return HandledSet(lease, record_open=lambda conn, pr: False)


def read(
    store: Store, handled: HandledSet, ref: str, *comments: Any, complete: bool = True
) -> None:
    stored = get_pr(store.conn, ref)
    assert stored is not None
    judged = {c.id: ELIGIBLE for c in comments}
    handled.record_read(
        stored, CommentRead(ref, builders.HEAD_A, complete, tuple(comments), judged)
    )


def load(store: Store, ref: str, gateway: Gateway, **over: Any) -> DueInputs:
    cfg = Config.model_validate({"settle": "5m", **over.pop("config", {})})
    return due.load_inputs(
        store.conn, cfg, ref, now=gateway.clock.now().timestamp(), facts=RoundFacts(), **over
    )


def advance(gateway: Gateway, seconds: float) -> None:
    gateway.clock.advance(seconds)  # type: ignore[attr-defined]


def test_load_inputs_takes_settle_times_from_local_first_seen_not_forge_times(
    store: Store, handled: HandledSet, pr: str, gateway: Gateway
) -> None:
    # the forge says the comment is a day old, but it was first stored a moment ago
    read(
        store,
        handled,
        pr,
        rc("gh:rc:c1", created="2026-01-01T12:00:01Z", updated="2026-02-01T00:00:00Z"),
    )
    seen = load(store, pr, gateway)
    assert [c.id for c in seen.eligible] == ["gh:rc:c1"]
    assert seen.eligible[0].settle_ts == gateway.clock.now().timestamp()
    assert due.decide(seen).why == "settling"
    advance(gateway, 300)
    assert due.decide(load(store, pr, gateway)).due is True  # settle exactly elapsed


def test_the_due_rule_does_not_trust_last_comments_read_at_alone(
    store: Store, lease: Lease, handled: HandledSet, pr: str, gateway: Gateway
) -> None:
    read(store, handled, pr, rc("gh:rc:c1", created="2026-01-01T12:00:01Z"))
    advance(gateway, 600)
    ok = load(store, pr, gateway)
    assert ok.records_current is True
    # the read stamped last_comments_read_at but its records were not applied (a crash between)
    with lease.transaction() as conn:
        conn.execute(
            "UPDATE prs SET last_comments_read_at = ?, records_current = 0 WHERE pr = ?",
            (gateway.clock.now().timestamp(), pr),
        )
    stale = load(store, pr, gateway)
    assert stale.records_current is False
    assert due.decide(stale) == Decision(False, "stale_records")


def test_only_eligible_unhandled_comments_count(
    store: Store, handled: HandledSet, pr: str, gateway: Gateway
) -> None:
    stored = get_pr(store.conn, pr)
    assert stored is not None
    comments = (
        rc("gh:rc:ok", created="2026-01-01T12:00:01Z"),
        rc("gh:rc:old", created="2025-01-01T00:00:00Z"),  # before install: seen
        rc("gh:rc:bad", created="2026-01-01T12:00:02Z"),  # ineligible
        rc("gh:rc:res", created="2026-01-01T12:00:03Z", resolved=True),
    )
    verdicts = {
        "gh:rc:ok": ELIGIBLE, "gh:rc:old": ELIGIBLE, "gh:rc:bad": ineligible("no_access"),
        "gh:rc:res": ELIGIBLE,
    }  # fmt: skip
    handled.record_read(stored, CommentRead(pr, builders.HEAD_A, True, comments, verdicts))
    assert [c.id for c in load(store, pr, gateway).eligible] == ["gh:rc:ok"]


def test_an_edit_restarts_the_settle_clock_of_that_comment(
    store: Store, handled: HandledSet, pr: str, gateway: Gateway
) -> None:
    read(store, handled, pr, rc("gh:rc:c1", created="2026-01-01T12:00:01Z", body="one"))
    first = load(store, pr, gateway).eligible[0].settle_ts
    advance(gateway, 100)
    read(
        store,
        handled,
        pr,
        rc("gh:rc:c1", created="2026-01-01T12:00:01Z", body="two", updated="2026-01-01T12:01:00Z"),
    )
    assert load(store, pr, gateway).eligible[0].settle_ts == first + 100


def test_a_read_that_is_not_complete_does_not_make_the_records_current(
    store: Store, lease: Lease, handled: HandledSet, pr: str, gateway: Gateway
) -> None:
    read(store, handled, pr, rc("gh:rc:c1", created="2026-01-01T12:00:01Z"))
    with lease.transaction() as conn:
        conn.execute("UPDATE prs SET records_current = 0 WHERE pr = ?", (pr,))
    read(store, handled, pr, rc("gh:rc:c1", created="2026-01-01T12:00:01Z"), complete=False)
    assert load(store, pr, gateway).records_current is False
    read(store, handled, pr, rc("gh:rc:c1", created="2026-01-01T12:00:01Z"))
    assert load(store, pr, gateway).records_current is True


def test_load_inputs_uses_the_per_repo_settings(
    store: Store, handled: HandledSet, pr: str, gateway: Gateway
) -> None:
    repo = f"{builders.HOST}/org/repo"
    cfg = {
        "settle": "5m",
        "max_rounds": 3,
        "repos": {repo: {"settle": "2h", "max_rounds": 7, "respond_fresh": True}},
    }
    seen = due.load_inputs(store.conn, Config.model_validate(cfg), pr, now=NOW, facts=RoundFacts())
    assert (seen.settle_seconds, seen.max_rounds, seen.respond_fresh) == (7200.0, 7, True)
    plain = load(store, pr, gateway)
    assert (plain.settle_seconds, plain.max_rounds, plain.respond_fresh) == (300.0, 3, False)


def test_load_inputs_reads_the_pr_row(store: Store, lease: Lease, gateway: Gateway) -> None:
    ref = insert_pr(store, 8, host_state="merged", is_fork=1, base_agent="agent-5")
    seen = load(store, ref, gateway)
    assert (seen.host_state, seen.is_fork, seen.base_agent) == ("merged", True, "agent-5")
    assert due.derive_state(seen) == ("closed", None)


def test_load_inputs_of_an_unknown_pr_is_an_error(store: Store, gateway: Gateway) -> None:
    with pytest.raises(CliError) as err:
        load(store, "forge.test:org/repo#404", gateway)
    assert err.value.code == "internal"


def test_the_clock_going_backwards_never_makes_a_pr_due(
    store: Store, handled: HandledSet, pr: str, gateway: Gateway
) -> None:
    read(store, handled, pr, rc("gh:rc:c1", created="2026-01-01T12:00:01Z"))
    advance(gateway, -3600)
    seen = load(store, pr, gateway)
    assert due.decide(seen) == Decision(False, "settling")
    assert due.derive_state(seen) == ("settling", None)


def test_an_edit_while_settling_can_not_hold_the_pr_past_one_hour(
    store: Store, handled: HandledSet, pr: str, gateway: Gateway
) -> None:
    read(store, handled, pr, rc("gh:rc:old", created="2026-01-01T12:00:01Z"))
    for step in range(16):  # a new comment every 4 minutes for an hour
        advance(gateway, 240)
        read(
            store, handled, pr,
            rc("gh:rc:old", created="2026-01-01T12:00:01Z"),
            *[rc(f"gh:rc:n{i}", created="2026-01-01T12:00:01Z") for i in range(step + 1)],
        )  # fmt: skip
        seen = load(store, pr, gateway)
        elapsed = 240 * (step + 1)
        assert due.decide(seen).due is (elapsed >= HOUR), step


def test_hostile_comment_ids_do_not_reach_sql(
    store: Store, handled: HandledSet, pr: str, gateway: Gateway
) -> None:
    hostile = "gh:rc:'; DROP TABLE prs;--"
    read(store, handled, pr, rc(hostile, created="2026-01-01T12:00:01Z"))
    assert [c.id for c in load(store, pr, gateway).eligible] == [hostile]
    assert store.conn.execute("SELECT count(*) FROM prs").fetchone()[0] == 1


def test_a_comment_re_edited_every_few_minutes_is_due_one_hour_after_it_became_unhandled(
    store: Store, handled: HandledSet, pr: str, gateway: Gateway
) -> None:
    read(store, handled, pr, rc("gh:rc:c1", created="2026-01-01T12:00:01Z", body="v0"))
    first = load(store, pr, gateway).eligible[0].since
    for step in range(1, 17):  # an edit every 4 minutes: settle_ts keeps restarting
        advance(gateway, 240)
        read(store, handled, pr, rc("gh:rc:c1", created="2026-01-01T12:00:01Z", body=f"v{step}"))
        seen = load(store, pr, gateway)
        assert seen.eligible[0].since == first  # unhandled_since is not restarted by an edit
        elapsed = 240 * step
        assert due.decide(seen).due is (elapsed >= HOUR), step
        expected = ("waiting", "capacity") if elapsed >= HOUR else ("settling", None)
        assert due.derive_state(seen) == expected, step


def test_a_handled_comment_edited_now_gets_a_fresh_settle_window(
    store: Store, handled: HandledSet, pr: str, gateway: Gateway
) -> None:
    read(store, handled, pr, rc("gh:rc:c1", created="2026-01-01T12:00:01Z", body="v0"))
    with store.transaction() as conn:
        conn.execute("UPDATE comments SET state = 'handled', handled_sha256 = body_sha256")
    advance(gateway, 2 * HOUR)  # first stored two hours ago, handled meanwhile
    read(store, handled, pr, rc("gh:rc:c1", created="2026-01-01T12:00:01Z", body="v1"))
    seen = load(store, pr, gateway)
    assert due.decide(seen) == Decision(False, "settling")
    assert due.derive_state(seen) == ("settling", None)
    advance(gateway, 300)
    assert due.decide(load(store, pr, gateway)).due is True


# ---- the detect handshake: records_current is cleared by the stamp ---------------------------


@pytest.fixture
def current_pr(store: Store, pr: str) -> str:
    with store.transaction() as conn:
        conn.execute("UPDATE prs SET records_current = 1 WHERE pr = ?", (pr,))
    return pr


def records_current(store: Store, ref: str) -> int:
    return store.conn.execute("SELECT records_current FROM prs WHERE pr = ?", (ref,)).fetchone()[0]


def test_a_complete_read_clears_records_current_even_without_a_hook(
    store: Store, lease: Lease, fakes: Fakes, gateway: Gateway, current_pr: str
) -> None:
    script_comments(fakes, current_pr, builders.real_shape_comments(current_pr))
    make(gateway, lease, on_read=None).read_comments(current_pr)
    stamped = store.conn.execute(
        "SELECT last_comments_read_at FROM prs WHERE pr = ?", (current_pr,)
    ).fetchone()[0]
    assert stamped == gateway.clock.now().timestamp()
    assert records_current(store, current_pr) == 0


def test_a_complete_read_whose_hook_raises_leaves_records_not_current(
    store: Store, lease: Lease, fakes: Fakes, gateway: Gateway, current_pr: str
) -> None:
    def boom(*_: Any) -> None:
        raise RuntimeError("the hook failed")

    script_comments(fakes, current_pr, builders.real_shape_comments(current_pr))
    with pytest.raises(RuntimeError):
        make(gateway, lease, on_read=boom).read_comments(current_pr)
    assert records_current(store, current_pr) == 0


def test_a_complete_read_with_the_records_hook_ends_current(
    store: Store, lease: Lease, fakes: Fakes, gateway: Gateway, current_pr: str
) -> None:
    script_comments(fakes, current_pr, builders.real_shape_comments(current_pr))
    hook = HandledSet(lease).record_read
    make(gateway, lease, on_read=hook).read_comments(current_pr)
    assert records_current(store, current_pr) == 1


def test_every_writer_of_last_comments_read_at_also_clears_records_current() -> None:
    src = Path(__file__).resolve().parent.parent / "src" / "review_respond"
    found = 0
    for path in sorted(src.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for literal in re.findall(r'"(?:[^"\\]|\\.)*"(?:\s*"(?:[^"\\]|\\.)*")*', text):
            sql = " ".join(re.findall(r'"((?:[^"\\]|\\.)*)"', literal))
            if re.search(r"UPDATE prs SET\b.*\blast_comments_read_at\s*=", sql, re.S):
                found += 1
                assert "records_current = 0" in sql, f"{path.name}: {sql}"
    assert found >= 1  # the scan finds the writer in detect.py
