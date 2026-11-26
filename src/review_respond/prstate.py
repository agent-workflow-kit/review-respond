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

from . import due, rounds
from .audit import format_ts
from .config import Config
from .detect import StoredPr, get_pr
from .errors import CliError
from .handled import CommentRecord, parse_instant, read_records, stored_body
from .output import sanitize_line
from .store import Store

MAX_BODY_CHARS: Final = 65536  # schemas/defs.json comment_record.body
MAX_PATH_CHARS: Final = 4096
MAX_UNTRUSTED_CHARS: Final = 4096  # schemas/defs.json round.escalation reason and summary


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
    """The derived state of a stored PR. `RoundFacts` come from the rounds table; TODO(#29): the
    open record comes from the records table; TODO(#19): `agent_busy`."""
    inputs = due.load_inputs(conn, config, pr, now=now, facts=rounds.round_facts(conn, pr))
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
        "rounds": rounds.count_rounds(conn, stored.pr),
        **counts(conn, stored.pr),
        "pending_id": None,  # TODO(#29): the records table
        "updated": host_ts(stored.updated_at),
    }


def _untrusted(text: str | None) -> str | None:
    """Agent-written text, sanitized (a newline stays a newline) and then cut to at most 4096
    characters, never inside a ``<U+XXXX>`` marker."""
    if text is None:
        return None
    out: list[str] = []
    size = 0
    for ch in text:
        piece = ch if ch == "\n" else sanitize_line(ch)
        if size + len(piece) > MAX_UNTRUSTED_CHARS:
            break
        out.append(piece)
        size += len(piece)
    return "".join(out)


def round_object(conn: sqlite3.Connection, row: rounds.RoundRow) -> dict[str, Any]:
    """One round (schemas/defs.json round). ``comment_ids`` are the comments the job selected;
    ``delivered`` those the round holds now (delivered, or handled when it went pending)."""
    delivered = [
        r[0]
        for r in conn.execute(
            "SELECT comment_id FROM comments WHERE pr = ? AND round = ?"
            " AND state IN ('delivered', 'handled') ORDER BY comment_id",
            (row.pr, row.id),
        )
    ]
    escalation = None
    if row.state == "escalated":
        escalation = {
            "reason": _untrusted(row.escalation_reason),
            "summary": _untrusted(row.escalation_summary),
        }
    failure = (
        None
        if row.failure_reason is None
        else {"reason": row.failure_reason, "detail": row.failure_detail}
    )
    flagged = row.cancel_requested_at
    return {
        "id": row.id,
        "pr": row.pr,
        "number": row.number,
        "state": row.state,
        "head_sha": row.head_sha,
        "agent_id": row.agent_id,
        "job_id": row.job_id,
        "base_agent_id": row.base_agent_id,
        "comment_ids": list(row.comment_ids),
        "delivered": delivered,
        "counted": row.counted,
        "cancel_requested_at": None if flagged is None else ts(flagged),
        "cancel_reason": row.cancel_reason,
        "note_sha256": row.note_sha256,
        "include_existing": row.include_existing,
        "escalation": escalation,
        "failure": failure,
        "started": None if row.started_at is None else ts(row.started_at),
        "ended": None if row.ended_at is None else ts(row.ended_at),
        "warnings": list(row.warnings),
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
