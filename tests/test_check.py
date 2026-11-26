"""`check` (steps 1 to 3 and `--pr`), `list` and `show` for PRs, end to end through the CLI entry
with the sibling fakes (docs/interface.md: check, list, show)."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from review_respond import check as check_module
from review_respond.cli import main
from review_respond.store import Store, open_state_dir, pass_lease_key, pr_digest, pr_lease_key
from tests.harness import builders, schemas
from tests.harness.builders import HOST, HOST_B, REVIEWER, pr_item, pr_ref
from tests.harness.fakes import Fakes
from tests.harness.plugin import World

LATER = "2999-01-01T00:00:00Z"  # after installed_at, so a comment is `unhandled`, not `seen`
OLD = 1_700_000_000.0  # a fixed stored time (epoch seconds) for deterministic goldens
PR7 = pr_ref(7)
PR8 = pr_ref(8)


# ---- helpers ---------------------------------------------------------------------------------


def write_config(world: World, **overrides: Any) -> Path:
    doc: dict[str, Any] = {
        "hosts_to_check": [HOST],
        "me": {"accounts": {HOST: "acct-me", HOST_B: "acct-me-b"}},
        "reviewers": [f"{HOST}:{REVIEWER['id']}", f"{HOST_B}:{REVIEWER['id']}"],
        "settle": "0s",
    }
    doc.update(overrides)
    path = world.root / "cfg" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_text(json.dumps(doc))
    path.chmod(0o600)
    return path


@pytest.fixture
def config(world: World) -> Path:
    return write_config(world)


def run(
    capsys: pytest.CaptureFixture[str], *argv: str, config: Path | None = None
) -> tuple[int, dict[str, Any], str]:
    full = [*argv, "--json"]
    if config is not None:
        full += ["--config", str(config)]
    code = main(full)
    out, err = capsys.readouterr()
    return code, json.loads(out) if out.strip() else {}, err


def script_prs(fakes: Fakes, items: list[dict[str, Any]], *, host: str = HOST, **kw: Any) -> None:
    fakes.script(
        "forgectl",
        "prs",
        json=builders.forgectl_prs(items, host=host),
        match={"flags": {"--host": host}},
        **kw,
    )


def script_comments(fakes: Fakes, pr: str, threads: list[dict[str, Any]], **kw: Any) -> None:
    fakes.script(
        "forgectl",
        "comments",
        json=builders.forgectl_comments(threads, pr=pr, **kw),
        match={"positionals": {"pr": pr}},
    )


def one_comment(
    cid: str = "gh:rc:c1", body: str = "Please rename this.", **kw: Any
) -> list[dict[str, Any]]:
    created = kw.pop("created_at", LATER)
    return [builders.thread("gh:rt:t1", [builders.comment(cid, body, created_at=created, **kw)])]


def db(world: World) -> sqlite3.Connection:
    return sqlite3.connect(Path(world.env["XDG_STATE_HOME"]) / "review-respond" / "state.db")


def sql(world: World, statement: str, *args: Any) -> list[tuple[Any, ...]]:
    conn = db(world)
    try:
        rows = conn.execute(statement, args).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def fix_times(world: World, *, block_at: float = OLD) -> None:
    """Replace the real local times in the state by fixed ones (deterministic goldens)."""
    sql(world, "UPDATE comments SET first_seen_at = ?, body_seen_at = ?, unhandled_since = ?", OLD,
        OLD, OLD)  # fmt: skip
    sql(world, "UPDATE prs SET block = json_set(block, '$.set_at', ?) WHERE block IS NOT NULL",
        block_at)  # fmt: skip


def set_agent(world: World, pr: str = PR7, agent: str = "agent-1") -> None:
    sql(world, "UPDATE prs SET base_agent = ? WHERE pr = ?", agent, pr)


def pr_of(doc: dict[str, Any], pr: str) -> dict[str, Any]:
    (found,) = [p for p in doc["prs"] if p["pr"] == pr]
    return found


def gold(golden: Any, name: str, doc: dict[str, Any]) -> None:
    """A golden file named `<command>[-variant]` against that command's schema."""
    command = name.split("-")[0]
    golden.check(f"cli/{name}", doc, f"review-respond/cli/{command}.json")


def valid(name: str, doc: dict[str, Any]) -> None:
    schemas.validate(f"review-respond/cli/{name}.json", doc)


def log_events(world: World) -> list[dict[str, Any]]:
    path = Path(world.env["XDG_STATE_HOME"]) / "review-respond" / "log.jsonl"
    if not path.exists():
        return []
    lines = [json.loads(line) for line in path.read_text().splitlines() if line]
    for line in lines:
        schemas.validate("review-respond/log.json", line)
    return lines


def one_pr_pass(world: World, capsys: pytest.CaptureFixture[str], config: Path, comments=None):  # type: ignore[no-untyped-def]
    script_prs(world.fakes, [pr_item(7)])
    script_comments(world.fakes, PR7, comments if comments is not None else one_comment())
    return run(capsys, "check", config=config)


# ---- check: the pass ---------------------------------------------------------------------------


