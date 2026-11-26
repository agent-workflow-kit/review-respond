"""Job placement and recovery with real processes (docs/interface.md: Job, Concurrency).

A round is started by ``check`` in both placements. The detached placement starts a real
``review-respond _job`` process (its pid is recorded in the world's registry the moment it exists);
the jobsctl placement is the same process started the way jobsctl would run the submitted command
line (jobsctl itself is a scripted fake: it records the call and answers). Both then run through
the same assertions: the end state of a finished job, a job killed mid-run that ``check``
finalizes, two finalizers racing, and the second round forking the first fork.

Every wait is bounded and every process is a recorded exact pid; nothing is found by name.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from review_respond import lineage
from review_respond.gateway import Gateway
from review_respond.placement import job_env
from review_respond.rounds import Rounds
from review_respond.store import (
    Store,
    open_state_dir,
    pr_digest,
    pr_lease_key,
    process_state,
    round_lease_key,
)
from tests.harness import builders
from tests.harness.agents import original, show_doc
from tests.harness.jobsctl import add_doc
from tests.harness.plugin import World
from tests.harness.spawn_run import started_doc
from tests.test_check import (
    AGENT,
    PR7,
    log_events,
    one_comment,
    pr_item,
    run,
    script_comments,
    script_prs,
    set_agent,
    sql,
    write_config,
)
from tests.test_job import (
    C1,
    FORK,
    HEAD,
    Scene,
    artifact_file,
    fork_agent,
    good_artifact,
    script_show,
    script_throwaway,
    show_after,
    waited,
)

FORK2 = "ag-fffffff3"
C2 = "gh:rc:c2"
HOWS = ["detached", "jobsctl"]


# ---- the world ----------------------------------------------------------------------------------


def eventually(check: Callable[[], bool], seconds: float = 40.0, what: str = "condition") -> None:
    """Poll ``check`` every 50 ms for at most ``seconds``; the failure names what was waited for."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out after {seconds}s waiting for {what}")


def gone(pid: int) -> bool:
    """The process ended (or is a zombie waiting for its parent to reap it)."""
    return process_state(pid) in (None, "Z", "X")


