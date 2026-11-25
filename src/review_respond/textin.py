"""Free text comes only from a file or stdin, never from argv (design guide section 4.1)."""

from __future__ import annotations

import sys
from pathlib import Path

from .errors import CliError


def read_text_arg(path: str, *, flag: str, max_bytes: int) -> str:
    """Read the text a ``--<name>-file`` flag names; ``-`` is stdin.

    Reads at most ``max_bytes`` plus one byte, so a huge file or an endless stdin never loads
    whole. Exit 2 (usage) when it is too large, not UTF-8 or unreadable.
    """
    if path == "":
        raise CliError("usage", f"{flag} needs a path or -")
    try:
        if path == "-":
            raw = sys.stdin.buffer.read(max_bytes + 1)
        else:
            with Path(path).open("rb") as handle:
                raw = handle.read(max_bytes + 1)
    except (OSError, ValueError) as exc:
        raise CliError("usage", f"{flag}: cannot read the file ({type(exc).__name__})") from None
    if len(raw) > max_bytes:
        raise CliError("usage", f"{flag}: the text is larger than {max_bytes} bytes")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise CliError("usage", f"{flag}: the text is not valid UTF-8") from None
