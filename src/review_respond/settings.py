"""Session settings and the generated permission profile (docs/interface.md: Session settings).

Every round passes the same kinds of safety settings to agent-spawn. This module derives them
from the loaded config and writes the permission profile file; it starts no process and reads
nothing from the round throwaway (a pull request's files are untrusted, so a planted
``.claude/settings.json`` with hooks must never be looked at; ``setting_sources`` never names
``project`` or ``local`` for the same reason).

The profile holds only configuration values and fixed strings, never text from a pull request.
Its shape is ``schemas/permission-profile.json``; ``validate_profile`` is the same check in code
(the schema validator is a test-only dependency) and ``tests/test_settings.py`` keeps the two in
agreement. The output is canonical JSON, so the same input gives the same bytes.

Deny by default: the run's permission mode is ``dontAsk`` (claude-session forces it for an
isolated run), so what the allow list does not name is refused. The allow list reads and edits
the throwaway (``${CWD}``, which claude-session expands to the run's cwd) and the repo's
``respond.read_dirs``, and runs ``respond.allow_commands`` and a few read-only git commands.
A fixed deny list always applies, also to a replacement profile. The sandbox has no network.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .config import (
    Config,
    ConfigError,
    ConfigWarning,
    RepoSettings,
    load_document,
    sandbox_warnings,
)
from .errors import CliError
from .prompt import sanitize
from .store import StateDir

PROFILE_DIR: Final = "profiles"
KIND: Final = "respond"
MODES: Final = frozenset({"headless", "interactive"})
MAX_PATH_CHARS: Final = 1024
MAX_LIST: Final = 64
MAX_ALLOW_RULES: Final = 256
MAX_ARGV: Final = 32
MAX_ARG_CHARS: Final = 256
MAX_COMMAND_CHARS: Final = 1024
MAX_ENV_NAMES: Final = 64

# The deny rules every round has (also with a replacement profile). Kept equal to the ``deny``
# ``contains`` list of schemas/permission-profile.json by a test.
MANDATORY_DENIES: Final = (
    "Bash(git push:*)",
    "Bash(git remote:*)",
    "Bash(git config:*)",
    "Bash(git credential:*)",
    "Bash(gh:*)",
    "Bash(glab:*)",
    "Bash(agent-spawn:*)",
    "Bash(memoctl add:*)",
    "Bash(memoctl delete:*)",
    "Bash(memoctl cron:*)",
    "Bash(notice-route send:*)",
    "WebFetch",
    "WebSearch",
    "mcp__*",
    "Agent",
    "Task",
)

_GIT_READ_ONLY: Final = ("status", "diff", "log", "show", "ls-files", "blame", "rev-parse")
_BUNDLED_ALLOW: Final = (
    "Read(/${CWD}/**)",
    "Glob",
    "Grep",
    "Edit(/${CWD}/**)",
    "Write(/${CWD}/**)",
    "Skill",
    *(f"Bash(git {sub}:*)" for sub in _GIT_READ_ONLY[:-1]),
)

_PATH_BAD_CHARS: Final = frozenset("*?[]{}\\")
_CREDENTIAL_SEGMENTS: Final = frozenset(
    {".ssh", ".aws", ".gnupg", ".kube", ".docker", ".config", ".claude", ".netrc", ".git-credentials"}
)
# Programs a command pattern may not start with: shells and launchers (they run anything), the
# network, and the tools the profile denies or that act with the user's credentials.
_REFUSED_PROGRAMS: Final = frozenset(
    {
        "sh", "bash", "dash", "zsh", "fish", "ksh", "csh", "tcsh", "ash", "busybox",
        "env", "sudo", "su", "doas", "xargs", "eval", "exec", "nohup", "nice", "ionice",
        "timeout", "time", "watch", "setsid", "command", "builtin", "find", "awk",
        "curl", "wget", "nc", "ncat", "socat", "ssh", "scp", "sftp", "rsync", "ftp", "telnet",
        "npx", "bunx", "uvx", "pipx", "gh", "glab", "agent-spawn", "memoctl", "notice-route",
        "review-respond", "forgectl", "pr-comment", "wtctl", "jobsctl", "claude-session",
        "claude", "ticketctl", "termctl", "gatectl",
    }
)  # fmt: skip
_INTERPRETER: Final = re.compile(
    r"python[0-9.]*|node(?:js)?|ruby|perl|php|lua|deno|bun|pwsh|powershell"
)
_EVAL_FLAGS: Final = frozenset({"-c", "-e", "--eval", "--exec", "-exec", "-E"})
_ARG_CHARS: Final = re.compile(r"[A-Za-z0-9._/=+@%,-]+")

_ENV_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_ENV_REFUSED: Final = frozenset(
    {
        "PATH", "HOME", "IFS", "ENV", "BASH_ENV", "SHELLOPTS", "PS4", "NODE_OPTIONS", "NODE_PATH",
        "PERL5LIB", "PERL5OPT", "RUBYOPT", "RUBYLIB", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS",
        "CLASSPATH", "SSH_AUTH_SOCK",
    }
)  # fmt: skip
_ENV_REFUSED_PREFIXES: Final = (
    "LD_", "DYLD_", "GIT_", "PYTHON", "REVIEW_RESPOND_", "XDG_", "CLAUDE", "ANTHROPIC",
)  # fmt: skip
_ENV_SECRET: Final = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_?KEY|PRIVATE_?KEY|AUTH", re.IGNORECASE
)

_ROUND_ID: Final = re.compile(r"rd-[a-z2-7]{8}")
_TIMEOUT: Final = re.compile(r"[0-9]{1,9}(?:ms|s|m|h|d)")
_PLAIN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+=-]*")

_CWD_RULE: Final = re.compile(r"/\$\{CWD\}(?:/[^*?\[\]{}\\]+)*/\*\*")
_ABS_RULE: Final = re.compile(r"(?://|~/)[^*?\[\]{}\\]+/\*\*")


def _refuse(key: str, message: str, value: object = None) -> CliError:
    """``config_invalid`` naming the key; a value is shown through the one sanitizer, cut."""
    shown = "" if value is None else f" ({sanitize(str(value), max_chars=60)})"
    return CliError("config_invalid", f"{key}: {message}{shown}", details={"key": key})


# ---- paths ----------------------------------------------------------------------------------


def _segments_ok(rest: str) -> bool:
    return all(seg not in ("", ".", "..") for seg in rest.split("/"))


def _plain_abs(value: object, key: str, *, home: bool = False) -> str:
    """A normalized absolute path (``~/x`` too when ``home``) with no glob or control character."""
    if not isinstance(value, str) or not value:
        raise _refuse(key, "must be a non-empty path string")
    shown = value
    if len(value) > MAX_PATH_CHARS:
        raise _refuse(key, f"longer than {MAX_PATH_CHARS} characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise _refuse(key, "is not valid UTF-8") from None
    if sanitize(value) != value:
        raise _refuse(key, "has control or invisible characters", shown)
    if any(ch in _PATH_BAD_CHARS for ch in value):
        raise _refuse(key, "has a glob or escape character", shown)
    if value.startswith("~/") and home:
        rest = value[2:]
    elif value.startswith("/") and not value.startswith("//"):
        rest = value[1:]
    else:
        raise _refuse(key, "must be an absolute path" + (" or ~/..." if home else ""), shown)
    if not rest or not _segments_ok(rest):
        raise _refuse(key, "must be normalized (no empty, . or .. part, no trailing slash)", shown)
    return value


def _is_inside(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent + "/")


def _check_read_dir(value: object, key: str, protected: Sequence[str]) -> str:
    path = _plain_abs(value, key, home=True)
    if any(seg in _CREDENTIAL_SEGMENTS for seg in path.split("/")):
        raise _refuse(key, "names a credential or configuration directory", path)
    for other in protected:
        if _is_inside(path, other) or _is_inside(other, path):
            raise _refuse(key, "overlaps the review-respond config or state directory", path)
    return path


def check_throwaway_root(path: str, *, config_dir: str, state_dir: str) -> str:
    """The round throwaway's real path, or ``config_invalid``.

    Refused: a relative, non-normalized or symlinked path (it must already be its own realpath),
    glob, control and invisible characters, anything that is not a directory, and any overlap with
    review-respond's own config or state directory. Looks only at the path itself (``lstat``
    and ``realpath``), never inside it.
    """
    root = _plain_abs(path, "throwaway")
    try:
        real = os.path.realpath(root, strict=True)
        info = os.lstat(root)
    except (OSError, ValueError):
        raise _refuse("throwaway", "does not exist or cannot be resolved", root) from None
    if real != root:
        raise _refuse("throwaway", "must be its own real path (a symlink is in the way)", root)
    if not stat.S_ISDIR(info.st_mode):
        raise _refuse("throwaway", "is not a directory", root)
    for other in (config_dir, state_dir):
        if _is_inside(root, other) or _is_inside(other, root):
            raise _refuse("throwaway", "overlaps the review-respond config or state directory", root)
    return real


# ---- command patterns and allow rules ------------------------------------------------------


def _check_argv(argv: Sequence[object], key: str) -> tuple[str, ...]:
    if not argv or len(argv) > MAX_ARGV:
        raise _refuse(key, f"a command pattern has 1 to {MAX_ARGV} elements")
    items: list[str] = []
    for item in argv:
        if (
            not isinstance(item, str)
            or len(item) > MAX_ARG_CHARS
            or not _ARG_CHARS.fullmatch(item)
            or not _segments_ok(item.strip("/") or "x")
            or item.startswith("//")
        ):
            raise _refuse(
                key, "a pattern element must be 1 to 256 characters of A-Za-z0-9._/=+@%,-", item
            )
        items.append(item)
    program = os.path.basename(items[0]).lower()
    if items[0].startswith("-") or program in _REFUSED_PROGRAMS:
        raise _refuse(key, "this program cannot be allowed", items[0])
    if program == "git" and (len(items) < 2 or items[1] not in _GIT_READ_ONLY):
        raise _refuse(key, "only read-only git commands can be allowed", items[0])
    if _INTERPRETER.fullmatch(program) and len(items) < 2:
        raise _refuse(key, "an interpreter needs an argument (such as -m pytest)", items[0])
    if any(item in _EVAL_FLAGS for item in items[1:]):
        raise _refuse(key, "an evaluate-code flag cannot be allowed")
    if len(" ".join(items)) > MAX_COMMAND_CHARS:
        raise _refuse(key, f"a command pattern is longer than {MAX_COMMAND_CHARS} characters")
    return tuple(items)


def check_allow_rule(rule: object) -> str:
    """An allow rule is one of the forms the profile may grant, or ``config_invalid``."""
    key = "permission_profile"
    if not isinstance(rule, str) or sanitize(rule) != rule:
        raise _refuse(key, "an allow rule has control or invisible characters", rule)
    m = re.fullmatch(r"([A-Za-z]+)(?:\((.*)\))?", rule, re.DOTALL)
    if m is None:
        raise _refuse(key, "an allow rule is not a tool name or Tool(pattern)", rule)
    tool, arg = m.group(1), m.group(2)
    ok = False
    if tool in ("Glob", "Grep"):
        ok = arg is None
    elif tool == "Skill":
        ok = arg is None or bool(_PLAIN.fullmatch(arg))
    elif tool == "Bash":
        if arg is not None and arg.endswith(":*") and not arg.startswith((":", "*")):
            _check_argv(arg[:-2].split(" "), key)
            ok = True
    elif tool == "Read":
        ok = arg is not None and bool(_CWD_RULE.fullmatch(arg) or _ABS_RULE.fullmatch(arg))
    elif tool in ("Edit", "Write"):
        ok = arg is not None and bool(_CWD_RULE.fullmatch(arg))
    if not ok or (arg is not None and ".." in arg.split("/")):
        raise _refuse(key, "this allow rule is not permitted for a respond session", rule)
    return rule


# ---- the profile ----------------------------------------------------------------------------


def _dedupe(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _dict(value: object, key: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise _refuse(key, "must be a mapping")
    return value  # pyright: ignore[reportUnknownVariableType]


def _strings(value: object, key: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):  # pyright: ignore[reportUnknownVariableType]
        raise _refuse(key, "must be a list of strings")
    return list(value)  # pyright: ignore[reportUnknownArgumentType]


def _read_base(base: object, protected: Sequence[str]) -> tuple[list[str], list[str], list[str]]:
    """(allow, deny, allowRead) of a replacement profile; only these keys are accepted."""
    doc = _dict(base, "permission_profile")
    if not set(doc) <= {"permissions", "sandbox"}:
        raise _refuse("permission_profile", "only the permissions and sandbox keys are allowed")
    perms = _dict(doc.get("permissions", {}), "permission_profile.permissions")
    if not set(perms) <= {"allow", "deny"}:
        raise _refuse("permission_profile.permissions", "only allow and deny are allowed")
    allow = [check_allow_rule(r) for r in _strings(perms.get("allow", []), "permissions.allow")]
    deny = _strings(perms.get("deny", []), "permissions.deny")
    box = _dict(doc.get("sandbox", {}), "permission_profile.sandbox")
    if not set(box) <= {"filesystem"}:
        raise _refuse("permission_profile.sandbox", "only filesystem.allowRead is allowed")
    fs = _dict(box.get("filesystem", {}), "permission_profile.sandbox.filesystem")
    if not set(fs) <= {"allowRead"}:
        raise _refuse("permission_profile.sandbox.filesystem", "only allowRead is allowed")
    reads = [
        _check_read_dir(p, "permission_profile.sandbox.filesystem.allowRead", protected)
        for p in _strings(fs.get("allowRead", []), "allowRead")
    ]
    return allow, deny, reads


def build_profile(
    repo: RepoSettings,
    *,
    config_dir: str,
    state_dir: str,
    base: object = None,
) -> dict[str, Any]:
    """The permission profile of one round (a JSON-ready document, validated).

    ``base`` is the parsed replacement profile of the ``permission_profile`` key (``None``: the
    bundled one). ``config_dir`` and ``state_dir`` are review-respond's own directories, denied
    for reading and writing.
    """
    cfg = _plain_abs(config_dir, "config_dir")
    state = _plain_abs(state_dir, "state_dir")
    own = (cfg, state)
    if len(repo.allow_commands) > MAX_LIST:
        raise _refuse("respond.allow_commands", f"more than {MAX_LIST} patterns")
    if len(repo.read_dirs) > MAX_LIST:
        raise _refuse("respond.read_dirs", f"more than {MAX_LIST} entries")
    if base is None:
        allow, deny, reads = list(_BUNDLED_ALLOW), [], []
    else:
        allow, deny, reads = _read_base(base, own)
    for argv in repo.allow_commands:
        items = _check_argv(argv, "respond.allow_commands")
        allow.append(f"Bash({' '.join(items)}:*)")
    for entry in repo.read_dirs:
        path = _check_read_dir(entry, "respond.read_dirs", own)
        reads.append(path)
        allow.append(f"Read(/{path}/**)" if path.startswith("/") else f"Read({path}/**)")
    deny += [*MANDATORY_DENIES]
    for tool in ("Read", "Edit", "Write"):
        deny += [f"{tool}(/{d}/**)" for d in own]
    doc: dict[str, Any] = {
        "permissions": {"allow": _dedupe(allow), "deny": _dedupe(deny)},
        "sandbox": {
            "filesystem": {
                "allowRead": _dedupe(reads),
                "denyRead": list(own),
                "allowWrite": [],
                "denyWrite": list(own),
            },
            "network": {"allowedDomains": []},
        },
    }
    validate_profile(doc)
    return doc


def _list(doc: object, key: str, *, max_items: int, min_items: int = 0) -> list[Any]:
    if not isinstance(doc, list) or not min_items <= len(doc) <= max_items:  # pyright: ignore[reportUnknownArgumentType]
        raise _refuse(key, f"must be a list of {min_items} to {max_items} items")
    return doc  # pyright: ignore[reportUnknownVariableType]


def validate_profile(doc: object) -> None:
    """``schemas/permission-profile.json`` in code; ``config_invalid`` on any difference."""
    top = _dict(doc, "profile")
    if set(top) != {"permissions", "sandbox"}:
        raise _refuse("profile", "must hold exactly permissions and sandbox")
    perms = _dict(top["permissions"], "profile.permissions")
    if set(perms) != {"allow", "deny"}:
        raise _refuse("profile.permissions", "must hold exactly allow and deny")
    for name, min_items in (("allow", 1), ("deny", 0)):
        rules = _list(perms[name], f"profile.permissions.{name}", max_items=MAX_ALLOW_RULES, min_items=min_items)
        if len(set(map(str, rules))) != len(rules):
            raise _refuse(f"profile.permissions.{name}", "has a duplicate")
        for rule in rules:
            if not isinstance(rule, str) or not 0 < len(rule) <= 2048 or sanitize(rule) != rule:
                raise _refuse(f"profile.permissions.{name}", "has an invalid rule", rule)
    missing = [r for r in MANDATORY_DENIES if r not in perms["deny"]]
    if missing:
        raise _refuse("profile.permissions.deny", "lacks a fixed deny", missing[0])
    box = _dict(top["sandbox"], "profile.sandbox")
    if set(box) != {"filesystem", "network"}:
        raise _refuse("profile.sandbox", "must hold exactly filesystem and network")
    fs = _dict(box["filesystem"], "profile.sandbox.filesystem")
    if set(fs) != {"allowRead", "denyRead", "allowWrite", "denyWrite"}:
        raise _refuse("profile.sandbox.filesystem", "has the wrong keys")
    if fs["allowWrite"] != []:
        raise _refuse("profile.sandbox.filesystem.allowWrite", "must be empty")
    for name, min_items in (("allowRead", 0), ("denyRead", 2), ("denyWrite", 2)):
        paths = _list(fs[name], f"profile.sandbox.filesystem.{name}", max_items=MAX_LIST, min_items=min_items)
        if len(set(map(str, paths))) != len(paths):
            raise _refuse(f"profile.sandbox.filesystem.{name}", "has a duplicate")
        for p in paths:
            _plain_abs(p, f"profile.sandbox.filesystem.{name}", home=True)
    net = _dict(box["network"], "profile.sandbox.network")
    if net != {"allowedDomains": []}:
        raise _refuse("profile.sandbox.network", "the respond session has no network")


def render_profile(doc: Mapping[str, Any]) -> bytes:
    """Canonical JSON: sorted keys, two-space indent, ASCII, one trailing newline."""
    return (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


def _profile_name(round_id: str) -> str:
    if not _ROUND_ID.fullmatch(round_id):
        raise ValueError("not a round id")
    return round_id + ".json"


def write_profile_file(state: StateDir, round_id: str, doc: Mapping[str, Any]) -> Path:
    """Validate ``doc`` and write it to ``<state>/profiles/<round id>.json`` (directory 0700,
    file 0600, created exclusively, never through a symlink). The path goes to agent-spawn's
    ``--permission-profile``."""
    name = _profile_name(round_id)
    validate_profile(doc)
    return state.write_private_file(PROFILE_DIR, name, render_profile(doc))


def remove_profile_file(state: StateDir, round_id: str) -> bool:
    """Delete the round's profile file; ``False`` when there is none."""
    return state.remove_private_file(PROFILE_DIR, _profile_name(round_id))


