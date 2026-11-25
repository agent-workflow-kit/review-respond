"""Scaffold invariants: module layout, dependencies, the live marker and schema files."""

from __future__ import annotations

import importlib
import json
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

MODULE_MAP = [
    "cli",
    "config",
    "gateway",
    "store",
    "audit",
    "hooks",
    "detect",
    "rounds",
    "job",
    "prompt",
    "artifact",
    "pending",
    "diffstore",
    "hgit",
    "confirm",
    "cron",
    "validate",
]

DRAFT = "https://json-schema.org/draft/2020-12/schema"


@pytest.mark.parametrize("name", MODULE_MAP)
def test_planned_module_imports(name: str) -> None:
    importlib.import_module(f"review_respond.{name}")


def test_dependencies_are_only_pyyaml_and_pydantic() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    names = sorted(
        d.split(">")[0].split("=")[0].split("<")[0].split("[")[0].strip()
        for d in project["dependencies"]
    )
    assert names == ["pydantic", "pyyaml"]


def test_fakebin_is_reserved() -> None:
    assert (ROOT / "tests" / "fakebin").is_dir()


def test_live_marker_is_registered_and_deselected_by_default() -> None:
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]
    assert any(m.startswith("live:") for m in cfg["markers"])
    assert "not live" in cfg["addopts"]


@pytest.mark.live
def test_live_marker_tests_are_not_collected_by_default() -> None:
    pytest.fail("a live test ran under the default selection")


SCHEMA_FILES = sorted((ROOT / "schemas").rglob("*.json"))


def test_schemas_exist() -> None:
    assert SCHEMA_FILES


@pytest.mark.parametrize("path", SCHEMA_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_schema_is_json_and_declares_draft_2020_12(path: Path) -> None:
    doc = json.loads(path.read_text())
    assert doc["$schema"] == DRAFT
