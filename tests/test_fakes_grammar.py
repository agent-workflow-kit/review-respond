"""Each fake refuses what the real sibling would refuse, and accepts what review-respond sends."""

from __future__ import annotations

import pytest

from review_respond.gateway import Gateway, parse_version

from .harness.fakes import Fakes

PR = "git.example.test/org/repo#7"
REPO = "git.example.test/org/repo"
SHA = "a" * 40
AG = "ag-aaaaaaaa"
JB = "jb-aaaaaaaa"
WT = "wt-aaaaaaaa"

# (tool, argv, expected). ``expected`` is a substring of the fake's reason: a case passes only
# when the rule under test fired, not because some other rule (such as the missing --json) did.
# Every case carries --json except the one that tests its absence.
YES = ["--yes"]
CREATE = ["create", "--throwaway", "--repo", REPO]
REPLY = ["reply", PR, "--to", "gh:ic:x", "--body-file", "-", "--yes", "--json"]
JOB_ADD = ["add", "--queue", "respond"]
REFUSED: list[tuple[str, list[str], str]] = [
    ("forgectl", ["prs", "--author", "me", "--bogus", "--json"], "unrecognized argument"),
    ("forgectl", ["prs", "--json"], "one of --author, --review-requested-from"),
    ("forgectl", ["prs", "--repo", REPO, "--host", "git.example.test", "--json"], "mutually"),
    ("forgectl", ["prs", "--author", "me", "--limit", "1001", "--json"], "out of range"),
    ("forgectl", ["prs", "--author", "me", "--limit", "0", "--json"], "out of range"),
    ("forgectl", ["prs", "--author", "me", "--state", "bogus", "--json"], "invalid choice"),
    ("forgectl", ["prs", "--author", "-x", "--json"], "expected one argument"),
    ("forgectl", ["prs", "--author", "trailing.", "--json"], "invalid value"),
    ("forgectl", ["prs", "--repo", "git.example.test/../x", "--json"], "invalid value"),
    ("forgectl", ["prs", "--repo", "git.example.test/-owner/x", "--json"], "invalid value"),
    ("forgectl", ["prs", "--repo", "git.example.test:0/o/r", "--json"], "invalid value"),
    (
        "forgectl",
        ["prs", "--host", "git.example.test:65536", "--author", "me", "--json"],
        "invalid",
    ),
    ("forgectl", ["prs", "--author", "me", "--updated-since", "0", "--json"], "invalid value"),
    ("forgectl", ["prs", "--author", "me", "--updated-since", "-5m", "--json"], "expected one"),
    ("forgectl", ["--timeout", "0", "prs", "--author", "me", "--json"], "invalid value"),
    ("forgectl", ["--config", "", "show", PR, "--json"], "invalid value"),
    ("forgectl", ["show", REPO, "--json"], "invalid value"),
    ("forgectl", ["show", "https://git.example.test/org/repo/issues/7", "--json"], "invalid value"),
    ("forgectl", ["show", "--json"], "required: pr"),
    ("forgectl", ["comments", f"{REPO}#07", "--json"], "invalid value"),
    ("forgectl", ["compare", PR, "--from", SHA, "--json"], "required: --to"),
    ("forgectl", ["compare", PR, "--from", "abc", "--to", SHA, "--json"], "invalid value"),
    ("forgectl", ["access", REPO, "--json"], "required: user"),
    ("forgectl", ["reviews", PR, "--json"], "invalid command"),
    ("forgectl", ["show", PR, "--yes", "--json"], "unrecognized argument"),
    ("forgectl", ["show", PR, "--json", "--json"], "more than once"),
    ("forgectl", ["show", PR], "always calls this program with --json"),
    ("forgectl", ["--verbose", "show", PR, "--quiet", "--json"], "mutually"),
    ("forgectl", ["show", PR, "--js", "--json"], "unrecognized argument"),
    ("forgectl", ["--version", "--bogus"], "--version takes no other argument"),
    ("forgectl", ["--version", "show"], "--version takes no other argument"),
    ("agent-spawn", ["resume", AG, "--fork", "--bogus", "--json"], "unrecognized argument"),
    ("agent-spawn", ["resume", "--fork", "--json"], "exactly one of"),
    ("agent-spawn", ["resume", AG, "--fork", "--select", "a=b", "--json"], "exactly one of"),
    ("agent-spawn", ["resume", "ag-AAAAAAAA", "--fork", "--json"], "invalid value"),
    ("agent-spawn", ["resume", AG, "--json"], "--fork is required"),
    ("agent-spawn", ["resume", AG, "--fork", "--wait", "--json"], "--wait is not accepted"),
    ("agent-spawn", ["resume", AG, "--fork", "--json"], "--kind is required here"),
    ("agent-spawn", ["launch", "--worktree", "none", "--json"], "--kind is required here"),
    (
        "agent-spawn",
        ["resume", AG, "--fork", "--prompt", "x", "--prompt-file", "/p", "--json"],
        "mutually",
    ),
    (
        "agent-spawn",
        ["resume", AG, "--fork", "--env-allowlist-only", "A", "B", "--json"],
        "unrecognized argument",
    ),
    (
        "agent-spawn",
        ["resume", AG, "--fork", "--env-allowlist-only", "A-B", "--json"],
        "invalid value",
    ),
    ("agent-spawn", ["resume", AG, "--fork", "--label", "agent=x", "--json"], "invalid value"),
    ("agent-spawn", ["resume", AG, "--fork", "--label", "owner", "--json"], "invalid value"),
    ("agent-spawn", ["resume", AG, "--fork", "--sandbox", "none", "--json"], "invalid choice"),
    ("agent-spawn", ["resume", AG, "--fork", "--no-promote", "--promote", "--json"], "mutually"),
    (
        "agent-spawn",
        ["resume", AG, "--fork", "--auto-exit", "--no-auto-exit", "--json"],
        "mutually",
    ),
    (
        "agent-spawn",
        ["resume", AG, "--fork", "--allow-tainted", "--actor", "auto:x", "--json"],
        "human-only",
    ),
    ("agent-spawn", ["resume", AG, "--fork", "--skill", ".x", "--json"], "invalid value"),
    ("agent-spawn", ["resume", AG, "--fork", "--skill", "-x", "--json"], "expected one argument"),
    ("agent-spawn", ["resume", AG, "--fork", "--timeout", "0", "--json"], "invalid value"),
    ("agent-spawn", ["resume", AG, "--fork", "--timeout", "0s", "--json"], "invalid value"),
    ("agent-spawn", ["resume", AG, "--fork", "--timeout", "40000d", "--json"], "over the maximum"),
    ("agent-spawn", ["resume", AG, "--fork", "--timeout", "1" * 33, "--json"], "invalid value"),
    ("agent-spawn", ["resume", AG, "--fork", "--cwd", "", "--json"], "invalid value"),
    ("agent-spawn", ["resume", AG, "--fork", "--prompt-file", "", "--json"], "invalid value"),
    (
        "agent-spawn",
        ["resume", AG, "--fork", "--permission-profile", "", "--json"],
        "invalid value",
    ),
    ("agent-spawn", ["resume", AG, "--fork", "--plugin-dir", "", "--json"], "invalid value"),
    ("agent-spawn", ["resume", AG, "--fork", "--input-file", "", "--json"], "invalid value"),
    ("agent-spawn", ["resume", AG, "--fork", "--hook", "pre-launch=x", "--json"], "invalid value"),
    ("agent-spawn", ["launch", "--hook", "bogus=x", "--json"], "invalid value"),
    ("agent-spawn", ["launch", "--worktree", "new", "--json"], "--repo is required"),
    ("agent-spawn", ["launch", "--branch", "b", "--json"], "--branch is not accepted"),
    ("agent-spawn", ["launch", "--implicit-done", "--no-implicit-done", "--json"], "mutually"),
    (
        "agent-spawn",
        ["launch", "--worktree", "none", "--mode", "headless", "--json"],
        "one of --skill, --prompt",
    ),
    (
        "agent-spawn",
        ["launch", "--worktree", "new", "--repo", REPO, "--cwd", "/abs", "--json"],
        "not accepted here",
    ),
    (
        "agent-spawn",
        ["launch", "--worktree", "/work", "--cwd", "session-dir", "--json"],
        "not accepted here",
    ),
    (
        "agent-spawn",
        ["launch", "--worktree", "/work", "--cwd", "../other", "--json"],
        "outside the worktree",
    ),
    (
        "agent-spawn",
        ["launch", "--worktree", "/work", "--cwd", "/elsewhere", "--json"],
        "outside the worktree",
    ),
    ("agent-spawn", ["wait", "--json"], "required: agent_id"),
    ("agent-spawn", ["wait", AG, "--wait-timeout", "5s", "--json"], "unrecognized argument"),
    ("agent-spawn", ["stop", AG, "--force", "--json"], "unrecognized argument"),
    ("agent-spawn", ["teardown", AG, "--force", "--actor", "auto:x", "--json"], "human-only"),
    ("agent-spawn", ["teardown", AG, "--hook", "post-launch=x", "--json"], "invalid value"),
    ("agent-spawn", ["prune", "--json"], "invalid command"),
    ("agent-spawn", ["show", AG, "--dry-run", "--json"], "unrecognized argument"),
    (
        "pr-comment",
        ["reply", PR, "--to", "gh:ic:x", "--body-file", "-", "--json"],
        "confirmation required",
    ),
    ("pr-comment", ["resolve", PR, "--thread", "gh:rt:x", "--json"], "confirmation required"),
    ("pr-comment", ["reply", PR, "--body-file", "-", "--yes", "--json"], "required: --to"),
    (
        "pr-comment",
        ["reply", PR, "--to", "bad", "--body-file", "-", "--yes", "--json"],
        "invalid value",
    ),
    ("pr-comment", ["reply", PR, "--to", "gh:ic:x", "--yes", "--json"], "required: --body-file"),
    (
        "pr-comment",
        ["reply", PR, "--to", "gh:ic:x", "--body-file", "-", "--yes", "--bogus", "--json"],
        "unrecognized argument",
    ),
    (
        "pr-comment",
        ["reply", PR, "--to", "gh:ic:x", "--body-file", "-x", "--yes", "--json"],
        "expected one argument",
    ),
    (
        "pr-comment",
        ["reply", PR, "--to", "gh:ic:x", "--body-file", "", "--yes", "--json"],
        "invalid value",
    ),
    ("pr-comment", [*REPLY, "--dedupe-key", "a/b"], "invalid value"),
    ("pr-comment", [*REPLY, "--dedupe-key", "k" * 129], "invalid value"),
    ("pr-comment", [*REPLY, "--hook", "bogus=x"], "invalid value"),
    (
        "pr-comment",
        [*REPLY, "--allow-mention", "someone", "--actor", "human:me"],
        "interactive-only",
    ),
    ("pr-comment", ["resolve", PR, "--yes", "--json"], "required: --thread"),
    ("pr-comment", ["resolve", PR, "--thread", "gh:ic:x", "--yes", "--json"], "invalid value"),
    (
        "pr-comment",
        ["resolve", PR, "--thread", "gh:rt:x", "--yes", "--body-file", "-", "--json"],
        "unrecognized argument",
    ),
    (
        "pr-comment",
        ["resolve", PR, "--thread", "gh:rt:x", "--expect-plan", "0" * 64, "--yes", "--json"],
        "unrecognized argument",
    ),
    ("pr-comment", ["approve", PR, "--yes", "--json"], "invalid command"),
    ("pr-comment", ["--", "reply", PR, "--json"], "'--' before the command"),
    ("pr-comment", [*REPLY, "--actor", "x"], "invalid value"),
    ("pr-comment", [*REPLY, "--override-escalation=forgectl", "--actor", "auto:x"], "human-only"),
    ("wtctl", [*CREATE, "--json"], "--ref is required"),
    ("wtctl", [*CREATE, "--ref", SHA, "--branch", "b", "--json"], "--branch is not accepted"),
    (
        "wtctl",
        ["create", "--repo", REPO, "--source", "remote", "--ref", SHA, "--json"],
        "--source is not accepted",
    ),
    ("wtctl", [*CREATE, "--ref", "abc", "--json"], "invalid value"),
    ("wtctl", ["create", "--throwaway", "--ref", SHA, "--json"], "required: --repo"),
    ("wtctl", [*CREATE, "--ref", SHA, "--label", "A=b", "--json"], "invalid value"),
    ("wtctl", [*CREATE, "--ref", SHA, "--hook", "pre-remove=x", "--json"], "invalid value"),
    ("wtctl", ["extract", WT, "--dest", "/d", "--json"], "one of --files-from, --delete-from"),
    (
        "wtctl",
        ["extract", WT, "--files-from", "-", "--delete-from", "-", "--dest", "/d", "--json"],
        "cannot both read",
    ),
    ("wtctl", ["extract", WT, "--files-from", "/f", "--json"], "required: --dest"),
    (
        "wtctl",
        ["extract", WT, "--files-from", "-x", "--dest", "/d", "--json"],
        "expected one argument",
    ),
    ("wtctl", ["extract", WT, "--files-from", "", "--dest", "/d", "--json"], "invalid value"),
    (
        "wtctl",
        ["extract", "rel/path", "--files-from", "/f", "--dest", "/d", "--json"],
        "invalid value",
    ),
    ("wtctl", ["extract", "ab", "--files-from", "/f", "--dest", "/d", "--json"], "invalid value"),
    ("wtctl", ["remove", WT, "--json"], "confirmation required"),
    ("wtctl", ["remove", WT, "--force", "--yes", "--json"], "human-only"),
    ("wtctl", ["remove", WT, "--dry-run", "--bogus", "--json"], "unrecognized argument"),
    ("wtctl", ["remove", WT, "--yes", "--hook", "pre-create=x", "--json"], "invalid value"),
    ("wtctl", ["show", "foo/bar", "--json"], "invalid value"),
    ("wtctl", ["label", "add", WT, "--json"], "required: label"),
    ("wtctl", ["label", "add", WT, "Bad Key=v", "--json"], "invalid value"),
    ("wtctl", ["label", "--json"], "a subcommand is required"),
    ("wtctl", ["list", "--throwaways", "--worktrees", "--json"], "mutually"),
    ("wtctl", ["list", "--state", "bogus", "--json"], "invalid choice"),
    ("wtctl", ["locate", "not a repo", "--json"], "invalid value"),
    ("wtctl", ["status", WT, "--json"], "invalid command"),
    ("wtctl", ["list", "--dry-run", "--json"], "unrecognized argument"),
    ("jobsctl", [*JOB_ADD, "--json"], "after '--' is required"),
    ("jobsctl", ["add", "--json", "--"], "after '--' is required"),
    ("jobsctl", ["add", "--queue", "Bad Q", "--json", "--", "x"], "invalid value"),
    ("jobsctl", ["add", "--dry-run", "--wait", "--json", "--", "x"], "mutually"),
    ("jobsctl", ["add", "--max-retries", "101", "--json", "--", "x"], "invalid value"),
    ("jobsctl", ["add", "--cwd", "rel", "--json", "--", "x"], "invalid value"),
    ("jobsctl", ["add", "--env", "NOEQ", "--json", "--", "x"], "invalid value"),
    ("jobsctl", ["add", "--env", "A=1", "--env", "A=2", "--json", "--", "x"], "a name given twice"),
    (
        "jobsctl",
        ["add", "--env", "A=1", "--pass-env", "A", "--json", "--", "x"],
        "both --env and --pass-env",
    ),
    ("jobsctl", ["add", "--priority", "1001", "--json", "--", "x"], "out of range"),
    ("jobsctl", ["add", "--priority", "-1001", "--json", "--", "x"], "out of range"),
    ("jobsctl", ["add", "--timeout", "0", "--json", "--", "x"], "invalid value"),
    ("jobsctl", ["add", "--timeout", "0s", "--json", "--", "x"], "invalid value"),
    ("jobsctl", ["add", "--timeout", "40000d", "--json", "--", "x"], "over the maximum"),
    ("jobsctl", ["add", "--unique-key", "", "--json", "--", "x"], "invalid value"),
    ("jobsctl", ["add", "--unique-key", "a b", "--json", "--", "x"], "invalid value"),
    ("jobsctl", ["add", "--unique-key", "k" * 257, "--json", "--", "x"], "invalid value"),
    ("jobsctl", ["--actor", "auto:x", "add", "--json", "--", "x"], "must follow the command"),
    ("jobsctl", ["show", "--json"], "exactly one of"),
    ("jobsctl", ["show", JB, "--unique-key", "k", "--json"], "exactly one of"),
    ("jobsctl", ["cancel", "--all", "--json"], "one of --queue, --label, --unique-key"),
    ("jobsctl", ["cancel", JB, "--all", "--queue", "q", "--json"], "cannot be combined"),
    ("jobsctl", ["resolve", JB, "--as", "bogus", "--json"], "invalid choice"),
    ("jobsctl", ["release", JB, "--json"], "invalid command"),
    ("jobsctl", ["show", JB, "--dry-run", "--json"], "unrecognized argument"),
    ("claude-session", ["kinds", "--bogus", "--json"], "unrecognized argument"),
    ("claude-session", ["--json", "kinds"], "must follow the command"),
    ("claude-session", ["kinds", "--repo", "bad repo", "--json"], "invalid value"),
    ("claude-session", ["status", "--json"], "invalid command"),
    ("claude-session", ["kinds", "--kind", "Bad Kind", "--json"], "invalid value"),
]

