"""Eligibility rules and the access cache (docs/interface.md: Eligibility). Pure rules and SQL; the
forgectl call, its budget and its answer check live in `detect.py`.

A comment is eligible or carries exactly one reason it is not (`REASONS`). The rules that need no
lookup run first (`prejudge`), so a comment that is ineligible anyway never costs a request; the
write-access rule (`reviewers` or `forgectl access`) is the last one.

The handled set (`handled.py`) stores each verdict's `eligible` and `reason` per comment.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass
from typing import Final

REASONS: Final = (
    "own",
    "bot",
    "unknown_author",
    "no_access",
    "unknown_access",
    "resolved",
    "minimized",
)
MARKER: Final = "<!-- pr-comment:v1 -->"  # the attribution marker of pr-comment footers
LOOKUP_COST: Final = 2  # one `forgectl access` is two host requests

# forgectl access.json `user`: a GitHub login (with an optional `[bot]`) or a GitLab username
LOGIN_RE: Final = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}(?:\[bot\])?|[A-Za-z0-9_.][A-Za-z0-9_.-]{0,254}"
)
# forgectl's input grammar `login` (tests/pinned/forgectl/grammar.json): the only logins that may
# be passed as an argument to `forgectl access`; stricter than the answer's `user` above
ARGV_LOGIN_RE: Final = re.compile(
    r"(?:[A-Za-z0-9][A-Za-z0-9_-]{0,38}(?:\[bot\])?|[A-Za-z0-9_]"
    r"|[A-Za-z0-9_.][A-Za-z0-9_.-]{0,253}[A-Za-z0-9_-])"
)
# pr-comment's marker detection (pr-comment docs/interface.md, Marker detection)
_DEDUPE_LINE_RE: Final = re.compile(r"<!-- forgectl:dedupe:[A-Za-z0-9._:-]{1,128} -->")
_LINE_BREAK_RE: Final = re.compile(r"\r\n|\n|\r")


def has_marker(body: str) -> bool:
    """True when ``body`` was posted through pr-comment: after trailing whitespace and at most
    one last forgectl dedupe line are removed, its last non-empty line is exactly the marker. A
    body that only quotes the marker elsewhere does not match."""
    lines = _LINE_BREAK_RE.split(body.rstrip())
    if lines and _DEDUPE_LINE_RE.fullmatch(lines[-1]):
        lines.pop()
    for line in reversed(lines):
        if line.strip():
            return line == MARKER
    return False


@dataclass(frozen=True)
class Verdict:
    eligible: bool
    reason: str | None = None


ELIGIBLE: Final = Verdict(True)


def ineligible(reason: str) -> Verdict:
    return Verdict(False, reason)


def entry_ids(entries: Collection[str], host: str) -> frozenset[str]:
    """The account ids of the `<host>:<id>` entries that name ``host`` (split at the last colon,
    so a host with a port keeps it)."""
    out: set[str] = set()
    for entry in entries:
        entry_host, _, account = entry.rpartition(":")
        if entry_host == host and account:
            out.add(account)
    return frozenset(out)


def prejudge(
    *,
    author_id: str | None,
    is_bot: bool | None,
    body: str,
    resolved: bool,
    minimized: bool,
    account_id: str,
    bot_ids: frozenset[str],
    own_prefix: str | None,
) -> Verdict | None:
    """The reason a comment is ineligible without any lookup, or None when only write access is
    left to decide. Order: unknown author, own, bot, resolved, minimized (a comment that is
    resolved or minimized never costs a lookup, whatever its author)."""
    if author_id is None:
        return ineligible("unknown_author")
    if author_id == account_id:
        # the marker wins over the opt-in prefix; without the prefix every own comment is own
        if has_marker(body) or own_prefix is None or not body.startswith(own_prefix):
            return ineligible("own")
    if is_bot is not False or author_id in bot_ids:
        return ineligible("bot")
    if resolved:
        return ineligible("resolved")
    if minimized:
        return ineligible("minimized")
    return None


# ---- the access cache (table `access_cache`) -------------------------------------------------


def cache_get(
    conn: sqlite3.Connection, repo: str, author_id: str, login: str, *, now: float, ttl: float
) -> tuple[bool, bool | None]:
    """(hit, write_access). A row is used only while younger than ``ttl`` seconds, not from the
    future, and only for the login it was stored with (a renamed login is a miss)."""
    row = conn.execute(
        "SELECT login, write_access, fetched_at FROM access_cache WHERE repo = ? AND author_id = ?",
        (repo, author_id),
    ).fetchone()
    if row is None:
        return False, None
    stored_login, answer, fetched_at = row
    if stored_login != login or not 0 <= now - fetched_at < ttl:
        return False, None
    return True, None if answer is None else bool(answer)


def cache_put(
    conn: sqlite3.Connection,
    repo: str,
    author_id: str,
    login: str,
    write_access: bool | None,
    *,
    now: float,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO access_cache (repo, author_id, login, write_access, fetched_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (repo, author_id, login, None if write_access is None else int(write_access), now),
    )
