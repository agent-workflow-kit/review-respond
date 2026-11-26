"""The private state directory, ``state.db`` (SQLite, WAL, additive migrations) and stored files.

docs/interface.md: Files and state, Upgrades. Every command opens the state directory first and
refuses (exit 12 ``state_unsafe``) when it is not private. A schema newer than this binary knows
is readable and not writable (exit 12 ``version_skew``).

Leases and compare-and-set transitions live at the end of this module (docs/interface.md:
Concurrency, decision D-19). The audit and log writers live in audit.py, and the tables of each
feature to the issue that needs them; this module gives them the connection, a transaction helper,
the migration list to append to, and the lease-checked compare-and-set writes.
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
import uuid
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Final, Literal

from .errors import CliError
from .gateway import Clock, SystemClock

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
    (
        # One row per lease key (`pass`, `pr:<digest>`, `round:<id>`). `token` names one
        # acquisition (two holders in one process, or a pid taken over, never share it); the pid,
        # its start time and the boot id are the holder's identity for the takeover rule.
        """CREATE TABLE leases (
            key TEXT PRIMARY KEY,
            token TEXT NOT NULL,
            pid INTEGER NOT NULL,
            pid_start TEXT NOT NULL,
            boot_id TEXT NOT NULL,
            acquired_at REAL NOT NULL,
            renewed_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            rev INTEGER NOT NULL DEFAULT 0
        ) WITHOUT ROWID""",
    ),
    (
        # One row per PR seen in a listing (#13). `host_state` is the host's fact (open, closed,
        # merged), never the derived PR state of `list`. Times are epoch seconds; `created_at` and
        # `updated_at` are the host's RFC 3339 text. `comment_count` is the number of comments
        # the last good read returned (the budget estimate). Later issues add columns (`block`,
        # `base_agent`, ...) with ALTER TABLE in their own migration.
        """CREATE TABLE prs (
            pr TEXT PRIMARY KEY,
            digest TEXT NOT NULL UNIQUE,
            host TEXT NOT NULL,
            repo TEXT NOT NULL,
            number INTEGER NOT NULL,
            host_state TEXT NOT NULL CHECK (host_state IN ('open', 'closed', 'merged')),
            head_sha TEXT NOT NULL,
            head_ref TEXT NOT NULL,
            head_repo TEXT,
            is_fork INTEGER NOT NULL CHECK (is_fork IN (0, 1)),
            base_ref TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            first_seen_at REAL NOT NULL DEFAULT 0,
            listed_at REAL NOT NULL DEFAULT 0,
            last_comments_read_at REAL,
            last_comments_attempt_at REAL,
            last_show_attempt_at REAL,
            read_failures INTEGER NOT NULL DEFAULT 0,
            comment_count INTEGER NOT NULL DEFAULT 0,
            rev INTEGER NOT NULL DEFAULT 0
        ) WITHOUT ROWID""",
        "CREATE INDEX prs_by_host ON prs (host, host_state)",
    ),
    (
        # The access cache (#14): one `forgectl access` answer per (repo, author id), kept for
        # `access_cache_ttl`. `write_access` NULL is the unknown answer (cached too); a failed
        # lookup is never stored. `login` is the login the answer was asked for: a row is used
        # only for a comment carrying that login. Times are epoch seconds.
        """CREATE TABLE access_cache (
            repo TEXT NOT NULL,
            author_id TEXT NOT NULL,
            login TEXT NOT NULL,
            write_access INTEGER CHECK (write_access IN (0, 1)),
            fetched_at REAL NOT NULL,
            PRIMARY KEY (repo, author_id)
        ) WITHOUT ROWID""",
    ),
    (
        # The handled set (#15): one row per (PR, comment id). `body_sha256` is the hash of the
        # whole current body; `body_file` names the stored (bounded) text under the PR's
        # directory and `body_bytes` is the full size. `first_seen_at` is the local time the
        # comment was first stored, `body_seen_at` the local time its current hash was first
        # stored (the settle timestamp). Times are epoch seconds. The CHECKs keep a `delivered`
        # row complete and the closed value sets closed. `pr` is an enforced foreign key
        # (PRAGMA foreign_keys = ON) without a cascade: deleting a PR must first delete its
        # comment rows and then call `StateDir.delete_owner_files` for the PR's digest.
        """CREATE TABLE comments (
            pr TEXT NOT NULL REFERENCES prs (pr),
            comment_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            author_id TEXT,
            author_login TEXT,
            created_at TEXT NOT NULL,
            host_updated_at TEXT NOT NULL,
            body_sha256 TEXT NOT NULL,
            body_file TEXT,
            body_bytes INTEGER NOT NULL,
            first_seen_at REAL NOT NULL,
            body_seen_at REAL NOT NULL,
            state TEXT NOT NULL
                CHECK (state IN ('seen', 'unhandled', 'delivered', 'handled', 'deleted')),
            round TEXT,
            delivered_sha256 TEXT,
            delivered_from TEXT CHECK (delivered_from IN ('seen', 'unhandled')),
            handled_sha256 TEXT,
            handled_reason TEXT CHECK (handled_reason IN ('resolved')),
            edited INTEGER NOT NULL DEFAULT 0 CHECK (edited IN (0, 1)),
            eligible INTEGER NOT NULL CHECK (eligible IN (0, 1)),
            reason TEXT,
            thread_resolved INTEGER NOT NULL CHECK (thread_resolved IN (0, 1)),
            outdated INTEGER CHECK (outdated IN (0, 1)),
            path TEXT,
            line INTEGER,
            reply_to TEXT,
            rev INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (pr, comment_id),
            CHECK (state != 'delivered'
                   OR (round IS NOT NULL AND delivered_sha256 IS NOT NULL
                       AND delivered_from IS NOT NULL)),
            CHECK (state != 'handled' OR handled_sha256 IS NOT NULL)
        ) WITHOUT ROWID""",
        "CREATE INDEX comments_by_state ON comments (pr, state)",
    ),
    (
        # The due rule (#16). `block` is null or JSON `{reason, set_at (epoch seconds),
        # comment_set_sha256}` with one of the seven documented reasons; only `cas_update` writes
        # it. `base_agent` is the resolved base agent id (null: none found). `records_current`
        # is 0 from the moment a complete comment read is stored until its comment records are
        # applied (same transaction as the records), so `last_comments_read_at` is never taken as
        # proof that the records are current.
        """ALTER TABLE prs ADD COLUMN block TEXT CHECK (block IS NULL OR CASE WHEN json_valid(block)
            THEN coalesce(json_extract(block, '$.reason') IN ('escalated', 'failed', 'stopped',
                'round_limit', 'no_agent', 'hook_vetoed', 'hook_escalated'), 0)
                AND coalesce(json_type(block, '$.set_at') IN ('integer', 'real'), 0)
                AND coalesce(json_type(block, '$.comment_set_sha256') = 'text', 0)
                AND json_remove(block, '$.reason', '$.set_at', '$.comment_set_sha256') = '{}'
            ELSE 0 END)""",
        "ALTER TABLE prs ADD COLUMN base_agent TEXT",
        "ALTER TABLE prs ADD COLUMN records_current INTEGER NOT NULL DEFAULT 0"
        " CHECK (records_current IN (0, 1))",
        # `comments.unhandled_since` is the local time the comment last became `unhandled` from
        # another state (the due rule's one-hour ceiling counts from it; an edit of an unhandled
        # comment never moves it). Null for rows stored before it existed: read as first_seen_at.
        "ALTER TABLE comments ADD COLUMN unhandled_since REAL",
    ),
    (
        # The rounds (#18). `trigger_kind` is the interface's `trigger` (TRIGGER is an SQL word).
        # `comment_ids` is the JSON list of the comments the job delivered; `counted` is 1 once
        # the run started (Counting) and never goes back. A terminal row always has `ended_at`; a
        # failed one its `failure_reason`; a canceled one its `cancel_reason`. Times are epoch
        # seconds. At most one round of a PR is queued or running, and numbers never repeat.
        """CREATE TABLE rounds (
            id TEXT PRIMARY KEY CHECK (length(id) = 11 AND id GLOB
                'rd-[a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7]'),
            pr TEXT NOT NULL REFERENCES prs (pr),
            number INTEGER NOT NULL CHECK (number >= 1),
            state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'pending', 'confirmed',
                'rejected', 'expired', 'closed', 'escalated', 'failed', 'canceled')),
            rev INTEGER NOT NULL DEFAULT 0,
            trigger_kind TEXT NOT NULL CHECK (trigger_kind IN ('check', 'respond')),
            forced INTEGER NOT NULL DEFAULT 0 CHECK (forced IN (0, 1)),
            include_existing INTEGER NOT NULL DEFAULT 0 CHECK (include_existing IN (0, 1)),
            note_sha256 TEXT,
            base_agent_id TEXT,
            job_id TEXT,
            detached_started_at REAL,
            agent_id TEXT,
            throwaway_id TEXT,
            head_sha TEXT,
            comment_ids TEXT NOT NULL DEFAULT '[]',
            counted INTEGER NOT NULL DEFAULT 0 CHECK (counted IN (0, 1)),
            cancel_requested_at REAL,
            cancel_reason TEXT CHECK (cancel_reason IN ('stopped', 'pr_closed',
                'nothing_to_respond', 'read_failed')),
            failure_reason TEXT CHECK (failure_reason IN ('throwaway_failed', 'launch_failed',
                'vetoed', 'refused', 'agent_gone', 'unreported', 'agent_failed', 'agent_lost',
                'timeout', 'job_terminated', 'job_lost', 'artifact_missing', 'artifact_invalid',
                'implicit_done', 'empty_result', 'diff_failed', 'internal')),
            failure_detail TEXT CHECK (failure_detail IN ('frozen_file', 'json', 'schema',
                'skill_marker', 'replies', 'paths', 'commit_message', 'path_state')),
            escalation_reason TEXT,
            escalation_summary TEXT,
            escalation_cause TEXT CHECK (escalation_cause IN ('hook')),
            warnings TEXT NOT NULL DEFAULT '[]',
            created_at REAL NOT NULL,
            started_at REAL,
            ended_at REAL,
            UNIQUE (pr, number),
            CHECK (state NOT IN ('confirmed', 'rejected', 'expired', 'closed', 'escalated',
                'failed', 'canceled') OR ended_at IS NOT NULL),
            CHECK (state != 'failed' OR failure_reason IS NOT NULL),
            CHECK (state != 'canceled' OR cancel_reason IS NOT NULL)
        ) WITHOUT ROWID""",
        "CREATE UNIQUE INDEX rounds_one_active ON rounds (pr) WHERE state IN ('queued', 'running')",
        # The state table's rules, kept by the database as well as by rounds.py: a terminal round
        # is immutable, `counted` never goes back, and a `pending` round only follows its record.
        """CREATE TRIGGER rounds_terminal_is_immutable BEFORE UPDATE ON rounds
        WHEN OLD.state IN ('confirmed', 'rejected', 'expired', 'closed', 'escalated', 'failed',
            'canceled')
        BEGIN SELECT RAISE(ABORT, 'a terminal round is immutable'); END""",
        """CREATE TRIGGER rounds_counted_never_decreases BEFORE UPDATE ON rounds
        WHEN NEW.counted < OLD.counted
        BEGIN SELECT RAISE(ABORT, 'a counted round stays counted'); END""",
        """CREATE TRIGGER rounds_pending_follows_its_record BEFORE UPDATE ON rounds
        WHEN OLD.state = 'pending' AND NEW.state != OLD.state
            AND NEW.state NOT IN ('confirmed', 'rejected', 'expired', 'closed')
        BEGIN SELECT RAISE(ABORT, 'a pending round is settled by its record'); END""",
    ),
    (
        # Fork lineage (#19). `forks` holds every agent id review-respond created (a round's
        # fork or fresh session) and is the only set it ever acts on: the original agent has no
        # row. `state`: round (a round's agent, not the base), base (the PR's base, at most one
        # per PR), teardown_pending, torn_down; states only move forward. `source` is the base
        # agent the fork was made from (null: a fresh session). `failed_passes` counts teardown
        # passes that did not end in a tombstone; `refused_at` is set when the third one is
        # reported (post-attention teardown_refused fires once). `prs.agent_busy` is the last
        # observation of the base agent (1 also when it could not be read): the due rule's
        # `agent_busy`. Times are epoch seconds.
        """CREATE TABLE forks (
            agent_id TEXT PRIMARY KEY CHECK (length(agent_id) = 11 AND agent_id GLOB
                'ag-[a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7]'),
            pr TEXT NOT NULL REFERENCES prs (pr),
            round TEXT NOT NULL UNIQUE REFERENCES rounds (id),
            source TEXT CHECK (source IS NULL OR (length(source) = 11 AND source GLOB
                'ag-[a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7]')),
            state TEXT NOT NULL CHECK (state IN ('round', 'base', 'teardown_pending',
                'torn_down')),
            failed_passes INTEGER NOT NULL DEFAULT 0 CHECK (failed_passes >= 0),
            refused_at REAL,
            rev INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        ) WITHOUT ROWID""",
        "CREATE UNIQUE INDEX forks_one_base ON forks (pr) WHERE state = 'base'",
        "CREATE INDEX forks_by_state ON forks (state, pr)",
        """CREATE TRIGGER forks_forward_only BEFORE UPDATE ON forks
        WHEN NEW.state != OLD.state AND NOT (
            (OLD.state = 'round' AND NEW.state IN ('base', 'teardown_pending', 'torn_down'))
            OR (OLD.state = 'base' AND NEW.state IN ('teardown_pending', 'torn_down'))
            OR (OLD.state = 'teardown_pending' AND NEW.state = 'torn_down'))
        BEGIN SELECT RAISE(ABORT, 'a fork state only moves forward'); END""",
        "ALTER TABLE prs ADD COLUMN agent_busy INTEGER NOT NULL DEFAULT 0"
        " CHECK (agent_busy IN (0, 1))",
        # A database from before this migration may hold rounds that already forked: each such
        # agent becomes a row in state `round` (`source` is the round's base agent when it is a
        # well-formed id, else null). An agent that is a PR's base, or a repeated id, is skipped.
        """INSERT OR IGNORE INTO forks (agent_id, pr, round, source, state, created_at, updated_at)
        SELECT r.agent_id, r.pr, r.id,
            CASE WHEN length(r.base_agent_id) = 11 AND r.base_agent_id GLOB
                'ag-[a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7]'
                AND r.base_agent_id != r.agent_id THEN r.base_agent_id END,
            'round', r.created_at, r.created_at
        FROM rounds r
        WHERE r.agent_id IS NOT NULL AND length(r.agent_id) = 11 AND r.agent_id GLOB
            'ag-[a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7]'
            AND NOT EXISTS (SELECT 1 FROM prs p WHERE p.base_agent = r.agent_id)""",
        # What a fork row says about its origin never changes.
        """CREATE TRIGGER forks_origin_is_immutable BEFORE UPDATE ON forks
        WHEN NEW.agent_id != OLD.agent_id OR NEW.pr != OLD.pr OR NEW.round != OLD.round
            OR NEW.source IS NOT OLD.source
        BEGIN SELECT RAISE(ABORT, 'a fork keeps its agent, PR, round and source'); END""",
    ),
)
SCHEMA_VERSION: int = len(MIGRATIONS)

HEARTBEAT_SECONDS: Final = 60
LEASE_TTL_SECONDS: Final = 300
BUSY_WAIT_SECONDS: Final = 30  # respond, stop, confirm, reject, edit, drop-reply: then exit 20
JOB_PR_WAIT_SECONDS: Final = 60  # _job on a pr: lease: then exit 1 lease_timeout
POLL_SECONDS: Final = 0.05

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
        try:
            os.chmod(target, 0o700, dir_fd=dir_fd, follow_symlinks=False)
        except NotImplementedError:
            raise _unsafe(
                f"cannot chmod {target} without following symlinks on this platform"
            ) from None
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


_DIGEST_RE: Final = re.compile(r"[0-9a-f]{64}")


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
                # Refresh the mtime so a reused orphan is not pruned (grace period) before the
                # caller's rows reference it. Never follows a symlink.
                try:
                    os.utime(digest, dir_fd=dfd, follow_symlinks=False)
                    return digest
                except FileNotFoundError:
                    pass  # pruned meanwhile: write it again below
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

    def write_private_file(self, subdir: str, name: str, data: bytes) -> Path:
        """Create ``<state>/<subdir>/<name>`` (directory 0700, file 0600 set explicitly) and
        return its path. Never follows a symlink and never replaces an existing file (the
        ``FileExistsError`` is the caller's). ``subdir`` and ``name`` are single plain names."""
        for part in (subdir, name):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", part):
                raise ValueError("not a plain file name")
        dfd = _open_dir(self.path / subdir, euid=self.euid, create=True, dir_fd=self.fd)
        try:
            fd = os.open(
                name,
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
            except BaseException:
                # A half-written file would make a retry fail with FileExistsError.
                with contextlib.suppress(OSError):
                    os.unlink(name, dir_fd=dfd)
                raise
            finally:
                os.close(fd)
            _fsync_dir(dfd)
        finally:
            os.close(dfd)
        return self.path / subdir / name

    def open_private_log(self, subdir: str, name: str) -> int:
        """Open (creating it 0600) ``<state>/<subdir>/<name>`` for appending and return the
        descriptor, which the caller closes. For the output of a detached job. Never follows a
        symlink; an existing file must be a regular, one-link file of this user (a restart of the
        same round appends to it). ``subdir`` and ``name`` are single plain names."""
        for part in (subdir, name):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", part):
                raise ValueError("not a plain file name")
        dfd = _open_dir(self.path / subdir, euid=self.euid, create=True, dir_fd=self.fd)
        try:
            fd = os.open(
                name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_APPEND
                | os.O_NOFOLLOW
                | os.O_NONBLOCK  # a planted FIFO must fail (ENXIO), never block
                | os.O_CLOEXEC,
                0o600,
                dir_fd=dfd,
            )
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != self.euid:
                    raise _unsafe(f"{self.path / subdir / name} is not a plain file of this user")
                os.fchmod(fd, 0o600)
            except BaseException:
                os.close(fd)
                raise
            return fd
        except OSError:
            raise _unsafe(f"cannot open {self.path / subdir / name}") from None
        finally:
            os.close(dfd)

    def remove_private_file(self, subdir: str, name: str) -> bool:
        """Delete ``<state>/<subdir>/<name>``; ``False`` when it or the directory is missing."""
        for part in (subdir, name):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", part):
                raise ValueError("not a plain file name")
        try:
            dfd = _open_dir(self.path / subdir, euid=self.euid, create=False, dir_fd=self.fd)
        except CliError:
            return False
        try:
            os.unlink(name, dir_fd=dfd)
            return True
        except FileNotFoundError:
            return False
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

    def prune_owner_files(
        self, owner: str, keep: Collection[str], *, min_age: float, now: float | None = None
    ) -> int:
        """Remove the owner's stored files that are not in ``keep`` and are older than ``min_age``
        seconds (by modification time), stale ``.tmp-*`` leftovers included. Returns how many.

        Only plain one-link files named by a sha256 or ``.tmp-*`` are considered; anything else
        stays. The owner directory itself is checked like any stored directory.
        """
        _stored_owner(owner)
        try:
            os.lstat(FILES_DIR, dir_fd=self.fd)
        except FileNotFoundError:
            return 0
        root_fd = _open_dir(self.path / FILES_DIR, euid=self.euid, create=False, dir_fd=self.fd)
        try:
            try:
                os.lstat(owner, dir_fd=root_fd)
            except FileNotFoundError:
                return 0
            dfd = _open_dir(
                self.path / FILES_DIR / owner, euid=self.euid, create=False, dir_fd=root_fd
            )
        finally:
            os.close(root_fd)
        current = time.time() if now is None else now
        removed = 0
        try:
            for name in os.listdir(dfd):
                if name in keep or not (name.startswith(".tmp-") or _DIGEST_RE.fullmatch(name)):
                    continue
                try:
                    info = os.lstat(name, dir_fd=dfd)
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_nlink != 1
                        or current - info.st_mtime < min_age
                    ):
                        continue
                    os.unlink(name, dir_fd=dfd)
                    removed += 1
                except FileNotFoundError:
                    continue
        finally:
            os.close(dfd)
        return removed

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


class _BusyError(CliError):
    """The state database is busy; a marker type so callers never compare message text."""


def _busy_error() -> CliError:
    return _BusyError("internal", "the state database is busy (another command holds it)")


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
        source: StateDir | None = None,
    ) -> None:
        self.conn = conn
        self.schema_version = schema_version
        self.read_only = read_only
        self._state = state
        self.source = source  # the state directory (not owned): the heartbeat reopens from it
        self.clock: Clock = SystemClock()  # lease times and waits; tests substitute a manual one

    @property
    def state_path(self) -> Path:
        if self.source is None:
            raise CliError("internal", "this store was not opened from a state directory")
        return self.source.path

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
        return cls(conn, version, read_only=read_only, source=state)

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

    def try_acquire(
        self,
        key: str,
        *,
        heartbeat: float | None = HEARTBEAT_SECONDS,
        on_event: EventCallback | None = None,
    ) -> Lease | None:
        """Take a lease without waiting; None when a live holder has it (see ``_try_acquire``)."""
        return _try_acquire(self, key, heartbeat=heartbeat, on_event=on_event)

    def acquire(
        self,
        key: str,
        *,
        wait: float,
        contended: Literal["busy", "lease_timeout"],
        heartbeat: float | None = HEARTBEAT_SECONDS,
        on_event: EventCallback | None = None,
    ) -> Lease:
        """Take a lease, waiting up to ``wait`` seconds (see ``_acquire``)."""
        return _acquire(
            self, key, wait=wait, contended=contended, heartbeat=heartbeat, on_event=on_event
        )

    def read_lease(self, key: str) -> LeaseRow | None:
        _check_key(key)
        return _read_lease(self.conn, key)

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