# What the real tools accept and the fakes must not refuse (the call then parses and, as nobody
# scripted it, is an "unscripted" violation: that proves the grammar let it through).
ACCEPTED: list[tuple[str, list[str], str]] = [
    ("forgectl", ["prs", "--author", "me", "--limit", "05", "--json"], "leading zero in --limit"),
    ("forgectl", ["prs", "--author", "ME", "--json"], "an upper-case login"),
    ("forgectl", ["--json", "show", PR], "global flags before the command"),
    ("forgectl", ["show", "https://git.example.test/org/repo/pull/7", "--json"], "a PR URL"),
    ("agent-spawn", ["--json", "show", AG], "a global flag before the command"),
    (
        "agent-spawn",
        ["--actor", "auto:x", "resume", AG, "--fork", "--kind", "respond", "--json"],
        "--actor before",
    ),
    (
        "agent-spawn",
        ["resume", AG, "--fork", "--kind", "respond", "--wait-timeout", "5s", "--json"],
        "--wait-timeout alone",
    ),
    (
        "agent-spawn",
        ["resume", "--fork", "--select", "kind=x", "--kind", "respond", "--json"],
        "--select kind=x",
    ),
    (
        "agent-spawn",
        ["resume", AG, "--fork", "--max-turns", "05", "--kind", "respond", "--json"],
        "--max-turns 05",
    ),
    (
        "agent-spawn",
        [
            "resume",
            AG,
            "--fork",
            "--override-escalation",
            "--actor",
            "human:me",
            "--kind",
            "respond",
            "--json",
        ],
        "override with a human actor",
    ),
    (
        "agent-spawn",
        ["launch", "--worktree", "/work", "--cwd", "sub/dir", "--kind", "respond", "--json"],
        "cwd inside the worktree",
    ),
    ("jobsctl", ["--json", "show", JB], "--json before the command"),
    ("jobsctl", ["add", "--wait-timeout", "5s", "--json", "--", "x"], "--wait-timeout alone"),
    ("jobsctl", ["cancel", JB, "--force", "--json"], "--force without a human actor"),
    ("wtctl", ["show", "wt-ABCDEFGHIJ", "--json"], "an upper-case selector"),
    ("wtctl", ["list", "--state", "partial", "--state", "gone", "--json"], "enumerated states"),
    (
        "pr-comment",
        [*REPLY, "--hook", "post-post=true", "--dedupe-key", "respond:p-1:k"],
        "post-post hook",
    ),
]