def load_base_profile(path: str) -> dict[str, Any]:
    """The replacement profile named by ``permission_profile`` (read like a config file)."""
    try:
        return load_document(Path(path))
    except ConfigError as exc:
        raise exc.to_cli_error() from None


# ---- the session settings -------------------------------------------------------------------


def _check_env_names(names: Sequence[str], token_envs: Iterable[str]) -> tuple[str, ...]:
    if len(names) > MAX_ENV_NAMES:
        raise _refuse("env_allowlist", f"more than {MAX_ENV_NAMES} names")
    tokens = set(token_envs)
    for name in names:
        if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise _refuse("env_allowlist", "not a valid variable name", name)
        if (
            name in _ENV_REFUSED
            or name in tokens
            or name.startswith(_ENV_REFUSED_PREFIXES)
            or _ENV_SECRET.search(name)
        ):
            raise _refuse("env_allowlist", "this variable cannot be passed to the session", name)
    return tuple(sorted(set(names)))


def _check_setting_sources(text: str) -> tuple[str, ...]:
    sources = tuple(s for s in text.split(",") if s)
    if text and (sources != tuple(dict.fromkeys(sources)) or any(s != "user" for s in sources)):
        raise _refuse(
            "setting_sources",
            "only user can be loaded: project and local settings come from the checkout",
            text,
        )
    return sources