# ---- process identity --------------------------------------------------------------------

_PROC: Final = Path("/proc")


def _stat_fields(pid: int) -> list[str] | None:
    """The fields of ``/proc/<pid>/stat`` after the command name (state first), or None."""
    try:
        raw = (_PROC / str(pid) / "stat").read_bytes()
    except (OSError, ValueError):
        return None
    # The name may hold any bytes (even a ")"): split at the last ")" and never fail to decode.
    _, sep, rest = raw.decode("utf-8", errors="replace").rpartition(")")
    return rest.split() if sep else None


def process_state(pid: int) -> str | None:
    """The one-letter state of a process (``Z`` zombie), or None when it does not exist."""
    fields = _stat_fields(pid)
    return None if not fields else fields[0]


def process_start(pid: int) -> str | None:
    """The start time of a process in clock ticks since boot (stat field 22), or None."""
    fields = _stat_fields(pid)
    return None if not fields or len(fields) < 20 else fields[19]


def boot_id() -> str:
    """The kernel's boot id; empty where the platform has none."""
    try:
        return (_PROC / "sys" / "kernel" / "random" / "boot_id").read_text().strip()
    except OSError:
        return ""


def holder_gone(pid: int, pid_start: str, holder_boot_id: str) -> bool:
    """True only when the holder is verified gone: another boot, no such process, a zombie (it can
    never run again) or a process with another start time (the pid was reused).

    The caller holds the database write lock, so no other command can take the row meanwhile.
    Without ``/proc`` only the pid is probed (``kill(pid, 0)``): a gap, a live pid is never
    called gone.
    """
    if holder_boot_id != boot_id() or pid <= 0:
        return True
    fields = _stat_fields(pid) if (_PROC / "self" / "stat").exists() else None
    if fields is not None and len(fields) >= 20:
        return fields[0] in ("Z", "X") or fields[19] != pid_start
    # No /proc, or its stat for this pid is missing or unparseable: only an absent pid is gone.
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return True
    except PermissionError:
        return False
    return False


