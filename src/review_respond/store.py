"""The private state directory, ``state.db`` (SQLite, WAL, additive migrations) and stored files.

docs/interface.md: Files and state, Upgrades. Every command opens the state directory first and
refuses (exit 12 ``state_unsafe``) when it is not private. A schema newer than this binary knows
is readable and not writable (exit 12 ``version_skew``).

Leases and compare-and-set transitions belong to #10, the audit and log writers to #11, and the
tables of each feature to the issue that needs them; this module only gives them the connection,
a transaction helper and the migration list to append to.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import sqlite3
import stat
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Final

from .errors import CliError

APP_DIR: Final = "review-respond"
DB_NAME: Final = "state.db"
FILES_DIR: Final = "files"
DEFAULT_BUSY_TIMEOUT_MS: Final = 5000
MAX_STORED_FILE_BYTES: Final = 64 * 1024 * 1024

# Additive, append-only: each entry is the statements of one version. Never edit a released
# entry; add a new one. The database's `PRAGMA user_version` is the number applied. Tables are
# added by the issue whose feature needs them (leases #10, PRs and comments #13+, ...).
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    ("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID",),
)
SCHEMA_VERSION: int = len(MIGRATIONS)

_OWNER_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")


def _unsafe(message: str) -> CliError:
    return CliError("state_unsafe", message)


def state_dir_path(environ: Mapping[str, str] | None = None) -> Path:
    """``$XDG_STATE_HOME/review-respond`` (only when absolute), else ``~/.local/state/...``."""
    env = os.environ if environ is None else environ
    xdg = env.get("XDG_STATE_HOME", "")
    if xdg.startswith("/"):
        base = Path(xdg)
    else:
        home = env.get("HOME") or os.path.expanduser("~")
        base = Path(home) / ".local" / "state"
    return base / APP_DIR


# ---- private directories and files -------------------------------------------------------


def _check_private(info: os.stat_result, path: Path | str, *, euid: int, what: str) -> None:
    if info.st_uid != euid:
        raise _unsafe(f"{what} {path} is owned by another user; refusing to use it")
    if info.st_mode & 0o077:
        raise _unsafe(
            f"{what} {path} is accessible to group or others; refusing to use it (chmod go-rwx)"
        )


def _open_dir(path: Path, *, euid: int, create: bool, dir_fd: int | None = None) -> int:
    """Open a private directory without following a symlink and return the descriptor.

    A missing directory is created 0700 when ``create`` (the mode is set explicitly, not left to
    the umask). A symlink, a non-directory, a foreign owner or any group/other bit is refused;
    an existing directory is never repaired.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    target: str | Path = path if dir_fd is None else path.name
    fd = -1
    for attempt in (0, 1):
        try:
            fd = os.open(target, flags, dir_fd=dir_fd)
            break
        except FileNotFoundError:
            if not create or attempt:
                raise _unsafe(f"state path {path} does not exist") from None
            try:
                if dir_fd is None:
                    _make_private_chain(path)
                else:
                    os.mkdir(target, 0o700, dir_fd=dir_fd)
            except FileExistsError:
                pass
            except OSError as exc:
                raise _unsafe(f"cannot create {path} ({exc.strerror})") from None
        except OSError:
            # ELOOP (symlink), ENOTDIR (file, fifo), EACCES and the rest: all refused.
            raise _unsafe(f"{path} is not a plain directory; refusing to use it") from None
    try:
        info = os.fstat(fd)
        _check_private(info, path, euid=euid, what="directory")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _make_private_chain(path: Path) -> None:
    missing: list[Path] = []
    cursor = path
    while not cursor.exists() and not cursor.is_symlink():
        missing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    for item in reversed(missing):
        try:
            os.mkdir(item, 0o700)
        except FileExistsError:
            continue
        fd = os.open(item, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)


