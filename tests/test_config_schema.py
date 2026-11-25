"""The pydantic model and schemas/config.json must accept and refuse the same documents.

A differential test: every leaf of the schema is probed with a pool of boundary and hostile
values, every object level with an extra key; the schema's verdict (jsonschema) must equal the
model's. The model is deliberately narrower in the cases `_narrower` names (each is a documented
conservative reading); it must never be wider.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from review_respond.config import Config, duration_ms

from .harness import schemas

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "schemas" / "config.json").read_text())
SAMPLE_HOST = "git.example.test"
SAMPLE_REPO = "git.example.test/team/service"

VALUES: list[Any] = [
    None, True, False, 0, 1, -1, 3, 63, 64, 65, 128, 600, 601, 1000, 1001, 1024, 1.5, 5.0,
    "", "x", "0", "5m", "7x", "1h", "500ms", "30d", "99999999999d", "-5m", " 5m", "5m\n",
    "required", "off", "user,local", "user,", "a b", "a\nb", "a\x00b", "a\x1bb", "a\x85b",
    "A=B", "1X", "FOO", "REVIEW_RESPOND_X", "x" * 64, "x" * 65, "x" * 128, "x" * 129,
    "x" * 256, "x" * 257, "x" * 1024, "x" * 1025, "h.test:1", "h.test:1:2", "H.test:1",
    "a/b", "/a", "a/", "a//b", "a/../b", "**/x/**", "*.pem", "it's", "ab", "abc",
    [], {}, ["a"], ["x", "x"], [["a"]], {"a": 1},
]  # fmt: skip


def leaves(schema: dict[str, Any], path: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    if "properties" in schema:
        for key, sub in schema["properties"].items():
            yield from leaves(sub, (*path, key))
    elif "patternProperties" in schema:
        sample = SAMPLE_HOST if path[-1:] in (("hosts",), ("accounts",)) else SAMPLE_REPO
        (sub,) = schema["patternProperties"].values()
        yield from leaves(sub, (*path, sample))
    elif schema.get("type") == "array" and isinstance(schema.get("items"), dict):
        if schema["items"].get("type") == "object":
            yield from leaves(schema["items"], (*path, 0))
        else:
            yield path  # arrays are probed as a whole (and item-wise below)
            yield (*path, 0)
    else:
        yield path


def objects(schema: dict[str, Any], path: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    if "properties" in schema:
        yield path
        for key, sub in schema["properties"].items():
            yield from objects(sub, (*path, key))
    elif "patternProperties" in schema:
        yield path
        sample = SAMPLE_HOST if path[-1:] in (("hosts",), ("accounts",)) else SAMPLE_REPO
        (sub,) = schema["patternProperties"].values()
        yield from objects(sub, (*path, sample))
    elif schema.get("type") == "array" and schema["items"].get("type") == "object":
        yield from objects(schema["items"], (*path, 0))


def nest(path: tuple[Any, ...], value: Any) -> dict[str, Any]:
    node: Any = value
    for part in reversed(path):
        node = [node] if part == 0 else {part: node}
    parent: Any = node
    for index, part in enumerate(path[:-1]):
        parent = parent[0] if part == 0 else parent[part]
        if part == 0 and path[-1] != "cmd" and index == len(path) - 2 and "hooks" in path:
            parent["cmd"] = "x"
    return node


def model_accepts(document: dict[str, Any]) -> bool:
    try:
        Config.model_validate(document)
    except ValidationError:
        return False
    return True


def schema_accepts(document: dict[str, Any]) -> bool:
    try:
        schemas.validate("review-respond/config.json", document)
    except Exception:
        return False
    return True


def _narrower(path: tuple[Any, ...], value: Any) -> bool:
    """Documented cases where the model refuses what the schema accepts."""
    key = ".".join(str(p) for p in path)
    # an integral float such as 5.0 (JSON Schema calls it an integer; the model is strict)
    if isinstance(value, float) and value == int(value):
        return True
    # async on a pre hook (it decides the outcome, so it is always waited for)
    if path[-1] == "async" and value is True and path[1] in ("pre-round", "pre-push"):
        return True
    if isinstance(value, str):
        # a trailing newline: Python's `$` would accept it; the model uses `\\Z` (ECMA-262 rules)
        if value.endswith("\n") and value.strip() != "":
            return True
        # durations above ten years
        if re.fullmatch(r"[0-9]+(?:ms|s|m|h|d)", value) and duration_ms(value) > 3650 * 86_400_000:
            return True
        # control characters in free text, paths, argv items, git identity, commands
        if re.search(r"[\x00-\x1f\x7f-\x9f]", value) and not re.search(
            r"^(?:[A-Za-z0-9._-]+)$", value
        ):
            return True
    # zero durations where zero would disable a safety bound
    zero = value == "0" or (isinstance(value, str) and re.fullmatch(r"0(?:ms|s|m|h|d)", value))
    if zero and key.split(".")[-1] in {"pending_ttl", "timeout", "kill_grace"}:
        return True
    # hook timeouts: durations are capped at 600s like the integer form
    if (
        key.endswith(".timeout")
        and "hooks" in path
        and isinstance(value, str)
        and re.fullmatch(r"[0-9]+(?:ms|s|m|h|d)", value)
        and duration_ms(value) > 600_000
    ):
        return True
    # a name starting with REVIEW_RESPOND_ is never passed to jobs
    if (
        path[-2:] == ("pass_env", 0)
        and isinstance(value, str)
        and value.upper().startswith("REVIEW_RESPOND_")
    ):
        return True
    # blank hook command
    if "cmd" == path[-1] and isinstance(value, str) and not value.strip():
        return True
    # null for a key the model reads as "unset": allowed by the schema only where `null` is typed
    return False


def _check(path: tuple[Any, ...], value: Any) -> None:
    document = nest(path, value)
    schema_says = schema_accepts(document)
    model_says = model_accepts(document)
    if schema_says == model_says:
        return
    assert schema_says and not model_says and _narrower(path, value), (
        f"path={path!r} value={value!r} schema={schema_says} model={model_says}"
    )


ALL_LEAVES = sorted(set(leaves(SCHEMA)), key=str)


@pytest.mark.parametrize("path", ALL_LEAVES, ids=lambda p: ".".join(map(str, p)))
def test_leaf_verdicts_agree(path: tuple[Any, ...]) -> None:
    for value in VALUES:
        _check(path, value)


@pytest.mark.parametrize("path", sorted(set(objects(SCHEMA)), key=str), ids=str)
def test_unknown_key_refused_everywhere(path: tuple[Any, ...]) -> None:
    node: Any = {"bogus": 1}
    if path and path[-1] == 0:
        node["cmd"] = "x"
    for part in reversed(path):
        node = [node] if part == 0 else {part: node}
    assert not schema_accepts(node)
    assert not model_accepts(node)


def test_map_keys_agree() -> None:
    hosts = ["h.test", "h.test:8443", "a", "H.test", "bad host", "a..b", "-x", "x-", "h:123456", ""]
    for host in hosts:
        for doc in ({"hosts": {host: {}}}, {"me": {"accounts": {host: "1"}}}):
            assert schema_accepts(doc) == model_accepts(doc), (host, doc)
    repos = ["h.test/a/b", "h:80/a", "h.test", "h.test/o/../r", "h.test/./r", "h.test//r", "/o/r"]
    for repo in repos:
        doc = {"repos": {repo: {}}}
        assert schema_accepts(doc) == model_accepts(doc), repo


def test_model_defaults_equal_schema_defaults() -> None:
    dumped = Config().canonical()
    for key, spec in SCHEMA["properties"].items():
        if "default" in spec and spec["default"] not in (None, {}):
            assert dumped.get(key) == spec["default"], key
        for sub, subspec in spec.get("properties", {}).items():
            if "default" in subspec and subspec["default"] is not None:
                assert dumped[key].get(sub) == subspec["default"], f"{key}.{sub}"