# ---- leases ------------------------------------------------------------------------------


_PASS_KEY: Final = "pass"
_PR_DIGEST_RE: Final = re.compile(r"[0-9a-f]{20}")
_KEY_RE: Final = re.compile(r"pass|pr:[0-9a-f]{20}|round:[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_IDENT_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")

EventCallback = Callable[[str, str], None]
# Pass `ctx.log.lease_callback()` (audit.py) as `on_event`: it logs `taken_over`, `stalled` and
# `lost` as `lease` events. It may run on the heartbeat thread (`lost`); the log writer is
# thread-safe and never raises for a failed append.


def pass_lease_key() -> str:
    return _PASS_KEY


def pr_digest(pr: str) -> str:
    """The PR digest: the first 20 hex characters of the sha256 of the PR reference."""
    return hashlib.sha256(pr.encode("utf-8", "surrogateescape")).hexdigest()[:20]


def pr_lease_key(pr_digest: str) -> str:
    if not isinstance(pr_digest, str) or not _PR_DIGEST_RE.fullmatch(pr_digest):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError("a PR digest is 20 lowercase hex characters")
    return f"pr:{pr_digest}"


def round_lease_key(round_id: str) -> str:
    key = f"round:{round_id}"
    if not isinstance(round_id, str) or not _KEY_RE.fullmatch(key):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError("invalid round id")
    return key