@pytest.mark.parametrize(
    ("tool", "argv", "expected"),
    REFUSED,
    ids=[f"{t}:{e}:{i}" for i, (t, _, e) in enumerate(REFUSED)],
)
def test_fake_refuses_what_the_real_sibling_refuses(
    gateway: Gateway, fakes: Fakes, tool: str, argv: list[str], expected: str
) -> None:
    result = gateway.run([tool, *argv], timeout=20)
    assert result.returncode in (2, 13), result.stderr
    violations = fakes.take_violations()
    assert [v["kind"] for v in violations] == ["usage"], violations
    assert violations[0]["argv"] == argv
    assert expected in violations[0]["message"], violations[0]["message"]
    # With --json the refusal is the sibling's envelope: usage, or confirmation_required (13).
    if "--json" in argv and argv.count("--json") == 1:
        code = "confirmation_required" if expected.startswith("confirmation") else "usage"
        assert result.json["error"]["code"] == code
        assert result.returncode == (13 if code == "confirmation_required" else 2)


@pytest.mark.parametrize(
    ("tool", "argv", "why"), ACCEPTED, ids=[f"{t}:{w}:{i}" for i, (t, _, w) in enumerate(ACCEPTED)]
)
def test_fake_accepts_what_the_real_sibling_accepts(
    gateway: Gateway, fakes: Fakes, tool: str, argv: list[str], why: str
) -> None:
    gateway.run([tool, *argv], timeout=20)
    assert [v["kind"] for v in fakes.take_violations()] == ["unscripted"], why


