"""Process handling of ``Gateway.run``: timeouts, escapes, interruption, bad inputs, git env."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from review_respond import gateway as gateway_mod
from review_respond.gateway import Gateway, GatewayError, SpawnError

from .harness.fakes import Fakes
from .harness.plugin import World
from .harness.procs import start_time


def alive(pid: int) -> bool:
    """True while ``pid`` runs; a zombie that nobody reaped yet counts as gone."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return text.rsplit(")", 1)[1].split()[0] != "Z"


def kill_own(pid: int, started: str | None) -> None:
    """Kill exactly the process recorded earlier (same pid and start time), nothing else."""
    if started is not None and start_time(pid) == started:
        os.kill(pid, signal.SIGKILL)


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway_mod, "_TERM_GRACE", 0.3)
    monkeypatch.setattr(gateway_mod, "_KILL_WAIT", 0.3)


def test_a_member_that_escaped_the_group_cannot_hang_the_call(
    gateway: Gateway, quick: None
) -> None:
    """A grandchild with setsid holds the pipe after its group is killed: return anyway."""
    began = time.monotonic()
    result = gateway.run(
        ["bash", "-c", "setsid sleep 30 & echo $!; sleep 0.2; exit 0"], timeout=0.5
    )
    took = time.monotonic() - began
    escaped = int(result.stdout.split()[0])
    started = start_time(escaped)
    try:
        assert result.timed_out
        assert took < 10, took
    finally:
        kill_own(escaped, started)


def test_members_that_ignore_sigterm_do_not_survive_a_timeout(
    gateway: Gateway, quick: None
) -> None:
    """The leader dies on SIGTERM and the member detached its pipes: SIGKILL still reaches it."""
    result = gateway.run(
        ["bash", "-c", "(trap '' TERM; exec sleep 30) >/dev/null 2>&1 & echo $!; sleep 60"],
        timeout=0.5,
    )
    member = int(result.stdout.split()[0])
    started = start_time(member)
    try:
        assert result.timed_out
        deadline = time.monotonic() + 5
        while alive(member) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not alive(member)
    finally:
        kill_own(member, started)


def test_on_spawn_raising_stops_and_reaps_the_child(world: World) -> None:
    seen: list[int] = []

    def boom(pid: int) -> None:
        seen.append(pid)
        world.procs.record(pid)
        raise RuntimeError("observer failed")

    gw = Gateway(world.env, on_spawn=boom, default_timeout=30)
    with pytest.raises(RuntimeError, match="observer failed"):
        gw.run(["sleep", "30"])
    assert world.procs.alive() == []
    assert start_time(seen[0]) is None  # reaped, not a zombie


