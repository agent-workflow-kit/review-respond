"""Schema validation against review-respond's own schemas and the pinned sibling copies."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parent.parent.parent
OWN_SCHEMAS = ROOT / "schemas"
PINNED = ROOT / "tests" / "pinned"
TOOLS = ("forgectl", "agent-spawn", "pr-comment", "wtctl", "jobsctl", "claude-session")


@cache
def _documents() -> dict[str, Any]:
    docs: dict[str, Any] = {}
    paths = sorted(OWN_SCHEMAS.rglob("*.json")) + sorted(PINNED.glob("*/schemas/**/*.json"))
    for path in paths:
        doc = json.loads(path.read_text())
        schema_id = doc["$id"]
        if schema_id in docs:
            raise ValueError(f"duplicate schema $id {schema_id} ({path})")
        docs[schema_id] = doc
    return docs


@cache
def registry() -> Registry:
    return Registry().with_resources(
        [(schema_id, Resource.from_contents(doc)) for schema_id, doc in _documents().items()]
    )


# Commands whose output schema has another name (jobsctl's mutating commands print change.json).
OUTPUT_ALIASES = {("jobsctl", "resolve"): "change"}


def output_schema_id(tool: str, command: str, exit_code: int = 0) -> str:
    """The pinned schema of ``<tool> <command> --json`` (the error envelope if exit_code != 0)."""
    if exit_code != 0:
        return f"{tool}/cli/error.json"
    name = OUTPUT_ALIASES.get((tool, command), command.replace(" ", "-"))
    return f"{tool}/cli/{name}.json"


def known(schema_id: str) -> bool:
    return schema_id in _documents()


def validate(schema_id: str, instance: Any) -> None:
    """Raise ``jsonschema.ValidationError`` when ``instance`` does not match ``schema_id``."""
    docs = _documents()
    if schema_id not in docs:
        raise KeyError(f"no schema with $id {schema_id}")
    Draft202012Validator(docs[schema_id], registry=registry()).validate(instance)


def pin(tool: str) -> dict[str, str]:
    """The ``PIN.json`` of a pinned sibling: tool, version, tag and commit."""
    return json.loads((PINNED / tool / "PIN.json").read_text())
