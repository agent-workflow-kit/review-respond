"""resolve_base and agent_busy (docs/interface.md: Fork lineage, Settle and round start): which
agent is a PR's base, what a label may and may not select, and what the due rule sees of it."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from review_respond import due, lineage, prstate, rounds
from review_respond.config import Config
from review_respond.gateway import AgentSpawn, Gateway, Wtctl
from review_respond.store import Lease, Store, open_state_dir, pr_digest, pr_lease_key
from tests.harness import builders
from tests.harness.adversarial import FORGED_AGENT_IDS
from tests.harness.agents import list_doc, original, show_doc, wt_record
from tests.harness.fakes import Fakes
from tests.harness.spawn_run import error_doc
from tests.test_lineage import FORK1, round_with_fork, switch
from tests.test_rounds import Env

C1, C2, C3 = "ag-cccccccc", "ag-dddddddd", "ag-eeeeeeee"
WT1, WT2, WT3 = "wt-aaaaaaaa", "wt-bbbbbbbb", "wt-cccccccc"
T_OLD, T_MID, T_NEW = "2026-01-01T10:00:00Z", "2026-01-01T11:00:00Z", "2026-01-01T12:00:00Z"
BRANCH = "feat/x"


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
    yield e
    assert e.lease is not None
    e.lease.release()


def cfg(**more: Any) -> Config:
    return Config.model_validate(more)


def fresh_cfg() -> Config:
    return cfg(repos={builders.REPO: {"respond_fresh": True}})


class Tools:
    def __init__(self, gateway: Gateway) -> None:
        self.spawn = AgentSpawn(gateway)
        self.wt = Wtctl(gateway)


@pytest.fixture
def tools(gateway: Gateway) -> Tools:
    return Tools(gateway)


def resolve(
    env: Env, tools: Tools, config: Config | None = None, *, pr: str | None = None,
    lease: Lease | None = None,
) -> lineage.Base:  # fmt: skip
    held = lease or env.lease
    assert held is not None
    return lineage.resolve_base(held, tools.spawn, tools.wt, config or cfg(), pr or env.pr)


def long_pr(env: Env) -> tuple[str, Lease]:
    """A stored PR whose reference cannot be a label value, and its lease."""
    ref = "git.example.test/org/" + "a" * 300 + "#7"
    with env.store.transaction() as conn:
        conn.execute(
            "INSERT INTO prs (pr, digest, host, repo, number, host_state, head_sha, head_ref,"
            " head_repo, is_fork, base_ref, created_at, updated_at) SELECT ?, ?, host, repo, 7,"
            " host_state, head_sha, head_ref, head_repo, is_fork, base_ref, created_at,"
            " updated_at FROM prs WHERE pr = ?",
            (ref, pr_digest(ref), env.pr),
        )
    lease = env.store.try_acquire(pr_lease_key(pr_digest(ref)), heartbeat=None)
    assert lease is not None
    return ref, lease


def stored(env: Env) -> tuple[str | None, int]:
    row = env.store.conn.execute(
        "SELECT base_agent, agent_busy FROM prs WHERE pr = ?", (env.pr,)
    ).fetchone()
    return row[0], row[1]


def script_list(fakes: Fakes, *agents: dict[str, Any]) -> None:
    fakes.script("agent-spawn", "list", json=list_doc(*agents), schema=False)


def script_wt(fakes: Fakes, record_id: str, branch: str | None = BRANCH, **kw: Any) -> None:
    fakes.script(
        "wtctl", "show", json=wt_record(record_id, branch, **kw),
        match={"positionals": {"selector": record_id}},
    )  # fmt: skip


def cand(agent_id: str, env: Env, wt: str = WT1, **over: Any) -> dict[str, Any]:
    over.setdefault("worktree", {"record_id": wt, "path": "/work/feat-x", "owned": True})
    return original(agent_id, env.pr, **over)


# ---- finding the original agent ----------------------------------------------------------------


def test_the_one_matching_agent_is_the_base_and_is_stored(
    env: Env, tools: Tools, fakes: Fakes
) -> None:
    script_list(fakes, cand(C1, env))
    script_wt(fakes, WT1)
    got = resolve(env, tools)
    assert (got.kind, got.agent_id, got.busy) == ("agent", C1, False)
    assert stored(env) == (C1, 0)
    (call,) = fakes.calls("agent-spawn", "list")
    assert call.argv == ["list", "--label", f"pr={env.pr}", "--json"]


def test_a_stored_base_is_used_without_a_search(env: Env, tools: Tools, fakes: Fakes) -> None:
    with env.store.transaction() as conn:
        conn.execute("UPDATE prs SET base_agent = ?", (C1,))
    fakes.script("agent-spawn", "show", json=show_doc(cand(C1, env)))
    got = resolve(env, tools)
    assert (got.kind, got.agent_id) == ("agent", C1)
    assert fakes.calls("agent-spawn", "list") == []


def test_two_equal_candidates_pick_the_most_recent(env: Env, tools: Tools, fakes: Fakes) -> None:
    script_list(
        fakes,
        cand(C1, env, WT1, created=T_OLD),
        cand(C2, env, WT2, created=T_NEW),
        cand(C3, env, WT3, created=T_MID),
    )
    script_wt(fakes, WT2)  # the newest is verified first and wins: the others are not asked
    assert resolve(env, tools).agent_id == C2


def test_equal_times_pick_a_stable_winner(env: Env, tools: Tools, fakes: Fakes) -> None:
    script_list(fakes, cand(C1, env, WT1), cand(C2, env, WT2))
    script_wt(fakes, WT2)
    assert resolve(env, tools).agent_id == C2


def test_the_preferred_owner_beats_a_newer_agent(env: Env, tools: Tools, fakes: Fakes) -> None:
    newer = cand(C2, env, WT2, created=T_NEW, labels=[f"pr={env.pr}", "owner=someone"])
    older = cand(C1, env, WT1, created=T_OLD)  # owner=ticket-work, the default prefer_owner
    script_list(fakes, newer, older)
    script_wt(fakes, WT1)
    assert resolve(env, tools).agent_id == C1


def test_prefer_owner_comes_from_the_config(env: Env, tools: Tools, fakes: Fakes) -> None:
    newer = cand(C2, env, WT2, created=T_NEW, labels=[f"pr={env.pr}", "owner=mine"])
    older = cand(C1, env, WT1, created=T_OLD)
    script_list(fakes, newer, older)
    script_wt(fakes, WT2)
    assert resolve(env, tools, cfg(prefer_owner="mine")).agent_id == C2


def test_without_the_preferred_owner_the_newest_wins(env: Env, tools: Tools, fakes: Fakes) -> None:
    a = cand(C1, env, WT1, created=T_OLD, labels=[f"pr={env.pr}", "owner=x"])
    b = cand(C2, env, WT2, created=T_NEW, labels=[f"pr={env.pr}"])
    script_list(fakes, a, b)
    script_wt(fakes, WT2)
    assert resolve(env, tools).agent_id == C2


def test_the_next_candidate_is_tried_when_the_best_is_on_another_branch(
    env: Env, tools: Tools, fakes: Fakes
) -> None:
    script_list(fakes, cand(C1, env, WT1, created=T_OLD), cand(C2, env, WT2, created=T_NEW))
    script_wt(fakes, WT2, "feat/other")
    script_wt(fakes, WT1)
    assert resolve(env, tools).agent_id == C1


# ---- forged labels and unrecorded ids ----------------------------------------------------------

# (name, override of the candidate, reaches the wtctl check)
REJECTED: list[tuple[str, dict[str, Any], bool]] = [
    ("another_repo", {"repo": "git.example.test/other/repo"}, False),
    ("no_repo", {"repo": None}, False),
    ("no_worktree", {"worktree": None}, False),
    ("third_party_kind", {"run_kind": "respond"}, False),
    ("tainted", {"tainted": True}, False),
    ("torn_down", {"status": "torn_down", "torn_down_at": T_NEW}, False),
    ("no_pr_label", {"labels": ["owner=ticket-work"]}, False),
    ("another_pr_label", {"labels": ["pr=git.example.test/org/repo#999"]}, False),
    ("our_owner_label", {"labels": ["pr=@PR@", "owner=review-respond"]}, False),
    ("forked_from_our_fork", {"forked_from": FORK1}, False),
    ("a_recorded_fork", {"agent_id": FORK1}, False),
]


@pytest.mark.parametrize(("name", "over", "reaches"), REJECTED, ids=[r[0] for r in REJECTED])
def test_a_forged_or_foreign_candidate_is_never_the_base(
    env: Env, tools: Tools, fakes: Fakes, name: str, over: dict[str, Any], reaches: bool
) -> None:
    round_with_fork(env, FORK1, "running")  # FORK1 is one of ours
    over = {
        k: ([v.replace("@PR@", env.pr) for v in val] if isinstance(val, list) else val)
        for k, val in over.items()
    }
    script_list(fakes, cand(over.pop("agent_id", C1), env, **over))
    got = resolve(env, tools)
    assert (got.kind, got.agent_id) == ("none", None)
    assert stored(env) == (None, 0)
    assert fakes.calls("wtctl") == []


def test_the_original_of_the_round_is_not_rejected_by_being_a_base_source(
    env: Env, tools: Tools, fakes: Fakes
) -> None:
    script_list(fakes, cand(C1, env))
    script_wt(fakes, WT1)
    assert resolve(env, tools).agent_id == C1


WT_REJECTED: list[tuple[str, dict[str, Any]]] = [
    ("another_branch", {"branch": "feat/other"}),
    ("detached", {"branch": None}),
    ("another_repo", {"repo": "git.example.test/other/repo"}),
    ("prefix_of_branch", {"branch": "feat"}),
    ("case_differs", {"branch": "Feat/X"}),
]


@pytest.mark.parametrize(("name", "over"), WT_REJECTED, ids=[r[0] for r in WT_REJECTED])
def test_a_label_alone_does_not_select_an_agent_on_another_branch(
    env: Env, tools: Tools, fakes: Fakes, name: str, over: dict[str, Any]
) -> None:
    script_list(fakes, cand(C1, env))
    script_wt(fakes, WT1, **{"branch": BRANCH, **over})
    got = resolve(env, tools)
    assert got.kind == "none" and stored(env) == (None, 0)


def test_a_worktree_record_that_wtctl_does_not_know_is_not_a_match(
    env: Env, tools: Tools, fakes: Fakes
) -> None:
    script_list(fakes, cand(C1, env))
    fakes.script("wtctl", "show", json=error_doc("not_found"), exit=10, schema=False)
    assert resolve(env, tools).kind == "none"


def test_a_failing_wtctl_is_not_a_match_and_does_not_hide_the_next_one(
    env: Env, tools: Tools, fakes: Fakes
) -> None:
    script_list(fakes, cand(C1, env, WT1, created=T_NEW), cand(C2, env, WT2, created=T_OLD))
    fakes.script(
        "wtctl", "show", json=error_doc("lower_failed"), exit=12, schema=False,
        match={"positionals": {"selector": WT1}},
    )  # fmt: skip
    script_wt(fakes, WT2)
    assert resolve(env, tools).agent_id == C2


def test_one_bad_candidate_does_not_hide_a_good_one(env: Env, tools: Tools, fakes: Fakes) -> None:
    script_list(fakes, cand(C2, env, WT2, created=T_NEW, run_kind="respond"), cand(C1, env))
    script_wt(fakes, WT1)
    assert resolve(env, tools).agent_id == C1


@pytest.mark.parametrize("name", sorted(set(FORGED_AGENT_IDS) - {"well_formed_but_foreign"}))
def test_a_candidate_with_a_forged_id_is_skipped(
    env: Env, tools: Tools, fakes: Fakes, name: str
) -> None:
    forged = cand(C1, env)
    forged["agent_id"] = FORGED_AGENT_IDS[name]
    script_list(fakes, forged)
    assert resolve(env, tools).kind == "none"
    assert fakes.calls("wtctl") == []


def test_an_unreadable_list_is_unknown_not_none(env: Env, tools: Tools, fakes: Fakes) -> None:
    fakes.script("agent-spawn", "list", stdout="not json", schema=False)
    got = resolve(env, tools)
    assert (got.kind, got.busy) == ("unknown", True)
    assert stored(env) == (None, 1)


def test_a_failing_list_is_unknown(env: Env, tools: Tools, fakes: Fakes) -> None:
    fakes.script("agent-spawn", "list", json=error_doc("lower_failed"), exit=12, schema=False)
    assert resolve(env, tools).kind == "unknown"


# ---- the label length limit and the fresh opt-in -----------------------------------------------


def test_a_pr_reference_too_long_for_a_label_has_no_lookup(
    env: Env, tools: Tools, fakes: Fakes
) -> None:
    ref, lease = long_pr(env)
    got = resolve(env, tools, pr=ref, lease=lease)
    assert got.kind == "none" and got.warnings == ("label_skipped",)
    assert fakes.calls("agent-spawn") == []


def test_no_agent_with_the_opt_in_is_a_fresh_session(env: Env, tools: Tools, fakes: Fakes) -> None:
    script_list(fakes)
    got = resolve(env, tools, fresh_cfg())
    assert (got.kind, got.agent_id, got.busy) == ("fresh", None, False)
    assert stored(env) == (None, 0)


def test_an_agent_found_beats_the_fresh_opt_in(env: Env, tools: Tools, fakes: Fakes) -> None:
    script_list(fakes, cand(C1, env))
    script_wt(fakes, WT1)
    assert resolve(env, tools, fresh_cfg()).kind == "agent"


def test_a_long_pr_with_the_opt_in_is_fresh(env: Env, tools: Tools, fakes: Fakes) -> None:
    ref, lease = long_pr(env)
    got = resolve(env, tools, fresh_cfg(), pr=ref, lease=lease)
    assert got.kind == "fresh" and got.warnings == ("label_skipped",)


# ---- the stored base ---------------------------------------------------------------------------


def set_stored(env: Env, agent: str) -> None:
    with env.store.transaction() as conn:
        conn.execute("UPDATE prs SET base_agent = ?", (agent,))


@pytest.mark.parametrize("how", ["exit_10", "torn_down"])
def test_a_lost_original_is_reset_and_the_search_continues(
    env: Env, tools: Tools, fakes: Fakes, how: str
) -> None:
    set_stored(env, C1)
    if how == "exit_10":
        fakes.script("agent-spawn", "show", json=error_doc("not_found"), exit=10, schema=False)
    else:
        gone = cand(C1, env, status="torn_down", torn_down_at=T_NEW)
        fakes.script("agent-spawn", "show", json=show_doc(gone))
    script_list(fakes, cand(C2, env, WT2))
    script_wt(fakes, WT2)
    got = resolve(env, tools)
    assert (got.agent_id, got.warnings) == (C2, ("base_lost",))
    assert stored(env)[0] == C2


def test_a_lost_base_with_nothing_else_is_none(env: Env, tools: Tools, fakes: Fakes) -> None:
    set_stored(env, C1)
    fakes.script("agent-spawn", "show", json=error_doc("not_found"), exit=10, schema=False)
    script_list(fakes)
    got = resolve(env, tools)
    assert (got.kind, got.warnings) == ("none", ("base_lost",))
    assert stored(env) == (None, 0)


def test_a_lost_base_fork_is_marked_torn_down_with_it(env: Env, tools: Tools, fakes: Fakes) -> None:
    set_stored(env, ORIG := "ag-oooooooo")
    switch(env, round_with_fork(env, FORK1, "pending"))
    fakes.script("agent-spawn", "show", json=error_doc("not_found"), exit=10, schema=False)
    script_list(fakes, cand(ORIG, env))  # the original is found again
    script_wt(fakes, WT1)
    got = resolve(env, tools)
    assert got.agent_id == ORIG
    row = lineage.get_fork(env.store.conn, FORK1)
    assert row is not None and row.state == "torn_down"


def test_a_failing_show_keeps_the_base_and_reports_busy(
    env: Env, tools: Tools, fakes: Fakes
) -> None:
    set_stored(env, C1)
    fakes.script("agent-spawn", "show", json=error_doc("lower_failed"), exit=12, schema=False)
    got = resolve(env, tools)
    assert (got.kind, got.agent_id, got.busy) == ("unknown", C1, True)
    assert stored(env) == (C1, 1)


def test_an_answer_about_another_agent_is_unknown(env: Env, tools: Tools, fakes: Fakes) -> None:
    set_stored(env, C1)
    fakes.script("agent-spawn", "show", json=show_doc(cand(C2, env)))
    got = resolve(env, tools)
    assert got.kind == "unknown" and stored(env)[0] == C1


def test_a_stored_id_that_is_not_an_agent_id_is_never_passed_to_a_sibling(
    env: Env, tools: Tools, fakes: Fakes
) -> None:
    set_stored(env, "--select")
    script_list(fakes)
    got = resolve(env, tools)
    assert got.kind == "none" and fakes.calls("agent-spawn", "show") == []
    assert stored(env)[0] is None


def test_the_base_of_a_confirmed_round_is_resolved_not_researched(
    env: Env, tools: Tools, fakes: Fakes
) -> None:
    set_stored(env, C1)
    switch(env, round_with_fork(env, FORK1, "pending"))
    fakes.script("agent-spawn", "show", json=show_doc(cand(FORK1, env, status="parked")))
    got = resolve(env, tools)
    assert (got.agent_id, got.busy) == (FORK1, False)
    assert fakes.calls("agent-spawn", "list") == []
    (call,) = fakes.calls("agent-spawn", "show")
    assert call.argv == ["show", FORK1, "--json"]


def test_a_second_round_forks_the_first_fork_with_no_extra_flags(
    env: Env, tools: Tools, fakes: Fakes, tmp_path: Path
) -> None:
    from tests.harness.spawn_run import started_doc

    set_stored(env, C1)
    switch(env, round_with_fork(env, FORK1, "pending"))
    fakes.script("agent-spawn", "show", json=show_doc(cand(FORK1, env, status="parked")))
    base = resolve(env, tools)
    fakes.script("agent-spawn", "resume", json=started_doc(first_working=False))
    assert base.agent_id is not None
    tools.spawn.resume_fork(
        base.agent_id, cwd=tmp_path, kind="respond", mode="headless", skill="respond-to-review",
        plugin_dir=tmp_path, prompt_file=tmp_path / "p", permission_profile=tmp_path / "pp",
        sandbox="required", setting_sources=(), env_allowlist=(),
        labels=(("round", "rd-aaaaaaaa"),), actor="auto:review-respond",
    )  # fmt: skip
    (call,) = fakes.calls("agent-spawn", "resume")
    assert call.argv[:3] == ["resume", FORK1, "--fork"]
    assert "--allow-tainted" not in call.argv and "--force" not in call.argv
    assert call.positionals == {"agent_id": FORK1}


# ---- agent_busy --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("over", "busy"),
    [
        ({"status": "parked"}, False),
        ({"status": "launching"}, True),
        ({"status": "live"}, True),
        ({"status": "tearing_down", "teardown_step": "started"}, True),
        ({"status": "parked", "promotion_pending": True}, True),
        ({"status": "live", "promotion_pending": True}, True),
    ],
)
def test_agent_busy_follows_the_agent_state(
    env: Env, tools: Tools, fakes: Fakes, over: dict[str, Any], busy: bool
) -> None:
    set_stored(env, C1)
    fakes.script("agent-spawn", "show", json=show_doc(cand(C1, env, **over)))
    got = resolve(env, tools)
    assert got.busy is busy and stored(env) == (C1, int(busy))


def test_a_found_agent_that_is_live_is_busy(env: Env, tools: Tools, fakes: Fakes) -> None:
    script_list(fakes, cand(C1, env, status="live"))
    script_wt(fakes, WT1)
    got = resolve(env, tools)
    assert got.busy and stored(env) == (C1, 1)


def test_busy_clears_when_the_agent_parks(env: Env, tools: Tools, fakes: Fakes) -> None:
    set_stored(env, C1)
    fakes.script("agent-spawn", "show", json=show_doc(cand(C1, env, status="live")))
    resolve(env, tools)
    fakes.script("agent-spawn", "show", json=show_doc(cand(C1, env, status="parked")))
    resolve(env, tools)
    assert stored(env) == (C1, 0)


# ---- what the due rule and the PR state see ----------------------------------------------------


def facts(env: Env) -> due.RoundFacts:
    return rounds.round_facts(env.store.conn, env.pr)


def test_the_round_facts_carry_the_stored_busy_flag(env: Env) -> None:
    assert facts(env).agent_busy is False
    with env.store.transaction() as conn:
        conn.execute("UPDATE prs SET agent_busy = 1")
    assert facts(env).agent_busy is True


def test_a_busy_agent_makes_the_pr_wait_agent_live(env: Env) -> None:
    set_stored(env, C1)
    with env.store.transaction() as conn:
        conn.execute("UPDATE prs SET agent_busy = 1")
    derived = prstate.derive(env.store.conn, cfg(), env.pr, 1e12)
    assert (derived.state, derived.waiting_reason) == ("waiting", "agent_live")
    decision = due.decide(derived.inputs)
    assert decision.due is False and decision.why == "agent_busy"


def test_an_idle_agent_lets_the_pr_be_due(env: Env) -> None:
    set_stored(env, C1)
    derived = prstate.derive(env.store.conn, cfg(), env.pr, 1e12)
    assert due.decide(derived.inputs).due is True
