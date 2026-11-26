"""The handled set: comment records, states and transitions (docs/interface.md: Handled set)."""

from __future__ import annotations

import hashlib
import json
import os
import select
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from review_respond import handled as handled_mod
from review_respond.detect import CommentRead, ReadComment, get_pr
from review_respond.eligibility import ELIGIBLE, Verdict, ineligible
from review_respond.errors import CliError
from review_respond.gateway import Gateway
from review_respond.handled import MAX_STORED_BODY_BYTES, CommentRecord, HandledSet
from review_respond.store import Lease, Store, open_state_dir, pass_lease_key, pr_lease_key
from tests.harness import adversarial, builders
from tests.harness.builders import PR
from tests.harness.fakes import Fakes
from tests.harness.plugin import World
from tests.test_detect import insert_pr, make, script_comments

WORKER = Path(__file__).parent / "harness" / "handled_worker.py"
INSTALLED = "2026-01-01T12:00:00Z"
BEFORE = "2026-01-01T11:59:59Z"
AFTER = "2026-01-01T12:00:01Z"
CID = "gh:rc:c1"


# ---- fixtures and helpers ------------------------------------------------------------------


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
    return insert_pr(store, 7)


class Env:
    """A handled set over one stored PR, with a manual clock and a switchable open record."""

    def __init__(self, store: Store, lease: Lease, ref: str, gateway: Gateway) -> None:
        self.store, self.lease, self.ref, self.gateway = store, lease, ref, gateway
        self.open_record = False
        self.hs = HandledSet(lease, record_open=lambda conn, pr: self.open_record)

    def advance(self, seconds: float) -> None:
        self.gateway.clock.advance(seconds)  # type: ignore[attr-defined]

    def read(
        self,
        *comments: ReadComment,
        complete: bool = True,
        verdicts: dict[str, Verdict] | None = None,
    ) -> None:
        stored = get_pr(self.store.conn, self.ref)
        assert stored is not None
        judged = verdicts if verdicts is not None else {c.id: ELIGIBLE for c in comments}
        self.hs.record_read(
            stored, CommentRead(self.ref, builders.HEAD_A, complete, tuple(comments), judged)
        )

    def get(self, cid: str = CID) -> CommentRecord:
        found = self.hs.get(self.ref, cid)
        assert found is not None
        return found


@pytest.fixture
def env(store: Store, lease: Lease, pr: str, gateway: Gateway) -> Env:
    return Env(store, lease, pr, gateway)


def rc(
    cid: str = CID,
    body: str = "Please rename this.",
    *,
    created: str = AFTER,
    updated: str | None = None,
    resolved: bool = False,
    outdated: bool | None = False,
    author: str | None = "acct-rev",
    thread: str = "gh:rt:t1",
    path: str | None = "src/app.py",
    line: int | None = 3,
    minimized: bool = False,
) -> ReadComment:
    return ReadComment(
        id=cid,
        thread_id=thread,
        author_id=author,
        author_login="reviewer-one" if author else None,
        author_is_bot=False,
        body=body,
        created_at=created,
        updated_at=updated or created,
        minimized=minimized,
        reply_to=None,
        thread_resolved=resolved,
        thread_resolvable=True,
        thread_outdated=outdated,
        path=path,
        line=line,
    )


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def table_rows(store: Store) -> list[tuple[Any, ...]]:
    return store.conn.execute("SELECT * FROM comments ORDER BY pr, comment_id").fetchall()


# ---- the table -----------------------------------------------------------------------------


def test_the_comments_table_is_the_next_migration(store: Store) -> None:
    columns = {r[1] for r in store.conn.execute("PRAGMA table_info(comments)")}
    assert {
        "pr", "comment_id", "body_sha256", "state", "round", "delivered_sha256",
        "handled_sha256", "delivered_from", "first_seen_at", "body_seen_at", "handled_reason",
        "edited", "eligible", "reason", "rev",
    } <= columns  # fmt: skip
    assert store.schema_version >= 5


def test_the_table_refuses_an_unknown_state_and_a_delivered_row_without_a_round(
    store: Store, env: Env
) -> None:
    env.read(rc())
    for sql in (
        "UPDATE comments SET state = 'bogus'",
        "UPDATE comments SET state = 'delivered'",  # no round, no delivered_sha256
        "UPDATE comments SET handled_reason = 'because'",
        "UPDATE comments SET delivered_from = 'handled'",
    ):
        with pytest.raises(Exception, match="CHECK"), store.transaction() as conn:
            conn.execute(sql)


# ---- first sight ---------------------------------------------------------------------------


def test_a_comment_created_before_installation_is_seen(env: Env) -> None:
    env.read(rc(created=BEFORE))
    record = env.get()
    assert record.state == "seen"
    assert record.round is None
    assert record.handled_sha256 is None


def test_a_comment_created_at_or_after_installation_is_unhandled(env: Env) -> None:
    env.read(rc("gh:rc:at", created=INSTALLED), rc("gh:rc:after", created=AFTER))
    assert env.get("gh:rc:at").state == "unhandled"
    assert env.get("gh:rc:after").state == "unhandled"


def test_first_sight_is_decided_per_comment_in_one_read(env: Env) -> None:
    env.read(rc("gh:rc:old", created=BEFORE), rc("gh:rc:new", created=AFTER))
    assert env.get("gh:rc:old").state == "seen"
    assert env.get("gh:rc:new").state == "unhandled"


def test_first_sight_compares_instants_not_text(env: Env) -> None:
    env.read(
        rc("gh:rc:east", created="2026-01-01T13:30:00+02:00"),  # 11:30Z, before
        rc("gh:rc:west", created="2026-01-01T07:30:00-05:00"),  # 12:30Z, after
        rc("gh:rc:frac", created="2026-01-01T11:59:59.999999Z"),  # before
        rc("gh:rc:lower", created="2026-01-01t12:00:00z"),  # equal: unhandled
        rc("gh:rc:leap", created="2026-01-01T11:59:60Z"),  # leap second: 12:00:00, not before
    )
    got = {c: env.get(f"gh:rc:{c}").state for c in ("east", "west", "frac", "lower", "leap")}
    assert got == {
        "east": "seen", "west": "unhandled", "frac": "seen", "lower": "unhandled",
        "leap": "unhandled",
    }  # fmt: skip


