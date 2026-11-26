"""The labels review-respond puts on what it creates (docs/interface.md: Labels).

Forks: ``pr``, ``owner``, ``repo`` and ``round``. Throwaways: ``app``, ``role``, ``round`` and,
for a clean checkout or once a record exists, ``pending``. Jobs: ``pr``, ``round`` and ``owner``.
A label value is at most 256 characters from the agent-spawn alphabet; a PR reference or repo id
that does not fit is left off (warning ``label_skipped``). Labels are not authenticated: nothing
here makes an agent or throwaway "ours"; only the ``forks`` table does (``lineage``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

OWNER: Final = "review-respond"
APP: Final = "review-respond"
MAX_VALUE: Final = 256
ROLES: Final = ("round", "clean")
_VALUE: Final = re.compile(r"[A-Za-z0-9._:/#@+=,-]{1,256}")

Label = tuple[str, str]


@dataclass(frozen=True)
class LabelSet:
    labels: tuple[Label, ...]
    warnings: tuple[str, ...] = ()


def value_ok(value: str) -> bool:
    """True when ``value`` can be a label value (1 to 256 characters of the allowed alphabet)."""
    return _VALUE.fullmatch(value) is not None


def _build(items: list[Label]) -> LabelSet:
    kept = tuple((k, v) for k, v in items if value_ok(v))
    return LabelSet(kept, ("label_skipped",) if len(kept) != len(items) else ())


def lookup_label(pr: str) -> Label | None:
    """The ``pr`` label that finds a PR's original agent, or None when the reference is too long
    or outside the alphabet (such a PR has no lookup)."""
    return ("pr", pr) if value_ok(pr) else None


def fork_labels(*, pr: str, repo_id: str, round_id: str) -> LabelSet:
    return _build([("pr", pr), ("owner", OWNER), ("repo", repo_id), ("round", round_id)])


def throwaway_labels(*, role: str, round_id: str, pending_id: str | None = None) -> LabelSet:
    if role not in ROLES:
        raise ValueError("role is round or clean")
    items: list[Label] = [("app", APP), ("role", role), ("round", round_id)]
    if pending_id is not None:
        items.append(("pending", pending_id))
    return _build(items)


def job_labels(*, pr: str, round_id: str) -> LabelSet:
    return _build([("pr", pr), ("round", round_id), ("owner", OWNER)])


def has_label(labels: object, key: str, value: str | None = None) -> bool:
    """Whether an agent's ``labels`` (a list of ``key=value`` strings) holds ``key`` (with
    ``value``). Splits at the first ``=`` like agent-spawn; anything that is not a string is
    ignored."""
    if not isinstance(labels, list):
        return False
    for item in labels:  # pyright: ignore[reportUnknownVariableType]
        if isinstance(item, str) and "=" in item:
            k, _, v = item.partition("=")
            if k == key and (value is None or v == value):
                return True
    return False
