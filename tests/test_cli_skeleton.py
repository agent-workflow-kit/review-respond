"""CLI skeleton: command set, global flags, actor rules, exit codes, envelope, result file."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from review_respond import actor as actor_mod
from review_respond import cli, output
from review_respond.cli import main
from review_respond.config import Config
from review_respond.errors import (
    CODE_EXIT,
    EXIT_CODES,
    REFUSAL_REASONS,
    CliError,
    error_envelope,
    exit_code_for,
    lower_error,
)
from review_respond.output import (
    Context,
    Outcome,
    dump_json,
    emit,
    sanitize_line,
    write_atomic,
)

from .harness import schemas

ROOT = Path(__file__).resolve().parent.parent
DEFS = json.loads((ROOT / "schemas" / "defs.json").read_text())["$defs"]
ERROR_SCHEMA = json.loads((ROOT / "schemas" / "cli" / "error.json").read_text())
INTERFACE = (ROOT / "docs" / "interface.md").read_text()

COMMANDS = [
    "check",
    "respond",
    "list",
    "show",
    "pending",
    "confirm",
    "approve",
    "reject",
    "edit",
    "drop-reply",
    "stop",
    "cron",
    "export",
    "prune",
    "validate",
]
# Minimal valid argv per command (positionals only).
MIN_ARGV: dict[str, list[str]] = {
    "respond": ["host/o/r#1"],
    "show": ["host/o/r#1"],
    "confirm": ["rp-aaaaaaaa"],
    "approve": ["rp-aaaaaaaa"],
    "reject": ["rp-aaaaaaaa"],
    "edit": ["rp-aaaaaaaa"],
    "drop-reply": ["rp-aaaaaaaa", "c1"],
    "stop": ["rd-aaaaaaaa"],
    "cron": ["install"],
    "_job": ["rd-aaaaaaaa"],
}


def argv_for(command: str, *extra: str) -> list[str]:
    return [command, *MIN_ARGV.get(command, []), *extra]


def validate_envelope(document: dict[str, Any]) -> None:
    """Check an error document against schemas/cli/error.json (and the defs it references)."""
    schemas.validate(ERROR_SCHEMA["$id"], document)


@pytest.fixture
def handler(monkeypatch: pytest.MonkeyPatch) -> Callable[[str, Callable[..., Outcome]], None]:
    def install(name: str, fn: Callable[..., Outcome]) -> None:
        monkeypatch.setitem(cli.HANDLERS, name, fn)

    return install


@pytest.fixture(autouse=True)
def non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(actor_mod, "stdin_is_tty", lambda: False)


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---- command set -------------------------------------------------------------------------


@pytest.mark.parametrize("command", [*COMMANDS, "_job"])
def test_every_command_answers_help(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main([command, "--help"])
    assert exc.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_job_is_hidden_from_top_level_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "_job" not in out
    for name in COMMANDS:
        if name != "approve":
            assert name in out


@pytest.mark.parametrize("command", [*COMMANDS, "_job"])
def test_unimplemented_command_exits_1_with_message(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, *argv_for(command))
    assert code == 1
    assert out == ""
    assert "not implemented yet" in err


def test_approve_is_confirm(
    handler: Callable[[str, Callable[..., Outcome]], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[str] = []

    def confirm(_ctx: Context, args: argparse.Namespace) -> Outcome:
        seen.append(args.command)
        return Outcome()

    handler("confirm", confirm)
    assert run(capsys, "approve", "rp-aaaaaaaa")[0] == 0
    assert seen == ["confirm"]


def test_version_flag_works_on_top_level_and_after_subcommand(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for argv in (["--version"], ["check", "--version"]):
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 0
        assert capsys.readouterr().out.startswith("review-respond ")


# ---- usage errors ------------------------------------------------------------------------


def test_no_command_is_usage_error_with_envelope(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys)
    assert (code, out) == (2, "")
    code, out, _ = run(capsys, "--json")
    assert code == 2
    validate_envelope(json.loads(out))
    assert json.loads(out)["error"]["code"] == "usage"


@pytest.mark.parametrize(
    "argv",
    [
        ["check", "--bogus"],
        ["bogus"],
        ["respond"],  # missing PR
        ["respond", "p#1", "--mode", "weird"],
        ["respond", "p#1", "--note", "text from argv"],  # no argv free text
        ["respond", "p#1", "--note-fi", "x"],  # no abbreviation onto --note-file
        ["edit", "rp-aaaaaaaa", "--body", "text"],
        ["confirm", "rp-aaaaaaaa", "--expect", "ABC"],
        ["check", "--verbose", "--quiet"],
        ["list", "--dry-run"],  # --dry-run only where it is meaningful
        ["check", "--override-escalation"],
        ["--config", "x.yaml", "check"],  # global flags go after the subcommand
        ["check", "--config", ""],
        ["check", "--result-file", ""],
        ["check", "--actor", "robot"],
        ["check", "--actor", "human:"],
        ["check", "--actor", "auto:a b"],
        ["check", "--decision-id", "has space"],
        ["prune", "--older-than", "5"],
        ["export", "--since", "yesterday"],
    ],
)
def test_usage_errors_exit_2_with_envelope(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, *argv, "--json")
    assert code == 2, err
    validate_envelope(json.loads(out))
    assert json.loads(out)["error"]["code"] == "usage"
    assert err.startswith("review-respond: error: usage:")


def test_usage_error_without_json_prints_only_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(capsys, "check", "--bogus")
    assert (code, out) == (2, "")
    assert "usage" in err


def test_job_accepts_config_before_the_subcommand(
    handler: Callable[[str, Callable[..., Outcome]], None],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    captured: list[Context] = []

    def job(ctx: Context, _args: argparse.Namespace) -> Outcome:
        captured.append(ctx)
        return Outcome()

    cfg = tmp_path / "c.yaml"
    cfg.write_text("")
    cfg.chmod(0o600)
    handler("_job", job)
    assert run(capsys, "--config", str(cfg), "_job", "rd-aaaaaaaa")[0] == 0
    assert captured[0].config == cfg


def test_stderr_message_is_sanitized(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "check", "--pr")
    assert code == 2
    _, _, err = run(capsys, "check", "\x1b[31mred\x1b]0;x\x07")
    assert "\x1b" not in err and "\x07" not in err


# ---- global flags reach the handler --------------------------------------------------------


def test_global_flags_reach_handler(
    handler: Callable[[str, Callable[..., Outcome]], None],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    captured: list[Context] = []

    def prune(ctx: Context, _args: argparse.Namespace) -> Outcome:
        captured.append(ctx)
        return Outcome()

    handler("prune", prune)
    result = tmp_path / "r.json"
    (tmp_path / "c.yaml").write_text("")
    (tmp_path / "c.yaml").chmod(0o600)
    code, _, _ = run(
        capsys,
        "prune",
        "--json",
        "--config",
        str(tmp_path / "c.yaml"),
        "--dry-run",
        "--yes",
        "--actor",
        "human:alice",
        "--decision-id",
        "d1",
        "--decision-id",
        "d2",
        "--result-file",
        str(result),
        "--verbose",
        "--older-than",
        "2d",
    )
    assert code == 0
    ctx = captured[0]
    assert ctx.store is None  # --dry-run on a fresh state never creates it (#9)
    assert ctx == Context(
        json=True,
        config=tmp_path / "c.yaml",
        settings=Config(),
        store=ctx.store,
        dry_run=True,
        yes=True,
        actor="human:alice",
        override_escalation=False,
        decision_ids=("d1", "d2"),
        result_file=result,
        verbose=True,
        quiet=False,
    )


def test_override_escalation_reaches_handler_for_respond_and_confirm(
    handler: Callable[[str, Callable[..., Outcome]], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    flags: list[bool] = []

    def record(ctx: Context, _args: argparse.Namespace) -> Outcome:
        flags.append(ctx.override_escalation)
        return Outcome()

    handler("respond", record)
    handler("confirm", record)
    run(capsys, "respond", "p#1", "--override-escalation", "--actor", "human:a")
    run(capsys, "confirm", "rp-aaaaaaaa", "--override-escalation", "--actor", "human:a")
    assert flags == [True, True]


# ---- actor ---------------------------------------------------------------------------------


def test_default_actor_is_auto_unknown_without_tty() -> None:
    assert actor_mod.default_actor(tty=False) == "auto:unknown"


def test_default_actor_on_tty_is_human_with_user() -> None:
    assert actor_mod.default_actor(tty=True, user="alice") == "human:alice"
    assert re.fullmatch(r"human:[A-Za-z0-9._@-]+", actor_mod.default_actor(tty=True))


def test_os_user_name_replaces_unsafe_characters(monkeypatch: pytest.MonkeyPatch) -> None:
    class Entry:
        pw_name = "we ird$name\n"

    monkeypatch.setattr(actor_mod.pwd, "getpwuid", lambda _uid: Entry())
    assert actor_mod.os_user_name() == "we_ird_name_"


def test_tty_gives_human_actor_to_handlers(
    handler: Callable[[str, Callable[..., Outcome]], None],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(actor_mod, "stdin_is_tty", lambda: True)
    monkeypatch.setattr(actor_mod, "os_user_name", lambda: "alice")
    actors: list[str] = []

    def check(ctx: Context, _args: argparse.Namespace) -> Outcome:
        actors.append(ctx.actor)
        return Outcome()

    handler("check", check)
    assert run(capsys, "check", "--yes")[0] == 0
    assert actors == ["human:alice"]


def test_explicit_auto_actor_on_tty_is_still_automatic(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(actor_mod, "stdin_is_tty", lambda: True)
    code, _, _ = run(capsys, "check", "--yes", "--actor", "auto:cron")
    assert code == 2


# ---- human_only: each of the four flags --------------------------------------------------

HUMAN_ONLY_CASES = [
    ("yes", ["check", "--yes"]),
    ("force", ["respond", "host/o/r#1", "--force"]),
    ("allow-sensitive", ["confirm", "rp-aaaaaaaa", "--allow-sensitive"]),
    ("allow-sensitive", ["approve", "rp-aaaaaaaa", "--allow-sensitive"]),
    ("override-escalation", ["respond", "host/o/r#1", "--override-escalation"]),
    ("override-escalation", ["confirm", "rp-aaaaaaaa", "--override-escalation"]),
]


@pytest.mark.parametrize(("flag", "argv"), HUMAN_ONLY_CASES)
@pytest.mark.parametrize(
    "actor_args", [[], ["--actor", "auto:unknown"], ["--actor", "auto:policy"]]
)
def test_human_only_with_automatic_actor(
    flag: str,
    argv: list[str],
    actor_args: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = run(capsys, *argv, *actor_args, "--json")
    assert code == 2
    document = json.loads(out)
    validate_envelope(document)
    assert document["error"]["code"] == "human_only"
    assert document["error"]["details"] == {"flags": [flag]}
    assert f"--{flag}" in err


@pytest.mark.parametrize(("flag", "argv"), HUMAN_ONLY_CASES)
def test_human_flags_pass_with_human_actor(
    flag: str, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = run(capsys, *argv, "--actor", "human:alice")
    assert code == 1  # reaches dispatch: the command itself is not implemented yet
    assert "not implemented yet" in err


def test_human_only_lists_every_offending_flag(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(
        capsys, "respond", "p#1", "--force", "--yes", "--override-escalation", "--json"
    )
    assert code == 2
    assert json.loads(out)["error"]["details"]["flags"] == [
        "yes",
        "force",
        "override-escalation",
    ]


def test_human_only_check_runs_before_handler(
    handler: Callable[[str, Callable[..., Outcome]], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    def boom(_ctx: Context, _args: argparse.Namespace) -> Outcome:
        raise AssertionError("handler must not run")

    handler("check", boom)
    assert run(capsys, "check", "--yes")[0] == 2


# ---- exit codes and envelope -------------------------------------------------------------


def raising(error: CliError) -> Callable[..., Outcome]:
    def fn(_ctx: Context, _args: argparse.Namespace) -> Outcome:
        raise error

    return fn


def make_error(code: str) -> CliError:
    if code == "refused":
        return CliError(code, "refusal text", reason="live", details={"agent": "a1"})
    if code == "lower_failed":
        return CliError(code, "sibling failed", lower={"code": "boom", "message": "m"})
    return CliError(code, f"{code} message")


@pytest.mark.parametrize("code", sorted(CODE_EXIT))
def test_every_error_code_produces_its_exit_code_and_envelope(
    code: str,
    handler: Callable[[str, Callable[..., Outcome]], None],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    handler("list", raising(make_error(code)))
    result = tmp_path / "out.json"
    exit_code, out, err = run(capsys, "list", "--json", "--result-file", str(result))
    assert exit_code == CODE_EXIT[code]
    document = json.loads(out)
    validate_envelope(document)
    assert document["error"]["code"] == code
    assert f"{code} message" in err or code in ("refused", "lower_failed")
    assert result.read_text() == out


def test_every_exit_code_is_covered() -> None:
    assert EXIT_CODES == {0, 1, 2, 10, 11, 12, 13, 20, 21, 22}


def test_error_without_json_prints_nothing_on_stdout(
    handler: Callable[[str, Callable[..., Outcome]], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    handler("list", raising(make_error("not_found")))
    code, out, err = run(capsys, "list")
    assert (code, out) == (10, "")
    assert "not_found" in err


def test_code_table_matches_interface_doc() -> None:
    section = INTERFACE.split("### Error codes", 1)[1].split("\n## ", 1)[0]
    documented: dict[str, int] = {}
    for line in section.splitlines():
        match = re.match(r"\| (\d+) \| ([^|]+) \|", line)
        if match:
            for name in re.findall(r"`([a-z_]+)`", match.group(2)):
                if name != "lower_error":  # a member name in the row, not a code
                    documented[name] = int(match.group(1))
    assert documented == CODE_EXIT


def test_exit_table_matches_interface_doc() -> None:
    section = INTERFACE.split("## Exit codes", 1)[1].split("### Refusal", 1)[0]
    documented = {int(m) for m in re.findall(r"^\| (\d+) \|", section, re.M)}
    assert documented == EXIT_CODES


def test_codes_and_reasons_match_schema_and_doc() -> None:
    assert set(CODE_EXIT) == set(DEFS["error_code"]["enum"])
    assert REFUSAL_REASONS == set(DEFS["error_reason"]["enum"])
    section = INTERFACE.split("### Refusal reasons (exit 20)", 1)[1].split("### Error envelope")[0]
    first_paragraph = section.strip().split("\n\n", 1)[0]
    assert REFUSAL_REASONS == set(
        re.findall(r"`([a-z_]+)`", first_paragraph.split("A refusal carries")[0])
    ) - {
        "cause",
        "recomputed",
        "path_state",
        "expect_mismatch",
        "reason",
        "not_yours",
        "fork_pr",
        "default_branch",
        "causes",
    }


def test_unknown_code_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown error code"):
        exit_code_for("nope")
    with pytest.raises(ValueError):
        CliError("nope", "x")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"code": "refused"},  # refusal without a reason
        {"code": "refused", "reason": "made_up"},
        {"code": "not_found", "reason": "live"},  # reason only with refused
        {"code": "lower_failed"},  # needs lower_error
        {"code": "not_found", "lower": {"code": "x"}},
    ],
)
def test_malformed_error_is_a_programming_error(kwargs: dict[str, Any]) -> None:
    code = kwargs.pop("code")
    with pytest.raises(ValueError):
        CliError(code, "m", **kwargs)


def test_lower_error_is_cleaned() -> None:
    assert lower_error({"code": "Bad Code!", "message": "x" * 5000, "extra": 1}) == {
        "code": "unknown",
        "message": "x" * 4096,
    }
    assert lower_error("not a mapping") == {"code": "unknown"}
    assert lower_error({"code": "ok_code", "reason": "BAD", "details": [1]}) == {"code": "ok_code"}
    assert lower_error({"code": "a", "reason": "live", "details": {"k": 1}}) == {
        "code": "a",
        "reason": "live",
        "details": {"k": 1},
    }


def test_envelope_keeps_lower_error_and_details() -> None:
    error = CliError(
        "lower_failed", "m", details={"step": 2}, lower={"code": "x", "reason": "busy"}
    )
    body = error_envelope(error)["error"]
    assert body == {
        "code": "lower_failed",
        "message": "m",
        "details": {"step": 2},
        "lower_error": {"code": "x", "reason": "busy"},
    }


def test_long_message_is_cut() -> None:
    assert len(CliError("internal", "x" * 10000).message) == 4096


def test_unexpected_exception_is_internal_without_leaking_message(
    handler: Callable[[str, Callable[..., Outcome]], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    def boom(_ctx: Context, _args: argparse.Namespace) -> Outcome:
        raise RuntimeError("secret-token-123")

    handler("list", boom)
    code, out, err = run(capsys, "list", "--json")
    assert code == 1
    document = json.loads(out)
    validate_envelope(document)
    assert document["error"]["code"] == "internal"
    assert "secret-token-123" not in out + err


def test_verbose_prints_traceback_for_internal_error(
    handler: Callable[[str, Callable[..., Outcome]], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    def boom(_ctx: Context, _args: argparse.Namespace) -> Outcome:
        raise RuntimeError("detail")

    handler("list", boom)
    _, _, err = run(capsys, "list", "--verbose")
    assert "Traceback" in err


# ---- result file ---------------------------------------------------------------------------


def outcome_handler(payload: dict[str, Any], exit_code: int) -> Callable[..., Outcome]:
    def fn(_ctx: Context, _args: argparse.Namespace) -> Outcome:
        return Outcome(payload=payload, human="human text", exit_code=exit_code)

    return fn


@pytest.mark.parametrize(
    ("exit_code", "payload"),
    [
        (0, {"prs": []}),
        (12, {"already_running": False, "error": {"code": "all_hosts_failed", "message": "m"}}),
    ],
)
def test_result_file_equals_stdout(
    exit_code: int,
    payload: dict[str, Any],
    handler: Callable[[str, Callable[..., Outcome]], None],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    handler("check", outcome_handler(payload, exit_code))
    result = tmp_path / "result.json"
    code, out, _ = run(capsys, "check", "--json", "--result-file", str(result))
    assert code == exit_code
    assert json.loads(out) == payload
    assert result.read_text() == out
    if exit_code == 12:
        error = json.loads(result.read_text())["error"]
        assert {"code", "message"} <= set(error)
        assert error["code"] in CODE_EXIT


def test_result_file_equals_stdout_on_refusal_exit_20(
    handler: Callable[[str, Callable[..., Outcome]], None],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    handler("respond", raising(CliError("refused", "busy", reason="busy")))
    result = tmp_path / "result.json"
    code, out, _ = run(capsys, "respond", "p#1", "--json", "--result-file", str(result))
    assert code == 20
    assert result.read_text() == out
    assert json.loads(out)["error"]["reason"] == "busy"


def test_result_file_written_without_json_and_stdout_is_human(
    handler: Callable[[str, Callable[..., Outcome]], None],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    handler("check", outcome_handler({"a": 1}, 0))
    result = tmp_path / "r.json"
    code, out, _ = run(capsys, "check", "--result-file", str(result))
    assert code == 0
    assert out == "human text\n"
    assert json.loads(result.read_text()) == {"a": 1}


def test_result_file_written_when_parse_fails(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    result = tmp_path / "r.json"
    code, out, _ = run(capsys, "check", "--bogus", "--json", "--result-file", str(result))
    assert code == 2
    assert result.read_text() == out
    validate_envelope(json.loads(out))


def test_result_file_replaces_existing_atomically_and_leaves_no_temp_files(
    handler: Callable[[str, Callable[..., Outcome]], None],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    result = tmp_path / "r.json"
    result.write_text("old")
    handler("check", outcome_handler({"new": True}, 0))
    run(capsys, "check", "--json", "--result-file", str(result))
    assert json.loads(result.read_text()) == {"new": True}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["r.json"]
    assert (result.stat().st_mode & 0o777) == 0o600


def test_result_file_write_failure_turns_success_into_exit_1(
    handler: Callable[[str, Callable[..., Outcome]], None],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    handler("check", outcome_handler({}, 0))
    code, _, err = run(capsys, "check", "--result-file", str(tmp_path / "missing" / "r.json"))
    assert code == 1
    assert "--result-file" in err


def test_result_file_write_failure_keeps_a_failing_exit_code(
    handler: Callable[[str, Callable[..., Outcome]], None],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    handler("check", raising(CliError("not_found", "x")))
    code, _, _ = run(capsys, "check", "--result-file", str(tmp_path / "missing" / "r.json"))
    assert code == 10


def test_quiet_suppresses_human_output_but_not_json(
    handler: Callable[[str, Callable[..., Outcome]], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    handler("check", outcome_handler({"a": 1}, 0))
    assert run(capsys, "check", "--quiet")[1] == ""
    assert json.loads(run(capsys, "check", "--quiet", "--json")[1]) == {"a": 1}


# ---- CI-like conditions --------------------------------------------------------------------


def run_subprocess(tmp_path: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_STATE_HOME": str(home / "state"),
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
    }
    return subprocess.run(
        [sys.executable, "-m", "review_respond", *argv],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
        check=False,
    )


def test_subprocess_version_and_no_tty_actor(tmp_path: Path) -> None:
    proc = run_subprocess(tmp_path, "--version")
    assert proc.returncode == 0
    assert proc.stdout.startswith("review-respond ")
    # Stdin is /dev/null: the actor is automatic, so --yes is refused.
    proc = run_subprocess(tmp_path, "check", "--yes", "--json")
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["error"]["code"] == "human_only"


def test_subprocess_result_file_and_unimplemented(tmp_path: Path) -> None:
    result = tmp_path / "r.json"
    proc = run_subprocess(tmp_path, "validate", "--json", "--result-file", str(result))
    assert proc.returncode == 1
    assert result.read_text() == proc.stdout


# ---- every error code has a producer here or a tracked owner ---------------------------------

# config: test_config.py; state_unsafe, version_skew: test_store.py; lease_*: test_leases.py
PRODUCED_HERE = {
    "lease_lost",
    "lease_timeout",
    "internal",
    "usage",
    "human_only",
    "config_invalid",
    "state_unsafe",
    "version_skew",
}
# error.code -> the issue whose feature produces it (tracked, per #6's acceptance criteria).
OWNER_ISSUE = {
    "not_found": 17,
    "vetoed": 37,
    "tool_missing": 12,
    "tool_too_old": 12,
    "lower_failed": 13,
    "all_hosts_failed": 17,
    "push_failed": 34,
    "reply_failed": 35,
    "outcome_unknown": 34,
    "identity_missing": 33,
    "throwaway_missing": 32,
    "confirmation_required": 32,
    "escalated": 37,
    "refused": 25,
    "ambiguous": 17,
    "nothing_to_respond": 25,
}


def test_every_error_code_is_produced_here_or_owned_by_an_issue() -> None:
    assert PRODUCED_HERE | set(OWNER_ISSUE) == set(CODE_EXIT)
    assert not PRODUCED_HERE & set(OWNER_ISSUE)


# ---- review round 1: output failures, write_atomic, parsing edges -------------------------


def subprocess_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_STATE_HOME": str(home / "state"),
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
    }


@pytest.fixture
def silenced(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Replace the fd-1 redirect (it would hit the test runner's own stdout)."""
    calls: list[int] = []
    monkeypatch.setattr(output, "_silence_stdout", lambda: calls.append(1))
    return calls


