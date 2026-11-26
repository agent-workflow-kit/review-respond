"""Append-only ``audit.jsonl`` and ``log.jsonl`` (docs/interface.md: Audit and log).

Both files live in the verified private state directory (0600, never repaired when unsafe) and are
written one whole line at a time to an ``O_APPEND`` descriptor under ``flock``, so several
processes (and the heartbeat thread of one) never interleave or tear a line. Every record is
checked against the shape of ``schemas/audit.json`` / ``schemas/log.json`` before it is written;
a value that does not fit is a programming error (``ValueError``) and nothing is written. Free
text from a host or a model never reaches these files: the only free string, a warning's ``key``,
goes through the one sanitizer and is cut to 256 characters.

``audit.jsonl`` is the record of external actions: if the ``attempted`` append fails the caller
must not act (``AuditLog`` raises ``CliError``). ``log.jsonl`` is diagnostics: an append that
fails is reported once on stderr and never stops a command.
"""

from __future__ import annotations

import base64
import contextlib
import errno
import fcntl
import json
import os
import re
import secrets
import stat
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Final

from .errors import CliError
from .gateway import Clock, SystemClock
from .output import PROG, sanitize_line, warn
from .store import EventCallback, StateDir

AUDIT_NAME: Final = "audit.jsonl"
LOG_NAME: Final = "log.jsonl"
MAX_KEY_CHARS: Final = 256
MAX_DECISION_IDS: Final = 16

