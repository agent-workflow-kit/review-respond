"""The duration grammar and RFC 3339 times for flag values (docs/interface.md: Config).

A duration is ``0`` or an integer and a unit: ``ms``, ``s``, ``m``, ``h`` or ``d``. Errors name
the flag and never echo the value.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from .errors import CliError

_DURATION_RE = re.compile(r"(0|[0-9]{1,15})(ms|s|m|h|d)?")
_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}
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
        return timedelta(seconds=int(match.group(1)) * _UNITS[unit])
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
    try:
        return datetime.fromisoformat(f"{day}T{clock}.{micro}{zone}")
    except ValueError:
        raise CliError("usage", f"{flag} is not a valid RFC 3339 time") from None


def parse_since(text: str, *, now: datetime | None = None, flag: str = "--since") -> datetime:
    """``--since``: an RFC 3339 time with an offset, or a duration meaning that long ago."""
    if re.match(r"[0-9]{4}-", text):
        return parse_rfc3339(text, flag=flag)
    current = now if now is not None else datetime.now(UTC)
    try:
        return current - parse_duration(text, flag=flag)
    except OverflowError:
        raise CliError("usage", f"{flag} is out of range") from None
