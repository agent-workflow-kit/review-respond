"""`respond <pr>`: start a round for one PR now (docs/interface.md: respond).

Under the PR's ``pr:`` lease (waited for up to 30 s, then exit 20 ``busy``):

1. ``forgectl show`` (the author must be you: else 20 ``ineligible``/``not_yours``; a fork PR is
   ``ineligible``/``fork_pr``; a closed or merged one ``pr_closed``) and one comment read;
2. the refusals, from the stored rows: ``round_active`` and ``pending_exists``; ``live`` (the base
   agent is busy: never overridden); ``round_limit`` (only a human ``--force`` overrides it);
3. exit 22 ``nothing_to_respond`` when no eligible unhandled comment exists (``--include-existing``
   adds the eligible ``seen`` ones);
4. the block is cleared (``due.clear_block``: ``human_respond``; ``human_force`` for
   ``round_limit``), the note is stored (``state.put_file(<PR digest>, bytes)``: its sha256 is the
   round's ``note_sha256``, which ``_job`` reads back), the round is created and its job placed
   by ``Placement.submit`` (jobsctl or detached). The settle delay and ``jobs.max_running`` do not
   apply to a human ``respond``.

``--dry-run`` takes no lease and calls no sibling: the same refusals are decided from the stored
rows (``agent_busy`` is the stored observation) and nothing is written. The note is passed in a
file or on stdin, never in argv or the environment; it is stored with the PR (8 KiB at most).

Seams: TODO(#37): the ``pre-round`` hooks run just before the block is cleared (a veto exits 11
and sets block ``hook_vetoed``, an escalation exits 13 and sets ``hook_escalated`` unless
``--override-escalation``); the audit file has no ``respond`` action, so nothing is recorded there
(the round's ``round_state`` log event is the record).
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Final

from . import due, lineage, rounds
from .config import Config
from .detect import Detection, StoredPr, get_pr
from .errors import CliError
from .gateway import Gateway, GatewayError
from .handled import HandledSet, read_records
from .output import Context, Outcome, sanitize_line
from .placement import Placement
from .prompt import is_visible
from .prref import parse_pr
from .prstate import round_object
from .rounds import Rounds, get_round
from .store import Lease, Store, pr_digest, pr_lease_key
from .textin import read_text_arg
from .validate import child_env

PR_LEASE_WAIT: Final = 30.0
MAX_NOTE_BYTES: Final = 8 * 1024
_SKILL: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}")
_KEEP_ERRORS: Final = ("lease_lost", "state_unsafe", "version_skew")


@dataclass(frozen=True)
class Request:
    """What the command line asked for, already checked."""

    pr: str
    force: bool
    include_existing: bool
    note: bytes | None
    skill: str | None
    mode: str | None


def _has_visible_text(text: str) -> bool:
    """A note needs one character that is not whitespace and that the prompt sanitizer keeps:
    the characters it renders as ``<U+XXXX>`` markers (control, format, variation selectors,
    fillers, NUL, U+200B) cannot make a note."""
    return any(is_visible(ch) for ch in text)


def read_request(args: argparse.Namespace) -> Request:
    """The usage checks, before any lease or sibling call."""
    pr = parse_pr(args.pr)
    # todo(#37): record override_escalation for respond when the pre-round hooks land
    # (the flag is accepted by the parser and has no effect until then)
    skill: str | None = args.skill
    if skill is not None and (not _SKILL.fullmatch(skill) or ".." in skill):
        raise CliError("usage", "--skill does not match agent-spawn's skill grammar")
    note: bytes | None = None
    if args.note_file is not None:
        text = read_text_arg(args.note_file, flag="--note-file", max_bytes=MAX_NOTE_BYTES)
        if not _has_visible_text(text):
            raise CliError("usage", "--note-file: the note is empty")
        note = text.encode("utf-8")
    return Request(
        pr=pr,
        force=bool(args.force),
        include_existing=bool(args.include_existing),
        note=note,
        skill=skill,
        mode=args.mode,
    )


# ---- the refusals, from the stored rows ---------------------------------------------------------


def _active_round(conn: sqlite3.Connection, pr: str) -> str | None:
    found = conn.execute(
        "SELECT id FROM rounds WHERE pr = ? AND state IN ('queued', 'running')", (pr,)
    ).fetchone()
    return None if found is None else str(found[0])


def _pending_round(conn: sqlite3.Connection, pr: str) -> str | None:
    found = conn.execute(
        "SELECT id FROM rounds WHERE pr = ? AND state = 'pending'", (pr,)
    ).fetchone()
    return None if found is None else str(found[0])


def refuse_unusable(stored: StoredPr) -> None:
    """A closed, merged or fork PR gets no round."""
    if stored.host_state != "open":
        raise CliError("refused", "the PR is closed or merged", reason="pr_closed")
    if stored.is_fork:
        raise CliError(
            "refused",
            "a fork PR gets no round in 0.x",
            reason="ineligible",
            details={"reason": "fork_pr"},
        )


def refuse_in_progress(conn: sqlite3.Connection, pr: str) -> None:
    """``round_active`` and ``pending_exists``."""
    active = _active_round(conn, pr)
    if active is not None:
        raise CliError(
            "refused",
            "a round of this PR is queued or running",
            reason="round_active",
            details={"round": active},
        )
    pending = _pending_round(conn, pr)
    if pending is not None:
        raise CliError(
            "refused",
            "the PR has a pending confirmation: confirm or reject it first",
            reason="pending_exists",
            details={"round": pending},
        )


def refuse_live(*, busy: bool, agent_id: str | None) -> None:
    """``live``: the base agent is launching, live or tearing down. Never overridden."""
    if busy:
        details: dict[str, Any] = {} if agent_id is None else {"agent_id": agent_id}
        raise CliError(
            "refused", "an agent working on the PR is live", reason="live", details=details
        )


def refuse_round_limit(
    conn: sqlite3.Connection, settings: Config, stored: StoredPr, force: bool
) -> None:
    counted = rounds.round_facts(conn, stored.pr).counted
    limit = settings.repo_settings(stored.repo).max_rounds
    if counted >= limit and not force:
        raise CliError(
            "refused",
            "the PR reached its round limit: --force starts another round",
            reason="round_limit",
            details={"counted": counted, "max_rounds": limit},
        )


def comment_ids(conn: sqlite3.Connection, pr: str, *, include_existing: bool) -> list[str]:
    """The eligible comments a round would answer: the unhandled ones, and with
    ``--include-existing`` the ``seen`` ones too."""
    wanted = {"unhandled", "seen"} if include_existing else {"unhandled"}
    found = [r.id for r in read_records(conn, pr, None) if r.eligible and r.state in wanted]
    return sorted(found)


# ---- the command --------------------------------------------------------------------------------


def _human(row: rounds.RoundRow, ids: list[str], *, dry_run: bool) -> str:
    verb = "would start" if dry_run else "started"
    return sanitize_line(f"{verb} {row.id} for {row.pr}: {len(ids)} comment(s)") + "\n"


def _dry_run(ctx: Context, settings: Config, req: Request) -> Outcome:
    store = ctx.store
    stored = get_pr(store.conn, req.pr) if store is not None else None
    if store is None or stored is None:
        raise CliError("not_found", "the PR is not stored: run check first")
    refuse_unusable(stored)
    refuse_in_progress(store.conn, req.pr)
    busy = bool(rounds.round_facts(store.conn, req.pr).agent_busy)
    refuse_live(busy=busy, agent_id=None)
    refuse_round_limit(store.conn, settings, stored, req.force)
    ids = comment_ids(store.conn, req.pr, include_existing=req.include_existing)
    if not ids:
        raise CliError("nothing_to_respond", "no eligible unhandled comment")
    payload = {"dry_run": True, "round": None, "comment_ids": ids}
    human = sanitize_line(f"would start a round for {req.pr}: {len(ids)} comment(s)") + "\n"
    return Outcome(payload, human)


def _read(det: Detection, lease: Lease, req: Request) -> StoredPr:
    """``forgectl show`` and the one comment read; the PR must be yours, open and not a fork."""
    try:
        ref = det.refresh_pr(req.pr)
    except CliError as error:
        if error.code == "lower_failed" and (error.lower or {}).get("code") == "not_found":
            raise CliError("not_found", "the host does not know this PR") from None
        raise
    if not ref.account_known or ref.facts is None:
        raise CliError(
            "lower_failed",
            "your account on the PR's host is unknown",
            lower={"code": ref.account_code or "identity_unknown"},
        )
    if ref.not_yours:
        raise CliError(
            "refused",
            "the PR is not yours",
            reason="ineligible",
            details={"reason": "not_yours"},
        )
    assert ref.stored is not None
    refuse_unusable(ref.stored)
    det.read_comments(req.pr)
    stored = get_pr(lease.store.conn, req.pr)
    assert stored is not None
    return stored


def _block_clearing(
    conn: sqlite3.Connection, req: Request, stored: StoredPr, settings: Config
) -> tuple[str, str] | None:
    """``(reason, cause)`` of the block a human ``respond`` clears: every block but
    ``round_limit``, which needs ``--force`` (or a raised ``max_rounds``)."""
    block = due.get_block(conn, req.pr)
    if block is None:
        return None
    if block.reason == "round_limit":
        counted = rounds.round_facts(conn, req.pr).counted
        over = counted >= settings.repo_settings(stored.repo).max_rounds
        return block.reason, "human_force" if over else "max_rounds_raised"
    return block.reason, "human_respond"


def _start(
    ctx: Context,
    settings: Config,
    store: Store,
    gateway: Gateway,
    lease: Lease,
    req: Request,
) -> Outcome:
    state = store.source
    if state is None:
        raise CliError("internal", "respond needs the state directory")
    conn = store.conn
    det = Detection(gateway, lease, settings, log=ctx.log, on_read=HandledSet(lease).record_read)
    stored = _read(det, lease, req)
    refuse_in_progress(conn, req.pr)
    try:
        base = lineage.resolve_base(lease, gateway.agent_spawn, gateway.wtctl, settings, req.pr)
    except GatewayError as exc:
        lineage.mark_unreadable(lease, req.pr)
        raise CliError(
            "lower_failed",
            "agent-spawn could not be read",
            lower={"code": "unknown", "message": str(exc)},
        ) from None
    if base.kind == "unknown":
        raise CliError(
            "lower_failed",
            "agent-spawn could not be read",
            lower={"code": "unreadable", "message": "the base agent could not be read"},
        )
    refuse_live(busy=base.busy, agent_id=base.agent_id)
    refuse_round_limit(conn, settings, stored, req.force)
    ids = comment_ids(conn, req.pr, include_existing=req.include_existing)
    if not ids:
        raise CliError("nothing_to_respond", "no eligible unhandled comment")
    placement = Placement(settings, state, gateway, config=ctx.config, log=ctx.log)
    placement.jobsctl_present()  # a too old jobsctl is found before anything changes
    # TODO(#37): the `pre-round` hooks run here, before the block is cleared and the round exists
    clearing = _block_clearing(conn, req, stored, settings)
    note_sha256 = state.put_file(stored.digest, req.note) if req.note is not None else None
    machine = Rounds(lease, log=ctx.log)
    # one transaction: the block clear, the read-failure reset (a human respond resets the count
    # even when the comment read was partial) and the round INSERT
    row = machine.create(
        req.pr,
        trigger="respond",
        forced=req.force,
        include_existing=req.include_existing,
        note_sha256=note_sha256,
        base_agent_id=base.agent_id,
        skill=req.skill,
        mode=req.mode,
        clear_block=clearing,
        reset_read_failures=True,
    )
    try:
        placement.submit(machine, row)
    except CliError as error:
        if error.code in _KEEP_ERRORS:
            raise
        # the round stays queued: the reconcile submits its job again once it is 2 minutes old
        message = f"round {row.id} was created but its job was not placed: {error.message}"
        details = {"round": row.id}
        if error.code == "lower_failed":
            raise CliError(
                "lower_failed", message, details=details, lower=error.lower or {"code": "unknown"}
            ) from None
        raise CliError(error.code, message, details=details) from None
    after = get_round(conn, row.id) or row
    payload = {"dry_run": False, "round": round_object(conn, after), "comment_ids": ids}
    return Outcome(payload, _human(after, ids, dry_run=False))


def handle(ctx: Context, args: argparse.Namespace) -> Outcome:
    settings, store = ctx.settings, ctx.store
    if settings is None:
        raise CliError("internal", "respond ran without a config")
    req = read_request(args)
    if ctx.dry_run:
        return _dry_run(ctx, settings, req)
    if store is None:
        raise CliError("internal", "respond ran without a state database")
    tokens = {c.token_env for c in settings.hosts.values() if c.token_env}
    gateway = Gateway(child_env(dict(os.environ), tokens))
    on_event = ctx.log.lease_callback() if ctx.log is not None else None
    lease = store.acquire(
        pr_lease_key(pr_digest(req.pr)), wait=PR_LEASE_WAIT, contended="busy", on_event=on_event
    )
    try:
        outcome = _start(ctx, settings, store, gateway, lease, req)
        lease.renew()  # the answer is only reported while the lease is still ours
    finally:
        lease.release()
    return outcome
