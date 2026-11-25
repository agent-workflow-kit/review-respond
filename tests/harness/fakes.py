"""Per-test installation of the sibling fakes, scripting of their answers and call inspection."""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import schemas

ROOT = Path(__file__).resolve().parent.parent.parent
FAKEBIN = ROOT / "tests" / "fakebin"
PINNED = ROOT / "tests" / "pinned"
TOOLS = schemas.TOOLS


@dataclass(frozen=True)
class Call:
    tool: str
    argv: list[str]
    cwd: str
    env: dict[str, str]
    stdin: str | None
    stdin_tty: bool
    outcome: str
    exit: int
    command: str | None
    flags: dict[str, Any]
    positionals: dict[str, Any]
    tail: list[str]

    @property
    def tokens(self) -> list[str]:
        return self.argv


class Fakes:
    """The fake siblings of one test. ``bin_dir`` goes on the PATH of the code under test."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin_dir = root / "bin"
        self.state_dir = root / "state"
        self.install()

    def install(self) -> None:
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FAKEBIN / "fakecore.py", self.bin_dir / "fakecore.py")
        (self.bin_dir / "fakebin.state").write_text(str(self.state_dir) + "\n")
        for tool in TOOLS:
            shutil.copyfile(PINNED / tool / "grammar.json", self.bin_dir / f"{tool}.grammar.json")
            lines = (FAKEBIN / tool).read_text().splitlines(keepends=True)
            target = self.bin_dir / tool
            target.write_text(f"#!{sys.executable}\n" + "".join(lines[1:]))
            target.chmod(0o755)
        if not (self.state_dir / "script.json").exists():
            self._save({"entries": []})

    # -- scripting -------------------------------------------------------------------------

    def script(
        self,
        tool: str,
        command: str,
        *,
        json: Any = None,
        stdout: str = "",
        stderr: str = "",
        exit: int = 0,
        schema: str | bool | None = None,
        match: dict[str, Any] | None = None,
        times: int | None = 1,
        sleep: float = 0.0,
    ) -> int:
        """Script one answer. ``json`` is validated against the pinned schema first.

        ``schema`` defaults to ``<tool>/cli/<command>.json`` for exit 0 and
        ``<tool>/cli/error.json`` otherwise; pass a schema id for commands whose non-zero exit
        prints a result (agent-spawn ``wait``, pr-comment ``resolve``), or ``False`` to skip
        validation explicitly.
        """
        assert tool in TOOLS, tool
        result: dict[str, Any] = {"exit": exit, "stderr": stderr, "sleep": sleep}
        if json is not None:
            if schema is not False:
                sid = (
                    schema if isinstance(schema, str) else self._default_schema(tool, command, exit)
                )
                schemas.validate(sid, json)
            result["stdout_json"] = json
        else:
            result["stdout"] = stdout
        doc = self._load()
        entry = {
            "id": len(doc["entries"]) + 1,
            "tool": tool,
            "command": command,
            "match": match or {},
            "result": result,
            "remaining": times,
        }
        doc["entries"].append(entry)
        self._save(doc)
        return int(entry["id"])

    @staticmethod
    def _default_schema(tool: str, command: str, exit: int) -> str:
        return schemas.output_schema_id(tool, command, exit)

    def _load(self) -> dict[str, Any]:
        return json.loads((self.state_dir / "script.json").read_text())

    def _save(self, doc: dict[str, Any]) -> None:
        (self.state_dir / "script.json").write_text(json.dumps(doc))

    def remaining(self) -> list[dict[str, Any]]:
        return [e for e in self._load()["entries"] if e["remaining"]]

    # -- inspection ------------------------------------------------------------------------

    def _read(self, name: str) -> list[dict[str, Any]]:
        path = self.state_dir / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    def calls(self, tool: str | None = None, command: str | None = None) -> list[Call]:
        out = []
        for rec in self._read("calls.jsonl"):
            parsed = rec.get("parsed") or {}
            call = Call(
                tool=rec["tool"],
                argv=rec["argv"],
                cwd=rec["cwd"],
                env=rec["env"],
                stdin=rec["stdin"],
                stdin_tty=rec["stdin_tty"],
                outcome=rec["outcome"],
                exit=rec["exit"],
                command=parsed.get("command"),
                flags=parsed.get("flags", {}),
                positionals=parsed.get("positionals", {}),
                tail=parsed.get("tail", []),
            )
            if (tool is None or call.tool == tool) and (command is None or call.command == command):
                out.append(call)
        return out

    def violations(self) -> list[dict[str, Any]]:
        return self._read("violations.jsonl")

    def take_violations(self) -> list[dict[str, Any]]:
        """Return and forget the violations; a test that provokes them on purpose calls this."""
        found = self.violations()
        path = self.state_dir / "violations.jsonl"
        if path.exists():
            path.write_text("")
        return found

    def assert_clean(self) -> None:
        found = self.violations()
        if found:
            lines = [
                f"{v['tool']} {v['kind']}: {' '.join(v['argv'])} ({v.get('message', '')})"
                for v in found
            ]
            raise AssertionError(
                "a fake refused a call the real sibling would refuse:\n" + "\n".join(lines)
            )


def assert_untrusted_only_in_files_or_stdin(calls: list[Call], texts: list[str]) -> None:
    """Fail when any hostile text shows up in a recorded argv or environment value."""
    for call in calls:
        for text in texts:
            if not text:
                continue
            for token in call.argv:
                assert text not in token, f"untrusted text in argv of {call.tool}: {token[:60]!r}"
            for key, value in call.env.items():
                assert text not in value, f"untrusted text in env {key} of {call.tool}"


def private_env(root: Path, fakes: Fakes) -> dict[str, str]:
    """A CI-like child environment: private HOME and XDG, PATH with the fakes first."""
    home = root / "home"
    for name in ("home", "xdg/config", "xdg/state", "xdg/cache", "xdg/data"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return {
        "PATH": f"{fakes.bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(root / "xdg" / "config"),
        "XDG_STATE_HOME": str(root / "xdg" / "state"),
        "XDG_CACHE_HOME": str(root / "xdg" / "cache"),
        "XDG_DATA_HOME": str(root / "xdg" / "data"),
        "LC_ALL": "C",
        "LANG": "C",
        "TZ": "UTC",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
