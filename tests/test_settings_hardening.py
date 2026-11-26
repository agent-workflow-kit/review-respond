"""Round 1 review fixes of the session settings (issue #21): the hostile inputs each one closes."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from review_respond.config import Config
from review_respond.errors import CliError
from review_respond.settings import (
    build_session_settings,
    check_allow_rule,
    check_throwaway_root,
    remove_profile_file,
    write_profile_file,
)
from review_respond.store import StateDir, open_state_dir

from .settings_support import CFG, HOME, REPO, STATE, home_env, prof, refused

__all__ = ["home_env"]

pytestmark = pytest.mark.usefixtures("home_env")


def base(*allow: str, reads: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "permissions": {"allow": list(allow), "deny": []},
        "sandbox": {"filesystem": {"allowRead": list(reads)}},
    }


# ---- R1: replacement-profile allow rules get the read_dirs checks --------------------------


@pytest.mark.parametrize(
    "rule",
    [
        "Read(////**)",
        "Read(//**)",
        "Read(~/**)",
        "Read(//home/u/.ssh/**)",
        "Read(~/.ssh/**)",
        "Read(~/.SSH/**)",
        "Read(//proc/**)",
        "Read(//proc/1/environ/**)",
        "Read(//etc/shadow/**)",
        "Read(//etc/**)",
        "Read(//opt/./x/**)",
        "Read(//opt//x/**)",
        "Read(//opt/../x/**)",
        "Read(~/./x/**)",
        "Read(~/x//y/**)",
        "Read(//home/u/**)",
        "Read(//home/**)",
        f"Read(/{CFG}/**)",
        f"Read(/{STATE}/x/**)",
        "Read(//opt/a(b)/**)",
        "Read(//opt/a*/**)",
        "Edit(/${CWD}/.git/**)",
        "Write(/${CWD}/.claude/**)",
        "Edit(/${CWD}/a/.GIT/**)",
        "Write(/${CWD}/a/.Claude/b/**)",
        "Edit(/${CWD}//x/**)",
        "Edit(/${CWD}/./x/**)",
        "Edit(/${CWD}/a/../x/**)",
        "Edit(/${CWD}/a(b)/**)",
        "Read(/${CWD}/../x/**)",
        "Skill(a..b)",
        "Skill(-x)",
        "Bash(git status(x):*)",
    ],
)
def test_replacement_allow_rules_are_checked_like_read_dirs(rule: str) -> None:
    refused(prof, base=base(rule))


@pytest.mark.parametrize(
    "rule",
    [
        "Read(//opt/lib/**)",
        "Read(~/.cache/tools/**)",
        "Read(/${CWD}/.git/**)",
        "Read(/${CWD}/src/**)",
        "Edit(/${CWD}/src/**)",
        "Write(/${CWD}/docs/a.b/**)",
        "Skill(plugin:name)",
    ],
)
def test_ordinary_replacement_allow_rules_pass(rule: str) -> None:
    assert rule in prof(base=base(rule))["permissions"]["allow"]


def test_the_bundled_allow_rules_pass_the_check() -> None:
    for rule in prof()["permissions"]["allow"]:
        check_allow_rule(rule, protected=(CFG, STATE))


# ---- R3: symlinked config and state directories --------------------------------------------


def test_symlinked_state_dir_is_denied_under_both_names(tmp_path: Path) -> None:
    real = tmp_path / "real-state"
    real.mkdir()
    link = tmp_path / "link-state"
    link.symlink_to(real)
    doc = prof(state_dir=str(link))
    fs = doc["sandbox"]["filesystem"]
    for name in (str(link), str(real)):
        assert name in fs["denyRead"] and name in fs["denyWrite"]
        for tool in ("Read", "Edit", "Write"):
            assert f"{tool}(/{name}/**)" in doc["permissions"]["deny"]


def test_symlinked_config_dir_is_denied_under_both_names(tmp_path: Path) -> None:
    real = tmp_path / "real-cfg"
    real.mkdir()
    link = tmp_path / "link-cfg"
    link.symlink_to(real)
    doc = prof(config_dir=str(link))
    assert str(real) in doc["sandbox"]["filesystem"]["denyRead"]
    assert f"Read(/{real}/**)" in doc["permissions"]["deny"]


def test_read_dirs_are_checked_against_the_resolved_state_dir(tmp_path: Path) -> None:
    real = tmp_path / "real-state"
    (real / "sub").mkdir(parents=True)
    link = tmp_path / "link-state"
    link.symlink_to(real)
    refused(prof, read_dirs=(str(real / "sub"),), state_dir=str(link))
    refused(prof, read_dirs=(str(tmp_path),), state_dir=str(link))


def test_throwaway_is_checked_against_the_resolved_state_dir(tmp_path: Path) -> None:
    real = tmp_path / "real-state"
    (real / "tw").mkdir(parents=True, mode=0o700)
    link = tmp_path / "link-state"
    link.symlink_to(real)
    refused(check_throwaway_root, str(real / "tw"), config_dir=CFG, state_dir=str(link))


# ---- R5: the program, interpreter and launcher checks --------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        # interpreters: a script path or exactly -m <module>
        ("python", "-"),
        ("python", "-I"),
        ("python3", "-Ic", "x"),
        ("python3.11", "-W", "error"),
        ("python", "-m"),
        ("python", "-m", "-c"),
        ("python", "-m", "timeit"),
        ("python", "-m", "http.server"),
        ("python", "-m", "pip"),
        ("python", "-m", "pip._internal"),
        ("python", "-m", "pdb"),
        ("python", "-m", "runpy"),
        ("python", "-m", "code"),
        ("python", "-m", "CProfile"),
        ("python", "-m", "pytest", "-c"),
        ("python", "-m", "pytest", "-xc"),
        ("python", "-m", "pytest", "--eval=x"),
        ("python", "script.py", "-e"),
        ("node", "-"),
        ("node", "--require", "x"),
        ("node", "--eval", "1"),
        ("node", "-p", "1"),
        ("node", "-r", "x"),
        ("perl", "-E", "say 1"),
        ("perl", "-x"),
        ("ruby", "-e", "1"),
        ("php", "-r", "1"),
        ("deno", "eval", "-e"),
        ("lua", "-"),
        ("pwsh", "-Command", "x"),
        ("Rscript", "-e", "1"),
        ("tclsh",),
        ("java", "-jar", "x.jar"),
        ("/usr/bin/python3",),
        # shells and other launchers
        ("zsh", "x"),
        ("fish", "x"),
        ("dash",),
        ("mksh",),
        ("/bin/bash",),
        ("BASH",),
        ("Sh",),
        ("busybox", "sh"),
        ("gawk", "x"),
        ("mawk", "x"),
        ("awk", "x"),
        ("sed", "x"),
        ("tar", "x"),
        ("vim", "x"),
        ("vi",),
        ("ed",),
        ("less", "x"),
        ("man", "x"),
        ("expect", "x"),
        ("script", "x"),
        ("strace", "x"),
        ("gdb", "x"),
        ("tmux", "x"),
        ("screen", "x"),
        ("parallel", "x"),
        ("flock", "x"),
        ("stdbuf", "x"),
        ("taskset", "x"),
        ("unshare", "x"),
        ("nsenter", "x"),
        ("chroot", "x"),
        ("systemd-run", "x"),
        ("pkexec", "x"),
        ("kill", "x"),
        ("mount", "x"),
        ("rm", "x"),
        ("dd", "x"),
        ("tee", "x"),
        ("cp", "x"),
        ("mv", "x"),
        ("zip", "x"),
        ("docker", "run"),
        ("podman", "run"),
        ("ping", "x"),
        ("dig", "x"),
        ("nslookup", "x"),
        ("openssl", "x"),
        ("lynx", "x"),
        ("w3m", "x"),
        ("aria2c", "x"),
        ("ssh-add",),
        ("git-shell",),
        ("/usr/lib/git-core/git-daemon",),
        ("tool.exe",),
        ("Curl", "x"),
        ("/usr/bin/GH", "x"),
        # package runners and make
        ("npm", "run", "x"),
        ("npm", "exec", "x"),
        ("npm", "install"),
        ("pnpm", "dlx", "x"),
        ("yarn", "run", "x"),
        ("uv", "run", "pytest"),
        ("uv", "tool", "run", "x"),
        ("poetry", "run", "x"),
        ("cargo", "run"),
        ("go", "run", "x"),
        ("NPM", "Run", "x"),
        ("make", "-f", "evil"),
        ("make", "-C", "dir"),
        ("make", "-Cdir"),
        ("make", "-fevil"),
        ("make", "CC=evil"),
        ("make", "SHELL=/bin/sh", "test"),
        ("FOO=bar", "make"),
        ("make", "--eval=x"),
        ("make", "--exec=x"),
        ("make", "--command=x"),
        ("make", "--print-x"),
    ],
)
def test_weak_program_patterns_are_refused(argv: tuple[str, ...]) -> None:
    refused(prof, allow_commands=(argv,))


@pytest.mark.parametrize(
    "argv",
    [
        ("make", "test"),
        ("make", "-j4", "test"),
        ("python", "-m", "pytest"),
        ("python3", "-m", "unittest"),
        ("python", "tools/check.py"),
        ("python", "tools/check.py", "--fast"),
        ("node", "scripts/lint.js"),
        ("cargo", "test"),
        ("go", "test"),
        ("npm", "test"),
        ("pytest",),
        ("ruff", "check", "src"),
    ],
)
def test_ordinary_test_commands_are_allowed(argv: tuple[str, ...]) -> None:
    doc = prof(allow_commands=(argv,))
    assert f"Bash({' '.join(argv)}:*)" in doc["permissions"]["allow"]


# ---- R6: read-only git subcommands ---------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        "--output=x", "--output", "--ext-diff", "--textconv", "--contents", "--no-index",
        "-Ox", "-O", "--exec-path=x", "--upload-pack=x", "--show-signature", "-c", "-C",
    ],
)  # fmt: skip
@pytest.mark.parametrize("sub", ["diff", "log", "show", "blame", "status"])
def test_git_options_that_write_or_run_programs_are_refused(sub: str, extra: str) -> None:
    refused(prof, allow_commands=(("git", sub, extra),))


@pytest.mark.parametrize("sub", ["status", "diff", "log", "show", "ls-files", "blame", "rev-parse"])
def test_read_only_git_subcommands_are_allowed(sub: str) -> None:
    assert f"Bash(git {sub}:*)" in prof(allow_commands=(("git", sub),))["permissions"]["allow"]


@pytest.mark.parametrize("sub", ["push", "commit", "checkout", "fetch", "config", "-c", "branch"])
def test_other_git_subcommands_are_refused(sub: str) -> None:
    refused(prof, allow_commands=(("git", sub),))


# ---- R8: read_dirs ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "~/.SSH",
        "~/.Aws/x",
        "/home/u/.NPMRC",
        "~/.pypirc",
        "~/.azure",
        "~/.cargo/credentials",
        "~/.cargo/credentials.toml",
        "~/.cargo",
        "/opt/.cargo/Credentials.toml",
        "~/.local",
        "~/.local/state",
        "~/.local/share",
        "~/.local/share/keyrings",
        "~/.local/share/keyrings/x",
        "/x/.local/share/keyrings",
        "/proc",
        "/proc/1",
        "/sys",
        "/sys/kernel",
        "/dev",
        "/dev/shm",
        "/etc",
        "/etc/shadow",
        "/etc/ssh",
        "/home",
        HOME,
    ],
)
def test_credential_and_system_read_dirs_are_refused(path: str) -> None:
    refused(prof, read_dirs=(path,))


def test_a_symlink_to_a_credential_directory_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "h"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    link = tmp_path / "innocent"
    link.symlink_to(home / ".ssh")
    refused(prof, read_dirs=(str(link),))
    refused(prof, read_dirs=("~/.ssh",))


def test_tilde_is_expanded_with_the_real_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_home = tmp_path / "h"
    (real_home / "proj" / "state").mkdir(parents=True)
    link_home = tmp_path / "hl"
    link_home.symlink_to(real_home)
    monkeypatch.setenv("HOME", str(link_home))
    state = str(real_home / "proj" / "state")
    refused(prof, read_dirs=("~/proj",), state_dir=state)
    refused(prof, read_dirs=("~/proj/state",), state_dir=state)
    refused(prof, read_dirs=("~",), state_dir=state)
    assert (
        "~/other"
        in prof(read_dirs=("~/other",), state_dir=state)["sandbox"]["filesystem"]["allowRead"]
    )


def test_read_dirs_may_not_overlap_the_throwaway() -> None:
    refused(prof, read_dirs=("/opt/x",), throwaway="/opt/x/tw")
    refused(prof, read_dirs=("/opt/x/tw/sub",), throwaway="/opt/x/tw")
    refused(prof, read_dirs=("/opt",), throwaway="/opt/x/tw")
    refused(prof, base=base("Read(//opt/x/**)"), throwaway="/opt/x/tw")
    assert (
        "/opt/y"
        in prof(read_dirs=("/opt/y",), throwaway="/opt/x/tw")["sandbox"]["filesystem"]["allowRead"]
    )


def test_session_settings_pass_the_throwaway_to_the_read_dir_check() -> None:
    config = Config.model_validate({"repos": {REPO: {"respond": {"read_dirs": ["/opt/x"]}}}})
    refused(
        build_session_settings,
        config,
        REPO,
        config_dir=CFG,
        state_dir=STATE,
        throwaway="/opt/x/tw",
    )


# ---- R9: an empty allow list ---------------------------------------------------------------


@pytest.mark.parametrize("doc", [{}, {"permissions": {}}, {"permissions": {"allow": []}}])
def test_a_profile_with_no_allow_rule_is_refused_by_name(doc: dict[str, Any]) -> None:
    err = refused(prof, base=doc)
    assert "permission_profile" in err.message and "no allow rule" in err.message


# ---- R10: parentheses ----------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/opt/a(b)", "/opt/(a", "/opt/a)", "~/a(b"])
def test_parentheses_in_read_dirs_are_refused(path: str) -> None:
    refused(prof, read_dirs=(path,))


@pytest.mark.parametrize("key", ["config_dir", "state_dir"])
def test_parentheses_in_own_dirs_are_refused_early(key: str) -> None:
    refused(prof, **{key: "/home/u/dir(1)"})


def test_parentheses_in_a_throwaway_are_refused(tmp_path: Path) -> None:
    d = tmp_path / "tw(1)"
    d.mkdir(mode=0o700)
    refused(check_throwaway_root, str(d), config_dir=CFG, state_dir=STATE)


# ---- R12: environment names ----------------------------------------------------------------


def names(*value: str) -> Config:
    return Config.model_validate({"env_allowlist": list(value)})


@pytest.mark.parametrize(
    "name",
    [
        "MY_KEY", "SIGNING_KEY", "GPG_PASSPHRASE", "DB_PASS", "SESSION_COOKIE", "X_BEARER",
        "MY_JWT", "SENTRY_DSN", "TLS_CERT", "CALLBACK_URL", "callback_url",
        "SHELL", "PAGER", "EDITOR", "VISUAL", "TMPDIR", "LESSOPEN", "PROMPT_COMMAND", "CDPATH",
        "BASH_FUNC_x", "BASH_ENV", "ENV", "GLIBC_TUNABLES", "GCONV_PATH",
        "NPM_CONFIG_REGISTRY", "PIP_INDEX_URL", "UV_INDEX", "JAVA_HOME",
        "JAVA_OPTS", "JDK_JAVA_OPTIONS",
        "_JAVA_AWT_WM_NONREPARENTING", "MAKEFLAGS", "MAKEFILES", "GOFLAGS", "PERLLIB",
        "RUBYGEMS_X", "NODE_ENV", "SSL_CERT_FILE", "HTTP_PROXY", "https_proxy", "NO_PROXY",
        "ALL_PROXY", "SSH_AGENT_PID", "SUDO_USER", "KUBECONFIG", "DOCKER_HOST", "AWS_PROFILE",
        "GOOGLE_APPLICATION_X", "AZURE_X", "path", "home", "Ld_Preload", "pythonpath",
    ],
)  # fmt: skip
def test_widened_env_denylist(name: str) -> None:
    refused(build_session_settings, names(name), REPO, config_dir=CFG, state_dir=STATE)


@pytest.mark.parametrize("name", ["LANG", "LC_ALL", "TERM", "TZ", "CI", "COLORTERM", "NO_COLOR"])
def test_plain_env_names_are_allowed(name: str) -> None:
    s = build_session_settings(names(name), REPO, config_dir=CFG, state_dir=STATE)
    assert s.env_allowlist == (name,)


def test_a_token_variable_matches_in_any_case() -> None:
    config = Config.model_validate(
        {
            "hosts": {"git.example.test": {"token_env": "FORGE_ACCESS"}},
            "env_allowlist": ["forge_access"],
        }
    )
    refused(build_session_settings, config, REPO, config_dir=CFG, state_dir=STATE)


# ---- R13: the throwaway root ---------------------------------------------------------------


def throwaway(tmp_path: Path, mode: int = 0o700) -> Path:
    d = tmp_path / "tw"
    d.mkdir()
    d.chmod(mode)
    return d


@pytest.mark.parametrize("mode", [0o770, 0o707, 0o777, 0o720, 0o702])
def test_a_group_or_other_writable_throwaway_is_refused(tmp_path: Path, mode: int) -> None:
    d = throwaway(tmp_path, mode)
    refused(check_throwaway_root, str(d), config_dir=CFG, state_dir=STATE)


@pytest.mark.parametrize("mode", [0o700, 0o750, 0o755])
def test_a_private_or_read_only_shared_throwaway_is_accepted(tmp_path: Path, mode: int) -> None:
    d = throwaway(tmp_path, mode)
    assert check_throwaway_root(str(d), config_dir=CFG, state_dir=STATE) == os.path.realpath(d)


def test_a_throwaway_owned_by_someone_else_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = throwaway(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(d).st_uid + 1)
    refused(check_throwaway_root, str(d), config_dir=CFG, state_dir=STATE)


def test_home_and_its_parents_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "h"
    (home / "tw").mkdir(parents=True, mode=0o700)
    home.chmod(0o700)
    monkeypatch.setenv("HOME", str(home))
    refused(check_throwaway_root, str(home), config_dir=CFG, state_dir=STATE)
    refused(check_throwaway_root, str(tmp_path), config_dir=CFG, state_dir=STATE)
    # below the home is fine: a throwaway usually is
    assert check_throwaway_root(str(home / "tw"), config_dir=CFG, state_dir=STATE)


@pytest.mark.parametrize(
    "path", ["/etc", "/usr", "/var", "/proc", "/sys", "/dev", "/tmp", "/home", "/root"]
)
def test_system_directories_are_never_a_throwaway(path: str) -> None:
    refused(check_throwaway_root, path, config_dir=CFG, state_dir=STATE)


def test_a_throwaway_need_not_be_under_the_state_dir(tmp_path: Path) -> None:
    d = throwaway(tmp_path)
    assert check_throwaway_root(str(d), config_dir=CFG, state_dir=STATE)


# ---- R14: skill, model and timeout ---------------------------------------------------------


def with_agent(**kw: Any) -> Config:
    config = Config.model_validate({})
    for key, value in kw.items():
        object.__setattr__(config.agent if key == "timeout" else config, key, value)
    return config


@pytest.mark.parametrize(
    "skill", ["a..b", "..", "-x", "a/b", "a b", "", "a" * 129, "a\nb", ".a", "a@b"]
)
def test_bad_skill_names_are_refused(skill: str) -> None:
    refused(build_session_settings, with_agent(skill=skill), REPO, config_dir=CFG, state_dir=STATE)


@pytest.mark.parametrize("skill", ["respond-to-review", "plugin:skill", "a.b_c-d", "A" * 128])
def test_good_skill_names_pass(skill: str) -> None:
    s = build_session_settings(with_agent(skill=skill), REPO, config_dir=CFG, state_dir=STATE)
    assert s.skill == skill


@pytest.mark.parametrize("model", ["-x", "--model", "a\nb", "a\x1bb", "a‮b", "x" * 129])
def test_bad_models_are_refused(model: str) -> None:
    refused(build_session_settings, with_agent(model=model), REPO, config_dir=CFG, state_dir=STATE)


@pytest.mark.parametrize("model", ["claude-sonnet-4", "sonnet[1m]", "a b", "vendor/model:1", "é"])
def test_models_follow_the_pinned_grammar(model: str) -> None:
    s = build_session_settings(with_agent(model=model), REPO, config_dir=CFG, state_dir=STATE)
    assert s.model == model


@pytest.mark.parametrize(
    "timeout",
    [
        "0",
        "0s",
        "0ms",
        "0.0s",
        "0m",
        "none",
        "500ms",
        "36501d",
        "3153600001s",
        "-1s",
        "1x",
        "",
        "1 s",
        "1e3s",
    ],
)
def test_bad_timeouts_are_refused(timeout: str) -> None:
    refused(
        build_session_settings, with_agent(timeout=timeout), REPO, config_dir=CFG, state_dir=STATE
    )


@pytest.mark.parametrize(
    "timeout", ["1s", "1000ms", "90", "1.5m", "30m", "2h", "7d", "36500d", "3153600000s"]
)
def test_good_timeouts_pass(timeout: str) -> None:
    s = build_session_settings(with_agent(timeout=timeout), REPO, config_dir=CFG, state_dir=STATE)
    assert s.timeout == timeout


# ---- R15 and R16: the profile file ---------------------------------------------------------


def put(state: StateDir, doc: dict[str, Any], round_id: str = "rd-abcdefgh") -> Path:
    return write_profile_file(state, round_id, doc, config_dir=CFG)


def good(state: StateDir) -> dict[str, Any]:
    return prof(state_dir=os.fspath(state.path))


def tampered(state: StateDir) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for edit in (
        lambda d: d["permissions"]["allow"].append("Bash(gh pr:*)"),
        lambda d: d["permissions"]["allow"].append("Bash(bash x:*)"),
        lambda d: d["permissions"]["allow"].append("Bash(python -m timeit:*)"),
        lambda d: d["permissions"]["allow"].append("Read(//home/u/.ssh/**)"),
        lambda d: d["permissions"]["allow"].append("Read(////**)"),
        lambda d: d["permissions"]["allow"].append("Edit(/${CWD}/.git/**)"),
        lambda d: d["permissions"]["allow"].append("Edit"),
        lambda d: d["permissions"]["deny"].remove(f"Read(/{state.path}/**)"),
        lambda d: d["permissions"]["deny"].remove(f"Write(/{CFG}/**)"),
        lambda d: d["sandbox"]["filesystem"]["denyRead"].remove(CFG),
        lambda d: d["sandbox"]["filesystem"]["denyWrite"].remove(os.fspath(state.path)),
    ):
        doc = good(state)
        edit(doc)
        out.append(doc)
    return out


def test_a_tampered_document_is_never_written(tmp_path: Path) -> None:
    state = open_state_dir(tmp_path / "state", create=True)
    for doc in tampered(state):
        with pytest.raises(CliError) as info:
            put(state, doc)
        assert info.value.code == "config_invalid"
    assert not (state.path / "profiles" / "rd-abcdefgh.json").exists()
    assert put(state, good(state)).exists()


def test_removing_a_directory_in_the_way_is_state_unsafe(tmp_path: Path) -> None:
    state = open_state_dir(tmp_path / "state", create=True)
    put(state, good(state), "rd-aaaaaaaa")
    (state.path / "profiles" / "rd-cccccccc.json").mkdir()
    with pytest.raises(CliError) as info:
        remove_profile_file(state, "rd-cccccccc")
    assert info.value.code == "state_unsafe"


@pytest.mark.parametrize(
    "error", [IsADirectoryError(21, "Is a directory"), PermissionError(13, "denied")]
)
def test_write_and_remove_map_os_errors_to_state_unsafe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    state = open_state_dir(tmp_path / "state", create=True)
    doc = good(state)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise error

    monkeypatch.setattr(StateDir, "write_private_file", boom)
    monkeypatch.setattr(StateDir, "remove_private_file", boom)
    for call in (lambda: put(state, doc), lambda: remove_profile_file(state, "rd-abcdefgh")):
        with pytest.raises(CliError) as info:
            call()
        assert info.value.code == "state_unsafe"


# ---- R18: the rules mean what the docs say under Claude's prefix matching ------------------


def matches(rule: str, tool: str, argument: str = "") -> bool:
    """A minimal Claude permission matcher: a bare tool name matches every use of the tool;
    ``Bash(prefix:*)`` matches the command ``prefix`` and any command that starts with
    ``prefix`` and a space (never a longer word)."""
    if rule == tool:
        return True
    if tool == "Bash" and rule.startswith("Bash(") and rule.endswith(":*)"):
        prefix = rule[len("Bash(") : -len(":*)")]
        return argument == prefix or argument.startswith(prefix + " ")
    if rule.endswith("*") and "(" not in rule:  # mcp__*
        return tool.startswith(rule[:-1])
    return False


def hits(rules: list[str], command: str) -> list[str]:
    return [r for r in rules if matches(r, "Bash", command)]


@pytest.mark.parametrize(
    "command",
    [
        "git push origin x",
        "git push",
        "git remote add o u",
        "git config user.name x",
        "git credential fill",
        "gh pr view",
        "gh",
        "glab mr list",
        "memoctl add x",
        "memoctl delete x",
        "memoctl cron x",
        "memoctl prune x",
        "agent-spawn launch",
        "notice-route send x",
        "notice-route flush x",
    ],
)
def test_every_dangerous_command_matches_a_generated_deny(command: str) -> None:
    doc = prof()
    assert hits(doc["permissions"]["deny"], command), command


@pytest.mark.parametrize("command", ["git status", "git diff", "git log -p", "git show HEAD"])
def test_read_only_git_matches_an_allow_and_no_deny(command: str) -> None:
    doc = prof()
    assert hits(doc["permissions"]["allow"], command)
    assert not hits(doc["permissions"]["deny"], command)


@pytest.mark.parametrize("command", ["git pushx", "git status2", "gitx status", "ghost x"])
def test_prefix_matching_is_by_word(command: str) -> None:
    doc = prof()
    assert not hits(doc["permissions"]["deny"], command)
    assert not hits(doc["permissions"]["allow"], command)


@pytest.mark.parametrize("tool", ["WebFetch", "WebSearch", "Agent", "Task", "mcp__x__y"])
def test_denied_tools_match_a_generated_deny(tool: str) -> None:
    assert any(matches(r, tool) for r in prof()["permissions"]["deny"])
