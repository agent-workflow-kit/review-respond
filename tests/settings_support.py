"""Shared constants and helpers of the session-settings tests (never collected as tests)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from review_respond.config import RepoSettings
from review_respond.errors import CliError
from review_respond.settings import build_profile

SCHEMA_ID = "review-respond/permission-profile.json"
HOME = "/home/u"
CFG = f"{HOME}/.config/review-respond"
STATE = f"{HOME}/.local/state/review-respond"
REPO = "git.example.test/team/service"
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def home_env(monkeypatch: pytest.MonkeyPatch) -> str:
    """HOME is a directory that does not exist, so no path below it needs a real file."""
    monkeypatch.setenv("HOME", HOME)
    return HOME


def repo(**kw: Any) -> RepoSettings:
    args: dict[str, Any] = dict(
        settle="5m", max_rounds=3, reviewers=(), respond_fresh=False,
        allow_commands=(), read_dirs=(),
    )  # fmt: skip
    args.update(kw)
    return RepoSettings(**args)


def refused(fn: Any, *a: Any, **kw: Any) -> CliError:
    with pytest.raises(CliError) as info:
        fn(*a, **kw)
    assert info.value.code == "config_invalid"
    return info.value


def prof(allow_commands: Any = (), read_dirs: Any = (), **kw: Any) -> dict[str, Any]:
    kw.setdefault("config_dir", CFG)
    kw.setdefault("state_dir", STATE)
    return build_profile(repo(allow_commands=allow_commands, read_dirs=read_dirs), **kw)
