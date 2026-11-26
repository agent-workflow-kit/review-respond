"""`list` and `show` over real rounds: the derived states, the round counts and `show rd-...`
(docs/interface.md: list, show, Objects), through the CLI entry with the sibling fakes."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from review_respond import due
from review_respond.cli import main
from review_respond.rounds import Rounds, get_round, round_facts
from review_respond.store import (
    Store,
    open_state_dir,
    pr_digest,
    pr_lease_key,
    round_lease_key,
)
from tests.harness.plugin import World
from tests.test_check import (
    HOST,
    PR7,
    PR8,
    agent_spawn,  # noqa: F401  (autouse: check resolves the base agent)
    gold,
    run,
    sql,
    two_prs,
    valid,
    write_config,
)

C1 = "gh:rc:c1"
HEAD = "e" * 40
BASE = "ag-bcdefghi"


@pytest.fixture
def config(world: World) -> Path:
    return write_config(world)


@pytest.fixture
def state(world: World) -> Path:
    return Path(world.env["XDG_STATE_HOME"]) / "review-respond"


class Harness:
    def __init__(self, store: Store, pr: str) -> None:
        self.store = store
        lease = store.try_acquire(pr_lease_key(pr_digest(pr)), heartbeat=None)
        assert lease is not None
        self.lease = lease
        self.rounds = Rounds(lease)
        self.pr = pr

    def close(self) -> None:
        self.lease.release()
        self.store.close()


@pytest.fixture
def seeded(
    world: World, capsys: pytest.CaptureFixture[str], config: Path, state: Path
) -> Iterator[Harness]:
    two_prs(world, capsys, config)
    store = Store.open(open_state_dir(state), write=True, close_state=True)
    h = Harness(store, PR7)
    yield h
    h.close()


def pr_row(doc: dict[str, Any], pr: str = PR7) -> dict[str, Any]:
    (found,) = [p for p in doc["prs"] if p["pr"] == pr]
    return found


def fixed_times(world: World) -> None:
    sql(world, "UPDATE rounds SET created_at = 1700000000, started_at = CASE WHEN started_at IS"
        " NULL THEN NULL ELSE 1700000060 END, ended_at = CASE WHEN ended_at IS NULL THEN NULL"
        " ELSE 1700000120 END, cancel_requested_at = CASE WHEN cancel_requested_at IS NULL"
        " THEN NULL ELSE 1700000030 END")  # fmt: skip


def test_a_queued_round_makes_the_pr_queued_and_counts_in_rounds(
    world: World, capsys: pytest.CaptureFixture[str], config: Path, seeded: Harness
) -> None:
    seeded.rounds.create(PR7, base_agent_id=BASE)
    _, doc, _ = run(capsys, "list", config=config)
    seven = pr_row(doc)
    assert (seven["state"], seven["waiting_reason"], seven["rounds"]) == ("queued", None, 1)
    assert pr_row(doc, PR8)["rounds"] == 0 and pr_row(doc, PR8)["state"] == "idle"
    valid("list", doc)


def test_a_running_round_makes_the_pr_running(
    world: World, capsys: pytest.CaptureFixture[str], config: Path, seeded: Harness
) -> None:
    rid = seeded.rounds.create(PR7).id
    seeded.rounds.start(rid, (C1,), head_sha=HEAD)
    _, doc, _ = run(capsys, "list", config=config)
    assert pr_row(doc)["state"] == "running"


def test_a_pending_round_makes_the_pr_pending_and_a_terminal_one_gives_the_block_back(
    world: World, capsys: pytest.CaptureFixture[str], config: Path, seeded: Harness
) -> None:
    rid = seeded.rounds.create(PR7).id
    seeded.rounds.start(rid, (C1,), head_sha=HEAD)
    seeded.rounds.to_pending(rid)
    _, doc, _ = run(capsys, "list", config=config)
    assert pr_row(doc)["state"] == "pending"
    with seeded.store.transaction() as conn:
        seeded.rounds.settle_pending(conn, rid, "rejected")
    _, doc, _ = run(capsys, "list", config=config)
    assert pr_row(doc)["state"] != "pending" and pr_row(doc)["rounds"] == 1


def test_a_failed_round_blocks_the_pr_failed_and_check_starts_nothing(
    world: World, capsys: pytest.CaptureFixture[str], config: Path, seeded: Harness
) -> None:
    sql(world, "UPDATE prs SET block = NULL")
    rid = seeded.rounds.create(PR7).id
    seeded.rounds.start(rid, (C1,), head_sha=HEAD)
    seeded.rounds.to_failed(rid, "timeout", counted=True)
    _, doc, _ = run(capsys, "list", config=config)
    seven = pr_row(doc)
    assert (seven["state"], seven["waiting_reason"], seven["block"]["reason"]) == (
        "waiting", "failed", "failed",
    )  # fmt: skip
    assert seven["unhandled"] == 1  # the comment went back to unhandled


def test_the_round_limit_counts_only_counted_rounds(
    world: World, capsys: pytest.CaptureFixture[str], config: Path, seeded: Harness
) -> None:
    conn = seeded.store.conn
    for _ in range(2):  # two rounds that never started their run
        rid = seeded.rounds.create(PR7).id
        seeded.rounds.cancel_pr_closed(rid)
    assert round_facts(conn, PR7).counted == 0
    rid = seeded.rounds.create(PR7).id
    seeded.rounds.start(rid, (C1,), head_sha=HEAD)
    seeded.rounds.to_failed(rid, "agent_failed", counted=True)
    facts = round_facts(conn, PR7)
    assert facts.counted == 1
    # the due rule sees the counted rounds against max_rounds
    cfg = _config(world)
    inputs = due.load_inputs(conn, cfg, PR7, now=1.0, facts=facts)
    assert inputs.facts.counted == 1


def _config(world: World) -> Any:
    from review_respond.config import load_config

    return load_config(write_config(world)).config


def test_show_of_a_round_by_id_and_by_prefix_validates(
    world: World, capsys: pytest.CaptureFixture[str], config: Path, seeded: Harness, golden: Any
) -> None:
    rid = seeded.rounds.create(PR7, base_agent_id=BASE, trigger="respond").id
    seeded.rounds.record_job(rid, job_id="jb-abcdefgh")
    seeded.rounds.start(rid, (C1,), head_sha=HEAD)
    seeded.rounds.record_agent(rid, "ag-abcdefgh")
    seeded.rounds.note_started(rid)
    fixed_times(world)
    # a live process holds the round (its job): `show` does not finalize it and says so
    job = seeded.store.try_acquire(round_lease_key(rid), heartbeat=None)
    assert job is not None
    code, doc, _ = run(capsys, "show", rid, config=config)
    assert code == 0
    valid("show", doc)
    assert doc["object_type"] == "round" and doc["finalize"] == "busy"
    shown = doc["round"]
    assert (shown["id"], shown["state"], shown["number"], shown["counted"]) == (
        rid,
        "running",
        1,
        True,
    )
    assert (shown["agent_id"], shown["job_id"], shown["base_agent_id"]) == (
        "ag-abcdefgh", "jb-abcdefgh", BASE,
    )  # fmt: skip
    assert shown["comment_ids"] == [C1] == shown["delivered"] and shown["head_sha"] == HEAD
    assert [c["id"] for c in doc["comments"]] == [C1] and doc["comments"][0]["state"] == "delivered"
    gold_doc = json.loads(json.dumps(doc).replace(rid, "rd-abcdefgh"))
    gold(golden, "show-round", gold_doc)
    code, again, _ = run(capsys, "show", rid[:7], config=config)
    assert code == 0 and again["round"]["id"] == rid
    job.release()


def test_show_of_a_queued_round_has_no_head_and_validates(
    world: World, capsys: pytest.CaptureFixture[str], config: Path, seeded: Harness
) -> None:
    rid = seeded.rounds.create(PR7).id
    _, doc, _ = run(capsys, "show", rid, config=config)
    valid("show", doc)
    assert doc["round"]["head_sha"] is None and doc["comments"] == []


def test_show_of_an_escalated_round_prints_the_question_sanitized(
    world: World, capsys: pytest.CaptureFixture[str], config: Path, seeded: Harness
) -> None:
    rid = seeded.rounds.create(PR7).id
    seeded.rounds.start(rid, (C1,), head_sha=HEAD)
    seeded.rounds.to_escalated(
        rid, reason="\x1b[2Jwhich?", summary="line one\nline two " + "x" * 5000, counted=True
    )
    code, doc, _ = run(capsys, "show", rid, config=config)
    assert code == 0
    valid("show", doc)
    esc = doc["round"]["escalation"]
    assert esc["reason"] == "<U+001B>[2Jwhich?"
    assert esc["summary"].startswith("line one\nline two ") and len(esc["summary"]) == 4096
    assert doc["round"]["state"] == "escalated" and doc["round"]["failure"] is None
    assert main(["show", rid, "--config", str(config)]) == 0
    assert "\x1b" not in capsys.readouterr().out


def test_show_of_a_failed_and_a_canceled_round_validates(
    world: World, capsys: pytest.CaptureFixture[str], config: Path, seeded: Harness
) -> None:
    rid = seeded.rounds.create(PR7).id
    seeded.rounds.start(rid, (C1,), head_sha=HEAD)
    seeded.rounds.to_failed(rid, "artifact_invalid", detail="schema", counted=True)
    _, doc, _ = run(capsys, "show", rid, config=config)
    valid("show", doc)
    assert doc["round"]["failure"] == {"reason": "artifact_invalid", "detail": "schema"}
    sql(world, "UPDATE prs SET block = NULL")
    rid2 = seeded.rounds.create(PR7).id
    seeded.rounds.request_cancel(rid2, "stopped")
    seeded.rounds.cancel_flagged(rid2)
    _, doc, _ = run(capsys, "show", rid2, config=config)
    valid("show", doc)
    assert doc["round"]["cancel_reason"] == "stopped" and doc["round"]["cancel_requested_at"]
    assert doc["round"]["ended"] is not None and doc["round"]["counted"] is False


def test_show_of_the_pr_lists_its_rounds_in_order(
    world: World, capsys: pytest.CaptureFixture[str], config: Path, seeded: Harness
) -> None:
    first = seeded.rounds.create(PR7).id
    seeded.rounds.cancel_pr_closed(first)
    second = seeded.rounds.create(PR7).id
    _, doc, _ = run(capsys, "show", PR7, config=config)
    valid("show", doc)
    assert [r["id"] for r in doc["rounds"]] == [first, second]
    assert [r["number"] for r in doc["rounds"]] == [1, 2]


def test_show_of_an_unknown_round_is_not_found_and_a_pending_id_still_is(
    world: World, capsys: pytest.CaptureFixture[str], config: Path, seeded: Harness
) -> None:
    for ref in ("rd-aaaaaaaa", "rp-aaaaaaaa"):
        code, doc, _ = run(capsys, "show", ref, config=config)
        assert code == 10 and doc["error"]["code"] == "not_found"


def test_show_of_a_prefix_of_two_rounds_is_ambiguous_exit_21(
    world: World, capsys: pytest.CaptureFixture[str], config: Path, seeded: Harness
) -> None:
    for number, rid in enumerate(("rd-abcd2222", "rd-abcd3333"), start=1):
        sql(world, "INSERT INTO rounds (id, pr, number, state, trigger_kind, created_at,"
            " ended_at, cancel_reason) VALUES (?, ?, ?, 'canceled', 'check', 1, 1, 'stopped')",
            rid, PR7, number)  # fmt: skip
    code, doc, _ = run(capsys, "show", "rd-abcd", config=config)
    assert code == 21 and doc["error"]["code"] == "ambiguous"
    code, doc, _ = run(capsys, "show", "rd-abcd2", config=config)
    assert code == 0 and doc["round"]["id"] == "rd-abcd2222"


def test_show_of_a_host_named_like_a_round_prefix_still_resolves_as_a_pr(
    world: World, capsys: pytest.CaptureFixture[str], config: Path, seeded: Harness
) -> None:
    code, doc, _ = run(capsys, "show", f"{HOST}/org/repo#7", config=config)
    assert code == 0 and doc["object_type"] == "pr"


def test_list_and_show_do_not_write_the_state_when_rounds_exist(
    world: World, capsys: pytest.CaptureFixture[str], config: Path, seeded: Harness, state: Path
) -> None:
    rid = seeded.rounds.create(PR7).id
    before = get_round(seeded.store.conn, rid)
    run(capsys, "list", config=config)
    run(capsys, "show", rid, config=config)
    assert get_round(seeded.store.conn, rid) == before


@pytest.mark.parametrize("lead", ["", "a"])
def test_a_question_of_control_characters_prints_within_4096_characters_and_validates(
    world: World, capsys: pytest.CaptureFixture[str], config: Path, seeded: Harness, lead: str
) -> None:
    rid = seeded.rounds.create(PR7).id
    seeded.rounds.start(rid, (C1,), head_sha=HEAD)
    seeded.rounds.to_escalated(
        rid, reason=lead + "\x01" * 4096, summary="\x1b" * 5000, counted=True
    )
    for ref in (rid, PR7):
        code, doc, _ = run(capsys, "show", ref, config=config)
        assert code == 0
        valid("show", doc)
        shown = doc["round"] if ref == rid else doc["rounds"][0]
        reason = shown["escalation"]["reason"]
        assert len(reason) <= 4096
        assert re.fullmatch(rf"{lead}(<U\+0001>)+", reason)  # a marker is never cut in two
        assert re.fullmatch(r"(<U\+001B>)+", shown["escalation"]["summary"])


def test_show_of_a_round_whose_comment_lost_its_stored_text_is_state_unsafe(
    world: World, capsys: pytest.CaptureFixture[str], config: Path, seeded: Harness
) -> None:
    rid = seeded.rounds.create(PR7).id
    seeded.rounds.start(rid, (C1,), head_sha=HEAD)
    sql(world, "UPDATE comments SET body_file = NULL WHERE comment_id = ?", C1)
    code, doc, _ = run(capsys, "show", rid, config=config)
    assert code == 12 and doc["error"]["code"] == "state_unsafe"
    code, doc, _ = run(capsys, "show", PR7, config=config)
    assert code == 12 and doc["error"]["code"] == "state_unsafe"
