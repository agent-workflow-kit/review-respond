"""The queued reconcile and the finalizer, driven by ``check`` and ``show`` against the strict
sibling fakes (docs/interface.md: Round states, Concurrency). The round and its job are set up the
way the real ones end up (a queued round whose job never ran, a running round whose ``_job`` is
gone); the fakes' recorded calls and the database are the evidence."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from review_respond.cli import main
from review_respond.finalizer import Finalizer
from review_respond.store import Store, open_state_dir, pr_digest, round_lease_key
from tests.harness.builders import pr_ref
from tests.harness.jobsctl import JOB, add_doc, job_doc
from tests.harness.plugin import World
from tests.harness.spawn_run import outcome_doc
from tests.test_check import (
    PR7,
    gold,
    log_events,
    one_comment,
    pr_item,
    script_comments,
    script_prs,
)
from tests.test_job import (
    C1,
    FORK,
    Scene,
    artifact_file,
    fork_agent,
    good_artifact,
    make_round,
    script_reads,
    script_resume,
    script_show,
    script_throwaway,
    script_wait,
    show_after,
    tw_doc,
)

OLD = 1_700_000_000.0
BASE = "ag-bcdefghi"


# ---- the scene ----------------------------------------------------------------------------------


@pytest.fixture
def scene(world: World, capsys: pytest.CaptureFixture[str]) -> Scene:
    return make_round(world, capsys)


def check(sc: Scene, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    """One ``check`` pass: the PR lists and reads as before."""
    script_prs(sc.fakes, [pr_item(7)])
    script_comments(sc.fakes, PR7, one_comment())
    code = main(["check", "--json", "--config", str(sc.config), *argv])
    out, _ = capsys.readouterr()
    return code, json.loads(out) if out.strip() else {}


def calls(sc: Scene, tool: str, command: str | None = None) -> list[Any]:
    return [c for c in sc.fakes.calls(tool, command) if c.command != "--version"]


def age(sc: Scene, seconds: float = 3600) -> None:
    """The round was created ``seconds`` ago (and its detached job, if any, started then)."""
    now = time.time()
    sc.sql(
        "UPDATE rounds SET created_at = ?, detached_started_at = CASE WHEN detached_started_at"
        " IS NULL THEN NULL ELSE ? END",
        now - seconds,
        now - seconds,
    )


def block(sc: Scene) -> Any:
    return sc.sql("SELECT json_extract(block, '$.reason') FROM prs")[0][0]


@contextmanager
def holding(sc: Scene, key: str) -> Iterator[Any]:
    """This test process holds a lease: a live holder, as ``_job`` is while it runs."""
    store = Store.open(open_state_dir(sc.state), write=True, close_state=True)
    try:
        held = store.try_acquire(key, heartbeat=None)
        assert held is not None
        try:
            yield held
        finally:
            held.release()
    finally:
        store.close()


def events(world: World, event: str) -> list[dict[str, Any]]:
    return [e for e in log_events(world) if e["event"] == event]


def warnings(world: World) -> list[str]:
    return [e["code"] for e in events(world, "warning")]


def steps(world: World) -> list[str]:
    return [e["step"] for e in events(world, "finalize")]


# ---- the queued reconcile -----------------------------------------------------------------------


def test_a_queued_round_with_no_job_that_is_young_is_left_alone(
    scene: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    age(scene, 30)
    assert check(scene, capsys)[0] == 0
    assert scene.round()["state"] == "queued"
    assert calls(scene, "jobsctl") == [] and scene.world.detached == []


def test_a_queued_round_with_no_job_is_left_alone_just_inside_the_submit_grace(
    scene: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    age(scene, 110)
    assert check(scene, capsys)[0] == 0
    assert scene.round()["state"] == "queued" and scene.round()["job_id"] is None
    assert calls(scene, "jobsctl") == [] and scene.world.detached == []


def test_a_queued_round_with_no_job_is_submitted_again_just_past_the_submit_grace(
    scene: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    age(scene, 130)
    scene.fakes.script("jobsctl", "add", json=add_doc(created=False))
    assert check(scene, capsys)[0] == 0
    assert scene.round()["job_id"] == JOB


def test_a_queued_round_with_no_job_is_submitted_again_with_jobsctl(
    scene: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    age(scene)
    scene.fakes.script("jobsctl", "add", json=add_doc(created=False))
    assert check(scene, capsys)[0] == 0
    row = scene.round()
    assert (row["state"], row["job_id"]) == ("queued", JOB)
    (add,) = calls(scene, "jobsctl", "add")
    assert add.flags["--unique-key"].startswith("respond:") and add.flags["--unique-key"].endswith(
        ":1"
    )
    assert add.tail[-2:] == ["_job", scene.rid]


def test_a_queued_round_with_no_job_is_started_again_detached(
    scene: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    scene.world.drop_tool("jobsctl")
    age(scene)
    assert check(scene, capsys)[0] == 0
    row = scene.round()
    assert row["state"] == "queued" and row["detached_started_at"] is not None
    (start,) = scene.world.detached
    assert start["argv"][-2:] == ["_job", scene.rid]


def test_a_resubmission_that_fails_leaves_the_round_for_the_next_pass(
    scene: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    age(scene)
    scene.fakes.script(
        "jobsctl", "add", json={"error": {"code": "busy", "message": "busy"}}, exit=12,
        schema=False,
    )  # fmt: skip
    assert check(scene, capsys)[0] == 0
    assert scene.round()["state"] == "queued" and scene.round()["job_id"] is None
    assert "housekeeping_failed" in warnings(scene.world)


@pytest.mark.parametrize("held_status", ["queued", "starting", "running", "held"])
def test_a_jobsctl_job_that_is_not_over_keeps_the_round_queued(
    scene: Scene, capsys: pytest.CaptureFixture[str], held_status: str
) -> None:
    scene.sql("UPDATE rounds SET job_id = ?", JOB)
    scene.fakes.script("jobsctl", "show", json=job_doc(status=held_status, detail=True))
    assert check(scene, capsys)[0] == 0
    assert scene.round()["state"] == "queued" and block(scene) is None
    assert not {"jobsctl_unreadable", "jobsctl_unavailable"} & set(warnings(scene.world))


JOB_ENDS = [
    ("failed", "missing_env"),
    ("failed", "spawn_failed"),
    ("failed", "lost"),
    ("canceled", "canceled"),
    ("done", "exit"),
]


@pytest.mark.parametrize(("status", "reason"), JOB_ENDS)
def test_a_jobsctl_job_that_ended_without_running_the_job_fails_the_round_job_lost(
    scene: Scene, capsys: pytest.CaptureFixture[str], status: str, reason: str, golden: Any
) -> None:
    scene.sql("UPDATE rounds SET job_id = ?", JOB)
    scene.fakes.script("jobsctl", "show", json=job_doc(status=status, reason=reason, detail=True))
    code, doc = check(scene, capsys)
    assert code == 0
    row = scene.round()
    assert (row["state"], row["failure_reason"], row["counted"]) == ("failed", "job_lost", 0)
    assert block(scene) == "failed" and scene.comment_states()[C1] == "unhandled"
    assert steps(scene.world) == ["failed"]
    assert [(e["from"], e["to"], e["reason"]) for e in events(scene.world, "round_state")][-1] == (
        "queued", "failed", "job_lost",
    )  # fmt: skip
    assert len(calls(scene, "jobsctl", "show")) == 1


def test_check_for_one_pr_leaves_the_rounds_and_held_jobs_of_another_alone(
    scene: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    pr_b = pr_ref(8)
    job_b, held_b = "jb-bbbbbbb2", "jb-ccccccc2"
    scene.sql("UPDATE rounds SET job_id = ?", JOB)
    scene.sql(
        "INSERT INTO prs (pr, digest, host, repo, number, host_state, head_sha, head_ref,"
        " head_repo, is_fork, base_ref, created_at, updated_at)"
        " SELECT ?, ?, host, repo, 8, host_state, head_sha, head_ref, head_repo, is_fork,"
        " base_ref, created_at, updated_at FROM prs WHERE pr = ?",
        pr_b, pr_digest(pr_b), PR7,
    )  # fmt: skip
    insert = (
        "INSERT INTO rounds (id, pr, number, state, trigger_kind, job_id, created_at, ended_at)"
        " VALUES (?, ?, ?, ?, 'check', ?, ?, ?)"
    )
    scene.sql(insert, "rd-bbbbbbb2", pr_b, 2, "queued", job_b, OLD, None)
    scene.sql(insert, "rd-bbbbbbb3", pr_b, 1, "escalated", held_b, OLD, time.time())
    scene.fakes.script(
        "jobsctl", "show", json=job_doc(status="failed", reason="lost", detail=True),
        match={"positionals": {"job": JOB}},
    )  # fmt: skip
    script_reads(scene)
    assert main(["check", "--json", "--config", str(scene.config), "--pr", PR7]) == 0
    capsys.readouterr()
    assert scene.round()["failure_reason"] == "job_lost"  # the checked PR was reconciled
    assert scene.sql("SELECT state FROM rounds WHERE id = 'rd-bbbbbbb2'") == [("queued",)]
    assert scene.sql("SELECT state FROM rounds WHERE id = 'rd-bbbbbbb3'") == [("escalated",)]
    shown = [c.positionals["job"] for c in calls(scene, "jobsctl", "show")]
    assert shown == [JOB] and calls(scene, "jobsctl", "resolve") == []


def test_a_canceled_job_of_a_flagged_round_ends_it_canceled_with_the_stored_reason(
    scene: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    scene.sql(
        "UPDATE rounds SET job_id = ?, cancel_requested_at = ?, cancel_reason = 'stopped'", JOB, OLD
    )
    scene.fakes.script("jobsctl", "show", json=job_doc(status="canceled", detail=True))
    assert check(scene, capsys)[0] == 0
    row = scene.round()
    assert (row["state"], row["cancel_reason"], row["failure_reason"]) == (
        "canceled", "stopped", None,
    )  # fmt: skip
    assert block(scene) == "stopped"
    assert steps(scene.world) == ["canceled"]


def test_a_job_that_jobsctl_no_longer_knows_ends_the_round_job_lost(
    scene: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    scene.sql("UPDATE rounds SET job_id = ?", JOB)
    scene.fakes.script(
        "jobsctl", "show", json={"error": {"code": "not_found", "message": "no job"}}, exit=10,
        schema=False,
    )  # fmt: skip
    assert check(scene, capsys)[0] == 0
    assert scene.round()["failure_reason"] == "job_lost"


@pytest.mark.parametrize(
    "answer",
    [
        ({"error": {"code": "internal", "message": "x"}}, 12),
        ({"error": {"code": "busy", "message": "x"}}, 12),
        (["not", "a", "job"], 0),
        ({"id": "jb-zzzzzzz2", "status": "failed"}, 0),
        ({"id": JOB, "status": "mystery"}, 0),
    ],
    ids=["internal", "busy", "not-an-object", "another-job", "unknown-status"],
)
def test_an_answer_jobsctl_cannot_give_decides_nothing(
    scene: Scene, capsys: pytest.CaptureFixture[str], answer: tuple[Any, int]
) -> None:
    scene.sql("UPDATE rounds SET job_id = ?", JOB)
    body, exit_code = answer
    scene.fakes.script("jobsctl", "show", json=body, exit=exit_code, schema=False)
    assert check(scene, capsys)[0] == 0
    assert scene.round()["state"] == "queued" and block(scene) is None
    assert {"jobsctl_unreadable"} & set(warnings(scene.world))


def test_an_oversized_jobsctl_answer_decides_nothing(
    scene: Scene, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import review_respond.gateway as gateway_mod

    monkeypatch.setattr(gateway_mod, "JOBSCTL_OUTPUT_CAP", 2048)
    scene.sql("UPDATE rounds SET job_id = ?", JOB)
    scene.fakes.script("jobsctl", "show", stdout="x" * 20000, exit=10, schema=False)
    assert check(scene, capsys)[0] == 0
    assert scene.round()["state"] == "queued" and block(scene) is None
    assert "jobsctl_unreadable" in warnings(scene.world)


def test_a_stored_job_with_jobsctl_gone_from_the_machine_decides_nothing(
    scene: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    scene.sql("UPDATE rounds SET job_id = ?", JOB)
    scene.world.drop_tool("jobsctl")
    assert check(scene, capsys)[0] == 0
    assert scene.round()["state"] == "queued"
    assert "jobsctl_unavailable" in warnings(scene.world)


def test_a_live_holder_of_the_round_lease_keeps_the_round_queued_whatever_jobsctl_says(
    scene: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    scene.sql("UPDATE rounds SET job_id = ?", JOB)
    with holding(scene, round_lease_key(scene.rid)):
        assert check(scene, capsys)[0] == 0
    assert scene.round()["state"] == "queued" and calls(scene, "jobsctl") == []


def test_a_detached_job_that_never_took_its_lease_is_lost(
    scene: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    scene.world.drop_tool("jobsctl")
    scene.sql("UPDATE rounds SET detached_started_at = 1")
    (scene.state / "jobs").mkdir(mode=0o700)
    out = scene.state / "jobs" / f"{scene.rid}.log"
    out.write_text("traceback")
    out.chmod(0o600)
    assert check(scene, capsys)[0] == 0
    row = scene.round()
    assert (row["state"], row["failure_reason"], block(scene)) == ("failed", "job_lost", "failed")
    assert not out.exists()  # the job's output goes with its round


def test_a_detached_job_that_is_young_is_not_lost_yet(
    scene: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    scene.world.drop_tool("jobsctl")
    scene.sql("UPDATE rounds SET detached_started_at = ?", time.time() - 20)
    assert check(scene, capsys)[0] == 0
    assert scene.round()["state"] == "queued"


def test_a_detached_job_that_holds_its_lease_is_not_lost_however_old(
    scene: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    scene.world.drop_tool("jobsctl")
    scene.sql("UPDATE rounds SET detached_started_at = 1")
    with holding(scene, round_lease_key(scene.rid)):
        assert check(scene, capsys)[0] == 0
    assert scene.round()["state"] == "queued"


def test_a_job_that_takes_its_lease_while_jobsctl_is_asked_keeps_the_round(
    scene: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reconcile takes the round's lease before it writes: a job that holds it wins."""
    scene.sql("UPDATE rounds SET job_id = ?", JOB)
    scene.fakes.script(
        "jobsctl", "show", json=job_doc(status="failed", detail=True),
        sleep=1.0,
    )  # fmt: skip
    got: list[bool] = []
    release = threading.Event()

    def late_job() -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not scene.fakes.calls("jobsctl", "show"):
            time.sleep(0.01)
        store = Store.open(open_state_dir(scene.state), write=True, close_state=True)
        try:
            lease = store.try_acquire(round_lease_key(scene.rid), heartbeat=None)
            got.append(lease is not None)
            release.wait(30)
            if lease is not None:
                lease.release()
        finally:
            store.close()

    t = threading.Thread(target=late_job)
    t.start()
    assert check(scene, capsys)[0] == 0
    release.set()
    t.join(timeout=30)
    assert got == [True]  # the job took the lease while jobsctl was being asked
    assert scene.round()["state"] == "queued" and block(scene) is None


