"""The run-outcome table (docs/interface.md: Run outcomes): one test row per documented outcome.

The expected values are written out from the interface table, independently of the code: each row
is (what happened, round state, failure reason, counted, `_job` exit code).
"""

from __future__ import annotations

from typing import Any

import pytest

from review_respond.outcomes import FAILURE_REASONS, Mapped, RunEnd, map_run_end

ARTIFACT_FAILURES = (
    "artifact_missing",
    "artifact_invalid",
    "implicit_done",
    "empty_result",
    "diff_failed",
)

# (id, RunEnd arguments, to, failure reason, counted, job exit)
ROWS: list[tuple[str, dict[str, Any], str, str | None, bool, int | None]] = [
    ("resume-0-run-started", dict(command="resume", exit=0), "running", None, False, None),
    ("launch-0-run-started", dict(command="launch", exit=0), "running", None, False, None),
    (
        "resume-0-first-working",
        dict(command="resume", exit=0, started=True),
        "running",
        None,
        True,
        None,
    ),
    (
        "wait-0-done-valid",
        dict(command="wait", exit=0, phase="session", outcome="done", end_reason="normal"),
        "pending",
        None,
        True,
        0,
    ),
    *[
        (
            f"wait-0-done-{why}",
            dict(
                command="wait",
                exit=0,
                phase="session",
                outcome="done",
                end_reason="normal",
                artifact_failure=why,
                detail="schema" if why == "artifact_invalid" else None,
            ),
            "failed",
            why,
            True,
            12,
        )
        for why in ARTIFACT_FAILURES
    ],
    ("resume-10-agent-gone", dict(command="resume", exit=10), "failed", "agent_gone", False, 12),
    ("wait-10-agent-lost", dict(command="wait", exit=10), "failed", "agent_lost", False, 12),
    (
        "wait-10-agent-lost-after-start",
        dict(command="wait", exit=10, started=True),
        "failed",
        "agent_lost",
        True,
        12,
    ),
    ("resume-11-vetoed", dict(command="resume", exit=11), "failed", "vetoed", False, 12),
    ("launch-11-vetoed", dict(command="launch", exit=11), "failed", "vetoed", False, 12),
    ("wait-11-vetoed", dict(command="wait", exit=11), "failed", "vetoed", False, 12),
    (
        "launch-12-launch-failed",
        dict(command="launch", exit=12, phase="launch", outcome="not-started"),
        "failed",
        "launch_failed",
        False,
        12,
    ),
    (
        "resume-12-launch-failed",
        dict(command="resume", exit=12),
        "failed",
        "launch_failed",
        False,
        12,
    ),
    (
        "wait-12-no-run-launch-failed",
        dict(command="wait", exit=12, outcome=None),
        "failed",
        "launch_failed",
        False,
        12,
    ),
    (
        "launch-12-canceled",
        dict(command="launch", exit=12, cancel_requested=True),
        "canceled",
        None,
        False,
        0,
    ),
    (
        "wait-12-canceled",
        dict(command="wait", exit=12, cancel_requested=True),
        "canceled",
        None,
        False,
        0,
    ),
    (
        "wait-13-session-escalated",
        dict(command="wait", exit=13, phase="session", outcome="blocked"),
        "escalated",
        None,
        True,
        13,
    ),
    (
        "wait-13-session-needs-input",
        dict(command="wait", exit=13, phase="session", outcome="needs-input"),
        "escalated",
        None,
        True,
        13,
    ),
    (
        "launch-13-hook-escalated",
        dict(command="launch", exit=13, phase="launch", outcome="escalated"),
        "escalated",
        None,
        False,
        13,
    ),
    (
        "wait-14-unreported",
        dict(command="wait", exit=14, phase="session", outcome="exited-unreported"),
        "failed",
        "unreported",
        True,
        12,
    ),
    *[
        (
            f"wait-15-killed-{why}-canceled",
            dict(command="wait", exit=15, outcome="killed", end_reason=why, cancel_requested=True),
            "canceled",
            None,
            True,
            0,
        )
        for why in ("stopped", "forwarded_term", "forwarded_hup", "forwarded_int")
    ],
    (
        "wait-15-killed-stopped-without-flag",
        dict(command="wait", exit=15, outcome="killed", end_reason="stopped"),
        "failed",
        "agent_failed",
        True,
        12,
    ),
    (
        "wait-15-killed-other-reason-with-flag",
        dict(
            command="wait",
            exit=15,
            outcome="killed",
            end_reason="internal_error",
            cancel_requested=True,
        ),
        "failed",
        "agent_failed",
        True,
        12,
    ),  # fmt: skip
    (
        "wait-15-killed-by-a-signal-no-flag",
        dict(command="wait", exit=15, outcome="killed", end_reason="signal_term"),
        "failed",
        "agent_failed",
        True,
        12,
    ),
    ("wait-16-timeout", dict(command="wait", exit=16), "failed", "timeout", True, 12),
    ("wait-17-lost", dict(command="wait", exit=17), "failed", "agent_lost", True, 12),
    ("wait-18-failed", dict(command="wait", exit=18), "failed", "agent_failed", True, 12),
    *[
        (f"{cmd}-20-refused", dict(command=cmd, exit=20), "failed", "refused", False, 12)
        for cmd in ("resume", "launch", "wait")
    ],
    *[
        (f"{cmd}-{code}-internal", dict(command=cmd, exit=code), "failed", "internal", False, 1)
        for cmd in ("resume", "launch", "wait")
        for code in (1, 2, 21, 22)
    ],
    (
        "wtctl-create-nonzero",
        dict(command="wtctl_create", exit=20),
        "failed",
        "throwaway_failed",
        False,
        12,
    ),
    (
        "wtctl-create-exit-1",
        dict(command="wtctl_create", exit=1),
        "failed",
        "throwaway_failed",
        False,
        12,
    ),
    ("sigterm-without-flag", dict(command="sigterm"), "failed", "job_terminated", False, 12),
    (
        "sigterm-without-flag-after-start",
        dict(command="sigterm", started=True),
        "failed",
        "job_terminated",
        True,
        12,
    ),
    ("job-gone", dict(command="job_gone"), "failed", "job_lost", False, None),
]