def test_every_tool_has_refusal_cases() -> None:
    for tool in ("forgectl", "agent-spawn", "pr-comment", "wtctl", "jobsctl", "claude-session"):
        assert sum(t == tool for t, _, _ in REFUSED) >= 4, tool


@pytest.mark.parametrize(
    "tool", ["forgectl", "agent-spawn", "pr-comment", "wtctl", "jobsctl", "claude-session"]
)
def test_each_fake_rejects_an_undocumented_flag(gateway: Gateway, fakes: Fakes, tool: str) -> None:
    command = {
        "forgectl": ["show", PR],
        "agent-spawn": ["show", AG],
        "pr-comment": ["resolve", PR, "--thread", "gh:rt:x", "--yes"],
        "wtctl": ["show", WT],
        "jobsctl": ["show", JB],
        "claude-session": ["kinds"],
    }[tool]
    result = gateway.run([tool, *command, "--definitely-not-a-flag", "--json"], timeout=20)
    assert result.returncode == 2
    assert [v["kind"] for v in fakes.take_violations()] == ["usage"]


def test_a_well_formed_call_nobody_scripted_is_a_violation(gateway: Gateway, fakes: Fakes) -> None:
    result = gateway.forgectl.show(PR)
    assert result.returncode == 99
    assert [v["kind"] for v in fakes.take_violations()] == ["unscripted"]


