"""Session settings and the generated permission profile (issue #21)."""

from __future__ import annotations

import builtins
import copy
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from review_respond import settings
from review_respond.config import Config
from review_respond.errors import CliError
from review_respond.gateway import Gateway
from review_respond.settings import (
    MANDATORY_DENIES,
    build_session_settings,
    check_throwaway_root,
    remove_profile_file,
    render_profile,
    write_profile_file,
)
from review_respond.store import StateDir, open_state_dir

from .harness import schemas
from .harness.fakes import Fakes
from .settings_support import CFG, REPO, ROOT, SCHEMA_ID, STATE, home_env, prof, refused

__all__ = ["home_env"]  # the fixture below is autouse through pytestmark

pytestmark = pytest.mark.usefixtures("home_env")


# ---- the generated document ---------------------------------------------------------------


def test_default_profile_golden(golden: Any) -> None:
    golden.check("settings/profile-default", prof(), SCHEMA_ID)


def test_repo_profile_golden(golden: Any) -> None:
    doc = prof(
        allow_commands=(("make", "test"), ("python", "-m", "pytest")),
        read_dirs=("/opt/toolchain/lib", "~/.cache/tools"),
    )
    golden.check("settings/profile-repo", doc, SCHEMA_ID)


def test_output_is_deterministic_and_canonical() -> None:
    a = render_profile(prof(allow_commands=(("make", "test"),)))
    b = render_profile(prof(allow_commands=(("make", "test"),)))
    assert a == b
    assert a.endswith(b"\n")
    assert json.loads(a) == prof(allow_commands=(("make", "test"),))


def test_mandatory_denies_match_the_schema_and_are_present() -> None:
    schema = json.loads((ROOT / "schemas" / "permission-profile.json").read_text())
    in_schema = [
        c["contains"]["const"]
        for c in schema["properties"]["permissions"]["properties"]["deny"]["allOf"]
    ]
    assert in_schema == list(MANDATORY_DENIES)
    deny = prof()["permissions"]["deny"]
    for rule in MANDATORY_DENIES:
        assert rule in deny
    for tool in ("Read", "Edit", "Write"):
        assert f"{tool}(/{CFG}/**)" in deny
        assert f"{tool}(/{STATE}/**)" in deny


def test_no_network_and_nothing_writable_outside_the_throwaway() -> None:
    doc = prof()
    assert doc["sandbox"]["network"] == {"allowedDomains": []}
    assert doc["sandbox"]["filesystem"]["allowWrite"] == []
    assert doc["sandbox"]["filesystem"]["denyRead"] == [CFG, STATE]
    assert doc["sandbox"]["filesystem"]["denyWrite"] == [CFG, STATE]
    assert set(doc) == {"permissions", "sandbox"}  # no hooks, env, mcp, mode keys


def test_allow_rules_are_scoped_to_the_throwaway() -> None:
    allow = prof()["permissions"]["allow"]
    assert "Edit(/${CWD}/**)" in allow and "Write(/${CWD}/**)" in allow
    assert "Read(/${CWD}/**)" in allow
    for bare in ("Bash", "Edit", "Write", "Read", "Agent", "Task", "WebFetch", "Bash(*)"):
        assert bare not in allow
    for rule in allow:
        settings.check_allow_rule(rule)  # the bundled profile passes its own check


