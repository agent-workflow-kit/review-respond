"""The run-outcome table (docs/interface.md: Run outcomes) as a pure function.

``map_run_end`` takes what ended a round's run (an exit code of ``resume``, ``launch`` or
``wait``, a failed throwaway, a SIGTERM of the job, a job that is gone) and says which state the
round goes to, the closed ``failure.reason``, whether the round counts toward ``max_rounds``, and
the job's own exit code. It reads nothing and writes nothing; ``rounds.Rounds.finish_run``
applies the answer.

Readings the table leaves open are the conservative ones: an exit code or a command the table does
not name is ``failed`` with reason ``internal`` (job exit 1); a counted run is never uncounted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

FAILURE_REASONS: Final = (
    "throwaway_failed",
    "launch_failed",
    "vetoed",
    "refused",
    "agent_gone",
    "unreported",
    "agent_failed",
    "agent_lost",
    "timeout",
    "job_terminated",
    "job_lost",
    "artifact_missing",
    "artifact_invalid",
    "implicit_done",
    "empty_result",
    "diff_failed",
    "internal",
)
ARTIFACT_FAILURES: Final = (
    "artifact_missing",
    "artifact_invalid",
    "implicit_done",
    "empty_result",
    "diff_failed",
)
FAILURE_DETAILS: Final = (
    "frozen_file",
    "json",
    "schema",
    "skill_marker",
    "replies",
    "paths",
    "commit_message",
    "path_state",
)
COMMANDS: Final = ("resume", "launch", "wait", "wtctl_create", "sigterm", "job_gone")
_AGENT_SPAWN: Final = ("resume", "launch", "wait")
STOP_REASONS: Final = ("stopped", "forwarded_term", "forwarded_hup", "forwarded_int")


@dataclass(frozen=True)
class RunEnd:
    """What happened. ``started`` is Counting's second signal (``first_working_at`` is set);
    ``artifact_failure`` is the validation or diff failure of a ``done`` run, if there was one;
    ``detail`` is the closed ``failure.detail`` of ``artifact_invalid`` (and of nothing else)."""

    command: str
    exit: int | None = None
    phase: str | None = None
    outcome: str | None = None
    end_reason: str | None = None
    cancel_requested: bool = False
    started: bool = False
    artifact_failure: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.command not in COMMANDS:
            raise ValueError(f"unknown command {self.command!r}")
        if self.artifact_failure is not None and self.artifact_failure not in ARTIFACT_FAILURES:
            raise ValueError("artifact_failure is not an artifact failure reason")
        if (self.artifact_failure == "artifact_invalid") != (self.detail is not None):
            raise ValueError("a detail belongs to artifact_invalid, and artifact_invalid has one")
        if self.detail is not None and self.detail not in FAILURE_DETAILS:
            raise ValueError("detail is not a closed failure detail")


@dataclass(frozen=True)
class Mapped:
    """``to`` is a round state (``pending``, ``escalated``, ``canceled``, ``failed``), ``running``
    (the round stays running) or ``reread`` (read ``agent-spawn show`` and map its last run)."""

    to: str
    failure_reason: str | None
    counted: bool
    job_exit: int | None
    cause: str | None = None


def _failed(reason: str, counted: bool, job_exit: int | None = 12) -> Mapped:
    return Mapped("failed", reason, counted, job_exit)


def _internal() -> Mapped:
    return _failed("internal", False, 1)


def _map(end: RunEnd) -> Mapped:
    command, code = end.command, end.exit
    if command == "job_gone":
        return _failed("job_lost", False, None)
    if command == "sigterm":
        if end.cancel_requested:
            return Mapped("canceled", None, False, 0)
        return _failed("job_terminated", False)
    if command == "wtctl_create":
        if code is None or code == 0:
            return _internal()
        return _failed("throwaway_failed", False)
    if code == 0:
        if command != "wait":
            return Mapped("running", None, False, None)
        if end.outcome not in (None, "done"):
            return _internal()
        if end.artifact_failure is not None:
            return _failed(end.artifact_failure, True)
        return Mapped("pending", None, True, 0)
    if code in (130, 143) and command == "wait":
        return Mapped("reread", None, False, None)
    if code == 10:
        if command == "resume":
            return _failed("agent_gone", False)
        if command == "wait":
            return _failed("agent_lost", False)
        return _internal()
    if code == 11:
        return _failed("vetoed", False)
    if code == 12:
        if end.cancel_requested:
            return Mapped("canceled", None, False, 0)
        return _failed("launch_failed", False)
    if code == 13:
        if end.phase == "session":
            return Mapped("escalated", None, True, 13)
        if end.phase == "launch":
            return Mapped("escalated", None, False, 13, "hook")
        return _internal()
    if code == 14:
        return _failed("unreported", True)
    if code == 15:
        if end.cancel_requested and end.end_reason in STOP_REASONS:
            return Mapped("canceled", None, True, 0)
        return _failed("agent_failed", True)
    if code == 16:
        return _failed("timeout", True)
    if code == 17:
        return _failed("agent_lost", True)
    if code == 18:
        return _failed("agent_failed", True)
    if code == 20:
        return _failed("refused", False)
    return _internal()


def map_run_end(end: RunEnd) -> Mapped:
    """The table row for ``end``; a run that reached ``working`` counts whatever ended it."""
    mapped = _map(end)
    if end.started and not mapped.counted and mapped.to != "reread":
        return Mapped(mapped.to, mapped.failure_reason, True, mapped.job_exit, mapped.cause)
    return mapped