def _check_key(key: str) -> None:
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError("invalid lease key")


@dataclass(frozen=True)
class LeaseRow:
    key: str
    token: str
    pid: int
    pid_start: str
    boot_id: str
    acquired_at: float
    renewed_at: float
    expires_at: float
    rev: int


_ROW_COLUMNS: Final = (
    "key, token, pid, pid_start, boot_id, acquired_at, renewed_at, expires_at, rev"
)


def cas_update(
    conn: sqlite3.Connection,
    table: str,
    key_column: str,
    key: object,
    rev: int,
    changes: Mapping[str, object],
    *,
    where: Mapping[str, object] | None = None,
) -> bool:
    """Set ``changes`` on the row ``key_column = key`` only while its ``rev`` is still ``rev``, and
    bump the ``rev``. ``where`` adds column equalities for a table whose key has several columns.
    True when this call made the transition, False when the row is missing or another writer got
    there first (the caller decides what that means for its table).

    Must run inside a transaction. Names are checked as plain identifiers; values are parameters.
    ``rev`` itself cannot be set through ``changes``.
    """
    extra = dict(where or {})
    names = [table, key_column, *changes, *extra]
    if not changes or any(
        not isinstance(n, str) or not _IDENT_RE.fullmatch(n)  # pyright: ignore[reportUnnecessaryIsInstance]
        for n in names
    ):
        raise ValueError("invalid table, column or empty change set")
    if key_column.lower() == "rev" or any(name.lower() == "rev" for name in changes):
        raise ValueError("rev is managed by the compare-and-set")  # SQLite names are case-blind
    assignments = ", ".join(f"{name} = ?" for name in changes)
    conditions = "".join(f" AND {name} = ?" for name in extra)
    cursor = conn.execute(
        f"UPDATE {table} SET {assignments}, rev = rev + 1"
        f" WHERE {key_column} = ? AND rev = ?{conditions}",
        (*changes.values(), key, rev, *extra.values()),
    )
    if cursor.rowcount > 1:
        raise CliError(
            "internal", f"{table}.{key_column} is not unique: compare-and-set matched several rows"
        )
    return cursor.rowcount == 1


