"""agent-spawn answers as the pinned schemas describe them, and what `_job` builds from them.

``result_doc``, ``error_doc`` and friends build the JSON the pinned ``agent-spawn`` prints for
``resume``, ``launch`` and ``wait`` (they are validated against the pinned schemas when a test
scripts them). ``run_end`` is the test-side stand-in for the runner of #23: it reads one finished
process (exit code and envelope) and builds the ``RunEnd`` the run-outcome table maps. The real
runner is #23's; nothing here is production code.
"""

from __future__ import annotations

from typing import Any

from review_respond.gateway import Result
from review_respond.outcomes import RunEnd

TS = "2026-01-01T12:00:00Z"
AGENT = "ag-abcdefgh"
BASE = "ag-bcdefghi"
SESSION = "cs-abcdefgh"

# outcome -> (exit code, final state, report status or None, default end_reason, phase)
OUTCOMES: dict[str, tuple[int, str, str | None, str, str]] = {
    "done": (0, "exited", "done", "normal", "session"),
    "blocked": (13, "exited", "blocked", "normal", "session"),
    "needs-input": (13, "exited", "needs-input", "normal", "session"),
    "failed": (18, "exited", "failed", "api_error", "session"),
    "exited-unreported": (14, "exited-unreported", None, "no_report", "session"),
    "killed": (15, "killed", None, "stopped", "session"),
    "timeout": (16, "timeout", None, "timeout", "session"),
    "lost": (17, "lost", None, "lost", "session"),
    "not-started": (12, "not-started", None, "config_invalid", "launch"),
    "vetoed": (11, "not-started", None, "hook_veto", "launch"),
    "escalated": (13, "not-started", None, "hook_escalate", "launch"),
}
_ERROR_FIELDS: dict[str, Any] = {
    "reason": None,
    "agent_id": None,
    "tool": "agent-spawn",
    "lower_code": None,
    "lower_error": None,
    "details": None,
    "candidates": [],
    "hook": None,
    "hook_stderr": None,
    "decision_ids": [],
}


def agent_doc(**over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "agent_id": AGENT, "status": "parked", "needs_attention": None, "park_reason": None,
        "kind": "respond", "run_kind": "respond", "escalated_by": None, "mode": "headless",
        "placement": None, "term_placement": None, "name": "respond abcdef",
        "repo": "git.example.test/org/repo", "cwd": "/work/throwaway", "labels": [],
        "csession_id": SESSION, "run": 1, "session_state": "exited", "activity": None,
        "needs_input_reason": None, "forked_from": BASE, "worktree": None, "holds": [],
        "window": None, "promote": False, "promotion_pending": False, "ephemeral": False,
        "last_outcome": None, "last_end_reason": None, "tainted": False,
        "supervisor_lost": False, "teardown_step": None, "created": TS, "last_live_at": None,
        "updated": TS, "torn_down_at": None, "warnings": [],
    }  # fmt: skip
    doc.update(over)
    return doc


def outcome_doc(
    outcome: str, *, end_reason: str | None = None, reason: str | None = None,
    summary: str | None = None, first_working: bool = True,
) -> dict[str, Any]:  # fmt: skip
    """The ``run`` object of a final run with ``outcome``."""
    code, state, report_status, default_reason, phase = OUTCOMES[outcome]
    report = (
        None
        if report_status is None
        else {
            "status": report_status,
            "implicit": False,
            "summary": summary,
            "reason": reason,
            "artifact": None,
            "reported_at": TS,
        }  # fmt: skip
    )
    return {
        "agent_id": AGENT, "csession_id": SESSION, "run": 1, "forked_from": None,
        "claude_session_id": None, "phase": phase, "outcome": outcome,
        "end_reason": end_reason or default_reason, "exit_code": code, "state": state,
        "report": report, "session_started_at": None, "cost_usd": None, "message": None,
        "warnings": [], "promoted": False, "auto_resumed": False,
        "first_working_at": TS if first_working and phase == "session" else None,
    }  # fmt: skip


def result_doc(run: dict[str, Any] | None, *, timed_out: bool = False) -> dict[str, Any]:
    """The result object of ``resume``, ``launch`` and ``wait`` (``run`` null: no final run)."""
    return {
        "agent": agent_doc(),
        "phase": None if run is None else run["phase"],
        "outcome": None if run is None else run["outcome"],
        "end_reason": None if run is None else run["end_reason"],
        "exit_code": None if run is None else run["exit_code"],
        "run": run,
        "first_working_at": None if run is None else run["first_working_at"],
        "timed_out": timed_out,
        "warnings": [],
    }


def started_doc(*, first_working: bool) -> dict[str, Any]:
    """``resume`` or ``launch`` without ``--wait``: the run started, no final run yet."""
    doc = result_doc(None)
    doc["first_working_at"] = TS if first_working else None
    return doc


def error_doc(code: str, *, reason: str | None = None) -> dict[str, Any]:
    fields = dict(_ERROR_FIELDS, reason=reason)
    if code == "ambiguous":
        fields["candidates"] = [AGENT, BASE]
    return {"error": {"code": code, "message": f"{code} (scripted)", **fields}}


# The launch phase of an envelope: what ended the run before claude was spawned.
_LAUNCH_ERRORS = {
    "escalated": "escalated",
    "vetoed": "vetoed",
    "launch_failed": "not-started",
    "no_run": "not-started",
    "tool_missing": "not-started",
}


def run_end(
    command: str,
    result: Result,
    *,
    artifact_failure: str | None = None,
    detail: str | None = None,
) -> tuple[RunEnd, tuple[str | None, str | None]]:
    """The ``RunEnd`` of a finished ``agent-spawn`` process and the report's (reason, summary).

    The cancel flag is not read here: ``Rounds.finish_run`` reads it from the round row.
    """
    doc = result.json if isinstance(result.json, dict) else None
    phase = outcome = end_reason = None
    started = False
    escalation: tuple[str | None, str | None] = (None, None)
    if doc is not None and "error" in doc:
        outcome = _LAUNCH_ERRORS.get(doc["error"]["code"])
        phase = None if outcome is None else "launch"
    elif doc is not None:
        started = doc.get("first_working_at") is not None
        run = doc.get("run")
        if isinstance(run, dict):
            phase, outcome, end_reason = run["phase"], run["outcome"], run["end_reason"]
            report = run["report"]
            if report is not None:
                escalation = (report["reason"], report["summary"])
    end = RunEnd(
        command=command,
        exit=result.returncode,
        phase=phase,
        outcome=outcome,
        end_reason=end_reason,
        started=started,
        artifact_failure=artifact_failure,
        detail=detail,
    )
    return end, escalation
