"""Leases and compare-and-set transitions (docs/interface.md: Concurrency)."""

from __future__ import annotations

import json
import os
import select
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from review_respond import store as store_mod
from review_respond.cli import HANDLERS, main
from review_respond.errors import CliError
from review_respond.output import Context, Outcome
from review_respond.store import (
    BUSY_WAIT_SECONDS,
    HEARTBEAT_SECONDS,
    JOB_PR_WAIT_SECONDS,
    LEASE_TTL_SECONDS,
    Store,
    cas_update,
    open_state_dir,
    pr_lease_key,
    round_lease_key,
)
from tests.harness.clock import FakeClock
from tests.harness.plugin import World

WORKER = Path(__file__).parent / "harness" / "lease_worker.py"
PASS = "pass"
PR = pr_lease_key("0123456789abcdef0123")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    opened = Store.open(open_state_dir(tmp_path / "state" / "rr"), write=True, close_state=True)
    opened.clock = FakeClock()
    yield opened
    opened.close()


def state_of(store: Store) -> Path:
    return store.state_path


def reopen(store: Store) -> Store:
    other = Store.open(open_state_dir(state_of(store)), write=True, close_state=True)
    other.clock = store.clock
    return other


# ---- a real child process -----------------------------------------------------------------


class Worker:
    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self.proc = proc
        self.buf = b""

    @property
    def pid(self) -> int:
        return self.proc.pid

    def line(self, timeout: float = 20.0) -> str:
        assert self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buf:
            left = deadline - time.monotonic()
            assert left > 0, f"worker {self.pid} said nothing (have {self.buf!r})"
            ready, _, _ = select.select([fd], [], [], left)
            if ready:
                chunk = os.read(fd, 4096)
                assert chunk, f"worker {self.pid} exited (have {self.buf!r})"
                self.buf += chunk
        text, self.buf = self.buf.split(b"\n", 1)
        return text.decode()

    def finish(self, timeout: float = 20.0) -> int:
        if self.proc.stdin and not self.proc.stdin.closed:
            self.proc.stdin.close()
        return self.proc.wait(timeout=timeout)

    def kill(self, *, reap: bool) -> None:
        """SIGKILL exactly this recorded process; ``reap`` False leaves it a zombie."""
        self.proc.send_signal(signal.SIGKILL)
        if reap:
            self.proc.wait(timeout=20)
        else:
            wait_gone_or_zombie(self.pid)


def wait_gone_or_zombie(pid: int) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        state = store_mod.process_state(pid)
        if state in (None, "Z"):
            return
        time.sleep(0.01)
    raise AssertionError(f"process {pid} did not die")


