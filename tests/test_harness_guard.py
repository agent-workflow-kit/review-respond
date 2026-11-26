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

pytestmark = pytest.mark.provokes_guard


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


# -- exec bypasses --------------------------------------------------------------------------


def test_a_symlink_to_a_real_sibling_under_another_name_is_blocked(world: World) -> None:
    real = world.root / "elsewhere" / "forgectl"
    real.parent.mkdir()
    real.write_text("#!/bin/sh\necho real\n")
    real.chmod(0o755)
    link = world.root / "innocent"
    link.symlink_to(real)
    with pytest.raises(GuardViolation, match="forgectl"):
        subprocess.run([str(link)], check=False)
    inside = world.fakes.bin_dir / "tool"
    inside.symlink_to(real)  # a link inside the fakes directory does not launder it
    with pytest.raises(GuardViolation, match="forgectl"):
        subprocess.run([str(inside)], check=False)


def test_an_executable_outside_the_allowed_directories_is_blocked(
    world: World, tmp_path: Path
) -> None:
    script = tmp_path / "anything-else"
    script.write_text("#!/bin/sh\ntrue\n")
    script.chmod(0o755)
    with pytest.raises(GuardViolation, match="outside the directories"):
        subprocess.run([str(script)], check=False)


def test_shell_true_cannot_run_a_sibling_or_a_client(world: World) -> None:
    with pytest.raises(GuardViolation):
        subprocess.run("gh pr list", shell=True, check=False)  # noqa: S602
    with pytest.raises(GuardViolation):
        subprocess.run("true; /home/x/bin/forgectl --version", shell=True, check=False)  # noqa: S602
    with pytest.raises(GuardViolation):
        subprocess.run("PATH=/usr/bin forgectl", shell=True, check=False)  # noqa: S602


def test_a_shell_may_run_the_installed_fakes(world: World) -> None:
    result = subprocess.run(
        f"{world.fakes.bin_dir}/forgectl --version",
        shell=True,  # noqa: S602
        capture_output=True,
        check=True,
    )
    assert result.stdout.startswith(b"forgectl ")


def test_env_cannot_launch_a_sibling_or_a_client(world: World) -> None:
    with pytest.raises(GuardViolation):
        subprocess.run(["env", "gh", "--version"], check=False)
    with pytest.raises(GuardViolation):
        subprocess.run(["env", "forgectl", "--version"], env={"PATH": "/usr/bin:/bin"}, check=False)
    with pytest.raises(GuardViolation):
        subprocess.run(["env", "FOO=1", "/usr/local/bin/wtctl"], check=False)
    with pytest.raises(GuardViolation):
        subprocess.run(["bash", "-c", "exec curl x"], check=False)
    for argv in (
        ["env", "-i", "forgectl", "--version"],
        ["env", "PATH=/usr/bin", "forgectl", "--version"],
        ["env", "-u", "PATH", "forgectl", "--version"],
        ["nice", "forgectl", "--version"],
        ["timeout", "5", "wtctl", "--version"],
        ["xargs", "jobsctl"],
    ):
        with pytest.raises(GuardViolation):
            subprocess.run(argv, env=world.env, check=False)


def test_a_program_outside_the_allowlist_is_blocked(world: World, tmp_path: Path) -> None:
    other = tmp_path / "outside"
    other.mkdir()
    prog = other / "ls"
    prog.write_bytes(Path("/bin/ls").read_bytes())
    prog.chmod(0o755)
    with pytest.raises(GuardViolation, match="outside the directories"):
        subprocess.run([str(prog)], check=False)
    for path in ("/etc/hostname", "/proc/self/exe", "/dev/null"):
        assert not any(Path(path).is_relative_to(d) for d in guard.SYSTEM_DIRS)


def test_ctypes_cannot_reach_libc(world: World) -> None:
    import ctypes

    with pytest.raises(GuardViolation):
        ctypes.CDLL(None)
    with pytest.raises(GuardViolation):
        ctypes.CDLL("libc.so.6")