# ---- the finalizer: a running round whose job is gone ------------------------------------------


def left_running(
    world: World, capsys: pytest.CaptureFixture[str], *, working: bool = True, **config: Any
) -> Scene:
    """A round the job left ``running`` for the finalizer: its agent was live when the wait ended
    (the job recorded the agent, released its lease and exited)."""
    sc = make_round(world, capsys, **config)
    script_reads(sc)
    script_throwaway(sc)
    script_resume(sc, working=working)
    script_wait(sc)
    script_show(sc, show_after(sc, status="live"))
    assert sc.job() == 0
    row = sc.round()
    assert (row["state"], row["agent_id"]) == ("running", FORK)
    assert sorted(sc.temp_files()) != []  # the finalizer needs them while the agent lives
    return sc


def final_run(sc: Scene, outcome: str, **more: Any) -> dict[str, Any]:
    """``agent-spawn show`` of the fork after its run ended with ``outcome``."""
    doc = show_after(sc, status="parked")
    if outcome != "done":
        doc["runs"][0]["outcome"] = outcome_doc(outcome, **more)
    return doc


def wt_show(sc: Scene, **over: Any) -> None:
    sc.fakes.script("wtctl", "show", json=tw_doc(sc.root, sc.rid, **over)["record"])


@pytest.fixture
def running(world: World, capsys: pytest.CaptureFixture[str]) -> Scene:
    return left_running(world, capsys)


