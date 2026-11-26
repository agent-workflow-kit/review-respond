"""Fork lineage (docs/interface.md: Fork lineage): the forks table, recording a round's fork, the
base switch (only for a round whose record landed), the teardown pass and the labels."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from review_respond import labels, lineage
from review_respond.errors import CliError
from review_respond.store import Store, open_state_dir, pass_lease_key
from tests.harness import builders
from tests.harness.adversarial import FORGED_AGENT_IDS
from tests.test_rounds import AGENT, BASE, Env

ORIGINAL = "ag-oooooooo"
FORK1, FORK2, FORK3 = "ag-fffffff2", "ag-fffffff3", "ag-fffffff4"
NOW = 1000.0


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    opened = Store.open(open_state_dir(tmp_path / "state" / "rr"), write=True, close_state=True)
    with opened.transaction() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('installed_at', ?)", ("2026-01-01T12:00:00Z",)
        )
    yield opened
    opened.close()


@pytest.fixture
def env(store: Store) -> Iterator[Env]:
    e = Env(store)
    set_base(e, ORIGINAL)
    yield e
    assert e.lease is not None
    e.lease.release()


def set_base(env: Env, agent: str | None) -> None:
    with env.store.transaction() as conn:
        conn.execute("UPDATE prs SET base_agent = ? WHERE pr = ?", (agent, env.pr))


def base_of(env: Env) -> str | None:
    return env.store.conn.execute(
        "SELECT base_agent FROM prs WHERE pr = ?", (env.pr,)
    ).fetchone()[0]


def states(env: Env) -> dict[str, str]:
    return {
        r[0]: r[1] for r in env.store.conn.execute("SELECT agent_id, state FROM forks")
    }


def round_with_fork(env: Env, agent: str, end: str = "pending") -> str:
    """A round of the PR whose fork is ``agent`` (forked from the PR's current base), ended in
    ``end`` by the documented path."""
    with env.store.transaction() as conn:  # a fresh round may deliver the comments again
        conn.execute(
            "UPDATE comments SET state = 'unhandled', round = NULL, delivered_sha256 = NULL,"
            " delivered_from = NULL, handled_sha256 = NULL"
        )
    rid = env.rounds.create(env.pr, base_agent_id=base_of(env)).id
    assert env.rounds.start(rid, (builders_c1(),), head_sha="e" * 40).row.state == "running"
    assert env.rounds.record_agent(rid, agent).changed
    if end == "running":
        return rid
    if end == "failed":
        env.rounds.to_failed(rid, "agent_failed", counted=True)
    elif end == "escalated":
        env.rounds.to_escalated(rid, reason="r", summary="s", counted=True)
    elif end == "canceled":
        env.rounds.request_cancel(rid, "stopped")
        env.rounds.cancel_flagged(rid)
    else:
        env.rounds.to_pending(rid)
        if end != "pending":
            with env.store.transaction() as conn:
                assert env.rounds.settle_pending(conn, rid, end)
    return rid


def builders_c1() -> str:
    from tests.test_rounds import C1

    return C1


def switch(env: Env, rid: str) -> lineage.Switch:
    assert env.lease is not None
    with env.lease.transaction() as conn:
        return lineage.switch_base(conn, rid, now=NOW)


# ---- the table ---------------------------------------------------------------------------------


def test_the_forks_table_and_the_busy_column_exist(store: Store) -> None:
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(forks)")}
    assert {"agent_id", "pr", "round", "source", "state", "failed_passes", "refused_at"} <= cols
    assert "agent_busy" in {r[1] for r in store.conn.execute("PRAGMA table_info(prs)")}


def test_a_recorded_fork_row_is_in_state_round_with_its_source(env: Env) -> None:
    rid = round_with_fork(env, FORK1, "running")
    fork = lineage.get_fork(env.store.conn, FORK1)
    assert fork is not None
    assert (fork.pr, fork.round, fork.source, fork.state) == (env.pr, rid, ORIGINAL, "round")
    assert (fork.failed_passes, fork.refused_at) == (0, None)


def test_a_fresh_session_has_no_source(env: Env) -> None:
    set_base(env, None)
    round_with_fork(env, FORK1, "running")
    fork = lineage.get_fork(env.store.conn, FORK1)
    assert fork is not None and fork.source is None


def test_recording_the_same_agent_again_keeps_one_row(env: Env) -> None:
    rid = round_with_fork(env, FORK1, "running")
    assert env.rounds.record_agent(rid, FORK1).changed is False
    assert states(env) == {FORK1: "round"}


@pytest.mark.parametrize("name", sorted(FORGED_AGENT_IDS))
def test_a_forged_agent_id_is_never_recorded(env: Env, name: str) -> None:
    rid = round_with_fork(env, FORK1, "running")
    with pytest.raises((ValueError, CliError)):  # a well-formed one: the round has its agent
        env.rounds.record_agent(rid, FORGED_AGENT_IDS[name])
    assert states(env) == {FORK1: "round"}


def test_the_original_agent_can_never_be_recorded_as_a_fork(env: Env) -> None:
    rid = env.rounds.create(env.pr, base_agent_id=ORIGINAL).id
    env.rounds.start(rid, (builders_c1(),), head_sha="e" * 40)
    with pytest.raises(CliError) as err:
        env.rounds.record_agent(rid, ORIGINAL)  # teardown would then act on the original
    assert err.value.code == "internal"
    assert states(env) == {}
    assert env.row(rid).agent_id is None  # the round row rolled back with it


def test_a_fork_is_never_its_own_source(env: Env) -> None:
    set_base(env, None)
    rid = env.rounds.create(env.pr, base_agent_id=FORK1).id
    env.rounds.start(rid, (builders_c1(),), head_sha="e" * 40)
    with pytest.raises(CliError):
        env.rounds.record_agent(rid, FORK1)
    assert states(env) == {}


def test_an_agent_id_recorded_for_another_round_is_refused(env: Env) -> None:
    round_with_fork(env, FORK1, "failed")
    rid = env.rounds.create(env.pr, base_agent_id=ORIGINAL).id
    env.rounds.start(rid, (builders_c1(),), head_sha="e" * 40)
    with pytest.raises(CliError) as err:
        env.rounds.record_agent(rid, FORK1)
    assert err.value.code == "internal"


@pytest.mark.parametrize(
    ("old", "new", "allowed"),
    [
        ("round", "base", True),
        ("round", "teardown_pending", True),
        ("round", "torn_down", True),
        ("base", "teardown_pending", True),
        ("base", "torn_down", True),
        ("teardown_pending", "torn_down", True),
        ("base", "round", False),
        ("teardown_pending", "base", False),
        ("teardown_pending", "round", False),
        ("torn_down", "base", False),
        ("torn_down", "round", False),
        ("torn_down", "teardown_pending", False),
    ],
)
def test_fork_states_only_move_forward(env: Env, old: str, new: str, allowed: bool) -> None:
    round_with_fork(env, FORK1, "running")
    with env.store.transaction() as conn:
        conn.execute("UPDATE forks SET state = ?", (old,))
    try:
        with env.store.transaction() as conn:
            conn.execute("UPDATE forks SET state = ?", (new,))
    except sqlite3.DatabaseError:
        assert not allowed
    else:
        assert allowed


def test_a_pr_has_at_most_one_base_fork(env: Env) -> None:
    round_with_fork(env, FORK1, "failed")
    round_with_fork(env, FORK2, "failed")
    with env.store.transaction() as conn:
        conn.execute("UPDATE forks SET state = 'base' WHERE agent_id = ?", (FORK1,))
    with pytest.raises(sqlite3.IntegrityError), env.store.transaction() as conn:
        conn.execute("UPDATE forks SET state = 'base' WHERE agent_id = ?", (FORK2,))


# ---- the base switch ---------------------------------------------------------------------------


def test_a_confirmed_round_makes_its_fork_the_base_and_leaves_the_original_alone(env: Env) -> None:
    rid = round_with_fork(env, FORK1, "pending")
    got = switch(env, rid)
    assert (got.changed, got.base, got.retired) == (True, FORK1, None)
    assert base_of(env) == FORK1
    assert states(env) == {FORK1: "base"}  # the original has no row: it can never be torn down


def test_the_next_confirm_retires_the_previous_base_fork(env: Env) -> None:
    switch(env, round_with_fork(env, FORK1, "pending"))
    rid2 = round_with_fork(env, FORK2, "confirmed")
    got = switch(env, rid2)
    assert (got.base, got.retired) == (FORK2, FORK1)
    assert base_of(env) == FORK2
    assert states(env) == {FORK1: "teardown_pending", FORK2: "base"}


def test_a_chain_of_forks_of_forks_follows_the_confirmed_rounds(env: Env) -> None:
    sources = []
    for fork in (FORK1, FORK2, FORK3):
        rid = round_with_fork(env, fork, "pending")
        sources.append(lineage.get_fork(env.store.conn, fork).source)  # type: ignore[union-attr]
        switch(env, rid)
    assert sources == [ORIGINAL, FORK1, FORK2]  # each round forks the last confirmed fork
    assert base_of(env) == FORK3
    assert states(env) == {FORK1: "teardown_pending", FORK2: "teardown_pending", FORK3: "base"}


def test_switching_twice_changes_nothing_the_second_time(env: Env) -> None:
    rid = round_with_fork(env, FORK1, "pending")
    switch(env, rid)
    again = switch(env, rid)
    assert again.changed is False and again.base == FORK1
    assert states(env) == {FORK1: "base"}


@pytest.mark.parametrize(
    "end", ["running", "rejected", "expired", "closed", "failed", "escalated", "canceled"]
)
def test_only_a_round_whose_record_landed_can_switch_the_base(env: Env, end: str) -> None:
    rid = round_with_fork(env, FORK1, end)
    with pytest.raises(ValueError):
        switch(env, rid)
    assert base_of(env) == ORIGINAL
    assert states(env) == {FORK1: "round"}


def test_a_queued_round_cannot_switch_the_base(env: Env) -> None:
    rid = env.queued()
    with pytest.raises(ValueError):
        switch(env, rid)
    assert base_of(env) == ORIGINAL


def test_a_failed_and_a_rejected_fork_is_never_a_base_later(env: Env) -> None:
    switch(env, round_with_fork(env, FORK1, "pending"))
    for fork, end in ((FORK2, "failed"), (FORK3, "rejected")):
        rid = round_with_fork(env, fork, end)
        with pytest.raises(ValueError):
            switch(env, rid)
    assert base_of(env) == FORK1
    assert states(env)[FORK1] == "base"


def test_a_switch_that_aborts_leaves_the_lineage_unchanged(env: Env) -> None:
    switch(env, round_with_fork(env, FORK1, "pending"))
    rid2 = round_with_fork(env, FORK2, "pending")
    assert env.lease is not None

    class Boom(Exception):
        pass

    with pytest.raises(Boom), env.lease.transaction() as conn:
        lineage.switch_base(conn, rid2, now=NOW)
        raise Boom
    assert base_of(env) == FORK1
    assert states(env) == {FORK1: "base", FORK2: "round"}


def test_a_round_without_an_agent_cannot_switch(env: Env) -> None:
    rid = env.running()
    env.rounds.to_pending(rid)
    with pytest.raises(ValueError):
        switch(env, rid)


def test_a_fork_row_of_another_pr_or_round_is_refused(env: Env) -> None:
    rid = round_with_fork(env, FORK1, "pending")
    with env.store.transaction() as conn:
        conn.execute("UPDATE rounds SET agent_id = ? WHERE id = ?", (FORK2, rid))
    with pytest.raises(CliError) as err:  # the round names an agent this tool never recorded
        switch(env, rid)
    assert err.value.code == "internal"
    assert base_of(env) == ORIGINAL


def test_a_fork_marked_for_teardown_never_becomes_the_base(env: Env) -> None:
    rid = round_with_fork(env, FORK1, "pending")
    with env.store.transaction() as conn:
        conn.execute("UPDATE forks SET state = 'teardown_pending'")
    with pytest.raises(ValueError):
        switch(env, rid)


def test_marking_for_teardown_refuses_the_base_and_the_unrecorded(env: Env) -> None:
    switch(env, round_with_fork(env, FORK1, "pending"))
    round_with_fork(env, FORK2, "failed")
    assert env.lease is not None
    with env.lease.transaction() as conn:
        assert lineage.mark_teardown_pending(conn, FORK1, now=NOW) is False  # the base
        assert lineage.mark_teardown_pending(conn, ORIGINAL, now=NOW) is False  # not ours
        assert lineage.mark_teardown_pending(conn, "ag-zzzzzzzz", now=NOW) is False
        assert lineage.mark_teardown_pending(conn, FORK2, now=NOW) is True
        assert lineage.mark_teardown_pending(conn, FORK2, now=NOW) is False  # already
    assert states(env) == {FORK1: "base", FORK2: "teardown_pending"}


# ---- the teardown pass -------------------------------------------------------------------------


def pending_teardown(env: Env, *agents: str) -> None:
    for a in agents:
        round_with_fork(env, a, "failed")
    assert env.lease is not None
    with env.lease.transaction() as conn:
        for a in agents:
            assert lineage.mark_teardown_pending(conn, a, now=NOW)


def pass_lease(env: Env) -> Any:
    held = getattr(env, "_pass", None)
    if held is None:
        held = env.store.try_acquire(pass_lease_key(), heartbeat=None)
        assert held is not None
        env._pass = held  # type: ignore[attr-defined]
    return held


def spawn(gateway: Any) -> Any:
    from review_respond.gateway import AgentSpawn

    return AgentSpawn(gateway)


def script_teardown(fakes: Any, agent: str, exit: int = 0, **kw: Any) -> None:
    from tests.harness.agents import original
    from tests.harness.spawn_run import error_doc

    match = {"positionals": {"agent_id": agent}}
    if exit == 0:
        fakes.script(
            "agent-spawn", "teardown", json={"agent": original(agent, "x"), "worktrees": [],
            "warnings": []}, schema=False, match=match, **kw,
        )  # fmt: skip
    else:
        fakes.script(
            "agent-spawn", "teardown", json=error_doc("conflict"), exit=exit, schema=False,
            match=match, **kw,
        )  # fmt: skip


def test_teardown_acts_only_on_recorded_forks_marked_for_it(env: Env, gateway: Any, fakes: Any) -> None:
    pending_teardown(env, FORK1)
    round_with_fork(env, FORK2, "pending")  # not marked
    switch(env, round_with_fork(env, FORK3, "pending"))  # the base
    script_teardown(fakes, FORK1)
    assert env.lease is not None
    done = lineage.teardown_pass(pass_lease(env), spawn(gateway))
    assert [(d.agent_id, d.outcome) for d in done] == [(FORK1, "torn_down")]
    assert [c.argv for c in fakes.calls("agent-spawn")] == [
        ["teardown", FORK1, "--actor", "auto:review-respond", "--json"]
    ]
    assert states(env) == {FORK1: "torn_down", FORK2: "round", FORK3: "base"}


@pytest.mark.parametrize("code", [0, 10])
def test_teardown_exit_0_or_10_means_torn_down(
    env: Env, gateway: Any, fakes: Any, code: int
) -> None:
    pending_teardown(env, FORK1)
    script_teardown(fakes, FORK1, code)
    assert env.lease is not None
    lineage.teardown_pass(pass_lease(env), spawn(gateway))
    assert states(env) == {FORK1: "torn_down"}


def test_a_live_fork_is_stopped_first_and_retried_next_pass(
    env: Env, gateway: Any, fakes: Any
) -> None:
    from tests.harness.agents import original

    pending_teardown(env, FORK1)
    script_teardown(fakes, FORK1, 20)
    fakes.script(
        "agent-spawn", "stop",
        json={"agent": original(FORK1, "x"), "action": "no_op", "removed": False,
              "warnings": []},
        schema=False,
    )  # fmt: skip
    assert env.lease is not None
    done = lineage.teardown_pass(pass_lease(env), spawn(gateway))
    assert [d.outcome for d in done] == ["retry"]
    assert [c.command for c in fakes.calls("agent-spawn")] == ["teardown", "stop"]
    fork = lineage.get_fork(env.store.conn, FORK1)
    assert fork is not None and fork.state == "teardown_pending" and fork.failed_passes == 1
    script_teardown(fakes, FORK1, 0)
    lineage.teardown_pass(pass_lease(env), spawn(gateway))
    assert states(env) == {FORK1: "torn_down"}


def test_three_failed_passes_report_teardown_refused_once(
    env: Env, gateway: Any, fakes: Any
) -> None:
    pending_teardown(env, FORK1)
    assert env.lease is not None
    refused: list[bool] = []
    for _ in range(5):
        script_teardown(fakes, FORK1, 12)
        done = lineage.teardown_pass(pass_lease(env), spawn(gateway))
        refused.append(done[0].refused)
    assert refused == [False, False, True, False, False]  # once, then it keeps retrying
    fork = lineage.get_fork(env.store.conn, FORK1)
    assert fork is not None and fork.state == "teardown_pending" and fork.failed_passes == 5
    assert fork.refused_at is not None


def test_exit_0_is_the_answer_whatever_the_body_says(
    env: Env, gateway: Any, fakes: Any
) -> None:
    pending_teardown(env, FORK1)
    fakes.script("agent-spawn", "teardown", stdout="not json", exit=0, schema=False)
    assert env.lease is not None
    lineage.teardown_pass(pass_lease(env), spawn(gateway))
    assert states(env) == {FORK1: "torn_down"}  # exit 0 is the answer; the body is not trusted


def test_the_pass_never_tears_down_the_pr_base_even_if_marked(
    env: Env, gateway: Any, fakes: Any
) -> None:
    switch(env, round_with_fork(env, FORK1, "pending"))
    with env.store.transaction() as conn:  # a corrupt row: base and teardown_pending at once
        conn.execute("DROP TRIGGER IF EXISTS forks_forward_only")
        conn.execute("UPDATE forks SET state = 'teardown_pending'")
    assert env.lease is not None
    assert lineage.teardown_pass(pass_lease(env), spawn(gateway)) == []
    assert fakes.calls("agent-spawn") == []


# ---- ids and labels ----------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["ag-abcdefgh", "ag-22222222"])
def test_agent_ids_that_match_are_accepted(text: str) -> None:
    assert lineage.is_agent_id(text)


@pytest.mark.parametrize("name", sorted(set(FORGED_AGENT_IDS) - {"well_formed_but_foreign"}))
def test_forged_agent_ids_are_not_agent_ids(name: str) -> None:
    assert not lineage.is_agent_id(FORGED_AGENT_IDS[name])


def test_only_recorded_ids_are_ours(env: Env) -> None:
    round_with_fork(env, FORK1, "running")
    conn = env.store.conn
    assert lineage.is_recorded(conn, FORK1)
    assert not lineage.is_recorded(conn, ORIGINAL)
    for forged in FORGED_AGENT_IDS.values():
        assert not lineage.is_recorded(conn, forged)


PRX = "git.example.test/org/repo#7"


def test_fork_labels_carry_the_four_documented_labels() -> None:
    got = labels.fork_labels(pr=PRX, repo_id="git.example.test/org/repo", round_id="rd-aaaaaaaa")
    assert got.labels == (
        ("pr", PRX), ("owner", "review-respond"), ("repo", "git.example.test/org/repo"),
        ("round", "rd-aaaaaaaa"),
    )  # fmt: skip
    assert got.warnings == ()


def test_a_label_value_over_256_characters_is_omitted_with_a_warning() -> None:
    long_repo = "h.example.test/" + "a" * 300
    got = labels.fork_labels(pr=long_repo + "#7", repo_id=long_repo, round_id="rd-aaaaaaaa")
    assert [k for k, _ in got.labels] == ["owner", "round"]
    assert got.warnings == ("label_skipped",)


def test_a_value_of_exactly_256_characters_is_kept() -> None:
    value = "x" * 256
    assert labels.value_ok(value) and not labels.value_ok(value + "x")


@pytest.mark.parametrize("value", ["a b", "a\nb", "", "é", "a;b", "$(x)"])
def test_a_value_outside_the_label_alphabet_is_not_a_label(value: str) -> None:
    assert not labels.value_ok(value) or value == ""


def test_throwaway_and_job_labels() -> None:
    assert labels.throwaway_labels(role="round", round_id="rd-aaaaaaaa").labels == (
        ("app", "review-respond"), ("role", "round"), ("round", "rd-aaaaaaaa"),
    )  # fmt: skip
    assert labels.throwaway_labels(
        role="clean", round_id="rd-aaaaaaaa", pending_id="rp-aaaaaaaa"
    ).labels[-1] == ("pending", "rp-aaaaaaaa")
    assert labels.job_labels(pr=PRX, round_id="rd-aaaaaaaa").labels == (
        ("pr", PRX), ("round", "rd-aaaaaaaa"), ("owner", "review-respond"),
    )  # fmt: skip
    with pytest.raises(ValueError):
        labels.throwaway_labels(role="other", round_id="rd-aaaaaaaa")