# The patterns of schemas/defs.json (tests/test_audit_log.py fails when they drift).
_HOST: Final = (
    r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:[.][a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*"
    r"(?::[0-9]{1,5})?$"
)
_PR: Final = (
    r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:[.][a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?"
    r"(?:/(?![.][.]?(?:/|#))[A-Za-z0-9._-]+)+#[1-9][0-9]{0,9}$"
)
_SHA: Final = r"^[0-9a-f]{40}$"
_SHA256: Final = r"^[0-9a-f]{64}$"
_TS: Final = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:[.][0-9]{1,6})?Z$"
_PENDING: Final = r"^rp-[a-z2-7]{8}$"
_ROUND: Final = r"^rd-[a-z2-7]{8}$"
_ACTOR: Final = r"^(?:human|auto):[A-Za-z0-9._@-]{1,64}$"
_DECISION: Final = r"^[A-Za-z0-9._:@/-]{1,128}$"
_COMMENT: Final = r"^[A-Za-z0-9][A-Za-z0-9:/_=.+-]{0,255}$"
_NAME: Final = r"^[a-z][a-z0-9_]{0,63}$"
_OBJECT: Final = r"^[A-Za-z0-9._:/#=+-]{1,256}$"
_ATTEMPT: Final = r"^[A-Za-z0-9_-]{22}$"

PR_STATES: Final = ("idle", "settling", "queued", "running", "pending", "waiting", "closed")
ROUND_STATES: Final = (
    "queued", "running", "pending", "confirmed", "rejected", "expired", "closed", "escalated",
    "failed", "canceled",
)  # fmt: skip
PENDING_STATES: Final = ("pending", "pushing", "pushed", "done", "rejected", "expired", "closed")
HOOKS: Final = ("pre-round", "pre-push", "post-pending", "post-attention", "post-confirm")
REFUSAL_REASONS: Final = (
    "live", "busy", "pending_exists", "round_active", "round_limit", "remote_moved",
    "diff_changed", "sensitive_paths", "expired", "not_pending", "ineligible", "pr_closed",
)  # fmt: skip
AUDIT_ACTIONS: Final = (
    "push", "reply", "resolve", "confirm", "reject", "edit", "drop_reply", "stop", "teardown",
)  # fmt: skip
RESULT_STATUSES: Final = ("ok", "failed", "refused", "vetoed", "escalated", "noop")
ACTOR_SOURCES: Final = ("flag", "tty", "default")
LEVELS: Final = ("debug", "info", "warn", "error")
FINALIZE_STEPS: Final = (
    "agent_found", "agent_missing", "pending_recorded", "escalated", "failed", "canceled",
    "left_running", "job_resolved", "teardown",
)  # fmt: skip
LEASE_KINDS: Final = ("pass", "pr", "round")
LEASE_ACTIONS: Final = ("acquired", "released", "taken_over", "lost", "stalled")

Check = Callable[[object], object]


# ---- field checks (each returns the value to write or raises ValueError) ------------------


def _pattern(regex: str) -> Check:
    compiled = re.compile(regex)

    def check(value: object) -> object:
        if not isinstance(value, str) or not compiled.fullmatch(value):
            raise ValueError("value does not match its pattern")
        return value

    return check


def _enum(*allowed: str) -> Check:
    def check(value: object) -> object:
        if not isinstance(value, str) or value not in allowed:
            raise ValueError("value is not one of the allowed names")
        return value

    return check


def _nullable(inner: Check) -> Check:
    return lambda value: None if value is None else inner(value)


def _integer(minimum: int | None = None) -> Check:
    def check(value: object) -> object:
        if type(value) is not int or (minimum is not None and value < minimum):
            raise ValueError("value is not an acceptable integer")
        return value

    return check


def _boolean(value: object) -> object:
    if type(value) is not bool:
        raise ValueError("value is not a boolean")
    return value


def _timestamp(value: object) -> object:
    if isinstance(value, datetime):
        return format_ts(value)
    return _pattern(_TS)(value)


def _decision_ids(value: object) -> object:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise ValueError("decision ids must be a list")
    items = [_pattern(_DECISION)(item) for item in value]  # pyright: ignore[reportUnknownVariableType]
    if len(items) > MAX_DECISION_IDS:
        raise ValueError("too many decision ids")
    return items


def _free_key(value: object) -> object:
    if not isinstance(value, str):
        raise ValueError("value is not text")
    return clean_text(value, MAX_KEY_CHARS)


def clean_text(text: str, limit: int) -> str:
    """The text through the one sanitizer (control, format and separator characters become
    ``<U+XXXX>`` markers), cut to at most ``limit`` characters without splitting a marker."""
    out: list[str] = []
    size = 0
    for ch in text:
        token = sanitize_line(ch)
        if size + len(token) > limit:
            break
        out.append(token)
        size += len(token)
    return "".join(out)


def format_ts(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _check_fields(
    fields: Mapping[str, object],
    required: Mapping[str, Check],
    optional: Mapping[str, Check],
    what: str,
) -> dict[str, object]:
    unknown = set(fields) - set(required) - set(optional)
    if unknown:
        raise ValueError(f"{what}: unknown field {sorted(unknown)[0]!r}")
    missing = set(required) - set(fields)
    if missing:
        raise ValueError(f"{what}: missing field {sorted(missing)[0]!r}")
    out: dict[str, object] = {}
    for name, value in fields.items():
        check = required.get(name) or optional[name]
        try:
            out[name] = check(value)
        except ValueError as exc:
            raise ValueError(f"{what}: field {name!r}: {exc}") from None
    return out


# ---- the file -----------------------------------------------------------------------------


def _unsafe(message: str) -> CliError:
    return CliError("state_unsafe", message)


class JsonlFile:
    """One append-only file of the state directory.

    The constructor checks an existing file (nothing is created); the first append opens it,
    creating it 0600 when absent, and checks the open descriptor, so a file swapped in between is
    refused too. Refused: a symlink, a FIFO or other special file (never opened blocking), a
    hard-linked file, a file owned by another user, a file accessible to group or others. Nothing
    is repaired.
    """

    def __init__(
        self,
        state: StateDir,
        name: str,
        *,
        write: Callable[[int, bytes], int] = os.write,
    ) -> None:
        self.name = name
        self.path = state.path / name
        self._euid = state.euid
        self._state_path = state.path
        self._dir_fd = os.dup(state.fd)
        self._write = write
        self._fd = -1
        self._closed = False
        self._lock = threading.Lock()
        try:
            self._check_existing()
        except BaseException:
            self.close()
            raise

    def _verify(self, info: os.stat_result) -> None:
        if not stat.S_ISREG(info.st_mode):
            raise _unsafe(f"{self.path} is not a regular file; refusing to use it")
        if info.st_nlink != 1:
            raise _unsafe(f"{self.path} has several hard links; refusing to use it")
        if info.st_uid != self._euid:
            raise _unsafe(f"{self.path} is owned by another user; refusing to use it")
        if info.st_mode & 0o077:
            raise _unsafe(
                f"{self.path} is accessible to group or others; refusing to use it (chmod go-rwx)"
            )

    def _check_existing(self) -> None:
        try:
            info = os.lstat(self.name, dir_fd=self._dir_fd)
        except FileNotFoundError:
            return
        except OSError:
            raise _unsafe(f"{self.path} is not a plain file; refusing to use it") from None
        self._verify(info)

    def _open(self) -> int:
        held = os.fstat(self._dir_fd)
        try:
            now = os.stat(self._state_path)
        except OSError:
            raise _unsafe(f"state directory {self._state_path} changed while in use") from None
        if (held.st_dev, held.st_ino) != (now.st_dev, now.st_ino):
            raise _unsafe(f"state directory {self._state_path} changed; refusing to use it")
        flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        created = False
        try:
            try:
                fd = os.open(self.name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self._dir_fd)
                created = True
            except FileExistsError:
                fd = os.open(self.name, flags, dir_fd=self._dir_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENXIO, errno.EISDIR, errno.ENOENT):
                raise _unsafe(f"{self.path} is not a plain file; refusing to use it") from None
            raise
        try:
            if created:
                os.fchmod(fd, 0o600)  # explicit mode, whatever the umask was
            self._verify(os.fstat(fd))
        except BaseException:
            os.close(fd)
            raise
        return fd

    def append(self, record: Mapping[str, object]) -> None:
        """Append one record as one line. ``OSError`` when the write fails (no partial line is
        left behind), ``CliError`` ``state_unsafe`` when the file is unsafe."""
        line = (
            json.dumps(
                record, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("ascii")
            + b"\n"
        )
        with self._lock:
            if self._closed:
                raise OSError(errno.EBADF, "writer is closed")
            if self._fd < 0:
                self._fd = self._open()
            fd = self._fd
            # flock keeps a short write's retry whole; a filesystem without it still has O_APPEND
            locked = False
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_EX)
                locked = True
            try:
                size = os.fstat(fd).st_size
                data = line
                if size > 0 and os.pread(fd, 1, size - 1) != b"\n":
                    data = b"\n" + line  # a crashed writer's unfinished line stays alone
                try:
                    view = memoryview(data)
                    while view:
                        written = self._write(fd, bytes(view))
                        if written <= 0:
                            raise OSError(errno.EIO, "write made no progress")
                        view = view[written:]
                except BaseException:
                    with contextlib.suppress(OSError):
                        os.ftruncate(fd, size)
                    raise
            finally:
                if locked:
                    with contextlib.suppress(OSError):
                        fcntl.flock(fd, fcntl.LOCK_UN)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            fds = (self._fd, self._dir_fd)
            self._fd = self._dir_fd = -1
            for fd in fds:
                if fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(fd)

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.close()


def open_jsonl_files(state: StateDir) -> tuple[JsonlFile, JsonlFile]:
    """The checked ``(audit.jsonl, log.jsonl)``; ``state_unsafe`` when either is unsafe."""
    audit = JsonlFile(state, AUDIT_NAME)
    try:
        return audit, JsonlFile(state, LOG_NAME)
    except BaseException:
        audit.close()
        raise


# ---- audit.jsonl --------------------------------------------------------------------------

_INPUT_CHECKS: Final[dict[str, Check]] = {
    "head_sha": _pattern(_SHA),
    "diff_sha256": _pattern(_SHA256),
    "commit_sha": _pattern(_SHA),
    "pending_id": _pattern(_PENDING),
    "round": _pattern(_ROUND),
    "comment_id": _pattern(_COMMENT),
    "config_sha256": _pattern(_SHA256),
    "actor_source": _enum(*ACTOR_SOURCES),
    "override_escalation": _boolean,
}
_RESULT_CHECKS: Final[dict[str, Check]] = {
    "exit_code": _nullable(_integer()),
    "reason": _nullable(_enum(*REFUSAL_REASONS)),
    "comment_id": _nullable(_pattern(_COMMENT)),
    "commit_sha": _nullable(_pattern(_SHA)),
    "lower_code": _nullable(_pattern(_NAME)),
    "lower_reason": _nullable(_pattern(_NAME)),
}


def new_attempt_id() -> str:
    """128 random bits as 22 URL-safe characters."""
    return base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode("ascii")


class AuditLog:
    """The record of external actions.

    Each push and reply is ``attempted`` before the call and ``outcome`` after it, sharing an
    ``attempt_id``; the human decisions are single outcome records (``decision``).
    ``config_sha256``, ``actor_source`` and ``override_escalation`` (written only when true)
    are added to every record's ``inputs``. A failed append raises ``CliError``: the caller
    must not act.
    """

    def __init__(
        self,
        file: JsonlFile,
        *,
        actor: str,
        decision_ids: Sequence[str] = (),
        config_sha256: str | None = None,
        actor_source: str | None = None,
        override_escalation: bool = False,
        clock: Clock | None = None,
    ) -> None:
        self._file = file
        self._clock: Clock = clock or SystemClock()
        self._actor = _pattern(_ACTOR)(actor)
        self._decision_ids = tuple(self._ids(decision_ids))
        defaults: dict[str, object] = {}
        if config_sha256 is not None:
            defaults["config_sha256"] = config_sha256
        if actor_source is not None:
            defaults["actor_source"] = actor_source
        if override_escalation:
            defaults["override_escalation"] = True
        self._defaults = _check_fields(defaults, {}, _INPUT_CHECKS, "inputs")

    @staticmethod
    def _ids(ids: Sequence[str]) -> list[str]:
        found: list[str] = []
        for item in _decision_ids(list(ids)):  # pyright: ignore[reportGeneralTypeIssues]
            if item not in found:
                found.append(item)
        return found

    def close(self) -> None:
        self._file.close()

    def attempted(
        self,
        action: str,
        target: str,
        *,
        inputs: Mapping[str, object] | None = None,
        decision_ids: Sequence[str] = (),
    ) -> str:
        """Record the attempt before the call; returns the ``attempt_id`` for the outcome."""
        attempt_id = new_attempt_id()
        self._append("attempted", attempt_id, action, target, inputs, None, decision_ids)
        return attempt_id

    def outcome(
        self,
        attempt_id: str,
        action: str,
        target: str,
        result: Mapping[str, object],
        *,
        inputs: Mapping[str, object] | None = None,
        decision_ids: Sequence[str] = (),
    ) -> None:
        _pattern(_ATTEMPT)(attempt_id)
        self._append("outcome", attempt_id, action, target, inputs, result, decision_ids)

    def decision(
        self,
        action: str,
        target: str,
        result: Mapping[str, object],
        *,
        inputs: Mapping[str, object] | None = None,
        decision_ids: Sequence[str] = (),
    ) -> None:
        """A human decision: one outcome record with its own ``attempt_id``."""
        self._append("outcome", new_attempt_id(), action, target, inputs, result, decision_ids)

    def _append(
        self,
        phase: str,
        attempt_id: str,
        action: str,
        target: str,
        inputs: Mapping[str, object] | None,
        result: Mapping[str, object] | None,
        decision_ids: Sequence[str],
    ) -> None:
        ids = self._ids([*self._decision_ids, *decision_ids])
        if len(ids) > MAX_DECISION_IDS:
            raise ValueError("too many decision ids")
        merged = self._defaults | _check_fields(dict(inputs or {}), {}, _INPUT_CHECKS, "inputs")
        checked_result: dict[str, object] | None = None
        if result is not None:
            checked_result = _check_fields(
                result,
                {"status": _enum(*RESULT_STATUSES)},
                _RESULT_CHECKS,
                "result",
            )
        record: dict[str, object] = {
            "ts": format_ts(self._clock.now()),
            "phase": phase,
            "attempt_id": attempt_id,
            "actor": self._actor,
            "decision_id": None if not ids else ids[0] if len(ids) == 1 else ids,
            "target": _check_fields({"target": target}, {"target": _pattern(_PR)}, {}, "audit")[
                "target"
            ],
            "action": _check_fields(
                {"action": action}, {"action": _enum(*AUDIT_ACTIONS)}, {}, "audit"
            )["action"],
            "inputs": merged,
            "result": checked_result,
        }
        try:
            self._file.append(record)
        except CliError:
            raise
        except OSError as exc:
            raise CliError(
                "internal", f"cannot append to {self._file.name} ({type(exc).__name__})"
            ) from None


# ---- log.jsonl ----------------------------------------------------------------------------

_NO_FIELDS: Final[dict[str, Check]] = {}
_EVENTS: Final[dict[str, tuple[dict[str, Check], dict[str, Check]]]] = {
    "check_pass": (
        {
            "started_at": _timestamp,
            "finished_at": _timestamp,
            "already_running": _boolean,
        },
        {"prs": _integer(0), "rounds_started": _integer(0), "errors": _integer(0)},
    ),
    "pr_state": (
        {
            "pr": _pattern(_PR),
            "from": _nullable(_enum(*PR_STATES)),
            "to": _enum(*PR_STATES),
            "reason": _pattern(_NAME),
        },
        _NO_FIELDS,
    ),
    "round_state": (
        {
            "round": _pattern(_ROUND),
            "from": _nullable(_enum(*ROUND_STATES)),
            "to": _enum(*ROUND_STATES),
            "reason": _pattern(_NAME),
        },
        _NO_FIELDS,
    ),
    "pending_state": (
        {
            "pending": _pattern(_PENDING),
            "from": _nullable(_enum(*PENDING_STATES)),
            "to": _enum(*PENDING_STATES),
            "reason": _pattern(_NAME),
        },
        _NO_FIELDS,
    ),
    "hook_result": (
        {"hook": _enum(*HOOKS), "exit_code": _nullable(_integer()), "timed_out": _boolean},
        {
            "object_id": _nullable(_pattern(_OBJECT)),
            "duration_ms": _integer(0),
            "async": _boolean,
            "vetoed": _boolean,
            "decision_ids": _decision_ids,
        },
    ),
    "finalize": (
        {"round": _pattern(_ROUND), "step": _enum(*FINALIZE_STEPS)},
        _NO_FIELDS,
    ),
    "host_failed": ({"host": _pattern(_HOST), "code": _pattern(_NAME)}, _NO_FIELDS),
    "lease": (
        {"kind": _enum(*LEASE_KINDS), "action": _enum(*LEASE_ACTIONS)},
        {"pr": _pattern(_PR), "round": _pattern(_ROUND)},
    ),
    "warning": (
        {"code": _pattern(_NAME)},
        {"pr": _pattern(_PR), "round": _pattern(_ROUND), "key": _free_key},
    ),
}

_LEASE_KEY = re.compile(r"(pass)|(pr):[0-9a-f]{20}|(round):([A-Za-z0-9][A-Za-z0-9_-]{0,63})")
_LEASE_LEVEL: Final = {"lost": "error", "taken_over": "warn", "stalled": "warn"}


class LogWriter:
    """Events with ids, codes and hashes only. Thread-safe; never raises for a failed append."""

    def __init__(self, file: JsonlFile, *, clock: Clock | None = None) -> None:
        self._file = file
        self._clock: Clock = clock or SystemClock()
        self._reported = False
        self._report_lock = threading.Lock()

    def close(self) -> None:
        self._file.close()

    def emit(self, event: str, fields: Mapping[str, object], *, level: str = "info") -> bool:
        """Append one event; False when the append failed (reported once on stderr).

        ``ValueError`` for an unknown event or a field that does not fit its schema; ``CliError``
        ``state_unsafe`` when the file turned out unsafe.
        """
        spec = _EVENTS.get(event)
        if spec is None:
            raise ValueError(f"unknown log event {event!r}")
        checked = _check_fields(fields, spec[0], spec[1], event)
        _enum(*LEVELS)(level)
        record: dict[str, object] = {
            "ts": format_ts(self._clock.now()),
            "level": level,
            "event": event,
            **checked,
        }
        try:
            self._file.append(record)
        except OSError as exc:
            with self._report_lock:
                first, self._reported = not self._reported, True
            if first:
                warn(
                    f"{PROG}: warning: cannot append to {self._file.name} "
                    f"({type(exc).__name__}); events are not being logged\n"
                )
            return False
        return True

    def warning(
        self,
        code: str,
        *,
        key: str | None = None,
        pr: str | None = None,
        round: str | None = None,
        level: str = "warn",
    ) -> bool:
        fields: dict[str, object] = {"code": code}
        for name, value in (("key", key), ("pr", pr), ("round", round)):
            if value is not None:
                fields[name] = value
        return self.emit("warning", fields, level=level)

    def lease(
        self,
        kind: str,
        action: str,
        *,
        pr: str | None = None,
        round: str | None = None,
        level: str | None = None,
    ) -> bool:
        fields: dict[str, object] = {"kind": kind, "action": action}
        if pr is not None:
            fields["pr"] = pr
        if round is not None:
            fields["round"] = round
        return self.emit("lease", fields, level=level or _LEASE_LEVEL.get(action, "info"))

    def lease_callback(self) -> EventCallback:
        """The ``on_event`` of ``Store.acquire``/``try_acquire``: logs ``taken_over``, ``stalled``
        and ``lost`` as ``lease`` events. May run on the heartbeat thread; the file lock makes it
        safe. A ``pr:`` key carries a digest, not the PR, so the event has no ``pr``."""

        def on_event(action: str, key: str) -> None:
            matched = _LEASE_KEY.fullmatch(key)
            if matched is None:
                raise ValueError("invalid lease key")
            kind = matched.group(1) or matched.group(2) or matched.group(3)
            self.lease(kind, action, round=matched.group(4))

        return on_event


__all__ = [
    "AUDIT_NAME",
    "LOG_NAME",
    "AuditLog",
    "JsonlFile",
    "LogWriter",
    "clean_text",
    "format_ts",
    "new_attempt_id",
    "open_jsonl_files",
]
