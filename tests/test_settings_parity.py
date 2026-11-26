"""The profile validator and the profile schema accept and refuse the same documents.

The two are one check written twice (the schema validator is a test-only dependency). This test
mutates both golden profiles in a large, deterministic number of ways and fails, printing the
document, on the first mutant the two judge differently.
"""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path
from typing import Any

import pytest

from review_respond import settings
from review_respond.errors import CliError
from review_respond.settings import validate_profile

from .harness import schemas
from .settings_support import ROOT, SCHEMA_ID

GOLDEN = ROOT / "tests" / "golden" / "settings"
SCHEMA = json.loads((ROOT / "schemas" / "permission-profile.json").read_text())

HIDDEN = [
    "\u0000",
    "\u0009",
    "\u000a",
    "\u000d",
    "\u001b",
    "\u001f",
    "\u007f",
    "\u0080",
    "\u009f",
    "\u00ad",
    "\u034f",
    "\u0600",
    "\u0605",
    "\u061c",
    "\u06dd",
    "\u070f",
    "\u0890",
    "\u0891",
    "\u08e2",
    "\u115f",
    "\u1160",
    "\u17b4",
    "\u17b5",
    "\u180b",
    "\u180e",
    "\u180f",
    "\u2000",
    "\u200a",
    "\u200b",
    "\u200d",
    "\u200e",
    "\u200f",
    "\u2028",
    "\u2029",
    "\u202a",
    "\u202e",
    "\u202f",
    "\u205f",
    "\u2060",
    "\u2066",
    "\u2069",
    "\u206f",
    "\u2800",
    "\u3164",
    "\ud800",
    "\udbff",
    "\udc00",
    "\udfff",
    "\ue000",
    "\uf8ff",
    "\ufe00",
    "\ufe0f",
    "\ufeff",
    "\uffa0",
    "\ufff0",
    "\ufff9",
    "\ufffb",
    "\ufffe",
    "\uffff",
    "\U000e0001",
    "\U000e0100",
    "\U000f0000",
    "\U0010ffff",
    "\U0001f600",
    "\U00010000",
    "\u00a0",
    "\u3000",
    "\u1680",
    "\uf900",
    "\u2fff",
    "\ufdd0",
]
PLAIN = ["é", "日", "a", "Z", "0", " ", "_", "-", ".", ":", "@", "+", "=", "$", "{", "}", "~"]
GLOBS = ["*", "?", "[", "]", "{", "}", "\\", "(", ")"]

RULES = [
    "", "a", "A", "_", "*", "mcp__*", "Bash", "Bash()", "Bash(x)", "Bash(x:*)", "Bash(x)\n",
    "Bash\n", "\nBash", "Bash(x\n)", "Bash(a(b))", "Bash(a)b", "Bash((", "Bash))", "(x)", "1Bash",
    "Bash (x)", "Bash(x) ", "Read(//a/**)", "Read(/${CWD}/**)", "Read(~/a/**)", "x" * 2048,
    "x" * 2049, "A" + "(" + "y" * 2045 + ")", "A" + "(" + "y" * 2046 + ")", "Bash(é)", "日本",
    "Bash(\U0001f600)", "Task", "Agent", "Task\n",
]  # fmt: skip
PATHS = [
    "", "/", "~", "~/", "/a", "~/a", "//a", "/a/", "~/a/", "/a//b", "/./a", "/a/.", "/..",
    "/a/..", "/a/../b", "~/./a", "~/../a", "/a/.b", "/a/..b", "/a/b.", "/.", "rel", "a/b",
    "~user/x", "./a", "../a", "/a\n", "/a\nb", "\n/a", "/a b", "/é", "/日本/x", "/a/~", "/~",
    "~/~", "~~/a", "/" + "a" * 1023, "/" + "a" * 1024, "/a" * 512, "/a" * 513, "/a/" + "b" * 1019,
]  # fmt: skip


