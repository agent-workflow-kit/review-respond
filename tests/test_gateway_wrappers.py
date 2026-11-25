"""The gateway wrappers send the documented command lines and keep untrusted text off argv."""

from __future__ import annotations

import os

import pytest

from review_respond.gateway import Gateway, GatewayError, parse_version

from .harness import adversarial
from .harness.clock import FakeClock
from .harness.fakes import Fakes, assert_untrusted_only_in_files_or_stdin
from .harness.plugin import World

PR = "git.example.test/org/repo#7"
REPO = "git.example.test/org/repo"
SHA = "a" * 40
AG = "ag-aaaaaaaa"


def accept(fakes: Fakes, tool: str, command: str, **kwargs: object) -> None:
    """Script a placeholder answer; the content is not under test, the command line is."""
    fakes.script(tool, command, json={}, schema=False, **kwargs)  # type: ignore[arg-type]


def only_call(fakes: Fakes, tool: str) -> list[str]:
    calls = fakes.calls(tool)
    assert len(calls) == 1, calls
    return calls[0].argv


def test_forgectl_wrappers(gateway: Gateway, fakes: Fakes) -> None:
    for command in ("prs", "show", "comments", "access", "whoami", "compare"):
        accept(fakes, "forgectl", command)
    gateway.forgectl.prs(author="me", state="open", limit=50)
    gateway.forgectl.show(PR)
    gateway.forgectl.comments(PR)
    gateway.forgectl.access(REPO, "reviewer-one")
    gateway.forgectl.whoami(host="git.example.test")
    gateway.forgectl.compare(PR, SHA, "b" * 40)
    argv = [c.argv for c in fakes.calls("forgectl")]
    assert argv == [
        ["prs", "--author", "me", "--state", "open", "--limit", "50", "--json"],
        ["show", PR, "--json"],
        ["comments", PR, "--json"],
        ["access", REPO, "reviewer-one", "--json"],
        ["whoami", "--host", "git.example.test", "--json"],
        ["compare", "--no-diff", PR, "--from", SHA, "--to", "b" * 40, "--json"],
    ]


FORK_KWARGS = dict(
    cwd="/work/throwaway",
    kind="respond",
    mode="headless",
    skill="respond-to-review",
    plugin_dir="/plugin",
    prompt_file="/state/prompt.md",
    permission_profile="/state/profile.json",
    sandbox="required",
    setting_sources=[],
    env_allowlist=[],
    labels=[("pr", PR), ("owner", "review-respond"), ("repo", REPO), ("round", "r-1")],
    actor="auto:review-respond",
)


def test_agent_spawn_fork_matches_the_interface_command_line(
    gateway: Gateway, fakes: Fakes
) -> None:
    accept(fakes, "agent-spawn", "resume")
    gateway.agent_spawn.resume_fork(AG, **FORK_KWARGS)  # type: ignore[arg-type]
    assert only_call(fakes, "agent-spawn") == [
        "resume", AG, "--fork", "--cwd", "/work/throwaway",
        "--kind", "respond", "--mode", "headless", "--skill", "respond-to-review",
        "--plugin-dir", "/plugin", "--prompt-file", "/state/prompt.md",
        "--permission-profile", "/state/profile.json", "--sandbox", "required",
        "--setting-sources", "", "--env-allowlist-only", "",
        "--no-promote", "--no-implicit-done",
        "--label", f"pr={PR}", "--label", "owner=review-respond", "--label", f"repo={REPO}",
        "--label", "round=r-1", "--actor", "auto:review-respond", "--json",
    ]  # fmt: skip


def test_agent_spawn_fork_with_allowlist_model_and_timeout(gateway: Gateway, fakes: Fakes) -> None:
    accept(fakes, "agent-spawn", "resume")
    kwargs = dict(FORK_KWARGS, env_allowlist=["PATH", "HOME"], setting_sources=["user", "project"])
    gateway.agent_spawn.resume_fork(AG, model="haiku", timeout="30m", **kwargs)  # type: ignore[arg-type]
    argv = only_call(fakes, "agent-spawn")
    assert ["--env-allowlist-only", "PATH", "--env-allowlist-only", "HOME"] == argv[
        argv.index("--env-allowlist-only") : argv.index("--env-allowlist-only") + 4
    ]
    assert (
        "--setting-sources" in argv and argv[argv.index("--setting-sources") + 1] == "user,project"
    )
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--timeout") + 1] == "30m"
    assert "--wait" not in argv