class FailingOut:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def write(self, _text: str) -> int:
        raise self.error

    def flush(self) -> None:
        raise self.error


@pytest.mark.parametrize("exit_code", [0, 10])
def test_emit_broken_pipe_keeps_exit_code(
    exit_code: int, monkeypatch: pytest.MonkeyPatch, silenced: list[int]
) -> None:
    monkeypatch.setattr(sys, "stdout", FailingOut(BrokenPipeError()))
    assert emit({"a": 1}, "", exit_code, Context(json=True)) == exit_code
    assert silenced == [1]


def test_silence_stdout_redirects_fd_1_to_devnull(tmp_path: Path) -> None:
    code = (
        "import os\nfrom review_respond.output import _silence_stdout\n"
        "_silence_stdout()\nos.write(1, b'lost')\nprint('lost too')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        env=subprocess_env(tmp_path),
        cwd=tmp_path,
        timeout=60,
        check=False,
    )
    assert (proc.returncode, proc.stdout) == (0, b"")


@pytest.mark.parametrize(("exit_code", "expected"), [(0, 1), (10, 10)])
def test_emit_with_no_stdout_never_raises(
    exit_code: int,
    expected: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "stdout", None)
    assert emit({"a": 1}, "", exit_code, Context(json=True)) == expected
    assert "cannot write to stdout" in capsys.readouterr().err


