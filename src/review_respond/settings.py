"""Session settings and the generated permission profile (docs/interface.md: Session settings).

Every round passes the same kinds of safety settings to agent-spawn. This module derives them
from the loaded config and writes the permission profile file; it starts no process and reads
nothing from the round throwaway (a pull request's files are untrusted, so a planted
``.claude/settings.json`` with hooks must never be looked at; ``setting_sources`` is always empty
for the same reason: claude-session refuses any setting source for the respond kind).

The profile holds only configuration values and fixed strings, never text from a pull request.
Its shape is ``schemas/permission-profile.json``; ``validate_profile`` is the same check in code
(the schema validator is a test-only dependency): the rule and path patterns below are the very
patterns of the schema, and ``tests/test_settings_parity.py`` runs both validators over a large
set of mutated documents and fails on any disagreement. The output is canonical JSON, so the same
input gives the same bytes.

Deny by default: the run's permission mode is ``dontAsk`` (claude-session forces it for an
isolated run), so what the allow list does not name is refused. The allow list reads and edits
the throwaway (``${CWD}``, which claude-session expands to the run's cwd) and the repo's
``respond.read_dirs``, and runs ``respond.allow_commands`` and a few read-only git commands.
A fixed deny list always applies, also to a replacement profile. The sandbox has no network.
Bare ``Glob`` and ``Grep`` allows do nothing on Claude Code 2.1.312 and later (there is no
``--tools`` list to open them), so they fail closed; reads are scoped by the ``Read`` rules.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
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
MAX_RULE_CHARS: Final = 2048
MAX_LIST: Final = 64
MAX_ALLOW_RULES: Final = 256
MAX_ARGV: Final = 32
MAX_ARG_CHARS: Final = 256
MAX_COMMAND_CHARS: Final = 1024
MAX_ENV_NAMES: Final = 64
MAX_TIMEOUT_SECONDS: Final = 3_153_600_000

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
    "Bash(memoctl prune:*)",
    "Bash(notice-route send:*)",
    "Bash(notice-route flush:*)",
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

# ---- the shared character and path patterns (also the schema's patterns) ----------------------

# Characters no profile string may hold: C0, DEL and C1 controls, format and bidirectional
# characters, line and paragraph separators, blank fillers, variation selectors, surrogates,
# private-use and the specials block, all in the basic plane. A character outside the basic plane
# is refused too (an engine without the unicode flag sees two surrogates, which are listed).
_BAD_RANGES: Final = (
    r"\u0000-\u001f\u007f-\u009f\u00ad\u034f\u0600-\u0605\u061c\u06dd\u070f\u0890-\u0891\u08e2"
    r"\u115f-\u1160\u17b4-\u17b5\u180b-\u180f\u2000-\u200f\u2028-\u202f\u205f-\u206f\u2800"
    r"\u3164\ud800-\uf8ff\ufe00-\ufe0f\ufeff\uffa0\ufff0-\uffff"
)
# A rule: a tool name, then at most one parenthesized argument with no parenthesis inside. The
# first lookahead rejects a newline (``$`` alone would accept one at the very end).
RULE_PATTERN: Final = r"^(?![\s\S]*\n)[A-Za-z_*][A-Za-z0-9_*]*(?:\([^()" + _BAD_RANGES + r"]*\))?$"
# An absolute or ``~/`` path of one or more segments: none empty, ``.`` or ``..``, no trailing
# slash, no glob, escape or parenthesis character.
PATH_PATTERN: Final = (
    r"^(?![\s\S]*\n)~?(?:/(?!\.{1,2}(?:/|$))[^/*?\[\]{}\\()" + _BAD_RANGES + r"]+)+$"
)
_RULE_RE: Final = re.compile(RULE_PATTERN)
_PATH_RE: Final = re.compile(PATH_PATTERN)

_PATH_BAD_CHARS: Final = frozenset("*?[]{}\\()")
# Credential and configuration locations, as lowercase segment sequences matched anywhere in a
# path; an ancestor of one below the home directory is refused too.
_CREDENTIAL_SEQS: Final = (
    (".ssh",),
    (".aws",),
    (".gnupg",),
    (".kube",),
    (".docker",),
    (".config",),
    (".claude",),
    (".netrc",),
    (".git-credentials",),
    (".npmrc",),
    (".pypirc",),
    (".azure",),
    (".local", "state"),
    (".local", "share", "keyrings"),
)
_SYSTEM_DIRS: Final = ("/proc", "/sys", "/dev")
# System files and directories no read rule may reach (a parent of one is refused too).
_SYSTEM_SECRETS: Final = (
    "/etc/shadow", "/etc/gshadow", "/etc/sudoers", "/etc/sudoers.d", "/etc/ssh",
    "/etc/ssl/private",
)  # fmt: skip
# Directories a throwaway may never be (a descendant of /tmp or /home is fine).
_THROWAWAY_ROOTS: Final = (
    "/etc",
    "/usr",
    "/var",
    "/proc",
    "/sys",
    "/dev",
    "/tmp",
    "/home",
    "/root",
)

# Programs a command pattern may not start with, matched by basename and by the whole string,
# in any case: shells and launchers (they run anything), editors and pagers, tools that run
# other programs, the network, containers, and the tools the profile denies or that act with the
# user's credentials.
_REFUSED_PROGRAMS: Final = frozenset(
    {
        "busybox", "elvish", "nu", "xonsh", "cmd",
        "env", "sudo", "su", "doas", "xargs", "eval", "exec", "nohup", "nice", "ionice",
        "timeout", "time", "watch", "setsid", "command", "builtin", "find",
        "awk", "gawk", "mawk", "nawk", "sed", "tar", "vim", "vi", "ed", "less", "more", "man",
        "expect", "script", "strace", "ltrace", "gdb", "tmux", "screen", "parallel", "flock",
        "stdbuf", "taskset", "unshare", "nsenter", "chroot", "systemd-run", "pkexec", "kill",
        "mount", "rm", "dd", "tee", "cp", "mv", "zip", "docker", "podman",
        "curl", "wget", "nc", "ncat", "socat", "ssh", "scp", "sftp", "rsync", "ftp", "telnet",
        "ping", "dig", "nslookup", "openssl", "lynx", "w3m", "aria2c", "ssh-add",
        "npx", "bunx", "uvx", "pipx", "gh", "glab", "agent-spawn", "memoctl", "notice-route",
        "review-respond", "forgectl", "pr-comment", "wtctl", "jobsctl", "claude-session",
        "claude", "ticketctl", "termctl", "gatectl",
    }
)  # fmt: skip
_SHELL: Final = re.compile(r"[a-z]*sh[0-9.]*")
_INTERPRETER: Final = re.compile(
    r"python[0-9.]*|pypy[0-9.]*|node(?:js)?|ruby|irb|perl|php|lua(?:jit)?|deno|bun|pwsh"
    r"|powershell|tclsh|wish|rscript|java|jshell|julia|ghc|runghc|ocaml|racket|guile|emacs"
)
_BAD_MODULES: Final = frozenset(
    {"timeit", "runpy", "code", "pdb", "http.server", "pip", "ensurepip", "trace", "idlelib"}
    | {"cprofile", "profile"}
)
_LAUNCHERS: Final = frozenset({"npm", "pnpm", "yarn", "uv", "poetry", "cargo", "go"})
_LAUNCHER_WORDS: Final = frozenset({"dlx", "exec", "install", "run"})
_BAD_FLAGS: Final = frozenset({"-c", "-C", "-e", "-E", "-f"})
_BAD_FLAG_PREFIXES: Final = ("--eval", "--exec", "--command", "--print")
_GIT_BAD_PREFIXES: Final = (
    "--output", "--ext-diff", "--textconv", "--contents", "--no-index", "-O", "--exec-path",
    "--upload-pack", "--show-signature",
)  # fmt: skip
_VAR_ASSIGN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_ARG_CHARS: Final = re.compile(r"[A-Za-z0-9._/=+@%,-]+")

_ENV_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_ENV_REFUSED: Final = frozenset(
    {
        "PATH", "HOME", "IFS", "ENV", "BASH_ENV", "SHELLOPTS", "PS4", "NODE_OPTIONS", "NODE_PATH",
        "PERL5LIB", "PERL5OPT", "RUBYOPT", "RUBYLIB", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS",
        "CLASSPATH", "SSH_AUTH_SOCK", "SHELL", "PAGER", "EDITOR", "VISUAL", "TMPDIR", "LESSOPEN",
        "PROMPT_COMMAND", "CDPATH", "MAKEFLAGS", "MAKEFILES", "GOFLAGS", "KUBECONFIG",
    }
)  # fmt: skip
_ENV_REFUSED_PREFIXES: Final = (
    "LD_", "DYLD_", "GIT_", "PYTHON", "REVIEW_RESPOND_", "XDG_", "CLAUDE", "ANTHROPIC",
    "BASH_FUNC_", "GLIBC_", "GCONV_PATH", "NPM_CONFIG_", "PIP_", "UV_", "JAVA", "JDK_", "_JAVA_",
    "PERL", "RUBY", "NODE_", "SSL_CERT_", "SSH_", "SUDO_", "DOCKER_", "AWS_", "GOOGLE_", "AZURE_",
)  # fmt: skip
_ENV_REFUSED_SUFFIXES: Final = ("_PROXY",)
_ENV_SECRET: Final = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|PASS|CREDENTIAL|API_?KEY|PRIVATE_?KEY|KEY|AUTH"
    r"|COOKIE|BEARER|JWT|DSN|CERT|_URL$",
    re.IGNORECASE,
)

_ROUND_ID: Final = re.compile(r"rd-[a-z2-7]{8}")
# The grammar of agent-spawn's --timeout (a plain duration, never ``none``): the number may be
# decimal, the unit is optional (seconds).
_TIMEOUT: Final = re.compile(r"(?=.{1,32}\Z)(?=[0-9.]*[1-9])([0-9]+(?:\.[0-9]+)?)(ms|s|m|h|d)?")
_TIMEOUT_UNIT: Final = {"ms": Decimal("0.001"), "s": 1, "m": 60, "h": 3600, "d": 86400, None: 1}
# agent-spawn's --skill grammar (no ``..``), and its --model grammar (anything not starting with
# a dash; a control or invisible character is refused here too).
_SKILL: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}")
_MODEL: Final = re.compile(r"[^-].{0,127}")

_UNSAFE_SOURCES: Final = (
    "claude-session refuses any setting source for the respond kind (unsafe_setting_sources): "
    "project and local settings come from the checkout and user settings may hold hooks"
)


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
        raise _refuse(key, "has a glob, escape or parenthesis character", shown)
    if value.startswith("~/") and home:
        rest = value[2:]
    elif value.startswith("/") and not value.startswith("//"):
        rest = value[1:]
    else:
        raise _refuse(key, "must be an absolute path" + (" or ~/..." if home else ""), shown)
    if not rest or not _segments_ok(rest):
        raise _refuse(key, "must be normalized (no empty, . or .. part, no trailing slash)", shown)
    if not _PATH_RE.search(value):
        raise _refuse(key, "has a character a profile path cannot hold", shown)
    return value


def _is_inside(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent + "/")


def _real(path: str) -> str:
    """The path with every symlink of its existing part resolved (a missing tail is kept)."""
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return path


def _homes() -> tuple[str, ...]:
    """The home directory as given and resolved."""
    raw = os.path.expanduser("~")
    if not raw.startswith("/"):
        raise _refuse("HOME", "the home directory is unknown")
    raw = os.path.normpath(raw)
    return tuple(dict.fromkeys((raw, _real(raw))))


def _forms(path: str) -> tuple[str, ...]:
    """``path`` as given and resolved, without a duplicate."""
    return tuple(dict.fromkeys((path, _real(path))))


def _credential_hit(segs: Sequence[str]) -> bool:
    for seq in _CREDENTIAL_SEQS:
        n = len(seq)
        if any(tuple(segs[i : i + n]) == seq for i in range(len(segs) - n + 1)):
            return True
    if segs and segs[-1] == ".cargo":
        return True  # its credentials file is inside
    return any(
        seg == ".cargo" and segs[i + 1].startswith("credentials") for i, seg in enumerate(segs[:-1])
    )


def _check_read_dir(
    value: object, key: str, protected: Sequence[str], throwaway: Sequence[str] = ()
) -> str:
    """A ``read_dirs`` entry (an absolute or ``~/`` path) the session may read, or refuse.

    The ``~/`` form is expanded with the real home and the entry is also looked up with
    ``realpath`` when it exists, so a symlink cannot hide a credential directory. Segments match
    in any case. Refused: a credential directory or an ancestor of one below the home, the home
    itself and its ancestors, ``/proc``, ``/sys``, ``/dev``, and any overlap (ancestor or
    descendant) with review-respond's own directories or the round throwaway.
    """
    path = _plain_abs(value, key, home=True)
    homes = _homes()
    cands = {home + path[1:] for home in homes} if path.startswith("~/") else {path}
    for cand in tuple(cands):
        if os.path.lexists(cand):
            cands.add(_real(cand))
    for cand in sorted(cands):
        segs = [s.lower() for s in cand.split("/") if s]
        if _credential_hit(segs):
            raise _refuse(key, "names a credential or configuration directory", path)
        if any(_is_inside(home, cand) for home in homes):
            raise _refuse(key, "is the home directory or one of its parents", path)
        if any(_is_inside(cand, d) for d in _SYSTEM_DIRS):
            raise _refuse(key, "is a system directory", path)
        if any(_is_inside(cand, d) or _is_inside(d, cand) for d in _SYSTEM_SECRETS):
            raise _refuse(key, "reaches a system credential file or directory", path)
        for home in homes:
            if cand.startswith(home + "/"):
                rel = tuple(s.lower() for s in cand[len(home) + 1 :].split("/"))
                if any(len(rel) < len(seq) and rel == seq[: len(rel)] for seq in _CREDENTIAL_SEQS):
                    raise _refuse(key, "holds a credential or configuration directory", path)
        for other in (*protected, *throwaway):
            if _is_inside(cand, other) or _is_inside(other, cand):
                raise _refuse(
                    key,
                    "overlaps the review-respond config or state directory or the throwaway",
                    path,
                )
    return path


def check_throwaway_root(path: str, *, config_dir: str, state_dir: str) -> str:
    """The round throwaway's real path, or ``config_invalid``.

    Refused: a relative, non-normalized or symlinked path (it must already be its own realpath),
    glob, control, invisible and parenthesis characters, anything that is not a directory, a
    directory not owned by the current user or writable by its group or others, the home
    directory and its ancestors, a system directory itself (``/etc``, ``/usr``, ``/var``,
    ``/proc``, ``/sys``, ``/dev``, ``/tmp``, ``/home``, ``/root``), and any overlap with
    review-respond's own config or state directory (as given and resolved). Looks only at the
    path itself (``lstat`` and ``realpath``), never inside it.
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
    if info.st_uid != os.geteuid():
        raise _refuse("throwaway", "is not owned by the current user", root)
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise _refuse("throwaway", "is writable by its group or others", root)
    if any(_is_inside(home, root) for home in _homes()) or root in _THROWAWAY_ROOTS:
        raise _refuse(
            "throwaway", "is the home directory, one of its parents or a system directory", root
        )
    for given in (config_dir, state_dir):
        for other in _forms(_plain_abs(given, "config_dir/state_dir")):
            if _is_inside(root, other) or _is_inside(other, root):
                raise _refuse(
                    "throwaway", "overlaps the review-respond config or state directory", root
                )
    return real


