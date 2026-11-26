"""`check` and the base agent: resolve_base before the due rule, agent_busy, and the teardown of
forks marked for it (docs/interface.md: Fork lineage, Settle and round start)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from review_respond.cli import main
from tests.harness.agents import list_doc, original, show_doc, wt_record
from tests.harness.fakes import Fakes
from tests.harness.plugin import World
from tests.harness.spawn_run import error_doc
from tests.test_check import (
    PR7,
    log_events,
    one_comment,
    pr_item,
    pr_of,
    run,
    script_comments,
    script_prs,
    sql,
    write_config,
)


@pytest.fixture
def config(world: World) -> Path:
    return write_config(world)


AG = "ag-cccccccc"
WT = "wt-aaaaaaaa"


def agent(**over: Any) -> dict[str, Any]:
    return original(AG, PR7, **over)


def pass_with(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> tuple[int, dict[str, Any]]:
    script_prs(world.fakes, [pr_item(7)])
    script_comments(world.fakes, PR7, one_comment())
    code, doc, _ = run(capsys, "check", config=config)
    return code, doc


def test_an_original_agent_is_found_stored_and_the_pr_is_due(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    world.fakes.script("agent-spawn", "list", json=list_doc(agent()), schema=False)
    world.fakes.script("wtctl", "show", json=wt_record(WT, "feat/x"))
    code, doc = pass_with(world, capsys, config)
    assert code == 0
    assert pr_of(doc, PR7) == {
        "pr": PR7, "state": "waiting", "started_round": None, "lease_held": False, "errors": [],
    }  # fmt: skip
    assert sql(world, "SELECT base_agent, agent_busy, block FROM prs") == [(AG, 0, None)]


def test_a_pr_without_an_agent_gets_the_no_agent_block_and_loses_it_when_one_appears(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    world.fakes.script("agent-spawn", "list", json=list_doc(), schema=False)
    pass_with(world, capsys, config)
    assert sql(world, "SELECT json_extract(block, '$.reason') FROM prs") == [("no_agent",)]
    world.fakes.script("agent-spawn", "list", json=list_doc(agent()), schema=False)
    world.fakes.script("wtctl", "show", json=wt_record(WT, "feat/x"))
    pass_with(world, capsys, config)
    assert sql(world, "SELECT base_agent, block FROM prs") == [(AG, None)]


def test_a_live_base_agent_keeps_the_pr_waiting_agent_live(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    world.fakes.script("agent-spawn", "list", json=list_doc(agent(status="live")), schema=False)
    world.fakes.script("wtctl", "show", json=wt_record(WT, "feat/x"))
    pass_with(world, capsys, config)
    assert sql(world, "SELECT base_agent, agent_busy FROM prs") == [(AG, 1)]
    code, shown, _ = run(capsys, "show", PR7, config=config)
    assert code == 0
    assert shown["pr"]["waiting_reason"] == "agent_live"
    # the agent parks: the next pass sees it
    world.fakes.script("agent-spawn", "show", json=show_doc(agent(status="parked")))
    pass_with(world, capsys, config)
    assert sql(world, "SELECT agent_busy FROM prs") == [(0,)]
    _, shown, _ = run(capsys, "show", PR7, config=config)
    assert shown["pr"]["waiting_reason"] == "capacity"


def test_an_unreadable_agent_spawn_is_lower_failed_and_sets_no_block(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    world.fakes.script("agent-spawn", "list", json=error_doc("lower_failed"), exit=12, schema=False)
    code, doc = pass_with(world, capsys, config)
    assert code == 0
    assert pr_of(doc, PR7)["errors"] == ["lower_failed"]
    assert sql(world, "SELECT block, base_agent, agent_busy FROM prs") == [(None, None, 1)]


def test_a_pr_with_nothing_eligible_never_asks_agent_spawn(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    script_prs(world.fakes, [pr_item(7)])
    script_comments(world.fakes, PR7, [])
    run(capsys, "check", config=config)
    assert world.fakes.calls("agent-spawn") == []


def insert_fork(world: World, agent_id: str, state: str, pr: str = PR7) -> None:
    sql(
        world,
        "INSERT INTO rounds (id, pr, number, state, trigger_kind, created_at, ended_at,"
        " cancel_reason) VALUES (?, ?, 1, 'canceled', 'check', 1, 1, 'stopped')",
        "rd-aaaaaaaa", pr,
    )  # fmt: skip
    sql(
        world,
        "INSERT INTO forks (agent_id, pr, round, state, created_at, updated_at)"
        " VALUES (?, ?, 'rd-aaaaaaaa', ?, 1, 1)",
        agent_id, pr, state,
    )  # fmt: skip


def test_a_pass_tears_down_forks_marked_for_it_and_nothing_else(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    script_prs(world.fakes, [pr_item(7)])
    script_comments(world.fakes, PR7, [])
    run(capsys, "check", config=config)  # stores the PR
    insert_fork(world, "ag-fffffff2", "teardown_pending")
    gone = original("ag-fffffff2", PR7, status="torn_down", torn_down_at="2026-01-01T12:00:00Z")
    world.fakes.script(
        "agent-spawn", "teardown",
        json={"agent": gone, "worktrees": [], "warnings": []}, schema=False,
    )  # fmt: skip
    script_prs(world.fakes, [pr_item(7)])
    script_comments(world.fakes, PR7, [])
    run(capsys, "check", config=config)
    (call,) = world.fakes.calls("agent-spawn", "teardown")
    assert call.argv == ["teardown", "ag-fffffff2", "--actor", "auto:review-respond", "--json"]
    assert sql(world, "SELECT state FROM forks") == [("torn_down",)]


def test_a_refused_teardown_is_logged_once_after_three_passes(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    script_prs(world.fakes, [pr_item(7)])
    script_comments(world.fakes, PR7, [])
    run(capsys, "check", config=config)
    insert_fork(world, "ag-fffffff2", "teardown_pending")
    for _ in range(4):
        world.fakes.script(
            "agent-spawn", "teardown", json=error_doc("lower_failed"), exit=12, schema=False
        )
        script_prs(world.fakes, [pr_item(7)])
        script_comments(world.fakes, PR7, [])
        run(capsys, "check", config=config)
    refused = [e for e in log_events(world) if e.get("code") == "teardown_refused"]
    assert len(refused) == 1
    assert sql(world, "SELECT state, failed_passes FROM forks") == [("teardown_pending", 4)]


def test_dry_run_calls_no_agent_spawn(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    pass_with_none = main(["check", "--dry-run", "--config", str(config), "--json"])
    capsys.readouterr()
    assert pass_with_none == 0
    assert world.fakes.calls("agent-spawn") == [] and world.fakes.calls("wtctl") == []


_ = Fakes
