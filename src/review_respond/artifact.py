"""Strict validation of the artifact the respond agent files (docs/interface.md: Artifact).

``validate_artifact`` is the one function the interface describes; every failure is one
``ArtifactResult`` whose ``failure`` is a round failure reason and whose ``detail`` is a closed
``failure.detail`` of ``artifact_invalid``. Steps run in the interface order: implicit, missing,
the frozen file, JSON, schema, marker, replies, paths, commit message. ``check_path_state`` is the
separate path-state step, shared with ``confirm``.

The schema step is hand-written against ``schemas/artifact.json`` (jsonschema is test-only; a test
holds the two together). The rules the interface lists under a later detail (path content and
uniqueness, the 4 KiB message, reply body size) are left to that step so each failure keeps its
stable detail; every later step is at least as strict as the schema keyword it replaces.

Everything in the artifact is agent-written and untrusted. Text that reaches a failure message goes
through the one sanitizer in ``prompt.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import stat
import unicodedata
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .outcomes import ARTIFACT_FAILURES, FAILURE_DETAILS
from .prompt import sanitize, sanitize_capped

MAX_ARTIFACT_BYTES: Final = 1024 * 1024
MAX_DEPTH: Final = 8
TOP_KEYS: Final = ("skill_marker", "replies", "changed_files", "deleted_files", "commit_message")
REPLY_KEYS: Final = ("comment_id", "body")
MAX_MARKER_CHARS: Final = 256
MAX_REPLIES: Final = 1000
MAX_BODY_CHARS: Final = 65536
MAX_PATHS: Final = 1000
MAX_PATH_BYTES: Final = 4096
MAX_COMPONENT_BYTES: Final = 255
MAX_COMMIT_MESSAGE_BYTES: Final = 4096
MAX_ESCALATION_BYTES: Final = 4096
COMMENT_ID_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9:/_=.+-]{0,255}$"

_COMMENT_ID_RE: Final = re.compile(COMMENT_ID_PATTERN)
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
# C0 (but not tab, newline, carriage return), DEL and C1: never in a reply or a commit message.
_TEXT_CONTROL_RE: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_GIT_SHORT_RE: Final = re.compile(r"git~[0-9]+")
_CHUNK: Final = 64 * 1024


@dataclass(frozen=True)
class FrozenRef:
    """``report.artifact`` of ``agent-spawn show --json``: where the frozen file is."""

    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Reply:
    comment_id: str
    body: str


@dataclass(frozen=True)
class Artifact:
    skill_marker: str
    replies: tuple[Reply, ...]
    changed_files: tuple[str, ...]
    deleted_files: tuple[str, ...]
    commit_message: str


@dataclass(frozen=True)
class ArtifactResult:
    """The one result: ``failure`` is None when valid. ``detail`` goes with ``artifact_invalid``
    only. ``message`` is a one-line, sanitized reason for logs (never parsed). ``raw`` is the
    validated file's bytes (the copy that is stored)."""

    failure: str | None
    detail: str | None
    message: str
    artifact: Artifact | None = None
    raw: bytes | None = None

    def __post_init__(self) -> None:
        if self.failure is not None and self.failure not in ARTIFACT_FAILURES:
            raise ValueError("failure is not an artifact failure reason")
        if (self.failure == "artifact_invalid") != (self.detail is not None):
            raise ValueError("a detail belongs to artifact_invalid, and artifact_invalid has one")
        if self.detail is not None and self.detail not in FAILURE_DETAILS:
            raise ValueError("detail is not a closed failure detail")

    @property
    def ok(self) -> bool:
        return self.failure is None


def _fail(failure: str, detail: str | None, message: str) -> ArtifactResult:
    return ArtifactResult(failure, detail, sanitize(message, max_chars=300))


def _invalid(detail: str, message: str) -> ArtifactResult:
    return _fail("artifact_invalid", detail, message)


def _q(value: object) -> str:
    """An untrusted value, quoted for a message: sanitized and short."""
    return repr(sanitize(str(value), max_chars=60))


# ---- the frozen file ----------------------------------------------------------------------


