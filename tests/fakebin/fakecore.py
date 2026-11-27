"""Engine shared by the sibling fakes in this directory.

A fake is a stand-in for one sibling program (forgectl, agent-spawn, ...). It parses its command
line with the sibling's documented grammar, taken from ``tests/pinned/<tool>/grammar.json`` (pinned
to the minimum version in the interface Dependencies table), and answers from responses a test
scripted beforehand. A call that the real sibling would refuse (an unknown flag, an argument that is
not the documented form, a missing required flag, a human-only flag without a human actor, ...)
exits 2 with the sibling's usage envelope AND is appended to ``violations.jsonl``; the test harness
fails the test at teardown when that file is not empty. A well-formed call nobody scripted is a
violation too, so a fake never invents an answer.

The fake records the environment it started with in ``calls.jsonl`` (tests assert on what the
gateway passed) but never uses it to decide an answer: the directory it runs from holds
``fakebin.state`` (the private state directory of the test) and a copy of the grammar. That keeps it
working when review-respond builds a child environment from scratch.

The grammars are deliberately STRICTER than the real tools in two ways: a flag given twice is
refused unless the grammar says the tool accepts it (``duplicate_globals``: pr-comment accepts the
same value twice, wtctl takes the last), because review-respond's wrapper never emits a duplicate;
and a few forms the real tool accepts are refused because review-respond never sends them
(``resume`` without ``--fork``, ``resume --fork --wait``, a ``--hook`` the tool does not need). Each
such rule carries a ``note`` in the grammar.

This module is standard library only and is copied per repository, never shared.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import select
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NEGATIVE_NUMBER = re.compile(r"^-[0-9]+(?:\.[0-9]+)?$")
USAGE_EXIT = 2
CONFIRMATION_EXIT = 13
DURATION = re.compile(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m|h|d)?")
UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, None: 1.0}


class Usage(Exception):
    """The real sibling would refuse this command line (usage, or confirmation_required)."""

    def __init__(self, message: str, exit_code: int = USAGE_EXIT, code: str = "usage") -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.code = code


@dataclass
class Parsed:
    command: str
    flags: dict[str, Any] = field(default_factory=dict)
    positionals: dict[str, Any] = field(default_factory=dict)
    tail: list[str] = field(default_factory=list)


def _has_surrogate(text: str) -> bool:
    return any("\udc80" <= ch <= "\udcff" for ch in text)


class Grammar:
    def __init__(self, doc: Mapping[str, Any]) -> None:
        self.doc = doc
        self.prog: str = doc["prog"]
        self.version: str = doc["version"]
        self.patterns: dict[str, str] = dict(doc.get("patterns", {}))
        self.globals: dict[str, dict[str, Any]] = dict(doc.get("global_flags", {}))
        self.commands: dict[str, dict[str, Any]] = dict(doc["commands"])
        # True: every global flag may precede the command; a list: only those flags may.
        before = doc.get("globals_before_command", False)
        self.before: bool | list[str] = before if isinstance(before, list) else bool(before)
        self.error_fields: dict[str, Any] = dict(doc.get("error_fields", {}))
        self.dup_policy: str = doc.get("duplicate_globals", "error")
        self.human_only_exit: int = int(doc.get("human_only_exit", USAGE_EXIT))

    # -- helpers ---------------------------------------------------------------------------

    def _pattern(self, spec: Mapping[str, Any]) -> re.Pattern[str] | None:
        name = spec.get("pattern")
        if not isinstance(name, str):
            return None
        return re.compile(self.patterns.get(name, name))

    def check_value(self, flag: str, spec: Mapping[str, Any], value: str) -> None:
        choices = spec.get("choices")
        if choices is not None and value not in choices:
            raise Usage(f"argument {flag}: invalid choice (choose from {', '.join(choices)})")
        pattern = self._pattern(spec)
        if pattern is not None and pattern.fullmatch(value) is None:
            raise Usage(f"argument {flag}: invalid value (does not match the documented grammar)")
        bounds = spec.get("range")
        if bounds is not None:
            try:
                number = int(value)
            except ValueError:
                raise Usage(f"argument {flag}: not a whole number") from None
            if not bounds[0] <= number <= bounds[1]:
                raise Usage(f"argument {flag}: out of range {bounds[0]}..{bounds[1]}")
        cap = spec.get("max_seconds")
        if cap is not None and (match := DURATION.fullmatch(value)):
            if float(match[1]) * UNIT_SECONDS[match[2]] > cap:
                raise Usage(f"argument {flag}: duration over the maximum")

    def allowed_globals(self, command: str) -> dict[str, dict[str, Any]]:
        out = {}
        for name, spec in self.globals.items():
            only = spec.get("only")
            if only is None or command in only:
                out[name] = spec
        return out

    def resolve_command(self, tokens: Sequence[str], start: int) -> tuple[str, int]:
        if start >= len(tokens):
            raise Usage("a command is required")
        first = tokens[start]
        two = f"{first} {tokens[start + 1]}" if start + 1 < len(tokens) else None
        if two is not None and two in self.commands:
            return two, start + 2
        if first in self.commands:
            return first, start + 1
        if any(name.split(" ")[0] == first for name in self.commands):
            raise Usage(f"{first}: a subcommand is required")
        raise Usage("invalid command (not a command review-respond may call)")

    # -- parsing ---------------------------------------------------------------------------

    def _take(
        self,
        tokens: Sequence[str],
        i: int,
        specs: Mapping[str, Mapping[str, Any]],
        store: dict[str, Any],
        *,
        is_global: bool,
    ) -> int:
        token = tokens[i]
        name, eq, inline = token.partition("=")
        if not name.startswith("--") or name not in specs:
            raise Usage(
                f"unrecognized argument: {name if name in self.all_option_names() else '?'}"
            )
        spec = specs[name]
        arity = int(spec.get("arity", 0))
        if arity == 0:
            if eq:
                raise Usage(f"argument {name}: ignored explicit argument")
            value: Any = True
            i += 1
        elif eq:
            value = inline
            i += 1
        else:
            if i + 1 >= len(tokens):
                raise Usage(f"argument {name}: expected one argument")
            raw = tokens[i + 1]
            # argparse: a lone "-" (stdin) is a value, other dash-leading text is an option.
            looks_flag = raw.startswith("-") and raw != "-" and not NEGATIVE_NUMBER.match(raw)
            if looks_flag:
                raise Usage(f"argument {name}: expected one argument")
            value = raw
            i += 2
        if arity:
            self.check_value(name, spec, value)
        if spec.get("repeat"):
            store.setdefault(name, []).append(value)
        elif name in store:
            if is_global and self.dup_policy == "last_wins":
                store[name] = value
            elif is_global and self.dup_policy == "same_value" and store[name] == value:
                pass
            else:
                raise Usage(f"argument {name}: given more than once")
        else:
            store[name] = value
        return i

    def all_option_names(self) -> set[str]:
        names = set(self.globals)
        for cmd in self.commands.values():
            names.update(cmd.get("flags", {}))
        return names

    def parse(self, tokens: Sequence[str]) -> Parsed:
        for index, token in enumerate(tokens, start=1):
            if _has_surrogate(token):
                raise Usage(f"argument {index} is not valid UTF-8")
        if tokens and tokens[0] == "--version":
            if len(tokens) != 1:
                raise Usage("--version takes no other argument")
            return Parsed(command="--version")
        flags: dict[str, Any] = {}
        i = 0
        while i < len(tokens) and tokens[i].startswith("-"):
            if tokens[i] == "--":
                raise Usage("'--' before the command is not accepted")
            allowed_first = self.before is True or (
                isinstance(self.before, list) and tokens[i].split("=")[0] in self.before
            )
            if not allowed_first:
                raise Usage("global flags must follow the command")
            i = self._take(tokens, i, self.globals, flags, is_global=True)
        command, i = self.resolve_command(tokens, i)
        cmd = self.commands[command]
        specs: dict[str, dict[str, Any]] = dict(self.allowed_globals(command))
        cmd_flags: dict[str, dict[str, Any]] = dict(cmd.get("flags", {}))
        overlap = set(specs) & set(cmd_flags)
        if overlap:
            raise AssertionError(f"grammar defines {sorted(overlap)} as global and local")
        specs.update(cmd_flags)
        for name in flags:
            if name not in specs:
                raise Usage(f"argument {name}: not accepted by this command")
        pos_specs: list[dict[str, Any]] = list(cmd.get("positionals", []))
        values: list[str] = []
        tail: list[str] = []
        dash_ok = bool(cmd.get("double_dash"))
        while i < len(tokens):
            token = tokens[i]
            if token == "--":
                if not dash_ok:
                    raise Usage("'--' is not accepted by this command")
                tail = list(tokens[i + 1 :])
                break
            if token.startswith("-") and token != "-" and not NEGATIVE_NUMBER.match(token):
                i = self._take(
                    tokens, i, specs, flags, is_global=token.split("=")[0] in self.globals
                )
                continue
            values.append(token)
            i += 1
        if cmd.get("double_dash_required") and not tail:
            raise Usage("the command after '--' is required")
        positionals = self._assign(pos_specs, values)
        parsed = Parsed(command=command, flags=flags, positionals=positionals, tail=tail)
        self._validate(cmd, specs, parsed)
        return parsed

    def _assign(self, specs: Sequence[Mapping[str, Any]], values: Sequence[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        index = 0
        for spec in specs:
            name = spec["name"]
            pattern = self._pattern(spec)
            if spec.get("repeat"):
                taken = list(values[index:])
                index = len(values)
                if len(taken) < int(spec.get("min", 0)):
                    raise Usage(f"the following arguments are required: {name}")
                for value in taken:
                    self._check_positional(name, pattern, value)
                out[name] = taken
                continue
            if index >= len(values):
                if spec.get("optional"):
                    continue
                raise Usage(f"the following arguments are required: {name}")
            self._check_positional(name, pattern, values[index])
            out[name] = values[index]
            index += 1
        if index < len(values):
            raise Usage(f"unrecognized argument at position {index + 1}")
        return out

    @staticmethod
    def _check_positional(name: str, pattern: re.Pattern[str] | None, value: str) -> None:
        if pattern is not None and pattern.fullmatch(value) is None:
            raise Usage(f"argument {name}: invalid value (does not match the documented grammar)")

    # -- rules -----------------------------------------------------------------------------

    def _validate(
        self, cmd: Mapping[str, Any], specs: Mapping[str, Mapping[str, Any]], parsed: Parsed
    ) -> None:
        flags = parsed.flags
        for name, spec in specs.items():
            if spec.get("required") and name not in flags:
                raise Usage(f"the following arguments are required: {name}")
        for name, values in flags.items():
            limit = specs[name].get("max_count")
            if limit is not None and isinstance(values, list) and len(values) > limit:
                raise Usage(f"argument {name}: at most {limit} values")
        for name in flags:
            if specs[name].get("refuse"):
                raise Usage(f"{name}: {specs[name]['refuse']}")
        for name in flags:
            if specs[name].get("human_only"):
                actor = flags.get("--actor")
                if not (isinstance(actor, str) and actor.startswith("human:")):
                    raise Usage(
                        f"{name} is human-only and needs --actor human:<user>", self.human_only_exit
                    )
        for rule in [*self.doc.get("rules", []), *cmd.get("rules", [])]:
            self._rule(rule, parsed)

    def _rule(self, rule: Mapping[str, Any], parsed: Parsed) -> None:
        flags = parsed.flags

        def has(name: str) -> bool:
            return name in flags

        if "when" in rule and not all(has(n) for n in rule["when"]):
            return
        if "when_absent" in rule and any(has(n) for n in rule["when_absent"]):
            return
        if "when_eq" in rule and not all(flags.get(k) == v for k, v in rule["when_eq"].items()):
            return
        if "when_pattern" in rule and not all(
            k in flags and re.fullmatch(v, str(flags[k])) for k, v in rule["when_pattern"].items()
        ):
            return
        if "when_ne" in rule and not all(flags.get(k) != v for k, v in rule["when_ne"].items()):
            return
        for name in rule.get("requires", []):
            if not has(name):
                raise Usage(f"{name} is required here")
        if "requires_any" in rule and not any(has(n) for n in rule["requires_any"]):
            if rule.get("confirmation"):
                raise Usage(
                    f"confirmation required: pass one of {', '.join(rule['requires_any'])} "
                    "(no terminal to ask on)",
                    CONFIRMATION_EXIT,
                    "confirmation_required",
                )
            raise Usage(f"one of {', '.join(rule['requires_any'])} is required here")
        for name, pattern in rule.get("forbids_value", {}).items():
            if has(name) and re.search(pattern, str(flags[name])):
                raise Usage(f"{name}: this value is not accepted here")
        if "cwd_within" in rule:
            flag, base = rule["cwd_within"]["flag"], rule["cwd_within"]["base"]
            if has(flag) and has(base):
                root = os.path.normpath(str(flags[base]))
                target = os.path.normpath(os.path.join(root, str(flags[flag])))
                if target != root and not target.startswith(root.rstrip("/") + "/"):
                    raise Usage(f"{flag} lies outside the worktree")
        if "distinct_names" in rule:
            first, second = rule["distinct_names"]
            seen = [v.split("=", 1)[0] for v in flags.get(first, [])]
            if len(seen) != len(set(seen)):
                raise Usage(f"{first}: a name given twice")
            if set(seen) & set(flags.get(second, [])):
                raise Usage(f"a name given with both {first} and {second}")
        for name in rule.get("forbids", []):
            if has(name):
                raise Usage(f"{name} is not accepted here")
        if "exclusive" in rule and sum(has(n) for n in rule["exclusive"]) > 1:
            raise Usage(f"{' and '.join(rule['exclusive'])} are mutually exclusive")
        if "not_same_value" in rule:
            first, second = rule["not_same_value"]
            if has(first) and has(second) and flags[first] == flags[second] == rule["value"]:
                raise Usage(f"{first} and {second} cannot both read {rule['value']!r}")
        if "positional_xor_flag" in rule:
            pos = rule["positional_xor_flag"]["positional"]
            flag = rule["positional_xor_flag"]["flag"]
            if (pos in parsed.positionals) == has(flag):
                raise Usage(f"exactly one of <{pos}> and {flag} is required")
        if "positional_or_any_flag" in rule:
            pos = rule["positional_or_any_flag"]["positional"]
            names = rule["positional_or_any_flag"]["flags"]
            if pos not in parsed.positionals and not any(has(n) for n in names):
                raise Usage(f"<{pos}> or one of {', '.join(names)} is required")
        if "positional_excludes_flags" in rule:
            pos = rule["positional_excludes_flags"]["positional"]
            names = rule["positional_excludes_flags"]["flags"]
            if pos in parsed.positionals and any(has(n) for n in names):
                raise Usage(f"<{pos}> cannot be combined with {', '.join(names)}")


# -- state ---------------------------------------------------------------------------------


class State:
    """The private per-test directory: scripted responses in, calls and violations out."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @contextmanager
    def locked(self) -> Iterator[None]:
        fd = os.open(self.root / "lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _append(self, name: str, record: Mapping[str, Any]) -> None:
        line = json.dumps(record, sort_keys=True) + "\n"
        fd = os.open(self.root / name, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)

    def record_call(self, record: Mapping[str, Any]) -> None:
        self._append("calls.jsonl", record)

    def record_violation(self, record: Mapping[str, Any]) -> None:
        self._append("violations.jsonl", record)

    def take_response(self, tool: str, parsed: Parsed) -> dict[str, Any] | None:
        path = self.root / "script.json"
        if not path.exists():
            return None
        doc = json.loads(path.read_text())
        found: dict[str, Any] | None = None
        for entry in doc["entries"]:
            if entry["tool"] != tool or entry["command"] != parsed.command:
                continue
            if entry["remaining"] is not None and entry["remaining"] <= 0:
                continue
            if not _matches(entry.get("match", {}), parsed):
                continue
            found = entry
            break
        if found is None:
            return None
        if found["remaining"] is not None:
            found["remaining"] -= 1
        path.write_text(json.dumps(doc))
        return found


def _matches(match: Mapping[str, Any], parsed: Parsed) -> bool:
    for name, wanted in match.get("positionals", {}).items():
        if parsed.positionals.get(name) != wanted:
            return False
    for name, wanted in match.get("flags", {}).items():
        got = parsed.flags.get(name)
        if isinstance(got, list):
            if not isinstance(wanted, list) or any(w not in got for w in wanted):
                return False
        elif got != wanted:
            return False
    if "tail" in match and parsed.tail != match["tail"]:
        return False
    return True


# -- entry point ---------------------------------------------------------------------------


def _load_grammar(base: Path, prog: str) -> Grammar:
    local = base / f"{prog}.grammar.json"
    path = local if local.exists() else base.parent / "pinned" / prog / "grammar.json"
    return Grammar(json.loads(path.read_text()))


def _state_dir(base: Path) -> State:
    pointer = base / "fakebin.state"
    if pointer.exists():
        return State(Path(pointer.read_text().strip()))
    raise SystemExit("fakebin: no fakebin.state next to the fake; it only runs under the harness")


def _emit(out: bytes, err: bytes) -> None:
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(err)
    sys.stderr.buffer.flush()


def _enc(text: str) -> bytes:
    return text.encode("utf-8", "surrogateescape")


def _envelope(grammar: Grammar, code: str, message: str) -> dict[str, Any]:
    """The sibling's error envelope with every key its pinned error schema requires."""
    return {"error": {"code": code, "message": message, **grammar.error_fields}}


def _words_before_tail(tokens: Sequence[str]) -> list[str]:
    """Tokens before the ``--`` that starts a command tail (a leading ``--`` is not a tail)."""
    cut = tokens.index("--") if "--" in tokens else 0
    return list(tokens[:cut]) if cut else list(tokens)


def _consumes_stdin(parsed: Parsed) -> bool:
    """True when the grammar reads stdin: a lone ``-`` as a flag value or a positional."""
    values: list[Any] = list(parsed.positionals.values())
    values += list(parsed.flags.values())
    for value in values:
        if value == "-" or (isinstance(value, list) and "-" in value):
            return True
    return False


def _pending_stdin() -> bytes:
    """Whatever the caller already piped (one bounded read; never waits for an open pipe)."""
    if sys.stdin is None or sys.stdin.isatty():
        return b""
    try:
        ready, _, _ = select.select([sys.stdin], [], [], 0.5)
        return os.read(sys.stdin.fileno(), 65536) if ready else b""
    except (OSError, ValueError):
        return b""


def main(prog: str, base: Path | None = None, argv: Sequence[str] | None = None) -> int:
    here = base if base is not None else Path(__file__).resolve().parent
    grammar = _load_grammar(here, prog)
    state = _state_dir(here)
    tokens = list(sys.argv[1:] if argv is None else argv)
    # A guess for the usage envelope only: a call that parsed is judged on its parsed flags.
    json_guess = "--json" in _words_before_tail(tokens)
    record: dict[str, Any] = {
        "tool": prog,
        "argv": tokens,
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "env": dict(os.environ),
        "stdin": None,
        "stdin_tty": sys.stdin is not None and sys.stdin.isatty(),
    }
    try:
        parsed = grammar.parse(tokens)
        if parsed.command != "--version" and "--json" not in parsed.flags:
            raise Usage("review-respond always calls this program with --json")
    except Usage as exc:
        record.update(outcome="usage_error", message=exc.message, exit=exc.exit_code, parsed=None)
        with state.locked():
            state.record_call(record)
            state.record_violation(
                {"tool": prog, "kind": "usage", "argv": tokens, "message": exc.message}
            )
        envelope = _envelope(grammar, exc.code, exc.message)
        out = _enc(json.dumps(envelope) + "\n") if json_guess else b""
        _emit(out, _enc(f"{prog}: error: {exc.message}\n"))
        return exc.exit_code
    record["parsed"] = {
        "command": parsed.command,
        "flags": parsed.flags,
        "positionals": parsed.positionals,
        "tail": parsed.tail,
    }
    if parsed.command == "--version":
        record.update(outcome="version", exit=0)
        with state.locked():
            state.record_call(record)
        _emit(_enc(f"{prog} {grammar.version}\n"), b"")
        return 0
    if _consumes_stdin(parsed):
        if sys.stdin is not None:
            record["stdin"] = sys.stdin.buffer.read().decode("utf-8", "surrogateescape")
    else:
        stray = _pending_stdin()
        if stray:
            record["stdin"] = stray.decode("utf-8", "surrogateescape")
            with state.locked():
                state.record_violation(
                    {
                        "tool": prog,
                        "kind": "stdin",
                        "argv": tokens,
                        "message": "stdin was piped but no argument reads it",
                    }
                )
    with state.locked():
        entry = state.take_response(prog, parsed)
        if entry is None:
            record.update(outcome="unscripted", exit=99)
            state.record_call(record)
            state.record_violation(
                {"tool": prog, "kind": "unscripted", "argv": tokens, "command": parsed.command}
            )
        else:
            record.update(outcome="matched", entry=entry["id"], exit=entry["result"]["exit"])
            state.record_call(record)
    if entry is None:
        message = f"{prog}: no response scripted for this call"
        out = _enc(json.dumps(_envelope(grammar, "internal", message)) + "\n")
        _emit(out, _enc(message + "\n"))
        return 99
    result = entry["result"]
    delay = float(result.get("sleep", 0))
    if delay:
        time.sleep(delay)
    gate = result.get("wait_for")
    if gate:  # a handshake with the test: answer once this file exists (at most 60 s)
        limit = time.monotonic() + 60
        while not os.path.exists(gate) and time.monotonic() < limit:
            time.sleep(0.02)
    if "stdout_json" in result:
        out = _enc(json.dumps(result["stdout_json"]) + "\n")
    else:
        out = _enc(result.get("stdout", ""))
    _emit(out, _enc(result.get("stderr", "")))
    return int(result["exit"])