FINALIZED = [
    # outcome, extra args of the run, round state, failure reason, counted, comment, block
    ("failed", {}, "failed", "agent_failed", 1, "unhandled", "failed"),
    ("killed", {}, "failed", "agent_failed", 1, "unhandled", "failed"),
    ("timeout", {}, "failed", "timeout", 1, "unhandled", "failed"),
    ("lost", {}, "failed", "agent_lost", 1, "unhandled", "failed"),
    ("exited-unreported", {}, "failed", "unreported", 1, "unhandled", "failed"),
    ("blocked", {"reason": "q?", "summary": "what next"}, "escalated", None, 1, "unhandled",
     "escalated"),
]  # fmt: skip


@pytest.mark.parametrize(
    ("outcome", "more", "state", "reason", "counted", "comment", "blocked_by"),
    FINALIZED,
    ids=[f[0] for f in FINALIZED],
)
def test_the_finalizer_maps_the_last_run_like_the_job_does(
    running: Scene,
    capsys: pytest.CaptureFixture[str],
    outcome: str,
    more: dict[str, Any],
    state: str,
    reason: str | None,
    counted: int,
    comment: str,
    blocked_by: str,
) -> None:
    script_show(running, final_run(running, outcome, **more))
    code, _ = check(running, capsys)
    assert code == 0
    row = running.round()
    assert (row["state"], row["failure_reason"], row["counted"]) == (state, reason, counted)
    assert running.comment_states()[C1] == comment and block(running) == blocked_by
    assert running.temp_files() == []  # the prompt and the profile go with the round
    assert steps(running.world)[-1] == state if state != "escalated" else True
    (show,) = calls(running, "agent-spawn", "show")[-1:]
    assert show.positionals["agent_id"] == FORK
    if state == "escalated":
        assert (row["escalation_reason"], row["escalation_summary"]) == ("q?", "what next")


