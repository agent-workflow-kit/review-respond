"""Fork lineage and base agent resolution (docs/interface.md: Fork lineage). Stage 2.

review-respond keeps a **base agent** per PR (``prs.base_agent``): the PR's original agent until a
round is confirmed, then the confirmed round's fork (forks of forks). It acts (resume, stop,
teardown) only on agent ids in its own ``forks`` table; a label finds candidates and never makes an
agent ours. The original agent has no row, so it is never stopped, torn down or labeled.

- ``record_fork`` (called by ``Rounds.record_agent`` in the round's transaction) stores a round's
  fork. It refuses an id that is some PR's base or some round's base: the original can never
  become a row.
- ``switch_base`` is the primitive of the lineage switch: in the caller's transaction (the record
  that landed a push, or finished without a diff) the round's fork becomes the base and the
  previous base fork, if ours, is marked ``teardown_pending``. Only a ``pending`` or ``confirmed``
  round can switch; a rejected, failed or live round never does.
- ``resolve_base`` finds the base (stored, else a verified label match, else a fresh session if
  the repo opted in) and stores it with the ``agent_busy`` observation the due rule reads.
- ``teardown_pass`` tears down the forks marked ``teardown_pending`` (the pass lease).

Seams owned by later issues:

- TODO(#26): the cleanup rule marks a terminal round's fork and a closed PR's base fork
  ``teardown_pending`` (``mark_teardown_pending`` is its primitive; it refuses the base).
- TODO(#32): ``confirm`` calls ``switch_base`` in the transaction that records ``pushed`` or
  ``done``.
- TODO(#37): ``post-attention`` ``teardown_refused`` fires when ``teardown_pass`` reports
  ``refused``.
- TODO(#23), TODO(#24): the job forks ``Base.agent_id`` (or launches fresh for ``Base.kind ==
  "fresh"``) and finalizes through the ``round=`` label, accepting only candidates ``is_recorded``
  says are ours or the documented ``forked_from``/``cwd`` match.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from . import labels
from .config import Config
from .errors import CliError
from .gateway import AgentSpawn, Result, Wtctl
from .store import Lease, cas_update, pass_lease_key, pr_digest, pr_lease_key

ACTOR: Final = "auto:review-respond"
FORK_STATES: Final = ("round", "base", "teardown_pending", "torn_down")
TEARDOWN_REFUSED_AFTER: Final = 3
# Kinds that read third-party text (architecture 5.3 kinds table; an unknown kind name gets the
# review profile): an agent that ran in one is not a PR's original agent.
THIRD_PARTY_KINDS: Final = frozenset({"review", "gate-check", "respond"})
BUSY_STATUSES: Final = frozenset({"launching", "live", "tearing_down"})
AGENT_STATUSES: Final = BUSY_STATUSES | {"parked", "torn_down"}
SWITCHABLE_ROUND_STATES: Final = ("pending", "confirmed")

_AGENT_RE: Final = re.compile(r"ag-[a-z2-7]{8}")
_WT_RE: Final = re.compile(r"wt-[a-z2-7]{8}")


def is_agent_id(text: object) -> bool:
    return isinstance(text, str) and _AGENT_RE.fullmatch(text) is not None


# ---- the forks table -----------------------------------------------------------------------


@dataclass(frozen=True)
class Fork:
    agent_id: str
    pr: str
    round: str
    source: str | None
    state: str
    failed_passes: int
    refused_at: float | None
    rev: int


_FORK_COLUMNS: Final = "agent_id, pr, round, source, state, failed_passes, refused_at, rev"


def get_fork(conn: sqlite3.Connection, agent_id: str) -> Fork | None:
    row = conn.execute(
        f"SELECT {_FORK_COLUMNS} FROM forks WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    return None if row is None else Fork(*row)


def is_recorded(conn: sqlite3.Connection, agent_id: object) -> bool:
    """True only for an agent id review-respond itself recorded (never from a label)."""
    return is_agent_id(agent_id) and get_fork(conn, str(agent_id)) is not None


def list_forks(
    conn: sqlite3.Connection, *, pr: str | None = None, state: str | None = None
) -> list[Fork]:
    where, args = [], []
    if pr is not None:
        where.append("pr = ?")
        args.append(pr)
    if state is not None:
        where.append("state = ?")
        args.append(state)
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    sql = f"SELECT {_FORK_COLUMNS} FROM forks{clause} ORDER BY created_at, agent_id"
    return [Fork(*r) for r in conn.execute(sql, args)]


def record_fork(
    conn: sqlite3.Connection,
    *,
    pr: str,
    round_id: str,
    agent_id: str,
    source: str | None,
    now: float,
) -> bool:
    """Store the agent of a round as a fork in state ``round`` (in the caller's transaction).
    False when the same row exists already. ``internal`` for an id that is already another
    round's, is its own source, or is any PR's base or round's base (an original agent)."""
    if not is_agent_id(agent_id) or (source is not None and not is_agent_id(source)):
        raise ValueError("agent ids are ag- plus 8 characters of a-z and 2-7")
    existing = get_fork(conn, agent_id)
    if existing is not None:
        if (existing.pr, existing.round) == (pr, round_id):
            return False
        raise CliError("internal", "an agent id recorded for another round")
    if agent_id == source:
        raise CliError("internal", "a fork cannot be its own source")
    if (
        conn.execute("SELECT 1 FROM prs WHERE base_agent = ?", (agent_id,)).fetchone() is not None
        or conn.execute("SELECT 1 FROM rounds WHERE base_agent_id = ?", (agent_id,)).fetchone()
        is not None
    ):
        raise CliError("internal", "an agent that is a base cannot be recorded as a fork")
    try:
        conn.execute(
            "INSERT INTO forks (agent_id, pr, round, source, state, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'round', ?, ?)",
            (agent_id, pr, round_id, source, now, now),
        )
    except sqlite3.IntegrityError:
        raise CliError("internal", "the round already has a fork") from None
    return True


def _set_state(conn: sqlite3.Connection, fork: Fork, state: str, now: float) -> None:
    if not cas_update(
        conn, "forks", "agent_id", fork.agent_id, fork.rev, {"state": state, "updated_at": now},
        where={"state": fork.state},
    ):  # fmt: skip
        raise CliError("internal", f"fork {fork.agent_id} changed under the write lock")


@dataclass(frozen=True)
class Switch:
    """``changed`` is False when the round's fork already was the base."""

    changed: bool
    base: str
    retired: str | None


def switch_base(conn: sqlite3.Connection, round_id: str, *, now: float) -> Switch:
    """The lineage switch, in the transaction of the record that landed a push (or ended without
    a diff): the round's fork becomes ``prs.base_agent`` and the previous base fork, if ours,
    becomes ``teardown_pending``. The original agent (no row) is only replaced as the base.

    ``ValueError`` for a round that is not ``pending`` or ``confirmed``, has no agent, or whose
    fork is no longer in state ``round``; ``internal`` when the round's agent is not a fork this
    tool recorded for that round.
    """
    found = conn.execute(
        "SELECT pr, state, agent_id FROM rounds WHERE id = ?", (round_id,)
    ).fetchone()
    if found is None:
        raise CliError("not_found", "no round matches")
    pr, state, agent_id = found
    if state not in SWITCHABLE_ROUND_STATES:
        raise ValueError(f"a {state} round cannot become the base")
    if agent_id is None:
        raise ValueError("the round has no agent")
    fork = get_fork(conn, agent_id)
    if fork is None or fork.pr != pr or fork.round != round_id:
        raise CliError("internal", "the round's agent is not a fork recorded for it")
    if fork.state == "base":
        return Switch(False, agent_id, None)
    if fork.state != "round":
        raise ValueError(f"a fork in state {fork.state} cannot become the base")
    row = conn.execute("SELECT rev, base_agent FROM prs WHERE pr = ?", (pr,)).fetchone()
    if row is None:
        raise CliError("internal", "the PR is not stored")
    rev, previous = row
    retired: str | None = None
    if previous is not None and previous != agent_id:
        old = get_fork(conn, previous)
        if old is not None and old.pr == pr and old.state == "base":
            _set_state(conn, old, "teardown_pending", now)
            retired = previous
    _set_state(conn, fork, "base", now)
    if not cas_update(conn, "prs", "pr", pr, rev, {"base_agent": agent_id}):
        raise CliError("internal", f"PR {pr} changed under the write lock")
    return Switch(True, agent_id, retired)


def mark_teardown_pending(conn: sqlite3.Connection, agent_id: str, *, now: float) -> bool:
    """Mark a fork in state ``round`` for teardown. False for anything else: an agent this tool
    did not record (the original, a forged id), the base, one already marked or torn down."""
    fork = get_fork(conn, agent_id) if is_agent_id(agent_id) else None
    if fork is None or fork.state != "round":
        return False
    if conn.execute("SELECT 1 FROM prs WHERE base_agent = ?", (agent_id,)).fetchone() is not None:
        return False
    _set_state(conn, fork, "teardown_pending", now)
    return True


# ---- teardown ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Teardown:
    agent_id: str
    pr: str
    outcome: str  # torn_down | retry
    refused: bool = False  # the third failed pass: post-attention teardown_refused fires once


def _reason(result: Result) -> str | None:
    doc = result.json
    err = doc.get("error") if isinstance(doc, dict) else None
    reason = err.get("reason") if isinstance(err, dict) else None  # pyright: ignore[reportUnknownMemberType]
    return reason if isinstance(reason, str) else None


def teardown_pass(lease: Lease, spawn: AgentSpawn) -> list[Teardown]:
    """Tear down every fork marked ``teardown_pending`` (under the pass lease): exit 0 or 10 ends
    it ``torn_down``; exit 20 with reason ``live`` stops the agent and retries next pass; anything
    else is a failed pass. The third failed pass sets ``refused_at`` and reports ``refused`` once;
    later passes keep retrying. Only rows of the forks table are ever passed to agent-spawn, and the
    PR's base never is."""
    if lease.key != pass_lease_key():
        raise ValueError("the teardown pass runs under the pass lease")
    store = lease.store
    done: list[Teardown] = []
    for fork in list_forks(store.conn, state="teardown_pending"):
        base = store.conn.execute("SELECT base_agent FROM prs WHERE pr = ?", (fork.pr,)).fetchone()
        if base is not None and base[0] == fork.agent_id:
            continue  # inconsistent row: never tear down the base
        result = spawn.teardown(fork.agent_id, actor=ACTOR)
        lease.renew()
        now = store.clock.now().timestamp()
        gone = not result.timed_out and result.returncode in (0, 10)
        live = not result.timed_out and result.returncode == 20 and _reason(result) == "live"
        if not gone and live:
            spawn.stop(fork.agent_id, actor=ACTOR)
            lease.renew()
        with lease.transaction() as conn:
            current = get_fork(conn, fork.agent_id)
            if current is None or current.state != "teardown_pending":
                continue
            if gone:
                _set_state(conn, current, "torn_down", now)
                done.append(Teardown(fork.agent_id, fork.pr, "torn_down"))
                continue
            passes = current.failed_passes + 1
            changes: dict[str, object] = {"failed_passes": passes, "updated_at": now}
            refused = passes >= TEARDOWN_REFUSED_AFTER and current.refused_at is None
            if refused:
                changes["refused_at"] = now
            cas_update(conn, "forks", "agent_id", current.agent_id, current.rev, changes)
            done.append(Teardown(fork.agent_id, fork.pr, "retry", refused))
    return done


# ---- the base agent ------------------------------------------------------------------------


@dataclass(frozen=True)
class Base:
    """What ``resolve_base`` found. ``kind``: ``agent`` (``agent_id`` is the base), ``fresh`` (no
    agent; the repo opted in to a fresh session), ``none`` (the due rule sets ``no_agent``) or
    ``unknown`` (agent-spawn could not be read: nothing is decided and the agent counts as busy).
    ``warnings`` are log warning codes (``base_lost``, ``label_skipped``)."""

    kind: str
    agent_id: str | None = None
    busy: bool = False
    warnings: tuple[str, ...] = ()


def agent_busy(agent: Mapping[str, Any]) -> bool:
    """The interface's ``agent_busy`` for one agent object: status ``launching``, ``live`` or
    ``tearing_down``, or a promotion pending."""
    return agent.get("status") in BUSY_STATUSES or agent.get("promotion_pending") is True


def _require_lease(lease: Lease, pr: str) -> None:
    if lease.key not in (pass_lease_key(), pr_lease_key(pr_digest(pr))):
        raise ValueError("the lease is not the pass lease or the lease of this PR")


def _show_agent(result: Result, agent_id: str) -> dict[str, Any] | None:
    """The agent object of an ``agent-spawn show`` answer, or None when it is not one about
    ``agent_id`` (an answer is checked, never trusted)."""
    doc = result.json
    agent = doc.get("agent") if isinstance(doc, dict) else None
    if (
        not result.ok
        or not isinstance(agent, dict)
        or agent.get("agent_id") != agent_id  # pyright: ignore[reportUnknownMemberType]
        or agent.get("status") not in AGENT_STATUSES  # pyright: ignore[reportUnknownMemberType]
    ):
        return None
    return agent  # pyright: ignore[reportUnknownVariableType]


def _created(agent: Mapping[str, Any]) -> float:
    try:
        return datetime.fromisoformat(str(agent.get("created"))).timestamp()
    except ValueError:
        return 0.0


def _candidate_ok(c: object, pr: str, repo: str, conn: sqlite3.Connection) -> bool:
    """The checks that need no sibling call. A label only found the candidate."""
    if not isinstance(c, dict):
        return False
    agent: dict[str, Any] = c  # pyright: ignore[reportUnknownVariableType]
    agent_id = agent.get("agent_id")
    worktree = agent.get("worktree")
    forked_from = agent.get("forked_from")
    agent_labels = agent.get("labels")
    return (
        is_agent_id(agent_id)
        and not is_recorded(conn, agent_id)
        and agent.get("status") in AGENT_STATUSES
        and agent.get("status") != "torn_down"
        and agent.get("repo") == repo
        and isinstance(worktree, dict)
        and isinstance(worktree.get("record_id"), str)  # pyright: ignore[reportUnknownMemberType]
        and _WT_RE.fullmatch(worktree["record_id"]) is not None  # pyright: ignore[reportUnknownArgumentType]
        and labels.has_label(agent_labels, "pr", pr)
        and not labels.has_label(agent_labels, "owner", labels.OWNER)
        and agent.get("run_kind") not in THIRD_PARTY_KINDS
        and (forked_from is None or not is_recorded(conn, forked_from))
    )


def _worktree_matches(wt: Wtctl, record_id: str, head_ref: str, repo: str) -> bool | None:
    """``wtctl show`` of the candidate's worktree names the PR's branch in the PR's repo.
    True: it does. False: no such worktree (exit 10) or it is another branch or repo. None: the
    answer is unreadable (timeout, another exit, overflow, not JSON): nothing is decided."""
    result = wt.show(record_id)
    if result.returncode == 10 and not result.timed_out and not result.overflow:
        return False
    doc = result.json
    if not result.ok or not isinstance(doc, dict):
        return None
    return (
        doc.get("id") == record_id  # pyright: ignore[reportUnknownMemberType]
        and doc.get("branch") == head_ref  # pyright: ignore[reportUnknownMemberType]
        and doc.get("repo") == repo  # pyright: ignore[reportUnknownMemberType]
    )


def _store(
    lease: Lease, pr: str, expect: str | None, base: str | None, busy: bool, *, lost: bool = False
) -> bool:
    """Write the base and the busy flag when the stored base still is ``expect``. ``lost``: the
    stored base is gone (its fork row, if ours, is marked torn down with it)."""
    now = lease.store.clock.now().timestamp()
    with lease.transaction() as conn:
        row = conn.execute("SELECT rev, base_agent FROM prs WHERE pr = ?", (pr,)).fetchone()
        if row is None or row[1] != expect:
            return False
        if lost and expect is not None:
            fork = get_fork(conn, expect)
            if fork is not None and fork.pr == pr and fork.state in ("base", "round"):
                _set_state(conn, fork, "torn_down", now)
        return cas_update(
            conn, "prs", "pr", pr, row[0], {"base_agent": base, "agent_busy": int(busy)}
        )


def mark_unreadable(lease: Lease, pr: str) -> None:
    """A sibling could not be asked at all (``GatewayError``): store ``agent_busy`` = 1 and keep the
    stored base, as for any unreadable agent-spawn answer."""
    _require_lease(lease, pr)
    row = lease.store.conn.execute("SELECT base_agent FROM prs WHERE pr = ?", (pr,)).fetchone()
    if row is not None:
        _store(lease, pr, row[0], row[0], True)


def resolve_base(lease: Lease, spawn: AgentSpawn, wt: Wtctl, config: Config, pr: str) -> Base:
    """The base agent of ``pr`` (see the module docstring), stored with the busy observation.

    Under the PR's lease (or the pass lease). A stored base is looked at with ``show``: not found
    or torn down resets it (warning ``base_lost``) and the search runs; an unreadable answer
    decides nothing (``unknown``, busy). The search lists agents labeled ``pr=<pr>``, drops every
    candidate that fails the checks of ``_candidate_ok``, prefers ``prefer_owner``, then the most
    recent (ties: the larger id), and takes the first whose worktree is on the PR's branch. A
    worktree answer that cannot be read (not exit 10, not a record) on a candidate no earlier one
    beat makes the result ``unknown``.
    """
    _require_lease(lease, pr)
    conn = lease.store.conn
    row = conn.execute("SELECT repo, head_ref, base_agent FROM prs WHERE pr = ?", (pr,)).fetchone()
    if row is None:
        raise CliError("internal", "the PR is not stored")
    repo, head_ref, stored = row
    warnings: list[str] = []
    if stored is not None:
        agent = None
        lost = not is_agent_id(stored)
        if not lost:
            result = spawn.show(stored)
            if result.returncode == 10 and not result.timed_out:
                lost = True
            else:
                agent = _show_agent(result, stored)
                if agent is None:
                    _store(lease, pr, stored, stored, True)
                    return Base("unknown", stored, True)
                if agent["status"] == "torn_down":
                    lost = True
        if not lost:
            assert agent is not None
            busy = agent_busy(agent)
            _store(lease, pr, stored, stored, busy)
            return Base("agent", stored, busy)
        if not _store(lease, pr, stored, None, False, lost=True):
            return Base("unknown", None, True)
        warnings.append("base_lost")
    lookup = labels.lookup_label(pr)
    found: list[dict[str, Any]] = []
    if lookup is None:
        warnings.append("label_skipped")
    else:
        listed = spawn.list(labels=[lookup])
        doc = listed.json
        agents = doc.get("agents") if isinstance(doc, dict) else None
        if not listed.ok or not isinstance(agents, list):
            _store(lease, pr, None, None, True)
            return Base("unknown", None, True, tuple(warnings))
        found = [c for c in agents if _candidate_ok(c, pr, repo, conn)]  # pyright: ignore[reportUnknownVariableType]
    prefer = config.prefer_owner
    found.sort(
        key=lambda c: (labels.has_label(c["labels"], "owner", prefer), _created(c), c["agent_id"]),
        reverse=True,
    )
    for c in found:
        matched = _worktree_matches(wt, c["worktree"]["record_id"], head_ref, repo)
        if matched is None:  # this candidate could win: decide nothing (as an unreadable agent)
            _store(lease, pr, None, None, True)
            return Base("unknown", None, True, tuple(warnings))
        if matched:
            busy = agent_busy(c)
            if not _store(lease, pr, None, c["agent_id"], busy):
                return Base("unknown", None, True, tuple(warnings))
            return Base("agent", c["agent_id"], busy, tuple(warnings))
    _store(lease, pr, None, None, False)
    kind = "fresh" if config.repo_settings(repo).respond_fresh else "none"
    return Base(kind, None, False, tuple(warnings))
