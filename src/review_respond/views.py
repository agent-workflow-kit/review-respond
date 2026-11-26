"""`list` and `show` for PRs (docs/interface.md: list, show).

Both are read-only commands: no lease, no audit or log writer, and they work on a state database
newer than this binary. The state they print is derived from the stored rows (`due.derive_state`).

TODO(#18): `show <round id>` and the rounds of a PR; TODO(#29): `show <pending id>` and the
pending id of a PR. Until those tables exist such an id matches nothing (exit 10).
"""

from __future__ import annotations

import argparse
import re
from typing import Any, Final

from . import prstate
from .errors import CliError
from .output import Context, Outcome, sanitize_line
from .prref import PR_REF_RE, REPO_ID_RE, parse_pr
from .store import Store

_OBJECT_ID_RE: Final = re.compile(r"r[dp]-[a-z2-7]{4,8}")


def _store(ctx: Context) -> Store:
    if ctx.settings is None or ctx.store is None:
        raise CliError("internal", "the command ran without a config or a state database")
    return ctx.store


def _row(item: dict[str, Any]) -> str:
    waiting = f" ({item['waiting_reason']})" if item["waiting_reason"] else ""
    line = (
        f"{item['pr']}  {item['state']}{waiting}  unhandled={item['unhandled']}"
        f" seen={item['seen']} ineligible={item['ineligible']} edited={item['edited']}"
        f" rounds={item['rounds']}"
    )
    return sanitize_line(line)


def handle_list(ctx: Context, args: argparse.Namespace) -> Outcome:
    store = _store(ctx)
    assert ctx.settings is not None
    state: str | None = args.state
    if args.waiting:
        if state not in (None, "waiting"):
            raise CliError("usage", "--waiting conflicts with --state " + state)
        state = "waiting"
    repo: str | None = args.repo
    if repo is not None and not REPO_ID_RE.fullmatch(repo):
        raise CliError("usage", "--repo is a repo id (host/project path)")
    now = store.clock.now().timestamp()
    rows: list[dict[str, Any]] = []
    for stored in prstate.stored_prs(store.conn):
        if repo is not None and stored.repo != repo:
            continue
        item = prstate.pr_object(store.conn, ctx.settings, stored, now)
        if state is None or item["state"] == state:
            rows.append(item)
    human = "".join(_row(item) + "\n" for item in rows) or "no pull requests\n"
    return Outcome({"prs": rows}, human)


def _resolve_pr(store: Store, text: str) -> str:
    """The stored PR a `show` argument names: the reference or URL, or (when no stored PR has
    exactly that reference) a reference without its host, which must match one stored PR."""
    stored = [p.pr for p in prstate.stored_prs(store.conn)]
    if PR_REF_RE.fullmatch(text) and text in stored:
        return text
    if PR_REF_RE.fullmatch(text):
        found = [p for p in stored if p.endswith("/" + text)]
        if len(found) > 1:
            raise CliError(
                "ambiguous", f"{text} matches {len(found)} stored PRs; give the host too"
            )
        if found:
            return found[0]
        raise CliError("not_found", "no stored PR matches")
    reference = parse_pr(text)
    if reference not in stored:
        raise CliError("not_found", "no stored PR matches")
    return reference


def _comment_line(item: dict[str, Any]) -> str:
    kind = "eligible" if item["eligible"] else f"ineligible: {item['reason']}"
    head = f"  {item['id']} [{item['state']}, {kind}]"
    body = "".join("\n      " + line for line in item["body"].split("\n"))
    return sanitize_line(head) + body


def handle_show(ctx: Context, args: argparse.Namespace) -> Outcome:
    store = _store(ctx)
    assert ctx.settings is not None
    text: str = args.ref
    if text.startswith(("rd-", "rp-")):
        if not _OBJECT_ID_RE.fullmatch(text):
            raise CliError("usage", "a round or pending id is rd-/rp- plus 4 to 8 characters")
        raise CliError("not_found", "no round or pending record matches")  # TODO(#18, #29)
    pr = _resolve_pr(store, text)
    stored = prstate.stored_prs_by_ref(store.conn, pr)
    now = store.clock.now().timestamp()
    item = prstate.pr_object(store.conn, ctx.settings, stored, now)
    comments = [prstate.comment_object(store, r) for r in prstate.comment_records(store, pr)]
    payload: dict[str, Any] = {"object_type": "pr", "pr": item, "rounds": [], "comments": comments}
    human = _row(item) + "\n" + "".join(_comment_line(c) + "\n" for c in comments)
    return Outcome(payload, human)