def test_a_later_read_never_reclassifies_a_seen_comment(env: Env) -> None:
    env.read(rc(created=BEFORE))
    env.advance(3600)
    env.read(rc(created=BEFORE))
    assert env.get().state == "seen"


def test_a_read_before_installed_at_exists_is_an_internal_error(env: Env, store: Store) -> None:
    with store.transaction() as conn:
        conn.execute("DELETE FROM meta WHERE key = 'installed_at'")
    with pytest.raises(CliError) as caught:
        env.read(rc())
    assert caught.value.code == "internal"
    assert table_rows(store) == []


def test_a_first_sight_comment_in_a_resolved_thread_is_handled_as_resolved(env: Env) -> None:
    env.read(
        rc("gh:rc:a", resolved=True),
        rc("gh:rc:b", resolved=True, created=BEFORE),
        verdicts={"gh:rc:a": ineligible("resolved"), "gh:rc:b": ineligible("resolved")},
    )
    for cid in ("gh:rc:a", "gh:rc:b"):
        record = env.get(cid)
        assert (record.state, record.handled_reason) == ("handled", "resolved")
        assert record.handled_sha256 == record.body_sha256
        assert record.round is None


def test_times_are_the_local_clock_not_the_host_clock(env: Env) -> None:
    now = env.gateway.clock.now().timestamp()
    env.read(rc(created="2020-01-01T00:00:00Z", updated="2099-01-01T00:00:00Z"))
    record = env.get()
    assert record.first_seen_at == now
    assert record.body_seen_at == now
    assert record.host_updated_at == "2099-01-01T00:00:00Z"


# ---- eligibility verdicts ------------------------------------------------------------------


def test_the_verdict_is_stored_and_re_evaluated_on_every_read(env: Env) -> None:
    env.read(rc(), verdicts={CID: ineligible("unknown_access")})
    assert (env.get().eligible, env.get().reason) == (False, "unknown_access")
    env.read(rc(), verdicts={CID: ELIGIBLE})
    assert (env.get().eligible, env.get().reason) == (True, None)
    env.read(rc(), verdicts={CID: ineligible("no_access")})
    assert (env.get().eligible, env.get().reason) == (False, "no_access")


@pytest.mark.parametrize("reason", ["own", "bot", "unknown_author", "no_access", "unknown_access"])
def test_an_ineligible_comment_stays_unhandled_and_is_not_delivered(env: Env, reason: str) -> None:
    env.read(rc(), verdicts={CID: ineligible(reason)})
    assert env.get().state == "unhandled"
    assert env.hs.deliver(env.ref, [CID], round_id="r-1") == []
    assert env.get().state == "unhandled"


def test_a_comment_without_a_verdict_is_ineligible(env: Env) -> None:
    env.read(rc(), verdicts={})
    assert (env.get().eligible, env.get().reason) == (False, "unknown_access")


def test_a_null_author_is_stored_as_null(env: Env) -> None:
    env.read(rc(author=None), verdicts={CID: ineligible("unknown_author")})
    assert env.get().author_id is None


# ---- idempotence ---------------------------------------------------------------------------


def test_reading_the_same_comments_again_changes_nothing(env: Env, store: Store) -> None:
    comments = (rc("gh:rc:a"), rc("gh:rc:b", created=BEFORE), rc("gh:rc:c", resolved=True))
    verdicts = {"gh:rc:a": ELIGIBLE, "gh:rc:b": ELIGIBLE, "gh:rc:c": ineligible("resolved")}
    env.read(*comments, verdicts=verdicts)
    before = table_rows(store)
    for _ in range(3):
        env.advance(600)
        env.read(*comments, verdicts=verdicts)
    assert table_rows(store) == before  # no rev bump, no moved time


def test_a_changed_updated_at_with_an_equal_hash_does_nothing(env: Env) -> None:
    env.read(rc())
    first = env.get()
    env.advance(60)
    env.read(rc(updated="2026-01-01T13:00:00Z"))
    again = env.get()
    assert again.state == "unhandled"
    assert again.body_seen_at == first.body_seen_at
    assert again.edited is False


# ---- edits ---------------------------------------------------------------------------------


def test_an_edit_of_an_unhandled_comment_restarts_its_settle_time(env: Env) -> None:
    env.read(rc(body="one"))
    first = env.get().body_seen_at
    env.advance(120)
    env.read(rc(body="two"))
    record = env.get()
    assert record.state == "unhandled"
    assert record.body_sha256 == sha("two")
    assert record.body_seen_at == first + 120
    assert record.first_seen_at == first  # the comment itself was first stored once


def _handled(env: Env, body: str = "one", cid: str = CID) -> None:
    env.read(rc(cid, body=body))
    assert env.hs.deliver(env.ref, [cid], round_id="r-1") == [cid]
    assert env.hs.mark_handled(env.ref, "r-1") == [cid]


def test_an_edited_handled_comment_becomes_unhandled_and_edited(env: Env) -> None:
    _handled(env)
    env.advance(30)
    env.read(rc(body="two"))
    record = env.get()
    assert (record.state, record.edited) == ("unhandled", True)
    assert record.handled_reason is None
    assert record.body_sha256 == sha("two")
    assert record.handled_sha256 == sha("one")  # what was answered stays on record
    assert (record.round, record.delivered_sha256, record.delivered_from) == (None, None, None)


