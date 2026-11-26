"""Detection (docs/interface.md: Detection): per-host listing, the request budget, the stored PR
state and the one per-PR comment read. Stage 1.

Every forgectl answer is untrusted: it is checked field by field against the pinned schema's
shape and bounds before anything is stored, and an answer that does not fit fails its host (a
listing) or its read (comments) and changes nothing. forgectl is only ever read here.

Owned by later issues, left as seams:

- Eligibility (`eligibility.py`) and the access cache are done here: `Detection.read_comments`
  returns one `Verdict` per comment (`CommentRead.verdicts`), and each uncached `forgectl access`
  lookup is charged `LOOKUP_COST` to the host's `Budget`, the one the reads are charged to.
- The comment records and the handled set are `handled.py` (#15): `HandledSet.record_read` is the
  `Detection(on_read=...)` hook; `absent_comment_ids` is the only rule about deletion (never from
  an incomplete read). `check.py` passes `HandledSet(...).record_read` as `on_read`, with the
  PR's lease held (`pr_scope`).
- The due rule, settle and the PR block live in `due.py`.
- The `check` command, `check --pr`, `all_hosts_failed` and the `pass` lease are `check.py`.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Collection, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Final

from . import deps
from . import eligibility as rules
from .audit import LogWriter
from .config import Config
from .durations import duration_ms
from .eligibility import LOOKUP_COST, Verdict
from .errors import CliError
from .gateway import Gateway, GatewayError, Result
from .store import Lease, cas_update, pr_digest

LISTING_LIMIT: Final = 1000  # forgectl's own maximum for `--limit`
MAX_COMMENT_BYTES_CHARS: Final = 1024 * 1024  # one comment body, in characters
MAX_COMMENTS_PER_READ: Final = 20_000
MAX_THREADS_PER_READ: Final = 20_000
MAX_WHOAMI_HOSTS: Final = 64

# The patterns of the pinned forgectl schemas (tests/pinned/forgectl/schemas/defs.json).
_HOST: Final = (
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?"
)
_REPO_RE: Final = re.compile(_HOST + r"(?:/(?!\.\.?(?:/|$))[A-Za-z0-9._-]+)+")
_HOST_RE: Final = re.compile(_HOST)
_SHA_RE: Final = re.compile(r"[0-9a-f]{40}")
_ACCOUNT_RE: Final = re.compile(r"[A-Za-z0-9:/_=-]{1,128}")
_ID_RE: Final = re.compile(
    r"gh:(?:ic|rc|rt|r):[A-Za-z0-9_=-]{1,100}|gl:d:[0-9a-f]{40}|gl:n:[1-9][0-9]{0,18}"
)
_TS_RE: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?"
    r"(?:[Zz]|[+-][0-9]{2}:[0-9]{2})"
)
_CODE_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
_PR_RE: Final = re.compile(_HOST + r"(?:/(?!\.\.?(?:/|#))[A-Za-z0-9._-]+)+#[1-9][0-9]{0,9}")
_FETCH_REF_RE: Final = re.compile(r"refs/(?:pull|merge-requests)/[1-9][0-9]{0,9}/head")
_TS_PARTS_RE: Final = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.[0-9]+)?"
    r"(?:[Zz]|[+-]([0-9]{2}):([0-9]{2}))"
)
_PR_STATES: Final = ("open", "closed", "merged")
_BACKENDS: Final = ("github", "gitlab")
_TOKEN_SOURCES: Final = ("token_env", "token_command", "stored_login", "none")
_SIDES: Final = ("left", "right", None)
_REVIEW_DECISIONS: Final = ("approved", "changes_requested", "review_required", "none")
_MERGE_METHODS: Final = ("squash", "merge", "rebase")
# defs.json#/$defs/error_code: the codes a host error entry may carry
_HOST_ERROR_CODES: Final = frozenset(
    {
        "internal", "usage", "config_invalid", "invalid_pr", "body_too_large", "method_required",
        "not_found", "vetoed", "auth_required", "permission_denied", "rate_limited",
        "upstream_error", "network_error", "tool_missing", "host_rejected", "outcome_unknown",
        "outcome_partial", "merge_unverified", "invalid_position", "quick_action_ran",
        "approved_stale_head", "wrong_host_response", "audit_unavailable", "audit_failed",
        "partial_results", "unsupported_host_version", "confirmation_required", "escalated",
        "refused",
    }
)  # fmt: skip
_TEXT_LIMIT: Final = 1 << 16
# one-line text may not carry a control character, C1, a line or paragraph separator, a zero-width
# space or directional mark, a bidirectional control, or the byte order mark. U+200C and U+200D
# (the joiners) stay allowed: emoji sequences and Persian text need them.
_UNSAFE_LINE_RE: Final = re.compile(
    "[\x00-\x1f\x7f\u0080-\u009f\u2028\u2029\u200b\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]"
)


class _Bad(Exception):
    """An answer that does not fit the pinned schema (becomes ``bad_output``)."""


class _HostFailed(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---- checked access to untrusted JSON -----------------------------------------------------


def _obj(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _Bad(f"{what} is not an object")
    return value  # pyright: ignore[reportUnknownVariableType]


def _array(value: object, what: str, limit: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > limit:  # pyright: ignore[reportUnknownArgumentType]
        raise _Bad(f"{what} is not a list of at most {limit}")
    return value  # pyright: ignore[reportUnknownVariableType]


def _bool(value: object, what: str) -> bool:
    if not isinstance(value, bool):
        raise _Bad(f"{what} is not a boolean")
    return value


def _opt_bool(value: object, what: str) -> bool | None:
    return None if value is None else _bool(value, what)


def _text(
    value: object, what: str, limit: int, *, one_line: bool = False, non_empty: bool = False
) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise _Bad(f"{what} is not text of at most {limit} characters")
    try:
        value.encode("utf-8")  # a lone surrogate would abort the SQLite bind and the JSON output
    except UnicodeEncodeError:
        raise _Bad(f"{what} is not valid UTF-8 text") from None
    if non_empty and not value:
        raise _Bad(f"{what} is empty")
    if one_line and _UNSAFE_LINE_RE.search(value):
        raise _Bad(f"{what} has a control character")
    return value


def _opt_text(
    value: object, what: str, limit: int, *, one_line: bool = False, non_empty: bool = False
) -> str | None:
    return (
        None if value is None else _text(value, what, limit, one_line=one_line, non_empty=non_empty)
    )


def _exact(value: object, keys: Collection[str], what: str) -> dict[str, Any]:
    """An object with exactly these members (the schema closes it and requires every one)."""
    doc = _obj(value, what)
    if set(doc) != set(keys):
        raise _Bad(f"{what} does not have exactly the members of its schema")
    return doc


def _int(
    value: object, what: str, *, minimum: int, maximum: int = 10**12, nullable: bool = False
) -> int | None:
    if value is None and nullable:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise _Bad(f"{what} is not an integer in range")
    return value


def _enum(value: object, allowed: Collection[object], what: str) -> Any:
    if not any(type(value) is type(a) and value == a for a in allowed):
        raise _Bad(f"{what} is not one of the allowed values")
    return value


def _ts(value: object, what: str) -> str:
    """An RFC 3339 instant: the schema's pattern and a real date, time and offset."""
    text = _match(value, _TS_RE, what)
    parts = _TS_PARTS_RE.fullmatch(text)
    if parts is None:
        raise _Bad(f"{what} has the wrong form")
    year, month, day, hour, minute, second = (int(p) for p in parts.groups()[:6])
    off_h, off_m = (int(p) if p is not None else 0 for p in parts.groups()[6:])
    try:
        datetime(year, month, day, hour, minute, min(second, 59))
    except ValueError:
        raise _Bad(f"{what} is not a real instant") from None
    if second > 60 or off_h > 23 or off_m > 59:
        raise _Bad(f"{what} is not a real instant")
    return text