def test_schema_rejects_weakened_documents() -> None:
    good = prof()
    schemas.validate(SCHEMA_ID, good)
    muts: list[Any] = []
    for fn in (
        lambda d: d.update(hooks={"PreToolUse": []}),
        lambda d: d["permissions"].update(defaultMode="bypassPermissions"),
        lambda d: d["permissions"]["deny"].remove("Bash(gh:*)"),
        lambda d: d["sandbox"]["network"].update(allowedDomains=["example.test"]),
        lambda d: d["sandbox"]["filesystem"].update(allowWrite=["/"]),
        lambda d: d["sandbox"].update(excludedCommands=["git"]),
        lambda d: d["permissions"]["allow"].append("a\nb"),
        lambda d: d["sandbox"]["filesystem"]["allowRead"].append("rel/path"),
        lambda d: d["sandbox"]["filesystem"]["allowRead"].append("/a/*"),
    ):
        doc = copy.deepcopy(good)
        fn(doc)
        muts.append(doc)
    for doc in muts:
        with pytest.raises(Exception):  # noqa: B017 (jsonschema.ValidationError)
            schemas.validate(SCHEMA_ID, doc)


def test_profile_has_no_text_from_outside_the_config_and_fixed_strings() -> None:
    text = render_profile(prof(allow_commands=(("make", "test"),), read_dirs=("/opt/x",)))
    assert b"\\u" not in text  # ASCII only, no escapes of hidden characters


# ---- allow_commands ------------------------------------------------------------------------


def test_allow_commands_become_prefix_rules() -> None:
    allow = prof(allow_commands=(("make", "test"), ("python", "-m", "pytest"), ("make", "test")))[
        "permissions"
    ]["allow"]
    assert allow.count("Bash(make test:*)") == 1
    assert "Bash(python -m pytest:*)" in allow


@pytest.mark.parametrize(
    "argv",
    [
        ("make", "a b"),
        ("make", "x\ny"),
        ("make", "x\x1b"),
        ("make", "*"),
        ("make", "a(b)"),
        ("make", "a:b"),
        ("make", "a\\b"),
        ("make", "a‮b"),
        ("make", "a​b"),
        ("make", ""),
        ("bash", "-c", "x"),
        ("sh",),
        ("env", "make"),
        ("sudo", "make"),
        ("xargs", "rm"),
        ("curl", "http://x"),
        ("gh", "pr"),
        ("git", "push"),
        ("agent-spawn", "launch"),
        ("python",),
        ("python3",),
        ("node",),
        ("python", "-c", "print(1)"),
        ("python", "-m", "pytest", "-c"),
        ("npx", "x"),
        ("/bin/sh", "-c", "x"),
        ("../x", "y"),
        ("make", ".."),
        (),
        ("m" * 300,),
    ],
)
def test_hostile_allow_commands_are_refused(argv: tuple[str, ...]) -> None:
    refused(prof, allow_commands=(argv,))


def test_allow_commands_are_bounded() -> None:
    refused(prof, allow_commands=tuple(("make", f"t{i}") for i in range(65)))
    refused(prof, allow_commands=(("make", *(f"a{i}" for i in range(40))),))
    refused(prof, allow_commands=(("make", *("a" * 200 for _ in range(10))),))


def test_error_messages_never_echo_raw_hostile_text() -> None:
    err = refused(prof, allow_commands=(("make", "x\x1b[31m‮"),))
    assert "\x1b" not in err.message and "‮" not in err.message


# ---- read_dirs -----------------------------------------------------------------------------


def test_read_dirs_become_read_allow_and_allow_read() -> None:
    doc = prof(read_dirs=("/opt/lib", "~/.cache/tools"))
    assert "Read(//opt/lib/**)" in doc["permissions"]["allow"]
    assert "Read(~/.cache/tools/**)" in doc["permissions"]["allow"]
    assert doc["sandbox"]["filesystem"]["allowRead"] == ["/opt/lib", "~/.cache/tools"]


@pytest.mark.parametrize(
    "path",
    [
        "rel/dir", "./x", "", "/", "~", "~/", "/a/../b", "~/../x", "/a//b", "//a", "/a/", "/a/./b",
        "/a/*", "/a/?", "/a/[x]", "/a/{x}", "/a\\b", "/a\nb", "/a\x00b", "/a‮b", "~user/x",
        "/home/u/.ssh", "/home/u/.ssh/keys", "/x/.aws", "~/.config/app", "~/.claude",
        f"{STATE}", f"{STATE}/x", f"{CFG}/config.d", "/home/u/.local", "/home/u/.local/state",
        "/home", "/home/u", "/" + "a" * 1100,
    ],
)  # fmt: skip
def test_hostile_read_dirs_are_refused(path: str) -> None:
    refused(prof, read_dirs=(path,))