def test_an_edit_is_held_back_while_a_record_of_the_pr_is_open(env: Env) -> None:
    _handled(env)
    env.open_record = True
    env.read(rc(body="two"))
    record = env.get()
    assert (record.state, record.edited) == ("handled", False)
    assert record.body_sha256 == sha("two")  # the new hash is stored and its settle time starts
    env.open_record = False
    env.read(rc(body="two"))  # the record ended: the next read acts on it
    assert (env.get().state, env.get().edited) == ("unhandled", True)


def test_an_edit_of_a_comment_whose_author_is_no_longer_eligible_stays_handled(env: Env) -> None:
    _handled(env)
    env.read(rc(body="two"), verdicts={CID: ineligible("no_access")})
    assert env.get().state == "handled"


def test_an_edit_during_the_round_keeps_delivered_until_the_round_ends(env: Env) -> None:
    env.read(rc(body="one"))
    env.hs.deliver(env.ref, [CID], round_id="r-1")
    env.read(rc(body="two"))
    record = env.get()
    assert (record.state, record.delivered_sha256, record.body_sha256) == (
        "delivered", sha("one"), sha("two"),
    )  # fmt: skip
    env.hs.mark_handled(env.ref, "r-1")
    assert env.get().state == "handled"
    assert env.get().handled_sha256 == sha("one")
    env.read(rc(body="two"))  # the next read applies the edit row
    assert (env.get().state, env.get().edited) == ("unhandled", True)


def test_an_edit_that_restores_the_old_text_while_a_record_is_open_is_not_seen(env: Env) -> None:
    _handled(env, "one")
    env.open_record = True
    env.read(rc(body="two"))
    env.read(rc(body="one"))
    env.open_record = False
    env.read(rc(body="one"))
    record = env.get()
    assert (record.state, record.edited) == ("handled", False)
    assert record.body_sha256 == sha("one")


def test_a_reopened_comment_restored_to_its_old_text_stays_unhandled(env: Env) -> None:
    # conservative: a comment that was reopened is answered again rather than silently dropped;
    # the restored text restarts the settle time like any other edit
    _handled(env, "one")
    env.read(rc(body="two"))
    env.advance(90)
    env.read(rc(body="one"))
    record = env.get()
    assert (record.state, record.edited) == ("unhandled", True)
    assert record.body_sha256 == sha("one")
    assert record.body_seen_at == record.first_seen_at + 90


def test_an_edit_and_its_restoration_between_two_reads_leaves_no_trace(env: Env) -> None:
    _handled(env, "one")
    before = env.get()
    env.read(rc(body="one", updated="2026-01-02T00:00:00Z"))
    assert env.get().state == "handled"
    assert env.get().rev == before.rev + 1  # only the host's updated_at moved


# ---- resolved and outdated threads ---------------------------------------------------------


@pytest.mark.parametrize("start", ["seen", "unhandled", "handled"])
def test_a_resolved_thread_marks_a_comment_handled_as_resolved(env: Env, start: str) -> None:
    if start == "seen":
        env.read(rc(created=BEFORE))
    elif start == "unhandled":
        env.read(rc())
    else:
        _handled(env)
    env.read(rc(resolved=True), verdicts={CID: ineligible("resolved")})
    record = env.get()
    assert record.state == "handled"
    if start != "handled":
        assert record.handled_reason == "resolved"
    if start == "handled":
        assert record.round == "r-1"  # the answer already given stays on record
        assert record.handled_sha256 == sha("one")
        assert record.handled_reason is None  # a round-handled comment gains no reason
        assert record.thread_resolved
    else:
        assert record.handled_sha256 == record.body_sha256


def test_a_delivered_comment_keeps_delivered_when_its_thread_is_resolved(env: Env) -> None:
    env.read(rc())
    env.hs.deliver(env.ref, [CID], round_id="r-1")
    env.read(rc(resolved=True), verdicts={CID: ineligible("resolved")})
    assert env.get().state == "delivered"
    env.hs.mark_handled(env.ref, "r-1")
    env.read(rc(resolved=True), verdicts={CID: ineligible("resolved")})
    assert (env.get().state, env.get().handled_reason) == ("handled", None)


def test_resolve_then_unresolve_then_redo_still_returns_a_round_handled_comment(env: Env) -> None:
    _handled(env)
    env.read(rc(body="one", resolved=True), verdicts={CID: ineligible("resolved")})
    env.read(rc(body="one", resolved=False))
    assert env.hs.redo(env.ref, "r-1") == [CID]
    assert env.get().state == "unhandled"


def test_a_resolution_clears_edited_on_the_comment_it_hands_over(env: Env) -> None:
    _handled(env, "one")
    env.read(rc(body="two"))
    assert env.get().edited
    env.read(rc(body="two", resolved=True), verdicts={CID: ineligible("resolved")})
    record = env.get()
    assert (record.state, record.handled_reason, record.edited) == ("handled", "resolved", False)
    assert record.handled_sha256 == sha("two")


def test_an_edit_made_while_the_thread_is_resolved_reopens_when_it_is_unresolved(env: Env) -> None:
    env.read(rc(body="one", resolved=True), verdicts={CID: ineligible("resolved")})
    env.read(rc(body="two", resolved=True), verdicts={CID: ineligible("resolved")})
    assert env.get().state == "handled"
    env.read(rc(body="two", resolved=False))
    assert (env.get().state, env.get().edited) == ("unhandled", True)


def test_an_unresolved_thread_stays_handled(env: Env) -> None:
    env.read(rc(resolved=True), verdicts={CID: ineligible("resolved")})
    env.read(rc(resolved=False))
    assert (env.get().state, env.get().handled_reason) == ("handled", "resolved")


def test_an_edit_after_a_thread_was_unresolved_reopens_the_comment(env: Env) -> None:
    env.read(rc(resolved=True), verdicts={CID: ineligible("resolved")})
    env.read(rc(body="changed", resolved=False))
    record = env.get()
    assert (record.state, record.edited, record.handled_reason) == ("unhandled", True, None)