def _check_private_file(path: Path, *, euid: int, must_exist: bool = False) -> bool:
    """Whether ``path`` exists; refuse anything but a private regular file with one link."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        if must_exist:
            raise _unsafe(f"{path} is missing") from None
        return False
    except OSError:
        raise _unsafe(f"{path} is not a plain file; refusing to use it") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _unsafe(f"{path} is not a regular file; refusing to use it")
        if info.st_nlink != 1:
            raise _unsafe(f"{path} has several hard links; refusing to use it")
        _check_private(info, path, euid=euid, what="file")
        return True
    finally:
        os.close(fd)


def _check_owner_id(owner: str) -> None:
    if not isinstance(owner, str) or not _OWNER_RE.fullmatch(owner):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError("invalid owner id")


def _check_digest(digest: str) -> None:
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError("invalid sha256")


@dataclass(frozen=True)
class StateDir:
    """A verified private state directory."""

    path: Path
    euid: int

    @property
    def db_path(self) -> Path:
        return self.path / DB_NAME

    def _owner_dir(self, owner: str, *, create: bool) -> int:
        _check_owner_id(owner)
        root_fd = _open_dir(self.path / FILES_DIR, euid=self.euid, create=create)
        try:
            return _open_dir(
                self.path / FILES_DIR / owner, euid=self.euid, create=create, dir_fd=root_fd
            )
        finally:
            os.close(root_fd)

    def put_file(self, owner: str, data: bytes) -> str:
        """Store ``data`` under the owner record's directory, named by its sha256; return it.

        Idempotent. An existing file of that name must already hold exactly these bytes.
        """
        digest = hashlib.sha256(data).hexdigest()
        dfd = self._owner_dir(owner, create=True)
        tmp = f".tmp-{uuid.uuid4().hex}"
        try:
            if self._exists(dfd, owner, digest):
                self._read(dfd, owner, digest)  # a differing file is state_unsafe
                return digest
            fd = os.open(
                tmp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=dfd,
            )
            try:
                os.fchmod(fd, 0o600)
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view) :]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(tmp, digest, src_dir_fd=dfd, dst_dir_fd=dfd)
            return digest
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dfd)
            raise
        finally:
            os.close(dfd)

    def get_file(self, owner: str, digest: str) -> bytes:
        """The bytes of a stored file; ``state_unsafe`` when missing, unsafe or its hash differs."""
        _check_digest(digest)
        dfd = self._owner_dir(owner, create=False)
        try:
            return self._read(dfd, owner, digest)
        finally:
            os.close(dfd)

    def delete_owner_files(self, owner: str) -> None:
        """Remove the owner's directory and its files (they go only with their owner)."""
        _check_owner_id(owner)
        if (
            not (self.path / FILES_DIR / owner).is_symlink()
            and not (self.path / FILES_DIR / owner).exists()
        ):
            return
        dfd = self._owner_dir(owner, create=False)
        try:
            for name in os.listdir(dfd):
                try:
                    os.unlink(name, dir_fd=dfd)
                except OSError as exc:
                    raise _unsafe(f"cannot remove stored file {name} ({exc.strerror})") from None
        finally:
            os.close(dfd)
        try:
            os.rmdir(self.path / FILES_DIR / owner)
        except OSError as exc:
            raise _unsafe(f"cannot remove stored files of {owner} ({exc.strerror})") from None

    def _exists(self, dfd: int, owner: str, digest: str) -> bool:
        try:
            os.lstat(digest, dir_fd=dfd)
        except FileNotFoundError:
            return False
        return True

    def _read(self, dfd: int, owner: str, digest: str) -> bytes:
        name = f"{self.path / FILES_DIR / owner / digest}"
        try:
            fd = os.open(
                digest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd
            )
        except FileNotFoundError:
            raise _unsafe(f"stored file {name} is missing") from None
        except OSError:
            raise _unsafe(f"stored file {name} is not a plain file") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise _unsafe(f"stored file {name} is not a regular file")
            if info.st_nlink != 1:
                raise _unsafe(f"stored file {name} has several hard links")
            _check_private(info, name, euid=self.euid, what="stored file")
            if info.st_size > MAX_STORED_FILE_BYTES:
                raise _unsafe(f"stored file {name} is larger than {MAX_STORED_FILE_BYTES} bytes")
            chunks: list[bytes] = []
            size = 0
            while chunk := os.read(fd, 65536):
                size += len(chunk)
                if size > MAX_STORED_FILE_BYTES:
                    raise _unsafe(f"stored file {name} is larger than {MAX_STORED_FILE_BYTES}")
                chunks.append(chunk)
        finally:
            os.close(fd)
        data = b"".join(chunks)
        if hashlib.sha256(data).hexdigest() != digest:
            raise _unsafe(f"stored file {name} does not match its sha256; refusing to use it")
        return data


def open_state_dir(path: Path | None = None, *, euid: int | None = None) -> StateDir:
    """Create (0700) or verify the state directory; ``state_unsafe`` when it is not private."""
    target = state_dir_path() if path is None else path
    uid = os.geteuid() if euid is None else euid
    fd = _open_dir(target, euid=uid, create=True)
    os.close(fd)
    return StateDir(target, uid)


# ---- migrations --------------------------------------------------------------------------