# ---- command patterns and allow rules ------------------------------------------------------


def _check_interpreter(items: Sequence[str], key: str) -> None:
    """An interpreter runs a script file or ``-m <module>``, never code from its arguments."""
    if len(items) < 2 or items[1] == "-":
        raise _refuse(key, "an interpreter needs a script path or -m <module>", items[0])
    if items[1].startswith("-"):
        if items[1] != "-m" or len(items) < 3 or items[2].startswith("-"):
            raise _refuse(key, "an interpreter may only be given a script path or -m <module>")
        module = items[2].lower()
        if any(module == bad or module.startswith(bad + ".") for bad in _BAD_MODULES):
            raise _refuse(key, "this module cannot be allowed", items[2])


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
    whole = items[0].lower()
    if (
        items[0].startswith("-")
        or program in _REFUSED_PROGRAMS
        or whole in _REFUSED_PROGRAMS
        or _SHELL.fullmatch(program)
        or _SHELL.fullmatch(whole)
        or program.startswith("git-")
        or program.endswith(".exe")
    ):
        raise _refuse(key, "this program cannot be allowed", items[0])
    if program == "git":
        if len(items) < 2 or items[1] not in _GIT_READ_ONLY:
            raise _refuse(key, "only read-only git commands can be allowed", items[0])
        if any(item.startswith(_GIT_BAD_PREFIXES) for item in items[2:]):
            raise _refuse(key, "this git option can write or run a program")
    if _INTERPRETER.fullmatch(program):
        _check_interpreter(items, key)
    for item in items:
        if _VAR_ASSIGN.match(item):
            raise _refuse(key, "a VAR=value element cannot be allowed", item)
    # git, make and the launchers take options that run code or change the directory; for any
    # other program (and for the arguments of an interpreter's script or module) only the exact
    # words count. An interpreter's own options come before its script and are refused above.
    broad = program == "git" or program == "make" or program in _LAUNCHERS
    for item in items[1:]:
        if item in _BAD_FLAGS or (
            broad
            and (
                item.lower().startswith(_BAD_FLAG_PREFIXES)
                or (item[:2] in ("-C", "-f") and not item.startswith("--"))
            )
        ):
            raise _refuse(
                key, "an option that runs code or changes the directory cannot be allowed", item
            )
        if program in _LAUNCHERS and item.lower() in _LAUNCHER_WORDS:
            raise _refuse(key, "a package runner subcommand cannot be allowed", item)
    if len(" ".join(items)) > MAX_COMMAND_CHARS:
        raise _refuse(key, f"a command pattern is longer than {MAX_COMMAND_CHARS} characters")
    return tuple(items)