def test_an_edit_in_a_still_resolved_thread_does_not_reopen(env: Env) -> None:
    env.read(rc(resolved=True), verdicts={CID: ineligible("resolved")})
    env.read(rc(body="changed", resolved=True), verdicts={CID: ineligible("resolved")})
    assert env.get().state == "handled"


def test_an_outdated_thread_changes_nothing_but_the_stored_flag(env: Env) -> None:
    env.read(rc(outdated=True, line=None))
    record = env.get()
    assert (record.state, record.eligible, record.outdated, record.line) == (
        "unhandled", True, True, None,
    )  # fmt: skip
    env.read(rc(outdated=None, line=None))
    assert env.get().outdated is None
    assert env.get().state == "unhandled"


# ---- deletion ------------------------------------------------------------------------------


@pytest.mark.parametrize("start", ["seen", "unhandled", "handled"])
def test_a_comment_absent_from_a_complete_read_is_deleted(env: Env, start: str) -> None:
    if start == "seen":
        env.read(rc(created=BEFORE))
    elif start == "unhandled":
        env.read(rc())
    else:
        _handled(env)
    env.read(rc("gh:rc:other"))
    assert env.get().state == "deleted"
    assert env.get("gh:rc:other").state == "unhandled"


def test_an_incomplete_read_deletes_nothing(env: Env) -> None:
    env.read(rc())
    env.read(complete=False)
    assert env.get().state == "unhandled"


def test_an_incomplete_read_still_records_the_comments_it_has(env: Env) -> None:
    env.read(rc("gh:rc:a"), complete=False)
    assert env.get("gh:rc:a").state == "unhandled"


def test_an_empty_complete_read_deletes_every_comment_of_that_pr_only(
    env: Env, store: Store
) -> None:
    other = insert_pr(store, 8)
    env.read(rc("gh:rc:a"))
    stored_other = get_pr(store.conn, other)
    assert stored_other is not None
    env.hs.record_read(
        stored_other,
        CommentRead(other, builders.HEAD_A, True, (rc("gh:rc:b"),), {"gh:rc:b": ELIGIBLE}),
    )
    env.read()
    assert env.get("gh:rc:a").state == "deleted"
    other_record = env.hs.get(other, "gh:rc:b")
    assert other_record is not None
    assert other_record.state == "unhandled"


def test_a_delivered_comment_keeps_delivered_when_deleted_during_its_round(env: Env) -> None:
    env.read(rc())
    env.hs.deliver(env.ref, [CID], round_id="r-1")
    env.read()
    assert env.get().state == "delivered"
    env.hs.mark_handled(env.ref, "r-1")
    env.read()
    assert env.get().state == "deleted"


def test_deleted_is_terminal_even_when_the_id_comes_back(env: Env) -> None:
    env.read(rc(body="one"))
    env.read()
    env.read(rc(body="two"))
    record = env.get()
    assert record.state == "deleted"
    assert record.body_sha256 == sha("one")
    assert env.hs.deliver(env.ref, [CID], round_id="r-1") == []


def test_a_deleted_comment_is_not_deleted_twice(env: Env) -> None:
    env.read(rc())
    env.read()
    rev = env.get().rev
    env.read()
    assert env.get().rev == rev


# ---- round transitions ---------------------------------------------------------------------


def test_a_job_delivers_an_unhandled_comment(env: Env) -> None:
    env.read(rc())
    assert env.hs.deliver(env.ref, [CID], round_id="r-1") == [CID]
    record = env.get()
    assert (record.state, record.delivered_from, record.round) == ("delivered", "unhandled", "r-1")
    assert record.delivered_sha256 == sha("Please rename this.")


def test_a_seen_comment_is_delivered_only_when_the_round_includes_existing(env: Env) -> None:
    env.read(rc(created=BEFORE))
    assert env.hs.deliver(env.ref, [CID], round_id="r-1") == []
    assert env.get().state == "seen"
    assert env.hs.deliver(env.ref, [CID], round_id="r-1", include_seen=True) == [CID]
    assert (env.get().state, env.get().delivered_from) == ("delivered", "seen")


def test_deliver_skips_handled_deleted_and_unknown_ids(env: Env) -> None:
    _handled(env, cid="gh:rc:h")
    env.read(rc("gh:rc:h", body="one"), rc("gh:rc:u"))
    assert env.hs.deliver(env.ref, ["gh:rc:h", "gh:rc:u", "gh:rc:nope"], round_id="r-2") == [
        "gh:rc:u"
    ]
    assert env.get("gh:rc:h").round == "r-1"


def test_a_round_that_becomes_pending_hands_its_comments_over(env: Env) -> None:
    env.read(rc("gh:rc:a"), rc("gh:rc:b"), rc("gh:rc:c"))
    env.hs.deliver(env.ref, ["gh:rc:a", "gh:rc:b"], round_id="r-1")
    assert sorted(env.hs.mark_handled(env.ref, "r-1")) == ["gh:rc:a", "gh:rc:b"]
    for cid in ("gh:rc:a", "gh:rc:b"):
        record = env.get(cid)
        assert (record.state, record.handled_reason) == ("handled", None)
        assert record.handled_sha256 == record.delivered_sha256
    assert env.get("gh:rc:c").state == "unhandled"
    assert env.hs.mark_handled(env.ref, "r-1") == []  # idempotent


def test_a_round_of_another_id_does_not_touch_the_comment(env: Env) -> None:
    env.read(rc())
    env.hs.deliver(env.ref, [CID], round_id="r-1")
    assert env.hs.mark_handled(env.ref, "r-2") == []
    assert env.hs.release(env.ref, "r-2") == []
    assert env.hs.redo(env.ref, "r-2") == []
    assert env.get().state == "delivered"