class Jobs:
    """Starts the job of the rounds ``check`` places, in either placement, and knows its pids.

    The job is started by ``start`` and not by ``check``, so that the sibling answers it needs
    (which carry the round's id) can be scripted first. Detached: the real ``start_detached`` runs
    then, with the very argv, environment and output file ``check`` gave it. jobsctl: the command
    line the scripted ``add`` recorded is run the way jobsctl would run it."""

    def __init__(self, world: World, how: str, monkeypatch: pytest.MonkeyPatch) -> None:
        self.world = world
        self.how = how
        self.pids: list[int] = []
        self.procs: list[subprocess.Popen[bytes]] = []
        self.deferred: list[tuple[Gateway, list[str], str, int, dict[str, str]]] = []
        self.started = 0
        if how == "detached":
            world.drop_tool("jobsctl")

            def defer(
                gw: Gateway,
                argv: list[str],
                *,
                cwd: str | os.PathLike[str],
                out_fd: int,
                env: dict[str, str] | None = None,
            ) -> int:
                self.deferred.append(
                    (
                        gw,
                        list(argv),
                        os.fspath(cwd),
                        os.dup(out_fd),
                        dict(gw.env if env is None else env),
                    )
                )
                return 2**22

            self.real = Gateway.start_detached
            monkeypatch.setattr(Gateway, "start_detached", defer)

    def start(self) -> int:
        """Start the next placed job and return its pid."""
        index = self.started
        self.started += 1
        if self.how == "detached":
            gw, argv, cwd, fd, env = self.deferred[index]
            try:
                pid = self.real(gw, argv, cwd=cwd, out_fd=fd, env=env)
            finally:
                os.close(fd)
            self.world.procs.record(pid)
        else:
            add = self.world.fakes.calls("jobsctl", "add")[index]
            state = Path(self.world.env["XDG_STATE_HOME"]) / "review-respond"
            out = (self.world.root / f"job{index}.out").open("wb")
            proc = subprocess.Popen(
                add.tail,
                env=job_env(self.world.env, []),
                cwd=state,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            out.close()
            self.procs.append(proc)
            pid = proc.pid
            self.world.procs.record(pid)
        self.pids.append(pid)
        return pid

    def reap(self) -> None:
        for proc in self.procs:
            proc.poll()


@pytest.fixture(params=HOWS)
def jobs(request: pytest.FixtureRequest, world: World, monkeypatch: pytest.MonkeyPatch) -> Jobs:
    return Jobs(world, request.param, monkeypatch)


def agent_reads(world: World) -> None:
    """What every ``check`` asks agent-spawn about the PR's original agent."""
    world.fakes.script(
        "agent-spawn", "show", json=show_doc(original(AGENT, PR7, status="parked")),
        match={"positionals": {"agent_id": AGENT}}, times=None,
    )  # fmt: skip
    world.fakes.script("agent-spawn", "list", json={"agents": []}, schema=False, times=None)


def start_round(
    world: World, capsys: pytest.CaptureFixture[str], jobs: Jobs, comments: list[dict[str, Any]]
) -> Scene:
    """``check`` finds PR 7 due and starts a round (first pass: no agent yet, then the original)."""
    config = write_config(world)
    agent_reads(world)
    script_prs(world.fakes, [pr_item(7)])
    script_comments(world.fakes, PR7, comments)
    assert run(capsys, "check", config=config)[0] == 0
    set_agent(world)
    return next_round(world, capsys, jobs, config, comments)


def next_round(
    world: World,
    capsys: pytest.CaptureFixture[str],
    jobs: Jobs,
    config: Path,
    comments: list[dict[str, Any]],
) -> Scene:
    if jobs.how == "jobsctl":
        world.fakes.script("jobsctl", "add", json=add_doc(f"jb-abcdefg{len(jobs.pids) + 2}"))
    script_prs(world.fakes, [pr_item(7)])
    script_comments(world.fakes, PR7, comments)
    code, doc, _ = run(capsys, "check", config=config)
    assert code == 0
    started = [p for p in doc["prs"] if p["pr"] == PR7][0]["started_round"]
    assert started is not None
    root = world.root / "tw" / f"throw{started}"
    root.mkdir(parents=True)
    root.chmod(0o700)
    state = Path(world.env["XDG_STATE_HOME"]) / "review-respond"
    sc = Scene(world, config, started, root, state)
    sc.before = len(world.fakes.calls())
    return sc


def scripts_for_round(
    sc: Scene,
    *,
    fork: str = FORK,
    wait_sleep: float = 0.0,
    live: bool = False,
    final_show: bool = True,
    comment: str = C1,
) -> None:
    """Every sibling answer of a round that ends with a valid artifact."""
    (sc.root / "src").mkdir(exist_ok=True)
    (sc.root / "src" / "app.py").write_text("x = 1\n")
    sc.fakes.script(
        "forgectl", "show", json=builders.forgectl_show(head_sha=HEAD),
        match={"positionals": {"pr": PR7}},
    )  # fmt: skip
    script_throwaway(sc)
    resume = started_doc(first_working=True)
    resume["agent"] = fork_agent(sc.rid) | {"agent_id": fork}
    sc.fakes.script("agent-spawn", "resume", json=resume)
    wait = waited(sc, "done")
    wait["agent"] = fork_agent(sc.rid) | {"agent_id": fork}
    sc.fakes.script(
        "agent-spawn", "wait", json=wait, schema="agent-spawn/cli/wait.json", sleep=wait_sleep
    )
    if not final_show:
        return  # the job is killed before it asks
    ref = artifact_file(sc, good_artifact(replies=[{"comment_id": comment, "body": "Renamed."}]))
    shown = show_after(sc, artifact=ref, status="live" if live else "parked")
    shown["agent"] = fork_agent(sc.rid, status="live" if live else "parked") | {"agent_id": fork}
    script_show(sc, shown)


def read_comments_for_job(sc: Scene, comments: list[dict[str, Any]]) -> None:
    script_comments(sc.fakes, PR7, comments)


# ---- both placements end a job the same way -----------------------------------------------------


@pytest.mark.real_detached
def test_both_placements_run_the_job_to_the_same_end(
    world: World, capsys: pytest.CaptureFixture[str], jobs: Jobs
) -> None:
    sc = start_round(world, capsys, jobs, one_comment())
    scripts_for_round(sc)
    read_comments_for_job(sc, one_comment())
    pid = jobs.start()
    eventually(lambda: gone(pid), what="the job process to end")
    jobs.reap()
    row = sc.round()
    # a valid artifact is where #23 stops: the stored diff and the record are #28's and #29's
    assert (row["state"], row["agent_id"], row["counted"]) == ("running", FORK, 1)
    assert sc.comment_states()[C1] == "delivered"
    assert sc.temp_files() == []
    assert sql(world, "SELECT key FROM leases WHERE key LIKE 'round:%'") == []  # released
    names = [(c.tool, c.command) for c in sc.calls()]
    assert names == [
        ("forgectl", "show"),
        ("forgectl", "comments"),
        ("wtctl", "create"),
        ("agent-spawn", "resume"),
        ("agent-spawn", "wait"),
        ("agent-spawn", "show"),
    ]
    if jobs.how == "detached":
        assert row["detached_started_at"] is not None and row["job_id"] is None
        out = Path(world.env["XDG_STATE_HOME"]) / "review-respond" / "jobs" / f"{sc.rid}.log"
        assert out.exists() and oct(out.stat().st_mode & 0o777) == "0o600"
    else:
        assert row["job_id"] is not None and row["detached_started_at"] is None


# ---- a job killed mid-run is finalized by check -------------------------------------------------


@pytest.mark.real_detached
def test_a_job_killed_mid_run_is_finalized_by_check(
    world: World, capsys: pytest.CaptureFixture[str], jobs: Jobs
) -> None:
    sc = start_round(world, capsys, jobs, one_comment())
    scripts_for_round(
        sc, wait_sleep=6.0, final_show=False
    )  # the job sits in `wait` while it is killed
    read_comments_for_job(sc, one_comment())
    pid = jobs.start()
    eventually(
        lambda: sc.round()["agent_id"] == FORK and bool(sc.fakes.calls("agent-spawn", "wait")),
        what="the job to record the agent and start waiting",
    )
    os.kill(pid, signal.SIGKILL)  # the exact pid this test started
    eventually(lambda: gone(pid), what="the killed job to be gone")
    jobs.reap()
    row = sc.round()
    assert (row["state"], row["agent_id"]) == ("running", FORK)  # nobody has finalized it yet
    assert sql(world, "SELECT count(*) FROM leases WHERE key = ?", round_lease_key(sc.rid)) == [
        (1,)
    ]  # the dead job's lease row is still there
    # the agent's run ended (killed with the job's process group gone): check finalizes the round
    sc.fakes.script("agent-spawn", "show", json=_failed_run(sc))
    script_prs(sc.fakes, [pr_item(7)])
    script_comments(sc.fakes, PR7, one_comment())
    code, doc, _ = run(capsys, "check", config=sc.config)
    assert code == 0
    row = sc.round()
    assert (row["state"], row["failure_reason"], row["counted"]) == ("failed", "agent_failed", 1)
    assert sc.comment_states()[C1] == "unhandled" and sc.temp_files() == []
    assert sql(world, "SELECT json_extract(block, '$.reason') FROM prs") == [("failed",)]
    assert sql(world, "SELECT count(*) FROM leases WHERE key = ?", round_lease_key(sc.rid)) == [
        (0,)
    ]
    leases = [e for e in log_events(world) if e["event"] == "lease" and e["action"] == "taken_over"]
    assert [e["round"] for e in leases if "round" in e] == [sc.rid]
    # the lease: no second finalizer will act on the ended round
    assert [e["step"] for e in log_events(world) if e["event"] == "finalize"] == ["failed"]


def _failed_run(sc: Scene) -> dict[str, Any]:
    from tests.harness.spawn_run import outcome_doc

    doc = show_after(sc, status="parked")
    doc["runs"][0]["outcome"] = outcome_doc("failed")
    return doc


# ---- two finalizers race ------------------------------------------------------------------------


def spawn_cli(world: World, *argv: str, out: Path) -> subprocess.Popen[bytes]:
    handle = out.open("wb")
    proc = subprocess.Popen(
        [sys.executable, "-I", "-m", "review_respond", *argv],
        env=world.env,
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    handle.close()
    world.procs.record(proc.pid)
    return proc


@pytest.mark.real_detached
def test_two_finalizers_race_and_exactly_one_takes_over(
    world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = Jobs(world, "detached", monkeypatch)
    sc = start_round(world, capsys, jobs, one_comment())
    scripts_for_round(sc, live=True)  # the job's own last `show` finds the agent still busy
    read_comments_for_job(sc, one_comment())
    pid = jobs.start()
    eventually(lambda: gone(pid), what="the job to leave the round to the finalizer")
    jobs.reap()
    assert (sc.round()["state"], sc.round()["agent_id"]) == ("running", FORK)
    assert sql(world, "SELECT count(*) FROM leases WHERE key LIKE 'round:%'") == [(0,)]
    sc.fakes.script("agent-spawn", "show", json=_failed_run(sc), sleep=2.0)
    out = world.root / "out"
    out.mkdir()
    a = spawn_cli(world, "show", sc.rid, "--json", "--config", str(sc.config), out=out / "a")
    b = spawn_cli(world, "show", sc.rid, "--json", "--config", str(sc.config), out=out / "b")
    for proc in (a, b):
        assert proc.wait(timeout=90) == 0
    finals = sorted(json.loads((out / n).read_text())["finalize"] for n in ("a", "b"))
    assert "done" in finals and set(finals) <= {"busy", "done"}
    row = sc.round()
    assert (row["state"], row["failure_reason"]) == ("failed", "agent_failed")
    # exactly one process took the lease, asked the agent and moved the round
    asked = [c for c in sc.fakes.calls("agent-spawn", "show") if c.positionals["agent_id"] == FORK]
    assert len(asked) == 2  # the job's own, then the winner's: never a third
    moves = [e for e in log_events(world) if e["event"] == "round_state" and e["to"] == "failed"]
    assert len(moves) == 1
    assert [e["step"] for e in log_events(world) if e["event"] == "finalize"] == ["failed"]


# ---- the second round forks the first fork ------------------------------------------------------


@pytest.mark.real_detached
def test_a_second_round_forks_the_first_fork_with_no_extra_flags(
    world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance criterion 4 of #19, end to end: round 1 runs as a real job and its fork is
    recorded; once its record is confirmed (the lineage switch ``confirm`` will make) ``check``
    starts round 2 from that fork, and the job's ``resume`` names the first fork and nothing
    more than the session settings."""
    jobs = Jobs(world, "detached", monkeypatch)
    sc1 = start_round(world, capsys, jobs, one_comment())
    scripts_for_round(sc1)
    read_comments_for_job(sc1, one_comment())
    pid = jobs.start()
    eventually(lambda: gone(pid), what="the first job to end")
    jobs.reap()
    assert (sc1.round()["state"], sc1.round()["agent_id"]) == ("running", FORK)
    # what `confirm` does in its transaction: the record ends, the round is confirmed, the fork
    # becomes the base (TODO(#29), TODO(#31): the real record and confirm)
    store = Store.open(open_state_dir(sc1.state), write=True, close_state=True)
    try:
        lease = store.try_acquire(pr_lease_key(pr_digest(PR7)), heartbeat=None)
        assert lease is not None
        Rounds(lease).to_pending(sc1.rid)
        with lease.transaction() as conn:
            Rounds(lease).settle_pending(conn, sc1.rid, "confirmed")
            switched = lineage.switch_base(conn, sc1.rid, now=store.clock.now().timestamp())
        lease.release()
    finally:
        store.close()
    assert (switched.base, switched.retired) == (FORK, None)
    assert sql(world, "SELECT base_agent FROM prs") == [(FORK,)]
    # round 2: a new comment; check resolves the base (the first fork) and starts the round
    second = [
        builders.thread(
            "gh:rt:t2", [builders.comment(C2, "And this too.", created_at="2999-01-01T00:00:00Z")]
        )
    ]
    world.fakes.script(
        "agent-spawn", "show", json=show_doc(original(FORK, PR7, status="parked")),
        match={"positionals": {"agent_id": FORK}},
    )  # fmt: skip
    sc2 = next_round(world, capsys, jobs, sc1.config, second)
    assert sql(world, "SELECT base_agent_id FROM rounds WHERE id = ?", sc2.rid) == [(FORK,)]
    scripts_for_round(sc2, fork=FORK2, comment=C2)
    read_comments_for_job(sc2, second)
    pid2 = jobs.start()
    eventually(lambda: gone(pid2), what="the second job to end")
    jobs.reap()
    assert (sc2.round()["state"], sc2.round()["agent_id"]) == ("running", FORK2)
    (resume,) = [c for c in sc2.fakes.calls("agent-spawn", "resume")][-1:]
    assert resume.positionals == {"agent_id": FORK} and "--fork" in resume.argv
    flags = set(resume.flags)
    assert not flags & {"--allow-tainted", "--force", "--wait", "--worktree", "--repo"}
    assert resume.flags["--cwd"] == str(sc2.root) and resume.flags["--kind"] == "respond"
    assert sql(world, "SELECT agent_id, state, source, round FROM forks ORDER BY created_at") == [
        (FORK, "base", AGENT, sc1.rid),
        (FORK2, "round", FORK, sc2.rid),
    ]
