"""Detection (docs/interface.md: Detection): per-host listing, the request budget, the stored PR
state and the one per-PR comment read. Stage 1.

Every forgectl answer is untrusted: it is checked field by field against the pinned schema's
shape and bounds before anything is stored, and an answer that does not fit fails its host (a
listing) or its read (comments) and changes nothing. forgectl is only ever read here.

Owned by later issues, left as seams:

- TODO(#14): eligibility, the access cache and the 2-per-lookup budget cost (`Budget.spend`).
- TODO(#15): the comment records and the handled set; `Detection(on_read=...)` receives each good
  read, and `absent_comment_ids` is the only rule about deletion (never from an incomplete read).
- TODO(#16): the due rule, settle and the PR block (columns arrive with their migration).
- TODO(#17): the `check` command, `check --pr`, `all_hosts_failed` and the `pass` lease.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from . import deps
from .audit import LogWriter
from .config import Config
from .errors import CliError
from .gateway import Gateway, GatewayError, Result
from .store import Lease, cas_update, pr_digest

LISTING_LIMIT: Final = 1000  # forgectl's own maximum for `--limit`
MAX_COMMENT_BYTES_CHARS: Final = 1024 * 1024  # one comment body, in characters
MAX_COMMENTS_PER_READ: Final = 20_000
MAX_THREADS_PER_READ: Final = 20_000
MAX_WHOAMI_HOSTS: Final = 64

# The patterns of the pinned forgectl schemas (tests/pinned/forgectl/schemas/defs.json).
_HOST: Final = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?"
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
_PR_STATES: Final = ("open", "closed", "merged")


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


def _text(value: object, what: str, limit: int, *, one_line: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise _Bad(f"{what} is not text of at most {limit} characters")
    if one_line and any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise _Bad(f"{what} has a control character")
    return value


def _opt_text(value: object, what: str, limit: int, *, one_line: bool = False) -> str | None:
    return None if value is None else _text(value, what, limit, one_line=one_line)


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
    return code if isinstance(code, str) and _CODE_RE.fullmatch(code) else "bad_output"


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


def _parse_facts(raw: object, what: str) -> PrFacts:
    item = _obj(raw, what)
    repo = _match(item.get("repo"), _REPO_RE, f"{what}.repo")
    number = item.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= 9_999_999_999:
        raise _Bad(f"{what}.number is not a PR number")
    pr = _text(item.get("pr"), f"{what}.pr", 1024)
    if pr != f"{repo}#{number}":  # also keeps the host part of the key honest
        raise _Bad(f"{what}.pr does not name its repo and number")
    state = item.get("state")
    if state not in _PR_STATES:
        raise _Bad(f"{what}.state is unknown")
    head = _obj(item.get("head"), f"{what}.head")
    head_repo = _opt_match(head.get("repo"), _REPO_RE, f"{what}.head.repo")
    is_fork = _opt_bool(head.get("is_fork"), f"{what}.head.is_fork")
    base = _obj(item.get("base"), f"{what}.base")
    return PrFacts(
        pr=pr,
        host=repo.split("/", 1)[0],
        repo=repo,
        number=number,
        state=str(state),
        head_sha=_match(head.get("sha"), _SHA_RE, f"{what}.head.sha"),
        head_ref=_text(head.get("ref"), f"{what}.head.ref", 255, one_line=True),
        head_repo=head_repo,
        # a null is_fork, a deleted head project and another head repo all count as a fork
        is_fork=is_fork is not False or head_repo != repo,
        base_ref=_text(base.get("ref"), f"{what}.base.ref", 255, one_line=True),
        created_at=_match(item.get("created_at"), _TS_RE, f"{what}.created_at"),
        updated_at=_match(item.get("updated_at"), _TS_RE, f"{what}.updated_at"),
    )


def _parse_listing(result: Result, host: str) -> tuple[list[PrFacts], bool]:
    code = _failure_code(result)
    if code is not None:
        raise _HostFailed(code)
    try:
        doc = _obj(result.json, "listing")
        items = _array(doc.get("items"), "items", LISTING_LIMIT)
        complete = _bool(doc.get("complete"), "complete")
        _array(doc.get("errors"), "errors", MAX_WHOAMI_HOSTS)
        facts: dict[str, PrFacts] = {}
        for index, raw in enumerate(items):
            parsed = _parse_facts(raw, f"items[{index}]")
            if parsed.host != host:
                raise _Bad("an item belongs to another host")
            facts.setdefault(parsed.pr, parsed)
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
    doc = _obj(result.json, "whoami")
    hosts = _array(doc.get("hosts"), "hosts", MAX_WHOAMI_HOSTS)
    errors = _array(doc.get("errors"), "errors", MAX_WHOAMI_HOSTS)
    accounts: dict[str, str | None] = {}
    credentialed: list[str] = []
    for index, raw in enumerate(hosts):
        entry = _obj(raw, f"hosts[{index}]")
        host = _match(entry.get("host"), _HOST_RE, "host")
        accounts[host] = _opt_match(entry.get("id"), _ACCOUNT_RE, "id")
        source = entry.get("token_source")
        if not isinstance(source, str):
            raise _Bad("token_source is not text")
        if source != "none":
            credentialed.append(host)
    failed: dict[str, str] = {}
    for index, raw in enumerate(errors):
        entry = _obj(raw, f"errors[{index}]")
        host = _match(entry.get("host"), _HOST_RE, "host")
        code = entry.get("code")
        failed.setdefault(
            host, code if isinstance(code, str) and _CODE_RE.fullmatch(code) else "bad_output"
        )
    return _Whoami(accounts, tuple(h for h in credentialed if h not in failed), failed)


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


@dataclass(frozen=True)
class CommentRead:
    pr: str
    head_sha: str
    complete: bool  # False: part of the PR could not be read; nothing may be marked deleted
    comments: tuple[ReadComment, ...]


def _parse_comments(result: Result, pr: str) -> CommentRead:
    doc = _obj(result.json, "comments")
    if doc.get("pr") != pr:
        raise _Bad("the answer is for another PR")
    head_sha = _match(doc.get("head_sha"), _SHA_RE, "head_sha")
    complete = _bool(doc.get("complete"), "complete")
    threads = _array(doc.get("threads"), "threads", MAX_THREADS_PER_READ)
    seen: set[str] = set()
    out: list[ReadComment] = []
    for t_index, raw_thread in enumerate(threads):
        thread = _obj(raw_thread, f"threads[{t_index}]")
        thread_id = _match(thread.get("id"), _ID_RE, "thread id")
        resolvable = _bool(thread.get("resolvable"), "resolvable")
        resolved = _bool(thread.get("resolved"), "resolved")
        if resolved and not resolvable:
            raise _Bad("a thread that cannot be resolved is resolved")
        outdated = _opt_bool(thread.get("outdated"), "outdated")
        path = _opt_text(thread.get("path"), "path", 4096, one_line=True)
        line = thread.get("line")
        if line is not None and (
            not isinstance(line, int) or isinstance(line, bool) or line < 1 or line > 10**9
        ):
            raise _Bad("line is not a line number")
        comments = _array(thread.get("comments"), "comments", MAX_COMMENTS_PER_READ)
        if not comments:
            raise _Bad("a thread has no comments")
        for raw in comments:
            comment = _obj(raw, "comment")
            comment_id = _match(comment.get("id"), _ID_RE, "comment id")
            if comment_id in seen or len(seen) >= MAX_COMMENTS_PER_READ:
                raise _Bad("duplicate or too many comments")
            seen.add(comment_id)
            author = _obj(comment.get("author"), "author")
            out.append(
                ReadComment(
                    id=comment_id,
                    thread_id=thread_id,
                    author_id=_opt_match(author.get("id"), _ACCOUNT_RE, "author id"),
                    author_login=_opt_text(author.get("login"), "login", 255, one_line=True),
                    author_is_bot=_opt_bool(author.get("is_bot"), "is_bot"),
                    body=_text(comment.get("body"), "body", MAX_COMMENT_BYTES_CHARS),
                    created_at=_match(comment.get("created_at"), _TS_RE, "created_at"),
                    updated_at=_match(comment.get("updated_at"), _TS_RE, "updated_at"),
                    minimized=_bool(comment.get("minimized"), "minimized"),
                    reply_to=_opt_match(comment.get("reply_to"), _ID_RE, "reply_to"),
                    thread_resolved=resolved,
                    thread_resolvable=resolvable,
                    thread_outdated=outdated,
                    path=path,
                    line=line,
                )
            )
    return CommentRead(pr, head_sha, complete, tuple(out))


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
    host_state = excluded.host_state, head_sha = excluded.head_sha,
    head_ref = excluded.head_ref, head_repo = excluded.head_repo, is_fork = excluded.is_fork,
    base_ref = excluded.base_ref, updated_at = excluded.updated_at,
    listed_at = excluded.listed_at, rev = rev + 1
"""