def test_a_test_that_leaves_a_violation_fails_at_teardown(gateway: Gateway, fakes: Fakes) -> None:
    gateway.run(["forgectl", "show", "--json"], timeout=20)
    with pytest.raises(AssertionError, match="real sibling would refuse"):
        fakes.assert_clean()
    fakes.take_violations()
    fakes.assert_clean()


def test_scripted_answers_are_consumed_in_order_and_run_out(gateway: Gateway, fakes: Fakes) -> None:
    fakes.script("jobsctl", "show", json={"a": 1}, schema=False, times=2)
    assert gateway.jobsctl.show(JB).json == {"a": 1}
    assert gateway.jobsctl.show(JB).json == {"a": 1}
    assert gateway.jobsctl.show(JB).returncode == 99
    fakes.take_violations()
    assert fakes.remaining() == []


def test_script_matching_uses_positionals_and_flags(gateway: Gateway, fakes: Fakes) -> None:
    fakes.script(
        "wtctl", "show", json={"which": "b"}, schema=False,
        match={"positionals": {"selector": "wt-bbbbbbbb"}},
    )  # fmt: skip
    assert gateway.wtctl.show("wt-bbbbbbbb").json == {"which": "b"}
    assert gateway.wtctl.show("wt-cccccccc").returncode == 99
    fakes.take_violations()