def test_git_refuses_state_that_redirects_it(world: World) -> None:
    good = dict(world.env)
    for extra in (
        {"XDG_CONFIG_HOME": "/root/.config"},
        {"GIT_DIR": "/elsewhere"},
        {"GIT_WORK_TREE": "/elsewhere"},
        {"GIT_EXEC_PATH": "/elsewhere"},
        {"GIT_SSH_COMMAND": "evil"},
        {"GIT_SSH": "evil"},
        {"GIT_ASKPASS": "evil"},
    ):
        with pytest.raises(GuardViolation):
            subprocess.run(["git", "--version"], env={**good, **extra}, check=False)
    unset = {k: v for k, v in good.items() if k != "XDG_CONFIG_HOME"}
    assert subprocess.run(["git", "--version"], env=unset, capture_output=True).returncode == 0


# -- sockets --------------------------------------------------------------------------------


def test_every_socket_entry_point_is_guarded(world: World) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        with pytest.raises(GuardViolation):
            s.sendto(b"x", ("192.0.2.1", 9))
        with pytest.raises(GuardViolation):
            s.sendmsg([b"x"], [], 0, ("192.0.2.1", 9))
        with pytest.raises(GuardViolation):
            s.bind(("0.0.0.0", 0))
        with pytest.raises(GuardViolation):
            s.bind(("192.0.2.1", 0))
    with pytest.raises(GuardViolation):
        socket.gethostbyname("example.com")
    with pytest.raises(GuardViolation):
        socket.gethostbyaddr("192.0.2.1")
    with pytest.raises(GuardViolation):
        socket.getnameinfo(("192.0.2.1", 80), 0)


def test_local_socket_forms_are_allowed(world: World) -> None:
    assert socket.getaddrinfo(None, 80)
    assert socket.gethostbyname("localhost")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        s.sendto(b"x", s.getsockname())
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0) as n:
        n.bind((0, 0))  # integers, not a host: must not raise on type


# -- swallowed violations -------------------------------------------------------------------


def test_a_swallowed_violation_is_still_recorded_and_fails_the_check(world: World) -> None:
    state = guard._state  # noqa: SLF001
    assert state is not None and state.violations == []
    try:
        subprocess.run(["gh", "--version"], check=False)
    except Exception:  # noqa: BLE001 - product code swallowing the guard error
        pass
    assert len(state.violations) == 1
    with pytest.raises(AssertionError, match="swallowed"):
        guard.assert_no_violations(state)
    state.violations.clear()
    guard.assert_no_violations(state)


# -- cleanup of surviving group members -----------------------------------------------------


def process_state(pid: int) -> str | None:
    """State letter from ``/proc/<pid>/stat``, or None once the process is gone.

    Reads once and treats a vanished file as gone: the member can be reaped between any
    existence check and the read, so check-then-read is racy.
    """
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return text.rsplit(")", 1)[1].split()[0]


def test_cleanup_kills_a_member_that_outlived_its_exited_leader(procs: ProcRegistry) -> None:
    leader = subprocess.Popen(
        ["bash", "-c", "sleep 60 >/dev/null 2>&1 & echo $!"],
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    out, _ = leader.communicate(timeout=10)
    member = int(out.split()[0])
    member_started = start_time(member)
    try:
        procs.record(leader.pid)  # recorded after the leader exited: started is None
        assert start_time(member) == member_started and member_started is not None
        assert [p.pid for p in procs.cleanup()] == [leader.pid]
        deadline = time.monotonic() + 5
        while process_state(member) not in (None, "Z") and time.monotonic() < deadline:
            time.sleep(0.05)
        assert process_state(member) in (None, "Z")
    finally:
        if start_time(member) == member_started:
            os.kill(member, signal.SIGKILL)


def test_cleanup_ignores_a_group_with_no_members_left(procs: ProcRegistry) -> None:
    done = subprocess.Popen(["true"], start_new_session=True)
    done.wait(10)
    procs.record(done.pid)
    assert procs.cleanup() == []