def test_read_dirs_are_bounded() -> None:
    refused(prof, read_dirs=tuple(f"/opt/d{i}" for i in range(65)))


# ---- replaced profile ----------------------------------------------------------------------


def test_replacement_profile_keeps_the_fixed_denies() -> None:
    base = {
        "permissions": {"allow": ["Read(/${CWD}/**)", "Edit(/${CWD}/**)"], "deny": []},
        "sandbox": {"filesystem": {"allowRead": []}},
    }
    doc = prof(base=base)
    schemas.validate(SCHEMA_ID, doc)
    assert "Bash(git push:*)" in doc["permissions"]["deny"]
    assert doc["permissions"]["allow"] == ["Read(/${CWD}/**)", "Edit(/${CWD}/**)"]


@pytest.mark.parametrize(
    "base",
    [
        {"hooks": {}},
        {"permissions": {"allow": ["Bash"]}},
        {"permissions": {"allow": ["Bash(*)"]}},
        {"permissions": {"allow": ["Bash(:*)"]}},
        {"permissions": {"allow": ["Edit"]}},
        {"permissions": {"allow": ["Write(/x/**)"]}},
        {"permissions": {"allow": ["Edit(/${CWD}/../x/**)"]}},
        {"permissions": {"allow": ["Read"]}},
        {"permissions": {"allow": ["WebFetch"]}},
        {"permissions": {"allow": ["mcp__x__y"]}},
        {"permissions": {"allow": ["Agent"]}},
        {"permissions": {"allow": ["Bash(gh pr:*)"]}},
        {"permissions": {"allow": [["x"]]}},
        {"permissions": {"allow": [], "defaultMode": "bypassPermissions"}},
        {"permissions": {"ask": []}},
        {"permissions": "x"},
        {"sandbox": {"network": {"allowedDomains": ["x.test"]}}},
        {"sandbox": {"filesystem": {"allowWrite": ["/"]}}},
        {"sandbox": {"excludedCommands": ["git"]}},
        [],
        "x",
    ],
)
def test_unsafe_replacement_profiles_are_refused(base: Any) -> None:
    refused(prof, base=base)


def test_profile_file_is_read_from_a_safe_file(tmp_path: Path) -> None:
    d = tmp_path / "cfg"
    d.mkdir(mode=0o700)
    f = d / "profile.json"
    f.write_text(json.dumps({"permissions": {"allow": ["Skill"], "deny": []}}))
    f.chmod(0o600)
    assert settings.load_base_profile(str(f)) == {"permissions": {"allow": ["Skill"], "deny": []}}
    f.chmod(0o666)
    refused(settings.load_base_profile, str(f))
    f.chmod(0o600)
    f.write_text("{not json")
    refused(settings.load_base_profile, str(f))
    refused(settings.load_base_profile, str(d / "missing.json"))
    f.write_text("a: &x 1\nb: *x\n")
    refused(settings.load_base_profile, str(f))


# ---- session settings ----------------------------------------------------------------------


def cfg(**kw: Any) -> Config:
    return Config.model_validate(kw)


def session(config: Config | None = None, **kw: Any) -> settings.SessionSettings:
    return build_session_settings(config or cfg(), REPO, config_dir=CFG, state_dir=STATE, **kw)


def test_defaults() -> None:
    s = session()
    assert (s.kind, s.mode, s.skill, s.sandbox) == (
        "respond",
        "headless",
        "respond-to-review",
        "required",
    )
    assert s.setting_sources == () and s.env_allowlist == ()
    assert s.model is None and s.timeout is None and s.warnings == ()
    schemas.validate(SCHEMA_ID, s.profile)