def _is_busy_error(error: CliError) -> bool:
    return isinstance(error, _BusyError)


def _lease_lost(key: str) -> CliError:
    return CliError("lease_lost", f"the {key} lease was lost; this command stops without writing")


def _contended(kind: Literal["busy", "lease_timeout"], key: str, wait: float) -> CliError:
    if kind == "lease_timeout":
        return CliError("lease_timeout", f"timed out after {wait:g}s waiting for the {key} lease")
    return CliError(
        "refused",
        f"another command holds {key}; retry later",
        reason="busy",
        details={"lease": key},
    )


RELEASE_ATTEMPTS: Final = 3


class Lease:
    """One held lease. Release it (or use it as a context manager); renewal runs in a heartbeat."""

    def __init__(
        self,
        store: Store,
        key: str,
        token: str,
        *,
        taken_over: bool,
        on_event: EventCallback | None = None,
    ) -> None:
        self.store = store
        self.key = key
        self.token = token
        self.taken_over = taken_over
        self._on_event = on_event
        self._pid = os.getpid()  # a forked child must never touch its parent's row
        self._lost = threading.Event()
        self._lost_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def lost(self) -> bool:
        """True once the heartbeat saw the lease gone (``transaction()`` is the authority)."""
        return self._lost.is_set()

    def _mark_lost(self) -> None:
        """Note the loss and report ``lost`` to ``on_event`` once per lease."""
        with self._lost_lock:
            first = not self._lost.is_set()
            self._lost.set()
        if first and self._on_event is not None:
            # a failing log must not hide the lease_lost the caller is about to raise
            with contextlib.suppress(Exception):
                self._on_event("lost", self.key)

    def _lose(self) -> CliError:
        self._mark_lost()
        return _lease_lost(self.key)

    def _renew_on(self, store: Store) -> None:
        if os.getpid() != self._pid:
            raise _lease_lost(self.key)  # a forked child: not a loss, the parent still holds it
        now = store.clock.now().timestamp()
        with store.transaction() as conn:
            cursor = conn.execute(
                "UPDATE leases SET renewed_at = ?, expires_at = ?, rev = rev + 1"
                " WHERE key = ? AND token = ?",
                (now, now + LEASE_TTL_SECONDS, self.key, self.token),
            )
            if cursor.rowcount != 1:
                raise self._lose()

    def renew(self) -> None:
        """Push the expiry out; ``lease_lost`` when the row is no longer this holder's."""
        self._renew_on(self.store)

    def start_heartbeat(self, interval: float = HEARTBEAT_SECONDS) -> None:
        """Renew every ``interval`` seconds from a thread with its own connection."""
        if self._thread is not None or self.store.source is None:
            return
        thread = threading.Thread(
            target=self._beat, args=(interval,), name=f"lease-{self.key}", daemon=True
        )
        self._thread = thread
        thread.start()

    def _beat(self, interval: float) -> None:
        source = self.store.source
        assert source is not None
        try:
            beat = Store.open(source, write=True)
        except CliError:
            return  # the next write finds out through its own lease check
        beat.clock = self.store.clock
        try:
            while not self._stop.wait(interval):
                try:
                    self._renew_on(beat)
                except CliError as error:
                    if error.code == "lease_lost":
                        self._mark_lost()
                        return
                    # a busy or unavailable database: try again at the next beat
                except sqlite3.Error:
                    pass
        finally:
            beat.close()

    def release(self) -> None:
        """Stop the heartbeat and delete the row if it is still this holder's. Idempotent."""
        if os.getpid() != self._pid:
            return  # a forked child: the row is the parent's
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)
        for _attempt in range(RELEASE_ATTEMPTS):
            try:
                with self.store.transaction() as conn:
                    conn.execute(
                        "DELETE FROM leases WHERE key = ? AND token = ?", (self.key, self.token)
                    )
                return
            except sqlite3.Error:
                return  # the row is taken over later: its holder is gone
            except CliError as error:
                if not _is_busy_error(error):
                    raise
                # busy: try again, then give up (the row is taken over later)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A write transaction that first checks, under the write lock, that this holder still
        owns the row: ``lease_lost`` (exit 1) otherwise, and nothing is written."""
        if os.getpid() != self._pid:
            raise _lease_lost(self.key)  # a forked child: not a loss, the parent still holds it
        with self.store.transaction() as conn:
            held = conn.execute(
                "SELECT 1 FROM leases WHERE key = ? AND token = ?", (self.key, self.token)
            ).fetchone()
            if held is None:
                raise self._lose()
            yield conn

    def compare_and_set(
        self, table: str, key_column: str, key: object, rev: int, changes: Mapping[str, object]
    ) -> bool:
        """One ``cas_update`` in a lease-checked transaction."""
        with self.transaction() as conn:
            return cas_update(conn, table, key_column, key, rev, changes)

    def __enter__(self) -> Lease:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.release()
        else:
            with contextlib.suppress(Exception):  # never mask the in-flight exception
                self.release()


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _read_lease(conn: sqlite3.Connection, key: str) -> LeaseRow | None:
    row = conn.execute(f"SELECT {_ROW_COLUMNS} FROM leases WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    k, token, pid, pid_start, boot, acquired, renewed, expires, rev = row
    sane = (
        isinstance(token, str)
        and isinstance(pid, int)
        and isinstance(pid_start, str)
        and isinstance(boot, str)
        and _is_number(acquired)
        and _is_number(renewed)
        and _is_number(expires)
        and isinstance(rev, int)
    )
    if not sane:
        # a damaged row: no live holder can own it, so it reads as a holder verified gone (pid 0)
        return LeaseRow(key, "", 0, "", "", 0.0, 0.0, 0.0, 0)
    return LeaseRow(
        k, token, pid, pid_start, boot, float(acquired), float(renewed), float(expires), rev
    )


def _claim(store: Store, key: str, on_event: EventCallback | None) -> tuple[Lease | None, bool]:
    """One attempt: (lease, False) when taken, (None, stalled) when a live holder has it.

    A busy database (another command holds the write lock) counts as a live holder: (None, False).
    """
    pid = os.getpid()
    token = secrets.token_hex(16)
    me = (pid, process_start(pid) or "", boot_id())
    now = store.clock.now().timestamp()
    try:
        with store.transaction() as conn:
            row = _read_lease(conn, key)
            taken_over = row is not None and holder_gone(row.pid, row.pid_start, row.boot_id)
            if row is None:
                conn.execute(
                    f"INSERT INTO leases ({_ROW_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                    (key, token, *me, now, now, now + LEASE_TTL_SECONDS),
                )
            elif taken_over:
                conn.execute(
                    "UPDATE leases SET token = ?, pid = ?, pid_start = ?, boot_id = ?,"
                    " acquired_at = ?, renewed_at = ?, expires_at = ?, rev = rev + 1"
                    " WHERE key = ?",
                    (token, *me, now, now, now + LEASE_TTL_SECONDS, key),
                )
            else:
                return None, row.expires_at <= now  # alive: never taken over, even when expired
    except CliError as error:
        if _is_busy_error(error):
            return None, False
        raise
    lease = Lease(store, key, token, taken_over=taken_over, on_event=on_event)
    if taken_over and on_event is not None:
        try:
            on_event("taken_over", key)
        except BaseException:
            with contextlib.suppress(Exception):
                lease.release()  # the row is committed: never leave it held
            raise
    return lease, False


def _try_acquire(
    self: Store,
    key: str,
    *,
    heartbeat: float | None = HEARTBEAT_SECONDS,
    on_event: EventCallback | None = None,
) -> Lease | None:
    """Take the lease without waiting; None when a live holder has it.

    A missing row, or a holder verified gone (whether or not expired), is taken at once. An
    expired lease whose holder is alive is left alone and reported as ``stalled`` to ``on_event``.
    ``heartbeat`` is the renewal interval in seconds (None: no heartbeat, for tests).
    """
    _check_key(key)
    lease, stalled = _claim(self, key, on_event)
    if lease is None:
        if stalled and on_event is not None:
            on_event("stalled", key)
        return None
    if heartbeat is not None:
        lease.start_heartbeat(heartbeat)
    return lease


def _acquire(
    self: Store,
    key: str,
    *,
    wait: float,
    contended: Literal["busy", "lease_timeout"],
    heartbeat: float | None = HEARTBEAT_SECONDS,
    on_event: EventCallback | None = None,
) -> Lease:
    """Take the lease, waiting up to ``wait`` seconds (the store's clock) for a live holder.

    Contended after the wait: ``refused`` with reason ``busy`` (exit 20) or ``lease_timeout``
    (exit 1), as the caller's row of the Concurrency table says. ``stalled`` is reported once.
    """
    _check_key(key)
    if not math.isfinite(wait):
        raise ValueError("wait must be a finite number of seconds")
    deadline = self.clock.monotonic() + wait
    reported = False
    while True:
        lease, stalled = _claim(self, key, on_event)
        if lease is not None:
            if heartbeat is not None:
                lease.start_heartbeat(heartbeat)
            return lease
        if stalled and not reported and on_event is not None:
            on_event("stalled", key)
            reported = True
        left = deadline - self.clock.monotonic()
        if left <= 0:
            raise _contended(contended, key, wait)
        self.clock.sleep(min(POLL_SECONDS, left))
