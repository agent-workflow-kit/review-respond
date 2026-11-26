"""Pytest plugin: isolation guard, fakes, process cleanup and golden-file fixtures."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import pytest

from review_respond.gateway import Gateway

from . import golden as golden_mod
from . import guard
from .clock import FakeClock
from .fakes import Fakes, private_env
from .procs import ProcRegistry

# Host variables that must never reach a test (credentials, git state, tool configuration).
SCRUBBED_PREFIXES = (
    "GIT_",
    "REVIEW_RESPOND_",
    "FORGECTL_",
    "AGENT_SPAWN_",
    "PR_COMMENT_",
    "WTCTL_",
)
SCRUBBED_NAMES = (
    "GH_TOKEN", "GITHUB_TOKEN", "GITLAB_TOKEN", "GLAB_TOKEN", "SSH_AUTH_SOCK", "ANTHROPIC_API_KEY",
    "TMUX", "EDITOR", "VISUAL", "PAGER", "XDG_RUNTIME_DIR",
)  # fmt: skip


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-golden", action="store_true", default=False, help="rewrite golden files"
    )


class World:
    """The private directory, child environment and fakes of one test."""

    def __init__(self, root: Path, fakes: Fakes, procs: ProcRegistry) -> None:
        self.root = root
        self.fakes = fakes
        self.procs = procs
        self.env = private_env(root, fakes)
        # What ``Gateway.start_detached`` was asked to start (no process is started, unless the
        # test carries the ``real_detached`` marker): argv, cwd, the env and the output path.
        self.detached: list[dict[str, object]] = []

    def drop_tool(self, name: str) -> None:
        """Remove a fake sibling from the PATH of this world (jobsctl is optional: without it a
        round's job runs detached)."""
        (self.fakes.bin_dir / name).unlink()

    @property
    def home(self) -> Path:
        return Path(self.env["HOME"])


@pytest.fixture(autouse=True)
def world(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[World]:
    """Isolate every test: private HOME and XDG, fakes first on PATH, the exec guard active."""
    for name in list(os.environ):
        if name.startswith(SCRUBBED_PREFIXES) or name in SCRUBBED_NAMES:
            monkeypatch.delenv(name)
    # Outside the test's own tmp_path, so tests that list tmp_path see only their files.
    root = tmp_path_factory.mktemp("world")
    fakes = Fakes(root)
    procs = ProcRegistry()
    w = World(root, fakes, procs)
    for key, value in w.env.items():
        monkeypatch.setenv(key, value)
    state = guard.activate([fakes.bin_dir], root)
    try:
        yield w
    finally:
        guard.deactivate()
        procs.cleanup()
    fakes.assert_clean()
    if request.node.get_closest_marker("fakes_may_remain") is None:
        left = fakes.remaining()
        assert not left, (
            f"scripted answers nobody asked for: {[(e['tool'], e['command']) for e in left]}"
        )
    # A test that provokes the guard on purpose says so; every other violation fails the test,
    # even when the code under test swallowed the exception.
    if request.node.get_closest_marker("provokes_guard") is None:
        guard.assert_no_violations(state)


@pytest.fixture(autouse=True)
def detached_recorder(
    request: pytest.FixtureRequest, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Gateway.start_detached`` records its call and starts nothing, so a test that makes
    ``check`` place a round never leaves a real process behind. Tests that want the real process
    carry the ``real_detached`` marker."""
    if request.node.get_closest_marker("real_detached") is not None:
        return

    def record(
        self: Gateway,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str],
        out_fd: int,
        env: Mapping[str, str] | None = None,
    ) -> int:
        world.detached.append(
            {
                "argv": list(argv),
                "cwd": os.fspath(cwd),
                "env": dict(self.env if env is None else env),
                "out": os.readlink(f"/proc/self/fd/{out_fd}"),
            }
        )
        return 2**22 + len(world.detached)  # a pid that no test process has

    monkeypatch.setattr(Gateway, "start_detached", record)


@pytest.fixture
def fakes(world: World) -> Fakes:
    return world.fakes


@pytest.fixture
def gateway(world: World) -> Gateway:
    """A gateway whose child environment is the world's, with every spawn recorded."""
    return Gateway(
        world.env, clock=FakeClock(), on_spawn=world.procs.on_spawn, default_timeout=60.0
    )


@pytest.fixture
def procs(world: World) -> ProcRegistry:
    return world.procs


@pytest.fixture
def golden(request: pytest.FixtureRequest):  # type: ignore[no-untyped-def]
    update = bool(request.config.getoption("--update-golden"))

    class Golden:
        def check(self, name: str, actual: object, schema_id: str) -> None:
            golden_mod.check(name, actual, schema_id, update=update)

        def cli(self, name: str, output: object) -> None:
            golden_mod.check_cli(name, output, update=update)

        def hook(self, name: str, payload: object) -> None:
            golden_mod.check_hook(name, payload, update=update)

    return Golden()
