"""The run-outcome table (docs/interface.md: Run outcomes) driven by real processes.

Each case runs the pinned-grammar fake ``agent-spawn`` as a subprocess through the gateway with an
answer validated against the pinned schemas, builds the ``RunEnd`` from what the process printed
(``tests/harness/spawn_run.py``) and applies it with ``Rounds.finish_run`` to a running round: the
round's state, failure reason and counting, and the job's exit code are the table's.

The real runner is #23's (the ``_job`` that calls these commands); the helper that builds the
``RunEnd`` here is a test-side stand-in for it, so these tests prove the table and its application
against the real command grammar and envelopes, not the runner.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from review_respond.gateway import Gateway, Result
from review_respond.outcomes import ARTIFACT_FAILURES, FAILURE_REASONS, map_run_end
from review_respond.store import Store, open_state_dir
from tests.harness.fakes import Fakes
from tests.harness.spawn_run import (
    BASE,
    OUTCOMES,
    error_doc,
    outcome_doc,
    result_doc,
    run_end,
    started_doc,
)
from tests.test_gateway_wrappers import FORK_KWARGS, REPO
from tests.test_rounds import Env

AG = "ag-abcdefgh"


@pytest.fixture
def store(tmp_path: Path, gateway: Gateway) -> Iterator[Store]:
    opened = Store.open(open_state_dir(tmp_path / "state" / "rr"), write=True, close_state=True)
    opened.clock = gateway.clock
    with opened.transaction() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('installed_at', ?)", ("2026-01-01T12:00:00Z",)
        )
    yield opened
    opened.close()


@pytest.fixture
def env(store: Store) -> Iterator[Env]:
    e = Env(store)
    yield e
    assert e.lease is not None
    e.lease.release()


@dataclass(frozen=True)
class Case:
    """One finished process and what the table says of it."""

    id: str
    command: str
    exit: int
    doc: dict[str, Any] | None  # None: nothing on stdout
    to: str  # the round state afterwards; "running" for a run that only started
    reason: str | None
    counted: bool
    job_exit: int | None
    flag: str | None = None  # a stored cancel flag
    artifact: tuple[str, str | None] | None = None  # (artifact_failure, detail)
    extra: dict[str, Any] = field(default_factory=dict)


def wait_run(outcome: str, *, end_reason: str | None = None, **more: Any) -> dict[str, Any]:
    return result_doc(outcome_doc(outcome, end_reason=end_reason, **more))


def case(id: str, command: str, exit: int, doc: Any, *expected: Any, **kw: Any) -> Case:
    to, reason, counted, job_exit = expected
    return Case(id, command, exit, doc, to, reason, counted, job_exit, **kw)


CASES: list[Case] = [
    # resume and launch: 0 is a run that started
    case("resume-0", "resume", 0, started_doc(first_working=False), "running", None, False, None),
    case("launch-0", "launch", 0, started_doc(first_working=False), "running", None, False, None),
    case(
        "resume-0-working",
        "resume",
        0,
        started_doc(first_working=True),
        "running",
        None,
        True,
        None,
    ),
    case(
        "launch-0-working",
        "launch",
        0,
        started_doc(first_working=True),
        "running",
        None,
        True,
        None,
    ),
    # wait 0: done, then the artifact checks the caller makes
    case("wait-0-done", "wait", 0, wait_run("done"), "pending", None, True, 0),
    *[
        case(
            f"wait-0-{why}",
            "wait",
            0,
            wait_run("done"),
            "failed",
            why,
            True,
            12,
            artifact=(why, "schema" if why == "artifact_invalid" else None),
        )  # fmt: skip
        for why in ARTIFACT_FAILURES
    ],
    # 10
    case("resume-10", "resume", 10, error_doc("not_found"), "failed", "agent_gone", False, 12),
    case("wait-10", "wait", 10, error_doc("not_found"), "failed", "agent_lost", False, 12),
    case("launch-10", "launch", 10, error_doc("not_found"), "failed", "internal", False, 1),
    # 11
    *[
        case(f"{c}-11", c, 11, error_doc("vetoed"), "failed", "vetoed", False, 12)
        for c in ("resume", "launch", "wait")
    ],
    case("wait-11-run", "wait", 11, wait_run("vetoed"), "failed", "vetoed", False, 12),
    # 12
    *[
        case(f"{c}-12", c, 12, error_doc("launch_failed"), "failed", "launch_failed", False, 12)
        for c in ("resume", "launch", "wait")
    ],
    *[
        case(
            f"{c}-12-canceled",
            c,
            12,
            error_doc("launch_failed"),
            "canceled",
            None,
            False,
            0,
            flag="pr_closed",
        )  # fmt: skip
        for c in ("resume", "launch", "wait")
    ],
    case("wait-12-run", "wait", 12, wait_run("not-started"), "failed", "launch_failed", False, 12),
    # 13: the session phase escalates counted, a launch-phase hook does not
    case(
        "wait-13-blocked",
        "wait",
        13,
        wait_run("blocked", reason="why", summary="what"),
        "escalated",
        None,
        True,
        13,
    ),  # fmt: skip
    case(
        "wait-13-needs-input",
        "wait",
        13,
        wait_run("needs-input", reason="why", summary="what"),
        "escalated",
        None,
        True,
        13,
    ),  # fmt: skip
    case("launch-13-hook", "launch", 13, error_doc("escalated"), "escalated", None, False, 13),
    case("resume-13-hook", "resume", 13, error_doc("escalated"), "escalated", None, False, 13),
    case("wait-13-hook-run", "wait", 13, wait_run("escalated"), "escalated", None, False, 13),
    # 14 to 18
    case("wait-14", "wait", 14, wait_run("exited-unreported"), "failed", "unreported", True, 12),
    case(
        "wait-15-stopped-flag",
        "wait",
        15,
        wait_run("killed", end_reason="stopped"),
        "canceled",
        None,
        True,
        0,
        flag="stopped",
    ),  # fmt: skip
    *[
        case(
            f"wait-15-{why}-flag",
            "wait",
            15,
            wait_run("killed", end_reason=why),
            "canceled",
            None,
            True,
            0,
            flag="stopped",
        )  # fmt: skip
        for why in ("forwarded_term", "forwarded_hup", "forwarded_int")
    ],
    case(
        "wait-15-stopped-no-flag",
        "wait",
        15,
        wait_run("killed", end_reason="stopped"),
        "failed",
        "agent_failed",
        True,
        12,
    ),  # fmt: skip
    case(
        "wait-15-other-reason-flag",
        "wait",
        15,
        wait_run("killed", end_reason="internal_error"),
        "failed",
        "agent_failed",
        True,
        12,
        flag="stopped",
    ),  # fmt: skip
    case("wait-16", "wait", 16, wait_run("timeout"), "failed", "timeout", True, 12),
    case("wait-17", "wait", 17, wait_run("lost"), "failed", "agent_lost", True, 12),
    case("wait-18", "wait", 18, wait_run("failed"), "failed", "agent_failed", True, 12),
    # 20
    *[
        case(f"{c}-20", c, 20, error_doc("refused", reason="live"), "failed", "refused", False, 12)
        for c in ("resume", "launch", "wait")
    ],
    # 1, 2, 21 and 22 are internal
    *[
        case(f"{c}-{code}", c, code, error_doc(name), "failed", "internal", False, 1)
        for c in ("resume", "launch", "wait")
        for code, name in ((1, "internal"), (2, "usage"), (21, "ambiguous"))
    ],
    case(
        "wait-22-timed-out",
        "wait",
        22,
        result_doc(None, timed_out=True),
        "failed",
        "internal",
        False,
        1,
    ),  # fmt: skip
    # a signal exit: only `wait` re-reads `agent-spawn show`; the others are internal
    case("launch-143", "launch", 143, None, "failed", "internal", False, 1),
    case("resume-130", "resume", 130, None, "failed", "internal", False, 1),
]


def test_the_cases_cover_every_exit_the_table_names() -> None:
    seen = {(c.command, c.exit) for c in CASES}
    for command in ("resume", "launch", "wait"):
        for code in (0, 1, 2, 11, 12, 13, 20, 21):
            assert (command, code) in seen, (command, code)
    for code in (10, 14, 15, 16, 17, 18, 22):
        assert ("wait", code) in seen, code
    assert ("resume", 10) in seen


def test_the_pinned_outcomes_are_the_ones_the_helper_builds() -> None:
    assert {o: row[0] for o, row in OUTCOMES.items()} == {
        "done": 0, "blocked": 13, "needs-input": 13, "failed": 18, "exited-unreported": 14,
        "killed": 15, "timeout": 16, "lost": 17, "not-started": 12, "vetoed": 11, "escalated": 13,
    }  # fmt: skip


def _run(gateway: Gateway, command: str) -> Result:
    if command == "resume":
        return gateway.agent_spawn.resume_fork(BASE, **FORK_KWARGS)  # type: ignore[arg-type]
    if command == "launch":
        return gateway.agent_spawn.launch(repo=REPO, **FORK_KWARGS)  # type: ignore[arg-type]
    return gateway.agent_spawn.wait(AG)


def _schema(c: Case) -> str | bool:
    if c.doc is None:
        return False
    return (
        f"agent-spawn/cli/{c.command}.json"
        if "error" not in c.doc
        else "agent-spawn/cli/error.json"
    )


@pytest.mark.parametrize("c", CASES, ids=[c.id for c in CASES])
def test_a_real_process_end_maps_to_the_round_the_table_names(
    env: Env, gateway: Gateway, fakes: Fakes, c: Case
) -> None:
    rid = env.running()
    if c.flag is not None:
        env.rounds.request_cancel(rid, c.flag)
    fakes.script("agent-spawn", c.command, json=c.doc, exit=c.exit, schema=_schema(c))
    result = _run(gateway, c.command)
    assert result.returncode == c.exit  # the process, not a stand-in, ended with this code
    artifact_failure, detail = c.artifact if c.artifact else (None, None)
    end, escalation = run_end(c.command, result, artifact_failure=artifact_failure, detail=detail)

    got = env.rounds.finish_run(rid, end, escalation=escalation)

    row = env.row(rid)
    assert row.state == c.to
    assert row.failure_reason == c.reason
    assert row.counted is c.counted
    flagged = map_run_end(dataclasses.replace(end, cancel_requested=c.flag is not None))
    assert flagged.job_exit == c.job_exit
    if c.to == "running":
        assert got.changed is c.counted  # only a run that reached working changes the row
    else:
        assert got.changed
    if c.to == "escalated":
        assert (row.escalation_cause == "hook") is (c.job_exit == 13 and not c.counted)
        if c.counted:
            assert (row.escalation_reason, row.escalation_summary) == ("why", "what")
    if c.reason == "artifact_invalid":
        assert row.failure_detail == "schema"


@pytest.mark.parametrize("code", [130, 143])
def test_a_signal_exit_of_wait_is_re_read_not_an_outcome(
    env: Env, gateway: Gateway, fakes: Fakes, code: int
) -> None:
    rid = env.running()
    fakes.script("agent-spawn", "wait", exit=code, schema=False)
    result = _run(gateway, "wait")
    assert result.returncode == code
    end, _ = run_end("wait", result)
    with pytest.raises(ValueError, match="re-read"):
        env.rounds.finish_run(rid, end)
    assert env.row(rid).state == "running"


def test_every_failure_reason_the_table_names_is_produced_by_a_process_case() -> None:
    produced = {c.reason for c in CASES if c.reason is not None}
    # throwaway_failed (wtctl), job_terminated and job_lost are not agent-spawn exits
    assert set(FAILURE_REASONS) - produced == {"throwaway_failed", "job_terminated", "job_lost"}