def test_values_come_from_the_config() -> None:
    s = session(
        cfg(
            skill="other-skill", model="m1", agent={"timeout": "30m"}, sandbox="preferred",
            env_allowlist=["ZED", "ALPHA"],
        )
    )  # fmt: skip
    assert (s.skill, s.model, s.timeout, s.sandbox) == ("other-skill", "m1", "30m", "preferred")


def test_repo_override_feeds_the_profile() -> None:
    config = cfg(
        repos={REPO: {"respond": {"allow_commands": [["make", "test"]], "read_dirs": ["/opt/lib"]}}}
    )
    allow = session(config).profile["permissions"]["allow"]
    assert "Bash(make test:*)" in allow and "Read(//opt/lib/**)" in allow
    other = build_session_settings(config, "git.example.test/x/y", config_dir=CFG, state_dir=STATE)
    assert "Bash(make test:*)" not in other.profile["permissions"]["allow"]


@pytest.mark.parametrize("level", ["preferred", "off"])
def test_weakened_sandbox_warns(level: str) -> None:
    s = session(cfg(sandbox=level))
    assert [w.code for w in s.warnings] == ["sandbox_weakened"]
    assert s.sandbox == level


def test_required_sandbox_does_not_warn() -> None:
    assert session(cfg(sandbox="required")).warnings == ()


def test_unknown_sandbox_is_refused() -> None:
    config = cfg()
    object.__setattr__(config, "sandbox", "none")
    refused(build_session_settings, config, REPO, config_dir=CFG, state_dir=STATE)


def test_setting_sources_are_always_empty() -> None:
    assert session(cfg(setting_sources="")).setting_sources == ()


@pytest.mark.parametrize(
    "value", ["user", "project", "local", "user,project", "project,user", "user,local"]
)
def test_any_setting_source_is_refused_at_config_load(value: str) -> None:
    with pytest.raises(Exception, match="setting_sources"):
        cfg(setting_sources=value)


@pytest.mark.parametrize("value", ["user", "project", "local", "user,project"])
def test_any_setting_source_is_refused_by_the_session_settings(value: str) -> None:
    config = cfg()
    object.__setattr__(config, "setting_sources", value)  # a config that skipped validation
    err = refused(session, config)
    assert "setting_sources" in err.message and "unsafe_setting_sources" in err.message


def test_env_allowlist_sorted_and_unique() -> None:
    s = session(cfg(env_allowlist=["ZED", "ALPHA", "TERM"]))
    assert s.env_allowlist == ("ALPHA", "TERM", "ZED")


@pytest.mark.parametrize(
    "name",
    [
        "GH_TOKEN", "GITHUB_TOKEN", "GITLAB_TOKEN", "MY_API_TOKEN", "DB_PASSWORD",
        "AWS_SECRET_ACCESS_KEY",
        "SSH_AUTH_SOCK", "ANTHROPIC_API_KEY", "REVIEW_RESPOND_X", "PATH", "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES", "BASH_ENV", "NODE_OPTIONS", "PYTHONPATH", "GIT_SSH_COMMAND",
        "GIT_CONFIG_GLOBAL", "HOME", "IFS",
    ],
)  # fmt: skip
def test_secret_or_dangerous_env_names_are_refused(name: str) -> None:
    refused(session, cfg(env_allowlist=[name]))


def test_a_configured_token_variable_is_never_allowed() -> None:
    config = cfg(
        hosts={"git.example.test": {"token_env": "FORGE_ACCESS"}}, env_allowlist=["FORGE_ACCESS"]
    )
    refused(session, config)


def test_env_allowlist_is_bounded() -> None:
    refused(session, cfg(env_allowlist=[f"V{i}" for i in range(65)]))


def test_model_and_skill_cannot_pose_as_options() -> None:
    refused(session, cfg(model="--dangerous"))
    refused(session, cfg(skill="-x"))


