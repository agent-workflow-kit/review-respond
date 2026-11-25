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
# SQLite `application_id` of a review-respond database (ASCII "RRSP"), set by migration 1; a
# database with another non-zero id, or tables but no version, is foreign and refused.
APPLICATION_ID: Final = 0x52525350
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID",
        f"PRAGMA application_id = {APPLICATION_ID}",
    ),
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


def _check_private(
    info: os.stat_result, path: Path | str, *, euid: int, what: str, check_mode: bool = True
) -> None:
    if info.st_uid != euid:
        raise _unsafe(f"{what} {path} is owned by another user; refusing to use it")
    if check_mode and info.st_mode & 0o077:
        raise _unsafe(
            f"{what} {path} is accessible to group or others; refusing to use it (chmod go-rwx)"
        )


_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _fsync_dir(fd: int) -> None:
    # Some filesystems refuse fsync on a directory; the data fsync is the one that must hold.
    with contextlib.suppress(OSError):
        os.fsync(fd)


def _settle_new_dir(target: str | Path, dir_fd: int | None) -> None:
    """Give a directory this process just created mode 0700 whatever the umask was.

    A umask such as 0777 leaves no owner bits, so the directory cannot even be opened until it
    is chmod-ed by name (it was created a moment ago in a directory only this user can write
    for the files tree, and the final verification refuses anything that is not ours).
    """
    try:
        fd = os.open(target, _DIR_FLAGS, dir_fd=dir_fd)
    except PermissionError:
        os.chmod(target, 0o700, dir_fd=dir_fd)
        fd = os.open(target, _DIR_FLAGS, dir_fd=dir_fd)
    try:
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def _open_dir(
    path: Path,
    *,
    euid: int,
    create: bool,
    dir_fd: int | None = None,
    check_mode: bool = True,
) -> int:
    """Open a private directory without following a symlink and return the descriptor.

    A missing directory is created 0700 when ``create`` (the mode is set explicitly, not left to
    the umask). A symlink, a non-directory, a foreign owner or any group/other bit is refused
    (``check_mode=False`` skips only the group/other test); an existing directory is never
    repaired. With ``dir_fd`` only the last component of ``path`` is used, relative to it.
    """
    target: str | Path = path if dir_fd is None else path.name
    fd = -1
    created = False
    for attempt in (0, 1):
        try:
            fd = os.open(target, _DIR_FLAGS, dir_fd=dir_fd)
            break
        except FileNotFoundError:
            if not create or attempt:
                raise _unsafe(f"state path {path} does not exist") from None
            try:
                if dir_fd is None:
                    _make_private_chain(path)
                else:
                    os.mkdir(target, 0o700, dir_fd=dir_fd)
                    created = True
                    _settle_new_dir(target, dir_fd)
            except FileExistsError:
                pass
            except OSError as exc:
                raise _unsafe(f"cannot create {path} ({exc.strerror})") from None
        except OSError:
            # ELOOP (symlink), ENOTDIR (file, fifo), EACCES and the rest: all refused.
            raise _unsafe(f"{path} is not a plain directory; refusing to use it") from None
    try:
        _check_private(os.fstat(fd), path, euid=euid, what="directory", check_mode=check_mode)
        if created and dir_fd is not None:
            _fsync_dir(dir_fd)
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
        _settle_new_dir(item, None)


def _lstat_private(
    name: str, *, dir_fd: int, shown: Path, euid: int, must_exist: bool = False
) -> os.stat_result | None:
    """The ``lstat`` of ``name`` in the state directory, or None when it does not exist.

    Refuses anything but a private regular file with one link. Only ``lstat`` is used, never an
    open and close: closing any descriptor of a SQLite file drops the POSIX locks SQLite holds
    on it in this process. A FIFO is refused without opening it.
    """
    try:
        info = os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        if must_exist:
            raise _unsafe(f"{shown} is missing") from None
        return None
    except OSError:
        raise _unsafe(f"{shown} is not a plain file; refusing to use it") from None
    if not stat.S_ISREG(info.st_mode):
        raise _unsafe(f"{shown} is not a regular file; refusing to use it")
    if info.st_nlink != 1:
        raise _unsafe(f"{shown} has several hard links; refusing to use it")
    _check_private(info, shown, euid=euid, what="file")
    return info


def _stored_owner(owner: str) -> None:
    try:
        _check_owner_id(owner)
    except ValueError:
        raise _unsafe("a stored record has an invalid owner id") from None


def _stored_digest(digest: str) -> None:
    try:
        _check_digest(digest)
    except ValueError:
        raise _unsafe("a stored record has an invalid file sha256") from None