def spawn(world: World, *args: str) -> Worker:
    proc = subprocess.Popen(
        [sys.executable, "-I", str(WORKER), *args],
        env=world.env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    world.procs.record(proc.pid)
    return Worker(proc)


def spawn_acquirer(world: World, store: Store, key: str, go: Path) -> Worker:
    return spawn(world, "acquire", str(state_of(store)), str(go), key)


def lease_row(store: Store, key: str) -> dict[str, object] | None:
    row = store.read_lease(key)
    return None if row is None else dict(row.__dict__)


# ---- keys, the table, the clock -----------------------------------------------------------


def test_constants_follow_the_interface() -> None:
    assert (HEARTBEAT_SECONDS, LEASE_TTL_SECONDS) == (60, 300)
    assert (BUSY_WAIT_SECONDS, JOB_PR_WAIT_SECONDS) == (30, 60)


@pytest.mark.parametrize(
    "key", ["", "Pass", "pr:", "pr:XYZ", "pr:" + "a" * 19, "pr:" + "a" * 21, "round:", "round:a/b",
            "round:../x", "round:" + "a" * 65, "other:abc", "pass ", "pass\n", "pr:" + "é" * 20]
)  # fmt: skip
def test_hostile_keys_are_rejected(store: Store, key: str) -> None:
    with pytest.raises(ValueError):
        store.try_acquire(key, heartbeat=None)


def test_key_helpers_validate() -> None:
    assert round_lease_key("rd-aaaaaaaa") == "round:rd-aaaaaaaa"
    with pytest.raises(ValueError):
        pr_lease_key("nothex")
    with pytest.raises(ValueError):
        round_lease_key("a b")


def test_acquire_records_the_row(store: Store) -> None:
    lease = store.try_acquire(PASS, heartbeat=None)
    assert lease is not None
    row = store.read_lease(PASS)
    assert row is not None
    assert row.pid == os.getpid()
    assert row.pid_start == store_mod.process_start(os.getpid())
    assert row.boot_id == store_mod.boot_id()
    now = store.clock.now().timestamp()
    assert row.acquired_at == row.renewed_at == now
    assert row.expires_at == now + LEASE_TTL_SECONDS
    assert not lease.taken_over


def test_a_held_lease_is_not_acquired_twice(store: Store) -> None:
    first = store.try_acquire(PR, heartbeat=None)
    assert first is not None
    assert store.try_acquire(PR, heartbeat=None) is None
    other = reopen(store)
    assert other.try_acquire(PR, heartbeat=None) is None
    other.close()
    # Other keys are independent.
    assert store.try_acquire(PASS, heartbeat=None) is not None


def test_release_frees_the_key_and_is_idempotent(store: Store) -> None:
    lease = store.try_acquire(PR, heartbeat=None)
    assert lease is not None
    lease.release()
    lease.release()
    assert store.read_lease(PR) is None
    assert store.try_acquire(PR, heartbeat=None) is not None


def test_context_manager_releases(store: Store) -> None:
    with store.acquire(PR, wait=0, contended="busy", heartbeat=None):
        assert store.read_lease(PR) is not None
    assert store.read_lease(PR) is None


def test_release_never_removes_another_holders_row(store: Store) -> None:
    lease = store.try_acquire(PR, heartbeat=None)
    assert lease is not None
    with store.transaction() as conn:
        conn.execute("UPDATE leases SET token = 'someone-else' WHERE key = ?", (PR,))
    lease.release()
    row = store.read_lease(PR)
    assert row is not None
    assert row.token == "someone-else"


def test_renew_moves_the_expiry(store: Store) -> None:
    lease = store.try_acquire(PR, heartbeat=None)
    assert lease is not None
    clock = store.clock
    assert isinstance(clock, FakeClock)
    clock.advance(100)
    lease.renew()
    row = store.read_lease(PR)
    assert row is not None
    assert row.renewed_at == clock.now().timestamp()
    assert row.expires_at == clock.now().timestamp() + LEASE_TTL_SECONDS
    assert row.acquired_at == clock.now().timestamp() - 100


# ---- takeover: only for a verified-gone holder --------------------------------------------


def test_a_live_holder_is_never_taken_over_even_expired_and_is_logged_stalled(
    world: World, store: Store
) -> None:
    go = world.root / "go"
    go.touch()
    holder = spawn_acquirer(world, store, PR, go)
    assert holder.line() == "READY"
    assert holder.line() == "ACQUIRED"
    clock = store.clock
    assert isinstance(clock, FakeClock)
    clock.advance(LEASE_TTL_SECONDS * 10)  # long past expires_at
    events: list[tuple[str, str]] = []
    before = lease_row(store, PR)
    assert store.try_acquire(PR, heartbeat=None, on_event=lambda *e: events.append(e)) is None
    assert events == [("stalled", PR)]
    assert lease_row(store, PR) == before  # untouched
    assert holder.finish() == 0


def test_a_killed_holders_lease_is_taken_over_at_once(world: World, store: Store) -> None:
    go = world.root / "go"
    go.touch()
    holder = spawn_acquirer(world, store, PR, go)
    assert (holder.line(), holder.line()) == ("READY", "ACQUIRED")
    row = store.read_lease(PR)
    assert row is not None and row.pid == holder.pid
    assert store.try_acquire(PR, heartbeat=None) is None  # not yet expired, alive
    holder.kill(reap=True)
    events: list[tuple[str, str]] = []
    lease = store.try_acquire(PR, heartbeat=None, on_event=lambda *e: events.append(e))
    assert lease is not None  # not expired, but verified gone
    assert lease.taken_over
    assert events == [("taken_over", PR)]
    row = store.read_lease(PR)
    assert row is not None and row.pid == os.getpid()


def test_an_unreaped_zombie_holder_counts_as_gone(world: World, store: Store) -> None:
    go = world.root / "go"
    go.touch()
    holder = spawn_acquirer(world, store, PR, go)
    assert (holder.line(), holder.line()) == ("READY", "ACQUIRED")
    holder.kill(reap=False)
    assert store_mod.process_state(holder.pid) == "Z"
    assert store.try_acquire(PR, heartbeat=None) is not None
    holder.proc.wait(timeout=20)


def tamper(store: Store, key: str, **columns: object) -> None:
    sets = ", ".join(f"{name} = ?" for name in columns)
    with store.transaction() as conn:
        conn.execute(f"UPDATE leases SET {sets} WHERE key = ?", (*columns.values(), key))


def test_a_different_start_time_means_the_pid_was_reused(store: Store) -> None:
    other = reopen(store)
    assert other.try_acquire(PR, heartbeat=None) is not None  # holder: this very process
    tamper(store, PR, pid_start="1")  # a start time this pid never had
    assert store.try_acquire(PR, heartbeat=None) is not None


def test_a_different_boot_id_means_gone(store: Store) -> None:
    assert store.try_acquire(PR, heartbeat=None) is not None
    tamper(store, PR, boot_id="another-boot")
    assert store.try_acquire(PR, heartbeat=None) is not None


def test_an_absent_pid_means_gone(store: Store) -> None:
    assert store.try_acquire(PR, heartbeat=None) is not None
    tamper(store, PR, pid=2**22 + 12345)  # above pid_max on Linux: no such process
    assert store.try_acquire(PR, heartbeat=None) is not None


def test_a_matching_identity_is_alive(store: Store) -> None:
    assert store.try_acquire(PR, heartbeat=None) is not None
    assert store.try_acquire(PR, heartbeat=None) is None


def test_the_takeover_is_a_new_holder_so_the_old_lease_object_is_lost(store: Store) -> None:
    old = store.try_acquire(PR, heartbeat=None)
    assert old is not None
    tamper(store, PR, pid_start="1")
    new = store.try_acquire(PR, heartbeat=None)
    assert new is not None
    with pytest.raises(CliError) as caught, old.transaction():
        pass
    assert caught.value.code == "lease_lost"
    with new.transaction():
        pass


# ---- concurrent acquire and takeover across processes -------------------------------------


def race(world: World, store: Store, key: str, count: int = 5) -> list[str]:
    go = world.root / "go"
    workers = [spawn_acquirer(world, store, key, go) for _ in range(count)]
    for worker in workers:
        assert worker.line() == "READY"
    go.touch()
    results = [worker.line() for worker in workers]
    for worker in workers:
        assert worker.finish() == 0
    return results


def test_concurrent_acquire_has_exactly_one_winner(world: World, store: Store) -> None:
    results = race(world, store, PR)
    assert sorted(results) == ["ACQUIRED"] + ["HELD"] * 4


def test_concurrent_takeover_of_a_dead_holder_has_exactly_one_winner(
    world: World, store: Store
) -> None:
    go = world.root / "go0"
    go.touch()
    dead = spawn_acquirer(world, store, PR, go)
    assert (dead.line(), dead.line()) == ("READY", "ACQUIRED")
    dead.kill(reap=True)
    results = race(world, store, PR)
    assert sorted(results) == ["ACQUIRED"] + ["HELD"] * 4


# ---- waiting and the documented contention results ----------------------------------------


def test_acquire_without_waiting_that_is_contended_is_busy(store: Store) -> None:
    assert store.try_acquire(PR, heartbeat=None) is not None
    with pytest.raises(CliError) as caught:
        store.acquire(PR, wait=0, contended="busy", heartbeat=None)
    assert caught.value.code == "refused"
    assert caught.value.reason == "busy"
    assert caught.value.exit_code == 20


def test_busy_after_waiting_the_documented_time(store: Store) -> None:
    clock = store.clock
    assert isinstance(clock, FakeClock)
    assert store.try_acquire(PR, heartbeat=None) is not None
    start = clock.monotonic()
    with pytest.raises(CliError) as caught:
        store.acquire(PR, wait=BUSY_WAIT_SECONDS, contended="busy", heartbeat=None)
    assert caught.value.reason == "busy"
    assert BUSY_WAIT_SECONDS <= clock.monotonic() - start < BUSY_WAIT_SECONDS + 1
    assert sum(clock.slept) >= BUSY_WAIT_SECONDS


def test_job_pr_wait_times_out_as_lease_timeout(store: Store) -> None:
    clock = store.clock
    assert isinstance(clock, FakeClock)
    assert store.try_acquire(PR, heartbeat=None) is not None
    with pytest.raises(CliError) as caught:
        store.acquire(PR, wait=JOB_PR_WAIT_SECONDS, contended="lease_timeout", heartbeat=None)
    assert caught.value.code == "lease_timeout"
    assert caught.value.exit_code == 1
    assert sum(clock.slept) >= JOB_PR_WAIT_SECONDS


def test_a_wait_ends_as_soon_as_the_holder_releases(store: Store) -> None:
    clock = store.clock
    assert isinstance(clock, FakeClock)
    first = store.try_acquire(PR, heartbeat=None)
    assert first is not None
    real_sleep = clock.sleep

    def sleep_then_release(seconds: float) -> None:
        real_sleep(seconds)
        first.release()

    clock.sleep = sleep_then_release  # type: ignore[method-assign]
    second = store.acquire(PR, wait=30, contended="busy", heartbeat=None)
    assert second.key == PR
    assert len(clock.slept) == 1


def test_a_wait_logs_stalled_once(store: Store) -> None:
    clock = store.clock
    assert isinstance(clock, FakeClock)
    other = reopen(store)
    assert other.try_acquire(PR, heartbeat=None) is not None  # alive: this process
    clock.advance(LEASE_TTL_SECONDS + 1)
    events: list[tuple[str, str]] = []
    with pytest.raises(CliError):
        store.acquire(PR, wait=2, contended="busy", heartbeat=None, on_event=lambda *e: events.append(e))
    assert events == [("stalled", PR)]


# ---- the exit codes through the CLI -------------------------------------------------------


def install(monkeypatch: pytest.MonkeyPatch, command: str, body: object) -> None:
    def handler(ctx: Context, _args: object) -> Outcome:
        assert ctx.store is not None
        ctx.store.clock = FakeClock()
        body(ctx.store)  # type: ignore[operator]
        return Outcome(payload={}, human="ok\n")

    monkeypatch.setitem(HANDLERS, command, handler)  # type: ignore[arg-type]


def test_cli_exits_1_lease_lost_for_a_write_after_losing_the_lease(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def body(store: Store) -> None:
        lease = store.try_acquire(PR, heartbeat=None)
        assert lease is not None
        tamper(store, PR, pid_start="1")
        assert store.try_acquire(PR, heartbeat=None) is not None  # someone took it over
        with lease.transaction() as conn:
            conn.execute("CREATE TABLE should_not_exist (x)")

    install(monkeypatch, "respond", body)
    code = main(["respond", "gh:o/r#1", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert out["error"]["code"] == "lease_lost"


def test_cli_exits_1_lease_timeout_when_the_pr_lease_stays_held(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def body(store: Store) -> None:
        other = reopen(store)
        assert other.try_acquire(PR, heartbeat=None) is not None
        store.acquire(PR, wait=JOB_PR_WAIT_SECONDS, contended="lease_timeout", heartbeat=None)

    install(monkeypatch, "respond", body)
    code = main(["respond", "gh:o/r#1", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert out["error"]["code"] == "lease_timeout"


def test_cli_exits_20_busy_when_a_pr_lease_stays_held(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def body(store: Store) -> None:
        other = reopen(store)
        assert other.try_acquire(PR, heartbeat=None) is not None
        store.acquire(PR, wait=BUSY_WAIT_SECONDS, contended="busy", heartbeat=None)

    install(monkeypatch, "respond", body)
    code = main(["respond", "gh:o/r#1", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 20
    assert out["error"] == {
        "code": "refused",
        "message": out["error"]["message"],
        "reason": "busy",
        "details": {"lease": PR},
    }


# ---- lease-checked transactions -----------------------------------------------------------


def test_a_transaction_commits_while_the_lease_is_held(store: Store) -> None:
    lease = store.try_acquire(PR, heartbeat=None)
    assert lease is not None
    with lease.transaction() as conn:
        conn.execute("INSERT INTO meta VALUES ('k', 'v')")
    assert store.conn.execute("SELECT value FROM meta WHERE key = 'k'").fetchone() == ("v",)


@pytest.mark.parametrize("how", ["deleted", "released", "taken_over"])
def test_a_write_after_a_lost_lease_is_lease_lost_and_changes_nothing(store: Store, how: str) -> None:
    lease = store.try_acquire(PR, heartbeat=None)
    assert lease is not None
    if how == "deleted":
        with store.transaction() as conn:
            conn.execute("DELETE FROM leases")
    elif how == "released":
        lease.release()
    else:
        tamper(store, PR, pid_start="1")
        assert store.try_acquire(PR, heartbeat=None) is not None
    with pytest.raises(CliError) as caught, lease.transaction() as conn:
        conn.execute("INSERT INTO meta VALUES ('k', 'v')")
    assert caught.value.code == "lease_lost"
    assert caught.value.exit_code == 1
    assert store.conn.execute("SELECT count(*) FROM meta").fetchone() == (0,)


def test_an_error_inside_a_lease_transaction_rolls_back_and_keeps_the_lease(store: Store) -> None:
    lease = store.try_acquire(PR, heartbeat=None)
    assert lease is not None
    with pytest.raises(RuntimeError), lease.transaction() as conn:
        conn.execute("INSERT INTO meta VALUES ('k', 'v')")
        raise RuntimeError("boom")
    assert store.conn.execute("SELECT count(*) FROM meta").fetchone() == (0,)
    assert store.read_lease(PR) is not None


def test_renew_after_a_lost_lease_is_lease_lost(store: Store) -> None:
    lease = store.try_acquire(PR, heartbeat=None)
    assert lease is not None
    with store.transaction() as conn:
        conn.execute("DELETE FROM leases")
    with pytest.raises(CliError) as caught:
        lease.renew()
    assert caught.value.code == "lease_lost"


# ---- compare-and-set on rev ---------------------------------------------------------------


@pytest.fixture
def table(store: Store) -> Store:
    with store.transaction() as conn:
        conn.execute("CREATE TABLE items (id TEXT PRIMARY KEY, value TEXT, rev INTEGER NOT NULL)")
        conn.execute("INSERT INTO items VALUES ('a', 'x', 0), ('b', 'x', 0)")
    return store


def item(store: Store, row_id: str) -> tuple[str, int]:
    return store.conn.execute("SELECT value, rev FROM items WHERE id = ?", (row_id,)).fetchone()


def test_cas_succeeds_once_per_rev(table: Store) -> None:
    with table.transaction() as conn:
        assert cas_update(conn, "items", "id", "a", 0, {"value": "y"})
        assert not cas_update(conn, "items", "id", "a", 0, {"value": "z"})  # stale
        assert cas_update(conn, "items", "id", "a", 1, {"value": "z"})
    assert item(table, "a") == ("z", 2)
    assert item(table, "b") == ("x", 0)


def test_cas_on_a_missing_row_is_false(table: Store) -> None:
    with table.transaction() as conn:
        assert not cas_update(conn, "items", "id", "nope", 0, {"value": "y"})


@pytest.mark.parametrize(
    ("table_name", "column", "changes"),
    [
        ("items; DROP TABLE items", "id", {"value": "y"}),
        ("items", "id; --", {"value": "y"}),
        ("items", "id", {"value = 'q', rev": "y"}),
        ("items", "id", {"rev": 99}),
        ("items", "id", {}),
        ("", "id", {"value": "y"}),
    ],
)
def test_cas_rejects_hostile_identifiers_and_a_rev_change(
    table: Store, table_name: str, column: str, changes: dict[str, object]
) -> None:
    with table.transaction() as conn, pytest.raises(ValueError):
        cas_update(conn, table_name, column, "a", 0, changes)
    assert item(table, "a") == ("x", 0)


def test_cas_values_are_parameters_not_sql(table: Store) -> None:
    hostile = "x'); DROP TABLE items; --"
    with table.transaction() as conn:
        assert cas_update(conn, "items", "id", "a", 0, {"value": hostile})
    assert item(table, "a") == (hostile, 1)


def test_lease_cas_checks_the_lease_in_the_same_transaction(table: Store) -> None:
    lease = table.try_acquire(PR, heartbeat=None)
    assert lease is not None
    assert lease.compare_and_set("items", "id", "a", 0, {"value": "y"})
    assert not lease.compare_and_set("items", "id", "a", 0, {"value": "z"})
    release = lease.release
    release()
    with pytest.raises(CliError) as caught:
        lease.compare_and_set("items", "id", "a", 1, {"value": "z"})
    assert caught.value.code == "lease_lost"
    assert item(table, "a") == ("y", 1)


def test_concurrent_cas_has_exactly_one_winner(world: World, table: Store) -> None:
    go = world.root / "go"
    workers = [
        spawn(world, "cas", str(state_of(table)), str(go), "items", "a", "0", f"v{i}")
        for i in range(5)
    ]
    for worker in workers:
        assert worker.line() == "READY"
    go.touch()
    results = [worker.line() for worker in workers]
    for worker in workers:
        assert worker.finish() == 0
    assert sorted(results) == ["LOST"] * 4 + ["WON"]
    value, rev = item(table, "a")
    assert rev == 1 and value in {f"v{i}" for i in range(5)}


# ---- the heartbeat ------------------------------------------------------------------------


def wait_until(check: object, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():  # type: ignore[operator]
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def test_the_heartbeat_renews_while_held_and_stops_on_release(tmp_path: Path) -> None:
    store = Store.open(open_state_dir(tmp_path / "s"), write=True, close_state=True)  # system clock
    lease = store.try_acquire(PR, heartbeat=0.02)
    assert lease is not None
    first = store.read_lease(PR)
    assert first is not None

    def advanced() -> bool:
        row = store.read_lease(PR)
        return row is not None and row.renewed_at > first.renewed_at

    wait_until(advanced)
    lease.release()
    assert store.read_lease(PR) is None
    time.sleep(0.2)  # a stopped heartbeat does not resurrect the row
    assert store.read_lease(PR) is None
    store.close()


def test_the_heartbeat_notices_a_lost_lease(tmp_path: Path) -> None:
    store = Store.open(open_state_dir(tmp_path / "s"), write=True, close_state=True)
    lease = store.try_acquire(PR, heartbeat=0.02)
    assert lease is not None
    with store.transaction() as conn:
        conn.execute("DELETE FROM leases")
    wait_until(lambda: lease.lost)
    assert store.read_lease(PR) is None  # it never re-creates a lost lease
    with pytest.raises(CliError) as caught, lease.transaction():
        pass
    assert caught.value.code == "lease_lost"
    lease.release()
    store.close()


# ---- migration ----------------------------------------------------------------------------


def test_the_leases_table_comes_from_an_appended_migration(tmp_path: Path) -> None:
    store = Store.open(open_state_dir(tmp_path / "s"), write=True, close_state=True)
    assert store.schema_version == len(store_mod.MIGRATIONS) >= 2
    conn = sqlite3.connect(store.state_path / "state.db")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(leases)")]
    conn.close()
    for needed in ("key", "pid", "pid_start", "boot_id", "acquired_at", "renewed_at", "expires_at", "rev"):
        assert needed in cols
    store.close()