def _cwd_rule_ok(arg: str, *, write: bool) -> bool:
    """``/${CWD}/**`` or ``/${CWD}/<plain segments>/**``; a write rule never reaches ``.git`` or
    ``.claude`` (the throwaway's git and settings directories)."""
    prefix = "/${CWD}"
    if arg == prefix + "/**":
        return True
    if not arg.startswith(prefix + "/") or not arg.endswith("/**") or len(arg) <= len(prefix) + 4:
        return False
    for seg in arg[len(prefix) + 1 : -3].split("/"):
        if (
            seg in ("", ".", "..")
            or any(ch in _PATH_BAD_CHARS for ch in seg)
            or sanitize(seg) != seg
        ):
            return False
        if write and seg.lower() in (".git", ".claude"):
            return False
    return True


def _abs_read_path(arg: str) -> str | None:
    """The path of ``//abs/path/**`` or ``~/path/**`` (``None`` for any other shape)."""
    if not arg.endswith("/**"):
        return None
    body = arg[:-3]
    if body.startswith("//"):
        return body[1:]
    if body.startswith("~/"):
        return body
    return None


def check_allow_rule(
    rule: object, *, protected: Sequence[str] = (), throwaway: Sequence[str] = ()
) -> str:
    """An allow rule is one of the forms the profile may grant, or ``config_invalid``.

    A ``Read`` rule of an absolute or ``~/`` path gets every ``read_dirs`` check.
    """
    key = "permission_profile"
    if not isinstance(rule, str) or sanitize(rule) != rule:
        raise _refuse(key, "an allow rule has control or invisible characters", rule)
    m = re.fullmatch(r"([A-Za-z]+)(?:\((.*)\))?", rule, re.DOTALL)
    if m is None:
        raise _refuse(key, "an allow rule is not a tool name or Tool(pattern)", rule)
    tool, arg = m.group(1), m.group(2)
    if arg is not None and ("(" in arg or ")" in arg):
        raise _refuse(key, "an allow rule argument has a parenthesis", rule)
    ok = False
    if tool in ("Glob", "Grep"):
        ok = arg is None
    elif tool == "Skill":
        ok = arg is None or (bool(_SKILL.fullmatch(arg)) and ".." not in arg)
    elif tool == "Bash":
        if arg is not None and arg.endswith(":*") and not arg.startswith((":", "*")):
            _check_argv(arg[:-2].split(" "), key)
            ok = True
    elif tool == "Read":
        if arg is not None:
            path = _abs_read_path(arg)
            if path is not None:
                _check_read_dir(path, key, protected, throwaway)
                ok = True
            else:
                ok = _cwd_rule_ok(arg, write=False)
    elif tool in ("Edit", "Write"):
        ok = arg is not None and _cwd_rule_ok(arg, write=True)
    if not ok:
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


