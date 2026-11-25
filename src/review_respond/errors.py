"""Exit codes, error codes and the error envelope (docs/interface.md: Exit codes, Error codes).

Pure data and pure functions: no I/O. ``CliError`` is the one exception a command raises to end
with a documented exit code; ``error_envelope`` builds the ``{"error": {...}}`` document that
``schemas/cli/error.json`` describes.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any, Final

EXIT_OK: Final = 0
EXIT_INTERNAL: Final = 1
EXIT_USAGE: Final = 2
EXIT_NOT_FOUND: Final = 10
EXIT_VETOED: Final = 11
EXIT_FAILED: Final = 12
EXIT_NEEDS_HUMAN: Final = 13
EXIT_REFUSED: Final = 20
EXIT_AMBIGUOUS: Final = 21
EXIT_NOTHING_TO_RESPOND: Final = 22

# error.code -> exit code (Error codes table). Exit 20 is always `refused` with a `reason`.
CODE_EXIT: Final[dict[str, int]] = {
    **dict.fromkeys(("internal", "lease_lost", "lease_timeout"), EXIT_INTERNAL),
    **dict.fromkeys(("usage", "config_invalid", "human_only"), EXIT_USAGE),
    "not_found": EXIT_NOT_FOUND,
    "vetoed": EXIT_VETOED,
    **dict.fromkeys(
        (
            "tool_missing",
            "tool_too_old",
            "lower_failed",
            "all_hosts_failed",
            "push_failed",
            "reply_failed",
            "outcome_unknown",
            "identity_missing",
            "throwaway_missing",
            "state_unsafe",
            "version_skew",
        ),
        EXIT_FAILED,
    ),
    **dict.fromkeys(("confirmation_required", "escalated"), EXIT_NEEDS_HUMAN),
    "refused": EXIT_REFUSED,
    "ambiguous": EXIT_AMBIGUOUS,
    "nothing_to_respond": EXIT_NOTHING_TO_RESPOND,
}

EXIT_CODES: Final = frozenset({EXIT_OK, *CODE_EXIT.values()})

REFUSAL_REASONS: Final = frozenset(
    {
        "live",
        "busy",
        "pending_exists",
        "round_active",
        "round_limit",
        "remote_moved",
        "diff_changed",
        "sensitive_paths",
        "expired",
        "not_pending",
        "ineligible",
        "pr_closed",
    }
)

MAX_MESSAGE_CHARS: Final = 4096
_LOWER_NAME_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")


def exit_code_for(code: str) -> int:
    """The exit code of an ``error.code``; ``ValueError`` for an unknown code."""
    try:
        return CODE_EXIT[code]
    except (KeyError, TypeError):
        raise ValueError("unknown error code") from None


def _cut(text: str) -> str:
    return text if len(text) <= MAX_MESSAGE_CHARS else text[:MAX_MESSAGE_CHARS]


def lower_error(error: object) -> dict[str, Any]:
    """Shape a sibling's error object as ``lower_error`` (``{code, message?, reason?, details?}``).

    The sibling's output is untrusted: a ``code`` or ``reason`` that is not a lowercase name
    becomes ``unknown``/is dropped, a message is cut to 4096 characters, ``details`` must be an
    object, and any other member is dropped.
    """
    source: Mapping[str, Any] = error if isinstance(error, Mapping) else {}  # pyright: ignore[reportUnknownVariableType]
    code = source.get("code")
    result: dict[str, Any] = {
        "code": code if isinstance(code, str) and _LOWER_NAME_RE.fullmatch(code) else "unknown"
    }
    message = source.get("message")
    if isinstance(message, str):
        result["message"] = _cut(message)
    reason = source.get("reason")
    if reason is None or (isinstance(reason, str) and _LOWER_NAME_RE.fullmatch(reason)):
        if reason is not None:
            result["reason"] = reason
    details = source.get("details")
    if isinstance(details, Mapping):
        result["details"] = copy.deepcopy(dict(details))  # pyright: ignore[reportUnknownArgumentType]
    return result


class CliError(Exception):
    """End the command with ``code``'s exit code and the error envelope.

    ``reason`` is required with ``refused`` and forbidden otherwise; ``lower_error`` is required
    with ``lower_failed`` and forbidden otherwise. A violation is a programming error
    (``ValueError``) so that a bad envelope never reaches a caller.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        reason: str | None = None,
        details: Mapping[str, Any] | None = None,
        lower: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code_for(code)
        if not isinstance(message, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError("message must be str")
        if code == "refused":
            if reason not in REFUSAL_REASONS:
                raise ValueError("refused needs a reason from the refusal list")
        elif reason is not None:
            raise ValueError("reason is only valid with code refused")
        if code == "lower_failed":
            if lower is None:
                raise ValueError("lower_failed needs lower_error")
        elif lower is not None:
            raise ValueError("lower_error is only valid with code lower_failed")
        self.code = code
        self.message = _cut(message)
        self.reason = reason
        self.details = copy.deepcopy(dict(details)) if details is not None else None
        self.lower = lower_error(lower) if lower is not None else None

    def body(self) -> dict[str, Any]:
        """The envelope's inner object: only the members that apply are present."""
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.reason is not None:
            body["reason"] = self.reason
        if self.details is not None:
            body["details"] = copy.deepcopy(self.details)
        if self.lower is not None:
            body["lower_error"] = copy.deepcopy(self.lower)
        return body


def error_envelope(error: CliError) -> dict[str, Any]:
    """The ``{"error": {...}}`` document printed on stdout with ``--json``."""
    return {"error": error.body()}


def usage_error(message: str) -> CliError:
    return CliError("usage", message)