@pytest.mark.parametrize(
    ("args", "to", "failure", "counted", "job_exit"),
    [pytest.param(*row[1:], id=row[0]) for row in ROWS],
)
def test_every_documented_outcome_maps_as_the_table_says(
    args: dict[str, Any], to: str, failure: str | None, counted: bool, job_exit: int | None
) -> None:
    got = map_run_end(RunEnd(**args))
    assert (got.to, got.failure_reason, got.counted, got.job_exit) == (
        to,
        failure,
        counted,
        job_exit,
    )


def test_a_launch_phase_escalation_carries_the_hook_cause_and_a_session_one_does_not() -> None:
    hook = map_run_end(RunEnd(command="launch", exit=13, phase="launch", outcome="escalated"))
    session = map_run_end(RunEnd(command="wait", exit=13, phase="session", outcome="blocked"))
    assert hook.cause == "hook" and session.cause is None


@pytest.mark.parametrize("code", [130, 143])
def test_a_signal_exit_of_wait_is_never_an_outcome_by_itself(code: int) -> None:
    got = map_run_end(RunEnd(command="wait", exit=code))
    assert got == Mapped(to="reread", failure_reason=None, counted=False, job_exit=None, cause=None)


@pytest.mark.parametrize("command", ["resume", "launch"])
def test_a_signal_exit_of_resume_or_launch_is_internal_not_a_reread(command: str) -> None:
    got = map_run_end(RunEnd(command=command, exit=143))
    assert (got.to, got.failure_reason, got.job_exit) == ("failed", "internal", 1)


