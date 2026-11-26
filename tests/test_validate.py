"""validate: config, state, dependency and credential checks; tool_missing and tool_too_old."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from review_respond import deps
from review_respond.cli import main
from review_respond.errors import CliError, error_envelope
from review_respond.gateway import Gateway

from .harness import schemas
from .harness.plugin import World

SECRET = "s3cr3t-token-VALUE-0123"
SIBLINGS = ("forgectl", "agent-spawn", "pr-comment", "wtctl", "jobsctl", "claude-session")


def stub(world: World, name: str, body: str) -> Path:
    """Replace (or add) an executable in the world's fake bin directory."""
    path = world.fakes.bin_dir / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


def stub_version(world: World, name: str, text: str, code: int = 0) -> None:
    stub(world, name, f"echo '{text}'\nexit {code}")


@pytest.fixture(autouse=True)
def fixed_git(world: World) -> None:
    """A git whose version does not depend on the machine (the golden files pin it)."""
    stub_version(world, "git", "git version 2.43.0")


def config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "cfg" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_text(text)
    path.chmod(0o600)
    return path


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any], str, str]:
    code = main(["validate", "--json", *argv])
    out, err = capsys.readouterr()
    return code, json.loads(out), out, err


def check_of(doc: dict[str, Any], name: str) -> dict[str, Any]:
    (found,) = [c for c in doc["checks"] if c["name"] == name]
    return found


def dep_of(doc: dict[str, Any], tool: str) -> dict[str, Any]:
    (found,) = [d for d in doc["dependencies"] if d["tool"] == tool]
    return found


def state_path(world: World) -> Path:
    return Path(world.env["XDG_STATE_HOME"]) / "review-respond"


# ---- the clean run ------------------------------------------------------------------------


def test_all_checks_pass(world: World, capsys: pytest.CaptureFixture[str], golden: Any) -> None:
    code, doc, _, err = run(capsys)
    assert code == 0 and doc["ok"] is True and err == ""
    assert [c["name"] for c in doc["checks"]] == ["config", "state", "dependencies", "credentials"]
    assert all(c["ok"] and c["code"] is None for c in doc["checks"])
    assert doc["warnings"] == []
    golden.check("cli/validate-ok", doc, "review-respond/cli/validate.json")


def test_only_version_is_asked_of_each_sibling(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys)
    calls = [c for tool in SIBLINGS for c in world.fakes.calls(tool)]
    assert sorted(c.tool for c in calls) == sorted(SIBLINGS)
    assert all(c.outcome == "version" for c in calls)


def test_nothing_is_created(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys)
    assert not state_path(world).exists()