def test_scripted_json_is_validated_against_the_pinned_schema(fakes: Fakes) -> None:
    from jsonschema import ValidationError

    with pytest.raises(ValidationError):
        fakes.script("forgectl", "whoami", json={"hosts": "not a list", "errors": []})
    with pytest.raises(ValidationError):
        fakes.script("wtctl", "show", json={"not": "a record"}, exit=0)
    with pytest.raises(ValidationError):
        fakes.script("wtctl", "show", json={"no": "code"}, exit=10)


def test_version_is_answered_without_a_script(gateway: Gateway, fakes: Fakes) -> None:
    assert parse_version(gateway.forgectl.version().stdout) == (0, 55, 2)
    assert parse_version(gateway.agent_spawn.version().stdout) == (0, 34, 0)
    assert parse_version(gateway.pr_comment.version().stdout) == (0, 33, 0)
    assert parse_version(gateway.wtctl.version().stdout) == (0, 31, 0)
    assert parse_version(gateway.jobsctl.version().stdout) == (0, 32, 1)
    assert parse_version(gateway.claude_session.version().stdout) == (0, 45, 2)
    assert fakes.violations() == []


def test_a_json_lookalike_in_a_flag_value_is_not_the_json_flag(
    gateway: Gateway, fakes: Fakes
) -> None:
    result = gateway.run(
        ["wtctl", "extract", WT, "--files-from=--json", "--dest", "/d"], timeout=20
    )
    assert result.returncode == 2
    (violation,) = fakes.take_violations()
    assert "always calls this program with --json" in violation["message"]