def test_agent_spawn_launch_and_the_rest(gateway: Gateway, fakes: Fakes) -> None:
    for command in ("launch", "wait", "show", "list", "stop", "teardown"):
        accept(fakes, "agent-spawn", command)
    kwargs = {k: v for k, v in FORK_KWARGS.items()}
    gateway.agent_spawn.launch(repo=REPO, **kwargs)  # type: ignore[arg-type]
    gateway.agent_spawn.wait(AG)
    gateway.agent_spawn.show(AG)
    gateway.agent_spawn.list(labels=[("round", "r-1")])
    gateway.agent_spawn.stop(AG, actor="auto:review-respond", grace="125s")
    gateway.agent_spawn.teardown(AG, actor="auto:review-respond")
    calls = fakes.calls("agent-spawn")
    assert calls[0].argv[:7] == [
        "launch",
        "--worktree",
        "none",
        "--cwd",
        "/work/throwaway",
        "--repo",
        REPO,
    ]
    assert [c.argv for c in calls[1:]] == [
        ["wait", AG, "--json"],
        ["show", AG, "--json"],
        ["list", "--label", "round=r-1", "--json"],
        ["stop", AG, "--grace", "125s", "--actor", "auto:review-respond", "--json"],
        ["teardown", AG, "--actor", "auto:review-respond", "--json"],
    ]


def test_pr_comment_reply_sends_the_body_on_stdin(gateway: Gateway, fakes: Fakes) -> None:
    accept(fakes, "pr-comment", "reply")
    body = "Thanks, fixed.\n```\nnot a fence\n```"
    gateway.pr_comment.reply(
        PR, to="gh:rc:abc", body=body, dedupe_key="respond:p-1:0123456789abcdef0123",
        actor="auto:review-respond", decision_ids=["d-1"],
    )  # fmt: skip
    (call,) = fakes.calls("pr-comment")
    assert call.argv == [
        "reply", PR, "--to", "gh:rc:abc", "--body-file", "-",
        "--dedupe-key", "respond:p-1:0123456789abcdef0123", "--yes",
        "--actor", "auto:review-respond", "--decision-id", "d-1", "--json",
    ]  # fmt: skip
    assert call.stdin == body


def test_pr_comment_resolve_and_the_empty_refusal(gateway: Gateway, fakes: Fakes) -> None:
    accept(fakes, "pr-comment", "resolve")
    gateway.pr_comment.resolve(PR, threads=["gh:rt:a", "gh:rt:b"], actor="human:me")
    assert only_call(fakes, "pr-comment") == [
        "resolve", PR, "--thread", "gh:rt:a", "--thread", "gh:rt:b", "--yes",
        "--actor", "human:me", "--json",
    ]  # fmt: skip
    with pytest.raises(GatewayError):
        gateway.pr_comment.resolve(PR, threads=[], actor="human:me")


def test_wtctl_wrappers(gateway: Gateway, fakes: Fakes) -> None:
    for command in ("create", "extract", "locate", "remove", "list", "show", "label add"):
        accept(fakes, "wtctl", command)
    gateway.wtctl.create_throwaway(
        repo=REPO, ref=SHA, source="remote",
        labels=[("app", "review-respond"), ("role", "clean"), ("round", "r-1")],
    )  # fmt: skip
    gateway.wtctl.extract("wt-aaaaaaaa", dest="/clean", files_from="/s/files", delete_from="/s/del")
    gateway.wtctl.locate(REPO)
    gateway.wtctl.remove("wt-aaaaaaaa")
    gateway.wtctl.list(labels=[("app", "review-respond")])
    gateway.wtctl.show("wt-aaaaaaaa")
    gateway.wtctl.label_add("wt-aaaaaaaa", [("pending", "p-1")])
    assert [c.argv for c in fakes.calls("wtctl")] == [
        ["create", "--throwaway", "--repo", REPO, "--ref", SHA, "--source", "remote",
         "--label", "app=review-respond", "--label", "role=clean", "--label", "round=r-1",
         "--json"],
        ["extract", "wt-aaaaaaaa", "--files-from", "/s/files", "--delete-from", "/s/del",
         "--dest", "/clean", "--json"],
        ["locate", REPO, "--json"],
        ["remove", "wt-aaaaaaaa", "--yes", "--json"],
        ["list", "--label", "app=review-respond", "--json"],
        ["show", "wt-aaaaaaaa", "--json"],
        ["label", "add", "wt-aaaaaaaa", "pending=p-1", "--json"],
    ]  # fmt: skip
    with pytest.raises(GatewayError):
        gateway.wtctl.extract("wt-aaaaaaaa", dest="/clean")
    with pytest.raises(GatewayError):
        gateway.wtctl.label_add("wt-aaaaaaaa", [])