def test_a_done_run_with_a_valid_artifact_is_validated_and_the_round_stays_running(
    running: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    (running.root / "src").mkdir(exist_ok=True)
    (running.root / "src" / "app.py").write_text("x = 1\n")
    ref = artifact_file(running, good_artifact())
    script_show(running, show_after(running, artifact=ref))
    wt_show(running)
    assert check(running, capsys)[0] == 0
    # TODO(#28)/TODO(#29): the stored diff and the pending record take over from here
    assert running.round()["state"] == "running" and running.comment_states()[C1] == "delivered"
    assert running.temp_files() == [] and "artifact_validated" in warnings(running.world)
    assert [c.command for c in calls(running, "wtctl")][-1] == "show"


@pytest.mark.parametrize(
    ("artifact", "reason"),
    [(None, "artifact_missing"), ("tampered", "artifact_invalid")],
    ids=["missing", "tampered"],
)
def test_a_done_run_without_a_usable_artifact_fails_the_round(
    running: Scene, capsys: pytest.CaptureFixture[str], artifact: str | None, reason: str
) -> None:
    ref = None
    if artifact == "tampered":
        ref = artifact_file(running, good_artifact()) | {"sha256": "0" * 64}
    script_show(running, show_after(running, artifact=ref))
    wt_show(running)
    assert check(running, capsys)[0] == 0
    row = running.round()
    assert (row["state"], row["failure_reason"], row["counted"]) == ("failed", reason, 1)
    assert block(running) == "failed"


def test_a_done_run_whose_throwaway_is_gone_fails_the_path_state_check(
    running: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    """No files, no valid artifact: a changed file that is not there fails ``path_state``."""
    ref = artifact_file(running, good_artifact())
    script_show(running, show_after(running, artifact=ref))
    running.fakes.script(
        "wtctl", "show", json={"error": {"code": "not_found", "message": "no", "reason": None,
        "details": None, "lower_error": None}}, exit=10, schema=False,
    )  # fmt: skip
    assert check(running, capsys)[0] == 0
    row = running.round()
    assert (row["state"], row["failure_reason"], row["failure_detail"]) == (
        "failed", "artifact_invalid", "path_state",
    )  # fmt: skip


def test_a_done_run_whose_throwaway_cannot_be_asked_is_decided_next_pass(
    running: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    ref = artifact_file(running, good_artifact())
    script_show(running, show_after(running, artifact=ref))
    running.fakes.script(
        "wtctl", "show", json={"error": {"code": "internal", "message": "x", "reason": None,
        "details": None, "lower_error": None}}, exit=1, schema=False,
    )  # fmt: skip
    assert check(running, capsys)[0] == 0
    assert running.round()["state"] == "running" and running.temp_files() != []
    assert "throwaway_unreadable" in warnings(running.world)


HOSTILE_PATHS = [
    ("/tmp/a\x00b", "nul"),
    ("/tmp/a\ud800b", "surrogate"),
    ("/tmp/a\x01b", "control"),
    ("/tmp/../a", "not-normalized"),
]


@pytest.mark.parametrize("path", [p for p, _ in HOSTILE_PATHS], ids=[i for _, i in HOSTILE_PATHS])
def test_a_done_run_whose_throwaway_path_is_hostile_is_left_for_the_next_pass(
    running: Scene, capsys: pytest.CaptureFixture[str], path: str
) -> None:
    ref = artifact_file(running, good_artifact())
    script_show(running, show_after(running, artifact=ref))
    wt_show(running, path=path)
    code, doc = check(running, capsys)
    assert code == 0 and doc.get("error") is None
    assert running.round()["state"] == "running" and running.temp_files() != []
    assert "throwaway_unreadable" in warnings(running.world)
    assert "housekeeping_failed" not in warnings(running.world)


@pytest.mark.parametrize(
    "path", [p for p, _ in HOSTILE_PATHS[:2]], ids=[i for _, i in HOSTILE_PATHS[:2]]
)
def test_a_fresh_candidate_with_a_hostile_throwaway_path_leaves_the_round_running(
    world: World, capsys: pytest.CaptureFixture[str], path: str
) -> None:
    sc = dead_before_agent(world, capsys)
    sc.sql("UPDATE rounds SET base_agent_id = NULL")
    wt_show(sc, path=path)
    list_answer(sc, candidate(sc, forked_from=None, run_kind="respond", cwd=str(sc.root)))
    code, _ = check(sc, capsys)
    assert code == 0 and sc.round()["state"] == "running"
    assert "throwaway_unreadable" in warnings(world)


def test_an_unexpected_error_of_one_round_does_not_stop_the_pass(
    running: Scene, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise ValueError("embedded null byte")

    monkeypatch.setattr(Finalizer, "finalize", boom)
    code, _ = check(running, capsys)
    assert code == 0
    assert running.round()["state"] == "running"
    assert "housekeeping_failed" in warnings(running.world)


def test_a_live_agent_leaves_the_round_running_with_its_files(
    running: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    script_show(running, show_after(running, status="live"))
    assert check(running, capsys)[0] == 0
    assert running.round()["state"] == "running" and running.temp_files() != []
    assert steps(running.world) == ["left_running"]
    # and again next pass: nothing was decided, nothing was written
    rev = running.round()["rev"]
    script_show(running, show_after(running, status="tearing_down"))
    assert check(running, capsys)[0] == 0 and running.round()["rev"] == rev


def test_an_agent_that_agent_spawn_does_not_know_ends_the_round_agent_lost(
    running: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    running.fakes.script(
        "agent-spawn", "show", json={"error": {"code": "not_found", "message": "no"}}, exit=10,
        schema=False,
    )  # fmt: skip
    assert check(running, capsys)[0] == 0
    row = running.round()
    assert (row["state"], row["failure_reason"], row["counted"]) == ("failed", "agent_lost", 1)
    assert running.sql("SELECT base_agent FROM prs") == [(None,)]  # the base is kept (not set here)


@pytest.mark.parametrize("answer", ["unreadable", "other_agent", "no_json"])
def test_a_show_that_cannot_be_read_decides_nothing(
    running: Scene, capsys: pytest.CaptureFixture[str], answer: str
) -> None:
    if answer == "unreadable":
        running.fakes.script(
            "agent-spawn", "show", json={"error": {"code": "internal", "message": "x"}},
            exit=1, schema=False,
        )  # fmt: skip
    elif answer == "other_agent":
        doc = show_after(running)
        doc["agent"]["agent_id"] = "ag-zzzzzzz2"
        script_show(running, doc)
    else:
        running.fakes.script("agent-spawn", "show", stdout="not json", schema=False)
    assert check(running, capsys)[0] == 0
    assert running.round()["state"] == "running" and running.temp_files() != []
    assert "show_failed" in warnings(running.world)


def test_a_run_that_was_stopped_after_a_stop_request_ends_the_round_canceled(
    running: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    running.sql(
        "UPDATE rounds SET cancel_requested_at = ?, cancel_reason = 'stopped', rev = rev + 1", OLD
    )
    script_show(running, final_run(running, "killed", end_reason="stopped"))
    assert check(running, capsys)[0] == 0
    row = running.round()
    assert (row["state"], row["cancel_reason"], row["counted"]) == ("canceled", "stopped", 1)
    assert block(running) == "stopped" and running.comment_states()[C1] == "unhandled"


def test_a_run_that_never_started_is_not_counted(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    sc = left_running(world, capsys, working=False)
    assert sc.round()["counted"] == 0
    script_show(sc, final_run(sc, "not-started"))
    assert check(sc, capsys)[0] == 0
    assert sc.round()["counted"] == 0 and sc.round()["failure_reason"] == "launch_failed"


# ---- a round whose job died before it recorded the agent ----------------------------------------


def candidate(sc: Scene, agent_id: str = FORK, **over: Any) -> dict[str, Any]:
    return fork_agent(sc.rid) | {"agent_id": agent_id, "forked_from": BASE} | over


def dead_before_agent(world: World, capsys: pytest.CaptureFixture[str], **config: Any) -> Scene:
    """A running round with a throwaway but no agent id: the job died between ``resume`` and the
    record of its answer."""
    sc = make_round(world, capsys, **config)
    script_reads(sc)
    script_throwaway(sc)
    script_resume(sc, working=False)
    script_wait(sc)
    script_show(sc, show_after(sc, status="live"))
    assert sc.job() == 0
    sc.sql("DELETE FROM forks")
    sc.sql("UPDATE rounds SET agent_id = NULL")
    return sc


def list_answer(sc: Scene, *agents: dict[str, Any]) -> None:
    sc.fakes.script("agent-spawn", "list", json={"agents": list(agents)}, schema=False)


def test_a_single_labelled_fork_of_the_base_is_recorded_and_finalized(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    sc = dead_before_agent(world, capsys)
    list_answer(sc, candidate(sc))
    script_show(sc, final_run(sc, "failed"))
    assert check(sc, capsys)[0] == 0
    row = sc.round()
    assert (row["agent_id"], row["state"], row["failure_reason"]) == (
        FORK,
        "failed",
        "agent_failed",
    )
    assert sc.sql("SELECT agent_id, state, source, round FROM forks") == [
        (FORK, "round", BASE, sc.rid)
    ]
    (listed,) = calls(sc, "agent-spawn", "list")[-1:]
    assert sorted(listed.flags["--label"]) == sorted([f"round={sc.rid}", "owner=review-respond"])
    assert steps(world) == ["agent_found", "failed"]


def test_no_candidate_ends_the_round_job_lost(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    sc = dead_before_agent(world, capsys)
    list_answer(sc)
    assert check(sc, capsys)[0] == 0
    row = sc.round()
    assert (row["state"], row["failure_reason"], row["counted"]) == ("failed", "job_lost", 0)
    assert sc.sql("SELECT count(*) FROM forks") == [(0,)] and block(sc) == "failed"
    assert steps(world) == ["agent_missing", "failed"] and sc.temp_files() == []


def test_several_candidates_record_none_and_warn_ambiguous_fork(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    sc = dead_before_agent(world, capsys)
    list_answer(sc, candidate(sc, "ag-fffffff2"), candidate(sc, "ag-fffffff3"))
    assert check(sc, capsys)[0] == 0
    row = sc.round()
    assert (row["state"], row["failure_reason"], row["agent_id"]) == ("failed", "job_lost", None)
    assert sc.sql("SELECT count(*) FROM forks") == [
        (0,)
    ]  # nothing is marked: teardown acts only on rows
    (warn,) = [e for e in events(world, "warning") if e["code"] == "ambiguous_fork"]
    assert warn["round"] == sc.rid and warn["key"] == "ag-fffffff2,ag-fffffff3"
    assert calls(sc, "agent-spawn", "teardown") == [] and calls(sc, "agent-spawn", "stop") == []


def test_no_more_than_ten_ambiguous_agent_ids_are_logged(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    sc = dead_before_agent(world, capsys)
    ids = [f"ag-fffffff{c}" for c in "2345672345"] + ["ag-ffffff22", "ag-ffffff23"]
    list_answer(sc, *(candidate(sc, i) for i in ids))
    assert check(sc, capsys)[0] == 0
    (warn,) = [e for e in events(world, "warning") if e["code"] == "ambiguous_fork"]
    assert warn["key"] == ",".join(sorted(ids)[:10]) and len(warn["key"].split(",")) == 10


@pytest.mark.parametrize(
    "bad",
    [
        {"forked_from": "ag-zzzzzzz2"},
        {"forked_from": None},
        {"labels": ["round=rd-aaaaaaaa", "owner=review-respond"]},
        {
            "labels": [],
        },
        {"labels": ["round=PLACEHOLDER", "owner=someone"]},
        {"agent_id": BASE},
        {"agent_id": "--select"},
    ],
    ids=[
        "other-source",
        "no-source",
        "other-round",
        "no-labels",
        "other-owner",
        "the-base",
        "not-an-id",
    ],
)
def test_a_candidate_that_fails_the_checks_is_not_accepted(
    world: World, capsys: pytest.CaptureFixture[str], bad: dict[str, Any]
) -> None:
    sc = dead_before_agent(world, capsys)
    over = dict(bad)
    if "labels" in over:
        over["labels"] = [x.replace("PLACEHOLDER", sc.rid) for x in over["labels"]]
    list_answer(sc, candidate(sc, **over))
    assert check(sc, capsys)[0] == 0
    row = sc.round()
    assert (row["state"], row["failure_reason"], row["agent_id"]) == ("failed", "job_lost", None)
    assert sc.sql("SELECT count(*) FROM forks") == [(0,)]


def test_a_candidate_already_recorded_for_another_round_is_not_accepted(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    sc = dead_before_agent(world, capsys)
    sc.sql(
        "INSERT INTO forks (agent_id, pr, round, source, state, created_at, updated_at)"
        " VALUES (?, ?, 'rd-aaaaaaab', NULL, 'round', 1, 1)",
        FORK,
        PR7,
    )
    list_answer(sc, candidate(sc))
    assert check(sc, capsys)[0] == 0
    assert sc.round()["failure_reason"] == "job_lost"
    assert sc.sql("SELECT round FROM forks") == [("rd-aaaaaaab",)]


def test_an_unreadable_list_leaves_the_round_running(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    sc = dead_before_agent(world, capsys)
    sc.fakes.script(
        "agent-spawn", "list", json={"error": {"code": "internal", "message": "x"}}, exit=1,
        schema=False,
    )  # fmt: skip
    assert check(sc, capsys)[0] == 0
    assert sc.round()["state"] == "running" and "list_failed" in warnings(world)


def test_a_fresh_session_is_accepted_only_in_the_round_throwaway(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    sc = dead_before_agent(world, capsys)
    sc.sql("UPDATE rounds SET base_agent_id = NULL")
    wt_show(sc)
    list_answer(sc, candidate(sc, forked_from=None, run_kind="respond", cwd=str(sc.root)))
    script_show(sc, final_run(sc, "failed"))
    assert check(sc, capsys)[0] == 0
    assert (sc.round()["agent_id"], sc.round()["state"]) == (FORK, "failed")
    assert sc.sql("SELECT source FROM forks") == [(None,)]


@pytest.mark.parametrize(
    "over",
    [{"cwd": "/somewhere/else"}, {"run_kind": "work"}, {"cwd": None}],
    ids=["other-cwd", "other-kind", "no-cwd"],
)
def test_a_fresh_candidate_outside_the_throwaway_is_not_accepted(
    world: World, capsys: pytest.CaptureFixture[str], over: dict[str, Any]
) -> None:
    sc = dead_before_agent(world, capsys)
    sc.sql("UPDATE rounds SET base_agent_id = NULL")
    wt_show(sc)
    base = {"forked_from": None, "run_kind": "respond", "cwd": str(sc.root)} | over
    list_answer(sc, candidate(sc, **base))
    assert check(sc, capsys)[0] == 0
    assert sc.round()["failure_reason"] == "job_lost"


def test_a_fresh_candidate_with_an_unreadable_throwaway_leaves_the_round_running(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    sc = dead_before_agent(world, capsys)
    sc.sql("UPDATE rounds SET base_agent_id = NULL")
    list_answer(sc, candidate(sc, forked_from=None, run_kind="respond", cwd=str(sc.root)))
    sc.fakes.script(
        "wtctl", "show", json={"error": {"code": "internal", "message": "x", "reason": None,
        "details": None, "lower_error": None}}, exit=1, schema=False,
    )  # fmt: skip
    assert check(sc, capsys)[0] == 0
    row = sc.round()
    assert (row["state"], row["agent_id"]) == ("running", None)
    assert "throwaway_unreadable" in warnings(world) and "left_running" in steps(world)
    assert sc.temp_files() != []


def test_a_fresh_candidate_with_no_recorded_throwaway_is_not_accepted(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    sc = dead_before_agent(world, capsys)
    sc.sql("UPDATE rounds SET base_agent_id = NULL, throwaway_id = NULL")
    list_answer(sc, candidate(sc, forked_from=None, run_kind="respond", cwd=str(sc.root)))
    assert check(sc, capsys)[0] == 0
    assert sc.round()["failure_reason"] == "job_lost"


# ---- the base after agent_gone, and held jobs ---------------------------------------------------


def test_a_round_that_ended_agent_gone_resets_the_pr_base(
    scene: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    scene.sql("UPDATE prs SET base_agent = ?", BASE)
    scene.sql(
        "UPDATE rounds SET state = 'failed', failure_reason = 'agent_gone', ended_at = 1,"
        " base_agent_id = ?",
        BASE,
    )
    scene.fakes.script("agent-spawn", "show", json={"error": {"code": "not_found",
        "message": "x"}}, exit=10, schema=False)  # fmt: skip
    scene.fakes.script("agent-spawn", "list", json={"agents": []}, schema=False)
    assert check(scene, capsys)[0] == 0
    assert scene.sql("SELECT base_agent FROM prs") == [(None,)]
    assert "base_lost" in warnings(scene.world)


def test_a_base_that_moved_on_is_not_reset(
    scene: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    scene.sql("UPDATE prs SET base_agent = 'ag-aaaaaab2'")
    scene.sql(
        "UPDATE rounds SET state = 'failed', failure_reason = 'agent_gone', ended_at = 1,"
        " base_agent_id = ?",
        BASE,
    )
    scene.fakes.script("agent-spawn", "show", json=show_after(scene), times=None, schema=False)
    assert check(scene, capsys)[0] == 0
    assert scene.sql("SELECT base_agent FROM prs") == [("ag-aaaaaab2",)]


def test_a_held_job_of_an_escalated_round_is_resolved_done(
    running: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    running.sql("UPDATE rounds SET job_id = ?", JOB)
    script_show(running, final_run(running, "blocked", reason="q", summary="s"))
    running.fakes.script("jobsctl", "show", json=job_doc(status="held", detail=True))
    running.fakes.script(
        "jobsctl", "resolve",
        json={"job": job_doc(status="done", reason="resolved"), "changed": True, "warnings": []},
        schema=False,
    )  # fmt: skip
    assert check(running, capsys)[0] == 0
    assert running.round()["state"] == "escalated"
    (resolve,) = calls(running, "jobsctl", "resolve")
    assert resolve.positionals["job"] == JOB and resolve.flags["--as"] == "done"
    assert resolve.flags["--actor"] == "auto:review-respond"
    assert steps(running.world)[-1] == "job_resolved"


@pytest.mark.parametrize(("hours", "resolved"), [(23, 1), (25, 0)])
def test_only_a_held_job_of_a_round_that_ended_in_the_last_day_is_resolved(
    scene: Scene, capsys: pytest.CaptureFixture[str], hours: int, resolved: int
) -> None:
    scene.sql(
        "UPDATE rounds SET job_id = ?, state = 'escalated', ended_at = ?",
        JOB,
        time.time() - hours * 3600,
    )
    list_answer(scene)  # the PR's base agent is looked up now that no round is active
    if resolved:  # outside the window jobsctl is not asked at all
        scene.fakes.script("jobsctl", "show", json=job_doc(status="held", detail=True))
        scene.fakes.script(
            "jobsctl", "resolve",
            json={"job": job_doc(status="done", reason="resolved"), "changed": True,
                  "warnings": []},
            schema=False,
        )  # fmt: skip
    assert check(scene, capsys)[0] == 0
    assert len(calls(scene, "jobsctl", "resolve")) == resolved
    assert len(calls(scene, "jobsctl", "show")) == resolved


def test_a_job_that_is_not_held_is_not_resolved(
    running: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    running.sql("UPDATE rounds SET job_id = ?", JOB)
    script_show(running, final_run(running, "blocked", reason="q", summary="s"))
    running.fakes.script("jobsctl", "show", json=job_doc(status="done", detail=True))
    assert check(running, capsys)[0] == 0
    assert calls(running, "jobsctl", "resolve") == []


# ---- a live holder is never taken over --------------------------------------------------------


def test_a_stalled_live_holder_is_never_taken_over(
    running: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    key = round_lease_key(running.rid)
    with holding(running, key):
        running.sql("UPDATE leases SET expires_at = 1, renewed_at = 1 WHERE key = ?", key)
        rev = running.round()["rev"]
        shows = len(calls(running, "agent-spawn", "show"))
        assert check(running, capsys)[0] == 0
        assert running.round()["state"] == "running" and running.round()["rev"] == rev
        assert len(calls(running, "agent-spawn", "show")) == shows  # not even looked at
    assert any(e["action"] == "stalled" for e in events(running.world, "lease"))


def test_show_prints_busy_for_a_live_holder_and_the_stored_row(
    running: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    before = len(calls(running, "agent-spawn", "show"))
    with holding(running, round_lease_key(running.rid)):
        code = main(["show", running.rid, "--json", "--config", str(running.config)])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0 and doc["finalize"] == "busy" and doc["round"]["state"] == "running"
    assert len(calls(running, "agent-spawn", "show")) == before


def test_show_runs_the_finalizer_and_prints_the_row_after(
    running: Scene, capsys: pytest.CaptureFixture[str], golden: Any
) -> None:
    script_show(running, final_run(running, "failed"))
    code = main(["show", running.rid, "--json", "--config", str(running.config)])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0
    assert doc["finalize"] == "done" and doc["round"]["state"] == "failed"
    assert doc["round"]["failure"] == {"reason": "agent_failed", "detail": None}
    text = json.dumps(doc).replace(running.rid, "rd-abcdefgh")
    scrubbed = json.loads(text)
    scrubbed["round"]["started"] = scrubbed["round"]["ended"] = "2026-01-01T12:00:00.000000Z"
    for item in scrubbed["comments"]:
        item["first_seen_at"] = "2026-01-01T12:00:00.000000Z"
    gold(golden, "show-round-finalized", scrubbed)
    # the finalizer opened the writers itself: the state change is logged
    assert [e["to"] for e in events(running.world, "round_state")][-1] == "failed"
    assert steps(running.world) == ["failed"]


@pytest.mark.parametrize("state", ["queued", "failed", "pending"])
def test_show_runs_no_finalizer_for_a_round_that_is_not_running(
    scene: Scene, capsys: pytest.CaptureFixture[str], state: str
) -> None:
    if state == "failed":
        scene.sql("UPDATE rounds SET state = 'failed', failure_reason = 'internal', ended_at = 1")
    elif state == "pending":
        scene.sql("UPDATE rounds SET state = 'pending'")
    before = len(scene.fakes.calls())
    code = main(["show", scene.rid, "--json", "--config", str(scene.config)])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0 and doc["finalize"] is None and doc["round"]["state"] == state
    assert len(scene.fakes.calls()) == before  # no sibling was asked


def test_show_writes_nothing_for_a_database_newer_than_this_binary(
    running: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = sqlite3.connect(running.state / "state.db")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.execute(f"PRAGMA user_version = {version + 1}")
    conn.commit()
    conn.close()
    code = main(["show", running.rid, "--json", "--config", str(running.config)])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0 and doc["finalize"] is None and doc["round"]["state"] == "running"


# ---- losing the lease ---------------------------------------------------------------------------


def test_a_lost_round_lease_during_the_finalizer_is_lease_lost_and_writes_nothing(
    running: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    key = round_lease_key(running.rid)
    script_show(running, final_run(running, "failed"), sleep=1.5)
    rev = running.round()["rev"]

    def steal() -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if len(running.fakes.calls("agent-spawn", "show")) > 1:
                running.sql("DELETE FROM leases WHERE key = ?", key)
                return
            time.sleep(0.02)

    t = threading.Thread(target=steal)
    t.start()
    script_prs(running.fakes, [pr_item(7)])
    script_comments(running.fakes, PR7, one_comment())
    code = main(["check", "--json", "--config", str(running.config)])
    t.join(timeout=30)
    capsys.readouterr()
    assert code == 1
    assert running.round()["state"] == "running" and running.round()["rev"] == rev


def test_a_check_pass_that_cannot_take_the_pass_lease_reports_already_running(
    scene: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    from review_respond.store import pass_lease_key

    with holding(scene, pass_lease_key()):
        code = main(["check", "--json", "--config", str(scene.config)])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0 and doc["already_running"] is True and doc["prs"] == []
    assert scene.round()["state"] == "queued"


# ---- hostile and odd answers ------------------------------------------------------------------


def test_an_exit_code_that_is_not_a_number_ends_the_round_internal(
    running: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = final_run(running, "failed")
    doc["runs"][0]["outcome"]["exit_code"] = "18"
    script_show(running, doc, schema=False)
    assert check(running, capsys)[0] == 0
    row = running.round()
    assert (row["state"], row["failure_reason"]) == ("failed", "internal")


@pytest.mark.parametrize("code", [130, 143])
def test_a_signal_exit_is_never_an_outcome_it_ends_the_round_internal(
    running: Scene, capsys: pytest.CaptureFixture[str], code: int
) -> None:
    doc = final_run(running, "failed")
    doc["runs"][0]["outcome"]["exit_code"] = code
    script_show(running, doc, schema=False)
    assert check(running, capsys)[0] == 0
    row = running.round()
    assert (row["state"], row["failure_reason"]) == ("failed", "internal")


def test_a_run_list_with_no_final_run_ends_the_round_agent_lost(
    running: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = final_run(running, "failed")
    doc["runs"] = [None]
    script_show(running, doc, schema=False)
    assert check(running, capsys)[0] == 0
    assert running.round()["failure_reason"] == "agent_lost"


def test_a_show_whose_runs_are_not_a_list_decides_nothing(
    running: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = final_run(running, "failed")
    doc["runs"] = {"0": "x"}
    script_show(running, doc, schema=False)
    assert check(running, capsys)[0] == 0
    assert running.round()["state"] == "running" and "show_failed" in warnings(running.world)


def test_an_escalation_text_with_control_characters_is_stored_cut_and_printed_sanitized(
    running: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    hostile = "\x1b[31mred\x00‮" + "x" * 10_000
    script_show(running, final_run(running, "blocked", reason=hostile, summary=hostile))
    assert check(running, capsys)[0] == 0
    stored = running.round()["escalation_reason"]
    assert len(stored.encode()) <= 4096
    main(["show", running.rid, "--json", "--config", str(running.config)])
    shown = json.loads(capsys.readouterr().out)["round"]["escalation"]["reason"]
    assert "\x1b" not in shown and "\x00" not in shown and "‮" not in shown
    assert "<U+001B>" in shown and len(shown) <= 4096


def test_a_database_that_stays_busy_makes_show_print_busy(
    running: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    """``try_acquire`` answers None for a busy database, like for a live holder."""
    release = threading.Event()
    locked = threading.Event()

    def hold() -> None:
        conn = sqlite3.connect(running.state / "state.db", isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        locked.set()
        release.wait(30)
        conn.execute("ROLLBACK")
        conn.close()

    t = threading.Thread(target=hold)
    t.start()
    assert locked.wait(10)
    try:
        code = main(["show", running.rid, "--json", "--config", str(running.config)])
    finally:
        release.set()
        t.join(timeout=30)
    doc = json.loads(capsys.readouterr().out)
    assert code == 0 and doc["finalize"] == "busy" and doc["round"]["state"] == "running"
