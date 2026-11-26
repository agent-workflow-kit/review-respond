"""The session settings reach agent-spawn exactly as docs/interface.md says (issue #21).

Exact command lines for a fork and a fresh launch built from ``agent_spawn_kwargs()``, the
pinned grammar's rule that ``--kind`` is always passed (spike V-1: a run of another kind is
refused as ``third_party_run``), and two ``live`` regressions of the spike's refusals.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from review_respond.config import Config
from review_respond.gateway import Gateway
from review_respond.settings import SessionSettings, build_session_settings

from .harness import guard
from .harness.fakes import Fakes
from .settings_support import CFG, REPO, STATE, home_env

__all__ = ["home_env"]

AG = "ag-aaaaaaaa"
LABELS = [("pr", "p-1"), ("owner", "review-respond"), ("repo", REPO), ("round", "rd-abcdefgh")]
ACTOR = "auto:review-respond"


def full_settings() -> SessionSettings:
    config = Config.model_validate(
        {
            "skill": "plugin:skill",
            "model": "m1",
            "agent": {"timeout": "30m"},
            "sandbox": "preferred",
            "env_allowlist": ["ZED", "ALPHA"],
        }
    )
    return build_session_settings(config, REPO, config_dir=CFG, state_dir=STATE)


def kwargs(s: SessionSettings) -> dict[str, Any]:
    return s.agent_spawn_kwargs(
        plugin_dir="/plugin", prompt_file="/state/p.md", permission_profile="/state/p.json"
    )


SESSION_FLAGS = [
    "--kind", "respond", "--mode", "headless", "--skill", "plugin:skill",
    "--plugin-dir", "/plugin", "--prompt-file", "/state/p.md",
    "--permission-profile", "/state/p.json", "--sandbox", "preferred",
    "--setting-sources", "", "--env-allowlist-only", "ALPHA", "--env-allowlist-only", "ZED",
    "--model", "m1", "--timeout", "30m", "--no-promote", "--no-implicit-done",
]  # fmt: skip
TAIL = [
    "--label", "pr=p-1", "--label", "owner=review-respond", "--label", f"repo={REPO}",
    "--label", "round=rd-abcdefgh", "--actor", ACTOR, "--json",
]  # fmt: skip


def test_fork_with_every_optional_flag_has_the_documented_command_line(
    gateway: Gateway, fakes: Fakes
) -> None:
    fakes.script("agent-spawn", "resume", json={}, schema=False)
    gateway.agent_spawn.resume_fork(
        AG, cwd="/work/tw", labels=LABELS, actor=ACTOR, **kwargs(full_settings())
    )
    assert fakes.calls("agent-spawn")[0].argv == [
        "resume", AG, "--fork", "--cwd", "/work/tw", *SESSION_FLAGS, *TAIL,
    ]  # fmt: skip


def test_launch_has_the_documented_command_line(gateway: Gateway, fakes: Fakes) -> None:
    fakes.script("agent-spawn", "launch", json={}, schema=False)
    gateway.agent_spawn.launch(
        cwd="/work/tw", repo=REPO, labels=LABELS, actor=ACTOR, **kwargs(full_settings())
    )
    assert fakes.calls("agent-spawn")[0].argv == [
        "launch", "--worktree", "none", "--cwd", "/work/tw", "--repo", REPO,
        *SESSION_FLAGS, *TAIL,
    ]  # fmt: skip


def test_defaults_omit_model_and_timeout_and_pass_one_empty_allowlist(
    gateway: Gateway, fakes: Fakes
) -> None:
    s = build_session_settings(Config.model_validate({}), REPO, config_dir=CFG, state_dir=STATE)
    fakes.script("agent-spawn", "launch", json={}, schema=False)
    gateway.agent_spawn.launch(cwd="/work/tw", repo=REPO, labels=[], actor=ACTOR, **kwargs(s))
    argv = fakes.calls("agent-spawn")[0].argv
    assert "--model" not in argv and "--timeout" not in argv
    assert argv.count("--env-allowlist-only") == 1
    assert argv[argv.index("--env-allowlist-only") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == ""


@pytest.mark.parametrize(
    "argv",
    [
        ["resume", AG, "--fork", "--cwd", "/work/tw", "--json"],
        ["launch", "--worktree", "none", "--cwd", "/work/tw", "--repo", REPO, "--json"],
    ],
)
def test_the_pinned_grammar_requires_kind_on_resume_fork_and_launch(
    gateway: Gateway, fakes: Fakes, argv: list[str]
) -> None:
    result = gateway.run(["agent-spawn", *argv], timeout=20)
    assert result.returncode in (2, 13), result.stderr
    violations = fakes.take_violations()
    assert [v["kind"] for v in violations] == ["usage"], violations
    assert "--kind is required" in violations[0]["message"]


def test_the_fake_accepts_a_fork_with_kind(gateway: Gateway, fakes: Fakes) -> None:
    fakes.script("agent-spawn", "resume", json={}, schema=False)
    gateway.agent_spawn.resume_fork(
        AG, cwd="/work/tw", labels=[], actor=ACTOR, **kwargs(full_settings())
    )
    assert fakes.violations() == []


# ---- live regressions of spike V-1 (need the real tools; never run in CI) ------------------

LIVE_TOOLS = ("agent-spawn", "wtctl", "claude")


def real_run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  (the real tools, by name, live suite only)
        args, cwd=cwd, capture_output=True, text=True, timeout=120, check=False
    )


def dry_run_refusal(cwd: str, *flags: str) -> str | None:
    """The refusal reason ``agent-spawn launch --dry-run`` reports for ``cwd`` (``None``: none)."""
    out = real_run(
        [
            "agent-spawn",
            "launch",
            "--dry-run",
            "--json",
            "--worktree",
            "none",
            "--cwd",
            cwd,
            "--mode",
            "headless",
            "--skill",
            "respond-to-review",
            *flags,
        ]  # fmt: skip
    )
    try:
        doc = json.loads(out.stdout)
    except json.JSONDecodeError:
        pytest.fail(f"agent-spawn printed no JSON: {out.stdout!r} {out.stderr!r}")
    refusal = doc.get("refusal") or doc.get("reason")
    return refusal.get("reason") if isinstance(refusal, dict) else refusal


@pytest.fixture
def live_throwaway(tmp_path: Path) -> Path:
    """A real wtctl throwaway of a tiny local repository (the live tests' only fixture)."""
    for tool in LIVE_TOOLS:
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not installed")
    guard.deactivate()  # live tests talk to the real tools
    src = tmp_path / "src"
    src.mkdir()
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-q",
         "--allow-empty", "-m", "x"],
    ):  # fmt: skip
        real_run(cmd, cwd=src)
    sha = real_run(["git", "rev-parse", "HEAD"], cwd=src).stdout.strip()
    out = real_run(
        [
            "wtctl",
            "create",
            "--throwaway",
            "--repo",
            "git.example.test/live/spike",
            "--ref",
            sha,
            "--source",
            str(src),
            "--json",
        ]  # fmt: skip
    )
    doc = json.loads(out.stdout)
    path = Path(doc["path"])
    (path / "sub").mkdir(exist_ok=True)
    return path


@pytest.mark.live
def test_live_a_run_of_another_kind_in_a_throwaway_is_refused(live_throwaway: Path) -> None:
    """Spike V-1: without ``--kind respond`` the run is ``third_party_run``."""
    assert dry_run_refusal(str(live_throwaway), "--kind", "other") == "third_party_run"
    assert dry_run_refusal(str(live_throwaway), "--kind", "respond") is None


@pytest.mark.live
def test_live_a_subdirectory_cwd_is_refused(live_throwaway: Path) -> None:
    """Spike V-1: the cwd is exactly the throwaway root; a subdirectory is ``third_party_cwd``."""
    assert dry_run_refusal(str(live_throwaway / "sub"), "--kind", "respond") == "third_party_cwd"
