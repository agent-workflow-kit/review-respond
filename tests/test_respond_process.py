"""`respond` with real processes: races for the one round of a PR (docs/interface.md: Concurrency).

Every process is started with ``start_new_session`` and recorded by exact pid in the world's
registry; the outcome is read from each process's exit code and answer and from the database, never
from the order of any log. The job is placed with the scripted jobsctl fake (``add`` only records
the call), so no ``_job`` runs. Each race runs several rounds of its own.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.harness import builders
from tests.harness.agents import original, show_doc
from tests.harness.builders import pr_item
from tests.harness.jobsctl import add_doc
from tests.harness.plugin import World
from tests.test_check import (
    AGENT,
    PR7,
    one_comment,
    run,
    script_prs,
    set_agent,
    sql,
    write_config,
)

REPEAT = range(5)


def started(world: World, capsys: pytest.CaptureFixture[str]) -> Path:
    """The PR is stored with its comments and an agent; every sibling answer the commands below
    need is scripted for any number of calls."""
    config = write_config(world)
    world.fakes.script("agent-spawn", "list", json={"agents": []}, schema=False, times=None)
    script_prs(world.fakes, [pr_item(7)], times=None)
    comments = builders.forgectl_comments(one_comment(), pr=PR7)
    world.fakes.script(
        "forgectl", "comments", json=comments, match={"positionals": {"pr": PR7}}, times=None
    )
    assert run(capsys, "check", config=config)[0] == 0
    set_agent(world)
    sql(world, "UPDATE prs SET block = NULL")
    world.fakes.script(
        "agent-spawn", "show", json=show_doc(original(AGENT, PR7, status="parked")), times=None
    )
    world.fakes.script(
        "forgectl", "show", json=builders.forgectl_show(pr=PR7),
        match={"positionals": {"pr": PR7}}, times=None,
    )  # fmt: skip
    world.fakes.script("jobsctl", "add", json=add_doc(), times=None)
    return config


def spawn(world: World, config: Path, *argv: str) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [sys.executable, "-m", "review_respond", *argv, "--json", "--config", str(config)],
        env=world.env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    world.procs.record(proc.pid)
    return proc


def finish(proc: subprocess.Popen[bytes]) -> tuple[int, dict[str, Any]]:
    out, err = proc.communicate(timeout=120)
    assert out.strip(), err.decode()
    return proc.returncode, json.loads(out)


def rounds(world: World) -> list[tuple[Any, ...]]:
    return sql(world, "SELECT id, state, trigger_kind FROM rounds ORDER BY number")


@pytest.mark.parametrize("attempt", REPEAT)
def test_two_responds_make_exactly_one_round(
    attempt: int, world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    config = started(world, capsys)
    procs = [spawn(world, config, "respond", PR7, "--actor", "human:tester") for _ in range(2)]
    results = [finish(p) for p in procs]
    codes = sorted(code for code, _ in results)
    assert codes == [0, 20], results
    loser = next(doc for code, doc in results if code == 20)
    assert loser["error"]["reason"] == "round_active"
    winner = next(doc for code, doc in results if code == 0)
    ((rid, state, trigger),) = rounds(world)
    assert (state, trigger, winner["round"]["id"]) == ("queued", "respond", rid)
    assert loser["error"]["details"] == {"round": rid}
    assert len(world.fakes.calls("jobsctl", "add")) == 1


@pytest.mark.parametrize("attempt", REPEAT)
def test_a_respond_and_a_check_make_exactly_one_round(
    attempt: int, world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    config = started(world, capsys)
    responder = spawn(world, config, "respond", PR7, "--actor", "human:tester")
    checker = spawn(world, config, "check")
    r_code, r_doc = finish(responder)
    c_code, c_doc = finish(checker)
    assert c_code == 0, c_doc  # check never exits 20
    ((rid, state, trigger),) = rounds(world)
    assert state == "queued"
    check_started = [p["started_round"] for p in c_doc["prs"] if p["started_round"]]
    if trigger == "respond":
        assert (r_code, r_doc["round"]["id"]) == (0, rid)
        assert check_started == []
    else:  # check won: respond is refused, never a second round
        assert (r_code, r_doc["error"]["reason"]) == (20, "round_active")
        assert check_started == [rid]
    assert len(world.fakes.calls("jobsctl", "add")) == 1


@pytest.mark.parametrize("attempt", REPEAT)
def test_a_respond_that_loses_to_a_held_lease_waits_then_proceeds(
    attempt: int, world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """``respond`` waits for the PR's lease (up to 30 s): a ``check`` that holds it finishes first
    and the round follows, from the same stored state."""
    config = started(world, capsys)
    checker = spawn(world, config, "check")
    responder = spawn(world, config, "respond", PR7, "--actor", "human:tester")
    finish(checker)
    r_code, _ = finish(responder)
    assert r_code in (0, 20)
    assert len(rounds(world)) == 1 and len(world.fakes.calls("jobsctl", "add")) == 1