def walk_lists(doc: Any, path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    found: list[tuple[str, ...]] = []
    if isinstance(doc, dict):
        for key, value in doc.items():
            if isinstance(value, list):
                found.append((*path, key))
            found += walk_lists(value, (*path, key))
    return found


def walk_dicts(doc: Any, path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    found: list[tuple[str, ...]] = []
    if isinstance(doc, dict):
        found.append(path)
        for key, value in doc.items():
            found += walk_dicts(value, (*path, key))
    return found


def at(doc: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        doc = doc[key]
    return doc


def schema_accepts(doc: Any) -> bool:
    try:
        schemas.validate(SCHEMA_ID, doc)
    except Exception:  # noqa: BLE001  (jsonschema.ValidationError)
        return False
    return True


def code_accepts(doc: Any) -> bool:
    try:
        validate_profile(doc)
    except CliError:
        return False
    return True


def strings_for(path: tuple[str, ...]) -> list[str]:
    if path[-1] in ("allow", "deny"):
        return RULES + [f"Bash(x{ch}y)" for ch in HIDDEN + PLAIN + GLOBS]
    return PATHS + [f"/a{ch}b" for ch in HIDDEN + PLAIN + GLOBS] + [f"~/a{ch}" for ch in HIDDEN]


def mutants(seed: str) -> list[Any]:
    base: dict[str, Any] = json.loads((GOLDEN / f"{seed}.json").read_text())
    rng = random.Random(f"parity-{seed}")
    out: list[Any] = [copy.deepcopy(base)]
    lists = walk_lists(base)
    dicts = walk_dicts(base)

    def fresh() -> Any:
        return copy.deepcopy(base)

    # every list: every crafted string appended, and substituted for the first element
    for path in lists:
        if path[-1] in ("allowWrite", "allowedDomains"):
            continue
        for text in strings_for(path):
            doc = fresh()
            at(doc, path).append(text)
            out.append(doc)
            doc = fresh()
            target = at(doc, path)
            if target:
                target[0] = text
                out.append(doc)
    # every list: empty, oversized, duplicated, retyped items, a single item
    for path in lists:
        for build in (
            lambda a: [],
            lambda a: [a[0]] * 2 if a else ["x", "x"],
            lambda a: [*a, *a],
            lambda a: a[:1],
            lambda a: a[1:],
            lambda a: [*a, 1],
            lambda a: [*a, None],
            lambda a: [*a, ["x"]],
            lambda a: [*a, {"x": 1}],
            lambda a: [*a, True],
            lambda a: [*(f"/p{i}" for i in range(63))] + a,
            lambda a: [*(f"/p{i}" for i in range(64))] + a,
            lambda a: [*(f"/p{i}" for i in range(65))] + a,
            lambda a: [*(f"Bash(x{i}:*)" for i in range(250))] + a,
            lambda a: [*(f"Bash(x{i}:*)" for i in range(256))] + a,
            lambda a: [*(f"Bash(x{i}:*)" for i in range(257))] + a,
            lambda a: "x",
            lambda a: None,
            lambda a: {},
            lambda a: 0,
        ):
            doc = fresh()
            parent = at(doc, path[:-1])
            parent[path[-1]] = build(parent[path[-1]])
            out.append(doc)
    # every dict: a missing key, an extra key, each key retyped
    for path in dicts:
        for key in list(at(base, path)):
            doc = fresh()
            del at(doc, path)[key]
            out.append(doc)
            for value in (None, 1, True, "x", [], {}, 1.5, [""], {"a": "b"}):
                doc = fresh()
                at(doc, path)[key] = value
                out.append(doc)
        for extra in ("hooks", "env", "defaultMode", "ask", "x", ""):
            doc = fresh()
            at(doc, path)[extra] = []
            out.append(doc)
            doc = fresh()
            at(doc, path)[extra] = {"a": 1}
            out.append(doc)
    for value in (None, 1, "x", [], [base], True):
        out.append(value)
    # the fixed denies: each removed, each replaced by a near miss
    deny = ("permissions", "deny")
    for rule in list(at(base, deny)):
        for new in (None, rule + " ", rule + "\n", rule.lower(), rule[:-1], "x" + rule):
            doc = fresh()
            items = at(doc, deny)
            items[items.index(rule)] = new if new is not None else "Z"
            out.append(doc)
    # the sandbox constants
    for fs_key in ("allowWrite",):
        for value in (["/"], ["x"], [""], [None]):
            doc = fresh()
            doc["sandbox"]["filesystem"][fs_key] = value
            out.append(doc)
    for value in (["x.test"], [""], [None], {}, "x"):
        doc = fresh()
        doc["sandbox"]["network"]["allowedDomains"] = value
        out.append(doc)
    # random compositions of up to four mutations
    singles = [m for m in out[1:] if isinstance(m, dict)]
    for _ in range(1500):
        doc = copy.deepcopy(rng.choice(singles))
        for _ in range(rng.randint(0, 3)):
            path = rng.choice(lists)
            try:
                target = at(doc, path)
            except (KeyError, TypeError):
                continue
            if not isinstance(target, list):
                continue
            text = rng.choice(strings_for(path))
            where = rng.randint(0, len(target))
            target.insert(where, text)
        out.append(doc)
    return out


@pytest.mark.parametrize("name", ["profile-default", "profile-repo"])
def test_schema_and_validator_agree_on_every_mutant(name: str) -> None:
    docs = mutants(name)
    assert len(docs) > 3000
    accepted = 0
    for doc in docs:
        by_schema, by_code = schema_accepts(doc), code_accepts(doc)
        if by_schema != by_code:
            shown = json.dumps(doc, ensure_ascii=True)[:3000]
            pytest.fail(
                f"schema {'accepts' if by_schema else 'refuses'} and validate_profile "
                f"{'accepts' if by_code else 'refuses'}: {shown}"
            )
        accepted += by_schema
    assert accepted > 100  # the corpus holds good documents too, not only broken ones


def test_the_schema_uses_the_code_patterns() -> None:
    defs = SCHEMA["$defs"]
    assert defs["rule"]["pattern"] == settings.RULE_PATTERN
    assert defs["path"]["pattern"] == settings.PATH_PATTERN
    assert defs["rule"]["maxLength"] == settings.MAX_RULE_CHARS
    assert defs["path"]["maxLength"] == settings.MAX_PATH_CHARS
    assert SCHEMA["properties"]["permissions"]["properties"]["allow"]["maxItems"] == (
        settings.MAX_ALLOW_RULES
    )
    for name in ("allowRead", "denyRead", "denyWrite"):
        assert SCHEMA["properties"]["sandbox"]["properties"]["filesystem"]["properties"][name][
            "maxItems"
        ] == (settings.MAX_LIST)


@pytest.mark.parametrize("text", ["Bash(x)\n", "/a\n", "Read(//a/**)\n", "\n"])
def test_a_trailing_newline_is_refused_by_both(text: str) -> None:
    doc: dict[str, Any] = json.loads((GOLDEN / "profile-default.json").read_text())
    doc["permissions"]["allow"].append(text)
    assert not schema_accepts(doc) and not code_accepts(doc)
    doc = json.loads((GOLDEN / "profile-default.json").read_text())
    doc["sandbox"]["filesystem"]["allowRead"].append(text)
    assert not schema_accepts(doc) and not code_accepts(doc)


@pytest.mark.parametrize("path", ["/", "~", "~/", "/a/", "//a", "/a//b", "/./a", "/a/..", "~/.."])
def test_path_shapes_the_schema_and_validator_both_refuse(path: str) -> None:
    doc: dict[str, Any] = json.loads((GOLDEN / "profile-default.json").read_text())
    doc["sandbox"]["filesystem"]["allowRead"].append(path)
    assert not schema_accepts(doc) and not code_accepts(doc)


def test_the_goldens_are_accepted_by_both() -> None:
    for path in sorted(Path(GOLDEN).glob("*.json")):
        doc = json.loads(path.read_text())
        assert schema_accepts(doc) and code_accepts(doc), path.name