def _read_base(
    base: object, protected: Sequence[str], throwaway: Sequence[str]
) -> tuple[list[str], list[str], list[str]]:
    """(allow, deny, allowRead) of a replacement profile; only these keys are accepted."""
    doc = _dict(base, "permission_profile")
    if not set(doc) <= {"permissions", "sandbox"}:
        raise _refuse("permission_profile", "only the permissions and sandbox keys are allowed")
    perms = _dict(doc.get("permissions", {}), "permission_profile.permissions")
    if not set(perms) <= {"allow", "deny"}:
        raise _refuse("permission_profile.permissions", "only allow and deny are allowed")
    allow = [
        check_allow_rule(r, protected=protected, throwaway=throwaway)
        for r in _strings(perms.get("allow", []), "permissions.allow")
    ]
    deny = _strings(perms.get("deny", []), "permissions.deny")
    box = _dict(doc.get("sandbox", {}), "permission_profile.sandbox")
    if not set(box) <= {"filesystem"}:
        raise _refuse("permission_profile.sandbox", "only filesystem.allowRead is allowed")
    fs = _dict(box.get("filesystem", {}), "permission_profile.sandbox.filesystem")
    if not set(fs) <= {"allowRead"}:
        raise _refuse("permission_profile.sandbox.filesystem", "only allowRead is allowed")
    reads = [
        _check_read_dir(p, "permission_profile.sandbox.filesystem.allowRead", protected, throwaway)
        for p in _strings(fs.get("allowRead", []), "allowRead")
    ]
    return allow, deny, reads


