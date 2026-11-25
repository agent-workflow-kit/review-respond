"""Each fake refuses what the real sibling would refuse, and accepts what review-respond sends."""

from __future__ import annotations

import pytest

from review_respond.gateway import Gateway

from .harness.fakes import Fakes

PR = "git.example.test/org/repo#7"
REPO = "git.example.test/org/repo"
SHA = "a" * 40
AG = "ag-aaaaaaaa"
JB = "jb-aaaaaaaa"
WT = "wt-aaaaaaaa"

# (tool, argv, why). Exit 2 unless the case is a jobsctl human-only refusal (13).
REFUSED: list[tuple[str, list[str], str]] = [
    ("forgectl", ["prs", "--author", "me", "--bogus", "--json"], "unknown flag"),
    ("forgectl", ["prs", "--json"], "no --repo, --author or --review-requested-from"),
    (
        "forgectl",
        ["prs", "--repo", REPO, "--host", "git.example.test", "--json"],
        "--repo + --host",
    ),
    ("forgectl", ["prs", "--author", "me", "--limit", "1001", "--json"], "limit over 1000"),
    ("forgectl", ["prs", "--author", "me", "--state", "bogus", "--json"], "bad choice"),
    ("forgectl", ["prs", "--author", "-x", "--json"], "value that looks like an option"),
    ("forgectl", ["show", REPO, "--json"], "PR reference without #N"),
    ("forgectl", ["show", "--json"], "missing PR"),
    ("forgectl", ["comments", f"{REPO}#07", "--json"], "leading zero in the number"),
    ("forgectl", ["compare", PR, "--from", SHA, "--json"], "missing --to"),
    ("forgectl", ["compare", PR, "--from", "abc", "--to", SHA, "--json"], "short sha"),
    ("forgectl", ["access", REPO, "--json"], "missing user"),
    ("forgectl", ["reviews", PR, "--json"], "command review-respond does not use"),
    ("forgectl", ["show", PR, "--yes", "--json"], "write-only flag on a read"),
    ("forgectl", ["show", PR, "--json", "--json"], "flag given twice"),
    ("forgectl", ["show", PR], "no --json"),
    ("forgectl", ["--verbose", "show", PR, "--quiet", "--json"], "--verbose with --quiet"),
    ("forgectl", ["show", PR, "--js", "--json"], "abbreviated flag"),
    ("agent-spawn", ["resume", AG, "--fork", "--bogus", "--json"], "unknown flag"),
    ("agent-spawn", ["resume", "--fork", "--json"], "no agent id and no --select"),
    ("agent-spawn", ["resume", AG, "--select", "a=b", "--json"], "id and --select"),
    ("agent-spawn", ["resume", "ag-AAAAAAAA", "--json"], "bad agent id"),
    ("agent-spawn", ["resume", AG, "--prompt", "x", "--prompt-file", "/p", "--json"], "exclusive"),
    ("agent-spawn", ["resume", AG, "--env-allowlist-only", "A", "B", "--json"], "nargs form"),
    ("agent-spawn", ["resume", AG, "--env-allowlist-only", "A-B", "--json"], "bad env name"),
    ("agent-spawn", ["resume", AG, "--label", "agent=x", "--json"], "reserved label key"),
    ("agent-spawn", ["resume", AG, "--label", "owner", "--json"], "label without ="),
    ("agent-spawn", ["resume", AG, "--sandbox", "none", "--json"], "bad choice"),
    ("agent-spawn", ["resume", AG, "--no-promote", "--promote", "--json"], "exclusive"),
    ("agent-spawn", ["resume", AG, "--auto-exit", "--no-auto-exit", "--json"], "exclusive"),
    ("agent-spawn", ["resume", AG, "--allow-tainted", "--actor", "auto:x", "--json"], "human-only"),
    ("agent-spawn", ["launch", "--worktree", "new", "--json"], "--worktree new needs --repo"),
    ("agent-spawn", ["launch", "--branch", "b", "--json"], "--branch without --worktree new"),
    ("agent-spawn", ["launch", "--implicit-done", "--no-implicit-done", "--json"], "exclusive"),
    ("agent-spawn", ["wait", "--json"], "missing agent id"),
    ("agent-spawn", ["wait", AG, "--wait-timeout", "5s", "--json"], "launch flag on wait"),
    ("agent-spawn", ["stop", AG, "--force", "--json"], "teardown flag on stop"),
    ("agent-spawn", ["teardown", AG, "--force", "--actor", "auto:x", "--json"], "human-only"),
    ("agent-spawn", ["--json", "show", AG], "global flag before the command"),
    ("agent-spawn", ["prune", "--json"], "command review-respond does not use"),
    ("agent-spawn", ["show", AG, "--dry-run", "--json"], "--dry-run on a read"),
    ("pr-comment", ["reply", PR, "--to", "gh:ic:x", "--body-file", "-", "--json"], "no --yes"),
    ("pr-comment", ["reply", PR, "--body-file", "-", "--yes", "--json"], "missing --to"),
    ("pr-comment", ["reply", PR, "--to", "bad", "--body-file", "-", "--yes", "--json"], "bad id"),
    ("pr-comment", ["reply", PR, "--to", "gh:ic:x", "--yes", "--json"], "missing --body-file"),
    (
        "pr-comment",
        ["reply", PR, "--to", "gh:ic:x", "--body-file", "-", "--yes", "--bogus"],
        "flag",
    ),
    ("pr-comment", ["resolve", PR, "--yes", "--json"], "missing --thread"),
    ("pr-comment", ["resolve", PR, "--thread", "gh:ic:x", "--yes", "--json"], "not a thread id"),
    (
        "pr-comment",
        ["resolve", PR, "--thread", "gh:rt:x", "--yes", "--body-file", "-"],
        "reply flag",
    ),
    ("pr-comment", ["resolve", PR, "--thread", "gh:rt:x", "--expect-plan", "0" * 64, "--yes"], "x"),
    ("pr-comment", ["approve", PR, "--yes", "--json"], "command review-respond does not use"),
    ("pr-comment", ["--", "reply", PR], "-- before the command"),
    (
        "pr-comment",
        ["reply", PR, "--to", "gh:ic:x", "--body-file", "-", "--yes", "--actor", "x"],
        "actor",
    ),
    (
        "pr-comment",
        [
            "reply",
            PR,
            "--to",
            "gh:ic:x",
            "--body-file",
            "-",
            "--yes",
            "--override-escalation=forgectl",
            "--actor",
            "auto:x",
            "--json",
        ],
        "human-only",
    ),  # fmt: skip
    ("wtctl", ["create", "--throwaway", "--repo", REPO, "--json"], "throwaway without --ref"),
    (
        "wtctl",
        ["create", "--throwaway", "--repo", REPO, "--ref", SHA, "--branch", "b", "--json"],
        "x",
    ),
    (
        "wtctl",
        ["create", "--repo", REPO, "--source", "remote", "--json"],
        "--source without --throwaway",
    ),
    ("wtctl", ["create", "--throwaway", "--repo", REPO, "--ref", "abc", "--json"], "short sha"),
    ("wtctl", ["create", "--throwaway", "--ref", SHA, "--json"], "missing --repo"),
    (
        "wtctl",
        ["create", "--throwaway", "--repo", REPO, "--ref", SHA, "--label", "A=b", "--json"],
        "x",
    ),
    ("wtctl", ["extract", WT, "--dest", "/d", "--json"], "no list"),
    (
        "wtctl",
        ["extract", WT, "--files-from", "-", "--delete-from", "-", "--dest", "/d"],
        "stdin twice",
    ),
    ("wtctl", ["extract", WT, "--files-from", "/f", "--json"], "missing --dest"),
    (
        "wtctl",
        ["extract", "rel/path", "--files-from", "/f", "--dest", "/d", "--json"],
        "relative path",
    ),
    ("wtctl", ["remove", WT, "--force", "--json"], "human-only"),
    ("wtctl", ["remove", WT, "--dry-run", "--bogus", "--json"], "unknown flag"),
    ("wtctl", ["show", "foo/bar", "--json"], "relative path selector"),
    ("wtctl", ["label", "add", WT, "--json"], "no labels"),
    ("wtctl", ["label", "add", WT, "Bad Key=v", "--json"], "bad label"),
    ("wtctl", ["label", "--json"], "no subcommand"),
    ("wtctl", ["list", "--throwaways", "--worktrees", "--json"], "exclusive"),
    ("wtctl", ["locate", "not a repo", "--json"], "bad repo id"),
    ("wtctl", ["status", WT, "--json"], "command review-respond does not use"),
    ("wtctl", ["list", "--dry-run", "--json"], "--dry-run on a command without it"),
    ("jobsctl", ["add", "--queue", "respond", "--json"], "no -- argv"),
    ("jobsctl", ["add", "--json", "--"], "empty argv"),
    ("jobsctl", ["add", "--queue", "Bad Q", "--json", "--", "x"], "bad queue"),
    ("jobsctl", ["add", "--dry-run", "--wait", "--json", "--", "x"], "exclusive"),
    ("jobsctl", ["add", "--wait-timeout", "5s", "--json", "--", "x"], "needs --wait"),
    ("jobsctl", ["add", "--max-retries", "101", "--json", "--", "x"], "over 100"),
    ("jobsctl", ["add", "--cwd", "rel", "--json", "--", "x"], "relative cwd"),
    ("jobsctl", ["add", "--env", "NOEQ", "--json", "--", "x"], "bad env"),
    ("jobsctl", ["show", "--json"], "no id"),
    ("jobsctl", ["show", JB, "--unique-key", "k", "--json"], "id and --unique-key"),
    ("jobsctl", ["cancel", JB, "--force", "--json"], "human-only"),
    ("jobsctl", ["cancel", "--all", "--json"], "--all without a selector"),
    ("jobsctl", ["cancel", JB, "--all", "--queue", "q", "--json"], "id with selectors"),
    ("jobsctl", ["resolve", JB, "--as", "bogus", "--json"], "bad choice"),
    ("jobsctl", ["release", JB, "--json"], "command review-respond does not use"),
    ("jobsctl", ["--json", "show", JB], "global flag before the command"),
    ("jobsctl", ["show", JB, "--dry-run", "--json"], "--dry-run on a read"),
    ("claude-session", ["kinds", "--bogus", "--json"], "unknown flag"),
    ("claude-session", ["--json", "kinds"], "global flag before the command"),
    ("claude-session", ["kinds", "--repo", "bad repo", "--json"], "bad repo id"),
    ("claude-session", ["status", "--json"], "command review-respond does not use"),
    ("claude-session", ["kinds", "--kind", "Bad Kind", "--json"], "bad kind"),
]


@pytest.mark.parametrize(
    ("tool", "argv", "why"), REFUSED, ids=[f"{t}:{w}:{i}" for i, (t, _, w) in enumerate(REFUSED)]
)
def test_fake_refuses_what_the_real_sibling_refuses(
    gateway: Gateway, fakes: Fakes, tool: str, argv: list[str], why: str
) -> None:
    result = gateway.run([tool, *argv], timeout=20)
    assert result.returncode in (2, 13), result.stderr
    violations = fakes.take_violations()
    assert [v["kind"] for v in violations] == ["usage"], violations
    assert violations[0]["argv"] == argv
    # With --json the refusal is the sibling's usage envelope.
    if "--json" in argv:
        assert result.json["error"]["code"] == "usage"


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
    assert gateway.version("forgectl") == (0, 55, 2)
    assert gateway.version("agent-spawn") == (0, 34, 0)
    assert gateway.version("pr-comment") == (0, 33, 0)
    assert gateway.version("wtctl") == (0, 31, 0)
    assert gateway.version("jobsctl") == (0, 32, 1)
    assert gateway.version("claude-session") == (0, 45, 2)
    assert fakes.violations() == []
