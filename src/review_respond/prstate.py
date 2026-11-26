"""The PR state object that `list`, `show` and `check` print (docs/interface.md: Objects).

Read-only: every function takes a connection and reads. The state is derived (`due.derive_state`)
from the stored rows; nothing here writes or takes a lease. Times are stored as epoch seconds (or
the host's RFC 3339 text) and printed as UTC RFC 3339 through `audit.format_ts`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from . import due
from .audit import format_ts
from .config import Config
from .detect import StoredPr, get_pr
from .errors import CliError
from .handled import CommentRecord, parse_instant, read_records, stored_body
from .output import sanitize_line
from .store import Store

MAX_BODY_CHARS: Final = 65536  # schemas/defs.json comment_record.body
MAX_PATH_CHARS: Final = 4096


def _tampered(what: str) -> CliError:
    return CliError("state_unsafe", f"the state database holds {what} that is not valid")


def ts(epoch: float) -> str:
    try:
        return format_ts(datetime.fromtimestamp(epoch, UTC))
    except (TypeError, ValueError, OverflowError, OSError):
        raise _tampered("a time") from None


def host_ts(text: str) -> str:
    """A host's RFC 3339 instant (any offset) as the UTC form the schemas use."""
    try:
        return format_ts(parse_instant(text))
    except (TypeError, ValueError, OverflowError, CliError):
        raise _tampered("an instant") from None


@dataclass(frozen=True)
class Derived:
    state: str
    waiting_reason: str | None
    inputs: due.DueInputs


def derive(conn: sqlite3.Connection, config: Config, pr: str, now: float) -> Derived:
    """The derived state of a stored PR. TODO(#18) TODO(#29): `RoundFacts` carries the queued and
    running rounds, the counted rounds and the open record; TODO(#19): `agent_busy`."""
    inputs = due.load_inputs(conn, config, pr, now=now, facts=due.RoundFacts())
    state, reason = due.derive_state(inputs)
    return Derived(state, reason, inputs)


def counts(conn: sqlite3.Connection, pr: str) -> dict[str, int]:
    """Comment counts of a PR. `unhandled` and `seen` count eligible comments only; `ineligible`
    counts every recorded ineligible comment still on the host and `edited` those flagged edited
    (a deleted comment counts nowhere)."""
    row = conn.execute(
        "SELECT"
        " coalesce(sum(state = 'unhandled' AND eligible = 1), 0),"
        " coalesce(sum(state = 'seen' AND eligible = 1), 0),"
        " coalesce(sum(state != 'deleted' AND eligible = 0), 0),"
        " coalesce(sum(state != 'deleted' AND edited = 1), 0)"
        " FROM comments WHERE pr = ?",
        (pr,),
    ).fetchone()
    return {"unhandled": row[0], "seen": row[1], "ineligible": row[2], "edited": row[3]}


def pr_object(
    conn: sqlite3.Connection, config: Config, stored: StoredPr, now: float
) -> dict[str, Any]:
    """One PR state object (schemas/defs.json pr_state)."""
    derived = derive(conn, config, stored.pr, now)
    block = derived.inputs.block
    base = conn.execute("SELECT base_agent FROM prs WHERE pr = ?", (stored.pr,)).fetchone()[0]
    return {
        "pr": stored.pr,
        "repo": stored.repo,
        "state": derived.state,
        "waiting_reason": derived.waiting_reason,
        "block": None if block is None else {"reason": block.reason, "set_at": ts(block.set_at)},
        "head_sha": stored.head_sha,
        "head_ref": stored.head_ref,
        "base_ref": stored.base_ref,
        "is_fork": stored.is_fork,
        "base_agent": base or None,
        "rounds": 0,  # TODO(#18): the rounds table
        **counts(conn, stored.pr),
        "pending_id": None,  # TODO(#29): the records table
        "updated": host_ts(stored.updated_at),
    }


def comment_object(store: Store, record: CommentRecord) -> dict[str, Any]:
    """One comment record (schemas/defs.json comment_record); the text is sanitized and cut."""
    if record.body_file is None:
        raise _tampered("a comment without its stored text")
    body = stored_body(store, record.pr, record.id)
    path = None if record.path is None else sanitize_line(record.path)[:MAX_PATH_CHARS]
    return {
        "id": record.id,
        "author_id": record.author_id,
        "eligible": record.eligible,
        "reason": record.reason,
        "state": record.state,
        "delivered_sha256": record.delivered_sha256,
        "delivered_from": record.delivered_from,
        "handled_reason": record.handled_reason,
        "first_seen_at": ts(record.first_seen_at),
        "edited": record.edited,
        "thread_id": record.thread_id,
        "path": path,
        "line": record.line,
        "body": body.text[:MAX_BODY_CHARS],
    }


def stored_prs(conn: sqlite3.Connection) -> list[StoredPr]:
    refs = [r[0] for r in conn.execute("SELECT pr FROM prs ORDER BY pr")]
    found = (get_pr(conn, ref) for ref in refs)
    return [row for row in found if row is not None]


def stored_prs_by_ref(conn: sqlite3.Connection, pr: str) -> StoredPr:
    found = get_pr(conn, pr)
    if found is None:
        raise CliError("not_found", "no stored PR matches")
    return found


def comment_records(store: Store, pr: str) -> list[CommentRecord]:
    return read_records(store.conn, pr)
