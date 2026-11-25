"""The duration grammar and RFC 3339 times for flag values (docs/interface.md: Config).

A duration is ``0`` or an integer and a unit: ``ms``, ``s``, ``m``, ``h`` or ``d``. Errors name
the flag and never echo the value.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from .errors import CliError

_DURATION_RE = re.compile(r"(0|[0-9]{1,15})(ms|s|m|h|d)?")
_UNIT_MS = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}
_RFC3339_RE = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2})[Tt]([0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.([0-9]{1,9}))?([Zz]|[+-][0-9]{2}:[0-9]{2})"
)


def parse_duration(text: str, *, flag: str = "duration") -> timedelta:
    """Parse a duration; ``CliError`` (usage) naming ``flag`` when it is invalid."""
    match = _DURATION_RE.fullmatch(text)
    # A bare number is valid only as 0; any other number needs a unit.
    if match is None or (match.group(2) is None and match.group(1) != "0"):
        raise CliError("usage", f"{flag} is not a duration (an integer and ms, s, m, h or d)")
    unit = match.group(2) or "s"
    try:
        return timedelta(milliseconds=int(match.group(1)) * _UNIT_MS[unit])
    except OverflowError:
        raise CliError("usage", f"{flag} is out of range") from None


def parse_rfc3339(text: str, *, flag: str = "time") -> datetime:
    """Parse an RFC 3339 time that carries an offset; the result is timezone-aware."""
    match = _RFC3339_RE.fullmatch(text)
    if match is None:
        raise CliError("usage", f"{flag} is not an RFC 3339 time with an offset")
    day, clock, fraction, offset = match.groups()
    micro = (fraction or "0").ljust(6, "0")[:6]
    zone = "+00:00" if offset in ("Z", "z") else offset
    # A leap second (seconds == 60) is parsed as :59 plus one second.
    leap = clock.endswith(":60")
    if leap:
        clock = clock[:-2] + "59"
    try:
        parsed = datetime.fromisoformat(f"{day}T{clock}.{micro}{zone}").astimezone(UTC)
        return parsed + timedelta(seconds=1) if leap else parsed
    except (ValueError, OverflowError):
        raise CliError("usage", f"{flag} is not a valid RFC 3339 time") from None


def parse_since(text: str, *, now: datetime | None = None, flag: str = "--since") -> datetime:
    """``--since``: an RFC 3339 time with an offset, or a duration meaning that long ago.

    ``now`` is the injectable current time (callers pass the gateway clock's ``now()``); the system
    clock is only the default when none is given.
    """
    if re.match(r"[0-9]{4}-", text):
        return parse_rfc3339(text, flag=flag)
    current = now if now is not None else datetime.now(UTC)
    try:
        return current - parse_duration(text, flag=flag)
    except OverflowError:
        raise CliError("usage", f"{flag} is out of range") from None


def duration_ms(text: str) -> int:
    """A duration in milliseconds; ``ValueError`` when it is not a duration (config values)."""
    match = _DURATION_RE.fullmatch(text)
    if match is None or (match.group(2) is None and match.group(1) != "0"):
        raise ValueError("not a duration")
    return int(match.group(1)) * _UNIT_MS[match.group(2) or "s"]