def test_a_pass_reports_the_due_pr_and_starts_nothing(
    world: World, capsys: pytest.CaptureFixture[str], config: Path, golden: Any
) -> None:
    code, doc, _ = one_pr_pass(world, capsys, config)
    assert code == 0
    # no agent yet: the due rule stores the no_agent block (TODO(#19) resolve_base)
    assert pr_of(doc, PR7)["state"] == "waiting"
    assert sql(world, "SELECT json_extract(block, '$.reason') FROM prs") == [("no_agent",)]
    set_agent(world)
    script_prs(world.fakes, [pr_item(7)])
    script_comments(world.fakes, PR7, one_comment())
    code, doc, _ = run(capsys, "check", config=config)
    assert code == 0
    valid("check", doc)
    # the block is cleared by the agent being found; the PR is due: waiting for capacity
    assert pr_of(doc, PR7) == {
        "pr": PR7, "state": "waiting", "started_round": None, "lease_held": False, "errors": [],
    }  # fmt: skip
    assert sql(world, "SELECT block FROM prs") == [(None,)]
    gold(golden, "check-due", doc)
    # nothing was started: no sibling other than forgectl was called
    for tool in ("agent-spawn", "wtctl", "pr-comment", "jobsctl", "claude-session"):
        assert world.fakes.calls(tool) == []


