"""`stop` and the closed-PR sweep end to end against the strict sibling fakes (docs/interface.md:
stop, Reject, expiry and closed PRs). The round is set up the way the real ones end up; the fakes'
recorded calls, the audit file and the database are the evidence."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from review_respond import lineage, stop
from review_respond.cli import main
from review_respond.store import Store, open_state_dir, round_lease_key
from tests.harness import schemas
from tests.harness.jobsctl import JOB, job_doc
from tests.harness.plugin import World
from tests.test_check import (
    PR7,
    gold,
    log_events,
    one_comment,
    pr_item,
    script_comments,
    script_prs,
    valid,
)
from tests.test_finalizer import block, final_run, left_running
from tests.test_job import FORK, Scene, fork_agent, make_round, script_show, show_after

HUMAN = ["--actor", "human:tester"]


_CAPSYS: list[pytest.CaptureFixture[str]] = []


@pytest.fixture(autouse=True)
def _capsys(capsys: pytest.CaptureFixture[str]) -> None:
    _CAPSYS[:] = [capsys]


def stop_cmd(sc: Scene, *extra: str, rid: str | None = None) -> tuple[int, dict[str, Any]]:
    sc.before = len(sc.fakes.calls())  # `Scene.calls` then lists what the stop did
    code = main(["stop", rid or sc.rid, "--json", "--config", str(sc.config), *extra])
    out, _ = _CAPSYS[0].readouterr()
    return code, json.loads(out) if out.strip() else {}


def audit(world: World) -> list[dict[str, Any]]:
    path = Path(world.env["XDG_STATE_HOME"]) / "review-respond" / "audit.jsonl"
    if not path.exists():
        return []
    found = [json.loads(line) for line in path.read_text().splitlines() if line]
    for record in found:
        schemas.validate("review-respond/audit.json", record)
    return found


def stop_doc(sc: Scene, *, status: str = "parked") -> dict[str, Any]:
    return {
        "agent": fork_agent(sc.rid, status=status), "action": "session_stop",
        "session_stop": None, "already_parked": False, "removed": False, "dry_run": False,
        "warnings": [],
    }  # fmt: skip


def spawn_error(code: str, message: str) -> dict[str, Any]:
    keys = ("reason", "agent_id", "tool", "lower_code", "lower_error", "details")
    keys += ("hook", "hook_stderr")
    return {
        "error": {
            "code": code, "message": message, **dict.fromkeys(keys),
            "candidates": [], "decision_ids": [],
        }
    }  # fmt: skip


def cancel_doc(*, status: str = "canceled") -> dict[str, Any]:
    return {"changed": True, "dry_run": False, "jobs": [job_doc(status=status)], "refused": []}


@pytest.fixture
def queued(world: World, capsys: pytest.CaptureFixture[str]) -> Scene:
    """A queued round no job was ever submitted for."""
    return make_round(world, capsys)


@pytest.fixture
def running(world: World, capsys: pytest.CaptureFixture[str]) -> Scene:
    """A running round whose job exited and whose fork is live (the finalizer's case)."""
    return left_running(world, capsys)


def fixed(doc: dict[str, Any], rid: str) -> dict[str, Any]:
    return json.loads(json.dumps(doc).replace(rid, "rd-abcdefgh"))  # type: ignore[no-any-return]


# ---- a queued round -----------------------------------------------------------------------------


def test_a_queued_round_with_no_job_is_canceled_and_blocks_the_pr(
    queued: Scene, golden: Any
) -> None:
    code, doc = stop_cmd(queued, *HUMAN)
    assert code == 0
    valid("stop", doc)
    assert (doc["changed"], doc["cancel_requested"], doc["dry_run"]) == (True, True, False)
    row = queued.round()
    assert (row["state"], row["cancel_reason"], row["counted"]) == ("canceled", "stopped", 0)
    assert row["cancel_requested_at"] is not None
    assert block(queued) == "stopped"
    gold(golden, "stop-queued", fixed(doc, queued.rid))
    # no sibling was called: there was no job and no agent
    assert queued.calls("jobsctl") == [] and queued.calls("agent-spawn") == []


def test_stopping_a_terminal_round_is_a_no_op(queued: Scene, golden: Any) -> None:
    assert stop_cmd(queued, *HUMAN)[0] == 0
    before = queued.round()
    code, doc = stop_cmd(queued, *HUMAN)
    assert code == 0
    valid("stop", doc)
    assert (doc["changed"], doc["cancel_requested"]) == (False, True)
    assert queued.round() == before
    gold(golden, "stop-noop", fixed(doc, queued.rid))


def test_stopping_a_round_that_ended_on_its_own_changes_nothing(queued: Scene) -> None:
    queued.sql(
        "UPDATE rounds SET state = 'failed', failure_reason = 'job_lost', ended_at = 1,"
        " rev = rev + 1"
    )
    code, doc = stop_cmd(queued, *HUMAN)
    assert code == 0 and (doc["changed"], doc["cancel_requested"]) == (False, False)
    assert queued.round()["cancel_requested_at"] is None and block(queued) is None


def test_a_pending_round_is_refused_pending_exists(queued: Scene) -> None:
    queued.sql("UPDATE rounds SET state = 'pending', counted = 1, rev = rev + 1")
    code, doc = stop_cmd(queued, *HUMAN)
    assert code == 20 and doc["error"]["reason"] == "pending_exists"
    assert doc["error"]["details"]["hint"] == "reject"
    row = queued.round()
    assert row["state"] == "pending" and row["cancel_requested_at"] is None
    assert queued.calls("jobsctl") == [] and queued.calls("agent-spawn") == []


def test_the_id_may_be_a_prefix_and_a_bad_or_unknown_id_is_refused(queued: Scene) -> None:
    code, doc = stop_cmd(queued, "--dry-run", rid=queued.rid[:7])
    assert code == 0 and doc["round"]["id"] == queued.rid
    assert stop_cmd(queued, rid="rd-aaaaaaaa")[0] == 10
    code, doc = stop_cmd(queued, rid="nonsense")
    assert (code, doc["error"]["code"]) == (2, "usage")
    for tail in ("p", "q"):
        queued.sql(
            "INSERT INTO rounds (id, pr, number, state, trigger_kind, created_at, ended_at,"
            " cancel_reason) VALUES (?, ?, ?, 'canceled', 'check', 1, 2, 'stopped')",
            f"rd-ppppqqq{tail}", PR7, 2 if tail == "p" else 3,
        )  # fmt: skip
    code, doc = stop_cmd(queued, rid="rd-pppp")
    assert (code, doc["error"]["code"]) == (21, "ambiguous")


# ---- a queued round with a jobsctl job ----------------------------------------------------------


def with_job(sc: Scene) -> None:
    sc.sql("UPDATE rounds SET job_id = ?", JOB)


def test_a_queued_job_is_canceled_through_jobsctl(queued: Scene) -> None:
    with_job(queued)
    queued.fakes.script("jobsctl", "cancel", json=cancel_doc())
    code, doc = stop_cmd(queued, *HUMAN)
    assert code == 0 and doc["changed"] is True
    assert queued.round()["state"] == "canceled" and block(queued) == "stopped"
    (call,) = queued.fakes.calls("jobsctl", "cancel")
    assert call.positionals["job"] == JOB and call.flags["--actor"] == "human:tester"
    assert "--force" not in call.flags  # a running job is never force-canceled


def test_a_job_that_already_started_is_left_to_read_the_flag(queued: Scene) -> None:
    with_job(queued)
    queued.fakes.script(
        "jobsctl", "cancel",
        json={"error": {"code": "refused", "message": "running", "reason": "live"}},
        exit=20,
    )  # fmt: skip
    code, doc = stop_cmd(queued, *HUMAN)
    assert code == 0
    assert (doc["changed"], doc["cancel_requested"], doc["round"]["state"]) == (
        True,
        True,
        "queued",
    )
    assert queued.round()["cancel_reason"] == "stopped" and block(queued) is None
    # the job's own first look at the round ends it
    assert main(["_job", queued.rid, "--config", str(queued.config)]) == 0
    row = queued.round()
    assert (row["state"], row["cancel_reason"]) == ("canceled", "stopped")
    assert block(queued) == "stopped"


def test_a_job_jobsctl_no_longer_knows_counts_as_gone(queued: Scene) -> None:
    with_job(queued)
    queued.fakes.script(
        "jobsctl", "cancel", json={"error": {"code": "not_found", "message": "no"}}, exit=10
    )
    assert stop_cmd(queued, *HUMAN)[0] == 0
    assert queued.round()["state"] == "canceled"


def test_a_failed_jobsctl_cancel_keeps_the_flag_and_exits_12(queued: Scene) -> None:
    with_job(queued)
    queued.fakes.script(
        "jobsctl", "cancel", json={"error": {"code": "internal", "message": "boom"}}, exit=1
    )
    code, doc = stop_cmd(queued, *HUMAN)
    assert (code, doc["error"]["code"]) == (12, "lower_failed")
    assert doc["error"]["lower_error"]["code"] == "internal"
    row = queued.round()
    assert row["state"] == "queued" and row["cancel_requested_at"] is not None
    attempts = [r for r in audit(queued.world) if r["phase"] == "outcome"]
    assert attempts[-1]["result"]["status"] == "failed"
    assert attempts[-1]["result"]["lower_code"] == "internal"


def test_without_jobsctl_a_stored_job_decides_nothing(queued: Scene) -> None:
    with_job(queued)
    queued.world.drop_tool("jobsctl")
    code, doc = stop_cmd(queued, *HUMAN)
    assert code == 0 and doc["round"]["state"] == "queued" and doc["cancel_requested"] is True
    assert queued.round()["cancel_requested_at"] is not None


# ---- a queued round with a detached job ---------------------------------------------------------


def detached(sc: Scene, seconds_ago: float) -> None:
    sc.sql("UPDATE rounds SET detached_started_at = ?", time.time() - seconds_ago)


def test_a_detached_job_that_is_gone_ends_the_round(queued: Scene) -> None:
    detached(queued, 3600)
    assert stop_cmd(queued, *HUMAN)[0] == 0
    assert queued.round()["state"] == "canceled" and block(queued) == "stopped"


def test_a_young_detached_job_may_not_have_its_lease_yet_and_is_left(queued: Scene) -> None:
    detached(queued, 5)
    code, doc = stop_cmd(queued, *HUMAN)
    assert code == 0 and doc["round"]["state"] == "queued" and doc["cancel_requested"] is True


def test_a_detached_job_that_holds_its_lease_is_left_to_read_the_flag(queued: Scene) -> None:
    detached(queued, 3600)
    store = Store.open(open_state_dir(queued.state), write=True, close_state=True)
    held = store.try_acquire(round_lease_key(queued.rid), heartbeat=None)
    assert held is not None
    try:
        code, doc = stop_cmd(queued, *HUMAN)
    finally:
        held.release()
        store.close()
    assert code == 0 and doc["round"]["state"] == "queued"


# ---- a running round ----------------------------------------------------------------------------


def test_a_running_round_with_an_agent_gets_agent_spawn_stop_with_the_grace(
    running: Scene,
) -> None:
    running.fakes.script("agent-spawn", "stop", json=stop_doc(running))
    code, doc = stop_cmd(running, *HUMAN)
    assert code == 0
    valid("stop", doc)
    assert (doc["changed"], doc["cancel_requested"], doc["round"]["state"]) == (
        True,
        True,
        "running",
    )
    (call,) = running.fakes.calls("agent-spawn", "stop")
    assert call.positionals["agent_id"] == FORK
    assert call.flags["--grace"] == "75s" and call.flags["--actor"] == "human:tester"
    row = running.round()
    assert row["cancel_reason"] == "stopped" and row["state"] == "running"


def test_a_stopped_running_round_ends_canceled_and_a_check_starts_nothing(
    running: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    running.fakes.script("agent-spawn", "stop", json=stop_doc(running))
    running.fakes.script("agent-spawn", "list", json={"agents": []}, schema=False, times=None)
    assert stop_cmd(running, *HUMAN)[0] == 0
    # the next check finalizes the round: the run was killed by the stop
    script_show(running, final_run(running, "killed", end_reason="stopped"))
    script_prs(running.fakes, [pr_item(7)])
    script_comments(running.fakes, PR7, one_comment())
    assert main(["check", "--json", "--config", str(running.config)]) == 0
    first = json.loads(capsys.readouterr().out)
    row = running.round()
    assert (row["state"], row["cancel_reason"], row["counted"]) == ("canceled", "stopped", 1)
    assert block(running) == "stopped"
    assert first["prs"][0]["started_round"] is None
    # and the pass after that starts nothing either: the PR is blocked until a human respond
    script_prs(running.fakes, [pr_item(7)])
    script_comments(running.fakes, PR7, one_comment())
    assert main(["check", "--json", "--config", str(running.config)]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["prs"][0]["started_round"] is None and second["prs"][0]["state"] == "waiting"
    assert running.sql("SELECT count(*) FROM rounds") == [(1,)]
    assert running.world.detached == []
    assert running.fakes.calls("jobsctl", "add") == []


def test_a_repeated_stop_asks_agent_spawn_again_and_reports_no_change(running: Scene) -> None:
    running.fakes.script("agent-spawn", "stop", json=stop_doc(running), times=2)
    assert stop_cmd(running, *HUMAN)[1]["changed"] is True
    code, doc = stop_cmd(running, *HUMAN)
    assert code == 0 and doc["changed"] is False and doc["cancel_requested"] is True
    assert len(running.fakes.calls("agent-spawn", "stop")) == 2


def test_a_running_round_without_an_agent_yet_is_left_to_its_job(running: Scene) -> None:
    running.sql("UPDATE rounds SET agent_id = NULL")
    code, doc = stop_cmd(running, *HUMAN)
    assert code == 0 and doc["changed"] is True and doc["round"]["state"] == "running"
    assert running.fakes.calls("agent-spawn", "stop") == []


def test_only_a_recorded_fork_is_ever_stopped(running: Scene) -> None:
    running.sql("DELETE FROM forks")
    code, doc = stop_cmd(running, *HUMAN)
    assert code == 0 and doc["changed"] is True
    assert running.fakes.calls("agent-spawn", "stop") == []
    assert not lineage.is_recorded(
        Store.open(open_state_dir(running.state), write=False, close_state=True).conn, FORK
    )
    assert any(e.get("code") == "stop_unrecorded_agent" for e in log_events(running.world))


def test_an_agent_agent_spawn_no_longer_knows_is_left_to_the_finalizer(running: Scene) -> None:
    running.fakes.script(
        "agent-spawn",
        "stop",
        json=spawn_error("not_found", "gone"),
        exit=10,
    )
    code, doc = stop_cmd(running, *HUMAN)
    assert code == 0 and doc["round"]["state"] == "running"


def test_a_failed_agent_spawn_stop_exits_12_and_keeps_the_flag(running: Scene) -> None:
    running.fakes.script(
        "agent-spawn", "stop",
        json=spawn_error("internal", "x"), exit=12,
    )  # fmt: skip
    code, doc = stop_cmd(running, *HUMAN)
    assert (code, doc["error"]["code"]) == (12, "lower_failed")
    row = running.round()
    assert row["state"] == "running" and row["cancel_requested_at"] is not None


def test_the_stop_grace_follows_jobs_kill_grace(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    sc = left_running(world, capsys, jobs={"kill_grace": "10m"})
    sc.fakes.script("agent-spawn", "stop", json=stop_doc(sc))
    assert stop_cmd(sc, *HUMAN)[0] == 0
    (call,) = sc.fakes.calls("agent-spawn", "stop")
    assert call.flags["--grace"] == "550s"


# ---- actor, audit, dry run, lease ---------------------------------------------------------------


def test_stop_does_not_need_a_human_actor_and_records_who_did_it(queued: Scene) -> None:
    code, _ = stop_cmd(queued, "--actor", "auto:policy", "--decision-id", "dec-1")
    assert code == 0
    records = audit(queued.world)
    assert [r["phase"] for r in records] == ["attempted", "outcome"]
    attempted, outcome = records
    assert attempted["attempt_id"] == outcome["attempt_id"]
    for record in records:
        assert record["action"] == "stop" and record["target"] == PR7
        assert record["actor"] == "auto:policy" and record["decision_id"] == "dec-1"
        assert record["inputs"]["round"] == queued.rid
        assert record["inputs"]["actor_source"] == "flag"
    assert outcome["result"]["status"] == "ok"


def test_a_no_op_is_one_decision_record(queued: Scene) -> None:
    stop_cmd(queued, *HUMAN)
    n = len(audit(queued.world))
    stop_cmd(queued, *HUMAN)
    new = audit(queued.world)[n:]
    assert [(r["phase"], r["result"]["status"]) for r in new] == [("outcome", "noop")]


def test_a_refusal_is_audited_as_refused(queued: Scene) -> None:
    queued.sql("UPDATE rounds SET state = 'pending', counted = 1, rev = rev + 1")
    stop_cmd(queued, *HUMAN)
    (record,) = audit(queued.world)
    assert record["result"] == {"status": "refused", "exit_code": 20, "reason": "pending_exists"}


def test_a_failed_first_audit_append_stops_before_anything_is_done(
    queued: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    from review_respond.audit import AuditLog
    from review_respond.errors import CliError

    def refuse(self: AuditLog, *a: Any, **k: Any) -> str:
        raise CliError("internal", "cannot append to audit.jsonl")

    monkeypatch.setattr(AuditLog, "attempted", refuse)
    with_job(queued)
    code, doc = stop_cmd(queued, *HUMAN)
    assert (code, doc["error"]["code"]) == (1, "internal")
    row = queued.round()
    assert row["cancel_requested_at"] is None and row["state"] == "queued"
    assert queued.fakes.calls("jobsctl") == []


def test_a_dry_run_changes_and_calls_nothing(running: Scene, golden: Any) -> None:
    state = running.state
    before = running.round()
    n_calls = len(running.fakes.calls())
    logs = {p.name: p.read_bytes() for p in state.glob("*.jsonl")}
    code, doc = stop_cmd(running, "--dry-run", *HUMAN)
    assert code == 0
    valid("stop", doc)
    assert (doc["dry_run"], doc["changed"], doc["cancel_requested"]) == (True, False, False)
    assert running.round() == before and len(running.fakes.calls()) == n_calls
    assert {p.name: p.read_bytes() for p in state.glob("*.jsonl")} == logs
    assert running.sql("SELECT count(*) FROM leases") == [(0,)]
    gold(golden, "stop-dry-run", fixed(doc, running.rid))


def test_a_dry_run_with_no_state_is_not_found(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["stop", "rd-aaaaaaaa", "--dry-run", "--json"]) == 10


def test_a_busy_pr_lease_is_refused_busy(queued: Scene, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stop, "PR_LEASE_WAIT", 0.2)
    from review_respond.store import pr_digest, pr_lease_key

    store = Store.open(open_state_dir(queued.state), write=True, close_state=True)
    held = store.try_acquire(pr_lease_key(pr_digest(PR7)), heartbeat=None)
    assert held is not None
    try:
        code, doc = stop_cmd(queued, *HUMAN)
    finally:
        held.release()
        store.close()
    assert (code, doc["error"]["reason"]) == (20, "busy")
    assert queued.round()["cancel_requested_at"] is None


# ---- the closed-PR sweep ------------------------------------------------------------------------


def close_pr(sc: Scene) -> None:
    sc.sql("UPDATE prs SET host_state = 'closed', rev = rev + 1")


def sweep(sc: Scene, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    script_prs(sc.fakes, [])
    assert main(["check", "--json", "--config", str(sc.config)]) == 0
    return json.loads(capsys.readouterr().out)  # type: ignore[no-any-return]


def test_check_stops_the_queued_round_of_a_closed_pr_without_a_block(
    queued: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    close_pr(queued)
    sweep(queued, capsys)
    row = queued.round()
    assert (row["state"], row["cancel_reason"], row["counted"]) == ("canceled", "pr_closed", 0)
    assert block(queued) is None
    records = audit(queued.world)
    assert {r["actor"] for r in records} == {"auto:review-respond"}
    assert [r["phase"] for r in records] == ["attempted", "outcome"]


def test_check_stops_the_running_round_of_a_closed_pr_through_agent_spawn(
    running: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    close_pr(running)
    running.fakes.script("agent-spawn", "stop", json=stop_doc(running))
    script_show(running, show_after(running, status="live"))  # the finalizer's look: still live
    sweep(running, capsys)
    (call,) = running.fakes.calls("agent-spawn", "stop")
    assert call.positionals["agent_id"] == FORK and call.flags["--actor"] == "auto:review-respond"
    row = running.round()
    assert row["cancel_reason"] == "pr_closed" and row["state"] == "running"
    # the run then ends stopped: canceled `pr_closed`, no block
    script_show(running, final_run(running, "killed", end_reason="stopped"))
    sweep(running, capsys)
    row = running.round()
    assert (row["state"], row["cancel_reason"]) == ("canceled", "pr_closed")
    assert block(running) is None


def test_the_sweep_leaves_a_pr_whose_lease_is_held(
    queued: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    from review_respond.store import pr_digest, pr_lease_key

    close_pr(queued)
    store = Store.open(open_state_dir(queued.state), write=True, close_state=True)
    held = store.try_acquire(pr_lease_key(pr_digest(PR7)), heartbeat=None)
    assert held is not None
    try:
        sweep(queued, capsys)
    finally:
        held.release()
        store.close()
    assert queued.round()["state"] == "queued" and queued.round()["cancel_requested_at"] is None


def test_a_stop_flag_wins_over_the_sweeps_reason(
    queued: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    with_job(queued)
    queued.fakes.script(
        "jobsctl", "cancel",
        json={"error": {"code": "refused", "message": "running", "reason": "live"}}, exit=20,
    )  # fmt: skip
    assert stop_cmd(queued, *HUMAN)[0] == 0
    close_pr(queued)
    # the sweep leaves a round that is already flagged; the queued reconcile ends it with the
    # flag's reason once jobsctl says the job is over
    queued.fakes.script("jobsctl", "show", json=job_doc(status="canceled", detail=True))
    sweep(queued, capsys)
    row = queued.round()
    assert (row["state"], row["cancel_reason"]) == ("canceled", "stopped")
    assert block(queued) == "stopped"


def test_an_open_pr_is_never_swept(queued: Scene, capsys: pytest.CaptureFixture[str]) -> None:
    script_prs(queued.fakes, [pr_item(7)])
    script_comments(queued.fakes, PR7, one_comment())
    assert main(["check", "--json", "--config", str(queued.config)]) == 0
    capsys.readouterr()
    assert queued.round()["cancel_requested_at"] is None


# ---- real processes -----------------------------------------------------------------------------


def spawn_stop(sc: Scene, *extra: str) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [sys.executable, "-m", "review_respond", "stop", sc.rid, "--json", "--config",
         str(sc.config), *HUMAN, *extra],
        env=sc.world.env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )  # fmt: skip
    sc.world.procs.record(proc.pid)
    return proc


def finish(proc: subprocess.Popen[bytes]) -> tuple[int, dict[str, Any]]:
    out, err = proc.communicate(timeout=60)
    assert out.strip(), err.decode()
    return proc.returncode, json.loads(out)


@pytest.mark.parametrize("round_no", range(3))
def test_two_stops_of_a_queued_round_change_it_once(queued: Scene, round_no: int) -> None:
    procs = [spawn_stop(queued) for _ in range(2)]
    results = [finish(p) for p in procs]
    assert [code for code, _ in results] == [0, 0]
    assert sorted(doc["changed"] for _, doc in results) == [False, True]
    row = queued.round()
    assert (row["state"], row["cancel_reason"]) == ("canceled", "stopped")
    assert block(queued) == "stopped"
    # one attempt with its outcome, and one no-op decision
    phases = sorted(r["phase"] for r in audit(queued.world))
    assert phases == ["attempted", "outcome", "outcome"]


def test_a_stop_racing_the_end_of_a_running_round_leaves_one_consistent_end(
    running: Scene,
) -> None:
    """The stop's agent-spawn call is slow; meanwhile another process ends the round. Whatever
    wins, the round has one terminal state, the block matches it and the stop exits 0."""
    running.fakes.script("agent-spawn", "stop", json=stop_doc(running), sleep=1.0)
    proc = spawn_stop(running)
    deadline = time.monotonic() + 30
    while running.round()["cancel_requested_at"] is None:
        assert time.monotonic() < deadline, "the stop never set its flag"
        time.sleep(0.02)
    from review_respond.outcomes import RunEnd
    from review_respond.rounds import Rounds

    store = Store.open(open_state_dir(running.state), write=True, close_state=True)
    lease = store.try_acquire(round_lease_key(running.rid), heartbeat=None)
    assert lease is not None
    try:
        moved = Rounds(lease).finish_run(
            running.rid, RunEnd(command="wait", exit=15, end_reason="stopped")
        )
    finally:
        lease.release()
        store.close()
    code, doc = finish(proc)
    assert code == 0 and moved.changed
    row = running.round()
    assert row["state"] == "canceled" and row["cancel_reason"] == "stopped"  # the flag was set
    assert block(running) == "stopped"
    assert doc["cancel_requested"] is True
    assert running.sql("SELECT count(*) FROM rounds WHERE state IN ('queued', 'running')") == [(0,)]
