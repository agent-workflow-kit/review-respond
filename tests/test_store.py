"""State store: private directory and files, migrations, version skew, stored text files."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from review_respond import store as store_mod
from review_respond.cli import HANDLERS, main
from review_respond.errors import CliError
from review_respond.output import Context, Outcome
from review_respond.store import (
    MIGRATIONS,
    SCHEMA_VERSION,
    Store,
    migrate,
    open_state_dir,
    state_dir_path,
)


@pytest.fixture
def sdir(tmp_path: Path) -> Path:
    return tmp_path / "state" / "review-respond"


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def unsafe(call: Callable[[], object]) -> CliError:
    with pytest.raises(CliError) as caught:
        call()
    assert caught.value.code == "state_unsafe"
    assert caught.value.exit_code == 12
    return caught.value


def user_version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


# ---- state directory location ------------------------------------------------------------


def test_state_dir_path_uses_absolute_xdg_state_home() -> None:
    assert state_dir_path({"XDG_STATE_HOME": "/x/y", "HOME": "/h"}) == Path("/x/y/review-respond")


@pytest.mark.parametrize("xdg", [None, "", "relative/dir"])
def test_state_dir_path_falls_back_to_home(xdg: str | None) -> None:
    env = {"HOME": "/h"}
    if xdg is not None:
        env["XDG_STATE_HOME"] = xdg
    assert state_dir_path(env) == Path("/h/.local/state/review-respond")


# ---- private directory -------------------------------------------------------------------


@pytest.mark.parametrize("umask", [0o022, 0o000, 0o077])
def test_created_with_explicit_modes_whatever_the_umask(sdir: Path, umask: int) -> None:
    old = os.umask(umask)
    try:
        open_state_dir(sdir)
    finally:
        os.umask(old)
    assert mode(sdir) == 0o700
    assert mode(sdir.parent) == 0o700  # an ancestor created on the way


def test_existing_private_directory_is_accepted(sdir: Path) -> None:
    sdir.mkdir(parents=True, mode=0o700)
    sdir.chmod(0o700)
    assert open_state_dir(sdir).path == sdir


@pytest.mark.parametrize("bits", [0o755, 0o750, 0o710, 0o701, 0o704, 0o770, 0o777])
def test_group_or_world_accessible_directory_is_refused(sdir: Path, bits: int) -> None:
    sdir.mkdir(parents=True)
    sdir.chmod(bits)
    unsafe(lambda: open_state_dir(sdir))
    assert mode(sdir) == bits  # refused, not repaired


def test_directory_owned_by_another_user_is_refused(sdir: Path) -> None:
    sdir.mkdir(parents=True, mode=0o700)
    sdir.chmod(0o700)
    unsafe(lambda: open_state_dir(sdir, euid=os.geteuid() + 1))


def test_symlinked_directory_is_refused(sdir: Path, tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    sdir.parent.mkdir(parents=True)
    sdir.symlink_to(real)
    unsafe(lambda: open_state_dir(sdir))


def test_regular_file_in_place_of_directory_is_refused(sdir: Path) -> None:
    sdir.parent.mkdir(parents=True)
    sdir.write_text("x")
    unsafe(lambda: open_state_dir(sdir))


def test_fifo_in_place_of_directory_is_refused(sdir: Path) -> None:
    sdir.parent.mkdir(parents=True)
    os.mkfifo(sdir)
    unsafe(lambda: open_state_dir(sdir))


def test_uncreatable_directory_is_state_unsafe(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    unsafe(lambda: open_state_dir(blocker / "review-respond"))


# ---- private database files --------------------------------------------------------------


def test_database_and_sidecars_are_0600(sdir: Path) -> None:
    with Store.open(open_state_dir(sdir), write=True) as db:
        with db.transaction():
            db.conn.execute("INSERT INTO meta(key, value) VALUES ('k', 'v')")
        assert (sdir / "state.db-wal").exists()
        assert mode(sdir / "state.db") == 0o600
        assert mode(sdir / "state.db-wal") == 0o600
        assert mode(sdir / "state.db-shm") == 0o600


def test_database_created_0600_under_a_loose_umask(sdir: Path) -> None:
    old = os.umask(0)
    try:
        Store.open(open_state_dir(sdir), write=True).close()
    finally:
        os.umask(old)
    assert mode(sdir / "state.db") == 0o600


@pytest.mark.parametrize("name", ["state.db", "state.db-wal", "state.db-shm"])
@pytest.mark.parametrize("bits", [0o644, 0o640, 0o660, 0o604])
def test_loose_database_file_is_refused(sdir: Path, name: str, bits: int) -> None:
    state = open_state_dir(sdir)
    Store.open(state, write=True).close()
    target = sdir / name
    target.touch()
    target.chmod(bits)
    unsafe(lambda: Store.open(state, write=True))
    unsafe(lambda: Store.open(state, write=False))


def test_symlinked_database_is_refused(sdir: Path, tmp_path: Path) -> None:
    state = open_state_dir(sdir)
    real = tmp_path / "elsewhere.db"
    real.touch(mode=0o600)
    (sdir / "state.db").symlink_to(real)
    unsafe(lambda: Store.open(state, write=True))


def test_symlinked_wal_is_refused(sdir: Path, tmp_path: Path) -> None:
    state = open_state_dir(sdir)
    Store.open(state, write=True).close()
    real = tmp_path / "w"
    real.touch(mode=0o600)
    (sdir / "state.db-wal").symlink_to(real)
    unsafe(lambda: Store.open(state, write=True))


def test_fifo_database_is_refused_without_blocking(sdir: Path) -> None:
    state = open_state_dir(sdir)
    os.mkfifo(sdir / "state.db", 0o600)
    unsafe(lambda: Store.open(state, write=True))


def test_directory_database_is_refused(sdir: Path) -> None:
    state = open_state_dir(sdir)
    (sdir / "state.db").mkdir(mode=0o700)
    unsafe(lambda: Store.open(state, write=True))


def test_hard_linked_database_is_refused(sdir: Path, tmp_path: Path) -> None:
    state = open_state_dir(sdir)
    Store.open(state, write=True).close()
    os.link(sdir / "state.db", tmp_path / "alias.db")
    unsafe(lambda: Store.open(state, write=True))


def test_database_owned_by_another_user_is_refused(sdir: Path) -> None:
    state = open_state_dir(sdir)
    Store.open(state, write=True).close()
    unsafe(lambda: Store.open(state, write=True, euid=os.geteuid() + 1))


# ---- migrations --------------------------------------------------------------------------

TEST_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    ("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",),
    ("CREATE TABLE a (id INTEGER PRIMARY KEY, x TEXT)", "CREATE INDEX a_x ON a(x)"),
    ("ALTER TABLE a ADD COLUMN y TEXT", "CREATE TABLE b (id INTEGER PRIMARY KEY)"),
)


def schema(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [(str(n), str(s)) for n, s in rows]


def test_the_real_migrations_start_at_one_and_define_meta() -> None:
    assert SCHEMA_VERSION == len(MIGRATIONS) >= 1


def test_fresh_database_is_migrated_to_the_latest_version(sdir: Path) -> None:
    with Store.open(open_state_dir(sdir), write=True) as db:
        assert db.schema_version == SCHEMA_VERSION
        tables = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master")}
        assert "meta" in tables
        assert db.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0
    assert user_version(sdir / "state.db") == SCHEMA_VERSION


def test_reopening_changes_nothing(sdir: Path) -> None:
    state = open_state_dir(sdir)
    with Store.open(state, write=True) as db:
        with db.transaction():
            db.conn.execute("INSERT INTO meta(key, value) VALUES ('installed_at', 't')")
    with Store.open(state, write=True) as db:
        assert db.conn.execute("SELECT value FROM meta WHERE key='installed_at'").fetchone() == (
            "t",
        )
        assert db.schema_version == SCHEMA_VERSION


def test_every_prior_version_upgrades_to_the_same_schema_and_keeps_data() -> None:
    fresh = sqlite3.connect(":memory:", isolation_level=None)
    assert migrate(fresh, TEST_MIGRATIONS) == len(TEST_MIGRATIONS)
    for start in range(len(TEST_MIGRATIONS)):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        migrate(conn, TEST_MIGRATIONS[:start])
        assert conn.execute("PRAGMA user_version").fetchone()[0] == start
        if start >= 1:
            conn.execute("INSERT INTO meta VALUES ('k', 'v')")
        assert migrate(conn, TEST_MIGRATIONS) == len(TEST_MIGRATIONS)
        assert schema(conn) == schema(fresh)
        if start >= 1:
            assert conn.execute("SELECT * FROM meta").fetchall() == [("k", "v")]


def test_migrating_to_the_current_version_is_a_noop() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    migrate(conn, TEST_MIGRATIONS)
    before = schema(conn)
    assert migrate(conn, TEST_MIGRATIONS) == len(TEST_MIGRATIONS)
    assert schema(conn) == before


def test_real_migrations_apply_in_order_on_each_prior_version() -> None:
    fresh = sqlite3.connect(":memory:", isolation_level=None)
    migrate(fresh, MIGRATIONS)
    for start in range(len(MIGRATIONS)):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        migrate(conn, MIGRATIONS[:start])
        migrate(conn, MIGRATIONS)
        assert schema(conn) == schema(fresh)


def test_failed_migration_rolls_back_the_whole_step() -> None:
    broken = (
        *TEST_MIGRATIONS[:1],
        ("CREATE TABLE half (id INTEGER)", "INSERT INTO nowhere VALUES (1)"),
    )
    conn = sqlite3.connect(":memory:", isolation_level=None)
    with pytest.raises(CliError) as caught:
        migrate(conn, broken)
    assert caught.value.code == "internal"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1  # step 1 stays applied
    names = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert {r[0] for r in names} == {"meta"}
    # a fixed later release finishes the job
    assert migrate(conn, TEST_MIGRATIONS) == len(TEST_MIGRATIONS)


def test_failed_migration_on_disk_leaves_a_usable_database(
    sdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = open_state_dir(sdir)
    bad = (*MIGRATIONS, ("CREATE TABLE t (id INTEGER)", "SELECT * FROM missing_table_zz"))
    monkeypatch.setattr(store_mod, "MIGRATIONS", bad)
    monkeypatch.setattr(store_mod, "SCHEMA_VERSION", len(bad))
    with pytest.raises(CliError) as caught:
        Store.open(state, write=True)
    assert caught.value.code == "internal"
    assert user_version(sdir / "state.db") == len(MIGRATIONS)
    monkeypatch.undo()
    with Store.open(state, write=True) as db:
        assert db.schema_version == SCHEMA_VERSION


def test_concurrent_first_opens_migrate_once_and_all_succeed(sdir: Path) -> None:
    state = open_state_dir(sdir)
    count = 8
    barrier = threading.Barrier(count)
    errors: list[BaseException] = []
    versions: list[int] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=10)
            with Store.open(state, write=True) as db:
                with db.transaction():
                    db.conn.execute(
                        "INSERT INTO meta(key, value) VALUES (?, 'x')",
                        (f"k{threading.get_ident()}",),
                    )
                versions.append(db.schema_version)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors
    assert versions == [SCHEMA_VERSION] * count
    with Store.open(state, write=False) as db:
        assert db.conn.execute("SELECT count(*) FROM meta").fetchone()[0] == count


def test_a_locked_database_gives_a_clear_error_after_the_busy_timeout(sdir: Path) -> None:
    state = open_state_dir(sdir)
    Store.open(state, write=True).close()
    holder = sqlite3.connect(sdir / "state.db", isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with Store.open(state, write=True, busy_timeout_ms=50) as db:
            with pytest.raises(CliError) as caught:
                with db.transaction():
                    pass
        assert caught.value.code == "internal"
        assert "busy" in caught.value.message
    finally:
        holder.execute("ROLLBACK")
        holder.close()


# ---- version skew ------------------------------------------------------------------------


def make_newer(sdir: Path) -> bytes:
    state = open_state_dir(sdir)
    with Store.open(state, write=True) as db:
        with db.transaction():
            db.conn.execute("INSERT INTO meta(key, value) VALUES ('k', 'v')")
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn = sqlite3.connect(sdir / "state.db", isolation_level=None)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    return (sdir / "state.db").read_bytes()


def test_newer_schema_refuses_writes_with_version_skew(sdir: Path) -> None:
    before = make_newer(sdir)
    with pytest.raises(CliError) as caught:
        Store.open(open_state_dir(sdir), write=True)
    assert caught.value.code == "version_skew"
    assert caught.value.exit_code == 12
    assert (sdir / "state.db").read_bytes() == before
    assert user_version(sdir / "state.db") == SCHEMA_VERSION + 1


def test_newer_schema_allows_reads_and_blocks_writes(sdir: Path) -> None:
    make_newer(sdir)
    with Store.open(open_state_dir(sdir), write=False) as db:
        assert db.schema_version == SCHEMA_VERSION + 1
        assert db.conn.execute("SELECT value FROM meta WHERE key='k'").fetchone() == ("v",)
        with pytest.raises(sqlite3.OperationalError):
            db.conn.execute("INSERT INTO meta(key, value) VALUES ('z', 'z')")
        with pytest.raises(CliError) as caught:
            with db.transaction():
                pass
        assert caught.value.code == "version_skew"
    assert user_version(sdir / "state.db") == SCHEMA_VERSION + 1


def test_older_schema_is_upgraded_even_by_a_read(sdir: Path) -> None:
    sdir.mkdir(parents=True, mode=0o700)
    sdir.chmod(0o700)
    # the database a v0 binary would have left: empty file
    (sdir / "state.db").touch(mode=0o600)
    with Store.open(open_state_dir(sdir), write=False) as db:
        assert db.schema_version == SCHEMA_VERSION


# ---- CLI integration ---------------------------------------------------------------------

COMMAND_ARGV: dict[str, list[str]] = {
    "check": ["check"],
    "respond": ["respond", "gh:o/r#1"],
    "list": ["list"],
    "show": ["show", "rp-aaaaaaaa"],
    "pending": ["pending"],
    "confirm": ["confirm", "rp-aaaaaaaa"],
    "reject": ["reject", "rp-aaaaaaaa"],
    "edit": ["edit", "rp-aaaaaaaa"],
    "drop-reply": ["drop-reply", "rp-aaaaaaaa", "c"],
    "stop": ["stop", "rd-aaaaaaaa"],
    "cron": ["cron", "install"],
    "export": ["export"],
    "prune": ["prune"],
    "validate": ["validate"],
    "_job": ["_job", "rd-aaaaaaaa"],
}
READS = {"list", "show", "pending", "export", "validate", "cron"}
WRITES = set(COMMAND_ARGV) - READS


def state_path(world: object) -> Path:
    return Path(os.environ["XDG_STATE_HOME"]) / "review-respond"


def install_probe(monkeypatch: pytest.MonkeyPatch, command: str, seen: list[Context]) -> None:
    def handler(ctx: Context, _args: object) -> Outcome:
        seen.append(ctx)
        return Outcome(payload={}, human="ok\n")

    monkeypatch.setitem(HANDLERS, command, handler)  # type: ignore[arg-type]


@pytest.mark.parametrize("command", sorted(COMMAND_ARGV))
def test_every_command_refuses_a_group_accessible_state_dir(
    command: str, world: object, capsys: pytest.CaptureFixture[str]
) -> None:
    path = state_path(world)
    path.mkdir(parents=True)
    path.chmod(0o750)
    code = main([*COMMAND_ARGV[command], "--json"])
    out = capsys.readouterr().out
    assert code == 12
    assert json.loads(out)["error"]["code"] == "state_unsafe"


def test_a_first_run_creates_the_private_state_directory(
    world: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Context] = []
    install_probe(monkeypatch, "list", seen)
    assert main(["list"]) == 0
    assert mode(state_path(world)) == 0o700
    assert seen[0].store is not None
    assert seen[0].store.schema_version == SCHEMA_VERSION


@pytest.mark.parametrize("command", sorted(WRITES))
def test_write_commands_exit_12_version_skew_on_a_newer_schema(
    command: str, world: object, capsys: pytest.CaptureFixture[str]
) -> None:
    make_newer(state_path(world))
    code = main([*COMMAND_ARGV[command], "--json"])
    body = json.loads(capsys.readouterr().out)
    assert code == 12
    assert body["error"]["code"] == "version_skew"


@pytest.mark.parametrize("command", sorted(READS))
def test_read_commands_work_on_a_newer_schema(
    command: str, world: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_newer(state_path(world))
    seen: list[Context] = []
    install_probe(monkeypatch, command, seen)
    assert main(COMMAND_ARGV[command]) == 0
    assert seen[0].store is not None
    assert seen[0].store.schema_version == SCHEMA_VERSION + 1


def test_the_store_is_closed_after_the_command(
    world: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Context] = []
    install_probe(monkeypatch, "list", seen)
    main(["list"])
    assert seen[0].store is not None
    with pytest.raises(sqlite3.ProgrammingError):
        seen[0].store.conn.execute("SELECT 1")


# ---- stored text files -------------------------------------------------------------------


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_stored_file_round_trips_under_its_owner_and_hash(sdir: Path) -> None:
    state = open_state_dir(sdir)
    digest = state.put_file("rp-abcd2345", b"hello\n")
    assert digest == sha(b"hello\n")
    path = sdir / "files" / "rp-abcd2345" / digest
    assert path.read_bytes() == b"hello\n"
    assert mode(path) == 0o600
    assert mode(path.parent) == 0o700
    assert mode(path.parent.parent) == 0o700
    assert state.get_file("rp-abcd2345", digest) == b"hello\n"


def test_put_is_idempotent_and_files_of_owners_are_separate(sdir: Path) -> None:
    state = open_state_dir(sdir)
    assert state.put_file("rp-a", b"x") == state.put_file("rp-a", b"x")
    state.put_file("rp-b", b"x")
    assert (sdir / "files" / "rp-a" / sha(b"x")).exists()
    assert (sdir / "files" / "rp-b" / sha(b"x")).exists()
    assert os.lstat(sdir / "files" / "rp-a" / sha(b"x")).st_ino != os.lstat(
        sdir / "files" / "rp-b" / sha(b"x")
    ).st_ino


def test_empty_and_binary_and_unicode_content(sdir: Path) -> None:
    state = open_state_dir(sdir)
    for data in (b"", b"\x00\xff\xfe", "héllo ‮".encode()):
        assert state.get_file("rp-a", state.put_file("rp-a", data)) == data


def test_tampered_file_is_state_unsafe(sdir: Path) -> None:
    state = open_state_dir(sdir)
    digest = state.put_file("rp-a", b"original")
    (sdir / "files" / "rp-a" / digest).write_bytes(b"tampered")
    unsafe(lambda: state.get_file("rp-a", digest))


def test_put_over_a_tampered_file_is_state_unsafe(sdir: Path) -> None:
    state = open_state_dir(sdir)
    digest = state.put_file("rp-a", b"original")
    (sdir / "files" / "rp-a" / digest).write_bytes(b"tampered")
    unsafe(lambda: state.put_file("rp-a", b"original"))


def test_missing_file_is_state_unsafe(sdir: Path) -> None:
    state = open_state_dir(sdir)
    unsafe(lambda: state.get_file("rp-a", sha(b"never stored")))


def test_a_file_stored_for_another_owner_is_not_found(sdir: Path) -> None:
    state = open_state_dir(sdir)
    digest = state.put_file("rp-a", b"mine")
    unsafe(lambda: state.get_file("rp-b", digest))


def test_symlinked_stored_file_is_refused(sdir: Path, tmp_path: Path) -> None:
    state = open_state_dir(sdir)
    digest = state.put_file("rp-a", b"data")
    path = sdir / "files" / "rp-a" / digest
    other = tmp_path / "copy"
    other.write_bytes(b"data")
    other.chmod(0o600)
    path.unlink()
    path.symlink_to(other)
    unsafe(lambda: state.get_file("rp-a", digest))


def test_loose_mode_stored_file_is_refused(sdir: Path) -> None:
    state = open_state_dir(sdir)
    digest = state.put_file("rp-a", b"data")
    (sdir / "files" / "rp-a" / digest).chmod(0o644)
    unsafe(lambda: state.get_file("rp-a", digest))


def test_fifo_stored_file_is_refused_without_blocking(sdir: Path) -> None:
    state = open_state_dir(sdir)
    digest = state.put_file("rp-a", b"data")
    path = sdir / "files" / "rp-a" / digest
    path.unlink()
    os.mkfifo(path, 0o600)
    unsafe(lambda: state.get_file("rp-a", digest))


def test_symlinked_owner_directory_is_refused(sdir: Path, tmp_path: Path) -> None:
    state = open_state_dir(sdir)
    state.put_file("rp-a", b"data")
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (sdir / "files" / "rp-evil").symlink_to(real)
    unsafe(lambda: state.put_file("rp-evil", b"data"))
    assert list(real.iterdir()) == []


def test_loose_owner_directory_is_refused(sdir: Path) -> None:
    state = open_state_dir(sdir)
    state.put_file("rp-a", b"data")
    (sdir / "files" / "rp-a").chmod(0o755)
    unsafe(lambda: state.put_file("rp-a", b"more"))


@pytest.mark.parametrize(
    "owner", ["", ".", "..", "a/b", "../x", "a\0b", "x" * 65, "sp ace", "é", "-rf", "a\n"]
)
def test_hostile_owner_ids_are_rejected(sdir: Path, owner: str) -> None:
    state = open_state_dir(sdir)
    with pytest.raises(ValueError):
        state.put_file(owner, b"x")
    with pytest.raises(ValueError):
        state.get_file(owner, sha(b"x"))
    assert not (sdir / "files").exists() or list((sdir / "files").iterdir()) == []


@pytest.mark.parametrize("digest", ["", "abc", "../" + "a" * 61, "A" * 64, "g" * 64, "a" * 65])
def test_malformed_digests_are_rejected(sdir: Path, digest: str) -> None:
    state = open_state_dir(sdir)
    with pytest.raises(ValueError):
        state.get_file("rp-a", digest)


def test_a_failed_put_leaves_no_partial_file(sdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = open_state_dir(sdir)
    state.put_file("rp-a", b"first")

    def boom(_fd: int) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(store_mod.os, "fsync", boom)
    with pytest.raises(OSError):
        state.put_file("rp-a", b"second")
    monkeypatch.undo()
    assert sorted(p.name for p in (sdir / "files" / "rp-a").iterdir()) == [sha(b"first")]


def test_delete_owner_files_removes_only_that_owner(sdir: Path) -> None:
    state = open_state_dir(sdir)
    state.put_file("rp-a", b"1")
    state.put_file("rp-a", b"2")
    digest = state.put_file("rp-b", b"3")
    state.delete_owner_files("rp-a")
    assert not (sdir / "files" / "rp-a").exists()
    assert state.get_file("rp-b", digest) == b"3"
    state.delete_owner_files("rp-a")  # already gone: not an error


def test_delete_owner_files_does_not_follow_a_symlinked_directory(
    sdir: Path, tmp_path: Path
) -> None:
    state = open_state_dir(sdir)
    state.put_file("rp-a", b"x")
    victim = tmp_path / "victim"
    victim.mkdir(mode=0o700)
    (victim / "keep").write_text("keep")
    (sdir / "files" / "rp-evil").symlink_to(victim)
    unsafe(lambda: state.delete_owner_files("rp-evil"))
    assert (victim / "keep").read_text() == "keep"
