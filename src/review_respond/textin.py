"""Free text comes only from a file or stdin, never from argv (design guide section 4.1)."""

from __future__ import annotations

import os
import stat
import sys

from .errors import CliError


def _read_regular(path: str, flag: str, max_bytes: int) -> bytes:
    """Read a regular file. It is opened without blocking, so a FIFO nobody writes to is refused
    at once instead of hanging the command."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise CliError("usage", f"{flag}: not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read(max_bytes + 1)
    finally:
        os.close(fd)


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
            raw = _read_regular(path, flag, max_bytes)
    except CliError:
        raise
    except (OSError, ValueError) as exc:
        raise CliError("usage", f"{flag}: cannot read the file ({type(exc).__name__})") from None
    if len(raw) > max_bytes:
        raise CliError("usage", f"{flag}: the text is larger than {max_bytes} bytes")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise CliError("usage", f"{flag}: the text is not valid UTF-8") from None