def _read_frozen(ref: FrozenRef) -> tuple[bytes | None, str]:
    """The file's bytes, or ``(None, reason)``. One descriptor: the type, the size, the read and the
    hash all come from the same open file, so nothing can be swapped between a check and the read.
    The last path component is never followed (``O_NOFOLLOW``); ``O_NONBLOCK`` keeps a FIFO from
    blocking the open, and the regular-file check refuses it after."""
    if not ref.path or "\x00" in ref.path or not ref.path.startswith("/"):
        return None, "the artifact path is not an absolute path"
    if ref.size < 0 or ref.size > MAX_ARTIFACT_BYTES:
        return None, f"the artifact size is not in 0..{MAX_ARTIFACT_BYTES}"
    if _SHA256_RE.fullmatch(ref.sha256) is None:
        return None, "the artifact sha256 is not 64 lowercase hex digits"
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(ref.path, flags)
    except OSError as exc:
        return None, f"cannot open the artifact without following links ({type(exc).__name__})"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, "the artifact is not a regular file"
        if info.st_size != ref.size:
            return None, "the artifact size differs from the report"
        chunks: list[bytes] = []
        total = 0
        while total <= ref.size:
            chunk = os.read(fd, min(_CHUNK, ref.size + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    except OSError as exc:
        return None, f"cannot read the artifact ({type(exc).__name__})"
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) != ref.size:
        return None, "the artifact length differs from the report"
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), ref.sha256):
        return None, "the artifact sha256 differs from the report"
    return data, ""


# ---- strict JSON --------------------------------------------------------------------------


class _JsonError(Exception):
    pass


def _check_depth(text: str) -> None:
    """Refuse nesting over ``MAX_DEPTH`` before parsing (string-aware, so brackets in strings do
    not count), so a deep document never reaches the parser's recursion."""
    depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise _JsonError("nesting is deeper than the limit")
        elif ch in "]}":
            depth -= 1


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _JsonError("duplicate key")
        out[key] = value
    return out


def _constant(_name: str) -> Any:
    raise _JsonError("NaN and Infinity are not JSON")


def _float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise _JsonError("a number is out of range")
    return value


def _has_lone_surrogate(value: Any) -> bool:
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return True
        return False
    if isinstance(value, list):
        return any(_has_lone_surrogate(v) for v in value)  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, dict):
        return any(
            _has_lone_surrogate(k) or _has_lone_surrogate(v)
            for k, v in value.items()  # pyright: ignore[reportUnknownVariableType]
        )
    return False


def _load(raw: bytes) -> tuple[Any, str]:
    """The parsed document, or ``(None, reason)``. UTF-8 only, no BOM, no duplicate keys, no
    NaN, no lone surrogate escape, depth at most ``MAX_DEPTH``; one value and nothing after it."""
    try:
        text = raw.decode("utf-8")
        _check_depth(text)
        doc = json.loads(
            text, object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float
        )
    except _JsonError as exc:
        return None, str(exc)
    except (UnicodeDecodeError, ValueError, RecursionError, MemoryError) as exc:
        return None, f"not strict JSON ({type(exc).__name__})"
    if _has_lone_surrogate(doc):
        return None, "a string holds a lone surrogate"
    return doc, ""


# ---- schema (hand-written against schemas/artifact.json) ----------------------------------


def _schema_problem(doc: Any) -> str | None:
    if not isinstance(doc, dict):
        return "the artifact is not an object"
    top: dict[str, Any] = doc  # pyright: ignore[reportUnknownVariableType]
    if set(top) != set(TOP_KEYS):
        extra = sorted(set(top) - set(TOP_KEYS))
        missing = sorted(set(TOP_KEYS) - set(top))
        return f"unknown keys {_q(extra)} or missing keys {_q(missing)}"
    marker = top["skill_marker"]
    if not isinstance(marker, str) or not 1 <= len(marker) <= MAX_MARKER_CHARS:
        return "skill_marker is not a string of 1 to 256 characters"
    replies = top["replies"]
    if not isinstance(replies, list) or len(replies) > MAX_REPLIES:  # pyright: ignore[reportUnknownArgumentType]
        return "replies is not a list of at most 1000"
    for item in replies:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(item, dict) or set(item) != set(REPLY_KEYS):  # pyright: ignore[reportUnknownArgumentType]
            return "a reply is not an object of exactly comment_id and body"
        cid = item["comment_id"]  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(cid, str) or _COMMENT_ID_RE.fullmatch(cid) is None:
            return "a reply comment_id is not a comment id"
        if not isinstance(item["body"], str):
            return "a reply body is not a string"
    for key in ("changed_files", "deleted_files"):
        paths = top[key]
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):  # pyright: ignore[reportUnknownVariableType]
            return f"{key} is not a list of strings"
    if not isinstance(top["commit_message"], str):
        return "commit_message is not a string"
    return None


# ---- text rules (shared with `edit`) ------------------------------------------------------


