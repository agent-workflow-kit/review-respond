"""The guard keeps tests off real siblings, real git state, forge clients and the network."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .harness import guard
from .harness.guard import GuardViolation
from .harness.plugin import World
from .harness.procs import Launched, ProcRegistry, start_time


def test_a_real_sibling_on_the_path_never_runs(world: World) -> None:
    other = world.root / "elsewhere"
    other.mkdir()
    script = other / "forgectl"
    script.write_text("#!/bin/sh\necho real\n")
    script.chmod(0o755)
    with pytest.raises(GuardViolation, match="only the installed fakes"):
        subprocess.run([str(script)], check=False)
    with pytest.raises(GuardViolation):
        subprocess.run(["forgectl", "--version"], env={"PATH": f"{other}:/usr/bin"}, check=False)


def test_a_sibling_missing_from_the_path_is_a_violation_not_a_filenotfound(world: World) -> None:
    with pytest.raises(GuardViolation):
        subprocess.run(["wtctl", "--version"], env={"PATH": "/usr/bin:/bin"}, check=False)


def test_the_installed_fakes_do_run(world: World) -> None:
    result = subprocess.run(
        [str(world.fakes.bin_dir / "forgectl"), "--version"], capture_output=True, check=True
    )
    assert result.stdout.startswith(b"forgectl ")


@pytest.mark.parametrize("name", ["gh", "glab", "claude", "termctl", "tmux", "ssh", "curl", "wget"])
def test_forge_terminal_and_network_clients_never_run(name: str) -> None:
    with pytest.raises(GuardViolation):
        subprocess.run([name, "--version"], check=False)


def test_git_needs_a_private_home_and_no_global_config(world: World) -> None:
    good = dict(world.env)
    assert subprocess.run(["git", "--version"], env=good, capture_output=True).returncode == 0
    for broken in (
        {k: v for k, v in good.items() if k != "HOME"},
        {**good, "HOME": "/root"},
        {k: v for k, v in good.items() if k != "GIT_CONFIG_GLOBAL"},
        {**good, "GIT_CONFIG_GLOBAL": "/etc/gitconfig"},
        {k: v for k, v in good.items() if k != "GIT_CONFIG_NOSYSTEM"},
    ):
        with pytest.raises(GuardViolation):
            subprocess.run(["git", "--version"], env=broken, check=False)


def test_the_test_process_itself_has_no_git_state(world: World) -> None:
    assert os.environ["HOME"] == str(world.home)
    assert os.environ["GIT_CONFIG_GLOBAL"] == os.devnull
    assert not any(k.startswith("REVIEW_RESPOND_") for k in os.environ)
    assert os.environ["LC_ALL"] == "C"


def test_os_system_and_exec_are_blocked() -> None:
    with pytest.raises(GuardViolation):
        os.system("true")
    with pytest.raises(GuardViolation):
        os.posix_spawn("/usr/bin/gh", ["gh"], {})


def test_non_loopback_network_is_blocked() -> None:
    with socket.socket() as s, pytest.raises(GuardViolation):
        s.connect(("192.0.2.1", 9))
    with pytest.raises(GuardViolation):
        socket.getaddrinfo("example.com", 443)


def test_unix_sockets_and_loopback_resolution_are_allowed(world: World) -> None:
    path = world.root / "s"
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(path))
        server.listen(1)
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(path))
    assert socket.getaddrinfo("localhost", 80)


def test_the_guard_is_inactive_outside_a_test_state() -> None:
    state = guard._state  # noqa: SLF001
    guard.deactivate()
    try:
        guard.check_exec("gh", ["gh"], {})  # no state: nothing to enforce
    finally:
        guard._state = state  # noqa: SLF001


def test_cleanup_kills_exactly_the_recorded_groups(procs: ProcRegistry) -> None:
    mine = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    other = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        procs.on_spawn(mine.pid)
        assert [p.pid for p in procs.alive()] == [mine.pid]
        assert [p.pid for p in procs.cleanup()] == [mine.pid]
        assert mine.wait(10) == -signal.SIGKILL
        time.sleep(0.2)
        assert other.poll() is None  # never recorded, never touched
    finally:
        other.kill()
        other.wait(10)
        mine.kill()


def test_cleanup_skips_a_reused_pid_and_the_runners_own_group(procs: ProcRegistry) -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        assert start_time(child.pid)
        procs.launched.append(
            Launched(pid=child.pid, pgid=child.pid, started="1")
        )  # stale start time
        procs.launched.append(
            Launched(pid=os.getpid(), pgid=os.getpgrp(), started=start_time(os.getpid()))
        )
        assert procs.cleanup() == []
        assert child.poll() is None
    finally:
        child.kill()
        child.wait(10)


def test_start_time_of_a_missing_process_is_none() -> None:
    assert start_time(2**22 + 12345) is None
    assert isinstance(Path("/proc/self/stat").read_text(), str)


def test_the_path_resolves_every_sibling_to_its_fake_only(world: World) -> None:
    import shutil

    for tool in ("forgectl", "agent-spawn", "pr-comment", "wtctl", "jobsctl", "claude-session"):
        assert shutil.which(tool, path=world.env["PATH"]) == str(world.fakes.bin_dir / tool)
