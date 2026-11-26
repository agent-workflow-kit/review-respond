"""The round table, ids and every row of the round state table (docs/interface.md: Round states).

The expectations are written from the interface table: for each transition, the target state, the
columns and the comment, block and counting effects of its "Same transaction" cell.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from review_respond import rounds as rounds_mod
from review_respond.audit import ROUND_STATES
from review_respond.detect import CommentRead, get_pr
from review_respond.eligibility import ELIGIBLE
from review_respond.errors import CliError
from review_respond.gateway import Gateway
from review_respond.handled import HandledSet
from review_respond.outcomes import RunEnd
from review_respond.rounds import (
    TERMINAL_STATES,
    RoundRow,
    Rounds,
    get_round,
    list_rounds,
    new_round_id,
    resolve_round_id,
    round_facts,
)
from review_respond.store import Lease, Store, open_state_dir, pr_digest, pr_lease_key
from tests.harness import builders
from tests.test_detect import insert_pr
from tests.test_handled import rc

C1, C2 = "gh:rc:c1", "gh:rc:c2"
AGENT = "ag-abcdefgh"
BASE = "ag-bcdefghi"
JOB = "jb-abcdefgh"
HEAD = "e" * 40
LATER = "2999-01-01T00:00:00Z"


@pytest.fixture
def store(tmp_path: Path, gateway: Gateway) -> Iterator[Store]:
    opened = Store.open(open_state_dir(tmp_path / "state" / "rr"), write=True, close_state=True)
    opened.clock = gateway.clock
    with opened.transaction() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('installed_at', ?)", ("2026-01-01T12:00:00Z",)
        )
    yield opened
    opened.close()


class Env:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.pr = insert_pr(store, 7)
        self.lease = store.try_acquire(pr_lease_key(pr_digest(self.pr)), heartbeat=None)
        assert self.lease is not None
        self.hs = HandledSet(self.lease)
        self.rounds = Rounds(self.lease)
        stored = get_pr(store.conn, self.pr)
        assert stored is not None
        self.hs.record_read(
            stored,
            CommentRead(
                self.pr,
                builders.HEAD_A,
                True,
                (rc(C1, created=LATER), rc(C2, body="Other.", created=LATER)),
                {C1: ELIGIBLE, C2: ELIGIBLE},
            ),
        )
        with store.transaction() as conn:
            conn.execute("UPDATE prs SET records_current = 1")

    def take_again(self) -> Lease:
        """The PR lease again, after a test gave it up for a child process."""
        self.lease = self.store.try_acquire(pr_lease_key(pr_digest(self.pr)), heartbeat=None)
        assert self.lease is not None
        return self.lease

    def row(self, rid: str) -> RoundRow:
        found = get_round(self.store.conn, rid)
        assert found is not None
        return found

    def comment(self, cid: str) -> tuple[str, str | None]:
        rec = self.hs.get(self.pr, cid)
        assert rec is not None
        return rec.state, rec.round

    def block(self) -> dict[str, Any] | None:
        text = self.store.conn.execute("SELECT block FROM prs WHERE pr = ?", (self.pr,)).fetchone()[
            0
        ]
        return None if text is None else json.loads(text)

    def queued(self, **kw: Any) -> str:
        return self.rounds.create(self.pr, base_agent_id=BASE, **kw).id

    def running(self, ids: tuple[str, ...] = (C1, C2)) -> str:
        rid = self.queued()
        assert self.rounds.start(rid, ids, head_sha=HEAD).row.state == "running"
        return rid

    def reach(self, state: str) -> str:
        """A round of this PR in ``state``, by the documented path."""
        with self.store.transaction() as conn:  # earlier rounds may have handled the comments
            conn.execute(
                "UPDATE comments SET state = 'unhandled', round = NULL, delivered_sha256 = NULL,"
                " delivered_from = NULL, handled_sha256 = NULL"
            )
        rid = self.queued()
        if state == "queued":
            return rid
        if state == "canceled":
            self.rounds.request_cancel(rid, "pr_closed")
            self.rounds.cancel_flagged(rid)
            return rid
        self.rounds.start(rid, (C1,), head_sha=HEAD)
        if state == "running":
            return rid
        if state == "pending":
            self.rounds.to_pending(rid)
        elif state == "escalated":
            self.rounds.to_escalated(rid, reason="r", summary="s", counted=True)
        elif state == "failed":
            self.rounds.to_failed(rid, "agent_failed", counted=True)
        else:
            self.rounds.to_pending(rid)
            with self.store.transaction() as conn:
                assert self.rounds.settle_pending(conn, rid, state)
        return rid


@pytest.fixture
def env(store: Store) -> Iterator[Env]:
    e = Env(store)
    yield e
    assert e.lease is not None
    e.lease.release()


# ---- ids -----------------------------------------------------------------------------------


def test_a_new_id_is_rd_and_eight_base32_characters() -> None:
    ids = {new_round_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(re.fullmatch(r"rd-[a-z2-7]{8}", i) for i in ids)


def test_a_prefix_of_four_to_eight_characters_resolves_when_unique(env: Env) -> None:
    rid = env.queued()
    for n in (4, 5, 8):
        assert resolve_round_id(env.store.conn, rid[: 3 + n]) == rid


@pytest.mark.parametrize(
    "text", ["rd-", "rd-abc", "rd-ABCD", "rd-abcd1", "rd-abcdefghi", "rp-abcd", "x"]
)
def test_a_malformed_round_id_is_a_usage_error(env: Env, text: str) -> None:
    with pytest.raises(CliError) as err:
        resolve_round_id(env.store.conn, text)
    assert err.value.code == "usage"


def test_an_unknown_round_id_is_not_found(env: Env) -> None:
    with pytest.raises(CliError) as err:
        resolve_round_id(env.store.conn, "rd-aaaaaaaa")
    assert err.value.code == "not_found"


def test_a_prefix_matching_two_rounds_is_ambiguous(env: Env) -> None:
    with env.store.transaction() as conn:
        for rid, number in (("rd-abcd2222", 1), ("rd-abcd3333", 2)):
            conn.execute(
                "INSERT INTO rounds (id, pr, number, state, trigger_kind, created_at, ended_at,"
                " cancel_reason) VALUES (?, ?, ?, 'canceled', 'check', 1, 1, 'stopped')",
                (rid, env.pr, number),
            )
    with pytest.raises(CliError) as err:
        resolve_round_id(env.store.conn, "rd-abcd")
    assert err.value.code == "ambiguous" and err.value.exit_code == 21
    assert resolve_round_id(env.store.conn, "rd-abcd2") == "rd-abcd2222"


# ---- the table -----------------------------------------------------------------------------


def test_the_round_states_are_the_ten_of_the_interface() -> None:
    assert set(ROUND_STATES) == {
        "queued", "running", "pending", "confirmed", "rejected", "expired", "closed",
        "escalated", "failed", "canceled",
    }  # fmt: skip
    assert TERMINAL_STATES == {
        "confirmed", "rejected", "expired", "closed", "escalated", "failed", "canceled",
    }  # fmt: skip


def test_the_table_refuses_a_second_active_round_of_one_pr(env: Env) -> None:
    env.queued()
    with env.store.transaction() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO rounds (id, pr, number, state, trigger_kind, created_at)"
            " VALUES ('rd-aaaaaaaa', ?, 9, 'running', 'check', 1)",
            (env.pr,),
        )


def test_the_table_refuses_an_unknown_state_and_a_malformed_id(env: Env) -> None:
    for rid, state in (("rd-aaaaaaaa", "bogus"), ("rd-AAAAAAAA", "queued"), ("rd-aaa", "queued")):
        with env.store.transaction() as conn, pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO rounds (id, pr, number, state, trigger_kind, created_at)"
                " VALUES (?, ?, 1, ?, 'check', 1)",
                (rid, env.pr, state),
            )


def test_the_table_keeps_terminal_rows_complete(env: Env) -> None:
    cases = [
        ("failed", None, None),  # no failure reason
        ("canceled", 1, None),  # no cancel reason
        ("confirmed", None, None),  # terminal without ended_at
    ]
    for state, ended, _ in cases:
        with env.store.transaction() as conn, pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO rounds (id, pr, number, state, trigger_kind, created_at, ended_at)"
                " VALUES ('rd-aaaaaaaa', ?, 1, ?, 'check', 1, ?)",
                (env.pr, state, ended),
            )


# ---- create --------------------------------------------------------------------------------


def test_create_stores_a_queued_round_with_its_number_and_trigger(env: Env) -> None:
    row = env.rounds.create(
        env.pr, trigger="respond", forced=True, include_existing=True,
        note_sha256="a" * 64, base_agent_id=BASE,
    )  # fmt: skip
    assert re.fullmatch(r"rd-[a-z2-7]{8}", row.id)
    assert (row.pr, row.number, row.state, row.counted, row.rev) == (env.pr, 1, "queued", False, 0)
    assert (row.trigger, row.forced, row.include_existing) == ("respond", True, True)
    assert (row.note_sha256, row.base_agent_id) == ("a" * 64, BASE)
    assert (row.job_id, row.agent_id, row.head_sha, row.comment_ids) == (None, None, None, ())
    assert row.started_at is None and row.ended_at is None and row.created_at > 0


def test_create_numbers_each_new_round_of_the_pr_in_turn(env: Env) -> None:
    numbers = []
    for _ in range(3):
        rid = env.queued()
        numbers.append(env.row(rid).number)
        env.rounds.request_cancel(rid, "stopped")
        env.rounds.cancel_flagged(rid)
    assert numbers == [1, 2, 3]


def test_create_refuses_while_a_round_is_queued_or_running(env: Env) -> None:
    rid = env.queued()
    for _ in range(2):
        with pytest.raises(CliError) as err:
            env.rounds.create(env.pr)
        assert err.value.code == "refused" and err.value.reason == "round_active"
        env.rounds.start(rid, (C1,), head_sha=HEAD)


def test_create_needs_the_pr_lease_of_that_pr(env: Env, store: Store) -> None:
    other = store.try_acquire(pr_lease_key("0" * 20), heartbeat=None)
    assert other is not None
    with pytest.raises(ValueError, match="lease"):
        Rounds(other).create(env.pr)
    other.release()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"trigger": "cron"},
        {"note_sha256": "xyz"},
        {"base_agent_id": "agent one"},
        {"base_agent_id": "AG-ABCDEFGH"},
    ],
)
def test_create_refuses_malformed_arguments(env: Env, kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        env.rounds.create(env.pr, **kwargs)
    assert list_rounds(env.store.conn, env.pr) == []


def test_create_for_an_unknown_pr_is_not_found(env: Env, store: Store) -> None:
    ghost = "git.example.test/org/repo#999"
    lease = store.try_acquire(pr_lease_key(pr_digest(ghost)), heartbeat=None)
    assert lease is not None
    with pytest.raises(CliError) as err:
        Rounds(lease).create(ghost)
    assert err.value.code == "not_found"
    lease.release()


def test_record_job_stores_a_jobsctl_id_or_a_detached_start_time(env: Env) -> None:
    rid = env.queued()
    assert env.rounds.record_job(rid, job_id=JOB).changed
    assert env.row(rid).job_id == JOB
    with pytest.raises(ValueError):
        env.rounds.record_job(rid, job_id="job-1")
    with pytest.raises(ValueError):
        env.rounds.record_job(rid)


def test_record_job_on_a_detached_round_stores_the_time(env: Env) -> None:
    rid = env.queued()
    assert env.rounds.record_job(rid, detached_at=1234.5).changed
    assert env.row(rid).detached_started_at == 1234.5


def test_record_job_is_for_a_queued_round_only(env: Env) -> None:
    rid = env.running()
    assert not env.rounds.record_job(rid, job_id=JOB).changed


# ---- queued -> canceled (four rows) and stop -------------------------------------------------


def test_queued_job_finds_the_cancel_flag_and_ends_canceled_with_the_stored_reason(
    env: Env,
) -> None:
    rid = env.queued()
    assert env.rounds.request_cancel(rid, "pr_closed").changed
    got = env.rounds.cancel_flagged(rid)
    assert got.changed and got.row.state == "canceled"
    row = env.row(rid)
    assert (row.cancel_reason, row.counted, row.ended_at is not None) == ("pr_closed", False, True)
    assert row.cancel_requested_at is not None
    assert env.block() is None  # only `stopped` blocks


def test_a_stop_flagged_queued_round_ends_canceled_and_blocks_the_pr_stopped(env: Env) -> None:
    rid = env.queued()
    env.rounds.request_cancel(rid, "stopped")
    assert env.rounds.cancel_flagged(rid).row.state == "canceled"
    block = env.block()
    assert block is not None and block["reason"] == "stopped"


def test_cancel_flagged_without_a_flag_is_refused(env: Env) -> None:
    rid = env.queued()
    with pytest.raises(ValueError, match="cancel"):
        env.rounds.cancel_flagged(rid)
    assert env.row(rid).state == "queued"


def test_request_cancel_keeps_the_first_reason(env: Env) -> None:
    rid = env.queued()
    assert env.rounds.request_cancel(rid, "stopped").changed
    again = env.rounds.request_cancel(rid, "pr_closed")
    assert not again.changed
    assert env.row(rid).cancel_reason == "stopped"


@pytest.mark.parametrize("reason", ["read_failed", "nothing_to_respond", "x", ""])
def test_request_cancel_takes_only_stopped_or_pr_closed(env: Env, reason: str) -> None:
    rid = env.queued()
    with pytest.raises(ValueError):
        env.rounds.request_cancel(rid, reason)


def test_request_cancel_on_a_pending_or_terminal_round_changes_nothing(env: Env) -> None:
    for state in ("pending", "failed", "confirmed"):
        rid = env.reach(state)
        assert not env.rounds.request_cancel(rid, "stopped").changed
        assert env.row(rid).cancel_requested_at is None


def test_queued_job_reads_the_pr_closed(env: Env) -> None:
    rid = env.queued()
    got = env.rounds.cancel_pr_closed(rid)
    assert got.changed
    row = env.row(rid)
    assert (row.state, row.cancel_reason, row.counted) == ("canceled", "pr_closed", False)
    assert env.block() is None


def test_queued_job_selects_no_comment(env: Env) -> None:
    rid = env.queued()
    env.rounds.cancel_nothing(rid)
    row = env.row(rid)
    assert (row.state, row.cancel_reason, row.counted) == ("canceled", "nothing_to_respond", False)
    assert env.block() is None


def test_a_failed_read_cancels_without_a_block_until_the_third_in_a_row(env: Env) -> None:
    for failures in (1, 2):
        rid = env.queued()
        with env.store.transaction() as conn:
            conn.execute("UPDATE prs SET read_failures = ?", (failures,))
        env.rounds.cancel_read_failed(rid)
        assert (env.row(rid).state, env.row(rid).cancel_reason) == ("canceled", "read_failed")
        assert env.block() is None
    rid = env.queued()
    with env.store.transaction() as conn:
        conn.execute("UPDATE prs SET read_failures = 3")
    env.rounds.cancel_read_failed(rid)
    block = env.block()
    assert block is not None and block["reason"] == "failed"
    assert env.row(rid).counted is False


def test_stop_of_a_queued_round_whose_job_was_canceled_or_never_submitted(env: Env) -> None:
    rid = env.queued()
    env.rounds.request_cancel(rid, "stopped")
    got = env.rounds.cancel_flagged(rid)
    assert (got.row.state, got.row.cancel_reason, got.row.counted) == ("canceled", "stopped", False)


# ---- queued reconcile ------------------------------------------------------------------------


def test_reconcile_of_a_lost_job_fails_the_round_job_lost_and_blocks_failed(env: Env) -> None:
    rid = env.queued()
    got = env.rounds.reconcile_lost(rid)
    assert got.changed
    row = env.row(rid)
    assert (row.state, row.counted) == ("failed", False)
    assert (row.failure_reason, row.failure_detail) == ("job_lost", None)
    block = env.block()
    assert block is not None and block["reason"] == "failed"


def test_reconcile_of_a_lost_job_with_the_cancel_flag_cancels_with_the_stored_reason(
    env: Env,
) -> None:
    rid = env.queued()
    env.rounds.request_cancel(rid, "pr_closed")
    env.rounds.reconcile_lost(rid)
    row = env.row(rid)
    assert (row.state, row.cancel_reason) == ("canceled", "pr_closed")
    assert env.block() is None


def test_reconcile_of_a_lost_job_with_a_stop_flag_blocks_stopped(env: Env) -> None:
    rid = env.queued()
    env.rounds.request_cancel(rid, "stopped")
    env.rounds.reconcile_lost(rid)
    block = env.block()
    assert env.row(rid).state == "canceled" and block is not None and block["reason"] == "stopped"


# ---- queued -> running ---------------------------------------------------------------------


def test_start_delivers_the_selected_comments_and_records_the_head(env: Env) -> None:
    rid = env.queued()
    with env.store.transaction() as conn:
        conn.execute("UPDATE prs SET read_failures = 2")
    got = env.rounds.start(rid, (C1, C2), head_sha=HEAD)
    assert got.changed
    row = env.row(rid)
    assert (row.state, row.head_sha, row.comment_ids) == ("running", HEAD, (C1, C2))
    assert row.started_at is not None and row.counted is False
    assert env.comment(C1) == ("delivered", rid) and env.comment(C2) == ("delivered", rid)
    rec = env.hs.get(env.pr, C1)
    assert rec is not None and rec.delivered_from == "unhandled" and rec.delivered_sha256
    failures = env.store.conn.execute("SELECT read_failures FROM prs").fetchone()[0]
    assert failures == 0  # a round that reaches running resets the count


def test_start_with_no_selectable_comment_ends_canceled_nothing_to_respond(env: Env) -> None:
    rid = env.queued()
    got = env.rounds.start(rid, (), head_sha=HEAD)
    assert got.changed and got.row.state == "canceled"
    assert got.row.cancel_reason == "nothing_to_respond" and got.row.head_sha is None


def test_start_skips_a_comment_edited_since_it_was_shown(env: Env) -> None:
    rid = env.queued()
    rec = env.hs.get(env.pr, C1)
    assert rec is not None
    got = env.rounds.start(
        rid, (C1, C2), head_sha=HEAD, expected={C1: "0" * 64, C2: rec.body_sha256}
    )
    # C2's hash differs from what the stored C2 has, so both are stale: nothing is delivered
    assert got.row.state == "canceled"
    assert env.comment(C1)[0] == "unhandled"


def test_start_delivers_only_the_comments_whose_shown_hash_still_holds(env: Env) -> None:
    rid = env.queued()
    one, two = env.hs.get(env.pr, C1), env.hs.get(env.pr, C2)
    assert one is not None and two is not None
    got = env.rounds.start(
        rid, (C1, C2), head_sha=HEAD, expected={C1: one.body_sha256, C2: "0" * 64}
    )
    assert got.row.state == "running" and got.row.comment_ids == (C1,)
    assert env.comment(C2)[0] == "unhandled"


def test_start_of_an_include_existing_round_also_takes_seen_comments(
    env: Env, store: Store
) -> None:
    with store.transaction() as conn:
        conn.execute("UPDATE comments SET state = 'seen' WHERE comment_id = ?", (C2,))
    rid = env.queued(include_existing=True)
    got = env.rounds.start(rid, (C1, C2), head_sha=HEAD)
    assert got.row.comment_ids == (C1, C2)
    rec = env.hs.get(env.pr, C2)
    assert rec is not None and rec.delivered_from == "seen"


def test_start_without_include_existing_leaves_seen_comments_alone(env: Env, store: Store) -> None:
    with store.transaction() as conn:
        conn.execute("UPDATE comments SET state = 'seen' WHERE comment_id = ?", (C2,))
    rid = env.queued()
    got = env.rounds.start(rid, (C1, C2), head_sha=HEAD)
    assert got.row.comment_ids == (C1,)


@pytest.mark.parametrize("head", ["", "abc", "E" * 40, "e" * 41])
def test_start_refuses_a_malformed_head(env: Env, head: str) -> None:
    rid = env.queued()
    with pytest.raises(ValueError):
        env.rounds.start(rid, (C1,), head_sha=head)
    assert env.row(rid).state == "queued"


def test_start_with_a_duplicate_id_delivers_it_once(env: Env) -> None:
    rid = env.queued()
    got = env.rounds.start(rid, (C1, C1), head_sha=HEAD)
    assert got.row.comment_ids == (C1,)


def test_a_running_round_records_its_agent_once(env: Env) -> None:
    rid = env.running()
    assert env.rounds.record_agent(rid, AGENT, throwaway_id="wt-1").changed
    assert env.row(rid).agent_id == AGENT and env.row(rid).throwaway_id == "wt-1"
    assert env.rounds.record_agent(rid, AGENT).changed is False  # same value: no change
    assert env.row(rid).agent_id == AGENT
    with pytest.raises(ValueError):
        env.rounds.record_agent(rid, "agent-1")


def test_a_run_that_reached_working_marks_the_round_counted(env: Env) -> None:
    rid = env.running()
    assert env.rounds.note_started(rid).changed
    assert env.row(rid).counted is True
    assert not env.rounds.note_started(rid).changed  # already counted


# ---- running -> pending / escalated / canceled / failed ------------------------------------


def test_running_to_pending_hands_the_comments_over_and_runs_the_record_step(env: Env) -> None:
    rid = env.running()
    seen: list[str] = []

    def record(conn: sqlite3.Connection, row: RoundRow) -> None:
        seen.append(row.state)
        conn.execute("SELECT 1")

    got = env.rounds.to_pending(rid, extra=record)
    assert got.changed and got.row.state == "pending" and seen == ["pending"]
    row = env.row(rid)
    assert row.counted is True and row.ended_at is None
    for cid in (C1, C2):
        assert env.comment(cid)[0] == "handled"
    assert env.block() is None


def test_a_failing_record_step_rolls_back_the_whole_transition(env: Env) -> None:
    rid = env.running()

    def boom(conn: sqlite3.Connection, row: RoundRow) -> None:
        raise CliError("internal", "no record")

    with pytest.raises(CliError):
        env.rounds.to_pending(rid, extra=boom)
    assert env.row(rid).state == "running" and env.comment(C1)[0] == "delivered"


def test_running_to_escalated_releases_blocks_and_stores_the_question(env: Env) -> None:
    rid = env.running()
    got = env.rounds.to_escalated(rid, reason="Which API?", summary="I need to know.", counted=True)
    assert got.changed
    row = env.row(rid)
    assert row.state == "escalated" and row.counted is True and row.ended_at is not None
    assert (row.escalation_reason, row.escalation_summary) == ("Which API?", "I need to know.")
    assert env.comment(C1) == ("unhandled", None) and env.comment(C2) == ("unhandled", None)
    block = env.block()
    assert block is not None and block["reason"] == "escalated"


def test_a_hook_escalation_is_not_counted_and_records_its_cause(env: Env) -> None:
    rid = env.running()
    env.rounds.to_escalated(rid, reason=None, summary=None, counted=False, cause="hook")
    row = env.row(rid)
    assert (row.counted, row.escalation_cause) == (False, "hook")


def test_the_stored_question_is_cut_to_4096_bytes_and_scrubbed(env: Env) -> None:
    rid = env.running()
    env.rounds.to_escalated(rid, reason="x" * 5000, summary="\udc80bad", counted=True)
    row = env.row(rid)
    assert row.escalation_reason is not None and len(row.escalation_reason) == 4096
    assert row.escalation_summary is not None and "\udc80" not in row.escalation_summary


def test_the_stored_question_is_cut_at_a_character_boundary_within_4096_bytes(env: Env) -> None:
    rid = env.running()
    # 3-byte characters: 1365 of them are 4095 bytes, the 1366th would cross 4096
    env.rounds.to_escalated(rid, reason="\u20ac" * 2000, summary="a\u20ac" * 3000, counted=True)
    row = env.row(rid)
    assert row.escalation_reason == "\u20ac" * 1365
    assert row.escalation_summary is not None
    assert len(row.escalation_summary.encode("utf-8")) <= 4096
    assert row.escalation_summary.encode("utf-8").decode("utf-8") == row.escalation_summary


def test_running_to_canceled_after_a_stop_releases_and_blocks_stopped(env: Env) -> None:
    rid = env.running()
    env.rounds.request_cancel(rid, "stopped")
    got = env.rounds.to_canceled(rid, counted=True)
    assert got.changed
    row = env.row(rid)
    assert (row.state, row.cancel_reason, row.counted) == ("canceled", "stopped", True)
    assert env.comment(C1) == ("unhandled", None)
    block = env.block()
    assert block is not None and block["reason"] == "stopped"


def test_running_to_canceled_for_a_closed_pr_sets_no_block(env: Env) -> None:
    rid = env.running()
    env.rounds.request_cancel(rid, "pr_closed")
    env.rounds.to_canceled(rid, counted=False)
    assert env.comment(C1) == ("unhandled", None) and env.block() is None


def test_running_to_canceled_without_the_flag_is_refused(env: Env) -> None:
    rid = env.running()
    with pytest.raises(ValueError, match="cancel"):
        env.rounds.to_canceled(rid, counted=False)
    assert env.row(rid).state == "running"


def test_running_to_failed_releases_blocks_failed_and_stores_the_reason(env: Env) -> None:
    rid = env.running()
    got = env.rounds.to_failed(rid, "artifact_invalid", detail="schema", counted=True)
    assert got.changed
    row = env.row(rid)
    assert (row.state, row.counted) == ("failed", True)
    assert (row.failure_reason, row.failure_detail) == ("artifact_invalid", "schema")
    assert env.comment(C1) == ("unhandled", None)
    block = env.block()
    assert block is not None and block["reason"] == "failed"


def test_a_failed_round_does_not_replace_a_block_already_set(env: Env) -> None:
    rid = env.running()
    with env.store.transaction() as conn:
        conn.execute(
            "UPDATE prs SET block = ?",
            (json.dumps({"reason": "no_agent", "set_at": 5, "comment_set_sha256": "0" * 64}),),
        )
    env.rounds.to_failed(rid, "timeout", counted=True)
    block = env.block()
    assert block is not None and block["reason"] == "no_agent"


def test_the_block_records_the_comment_set_released_by_the_round(env: Env) -> None:
    from review_respond.due import comment_set_sha256

    rid = env.running()
    env.rounds.to_failed(rid, "timeout", counted=True)
    block = env.block()
    assert block is not None and block["comment_set_sha256"] == comment_set_sha256([C1, C2])


@pytest.mark.parametrize(
    ("reason", "detail"),
    [
        ("nope", None),
        ("agent_failed", "schema"),
        ("artifact_invalid", "nope"),
        ("job_lost", "json"),
    ],
)
def test_to_failed_refuses_an_open_reason_or_a_misplaced_detail(
    env: Env, reason: str, detail: str | None
) -> None:
    rid = env.running()
    with pytest.raises(ValueError):
        env.rounds.to_failed(rid, reason, detail=detail, counted=True)
    assert env.row(rid).state == "running"


def test_a_running_round_that_the_finalizer_cannot_find_a_job_for_fails_job_lost(env: Env) -> None:
    rid = env.running()
    got = env.rounds.finish_run(rid, RunEnd(command="job_gone"))
    assert got.row.state == "failed" and got.row.failure_reason == "job_lost"


# ---- finish_run applies the outcome table --------------------------------------------------


def test_finish_run_wait_done_valid_goes_pending(env: Env) -> None:
    rid = env.running()
    got = env.rounds.finish_run(
        rid, RunEnd(command="wait", exit=0, phase="session", outcome="done")
    )
    assert got.row.state == "pending" and got.row.counted is True


def test_finish_run_wait_done_with_an_invalid_artifact_fails_with_its_detail(env: Env) -> None:
    rid = env.running()
    end = RunEnd(
        command="wait",
        exit=0,
        phase="session",
        outcome="done",
        artifact_failure="artifact_invalid",
        detail="skill_marker",
    )
    got = env.rounds.finish_run(rid, end)
    assert (got.row.state, got.row.failure_reason, got.row.failure_detail) == (
        "failed", "artifact_invalid", "skill_marker",
    )  # fmt: skip


def test_finish_run_session_exit_13_escalates_with_the_report(env: Env) -> None:
    rid = env.running()
    end = RunEnd(command="wait", exit=13, phase="session", outcome="blocked")
    got = env.rounds.finish_run(rid, end, escalation=("why", "what"))
    assert got.row.state == "escalated" and got.row.counted is True
    assert (got.row.escalation_reason, got.row.escalation_summary) == ("why", "what")


def test_finish_run_launch_exit_13_escalates_uncounted_with_the_hook_cause(env: Env) -> None:
    rid = env.running()
    end = RunEnd(command="launch", exit=13, phase="launch", outcome="escalated")
    got = env.rounds.finish_run(rid, end)
    assert (got.row.state, got.row.counted, got.row.escalation_cause) == (
        "escalated",
        False,
        "hook",
    )


def test_finish_run_reads_the_cancel_flag_from_the_row_not_the_caller(env: Env) -> None:
    rid = env.running()
    env.rounds.request_cancel(rid, "stopped")
    got = env.rounds.finish_run(
        rid, RunEnd(command="wait", exit=15, outcome="killed", end_reason="stopped")
    )
    assert got.row.state == "canceled" and got.row.cancel_reason == "stopped"
    assert got.row.counted is True


def test_finish_run_killed_without_the_flag_fails_agent_failed(env: Env) -> None:
    rid = env.running()
    got = env.rounds.finish_run(
        rid, RunEnd(command="wait", exit=15, outcome="killed", end_reason="stopped")
    )
    assert (got.row.state, got.row.failure_reason) == ("failed", "agent_failed")


def test_finish_run_exit_0_of_resume_keeps_the_round_running_and_counts_a_started_run(
    env: Env,
) -> None:
    rid = env.running()
    got = env.rounds.finish_run(rid, RunEnd(command="resume", exit=0))
    assert not got.changed and got.row.state == "running" and got.row.counted is False
    got = env.rounds.finish_run(rid, RunEnd(command="launch", exit=0, started=True))
    assert got.changed and got.row.state == "running" and got.row.counted is True


def test_finish_run_refuses_a_signal_exit_the_caller_must_map_the_last_run(env: Env) -> None:
    rid = env.running()
    with pytest.raises(ValueError, match="re-read"):
        env.rounds.finish_run(rid, RunEnd(command="wait", exit=143))
    assert env.row(rid).state == "running"


def test_finish_run_sigterm_without_a_flag_fails_job_terminated(env: Env) -> None:
    rid = env.running()
    got = env.rounds.finish_run(rid, RunEnd(command="sigterm"))
    assert (got.row.state, got.row.failure_reason) == ("failed", "job_terminated")


def test_finish_run_never_uncounts_a_counted_round(env: Env) -> None:
    rid = env.running()
    env.rounds.note_started(rid)
    got = env.rounds.finish_run(rid, RunEnd(command="wait", exit=20))
    assert got.row.state == "failed" and got.row.counted is True


def test_finish_run_of_a_queued_round_changes_nothing(env: Env) -> None:
    rid = env.queued()
    got = env.rounds.finish_run(rid, RunEnd(command="wait", exit=18))
    assert not got.changed and got.row.state == "queued"


# ---- pending -> terminal (the record's transaction) --------------------------------------------


@pytest.mark.parametrize("to", ["confirmed", "rejected", "expired", "closed"])
def test_a_pending_round_follows_its_record_to_a_terminal_state(env: Env, to: str) -> None:
    rid = env.reach("pending")
    with env.store.transaction() as conn:
        assert env.rounds.settle_pending(conn, rid, to) is True
    row = env.row(rid)
    assert row.state == to and row.ended_at is not None and row.counted is True


@pytest.mark.parametrize(
    "to", ["queued", "running", "pending", "failed", "escalated", "canceled", "x"]
)
def test_a_pending_round_cannot_go_to_another_state_by_its_record(env: Env, to: str) -> None:
    rid = env.reach("pending")
    with env.store.transaction() as conn, pytest.raises(ValueError):
        env.rounds.settle_pending(conn, rid, to)


def test_settle_pending_of_a_round_in_another_state_changes_nothing(env: Env) -> None:
    rid = env.reach("running")
    with env.store.transaction() as conn:
        assert env.rounds.settle_pending(conn, rid, "confirmed") is False
    assert env.row(rid).state == "running"


def test_settle_pending_twice_changes_it_once(env: Env) -> None:
    rid = env.reach("pending")
    with env.store.transaction() as conn:
        assert env.rounds.settle_pending(conn, rid, "rejected") is True
        assert env.rounds.settle_pending(conn, rid, "closed") is False
    assert env.row(rid).state == "rejected"


# ---- the whole table: every (state, transition) pair ---------------------------------------------

# For each way to move a round, the states it may start from. Written out from the interface.
MOVES: dict[str, tuple[Callable[[Env, str], Any], set[str]]] = {
    "cancel_flagged": (
        lambda e, r: (e.rounds.request_cancel(r, "stopped"), e.rounds.cancel_flagged(r))[1],
        {"queued"},
    ),
    "cancel_pr_closed": (lambda e, r: e.rounds.cancel_pr_closed(r), {"queued"}),
    "cancel_read_failed": (lambda e, r: e.rounds.cancel_read_failed(r), {"queued"}),
    "cancel_nothing": (lambda e, r: e.rounds.cancel_nothing(r), {"queued"}),
    "start": (lambda e, r: e.rounds.start(r, (C2,), head_sha=HEAD), {"queued"}),
    "reconcile_lost": (lambda e, r: e.rounds.reconcile_lost(r), {"queued"}),
    "to_pending": (lambda e, r: e.rounds.to_pending(r), {"running"}),
    "to_escalated": (
        lambda e, r: e.rounds.to_escalated(r, reason=None, summary=None, counted=True),
        {"running"},
    ),
    "to_failed": (lambda e, r: e.rounds.to_failed(r, "timeout", counted=True), {"running"}),
    "to_canceled": (
        lambda e, r: (
            e.rounds.request_cancel(r, "pr_closed")
            if e.row(r).cancel_requested_at is None
            else None,
            e.rounds.to_canceled(r, counted=True),
        )[1],
        {"running"},
    ),
}


@pytest.mark.parametrize("state", ROUND_STATES)
@pytest.mark.parametrize("move", sorted(MOVES))
def test_a_move_changes_a_round_only_from_the_states_the_table_names(
    env: Env, move: str, state: str
) -> None:
    do, allowed = MOVES[move]
    rid = env.reach(state)
    before = env.row(rid)
    got = do(env, rid)
    after = env.row(rid)
    if state in allowed:
        assert got.changed and after.state != state and after.rev > before.rev
    else:
        assert not got.changed
        assert after.state == state
        if state != "queued" and state != "running":
            assert after == before  # terminal and pending rows are immutable here


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
def test_a_terminal_round_never_changes_again(env: Env, state: str) -> None:
    rid = env.reach(state)
    before = env.row(rid)
    for do, _ in MOVES.values():
        do(env, rid)
    for call in (
        lambda: env.rounds.note_started(rid),
        lambda: env.rounds.record_agent(rid, AGENT),
        lambda: env.rounds.record_job(rid, job_id=JOB),
        lambda: env.rounds.request_cancel(rid, "stopped"),
        lambda: env.rounds.finish_run(rid, RunEnd(command="wait", exit=18)),
    ):
        assert not call().changed
    with env.store.transaction() as conn:
        assert env.rounds.settle_pending(conn, rid, "confirmed") is False
    assert env.row(rid) == before


# ---- facts, counting, lookups --------------------------------------------------------------------


def test_round_facts_follow_the_rounds_of_the_pr(env: Env) -> None:
    conn = env.store.conn
    facts = round_facts(conn, env.pr)
    assert (facts.queued, facts.running, facts.counted, facts.record_open) == (
        False,
        False,
        0,
        False,
    )
    rid = env.queued()
    assert round_facts(conn, env.pr).queued is True
    env.rounds.start(rid, (C1,), head_sha=HEAD)
    facts = round_facts(conn, env.pr)
    assert (facts.queued, facts.running, facts.counted) == (False, True, 0)
    env.rounds.note_started(rid)
    assert round_facts(conn, env.pr).counted == 1
    env.rounds.to_pending(rid)
    facts = round_facts(conn, env.pr)
    assert (facts.running, facts.counted, facts.record_open) == (False, 1, True)
    with env.store.transaction() as c:
        env.rounds.settle_pending(c, rid, "rejected")
    facts = round_facts(conn, env.pr)
    assert (facts.record_open, facts.counted) == (False, 1)  # a rejected round still counts


def test_counted_rounds_are_those_whose_run_started_not_every_round(env: Env) -> None:
    queued = env.reach("queued")
    env.rounds.cancel_pr_closed(queued)
    env.reach("canceled")
    assert round_facts(env.store.conn, env.pr).counted == 0
    env.reach("failed")
    env.reach("escalated")
    assert round_facts(env.store.conn, env.pr).counted == 2  # both started their run


def test_facts_of_another_pr_are_not_mixed_in(env: Env, store: Store) -> None:
    env.reach("running")
    other = insert_pr(store, 8)
    assert round_facts(store.conn, other).running is False


def test_list_rounds_is_ordered_by_number(env: Env) -> None:
    ids = [env.reach("failed"), env.reach("canceled"), env.reach("queued")]
    assert [r.id for r in list_rounds(env.store.conn, env.pr)] == ids
    assert [r.number for r in list_rounds(env.store.conn, env.pr)] == [1, 2, 3]


def test_rounds_of_a_pr_cannot_be_read_with_another_prs_lease(env: Env, store: Store) -> None:
    rid = env.reach("running")
    other_pr = insert_pr(store, 9)
    lease = store.try_acquire(pr_lease_key(pr_digest(other_pr)), heartbeat=None)
    assert lease is not None
    with pytest.raises(ValueError, match="lease"):
        Rounds(lease).to_failed(rid, "timeout", counted=True)
    lease.release()


def test_the_round_lease_may_drive_the_transitions_too(env: Env, store: Store) -> None:
    rid = env.reach("running")
    from review_respond.store import round_lease_key

    lease = store.try_acquire(round_lease_key(rid), heartbeat=None)
    assert lease is not None
    got = Rounds(lease).to_failed(rid, "timeout", counted=True)
    assert got.changed
    lease.release()


def test_a_lost_lease_aborts_the_transition_without_writing(env: Env, store: Store) -> None:
    rid = env.reach("running")
    assert env.lease is not None
    with store.transaction() as conn:
        conn.execute("DELETE FROM leases WHERE key = ?", (env.lease.key,))
    with pytest.raises(CliError) as err:
        env.rounds.to_failed(rid, "timeout", counted=True)
    assert err.value.code == "lease_lost"
    assert env.row(rid).state == "running" and env.comment(C1)[0] == "delivered"


def test_an_unknown_round_is_not_found(env: Env) -> None:
    with pytest.raises(CliError) as err:
        env.rounds.to_failed("rd-aaaaaaaa", "timeout", counted=True)
    assert err.value.code == "not_found"
    assert rounds_mod.get_round(env.store.conn, "rd-aaaaaaaa") is None


class FakeLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def emit(self, event: str, fields: Any, *, level: str = "info") -> bool:
        self.events.append((event, dict(fields)))
        return True


def test_every_change_of_state_logs_one_round_state_event_after_commit(
    env: Env, store: Store
) -> None:
    log = FakeLog()
    assert env.lease is not None
    rounds = Rounds(env.lease, log=log)  # type: ignore[arg-type]
    rid = rounds.create(env.pr).id
    rounds.start(rid, (C1,), head_sha=HEAD)
    rounds.to_failed(rid, "timeout", counted=True)
    rounds.to_failed(rid, "timeout", counted=True)  # no change: no event
    assert [(e, f["from"], f["to"], f["reason"]) for e, f in log.events] == [
        ("round_state", None, "queued", "created"),
        ("round_state", "queued", "running", "started"),
        ("round_state", "running", "failed", "timeout"),
    ]
    assert all(f["round"] == rid for _, f in log.events)


# ---- the end of a round queues its post-attention in the same transaction -----------------------


def _stop_then(move: Callable[[Env, str], Any]) -> Callable[[Env, str], Any]:
    def run(env: Env, rid: str) -> Any:
        env.rounds.request_cancel(rid, "stopped")
        return move(env, rid)

    return run


def _failures_then(move: Callable[[Env, str], Any]) -> Callable[[Env, str], Any]:
    def run(env: Env, rid: str) -> Any:
        with env.store.transaction() as conn:
            conn.execute("UPDATE prs SET read_failures = 3")
        return move(env, rid)

    return run


# name -> (state it starts from, the move given its extra step, sets a block)
EndMove = Callable[[Env, str, Any], Any]
ENDS: dict[str, tuple[str, EndMove, bool]] = {
    "escalated": (
        "running",
        lambda e, r, x: e.rounds.to_escalated(r, reason="r", summary="s", counted=True, extra=x),
        True,
    ),
    "failed": (
        "running",
        lambda e, r, x: e.rounds.to_failed(r, "timeout", counted=True, extra=x),
        True,
    ),
    "job_lost": ("queued", lambda e, r, x: e.rounds.reconcile_lost(r, extra=x), True),
    "third_read_failed": (
        "queued",
        lambda e, r, x: _failures_then(lambda e2, r2: e2.rounds.cancel_read_failed(r2, extra=x))(
            e, r
        ),
        True,
    ),
    "flagged_stopped": (
        "queued",
        lambda e, r, x: _stop_then(lambda e2, r2: e2.rounds.cancel_flagged(r2, extra=x))(e, r),
        True,
    ),
    "running_stopped": (
        "running",
        lambda e, r, x: _stop_then(lambda e2, r2: e2.rounds.to_canceled(r2, counted=True, extra=x))(
            e, r
        ),
        True,
    ),
    "finish_run": (
        "running",
        lambda e, r, x: e.rounds.finish_run(r, RunEnd(command="wait", exit=18), extra=x),
        True,
    ),
    "pr_closed": ("queued", lambda e, r, x: e.rounds.cancel_pr_closed(r, extra=x), False),
    "nothing": ("queued", lambda e, r, x: e.rounds.cancel_nothing(r, extra=x), False),
    "start_nothing": (
        "queued",
        lambda e, r, x: e.rounds.start(r, (), head_sha=HEAD, extra=x),
        False,
    ),
}


def _block_text(conn: sqlite3.Connection, pr: str) -> str | None:
    return conn.execute("SELECT block FROM prs WHERE pr = ?", (pr,)).fetchone()[0]


@pytest.mark.parametrize("name", sorted(ENDS))
def test_an_ending_transition_runs_extra_in_its_transaction_after_the_block_is_set(
    env: Env, name: str
) -> None:
    state, move, sets_block = ENDS[name]
    rid = env.reach(state)
    seen: list[tuple[str, bool, bool]] = []

    def extra(conn: sqlite3.Connection, row: RoundRow, new_block: bool) -> None:
        seen.append((row.state, new_block, _block_text(conn, row.pr) is not None))

    got = move(env, rid, extra)
    assert got.changed and got.new_block is sets_block
    assert seen == [(got.row.state, sets_block, sets_block)]
    assert got.row.state in TERMINAL_STATES


@pytest.mark.parametrize("name", sorted(ENDS))
def test_an_extra_that_raises_rolls_back_the_state_change_and_the_block(
    env: Env, name: str
) -> None:
    state, move, _ = ENDS[name]
    rid = env.reach(state)

    def boom(conn: sqlite3.Connection, row: RoundRow, new_block: bool) -> None:
        raise CliError("internal", "no post")

    with pytest.raises(CliError):
        move(env, rid, boom)
    assert env.row(rid).state == state and env.block() is None
    assert env.row(rid).ended_at is None


@pytest.mark.parametrize("name", sorted(n for n, (_, _, blocks) in ENDS.items() if blocks))
def test_new_block_is_false_when_the_pr_already_had_a_block(env: Env, name: str) -> None:
    state, move, _ = ENDS[name]
    rid = env.reach(state)
    with env.store.transaction() as conn:
        conn.execute(
            "UPDATE prs SET block = ?",
            (json.dumps({"reason": "no_agent", "set_at": 5, "comment_set_sha256": "0" * 64}),),
        )
    seen: list[bool] = []
    got = move(env, rid, lambda conn, row, new_block: seen.append(new_block))
    assert got.changed and got.new_block is False and seen == [False]
    block = env.block()
    assert block is not None and block["reason"] == "no_agent"


def test_a_transition_that_does_not_end_the_round_never_runs_extra(env: Env) -> None:
    rid = env.queued()
    seen: list[bool] = []
    got = env.rounds.start(rid, (C1,), head_sha=HEAD, extra=lambda conn, row, nb: seen.append(nb))
    assert got.row.state == "running" and seen == [] and got.new_block is False


# ---- the database keeps the state table's rules ------------------------------------------------


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
def test_the_database_refuses_any_update_of_a_terminal_round(env: Env, state: str) -> None:
    rid = env.reach(state)
    for sql in (
        "UPDATE rounds SET warnings = '[]' WHERE id = ?",
        "UPDATE rounds SET state = 'queued' WHERE id = ?",
        "UPDATE rounds SET rev = rev + 1 WHERE id = ?",
    ):
        with env.store.transaction() as conn, pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, (rid,))
    assert env.row(rid).state == state


def test_the_database_refuses_to_lower_counted(env: Env) -> None:
    rid = env.running()
    env.rounds.note_started(rid)
    with env.store.transaction() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE rounds SET counted = 0 WHERE id = ?", (rid,))
    assert env.row(rid).counted is True


@pytest.mark.parametrize("to", ["queued", "running", "failed", "escalated", "canceled"])
def test_the_database_lets_a_pending_round_only_follow_its_record(env: Env, to: str) -> None:
    rid = env.reach("pending")
    with env.store.transaction() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE rounds SET state = ?, ended_at = 1, failure_reason = 'timeout',"
            " cancel_reason = 'stopped' WHERE id = ?",
            (to, rid),
        )
    assert env.row(rid).state == "pending"


def test_the_database_lets_a_pending_round_take_a_same_state_update(env: Env) -> None:
    rid = env.reach("pending")
    with env.store.transaction() as conn:
        conn.execute("UPDATE rounds SET rev = rev + 1, warnings = ? WHERE id = ?", ('["w"]', rid))
    assert env.row(rid).state == "pending"


@pytest.mark.parametrize("to", ["confirmed", "rejected", "expired", "closed"])
def test_the_database_lets_a_pending_round_settle(env: Env, to: str) -> None:
    rid = env.reach("pending")
    with env.store.transaction() as conn:
        conn.execute("UPDATE rounds SET state = ?, ended_at = 1 WHERE id = ?", (to, rid))
    assert env.row(rid).state == to


# ---- an agent id is never replaced -------------------------------------------------------------


def test_recording_a_different_agent_is_internal_and_changes_nothing(env: Env) -> None:
    rid = env.running()
    env.rounds.record_agent(rid, AGENT)
    before = env.row(rid)
    with pytest.raises(CliError) as err:
        env.rounds.record_agent(rid, "ag-zzzzzzzz")
    assert err.value.code == "internal"
    assert env.row(rid) == before


# ---- the lease clock ----------------------------------------------------------------------------


def test_settle_pending_stamps_the_lease_clock_and_logs_the_state_change(
    env: Env, store: Store
) -> None:
    rid = env.reach("pending")
    log = FakeLog()
    assert env.lease is not None
    rounds = Rounds(env.lease, log=log)  # type: ignore[arg-type]
    store.clock.advance(5000)  # type: ignore[attr-defined]
    with store.transaction() as conn:
        assert rounds.settle_pending(conn, rid, "rejected") is True
        assert rounds.settle_pending(conn, rid, "closed") is False
    assert env.row(rid).ended_at == store.clock.now().timestamp()
    assert [(e, f["from"], f["to"]) for e, f in log.events] == [
        ("round_state", "pending", "rejected")
    ]


# ---- a stored cancel flag wins over what the job saw ---------------------------------------------


@pytest.mark.parametrize("seen", ["cancel_pr_closed", "cancel_nothing", "cancel_read_failed"])
def test_a_stored_stop_flag_wins_over_the_reason_the_job_saw(env: Env, seen: str) -> None:
    rid = env.queued()
    env.rounds.request_cancel(rid, "stopped")
    got = getattr(env.rounds, seen)(rid)
    assert got.changed and got.row.cancel_reason == "stopped"
    block = env.block()
    assert block is not None and block["reason"] == "stopped" and got.new_block is True


def test_a_stored_pr_closed_flag_wins_over_the_third_failed_read(env: Env) -> None:
    rid = env.queued()
    env.rounds.request_cancel(rid, "pr_closed")
    with env.store.transaction() as conn:
        conn.execute("UPDATE prs SET read_failures = 3")
    got = env.rounds.cancel_read_failed(rid)
    assert got.row.cancel_reason == "pr_closed" and got.new_block is False
    assert env.block() is None


@pytest.mark.parametrize(("flag", "blocks"), [("stopped", True), ("pr_closed", False)])
def test_a_flagged_start_with_no_selection_ends_canceled_with_the_flag_reason(
    env: Env, flag: str, blocks: bool
) -> None:
    rid = env.queued()
    env.rounds.request_cancel(rid, flag)
    got = env.rounds.start(rid, (), head_sha=HEAD)
    assert got.changed and got.row.state == "canceled" and got.row.cancel_reason == flag
    block = env.block()
    assert (block is not None and block["reason"] == "stopped") is blocks
    assert (block is None) is not blocks
