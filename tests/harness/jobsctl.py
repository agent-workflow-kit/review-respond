"""jobsctl answers as the pinned schemas describe them (validated when a test scripts them): the
job object ``add`` prints and the job ``show`` prints, by status."""

from __future__ import annotations

from typing import Any

TS = "2026-01-01T12:00:00.000Z"
JOB = "jb-abcdefgh"


def job_doc(
    job_id: str = JOB,
    *,
    status: str = "queued",
    reason: str | None = None,
    detail: bool = False,
    unique_key: str | None = "respond:0:1",
    **over: Any,
) -> dict[str, Any]:
    """A job in ``status``; ``detail`` adds what ``show`` prints (``attempts``, ``output``)."""
    terminal = status in ("done", "failed", "canceled")
    if terminal and reason is None:
        reason = {"done": "exit", "failed": "missing_env", "canceled": "canceled"}[status]
    doc: dict[str, Any] = {
        "id": job_id, "queue": "respond", "status": status, "reason": reason,
        "hold_reason": "exit_13" if status == "held" else None,
        "argv": ["/usr/bin/review-respond", "_job", "rd-abcdefgh"], "cwd": "/state",
        "pass_env": [], "labels": [], "unique_key": unique_key, "priority": 0, "pinned": False,
        "rank": 1 if status == "queued" else None, "attempt": 0 if status == "queued" else 1,
        "retried_from_attempt": None, "overrides": {},
        "effective": {"timeout": None, "max_retries": 0, "kill_grace": 125},
        "not_before": None, "blocked_by": None, "force_requested_by": None, "override_hooks": [],
        "shim_lost": False, "created_at": TS, "enqueued_at": TS, "status_changed_at": TS,
        "claimed_at": None if status == "queued" else TS, "ended_at": TS if terminal else None,
        "actor": "auto:review-respond", "decision_ids": [], "exit": None, "leftovers": None,
        "resolved_path": None, "resolved_by": None, "note": None, "env": {},
    }  # fmt: skip
    doc.update(over)
    if detail:
        doc.update({"attempts": [], "output": None})
    return doc


def add_doc(job_id: str = JOB, *, created: bool = True, **over: Any) -> dict[str, Any]:
    """``jobsctl add --json``."""
    return {
        "created": created, "queue_created": False, "dry_run": False, "waited": False,
        "warnings": [], "job": job_doc(job_id, **over),
    }  # fmt: skip
