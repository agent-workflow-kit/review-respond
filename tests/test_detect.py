"""Detection: per-host listing, request budget, stored PR state and the per-PR comment read
(docs/interface.md: Detection)."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from review_respond import cli as cli_mod
from review_respond import detect as detect_mod
from review_respond.audit import LogWriter, open_jsonl_files
from review_respond.config import Config
from review_respond.detect import (
    CommentRead,
    Detection,
    HostResult,
    absent_comment_ids,
    pr_digest,
    read_cost,
)
from review_respond.errors import CliError
from review_respond.gateway import Gateway
from review_respond.output import Context, Outcome
from review_respond.store import Lease, Store, open_state_dir, pass_lease_key
from tests.harness import adversarial, builders
from tests.harness.builders import HOST, HOST_B, pr_item, pr_ref
from tests.harness.fakes import Fakes
from tests.harness.plugin import World

ACCOUNTS = {HOST: "acct-me", HOST_B: "acct-me-b"}


# ---- fixtures and helpers ------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path, gateway: Gateway) -> Iterator[Store]:
    opened = Store.open(open_state_dir(tmp_path / "state" / "rr"), write=True, close_state=True)
    opened.clock = gateway.clock
    yield opened
    opened.close()


@pytest.fixture
def lease(store: Store) -> Iterator[Lease]:
    held = store.try_acquire(pass_lease_key(), heartbeat=None)
    assert held is not None
    yield held
    held.release()


def settings(
    *, hosts: list[str] | None = None, budget: int | None = None, accounts: bool = True
) -> Config:
    doc: dict[str, Any] = {"hosts_to_check": hosts if hosts is not None else [HOST]}
    if accounts:
        doc["me"] = {"accounts": ACCOUNTS}
    if budget is not None:
        doc["limits"] = {"requests_per_host": budget}
    return Config.model_validate(doc)


def make(
    gateway: Gateway,
    lease: Lease,
    *,
    config: Config | None = None,
    on_read: Callable[..., None] | None = None,
    log: LogWriter | None = None,
) -> Detection:
    return Detection(gateway, lease, config or settings(), log=log, on_read=on_read)


def script_prs(
    fakes: Fakes,
    items: list[dict[str, Any]],
    *,
    host: str = HOST,
    complete: bool = True,
    **kwargs: Any,
) -> None:
    fakes.script(
        "forgectl",
        "prs",
        json=builders.forgectl_prs(items, complete=complete, host=host),
        match={"flags": {"--host": host}},
        **kwargs,
    )


def script_comments(fakes: Fakes, pr: str, doc: dict[str, Any] | None = None) -> None:
    fakes.script(
        "forgectl",
        "comments",
        json=doc or builders.forgectl_comments([], pr=pr),
        match={"positionals": {"pr": pr}},
    )


def rows(store: Store) -> dict[str, sqlite3.Row]:
    store.conn.row_factory = sqlite3.Row
    try:
        return {r["pr"]: r for r in store.conn.execute("SELECT * FROM prs ORDER BY pr")}
    finally:
        store.conn.row_factory = None


def forgectl_calls(fakes: Fakes, command: str) -> list[list[str]]:
    return [c.argv for c in fakes.calls("forgectl", command)]


def call_prs(fakes: Fakes) -> list[str]:
    """The PR of each `forgectl comments` call, in order."""
    return [c.positionals["pr"] for c in fakes.calls("forgectl", "comments")]


def insert_pr(store: Store, number: int, **overrides: Any) -> str:
    """A stored PR row, as an earlier pass would have left it."""
    ref = pr_ref(number)
    values: dict[str, Any] = {
        "pr": ref,
        "digest": pr_digest(ref),
        "host": HOST,
        "repo": f"{HOST}/org/repo",
        "number": number,
        "host_state": "open",
        "head_sha": builders.HEAD_A,
        "head_ref": "feat/x",
        "head_repo": f"{HOST}/org/repo",
        "is_fork": 0,
        "base_ref": "main",
        "created_at": builders.TS,
        "updated_at": builders.TS,
        "listed_at": 1.0,
        **overrides,
    }
    names = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    with store.transaction() as conn:
        conn.execute(f"INSERT INTO prs ({names}) VALUES ({marks})", tuple(values.values()))
    return ref


# ---- migration -----------------------------------------------------------------------------


def test_the_prs_table_exists_and_is_additive(store: Store) -> None:
    columns = {r[1] for r in store.conn.execute("PRAGMA table_info(prs)")}
    assert {
        "pr",
        "digest",
        "host",
        "host_state",
        "head_sha",
        "is_fork",
        "created_at",
        "last_comments_read_at",
        "last_comments_attempt_at",
        "read_failures",
        "comment_count",
        "rev",
    } <= columns
    assert store.schema_version >= 3


def test_pr_digest_is_twenty_hex_of_the_sha256_of_the_reference() -> None:
    import hashlib

    ref = pr_ref(7)
    assert pr_digest(ref) == hashlib.sha256(ref.encode()).hexdigest()[:20]
    assert len(pr_digest(ref)) == 20


# ---- listing -------------------------------------------------------------------------------


def test_listing_asks_forgectl_for_my_open_prs_on_one_host(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    script_prs(fakes, [])
    result = make(gateway, lease).run().hosts[0]
    assert result.ok
    assert forgectl_calls(fakes, "prs") == [
        ["prs", "--author", "me", "--host", HOST, "--state", "open", "--limit", "1000", "--json"]
    ]


def test_listed_prs_are_upserted_with_head_fork_flag_and_created_at(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    items = [
        pr_item(1, head_sha=builders.HEAD_A, created_at="2026-01-01T10:00:00Z"),
        pr_item(2, is_fork=True, head_repo=f"{HOST}/someone/repo"),
        pr_item(3, is_fork=None, head_repo=None),  # the head project was deleted
        pr_item(4, is_fork=False, head_repo=f"{HOST}/org/other"),  # not the PR's repo
    ]
    script_prs(fakes, items)
    for n in (1,):
        script_comments(fakes, pr_ref(n))
    make(gateway, lease).run()
    stored = rows(store)
    assert set(stored) == {pr_ref(n) for n in (1, 2, 3, 4)}
    first = stored[pr_ref(1)]
    assert first["digest"] == pr_digest(pr_ref(1))
    assert (first["host"], first["repo"], first["number"]) == (HOST, f"{HOST}/org/repo", 1)
    assert (first["host_state"], first["head_sha"], first["head_ref"]) == (
        "open",
        builders.HEAD_A,
        "feat/x",
    )
    assert first["created_at"] == "2026-01-01T10:00:00Z"
    assert first["is_fork"] == 0
    # a null is_fork counts as a fork (interface: Eligibility 4); so does another head repo
    assert [stored[pr_ref(n)]["is_fork"] for n in (2, 3, 4)] == [1, 1, 1]


def test_a_second_listing_updates_the_head_but_keeps_created_at_and_first_seen(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    script_prs(fakes, [pr_item(1)])
    script_comments(fakes, pr_ref(1))
    detection = make(gateway, lease)
    detection.run()
    before = rows(store)[pr_ref(1)]
    gateway.clock.advance(60)  # type: ignore[attr-defined]
    script_prs(
        fakes,
        [
            pr_item(
                1, head_sha=builders.HEAD_B, head_ref="feat/y", created_at="2030-01-01T00:00:00Z"
            )
        ],
    )
    script_comments(fakes, pr_ref(1))
    make(gateway, lease).run()
    after = rows(store)[pr_ref(1)]
    assert (after["head_sha"], after["head_ref"]) == (builders.HEAD_B, "feat/y")
    assert after["created_at"] == before["created_at"] == builders.TS
    assert after["first_seen_at"] == before["first_seen_at"]
    assert after["listed_at"] > before["listed_at"]
    assert after["rev"] > before["rev"]


def test_fork_prs_are_listed_but_their_comments_are_never_read(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    script_prs(fakes, [pr_item(2, is_fork=True, head_repo=f"{HOST}/someone/repo")])
    result = make(gateway, lease).run().hosts[0]
    assert result.listed == (pr_ref(2),)
    assert result.attempted == ()
    assert forgectl_calls(fakes, "comments") == []
    assert rows(store)[pr_ref(2)]["last_comments_attempt_at"] is None


# ---- identity and hosts --------------------------------------------------------------------


def test_the_account_id_comes_from_config_and_whoami_is_not_asked(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    script_prs(fakes, [])
    detection = make(gateway, lease)
    detection.run()
    assert detection.account_id(HOST) == "acct-me"
    assert forgectl_calls(fakes, "whoami") == []


def test_the_account_id_comes_from_whoami_once_for_all_hosts(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    fakes.script("forgectl", "whoami", json=builders.forgectl_whoami_hosts(HOST, HOST_B))
    script_prs(fakes, [])
    script_prs(fakes, [], host=HOST_B)
    detection = make(gateway, lease, config=settings(hosts=[HOST, HOST_B], accounts=False))
    detection.run()
    assert detection.account_id(HOST) == "acct-me-git"
    assert detection.account_id(HOST_B) == "acct-me-other"
    assert len(forgectl_calls(fakes, "whoami")) == 1  # cached for the pass


def test_hosts_default_to_those_whoami_lists_with_a_credential(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    fakes.script(
        "forgectl",
        "whoami",
        json=builders.forgectl_whoami_hosts(HOST, HOST_B, token_source={HOST_B: "none"}),
    )
    script_prs(fakes, [])
    config = Config.model_validate({})
    result = Detection(gateway, lease, config).run()
    assert [h.host for h in result.hosts] == [HOST]


def test_a_failed_whoami_with_no_configured_hosts_is_lower_failed(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    fakes.script(
        "forgectl", "whoami", json=builders.error_envelope("auth_required", "no token"), exit=12
    )
    with pytest.raises(CliError) as caught:
        Detection(gateway, lease, Config.model_validate({})).run()
    assert caught.value.code == "lower_failed"
    assert caught.value.exit_code == 12
    assert caught.value.lower is not None and caught.value.lower["code"] == "auth_required"


def test_a_host_without_an_account_id_is_a_failed_host_and_nothing_is_read(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    fakes.script("forgectl", "whoami", json=builders.forgectl_whoami_hosts(HOST, ids={HOST: None}))
    result = make(gateway, lease, config=settings(accounts=False)).run().hosts[0]
    assert (result.ok, result.code) == (False, "identity_unknown")
    assert forgectl_calls(fakes, "prs") == []


def test_a_configured_host_whoami_does_not_know_is_failed(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    fakes.script(
        "forgectl",
        "whoami",
        json=builders.forgectl_whoami_hosts(
            HOST_B, errors=[builders.host_error(HOST, "auth_required")]
        ),
        exit=12,
        schema="forgectl/cli/whoami.json",
    )
    config = settings(hosts=[HOST], accounts=False)
    result = make(gateway, lease, config=config).run().hosts[0]
    assert (result.ok, result.code) == (False, "auth_required")


# ---- one host failing never fails another --------------------------------------------------


def test_a_host_failure_leaves_the_other_hosts_processed(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease, tmp_path: Path
) -> None:
    files = open_jsonl_files(store.source)  # type: ignore[arg-type]
    log = LogWriter(files[1], clock=gateway.clock)
    insert_pr(store, 9)  # stored open on the failing host: it must stay exactly as it was
    before = rows(store)[pr_ref(9)]
    fakes.script(
        "forgectl",
        "prs",
        json=builders.error_envelope("upstream_error", "boom"),
        exit=12,
        match={"flags": {"--host": HOST}},
    )
    other = pr_item(5, host=HOST_B)
    script_prs(fakes, [other], host=HOST_B)
    script_comments(fakes, other["pr"])
    detection = make(gateway, lease, config=settings(hosts=[HOST, HOST_B]), log=log)
    result = detection.run()
    failed, ok = result.hosts
    assert (failed.host, failed.ok, failed.code) == (HOST, False, "upstream_error")
    assert (ok.host, ok.ok, ok.attempted) == (HOST_B, True, (other["pr"],))
    assert not result.all_failed
    stored = rows(store)
    assert set(stored) == {pr_ref(9), other["pr"]}
    assert dict(stored[pr_ref(9)]) == dict(before)  # nothing on the failed host changed
    events = [
        json.loads(line) for line in (store.state_path / "log.jsonl").read_text().splitlines()
    ]
    assert [(e["event"], e["host"], e["code"]) for e in events] == [
        ("host_failed", HOST, "upstream_error")
    ]
    files[0].close()
    files[1].close()


def test_all_hosts_failing_is_reported_as_such(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    for host in (HOST, HOST_B):
        fakes.script(
            "forgectl",
            "prs",
            json=builders.error_envelope("network_error", "down"),
            exit=12,
            match={"flags": {"--host": host}},
        )
    result = make(gateway, lease, config=settings(hosts=[HOST, HOST_B])).run()
    assert result.all_failed
    assert [h.code for h in result.hosts] == ["network_error", "network_error"]


def hostile(doc: dict[str, Any]) -> dict[str, Any]:
    return doc


BAD_LISTINGS: dict[str, Callable[[], tuple[dict[str, Any], int]]] = {
    "partial_results": lambda: (
        {
            **builders.forgectl_prs([pr_item(1)]),
            "errors": [builders.host_error(HOST, "rate_limited")],
            "complete": False,
        },
        12,
    ),
    "errors_on_exit_zero": lambda: (
        {**builders.forgectl_prs([pr_item(1)]), "errors": [builders.host_error(HOST)]},
        0,
    ),
    "item_missing_head": lambda: (
        builders.forgectl_prs([{k: v for k, v in pr_item(1).items() if k != "head"}]),
        0,
    ),
    "other_hosts_pr": lambda: (builders.forgectl_prs([pr_item(1, host=HOST_B)]), 0),
    "pr_reference_forged": lambda: (
        builders.forgectl_prs([{**pr_item(1), "pr": "../../etc/passwd#1"}]),
        0,
    ),
    "number_disagrees_with_reference": lambda: (
        builders.forgectl_prs([{**pr_item(1), "number": 2}]),
        0,
    ),
    "head_sha_not_hex": lambda: (
        builders.forgectl_prs([pr_item(1, head_sha="HEAD; rm -rf /")]),
        0,
    ),
    "items_not_a_list": lambda: ({**builders.forgectl_prs([]), "items": {"a": 1}}, 0),
    "complete_missing": lambda: (
        {k: v for k, v in builders.forgectl_prs([]).items() if k != "complete"},
        0,
    ),
    "state_unknown": lambda: (builders.forgectl_prs([{**pr_item(1), "state": "weird"}]), 0),
    "too_many_items": lambda: (
        builders.forgectl_prs([pr_item(n) for n in range(1, 1003)]),
        0,
    ),
    "ref_with_newline": lambda: (
        builders.forgectl_prs([pr_item(1, head_ref="feat/x\nSigned-off-by: root")]),
        0,
    ),
}


@pytest.mark.parametrize("case", sorted(BAD_LISTINGS))
def test_a_listing_that_does_not_fit_the_schema_fails_the_host_and_changes_nothing(
    case: str, gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    doc, exit_code = BAD_LISTINGS[case]()
    fakes.script(
        "forgectl", "prs", json=doc, exit=exit_code, schema=False, match={"flags": {"--host": HOST}}
    )
    result = make(gateway, lease).run().hosts[0]
    assert not result.ok
    assert result.code is not None
    assert rows(store) == {}
    assert forgectl_calls(fakes, "comments") == []


@pytest.mark.parametrize("stdout", ["", "not json", "[]", "null", '{"items": ', "\u0000"])
def test_unparseable_listing_output_fails_the_host(
    stdout: str, gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    fakes.script("forgectl", "prs", stdout=stdout, schema=False, match={"flags": {"--host": HOST}})
    result = make(gateway, lease).run().hosts[0]
    assert (result.ok, result.code) == (False, "bad_output")


def test_a_listing_that_exits_nonzero_without_an_envelope_fails_with_its_exit_code(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    fakes.script(
        "forgectl", "prs", stdout="", stderr="panic", exit=3, schema=False,
        match={"flags": {"--host": HOST}},
    )  # fmt: skip
    result = make(gateway, lease).run().hosts[0]
    assert (result.ok, result.code) == (False, "exit_3")


def test_an_error_code_that_is_not_a_plain_name_cannot_reach_the_log_or_result(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    fakes.script(
        "forgectl",
        "prs",
        json={"error": {"code": 'x\nINJECT {"level":"error"}', "message": "m"}},
        exit=12,
        schema=False,
        match={"flags": {"--host": HOST}},
    )
    result = make(gateway, lease).run().hosts[0]
    assert result.code == "bad_output"


# ---- closed and merged PRs ---------------------------------------------------------------


def test_a_complete_listing_reads_unlisted_open_prs_with_show_to_learn_closed(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    gone, merged, other_host = insert_pr(store, 8), insert_pr(store, 9), insert_pr(store, 10)
    with store.transaction() as conn:
        conn.execute("UPDATE prs SET host = ? WHERE pr = ?", (HOST_B, other_host))
    script_prs(fakes, [])
    fakes.script("forgectl", "show", json=builders.forgectl_show(pr=gone, state="closed"),
                 match={"positionals": {"pr": gone}})  # fmt: skip
    fakes.script("forgectl", "show", json=builders.forgectl_show(pr=merged, state="merged"),
                 match={"positionals": {"pr": merged}})  # fmt: skip
    result = make(gateway, lease).run().hosts[0]
    stored = rows(store)
    assert stored[gone]["host_state"] == "closed"
    assert stored[merged]["host_state"] == "merged"
    assert stored[other_host]["host_state"] == "open"  # another host's PR is not this host's
    assert set(result.closed) == {gone, merged}


def test_an_incomplete_listing_never_infers_a_close(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    ref = insert_pr(store, 8)
    script_prs(fakes, [], complete=False)
    make(gateway, lease).run()
    assert forgectl_calls(fakes, "show") == []
    assert rows(store)[ref]["host_state"] == "open"


@pytest.mark.parametrize(
    ("answer", "exit_code"),
    [
        (builders.error_envelope("not_found", "no such PR"), 10),
        (builders.error_envelope("upstream_error", "boom"), 12),
        (builders.forgectl_show(pr=pr_ref(8), state="open"), 0),
    ],
    ids=["not_found_is_unknown_not_closed", "upstream_error", "still_open"],
)
def test_show_that_does_not_report_closed_or_merged_changes_nothing(
    answer: dict[str, Any],
    exit_code: int,
    gateway: Gateway,
    fakes: Fakes,
    store: Store,
    lease: Lease,
) -> None:
    ref = insert_pr(store, 8)
    script_prs(fakes, [])
    fakes.script("forgectl", "show", json=answer, exit=exit_code, schema=False)
    result = make(gateway, lease).run().hosts[0]
    assert rows(store)[ref]["host_state"] == "open"
    assert result.closed == ()
    assert result.ok


def test_show_for_a_different_pr_than_asked_is_ignored(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    ref = insert_pr(store, 8)
    script_prs(fakes, [])
    fakes.script("forgectl", "show", json=builders.forgectl_show(pr=pr_ref(99), state="closed"))
    make(gateway, lease).run()
    assert rows(store)[ref]["host_state"] == "open"


def test_a_closed_pr_is_never_reopened_by_a_stale_listing_row(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    ref = insert_pr(store, 8, host_state="closed")
    script_prs(fakes, [pr_item(8)])
    script_comments(fakes, ref)
    make(gateway, lease).run()
    # the host lists it as open now (reopened): the listing is the newer fact
    assert rows(store)[ref]["host_state"] == "open"


# ---- order and request budget --------------------------------------------------------------


def test_prs_are_read_least_recently_attempted_first_never_attempted_first_of_all(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    a = insert_pr(store, 1, last_comments_attempt_at=500.0)
    b = insert_pr(store, 2, last_comments_attempt_at=100.0)
    c = insert_pr(store, 3)  # never attempted
    d = insert_pr(store, 4, last_comments_attempt_at=100.0)  # a tie: the PR reference decides
    script_prs(fakes, [pr_item(n) for n in (1, 2, 3, 4)])
    for ref in (a, b, c, d):
        script_comments(fakes, ref)
    make(gateway, lease).run()
    assert call_prs(fakes) == [c, b, d, a]


def test_budget_exhaustion_defers_the_rest_which_go_first_next_pass(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    refs = [pr_ref(n) for n in (1, 2, 3, 4)]
    items = [pr_item(n) for n in (1, 2, 3, 4)]
    # listing costs 1, each read 1: a budget of 3 reads two PRs
    script_prs(fakes, items)
    for ref in refs[:2]:
        script_comments(fakes, ref)
    first = make(gateway, lease, config=settings(budget=3)).run().hosts[0]
    assert first.attempted == (refs[0], refs[1])
    assert first.deferred == (refs[2], refs[3])
    assert first.spent == 3
    gateway.clock.advance(300)  # type: ignore[attr-defined]
    script_prs(fakes, items)
    for ref in (refs[2], refs[3]):
        script_comments(fakes, ref)
    second = make(gateway, lease, config=settings(budget=3)).run().hosts[0]
    assert second.attempted == (refs[2], refs[3])  # the deferred ones first
    assert second.deferred == (refs[0], refs[1])


def test_the_read_cost_estimate_is_one_plus_a_hundredth_of_the_stored_comments() -> None:
    assert [read_cost(n) for n in (0, 1, 99, 100, 250, 1234)] == [1, 1, 1, 2, 3, 13]


def test_a_pr_with_many_stored_comments_costs_more_and_may_not_fit(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    big = insert_pr(store, 1, comment_count=450)  # cost 5
    small = insert_pr(store, 2, last_comments_attempt_at=9.0)
    script_prs(fakes, [pr_item(1), pr_item(2)])
    script_comments(fakes, big)
    result = make(gateway, lease, config=settings(budget=6)).run().hosts[0]
    # listing 1 + big 5 = 6: the small one (1 more) does not fit
    assert (result.attempted, result.deferred, result.spent) == ((big,), (small,), 6)


def test_the_first_read_of_a_pass_goes_ahead_even_when_the_estimate_exceeds_the_budget(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    huge = insert_pr(store, 1, comment_count=100_000)  # cost 1001
    script_prs(fakes, [pr_item(1)])
    script_comments(fakes, huge)
    result = make(gateway, lease, config=settings(budget=5)).run().hosts[0]
    assert result.attempted == (huge,)  # otherwise this PR would starve for ever


def test_a_budget_of_one_is_spent_on_the_listing_and_still_reads_one_pr(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    script_prs(fakes, [pr_item(1), pr_item(2)])
    script_comments(fakes, pr_ref(1))
    result = make(gateway, lease, config=settings(budget=1)).run().hosts[0]
    assert result.attempted == (pr_ref(1),)
    assert result.deferred == (pr_ref(2),)


def test_the_listing_costs_one_per_hundred_items(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    items = [pr_item(n, is_fork=True, head_repo=f"{HOST}/f/r") for n in range(1, 251)]
    script_prs(fakes, items)
    result = make(gateway, lease).run().hosts[0]
    assert result.spent == 3


def test_show_for_a_closed_pr_is_paid_from_the_budget_and_may_be_deferred(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    one, two = insert_pr(store, 8), insert_pr(store, 9)
    script_prs(fakes, [])
    fakes.script("forgectl", "show", json=builders.forgectl_show(pr=one, state="closed"),
                 match={"positionals": {"pr": one}})  # fmt: skip
    result = make(gateway, lease, config=settings(budget=2)).run().hosts[0]
    # listing 1, one show 1: the second show does not fit and waits for the next pass
    assert result.closed == (one,)
    assert rows(store)[two]["host_state"] == "open"
    assert len(forgectl_calls(fakes, "show")) == 1


def test_a_failing_read_does_not_starve_the_others(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    bad, good = pr_ref(1), pr_ref(2)
    items = [pr_item(1), pr_item(2)]
    script_prs(fakes, items)
    fakes.script("forgectl", "comments", json=builders.error_envelope("upstream_error", "x"),
                 exit=12, match={"positionals": {"pr": bad}})  # fmt: skip
    script_comments(fakes, good)
    first = make(gateway, lease).run().hosts[0]
    assert first.attempted == (bad, good)  # the failure did not stop the pass
    assert first.failed == (bad,)
    assert first.read == (good,)
    stored = rows(store)
    assert stored[bad]["last_comments_attempt_at"] is not None  # stamped before the call
    assert stored[bad]["last_comments_read_at"] is None
    assert stored[bad]["read_failures"] == 1


def test_a_pr_that_fails_every_time_cannot_hold_the_front_of_the_queue(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    bad, good = pr_ref(1), pr_ref(2)
    items = [pr_item(1), pr_item(2)]
    config = settings(budget=2)  # the listing and one read per pass
    script_prs(fakes, items)
    fakes.script("forgectl", "comments", json=builders.error_envelope("upstream_error", "x"),
                 exit=12, match={"positionals": {"pr": bad}})  # fmt: skip
    assert make(gateway, lease, config=config).run().hosts[0].attempted == (bad,)
    gateway.clock.advance(300)  # type: ignore[attr-defined]
    script_prs(fakes, items)
    script_comments(fakes, good)
    second = make(gateway, lease, config=config).run().hosts[0]
    assert second.attempted == (good,)  # the failed one was attempted more recently
    assert rows(store)[good]["read_failures"] == 0
    assert rows(store)[bad]["read_failures"] == 1


# ---- read_comments -------------------------------------------------------------------------


def test_read_comments_parses_the_real_world_shape(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    ref = insert_pr(store, 7)
    script_comments(fakes, ref, builders.real_shape_comments(ref))
    read = make(gateway, lease).read_comments(ref)
    assert isinstance(read, CommentRead)
    assert read.complete
    by_id = {c.id: c for c in read.comments}
    assert list(by_id) == [
        "gh:rc:PRRC_kwDOabc-1",
        "gh:rc:PRRC_kwDOabc-2",
        "gh:rc:PRRC_kwDOabc-3",
        "gh:ic:IC_kwDOabc-4",
        "gh:rc:PRRC_kwDOabc-5",
    ]
    bot_unknown = by_id["gh:rc:PRRC_kwDOabc-3"]
    assert bot_unknown.author_is_bot is None  # null stays null: eligibility counts it as a bot
    assert bot_unknown.thread_outdated is True and bot_unknown.line is None
    ghost = by_id["gh:ic:IC_kwDOabc-4"]
    assert (ghost.author_id, ghost.author_login, ghost.author_is_bot) == (None, None, None)
    assert ghost.thread_resolvable is False and ghost.path is None
    assert by_id["gh:rc:PRRC_kwDOabc-5"].minimized and by_id["gh:rc:PRRC_kwDOabc-5"].thread_resolved
    assert by_id["gh:rc:PRRC_kwDOabc-2"].reply_to == "gh:rc:PRRC_kwDOabc-1"
    assert by_id["gh:rc:PRRC_kwDOabc-1"].body == "Please rename `foo`."
    assert forgectl_calls(fakes, "comments") == [["comments", ref, "--json"]]


def test_read_comments_success_records_time_count_and_resets_failures(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    ref = insert_pr(store, 7, read_failures=2)
    script_comments(fakes, ref, builders.real_shape_comments(ref))
    make(gateway, lease).read_comments(ref)
    row = rows(store)[ref]
    now = gateway.clock.now().timestamp()
    assert row["last_comments_read_at"] == now
    assert row["last_comments_attempt_at"] == now
    assert row["read_failures"] == 0
    assert row["comment_count"] == 5


def test_read_comments_failure_changes_no_record_but_counts_the_failure(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    ref = insert_pr(store, 7, comment_count=4, last_comments_read_at=3.0)
    fakes.script("forgectl", "comments", json=builders.error_envelope("rate_limited", "slow down"),
                 exit=12)  # fmt: skip
    with pytest.raises(CliError) as caught:
        make(gateway, lease).read_comments(ref)
    error = caught.value
    assert (error.code, error.exit_code) == ("lower_failed", 12)
    assert error.lower is not None and error.lower["code"] == "rate_limited"
    row = rows(store)[ref]
    assert (row["comment_count"], row["last_comments_read_at"]) == (4, 3.0)
    assert row["read_failures"] == 1
    assert row["last_comments_attempt_at"] == gateway.clock.now().timestamp()


def test_lower_failed_reaches_the_cli_envelope_with_exit_12(
    gateway: Gateway,
    fakes: Fakes,
    store: Store,
    lease: Lease,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    golden: Any,
) -> None:
    ref = insert_pr(store, 7)
    fakes.script("forgectl", "comments", json=builders.error_envelope("rate_limited", "slow down"),
                 exit=12)  # fmt: skip
    detection = make(gateway, lease)

    def check(_ctx: Context, _args: argparse.Namespace) -> Outcome:
        detection.read_comments(ref)
        return Outcome()

    monkeypatch.setitem(cli_mod.HANDLERS, "check", check)
    code = cli_mod.main(["check", "--json"])
    out = capsys.readouterr().out
    assert code == 12
    document = json.loads(out)
    assert document["error"]["code"] == "lower_failed"
    assert document["error"]["lower_error"]["code"] == "rate_limited"
    golden.check("cli/lower-failed", document, "review-respond/cli/error.json")


COMMENT_BREAKERS: dict[str, Callable[[str], dict[str, Any]]] = {
    "other_pr": lambda ref: builders.forgectl_comments([], pr=pr_ref(99)),
    "threads_not_a_list": lambda ref: {**builders.forgectl_comments([], pr=ref), "threads": "x"},
    "complete_not_bool": lambda ref: {**builders.forgectl_comments([], pr=ref), "complete": "yes"},
    "thread_without_comments": lambda ref: builders.forgectl_comments(
        [builders.thread("gh:rt:T1", [])], pr=ref
    ),
    "comment_id_forged": lambda ref: builders.forgectl_comments(
        [builders.thread("gh:rt:T1", [builders.comment("../../x", "b")])], pr=ref
    ),
    "body_not_text": lambda ref: builders.forgectl_comments(
        [builders.thread("gh:rt:T1", [{**builders.comment("gh:rc:C1", "b"), "body": 7}])], pr=ref
    ),
    "oversized_body": lambda ref: builders.forgectl_comments(
        [builders.thread("gh:rt:T1", [builders.comment("gh:rc:C1", "x" * (1024 * 1024 + 1))])],
        pr=ref,
    ),
    "duplicate_comment_id": lambda ref: builders.forgectl_comments(
        [
            builders.thread("gh:rt:T1", [builders.comment("gh:rc:C1", "a")]),
            builders.thread("gh:rt:T2", [builders.comment("gh:rc:C1", "b")]),
        ],
        pr=ref,
    ),
    "author_id_not_text": lambda ref: builders.forgectl_comments(
        [
            builders.thread(
                "gh:rt:T1",
                [
                    builders.comment(
                        "gh:rc:C1", "a", author={"login": "x", "id": 5, "is_bot": False}
                    )
                ],
            )
        ],
        pr=ref,
    ),
}


@pytest.mark.parametrize("case", sorted(COMMENT_BREAKERS))
def test_a_comment_read_that_does_not_fit_the_schema_is_lower_failed_and_changes_nothing(
    case: str, gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    ref = insert_pr(store, 7, comment_count=2)
    fakes.script("forgectl", "comments", json=COMMENT_BREAKERS[case](ref), schema=False)
    with pytest.raises(CliError) as caught:
        make(gateway, lease).read_comments(ref)
    assert caught.value.code == "lower_failed"
    assert caught.value.lower is not None and caught.value.lower["code"] == "bad_output"
    row = rows(store)[ref]
    assert (row["comment_count"], row["last_comments_read_at"], row["read_failures"]) == (
        2,
        None,
        1,
    )


def test_hostile_comment_text_is_carried_unchanged_and_never_in_argv(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    ref = insert_pr(store, 7)
    bodies = list(adversarial.FENCE_BREAKOUTS.values()) + list(
        adversarial.CONTROL_CHARACTERS.values()
    )
    threads = [
        builders.thread(f"gh:rt:T{i}", [builders.comment(f"gh:rc:C{i}", body)])
        for i, body in enumerate(bodies)
    ]
    script_comments(fakes, ref, builders.forgectl_comments(threads, pr=ref))
    read = make(gateway, lease).read_comments(ref)
    assert [c.body for c in read.comments] == bodies
    from tests.harness.fakes import assert_untrusted_only_in_files_or_stdin

    assert_untrusted_only_in_files_or_stdin(fakes.calls("forgectl"), bodies)


def test_unicode_and_empty_bodies_round_trip(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    ref = insert_pr(store, 7)
    bodies = ["", "naïve 日本語 \U0001f600", "‮evil", "a" * 100_000]
    threads = [
        builders.thread(f"gh:rt:T{i}", [builders.comment(f"gh:rc:C{i}", b)])
        for i, b in enumerate(bodies)
    ]
    script_comments(fakes, ref, builders.forgectl_comments(threads, pr=ref))
    assert [c.body for c in make(gateway, lease).read_comments(ref).comments] == bodies


def test_a_comment_read_of_an_unstored_pr_is_an_internal_error(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    with pytest.raises(CliError) as caught:
        make(gateway, lease).read_comments(pr_ref(7))
    assert caught.value.code == "internal"
    assert forgectl_calls(fakes, "comments") == []


def test_absent_ids_are_only_known_from_a_complete_read() -> None:
    stored = {"gh:rc:A", "gh:rc:B", "gh:rc:C"}
    comments = tuple(
        detect_mod.ReadComment(
            id=i, thread_id="gh:rt:T", author_id="x", author_login="x", author_is_bot=False,
            body="b", created_at=builders.TS, updated_at=builders.TS, minimized=False,
            reply_to=None, thread_resolved=False, thread_resolvable=True, thread_outdated=False,
            path=None, line=None,
        )
        for i in ("gh:rc:A",)
    )  # fmt: skip
    complete = CommentRead(pr=pr_ref(7), head_sha=builders.HEAD_A, complete=True, comments=comments)
    partial = CommentRead(pr=pr_ref(7), head_sha=builders.HEAD_A, complete=False, comments=comments)
    assert absent_comment_ids(stored, complete) == {"gh:rc:B", "gh:rc:C"}
    assert absent_comment_ids(stored, partial) == set()  # never mark anything deleted


def test_an_incomplete_read_is_returned_as_incomplete(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    ref = insert_pr(store, 7)
    script_comments(fakes, ref, builders.forgectl_comments([], pr=ref, complete=False))
    assert make(gateway, lease).read_comments(ref).complete is False


def test_the_on_read_seam_gets_each_successful_read_only(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    seen: list[tuple[str, int]] = []
    script_prs(fakes, [pr_item(1), pr_item(2)])
    script_comments(fakes, pr_ref(1), builders.real_shape_comments(pr_ref(1)))
    fakes.script("forgectl", "comments", json=builders.error_envelope("upstream_error", "x"),
                 exit=12, match={"positionals": {"pr": pr_ref(2)}})  # fmt: skip
    make(gateway, lease, on_read=lambda pr, read: seen.append((pr.pr, len(read.comments)))).run()
    assert seen == [(pr_ref(1), 5)]


# ---- installed_at --------------------------------------------------------------------------


def meta(store: Store, key: str) -> str | None:
    row = store.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def test_installed_at_is_stored_at_the_first_read_and_never_moves(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    ref = insert_pr(store, 7)
    assert meta(store, "installed_at") is None
    detection = make(gateway, lease)
    start = gateway.clock.now()
    gateway.clock.advance(5)  # type: ignore[attr-defined]  # the read happens after the start
    script_comments(fakes, ref)
    detection.read_comments(ref)
    assert meta(store, "installed_at") == start.strftime("%Y-%m-%dT%H:%M:%SZ")
    gateway.clock.advance(3600)  # type: ignore[attr-defined]
    script_comments(fakes, ref)
    make(gateway, lease).read_comments(ref)
    assert meta(store, "installed_at") == start.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_installed_at_is_not_stored_by_a_pass_that_read_nothing(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    script_prs(fakes, [])
    make(gateway, lease).run()
    assert meta(store, "installed_at") is None


def test_installed_at_survives_a_failed_first_read(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    ref = insert_pr(store, 7)
    fakes.script("forgectl", "comments", json=builders.error_envelope("upstream_error", "x"),
                 exit=12)  # fmt: skip
    with pytest.raises(CliError):
        make(gateway, lease).read_comments(ref)
    assert meta(store, "installed_at") is not None  # the command did try to read


# ---- leases, dependencies, gateway ---------------------------------------------------------


def test_writes_need_the_lease_a_lost_lease_aborts_with_lease_lost(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    script_prs(fakes, [pr_item(1)])
    detection = make(gateway, lease)
    lease.release()
    with pytest.raises(CliError) as caught:
        detection.run()
    assert caught.value.code == "lease_lost"
    assert rows(store) == {}


def test_a_missing_forgectl_is_tool_missing_before_any_call(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease, world: World
) -> None:
    world_path = world.env["PATH"]
    gateway.env["PATH"] = "/nonexistent-dir-for-test"
    try:
        with pytest.raises(CliError) as caught:
            make(gateway, lease).run()
    finally:
        gateway.env["PATH"] = world_path
    assert (caught.value.code, caught.value.exit_code) == ("tool_missing", 12)


def test_an_old_forgectl_is_tool_too_old(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease, world: World
) -> None:
    path = world.fakes.bin_dir / "forgectl"
    path.write_text("#!/bin/sh\necho 'forgectl 0.1.0'\n")
    path.chmod(0o755)
    with pytest.raises(CliError) as caught:
        make(gateway, lease).run()
    assert (caught.value.code, caught.value.exit_code) == ("tool_too_old", 12)


def test_the_dependency_check_runs_once_per_detection(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    script_prs(fakes, [pr_item(1)])
    script_comments(fakes, pr_ref(1))
    detection = make(gateway, lease)
    detection.run()
    script_comments(fakes, pr_ref(1))
    detection.read_comments(pr_ref(1))
    assert [c.outcome for c in fakes.calls("forgectl")].count("version") == 1


def test_every_forgectl_call_is_a_read(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    script_prs(fakes, [pr_item(1)])
    script_comments(fakes, pr_ref(1))
    make(gateway, lease).run()
    commands = {c.command for c in fakes.calls("forgectl") if c.command != "--version"}
    assert commands <= {"prs", "show", "comments", "whoami", "access"}
    assert not fakes.calls("pr-comment") and not fakes.calls("git")


def test_the_result_is_a_plain_value(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    script_prs(fakes, [])
    result = make(gateway, lease).run().hosts[0]
    assert isinstance(result, HostResult)
    assert result.warnings == ()
    assert result.listed == () and result.deferred == ()


def test_output_past_the_bound_fails_the_host_as_too_large(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    fakes.script("forgectl", "prs", stdout="x" * (9 * 1024 * 1024), schema=False,
                 match={"flags": {"--host": HOST}})  # fmt: skip
    result = make(gateway, lease).run().hosts[0]
    assert (result.ok, result.code) == (False, "output_too_large")
    assert rows(store) == {}


# ---- refresh_pr (the `show` read of check --pr and respond) -------------------------------


def test_refresh_pr_stores_the_show_facts(
    gateway: Gateway, fakes: Fakes, store: Store, lease: Lease
) -> None:
    ref = pr_ref(7)
    fakes.script("forgectl", "show", json=builders.forgectl_show(pr=ref, head_sha=builders.HEAD_B))
    stored = make(gateway, lease).refresh_pr(ref)
    assert (stored.pr, stored.head_sha, stored.is_fork, stored.host_state) == (
        ref,
        builders.HEAD_B,
        False,
        "open",
    )
    assert rows(store)[ref]["digest"] == pr_digest(ref)


@pytest.mark.parametrize(
    ("answer", "exit_code", "lower_code"),
    [
        (builders.error_envelope("not_found", "gone"), 10, "not_found"),
        (builders.forgectl_show(pr=pr_ref(99)), 0, "bad_output"),
    ],
    ids=["not_found", "another_pr"],
)
def test_refresh_pr_failure_is_lower_failed_and_stores_nothing(
    answer: dict[str, Any],
    exit_code: int,
    lower_code: str,
    gateway: Gateway,
    fakes: Fakes,
    store: Store,
    lease: Lease,
) -> None:
    fakes.script("forgectl", "show", json=answer, exit=exit_code, schema=False)
    with pytest.raises(CliError) as caught:
        make(gateway, lease).refresh_pr(pr_ref(7))
    assert caught.value.code == "lower_failed"
    assert caught.value.lower is not None and caught.value.lower["code"] == lower_code
    assert rows(store) == {}