@pytest.mark.parametrize("source", ["unhandled", "seen"])
def test_a_round_that_fails_returns_its_comments_to_where_they_came_from(
    env: Env, source: str
) -> None:
    env.read(rc(created=BEFORE if source == "seen" else AFTER))
    env.hs.deliver(env.ref, [CID], round_id="r-1", include_seen=True)
    assert env.hs.release(env.ref, "r-1") == [CID]
    assert env.get().state == source


def test_reject_redo_makes_the_rounds_handled_comments_unhandled(env: Env) -> None:
    _handled(env)
    assert env.hs.redo(env.ref, "r-1") == [CID]
    assert env.get().state == "unhandled"
    assert env.hs.redo(env.ref, "r-1") == []  # a second redo finds nothing


def test_redo_leaves_resolved_comments_handled(env: Env) -> None:
    _handled(env, cid="gh:rc:a")
    env.read(
        rc("gh:rc:a", body="one"),
        rc("gh:rc:b", resolved=True),
        verdicts={"gh:rc:a": ELIGIBLE, "gh:rc:b": ineligible("resolved")},
    )
    assert env.hs.redo(env.ref, "r-1") == ["gh:rc:a"]
    assert env.get("gh:rc:b").state == "handled"


def test_a_record_that_ends_without_redo_changes_no_comment(env: Env) -> None:
    _handled(env)
    before = env.get()
    env.read(rc(body="one"))  # the next read finds the same text
    assert env.get() == before


# ---- stored text ---------------------------------------------------------------------------


def test_the_body_hash_covers_the_whole_body_and_the_text_is_stored(env: Env) -> None:
    env.read(rc(body="naïve 日本語 \U0001f600"))
    record = env.get()
    assert record.body_sha256 == sha("naïve 日本語 \U0001f600")
    stored = env.hs.body(env.ref, CID)
    assert stored.text == "naïve 日本語 \U0001f600"
    assert stored.truncated is False


def test_an_empty_body_is_stored(env: Env) -> None:
    env.read(rc(body=""))
    assert env.get().body_sha256 == sha("")
    assert env.hs.body(env.ref, CID).text == ""


def test_a_huge_body_is_cut_for_storage_but_hashed_whole(env: Env) -> None:
    body = "é" * (MAX_STORED_BODY_BYTES)  # 2 bytes each: the cut falls inside nothing
    env.read(rc(body=body + "tail"))
    record = env.get()
    assert record.body_sha256 == sha(body + "tail")
    assert record.body_bytes == len((body + "tail").encode())
    stored = env.hs.body(env.ref, CID)
    assert stored.truncated is True
    assert len(stored.text.encode()) <= MAX_STORED_BODY_BYTES
    assert stored.text.startswith("é")
    assert stored.original_bytes == record.body_bytes


def test_the_cut_never_splits_a_character(env: Env) -> None:
    env.read(rc(body="a" + "\U0001f600" * MAX_STORED_BODY_BYTES))
    text = env.hs.body(env.ref, CID).text
    assert text.startswith("a\U0001f600")
    assert text.endswith("\U0001f600")


def test_stored_text_is_returned_sanitized(env: Env) -> None:
    hostile = "ok\x1b[31m‮evil​\nsecond\rline\x00"
    env.read(rc(body=hostile))
    text = env.hs.body(env.ref, CID).text
    assert "\x1b" not in text and "‮" not in text and "\x00" not in text
    assert "<U+001B>" in text and "<U+202E>" in text and "<U+0000>" in text
    assert "\n" in text  # line structure survives; the raw bytes stay in the file
    assert "<U+000D>" in text


@pytest.mark.parametrize("name", sorted(adversarial.HOSTILE_COMMENT_TEXTS))
def test_hostile_texts_are_carried_inertly(env: Env, name: str) -> None:
    text = adversarial.HOSTILE_COMMENT_TEXTS[name]
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        pytest.skip("a lone surrogate never reaches the store: the read check refuses it")
    env.read(rc(body=text))
    assert env.get().body_sha256 == sha(text)
    shown = env.hs.body(env.ref, CID).text
    assert all(ord(ch) >= 0x20 or ch == "\n" for ch in shown)


@pytest.mark.parametrize("name", sorted(adversarial.SIZE_EXTREMES))
def test_size_extremes_are_stored_bounded(env: Env, name: str) -> None:
    text = adversarial.SIZE_EXTREMES[name]
    env.read(rc(body=text))
    assert env.get().body_sha256 == sha(text)
    assert len(env.hs.body(env.ref, CID).text.encode()) <= MAX_STORED_BODY_BYTES * 4  # markers


def test_the_same_text_is_stored_once_and_an_edit_adds_one_file(env: Env, store: Store) -> None:
    owner_dir = store.state_path / "files" / get_pr(store.conn, env.ref).digest  # type: ignore[union-attr]
    env.read(rc(body="one"))
    env.read(rc(body="one"))
    assert len(list(owner_dir.iterdir())) == 1
    env.read(rc(body="two"))
    assert len(list(owner_dir.iterdir())) == 2


def test_a_missing_stored_file_is_state_unsafe_not_empty_text(env: Env, store: Store) -> None:
    env.read(rc(body="one"))
    digest = get_pr(store.conn, env.ref).digest  # type: ignore[union-attr]
    for entry in (store.state_path / "files" / digest).iterdir():
        entry.unlink()
    with pytest.raises(CliError) as caught:
        env.hs.body(env.ref, CID)
    assert caught.value.code == "state_unsafe"


def test_the_body_of_an_unknown_comment_is_an_internal_error(env: Env) -> None:
    with pytest.raises(CliError) as caught:
        env.hs.body(env.ref, "gh:rc:nope")
    assert caught.value.code == "internal"


# ---- leases and compare-and-set ------------------------------------------------------------


def test_a_lost_lease_writes_nothing_not_even_a_file(env: Env, store: Store) -> None:
    with store.transaction() as conn:
        conn.execute("DELETE FROM leases")
    with pytest.raises(CliError) as caught:
        env.read(rc())
    assert caught.value.code == "lease_lost"
    assert table_rows(store) == []
    assert not (store.state_path / "files").exists()