def test_the_human_output_names_the_due_pr(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    one_pr_pass(world, capsys, config)
    set_agent(world)
    script_prs(world.fakes, [pr_item(7)])
    script_comments(world.fakes, PR7, one_comment())
    assert main(["check", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert f"{PR7} waiting DUE" in out and "1 PRs, 1 due" in out


def test_a_pr_inside_the_settle_window_is_settling(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(world, settle="1h")
    code, doc, _ = one_pr_pass(world, capsys, config)
    assert code == 0
    set_agent(world)
    script_prs(world.fakes, [pr_item(7)])
    script_comments(world.fakes, PR7, one_comment())
    _, doc, _ = run(capsys, "check", config=config)
    assert pr_of(doc, PR7)["state"] == "settling"


def test_a_pr_without_comments_is_idle(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    code, doc, _ = one_pr_pass(world, capsys, config, comments=[])
    assert code == 0 and pr_of(doc, PR7)["state"] == "idle"


def test_a_fork_pr_is_listed_idle_and_never_read(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    script_prs(world.fakes, [pr_item(7, is_fork=True, head_repo=f"{HOST}/other/repo")])
    code, doc, _ = run(capsys, "check", config=config)
    assert code == 0 and pr_of(doc, PR7)["state"] == "idle"
    assert world.fakes.calls("forgectl", "comments") == []


def test_an_ineligible_comment_never_makes_a_pr_due(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    mine = one_comment(author=builders.ME)
    code, doc, _ = one_pr_pass(world, capsys, config, comments=mine)
    assert code == 0 and pr_of(doc, PR7)["state"] == "idle"


def test_an_old_comment_is_seen_not_unhandled(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    code, doc, _ = one_pr_pass(world, capsys, config, comments=one_comment(created_at=builders.TS))
    assert code == 0 and pr_of(doc, PR7)["state"] == "idle"
    assert sql(world, "SELECT state FROM comments") == [("seen",)]


def test_a_deleted_comment_leaves_the_prompt_set_and_shows_deleted(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    one_pr_pass(world, capsys, config)
    set_agent(world)
    script_prs(world.fakes, [pr_item(7)])
    script_comments(world.fakes, PR7, [])
    code, doc, _ = run(capsys, "check", config=config)
    assert code == 0 and pr_of(doc, PR7)["state"] == "idle"
    assert sql(world, "SELECT state FROM comments") == [("deleted",)]
    _, shown, _ = run(capsys, "show", PR7, config=config)
    assert [c["state"] for c in shown["comments"]] == ["deleted"]


def test_an_edit_replaces_the_text_and_restarts_the_settle_clock(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    one_pr_pass(world, capsys, config)
    fix_times(world)
    script_prs(world.fakes, [pr_item(7)])
    script_comments(
        world.fakes, PR7, one_comment(body="Please rename it to bar.", updated_at=LATER)
    )
    code, _, _ = run(capsys, "check", config=config)
    assert code == 0
    ((settle, since),) = sql(world, "SELECT body_seen_at, unhandled_since FROM comments")
    assert settle > OLD and since == OLD  # an edit never moves unhandled_since
    _, shown, _ = run(capsys, "show", PR7, config=config)
    assert shown["comments"][0]["body"] == "Please rename it to bar."


def test_an_incomplete_read_leaves_the_records_stale_and_the_pr_not_due(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    script_prs(world.fakes, [pr_item(7)])
    script_comments(world.fakes, PR7, one_comment(), complete=False)
    code, doc, _ = run(capsys, "check", config=config)
    assert code == 0
    assert sql(world, "SELECT records_current, block FROM prs") == [(0, None)]
    assert pr_of(doc, PR7)["errors"] == []


def test_stale_records_are_reported_but_no_block_is_set(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    one_pr_pass(world, capsys, config)
    sql(world, "UPDATE prs SET records_current = 0, block = NULL")
    # the read fails: the records stay stale, nothing is set or cleared
    script_prs(world.fakes, [pr_item(7)])
    world.fakes.script(
        "forgectl", "comments", json=builders.error_envelope("rate_limited", "slow"), exit=12
    )
    code, doc, _ = run(capsys, "check", config=config)
    assert code == 0
    assert pr_of(doc, PR7)["errors"] == ["lower_failed"]
    assert sql(world, "SELECT block, records_current FROM prs") == [(None, 0)]


def test_a_pr_that_is_no_longer_listed_and_closed_is_reported_closed(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    one_pr_pass(world, capsys, config)
    script_prs(world.fakes, [])
    world.fakes.script(
        "forgectl", "show", json=builders.forgectl_show(pr=PR7, state="merged"),
        match={"positionals": {"pr": PR7}},
    )  # fmt: skip
    code, doc, _ = run(capsys, "check", config=config)
    assert code == 0 and pr_of(doc, PR7)["state"] == "closed"
    valid("check", doc)


# ---- hosts -------------------------------------------------------------------------------------


def test_one_failing_host_does_not_stop_the_other(
    world: World, capsys: pytest.CaptureFixture[str], golden: Any
) -> None:
    config = write_config(world, hosts_to_check=[HOST, HOST_B])
    world.fakes.script(
        "forgectl", "prs", json=builders.error_envelope("upstream_error", "boom"), exit=12,
        match={"flags": {"--host": HOST}},
    )  # fmt: skip
    script_prs(world.fakes, [pr_item(3, host=HOST_B)], host=HOST_B)
    script_comments(world.fakes, pr_ref(3, host=HOST_B), [])
    code, doc, _ = run(capsys, "check", config=config)
    assert code == 0
    assert doc["hosts"] == [
        {"host": HOST, "ok": False, "code": "upstream_error", "warnings": []},
        {"host": HOST_B, "ok": True, "code": None, "warnings": []},
    ]
    assert [p["pr"] for p in doc["prs"]] == [pr_ref(3, host=HOST_B)]
    gold(golden, "check-one-host-failed", doc)


def test_every_host_failing_exits_12_all_hosts_failed_with_the_result_object(
    world: World, capsys: pytest.CaptureFixture[str], golden: Any
) -> None:
    config = write_config(world, hosts_to_check=[HOST, HOST_B])
    for host in (HOST, HOST_B):
        world.fakes.script(
            "forgectl", "prs", json=builders.error_envelope("auth_required", "no"), exit=12,
            match={"flags": {"--host": host}},
        )  # fmt: skip
    code, doc, _ = run(capsys, "check", config=config)
    assert code == 12
    assert doc["error"]["code"] == "all_hosts_failed"
    assert [h["code"] for h in doc["hosts"]] == ["auth_required", "auth_required"]
    assert doc["prs"] == []
    gold(golden, "check-all-hosts-failed", doc)


def test_a_broken_host_among_working_ones_does_not_exit_12(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(world, hosts_to_check=[HOST, HOST_B])
    script_prs(world.fakes, [], host=HOST)
    world.fakes.script("forgectl", "prs", stdout="not json", schema=False,
                       match={"flags": {"--host": HOST_B}})  # fmt: skip
    code, doc, _ = run(capsys, "check", config=config)
    assert code == 0 and [h["ok"] for h in doc["hosts"]] == [True, False]


def test_no_hosts_to_check_is_a_clean_empty_pass(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(world, hosts_to_check=[])
    code, doc, _ = run(capsys, "check", config=config)
    assert code == 0 and doc["hosts"] == [] and doc["prs"] == []


def test_whoami_failing_outright_is_exit_12_lower_failed_with_the_result_object(
    world: World, capsys: pytest.CaptureFixture[str], golden: Any
) -> None:
    config = write_config(world)
    doc = json.loads(config.read_text())
    del doc["hosts_to_check"]
    config.write_text(json.dumps(doc))
    world.fakes.script(
        "forgectl", "whoami", json=builders.error_envelope("network_error", "down"), exit=1
    )
    code, out, _ = run(capsys, "check", config=config)
    assert code == 12
    assert out["error"]["code"] == "lower_failed"
    assert out["error"]["lower_error"]["code"] == "network_error"
    assert out["hosts"] == [] and out["prs"] == []
    gold(golden, "check-whoami-failed", out)


def test_the_hosts_default_to_the_ones_whoami_lists(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(world)
    doc = json.loads(config.read_text())
    del doc["hosts_to_check"]
    del doc["me"]
    config.write_text(json.dumps(doc))
    world.fakes.script(
        "forgectl", "whoami", json=builders.forgectl_whoami_hosts(HOST, ids={HOST: "acct-me"})
    )
    script_prs(world.fakes, [])
    code, out, _ = run(capsys, "check", config=config)
    assert code == 0 and [h["host"] for h in out["hosts"]] == [HOST]


def test_the_budget_defers_prs_and_says_so(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(world, limits={"requests_per_host": 2})
    script_prs(world.fakes, [pr_item(7), pr_item(8)])
    script_comments(world.fakes, PR7, [])
    code, doc, _ = run(capsys, "check", config=config)
    assert code == 0
    assert doc["hosts"][0]["warnings"] == ["budget_exhausted"]
    assert len(world.fakes.calls("forgectl", "comments")) == 1
    # both are still reported (the deferred one from its stored state); the deferred one is
    # attempted first on the next pass
    assert {p["pr"] for p in doc["prs"]} == {PR7, PR8}
    script_prs(world.fakes, [pr_item(7), pr_item(8)])
    script_comments(world.fakes, PR8, [])
    run(capsys, "check", config=config)
    assert [c.positionals["pr"] for c in world.fakes.calls("forgectl", "comments")] == [PR7, PR8]


def test_a_failed_read_is_a_per_pr_error_not_the_exit_code(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    script_prs(world.fakes, [pr_item(7)])
    world.fakes.script(
        "forgectl", "comments", json=builders.error_envelope("rate_limited", "x"), exit=12
    )
    code, doc, _ = run(capsys, "check", config=config)
    assert code == 0
    assert pr_of(doc, PR7)["errors"] == ["lower_failed"]
    assert doc["hosts"][0]["warnings"] == ["read_failed"]


def test_forgectl_missing_exits_12_tool_missing_before_anything(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    (world.fakes.bin_dir / "forgectl").unlink()
    code, doc, _ = run(capsys, "check", config=config)
    assert code == 12 and doc["error"]["code"] == "tool_missing"
    assert sql(world, "SELECT count(*) FROM leases") == [(0,)]  # the lease was let go


# ---- leases ------------------------------------------------------------------------------------


def hold(world: World, key: str) -> Store:
    store = Store.open(open_state_dir(), write=True, close_state=True)
    assert store.try_acquire(key, heartbeat=None) is not None
    return store


def test_a_held_pr_lease_is_reported_lease_held_and_the_pr_is_not_read(
    world: World, capsys: pytest.CaptureFixture[str], config: Path, golden: Any
) -> None:
    one_pr_pass(world, capsys, config)
    holder = hold(world, pr_lease_key(pr_digest(PR7)))
    try:
        script_prs(world.fakes, [pr_item(7)])
        code, doc, _ = run(capsys, "check", config=config)
    finally:
        holder.close()
    assert code == 0
    assert pr_of(doc, PR7)["lease_held"] is True
    assert len(world.fakes.calls("forgectl", "comments")) == 1  # only the first pass read it
    gold(golden, "check-lease-held", doc)


def test_the_pass_lease_held_by_a_live_process_is_already_running(
    world: World, capsys: pytest.CaptureFixture[str], config: Path, golden: Any
) -> None:
    holder = hold(world, "pass")
    try:
        code, doc, _ = run(capsys, "check", config=config)
    finally:
        holder.close()
    assert code == 0 and doc["already_running"] is True
    assert doc["prs"] == [] and doc["hosts"] == []
    assert world.fakes.calls("forgectl") == []
    gold(golden, "check-already-running", doc)
    events = log_events(world)
    assert [e["event"] for e in events if e["event"] == "check_pass"] == ["check_pass"]


def wait_for_call(fakes: Fakes, command: str, seconds: float = 30.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if fakes.calls("forgectl", command):
            return
        time.sleep(0.05)
    raise AssertionError(f"forgectl {command} was never called")


def start_check(world: World, config: Path, *extra: str) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        [sys.executable, "-m", "review_respond", "check", "--json", "--config", str(config)]
        + list(extra),
        env=world.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )  # fmt: skip
    world.procs.record(proc.pid)
    return proc


def test_two_concurrent_check_processes_one_gets_already_running(
    world: World, config: Path
) -> None:
    script_prs(world.fakes, [], sleep=3.0)
    first = start_check(world, config)
    wait_for_call(world.fakes, "prs")  # the first holds the pass lease now
    second = start_check(world, config)
    out2, err2 = second.communicate(timeout=60)
    out1, err1 = first.communicate(timeout=60)
    assert (first.returncode, second.returncode) == (0, 0), (err1, err2)
    d1, d2 = json.loads(out1), json.loads(out2)
    valid("check", d1)
    valid("check", d2)
    assert d1["already_running"] is False and d2["already_running"] is True
    assert len(world.fakes.calls("forgectl", "prs")) == 1


def test_check_pr_takes_the_same_pass_lease(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    holder = hold(world, "pass")
    try:
        code, doc, _ = run(capsys, "check", "--pr", PR7, config=config)
    finally:
        holder.close()
    assert code == 0 and doc["already_running"] is True
    assert world.fakes.calls("forgectl") == []


# ---- check --pr --------------------------------------------------------------------------------


def script_show(fakes: Fakes, pr: str = PR7, **kw: Any) -> None:
    fakes.script(
        "forgectl", "show", json=builders.forgectl_show(pr=pr, **kw),
        match={"positionals": {"pr": pr}},
    )  # fmt: skip


def test_check_pr_reads_one_pr_and_reports_it(
    world: World, capsys: pytest.CaptureFixture[str], config: Path, golden: Any
) -> None:
    script_show(world.fakes)
    script_comments(world.fakes, PR7, one_comment())
    code, doc, _ = run(capsys, "check", "--pr", PR7, config=config)
    assert code == 0
    assert doc["hosts"] == [{"host": HOST, "ok": True, "code": None, "warnings": []}]
    assert [p["pr"] for p in doc["prs"]] == [PR7]
    assert world.fakes.calls("forgectl", "prs") == []  # no listing
    assert sql(world, "SELECT value IS NOT NULL FROM meta WHERE key = 'installed_at'") == [(1,)]
    gold(golden, "check-pr", doc)


def test_check_pr_accepts_an_https_url(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    script_show(world.fakes)
    script_comments(world.fakes, PR7, [])
    url = f"https://{HOST}/org/repo/pull/7"
    code, doc, _ = run(capsys, "check", "--pr", url, config=config)
    assert code == 0 and doc["prs"][0]["pr"] == PR7


def test_check_pr_of_a_pr_that_is_not_yours_is_refused_in_prs(
    world: World, capsys: pytest.CaptureFixture[str], config: Path, golden: Any
) -> None:
    other = builders.forgectl_show()
    other["author"] = {"login": "someone", "id": "acct-other", "is_bot": False}
    world.fakes.script("forgectl", "show", json=other, match={"positionals": {"pr": PR7}})
    code, doc, _ = run(capsys, "check", "--pr", PR7, config=config)
    assert code == 0
    assert doc["prs"] == [
        {"pr": PR7, "state": "idle", "started_round": None, "lease_held": False,
         "errors": ["refused"]}
    ]  # fmt: skip
    assert sql(world, "SELECT count(*) FROM prs") == [(0,)]
    assert world.fakes.calls("forgectl", "comments") == []
    gold(golden, "check-pr-not-yours", doc)


def test_check_pr_with_an_unknown_account_is_all_hosts_failed_identity_unknown(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(world, me={"accounts": {}})
    script_show(world.fakes)
    world.fakes.script(
        "forgectl", "whoami", json=builders.forgectl_whoami_hosts(HOST, ids={HOST: None})
    )
    code, doc, _ = run(capsys, "check", "--pr", PR7, config=config)
    assert code == 12 and doc["error"]["code"] == "all_hosts_failed"
    assert doc["hosts"] == [{"host": HOST, "ok": False, "code": "identity_unknown", "warnings": []}]
    assert sql(world, "SELECT count(*) FROM prs") == [(0,)]


def test_check_pr_when_the_host_fails_is_exit_12_with_the_result_object(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    world.fakes.script(
        "forgectl", "show", json=builders.error_envelope("network_error", "down"), exit=1
    )
    code, doc, _ = run(capsys, "check", "--pr", PR7, config=config)
    assert code == 12 and doc["error"]["code"] == "all_hosts_failed"
    assert doc["hosts"][0]["code"] == "network_error"
    valid("check", doc)


def test_check_pr_when_the_host_fails_logs_host_failed(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    world.fakes.script(
        "forgectl", "show", json=builders.error_envelope("network_error", "down"), exit=1
    )
    run(capsys, "check", "--pr", PR7, config=config)
    assert [(e["host"], e["code"]) for e in log_events(world) if e["event"] == "host_failed"] == [
        (HOST, "network_error")
    ]


def test_check_pr_reports_the_whoami_failure_code_of_an_unknown_account(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(world, me={"accounts": {}})
    script_show(world.fakes)
    world.fakes.script(
        "forgectl", "whoami",
        json=builders.forgectl_whoami_hosts(
            HOST_B, errors=[builders.host_error(HOST, "auth_required")]
        ),
        exit=12, schema=False,
    )  # fmt: skip
    code, doc, _ = run(capsys, "check", "--pr", PR7, config=config)
    assert code == 12 and doc["error"]["code"] == "all_hosts_failed"
    assert doc["hosts"][0]["code"] == "auth_required"
    assert sql(world, "SELECT count(*) FROM prs") == [(0,)]


def test_check_pr_of_a_pr_the_host_does_not_know_is_exit_10_not_found(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    world.fakes.script(
        "forgectl", "show", json=builders.error_envelope("not_found", "no such PR"), exit=10
    )
    code, doc, _ = run(capsys, "check", "--pr", PR7, config=config)
    assert code == 10 and doc["error"]["code"] == "not_found"


def test_check_pr_of_a_closed_pr_is_closed_and_not_read(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    script_show(world.fakes, state="closed")
    code, doc, _ = run(capsys, "check", "--pr", PR7, config=config)
    assert code == 0 and doc["prs"][0]["state"] == "closed"
    assert world.fakes.calls("forgectl", "comments") == []


def test_check_pr_with_a_failing_read_reports_lower_failed(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    script_show(world.fakes)
    world.fakes.script(
        "forgectl", "comments", json=builders.error_envelope("rate_limited", "x"), exit=12
    )
    code, doc, _ = run(capsys, "check", "--pr", PR7, config=config)
    assert code == 0 and doc["prs"][0]["errors"] == ["lower_failed"]
    assert doc["hosts"] == [{"host": HOST, "ok": True, "code": None, "warnings": ["read_failed"]}]


def test_check_pr_with_a_held_pr_lease_is_lease_held(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    one_pr_pass(world, capsys, config)
    holder = hold(world, pr_lease_key(pr_digest(PR7)))
    try:
        code, doc, _ = run(capsys, "check", "--pr", PR7, config=config)
    finally:
        holder.close()
    assert code == 0 and doc["prs"][0]["lease_held"] is True


@pytest.mark.parametrize("bad", ["", "nope", "host/x", "http://h/o/r/pull/7", "a b#1", "h/o#0"])
def test_check_pr_refuses_a_malformed_reference(
    bad: str, world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    code, doc, _ = run(capsys, "check", "--pr", bad, config=config)
    assert code == 2 and doc["error"]["code"] == "usage"


# ---- dry run, logs -----------------------------------------------------------------------------


def test_dry_run_reads_the_stored_state_and_calls_nothing(
    world: World, capsys: pytest.CaptureFixture[str], config: Path, golden: Any
) -> None:
    one_pr_pass(world, capsys, config)
    before = len(log_events(world))
    code, doc, _ = run(capsys, "check", "--dry-run", config=config)
    assert code == 0 and doc["dry_run"] is True
    assert [p["pr"] for p in doc["prs"]] == [PR7]
    assert len(world.fakes.calls("forgectl")) == 3  # the first pass only (--version, prs, comments)
    assert len(log_events(world)) == before
    gold(golden, "check-dry-run", doc)


def test_dry_run_without_a_state_directory_creates_nothing(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    code, doc, _ = run(capsys, "check", "--dry-run", config=config)
    assert code == 0 and doc["prs"] == [] and doc["hosts"] == []
    assert not (Path(world.env["XDG_STATE_HOME"]) / "review-respond").exists()


def test_the_pass_logs_check_pass_and_pr_state_events(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    one_pr_pass(world, capsys, config)
    events = log_events(world)
    (passed,) = [e for e in events if e["event"] == "check_pass"]
    assert (
        passed["already_running"] is False and passed["prs"] == 1 and passed["rounds_started"] == 0
    )
    states = [e for e in events if e["event"] == "pr_state"]
    assert [(e["from"], e["to"]) for e in states] == [(None, "waiting")]


def test_a_failed_host_is_logged(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    world.fakes.script(
        "forgectl", "prs", json=builders.error_envelope("auth_required", "no"), exit=12
    )
    run(capsys, "check", config=config)
    assert [e["code"] for e in log_events(world) if e["event"] == "host_failed"] == [
        "auth_required"
    ]


# ---- a lost pass lease, PR lease heartbeat, tampered state ------------------------------------


def lose_the_pass_lease_inside_evaluate(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    original = check_module._Pass.evaluate  # pyright: ignore[reportPrivateUsage]

    def evaluate(self: Any, *args: Any, **kwargs: Any) -> Any:
        sql(world, "DELETE FROM leases WHERE key = ?", pass_lease_key())
        return original(self, *args, **kwargs)

    monkeypatch.setattr(check_module._Pass, "evaluate", evaluate)  # pyright: ignore


def test_a_pass_that_lost_its_lease_exits_1_lease_lost(
    world: World,
    capsys: pytest.CaptureFixture[str],
    config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_prs(world.fakes, [pr_item(7), pr_item(8)])
    script_comments(world.fakes, PR7, [])
    script_comments(world.fakes, PR8, [])
    lose_the_pass_lease_inside_evaluate(world, monkeypatch)
    code, doc, _ = run(capsys, "check", config=config)
    assert code == 1 and doc["error"]["code"] == "lease_lost"


def test_check_pr_that_lost_its_lease_exits_1_lease_lost(
    world: World,
    capsys: pytest.CaptureFixture[str],
    config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_show(world.fakes)
    script_comments(world.fakes, PR7, [])
    lose_the_pass_lease_inside_evaluate(world, monkeypatch)
    code, doc, _ = run(capsys, "check", "--pr", PR7, config=config)
    assert code == 1 and doc["error"]["code"] == "lease_lost"


def test_a_pr_lease_has_a_heartbeat_that_stops_on_release(
    world: World,
    capsys: pytest.CaptureFixture[str],
    config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []
    original = check_module._Pass.evaluate  # pyright: ignore[reportPrivateUsage]

    def evaluate(self: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append([t.name for t in threading.enumerate() if t.name.startswith("lease-pr:")])
        return original(self, *args, **kwargs)

    monkeypatch.setattr(check_module._Pass, "evaluate", evaluate)  # pyright: ignore
    one_pr_pass(world, capsys, config)
    assert len(seen) == 1 and len(seen[0]) == 1
    assert [t for t in threading.enumerate() if t.name.startswith("lease-")] == []


def test_a_tampered_body_file_met_during_a_pass_is_exit_12_state_unsafe_with_the_result(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    one_pr_pass(world, capsys, config)
    root = Path(world.env["XDG_STATE_HOME"]) / "review-respond"
    for path in [p for p in root.rglob("*") if p.is_file() and len(p.name) == 64]:
        path.write_text("tampered")
    script_prs(world.fakes, [pr_item(7)])
    # a new comment with the text of a stored one meets the tampered file
    script_comments(world.fakes, PR7, one_comment() + one_comment("gh:rc:c2"))
    code, doc, _ = run(capsys, "check", config=config)
    assert code == 12 and doc["error"]["code"] == "state_unsafe"
    assert doc["hosts"] == [] and doc["prs"] == []
    valid("check", doc)


@pytest.mark.parametrize(
    ("statement", "command"),
    [
        ("UPDATE prs SET updated_at = 'garbage'", "list"),
        ("UPDATE comments SET first_seen_at = 'x'", "show"),
        ("UPDATE comments SET first_seen_at = 1e300", "show"),
        ("UPDATE comments SET body_file = NULL", "show"),
    ],
)
def test_tampered_state_values_are_exit_12_state_unsafe(
    statement: str,
    command: str,
    world: World,
    capsys: pytest.CaptureFixture[str],
    config: Path,
) -> None:
    one_pr_pass(world, capsys, config)
    sql(world, statement)
    argv = ["list"] if command == "list" else ["show", PR7]
    code, doc, _ = run(capsys, *argv, config=config)
    assert code == 12 and doc["error"]["code"] == "state_unsafe"


# ---- list --------------------------------------------------------------------------------------


def two_prs(world: World, capsys: pytest.CaptureFixture[str], config: Path) -> None:
    """PR 7 with an eligible comment (block no_agent), PR 8 idle, PR 9 closed."""
    script_prs(world.fakes, [pr_item(7), pr_item(8)])
    script_comments(world.fakes, PR7, one_comment() + [
        builders.thread("gh:rt:t2", [builders.comment("gh:rc:c2", "mine", author=builders.ME,
                                                      created_at=LATER)])])  # fmt: skip
    script_comments(world.fakes, PR8, [])
    assert run(capsys, "check", config=config)[0] == 0
    fix_times(world)


def test_list_prints_the_state_and_the_counts_and_validates(
    world: World, capsys: pytest.CaptureFixture[str], config: Path, golden: Any
) -> None:
    two_prs(world, capsys, config)
    code, doc, _ = run(capsys, "list", config=config)
    assert code == 0
    valid("list", doc)
    seven = pr_of(doc, PR7)
    assert (seven["state"], seven["waiting_reason"]) == ("waiting", "no_agent")
    assert seven["block"] == {"reason": "no_agent", "set_at": "2023-11-14T22:13:20.000000Z"}
    assert (seven["unhandled"], seven["seen"], seven["ineligible"], seven["edited"]) == (1, 0, 1, 0)
    assert seven["rounds"] == 0 and seven["pending_id"] is None and seven["base_agent"] is None
    assert pr_of(doc, PR8)["state"] == "idle"
    gold(golden, "list", doc)


def test_list_filters_by_state_repo_and_waiting(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    two_prs(world, capsys, config)
    _, doc, _ = run(capsys, "list", "--state", "idle", config=config)
    assert [p["pr"] for p in doc["prs"]] == [PR8]
    _, doc, _ = run(capsys, "list", "--waiting", config=config)
    assert [p["pr"] for p in doc["prs"]] == [PR7]
    _, doc, _ = run(capsys, "list", "--waiting", "--state", "waiting", config=config)
    assert [p["pr"] for p in doc["prs"]] == [PR7]
    _, doc, _ = run(capsys, "list", "--repo", f"{HOST}/org/repo", config=config)
    assert len(doc["prs"]) == 2
    _, doc, _ = run(capsys, "list", "--repo", f"{HOST}/org/other", config=config)
    assert doc["prs"] == []
    valid("list", doc)


def test_list_waiting_conflicting_with_another_state_is_a_usage_error(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    code, doc, _ = run(capsys, "list", "--waiting", "--state", "idle", config=config)
    assert code == 2 and doc["error"]["code"] == "usage"


def test_list_refuses_a_malformed_repo_id(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    code, doc, _ = run(capsys, "list", "--repo", "../etc", config=config)
    assert code == 2


def test_list_on_an_empty_state_is_empty_and_human_readable(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    assert main(["list", "--config", str(config)]) == 0
    assert capsys.readouterr().out == "no pull requests\n"


def test_list_and_show_never_write_the_log_or_the_audit_file(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    two_prs(world, capsys, config)
    state = Path(world.env["XDG_STATE_HOME"]) / "review-respond"
    size = (state / "log.jsonl").stat().st_size
    run(capsys, "list", config=config)
    run(capsys, "show", PR7, config=config)
    assert (state / "log.jsonl").stat().st_size == size
    assert not (state / "audit.jsonl").exists() or (state / "audit.jsonl").stat().st_size == 0


def test_list_prints_a_closed_pr_and_a_fork_as_idle(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    script_prs(world.fakes, [pr_item(7, is_fork=True, head_repo=f"{HOST}/other/repo")])
    run(capsys, "check", config=config)
    sql(world, "INSERT INTO prs (pr, digest, host, repo, number, host_state, head_sha, head_ref,"
        " is_fork, base_ref, created_at, updated_at) VALUES (?, ?, ?, ?, 9, 'merged', ?, 'b', 0,"
        " 'main', ?, ?)", pr_ref(9), pr_digest(pr_ref(9)), HOST, f"{HOST}/org/repo",
        builders.HEAD_A, "2026-01-01T12:00:00+02:00", "2026-01-01T12:00:00+02:00")  # fmt: skip
    _, doc, _ = run(capsys, "list", config=config)
    assert pr_of(doc, PR7)["state"] == "idle" and pr_of(doc, pr_ref(9))["state"] == "closed"
    assert pr_of(doc, pr_ref(9))["updated"] == "2026-01-01T10:00:00.000000Z"
    valid("list", doc)


# ---- show --------------------------------------------------------------------------------------


def test_show_pr_lists_comments_including_the_ineligible_ones(
    world: World, capsys: pytest.CaptureFixture[str], config: Path, golden: Any
) -> None:
    two_prs(world, capsys, config)
    code, doc, _ = run(capsys, "show", PR7, config=config)
    assert code == 0
    valid("show", doc)
    assert doc["object_type"] == "pr" and doc["rounds"] == []
    by_id = {c["id"]: c for c in doc["comments"]}
    assert by_id["gh:rc:c1"]["eligible"] is True and by_id["gh:rc:c1"]["reason"] is None
    assert by_id["gh:rc:c2"]["eligible"] is False and by_id["gh:rc:c2"]["reason"] == "own"
    assert by_id["gh:rc:c1"]["body"] == "Please rename this."
    assert by_id["gh:rc:c1"]["first_seen_at"] == "2023-11-14T22:13:20.000000Z"
    gold(golden, "show-pr", doc)


def test_show_accepts_a_url(world: World, capsys: pytest.CaptureFixture[str], config: Path) -> None:
    two_prs(world, capsys, config)
    code, doc, _ = run(capsys, "show", f"https://{HOST}/org/repo/pull/7", config=config)
    assert code == 0 and doc["pr"]["pr"] == PR7


def test_show_of_a_reference_without_the_host_is_not_found(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    two_prs(world, capsys, config)
    code, doc, _ = run(capsys, "show", "org/repo#7", config=config)
    assert code == 10 and doc["error"]["code"] == "not_found"


def test_show_of_a_pr_on_a_host_named_like_an_id_prefix_is_a_pr(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    host = "rd-git.example.test"
    config = write_config(world, hosts_to_check=[host], me={"accounts": {host: "acct-me"}})
    ref = pr_ref(1, host=host)
    script_prs(world.fakes, [pr_item(1, host=host)], host=host)
    script_comments(world.fakes, ref, [])
    assert run(capsys, "check", config=config)[0] == 0
    code, doc, _ = run(capsys, "show", ref, config=config)
    assert code == 0 and doc["pr"]["pr"] == ref
    code, doc, _ = run(capsys, "show", pr_ref(2, host=host), config=config)
    assert code == 10 and doc["error"]["code"] == "not_found"


@pytest.mark.parametrize("ref", [pr_ref(99), "org/none#1", "rd-aaaaaaaa", "rp-bbbbbbbb"])
def test_show_of_nothing_is_exit_10_not_found(
    ref: str, world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    code, doc, _ = run(capsys, "show", ref, config=config)
    assert code == 10 and doc["error"]["code"] == "not_found"


@pytest.mark.parametrize("ref", ["rd-", "rd-A", "garbage", "http://x/y", "a\nb#1"])
def test_show_of_a_malformed_name_is_a_usage_error(
    ref: str, world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    code, doc, _ = run(capsys, "show", ref, config=config)
    assert code == 2 and doc["error"]["code"] == "usage"


def test_show_sanitizes_hostile_comment_text_and_stays_within_the_schema(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    hostile = "\x1b[2J\x00‮" * 9000 + "tail"
    script_prs(world.fakes, [pr_item(7)])
    script_comments(world.fakes, PR7, one_comment(body=hostile))
    run(capsys, "check", config=config)
    code, doc, _ = run(capsys, "show", PR7, config=config)
    assert code == 0
    valid("show", doc)
    body = doc["comments"][0]["body"]
    assert "\x1b" not in body and "\x00" not in body and "‮" not in body
    assert len(body) <= 65536
    assert main(["show", PR7, "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "‮" not in out


def test_a_tampered_stored_body_is_state_unsafe(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    one_pr_pass(world, capsys, config)
    root = Path(world.env["XDG_STATE_HOME"]) / "review-respond"
    stored = [p for p in root.rglob("*") if p.is_file() and len(p.name) == 64]
    assert stored
    for path in stored:
        path.write_text("tampered")
    code, doc, _ = run(capsys, "show", PR7, config=config)
    assert code == 12 and doc["error"]["code"] == "state_unsafe"


def test_show_on_a_newer_schema_still_works_read_only(
    world: World, capsys: pytest.CaptureFixture[str], config: Path
) -> None:
    one_pr_pass(world, capsys, config)
    sql(world, "PRAGMA user_version = 999")
    code, doc, _ = run(capsys, "show", PR7, config=config)
    assert code == 0
    code, doc, _ = run(capsys, "check", config=config)
    assert code == 12 and doc["error"]["code"] == "version_skew"