def _plain_option(value: str | None, key: str) -> str | None:
    if value is None:
        return None
    if len(value) > 128 or not _PLAIN.fullmatch(value) or sanitize(value) != value:
        raise _refuse(key, "must be plain text that does not start with a dash", value)
    return value


@dataclass(frozen=True)
class SessionSettings:
    """The per-call safety settings of one round (what agent-spawn is given)."""

    kind: str
    mode: str
    skill: str
    sandbox: str
    setting_sources: tuple[str, ...]
    env_allowlist: tuple[str, ...]
    model: str | None
    timeout: str | None
    profile: dict[str, Any]
    warnings: tuple[ConfigWarning, ...]

    def agent_spawn_kwargs(
        self, *, plugin_dir: str | os.PathLike[str], prompt_file: str | os.PathLike[str],
        permission_profile: str | os.PathLike[str],
    ) -> dict[str, Any]:  # fmt: skip
        """Keyword arguments of ``AgentSpawn.resume_fork`` and ``launch`` (not ``cwd``, ``repo``,
        ``labels`` or ``actor``)."""
        return {
            "kind": self.kind,
            "mode": self.mode,
            "skill": self.skill,
            "plugin_dir": plugin_dir,
            "prompt_file": prompt_file,
            "permission_profile": permission_profile,
            "sandbox": self.sandbox,
            "setting_sources": list(self.setting_sources),
            "env_allowlist": list(self.env_allowlist),
            "model": self.model,
            "timeout": self.timeout,
        }