def _own_dirs(config_dir: str, state_dir: str) -> tuple[str, ...]:
    """The config and state directories as given and resolved (symlinks followed), deduplicated:
    every form is denied, so a symlinked directory is closed under both names."""
    cfg = _plain_abs(config_dir, "config_dir")
    state = _plain_abs(state_dir, "state_dir")
    forms = [*_forms(cfg), *_forms(state)]
    return tuple(_plain_abs(f, "config_dir/state_dir") for f in dict.fromkeys(forms))


def _throwaway_forms(throwaway: str | None) -> tuple[str, ...]:
    return () if throwaway is None else _forms(_plain_abs(throwaway, "throwaway"))


def build_profile(
    repo: RepoSettings,
    *,
    config_dir: str,
    state_dir: str,
    base: object = None,
    throwaway: str | None = None,
) -> dict[str, Any]:
    """The permission profile of one round (a JSON-ready document, validated).

    ``base`` is the parsed replacement profile of the ``permission_profile`` key (``None``: the
    bundled one). ``config_dir`` and ``state_dir`` are review-respond's own directories, denied
    for reading and writing under the name given and the resolved name. ``throwaway`` (when
    known) may not overlap any ``read_dirs`` entry or replacement-profile read rule.
    """
    own = _own_dirs(config_dir, state_dir)
    tw = _throwaway_forms(throwaway)
    if len(repo.allow_commands) > MAX_LIST:
        raise _refuse("respond.allow_commands", f"more than {MAX_LIST} patterns")
    if len(repo.read_dirs) > MAX_LIST:
        raise _refuse("respond.read_dirs", f"more than {MAX_LIST} entries")
    if base is None:
        allow, deny, reads = list(_BUNDLED_ALLOW), [], []
    else:
        allow, deny, reads = _read_base(base, own, tw)
    for argv in repo.allow_commands:
        items = _check_argv(argv, "respond.allow_commands")
        allow.append(f"Bash({' '.join(items)}:*)")
    for entry in repo.read_dirs:
        path = _check_read_dir(entry, "respond.read_dirs", own, tw)
        reads.append(path)
        allow.append(f"Read(/{path}/**)" if path.startswith("/") else f"Read({path}/**)")
    if not allow:
        raise _refuse("permission_profile", "no allow rule: the session could do nothing")
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
    """``schemas/permission-profile.json`` in code; ``config_invalid`` on any difference.

    The rule and path checks use the schema's own patterns (``RULE_PATTERN``, ``PATH_PATTERN``).
    """
    top = _dict(doc, "profile")
    if set(top) != {"permissions", "sandbox"}:
        raise _refuse("profile", "must hold exactly permissions and sandbox")
    perms = _dict(top["permissions"], "profile.permissions")
    if set(perms) != {"allow", "deny"}:
        raise _refuse("profile.permissions", "must hold exactly allow and deny")
    for name, min_items in (("allow", 1), ("deny", 0)):
        rules = _list(
            perms[name],
            f"profile.permissions.{name}",
            max_items=MAX_ALLOW_RULES,
            min_items=min_items,
        )
        if len(set(map(str, rules))) != len(rules):
            raise _refuse(f"profile.permissions.{name}", "has a duplicate")
        for rule in rules:
            if (
                not isinstance(rule, str)
                or not 0 < len(rule) <= MAX_RULE_CHARS
                or not _RULE_RE.search(rule)
            ):
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
        paths = _list(
            fs[name], f"profile.sandbox.filesystem.{name}", max_items=MAX_LIST, min_items=min_items
        )
        if len(set(map(str, paths))) != len(paths):
            raise _refuse(f"profile.sandbox.filesystem.{name}", "has a duplicate")
        for p in paths:
            if (
                not isinstance(p, str)
                or not 2 <= len(p) <= MAX_PATH_CHARS
                or not _PATH_RE.search(p)
            ):
                raise _refuse(f"profile.sandbox.filesystem.{name}", "has an invalid path", p)
    net = _dict(box["network"], "profile.sandbox.network")
    if net != {"allowedDomains": []}:
        raise _refuse("profile.sandbox.network", "the respond session has no network")


