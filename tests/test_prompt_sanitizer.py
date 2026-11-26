"""The one sanitizer (docs/interface.md: Prompt; issue #20)."""

from __future__ import annotations

import ast
import random
import re
import unicodedata
from pathlib import Path

import pytest

from review_respond import audit, detect, output, prompt, prstate
from review_respond.config import _NO_CONTROLS  # pyright: ignore[reportPrivateUsage]
from review_respond.prompt import sanitize, sanitize_capped
from tests.harness import adversarial


def safe(ch: str) -> bool:
    return unicodedata.category(ch) not in {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}


SRC = Path(__file__).resolve().parent.parent / "src" / "review_respond"


def test_every_hostile_fixture_comes_out_printable() -> None:
    for name, text in adversarial.HOSTILE_COMMENT_TEXTS.items():
        out = sanitize(text, newlines=True)
        assert all(ch == "\n" or safe(ch) for ch in out), name
        out.encode("utf-8")  # no lone surrogate survives


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a\x00b", "a<U+0000>b"),
        ("\x1b[31mred", "<U+001B>[31mred"),
        ("x\u202e", "x<U+202E>"),
        ("p\u200bw", "p<U+200B>w"),
        ("a\ud800b", "a<U+D800>b"),
        ("\x85", "<U+0085>"),
        ("a\ufe0fb", "a<U+FE0F>b"),
        ("\U000e0069", "<U+E0069>"),
        ("e\u0301é 中", "e\u0301é 中"),
    ],
)
def test_marks(text: str, expected: str) -> None:
    assert sanitize(text) == expected


def test_newline_handling() -> None:
    assert sanitize("a\nb") == "a<U+000A>b"
    assert sanitize("a\nb", newlines=True) == "a\nb"
    assert sanitize("a\r\nb", newlines=True) == "a\nb"
    assert sanitize("a\rb", newlines=True) == "a<U+000D>b"
    assert sanitize("a\u2028b", newlines=True) == "a<U+2028>b"
    assert sanitize("a\tb", newlines=True) == "a<U+0009>b"


def test_cut_never_splits_a_marker() -> None:
    assert sanitize("ab\x00cd", max_chars=3) == "ab"
    assert sanitize("ab\x00cd", max_chars=10) == "ab<U+0000>"
    assert sanitize("abc", max_chars=0) == ""


def test_capped_reports_the_cut_bytes() -> None:
    out, cut = sanitize_capped("a" * 10 + "é", max_bytes=10)
    assert (out, cut) == ("a" * 10, 2)
    out, cut = sanitize_capped("ab\x00", max_bytes=3)
    assert (out, cut) == ("ab", 1)
    assert sanitize_capped("short", max_bytes=100) == ("short", 0)


def test_fuzz_properties() -> None:
    rng = random.Random(20)
    pool = (
        [chr(c) for c in range(0, 0x2FF)]
        + [chr(c) for c in range(0x2000, 0x2070)]
        + [chr(c) for c in (0xD800, 0xDC80, 0xFEFF, 0xFE0F, 0xE0001, 0xE0100, 0xFFFF, 0x10FFFF)]
        + list("<U+>/untrusted\n\r")
    )
    for _ in range(400):
        text = "".join(rng.choice(pool) for _ in range(rng.randrange(0, 60)))
        for newlines in (False, True):
            out = sanitize(text, newlines=newlines)
            assert sanitize(out, newlines=newlines) == out  # a fixed point
            assert all(safe(ch) or (newlines and ch == "\n") for ch in out)
            out.encode("utf-8")
            limit = rng.randrange(0, 40)
            cut = sanitize(text, newlines=newlines, max_chars=limit)
            assert len(cut) <= limit
            assert out.startswith(cut)
            assert not re.search(r"<U\+[0-9A-F]*$", cut) or cut == out[: len(cut)]
            capped, omitted = sanitize_capped(text, max_bytes=limit, newlines=newlines)
            assert len(capped.encode()) <= limit
            assert out.startswith(capped)
            assert (omitted == 0) == (capped == out)


def test_parsers_reject_nothing_the_sanitizer_lets_through() -> None:
    """Every character the config and detect parsers reject is marked, except nothing: the
    joiners U+200C/U+200D (which parsers allow) are marked too, the conservative side."""
    for code in range(0x110000):
        ch = chr(code)
        rejected = _NO_CONTROLS.search(ch) or detect._UNSAFE_LINE_RE.search(ch)  # pyright: ignore[reportPrivateUsage]
        if rejected:
            assert sanitize(ch) != ch, hex(code)


def test_joiners_are_marked() -> None:
    assert sanitize("a\u200db\u200cc") == "a<U+200D>b<U+200C>c"


def test_output_and_audit_and_prstate_use_it() -> None:
    assert output.sanitize_line is prompt.sanitize_line
    assert audit.clean_text("a\u202eb\x00", 100) == sanitize("a\u202eb\x00")
    assert audit.clean_text("ab\x00", 3) == "ab"
    assert prstate._untrusted("a\nb\x00") == "a\nb<U+0000>"  # pyright: ignore[reportPrivateUsage]


def test_no_second_copy_of_the_sanitizer() -> None:
    """Only prompt.py may classify characters or build ``<U+XXXX>`` markers."""
    offenders: list[str] = []
    for path in sorted(SRC.glob("*.py")):
        if path.name == "prompt.py":
            continue
        source = path.read_text()
        tree = ast.parse(source)
        docstrings = {
            id(n.body[0].value)
            for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))
            and n.body
            and isinstance(n.body[0], ast.Expr)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "category":
                offenders.append(f"{path.name}: unicodedata.category")
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "<U+" in node.value and id(node) not in docstrings:
                    offenders.append(f"{path.name}: marker literal")
            if isinstance(node, ast.JoinedStr) and "U+" in ast.unparse(node):
                offenders.append(f"{path.name}: marker f-string")
            if isinstance(node, ast.FunctionDef) and node.name.startswith("sanitize"):
                offenders.append(f"{path.name}: def {node.name}")
    assert offenders == []
