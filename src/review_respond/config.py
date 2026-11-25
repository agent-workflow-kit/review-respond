"""YAML plus drop-ins, environment overrides and validation (docs/interface.md: Config).

Sources, lowest to highest: built-in defaults, ``config.yaml``, ``config.d/*.yaml`` (bytewise
lexical order; maps merge, ``hooks.<name>`` lists append, every other value is replaced by the
later file), then the ``REVIEW_RESPOND_*`` variables the interface lists. ``Config.repo_settings``
applies a ``repos.<id>`` entry on top of that (flags are the caller's, above all of it).

Every failure is a ``ConfigError`` (``config_invalid``, exit 2) that names the source (a file or
an environment variable) and the offending key, never an input value: YAML parser and pydantic
messages can quote the input, so only positions and rule descriptions are reported.

YAML is read with a ``SafeLoader`` subclass that refuses anchors, aliases (no expansion bombs),
explicit tags, non-string keys, duplicate keys and the merge key, and resolves scalars by the
YAML 1.2 core schema (``yes``/``no``/``on``/``off`` are strings, integers are plain decimal).
Files are size-capped, and must be regular, owned by you and not group or world writable
(hook commands run from this config).

The patterns below are those of ``schemas/config.json`` with ``$`` written ``\\Z``: JSON Schema
regexes are ECMA-262, where ``$`` does not match before a trailing newline, but Python's does.
``tests/test_config_schema.py`` keeps the model and the schema in agreement.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Final, Literal

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

from .actor import os_user_name
from .durations import duration_ms
from .errors import CliError

MAX_FILE_BYTES: Final = 256 * 1024
MAX_TOTAL_BYTES: Final = 1024 * 1024
MAX_DROP_INS: Final = 256
MAX_DURATION_MS: Final = 3650 * 86_400_000  # ten years: bounds every duration key
MAX_HOOK_TIMEOUT_MS: Final = 600_000
MAX_WARNING_CHARS: Final = 256
_MAX_ERRORS_SHOWN: Final = 5
_MAX_KEY_CHARS: Final = 80
ENV_PREFIX: Final = "REVIEW_RESPOND_"


def _ecma(pattern: str) -> str:
    """The same pattern for engines where ``$`` must not match before a trailing newline."""
    return pattern.replace("$", r"\Z")


# --- errors ------------------------------------------------------------------------------------


def _esc(text: str, limit: int = _MAX_KEY_CHARS) -> str:
    """``text`` with control and non-ASCII characters escaped and a length cap (terminal safety)."""
    out = text.encode("unicode_escape", "backslashreplace").decode("ascii")
    return out if len(out) <= limit else out[:limit] + "..."


class ConfigError(Exception):
    """Invalid config (``config_invalid``, exit 2). Names ``source`` and ``key``, never a value."""

    def __init__(
        self,
        source: str | None,
        key: str | None,
        message: str,
        more: tuple[str, ...] = (),
    ) -> None:
        self.source = source
        self.key = key
        self.reason = message
        self.more = more
        head = ": ".join(part for part in (source, key, message) if part)
        super().__init__("; ".join((head, *more)))

    def to_cli_error(self) -> CliError:
        return CliError("config_invalid", str(self))


@dataclass(frozen=True)
class ConfigWarning:
    """A load-time warning (stable ``code``, the ``key`` it concerns and a short message)."""

    code: str
    key: str
    message: str

    def __post_init__(self) -> None:
        if len(self.message) > MAX_WARNING_CHARS:
            object.__setattr__(self, "message", self.message[: MAX_WARNING_CHARS - 3] + "...")


def _name(path: Path | str) -> str:
    return _esc(str(path), 1024)


# --- YAML --------------------------------------------------------------------------------------


def _at(mark: Any) -> str:
    return f"line {mark.line + 1}, column {mark.column + 1}"


class _Rejected(Exception):
    """A refusal whose message carries its position and never an input value."""


class _StrictLoader(yaml.SafeLoader):
    yaml_implicit_resolvers: dict[Any, Any] = {}

    def compose_node(self, parent: Any, index: Any) -> Any:
        event = self.peek_event()
        where = _at(event.start_mark)
        if isinstance(event, yaml.AliasEvent):
            raise _Rejected(f"aliases are not allowed ({where})")
        if getattr(event, "anchor", None) is not None:
            raise _Rejected(f"anchors are not allowed ({where})")
        if getattr(event, "tag", None) is not None:
            raise _Rejected(f"YAML tags are not allowed ({where})")
        return super().compose_node(parent, index)

    def construct_object(self, node: Any, deep: bool = False) -> Any:
        try:
            return super().construct_object(node, deep)
        except (ValueError, OverflowError):
            raise _Rejected(f"invalid value ({_at(node.start_mark)})") from None

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        seen: set[str] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            if not isinstance(key, str):
                raise _Rejected(f"mapping keys must be strings ({_at(key_node.start_mark)})")
            if key == "<<":
                raise _Rejected(f"the merge key is not allowed ({_at(key_node.start_mark)})")
            if key in seen:
                raise _Rejected(f"duplicate key ({_at(key_node.start_mark)})")
            seen.add(key)
        return super().construct_mapping(node, deep)


# YAML 1.2 core schema, narrowed: decimal integers (no leading zero, at most 18 digits; a longer
# digit string matches the float resolver instead, and the strict models refuse it naming the
# key) without ``_``; no
# timestamps, no merge or ``=`` value tags, no yes/no/on/off.
for _tag, _pattern, _first_chars in (
    ("bool", r"^(?:true|True|TRUE|false|False|FALSE)\Z", "tTfF"),
    ("int", r"^-?(?:0|[1-9][0-9]{0,17})\Z", "-0123456789"),
    (
        "float",
        r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
        r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))\Z",
        "-+0123456789.",
    ),
    ("null", r"^(?:~|null|Null|NULL|)\Z", ["~", "n", "N", ""]),
):
    _StrictLoader.add_implicit_resolver(
        f"tag:yaml.org,2002:{_tag}", re.compile(_pattern), list(_first_chars)
    )


def _check_owner_and_mode(
    info: os.stat_result, path: Path | str, *, allow_sticky: bool = False
) -> None:
    """Owned by you and not group/world writable. ``allow_sticky`` waives only the write check,
    for a directory whose sticky bit stops others from replacing your file (such as ``/tmp``)."""
    if info.st_uid != os.geteuid():
        raise ConfigError(_name(path), None, "owned by another user; refusing to load it")
    if info.st_mode & 0o022 and not (allow_sticky and info.st_mode & stat.S_ISVTX):
        raise ConfigError(
            _name(path), None, "writable by group or others; refusing to load it (chmod go-w)"
        )


def _read_file(path: Path) -> bytes:
    """The bytes of a regular, owned, not group/world-writable file (symlinks are followed and
    the target is checked), read through one descriptor so the checks apply to what is read."""
    name = _name(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise ConfigError(name, None, "not found") from None
    except OSError as exc:
        raise ConfigError(name, None, f"cannot be read ({exc.strerror})") from None
    except ValueError:
        raise ConfigError(name, None, "is not a valid path") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(name, None, "not a regular file")
        _check_owner_and_mode(info, path)
        if info.st_size > MAX_FILE_BYTES:
            raise ConfigError(name, None, f"larger than {MAX_FILE_BYTES} bytes")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise ConfigError(name, None, f"larger than {MAX_FILE_BYTES} bytes")
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as exc:
        raise ConfigError(name, None, f"cannot be read ({exc.strerror})") from None
    finally:
        os.close(fd)


def _check_parent(path: Path) -> None:
    """The directory holding the config file must not let others replace it."""
    parent = path.parent
    try:
        info = os.stat(parent)
    except OSError as exc:
        raise ConfigError(_name(parent), None, f"cannot be read ({exc.strerror})") from None
    _check_owner_and_mode(info, parent, allow_sticky=True)


def _parse(data: bytes, name: str) -> dict[str, Any]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(name, None, "not valid UTF-8") from None
    try:
        document = yaml.load(text, Loader=_StrictLoader)  # a SafeLoader subclass
    except _Rejected as exc:
        raise ConfigError(name, None, str(exc)) from None
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" ({_at(mark)})" if mark else ""
        raise ConfigError(name, None, f"invalid YAML{where}") from None
    except RecursionError:
        raise ConfigError(name, None, "invalid YAML (nested too deeply)") from None
    except Exception:  # any parser failure; its text may quote the input
        raise ConfigError(name, None, "invalid YAML") from None
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise ConfigError(name, None, "the top level must be a mapping")
    return document  # pyright: ignore[reportUnknownVariableType]


# --- the model ---------------------------------------------------------------------------------

_HOST_BODY: Final = (
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:[.][a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?"
)
_HOST: Final = _ecma(rf"^{_HOST_BODY}$")
_REPO: Final = _ecma(rf"^{_HOST_BODY}(?:/(?![.][.]?(?:/|$))[A-Za-z0-9._-]+)+$")
_ACCOUNT_ID: Final = _ecma(r"^[A-Za-z0-9._@=+:-]+$")
_ACCOUNT_ENTRY: Final = _ecma(r"^[a-z0-9][a-z0-9.-]*(?::[0-9]{1,5})?:[A-Za-z0-9._@=+-]{1,128}$")
_DURATION: Final = _ecma(r"^(?:0|[0-9]+(?:ms|s|m|h|d))$")
_ENV_NAME: Final = _ecma(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_GLOB: Final = _ecma(r"^(?!/)(?!.*/$)(?!.*//)(?!(?:.*/)?[.][.]?(?:/|$))[A-Za-z0-9._*?/:@+-]+$")
_NAME: Final = _ecma(r"^[A-Za-z0-9._@-]+$")
_OWNER: Final = _ecma(r"^[A-Za-z0-9._-]+$")
_QUEUE: Final = _ecma(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SETTING_SOURCES: Final = _ecma(r"^(?:(?:user|project|local)(?:,(?:user|project|local))*)?$")
_PREFIX: Final = _ecma(r"^[^\s]+$")
# Narrower than the schema's `^[^'\s]+$`: the cron line sources this file inside a shell command.
_ENV_FILE: Final = _ecma(r"^[A-Za-z0-9._/~+@:=,-]+$")
# No C0, DEL or C1 control character, line or paragraph separator, or bidirectional control (a
# newline or tab is allowed only where a field says so).
_INVISIBLE: Final = "\u2028\u2029\u200e\u200f\u202a-\u202e\u2066-\u2069"
_NO_CONTROLS: Final = re.compile(rf"[\x00-\x1f\x7f-\x9f{_INVISIBLE}]")
_HOOK_CMD_CONTROLS: Final = re.compile(rf"[\x00-\x08\x0b-\x1f\x7f-\x9f{_INVISIBLE}]")

DEFAULT_OFF_LIMITS: Final[tuple[str, ...]] = (
    ".github/**", ".gitlab-ci.yml", ".gitlab/**", ".circleci/**", "Jenkinsfile",
    "azure-pipelines.yml", "Dockerfile*", "docker-compose*", "Makefile", "GNUmakefile",
    "**/.husky/**", ".pre-commit-config.yaml", ".gitmodules", "**/.claude/**", "**/.cursor/**",
    "**/.gemini/**", "**/.codex/**", ".mcp.json", "CLAUDE*.md", "AGENTS.md", "GEMINI.md",
    ".cursorrules", ".windsurfrules", ".clinerules", "**/.vscode/**", "**/.devcontainer/**",
    "**/.zed/**", ".envrc", ".gitattributes", "CODEOWNERS", "**/auth/**", "**/security/**",
    "**/secrets/**", "**/crypto/**", "*.pem", "*.key", ".env", ".env.*",
)  # fmt: skip
DEFAULT_TRAILER: Final = "Assisted-by: review-respond (confirmed by <me.name>)"
HOOK_NAMES: Final = ("pre-round", "pre-push", "post-pending", "post-attention", "post-confirm")


def _ms(text: str) -> int:
    """Milliseconds of a duration the schema's pattern accepted; leading zeros are insignificant
    and a number far above the cap is refused without converting it."""
    match = re.fullmatch(r"([0-9]+)(ms|s|m|h|d)?", text)
    if match is None:
        raise ValueError("not a duration")
    digits = match.group(1).lstrip("0") or "0"
    if len(digits) > 15:
        raise ValueError("duration is too large (at most 3650d)")
    return duration_ms(digits + (match.group(2) or ""))


def _duration_in_range(text: str) -> str:
    if _ms(text) > MAX_DURATION_MS:
        raise ValueError("duration is too large (at most 3650d)")
    return text


def _positive_duration(text: str) -> str:
    _duration_in_range(text)
    if _ms(text) < 1:
        raise ValueError("must be positive (not 0)")
    return text


def _no_controls(text: str) -> str:
    if _NO_CONTROLS.search(text):
        raise ValueError("must not contain control characters")
    return text


def _not_blank(text: str) -> str:
    if not text.strip():
        raise ValueError("must not be blank")
    return text


def _not_null(cls: Any, value: Any) -> Any:
    if value is None:
        raise ValueError("must not be null")
    return value


def _unique(items: list[Any]) -> list[Any]:
    seen: set[Any] = set()
    unhashable: list[Any] = []
    for item in items:
        try:
            duplicate = item in seen
            if not duplicate:
                seen.add(item)
        except TypeError:  # an unhashable entry: fall back to comparing
            duplicate = item in unhashable
            if not duplicate:
                unhashable.append(item)
        if duplicate:
            raise ValueError("must not repeat an entry")
    return items


def _unique_or_none(cls: Any, value: list[Any] | None) -> list[Any] | None:
    return value if value is None else _unique(value)


Duration = Annotated[str, StringConstraints(pattern=_DURATION), AfterValidator(_duration_in_range)]
PositiveDuration = Annotated[
    str, StringConstraints(pattern=_DURATION), AfterValidator(_positive_duration)
]
HostKey = Annotated[str, StringConstraints(pattern=_HOST)]
RepoKey = Annotated[str, StringConstraints(pattern=_REPO)]
EnvName = Annotated[str, StringConstraints(pattern=_ENV_NAME)]
AccountEntry = Annotated[str, StringConstraints(pattern=_ACCOUNT_ENTRY)]
Glob = Annotated[str, StringConstraints(min_length=1, max_length=256, pattern=_GLOB)]
Text = Annotated[str, AfterValidator(_no_controls)]
NonBlankText = Annotated[str, AfterValidator(_no_controls), AfterValidator(_not_blank)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, regex_engine="python-re")


class Me(_Model):
    name: str | None = Field(default=None, min_length=1, max_length=64, pattern=_NAME)
    accounts: dict[
        HostKey,
        Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=_ACCOUNT_ID)],
    ] = Field(default_factory=dict)
    _nn = field_validator("name", mode="before")(_not_null)


class HostCredential(_Model):
    token_env: EnvName | None = None
    token_command: list[Annotated[str, StringConstraints(min_length=1, max_length=1024)]] | None = (
        Field(default=None, min_length=1, max_length=32)
    )
    _nn = field_validator("token_env", "token_command", mode="before")(_not_null)

    @field_validator("token_command", mode="after")
    @classmethod
    def _command_has_no_controls(cls, value: list[str] | None) -> list[str] | None:
        for item in value or []:
            _no_controls(item)
        return value


class Agent(_Model):
    timeout: PositiveDuration | None = None


class Jobs(_Model):
    queue: str = Field(default="respond", pattern=_QUEUE)
    kill_grace: PositiveDuration = "125s"
    max_running: int = Field(default=1, ge=1, le=64)
    pass_env: list[EnvName] = Field(default_factory=list)
    _unique_names = field_validator("pass_env", mode="after")(_unique)

    @field_validator("pass_env", mode="after")
    @classmethod
    def _not_review_respond_names(cls, value: list[str]) -> list[str]:
        # Jobs never see REVIEW_RESPOND_* (interface: Job); naming one here could not work.
        if any(name.upper().startswith(ENV_PREFIX) for name in value):
            raise ValueError("names starting with REVIEW_RESPOND_ are never passed to jobs")
        return value


class Git(_Model):
    user_name: NonBlankText | None = Field(default=None, min_length=1, max_length=256)
    user_email: Text | None = Field(default=None, min_length=3, max_length=256)
    # One line: the trailer is the commit message's last paragraph, so it cannot hold a newline.
    trailer: NonBlankText = Field(default=DEFAULT_TRAILER, min_length=1, max_length=256)


class Limits(_Model):
    requests_per_host: int = Field(default=120, ge=1, le=100000)
    prompt_bytes: int = Field(default=65536, ge=1024, le=16777216)
    reply_bytes: int = Field(default=8192, ge=1, le=65536)
    diff_bytes: int = Field(default=262144, ge=1024, le=268435456)


class Attention(_Model):
    ineligible_comment: bool = False


class Retention(_Model):
    records: Duration = "30d"


class Cron(_Model):
    env_file: str | None = Field(default=None, min_length=1, max_length=1024, pattern=_ENV_FILE)


_HOOK_TIMEOUT_MESSAGE: Final = "must be 1 to 600 (seconds) or a duration of at most 600s"


def _hook_timeout(value: Any) -> int | str:
    """One check, one message: an integer of seconds, or a positive duration of at most 600s."""
    if isinstance(value, bool):
        raise ValueError(_HOOK_TIMEOUT_MESSAGE)
    if isinstance(value, int):
        if not 1 <= value <= 600:
            raise ValueError(_HOOK_TIMEOUT_MESSAGE)
        return value
    if isinstance(value, str) and re.fullmatch(_DURATION, value):
        try:
            ms = _ms(value)
        except ValueError:
            raise ValueError(_HOOK_TIMEOUT_MESSAGE) from None
        if 1 <= ms <= MAX_HOOK_TIMEOUT_MS:
            return value
    raise ValueError(_HOOK_TIMEOUT_MESSAGE)


class HookCmd(_Model):
    cmd: str = Field(min_length=1, max_length=4096)
    timeout: Annotated[int | str, BeforeValidator(_hook_timeout)] = 30
    async_: bool = Field(default=False, alias="async")

    @field_validator("cmd", mode="after")
    @classmethod
    def _plain_command(cls, value: str) -> str:
        if _HOOK_CMD_CONTROLS.search(value):
            raise ValueError("must not contain control characters (a newline or tab is allowed)")
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @property
    def timeout_ms(self) -> int:
        return self.timeout * 1000 if isinstance(self.timeout, int) else _ms(self.timeout)


class Hooks(_Model):
    pre_round: list[HookCmd] = Field(default_factory=list, alias="pre-round")
    pre_push: list[HookCmd] = Field(default_factory=list, alias="pre-push")
    post_pending: list[HookCmd] = Field(default_factory=list, alias="post-pending")
    post_attention: list[HookCmd] = Field(default_factory=list, alias="post-attention")
    post_confirm: list[HookCmd] = Field(default_factory=list, alias="post-confirm")

    @field_validator("pre_round", "pre_push", mode="after")
    @classmethod
    def _pre_hooks_do_not_run_async(cls, value: list[HookCmd]) -> list[HookCmd]:
        # A pre hook decides the command's outcome, so it is always waited for.
        if any(hook.async_ for hook in value):
            raise ValueError("async is only for post hooks")
        return value


class RepoRespond(_Model):
    allow_commands: list[
        Annotated[
            list[Annotated[str, StringConstraints(min_length=1, max_length=256)]],
            Field(min_length=1, max_length=32),
        ]
    ] = Field(default_factory=list, max_length=64)
    read_dirs: list[Annotated[str, StringConstraints(min_length=1, max_length=1024)]] = Field(
        default_factory=list, max_length=64
    )

    @field_validator("allow_commands", mode="after")
    @classmethod
    def _argv_without_controls(cls, value: list[list[str]]) -> list[list[str]]:
        for argv in value:
            for item in argv:
                _no_controls(item)
        return value

    @field_validator("read_dirs", mode="after")
    @classmethod
    def _dirs_without_controls(cls, value: list[str]) -> list[str]:
        for item in value:
            _no_controls(item)
        return value


class RepoOverride(_Model):
    """A key left out inherits the global value (so ``reviewers: []`` is an explicit empty list)."""

    respond_fresh: bool | None = None
    respond: RepoRespond = Field(default_factory=RepoRespond)
    settle: Duration | None = None
    max_rounds: int | None = Field(default=None, ge=1, le=1000)
    reviewers: list[AccountEntry] | None = None
    _nn = field_validator("respond_fresh", "settle", "max_rounds", "reviewers", mode="before")(
        _not_null
    )

    @field_validator("reviewers", mode="after")
    @classmethod
    def _unique_reviewers(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _unique(value)


@dataclass(frozen=True)
class RepoSettings:
    """The per-repo view: the ``repos.<id>`` entry over the global values."""

    settle: str
    max_rounds: int
    reviewers: tuple[str, ...]
    respond_fresh: bool
    allow_commands: tuple[tuple[str, ...], ...]
    read_dirs: tuple[str, ...]


class Config(_Model):
    me: Me = Field(default_factory=Me)
    hosts: dict[HostKey, HostCredential] = Field(default_factory=dict)
    hosts_to_check: list[HostKey] | None = None
    reviewers: list[AccountEntry] = Field(default_factory=list)
    bot_authors: list[AccountEntry] = Field(default_factory=list)
    respond_to_own_comments_prefix: Text | None = Field(
        default=None, min_length=1, max_length=64, pattern=_PREFIX
    )
    settle: Duration = "5m"
    max_rounds: int = Field(default=3, ge=1, le=1000)
    pending_ttl: PositiveDuration = "7d"
    access_cache_ttl: Duration = "1h"
    prefer_owner: str = Field(default="ticket-work", min_length=1, max_length=64, pattern=_OWNER)
    resolve_after_confirm: bool = False
    off_limits: list[Glob] = Field(default_factory=lambda: list(DEFAULT_OFF_LIMITS))
    skill: NonBlankText = Field(default="respond-to-review", min_length=1, max_length=128)
    permission_profile: Text | None = Field(default=None, min_length=1, max_length=1024)
    sandbox: Literal["required", "preferred", "off"] = "required"
    setting_sources: str = Field(default="", pattern=_SETTING_SOURCES)
    env_allowlist: list[EnvName] = Field(default_factory=list)
    model: NonBlankText | None = Field(default=None, min_length=1, max_length=128)
    agent: Agent = Field(default_factory=Agent)
    jobs: Jobs = Field(default_factory=Jobs)
    git: Git = Field(default_factory=Git)
    limits: Limits = Field(default_factory=Limits)
    attention: Attention = Field(default_factory=Attention)
    retention: Retention = Field(default_factory=Retention)
    cron: Cron = Field(default_factory=Cron)
    hooks: Hooks = Field(default_factory=Hooks)
    repos: dict[RepoKey, RepoOverride] = Field(default_factory=dict)
    # Keys that may be null are `null`-able on purpose (prefix, profile, model); the rest may
    # not be written as null.
    _nn = field_validator(
        "hosts_to_check",
        "settle",
        "max_rounds",
        "pending_ttl",
        "access_cache_ttl",
        "prefer_owner",
        "resolve_after_confirm",
        "skill",
        "sandbox",
        "setting_sources",
        mode="before",
    )(_not_null)
    _unique_lists = field_validator(
        "hosts_to_check", "reviewers", "bot_authors", "off_limits", "env_allowlist", mode="after"
    )(_unique_or_none)

    def me_name(self) -> str:
        """``me.name``, else the OS user name (cleaned to the name set)."""
        return self.me.name or os_user_name()

    def repo_settings(self, repo_id: str) -> RepoSettings:
        """``repos.<repo_id>`` over the global values (which already hold environment over files
        over defaults); precedence: flags (the caller) > repo > environment > config > defaults."""
        entry = self.repos.get(repo_id)
        if entry is None:
            entry = RepoOverride()
        return RepoSettings(
            settle=entry.settle if entry.settle is not None else self.settle,
            max_rounds=entry.max_rounds if entry.max_rounds is not None else self.max_rounds,
            reviewers=tuple(entry.reviewers if entry.reviewers is not None else self.reviewers),
            respond_fresh=bool(entry.respond_fresh),
            allow_commands=tuple(tuple(argv) for argv in entry.respond.allow_commands),
            read_dirs=tuple(entry.respond.read_dirs),
        )

    def canonical(self) -> dict[str, Any]:
        """The merged config after defaults, as JSON data (audit's ``config_sha256`` input)."""
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)

    def sha256(self) -> str:
        text = json.dumps(
            self.canonical(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return hashlib.sha256(text.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class LoadedConfig:
    config: Config
    warnings: tuple[ConfigWarning, ...]
    files: tuple[Path, ...]


# --- merge and validation ----------------------------------------------------------------------


def _merge(
    base: dict[str, Any], over: dict[str, Any], path: tuple[str, ...] = ()
) -> dict[str, Any]:
    """``over`` on top of ``base``: maps merge, hook lists append, anything else is replaced."""
    out = dict(base)
    for key, value in over.items():
        current = out.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            out[key] = _merge(current, value, (*path, key))  # pyright: ignore[reportUnknownArgumentType]
        elif (
            path == ("hooks",)
            and key in HOOK_NAMES
            and isinstance(current, list)
            and isinstance(value, list)
        ):
            out[key] = current + value  # pyright: ignore[reportUnknownVariableType]
        else:
            out[key] = value
    return out


_DURATION_HINT: Final = "a duration such as 500ms, 30s, 5m, 2h or 7d"
_DURATION_KEYS: Final = frozenset(
    {
        "settle", "pending_ttl", "access_cache_ttl", "agent.timeout", "jobs.kill_grace",
        "retention.records",
    }
)  # fmt: skip


_VALIDATOR_LOC: Final = re.compile(r"function-(?:after|before|wrap|plain)\[")


def _dotted(loc: tuple[Any, ...]) -> str:
    out = ""
    for part in loc:
        if part == "[key]" or (isinstance(part, str) and _VALIDATOR_LOC.match(part)):
            continue  # pydantic internals, not keys
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += ("." if out else "") + _esc(str(part))
    return out or "(top level)"


def _message(error: Mapping[str, Any], key: str) -> str:
    kind = error["type"]
    if kind == "extra_forbidden":
        return "unknown key"
    is_duration = key in _DURATION_KEYS or key.startswith("repos.") and key.endswith(".settle")
    if kind == "string_type" and is_duration:
        return "must be a duration string; write it quoted, for example '0s' or '5m'"
    if kind == "string_pattern_mismatch":
        return "has an invalid format" + (f" ({_DURATION_HINT})" if is_duration else "")
    return str(error["msg"]).removeprefix("Value error, ")


def _validate(
    document: dict[str, Any], source: str, env_origin: Mapping[str, str] | None = None
) -> Config:
    try:
        return Config.model_validate(document)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False, include_context=False)
    problems: list[tuple[str, str, str]] = []  # (source, key, message)
    for error in errors:
        key = _dotted(error["loc"])
        where = source
        if env_origin and key in env_origin:
            where = f"environment variable {env_origin[key]}"
        problems.append((where, key, _message(error, key)))
    first, rest = problems[0], problems[1:]
    lines = [
        (f"{s}: " if s != first[0] else "") + f"{k}: {m}"
        for s, k, m in rest[: _MAX_ERRORS_SHOWN - 1]
    ]
    if len(rest) > _MAX_ERRORS_SHOWN - 1:
        lines.append(f"and {len(rest) - (_MAX_ERRORS_SHOWN - 1)} more")
    raise ConfigError(first[0], first[1], first[2], tuple(lines))


# --- environment -------------------------------------------------------------------------------

# The only keys the environment may set (interface: Environment variables), and their kind.
ENV_KEYS: Final[dict[str, str]] = {
    "settle": "str",
    "max_rounds": "int",
    "access_cache_ttl": "str",
    "prefer_owner": "str",
    "retention.records": "str",
    "limits.requests_per_host": "int",
}
_ENV_BY_NAME: Final = {p.upper().replace(".", "__"): p for p in ENV_KEYS}
# Names the interface documents for other purposes (hook variables).
_HOOK_VARS: Final = frozenset({"HOOK", "ID", "OBJECT_TYPE"})
_INT: Final = re.compile(r"(?:0|[1-9][0-9]{0,17})")
_FILE_ONLY_TOP: Final = frozenset(
    name.upper()
    for name in (
        "off_limits sandbox permission_profile setting_sources env_allowlist skill hosts jobs git "
        "hooks reviewers bot_authors respond_to_own_comments_prefix resolve_after_confirm repos "
        "me hosts_to_check pending_ttl model agent limits attention retention cron"
    ).split()
)


def _env_value(var: str, path: str, raw: str) -> Any:
    if ENV_KEYS[path] == "int":
        if not _INT.fullmatch(raw):
            raise ConfigError(
                f"environment variable {var}",
                path,
                "must be a plain decimal integer of at most 18 digits (no leading zeros)",
            )
        return int(raw)
    return raw


def _set_path(document: dict[str, Any], path: str, value: Any) -> None:
    *parents, leaf = path.split(".")
    node = document
    for part in parents:
        child = node.get(part)
        if not isinstance(child, dict):
            child = node[part] = {}
        node = child  # pyright: ignore[reportUnknownVariableType]
    node[leaf] = value


def _env_overlay(
    environ: Mapping[str, str],
) -> tuple[dict[str, Any], dict[str, str], list[ConfigWarning]]:
    overlay: dict[str, Any] = {}
    origin: dict[str, str] = {}
    warnings: list[ConfigWarning] = []
    for var in sorted(environ):
        if not var.startswith(ENV_PREFIX):
            continue
        rest = var[len(ENV_PREFIX) :]
        if rest in _HOOK_VARS:
            continue
        path = _ENV_BY_NAME.get(rest)
        if path is None:
            if rest.split("__", 1)[0] in _FILE_ONLY_TOP:
                why = "cannot be set from the environment (config file only)"
            else:
                why = "is not a config variable"
            warnings.append(
                ConfigWarning("env_ignored", _esc(var, 253), f"{_esc(var)} {why}; ignored")
            )
            continue
        _set_path(overlay, path, _env_value(var, path, environ[var]))
        origin[path] = var
    return overlay, origin, warnings


def _config_warnings(config: Config) -> list[ConfigWarning]:
    if config.sandbox == "required":
        return []
    return [
        ConfigWarning(
            "sandbox_weakened",
            "sandbox",
            f"sandbox is {config.sandbox}: the respond session may run without a sandbox",
        )
    ]


# --- loading -----------------------------------------------------------------------------------


def default_config_path(environ: Mapping[str, str] | None = None) -> Path:
    """``$XDG_CONFIG_HOME/review-respond/config.yaml`` (only when absolute), else ``~/.config``."""
    env = os.environ if environ is None else environ
    xdg = env.get("XDG_CONFIG_HOME", "")
    if xdg.startswith("/"):
        base = Path(xdg)
    else:
        home = env.get("HOME") or os.path.expanduser("~")
        base = Path(home) / ".config"
    return base / "review-respond" / "config.yaml"


def _dropin_paths(directory: Path) -> list[Path]:
    name = _name(directory)
    try:
        info = os.stat(directory)
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as exc:
        raise ConfigError(name, None, f"cannot be read ({exc.strerror})") from None
    except ValueError:
        raise ConfigError(name, None, "is not a valid path") from None
    if not stat.S_ISDIR(info.st_mode):
        raise ConfigError(name, None, "not a directory")
    _check_owner_and_mode(info, directory)
    try:
        entries = sorted(os.listdir(directory), key=os.fsencode)
    except OSError as exc:
        raise ConfigError(name, None, f"cannot be read ({exc.strerror})") from None
    names = [n for n in entries if n.endswith(".yaml") and not n.startswith(".")]
    if len(names) > MAX_DROP_INS:
        raise ConfigError(name, None, f"more than {MAX_DROP_INS} drop-in files")
    return [directory / n for n in names]


def load_config(
    explicit: Path | str | None = None, environ: Mapping[str, str] | None = None
) -> LoadedConfig:
    """Load, merge and validate the config.

    ``explicit`` is the ``--config`` file: it must exist and replaces ``config.yaml``; drop-ins
    are read from the ``config.d`` beside it (a job gets the check's config path, so both read
    the same files). Without it a missing default file means built-in defaults. Each file is
    validated alone (so an error names its file), then the merge plus the environment as a whole.
    """
    env = os.environ if environ is None else environ
    path = Path(os.fspath(explicit)) if explicit is not None else default_config_path(env)
    files: list[Path] = []
    if explicit is not None:
        files.append(path)
    else:
        try:
            os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            pass  # no default file: built-in defaults
        except OSError as exc:
            raise ConfigError(_name(path), None, f"cannot be read ({exc.strerror})") from None
        except ValueError:
            raise ConfigError(_name(path), None, "is not a valid path") from None
        else:
            files.append(path)
    files.extend(_dropin_paths(path.parent / "config.d"))
    merged: dict[str, Any] = {}
    total = 0
    for index, file in enumerate(files):
        data = _read_file(file)
        if index == 0 and file == path:
            _check_parent(path)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ConfigError(
                _name(file),
                None,
                f"the config files together are larger than {MAX_TOTAL_BYTES} bytes",
            )
        document = _parse(data, _name(file))
        _validate(document, _name(file))
        merged = _merge(merged, document)
    overlay, origin, warnings = _env_overlay(env)
    merged = _merge(merged, overlay)
    if len(files) == 1:
        label = _name(files[0])
    else:
        label = "merged config" if files else "environment"
    config = _validate(merged, label, origin)
    warnings.extend(_config_warnings(config))
    return LoadedConfig(config=config, warnings=tuple(warnings), files=tuple(files))