def _upsert(conn: sqlite3.Connection, facts: PrFacts, now: float) -> None:
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
    spent: int = 0  # estimated requests


@dataclass(frozen=True)
class PassDetection:
    hosts: tuple[HostResult, ...]

    @property
    def all_failed(self) -> bool:
        """No host could be read (`check` exits 12 `all_hosts_failed`, #17)."""
        return bool(self.hosts) and not any(h.ok for h in self.hosts)


OnRead = Callable[[StoredPr, CommentRead], None]


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
        started_at: datetime | None = None,
    ) -> None:
        self.gateway = gateway
        self.lease = lease
        self.settings = settings
        self.log = log
        self.on_read = on_read
        self.started_at = started_at or gateway.clock.now()
        self._checked = False
        self._whoami: _Whoami | CliError | None = None

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
        """`hosts_to_check`, or the hosts `forgectl whoami` lists with a credential."""
        configured = self.settings.hosts_to_check
        if configured is not None:
            return list(dict.fromkeys(configured))
        return list(self._who().credentialed)

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
        return (who.accounts.get(host), None) if who.accounts.get(host) else (None, "identity_unknown")

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
            facts, complete = _parse_listing(listing, host)
        except _HostFailed as failure:
            return self._fail(host, failure.code)
        budget = self.settings.limits.requests_per_host
        spent = listing_cost(len(facts))
        now = self._now()
        with self.lease.transaction() as conn:
            for item in facts:
                _upsert(conn, item, now)
        listed = tuple(item.pr for item in facts)
        closed: list[str] = []
        if complete:
            for pr in self._unlisted_open(host, set(listed)):
                if spent + 1 > budget:
                    break  # the rest is learned next pass
                spent += 1
                if self._learn_closed(pr):
                    closed.append(pr)
        attempted: list[str] = []
        read: list[str] = []
        failed: list[str] = []
        deferred: list[str] = []
        work = self._work(host, set(listed))
        for index, (pr, count) in enumerate(work):
            cost = read_cost(count)
            # the first read of a pass always goes ahead: a PR whose estimate exceeds the whole
            # budget (or a budget the listing used up) would otherwise never be read
            if attempted and spent + cost > budget:
                deferred = [p for p, _ in work[index:]]
                break
            spent += cost
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
            spent=spent,
        )

    def _unlisted_open(self, host: str, listed: set[str]) -> list[str]:
        rows = self.lease.store.conn.execute(
            "SELECT pr FROM prs WHERE host = ? AND host_state = 'open' ORDER BY pr", (host,)
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
        outcome = self._run(lambda: self.gateway.forgectl.show(pr))
        if isinstance(outcome, str) or _failure_code(outcome) is not None:
            return False
        try:
            facts = _parse_facts(outcome.json, "show")
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

    def refresh_pr(self, pr: str) -> StoredPr:
        """`forgectl show` for one PR, stored like a listing item (`check --pr`, `respond`)."""
        self._require()
        outcome = self._run(lambda: self.gateway.forgectl.show(pr))
        code = outcome if isinstance(outcome, str) else _failure_code(outcome)
        facts: PrFacts | None = None
        if code is None and not isinstance(outcome, str):
            try:
                facts = _parse_facts(outcome.json, "show")
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
        with self.lease.transaction() as conn:
            _upsert(conn, facts, self._now())
            stored = get_pr(conn, pr)
        if stored is None:  # cannot happen: the upsert just ran
            raise CliError("internal", "a stored PR vanished")
        return stored

    def _ensure_installed_at(self) -> None:
        stamp = self.started_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.lease.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('installed_at', ?)", (stamp,))

    def read_comments(self, pr: str) -> CommentRead:
        """The one per-PR read (check, check --pr, respond and the job).

        Stamps the attempt first, so a failing PR cannot keep the front of the queue. Failure
        (``lower_failed``) changes nothing but the failure count; an incomplete read is returned
        as it is and marks nothing deleted (``absent_comment_ids``).
        """
        self._require()
        with self.lease.transaction() as conn:
            if get_pr(conn, pr) is None:
                raise CliError("internal", "a comment read of a PR that is not stored")
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
            conn.execute(
                "UPDATE prs SET last_comments_read_at = ?, read_failures = 0, comment_count = ?,"
                " rev = rev + 1 WHERE pr = ?",
                (self._now(), len(read.comments), pr),
            )
            stored = get_pr(conn, pr)
        if self.on_read is not None and stored is not None:
            self.on_read(stored, read)
        return read

    def _read_failed(self, pr: str) -> None:
        with self.lease.transaction() as conn:
            conn.execute(
                "UPDATE prs SET read_failures = read_failures + 1, rev = rev + 1 WHERE pr = ?",
                (pr,),
            )