def test_a_json_lookalike_in_the_job_tail_is_not_the_json_flag(
    gateway: Gateway, fakes: Fakes
) -> None:
    gateway.run(
        ["jobsctl", "add", "--queue", "respond", "--", "review-respond", "--json"], timeout=20
    )
    (violation,) = fakes.take_violations()
    assert "always calls this program with --json" in violation["message"]


def test_stdin_is_read_for_a_flag_value_of_dash_in_either_spelling(
    gateway: Gateway, fakes: Fakes
) -> None:
    fakes.script("wtctl", "extract", json={}, schema=False, times=2)
    for form in (["--files-from", "-"], ["--files-from=-"]):
        gateway.run(
            ["wtctl", "extract", WT, *form, "--dest", "/d", "--json"], stdin="a.txt\n", timeout=20
        )
    assert [c.stdin for c in fakes.calls("wtctl")] == ["a.txt\n", "a.txt\n"]
    assert fakes.violations() == []


def test_stdin_the_grammar_never_reads_is_a_violation(gateway: Gateway, fakes: Fakes) -> None:
    fakes.script("wtctl", "show", json={}, schema=False)
    gateway.run(["wtctl", "show", WT, "--json"], stdin="surprise", timeout=20)
    (violation,) = fakes.take_violations()
    assert violation["kind"] == "stdin"
    assert fakes.calls("wtctl")[0].stdin == "surprise"


SCHEMA_CALLS = {
    "forgectl": ["show", PR, "--json"],
    "agent-spawn": ["show", AG, "--json"],
    "pr-comment": ["resolve", PR, "--thread", "gh:rt:x", "--yes", "--json"],
    "wtctl": ["show", WT, "--json"],
    "jobsctl": ["show", JB, "--json"],
    "claude-session": ["kinds", "--json"],
}


@pytest.mark.parametrize("tool", sorted(SCHEMA_CALLS))
def test_the_fakes_error_envelopes_match_the_pinned_error_schema(
    gateway: Gateway, fakes: Fakes, tool: str
) -> None:
    from .harness import schemas

    usage = gateway.run([tool, "--definitely-not-a-flag", "--json"], timeout=20)
    assert usage.returncode == 2
    schemas.validate(f"{tool}/cli/error.json", usage.json)
    assert usage.json["error"]["code"] == "usage"
    internal = gateway.run([tool, *SCHEMA_CALLS[tool]], timeout=20)
    assert internal.returncode == 99
    schemas.validate(f"{tool}/cli/error.json", internal.json)
    assert internal.json["error"]["code"] == "internal"
    assert {v["kind"] for v in fakes.take_violations()} == {"usage", "unscripted"}
