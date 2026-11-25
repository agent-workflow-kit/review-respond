"""The pinned sibling copies name their version, match the Dependencies table and resolve."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from .harness import builders, schemas

ROOT = Path(__file__).resolve().parent.parent
INTERFACE = (ROOT / "docs" / "interface.md").read_text()

# The commands review-respond may call, from the interface Dependencies table.
USED_COMMANDS: dict[str, set[str]] = {
    "forgectl": {"prs", "show", "comments", "access", "whoami", "compare"},
    "agent-spawn": {"list", "show", "resume", "launch", "wait", "stop", "teardown"},
    "pr-comment": {"reply", "resolve"},
    "wtctl": {"create", "extract", "locate", "remove", "list", "label add", "show"},
    "claude-session": {"kinds"},
    "jobsctl": {"add", "show", "cancel", "resolve"},
}


def minimums() -> dict[str, str]:
    table = INTERFACE.split("## Dependencies", 1)[1].split("\n## ", 1)[0]
    out = {}
    for line in table.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 4 and re.fullmatch(r"\d+\.\d+\.\d+", cells[3]):
            out[cells[0]] = cells[3]
    return out


def test_the_dependencies_table_lists_every_pinned_tool() -> None:
    assert set(minimums()) == set(schemas.TOOLS)


@pytest.mark.parametrize("tool", schemas.TOOLS)
def test_pin_names_the_minimum_version_of_the_sibling(tool: str) -> None:
    pin = schemas.pin(tool)
    assert pin["tool"] == tool
    assert pin["version"] == minimums()[tool]
    assert pin["tag"] == f"v{pin['version']}"
    assert re.fullmatch(r"[0-9a-f]{40}", pin["commit"])


@pytest.mark.parametrize("tool", schemas.TOOLS)
def test_grammar_is_pinned_to_the_same_version(tool: str) -> None:
    grammar = json.loads((schemas.PINNED / tool / "grammar.json").read_text())
    assert grammar["prog"] == tool
    assert grammar["version"] == schemas.pin(tool)["version"]
    assert f"v{grammar['version']}" in grammar["source"] or "at v" in grammar["source"]


@pytest.mark.parametrize("tool", schemas.TOOLS)
def test_grammar_commands_are_exactly_the_ones_review_respond_uses(tool: str) -> None:
    grammar = json.loads((schemas.PINNED / tool / "grammar.json").read_text())
    assert set(grammar["commands"]) == USED_COMMANDS[tool]


@pytest.mark.parametrize("tool", schemas.TOOLS)
def test_every_used_command_has_a_pinned_output_schema(tool: str) -> None:
    for command in USED_COMMANDS[tool]:
        assert schemas.known(schemas.output_schema_id(tool, command)), command
    assert schemas.known(f"{tool}/cli/error.json")


def refs(node: Any) -> list[str]:
    if isinstance(node, dict):
        out = [node["$ref"]] if isinstance(node.get("$ref"), str) else []
        return out + [r for v in node.values() for r in refs(v)]
    if isinstance(node, list):
        return [r for v in node for r in refs(v)]
    return []


@pytest.mark.parametrize("tool", schemas.TOOLS)
def test_every_reference_in_a_pinned_schema_resolves(tool: str) -> None:
    registry = schemas.registry()
    checked = 0
    for path in sorted((schemas.PINNED / tool / "schemas").rglob("*.json")):
        doc = json.loads(path.read_text())
        resolver = registry.resolver(base_uri=doc["$id"])
        for ref in refs(doc):
            resolver.lookup(ref)
            checked += 1
    assert checked > 0


def test_every_pinned_schema_id_is_prefixed_with_its_tool() -> None:
    for tool in schemas.TOOLS:
        for path in (schemas.PINNED / tool / "schemas").rglob("*.json"):
            assert json.loads(path.read_text())["$id"].startswith(f"{tool}/"), path


def test_the_registry_holds_review_respond_and_all_siblings() -> None:
    assert schemas.known("review-respond/cli/check.json")
    assert schemas.known("forgectl/cli/comments.json")


def test_builders_produce_documents_that_validate() -> None:
    schemas.validate("forgectl/cli/show.json", builders.forgectl_show())
    schemas.validate("forgectl/cli/whoami.json", builders.forgectl_whoami())
    for name, doc in builders.scenario_comment_lifecycle().items():
        schemas.validate("forgectl/cli/comments.json", doc), name
    before, after = builders.scenario_moved_head()
    schemas.validate("forgectl/cli/show.json", before)
    schemas.validate("forgectl/cli/show.json", after)
    assert before["head"]["sha"] != after["head"]["sha"]
    assert before["pr"] == after["pr"]


def test_the_validator_rejects_a_wrong_document() -> None:
    from jsonschema import ValidationError

    bad = builders.forgectl_show()
    del bad["head"]
    with pytest.raises(ValidationError):
        schemas.validate("forgectl/cli/show.json", bad)
