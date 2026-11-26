"""The sibling programs review-respond needs: minimum versions and the probe (interface:
Dependencies).

Every program is run through the gateway, only as ``<tool> --version``. ``validate`` reports each
one; any other command calls ``require`` and exits 12 ``tool_missing`` or ``tool_too_old``.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from .errors import CliError
from .gateway import Gateway, GatewayError, Result, parse_version

_ORG_URL: Final = "https://github.com/agent-workflow-kit"
# A version line is short; reading only its start keeps a hostile output from costing anything
# (and keeps ``int()`` away from a many-thousand-digit run).
_MAX_VERSION_TEXT: Final = 1024
_MAX_FOUND_CHARS: Final = 64  # schemas/cli/validate.json


@dataclass(frozen=True)
class Dependency:
    tool: str
    minimum: str  # as written in the Dependencies table
    install: str

    @property
    def floor(self) -> tuple[int, int, int]:
        parts = [int(p) for p in self.minimum.split(".")]
        parts += [0] * (3 - len(parts))
        return parts[0], parts[1], parts[2]


def _uv(tool: str) -> str:
    return f"uv tool install git+{_ORG_URL}/{tool}"


# The Minimum column of docs/interface.md, in its order (the order `validate` reports).
DEPENDENCIES: Final = (
    Dependency("forgectl", "0.55.2", _uv("forgectl")),
    Dependency("agent-spawn", "0.34.0", _uv("agent-spawn")),
    Dependency("pr-comment", "0.33.0", _uv("pr-comment")),
    Dependency("wtctl", "0.31.0", _uv("wtctl")),
    Dependency("claude-session", "0.45.2", _uv("claude-session")),
    Dependency("git", "2.34", "install git 2.34 or newer with your package manager"),
    Dependency("jobsctl", "0.32.1", _uv("jobsctl")),
)
BY_TOOL: Final = {dep.tool: dep for dep in DEPENDENCIES}
# Required whatever the config says; claude-session depends on `sandbox`, jobsctl is optional.
ALWAYS_REQUIRED: Final = frozenset({"forgectl", "agent-spawn", "pr-comment", "wtctl", "git"})


@dataclass(frozen=True)
class DepStatus:
    tool: str
    required: bool
    minimum: str
    found: str | None
    ok: bool
    code: str | None  # tool_missing | tool_too_old | None
    unreadable: bool  # the version could not be read (a warning, never a failure)
    message: str  # names the install command when not ok; for display only


def _versions(gateway: Gateway) -> dict[str, Callable[[], Result]]:
    return {
        "forgectl": gateway.forgectl.version,
        "agent-spawn": gateway.agent_spawn.version,
        "pr-comment": gateway.pr_comment.version,
        "wtctl": gateway.wtctl.version,
        "claude-session": gateway.claude_session.version,
        "git": gateway.git.version,
        "jobsctl": gateway.jobsctl.version,
    }


def probe(gateway: Gateway, dep: Dependency, *, required: bool) -> DepStatus:
    """Run ``<tool> --version`` and compare it with the minimum.

    A program that cannot be started, exits non-zero or times out counts as missing. One that
    prints no readable version, or ``0.0.0`` (a package that hard-codes its version), is
    "unreadable": it passes with a warning. An optional program that is absent passes.
    """
    # Resolve first: an absent program is "missing" without trying to start it.
    if shutil.which(dep.tool, path=gateway.env.get("PATH")) is None:
        return _absent(dep, required)
    try:
        result = _versions(gateway)[dep.tool]()
    except GatewayError:
        return _absent(dep, required)
    if not result.ok:
        return _absent(dep, required)
    try:
        triple = parse_version(result.stdout[:_MAX_VERSION_TEXT])
    except ValueError:
        triple = None
    if triple is None or triple == (0, 0, 0):
        found = "0.0.0" if triple == (0, 0, 0) else None
        return DepStatus(dep.tool, required, dep.minimum, found, True, None, True, "")
    found = ".".join(str(n) for n in triple)
    if len(found) > _MAX_FOUND_CHARS:
        return DepStatus(dep.tool, required, dep.minimum, None, True, None, True, "")
    if triple < dep.floor:
        message = (
            f"{dep.tool} {found} is older than the minimum {dep.minimum}; upgrade: {dep.install}"
        )
        return DepStatus(
            dep.tool, required, dep.minimum, found, False, "tool_too_old", False, message
        )
    return DepStatus(dep.tool, required, dep.minimum, found, True, None, False, "")


def _absent(dep: Dependency, required: bool) -> DepStatus:
    if not required:
        return DepStatus(dep.tool, required, dep.minimum, None, True, None, False, "")
    message = f"{dep.tool} is not installed or does not run; install it: {dep.install}"
    return DepStatus(dep.tool, required, dep.minimum, None, False, "tool_missing", False, message)


def require(gateway: Gateway, *tools: str) -> None:
    """Exit 12 ``tool_missing`` or ``tool_too_old`` for the first of ``tools`` that fails."""
    for tool in tools:
        status = probe(gateway, BY_TOOL[tool], required=True)
        if not status.ok and status.code is not None:
            raise CliError(status.code, status.message)