def _check_owner_id(owner: str) -> None:
    if not isinstance(owner, str) or not _OWNER_RE.fullmatch(owner):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError("invalid owner id")


def _check_digest(digest: str) -> None:
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError("invalid sha256")


class StateDir:
    """A verified private state directory, held open by descriptor.

    Stored files are handled relative to the descriptor, so replacing the directory by a symlink
    after it was verified cannot redirect them. ``close()`` releases it (also done when the
    object is collected).
    """

    def __init__(self, path: Path, euid: int, fd: int) -> None:
        self.path = path
        self.euid = euid
        self.fd = fd

    def __repr__(self) -> str:
        return f"StateDir({str(self.path)!r})"

    @property
    def db_path(self) -> Path:
        return self.path / DB_NAME

    def close(self) -> None:
        fd, self.fd = self.fd, -1
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)

    def __del__(self) -> None:
        self.close()

    def verify_unchanged(self) -> None:
        """``state_unsafe`` unless the path still names the directory this object holds."""
        try:
            held = os.fstat(self.fd)
            now = os.stat(self.path)
        except OSError:
            raise _unsafe(f"state directory {self.path} changed while in use") from None
        if (held.st_dev, held.st_ino) != (now.st_dev, now.st_ino):
            raise _unsafe(f"state directory {self.path} changed while in use; refusing to use it")

    def _owner_dir(self, owner: str, *, create: bool, check_mode: bool = True) -> int:
        root_fd = _open_dir(self.path / FILES_DIR, euid=self.euid, create=create, dir_fd=self.fd)
        try:
            return _open_dir(
                self.path / FILES_DIR / owner,
                euid=self.euid,
                create=create,
                dir_fd=root_fd,
                check_mode=check_mode,
            )
        finally:
            os.close(root_fd)

    def put_file(self, owner: str, data: bytes) -> str:
        """Store ``data`` under the owner record's directory, named by its sha256; return it.

        Idempotent. An existing file of that name must already hold exactly these bytes. Data
        over ``MAX_STORED_FILE_BYTES`` is refused before anything is written.
        """
        _check_owner_id(owner)
        if len(data) > MAX_STORED_FILE_BYTES:
            raise CliError(
                "internal",
                f"refusing to store {len(data)} bytes: the limit is {MAX_STORED_FILE_BYTES}",
            )
        digest = hashlib.sha256(data).hexdigest()
        dfd = self._owner_dir(owner, create=True)
        tmp = f".tmp-{uuid.uuid4().hex}"
        try:
            if self._exists(dfd, digest):
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
            _fsync_dir(dfd)
            return digest
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dfd)
            raise
        finally:
            os.close(dfd)

    def get_file(self, owner: str, digest: str) -> bytes:
        """The bytes of a stored file; ``state_unsafe`` when missing, unsafe or its hash differs.

        The owner and digest come from stored records, so a malformed one is ``state_unsafe``.
        """
        _stored_owner(owner)
        _stored_digest(digest)
        dfd = self._owner_dir(owner, create=False)
        try:
            return self._read(dfd, owner, digest)
        finally:
            os.close(dfd)

    def delete_owner_files(self, owner: str) -> None:
        """Remove the owner's directory and its files (they go only with their owner).

        Every entry is checked first (regular file, one link, no subdirectory); nothing is removed
        when one is not. The directory itself must be a real directory of this user, but its mode
        is not required to be private, so a loosened one can still be cleaned up.
        """
        _stored_owner(owner)
        try:
            os.lstat(FILES_DIR, dir_fd=self.fd)
        except FileNotFoundError:
            return
        root_fd = _open_dir(self.path / FILES_DIR, euid=self.euid, create=False, dir_fd=self.fd)
        try:
            try:
                os.lstat(owner, dir_fd=root_fd)
            except FileNotFoundError:
                return
            dfd = _open_dir(
                self.path / FILES_DIR / owner,
                euid=self.euid,
                create=False,
                dir_fd=root_fd,
                check_mode=False,
            )
            try:
                names = os.listdir(dfd)
                for name in names:
                    info = os.lstat(name, dir_fd=dfd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise _unsafe(
                            f"{self.path / FILES_DIR / owner / name} is not a plain stored file; "
                            "refusing to remove the directory"
                        )
                for name in names:
                    try:
                        os.unlink(name, dir_fd=dfd)
                    except OSError as exc:
                        raise _unsafe(
                            f"cannot remove stored file {name} ({exc.strerror})"
                        ) from None
            finally:
                os.close(dfd)
            try:
                os.rmdir(owner, dir_fd=root_fd)
            except OSError as exc:
                raise _unsafe(f"cannot remove stored files of {owner} ({exc.strerror})") from None
            _fsync_dir(root_fd)
        finally:
            os.close(root_fd)

    def _exists(self, dfd: int, digest: str) -> bool:
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


def open_state_dir(
    path: Path | None = None, *, euid: int | None = None, create: bool = True
) -> StateDir:
    """Create (0700, unless ``create`` is false) or verify the state directory.

    ``state_unsafe`` when it is not private. The returned object holds the verified directory open.
    """
    target = state_dir_path() if path is None else path
    uid = os.geteuid() if euid is None else euid
    return StateDir(target, uid, _open_dir(target, euid=uid, create=create))


# ---- migrations --------------------------------------------------------------------------


def _busy(exc: sqlite3.Error) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _busy_error() -> CliError:
    return CliError("internal", "the state database is busy (another command holds it)")


def _arm(conn: sqlite3.Connection, deadline: float | None) -> None:
    """Make the next statement wait at most until ``deadline`` (one budget for a whole open)."""
    if deadline is None:
        return
    remaining = int((deadline - time.monotonic()) * 1000)
    if remaining <= 0:
        raise _busy_error()
    conn.execute(f"PRAGMA busy_timeout = {remaining}")


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(
    conn: sqlite3.Connection,
    migrations: Sequence[Sequence[str]] | None = None,
    *,
    deadline: float | None = None,
) -> int:
    """Apply the missing migrations in order, each in one transaction with its version bump.

    ``conn`` must be in autocommit mode (``isolation_level=None``). A step that fails rolls back
    completely and leaves the earlier steps applied. Concurrent callers are safe: the version is
    re-read under the write lock, so each step runs once. ``deadline`` (a ``time.monotonic``
    value) bounds the total wait for locks. Returns the version reached.
    """
    steps = MIGRATIONS if migrations is None else tuple(migrations)
    _arm(conn, deadline)
    current = _user_version(conn)
    if current > len(steps):
        raise CliError("version_skew", _skew_message(current, len(steps)))
    while current < len(steps):
        _arm(conn, deadline)
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


def _check_ours(conn: sqlite3.Connection, db: Path) -> int:
    """The schema version of a database that is ours or empty; a foreign one is refused.

    Read in one snapshot: a concurrent first open may migrate between two separate reads.
    """
    conn.execute("BEGIN")
    try:
        found = _user_version(conn)
        application_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
        tables = int(
            conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
        )
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
    if found < 0 or application_id not in (0, APPLICATION_ID):
        raise _unsafe(f"{db} is not a review-respond state database; refusing to use it")
    if found == 0 and tables:
        raise _unsafe(f"{db} is a database of another program; refusing to adopt it")
    return found


# ---- the store ---------------------------------------------------------------------------


def _db_identity(state: StateDir, uid: int, *, must_exist: bool = False) -> tuple[int, int] | None:
    info = _lstat_private(
        DB_NAME, dir_fd=state.fd, shown=state.db_path, euid=uid, must_exist=must_exist
    )
    for sidecar in (f"{DB_NAME}-wal", f"{DB_NAME}-shm"):
        _lstat_private(sidecar, dir_fd=state.fd, shown=state.path / sidecar, euid=uid)
    return None if info is None else (info.st_dev, info.st_ino)


def _create_db(state: StateDir, uid: int) -> tuple[int, int]:
    try:
        fd = os.open(
            DB_NAME,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=state.fd,
        )
    except FileExistsError:
        pass
    except OSError as exc:
        raise _unsafe(f"cannot create {state.db_path} ({exc.strerror})") from None
    else:
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
    identity = _db_identity(state, uid, must_exist=True)
    assert identity is not None
    return identity


def _reverify(state: StateDir, uid: int, identity: tuple[int, int]) -> None:
    """After connecting: still the same directory, still the same private database file."""
    state.verify_unchanged()
    if _db_identity(state, uid, must_exist=True) != identity:
        raise _unsafe(f"{state.db_path} was replaced while opening it; refusing to use it")


def _database_error(exc: sqlite3.Error, db: Path) -> CliError:
    if _busy(exc):
        return _busy_error()
    text = str(exc).lower()
    if "not a database" in text or "malformed" in text:
        return _unsafe(
            f"{db} is not a readable SQLite database ({exc}); move it aside to start fresh"
        )
    return CliError("internal", f"state database error ({exc})")


class Store:
    """An open ``state.db`` connection (autocommit; use ``transaction()`` for writes)."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        schema_version: int,
        *,
        read_only: bool,
        state: StateDir | None = None,
    ) -> None:
        self.conn = conn
        self.schema_version = schema_version
        self.read_only = read_only
        self._state = state

    @classmethod
    def open(
        cls,
        state: StateDir,
        *,
        write: bool,
        euid: int | None = None,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        close_state: bool = False,
    ) -> Store:
        """Open (creating and migrating when needed) the state database.

        A schema older than this binary is upgraded, also for a read: migrations are additive.
        A newer schema raises ``version_skew`` when ``write`` and opens ``query_only`` otherwise.
        ``close_state`` makes ``close()`` also close ``state``. The whole open waits at most
        ``busy_timeout_ms`` for locks.
        """
        uid = state.euid if euid is None else euid
        try:
            opened = cls._open(state, uid, write, busy_timeout_ms, create=True)
        except BaseException:
            if close_state:
                state.close()
            raise
        assert opened is not None
        opened._state = state if close_state else None
        return opened

    @classmethod
    def open_read_only(
        cls,
        state: StateDir,
        *,
        euid: int | None = None,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        close_state: bool = False,
    ) -> Store | None:
        """Open an existing ``state.db`` as it is: never created, never migrated, never written.

        Returns None when there is no database. Used by ``--dry-run``.
        """
        uid = state.euid if euid is None else euid
        try:
            opened = cls._open(state, uid, False, busy_timeout_ms, create=False)
        except BaseException:
            if close_state:
                state.close()
            raise
        if opened is not None and close_state:
            opened._state = state
        elif close_state:
            state.close()
        return opened

    @classmethod
    def _open(
        cls, state: StateDir, uid: int, write: bool, busy_timeout_ms: int, *, create: bool
    ) -> Store | None:
        db = state.db_path
        state.verify_unchanged()
        identity = _db_identity(state, uid)
        if identity is None:
            if not create:
                return None
            identity = _create_db(state, uid)
        deadline = time.monotonic() + busy_timeout_ms / 1000
        try:
            conn = sqlite3.connect(db, isolation_level=None, timeout=busy_timeout_ms / 1000)
        except sqlite3.Error as exc:
            raise CliError("internal", f"cannot open {db} ({exc})") from None
        try:
            _reverify(state, uid, identity)
            _arm(conn, deadline)
            conn.execute("PRAGMA foreign_keys = ON")
            found = _check_ours(conn, db)
            if not create:
                conn.execute("PRAGMA query_only = ON")
                version, read_only = found, True
            elif found > SCHEMA_VERSION:
                if write:
                    raise CliError("version_skew", _skew_message(found, SCHEMA_VERSION))
                conn.execute("PRAGMA query_only = ON")
                version, read_only = found, True
            else:
                _enable_wal(conn, deadline)
                try:
                    version, read_only = migrate(conn, MIGRATIONS, deadline=deadline), False
                except CliError as exc:
                    # The database became newer between the check and the migration (another
                    # binary upgraded it): a read goes on read-only, a write refuses.
                    if exc.code != "version_skew" or write:
                        raise
                    conn.execute("PRAGMA query_only = ON")
                    version, read_only = _user_version(conn), True
            _reverify(state, uid, identity)
            conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        except sqlite3.Error as exc:
            conn.close()
            raise _database_error(exc, db) from None
        except BaseException:
            conn.close()
            raise
        return cls(conn, version, read_only=read_only)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One write transaction (``BEGIN IMMEDIATE``): commit on success, roll back on error.

        The schema version is re-read under the write lock: a binary that upgraded the database
        after this connection opened makes the write ``version_skew``.
        """
        if self.read_only:
            raise CliError("version_skew", _skew_message(self.schema_version, SCHEMA_VERSION))
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if _busy(exc):
                raise _busy_error() from None
            raise
        try:
            found = _user_version(self.conn)
            if found > SCHEMA_VERSION:
                raise CliError("version_skew", _skew_message(found, SCHEMA_VERSION))
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
        if self._state is not None:
            self._state.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _enable_wal(conn: sqlite3.Connection, deadline: float) -> None:
    """Switch to WAL once. The switch needs an exclusive moment that the busy handler does not
    always cover, so it is retried briefly (until ``deadline``), and skipped when already WAL."""
    _arm(conn, deadline)
    if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
        return
    while True:
        _arm(conn, deadline)  # raises the busy error once the deadline has passed
        try:
            mode = str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
        except sqlite3.OperationalError as exc:
            if not _busy(exc):
                raise
            time.sleep(0.01)
            continue
        if mode != "wal":
            raise CliError("internal", f"cannot enable WAL on the state database ({mode})")
        return