def _busy(exc: sqlite3.Error) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _busy_error() -> CliError:
    return CliError("internal", "the state database is busy (another command holds it)")


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection, migrations: Sequence[Sequence[str]] | None = None) -> int:
    """Apply the missing migrations in order, each in one transaction with its version bump.

    ``conn`` must be in autocommit mode (``isolation_level=None``). A step that fails rolls back
    completely and leaves the earlier steps applied. Concurrent callers are safe: the version is
    re-read under the write lock, so each step runs once. Returns the version reached.
    """
    steps = MIGRATIONS if migrations is None else tuple(migrations)
    current = _user_version(conn)
    if current > len(steps):
        raise CliError("version_skew", _skew_message(current, len(steps)))
    while current < len(steps):
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if _busy(exc):
                raise _busy_error() from None
            raise CliError("internal", f"state database error ({exc})") from None
        try:
            current = _user_version(conn)  # another process may have migrated meanwhile
            if current > len(steps):
                raise CliError("version_skew", _skew_message(current, len(steps)))
            if current < len(steps):
                for statement in steps[current]:
                    conn.execute(statement)
                current += 1
                conn.execute(f"PRAGMA user_version = {current}")
            conn.execute("COMMIT")
        except BaseException as exc:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                if _busy(exc):
                    raise _busy_error() from None
                raise CliError(
                    "internal", f"migration {current + 1} failed ({type(exc).__name__}: {exc})"
                ) from None
            raise
    return current


def _skew_message(found: int, known: int) -> str:
    return (
        f"state.db has schema version {found}, newer than this review-respond knows ({known}); "
        "read-only commands work, upgrade review-respond to run others"
    )


# ---- the store ---------------------------------------------------------------------------


class Store:
    """An open ``state.db`` connection (autocommit; use ``transaction()`` for writes)."""

    def __init__(self, conn: sqlite3.Connection, schema_version: int, *, read_only: bool) -> None:
        self.conn = conn
        self.schema_version = schema_version
        self.read_only = read_only

    @classmethod
    def open(
        cls,
        state: StateDir,
        *,
        write: bool,
        euid: int | None = None,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> Store:
        """Open (creating and migrating when needed) the state database.

        A schema older than this binary is upgraded, also for a read: migrations are additive.
        A newer schema raises ``version_skew`` when ``write`` and opens ``query_only`` otherwise.
        """
        uid = state.euid if euid is None else euid
        db = state.db_path
        sidecars = (Path(f"{db}-wal"), Path(f"{db}-shm"))
        if not _check_private_file(db, euid=uid):
            try:
                fd = os.open(
                    db,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                )
            except FileExistsError:
                pass
            except OSError as exc:
                raise _unsafe(f"cannot create {db} ({exc.strerror})") from None
            else:
                os.fchmod(fd, 0o600)
                os.close(fd)
            _check_private_file(db, euid=uid, must_exist=True)
        for sidecar in sidecars:
            _check_private_file(sidecar, euid=uid)
        try:
            conn = sqlite3.connect(db, isolation_level=None, timeout=busy_timeout_ms / 1000)
        except sqlite3.Error as exc:
            raise CliError("internal", f"cannot open {db} ({exc})") from None
        try:
            conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            conn.execute("PRAGMA foreign_keys = ON")
            found = _user_version(conn)
            if found > SCHEMA_VERSION:
                if write:
                    raise CliError("version_skew", _skew_message(found, SCHEMA_VERSION))
                conn.execute("PRAGMA query_only = ON")
                version, read_only = found, True
            else:
                _enable_wal(conn)
                version, read_only = migrate(conn, MIGRATIONS), False
            for sidecar in sidecars:
                _check_private_file(sidecar, euid=uid)
        except sqlite3.Error as exc:
            conn.close()
            if _busy(exc):
                raise _busy_error() from None
            raise CliError("internal", f"state database error ({exc})") from None
        except BaseException:
            conn.close()
            raise
        return cls(conn, version, read_only=read_only)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One write transaction (``BEGIN IMMEDIATE``): commit on success, roll back on error."""
        if self.read_only:
            raise CliError("version_skew", _skew_message(self.schema_version, SCHEMA_VERSION))
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if _busy(exc):
                raise _busy_error() from None
            raise
        try:
            yield self.conn
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                self.conn.execute("ROLLBACK")
            raise
        try:
            self.conn.execute("COMMIT")
        except sqlite3.Error:
            with contextlib.suppress(sqlite3.Error):
                self.conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Switch to WAL once. The switch needs an exclusive moment that the busy handler does not
    always cover, so it is retried briefly (bounded), and skipped when already WAL."""
    if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
        return
    deadline = time.monotonic() + 5.0
    while True:
        try:
            mode = str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
        except sqlite3.OperationalError as exc:
            if not _busy(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
            continue
        if mode != "wal":
            raise CliError("internal", f"cannot enable WAL on the state database ({mode})")
        return
