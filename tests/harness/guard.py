"""Guard that keeps tests away from real siblings, real git state, forge clients and the network.

It is a Python audit hook (``sys.addaudithook``), so nothing in the standard library is patched.
The hook cannot be removed once installed; it only acts while a ``GuardState`` is active, which the
autouse fixture in ``plugin.py`` sets for every test.

Rules while active:

- a sibling program (forgectl, agent-spawn, pr-comment, wtctl, jobsctl, claude-session) may only
  run from a directory of fakes the test installed;
- ``git`` may only run with a private HOME and no global or system configuration;
- forge, terminal and network clients (gh, glab, claude, termctl, tmux, ssh, curl, wget) never run;
- ``os.system`` never runs;
- sockets connect only to unix paths and the loopback interface.
"""

from __future__ import annotations

import os
import shutil
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


class GuardViolation(RuntimeError):
    """A test tried to run something the guard forbids."""


@dataclass
class GuardState:
    fake_dirs: list[Path]
    home_root: Path
    seen: list[str] = field(default_factory=list)


_state: GuardState | None = None
_installed = False


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


def check_exec(executable: Any, argv: Sequence[Any] | None, env: Mapping[Any, Any] | None) -> None:
    state = _state
    if state is None:
        return
    exe = _text(executable) if executable is not None else ""
    arg0 = _text(argv[0]) if argv else exe
    env_map = _env_map(env)
    names = {Path(exe).name, Path(arg0).name} - {""}
    state.seen.append(" ".join(sorted(names)))
    if names & NEVER:
        raise GuardViolation(f"tests never run {sorted(names & NEVER)}")
    siblings = names & SIBLINGS
    if siblings:
        resolved = _resolve(exe or arg0, env_map)
        allowed = [d.resolve() for d in state.fake_dirs]
        if resolved is None or resolved.parent.resolve() not in allowed:
            raise GuardViolation(
                f"{sorted(siblings)} resolved to {resolved}: only the installed fakes may run"
            )
    if "git" in names:
        _check_git_env(state, env_map)


def _check_git_env(state: GuardState, env: dict[str, str]) -> None:
    home = env.get("HOME", "")
    if not home or not Path(home).resolve().is_relative_to(state.home_root.resolve()):
        raise GuardViolation("git needs HOME inside the test's private directory")
    global_cfg = env.get("GIT_CONFIG_GLOBAL")
    if global_cfg is None or not (
        global_cfg == os.devnull
        or Path(global_cfg).resolve().is_relative_to(state.home_root.resolve())
    ):
        raise GuardViolation("git needs GIT_CONFIG_GLOBAL set to /dev/null or a private file")
    if env.get("GIT_CONFIG_NOSYSTEM") != "1" and env.get("GIT_CONFIG_SYSTEM") != os.devnull:
        raise GuardViolation("git needs GIT_CONFIG_NOSYSTEM=1 or GIT_CONFIG_SYSTEM=/dev/null")


def _check_address(address: Any) -> None:
    if isinstance(address, str | bytes):  # a unix socket path
        return
    host = address[0] if isinstance(address, tuple) and address else ""
    if _text(host) not in LOOPBACK:
        raise GuardViolation(f"tests never open network connections (to {_text(host)})")


def _hook(event: str, args: tuple[Any, ...]) -> None:
    if _state is None:
        return
    if event == "subprocess.Popen":
        check_exec(args[0], args[1], args[3])
    elif event in ("os.exec", "os.posix_spawn"):
        check_exec(args[0], args[1], args[2])
    elif event == "os.spawn":
        check_exec(args[1], args[2], args[3])
    elif event == "os.system":
        raise GuardViolation("tests never call os.system")
    elif event == "socket.connect":
        _check_address(args[1])
    elif event == "socket.getaddrinfo":
        if _text(args[0]) not in LOOPBACK:
            raise GuardViolation(f"tests never resolve host names ({_text(args[0])})")


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