def test_mode_is_headless_or_interactive() -> None:
    assert session(mode="interactive").mode == "interactive"
    with pytest.raises(ValueError):
        session(mode="x")


def test_custom_profile_file_replaces_the_bundled_one(tmp_path: Path) -> None:
    d = tmp_path / "p"
    d.mkdir(mode=0o700)
    f = d / "p.json"
    f.write_text(json.dumps({"permissions": {"allow": ["Skill"], "deny": []}}))
    f.chmod(0o600)
    s = session(cfg(permission_profile=str(f)))
    assert s.profile["permissions"]["allow"] == ["Skill"]
    assert "Bash(gh:*)" in s.profile["permissions"]["deny"]


def test_flags_reach_agent_spawn_as_documented(gateway: Gateway, fakes: Fakes) -> None:
    s = session(cfg(env_allowlist=["B", "A"], model="m1", agent={"timeout": "30m"}))
    fakes.script("agent-spawn", "resume", json={}, schema=False)
    gateway.agent_spawn.resume_fork(
        "ag-aaaaaaaa", cwd="/work/tw", labels=[], actor="auto:review-respond",
        **s.agent_spawn_kwargs(
            plugin_dir="/plugin", prompt_file="/state/p.md", permission_profile="/state/p.json"
        ),
    )  # fmt: skip
    argv = fakes.calls("agent-spawn")[0].argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    i = argv.index("--env-allowlist-only")
    assert argv[i : i + 4] == ["--env-allowlist-only", "A", "--env-allowlist-only", "B"]
    assert argv[argv.index("--sandbox") + 1] == "required"
    assert argv[argv.index("--model") + 1] == "m1" and argv[argv.index("--timeout") + 1] == "30m"


def test_empty_allowlist_is_one_empty_flag(gateway: Gateway, fakes: Fakes) -> None:
    s = session()
    fakes.script("agent-spawn", "resume", json={}, schema=False)
    gateway.agent_spawn.resume_fork(
        "ag-aaaaaaaa", cwd="/work/tw", labels=[], actor="auto:review-respond",
        **s.agent_spawn_kwargs(
            plugin_dir="/p", prompt_file="/s/p.md", permission_profile="/s/p.json"
        ),
    )  # fmt: skip
    argv = fakes.calls("agent-spawn")[0].argv
    assert argv[argv.index("--env-allowlist-only") + 1] == ""


# ---- the profile file ----------------------------------------------------------------------


def state_prof(state: StateDir) -> dict[str, Any]:
    """A profile whose state directory is the opened one (what the writer requires)."""
    return prof(state_dir=os.fspath(state.path))


def put(state: StateDir, round_id: str, doc: dict[str, Any]) -> Path:
    return write_profile_file(state, round_id, doc, config_dir=CFG)


def test_profile_file_is_private(tmp_path: Path) -> None:
    state = open_state_dir(tmp_path / "state", create=True)
    doc = state_prof(state)
    path = put(state, "rd-abcdefgh", doc)
    assert path == state.path / "profiles" / "rd-abcdefgh.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert path.read_bytes() == render_profile(doc)
    with pytest.raises(FileExistsError):
        put(state, "rd-abcdefgh", doc)
    assert remove_profile_file(state, "rd-abcdefgh") is True
    assert remove_profile_file(state, "rd-abcdefgh") is False


def test_profile_file_never_follows_a_planted_symlink(tmp_path: Path) -> None:
    state = open_state_dir(tmp_path / "state", create=True)
    doc = state_prof(state)
    put(state, "rd-aaaaaaaa", doc)
    target = tmp_path / "victim"
    target.write_text("keep")
    (state.path / "profiles" / "rd-bbbbbbbb.json").symlink_to(target)
    with pytest.raises(OSError):
        put(state, "rd-bbbbbbbb", doc)
    assert target.read_text() == "keep"