def test_jobsctl_wrappers(gateway: Gateway, fakes: Fakes) -> None:
    for command in ("add", "show", "cancel", "resolve"):
        accept(fakes, "jobsctl", command)
    gateway.jobsctl.add(
        ["/bin/review-respond", "_job", "r-1"], queue="respond", unique_key="respond:abc:7",
        cwd="/state", kill_grace="125s", labels=[("round", "r-1")],
        env={"XDG_CONFIG_HOME": "/c"}, pass_env=["TOKEN_NAME"],
    )  # fmt: skip
    gateway.jobsctl.show("jb-aaaaaaaa")
    gateway.jobsctl.cancel("jb-aaaaaaaa")
    gateway.jobsctl.resolve("jb-aaaaaaaa", actor="auto:review-respond")
    assert [c.argv for c in fakes.calls("jobsctl")] == [
        ["add", "--queue", "respond", "--unique-key", "respond:abc:7", "--cwd", "/state",
         "--max-retries", "0", "--timeout", "none", "--kill-grace", "125s",
         "--env", "XDG_CONFIG_HOME=/c", "--pass-env", "TOKEN_NAME", "--label", "round=r-1",
         "--json", "--", "/bin/review-respond", "_job", "r-1"],
        ["show", "jb-aaaaaaaa", "--json"],
        ["cancel", "jb-aaaaaaaa", "--json"],
        ["resolve", "jb-aaaaaaaa", "--as", "done", "--actor", "auto:review-respond", "--json"],
    ]  # fmt: skip
    with pytest.raises(GatewayError):
        gateway.jobsctl.add([], queue="respond", unique_key="k", cwd="/state")


def test_claude_session_kinds(gateway: Gateway, fakes: Fakes) -> None:
    accept(fakes, "claude-session", "kinds")
    gateway.claude_session.kinds()
    assert only_call(fakes, "claude-session") == ["kinds", "--kind", "respond", "--json"]


@pytest.mark.parametrize(
    "call",
    [
        lambda g: g.forgectl.show("-x"),
        lambda g: g.forgectl.access("r", "--yes"),
        lambda g: g.agent_spawn.stop("--force", actor="a"),
        lambda g: g.wtctl.remove("--force"),
        lambda g: g.wtctl.label_add("wt-aaaaaaaa", [("-k", "v")]) and None,
        lambda g: g.jobsctl.cancel("--all"),
        lambda g: g.pr_comment.reply("p", to="--yes", body="b", dedupe_key="k", actor="a"),
        lambda g: g.forgectl.show("a\x00b"),
        lambda g: g.git.run(["--exec-path=/tmp"]),
        lambda g: g.git.run([]),
    ],
)
def test_option_lookalikes_and_nul_never_reach_a_process(
    gateway: Gateway, fakes: Fakes, call: object
) -> None:
    with pytest.raises(GatewayError):
        call(gateway)  # type: ignore[operator]
    assert fakes.calls() == []


@pytest.mark.parametrize("name", sorted(adversarial.HOSTILE_COMMENT_TEXTS))
def test_hostile_reply_text_travels_only_on_stdin(
    gateway: Gateway, fakes: Fakes, world: World, name: str
) -> None:
    body = adversarial.HOSTILE_COMMENT_TEXTS[name]
    accept(fakes, "pr-comment", "reply")
    gateway.pr_comment.reply(
        PR, to="gh:ic:x", body=body, dedupe_key="respond:p-1:k", actor="auto:review-respond"
    )
    (call,) = fakes.calls("pr-comment")
    assert call.stdin == body
    assert_untrusted_only_in_files_or_stdin([call], [body] if len(body) > 3 else [])


def test_the_child_environment_is_exactly_the_given_one(
    world: World, fakes: Fakes, procs: object
) -> None:
    gw = Gateway({"PATH": world.env["PATH"], "ONLY": "1"})
    accept(fakes, "forgectl", "show")
    gw.forgectl.show(PR)
    (call,) = fakes.calls("forgectl")
    assert call.env["ONLY"] == "1"
    assert "HOME" not in call.env and "XDG_CONFIG_HOME" not in call.env
    assert not any(k.startswith(("GIT_", "REVIEW_RESPOND_")) for k in call.env)


