"""The handled set (docs/interface.md: Handled set): one record per comment, its state and every
transition of the state table. Stage 1.

Writes only go through ``lease.transaction()`` (a lost lease aborts with ``lease_lost`` before
anything is written) and every change of an existing row is ``cas_update`` on its ``rev``.

Comment text is untrusted. The row keeps the sha256 of the whole body (that hash decides edits)
and the text itself is stored as a file named by hash under the PR's directory of the state
directory, cut to ``MAX_STORED_BODY_BYTES``. Text leaves this module only through
``HandledSet.body``, sanitized.

Seams owned by later issues:

- TODO(#29): ``record_open`` tells whether a record (a pending confirmation) of the PR is open; the
  default says none, because the records table does not exist yet.
- ``due.py`` reads ``body_seen_at`` (the settle timestamp) of the eligible unhandled records;
  nothing here decides when a PR is due.
- TODO(#18), TODO(#23): the round code calls ``deliver`` (the job selecting comments into a
  prompt), ``mark_handled`` (the round becoming pending), ``release`` (failed, canceled,
  escalated) and ``redo`` (``reject --redo``).
- TODO(#20): the one sanitizer; ``body`` uses the line sanitizer of ``output.py`` meanwhile.
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Final

from .detect import CommentRead, ReadComment, StoredPr, absent_comment_ids
from .errors import CliError
from .output import sanitize_line
from .store import Lease, StateDir, cas_update

MAX_STORED_BODY_BYTES: Final = 64 * 1024
_ATTEMPTS: Final = 3
PRUNE_GRACE_SECONDS: Final = 3600.0

_COLUMNS: Final = (
    "pr, comment_id, thread_id, author_id, author_login, created_at, host_updated_at,"
    " body_sha256, body_file, body_bytes, first_seen_at, body_seen_at, state, round,"
    " delivered_sha256, delivered_from, handled_sha256, handled_reason, edited, eligible, reason,"
    " thread_resolved, outdated, path, line, reply_to, rev"
)
_INSTANT_RE: Final = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]+))?"
    r"(?:([Zz])|([+-])([0-9]{2}):([0-9]{2}))"
)

RecordOpen = Callable[[sqlite3.Connection, str], bool]


@dataclass(frozen=True)
class CommentRecord:
    pr: str
    id: str
    thread_id: str
    author_id: str | None
    author_login: str | None
    created_at: str
    host_updated_at: str
    body_sha256: str
    body_file: str | None
    body_bytes: int
    first_seen_at: float
    body_seen_at: float  # the settle timestamp: when the current body hash was first stored
    state: str
    round: str | None
    delivered_sha256: str | None
    delivered_from: str | None
    handled_sha256: str | None
    handled_reason: str | None
    edited: bool
    eligible: bool
    reason: str | None
    thread_resolved: bool
    outdated: bool | None
    path: str | None
    line: int | None
    reply_to: str | None
    rev: int


@dataclass(frozen=True)
class StoredBody:
    text: str  # sanitized, untrusted
    truncated: bool
    original_bytes: int


class Delivered(list[str]):
    """The ids ``deliver`` moved to ``delivered`` (a list), plus ``stale``: the ids it skipped
    because the stored body hash differs from the one the caller expected."""

    def __init__(self, delivered: Sequence[str] = (), stale: Sequence[str] = ()) -> None:
        super().__init__(delivered)
        self.stale = list(stale)


class _Retry(Exception):
    """A body file the transaction needs was not stored yet (the row changed meanwhile)."""


def parse_instant(text: str) -> datetime:
    """An RFC 3339 instant as an aware datetime (a leap second counts as the next second)."""
    parts = _INSTANT_RE.fullmatch(text)
    if parts is None:
        raise CliError("internal", "a stored instant is not RFC 3339")
    year, month, day, hour, minute, second = (int(p) for p in parts.groups()[:6])
    fraction = parts.group(7) or ""
    zone = UTC
    if parts.group(9) is not None:
        offset = timedelta(hours=int(parts.group(10)), minutes=int(parts.group(11)))
        zone = timezone(-offset if parts.group(9) == "-" else offset)
    try:
        moment = datetime(
            year, month, day, hour, minute, min(second, 59), int(fraction[:6].ljust(6, "0")), zone
        )
    except ValueError:
        raise CliError("internal", "a stored instant is not a real time") from None
    return moment + timedelta(seconds=1) if second == 60 else moment


def _digest(text: str) -> str:
    return sha256(text.encode("utf-8", "replace")).hexdigest()


def _cut(text: str) -> bytes:
    """The text as UTF-8, cut to the storage bound without splitting a character."""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= MAX_STORED_BODY_BYTES:
        return raw
    return raw[:MAX_STORED_BODY_BYTES].decode("utf-8", "ignore").encode("utf-8")


def _record(row: Sequence[Any]) -> CommentRecord:
    v = list(row)
    v[18] = bool(v[18])
    v[19] = bool(v[19])
    v[21] = bool(v[21])
    v[22] = None if v[22] is None else bool(v[22])
    return CommentRecord(*v)


def _flag(value: bool | None) -> int | None:
    return None if value is None else int(value)


class HandledSet:
    def __init__(
        self,
        lease: Lease,
        *,
        record_open: RecordOpen | None = None,
        now: Callable[[], float] | None = None,
        prune_grace: float = PRUNE_GRACE_SECONDS,
    ) -> None:
        self.lease = lease
        self._prune_grace = prune_grace
        self._record_open = record_open
        self._now = now or (lambda: lease.store.clock.now().timestamp())

    # -- reading ----------------------------------------------------------------------------

    def get(self, pr: str, comment_id: str) -> CommentRecord | None:
        row = self.lease.store.conn.execute(
            f"SELECT {_COLUMNS} FROM comments WHERE pr = ? AND comment_id = ?", (pr, comment_id)
        ).fetchone()
        return None if row is None else _record(row)

    def records(self, pr: str, state: str | None = None) -> list[CommentRecord]:
        sql = f"SELECT {_COLUMNS} FROM comments WHERE pr = ?"
        args: tuple[str, ...] = (pr,)
        if state is not None:
            sql += " AND state = ?"
            args = (pr, state)
        rows = self.lease.store.conn.execute(sql + " ORDER BY body_seen_at, comment_id", args)
        return [_record(r) for r in rows]

    def body(self, pr: str, comment_id: str) -> StoredBody:
        """The stored text of a comment, sanitized (control and invisible characters become
        visible markers; line breaks stay).

        Errors: ``internal`` when there is nothing to read (an unknown comment or PR, a row without
        a file, a store not opened from a state directory); ``state_unsafe`` when the stored file
        is missing, altered or unsafe."""
        record = self.get(pr, comment_id)
        stored = self.lease.store.source
        if record is None or stored is None:
            raise CliError("internal", "no stored text for this comment")
        stored_pr = self.lease.store.conn.execute(
            "SELECT digest FROM prs WHERE pr = ?", (pr,)
        ).fetchone()
        if record.body_file is None or stored_pr is None:
            raise CliError("internal", "no stored text for this comment")
        raw = stored.get_file(stored_pr[0], record.body_file)
        text = "\n".join(sanitize_line(part) for part in raw.decode("utf-8", "replace").split("\n"))
        return StoredBody(text, record.body_bytes > len(raw), record.body_bytes)

    # -- a read -----------------------------------------------------------------------------

    def record_read(self, pr: StoredPr, read: CommentRead) -> None:
        """Record one good read of ``pr`` (the ``Detection(on_read=...)`` hook): first sight,
        verdicts, edits, resolution and deletion, in one transaction.

        The caller must serialize reads of one PR on one lease: two readers on different leases
        could apply an older read over a newer one. TODO(#17): ``check`` must hold the PR lease
        around the detection that calls this hook.

        After the rows commit, stored body files no row refers to (and older than the grace
        period) are removed; files are shared between rows, so only the set of the PR is compared.
        """
        if read.pr != pr.pr:
            raise CliError("internal", "a read of another PR was given for recording")
        stored = self.lease.store.source
        if stored is None:
            raise CliError("internal", "the store was not opened from a state directory")
        for _ in range(_ATTEMPTS):
            with self.lease.transaction() as conn:  # checks the lease before any file is written
                known = dict(
                    conn.execute(
                        "SELECT comment_id, body_sha256 FROM comments WHERE pr = ?", (pr.pr,)
                    ).fetchall()
                )
            files = self._put_files(stored, pr, read, known)
            try:
                with self.lease.transaction() as conn:
                    self._apply(conn, pr, read, files)
            except _Retry:
                continue
            # The rows are already committed: a cleanup failure (I/O, or an owner directory that
            # turned unsafe) must not fail the recorded read. Orphans are pruned on a later read.
            with contextlib.suppress(OSError, CliError):
                self._prune(stored, pr)
            return
        raise CliError("internal", "the comment rows kept changing while they were recorded")

    def _prune(self, stored: StateDir, pr: StoredPr) -> None:
        keep = {
            name
            for (name,) in self.lease.store.conn.execute(
                "SELECT body_file FROM comments WHERE pr = ? AND body_file IS NOT NULL", (pr.pr,)
            )
        }
        stored.prune_owner_files(pr.digest, keep, min_age=self._prune_grace)

    @staticmethod
    def _put_files(
        stored: StateDir, pr: StoredPr, read: CommentRead, known: dict[str, str]
    ) -> dict[str, str]:
        """body sha256 -> stored file name, for every body that is new or changed."""
        files: dict[str, str] = {}
        for c in read.comments:
            digest = _digest(c.body)
            if known.get(c.id) != digest and digest not in files:
                files[digest] = stored.put_file(pr.digest, _cut(c.body))
        return files

    def _apply(
        self, conn: sqlite3.Connection, pr: StoredPr, read: CommentRead, files: dict[str, str]
    ) -> None:
        now = self._now()
        installed = self._installed_at(conn)
        rows = {
            r.id: r
            for r in (
                _record(x)
                for x in conn.execute(f"SELECT {_COLUMNS} FROM comments WHERE pr = ?", (pr.pr,))
            )
        }
        open_cache: list[bool] = []

        def record_open() -> bool:
            if not open_cache:
                open_cache.append(
                    self._record_open(conn, pr.pr) if self._record_open is not None else False
                )
            return open_cache[0]

        for c in read.comments:
            verdict = read.verdicts.get(c.id)
            eligible, reason = (
                (verdict.eligible, verdict.reason) if verdict else (False, "unknown_access")
            )
            existing = rows.get(c.id)
            if existing is None:
                self._insert(conn, pr, c, eligible, reason, installed, now, files)
            elif existing.state != "deleted":
                self._update(conn, existing, c, eligible, reason, now, files, record_open)
        live = [r.id for r in rows.values() if r.state not in ("deleted", "delivered")]
        for comment_id in sorted(absent_comment_ids(live, read)):
            self._change(conn, rows[comment_id], {"state": "deleted"})
        if read.complete:  # the records now match the read that detect.py stamped as read
            conn.execute("UPDATE prs SET records_current = 1, rev = rev + 1 WHERE pr = ?", (pr.pr,))

    def _installed_at(self, conn: sqlite3.Connection) -> datetime:
        row = conn.execute("SELECT value FROM meta WHERE key = 'installed_at'").fetchone()
        if row is None:
            raise CliError("internal", "comments were read before installed_at was stored")
        return parse_instant(row[0])

    def _insert(
        self,
        conn: sqlite3.Connection,
        pr: StoredPr,
        c: ReadComment,
        eligible: bool,
        reason: str | None,
        installed: datetime,
        now: float,
        files: dict[str, str],
    ) -> None:
        digest = _digest(c.body)
        if digest not in files:
            raise _Retry
        state, handled_reason, handled_sha = "unhandled", None, None
        if c.thread_resolved:  # a resolved thread leaves prompts, whatever else is true
            state, handled_reason, handled_sha = "handled", "resolved", digest
        elif parse_instant(c.created_at) < installed:
            state = "seen"  # installing review-respond never answers old comments
        conn.execute(
            "INSERT INTO comments (pr, comment_id, thread_id, author_id, author_login, created_at,"
            " host_updated_at, body_sha256, body_file, body_bytes, first_seen_at, body_seen_at,"
            " state, handled_sha256, handled_reason, eligible, reason, thread_resolved, outdated,"
            " path, line, reply_to) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                pr.pr, c.id, c.thread_id, c.author_id, c.author_login, c.created_at,
                c.updated_at, digest, files[digest], len(c.body.encode("utf-8", "replace")),
                now, now, state, handled_sha, handled_reason, int(eligible), reason,
                int(c.thread_resolved), _flag(c.thread_outdated), c.path, c.line, c.reply_to,
            ),
        )  # fmt: skip

    def _update(
        self,
        conn: sqlite3.Connection,
        row: CommentRecord,
        c: ReadComment,
        eligible: bool,
        reason: str | None,
        now: float,
        files: dict[str, str],
        record_open: Callable[[], bool],
    ) -> None:
        digest = _digest(c.body)
        host: dict[str, object] = {
            "thread_id": c.thread_id,
            "author_id": c.author_id,
            "author_login": c.author_login,
            "host_updated_at": c.updated_at,
            "eligible": int(eligible),
            "reason": reason,
            "thread_resolved": int(c.thread_resolved),
            "outdated": _flag(c.thread_outdated),
            "path": c.path,
            "line": c.line,
            "reply_to": c.reply_to,
        }
        before: dict[str, object] = {
            "thread_id": row.thread_id,
            "author_id": row.author_id,
            "author_login": row.author_login,
            "host_updated_at": row.host_updated_at,
            "eligible": int(row.eligible),
            "reason": row.reason,
            "thread_resolved": int(row.thread_resolved),
            "outdated": _flag(row.outdated),
            "path": row.path,
            "line": row.line,
            "reply_to": row.reply_to,
        }
        changes = {k: v for k, v in host.items() if before[k] != v}
        if digest != row.body_sha256:  # an edit restarts the settle time, a restore too
            if digest not in files:
                raise _Retry
            changes.update(
                body_sha256=digest,
                body_file=files[digest],
                body_bytes=len(c.body.encode("utf-8", "replace")),
                body_seen_at=now,
            )
        # `delivered` keeps its state until its round ends; the next read applies the rest
        if row.state in ("seen", "unhandled", "handled") and c.thread_resolved:
            if row.state != "handled":  # a comment a round handled keeps its round and no reason
                changes.update(
                    state="handled", handled_reason="resolved", handled_sha256=digest, edited=0
                )
        elif (
            row.state == "handled"
            and digest != row.handled_sha256
            and eligible
            and not record_open()
        ):
            # handled_sha256 stays: it records what was last answered (no CHECK needs it here)
            changes.update(
                state="unhandled",
                edited=1,
                handled_reason=None,
                round=None,
                delivered_sha256=None,
                delivered_from=None,
            )
        self._change(conn, row, changes)

    @staticmethod
    def _change(conn: sqlite3.Connection, row: CommentRecord, changes: dict[str, object]) -> None:
        if changes and not cas_update(
            conn, "comments", "comment_id", row.id, row.rev, changes, where={"pr": row.pr}
        ):
            # cannot happen inside the write transaction that read the row
            raise CliError("internal", f"comment {row.id} changed under the write lock")

    # -- round transitions ------------------------------------------------------------------

    @contextlib.contextmanager
    def _write(self, conn: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
        """The caller's open transaction (``conn``), or one of this lease's own."""
        if conn is not None:
            yield conn
        else:
            with self.lease.transaction() as own:
                yield own

    def _transition(
        self, conn: sqlite3.Connection, pr: str, ids: Sequence[str] | None,
        round_id: str | None, want: str,
        change: Callable[[CommentRecord], dict[str, object] | None],
    ) -> list[str]:  # fmt: skip
        done: list[str] = []
        sql = f"SELECT {_COLUMNS} FROM comments WHERE pr = ? AND state = ?"
        args: list[object] = [pr, want]
        if round_id is not None:
            sql += " AND round = ?"
            args.append(round_id)
        rows = [_record(r) for r in conn.execute(sql + " ORDER BY comment_id", args)]
        order = {c: i for i, c in enumerate(ids)} if ids is not None else None
        if order is not None:
            rows = sorted((r for r in rows if r.id in order), key=lambda r: order[r.id])
        for row in rows:
            changes = change(row)
            if changes is not None:
                self._change(conn, row, changes)
                done.append(row.id)
        return done

    # Each transition below runs in the caller's open transaction when ``conn`` is given (the
    # round rows of #18 and #23 must change atomically with the comments), else in its own.

    def deliver(
        self,
        pr: str,
        ids: Sequence[str],
        *,
        round_id: str,
        include_seen: bool = False,
        expected: Mapping[str, str] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> Delivered:
        """A job selects comments into its prompt: eligible ``unhandled`` ones (and ``seen`` ones
        of an ``--include-existing`` round) become ``delivered``. Returns the ids delivered.

        ``expected`` maps comment ids to the body sha256 the caller showed; an id whose stored hash
        differs (an edit between showing and delivering) is skipped and listed in ``.stale``."""
        done: list[str] = []
        stale: list[str] = []

        def make(source: str) -> Callable[[CommentRecord], dict[str, object] | None]:
            def change(row: CommentRecord) -> dict[str, object] | None:
                if not row.eligible:
                    return None
                if expected is not None and expected.get(row.id, row.body_sha256) != (
                    row.body_sha256
                ):
                    stale.append(row.id)
                    return None
                return {
                    "state": "delivered",
                    "delivered_from": source,
                    "round": round_id,
                    "delivered_sha256": row.body_sha256,
                }

            return change

        with self._write(conn) as c:
            for want in ("unhandled", "seen") if include_seen else ("unhandled",):
                done += self._transition(c, pr, ids, None, want, make(want))
        order = {i: n for n, i in enumerate(ids)}
        return Delivered(sorted(done, key=order.__getitem__), sorted(stale, key=order.__getitem__))

    def mark_handled(
        self, pr: str, round_id: str, *, conn: sqlite3.Connection | None = None
    ) -> list[str]:
        """The round became ``pending``: its delivered comments are handled (as delivered)."""
        with self._write(conn) as c:
            return self._transition(
                c, pr, None, round_id, "delivered",
                lambda row: {
                    "state": "handled",
                    "handled_sha256": row.delivered_sha256,
                    "handled_reason": None,
                    "edited": 0,
                },
            )  # fmt: skip

    def release(
        self, pr: str, round_id: str, *, conn: sqlite3.Connection | None = None
    ) -> list[str]:
        """The round ended ``failed``, ``canceled`` or ``escalated``: its comments go back to the
        state they were delivered from."""
        with self._write(conn) as c:
            return self._transition(
                c, pr, None, round_id, "delivered",
                lambda row: {
                    "state": row.delivered_from or "unhandled",
                    "round": None,
                    "delivered_sha256": None,
                    "delivered_from": None,
                },
            )  # fmt: skip

    def redo(self, pr: str, round_id: str, *, conn: sqlite3.Connection | None = None) -> list[str]:
        """``reject --redo``: the comments the round handled (not those handled as resolved)
        become ``unhandled`` again."""
        with self._write(conn) as c:
            return self._transition(
                c, pr, None, round_id, "handled",
                lambda row: None
                if row.handled_reason is not None
                else {
                    "state": "unhandled",
                    "round": None,
                    "delivered_sha256": None,
                    "delivered_from": None,
                    "handled_sha256": None,
                },
            )  # fmt: skip
