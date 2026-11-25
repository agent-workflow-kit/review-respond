"""Guard that keeps tests away from real siblings, real git state, forge clients and the network.

It is a Python audit hook (``sys.addaudithook``), so nothing in the standard library is patched.
The hook cannot be removed once installed; it only acts while a ``GuardState`` is active, which the
autouse fixture in ``plugin.py`` sets for every test.

Rules while active:

- an executable is resolved (PATH-relative names too, then symlinks) and must lie under an allowed
  directory (the fakes, the interpreter and its prefixes, the test's private root, the standard
  system directories); its resolved name must not be a sibling program (forgectl, agent-spawn,
  pr-comment, wtctl, jobsctl, claude-session) unless it lives in a directory of fakes the test
  installed, nor one of forge, terminal and network clients (gh, glab, claude, termctl, tmux, ssh,
  curl, wget);
- shells and ``env`` (sh, bash, dash, zsh, env, ...) are scanned: every argv token and every word
  of a ``-c`` string is checked for those names, so ``shell=True`` and ``env forgectl`` do not slip
  through;
- ``git`` may only run with a private HOME, no global or system configuration, XDG_CONFIG_HOME
  unset or private, and none of GIT_DIR, GIT_WORK_TREE, GIT_EXEC_PATH, GIT_SSH*, GIT_ASKPASS;
- ``os.system`` never runs and ``ctypes`` cannot load libraries or call functions;
- sockets connect, bind, send and resolve only unix paths and the loopback interface.

Every violation is recorded on the state and raised as ``GuardViolation``. Product code that
swallows the exception (``except Exception``) still fails the test: the plugin asserts at teardown
that no violation was recorded (tests that provoke violations on purpose carry the
``provokes_guard`` marker).

Known limit: audit hooks are per-process. A child interpreter (a python started by the test, a
shell script that runs python) is not guarded; the guard only sees what the test process itself
executes. Keep such children out of tests.
"""

from __future__ import annotations

import ctypes  # noqa: F401  # imported before the hook: its own import loads libc
import ipaddress
import os
import re
import shutil
import socket
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SIBLINGS = frozenset(
    {"forgectl", "agent-spawn", "pr-comment", "wtctl", "jobsctl", "claude-session"}
)
NEVER = frozenset({"gh", "glab", "claude", "termctl", "tmux", "ssh", "curl", "wget"})
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", ""})


SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "fish", "csh", "tcsh", "env", "busybox"})
SYSTEM_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/proc", "/dev")
GIT_FORBIDDEN = ("GIT_DIR", "GIT_WORK_TREE", "GIT_EXEC_PATH", "GIT_ASKPASS")
# ctypes can reach any libc function (system, execv, ...): nothing may load a library or call one.
CTYPES_ALLOWED_LIBRARIES: frozenset[str] = frozenset()
WORD_SPLIT = re.compile(r"[\s;|&()<>`$\"'{}=]+")


class GuardViolation(RuntimeError):
    """A test tried to run something the guard forbids."""


@dataclass
class GuardState:
    fake_dirs: list[Path]
    home_root: Path
    seen: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)


_state: GuardState | None = None
_installed = False


def _violate(state: GuardState, message: str) -> GuardViolation:
    """Record the violation (so swallowing the exception cannot hide it) and return the error."""
    state.violations.append(message)
    return GuardViolation(message)


def assert_no_violations(state: GuardState) -> None:
    """Fail when the guard fired during the test, even if the code under test swallowed it."""
    if state.violations:
        raise AssertionError(
            "the test isolation guard fired (possibly swallowed by product code):\n"
            + "\n".join(state.violations)
        )


def _text(value: Any) -> str:
    return os.fsdecode(value) if isinstance(value, bytes | os.PathLike) else str(value)


def _env_map(env: Mapping[Any, Any] | None) -> dict[str, str]:
    source = os.environ if env is None else env
    return {_text(k): _text(v) for k, v in source.items()}


def _resolve(executable: str, env: dict[str, str]) -> Path | None:
    if "/" in executable:
        return Path(os.path.abspath(executable))
    found = shutil.which(executable, path=env.get("PATH", ""))
    return Path(os.path.abspath(found)) if found else None


def _allowed_roots(state: GuardState) -> list[Path]:
    roots = [d.resolve() for d in state.fake_dirs]
    roots += [
        state.home_root.resolve(),
        Path(sys.prefix).resolve(),
        Path(sys.base_prefix).resolve(),
    ]
    roots += [Path(os.path.realpath(sys.executable)).parent]
    roots += [Path(d) for d in SYSTEM_DIRS]
    return roots


def _in_fakes(state: GuardState, path: Path) -> bool:
    return path.resolve().parent in [d.resolve() for d in state.fake_dirs]


def _forbidden_words(state: GuardState, text: str, env: dict[str, str]) -> set[str]:
    """Sibling and NEVER names a shell would run from ``text`` (a token or a ``-c`` string)."""
    found: set[str] = set()
    for word in WORD_SPLIT.split(text):
        name = Path(word).name
        if name in NEVER:
            found.add(name)
        elif name in SIBLINGS:
            if "/" in word:
                if not _in_fakes(state, Path(os.path.abspath(word))):
                    found.add(name)
            elif "PATH" in text:
                found.add(name)  # the string rewrites the lookup path: cannot vouch for it
            else:
                resolved = _resolve(name, env)
                if resolved is None or not _in_fakes(state, resolved):
                    found.add(name)
    return found


