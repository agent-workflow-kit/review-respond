"""agent-spawn and wtctl answers for the lineage tests: the agent object of an original agent (one
the PR's author launched), the ``show`` envelope and a wtctl worktree record. Everything is
validated against the pinned schemas when a test scripts it (``Fakes.script``)."""

from __future__ import annotations

from typing import Any

from tests.harness import builders
from tests.harness.spawn_run import TS, agent_doc

REPO = builders.REPO
OWNER_LABEL = "ticket-work"


def original(agent_id: str, pr: str, **over: Any) -> dict[str, Any]:
    """An agent the PR's author launched in their own worktree on the PR's branch."""
    fields: dict[str, Any] = {
        "agent_id": agent_id,
        "kind": "work",
        "run_kind": "work",
        "forked_from": None,
        "cwd": "/work/feat-x",
        "labels": [f"pr={pr}", f"owner={OWNER_LABEL}", f"repo={REPO}"],
        "worktree": {"record_id": "wt-aaaaaaaa", "path": "/work/feat-x", "owned": True},
        "created": TS,
        "repo": REPO,
    }
    fields.update(over)
    return agent_doc(**fields)


def show_doc(agent: dict[str, Any]) -> dict[str, Any]:
    """``agent-spawn show --json`` around an agent object."""
    return {
        "agent": agent,
        "runs": [],
        "session": None,
        "supervisor": None,
        "third_party_run": False,
        "repo_config_changed": None,
        "warnings": [],
    }


def list_doc(*agents: dict[str, Any]) -> dict[str, Any]:
    return {"agents": list(agents)}


def wt_record(record_id: str, branch: str | None, repo: str = REPO) -> dict[str, Any]:
    """An active wtctl worktree record on ``branch``."""
    return {
        "id": record_id, "object_type": "worktree", "owned": True, "state": "active",
        "repo": repo, "clone_path": "/clones/repo", "common_dir": "/clones/repo/.git",
        "admin_dir": f"/clones/repo/.git/worktrees/{record_id}", "common_id": "1:2",
        "dir_id": "1:3", "base_dir": "/work", "root_id": None, "path": "/work/feat-x",
        "name": "feat-x", "branch": branch, "created_branch": True, "base": "refs/heads/main",
        "base_sha": "a" * 40, "head_at_create": "a" * 40, "ref": None, "source": None,
        "depth": None, "fetch_url": "https://git.example.test/org/repo.git", "remote": "origin",
        "labels": [], "holders": [], "sparse": None, "baseline_digest": "b" * 64,
        "bootstrap": {"state": "none", "steps": [], "created_paths_count": 0},
        "created": TS, "updated": TS, "missing_since": None, "removed_at": None,
        "last_error": None,
    }  # fmt: skip
