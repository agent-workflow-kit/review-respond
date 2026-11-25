"""Golden files: every ``--json`` output and hook payload is validated against its schema.

``check`` validates the document against the schema first (so a golden can never pin an invalid
document), then compares it with ``tests/golden/<name>.json``. ``pytest --update-golden`` writes
the file instead; the diff is then reviewed like any other change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import schemas

GOLDEN = Path(__file__).resolve().parent.parent / "golden"


def render(doc: Any) -> str:
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def check(name: str, actual: Any, schema_id: str, *, update: bool = False) -> None:
    schemas.validate(schema_id, actual)
    path = GOLDEN / f"{name}.json"
    text = render(actual)
    if update:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return
    if not path.exists():
        raise AssertionError(
            f"missing golden file {path}; run pytest --update-golden and review it"
        )
    expected = path.read_text()
    if expected != text:
        raise AssertionError(f"golden file {path} differs from the actual document:\n{text}")


def check_hook(name: str, payload: Any, *, update: bool = False) -> None:
    """A hook payload (stdin of a hook) against ``schemas/hooks/<name>.json``."""
    check(f"hooks/{name}", payload, f"review-respond/hooks/{name}.json", update=update)


def check_cli(name: str, output: Any, *, update: bool = False) -> None:
    """A ``--json`` output against ``schemas/cli/<name>.json``."""
    check(f"cli/{name}", output, f"review-respond/cli/{name}.json", update=update)