@pytest.mark.parametrize("code", [3, 9, 19, 23, 99, 255, -1])
def test_an_exit_code_the_table_does_not_name_is_internal(code: int) -> None:
    got = map_run_end(RunEnd(command="wait", exit=code))
    assert (got.to, got.failure_reason, got.counted, got.job_exit) == (
        "failed",
        "internal",
        False,
        1,
    )


def test_a_launch_exit_10_is_internal_because_the_table_names_only_resume_and_wait() -> None:
    got = map_run_end(RunEnd(command="launch", exit=10))
    assert (got.to, got.failure_reason) == ("failed", "internal")


def test_a_wait_exit_0_whose_outcome_is_not_done_is_internal() -> None:
    got = map_run_end(RunEnd(command="wait", exit=0, outcome="blocked", phase="session"))
    assert (got.to, got.failure_reason, got.counted, got.job_exit) == (
        "failed",
        "internal",
        False,
        1,
    )


def test_exit_13_without_a_phase_is_internal() -> None:
    got = map_run_end(RunEnd(command="wait", exit=13, phase=None))
    assert (got.to, got.failure_reason) == ("failed", "internal")


def test_a_run_that_reached_working_counts_even_when_its_outcome_row_says_no() -> None:
    # Counting: a run with a non-null first_working_at counts whatever ended it.
    got = map_run_end(RunEnd(command="wait", exit=20, started=True))
    assert got.counted is True


def test_a_wait_exit_0_ignores_an_artifact_failure_only_when_there_is_none() -> None:
    ok = map_run_end(RunEnd(command="wait", exit=0, phase="session", outcome="done"))
    assert ok.to == "pending"


def test_an_unknown_artifact_failure_name_is_refused() -> None:
    with pytest.raises(ValueError, match="failure"):
        map_run_end(
            RunEnd(command="wait", exit=0, phase="session", outcome="done", artifact_failure="x")
        )


def test_an_unknown_command_is_refused() -> None:
    with pytest.raises(ValueError, match="command"):
        RunEnd(command="stop", exit=0)


def test_the_rows_produce_every_closed_failure_reason_and_only_those() -> None:
    produced = {
        mapped.failure_reason
        for _, args, *_ in ROWS
        if (mapped := map_run_end(RunEnd(**args))).failure_reason is not None
    }
    # the table's closed list, written out from the interface
    assert produced == set(FAILURE_REASONS)
    assert produced == {
        "throwaway_failed", "launch_failed", "vetoed", "refused", "agent_gone", "unreported",
        "agent_failed", "agent_lost", "timeout", "job_terminated", "job_lost", "artifact_missing",
        "artifact_invalid", "implicit_done", "empty_result", "diff_failed", "internal",
    }  # fmt: skip


def test_sigterm_with_the_cancel_flag_ends_canceled_not_failed() -> None:
    got = map_run_end(RunEnd(command="sigterm", cancel_requested=True))
    assert (got.to, got.failure_reason, got.job_exit) == ("canceled", None, 0)


def test_artifact_invalid_carries_its_detail_in_the_run_end() -> None:
    end = RunEnd(
        command="wait", exit=0, outcome="done", artifact_failure="artifact_invalid", detail="json"
    )
    assert end.detail == "json"


def test_artifact_invalid_without_a_detail_is_refused_when_the_run_end_is_built() -> None:
    with pytest.raises(ValueError, match="detail"):
        RunEnd(command="wait", exit=0, outcome="done", artifact_failure="artifact_invalid")


@pytest.mark.parametrize(
    "failure", [None, *[a for a in ARTIFACT_FAILURES if a != "artifact_invalid"]]
)
def test_a_detail_with_anything_but_artifact_invalid_is_refused(failure: str | None) -> None:
    with pytest.raises(ValueError, match="detail"):
        RunEnd(command="wait", exit=0, outcome="done", artifact_failure=failure, detail="json")


def test_an_open_detail_is_refused() -> None:
    with pytest.raises(ValueError, match="detail"):
        RunEnd(
            command="wait", exit=0, outcome="done", artifact_failure="artifact_invalid", detail="x"
        )