def test_an_interrupt_during_communicate_stops_the_group(
    gateway: Gateway, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = subprocess.Popen.communicate
    calls = {"n": 0}

    def interrupted(self: subprocess.Popen[bytes], *args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt
        return real(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "communicate", interrupted)
    with pytest.raises(KeyboardInterrupt):
        gateway.run(["sleep", "30"])
    assert world.procs.launched and world.procs.alive() == []
    assert start_time(world.procs.launched[0].pid) is None


@pytest.mark.parametrize(
    "call",
    [
        lambda g: g.run(["/definitely/not/here"]),
        lambda g: g.run(["sleep", "0"], cwd="/definitely/not/here"),
        lambda g: g.run(["true", "x" * (4 * 1024 * 1024)]),
    ],
    ids=["missing binary", "missing cwd", "argument list too long"],
)
def test_spawn_failures_are_typed(gateway: Gateway, call: object) -> None:
    with pytest.raises(GatewayError):
        call(gateway)  # type: ignore[operator]


def test_a_failed_spawn_is_a_spawn_error(gateway: Gateway) -> None:
    with pytest.raises(SpawnError):
        gateway.run(["/definitely/not/here"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"env": {"A=B": "x"}},
        {"env": {"": "x"}},
        {"env": {"A\x00": "x"}},
        {"env": {"A": "x\x00y"}},
        {"env": {"A": "\udcff"}},
        {"stdin": "bad \ud800 text"},
        {"timeout": 0},
        {"timeout": -1},
        {"timeout": "x"},
        {"timeout": float("nan")},
        {"timeout": True},
    ],
    ids=lambda k: next(iter(k)) + ":" + repr(next(iter(k.values())))[:20],
)
def test_bad_inputs_are_refused_before_anything_starts(
    gateway: Gateway, world: World, kwargs: dict[str, object]
) -> None:
    with pytest.raises(GatewayError):
        gateway.run(["true"], **kwargs)  # type: ignore[arg-type]
    assert world.procs.launched == []


def test_surrogates_and_non_strings_in_argv_are_refused(gateway: Gateway) -> None:
    with pytest.raises(GatewayError):
        gateway.run(["true", "a\udcffb"])
    with pytest.raises(GatewayError):
        gateway.run(["true", 5])  # type: ignore[list-item]
    with pytest.raises(GatewayError):
        gateway.run("true")  # type: ignore[arg-type]


def test_an_empty_argument_is_still_allowed_for_the_env_allowlist_form() -> None:
    from review_respond.gateway import _arg

    assert _arg("", "env name") == ""


def test_a_valid_timeout_and_none_are_accepted(gateway: Gateway) -> None:
    assert gateway.run(["true"], timeout=5).ok
    assert gateway.run(["true"], timeout=None).ok


def test_git_works_when_the_gateway_env_carries_a_config_count(world: World) -> None:
    gw = Gateway(
        {
            **world.env,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_VALUE_0": "evil",
        }
    )
    repo = world.root / "r-count"
    repo.mkdir()
    env = gw.git.environment()
    assert not any(k.startswith("GIT_CONFIG_COUNT") for k in env)
    assert gw.git.run(["init", "-q", "."], cwd=repo).ok
    # The count alone (no key) must not break a plain call either.
    lone = Gateway({**world.env, "GIT_CONFIG_COUNT": "1"})
    assert lone.git.run(["status", "--porcelain"], cwd=repo).ok


def test_git_run_refuses_options_as_the_subcommand(gateway: Gateway) -> None:
    for first in ("-c", "-C", "--exec-path=/tmp"):
        with pytest.raises(GatewayError):
            gateway.git.run([first, "x", "status"])


def test_jobsctl_env_values_travel_on_argv_so_they_must_not_be_secret(
    gateway: Gateway, fakes: Fakes
) -> None:
    fakes.script("jobsctl", "add", json={}, schema=False)
    gateway.jobsctl.add(
        ["/bin/review-respond", "_job"], queue="respond", unique_key="k", cwd="/state",
        env={"XDG_CONFIG_HOME": "/visible/in/ps"}, pass_env=["SECRET_NAME"],
    )  # fmt: skip
    (call,) = fakes.calls("jobsctl")
    assert "XDG_CONFIG_HOME=/visible/in/ps" in call.argv  # a value is on argv: never a secret
    assert "SECRET_NAME" in call.argv and not any("SECRET_NAME=" in t for t in call.argv)


def test_max_output_stops_a_flooding_program(gateway: Gateway) -> None:
    began = time.monotonic()
    result = gateway.run(
        ["bash", "-c", "head -c 5000000 /dev/zero; sleep 30"], timeout=20, max_output=4096
    )
    assert result.overflow and not result.ok
    assert len(result.stdout) <= 4096
    assert time.monotonic() - began < 10


def test_max_output_keeps_a_small_output_whole(gateway: Gateway) -> None:
    result = gateway.run(["bash", "-c", "echo out; echo err >&2"], max_output=4096)
    assert result.ok and (result.stdout, result.stderr) == ("out\n", "err\n")


def test_max_output_still_times_out(gateway: Gateway, quick: None) -> None:
    result = gateway.run(["bash", "-c", "echo hi; sleep 30"], timeout=0.5, max_output=4096)
    assert result.timed_out and not result.ok and result.stdout == "hi\n"


def test_max_output_refuses_stdin(gateway: Gateway) -> None:
    with pytest.raises(GatewayError):
        gateway.run(["cat"], stdin="x", max_output=10)


def test_deeply_nested_json_output_does_not_raise(gateway: Gateway) -> None:
    result = gateway.run(["bash", "-c", "head -c 100000 /dev/zero | tr '\\0' '['"])
    assert result.json is None and result.json_error is not None


FLOODER_IGNORING_TERM = "trap '' TERM; while :; do head -c 1000000 /dev/zero; done"


def test_overflow_kill_never_reads_uncapped(
    gateway: Gateway, quick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("communicate buffers without a cap")

    monkeypatch.setattr(subprocess.Popen, "communicate", forbidden)
    began = time.monotonic()
    result = gateway.run(["bash", "-c", FLOODER_IGNORING_TERM], timeout=20, max_output=4096)
    assert result.overflow and len(result.stdout) <= 4096
    assert time.monotonic() - began < 5  # SIGKILL at once: no SIGTERM grace


def test_timeout_kill_never_reads_uncapped(
    gateway: Gateway, quick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("communicate buffers without a cap")

    monkeypatch.setattr(subprocess.Popen, "communicate", forbidden)
    result = gateway.run(
        ["bash", "-c", "trap '' TERM; echo hi; sleep 30"], timeout=0.5, max_output=4096
    )
    assert result.timed_out and result.stdout == "hi\n"


@pytest.mark.parametrize(
    "script",
    ["echo hi", "echo hi; sleep 30", FLOODER_IGNORING_TERM],
    ids=["clean", "timeout", "overflow"],
)
def test_bounded_run_closes_its_pipes(
    gateway: Gateway, quick: None, monkeypatch: pytest.MonkeyPatch, script: str
) -> None:
    procs: list[subprocess.Popen[bytes]] = []
    real = subprocess.Popen

    class Recording(real):  # type: ignore[type-arg,misc]
        def __init__(self, *a: Any, **k: Any) -> None:
            super().__init__(*a, **k)
            procs.append(self)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "Popen", Recording)
    gateway.run(["bash", "-c", script], timeout=0.5, max_output=4096)
    (proc,) = procs
    assert proc.stdout is not None and proc.stderr is not None
    assert proc.stdout.closed and proc.stderr.closed