def _opt_ts(value: object, what: str) -> str | None:
    return None if value is None else _ts(value, what)


def _url(value: object, what: str) -> str:
    text = _text(value, what, 8192, one_line=True)
    if not text.startswith("https://") or len(text) < 9:
        raise _Bad(f"{what} is not an https URL")
    return text


def _login(value: object, what: str) -> str | None:
    return _opt_text(value, what, 255, one_line=True, non_empty=True)


def _labels(value: object, what: str) -> None:
    for index, label in enumerate(_array(value, what, 1000)):
        _text(label, f"{what}[{index}]", 1024, one_line=True)


def _author(value: object, what: str) -> tuple[str | None, str | None, bool | None]:
    """(id, login, is_bot) of an `author` object."""
    author = _exact(value, ("login", "id", "is_bot"), what)
    return (
        _opt_match(author.get("id"), _ACCOUNT_RE, f"{what}.id"),
        _login(author.get("login"), f"{what}.login"),
        _opt_bool(author.get("is_bot"), f"{what}.is_bot"),
    )


def _host_errors(value: object, what: str) -> list[tuple[str, str]]:
    """(host, code) of each entry of an `errors` array (`host_error` objects)."""
    out: list[tuple[str, str]] = []
    for index, raw in enumerate(_array(value, what, MAX_WHOAMI_HOSTS)):
        entry = _exact(raw, ("host", "code", "message"), f"{what}[{index}]")
        host = _match(entry.get("host"), _HOST_RE, f"{what}[{index}].host")
        code = _enum(entry.get("code"), sorted(_HOST_ERROR_CODES), f"{what}[{index}].code")
        _text(entry.get("message"), f"{what}[{index}].message", _TEXT_LIMIT)
        out.append((host, code))
    return out


