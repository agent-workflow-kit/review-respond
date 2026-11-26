"""``validate``: offline checks of config, state, dependencies and credentials.

Never prompts, never sends anything, never runs a ``token_command`` and never prints a credential
or the value of a token variable. It does not open the state: the ``state`` check looks at the
directory read-only and creates nothing.

TODO(#38): the `sandbox` probe (``claude-session kinds --kind respond --json``) and the
`cron_path` check (skipped when no managed block is installed) are added to ``run_checks`` by #38;
the schema already names both.
"""

from __future__ import annotations

import argparse
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from .config import Config, ConfigError, ConfigWarning, load_config
from .deps import ALWAYS_REQUIRED, DEPENDENCIES, DepStatus, probe
from .errors import EXIT_FAILED, EXIT_OK, CliError
from .gateway import Gateway
from .output import Context, Outcome, sanitize_line
from .store import open_state_dir, state_dir_path

# What a child program may see of this process's environment (the gateway builds nothing else).
_CHILD_ENV: Final = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ")


def child_env(environ: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in environ.items() if k in _CHILD_ENV or k.startswith("XDG_")}


@dataclass
class _Check:
    name: str
    ok: bool = True
    code: str | None = None
    skipped: bool = False
    lines: list[str] = field(default_factory=lambda: [])

    def fail(self, code: str, line: str) -> None:
        self.ok = False
        self.code = self.code or code
        self.lines.append(line)

    def document(self) -> dict[str, Any]:
        doc: dict[str, Any] = {"name": self.name, "ok": self.ok, "code": self.code}
        if self.skipped:
            doc["skipped"] = True
        return doc


def _check_config(path: Path | None) -> tuple[_Check, Config | None, tuple[ConfigWarning, ...]]:
    check = _Check("config")
    try:
        loaded = load_config(path)
    except ConfigError as exc:
        check.fail("config_invalid", str(exc))
        return check, None, ()
    return check, loaded.config, loaded.warnings


def _check_state() -> _Check:
    """The state directory, read-only: a missing one is fine (the first command creates it)."""
    check = _Check("state")
    path = state_dir_path()
    if not os.path.lexists(path):
        return check
    try:
        open_state_dir(path, create=False).close()
    except CliError as exc:
        check.fail(exc.code, exc.message)
    return check


def _check_dependencies(gateway: Gateway, config: Config | None) -> tuple[_Check, list[DepStatus]]:
    check = _Check("dependencies")
    # claude-session serves the sandbox probe: needed unless the sandbox is not required. With no
    # readable config the default (`required`) applies.
    sandbox_required = config is None or config.sandbox == "required"
    statuses: list[DepStatus] = []
    for dep in DEPENDENCIES:
        required = dep.tool in ALWAYS_REQUIRED or (
            dep.tool == "claude-session" and sandbox_required
        )
        status = probe(gateway, dep, required=required)
        statuses.append(status)
        if not status.ok and status.code is not None:
            # a missing program outranks one that is too old
            if check.code is None or status.code == "tool_missing":
                check.code = status.code
            check.ok = False
            check.lines.append(status.message)
    return check, statuses


def _check_credentials(config: Config | None, environ: Mapping[str, str]) -> _Check:
    """``token_env`` is set and non-empty; ``token_command``'s first element resolves."""
    check = _Check("credentials")
    if config is None:
        check.skipped = True
        return check
    path = environ.get("PATH")
    for host, credential in config.hosts.items():
        if credential.token_env is not None and not environ.get(credential.token_env):
            check.fail("token_env_unset", f"{host}: {credential.token_env} is not set or is empty")
        if credential.token_command is not None:
            program = credential.token_command[0]
            if shutil.which(program, path=path) is None:
                check.fail(
                    "token_command_unresolvable",
                    f"{host}: token_command {program} does not resolve to an executable",
                )
    return check


def run_checks(
    ctx: Context, gateway: Gateway, environ: Mapping[str, str]
) -> tuple[list[_Check], list[DepStatus], list[str]]:
    config_check, config, config_warnings = _check_config(ctx.config)
    state_check = _check_state()
    dep_check, statuses = _check_dependencies(gateway, config)
    credentials = _check_credentials(config, environ)
    warnings = [w.code for w in config_warnings]
    warnings += [f"{s.tool.replace('-', '_')}_version_unreadable" for s in statuses if s.unreadable]
    unique = list(dict.fromkeys(warnings))
    return [config_check, state_check, dep_check, credentials], statuses, unique


def _human(checks: list[_Check], statuses: list[DepStatus], warnings: list[str]) -> str:
    out: list[str] = []
    for check in checks:
        if check.name == "dependencies":
            state = "ok" if check.ok else f"FAIL {check.code}"
            out.append(f"{check.name}: {state}")
            for s in statuses:
                found = s.found if s.found is not None else "not found"
                note = "" if s.ok else f" ({s.code})"
                out.append(f"  {s.tool} {found} (minimum {s.minimum}){note}")
            out.extend(f"  {line}" for line in check.lines)
            continue
        if check.skipped:
            out.append(f"{check.name}: skipped")
        elif check.ok:
            out.append(f"{check.name}: ok")
        else:
            out.append(f"{check.name}: FAIL {check.code}")
            out.extend(f"  {line}" for line in check.lines)
    out.extend(f"warning: {code}" for code in warnings)
    return "".join(sanitize_line(line) + "\n" for line in out)


def handle(ctx: Context, _args: argparse.Namespace) -> Outcome:
    """Run every check; exit 12 with the document when any fails, 0 otherwise."""
    environ = dict(os.environ)
    gateway = Gateway(child_env(environ))
    checks, statuses, warnings = run_checks(ctx, gateway, environ)
    ok = all(check.ok for check in checks)
    payload: dict[str, Any] = {
        "ok": ok,
        "dependencies": [
            {
                "tool": s.tool,
                "required": s.required,
                "found": s.found,
                "minimum": s.minimum,
                "ok": s.ok,
            }
            for s in statuses
        ],
        "checks": [check.document() for check in checks],
        "warnings": warnings,
    }
    return Outcome(payload, _human(checks, statuses, warnings), EXIT_OK if ok else EXIT_FAILED)