def test_stdin_is_closed_when_none_is_given(gateway: Gateway, fakes: Fakes) -> None:
    accept(fakes, "forgectl", "show")
    gateway.forgectl.show(PR)
    (call,) = fakes.calls("forgectl")
    assert call.stdin is None and call.stdin_tty is False


def test_output_is_decoded_as_utf8_with_surrogateescape(gateway: Gateway, fakes: Fakes) -> None:
    fakes.script("forgectl", "show", stdout="café bad\udcff end\n", schema=False)
    result = gateway.forgectl.show(PR)
    assert result.stdout == "café bad\udcff end\n"
    assert result.json is None and result.json_error is not None


def test_json_output_is_parsed_and_bad_json_is_reported(gateway: Gateway, fakes: Fakes) -> None:
    accept(fakes, "forgectl", "show")
    fakes.script("forgectl", "comments", stdout="not json", schema=False)
    assert gateway.forgectl.show(PR).json == {}
    bad = gateway.forgectl.comments(PR)
    assert bad.json is None and bad.json_error and bad.ok


def test_exit_codes_and_error_bodies_are_returned_untouched(gateway: Gateway, fakes: Fakes) -> None:
    fakes.script(
        "wtctl", "show", exit=10,
        json={"error": {"code": "not_found", "message": "no such record"}},
    )  # fmt: skip
    result = gateway.wtctl.show("wt-aaaaaaaa")
    assert (result.returncode, result.ok) == (10, False)
    assert result.json["error"]["code"] == "not_found"


def test_a_timeout_stops_exactly_the_group_the_gateway_started(
    gateway: Gateway, fakes: Fakes, world: World
) -> None:
    fakes.script("agent-spawn", "wait", json={}, schema=False, sleep=30)
    result = gateway.run(["agent-spawn", "wait", AG, "--json"], timeout=1.0)
    assert result.timed_out and not result.ok and result.returncode < 0
    assert world.procs.launched and world.procs.alive() == []


def test_every_spawn_is_reported_to_the_observer(
    gateway: Gateway, fakes: Fakes, world: World
) -> None:
    accept(fakes, "forgectl", "show")
    gateway.forgectl.show(PR)
    assert len(world.procs.launched) == 1
    assert world.procs.launched[0].pgid == world.procs.launched[0].pid


def test_the_worlds_clock_is_injectable(gateway: Gateway) -> None:
    assert isinstance(gateway.clock, FakeClock)
    before = gateway.clock.now()
    gateway.clock.sleep(90)
    assert (gateway.clock.now() - before).total_seconds() == 90


def test_the_system_clock_is_the_default() -> None:
    from review_respond.gateway import SystemClock

    gw = Gateway({"PATH": "/usr/bin"})
    assert isinstance(gw.clock, SystemClock)
    assert gw.clock.now().tzinfo is not None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("forgectl 0.55.2\n", (0, 55, 2)),
        ("tool 1.2.3-rc1", (1, 2, 3)),
        ("git version 2.43.0", (2, 43, 0)),
        ("none", None),
        ("", None),
    ],
)
def test_parse_version(text: str, expected: tuple[int, int, int] | None) -> None:
    assert parse_version(text) == expected


def test_git_ignores_global_and_system_configuration(gateway: Gateway, world: World) -> None:
    (world.home / ".gitconfig").write_text("[user]\n\tname = Leaked Name\n[alias]\n\tst = status\n")
    repo = world.root / "repo"
    repo.mkdir()
    assert gateway.git.run(["init", "-q", "."], cwd=repo).ok
    result = gateway.git.run(["config", "--get", "user.name"], cwd=repo)
    assert result.returncode == 1 and result.stdout == ""
    assert gateway.git.run(["st"], cwd=repo).returncode != 0


def test_git_drops_inherited_git_variables(world: World) -> None:
    gw = Gateway({**world.env, "GIT_DIR": "/nonexistent", "GIT_SSH_COMMAND": "evil"})
    env = gw.git.environment()
    assert "GIT_DIR" not in env and "GIT_SSH_COMMAND" not in env
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull and env["GIT_CONFIG_NOSYSTEM"] == "1"
    repo = world.root / "r2"
    repo.mkdir()
    assert gw.git.run(["init", "-q", "."], cwd=repo).ok
    assert gw.git.version().ok
