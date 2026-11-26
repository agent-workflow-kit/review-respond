"""Two real processes moving the same round (docs/interface.md: Round states, Concurrency).

One holds the PR's lease and the other the round's own lease, as `stop` and the finalizer do. They
start together on a go file; the winner is read from what each reports and from the database,
never from the order of any log. Exactly one move may change the round.
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

from review_respond.rounds import Rounds, get_round
from review_respond.store import (
    Store,
    open_state_dir,
    pr_digest,
    pr_lease_key,
    round_lease_key,
)
from tests.harness.plugin import World
from tests.test_rounds import C1, C2, Env

WORKER = Path(__file__).parent / "harness" / "rounds_worker.py"


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


def spawn(
    world: World, store: Store, go: Path, key: str, rid: str, action: str
) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [sys.executable, "-I", str(WORKER), str(store.state_path), str(go), key, rid, action],
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
    world: World, store: Store, tmp_path: Path, rid: str, env: Env, actions: list[str]
) -> list[str]:
    """Run two actions in two processes: the first under the round lease (the finalizer), the
    second under the PR lease (`stop`, the reconcile). The test's own PR lease is given up first
    (``Env.lease`` is taken again by ``Env.again``)."""
    go = tmp_path / "go"
    assert env.lease is not None
    env.lease.release()
    keys = [round_lease_key(rid), pr_lease_key(pr_digest(env.pr))]
    assert len(actions) == 2
    procs = [spawn(world, store, go, k, rid, a) for k, a in zip(keys, actions, strict=True)]
    try:
        assert [line(p) for p in procs] == ["READY"] * 2
        go.write_text("go")
        results = [line(p) for p in procs]
        for p in procs:
            assert p.wait(timeout=30) == 0
        return results
    finally:
        for p in procs:
            if p.poll() is None:
                p.wait(timeout=30)


def changed(results: list[str]) -> list[bool]:
    return [r.split()[1] == "changed" for r in results]


def block_reason(store: Store) -> str | None:
    import json

    text = store.conn.execute("SELECT block FROM prs").fetchone()[0]
    return None if text is None else json.loads(text)["reason"]


RUNNING_RACES = [
    ("to_failed", "to_failed", "failed"),
    ("to_failed", "to_pending", None),
    ("to_pending", "to_failed", None),
    ("to_escalated", "to_failed", None),
    ("finish_failed", "to_pending", None),
]


@pytest.mark.parametrize(("first", "second", "same"), RUNNING_RACES)
def test_two_moves_of_a_running_round_change_it_once(
    world: World, store: Store, env: Env, tmp_path: Path, first: str, second: str, same: str | None
) -> None:
    rid = env.running()
    before = env.row(rid)
    results = race(world, store, tmp_path, rid, env, [first, second])
    assert sorted(changed(results)) == [False, True]
    after = get_round(store.conn, rid)
    assert after is not None and after.state != "running"
    winner = [r for r in results if "unchanged" not in r][0].split()[2]
    assert after.state == winner  # the reported winner is what is stored
    assert after.rev == before.rev + 1  # exactly one compare-and-set landed
    for cid in (C1, C2):
        state, owner = env.comment(cid)
        if after.state == "pending":
            assert (state, owner) == ("handled", rid)
        else:
            assert (state, owner) == ("unhandled", None)
    expected = {"pending": None, "escalated": "escalated", "failed": "failed"}[after.state]
    assert block_reason(store) == expected


def test_a_stop_and_a_failure_end_a_running_round_once(
    world: World, store: Store, env: Env, tmp_path: Path
) -> None:
    rid = env.running()
    env.rounds.request_cancel(rid, "stopped")
    results = race(world, store, tmp_path, rid, env, ["to_canceled", "to_failed"])
    assert sorted(changed(results)) == [False, True]
    after = env.row(rid)
    assert after.state in ("canceled", "failed")
    assert block_reason(store) == ("stopped" if after.state == "canceled" else "failed")


QUEUED_RACES = [
    ("start", "reconcile_lost"),
    ("start", "cancel_flagged"),
    ("cancel_flagged", "reconcile_lost"),
]


@pytest.mark.parametrize(("first", "second"), QUEUED_RACES)
def test_two_moves_of_a_queued_round_change_it_once(
    world: World, store: Store, env: Env, tmp_path: Path, first: str, second: str
) -> None:
    rid = env.queued()
    env.rounds.request_cancel(rid, "pr_closed")
    results = race(world, store, tmp_path, rid, env, [first, second])
    assert sorted(changed(results)) == [False, True]
    after = env.row(rid)
    assert after.state in ("running", "canceled", "failed")
    delivered = env.comment(C1)[0] == "delivered"
    assert delivered == (after.state == "running")  # comments follow the winner alone


def test_two_settlements_of_a_pending_round_land_once(
    world: World, store: Store, env: Env, tmp_path: Path
) -> None:
    rid = env.reach("pending")
    results = race(world, store, tmp_path, rid, env, ["settle_rejected", "settle_closed"])
    assert sorted(changed(results)) == [False, True]
    assert env.row(rid).state in ("rejected", "closed")


def test_a_round_created_while_another_process_holds_its_round_lease_is_unaffected(
    world: World, store: Store, env: Env, tmp_path: Path
) -> None:
    rid = env.running()
    go = tmp_path / "go"
    assert env.lease is not None
    env.lease.release()
    proc = spawn(world, store, go, round_lease_key(rid), rid, "to_failed")
    try:
        assert line(proc) == "READY"
        go.write_text("go")
        assert line(proc).startswith("RESULT changed failed")
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.wait(timeout=30)
    again = Rounds(env.take_again()).create(env.pr)
    assert again.number == 2