def check_exec(executable: Any, argv: Sequence[Any] | None, env: Mapping[Any, Any] | None) -> None:
    state = _state
    if state is None:
        return
    exe = _text(executable) if executable is not None else ""
    tokens = [_text(a) for a in argv] if argv else []
    arg0 = tokens[0] if tokens else exe
    env_map = _env_map(env)
    resolved = _resolve(exe or arg0, env_map)
    real = Path(os.path.realpath(resolved)) if resolved is not None else None
    names = {Path(exe).name, Path(arg0).name} - {""}
    if real is not None:
        names.add(real.name)
    state.seen.append(" ".join(sorted(names)))
    if names & NEVER:
        raise _violate(state, f"tests never run {sorted(names & NEVER)}")
    siblings = names & SIBLINGS
    if siblings and (resolved is None or real is None or not _in_fakes(state, resolved)):
        raise _violate(
            state, f"{sorted(siblings)} resolved to {resolved}: only the installed fakes may run"
        )
    if siblings and real is not None and real.name in SIBLINGS and not _in_fakes(state, real):
        raise _violate(state, f"{sorted(siblings)} is a link to {real}: only the fakes may run")
    if (
        real is not None
        and real.exists()
        and not any(real.is_relative_to(r) for r in _allowed_roots(state))
    ):
        raise _violate(state, f"{real} is outside the directories tests may execute from")
    if names & SHELLS:
        for token in tokens[1:]:
            bad = _forbidden_words(state, token, env_map)
            if bad:
                raise _violate(state, f"a shell or env would run {sorted(bad)}: {token[:80]!r}")
    if "git" in names:
        _check_git_env(state, env_map)


def _check_git_env(state: GuardState, env: dict[str, str]) -> None:
    root = state.home_root.resolve()
    home = env.get("HOME", "")
    if not home or not Path(home).resolve().is_relative_to(root):
        raise _violate(state, "git needs HOME inside the test's private directory")
    global_cfg = env.get("GIT_CONFIG_GLOBAL")
    if global_cfg is None or not (
        global_cfg == os.devnull or Path(global_cfg).resolve().is_relative_to(root)
    ):
        raise _violate(state, "git needs GIT_CONFIG_GLOBAL set to /dev/null or a private file")
    if env.get("GIT_CONFIG_NOSYSTEM") != "1" and env.get("GIT_CONFIG_SYSTEM") != os.devnull:
        raise _violate(state, "git needs GIT_CONFIG_NOSYSTEM=1 or GIT_CONFIG_SYSTEM=/dev/null")
    xdg = env.get("XDG_CONFIG_HOME")
    if xdg is not None and not Path(xdg).resolve().is_relative_to(root):
        raise _violate(state, "git needs XDG_CONFIG_HOME unset or inside the private directory")
    forbidden = [k for k in env if k in GIT_FORBIDDEN or k.startswith("GIT_SSH")]
    if forbidden:
        raise _violate(state, f"git must not run with {sorted(forbidden)}")


def _is_loopback(host: str) -> bool:
    if host in LOOPBACK:
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


def _check_host(state: GuardState, host: Any, what: str, *, bind: bool = False) -> None:
    if host is None:
        return  # getaddrinfo(None, port): the local host
    text = _text(host)
    if bind and text in ("", "0.0.0.0", "::", "<any>"):
        raise _violate(state, f"tests never listen on every interface ({what})")
    if not _is_loopback(text):
        raise _violate(state, f"tests never open network connections or resolve names ({text})")


def _check_address(state: GuardState, sock: Any, address: Any, *, bind: bool = False) -> None:
    family = getattr(sock, "family", None)
    if family not in (socket.AF_INET, socket.AF_INET6):
        return  # unix, netlink, packet and other local families have no network host
    if isinstance(address, str | bytes) or address is None:
        return
    host = address[0] if isinstance(address, tuple) and address else ""
    _check_host(state, host, "socket", bind=bind)


def _hook(event: str, args: tuple[Any, ...]) -> None:
    state = _state
    if state is None:
        return
    if event == "subprocess.Popen":
        check_exec(args[0], args[1], args[3])
    elif event in ("os.exec", "os.posix_spawn"):
        check_exec(args[0], args[1], args[2])
    elif event == "os.spawn":
        check_exec(args[1], args[2], args[3])
    elif event == "os.system":
        raise _violate(state, "tests never call os.system")
    elif event in ("socket.connect", "socket.sendto", "socket.sendmsg"):
        _check_address(state, args[0], args[1])
    elif event == "socket.bind":
        _check_address(state, args[0], args[1], bind=True)
    elif event == "socket.getaddrinfo":
        _check_host(state, args[0], "getaddrinfo")
    elif event in ("socket.gethostbyname", "socket.gethostbyaddr"):
        _check_host(state, args[0], event)
    elif event == "socket.getnameinfo":
        address = args[0]
        _check_host(state, address[0] if isinstance(address, tuple) and address else "", event)
    elif event == "ctypes.dlopen":
        if args[0] not in CTYPES_ALLOWED_LIBRARIES:
            raise _violate(state, f"tests never load libraries through ctypes ({args[0]!r})")
    elif event == "ctypes.call_function":
        raise _violate(state, "tests never call foreign functions through ctypes")


def install() -> None:
    global _installed
    if not _installed:
        sys.addaudithook(_hook)
        _installed = True


def activate(fake_dirs: list[Path], home_root: Path) -> GuardState:
    global _state
    install()
    _state = GuardState(fake_dirs=fake_dirs, home_root=home_root)
    return _state


def deactivate() -> None:
    global _state
    _state = None
