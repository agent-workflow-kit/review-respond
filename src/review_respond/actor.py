"""Actor and decision-id handling (design guide section 6; docs/interface.md: Global flags)."""

from __future__ import annotations

import os
import pwd
import re
import sys
from collections.abc import Iterable, Mapping

from .errors import CliError

_NAME = r"[A-Za-z0-9._@-]{1,64}"
_ACTOR_RE = re.compile(rf"(?:human|auto):{_NAME}")
_DECISION_ID_RE = re.compile(r"[A-Za-z0-9._:@/-]{1,128}")
MAX_DECISION_IDS = 16  # keep in sync with schemas/defs.json decision_ids.maxItems

# Flags only a human actor may use (interface: Global flags, `--yes`).
HUMAN_ONLY_FLAGS = ("yes", "force", "allow_sensitive", "override_escalation")


def parse_actor(text: str) -> str:
    """Validate an ``--actor`` value (``human:<user>`` or ``auto:<name>``)."""
    if not _ACTOR_RE.fullmatch(text):
        raise CliError("usage", "--actor must be human:<user> or auto:<name>")
    return text


def parse_decision_id(text: str) -> str:
    if not _DECISION_ID_RE.fullmatch(text):
        raise CliError("usage", "--decision-id has an invalid value")
    return text


def check_decision_ids(ids: Iterable[str]) -> list[str]:
    result = [parse_decision_id(item) for item in ids]
    if len(result) > MAX_DECISION_IDS:
        raise CliError("usage", f"--decision-id given more than {MAX_DECISION_IDS} times")
    return result


def stdin_is_tty() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (ValueError, OSError):
        return False


def os_user_name() -> str:
    """The login name from the password database, characters outside the name set replaced."""
    try:
        name = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        name = "unknown"
    cleaned = re.sub(r"[^A-Za-z0-9._@-]", "_", name)[:64]
    return cleaned or "unknown"


def default_actor(*, tty: bool | None = None, user: str | None = None) -> str:
    """``human:<user>`` on a TTY, ``auto:unknown`` otherwise.

    ``user`` is ``me.name`` from the loaded config when it is set (the caller passes it), else
    the OS user name.
    """
    is_tty = stdin_is_tty() if tty is None else tty
    if not is_tty:
        return "auto:unknown"
    return f"human:{user if user is not None else os_user_name()}"


def is_automatic(actor: str) -> bool:
    return actor.startswith("auto:")


def check_human_only(actor: str, flags: Mapping[str, bool]) -> None:
    """Exit 2 ``human_only`` when an automatic actor uses a human-only flag."""
    if not is_automatic(actor):
        return
    used = [name for name in HUMAN_ONLY_FLAGS if flags.get(name)]
    if used:
        flag_list = ", ".join("--" + name.replace("_", "-") for name in used)
        raise CliError(
            "human_only",
            f"{flag_list} needs a human actor (a TTY, or --actor human:<user>)",
            details={"flags": [name.replace("_", "-") for name in used]},
        )