def test_a_lease_lost_between_the_file_and_the_rows_writes_no_rows(
    env: Env, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = store.source.put_file  # type: ignore[union-attr]

    def put_then_lose(owner: str, data: bytes) -> str:
        result = real(owner, data)
        with store.transaction() as conn:
            conn.execute("DELETE FROM leases")
        return result

    monkeypatch.setattr(store.source, "put_file", put_then_lose)
    with pytest.raises(CliError) as caught:
        env.read(rc())
    assert caught.value.code == "lease_lost"
    assert table_rows(store) == []


def test_a_read_only_store_cannot_record(
    env: Env, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(store, "read_only", True)
        with pytest.raises(CliError) as caught:
            env.read(rc())
    assert caught.value.code == "version_skew"


def test_every_state_change_is_a_compare_and_set_that_fails_loudly(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.read(rc())
    calls: list[str] = []

    def lost(
        conn: Any, table: str, key_col: str, key: object, rev: int, changes: Any, **kw: Any
    ) -> bool:
        calls.append(table)
        return False

    monkeypatch.setattr(handled_mod, "cas_update", lost)
    with pytest.raises(CliError) as caught:
        env.read(rc(body="changed"))
    assert caught.value.code == "internal"
    assert calls  # the edit went through cas_update
    with pytest.raises(CliError):
        env.hs.deliver(env.ref, [CID], round_id="r-1")


def test_a_stale_cas_leaves_the_row_alone(env: Env, store: Store) -> None:
    from review_respond.store import cas_update

    env.read(rc())
    rev = env.get().rev
    with env.lease.transaction() as conn:
        assert cas_update(conn, "comments", "comment_id", CID, rev, {"state": "seen"})
        assert not cas_update(conn, "comments", "comment_id", CID, rev, {"state": "handled"})
    assert env.get().state == "seen"


# ---- two readers racing, in real processes -------------------------------------------------


def spawn(world: World, store: Store, go: Path, doc: Path, key: str) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [sys.executable, "-I", str(WORKER), str(store.state_path), str(go), PR, str(doc), key],
        env=world.env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    world.procs.record(proc.pid)
    return proc


def line(proc: subprocess.Popen[bytes], timeout: float = 30.0) -> str:
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    buf = b""
    while b"\n" not in buf:
        left = deadline - time.monotonic()
        assert left > 0, f"worker {proc.pid} said nothing (have {buf!r})"
        ready, _, _ = select.select([proc.stdout.fileno()], [], [], left)
        if ready:
            chunk = os.read(proc.stdout.fileno(), 4096)
            assert chunk, f"worker {proc.pid} exited (have {buf!r})"
            buf += chunk
    return buf.split(b"\n", 1)[0].decode()


def write_doc(path: Path, comments: list[ReadComment], *, complete: bool = True) -> Path:
    doc = {
        "complete": complete,
        "comments": [asdict(c) for c in comments],
        "verdicts": {c.id: [True, None] for c in comments},
    }
    path.write_text(json.dumps(doc))
    return path


def race(world: World, store: Store, tmp_path: Path, docs: list[Path]) -> list[str]:
    go = tmp_path / "go"
    procs = [spawn(world, store, go, doc, pr_lease_key(f"{i:020x}")) for i, doc in enumerate(docs)]
    try:
        assert [line(p) for p in procs] == ["READY"] * len(docs)
        go.write_text("go")
        results = [line(p) for p in procs]
        for p in procs:
            assert p.wait(timeout=30) == 0
        return results
    finally:
        for p in procs:
            if p.poll() is None:
                p.wait(timeout=30)


def test_two_readers_first_seeing_a_comment_store_one_row(
    world: World, store: Store, pr: str, tmp_path: Path
) -> None:
    doc = write_doc(tmp_path / "doc.json", [rc("gh:rc:a"), rc("gh:rc:b", created=BEFORE)])
    assert race(world, store, tmp_path, [doc, doc]) == ["DONE", "DONE"]
    rows = store.conn.execute("SELECT comment_id, state FROM comments ORDER BY 1").fetchall()
    assert rows == [("gh:rc:a", "unhandled"), ("gh:rc:b", "seen")]


def test_two_readers_of_an_edit_make_one_transition(
    world: World, store: Store, lease: Lease, pr: str, tmp_path: Path, gateway: Gateway
) -> None:
    hs = HandledSet(lease)
    stored = get_pr(store.conn, pr)
    assert stored is not None
    hs.record_read(stored, CommentRead(pr, "a" * 40, True, (rc(body="one"),), {CID: ELIGIBLE}))
    hs.deliver(pr, [CID], round_id="r-1")
    hs.mark_handled(pr, "r-1")
    seeded = hs.get(pr, CID)
    assert seeded is not None
    doc = write_doc(tmp_path / "doc.json", [rc(body="two")])
    assert race(world, store, tmp_path, [doc, doc]) == ["DONE", "DONE"]
    after = hs.get(pr, CID)
    assert after is not None
    assert (after.state, after.edited, after.body_sha256) == ("unhandled", True, sha("two"))
    assert after.rev == seeded.rev + 1  # exactly one writer made the transition


def test_a_reader_and_a_deleting_reader_end_in_a_consistent_row(
    world: World, store: Store, lease: Lease, pr: str, tmp_path: Path
) -> None:
    hs = HandledSet(lease)
    stored = get_pr(store.conn, pr)
    assert stored is not None
    hs.record_read(stored, CommentRead(pr, "a" * 40, True, (rc(body="one"),), {CID: ELIGIBLE}))
    keep = write_doc(tmp_path / "keep.json", [rc(body="one")])
    drop = write_doc(tmp_path / "drop.json", [])
    assert race(world, store, tmp_path, [keep, drop]) == ["DONE", "DONE"]
    after = hs.get(pr, CID)
    assert after is not None and after.state == "deleted"  # terminal: no order revives it


# ---- through detection ---------------------------------------------------------------------


def detect_into(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease, doc: dict[str, Any]
) -> None:
    script_comments(fakes, PR, doc)
    hs = HandledSet(lease)
    make(gateway, lease, on_read=hs.record_read).read_comments(PR)


def test_a_real_shape_read_fills_the_comments_with_their_verdicts(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease, pr: str
) -> None:
    detect_into(gateway, fakes, store, lease, builders.real_shape_comments())
    got = {
        r[0]: r[1:]
        for r in store.conn.execute(
            "SELECT comment_id, state, eligible, reason, handled_reason FROM comments"
        )
    }
    assert got == {
        "gh:rc:PRRC_kwDOabc-1": ("unhandled", 1, None, None),
        "gh:rc:PRRC_kwDOabc-2": ("unhandled", 0, "own", None),
        "gh:rc:PRRC_kwDOabc-3": ("unhandled", 0, "bot", None),
        "gh:ic:IC_kwDOabc-4": ("unhandled", 0, "unknown_author", None),
        "gh:rc:PRRC_kwDOabc-5": ("handled", 0, "resolved", "resolved"),
    }


def test_the_comment_lifecycle_runs_through_detection(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease, pr: str
) -> None:
    scenario = builders.scenario_comment_lifecycle("Please rename this.")
    hs = HandledSet(lease, record_open=lambda conn, ref: False)

    def read(doc: dict[str, Any]) -> CommentRecord:
        script_comments(fakes, PR, doc)
        make(gateway, lease, on_read=hs.record_read).read_comments(PR)
        record = hs.get(PR, "gh:rc:c1")
        assert record is not None
        return record

    first = read(scenario["fresh"])
    assert first.state == "unhandled"
    hs.deliver(PR, ["gh:rc:c1"], round_id="r-1")
    hs.mark_handled(PR, "r-1")
    assert read(scenario["incomplete_listing"]).state == "handled"
    assert read(scenario["outdated_thread"]).state == "handled"
    assert read(scenario["edited"]).state == "unhandled"
    assert read(scenario["resolved_thread"]).handled_reason == "resolved"
    assert read(scenario["deleted_comment"]).state == "deleted"


def test_an_unjudged_failed_read_records_nothing(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease, pr: str
) -> None:
    fakes.script(
        "forgectl", "comments", json=builders.error_envelope("upstream_error", "x"), exit=12
    )
    hs = HandledSet(lease)
    with pytest.raises(CliError):
        make(gateway, lease, on_read=hs.record_read).read_comments(PR)
    assert table_rows(store) == []


def test_record_open_default_is_no_record(env: Env, store: Store, lease: Lease) -> None:
    plain = HandledSet(lease)
    stored = get_pr(store.conn, env.ref)
    assert stored is not None
    c = (rc(body="one"),)
    plain.record_read(stored, CommentRead(env.ref, "a" * 40, True, c, {CID: ELIGIBLE}))
    plain.deliver(env.ref, [CID], round_id="r-1")
    plain.mark_handled(env.ref, "r-1")
    c2 = (rc(body="two"),)
    plain.record_read(stored, CommentRead(env.ref, "a" * 40, True, c2, {CID: ELIGIBLE}))
    assert plain.get(env.ref, CID).state == "unhandled"  # type: ignore[union-attr]


def test_a_read_for_another_pr_than_the_stored_one_is_refused(env: Env, store: Store) -> None:
    stored = get_pr(store.conn, env.ref)
    assert stored is not None
    with pytest.raises(CliError) as caught:
        env.hs.record_read(stored, CommentRead(PR + "9", "a" * 40, True, (), {}))
    assert caught.value.code == "internal"


# ---- review round 1 ------------------------------------------------------------------------


def test_records_lists_a_pr_and_filters_by_state(env: Env) -> None:
    env.read(rc("gh:rc:a"), rc("gh:rc:b"), rc("gh:rc:c", created=BEFORE))
    env.hs.deliver(env.ref, ["gh:rc:a"], round_id="r-1")
    assert [r.id for r in env.hs.records(env.ref)] == ["gh:rc:a", "gh:rc:b", "gh:rc:c"]
    assert [r.id for r in env.hs.records(env.ref, "unhandled")] == ["gh:rc:b"]
    assert [r.id for r in env.hs.records(env.ref, "delivered")] == ["gh:rc:a"]
    assert env.hs.records(env.ref, "handled") == []
    assert env.hs.records("gh:none#1") == []


def test_round_transitions_compose_in_the_callers_transaction(env: Env) -> None:
    env.read(rc("gh:rc:a"), rc("gh:rc:b"))
    with env.lease.transaction() as conn:
        assert env.hs.deliver(env.ref, ["gh:rc:a", "gh:rc:b"], round_id="r-1", conn=conn) == [
            "gh:rc:a", "gh:rc:b",
        ]  # fmt: skip
        assert env.hs.release(env.ref, "r-1", conn=conn) == ["gh:rc:a", "gh:rc:b"]
        env.hs.deliver(env.ref, ["gh:rc:a", "gh:rc:b"], round_id="r-2", conn=conn)
        assert env.hs.mark_handled(env.ref, "r-2", conn=conn) == ["gh:rc:a", "gh:rc:b"]
        assert env.hs.redo(env.ref, "r-2", conn=conn) == ["gh:rc:a", "gh:rc:b"]
    assert {r.state for r in env.hs.records(env.ref)} == {"unhandled"}


def test_a_failing_caller_transaction_rolls_every_transition_back(env: Env) -> None:
    env.read(rc())
    with pytest.raises(RuntimeError), env.lease.transaction() as conn:
        env.hs.deliver(env.ref, [CID], round_id="r-1", conn=conn)
        env.hs.mark_handled(env.ref, "r-1", conn=conn)
        raise RuntimeError("the round row failed")
    assert env.get().state == "unhandled"


def test_the_same_comment_id_in_two_prs_is_updated_per_pr(
    env: Env, store: Store, lease: Lease, gateway: Gateway
) -> None:
    other = Env(store, lease, insert_pr(store, 8), gateway)
    env.read(rc(body="one"))
    other.read(rc(body="one"))
    for e in (env, other):
        assert e.hs.deliver(e.ref, [CID], round_id=f"r-{e.ref}") == [CID]
    assert env.hs.mark_handled(env.ref, f"r-{env.ref}") == [CID]
    assert other.get().state == "delivered"
    env.read(rc(body="two"))
    other.read(rc(body="two"))
    assert env.get().state == "unhandled"
    assert other.get().state == "delivered"
    assert other.hs.release(other.ref, f"r-{other.ref}") == [CID]
    assert other.get().state == "unhandled"


def _bodies(store: Store, ref: str) -> Path:
    return store.state_path / "files" / get_pr(store.conn, ref).digest  # type: ignore[union-attr]


def _backdate(directory: Path, seconds: float = 7200) -> None:
    old = time.time() - seconds
    for entry in directory.iterdir():
        os.utime(entry, (old, old))


def test_files_no_row_refers_to_are_removed_after_the_grace_period(env: Env, store: Store) -> None:
    for text in ("v1", "v2", "v3", "v4", "v5", "v6"):
        env.read(rc(body=text))
    directory = _bodies(store, env.ref)
    assert len(list(directory.iterdir())) == 6  # recent: all kept
    _backdate(directory)
    (directory / ".tmp-leftover").write_bytes(b"x")
    _backdate(directory)
    env.read(rc(body="v6"))
    assert [e.name for e in directory.iterdir()] == [env.get().body_file]


def test_a_recent_unreferenced_file_is_kept(env: Env, store: Store) -> None:
    env.read(rc(body="one"))
    env.read(rc(body="two"))
    directory = _bodies(store, env.ref)
    env.read(rc(body="two"))
    assert len(list(directory.iterdir())) == 2


def test_a_file_another_comment_still_uses_is_kept(env: Env, store: Store) -> None:
    env.read(rc("gh:rc:a", body="same"), rc("gh:rc:b", body="same"))
    directory = _bodies(store, env.ref)
    env.read(rc("gh:rc:a", body="same"), rc("gh:rc:b", body="other"))
    _backdate(directory)
    env.read(rc("gh:rc:a", body="same"), rc("gh:rc:b", body="other"))
    assert sorted(e.name for e in directory.iterdir()) == sorted(
        {env.get("gh:rc:a").body_file, env.get("gh:rc:b").body_file}  # type: ignore[arg-type]
    )
    assert env.hs.body(env.ref, "gh:rc:a").text == "same"


def test_a_changed_row_is_retried_and_ends_recorded_with_its_own_hash(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.read(rc(body="old"))
    real = HandledSet._put_files
    calls: list[int] = []

    def meddle(*args: Any) -> Any:
        result = real(*args)
        calls.append(1)
        if len(calls) == 1:  # another writer changes the row after the first look
            with env.lease.transaction() as conn:
                conn.execute("UPDATE comments SET body_sha256 = ?, rev = rev + 1", ("e" * 64,))
        return result

    monkeypatch.setattr(HandledSet, "_put_files", staticmethod(meddle))
    env.read(rc(body="old"))  # known equal: no file is put, so the changed row needs a retry
    assert len(calls) == 2
    assert env.get().body_sha256 == sha("old")
    assert env.hs.body(env.ref, CID).text == "old"


def test_a_row_that_changes_on_every_attempt_fails_after_three(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.read(rc(body="old"))
    real = HandledSet._put_files
    calls: list[int] = []

    def meddle(*args: Any) -> Any:
        real(*args)
        calls.append(1)
        with env.lease.transaction() as conn:
            conn.execute(
                "UPDATE comments SET body_sha256 = ?, rev = rev + 1", (str(len(calls)) * 64,)
            )
        return {}  # the file the rows need is never there when they are written

    monkeypatch.setattr(HandledSet, "_put_files", staticmethod(meddle))
    with pytest.raises(CliError) as caught:
        env.read(rc(body="old"))
    assert caught.value.code == "internal"
    assert len(calls) == 3
    assert env.get().body_sha256 == "3" * 64  # the reader wrote nothing


def test_deliver_skips_a_comment_edited_since_the_caller_saw_it(env: Env) -> None:
    env.read(rc("gh:rc:a", body="one"), rc("gh:rc:b", body="one"))
    env.read(rc("gh:rc:a", body="two"), rc("gh:rc:b", body="one"))
    got = env.hs.deliver(
        env.ref,
        ["gh:rc:a", "gh:rc:b"],
        round_id="r-1",
        expected={"gh:rc:a": sha("one"), "gh:rc:b": sha("one")},
    )
    assert got == ["gh:rc:b"]
    assert got.stale == ["gh:rc:a"]
    assert env.get("gh:rc:a").state == "unhandled"
    assert env.hs.deliver(
        env.ref, ["gh:rc:a"], round_id="r-1", expected={"gh:rc:a": sha("two")}
    ) == ["gh:rc:a"]


def test_deliver_without_expected_hashes_has_nothing_stale(env: Env) -> None:
    env.read(rc())
    assert env.hs.deliver(env.ref, [CID], round_id="r-1").stale == []


@pytest.mark.parametrize(
    "failure", [OSError("disk gone"), CliError("state_unsafe", "owner dir altered")]
)
def test_a_failing_cleanup_does_not_fail_the_recorded_read(
    env: Env, store: Store, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise failure

    monkeypatch.setattr(HandledSet, "_prune", boom)
    env.read(rc())
    assert len(table_rows(store)) == 1