def _match(value: object, pattern: re.Pattern[str], what: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise _Bad(f"{what} has the wrong form")
    return value


def _opt_match(value: object, pattern: re.Pattern[str], what: str) -> str | None:
    return None if value is None else _match(value, pattern, what)


# ---- forgectl answers ---------------------------------------------------------------------


def _error_code(doc: object) -> str | None:
    """The `error.code` of an envelope when it is a plain name; "bad_output" when it is not;
    None when there is no envelope."""
    if not isinstance(doc, dict) or not isinstance(doc.get("error"), dict):
        return None
    code = doc["error"].get("code")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return code if isinstance(code, str) and _CODE_RE.fullmatch(code) else "bad_output"


def _first_host_error(doc: object) -> str | None:
    """The code of the first host error of a `prs` or `whoami` answer, if there is one."""
    if not isinstance(doc, dict):
        return None
    errors = doc.get("errors")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
    if not isinstance(errors, list) or not errors:
        return None
    first = errors[0]  # pyright: ignore[reportUnknownVariableType]
    code = first.get("code") if isinstance(first, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return code if isinstance(code, str) and code in _HOST_ERROR_CODES else "bad_output"


def _failure_code(result: Result) -> str | None:
    """Why a forgectl run did not give a usable answer (a plain code), or None when it did."""
    if result.timed_out:
        return "timeout"
    if result.overflow:
        return "output_too_large"
    envelope = _error_code(result.json)
    if result.returncode != 0:
        if envelope is not None:
            return envelope
        partial = _first_host_error(result.json)
        if partial is not None:
            return partial
        return f"exit_{result.returncode}" if result.json is None else "bad_output"
    if not isinstance(result.json, dict):
        return "bad_output"
    return envelope or _first_host_error(result.json)


def _lower(result: Result, code: str) -> dict[str, Any]:
    """The `lower_error` for a failed forgectl run: its own error object when it printed one."""
    if isinstance(result.json, dict) and isinstance(result.json.get("error"), dict):  # pyright: ignore[reportUnknownMemberType]
        return dict(result.json["error"])  # pyright: ignore[reportUnknownArgumentType]
    return {"code": code, "message": f"forgectl {_name(result)} did not give a usable answer"}


def _name(result: Result) -> str:
    return result.argv[1] if len(result.argv) > 1 else "forgectl"


@dataclass(frozen=True)
class PrFacts:
    """What a listing item or a `show` answer says about a PR."""

    pr: str
    host: str
    repo: str
    number: int
    state: str
    head_sha: str
    head_ref: str
    head_repo: str | None
    is_fork: bool
    base_ref: str
    created_at: str
    updated_at: str
    author_id: str | None = None


_ITEM_KEYS: Final = (
    "pr", "pr_id", "repo", "number", "url", "title", "author", "state", "draft", "labels",
    "head", "base", "created_at", "updated_at", "repo_id",
)  # fmt: skip
_SHOW_KEYS: Final = (
    "pr", "repo", "repo_id", "pr_id", "number", "url", "backend", "title", "body", "author",
    "state", "draft", "created_at", "updated_at", "closed_at", "merged_at", "merged_by", "labels",
    "author_association", "head", "base", "merged_head_sha", "merge_commit_sha", "head_fetch_ref",
    "mergeable", "blocked_by", "auto_merge", "merge_queue", "merge_methods_allowed", "additions",
    "deletions", "changed_files", "review_decision", "approvals", "approvals_at_head",
    "approvals_left",
)  # fmt: skip


def _parse_facts(raw: object, what: str, *, show: bool = False) -> PrFacts:
    """A listing item (`show=False`) or a `show` answer, each with exactly its schema's members."""
    item = _exact(raw, _SHOW_KEYS if show else _ITEM_KEYS, what)
    repo = _match(item.get("repo"), _REPO_RE, f"{what}.repo")
    number = _int(item.get("number"), f"{what}.number", minimum=1, maximum=9_999_999_999)
    pr = _match(item.get("pr"), _PR_RE, f"{what}.pr")
    if pr != f"{repo}#{number}":  # also keeps the host part of the key honest
        raise _Bad(f"{what}.pr does not name its repo and number")
    _match(item.get("pr_id"), _ACCOUNT_RE, f"{what}.pr_id")
    _match(item.get("repo_id"), _ACCOUNT_RE, f"{what}.repo_id")
    _url(item.get("url"), f"{what}.url")
    _text(item.get("title"), f"{what}.title", _TEXT_LIMIT, one_line=True)
    author_id, _login_name, _is_bot = _author(item.get("author"), f"{what}.author")
    state = _enum(item.get("state"), _PR_STATES, f"{what}.state")
    _bool(item.get("draft"), f"{what}.draft")
    _labels(item.get("labels"), f"{what}.labels")
    created_at = _ts(item.get("created_at"), f"{what}.created_at")
    updated_at = _ts(item.get("updated_at"), f"{what}.updated_at")
    head = _exact(item.get("head"), ("sha", "ref", "repo", "is_fork"), f"{what}.head")
    head_repo = _opt_match(head.get("repo"), _REPO_RE, f"{what}.head.repo")
    is_fork = _opt_bool(head.get("is_fork"), f"{what}.head.is_fork")
    base_keys = ("ref", "sha", "is_default") if show else ("ref",)
    base = _exact(item.get("base"), base_keys, f"{what}.base")
    if show:
        _opt_match(base.get("sha"), _SHA_RE, f"{what}.base.sha")
        _opt_bool(base.get("is_default"), f"{what}.base.is_default")
        _check_show_rest(item, state, what)
    return PrFacts(
        pr=pr,
        host=repo.split("/", 1)[0],
        repo=repo,
        number=number or 0,
        state=str(state),
        head_sha=_match(head.get("sha"), _SHA_RE, f"{what}.head.sha"),
        head_ref=_text(head.get("ref"), f"{what}.head.ref", 255, one_line=True, non_empty=True),
        head_repo=head_repo,
        # a null is_fork, a deleted head project and another head repo all count as a fork
        is_fork=is_fork is not False or head_repo != repo,
        base_ref=_text(base.get("ref"), f"{what}.base.ref", 255, one_line=True, non_empty=True),
        created_at=created_at,
        updated_at=updated_at,
        author_id=author_id,
    )


def _check_show_rest(item: dict[str, Any], state: str, what: str) -> None:
    """The members only `show` carries, and the rules tying them to `state`."""
    _enum(item.get("backend"), _BACKENDS, f"{what}.backend")
    _opt_text(item.get("body"), f"{what}.body", MAX_COMMENT_BYTES_CHARS)
    closed_at = _opt_ts(item.get("closed_at"), f"{what}.closed_at")
    merged_at = _opt_ts(item.get("merged_at"), f"{what}.merged_at")
    merged_by = _login(item.get("merged_by"), f"{what}.merged_by")
    _opt_text(item.get("author_association"), f"{what}.author_association", 255)
    merged_head = _opt_match(item.get("merged_head_sha"), _SHA_RE, f"{what}.merged_head_sha")
    merge_commit = _opt_match(item.get("merge_commit_sha"), _SHA_RE, f"{what}.merge_commit_sha")
    _match(item.get("head_fetch_ref"), _FETCH_REF_RE, f"{what}.head_fetch_ref")
    _opt_bool(item.get("mergeable"), f"{what}.mergeable")
    for index, blocker in enumerate(_array(item.get("blocked_by"), f"{what}.blocked_by", 1000)):
        _obj(blocker, f"{what}.blocked_by[{index}]")
    auto = _exact(item.get("auto_merge"), ("enabled", "by"), f"{what}.auto_merge")
    auto_enabled = _bool(auto.get("enabled"), f"{what}.auto_merge.enabled")
    _login(auto.get("by"), f"{what}.auto_merge.by")
    _opt_bool(item.get("merge_queue"), f"{what}.merge_queue")
    methods = item.get("merge_methods_allowed")
    if methods is not None:
        listed = _array(methods, f"{what}.merge_methods_allowed", len(_MERGE_METHODS))
        for method in listed:
            _enum(method, _MERGE_METHODS, f"{what}.merge_methods_allowed")
        if len(set(listed)) != len(listed):
            raise _Bad(f"{what}.merge_methods_allowed has a duplicate")
    for name in ("additions", "deletions", "changed_files", "approvals_at_head", "approvals_left"):
        _int(item.get(name), f"{what}.{name}", minimum=0, nullable=True)
    _int(item.get("approvals"), f"{what}.approvals", minimum=0)
    _enum(item.get("review_decision"), _REVIEW_DECISIONS, f"{what}.review_decision")
    if state == "open" and closed_at is not None:
        raise _Bad(f"{what}.closed_at is set on an open PR")
    if state == "merged":
        if merged_at is None or auto_enabled:
            raise _Bad(f"{what} is merged without merged_at, or with auto-merge on")
    elif any(v is not None for v in (merged_at, merged_by, merged_head, merge_commit)):
        raise _Bad(f"{what} has merge facts but is not merged")


def _parse_listing(
    result: Result, host: str, account: str | None = None
) -> tuple[list[PrFacts], bool]:
    code = _failure_code(result)
    if code is not None:
        raise _HostFailed(code)
    try:
        doc = _exact(
            result.json, ("items", "complete", "errors", "hosts_queried", "rate_limit"), "listing"
        )
        items = _array(doc.get("items"), "items", LISTING_LIMIT)
        complete = _bool(doc.get("complete"), "complete")
        _host_errors(doc.get("errors"), "errors")
        for index, queried in enumerate(_array(doc.get("hosts_queried"), "hosts_queried", 64)):
            _match(queried, _HOST_RE, f"hosts_queried[{index}]")
        limit = _exact(doc.get("rate_limit"), ("host", "remaining", "reset_at"), "rate_limit")
        _opt_match(limit.get("host"), _HOST_RE, "rate_limit.host")
        _int(limit.get("remaining"), "rate_limit.remaining", minimum=0, nullable=True)
        _opt_ts(limit.get("reset_at"), "rate_limit.reset_at")
        facts: dict[str, PrFacts] = {}
        for index, raw in enumerate(items):
            parsed = _parse_facts(raw, f"items[{index}]")
            if parsed.host != host:
                raise _Bad("an item belongs to another host")
            if parsed.state != "open":
                raise _Bad("a listing of open PRs has a PR that is not open")
            if account is not None and parsed.author_id != account:
                raise _Bad("an item of `--author me` is not yours")
            facts.setdefault(parsed.pr, parsed)
        # exactly the limit means older items may have been cut, whatever `complete` says
        if len(items) >= LISTING_LIMIT:
            complete = False
    except _Bad:
        raise _HostFailed("bad_output") from None
    return list(facts.values()), complete


# ---- identity -----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Whoami:
    accounts: Mapping[str, str | None]  # host -> account id (None: the host gave none)
    credentialed: tuple[str, ...]  # hosts with a credential, in forgectl's order
    errors: Mapping[str, str]  # host -> the code forgectl reported for it


def _parse_whoami(result: Result) -> _Whoami:
    doc = _exact(result.json, ("hosts", "errors"), "whoami")
    hosts = _array(doc.get("hosts"), "hosts", MAX_WHOAMI_HOSTS)
    accounts: dict[str, str | None] = {}
    credentialed: list[str] = []
    for index, raw in enumerate(hosts):
        entry = _exact(
            raw,
            ("host", "backend", "login", "id", "me", "me_matches_login", "token_source", "scopes"),
            f"hosts[{index}]",
        )
        host = _match(entry.get("host"), _HOST_RE, "host")
        if host in accounts:  # a repeated host would let the later entry forge the account id
            raise _Bad("a host is listed twice")
        _enum(entry.get("backend"), _BACKENDS, "backend")
        _login(entry.get("login"), "login")
        accounts[host] = _opt_match(entry.get("id"), _ACCOUNT_RE, "id")
        _login(entry.get("me"), "me")
        _opt_bool(entry.get("me_matches_login"), "me_matches_login")
        source = _enum(entry.get("token_source"), _TOKEN_SOURCES, "token_source")
        scopes = entry.get("scopes")
        if scopes is not None:
            for scope in _array(scopes, "scopes", 1000):
                _text(scope, "scope", 1024)
        if source != "none":
            credentialed.append(host)
    failed: dict[str, str] = {}
    for host, code in _host_errors(doc.get("errors"), "errors"):
        if host in failed or host in accounts:
            raise _Bad("a host is listed twice")
        failed[host] = code
    return _Whoami(accounts, tuple(credentialed), failed)


# ---- comments -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadComment:
    """One comment of a read, flattened with the facts of its thread. Text is untrusted."""

    id: str
    thread_id: str
    author_id: str | None
    author_login: str | None
    author_is_bot: bool | None  # None stays None: eligibility counts it as a bot
    body: str
    created_at: str
    updated_at: str
    minimized: bool
    reply_to: str | None
    thread_resolved: bool
    thread_resolvable: bool
    thread_outdated: bool | None
    path: str | None
    line: int | None
    side: str | None = None  # the thread's side (left or right); only the prompt shows it


@dataclass(frozen=True)
class CommentRead:
    pr: str
    head_sha: str
    complete: bool  # False: part of the PR could not be read; nothing may be marked deleted
    comments: tuple[ReadComment, ...]
    # comment id -> eligibility (Eligibility); empty on a read that was not judged yet
    verdicts: Mapping[str, Verdict] = field(default_factory=dict[str, Verdict])


_THREAD_KEYS: Final = (
    "id", "resolvable", "resolved", "outdated", "path", "line", "side", "start_line",
    "commit_sha", "comments",
)  # fmt: skip
_COMMENT_KEYS: Final = (
    "id", "author", "body", "created_at", "updated_at", "minimized", "reply_to", "review_id",
)  # fmt: skip


def _parse_comments(result: Result, pr: str) -> CommentRead:
    doc = _exact(result.json, ("pr", "head_sha", "threads", "complete"), "comments")
    if _match(doc.get("pr"), _PR_RE, "pr") != pr:
        raise _Bad("the answer is for another PR")
    head_sha = _match(doc.get("head_sha"), _SHA_RE, "head_sha")
    complete = _bool(doc.get("complete"), "complete")
    threads = _array(doc.get("threads"), "threads", MAX_THREADS_PER_READ)
    seen: set[str] = set()
    out: list[ReadComment] = []
    for t_index, raw_thread in enumerate(threads):
        thread = _exact(raw_thread, _THREAD_KEYS, f"threads[{t_index}]")
        thread_id = _match(thread.get("id"), _ID_RE, "thread id")
        resolvable = _bool(thread.get("resolvable"), "resolvable")
        resolved = _bool(thread.get("resolved"), "resolved")
        if resolved and not resolvable:
            raise _Bad("a thread that cannot be resolved is resolved")
        outdated = _opt_bool(thread.get("outdated"), "outdated")
        path = _opt_text(thread.get("path"), "path", 4096, one_line=True)
        line = _int(thread.get("line"), "line", minimum=1, maximum=10**9, nullable=True)
        side: str | None = _enum(thread.get("side"), _SIDES, "side")
        _int(thread.get("start_line"), "start_line", minimum=1, maximum=10**9, nullable=True)
        _opt_match(thread.get("commit_sha"), _SHA_RE, "commit_sha")
        comments = _array(thread.get("comments"), "comments", MAX_COMMENTS_PER_READ)
        if not comments:
            raise _Bad("a thread has no comments")
        for raw in comments:
            comment = _exact(raw, _COMMENT_KEYS, "comment")
            comment_id = _match(comment.get("id"), _ID_RE, "comment id")
            if comment_id in seen or len(seen) >= MAX_COMMENTS_PER_READ:
                raise _Bad("duplicate or too many comments")
            seen.add(comment_id)
            author_id, author_login, author_is_bot = _author(comment.get("author"), "author")
            _opt_match(comment.get("review_id"), _ID_RE, "review_id")
            out.append(
                ReadComment(
                    id=comment_id,
                    thread_id=thread_id,
                    author_id=author_id,
                    author_login=author_login,
                    author_is_bot=author_is_bot,
                    body=_text(comment.get("body"), "body", MAX_COMMENT_BYTES_CHARS),
                    created_at=_ts(comment.get("created_at"), "created_at"),
                    updated_at=_ts(comment.get("updated_at"), "updated_at"),
                    minimized=_bool(comment.get("minimized"), "minimized"),
                    reply_to=_opt_match(comment.get("reply_to"), _ID_RE, "reply_to"),
                    thread_resolved=resolved,
                    thread_resolvable=resolvable,
                    thread_outdated=outdated,
                    path=path,
                    line=line,
                    side=side,
                )
            )
    return CommentRead(pr, head_sha, complete, tuple(out))


_ACCESS_KEYS: Final = ("repo", "user", "write_access", "level", "source", "reason")
_LEVELS: Final = ("admin", "maintain", "write", "triage", "read", "none", None)
_ACCESS_SOURCES: Final = ("collaborator_permission", "project_member")


def _parse_access(result: Result, repo: str) -> bool | None:
    """`write_access` of a `forgectl access` answer (null: unknown), checked against its pinned
    schema: exactly its members, the repo that was asked, a login-shaped `user`, enums, and a
    `reason` exactly when the answer is null."""
    doc = _exact(result.json, _ACCESS_KEYS, "access")
    if _match(doc.get("repo"), _REPO_RE, "access.repo") != repo:
        raise _Bad("the answer is for another repo")
    _match(doc.get("user"), rules.LOGIN_RE, "access.user")
    answer = _opt_bool(doc.get("write_access"), "access.write_access")
    _enum(doc.get("level"), _LEVELS, "access.level")
    _enum(doc.get("source"), _ACCESS_SOURCES, "access.source")
    reason = _enum(doc.get("reason"), ("no_permission_to_read", None), "access.reason")
    if (answer is None) != (reason is not None):
        raise _Bad("access.reason is set exactly when write_access is null")
    return answer


def absent_comment_ids(stored: Collection[str], read: CommentRead) -> set[str]:
    """The stored comment ids a read proves gone. Only a complete read proves anything: with
    ``complete: false`` the answer is empty and nothing may be marked deleted."""
    if not read.complete:
        return set()
    return set(stored) - {c.id for c in read.comments}


# ---- budget -------------------------------------------------------------------------------


def read_cost(stored_comments: int) -> int:
    """The estimated cost of one comment read: 1 plus 1 per 100 stored comments."""
    return 1 + max(0, stored_comments) // 100


def listing_cost(items: int) -> int:
    """The estimated cost of a listing: 1 per 100 items, at least 1."""
    return max(1, -(-items // 100))


class Budget:
    """One host's request budget for one pass: listing, shows, reads and access lookups are all
    charged here (``limits.requests_per_host``). The costs are estimates."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.spent = 0

    def fits(self, cost: int) -> bool:
        return self.spent + cost <= self.limit

    def spend(self, cost: int) -> None:
        self.spent += cost


# ---- stored PRs ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredPr:
    pr: str
    digest: str
    host: str
    repo: str
    number: int
    host_state: str
    head_sha: str
    head_ref: str
    head_repo: str | None
    is_fork: bool
    base_ref: str
    created_at: str
    updated_at: str
    last_comments_read_at: float | None
    last_comments_attempt_at: float | None
    read_failures: int
    comment_count: int
    rev: int


_COLUMNS: Final = (
    "pr, digest, host, repo, number, host_state, head_sha, head_ref, head_repo, is_fork,"
    " base_ref, created_at, updated_at, last_comments_read_at, last_comments_attempt_at,"
    " read_failures, comment_count, rev"
)


def get_pr(conn: sqlite3.Connection, pr: str) -> StoredPr | None:
    row = conn.execute(f"SELECT {_COLUMNS} FROM prs WHERE pr = ?", (pr,)).fetchone()
    if row is None:
        return None
    values = list(row)
    values[9] = bool(values[9])
    return StoredPr(*values)


_UPSERT: Final = """
INSERT INTO prs (pr, digest, host, repo, number, host_state, head_sha, head_ref, head_repo,
                 is_fork, base_ref, created_at, updated_at, first_seen_at, listed_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (pr) DO UPDATE SET
    head_sha = excluded.head_sha,
    head_ref = excluded.head_ref, head_repo = excluded.head_repo, is_fork = excluded.is_fork,
    base_ref = excluded.base_ref, updated_at = excluded.updated_at,
    listed_at = excluded.listed_at, rev = rev + 1
"""


def _upsert(conn: sqlite3.Connection, facts: PrFacts, now: float) -> None:
    """Store a PR's facts. ``host_state`` is never written by the upsert itself: a change of it
    goes through ``cas_update`` (the same rule as every other writer of that column)."""
    row = get_pr(conn, facts.pr)
    if row is not None and row.host_state != facts.state:
        # state and facts in one compare-and-set; the open transaction makes it the only writer
        changes = {
            "host_state": facts.state, "head_sha": facts.head_sha, "head_ref": facts.head_ref,
            "head_repo": facts.head_repo, "is_fork": int(facts.is_fork),
            "base_ref": facts.base_ref, "updated_at": facts.updated_at, "listed_at": now,
        }  # fmt: skip
        if not cas_update(conn, "prs", "pr", facts.pr, row.rev, changes):
            raise CliError("internal", "a stored PR changed under the pass lease")
        return
    conn.execute(
        _UPSERT,
        (
            facts.pr, pr_digest(facts.pr), facts.host, facts.repo, facts.number, facts.state,
            facts.head_sha, facts.head_ref, facts.head_repo, int(facts.is_fork), facts.base_ref,
            facts.created_at, facts.updated_at, now, now,
        ),
    )  # fmt: skip


# ---- results ------------------------------------------------------------------------------


@dataclass(frozen=True)
class HostResult:
    host: str
    ok: bool
    code: str | None = None  # why the host failed
    warnings: tuple[str, ...] = ()
    listed: tuple[str, ...] = ()  # every PR of the listing
    closed: tuple[str, ...] = ()  # PRs learned closed or merged this pass
    attempted: tuple[str, ...] = ()  # comment reads started, in order
    read: tuple[str, ...] = ()  # of those, the ones that succeeded
    failed: tuple[str, ...] = ()  # and the ones that did not
    deferred: tuple[str, ...] = ()  # PRs the budget did not reach (first next pass)
    held: tuple[str, ...] = ()  # PRs whose `pr:` lease another command held (`lease_held`)
    spent: int = 0  # estimated requests


@dataclass(frozen=True)
class PassDetection:
    hosts: tuple[HostResult, ...]

    @property
    def all_failed(self) -> bool:
        """No host could be read (`check` exits 12 `all_hosts_failed`)."""
        return bool(self.hosts) and not any(h.ok for h in self.hosts)


@dataclass(frozen=True)
class Refreshed:
    """The answer of ``refresh_pr``: the stored PR, and the author the host reported."""

    stored: StoredPr | None  # None: not stored because the PR is not yours
    author_id: str | None
    not_yours: bool
    # False: your account id is unknown, nothing was stored and ``not_yours`` means nothing; the
    # caller treats it as a failure (``identity_unknown``)
    account_known: bool = True
    # the failure code of the account resolution when ``account_known`` is false
    account_code: str | None = None
    # what `forgectl show` said (state, head, fork); None only when nothing was read
    facts: PrFacts | None = None


OnRead = Callable[[StoredPr, CommentRead], None]
# The scope of one PR's read: entered before the read, yields False when the PR's lease is held by
# another command (the PR is skipped, not charged to the budget and attempted first next pass).
PrScope = Callable[[str], AbstractContextManager[bool]]


class Detection:
    """One detection pass's state: the gateway, the held ``pass`` lease and the config.

    Every write goes through ``lease.transaction()``, so a lost lease aborts with ``lease_lost``.
    Columns only detection writes (listing facts, read stamps) are written with a plain UPDATE
    that bumps ``rev`` (a concurrent compare-and-set writer of another column then retries);
    a state change that depends on what was read (closed, merged) is a compare-and-set.
    """

    def __init__(
        self,
        gateway: Gateway,
        lease: Lease,
        settings: Config,
        *,
        log: LogWriter | None = None,
        on_read: OnRead | None = None,
        pr_scope: PrScope | None = None,
        started_at: datetime | None = None,
    ) -> None:
        self.gateway = gateway
        self.lease = lease
        self.settings = settings
        self.log = log
        self.on_read = on_read
        self.pr_scope: PrScope = pr_scope or (lambda _pr: nullcontext(True))
        self.started_at = started_at or gateway.clock.now()
        self._checked = False
        self._whoami: _Whoami | CliError | None = None
        self._pass_budgets: dict[str, Budget] = {}  # only while a host pass runs

    # -- plumbing ---------------------------------------------------------------------------

    def _now(self) -> float:
        return self.gateway.clock.now().timestamp()

    def _require(self) -> None:
        """Exit 12 `tool_missing` / `tool_too_old` before the first forgectl call (once)."""
        if not self._checked:
            deps.require(self.gateway, "forgectl")
            self._checked = True

    def _run(self, call: Callable[[], Result]) -> Result | str:
        """The result, or the code of a failure to run forgectl at all."""
        try:
            return call()
        except GatewayError:
            return "spawn_failed"

    # -- identity and hosts -----------------------------------------------------------------

    def _who(self) -> _Whoami:
        """`forgectl whoami`, asked once per pass; its failure is remembered too."""
        if self._whoami is None:
            self._whoami = self._ask_whoami()
        if isinstance(self._whoami, CliError):
            raise self._whoami
        return self._whoami

    def _ask_whoami(self) -> _Whoami | CliError:
        self._require()
        outcome = self._run(self.gateway.forgectl.whoami)
        if isinstance(outcome, str):
            return CliError(
                "lower_failed",
                "forgectl whoami could not run",
                lower={"code": outcome, "message": "forgectl whoami could not run"},
            )
        # exit 12 (partial_results) still lists the hosts that answered
        usable = outcome.returncode in (0, 12) and not outcome.timed_out and not outcome.overflow
        try:
            if not usable:
                raise _Bad("whoami failed")
            return _parse_whoami(outcome)
        except _Bad:
            code = _failure_code(outcome) or "bad_output"
            return CliError(
                "lower_failed",
                f"forgectl whoami failed ({code})",
                lower=_lower(outcome, code),
            )

    def hosts(self) -> list[str]:
        """`hosts_to_check`, or the hosts `forgectl whoami` lists with a credential followed by
        the ones it reports an error for (those are failed hosts of the pass, never silent)."""
        configured = self.settings.hosts_to_check
        if configured is not None:
            return list(dict.fromkeys(configured))
        who = self._who()
        return list(dict.fromkeys([*who.credentialed, *who.errors]))

    def _resolve_account(self, host: str) -> tuple[str | None, str | None]:
        """(account id, failure code): the configured id, else the one `whoami` reports."""
        configured = self.settings.me.accounts.get(host)
        if configured is not None:
            return configured, None
        try:
            who = self._who()
        except CliError as error:
            lower = error.lower or {}
            code = lower.get("code")
            return None, code if code != "unknown" and isinstance(code, str) else "whoami_failed"
        if host in who.errors:
            return None, who.errors[host]
        return (
            (who.accounts.get(host), None) if who.accounts.get(host) else (None, "identity_unknown")
        )

    def budget(self, host: str) -> Budget:
        """The host's budget of the running pass; a read outside a host pass gets a fresh one
        each time (a spent budget never carries over to a later read)."""
        return self._pass_budgets.get(host) or Budget(self.settings.limits.requests_per_host)

    def account_id(self, host: str) -> str | None:
        """Your account id on ``host`` (the comparison key of eligibility, #14)."""
        return self._resolve_account(host)[0]

    # -- one pass -----------------------------------------------------------------------------

    def run(self) -> PassDetection:
        """List and read every configured host; a failed host never stops the others."""
        self._require()
        return PassDetection(tuple(self.host_pass(host) for host in self.hosts()))

    def _fail(self, host: str, code: str) -> HostResult:
        if self.log is not None:
            self.log.emit("host_failed", {"host": host, "code": code}, level="warn")
        return HostResult(host=host, ok=False, code=code)

    def host_pass(self, host: str) -> HostResult:
        """One host's pass; text forgectl sent that cannot be stored fails the host."""
        try:
            return self._host_pass(host)
        except UnicodeEncodeError:  # defence in depth: the parsers already refuse such text
            return self._fail(host, "bad_output")
        finally:
            self._pass_budgets.pop(host, None)

    def _host_pass(self, host: str) -> HostResult:
        self._require()
        account, why = self._resolve_account(host)
        if account is None:
            return self._fail(host, why or "identity_unknown")
        listing = self._run(
            lambda: self.gateway.forgectl.prs(
                author="me", host=host, state="open", limit=LISTING_LIMIT
            )
        )
        try:
            if isinstance(listing, str):
                raise _HostFailed(listing)
            facts, complete = _parse_listing(listing, host, account)
        except _HostFailed as failure:
            return self._fail(host, failure.code)
        budget = self._pass_budgets[host] = Budget(self.settings.limits.requests_per_host)
        budget.spend(listing_cost(len(facts)))
        now = self._now()
        with self.lease.transaction() as conn:
            for item in facts:
                _upsert(conn, item, now)
        listed = tuple(item.pr for item in facts)
        closed: list[str] = []
        if complete:
            shown = 0
            for pr in self._unlisted_open(host, set(listed)):
                # like the first read, the first show of a pass always goes ahead: with a budget
                # the listing used up, no closure would ever be learned otherwise
                if shown and not budget.fits(1):
                    break  # the rest is learned next pass
                shown += 1
                budget.spend(1)
                if self._learn_closed(pr):
                    closed.append(pr)
        attempted: list[str] = []
        read: list[str] = []
        failed: list[str] = []
        deferred: list[str] = []
        held: list[str] = []
        work = self._work(host, set(listed))
        for index, (pr, count) in enumerate(work):
            cost = read_cost(count)
            # the first read of a pass always goes ahead: a PR whose estimate exceeds the whole
            # budget (or a budget the listing used up) would otherwise never be read
            if attempted and not budget.fits(cost):
                deferred = [p for p, _ in work[index:]]
                break
            with self.pr_scope(pr) as free:
                if not free:  # another command holds the PR: skipped, not charged
                    held.append(pr)
                    continue
                budget.spend(cost)
                attempted.append(pr)
                try:
                    self.read_comments(pr)
                except CliError as error:
                    if error.code != "lower_failed":
                        raise
                    failed.append(pr)
                else:
                    read.append(pr)
        return HostResult(
            host=host,
            ok=True,
            listed=listed,
            closed=tuple(closed),
            attempted=tuple(attempted),
            read=tuple(read),
            failed=tuple(failed),
            deferred=tuple(deferred),
            held=tuple(held),
            spent=budget.spent,
        )

    def _unlisted_open(self, host: str, listed: set[str]) -> list[str]:
        rows = self.lease.store.conn.execute(
            "SELECT pr FROM prs WHERE host = ? AND host_state = 'open'"
            " ORDER BY last_show_attempt_at IS NOT NULL, last_show_attempt_at, pr",
            (host,),
        )
        return [pr for (pr,) in rows if pr not in listed]

    def _work(self, host: str, listed: set[str]) -> list[tuple[str, int]]:
        """The listed non-fork open PRs, never attempted first, then least recently attempted."""
        rows = self.lease.store.conn.execute(
            "SELECT pr, comment_count FROM prs WHERE host = ? AND host_state = 'open'"
            " AND is_fork = 0"
            " ORDER BY last_comments_attempt_at IS NOT NULL, last_comments_attempt_at, pr",
            (host,),
        )
        return [(pr, count) for pr, count in rows if pr in listed]

    def _learn_closed(self, pr: str) -> bool:
        """`forgectl show` for a stored open PR that was not listed. Only the host's `closed` or
        `merged` counts: a failure, `not_found` and `open` all leave the PR as it was."""
        with (
            self.lease.transaction() as conn
        ):  # stamped first, so a failing PR cannot hold the front
            conn.execute(
                "UPDATE prs SET last_show_attempt_at = ?, rev = rev + 1 WHERE pr = ?",
                (self._now(), pr),
            )
        outcome = self._run(lambda: self.gateway.forgectl.show(pr))
        if isinstance(outcome, str) or _failure_code(outcome) is not None:
            return False
        try:
            facts = _parse_facts(outcome.json, "show", show=True)
        except _Bad:
            return False
        if facts.pr != pr or facts.state == "open":
            return False
        with self.lease.transaction() as conn:
            row = get_pr(conn, pr)
            if row is None or row.host_state != "open":
                return False
            return cas_update(
                conn, "prs", "pr", pr, row.rev,
                {"host_state": facts.state, "updated_at": facts.updated_at},
            )  # fmt: skip

    # -- the per-PR reads -------------------------------------------------------------------

    def refresh_pr(self, pr: str) -> Refreshed:
        """`forgectl show` for one PR, stored like a listing item (`check --pr`, `respond`).

        A PR whose author is not your account is not stored: the result has ``stored=None`` and
        ``not_yours`` true, so the caller can refuse it. When your account id is unknown nothing is
        stored either: ``stored=None`` and ``account_known`` false (``not_yours`` is then
        meaningless and false); the caller must fail with ``identity_unknown``.
        """
        self._require()
        outcome = self._run(lambda: self.gateway.forgectl.show(pr))
        code = outcome if isinstance(outcome, str) else _failure_code(outcome)
        facts: PrFacts | None = None
        if code is None and not isinstance(outcome, str):
            try:
                facts = _parse_facts(outcome.json, "show", show=True)
                if facts.pr != pr:
                    raise _Bad("the answer is for another PR")
            except _Bad:
                code = "bad_output"
        if code is not None or facts is None:
            lower = (
                _lower(outcome, code or "bad_output")
                if not isinstance(outcome, str)
                else {"code": outcome}
            )
            raise CliError("lower_failed", f"forgectl show failed ({code})", lower=lower)
        account, account_code = self._resolve_account(facts.host)
        if account is None:
            return Refreshed(
                stored=None,
                author_id=facts.author_id,
                not_yours=False,
                account_known=False,
                account_code=account_code or "identity_unknown",
                facts=facts,
            )
        if facts.author_id != account:
            return Refreshed(stored=None, author_id=facts.author_id, not_yours=True, facts=facts)
        with self.lease.transaction() as conn:
            _upsert(conn, facts, self._now())
            stored = get_pr(conn, pr)
        if stored is None:  # cannot happen: the upsert just ran
            raise CliError("internal", "a stored PR vanished")
        return Refreshed(stored=stored, author_id=facts.author_id, not_yours=False, facts=facts)

    def _ensure_installed_at(self) -> None:
        stamp = self.started_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.lease.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('installed_at', ?)", (stamp,)
            )

    def read_comments(self, pr: str) -> CommentRead:
        """The one per-PR read (check, check --pr, respond and the job).

        Refused (``refused``) before any forgectl call for a fork PR or a PR stored as closed or
        merged. Stamps the attempt first, so a failing PR cannot keep the front of the queue.
        Failure (``lower_failed``) changes nothing but the failure count; an incomplete read is
        returned as it is, marks nothing deleted (``absent_comment_ids``) and counts as no read
        (the stored count only grows, the failures and the read time stay).
        """
        with self.lease.transaction() as conn:
            row = get_pr(conn, pr)
        if row is None:
            raise CliError("internal", "a comment read of a PR that is not stored")
        # refused before any forgectl call: such comments are never read (Eligibility 4)
        if row.is_fork:
            raise CliError(
                "refused",
                "the comments of a fork PR are never read",
                reason="ineligible",
                details={"reason": "fork_pr"},
            )
        if row.host_state != "open":
            raise CliError("refused", "the PR is closed or merged", reason="pr_closed")
        self._require()
        account = self._account_for_judging(row.host)
        self._ensure_installed_at()
        with self.lease.transaction() as conn:
            conn.execute(
                "UPDATE prs SET last_comments_attempt_at = ?, rev = rev + 1 WHERE pr = ?",
                (self._now(), pr),
            )
        outcome = self._run(lambda: self.gateway.forgectl.comments(pr))
        code = outcome if isinstance(outcome, str) else _failure_code(outcome)
        read: CommentRead | None = None
        if code is None and not isinstance(outcome, str):
            try:
                read = _parse_comments(outcome, pr)
            except _Bad:
                code = "bad_output"
        if read is None:
            self._read_failed(pr)
            lower = (
                _lower(outcome, code or "bad_output")
                if not isinstance(outcome, str)
                else {"code": outcome}
            )
            if code == "bad_output" and not isinstance(outcome, str):
                lower = {"code": "bad_output", "message": "forgectl comments output does not fit"}
            raise CliError("lower_failed", f"forgectl comments failed ({code})", lower=lower)
        with self.lease.transaction() as conn:
            if read.complete:
                conn.execute(
                    "UPDATE prs SET last_comments_read_at = ?, read_failures = 0,"
                    " records_current = 0, comment_count = ?, rev = rev + 1 WHERE pr = ?",
                    (self._now(), len(read.comments), pr),
                )
            else:  # part of the PR was not read: the count only grows, nothing counts as read
                conn.execute(
                    "UPDATE prs SET comment_count = MAX(comment_count, ?), rev = rev + 1"
                    " WHERE pr = ?",
                    (len(read.comments), pr),
                )
            stored = get_pr(conn, pr)
        read = replace(read, verdicts=self._judge(row, read, account))
        # last_comments_read_at was stored above, before on_read records the comments, so it is
        # not proof the records are current: records_current is 0 until HandledSet.record_read
        # applies them (due.py reads that flag).
        if self.on_read is not None and stored is not None:
            self.on_read(stored, read)
        return read

    # -- eligibility ------------------------------------------------------------------------

    def _account_for_judging(self, host: str) -> str:
        """Your account id: without it `own` cannot be told, so the read fails (nothing changes)."""
        account, why = self._resolve_account(host)
        if account is None:
            code = why or "identity_unknown"
            raise CliError(
                "lower_failed",
                f"your account id on {host} is unknown ({code})",
                lower={"code": code, "message": "your account id is unknown"},
            )
        return account

    def _judge(self, pr: StoredPr, read: CommentRead, account: str) -> dict[str, Verdict]:
        """One verdict per comment of a read (Eligibility). The PR itself is open, yours and not a
        fork here: `read_comments` refuses any other before it reads."""
        repo_settings = self.settings.repo_settings(pr.repo)
        reviewers = rules.entry_ids(repo_settings.reviewers, pr.host)
        bot_ids = rules.entry_ids(self.settings.bot_authors, pr.host)
        prefix = self.settings.respond_to_own_comments_prefix
        asked: dict[tuple[str, str], bool | None] = {}  # one lookup per author and login
        budget = self.budget(pr.host)  # shared by every lookup of this read
        out: dict[str, Verdict] = {}
        for comment in read.comments:
            verdict = rules.prejudge(
                author_id=comment.author_id,
                is_bot=comment.author_is_bot,
                body=comment.body,
                resolved=comment.thread_resolved,
                minimized=comment.minimized,
                account_id=account,
                bot_ids=bot_ids,
                own_prefix=prefix,
            )
            if verdict is None:
                verdict = self._write_access(pr, comment, reviewers, asked, budget)
            out[comment.id] = verdict
        return out

    def _write_access(
        self,
        pr: StoredPr,
        comment: ReadComment,
        reviewers: frozenset[str],
        asked: dict[tuple[str, str], bool | None],
        budget: Budget,
    ) -> Verdict:
        author_id, login = comment.author_id, comment.author_login
        if author_id is None:  # cannot happen after `prejudge`; kept for the type
            return rules.ineligible("unknown_author")
        if author_id in reviewers:
            return rules.ELIGIBLE
        # a login that is not shaped like one is never passed to forgectl (it would be argv)
        if login is None or rules.ARGV_LOGIN_RE.fullmatch(login) is None:
            return rules.ineligible("unknown_access")
        key = (author_id, login)
        if key not in asked:
            asked[key] = self._lookup_access(pr, author_id, login, budget)
        answer = asked[key]
        if answer is None:
            return rules.ineligible("unknown_access")
        return rules.ELIGIBLE if answer else rules.ineligible("no_access")

    def _lookup_access(
        self, pr: StoredPr, author_id: str, login: str, budget: Budget
    ) -> bool | None:
        """`write_access` of an author: from the cache, else from `forgectl access` (charged
        `LOOKUP_COST` to the host's budget, whether or not it works). None is the unknown answer:
        a null answer, a failed or misfit lookup, or no budget left. Only an answer that fits its
        schema is cached."""
        ttl = duration_ms(self.settings.access_cache_ttl) / 1000
        now = self._now()
        hit, cached = rules.cache_get(
            self.lease.store.conn, pr.repo, author_id, login, now=now, ttl=ttl
        )
        if hit:
            return cached
        if not budget.fits(LOOKUP_COST):
            return None
        budget.spend(LOOKUP_COST)
        outcome = self._run(lambda: self.gateway.forgectl.access(pr.repo, login))
        if isinstance(outcome, str) or _failure_code(outcome) is not None:
            return None
        try:
            answer = _parse_access(outcome, pr.repo)
        except _Bad:
            return None
        with self.lease.transaction() as conn:
            rules.cache_put(conn, pr.repo, author_id, login, answer, now=now)
        return answer

    def _read_failed(self, pr: str) -> None:
        with self.lease.transaction() as conn:
            conn.execute(
                "UPDATE prs SET read_failures = read_failures + 1, rev = rev + 1 WHERE pr = ?",
                (pr,),
            )
