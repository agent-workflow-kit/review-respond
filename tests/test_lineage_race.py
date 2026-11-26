"""Two real processes moving the fork lineage (docs/interface.md: Fork lineage, Concurrency).

Each worker holds a lease (the PR's, a round's or the pass lease) and they start together on a go
file, so the writes really overlap. The outcome is read from what each reports and from the
database, never from the order of any log. The PIDs are recorded in ``world.procs``.
"""

from __future__ import annotations

import os
import select
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from review_respond import lineage
from review_respond.store import (
    Store,
    open_state_dir,
    pass_lease_key,
    pr_digest,
    pr_lease_key,
    round_lease_key,
)
from tests.harness.agents import original
from tests.harness.plugin import World
from tests.test_detect import insert_pr
from tests.test_lineage import (
    FORK1,
    FORK2,
    FORK3,
    ORIGINAL,
    base_of,
    round_with_fork,
    states,
    switch,
)
from tests.test_rounds import Env

WORKER = Path(__file__).parent / "harness" / "lineage_worker.py"
ATTEMPTS = range(5)


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
    with store.transaction() as conn:
        conn.execute("UPDATE prs SET base_agent = ?", (ORIGINAL,))
    yield e
    if e.lease is not None:
        e.lease.release()


def launch(
    world: World, store: Store, go: Path, key: str, action: str, *args: str
) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [sys.executable, "-I", str(WORKER), str(store.state_path), str(go), key, action, *args],
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
            assert chunk, (
                f"worker {proc.pid} exited (have {buf!r}, {proc.stderr and proc.stderr.read()!r})"
            )
            buf += chunk
    return buf.split(b"\n", 1)[0].decode()


def race(
    world: World, store: Store, tmp_path: Path, jobs: list[tuple[str, str, tuple[str, ...]]]
) -> list[str]:
    """Run (lease key, action, args) in one process each, started together."""
    go = tmp_path / "go"
    procs = [launch(world, store, go, key, action, *args) for key, action, args in jobs]
    try:
        assert [line(p) for p in procs] == ["READY"] * len(procs)
        go.write_text("go")
        results = [line(p) for p in procs]
        for p in procs:
            assert p.wait(timeout=30) == 0
        return results
    finally:
        for p in procs:
            if p.poll() is None:
                p.wait(timeout=30)


def bases(env: Env) -> list[str]:
    return [r[0] for r in env.store.conn.execute("SELECT agent_id FROM forks WHERE state = 'base'")]


@pytest.mark.parametrize("attempt", ATTEMPTS)
def test_two_processes_switching_the_same_round_change_it_once(
    world: World, store: Store, env: Env, tmp_path: Path, attempt: int
) -> None:
    rid = round_with_fork(env, FORK1, "pending")
    assert env.lease is not None
    env.lease.release()
    env.lease = None
    pr_key = pr_lease_key(pr_digest(env.pr))
    results = race(
        world, store, tmp_path,
        [(round_lease_key(rid), "switch", (rid,)), (pr_key, "switch", (rid,))],
    )  # fmt: skip
    assert sorted(r.split()[1] for r in results) == ["changed", "unchanged"]
    assert bases(env) == [FORK1] and base_of(env) == FORK1
    assert states(env) == {FORK1: "base"}


@pytest.mark.parametrize("attempt", ATTEMPTS)
def test_two_rounds_switching_at_once_leave_exactly_one_base(
    world: World, store: Store, env: Env, tmp_path: Path, attempt: int
) -> None:
    r1 = round_with_fork(env, FORK1, "pending")
    r2 = round_with_fork(env, FORK2, "pending")
    assert env.lease is not None
    env.lease.release()
    env.lease = None
    pr_key = pr_lease_key(pr_digest(env.pr))
    results = race(
        world, store, tmp_path,
        [(round_lease_key(r1), "switch", (r1,)), (pr_key, "switch", (r2,))],
    )  # fmt: skip
    assert all(r.startswith("RESULT changed") for r in results)
    (winner,) = bases(env)
    loser = FORK1 if winner == FORK2 else FORK2
    assert base_of(env) == winner
    assert states(env) == {winner: "base", loser: "teardown_pending"}


@pytest.mark.parametrize("attempt", ATTEMPTS)
def test_one_agent_id_is_recorded_for_one_round_only(
    world: World, store: Store, env: Env, tmp_path: Path, attempt: int
) -> None:
    other = insert_pr(store, 8)
    rids = ["rd-aaaaaaaa", "rd-bbbbbbbb"]
    with store.transaction() as conn:
        for rid, pr, n in zip(rids, (env.pr, other), (1, 1), strict=True):
            conn.execute(
                "INSERT INTO rounds (id, pr, number, state, trigger_kind, created_at, started_at)"
                " VALUES (?, ?, ?, 'running', 'check', 1, 1)",
                (rid, pr, n),
            )
    assert env.lease is not None
    env.lease.release()
    env.lease = None
    results = race(
        world, store, tmp_path,
        [(round_lease_key(r), "record", (r, FORK1)) for r in rids],
    )  # fmt: skip
    assert sorted(results) == ["FAILED internal", "RESULT recorded"]
    assert len(states(env)) == 1 and FORK1 in states(env)


@pytest.mark.parametrize("attempt", ATTEMPTS)
def test_a_teardown_pass_and_a_switch_never_touch_the_base(
    world: World, store: Store, env: Env, tmp_path: Path, attempt: int
) -> None:
    marked = round_with_fork(env, FORK3, "failed")
    assert marked
    assert env.lease is not None
    with env.lease.transaction() as conn:
        assert lineage.mark_teardown_pending(conn, FORK3, now=1.0)
    switch(env, round_with_fork(env, FORK1, "pending"))
    r2 = round_with_fork(env, FORK2, "pending")
    env.lease.release()
    env.lease = None
    doc = {
        "agent": original(FORK3, env.pr, status="torn_down", torn_down_at="2026-01-01T12:00:00Z"),
        "worktrees": [],
        "warnings": [],
    }
    world.fakes.script("agent-spawn", "teardown", json=doc, schema=False, times=None)
    pr_key = pr_lease_key(pr_digest(env.pr))
    race(
        world, store, tmp_path,
        [(pass_lease_key(), "teardown", ()), (pr_key, "switch", (r2,))],
    )  # fmt: skip
    final = states(env)
    assert final[FORK3] == "torn_down"
    assert final[FORK2] == "base" and base_of(env) == FORK2 and bases(env) == [FORK2]
    assert final[FORK1] in ("teardown_pending", "torn_down")
    asked = [c.argv[1] for c in world.fakes.calls("agent-spawn", "teardown")]
    assert FORK3 in asked and set(asked) <= {FORK1, FORK3} and FORK2 not in asked