def build_session_settings(
    config: Config,
    repo_id: str,
    *,
    config_dir: str,
    state_dir: str,
    mode: str = "headless",
) -> SessionSettings:
    """The settings of a round for ``repo_id``; ``config_invalid`` for an unsafe value."""
    if mode not in MODES:
        raise ValueError("mode is headless or interactive")
    if config.sandbox not in ("required", "preferred", "off"):
        raise _refuse("sandbox", "must be required, preferred or off", config.sandbox)
    base = None if config.permission_profile is None else load_base_profile(config.permission_profile)
    profile = build_profile(
        config.repo_settings(repo_id), config_dir=config_dir, state_dir=state_dir, base=base
    )
    skill = _plain_option(config.skill, "skill")
    timeout = config.agent.timeout
    if timeout is not None and not _TIMEOUT.fullmatch(timeout):
        raise _refuse("agent.timeout", "must be a plain duration such as 30m", timeout)
    assert skill is not None
    return SessionSettings(
        kind=KIND,
        mode=mode,
        skill=skill,
        sandbox=config.sandbox,
        setting_sources=_check_setting_sources(config.setting_sources),
        env_allowlist=_check_env_names(
            config.env_allowlist,
            (h.token_env for h in config.hosts.values() if h.token_env is not None),
        ),
        model=_plain_option(config.model, "model"),
        timeout=timeout,
        profile=profile,
        warnings=tuple(sandbox_warnings(config.sandbox)),
    )