def reply_problem(body: str, reply_bytes: int) -> str | None:
    """Why ``body`` is not a valid reply, or None: non-empty, at most ``reply_bytes`` UTF-8 bytes
    (and the schema's 65536 characters), no control character other than tab and line breaks."""
    try:
        size = len(body.encode("utf-8"))
    except UnicodeEncodeError:
        return "the reply is not valid UTF-8"
    if not body.strip():
        return "the reply is empty"
    if size > reply_bytes or len(body) > MAX_BODY_CHARS:
        return f"the reply is larger than {reply_bytes} bytes"
    if _TEXT_CONTROL_RE.search(body):
        return "the reply holds a control character"
    return None


def commit_message_problem(message: str) -> str | None:
    """Why ``message`` is not a valid commit message, or None: at most 4 KiB, no control character
    other than tab and line breaks. (The commit-message procedure comes later, in confirm.)"""
    try:
        size = len(message.encode("utf-8"))
    except UnicodeEncodeError:
        return "the commit message is not valid UTF-8"
    if size > MAX_COMMIT_MESSAGE_BYTES:
        return f"the commit message is larger than {MAX_COMMIT_MESSAGE_BYTES} bytes"
    if _TEXT_CONTROL_RE.search(message):
        return "the commit message holds a control character"
    return None


def escalation_text(text: str | None) -> str | None:
    """An escalation reason or summary from the report, made safe to store: sanitized (one marker
    per invisible or control character, a newline and tab kept) and cut to 4096 UTF-8 bytes
    without splitting a marker."""
    if text is None:
        return None
    return sanitize_capped(text, max_bytes=MAX_ESCALATION_BYTES, newlines=True)[0]


# ---- paths --------------------------------------------------------------------------------


def _fold_git(component: str) -> str:
    folded = unicodedata.normalize("NFKC", component).casefold()
    return unicodedata.normalize("NFKC", folded).rstrip(" .")


def path_problem(path: str) -> str | None:
    """Why ``path`` is not an acceptable repo path, or None. Refused, never rewritten."""
    if not path:
        return "a path is empty"
    try:
        size = len(path.encode("utf-8"))
    except UnicodeEncodeError:
        return "a path is not valid UTF-8"
    if size > MAX_PATH_BYTES:
        return f"a path is longer than {MAX_PATH_BYTES} bytes"
    if sanitize(path) != path:
        return "a path holds a control, invisible or unassigned character"
    if path.startswith("/"):
        return "a path is absolute"
    if path.startswith("-"):
        return "a path starts with a dash"
    if "\\" in path or ":" in path:
        return "a path holds a backslash or a colon"
    for component in path.split("/"):
        if component in ("", ".", ".."):
            return "a path has an empty, '.' or '..' component"
        if len(component.encode("utf-8")) > MAX_COMPONENT_BYTES:
            return f"a path component is longer than {MAX_COMPONENT_BYTES} bytes"
        folded = _fold_git(component)
        if folded == ".git" or _GIT_SHORT_RE.fullmatch(folded):
            return "a path component is .git or an alias of it"
    return None


def _key(path: str) -> tuple[str, ...]:
    """Identity of a path for the uniqueness and prefix rules: NFC, case folded, per component."""
    return tuple(
        unicodedata.normalize("NFC", unicodedata.normalize("NFC", c).casefold())
        for c in path.split("/")
    )


def _paths_problem(changed: Sequence[str], deleted: Sequence[str]) -> str | None:
    for name, paths in (("changed_files", changed), ("deleted_files", deleted)):
        if len(paths) > MAX_PATHS:
            return f"{name} has more than {MAX_PATHS} paths"
        for path in paths:
            problem = path_problem(path)
            if problem is not None:
                return f"{name}: {problem} ({_q(path)})"
    keys: dict[tuple[str, ...], str] = {}
    for path in (*changed, *deleted):
        key = _key(path)
        if key in keys:
            return f"a path is listed twice or in both lists ({_q(path)})"
        keys[key] = path
    for key, path in keys.items():
        for end in range(1, len(key)):
            if key[:end] in keys:
                return f"a path is a directory prefix of another ({_q(path)})"
    return None


# ---- the parsed artifact ------------------------------------------------------------------