@pytest.mark.parametrize(
    "rid", ["", "../x", "rd-ABCDEFGH", "rd-abc", "rd-abcdefgh/x", "x" * 300, "rd-abcdefg\n"]
)
def test_profile_file_refuses_bad_round_ids(tmp_path: Path, rid: str) -> None:
    state = open_state_dir(tmp_path / "state", create=True)
    with pytest.raises(ValueError):
        put(state, rid, state_prof(state))
    with pytest.raises(ValueError):
        remove_profile_file(state, rid)


def test_an_invalid_document_is_never_written(tmp_path: Path) -> None:
    state = open_state_dir(tmp_path / "state", create=True)
    bad = state_prof(state)
    bad["hooks"] = {}
    with pytest.raises(CliError):
        put(state, "rd-abcdefgh", bad)
    assert not (state.path / "profiles" / "rd-abcdefgh.json").exists()


# ---- the throwaway root --------------------------------------------------------------------


def test_throwaway_root_returns_the_realpath(tmp_path: Path) -> None:
    d = tmp_path / "tw"
    d.mkdir(mode=0o700)
    assert check_throwaway_root(str(d), config_dir=CFG, state_dir=STATE) == os.path.realpath(d)


def test_throwaway_root_refuses_unsafe_paths(tmp_path: Path) -> None:
    d = tmp_path / "tw"
    d.mkdir(mode=0o700)
    (tmp_path / "link").symlink_to(d)
    (d / "sub").mkdir()
    (d / "f").write_text("x")
    star = tmp_path / "a*b"
    star.mkdir()
    nl = tmp_path / "a\nb"
    nl.mkdir()
    bad = [
        "tw", "", "/", str(d) + "/", str(d) + "/../tw", str(d) + "/./sub", str(tmp_path / "link"),
        str(tmp_path / "link" / "sub"), str(d / "f"), str(tmp_path / "missing"), str(star), str(nl),
        str(d) + "\x00", "//" + str(d)[1:], CFG, STATE, STATE + "/x", "/home/u",
    ]  # fmt: skip
    for path in bad:
        refused(check_throwaway_root, path, config_dir=CFG, state_dir=STATE)


def test_throwaway_inside_the_state_dir_is_refused(tmp_path: Path) -> None:
    state = tmp_path / "state"
    inner = state / "tw"
    inner.mkdir(parents=True)
    refused(check_throwaway_root, str(inner), config_dir=CFG, state_dir=str(state))
    refused(check_throwaway_root, str(tmp_path), config_dir=CFG, state_dir=str(state))


# ---- nothing in the throwaway is read ------------------------------------------------------


def test_planted_config_in_the_throwaway_is_never_read(tmp_path: Path, monkeypatch: Any) -> None:
    tw = tmp_path / "tw"
    (tw / ".claude").mkdir(parents=True)
    tw.chmod(0o700)
    (tw / ".claude" / "settings.json").write_text('{"hooks": {"PreToolUse": [{"x": 1}]}}')
    (tw / ".claude" / "settings.local.json").write_text("{}")
    seen: list[str] = []
    real_open = os.open

    def spy(path: Any, *a: Any, **k: Any) -> int:
        seen.append(os.fspath(path))
        return real_open(path, *a, **k)

    monkeypatch.setattr(os, "open", spy)
    real_builtin = builtins.open

    def spy_open(file: Any, *a: Any, **k: Any) -> Any:
        seen.append(os.fspath(file))
        return real_builtin(file, *a, **k)

    monkeypatch.setattr(builtins, "open", spy_open)
    root = check_throwaway_root(str(tw), config_dir=CFG, state_dir=STATE)
    s = session()
    assert s.setting_sources == ()
    assert "hooks" not in s.profile
    assert not [p for p in seen if p.startswith(root + "/")]
    assert b"PreToolUse" not in render_profile(s.profile)
