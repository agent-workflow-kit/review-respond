"""Result printing, the atomic ``--result-file`` and the per-run context."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Context:
    """The global flags of one run, after validation."""

    json: bool = False
    config: Path | None = None
    dry_run: bool = False
    yes: bool = False
    actor: str = "auto:unknown"
    override_escalation: bool = False
    decision_ids: tuple[str, ...] = ()
    result_file: Path | None = None
    verbose: bool = False
    quiet: bool = False


@dataclass(frozen=True)
class Outcome:
    """What a command handler returns.

    ``payload`` is the ``--json`` document (it validates against ``schemas/cli/<command>.json``);
    ``human`` is the text printed without ``--json``. A handler ending in a documented non-zero
    exit with a result object (``confirm`` and ``check`` on exit 12) returns it here with that
    ``exit_code`` instead of raising ``CliError``.
    """

    payload: dict[str, Any] = field(default_factory=lambda: {})
    human: str = ""
    exit_code: int = 0


def sanitize_line(text: str) -> str:
    """Mark control, format and separator characters so untrusted text cannot move the cursor."""
    return "".join(
        f"<U+{ord(ch):04X}>"
        if unicodedata.category(ch) in {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
        else ch
        for ch in text
    )


def dump_json(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a synced temporary file in the same directory."""
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def emit(document: dict[str, Any], human: str, exit_code: int, ctx: Context) -> int:
    """Print the result and write ``--result-file``; returns the exit code to use.

    The JSON document is written to ``--result-file`` whatever the exit code and whether or not
    ``--json`` is set, and is byte-identical to stdout under ``--json``. Without ``--json``
    stdout carries ``human`` (not under ``--quiet``). A failed result-file write is reported on
    stderr and turns a successful exit into 1.
    """
    text = dump_json(document)
    code = exit_code
    if ctx.result_file is not None:
        try:
            write_atomic(ctx.result_file, text)
        except OSError as exc:
            sys.stderr.write(f"review-respond: cannot write --result-file ({type(exc).__name__})\n")
            if code == 0:
                code = 1
    try:
        if ctx.json:
            sys.stdout.write(text)
        elif human and not ctx.quiet:
            sys.stdout.write(human if human.endswith("\n") else human + "\n")
        sys.stdout.flush()
    except BrokenPipeError:
        pass
    return code
