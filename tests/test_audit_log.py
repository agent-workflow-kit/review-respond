"""audit.jsonl and log.jsonl writers (docs/interface.md: Audit and log)."""

from __future__ import annotations

import json
import os
import select
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from review_respond import store as store_mod
from review_respond.audit import AuditLog, JsonlFile, LogWriter, open_jsonl_files
from review_respond.cli import main
from review_respond.errors import CliError
from review_respond.store import StateDir, Store, open_state_dir
from tests.harness import schemas
from tests.harness.clock import FakeClock
from tests.harness.plugin import World

ROOT = Path(__file__).resolve().parent.parent
DEFS = json.loads((ROOT / "schemas" / "defs.json").read_text())["$defs"]
WORKER = Path(__file__).parent / "harness" / "append_worker.py"
LEASE_WORKER = Path(__file__).parent / "harness" / "lease_worker.py"
PR = "example.com/org/repo#12"
SHA = "a" * 40
SHA256 = "b" * 64


@pytest.fixture
def state(world: World) -> StateDir:
    return open_state_dir(world.root / "state" / "rr")


def records(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in path.read_text().split("\n")[:-1]:
        out.append(json.loads(line))
    return out


def audit_of(state: StateDir, **kw: Any) -> AuditLog:
    kw.setdefault("actor", "human:tester")
    kw.setdefault("clock", FakeClock())
    return AuditLog(JsonlFile(state, "audit.jsonl"), **kw)


def log_of(state: StateDir) -> LogWriter:
    return LogWriter(JsonlFile(state, "log.jsonl"), clock=FakeClock())


def check_all(path: Path, schema: str) -> list[dict[str, Any]]:
    found = records(path)
    for item in found:
        schemas.validate(schema, item)
    return found


# ---- audit records -----------------------------------------------------------------------


def test_audit_pair_shares_attempt_id_and_validates(state: StateDir, golden: Any) -> None:
    audit = audit_of(
        state, decision_ids=["d-1"], config_sha256=SHA256, override_escalation=True,
        actor_source="flag",
    )  # fmt: skip
    attempt = audit.attempted(
        "push", PR, inputs={"head_sha": SHA, "diff_sha256": SHA256, "pending_id": "rp-aaaaaaaa"}
    )
    audit.outcome(
        attempt, "push", PR,
        {"status": "ok", "exit_code": 0, "reason": None, "commit_sha": SHA},
        inputs={"head_sha": SHA, "commit_sha": SHA},
    )  # fmt: skip
    first, second = check_all(state.path / "audit.jsonl", "review-respond/audit.json")
    assert first["phase"] == "attempted" and first["result"] is None
    assert second["phase"] == "outcome"
    assert first["attempt_id"] == second["attempt_id"] == attempt
    assert len(attempt) == 22
    assert first["inputs"]["config_sha256"] == SHA256
    assert first["inputs"]["override_escalation"] is True
    golden.check("audit/attempted", {**first, "attempt_id": "A" * 22}, "review-respond/audit.json")
    golden.check("audit/outcome", {**second, "attempt_id": "A" * 22}, "review-respond/audit.json")


def test_attempt_ids_are_random(state: StateDir) -> None:
    audit = audit_of(state)
    ids = {audit.attempted("reply", PR) for _ in range(50)}
    assert len(ids) == 50


def test_human_decision_is_one_outcome_record(state: StateDir) -> None:
    audit = audit_of(state, decision_ids=["a", "b"])
    audit.decision("reject", PR, {"status": "ok"}, inputs={"pending_id": "rp-aaaaaaaa"})
    (only,) = check_all(state.path / "audit.jsonl", "review-respond/audit.json")
    assert only["phase"] == "outcome"
    assert only["decision_id"] == ["a", "b"]
    assert "override_escalation" not in only["inputs"]


@pytest.mark.parametrize(
    ("ids", "expected"), [((), None), (("one",), "one"), (("a", "b"), ["a", "b"])]
)
def test_decision_id_forms(state: StateDir, ids: Sequence[str], expected: Any) -> None:
    audit_of(state, decision_ids=ids).decision("stop", PR, {"status": "ok"})
    (only,) = check_all(state.path / "audit.jsonl", "review-respond/audit.json")
    assert only["decision_id"] == expected


def test_decision_ids_merge_and_dedupe(state: StateDir) -> None:
    audit = audit_of(state, decision_ids=["a"])
    audit.decision("confirm", PR, {"status": "ok"}, decision_ids=["a", "hook-1"])
    (only,) = records(state.path / "audit.jsonl")
    assert only["decision_id"] == ["a", "hook-1"]


def test_timestamp_is_utc_microseconds(state: StateDir) -> None:
    clock = FakeClock(datetime(2026, 3, 4, 5, 6, 7, 89, tzinfo=UTC))
    audit = AuditLog(JsonlFile(state, "audit.jsonl"), actor="auto:x", clock=clock)
    audit.decision("stop", PR, {"status": "noop"})
    (only,) = records(state.path / "audit.jsonl")
    assert only["ts"] == "2026-03-04T05:06:07.000089Z"


@pytest.mark.parametrize(
    "bad",
    [
        {"action": "nuke"},
        {"target": "not a pr"},
        {"target": PR + "\n"},
        {"target": "../x#1"},
        {"inputs": {"head_sha": "A" * 40}},
        {"inputs": {"diff_sha256": "abc"}},
        {"inputs": {"pending_id": "rp-AAAAAAAA"}},
        {"inputs": {"comment_id": "gh:rc:x\nforged"}},
        {"inputs": {"comment_id": "é"}},
        {"inputs": {"unknown": 1}},
        {"inputs": {"override_escalation": "yes"}},
        {"inputs": {"actor_source": "root"}},
        {"result": {"status": "great"}},
        {"result": {}},
        {"result": {"status": "ok", "extra": 1}},
        {"result": {"status": "ok", "exit_code": "0"}},
        {"result": {"status": "ok", "exit_code": True}},
        {"result": {"status": "refused", "reason": "because"}},
        {"result": {"status": "failed", "lower_code": "Bad Code"}},
        {"result": {"status": "ok", "commit_sha": "zz"}},
    ],
)
def test_hostile_audit_fields_are_refused_and_nothing_is_written(
    state: StateDir, bad: dict[str, Any]
) -> None:
    audit = audit_of(state)
    args: dict[str, Any] = {"action": "reply", "target": PR, "result": {"status": "ok"}}
    args.update(bad)
    with pytest.raises(ValueError):
        audit.decision(args["action"], args["target"], args["result"], inputs=args.get("inputs"))
    path = state.path / "audit.jsonl"
    assert not path.exists() or path.read_bytes() == b""


@pytest.mark.parametrize("actor", ["root", "human:", "human:a b", "human:x\n", "auto:" + "a" * 65])
def test_hostile_actor_refused(state: StateDir, actor: str) -> None:
    with pytest.raises(ValueError):
        audit_of(state, actor=actor)


def test_too_many_decision_ids_refused(state: StateDir) -> None:
    with pytest.raises(ValueError):
        audit_of(state, decision_ids=[f"d{i}" for i in range(17)])


def test_outcome_requires_a_valid_attempt_id(state: StateDir) -> None:
    with pytest.raises(ValueError):
        audit_of(state).outcome("short", "reply", PR, {"status": "ok"})


def test_failed_first_append_raises_and_leaves_no_partial_line(state: StateDir) -> None:
    calls: list[int] = []

    def short_then_fail(fd: int, data: bytes) -> int:
        calls.append(len(data))
        if len(calls) == 1:
            return os.write(fd, data[:10])
        raise OSError(28, "No space left on device")

    audit = AuditLog(
        JsonlFile(state, "audit.jsonl", write=short_then_fail), actor="human:t", clock=FakeClock()
    )
    with pytest.raises(CliError) as caught:
        audit.attempted("push", PR)
    assert caught.value.code == "internal"
    assert (state.path / "audit.jsonl").read_bytes() == b""


def test_unterminated_last_line_is_not_extended(state: StateDir) -> None:
    path = state.path / "audit.jsonl"
    path.write_bytes(b'{"half":')
    path.chmod(0o600)
    audit_of(state).decision("stop", PR, {"status": "ok"})
    lines = path.read_bytes().split(b"\n")
    assert lines[0] == b'{"half":'
    assert json.loads(lines[1])["action"] == "stop"
    assert lines[2] == b""


# ---- the files ---------------------------------------------------------------------------


@pytest.mark.parametrize("umask", [0o022, 0o000, 0o777])
def test_files_are_created_0600_whatever_the_umask(state: StateDir, umask: int) -> None:
    old = os.umask(umask)
    try:
        log_of(state).warning("probe")
        audit_of(state).decision("stop", PR, {"status": "ok"})
    finally:
        os.umask(old)
    for name in ("log.jsonl", "audit.jsonl"):
        assert stat.S_IMODE((state.path / name).stat().st_mode) == 0o600


def test_nothing_is_created_until_the_first_append(state: StateDir) -> None:
    log_of(state)
    audit_of(state)
    assert not (state.path / "log.jsonl").exists()
    assert not (state.path / "audit.jsonl").exists()


def test_existing_file_is_appended_to_not_truncated(state: StateDir) -> None:
    log_of(state).warning("one")
    log_of(state).warning("two")
    assert [r["code"] for r in records(state.path / "log.jsonl")] == ["one", "two"]


def _plant(state: StateDir, kind: str) -> None:
    path = state.path / "log.jsonl"
    other = state.path.parent / "elsewhere"
    if kind == "symlink":
        other.write_text("")
        path.symlink_to(other)
    elif kind == "dangling_symlink":
        path.symlink_to(state.path.parent / "nowhere")
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind == "directory":
        path.mkdir(mode=0o700)
    elif kind == "group_readable":
        path.write_text("")
        path.chmod(0o640)
    elif kind == "hard_link":
        other.write_text("")
        other.chmod(0o600)
        os.link(other, path)
    else:
        raise AssertionError(kind)


@pytest.mark.parametrize(
    "kind", ["symlink", "dangling_symlink", "fifo", "directory", "group_readable", "hard_link"]
)
def test_unsafe_existing_files_are_refused_without_hanging(state: StateDir, kind: str) -> None:
    _plant(state, kind)
    with pytest.raises(CliError) as caught:
        JsonlFile(state, "log.jsonl")
    assert caught.value.code == "state_unsafe"


def test_unsafe_file_planted_after_the_check_is_refused_at_open(state: StateDir) -> None:
    file = JsonlFile(state, "log.jsonl")
    _plant(state, "symlink")
    with pytest.raises(CliError) as caught:
        LogWriter(file, clock=FakeClock()).warning("probe")
    assert caught.value.code == "state_unsafe"
    assert (state.path.parent / "elsewhere").read_bytes() == b""


def test_a_file_owned_by_another_user_is_refused(state: StateDir) -> None:
    (state.path / "log.jsonl").write_text("")
    (state.path / "log.jsonl").chmod(0o600)
    foreign = StateDir(state.path, state.euid + 1, os.dup(state.fd))
    with pytest.raises(CliError) as caught:
        JsonlFile(foreign, "log.jsonl")
    assert caught.value.code == "state_unsafe"
    foreign.close()


def test_unsafe_mode_is_not_repaired(state: StateDir) -> None:
    _plant(state, "group_readable")
    with pytest.raises(CliError):
        JsonlFile(state, "log.jsonl")
    assert stat.S_IMODE((state.path / "log.jsonl").stat().st_mode) == 0o640


def test_open_jsonl_files_checks_both(state: StateDir) -> None:
    (state.path / "audit.jsonl").symlink_to(state.path.parent / "nowhere")
    with pytest.raises(CliError):
        open_jsonl_files(state)


# ---- log events --------------------------------------------------------------------------

LOG_CASES: list[tuple[str, dict[str, Any]]] = [
    ("check_pass", {"started_at": "2026-01-01T12:00:00Z", "finished_at": "2026-01-01T12:00:01Z",
                    "already_running": False, "prs": 3, "rounds_started": 1, "errors": 0}),
    ("pr_state", {"pr": PR, "from": None, "to": "idle", "reason": "first_seen"}),
    ("round_state", {"round": "rd-aaaaaaaa", "from": "queued", "to": "running", "reason": "go"}),
    ("pending_state", {"pending": "rp-aaaaaaaa", "from": "pending", "to": "pushing",
                       "reason": "x"}),
    ("hook_result", {"hook": "pre-push", "object_id": "rp-aaaaaaaa", "exit_code": 13,
                     "duration_ms": 5, "timed_out": False, "async": False, "vetoed": False,
                     "decision_ids": ["d1"]}),
    ("finalize", {"round": "rd-aaaaaaaa", "step": "teardown"}),
    ("host_failed", {"host": "example.com", "code": "timeout"}),
    ("lease", {"kind": "round", "action": "acquired", "round": "rd-aaaaaaaa"}),
    ("warning", {"code": "sandbox_weakened", "key": "sandbox", "pr": PR}),
]  # fmt: skip


@pytest.mark.parametrize(("event", "fields"), LOG_CASES, ids=[c[0] for c in LOG_CASES])
def test_every_event_validates_and_matches_golden(
    state: StateDir, golden: Any, event: str, fields: dict[str, Any]
) -> None:
    log_of(state).emit(event, fields, level="warn")
    (only,) = check_all(state.path / "log.jsonl", "review-respond/log.json")
    assert only["event"] == event and only["level"] == "warn"
    golden.check(f"log/{event}", only, "review-respond/log.json")


def test_event_accepts_datetimes_for_timestamps(state: StateDir) -> None:
    when = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    log_of(state).emit(
        "check_pass", {"started_at": when, "finished_at": when, "already_running": True}
    )
    (only,) = check_all(state.path / "log.jsonl", "review-respond/log.json")
    assert only["started_at"] == "2026-01-01T12:00:00.000000Z"


@pytest.mark.parametrize(
    ("event", "fields", "level"),
    [
        ("nope", {}, "info"),
        ("finalize", {"round": "rd-aaaaaaaa", "step": "teardown"}, "loud"),
        ("finalize", {"round": "rd-aaaaaaaa"}, "info"),
        ("finalize", {"round": "rd-aaaaaaaa", "step": "teardown", "text": "hi"}, "info"),
        ("finalize", {"round": "rd-AAAAAAAA", "step": "teardown"}, "info"),
        ("finalize", {"round": "rd-aaaaaaaa", "step": "other"}, "info"),
        ("pr_state", {"pr": PR, "from": "bogus", "to": "idle", "reason": "x"}, "info"),
        ("pr_state", {"pr": PR, "to": "idle", "reason": "x"}, "info"),
        ("hook_result", {"hook": "pre-push", "exit_code": 0, "timed_out": 0}, "info"),
        ("hook_result", {"hook": "pre-push", "exit_code": 0, "timed_out": False,
                         "duration_ms": -1}, "info"),
        ("hook_result", {"hook": "pre-push", "exit_code": 0, "timed_out": False,
                         "object_id": "a b"}, "info"),
        ("check_pass", {"started_at": "yesterday", "finished_at": "2026-01-01T12:00:00Z",
                        "already_running": False}, "info"),
        ("check_pass", {"started_at": "2026-01-01T12:00:00Z", "finished_at": "2026-01-01T12:00:00Z",
                        "already_running": False, "prs": -1}, "info"),
        ("host_failed", {"host": "EXAMPLE.com\n", "code": "x"}, "info"),
        ("warning", {"code": "Bad"}, "info"),
        ("warning", {"code": "x", "key": 5}, "info"),
        ("lease", {"kind": "round", "action": "stolen"}, "info"),
    ],
)  # fmt: skip
def test_hostile_events_are_refused(
    state: StateDir, event: str, fields: dict[str, Any], level: str
) -> None:
    with pytest.raises(ValueError):
        log_of(state).emit(event, fields, level=level)
    assert not (state.path / "log.jsonl").exists()


def test_warning_key_is_sanitized_and_truncated(state: StateDir) -> None:
    hostile = "env\x1b[31m‮⁦\x00\ud800 " + "k" * 600
    log_of(state).warning("env_ignored", key=hostile)
    raw = (state.path / "log.jsonl").read_bytes()
    assert raw.count(b"\n") == 1 and raw.endswith(b"\n")
    assert all(b < 0x7F and (b >= 0x20 or b == 0x0A) for b in raw)
    (only,) = check_all(state.path / "log.jsonl", "review-respond/log.json")
    assert len(only["key"]) <= 256
    assert "\x1b" not in only["key"] and "‮" not in only["key"]
    assert only["key"].startswith("env<U+001B>[31m<U+202E><U+2066><U+0000><U+D800><U+2028>")


def test_a_failing_log_append_warns_once_and_does_not_raise(
    state: StateDir, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(fd: int, data: bytes) -> int:
        raise OSError(28, "No space left on device")

    log = LogWriter(JsonlFile(state, "log.jsonl", write=fail), clock=FakeClock())
    assert log.warning("one") is False
    assert log.warning("two") is False
    assert capsys.readouterr().err.count("log.jsonl") == 1


def test_closed_writer_does_not_raise(state: StateDir) -> None:
    log = log_of(state)
    log.close()
    assert log.warning("late") is False


# ---- lease events ------------------------------------------------------------------------


def test_lease_callback_maps_keys(state: StateDir) -> None:
    callback = log_of(state).lease_callback()
    callback("taken_over", "pass")
    callback("stalled", "pr:" + "0" * 20)
    callback("lost", "round:rd-aaaaaaaa")
    found = check_all(state.path / "log.jsonl", "review-respond/log.json")
    assert [(r["kind"], r["action"], r["level"]) for r in found] == [
        ("pass", "taken_over", "warn"),
        ("pr", "stalled", "warn"),
        ("round", "lost", "error"),
    ]
    assert found[2]["round"] == "rd-aaaaaaaa"
    assert "pr" not in found[1]


@pytest.mark.parametrize(
    ("action", "key"), [("stolen", "pass"), ("lost", "other:x"), ("lost", "pass\n")]
)
def test_lease_callback_refuses_unknown_input(state: StateDir, action: str, key: str) -> None:
    with pytest.raises(ValueError):
        log_of(state).lease_callback()(action, key)


def test_lease_callback_is_thread_safe(state: StateDir) -> None:
    callback = log_of(state).lease_callback()
    errors: list[BaseException] = []

    def work(n: int) -> None:
        try:
            for i in range(40):
                callback("lost", f"round:rd-{'a' * 7}{'abcdefg'[(n + i) % 7]}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    assert len(check_all(state.path / "log.jsonl", "review-respond/log.json")) == 320


def test_real_takeover_of_a_dead_holder_is_logged(world: World, state: StateDir) -> None:
    go = world.root / "go"
    go.write_text("")
    proc = subprocess.Popen(
        [sys.executable, "-I", str(LEASE_WORKER), "acquire", str(state.path), str(go), "pass"],
        env=world.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True,
    )  # fmt: skip
    world.procs.record(proc.pid)
    assert proc.stdout is not None
    deadline = time.monotonic() + 20
    seen = b""
    while b"ACQUIRED" not in seen and time.monotonic() < deadline:
        ready, _, _ = select.select([proc.stdout], [], [], 1)
        if ready:
            seen += os.read(proc.stdout.fileno(), 4096)
    assert b"ACQUIRED" in seen
    proc.kill()
    proc.wait(timeout=20)
    store = Store.open(open_state_dir(state.path), write=True, close_state=True)
    try:
        lease = store.try_acquire("pass", heartbeat=None, on_event=log_of(state).lease_callback())
        assert lease is not None
        lease.release()
    finally:
        store.close()
    (only,) = check_all(state.path / "log.jsonl", "review-respond/log.json")
    assert (only["kind"], only["action"]) == ("pass", "taken_over")


# ---- concurrency -------------------------------------------------------------------------


def _run_workers(world: World, state: StateDir, kind: str, workers: int, count: int) -> list[int]:
    go = world.root / f"go-{kind}"
    procs = []
    for n in range(workers):
        proc = subprocess.Popen(
            [sys.executable, "-I", str(WORKER), kind, str(state.path), str(go), f"w{n}",
             str(count)],
            env=world.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )  # fmt: skip
        world.procs.record(proc.pid)
        procs.append(proc)
    for proc in procs:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == b"READY", proc.stderr and proc.stderr.read()
    go.write_text("")
    for proc in procs:
        assert proc.wait(timeout=60) == 0, proc.stderr and proc.stderr.read()
    return [p.pid for p in procs]


def test_concurrent_processes_never_tear_log_lines(world: World, state: StateDir) -> None:
    pids = _run_workers(world, state, "log", workers=6, count=60)
    assert len(set(pids)) == 6
    found = check_all(state.path / "log.jsonl", "review-respond/log.json")
    keys = sorted(r["key"] for r in found)
    expected = sorted(f"w{n}-{i}-{'x' * 200}"[:256] for n in range(6) for i in range(60))
    assert keys == expected


def test_concurrent_processes_never_tear_audit_lines(world: World, state: StateDir) -> None:
    _run_workers(world, state, "audit", workers=5, count=40)
    found = check_all(state.path / "audit.jsonl", "review-respond/audit.json")
    assert len(found) == 5 * 40 * 2
    by_attempt: dict[str, list[dict[str, Any]]] = {}
    for item in found:
        by_attempt.setdefault(item["attempt_id"], []).append(item)
    assert len(by_attempt) == 200
    for pair in by_attempt.values():
        assert sorted(p["phase"] for p in pair) == ["attempted", "outcome"]
    comments = sorted(p["result"]["comment_id"] for p in found if p["phase"] == "outcome")
    assert comments == sorted(f"w{n}-{i}" for n in range(5) for i in range(40))


# ---- the command line --------------------------------------------------------------------


def _state_path(world: World) -> Path:
    return store_mod.state_dir_path(world.env)


def test_config_warnings_are_logged_even_under_quiet(
    world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REVIEW_RESPOND_NOT_A_THING", "1")
    assert main(["reject", "rp-aaaaaaaa", "--quiet"]) == 1  # no handler yet: internal
    found = check_all(_state_path(world) / "log.jsonl", "review-respond/log.json")
    assert [(r["event"], r["code"], r["level"]) for r in found] == [
        ("warning", "env_ignored", "warn")
    ]
    assert found[0]["key"] == "REVIEW_RESPOND_NOT_A_THING"
    assert "comment" not in json.dumps(found)
    capsys.readouterr()


def test_dry_run_and_validate_write_no_log(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_RESPOND_NOT_A_THING", "1")
    main(["check", "--dry-run"])
    main(["validate"])
    assert not _state_path(world).exists()


def test_read_only_commands_do_not_write_the_log(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REVIEW_RESPOND_NOT_A_THING", "1")
    main(["list"])
    assert not (_state_path(world) / "log.jsonl").exists()


def test_unsafe_log_file_refuses_the_command_before_the_config_is_read(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = open_state_dir(_state_path(world))
    (state.path / "log.jsonl").symlink_to(tmp_path / "nowhere")
    state.close()
    assert main(["check", "--config", "/definitely/not/there.yaml"]) == 12
    assert "state_unsafe" in capsys.readouterr().err


# ---- drift from the schemas --------------------------------------------------------------


def test_patterns_and_enums_match_the_schemas() -> None:
    from review_respond import audit as mod

    for const, name in [
        ("_HOST", "host"), ("_PR", "pr"), ("_SHA", "sha"), ("_SHA256", "sha256"), ("_TS", "ts"),
        ("_PENDING", "pending_id"), ("_ROUND", "round_id"), ("_ACTOR", "actor"),
        ("_DECISION", "decision_id"), ("_COMMENT", "comment_id"),
    ]:  # fmt: skip
        assert getattr(mod, const) == DEFS[name]["pattern"], name
    for const, name in [
        ("PR_STATES", "pr_state_name"), ("ROUND_STATES", "round_state"),
        ("PENDING_STATES", "pending_state"), ("HOOKS", "hook_name"),
        ("REFUSAL_REASONS", "error_reason"),
    ]:  # fmt: skip
        assert list(getattr(mod, const)) == DEFS[name]["enum"], name
    audit = json.loads((ROOT / "schemas" / "audit.json").read_text())
    log = json.loads((ROOT / "schemas" / "log.json").read_text())
    assert list(mod.AUDIT_ACTIONS) == audit["properties"]["action"]["enum"]
    input_props = audit["properties"]["inputs"]["properties"]
    assert list(mod.ACTOR_SOURCES) == input_props["actor_source"]["enum"]
    assert set(mod._INPUT_CHECKS) == set(audit["properties"]["inputs"]["properties"])
    assert set(mod._RESULT_CHECKS) | {"status"} == set(
        audit["properties"]["result"]["oneOf"][0]["properties"]
    )
    events = {b["properties"]["event"]["const"]: b for b in log["oneOf"]}
    assert set(mod._EVENTS) == set(events)
    for event, (required, optional) in mod._EVENTS.items():
        props = set(events[event]["properties"]) - {"ts", "level", "event"}
        assert set(required) | set(optional) == props, event
        assert set(required) == set(events[event]["required"]) - {"ts", "level", "event"}, event


# ---- round 1 review fixes ----------------------------------------------------------------


def _first_append(state: StateDir) -> JsonlFile:
    file = JsonlFile(state, "log.jsonl")
    file.append({"n": 1})
    return file


def _swap_unlink(state: StateDir) -> None:
    (state.path / "log.jsonl").unlink()


def _swap_rename(state: StateDir) -> None:
    (state.path / "log.jsonl").rename(state.path / "moved.jsonl")


def _swap_symlink(state: StateDir) -> None:
    target = state.path.parent / "elsewhere"
    target.write_text("")
    (state.path / "log.jsonl").unlink()
    (state.path / "log.jsonl").symlink_to(target)


def _swap_replace(state: StateDir) -> None:
    path = state.path / "log.jsonl"
    path.unlink()
    path.write_text("")
    path.chmod(0o600)


def _swap_chmod(state: StateDir) -> None:
    (state.path / "log.jsonl").chmod(0o666)


def _swap_hard_link(state: StateDir) -> None:
    os.link(state.path / "log.jsonl", state.path / "alias.jsonl")


@pytest.mark.parametrize(
    "swap",
    [_swap_unlink, _swap_rename, _swap_symlink, _swap_replace, _swap_chmod, _swap_hard_link],
)
def test_a_file_changed_while_open_is_refused_on_the_next_append(
    state: StateDir, swap: Any
) -> None:
    file = _first_append(state)
    swap(state)
    with pytest.raises(CliError) as caught:
        file.append({"n": 2})
    assert caught.value.code == "state_unsafe"
    assert (
        not (state.path.parent / "elsewhere").exists()
        or (state.path.parent / "elsewhere").read_bytes() == b""
    )


def test_changed_file_is_refused_through_audit_and_log_writers(state: StateDir) -> None:
    audit = audit_of(state)
    audit.decision("stop", PR, {"status": "ok"})
    _swap_unlink_audit = state.path / "audit.jsonl"
    _swap_unlink_audit.unlink()
    with pytest.raises(CliError) as caught:
        audit.decision("stop", PR, {"status": "ok"})
    assert caught.value.code == "state_unsafe"
    log = log_of(state)
    log.warning("one")
    (state.path / "log.jsonl").unlink()
    with pytest.raises(CliError) as caught:
        log.warning("two")
    assert caught.value.code == "state_unsafe"


@pytest.mark.parametrize("key", ["round:abc", "round:rd-ABCDEFGH", "round:rd-aaaaaaa1"])
@pytest.mark.parametrize("action", ["taken_over", "stalled", "lost"])
def test_lease_callback_is_total_for_round_keys_the_store_accepts(
    state: StateDir, key: str, action: str
) -> None:
    log_of(state).lease_callback()(action, key)
    (only,) = check_all(state.path / "log.jsonl", "review-respond/log.json")
    assert (only["kind"], only["action"]) == ("round", action)
    assert "round" not in only


def test_failed_first_audit_append_prevents_the_external_action(state: StateDir) -> None:
    def fail(fd: int, data: bytes) -> int:
        raise OSError(28, "No space left on device")

    actions: list[str] = []

    def act(audit: AuditLog) -> None:
        audit.attempted("push", PR)
        actions.append("pushed")

    broken = AuditLog(
        JsonlFile(state, "audit.jsonl", write=fail), actor="human:t", clock=FakeClock()
    )
    with pytest.raises(CliError):
        act(broken)
    assert actions == []
    act(audit_of(state))
    assert actions == ["pushed"]


SENTINEL = "SENTINEL-COMMENT-BODY-9f3"


def test_no_fixture_comment_text_reaches_either_file(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(f"REVIEW_RESPOND_{SENTINEL.replace('-', '_')}", "1")
    state = open_state_dir(_state_path(world))
    audit = audit_of(state)
    attempt = audit.attempted("reply", PR, inputs={"comment_id": "gh:rc:1"})
    audit.outcome(attempt, "reply", PR, {"status": "ok", "comment_id": "gh:rc:2"})
    for bad in (
        lambda: audit.decision("reply", PR, {"status": "ok", "comment_id": SENTINEL + "\n x"}),
        lambda: audit.decision("reply", SENTINEL, {"status": "ok"}),
        lambda: audit.decision("reply", PR, {"status": "ok"}, inputs={"head_sha": SENTINEL}),
    ):
        with pytest.raises(ValueError) as caught:
            bad()
        assert SENTINEL not in str(caught.value)
    log_of(state).warning("env_ignored", key="REVIEW_RESPOND_NOT_A_THING")
    main(["reject", "rp-aaaaaaaa", "--quiet"])
    for name in ("audit.jsonl", "log.jsonl"):
        assert SENTINEL.encode() not in (state.path / name).read_bytes()


def test_flock_failure_never_truncates_another_writers_line(
    state: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    path = state.path / "log.jsonl"

    def no_lock(fd: int, op: int) -> None:
        raise OSError(errno_enolck, "no locks")

    errno_enolck = 37
    other: list[int] = []

    def other_then_fail(fd: int, data: bytes) -> int:
        other.append(os.open(path, os.O_WRONLY | os.O_APPEND))
        os.write(other[0], b'{"other":1}\n')
        raise OSError(28, "No space left on device")

    file = JsonlFile(state, "log.jsonl", write=other_then_fail)
    monkeypatch.setattr(fcntl, "flock", no_lock)
    with pytest.raises(OSError):
        file.append({"n": 1})
    os.close(other[0])
    assert path.read_bytes() == b'{"other":1}\n'


def test_check_fields_with_mixed_type_unknown_keys_is_a_value_error(state: StateDir) -> None:
    with pytest.raises(ValueError):
        log_of(state).emit("finalize", {1: "x", "a": "y", b"k": 2})  # type: ignore[dict-item]


@pytest.mark.parametrize("code", [10**400, 2**31, -(2**31) - 1, True])
def test_exit_code_is_bounded(state: StateDir, code: int) -> None:
    with pytest.raises(ValueError):
        audit_of(state).decision("push", PR, {"status": "failed", "exit_code": code})
    with pytest.raises(ValueError):
        log_of(state).emit("hook_result", {"hook": "pre-push", "exit_code": code,
                                           "timed_out": False})  # fmt: skip


@pytest.mark.parametrize("code", [-9, -15, 0, 255, 2**31 - 1, -(2**31)])
def test_exit_code_bounds_accept_signals_and_extremes(state: StateDir, code: int) -> None:
    audit_of(state).decision("push", PR, {"status": "failed", "exit_code": code})
    log_of(state).emit("hook_result", {"hook": "pre-push", "exit_code": code,
                                       "timed_out": False})  # fmt: skip
    assert len(check_all(state.path / "audit.jsonl", "review-respond/audit.json")) == 1


def test_naive_datetimes_are_refused(state: StateDir) -> None:
    naive = datetime(2026, 1, 1, 12, 0, 0)
    with pytest.raises(ValueError):
        log_of(state).emit(
            "check_pass", {"started_at": naive, "finished_at": naive, "already_running": False}
        )
    audit = AuditLog(JsonlFile(state, "audit.jsonl"), actor="auto:x", clock=FakeClock(naive))
    with pytest.raises(ValueError):
        audit.decision("stop", PR, {"status": "ok"})