def check_profile_document(doc: object, *, config_dir: str, state_dir: str) -> None:
    """Beyond ``validate_profile`` (the schema): a document about to be written must also hold
    only allow rules the profile may grant and deny the resolved config and state directories.
    A tampered document is refused."""
    validate_profile(doc)
    top = _dict(doc, "profile")
    perms = _dict(top["permissions"], "profile.permissions")
    fs = _dict(_dict(top["sandbox"], "profile.sandbox")["filesystem"], "profile.sandbox.filesystem")
    own = _own_dirs(config_dir, state_dir)
    for rule in perms["allow"]:
        check_allow_rule(rule, protected=own)
    for tool in ("Read", "Edit", "Write"):
        for d in own:
            if f"{tool}(/{d}/**)" not in perms["deny"]:
                raise _refuse("profile.permissions.deny", f"lacks the {tool} deny of an own dir", d)
    for name in ("denyRead", "denyWrite"):
        for d in own:
            if d not in fs[name]:
                raise _refuse(f"profile.sandbox.filesystem.{name}", "lacks an own dir", d)


def render_profile(doc: Mapping[str, Any]) -> bytes:
    """Canonical JSON: sorted keys, two-space indent, ASCII, one trailing newline."""
    return (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


def _profile_name(round_id: str) -> str:
    if not _ROUND_ID.fullmatch(round_id):
        raise ValueError("not a round id")
    return round_id + ".json"


def _unsafe(exc: OSError) -> CliError:
    return CliError(
        "state_unsafe", f"the profile file or its directory is not usable ({exc.strerror})"
    )


def write_profile_file(
    state: StateDir, round_id: str, doc: Mapping[str, Any], *, config_dir: str
) -> Path:
    """Validate ``doc`` (``check_profile_document``) and write it to
    ``<state>/profiles/<round id>.json`` (directory 0700, file 0600, created exclusively, never
    through a symlink). The path goes to agent-spawn's ``--permission-profile``. A directory or
    a permission problem in the way is ``state_unsafe``."""
    name = _profile_name(round_id)
    check_profile_document(doc, config_dir=config_dir, state_dir=os.fspath(state.path))
    try:
        return state.write_private_file(PROFILE_DIR, name, render_profile(doc))
    except (IsADirectoryError, NotADirectoryError, PermissionError) as exc:
        raise _unsafe(exc) from None


def remove_profile_file(state: StateDir, round_id: str) -> bool:
    """Delete the round's profile file; ``False`` when there is none."""
    name = _profile_name(round_id)
    try:
        return state.remove_private_file(PROFILE_DIR, name)
    except (IsADirectoryError, NotADirectoryError, PermissionError) as exc:
        raise _unsafe(exc) from None


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
    tokens = {t.upper() for t in token_envs}
    for name in names:
        if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise _refuse("env_allowlist", "not a valid variable name", name)
        upper = name.upper()
        if (
            upper in _ENV_REFUSED
            or upper in tokens
            or upper.startswith(_ENV_REFUSED_PREFIXES)
            or upper.endswith(_ENV_REFUSED_SUFFIXES)
            or _ENV_SECRET.search(name)
        ):
            raise _refuse("env_allowlist", "this variable cannot be passed to the session", name)
    return tuple(sorted(set(names)))


def _check_setting_sources(text: str) -> tuple[str, ...]:
    if text:
        raise _refuse("setting_sources", _UNSAFE_SOURCES, text)
    return ()


def _check_skill(value: str | None) -> str | None:
    if value is None:
        return None
    if not _SKILL.fullmatch(value) or ".." in value:
        raise _refuse("skill", "must match agent-spawn's skill grammar (no .. part)", value)
    return value


def _check_model(value: str | None) -> str | None:
    if value is None:
        return None
    if not _MODEL.fullmatch(value) or sanitize(value) != value:
        raise _refuse("model", "must be plain text that does not start with a dash", value)
    return value


def _check_timeout(value: str | None) -> str | None:
    """A plain duration of 1 second to 100 years (agent-spawn's grammar); never ``none``."""
    if value is None:
        return None
    m = _TIMEOUT.fullmatch(value)
    seconds = None if m is None else Decimal(m.group(1)) * Decimal(_TIMEOUT_UNIT[m.group(2)])
    if seconds is None or not 1 <= seconds <= MAX_TIMEOUT_SECONDS:
        raise _refuse(
            "agent.timeout", "must be a duration of 1 second to 3153600000 seconds", value
        )
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
    throwaway: str | None = None,
) -> SessionSettings:
    """The settings of a round for ``repo_id``; ``config_invalid`` for an unsafe value."""
    if mode not in MODES:
        raise ValueError("mode is headless or interactive")
    if config.sandbox not in ("required", "preferred", "off"):
        raise _refuse("sandbox", "must be required, preferred or off", config.sandbox)
    base = (
        None if config.permission_profile is None else load_base_profile(config.permission_profile)
    )
    profile = build_profile(
        config.repo_settings(repo_id),
        config_dir=config_dir,
        state_dir=state_dir,
        base=base,
        throwaway=throwaway,
    )
    skill = _check_skill(config.skill)
    timeout = _check_timeout(config.agent.timeout)
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
        model=_check_model(config.model),
        timeout=timeout,
        profile=profile,
        warnings=tuple(sandbox_warnings(config.sandbox)),
    )
