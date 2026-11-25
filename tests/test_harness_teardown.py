"""What the harness asserts at the end of a test, checked in a child pytest run.

The child is a separate interpreter (the guard is per-process), started in a scratch directory with
this repository on its path.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from .harness.plugin import World

REPO = Path(__file__).resolve().parent.parent

CONFTEST = 'pytest_plugins = ["tests.harness.plugin"]\n'


def run_child(world: World, source: str) -> subprocess.CompletedProcess[str]:
    work = world.root / "child"
    work.mkdir()
    (work / "conftest.py").write_text(CONFTEST)
    (work / "test_child.py").write_text(source)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(world.home),
        "PYTHONPATH": str(REPO),
        "LC_ALL": "C",
    }
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_child.py"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_a_swallowed_guard_violation_fails_the_test_at_teardown(world: World) -> None:
    result = run_child(
        world,
        "import subprocess\n"
        "def test_swallows():\n"
        "    try:\n"
        "        subprocess.run(['gh', '--version'])\n"
        "    except Exception:\n"
        "        pass\n",
    )
    assert result.returncode != 0, result.stdout
    assert "guard fired" in result.stdout


def test_the_provokes_guard_marker_allows_a_provoked_violation(world: World) -> None:
    result = run_child(
        world,
        "import subprocess, pytest\n"
        "from tests.harness.guard import GuardViolation\n"
        "@pytest.mark.provokes_guard\n"
        "def test_provokes():\n"
        "    with pytest.raises(GuardViolation):\n"
        "        subprocess.run(['gh', '--version'])\n",
    )
    assert result.returncode == 0, result.stdout


def test_a_scripted_answer_nobody_consumed_fails_the_test_at_teardown(world: World) -> None:
    result = run_child(
        world,
        "def test_unused(fakes):\n    fakes.script('forgectl', 'show', json={}, schema=False)\n",
    )
    assert result.returncode != 0, result.stdout
    assert "nobody asked for" in result.stdout


def test_the_fakes_may_remain_marker_allows_leftovers(world: World) -> None:
    result = run_child(
        world,
        "import pytest\n"
        "@pytest.mark.fakes_may_remain\n"
        "def test_unused(fakes):\n"
        "    fakes.script('forgectl', 'show', json={}, schema=False)\n",
    )
    assert result.returncode == 0, result.stdout


def test_script_checks_its_target_at_script_time(world: World) -> None:
    fakes = world.fakes
    with pytest.raises(ValueError, match="no command"):
        fakes.script("forgectl", "shwo", json={}, schema=False)
    with pytest.raises(ValueError, match="unknown match keys"):
        fakes.script("forgectl", "show", json={}, schema=False, match={"flag": {}})
    with pytest.raises(ValueError, match="no positional"):
        fakes.script("forgectl", "show", json={}, schema=False, match={"positionals": {"prr": "x"}})
    with pytest.raises(ValueError, match="no flag"):
        fakes.script("forgectl", "show", json={}, schema=False, match={"flags": {"--nope": 1}})
    assert fakes.remaining() == []


def test_script_with_stdout_needs_an_explicit_schema_choice(world: World) -> None:
    with pytest.raises(ValueError, match="schema="):
        world.fakes.script("forgectl", "show", stdout="not json")
    world.fakes.script("forgectl", "show", stdout="not json", schema=False)
    world.fakes.take_violations()
    assert os.path.exists(world.fakes.state_dir / "script.json")
    world.fakes._save({"entries": []})  # noqa: SLF001 - nothing consumes it in this test
