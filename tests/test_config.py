"""Config load: YAML hardening, drop-ins, precedence, environment, validation, the CLI path."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from review_respond import cli
from review_respond.config import (
    DEFAULT_OFF_LIMITS,
    MAX_DROP_INS,
    MAX_FILE_BYTES,
    Config,
    ConfigError,
    default_config_path,
    load_config,
)
from review_respond.errors import CliError

from .harness import schemas

ROOT = Path(__file__).resolve().parent.parent


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)
    return path


def conf(tmp_path: Path, text: str) -> Path:
    return write(tmp_path / "cfg" / "config.yaml", text)


def load(tmp_path: Path, text: str, env: dict[str, str] | None = None) -> Config:
    return load_config(conf(tmp_path, text), env or {}).config


def fails(tmp_path: Path, text: str, *needles: str, env: dict[str, str] | None = None) -> str:
    with pytest.raises(ConfigError) as exc:
        load(tmp_path, text, env)
    message = str(exc.value)
    for needle in needles:
        assert needle in message, message
    return message


# ---- defaults and the example file -----------------------------------------------------------


def test_defaults_when_no_file_exists(world: Any) -> None:
    loaded = load_config(None, world.env)
    assert loaded.files == ()
    cfg = loaded.config
    assert (cfg.settle, cfg.max_rounds, cfg.pending_ttl) == ("5m", 3, "7d")
    assert cfg.sandbox == "required"
    assert tuple(cfg.off_limits) == DEFAULT_OFF_LIMITS
    assert cfg.git.trailer == "Assisted-by: review-respond (confirmed by <me.name>)"


def test_example_config_loads_and_equals_the_defaults(tmp_path: Path) -> None:
    text = (ROOT / "config.example.yaml").read_text()
    cfg = load(tmp_path, text)
    assert cfg.repos["git.example.test/team/service"].respond_fresh is False
    # Everything in the example is a default except what the file sets on purpose.
    expected = Config.model_validate(
        {
            "hosts": {"git.example.test": {"token_env": "EXAMPLE_PUSH_TOKEN"}},
            "hosts_to_check": ["git.example.test"],
            "me": {"name": "me"},
            "repos": {
                "git.example.test/team/service": {
                    "respond_fresh": False,
                    "respond": {"allow_commands": [], "read_dirs": []},
                    "settle": "5m",
                    "max_rounds": 3,
                    "reviewers": [],
                }
            },
        }
    )
    assert cfg == expected


def test_empty_and_comment_only_files_are_defaults(tmp_path: Path) -> None:
    assert load(tmp_path, "") == Config()
    assert load(tmp_path, "# nothing\n") == Config()


def test_default_path_follows_xdg_then_home() -> None:
    assert default_config_path({"XDG_CONFIG_HOME": "/x", "HOME": "/h"}) == Path(
        "/x/review-respond/config.yaml"
    )
    # a relative XDG_CONFIG_HOME is ignored (XDG spec)
    assert default_config_path({"XDG_CONFIG_HOME": "rel", "HOME": "/h"}) == Path(
        "/h/.config/review-respond/config.yaml"
    )


def test_default_location_is_read(world: Any) -> None:
    path = default_config_path(world.env)
    write(path, "max_rounds: 7\n")
    assert load_config(None, world.env).config.max_rounds == 7


def test_explicit_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml", {})


# ---- YAML hardening --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("settle: 5m\nsettle: 6m\n", "duplicate key"),
        ("me: {name: a, name: b}\n", "duplicate key"),
        ("a: &x 5m\nsettle: *x\n", "not allowed"),
        ("settle: *x\n", "not allowed"),
        ("? [a]\n: 1\n", "keys must be strings"),
        ("1: x\n", "keys must be strings"),
        ("<<: {max_rounds: 2}\n", "merge key"),
        ("settle: !!str 5m\n", "tags are not allowed"),
        ("settle: !!python/object:os.system x\n", "tags are not allowed"),
        ("- a\n- b\n", "must be a mapping"),
        ("a: 1\n---\nb: 2\n", "invalid YAML"),
        ("a: [unclosed\n", "invalid YAML"),
        ('a: "\\xZZ"\n', "invalid YAML"),
    ],
)
def test_hostile_yaml_is_refused(tmp_path: Path, text: str, needle: str) -> None:
    fails(tmp_path, text, needle)


def test_error_never_quotes_input(tmp_path: Path) -> None:
    message = fails(tmp_path, "a: [SECRETVALUE\n")
    assert "SECRETVALUE" not in message
    message = fails(tmp_path, "settle: SECRETVALUE\n", "settle")
    assert "SECRETVALUE" not in message
    message = fails(tmp_path, "max_rounds: SECRETVALUE\n", "max_rounds")
    assert "SECRETVALUE" not in message


def test_deep_nesting_is_refused_not_crashed(tmp_path: Path) -> None:
    fails(tmp_path, "a: " + "[" * 5000 + "]" * 5000 + "\n", "invalid YAML")


def test_invalid_utf8_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_bytes(b"settle: \xff\xfe\n")
    path.chmod(0o600)
    with pytest.raises(ConfigError, match="UTF-8"):
        load_config(path, {})


def test_oversized_file_is_refused(tmp_path: Path) -> None:
    fails(tmp_path, "# " + "x" * MAX_FILE_BYTES + "\n", "larger than")


def test_yaml_one_one_booleans_are_strings(tmp_path: Path) -> None:
    fails(tmp_path, "resolve_after_confirm: yes\n", "resolve_after_confirm")
    fails(tmp_path, "attention:\n  ineligible_comment: on\n", "attention.ineligible_comment")


def test_non_regular_and_unsafe_files_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not a regular file"):
        load_config(Path("/dev/null"), {})
    with pytest.raises(ConfigError, match="not a regular file"):
        load_config(tmp_path, {})
    path = conf(tmp_path, "")
    path.chmod(0o660)
    with pytest.raises(ConfigError, match="writable by group or others"):
        load_config(path, {})
    path.chmod(0o602)
    with pytest.raises(ConfigError, match="writable by group or others"):
        load_config(path, {})


def test_symlinked_config_is_followed(tmp_path: Path) -> None:
    target = write(tmp_path / "real.yaml", "max_rounds: 4\n")
    link = tmp_path / "link.yaml"
    link.symlink_to(target)
    assert load_config(link, {}).config.max_rounds == 4


# ---- drop-ins and merge ----------------------------------------------------------------------


def dropin_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "cfg" / "config.d"
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    return directory


def test_dropins_merge_in_lexical_order(tmp_path: Path) -> None:
    main = conf(
        tmp_path, "max_rounds: 2\nsettle: 1m\nme:\n  name: base\n  accounts: {a.test: '1'}\n"
    )
    directory = dropin_dir(tmp_path)
    write(directory / "20-b.yaml", "settle: 3m\n")
    write(directory / "10-a.yaml", "settle: 2m\nme:\n  accounts: {b.test: '2'}\n")
    write(directory / "ignored.txt", "settle: 9m\n")
    write(directory / ".hidden.yaml", "settle: 9m\n")
    loaded = load_config(main, {})
    cfg = loaded.config
    assert cfg.settle == "3m"  # 20-b after 10-a
    assert cfg.max_rounds == 2
    assert cfg.me.name == "base"
    assert cfg.me.accounts == {"a.test": "1", "b.test": "2"}  # maps merge
    assert [f.name for f in loaded.files] == ["config.yaml", "10-a.yaml", "20-b.yaml"]


def test_hook_lists_append_other_lists_replace(tmp_path: Path) -> None:
    main = conf(tmp_path, "hooks:\n  pre-push: [{cmd: a}]\nreviewers: ['h.test:1']\n")
    directory = dropin_dir(tmp_path)
    write(directory / "10.yaml", "hooks:\n  pre-push: [{cmd: b}]\nreviewers: ['h.test:2']\n")
    cfg = load_config(main, {}).config
    assert [h.cmd for h in cfg.hooks.pre_push] == ["a", "b"]
    assert cfg.reviewers == ["h.test:2"]


def test_dropins_without_a_main_file_read_beside_the_default(world: Any) -> None:
    directory = default_config_path(world.env).parent / "config.d"
    directory.mkdir(parents=True)
    directory.chmod(0o700)
    write(directory / "awkit.yaml", "max_rounds: 9\n")
    assert load_config(None, world.env).config.max_rounds == 9


def test_bad_dropin_is_named(tmp_path: Path) -> None:
    main = conf(tmp_path, "")
    write(dropin_dir(tmp_path) / "50-bad.yaml", "max_rounds: 0\n")
    with pytest.raises(ConfigError) as exc:
        load_config(main, {})
    assert "50-bad.yaml" in str(exc.value) and "max_rounds" in str(exc.value)


def test_unsafe_dropin_dir_and_count(tmp_path: Path) -> None:
    main = conf(tmp_path, "")
    directory = dropin_dir(tmp_path)
    directory.chmod(0o777)
    with pytest.raises(ConfigError, match="writable by group or others"):
        load_config(main, {})
    directory.chmod(0o700)
    for i in range(MAX_DROP_INS + 1):
        (directory / f"{i:04}.yaml").write_text("")
    with pytest.raises(ConfigError, match="drop-in files"):
        load_config(main, {})


def test_dropin_dir_that_is_a_file_is_refused(tmp_path: Path) -> None:
    main = conf(tmp_path, "")
    write(tmp_path / "cfg" / "config.d", "x")
    with pytest.raises(ConfigError, match="not a directory"):
        load_config(main, {})


# ---- validation names the key ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("bogus: 1\n", "bogus"),
        ("me:\n  bogus: 1\n", "me.bogus"),
        ("settle: 5\n", "settle"),
        ("settle: 5x\n", "settle"),
        ("settle: -5m\n", "settle"),
        ("settle: 99999999d\n", "settle"),
        ("settle: " + "9" * 5000 + "m\n", "settle"),
        ("settle: null\n", "settle"),
        ("max_rounds: 0\n", "max_rounds"),
        ("max_rounds: 1001\n", "max_rounds"),
        ("max_rounds: '3'\n", "max_rounds"),
        ("max_rounds: 3.0\n", "max_rounds"),
        ("max_rounds: true\n", "max_rounds"),
        ("pending_ttl: 0\n", "pending_ttl"),
        ("access_cache_ttl: 1y\n", "access_cache_ttl"),
        ("prefer_owner: 'a b'\n", "prefer_owner"),
        ("sandbox: maybe\n", "sandbox"),
        ("setting_sources: bogus\n", "setting_sources"),
        ("me:\n  name: 'a b'\n", "me.name"),
        ("me:\n  name: ''\n", "me.name"),
        ("me:\n  accounts: {Bad Host: '1'}\n", "me.accounts"),
        ("me:\n  accounts: {h.test: 12345}\n", "me.accounts.h.test"),
        ("hosts:\n  h.test:\n    token_env: 1BAD\n", "hosts.h.test.token_env"),
        ("hosts:\n  h.test:\n    token_command: []\n", "hosts.h.test.token_command"),
        ("hosts:\n  h.test:\n    token_command: ['']\n", "hosts.h.test.token_command[0]"),
        ('hosts:\n  h.test:\n    token_command: ["a\\nb"]\n', "hosts.h.test.token_command"),
        ("hosts:\n  h.test:\n    token: x\n", "hosts.h.test.token"),
        ("hosts_to_check: [h.test, h.test]\n", "hosts_to_check"),
        ("hosts_to_check: [BAD]\n", "hosts_to_check[0]"),
        ("reviewers: [nocolon]\n", "reviewers[0]"),
        ("bot_authors: ['h.test:1', 'h.test:1']\n", "bot_authors"),
        ("respond_to_own_comments_prefix: 'a b'\n", "respond_to_own_comments_prefix"),
        ("off_limits: ['/abs']\n", "off_limits[0]"),
        ("off_limits: ['a/../b']\n", "off_limits[0]"),
        ("off_limits: ['a/']\n", "off_limits[0]"),
        ("off_limits: [a, a]\n", "off_limits"),
        ("env_allowlist: ['A=B']\n", "env_allowlist[0]"),
        ("skill: ''\n", "skill"),
        ('skill: "a\\nb"\n', "skill"),
        ("agent:\n  timeout: 0\n", "agent.timeout"),
        ("jobs:\n  queue: 'Bad Queue'\n", "jobs.queue"),
        ("jobs:\n  kill_grace: 0s\n", "jobs.kill_grace"),
        ("jobs:\n  max_running: 0\n", "jobs.max_running"),
        ("jobs:\n  pass_env: [REVIEW_RESPOND_X]\n", "jobs.pass_env"),
        ("jobs:\n  pass_env: [review_respond_x]\n", "jobs.pass_env"),
        ('git:\n  trailer: "a\\nb"\n', "git.trailer"),
        ("git:\n  trailer: ''\n", "git.trailer"),
        ("git:\n  user_email: ab\n", "git.user_email"),
        ("limits:\n  requests_per_host: 0\n", "limits.requests_per_host"),
        ("limits:\n  prompt_bytes: 10\n", "limits.prompt_bytes"),
        ("limits:\n  diff_bytes: 999999999999\n", "limits.diff_bytes"),
        ("retention:\n  records: 30\n", "retention.records"),
        ('cron:\n  env_file: "a b"\n', "cron.env_file"),
        ('cron:\n  env_file: "it\'s"\n', "cron.env_file"),
        ("hooks:\n  nope: []\n", "hooks.nope"),
        ("hooks:\n  pre-round: [{timeout: 5}]\n", "hooks.pre-round[0].cmd"),
        ("hooks:\n  pre-round: [{cmd: ''}]\n", "hooks.pre-round[0].cmd"),
        ("hooks:\n  pre-round: [{cmd: '   '}]\n", "hooks.pre-round[0].cmd"),
        ('hooks:\n  pre-round: [{cmd: "a\\x1bb"}]\n', "hooks.pre-round[0].cmd"),
        ("hooks:\n  pre-round: [{cmd: a, timeout: 0}]\n", "hooks.pre-round[0].timeout"),
        ("hooks:\n  pre-round: [{cmd: a, timeout: 601}]\n", "hooks.pre-round[0].timeout"),
        ("hooks:\n  pre-round: [{cmd: a, timeout: 1h}]\n", "hooks.pre-round[0].timeout"),
        ("hooks:\n  pre-round: [{cmd: a, timeout: 0s}]\n", "hooks.pre-round[0].timeout"),
        ("hooks:\n  pre-push: [{cmd: a, async: true}]\n", "hooks.pre-push"),
        ("hooks:\n  post-confirm: [{cmd: a, extra: 1}]\n", "hooks.post-confirm[0].extra"),
        ("repos:\n  bad: {}\n", "repos"),
        ("repos:\n  h.test/a/../b: {}\n", "repos"),
        ("repos:\n  h.test/a:\n    settle: nope\n", "repos.h.test/a.settle"),
        ("repos:\n  h.test/a:\n    max_rounds: 0\n", "repos.h.test/a.max_rounds"),
        ("repos:\n  h.test/a:\n    settle: null\n", "repos.h.test/a.settle"),
        ("repos:\n  h.test/a:\n    sandbox: off\n", "repos.h.test/a.sandbox"),
        ("repos:\n  h.test/a:\n    respond:\n      allow_commands: [[]]\n", "allow_commands[0]"),
        (
            "repos:\n  h.test/a:\n    respond:\n      allow_commands: ['make']\n",
            "allow_commands[0]",
        ),
        ("repos:\n  h.test/a:\n    respond:\n      read_dirs: ['']\n", "read_dirs[0]"),
        ("repos:\n  h.test/a:\n    reviewers: ['h.test:1', 'h.test:1']\n", "reviewers"),
    ],
)
def test_invalid_values_name_the_key(tmp_path: Path, text: str, key: str) -> None:
    message = fails(tmp_path, text, key)
    assert "config.yaml" in message


def test_hostile_key_names_are_escaped_and_capped(tmp_path: Path) -> None:
    message = fails(tmp_path, '"\\x1b[31m' + "k" * 500 + '": 1\n', "unknown key")
    assert "\x1b" not in message
    assert len(message) < 400


def test_many_errors_are_summarized(tmp_path: Path) -> None:
    text = "".join(f"k{i}: 1\n" for i in range(20))
    message = fails(tmp_path, text)
    assert "and " in message and "more" in message


def test_zero_is_allowed_where_it_means_something(tmp_path: Path) -> None:
    cfg = load(tmp_path, "settle: '0'\naccess_cache_ttl: 0s\nretention:\n  records: '0'\n")
    assert (cfg.settle, cfg.access_cache_ttl, cfg.retention.records) == ("0", "0s", "0")


def test_hook_timeout_forms(tmp_path: Path) -> None:
    cfg = load(tmp_path, "hooks:\n  post-pending: [{cmd: a, timeout: 90s}, {cmd: b, timeout: 5}]\n")
    first, second = cfg.hooks.post_pending
    assert (first.timeout_ms, second.timeout_ms, first.async_) == (90_000, 5_000, False)


def test_sandbox_weakening_warns(tmp_path: Path) -> None:
    loaded = load_config(conf(tmp_path, "sandbox: preferred\n"), {})
    assert [w.code for w in loaded.warnings] == ["sandbox_weakened"]
    assert loaded.warnings[0].key == "sandbox"
    assert load_config(conf(tmp_path, "sandbox: required\n"), {}).warnings == ()


def test_sha256_is_stable_and_follows_content(tmp_path: Path) -> None:
    a, b = load(tmp_path, "max_rounds: 4\n"), load(tmp_path, "max_rounds: 4\n")
    assert a.sha256() == b.sha256() and len(a.sha256()) == 64
    assert load(tmp_path, "max_rounds: 5\n").sha256() != a.sha256()


# ---- precedence: repo > env > config > defaults ----------------------------------------------


def test_environment_overrides_the_file(tmp_path: Path) -> None:
    text = "settle: 5m\nmax_rounds: 3\nprefer_owner: a\n"
    env = {
        "REVIEW_RESPOND_SETTLE": "9m",
        "REVIEW_RESPOND_MAX_ROUNDS": "8",
        "REVIEW_RESPOND_ACCESS_CACHE_TTL": "2h",
        "REVIEW_RESPOND_PREFER_OWNER": "b",
        "REVIEW_RESPOND_RETENTION__RECORDS": "10d",
        "REVIEW_RESPOND_LIMITS__REQUESTS_PER_HOST": "50",
    }
    cfg = load(tmp_path, text, env)
    assert (cfg.settle, cfg.max_rounds, cfg.prefer_owner) == ("9m", 8, "b")
    assert (cfg.access_cache_ttl, cfg.retention.records) == ("2h", "10d")
    assert cfg.limits.requests_per_host == 50


def test_repo_overrides_environment_and_file(tmp_path: Path) -> None:
    text = (
        "settle: 5m\nreviewers: ['h.test:1']\n"
        "repos:\n  h.test/a:\n    settle: 1m\n    reviewers: []\n    respond_fresh: true\n"
        "    respond: {allow_commands: [[make, test]], read_dirs: [/d]}\n  h.test/b: {}\n"
    )
    cfg = load(tmp_path, text, {"REVIEW_RESPOND_SETTLE": "9m", "REVIEW_RESPOND_MAX_ROUNDS": "6"})
    a = cfg.repo_settings("h.test/a")
    assert (a.settle, a.max_rounds, a.reviewers, a.respond_fresh) == ("1m", 6, (), True)
    assert a.allow_commands == (("make", "test"),) and a.read_dirs == ("/d",)
    for other in ("h.test/b", "h.test/unlisted"):
        b = cfg.repo_settings(other)
        assert (b.settle, b.max_rounds, b.reviewers, b.respond_fresh) == (
            "9m", 6, ("h.test:1",), False
        )  # fmt: skip


@pytest.mark.parametrize(
    ("var", "value", "key"),
    [
        ("REVIEW_RESPOND_SETTLE", "soon", "settle"),
        ("REVIEW_RESPOND_SETTLE", "", "settle"),
        ("REVIEW_RESPOND_MAX_ROUNDS", "three", "max_rounds"),
        ("REVIEW_RESPOND_MAX_ROUNDS", "03", "max_rounds"),
        ("REVIEW_RESPOND_MAX_ROUNDS", "-1", "max_rounds"),
        ("REVIEW_RESPOND_MAX_ROUNDS", "9" * 30, "max_rounds"),
        ("REVIEW_RESPOND_MAX_ROUNDS", "0", "max_rounds"),
        ("REVIEW_RESPOND_LIMITS__REQUESTS_PER_HOST", "x", "limits.requests_per_host"),
        ("REVIEW_RESPOND_PREFER_OWNER", "a b", "prefer_owner"),
        ("REVIEW_RESPOND_RETENTION__RECORDS", "1", "retention.records"),
    ],
)
def test_bad_environment_value_names_variable_and_key(
    tmp_path: Path, var: str, value: str, key: str
) -> None:
    message = fails(tmp_path, "", var, key, env={var: value})
    assert "environment variable" in message
    if value:
        assert value not in message


@pytest.mark.parametrize(
    "var",
    [
        "REVIEW_RESPOND_SANDBOX",
        "REVIEW_RESPOND_OFF_LIMITS",
        "REVIEW_RESPOND_HOOKS__PRE_PUSH",
        "REVIEW_RESPOND_REPOS",
        "REVIEW_RESPOND_GIT__TRAILER",
        "REVIEW_RESPOND_PENDING_TTL",
        "REVIEW_RESPOND_TYPO",
    ],
)
def test_other_variables_never_set_config(tmp_path: Path, var: str) -> None:
    path = conf(tmp_path, "")
    loaded = load_config(path, {var: "off"})
    assert loaded.config == Config()
    assert [w.code for w in loaded.warnings] == ["env_ignored"]
    assert var in loaded.warnings[0].message


def test_hook_variables_are_not_config_and_not_warned(tmp_path: Path) -> None:
    env = {
        "REVIEW_RESPOND_HOOK": "pre-push",
        "REVIEW_RESPOND_ID": "x",
        "REVIEW_RESPOND_OBJECT_TYPE": "pending",
    }
    loaded = load_config(conf(tmp_path, ""), env)
    assert loaded.warnings == () and loaded.config == Config()


def test_environment_alone_without_files(world: Any) -> None:
    cfg = load_config(None, {**world.env, "REVIEW_RESPOND_MAX_ROUNDS": "2"}).config
    assert cfg.max_rounds == 2


# ---- the example file and the YAML round trip agree with the schema ---------------------------


def test_defaults_dump_validates_against_the_schema() -> None:
    schemas.validate("review-respond/config.json", Config().canonical())


def test_example_dump_validates_against_the_schema(tmp_path: Path) -> None:
    cfg = load(tmp_path, (ROOT / "config.example.yaml").read_text())
    schemas.validate("review-respond/config.json", cfg.canonical())
    json.dumps(cfg.canonical())


def test_default_off_limits_match_the_schema_and_example() -> None:
    schema = json.loads((ROOT / "schemas" / "config.json").read_text())
    assert list(DEFAULT_OFF_LIMITS) == schema["properties"]["off_limits"]["default"]
    example = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    assert list(DEFAULT_OFF_LIMITS) == example["off_limits"]


# ---- the CLI path -----------------------------------------------------------------------------


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_invalid_config_exits_2_with_the_envelope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = conf(tmp_path, "max_rounds: 0\n")
    code, out, err = run(capsys, "pending", "--json", "--config", str(path))
    assert code == 2
    body = json.loads(out)
    schemas.validate("review-respond/cli/error.json", body)
    assert body["error"]["code"] == "config_invalid"
    assert "max_rounds" in body["error"]["message"]
    assert "config_invalid" in err


def test_missing_explicit_config_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(capsys, "pending", "--json", "--config", str(tmp_path / "none.yaml"))
    assert (code, json.loads(out)["error"]["code"]) == (2, "config_invalid")


def test_invalid_environment_exits_2_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_RESPOND_MAX_ROUNDS", "many")
    code, out, _ = run(capsys, "pending", "--json")
    message = json.loads(out)["error"]["message"]
    assert code == 2 and "REVIEW_RESPOND_MAX_ROUNDS" in message and "max_rounds" in message


def test_validate_does_not_fail_on_a_bad_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = conf(tmp_path, "max_rounds: 0\n")
    code, out, _ = run(capsys, "validate", "--json", "--config", str(path))
    # exit 1 `internal`: validate is not implemented yet (TODO(#12)), but not config_invalid
    assert json.loads(out)["error"]["code"] == "internal" and code == 1


def test_human_only_outranks_a_bad_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = conf(tmp_path, "max_rounds: 0\n")
    code, out, _ = run(
        capsys, "pending", "--json", "--config", str(path), "--yes", "--actor", "auto:x"
    )
    assert (code, json.loads(out)["error"]["code"]) == (2, "human_only")


def test_default_actor_uses_me_name_on_a_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from review_respond import actor as actor_mod

    seen: list[Any] = []
    path = conf(tmp_path, "me:\n  name: alice\n")
    monkeypatch.setattr(actor_mod, "stdin_is_tty", lambda: True)
    monkeypatch.setitem(
        cli.HANDLERS,
        "pending",
        lambda ctx, _a: (seen.append(ctx), cli.Outcome())[1],
    )
    assert run(capsys, "pending", "--config", str(path))[0] == 0
    assert seen[0].actor == "human:alice"
    assert seen[0].settings.me.name == "alice"


def test_default_actor_falls_back_to_the_os_user_without_me_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from review_respond import actor as actor_mod

    seen: list[Any] = []
    path = conf(tmp_path, "")
    monkeypatch.setattr(actor_mod, "stdin_is_tty", lambda: True)
    monkeypatch.setitem(
        cli.HANDLERS, "pending", lambda ctx, _a: (seen.append(ctx), cli.Outcome())[1]
    )
    run(capsys, "pending", "--config", str(path))
    assert seen[0].actor == f"human:{actor_mod.os_user_name()}"


def test_warnings_go_to_stderr_unless_quiet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = conf(tmp_path, "sandbox: off\n")
    _, _, err = run(capsys, "pending", "--config", str(path))
    assert "sandbox_weakened" in err
    _, _, err = run(capsys, "pending", "--config", str(path), "--quiet")
    assert "sandbox_weakened" not in err


def test_config_error_is_a_cli_error() -> None:
    error = ConfigError("f", "k", "bad").to_cli_error()
    assert isinstance(error, CliError) and (error.code, error.exit_code) == ("config_invalid", 2)
