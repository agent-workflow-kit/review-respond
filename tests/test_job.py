"""The hidden ``_job`` command end to end against the strict sibling fakes (docs/interface.md: Job,
Run outcomes). Every call goes through the gateway; the fakes' recorded calls are the evidence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from review_respond.cli import main
from review_respond.rounds import Rounds
from review_respond.store import Store, open_state_dir, pr_digest, pr_lease_key
from tests.harness import builders
from tests.harness.agents import wt_record
from tests.harness.fakes import Fakes, assert_untrusted_only_in_files_or_stdin
from tests.harness.plugin import World
from tests.harness.spawn_run import (
    AGENT,
    BASE,
    SESSION,
    TS,
    agent_doc,
    error_doc,
    outcome_doc,
    result_doc,
    started_doc,
)
from tests.test_check import (
    HOST,
    PR7,
    one_comment,
    script_comments,
    script_prs,
    write_config,
)

FORK = "ag-fffffff2"
C1 = "gh:rc:c1"
HEAD = "a" * 40


# ---- the scene ---------------------------------------------------------------------------------


@dataclass
class Scene:
    world: World
    config: Path
    rid: str
    root: Path
    state: Path

    @property
    def fakes(self) -> Fakes:
        return self.world.fakes

    def sql(self, statement: str, *args: Any) -> list[tuple[Any, ...]]:
        conn = sqlite3.connect(self.state / "state.db")
        try:
            rows = conn.execute(statement, args).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    def round(self) -> dict[str, Any]:
        conn = sqlite3.connect(self.state / "state.db")
        conn.row_factory = sqlite3.Row
        try:
            return dict(conn.execute("SELECT * FROM rounds WHERE id = ?", (self.rid,)).fetchone())
        finally:
            conn.close()

    def comment_states(self) -> dict[str, str]:
        return dict(self.sql("SELECT comment_id, state FROM comments"))

    def temp_files(self) -> list[str]:
        found: list[str] = []
        for sub in ("prompts", "profiles"):
            d = self.state / sub
            if d.is_dir():
                found += [f"{sub}/{p.name}" for p in d.iterdir()]
        return found

    before: int = 0

    def calls(self, tool: str | None = None, command: str | None = None) -> list[Any]:
        """The calls of the job (the ``check`` that made the round is not counted)."""
        return [
            c
            for c in self.fakes.calls()[self.before :]
            if c.command != "--version"
            and (tool is None or c.tool == tool)
            and (command is None or c.command == command)
        ]

    def job(self, *extra: str) -> int:
        self.before = len(self.fakes.calls())
        return main(["_job", self.rid, "--config", str(self.config), *extra])


def make_round(
    world: World,
    capsys: pytest.CaptureFixture[str],
    *,
    comments: list[dict[str, Any]] | None = None,
    base: str | None = BASE,
    note: bytes | None = None,
    include_existing: bool = False,
    **config: Any,
) -> Scene:
    cfg = write_config(world, **config)
    script_prs(world.fakes, [builders.pr_item(7)])
    world.fakes.script("agent-spawn", "list", json={"agents": []}, schema=False)
    script_comments(world.fakes, PR7, comments if comments is not None else one_comment())
    assert main(["check", "--json", "--config", str(cfg)]) == 0
    capsys.readouterr()
    state = Path(world.env["XDG_STATE_HOME"]) / "review-respond"
    store = Store.open(open_state_dir(state), write=True, close_state=True)
    try:
        lease = store.try_acquire(pr_lease_key(pr_digest(PR7)), heartbeat=None)
        assert lease is not None
        note_sha = None
        if note is not None:
            note_sha = store.source.put_file(pr_digest(PR7), note)  # type: ignore[union-attr]
        row = Rounds(lease).create(
            PR7, base_agent_id=base, note_sha256=note_sha, include_existing=include_existing
        )
        lease.release()
    finally:
        store.close()
    root = world.root / "tw" / "throw1"
    root.mkdir(parents=True)
    root.chmod(0o700)
    return Scene(world, cfg, row.id, root, state)


@pytest.fixture
def scene(world: World, capsys: pytest.CaptureFixture[str]) -> Scene:
    return make_round(world, capsys)


def tw_doc(root: Path) -> dict[str, Any]:
    record = wt_record("wt-throwaa2", None) | {
        "object_type": "throwaway", "owned": None, "common_dir": None, "common_id": None,
        "admin_dir": None, "name": None, "branch": None, "created_branch": None, "base": None,
        "base_sha": None, "head_at_create": None, "remote": None, "baseline_digest": None,
        "bootstrap": None, "base_dir": str(root.parent), "root_id": "1:9", "holders": [],
        "sparse": None, "ref": HEAD, "source": "remote", "depth": 1, "path": str(root),
    }  # fmt: skip
    return {
        "record": record, "branch_action": None, "bootstrap": None, "reused": False,
        "warnings": [], "dry_run": False,
    }  # fmt: skip


def fork_agent(rid: str, **over: Any) -> dict[str, Any]:
    return agent_doc(agent_id=FORK, labels=[f"round={rid}", "owner=review-respond"], **over)


def script_reads(sc: Scene) -> None:
    sc.fakes.script(
        "forgectl", "show", json=builders.forgectl_show(head_sha=HEAD),
        match={"positionals": {"pr": PR7}},
    )  # fmt: skip
    script_comments(sc.fakes, PR7, one_comment())


def script_throwaway(sc: Scene, **kw: Any) -> None:
    sc.fakes.script("wtctl", "create", json=tw_doc(sc.root), **kw)


def resumed(sc: Scene, *, working: bool = True) -> dict[str, Any]:
    doc = started_doc(first_working=working)
    doc["agent"] = fork_agent(sc.rid)
    return doc


def script_resume(sc: Scene, *, working: bool = True, **kw: Any) -> None:
    sc.fakes.script("agent-spawn", "resume", json=resumed(sc, working=working), **kw)


def waited(sc: Scene, outcome: str, **more: Any) -> dict[str, Any]:
    doc = result_doc(outcome_doc(outcome, **more))
    doc["agent"] = fork_agent(sc.rid)
    return doc


def script_wait(sc: Scene, outcome: str = "done", *, exit: int | None = None, **kw: Any) -> None:
    run = outcome_doc(outcome)
    code = run["exit_code"] if exit is None else exit
    sc.fakes.script("agent-spawn", "wait", json=waited(sc, outcome), exit=code, **kw)


def artifact_file(sc: Scene, doc: dict[str, Any] | bytes) -> dict[str, Any]:
    raw = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
    path = sc.world.root / "artifacts" / f"a{len(list((sc.world.root / 'artifacts').glob('*')))}.json" if (sc.world.root / "artifacts").exists() else sc.world.root / "artifacts" / "a0.json"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(raw)
    return {
        "name": "artifact.json", "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
    }  # fmt: skip


MARKER = "review-respond:respond-to-review:v1"


def good_artifact(**over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "skill_marker": MARKER,
        "replies": [{"comment_id": C1, "body": "Renamed."}],
        "changed_files": ["src/app.py"],
        "deleted_files": [],
        "commit_message": "fix: rename",
    }
    return doc | over


def show_after(
    sc: Scene,
    *,
    artifact: dict[str, Any] | None = None,
    implicit: bool = False,
    status: str = "parked",
) -> dict[str, Any]:
    run = outcome_doc("done")
    run["report"]["artifact"] = artifact
    run["report"]["implicit"] = implicit
    return {
        "agent": fork_agent(sc.rid, status=status),
        "runs": [
            {
                "run": 1, "csession_id": SESSION, "run_kind": "respond", "mode": "headless",
                "promoted": False, "auto_resumed": False, "adopted_at": None, "ended_at": TS,
                "first_working_at": TS, "window": None, "outcome": run,
            }
        ],
        "session": None, "supervisor": None, "third_party_run": False,
        "repo_config_changed": None, "warnings": [],
    }  # fmt: skip


def script_show(sc: Scene, doc: dict[str, Any], **kw: Any) -> None:
    sc.fakes.script("agent-spawn", "show", json=doc, **kw)


def happy(sc: Scene, *, artifact: dict[str, Any] | bytes | None = None) -> None:
    """Every call of a round that ends with a valid artifact."""
    (sc.root / "src").mkdir(exist_ok=True)
    (sc.root / "src" / "app.py").write_text("x = 1\n")
    script_reads(sc)
    script_throwaway(sc)
    script_resume(sc)
    script_wait(sc)
    ref = artifact_file(sc, good_artifact() if artifact is None else artifact)
    script_show(sc, show_after(sc, artifact=ref))


# ---- the happy path -------------------------------------------------------------------------


def test_a_done_run_with_a_valid_artifact_stops_at_the_validated_artifact(scene: Scene) -> None:
    happy(scene)
    assert scene.job() == 0
    row = scene.round()
    # TODO(#28)/TODO(#29): the stored diff and the pending record take over from here
    assert (row["state"], row["agent_id"], row["throwaway_id"]) == ("running", FORK, "wt-throwaa2")
    assert row["head_sha"] == HEAD and row["counted"] == 1
    assert json.loads(row["comment_ids"]) == [C1]
    assert scene.comment_states()[C1] == "delivered"
    assert scene.sql("SELECT agent_id, state, source, round FROM forks") == [
        (FORK, "round", BASE, scene.rid)
    ]
    assert scene.temp_files() == []
    names = [(c.tool, c.command) for c in scene.calls()]
    assert names == [
        ("forgectl", "show"),
        ("forgectl", "comments"),
        ("wtctl", "create"),
        ("agent-spawn", "resume"),
        ("agent-spawn", "wait"),
        ("agent-spawn", "show"),
    ]


def test_the_session_flags_labels_and_cwd_of_the_fork(scene: Scene) -> None:
    happy(scene)
    scene.job()
    (create,) = scene.calls("wtctl", "create")
    assert create.flags["--ref"] == HEAD
    assert sorted(create.flags["--label"]) == sorted(
        ["app=review-respond", "role=round", f"round={scene.rid}"]
    )
    (resume,) = scene.calls("agent-spawn", "resume")
    assert resume.positionals["agent_id"] == BASE
    assert resume.flags["--cwd"] == str(scene.root)
    assert resume.flags["--kind"] == "respond" and resume.flags["--mode"] == "headless"
    assert "--wait" not in resume.argv and "--no-implicit-done" in resume.argv
    assert resume.flags["--actor"] == "auto:review-respond"
    assert sorted(resume.flags["--label"]) == sorted(
        [f"pr={PR7}", "owner=review-respond", f"repo={builders.REPO}", f"round={scene.rid}"]
    )
    (wait,) = scene.calls("agent-spawn", "wait")
    assert wait.positionals["agent_id"] == FORK
    assert not any("REVIEW_RESPOND" in k for c in scene.calls() for k in c.env)
