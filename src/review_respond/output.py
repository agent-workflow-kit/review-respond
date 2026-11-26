"""Result printing, the atomic ``--result-file`` and the per-run context."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import Config
from .store import Store

if TYPE_CHECKING:
    from .audit import AuditLog, LogWriter

PROG = "review-respond"


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
    # The loaded config; None for `validate` when the config is invalid (it loads the config
    # itself and reports that as its failed `config` check) and for the fallback context used
    # before the flags are parsed.
    settings: Config | None = None
    # The open state database (every command opens it first; a read-only one on a newer schema).
    store: Store | None = None
    # The append-only files (None for `validate`, `--dry-run` and the read-only commands, which
    # never write them). `audit` is None too until the config is loaded (it records its hash).
    audit: AuditLog | None = None
    log: LogWriter | None = None


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
    """Write ``text`` to ``path``.

    A regular file (or a path that does not exist yet) is replaced through a synced temporary
    file in the same directory; a symlink is kept and its target written. An existing
    non-regular path (``/dev/null``, a fifo, ``/dev/stdout``) is written in place.
    """
    try:
        regular = stat.S_ISREG(os.stat(path).st_mode)
        exists = True
    except OSError:
        regular, exists = False, False
    if exists and not regular:
        with open(path, "w", encoding="utf-8") as plain:
            plain.write(text)
        return
    target = Path(os.path.realpath(path))
    directory = target.parent
    # A short fixed-length temp name keeps a ~255-byte target name under NAME_MAX.
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".rr-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
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


def warn(message: str) -> None:
    """Write to stderr; a closed or failing stderr never raises."""
    try:
        sys.stderr.write(message)
    except (OSError, ValueError, AttributeError):
        pass


def _silence_stdout() -> None:
    """Point fd 1 at /dev/null so a later flush at interpreter exit cannot fail (exit 120)."""
    try:
        null = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(null, 1)
        finally:
            os.close(null)
    except OSError:
        pass


def emit(document: dict[str, Any], human: str, exit_code: int, ctx: Context) -> int:
    """Print the result and write ``--result-file``; returns the exit code to use.

    The JSON document is written to ``--result-file`` whatever the exit code and whether or not
    ``--json`` is set, and is byte-identical to stdout under ``--json``. Without ``--json``
    stdout carries ``human`` (not under ``--quiet``). A failed result-file or stdout write is
    reported on stderr and turns a successful exit into 1; a failing exit code is kept. A closed
    reader (broken pipe) is not an error. This function never raises.
    """
    code = exit_code
    try:
        text = dump_json(document)
    except (TypeError, ValueError):
        warn(f"{PROG}: cannot serialize the result\n")
        return code or 1
    if ctx.result_file is not None:
        try:
            write_atomic(ctx.result_file, text)
        except (OSError, ValueError) as exc:
            warn(f"{PROG}: cannot write --result-file ({type(exc).__name__})\n")
            if code == 0:
                code = 1
    try:
        out = sys.stdout
        if ctx.json:
            out.write(text)
        elif human and not ctx.quiet:
            out.write(human if human.endswith("\n") else human + "\n")
        out.flush()
    except BrokenPipeError:
        _silence_stdout()
    except (OSError, ValueError, AttributeError) as exc:
        # ValueError covers UnicodeEncodeError and a closed file; AttributeError a None stdout.
        if sys.stdout is not None:
            _silence_stdout()
        warn(f"{PROG}: cannot write to stdout ({type(exc).__name__})\n")
        if code == 0:
            code = 1
    return code