def test_human_output_and_result_file(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "r.json"
    assert main(["validate", "--result-file", str(target)]) == 0
    text = capsys.readouterr().out
    assert "config: ok" in text and "credentials: ok" in text
    assert json.loads(target.read_text())["ok"] is True
    assert main(["validate", "--json", "--result-file", str(target)]) == 0
    assert target.read_text() == capsys.readouterr().out


# ---- dependencies -------------------------------------------------------------------------


def test_missing_dependency_fails_with_install_command(
    world: World, capsys: pytest.CaptureFixture[str], golden: Any
) -> None:
    (world.fakes.bin_dir / "wtctl").unlink()
    code, doc, _, _ = run(capsys)
    assert code == 12 and doc["ok"] is False
    assert dep_of(doc, "wtctl") == {
        "tool": "wtctl", "required": True, "found": None, "minimum": "0.31.0", "ok": False,
    }  # fmt: skip
    assert check_of(doc, "dependencies") == {
        "name": "dependencies",
        "ok": False,
        "code": "tool_missing",
    }
    golden.check("cli/validate-tool-missing", doc, "review-respond/cli/validate.json")
    assert main(["validate"]) == 12
    out = capsys.readouterr().out
    assert "uv tool install git+https://github.com/agent-workflow-kit/wtctl" in out


def test_too_old_dependency_fails(
    world: World, capsys: pytest.CaptureFixture[str], golden: Any
) -> None:
    stub_version(world, "pr-comment", "pr-comment 0.32.9")
    code, doc, _, _ = run(capsys)
    assert code == 12
    assert dep_of(doc, "pr-comment")["found"] == "0.32.9"
    assert dep_of(doc, "pr-comment")["ok"] is False
    assert check_of(doc, "dependencies")["code"] == "tool_too_old"
    golden.check("cli/validate-tool-too-old", doc, "review-respond/cli/validate.json")


def test_missing_outranks_too_old_in_the_check_code(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    stub_version(world, "pr-comment", "pr-comment 0.1.0")
    (world.fakes.bin_dir / "forgectl").unlink()
    _, doc, _, _ = run(capsys)
    assert check_of(doc, "dependencies")["code"] == "tool_missing"


def test_git_floor_is_two_dot_thirty_four(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    stub_version(world, "git", "git version 2.34.0")
    code, doc, _, _ = run(capsys)
    assert code == 0 and dep_of(doc, "git")["minimum"] == "2.34"
    stub_version(world, "git", "git version 2.33.9")
    code, doc, _, _ = run(capsys)
    assert code == 12 and check_of(doc, "dependencies")["code"] == "tool_too_old"


def test_version_comparison_is_numeric_not_textual(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    stub_version(world, "wtctl", "wtctl 0.100.0")
    code, doc, _, _ = run(capsys)
    assert code == 0 and dep_of(doc, "wtctl")["ok"] is True


def test_optional_jobsctl_may_be_missing(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    (world.fakes.bin_dir / "jobsctl").unlink()
    code, doc, _, _ = run(capsys)
    assert code == 0
    assert dep_of(doc, "jobsctl") == {
        "tool": "jobsctl", "required": False, "found": None, "minimum": "0.32.1", "ok": True,
    }  # fmt: skip


def test_optional_jobsctl_too_old_fails(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    stub_version(world, "jobsctl", "jobsctl 0.1.0")
    code, doc, _, _ = run(capsys)
    assert code == 12 and check_of(doc, "dependencies")["code"] == "tool_too_old"


def test_claude_session_is_needed_only_with_a_sandbox(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (world.fakes.bin_dir / "claude-session").unlink()
    code, doc, _, _ = run(capsys)
    assert code == 12 and dep_of(doc, "claude-session")["required"] is True
    path = config(tmp_path, "sandbox: preferred\n")
    code, doc, _, _ = run(capsys, "--config", str(path))
    assert code == 0 and dep_of(doc, "claude-session")["required"] is False
    assert doc["warnings"] == ["sandbox_weakened"]


@pytest.mark.parametrize("text", ["0.0.0", "no number here", ""])
def test_unreadable_version_is_a_warning_not_a_failure(
    text: str, world: World, capsys: pytest.CaptureFixture[str], golden: Any
) -> None:
    stub_version(world, "agent-spawn", text)
    code, doc, _, _ = run(capsys)
    assert code == 0 and doc["ok"] is True
    assert doc["warnings"] == ["agent_spawn_version_unreadable"]
    assert dep_of(doc, "agent-spawn")["ok"] is True
    assert dep_of(doc, "agent-spawn")["found"] == ("0.0.0" if text == "0.0.0" else None)
    if text == "0.0.0":
        golden.check("cli/validate-version-unreadable", doc, "review-respond/cli/validate.json")


def test_a_tool_that_fails_to_run_counts_as_missing(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    stub_version(world, "forgectl", "forgectl 9.9.9", code=3)
    code, doc, _, _ = run(capsys)
    assert code == 12 and check_of(doc, "dependencies")["code"] == "tool_missing"
    assert dep_of(doc, "forgectl")["found"] is None


def test_hostile_version_output_is_bounded(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    digits = "9" * 5000
    stub(world, "wtctl", f"printf 'wtctl {digits}.1.1 \\033[31m\\n'")
    code, doc, out, _ = run(capsys)
    assert code == 0 and dep_of(doc, "wtctl")["found"] is None
    assert doc["warnings"] == ["wtctl_version_unreadable"]
    assert "\x1b" not in out


def test_a_version_after_a_long_banner_is_found(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    stub(world, "wtctl", "head -c 3000 /dev/zero | tr '\\0' 'x'; echo; echo 'wtctl 0.31.0'")
    code, doc, _, _ = run(capsys)
    assert code == 0 and dep_of(doc, "wtctl")["found"] == "0.31.0"
    assert doc["warnings"] == []


def test_flooding_version_output_is_cut_off_and_unreadable(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    stub(world, "wtctl", "head -c 3000000 /dev/zero | tr '\\0' 'x'; echo 'wtctl 0.31.0'")
    code, doc, _, _ = run(capsys)
    assert code == 0
    assert dep_of(doc, "wtctl")["found"] is None
    assert doc["warnings"] == ["wtctl_version_unreadable"]


def test_deeply_nested_json_version_output_is_unreadable_not_an_internal_error(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    stub(world, "wtctl", "head -c 40000 /dev/zero | tr '\\0' '['; echo")
    code, doc, _, _ = run(capsys)
    assert code == 0 and doc["warnings"] == ["wtctl_version_unreadable"]


def test_version_is_read_from_stderr_when_stdout_has_none(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    stub(world, "wtctl", "echo 'no version here'; echo 'wtctl 0.31.0' >&2")
    code, doc, _, _ = run(capsys)
    assert code == 0 and dep_of(doc, "wtctl")["found"] == "0.31.0"
    assert doc["warnings"] == []


def test_optional_jobsctl_on_the_path_that_fails_is_a_warning(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    stub_version(world, "jobsctl", "jobsctl 9.9.9", code=3)
    code, doc, _, _ = run(capsys)
    assert code == 0 and dep_of(doc, "jobsctl")["ok"] is True
    assert doc["warnings"] == ["jobsctl_version_unreadable"]
    schemas.validate("review-respond/cli/validate.json", doc)


def test_missing_after_too_old_still_gives_tool_missing(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    stub_version(world, "forgectl", "forgectl 0.1.0")  # earlier in the order, too old
    (world.fakes.bin_dir / "wtctl").unlink()  # later, missing
    _, doc, _, _ = run(capsys)
    assert check_of(doc, "dependencies")["code"] == "tool_missing"


def test_claude_session_too_old_fails_even_when_the_sandbox_is_not_required(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stub_version(world, "claude-session", "claude-session 0.1.0")
    path = config(tmp_path, "sandbox: preferred\n")
    code, doc, _, _ = run(capsys, "--config", str(path))
    assert code == 12 and check_of(doc, "dependencies")["code"] == "tool_too_old"


CS = "git+https://github.com/agent-workflow-kit/claude-session"


def test_claude_session_install_hint_copies_links(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    (world.fakes.bin_dir / "claude-session").unlink()
    assert main(["validate"]) == 12
    assert f"uv tool install --link-mode=copy {CS}" in capsys.readouterr().out


def test_too_old_hint_reinstalls(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    stub_version(world, "wtctl", "wtctl 0.1.0")
    stub_version(world, "claude-session", "claude-session 0.1.0")
    assert main(["validate"]) == 12
    out = capsys.readouterr().out
    assert "uv tool install --reinstall git+https://github.com/agent-workflow-kit/wtctl" in out
    assert f"uv tool install --reinstall --link-mode=copy {CS}" in out


def test_each_dependency_problem_is_printed_once(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    (world.fakes.bin_dir / "wtctl").unlink()
    stub_version(world, "pr-comment", "pr-comment 0.1.0")
    assert main(["validate"]) == 12
    out = capsys.readouterr().out
    assert out.count("agent-workflow-kit/wtctl") == 1
    assert out.count("agent-workflow-kit/pr-comment") == 1
    assert "dependencies: FAIL tool_missing" in out
    assert "  forgectl 9.9.9" not in out and "forgectl" in out  # healthy ones keep their line


def test_child_env_is_an_explicit_list(world: World) -> None:
    from review_respond.validate import child_env

    environ = {
        "PATH": "/usr/bin",
        "XDG_CONFIG_HOME": "/c",
        "XDG_X": "canary",
        "XDG_STATE_HOME": "/s",
        "HOME": "/h",
        "SECRET_X": "canary",
    }
    assert child_env(environ) == {
        "PATH": "/usr/bin", "XDG_CONFIG_HOME": "/c", "XDG_STATE_HOME": "/s", "HOME": "/h",
    }  # fmt: skip
    assert "HOME" not in child_env(environ, {"HOME"})


def test_a_token_env_never_reaches_a_child(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    dump = tmp_path / "env.txt"
    stub(world, "wtctl", f"env > {dump}; echo 'wtctl 0.31.0'")
    monkeypatch.setenv("XDG_RR_TOKEN", SECRET)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    path = hosts_config(tmp_path, "  github.com:\n    token_env: XDG_RR_TOKEN\n")
    code, _, _, _ = run(capsys, "--config", str(path))
    assert code == 0
    seen = dump.read_text()
    assert SECRET not in seen and "XDG_RR_TOKEN" not in seen
    assert "XDG_CACHE_HOME=" in seen


def planted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> Path:
    """An executable named ``name`` in a directory that is the working directory."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    marker = tmp_path / "planted-ran"
    script = cwd / name
    script.write_text(f"#!/bin/sh\ntouch {marker}\necho '{name} 99.0.0'\n")
    script.chmod(0o755)
    monkeypatch.chdir(cwd)
    return marker


def test_empty_and_relative_path_entries_are_ignored_for_dependencies(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    (world.fakes.bin_dir / "forgectl").unlink()
    marker = planted(tmp_path, monkeypatch, "forgectl")
    bin_dir = str(world.fakes.bin_dir)
    monkeypatch.setenv("PATH", f"::.:{bin_dir}:rel/dir:/nonexistent")
    code, doc, _, _ = run(capsys)
    assert code == 12 and check_of(doc, "dependencies")["code"] == "tool_missing"
    assert dep_of(doc, "forgectl")["found"] is None
    assert not marker.exists()


@pytest.mark.parametrize("command", ["rr-planted", "./rr-planted", "rr-planted/../rr-planted"])
def test_token_command_never_resolves_against_the_working_directory(
    command: str, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    marker = planted(tmp_path, monkeypatch, "rr-planted")
    monkeypatch.setenv("PATH", f"{world.fakes.bin_dir}::.")
    path = hosts_config(tmp_path, f"  github.com:\n    token_command: ['{command}']\n")
    code, doc, _, _ = run(capsys, "--config", str(path))
    assert code == 12
    assert check_of(doc, "credentials")["code"] == "token_command_unresolvable"
    assert not marker.exists()


# ---- require() for the other commands ------------------------------------------------------


def test_require_raises_tool_missing(world: World, gateway: Gateway) -> None:
    (world.fakes.bin_dir / "agent-spawn").unlink()
    with pytest.raises(CliError) as caught:
        deps.require(gateway, "forgectl", "agent-spawn")
    error = caught.value
    assert (error.code, error.exit_code) == ("tool_missing", 12)
    assert "uv tool install git+https://github.com/agent-workflow-kit/agent-spawn" in error.message
    schemas.validate("review-respond/cli/error.json", error_envelope(error))


def test_require_raises_tool_too_old(world: World, gateway: Gateway) -> None:
    stub_version(world, "wtctl", "wtctl 0.30.0")
    with pytest.raises(CliError) as caught:
        deps.require(gateway, "wtctl")
    error = caught.value
    assert (error.code, error.exit_code) == ("tool_too_old", 12)
    assert "0.30.0" in error.message and "0.31.0" in error.message
    assert "agent-workflow-kit/wtctl" in error.message
    schemas.validate("review-respond/cli/error.json", error_envelope(error))


def test_require_passes_when_all_are_fine(world: World, gateway: Gateway) -> None:
    deps.require(gateway, "forgectl", "agent-spawn", "pr-comment", "wtctl", "git")


def test_require_tolerates_an_unreadable_version(world: World, gateway: Gateway) -> None:
    stub_version(world, "wtctl", "wtctl 0.0.0")
    deps.require(gateway, "wtctl")


# ---- config -------------------------------------------------------------------------------


def test_invalid_config_is_a_failed_check_not_an_envelope(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str], golden: Any
) -> None:
    path = config(tmp_path, "max_rounds: 0\n")
    code, doc, _, err = run(capsys, "--config", str(path))
    assert code == 12 and "error" not in doc and doc["ok"] is False
    assert check_of(doc, "config") == {"name": "config", "ok": False, "code": "config_invalid"}
    # the other checks still run; credentials cannot be judged without a config
    assert check_of(doc, "dependencies")["ok"] is True
    assert check_of(doc, "state")["ok"] is True
    assert check_of(doc, "credentials") == {
        "name": "credentials", "ok": True, "code": None, "skipped": True,
    }  # fmt: skip
    golden.check("cli/validate-config-invalid", doc, "review-respond/cli/validate.json")
    assert err == ""


def test_invalid_config_message_names_the_key_not_the_value(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = config(tmp_path, f"max_rounds: {SECRET}\n")
    assert main(["validate", "--config", str(path)]) == 12
    out = capsys.readouterr().out
    assert "max_rounds" in out and SECRET not in out


def test_missing_explicit_config_is_a_failed_check(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc, _, _ = run(capsys, "--config", str(tmp_path / "nope.yaml"))
    assert code == 12 and check_of(doc, "config")["code"] == "config_invalid"


def test_invalid_environment_variable_is_a_failed_check(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_RESPOND_MAX_ROUNDS", "many")
    code, doc, _, _ = run(capsys)
    assert code == 12 and check_of(doc, "config")["ok"] is False


def test_config_warnings_are_listed(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = config(tmp_path, "sandbox: off\n")
    (world.fakes.bin_dir / "claude-session").unlink()
    code, doc, _, err = run(capsys, "--config", str(path))
    assert code == 0 and doc["warnings"] == ["sandbox_weakened"]
    assert "sandbox_weakened" in err


def test_config_error_does_not_hide_a_bad_dependency(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (world.fakes.bin_dir / "wtctl").unlink()
    path = config(tmp_path, "max_rounds: 0\n")
    code, doc, _, _ = run(capsys, "--config", str(path))
    assert code == 12
    assert [c["ok"] for c in doc["checks"]] == [False, True, False, True]


# ---- credentials --------------------------------------------------------------------------


def hosts_config(tmp_path: Path, body: str) -> Path:
    return config(tmp_path, "hosts:\n" + body)


def test_token_env_set_is_ok_and_never_printed(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    monkeypatch.setenv("RR_TEST_TOKEN", SECRET)
    path = hosts_config(tmp_path, "  github.com:\n    token_env: RR_TEST_TOKEN\n")
    assert main(["validate", "--json", "--verbose", "--config", str(path)]) == 0
    out, err = capsys.readouterr()
    assert SECRET not in out and SECRET not in err
    assert main(["validate", "--config", str(path)]) == 0
    out, err = capsys.readouterr()
    assert SECRET not in out and SECRET not in err


@pytest.mark.parametrize("value", [None, ""])
def test_token_env_unset_or_empty_fails(
    value: str | None, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], golden: Any,
) -> None:  # fmt: skip
    monkeypatch.delenv("RR_TEST_TOKEN", raising=False)
    if value is not None:
        monkeypatch.setenv("RR_TEST_TOKEN", value)
    path = hosts_config(tmp_path, "  github.com:\n    token_env: RR_TEST_TOKEN\n")
    code, doc, _, _ = run(capsys, "--config", str(path))
    assert code == 12 and doc["ok"] is False
    assert check_of(doc, "credentials") == {
        "name": "credentials", "ok": False, "code": "token_env_unset",
    }  # fmt: skip
    if value is None:
        golden.check("cli/validate-credentials-failed", doc, "review-respond/cli/validate.json")
    assert main(["validate", "--config", str(path)]) == 12
    text = capsys.readouterr().out
    assert "github.com" in text and "RR_TEST_TOKEN" in text


def test_token_command_resolving_is_ok_and_is_not_run(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = tmp_path / "ran"
    helper = stub(world, "rr-token", f"echo {SECRET}; touch {marker}")
    path = hosts_config(
        tmp_path, f"  github.com:\n    token_command: ['{helper}', 'get', '--flag']\n"
    )
    code, doc, out, err = run(capsys, "--config", str(path))
    assert code == 0 and check_of(doc, "credentials")["ok"] is True
    assert not marker.exists() and SECRET not in out + err


def test_token_command_found_on_path_is_ok(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stub(world, "rr-token", "exit 0")
    path = hosts_config(tmp_path, "  github.com:\n    token_command: [rr-token]\n")
    code, doc, _, _ = run(capsys, "--config", str(path))
    assert code == 0 and check_of(doc, "credentials")["ok"] is True


@pytest.mark.parametrize("kind", ["missing_name", "missing_path", "not_executable", "directory"])
def test_token_command_not_resolving_fails(
    kind: str, world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = {
        "missing_name": "rr-no-such-helper",
        "missing_path": str(tmp_path / "absent"),
        "not_executable": str(tmp_path / "plain"),
        "directory": str(tmp_path),
    }[kind]
    (tmp_path / "plain").write_text("#!/bin/sh\n")
    (tmp_path / "plain").chmod(0o600)
    path = hosts_config(tmp_path, f"  github.com:\n    token_command: ['{target}']\n")
    code, doc, _, _ = run(capsys, "--config", str(path))
    assert code == 12
    assert check_of(doc, "credentials")["code"] == "token_command_unresolvable"


def test_every_host_is_checked_and_the_first_failure_names_the_code(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    monkeypatch.setenv("RR_OK", "x")
    monkeypatch.delenv("RR_BAD", raising=False)
    path = hosts_config(
        tmp_path,
        "  a.example:\n    token_env: RR_OK\n"
        "  b.example:\n    token_env: RR_BAD\n"
        "  c.example:\n    token_command: [rr-no-such-helper]\n",
    )
    assert main(["validate", "--json", "--config", str(path)]) == 12
    out = capsys.readouterr().out
    assert check_of(json.loads(out), "credentials")["code"] == "token_env_unset"  # host order
    assert main(["validate", "--config", str(path)]) == 12
    text = capsys.readouterr().out
    assert "b.example" in text and "c.example" in text and "RR_BAD" in text
    assert "a.example" not in text and "RR_OK" not in text


def test_a_host_with_no_credential_configured_is_fine(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = hosts_config(tmp_path, "  github.com: {}\n")
    code, doc, _, _ = run(capsys, "--config", str(path))
    assert code == 0 and check_of(doc, "credentials")["ok"] is True


def test_validate_sends_nothing_to_a_forge(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    monkeypatch.setenv("RR_TEST_TOKEN", SECRET)
    path = hosts_config(tmp_path, "  github.com:\n    token_env: RR_TEST_TOKEN\n")
    run(capsys, "--config", str(path))
    for tool in SIBLINGS:
        assert all(c.outcome == "version" for c in world.fakes.calls(tool))


# ---- state --------------------------------------------------------------------------------


def test_missing_state_directory_is_fine_and_stays_missing(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc, _, _ = run(capsys)
    assert code == 0 and check_of(doc, "state")["ok"] is True
    assert not state_path(world).exists()


def test_private_state_directory_is_ok_and_untouched(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    path = state_path(world)
    path.mkdir(parents=True)
    path.chmod(0o700)
    code, doc, _, _ = run(capsys)
    assert code == 0 and check_of(doc, "state")["ok"] is True
    assert list(path.iterdir()) == []


def test_unsafe_state_directory_fails_and_is_not_repaired(
    world: World, capsys: pytest.CaptureFixture[str], golden: Any
) -> None:
    path = state_path(world)
    path.mkdir(parents=True)
    path.chmod(0o755)
    code, doc, _, _ = run(capsys)
    assert code == 12 and doc["ok"] is False
    assert check_of(doc, "state") == {"name": "state", "ok": False, "code": "state_unsafe"}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o755
    golden.check("cli/validate-state-unsafe", doc, "review-respond/cli/validate.json")


def test_symlinked_state_directory_fails(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    path = state_path(world)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)
    code, doc, _, _ = run(capsys)
    assert code == 12 and check_of(doc, "state")["code"] == "state_unsafe"
    assert list(target.iterdir()) == []


def test_state_file_instead_of_directory_fails(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    path = state_path(world)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    code, doc, _, _ = run(capsys)
    assert code == 12 and check_of(doc, "state")["code"] == "state_unsafe"


def test_unreadable_state_parent_is_a_failed_check_not_absent(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    path = state_path(world)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0)
    try:
        code, doc, _, err = run(capsys)
    finally:
        path.parent.chmod(0o700)
    assert code == 12 and check_of(doc, "state") == {
        "name": "state", "ok": False, "code": "state_unsafe",
    }  # fmt: skip
    assert err == ""


def test_an_unset_path_reports_every_tool_missing_without_crashing(world: World) -> None:
    gateway = Gateway({"HOME": str(world.fakes.bin_dir)})  # no PATH at all
    status = deps.probe(gateway, deps.BY_TOOL["wtctl"], required=True)
    assert not status.ok and status.found is None and status.code == "tool_missing"
    with pytest.raises(CliError) as caught:
        deps.require(gateway, "wtctl")
    assert caught.value.code == "tool_missing"