def parse_artifact_bytes(
    raw: bytes, *, delivered_ids: Collection[str], marker: str, reply_bytes: int
) -> ArtifactResult:
    """Every step after the frozen file: JSON, schema, marker, replies, paths, commit message."""
    doc, why = _load(raw)
    if why:
        return _invalid("json", why)
    problem = _schema_problem(doc)
    if problem is not None:
        return _invalid("schema", problem)
    if not hmac.compare_digest(doc["skill_marker"].encode(), marker.encode()):
        return _invalid("skill_marker", "skill_marker is not the bundled marker")
    seen: set[str] = set()
    replies: list[Reply] = []
    for item in doc["replies"]:
        cid: str = item["comment_id"]
        if cid not in delivered_ids:
            return _invalid("replies", f"a reply names a comment that was not delivered ({_q(cid)})")
        if cid in seen:
            return _invalid("replies", f"a comment has two replies ({_q(cid)})")
        seen.add(cid)
        problem = reply_problem(item["body"], reply_bytes)
        if problem is not None:
            return _invalid("replies", f"{problem} ({_q(cid)})")
        replies.append(Reply(cid, item["body"]))
    changed: list[str] = doc["changed_files"]
    deleted: list[str] = doc["deleted_files"]
    problem = _paths_problem(changed, deleted)
    if problem is not None:
        return _invalid("paths", problem)
    problem = commit_message_problem(doc["commit_message"])
    if problem is not None:
        return _invalid("commit_message", problem)
    artifact = Artifact(
        skill_marker=doc["skill_marker"],
        replies=tuple(replies),
        changed_files=tuple(changed),
        deleted_files=tuple(deleted),
        commit_message=doc["commit_message"],
    )
    return ArtifactResult(None, None, "", artifact, raw)


def validate_artifact(
    *,
    implicit: bool,
    artifact: FrozenRef | None,
    delivered_ids: Collection[str],
    marker: str,
    reply_bytes: int,
) -> ArtifactResult:
    """The interface's ``validate(outcome)``: ``implicit`` is ``report.implicit``, ``artifact`` is
    ``report.artifact`` (None when the report has none). Call it only once the run is final.

    A delivered comment without a reply is allowed: ``show`` lists it as unanswered.
    """
    if implicit:
        return _fail("implicit_done", None, "the run ended by an implicit done")
    if artifact is None:
        return _fail("artifact_missing", None, "the report has no artifact")
    raw, why = _read_frozen(artifact)
    if raw is None:
        return _invalid("frozen_file", why)
    return parse_artifact_bytes(raw, delivered_ids=delivered_ids, marker=marker, reply_bytes=reply_bytes)


# ---- path-state checks --------------------------------------------------------------------

_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _probe(root_fd: int, path: str) -> str:
    """``absent`` (ENOENT somewhere on the way), ``file`` (a regular file with one link, reached
    without following any link) or ``other`` (anything else, including an error)."""
    components = path.split("/")
    opened: list[int] = []
    current = root_fd
    try:
        for component in components[:-1]:
            try:
                current = os.open(component, _DIR_FLAGS, dir_fd=current)
            except FileNotFoundError:
                return "absent"
            except OSError:
                return "other"
            opened.append(current)
        try:
            info = os.stat(components[-1], dir_fd=current, follow_symlinks=False)
        except FileNotFoundError:
            return "absent"
        except OSError:
            return "other"
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            return "file"
        return "other"
    finally:
        for fd in opened:
            os.close(fd)


def check_path_state(root: Path, *, changed: Sequence[str], deleted: Sequence[str]) -> ArtifactResult:
    """Path-state checks (``artifact_invalid`` detail ``path_state``): by a no-follow walk from
    ``root`` (the round throwaway; a symlink there is refused), each changed path is a regular file
    with one link and each deleted path is absent (ENOENT). The path rules run again first, so a
    list read back from a stored record never reaches the disk unchecked.

    Run it after the agent is not live, before every extract (record time and ``confirm``).
    """
    for path in (*changed, *deleted):
        problem = path_problem(path)
        if problem is not None:
            return _invalid("path_state", f"{problem} ({_q(path)})")
    try:
        root_fd = os.open(root, _DIR_FLAGS)
    except OSError as exc:
        return _invalid("path_state", f"cannot open the throwaway ({type(exc).__name__})")
    try:
        for path in changed:
            if _probe(root_fd, path) != "file":
                return _invalid(
                    "path_state", f"a changed path is not a regular file with one link ({_q(path)})"
                )
        for path in deleted:
            if _probe(root_fd, path) != "absent":
                return _invalid("path_state", f"a deleted path is not absent ({_q(path)})")
    finally:
        os.close(root_fd)
    return ArtifactResult(None, None, "")