@pytest.mark.parametrize(("exit_code", "expected"), [(0, 1), (10, 10)])
def test_emit_to_dev_full_never_raises(
    exit_code: int,
    expected: int,
    monkeypatch: pytest.MonkeyPatch,
    silenced: list[int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    with open("/dev/full", "wb", buffering=0) as raw:
        full = io.TextIOWrapper(raw, encoding="utf-8", write_through=True)
        monkeypatch.setattr(sys, "stdout", full)
        assert emit({"a": 1}, "", exit_code, Context(json=True)) == expected
        monkeypatch.setattr(sys, "stdout", sys.__stdout__)
        full.detach()
    assert silenced == [1]
    assert "cannot write to stdout" in capsys.readouterr().err


@pytest.mark.parametrize(("exit_code", "expected"), [(0, 1), (10, 10)])
def test_emit_with_ascii_stdout_never_raises(
    exit_code: int,
    expected: int,
    monkeypatch: pytest.MonkeyPatch,
    silenced: list[int],
) -> None:
    ascii_out = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
    monkeypatch.setattr(sys, "stdout", ascii_out)
    assert emit({"a": "\u00e9"}, "", exit_code, Context(json=True)) == expected


def test_closed_stdout_and_dev_full_in_a_real_process(tmp_path: Path) -> None:
    base = f"{shlex.quote(sys.executable)} -m review_respond check --yes --json"
    for redirect in (">&-", "> /dev/full"):
        proc = subprocess.run(
            ["/bin/sh", "-c", f"{base} {redirect}"],
            capture_output=True,
            text=True,
            env=subprocess_env(tmp_path),
            cwd=tmp_path,
            stdin=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
        assert proc.returncode == 2, (redirect, proc.stderr)
        assert "Traceback" not in proc.stderr


def test_invalid_utf8_argv_gives_envelope_and_result_file(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    result = tmp_path / "r.json"
    bad = os.fsdecode(b"\xff")
    code, out, err = run(capsys, "check", bad, "--json", "--result-file", str(result))
    assert code == 2
    document = json.loads(out)
    validate_envelope(document)
    out.encode("utf-8")
    err.encode("utf-8")
    assert result.read_bytes() == out.encode("utf-8")


def test_keyboard_interrupt_is_exit_1_internal_with_result_file(
    handler: Callable[[str, Callable[..., Outcome]], None],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    def interrupt(_ctx: Context, _args: argparse.Namespace) -> Outcome:
        raise KeyboardInterrupt

    handler("check", interrupt)
    result = tmp_path / "r.json"
    code, out, err = run(capsys, "check", "--json", "--result-file", str(result))
    assert code == 1
    document = json.loads(out)
    validate_envelope(document)
    assert document["error"] == {"code": "internal", "message": "interrupted"}
    assert result.read_text() == out
    assert "interrupted" in err


def test_report_never_raises_when_stderr_is_broken(
    handler: Callable[[str, Callable[..., Outcome]], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler("list", raising(make_error("not_found")))
    monkeypatch.setattr(sys, "stderr", None)
    assert main(["list"]) == 10


def test_verbose_traceback_is_sanitized(
    handler: Callable[[str, Callable[..., Outcome]], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    def boom(_ctx: Context, _args: argparse.Namespace) -> Outcome:
        raise RuntimeError("evil\x1b[31m\u202e")

    handler("list", boom)
    _, _, err = run(capsys, "list", "--verbose")
    assert "Traceback" in err
    assert "\x1b" not in err and "\u202e" not in err
    assert "<U+001B>" in err


# write_atomic


def test_write_atomic_writes_into_dev_null() -> None:
    write_atomic(Path("/dev/null"), "x")
    assert stat.S_ISCHR(os.stat("/dev/null").st_mode)


def test_write_atomic_writes_a_fifo_in_place(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        write_atomic(fifo, "hello")
        assert os.read(reader, 100) == b"hello"
    finally:
        os.close(reader)
    assert stat.S_ISFIFO(os.stat(fifo).st_mode)
    assert [p.name for p in tmp_path.iterdir()] == ["pipe"]


def test_write_atomic_keeps_a_symlink_and_writes_its_target(tmp_path: Path) -> None:
    target = tmp_path / "real.json"
    target.write_text("old")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    write_atomic(link, "new")
    assert link.is_symlink()
    assert target.read_text() == "new"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["link.json", "real.json"]


def test_write_atomic_follows_a_dangling_symlink(tmp_path: Path) -> None:
    target = tmp_path / "later.json"
    link = tmp_path / "link.json"
    link.symlink_to(target)
    write_atomic(link, "new")
    assert link.is_symlink()
    assert target.read_text() == "new"


def test_write_atomic_works_for_a_255_byte_name(tmp_path: Path) -> None:
    path = tmp_path / ("a" * 255)
    write_atomic(path, "x")
    assert [p.name for p in tmp_path.iterdir()] == ["a" * 255]
    assert (path.stat().st_mode & 0o777) == 0o600


def test_write_atomic_syncs_file_then_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real = os.fsync
    kinds: list[bool] = []

    def spy(fd: int) -> None:
        kinds.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        real(fd)

    monkeypatch.setattr(output.os, "fsync", spy)
    write_atomic(tmp_path / "r.json", "x")
    assert kinds == [False, True]


def test_write_atomic_temp_file_is_beside_the_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real = os.replace
    seen: list[tuple[Path, Path]] = []

    def spy(src: str, dst: Path) -> None:
        seen.append((Path(src).parent, Path(dst).parent))
        real(src, dst)

    monkeypatch.setattr(output.os, "replace", spy)
    write_atomic(tmp_path / "r.json", "x")
    assert seen == [(tmp_path, tmp_path)]


@pytest.mark.parametrize("failing", ["replace", "fsync"])
def test_write_atomic_failure_leaves_no_temp_file(
    failing: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail(*_args: Any) -> None:
        raise OSError("boom")

    monkeypatch.setattr(output.os, failing, fail)
    with pytest.raises(OSError):
        write_atomic(tmp_path / "r.json", "x")
    assert list(tmp_path.iterdir()) == []


# ---- flags and parsing edges -------------------------------------------------------------


def test_quiet_still_reports_errors_and_writes_result_file(
    handler: Callable[[str, Callable[..., Outcome]], None],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    handler("check", raising(make_error("not_found")))
    result = tmp_path / "r.json"
    code, out, err = run(capsys, "check", "--quiet", "--result-file", str(result))
    assert (code, out) == (10, "")
    assert "not_found" in err
    validate_envelope(json.loads(result.read_text()))
    code, out, _ = run(capsys, "check", "--quiet", "--json")
    assert code == 10
    validate_envelope(json.loads(out))


@pytest.mark.parametrize(
    "argv", [["check", "--json", "--result-file"], ["check", "--json", "--pr"]]
)
def test_json_envelope_is_printed_when_the_parse_fails(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(capsys, *argv)
    assert code == 2
    validate_envelope(json.loads(out))


DRY_RUN = {"check", "respond", "confirm", "approve", "reject", "edit", "drop-reply", "stop"} | {
    "prune",
    "cron",
}


@pytest.mark.parametrize("command", [*COMMANDS, "_job"])
def test_dry_run_is_accepted_only_where_documented(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run(capsys, *argv_for(command, "--dry-run"))[0]
    assert code == (1 if command in DRY_RUN else 2)


@pytest.mark.parametrize("command", [*COMMANDS, "_job"])
def test_override_escalation_is_accepted_only_on_respond_and_confirm(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run(capsys, *argv_for(command, "--override-escalation", "--actor", "human:a"))[0]
    assert code == (1 if command in {"respond", "confirm", "approve"} else 2)


@pytest.mark.parametrize("value", ["A" * 64, "a" * 65, "a" * 63, "a" * 64 + "\n", "g" * 64])
def test_expect_rejects_anything_but_64_lowercase_hex(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = run(capsys, "confirm", "rp-aaaaaaaa", "--expect", value)
    assert code == 2
    assert "--expect" in err


def test_expect_accepts_64_lowercase_hex(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(capsys, "confirm", "rp-aaaaaaaa", "--expect", "0123456789abcdef" * 4)[0] == 1


def test_abbreviated_flags_are_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(capsys, "--vers")[0] == 2
    code, out, err = run(capsys, "check", "--js")
    assert (code, out) == (2, "")
    assert "usage" in err


def test_json_without_a_command_prints_only_the_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = run(capsys, "--json")
    assert code == 2
    assert list(json.loads(out)) == ["error"]
    assert "options:" not in out + err and "positional" not in out + err


def test_stderr_marks_bidi_and_separator_characters(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, err = run(capsys, "check", "a\u202eb\u2028c")
    assert "<U+202E>" in err and "<U+2028>" in err
    assert sanitize_line("x\u202e") == "x<U+202E>"


def test_dump_json_is_sorted_indented_with_trailing_newline() -> None:
    assert dump_json({"b": 1, "a": {"d": 2, "c": "\u00e9"}}) == (
        '{\n  "a": {\n    "c": "\u00e9",\n    "d": 2\n  },\n  "b": 1\n}\n'
    )


def test_since_time_error_is_not_a_duration_error(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "export", "--since", "2024-1-1T00:00:00Z")
    assert code == 2
    assert "RFC 3339" in err and "duration" not in err


def test_lower_error_code_length_and_input_isolation() -> None:
    assert lower_error({"code": "a" * 65}) == {"code": "unknown"}
    assert lower_error({"code": "a" * 64}) == {"code": "a" * 64}
    details: dict[str, Any] = {"k": [1]}
    cleaned = lower_error({"code": "x", "details": details})
    details["k"].append(2)
    details["new"] = 1
    assert cleaned["details"] == {"k": [1]}


@pytest.mark.parametrize("state", cli.PR_STATES)
def test_every_documented_state_parses(
    state: str,
    handler: Callable[[str, Callable[..., Outcome]], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[str] = []

    def record(_ctx: Context, args: argparse.Namespace) -> Outcome:
        seen.append(args.state)
        return Outcome()

    handler("list", record)
    assert run(capsys, "list", "--state", state)[0] == 0
    assert seen == [state]


def test_documented_states_are_the_seven_of_the_interface() -> None:
    assert set(cli.PR_STATES) == {
        "idle",
        "settling",
        "queued",
        "running",
        "pending",
        "waiting",
        "closed",
    }


def test_os_user_name_is_cut_to_64(monkeypatch: pytest.MonkeyPatch) -> None:
    class Entry:
        pw_name = "u" * 100

    monkeypatch.setattr(actor_mod.pwd, "getpwuid", lambda _uid: Entry())
    assert actor_mod.os_user_name() == "u" * 64


def test_job_config_after_the_subcommand_wins(
    handler: Callable[[str, Callable[..., Outcome]], None],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    captured: list[Context] = []

    def job(ctx: Context, _args: argparse.Namespace) -> Outcome:
        captured.append(ctx)
        return Outcome()

    second = tmp_path / "b.yaml"
    second.write_text("")
    second.chmod(0o600)
    handler("_job", job)
    code = run(capsys, "--config", "/a.yaml", "_job", "rd-aaaaaaaa", "--config", str(second))[0]
    assert code == 0
    assert captured[0].config == second
