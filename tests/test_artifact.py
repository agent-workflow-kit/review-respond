"""Artifact validation (docs/interface.md: Artifact; issue #22)."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import random
import socket
import threading
import unicodedata
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from review_respond import artifact as art
from review_respond.artifact import (
    ArtifactResult,
    FrozenRef,
    check_path_state,
    commit_message_problem,
    parse_artifact_bytes,
    reply_problem,
    validate_artifact,
)
from review_respond.outcomes import FAILURE_DETAILS, RunEnd
from tests.harness import adversarial
from tests.harness.schemas import OWN_SCHEMAS, registry

MARKER = "respond-to-review/v1:3f9a"
IDS = frozenset({"gh:rc:AAA", "gh:rc:BBB", "gh:rc:CCC"})
REPLY_BYTES = 8192


def good() -> dict[str, Any]:
    return {
        "skill_marker": MARKER,
        "replies": [
            {"comment_id": "gh:rc:AAA", "body": "Done, thanks.\nSee the new test."},
            {"comment_id": "gh:rc:BBB", "body": "Kept: it is intended."},
        ],
        "changed_files": ["src/a.py", "docs/Guide.md"],
        "deleted_files": ["old/b.py"],
        "commit_message": "fix: handle the empty case\n\nWhy: it crashed.",
    }


def dump(obj: object) -> bytes:
    return json.dumps(obj).encode()


def parse(raw: bytes, *, ids: frozenset[str] = IDS) -> ArtifactResult:
    return parse_artifact_bytes(raw, delivered_ids=ids, marker=MARKER, reply_bytes=REPLY_BYTES)


def invalid(result: ArtifactResult, detail: str) -> None:
    assert result.failure == "artifact_invalid", result.message
    assert result.detail == detail, result.message
    assert result.artifact is None
    assert not result.ok


def freeze(tmp_path: Path, raw: bytes, name: str = "artifact.json") -> FrozenRef:
    path = tmp_path / name
    path.write_bytes(raw)
    return FrozenRef(path=str(path), size=len(raw), sha256=hashlib.sha256(raw).hexdigest())


def run(ref: FrozenRef | None, *, implicit: bool = False) -> ArtifactResult:
    return validate_artifact(
        implicit=implicit, artifact=ref, delivered_ids=IDS, marker=MARKER, reply_bytes=REPLY_BYTES
    )


# ---- golden valid artifacts ---------------------------------------------------------------


def test_golden_valid_artifact(tmp_path: Path) -> None:
    raw = dump(good())
    result = run(freeze(tmp_path, raw))
    assert result.ok and result.failure is None and result.detail is None
    assert result.raw == raw
    a = result.artifact
    assert a is not None
    assert a.skill_marker == MARKER
    assert [(r.comment_id, r.body) for r in a.replies] == [
        ("gh:rc:AAA", "Done, thanks.\nSee the new test."),
        ("gh:rc:BBB", "Kept: it is intended."),
    ]
    assert a.changed_files == ("src/a.py", "docs/Guide.md")
    assert a.deleted_files == ("old/b.py",)


def test_valid_minimal_artifact() -> None:
    doc = good() | {"replies": [], "changed_files": [], "deleted_files": [], "commit_message": ""}
    assert parse(dump(doc)).ok


def test_valid_reply_only_round_leaves_some_delivered_ids_unanswered() -> None:
    doc = good() | {"replies": [{"comment_id": "gh:rc:CCC", "body": "ok"}]}
    assert parse(dump(doc)).ok  # a delivered comment without a reply is allowed (shown by `show`)


def test_valid_unicode_bodies_paths_and_message() -> None:
    doc = good()
    doc["replies"] = [{"comment_id": "gh:rc:AAA", "body": "café 中文 \U0001f468‍\U0001f469"}]
    doc["changed_files"] = ["déjà/中.txt", "é/x"]
    doc["commit_message"] = "fix: ü"
    assert parse(dump(doc)).ok


def test_validate_golden_json_text_with_unicode_escapes() -> None:
    raw = (
        b'{"skill_marker": "'
        + MARKER.encode()
        + b'", "replies": [{"comment_id": "gh:rc:AAA", "body": "\\u00e9\\n"}], '
        + b'"changed_files": [], "deleted_files": [], "commit_message": ""}'
    )
    result = parse(raw)
    assert result.ok and result.artifact is not None
    assert result.artifact.replies[0].body == "é\n"


# ---- steps before the file: implicit, missing ---------------------------------------------


def test_implicit_done_wins_over_everything(tmp_path: Path) -> None:
    result = run(freeze(tmp_path, dump(good())), implicit=True)
    assert (result.failure, result.detail) == ("implicit_done", None)
    assert run(None, implicit=True).failure == "implicit_done"


def test_missing_artifact() -> None:
    result = run(None)
    assert (result.failure, result.detail) == ("artifact_missing", None)
    assert result.artifact is None


# ---- frozen_file --------------------------------------------------------------------------


def test_frozen_file_missing(tmp_path: Path) -> None:
    invalid(run(FrozenRef(str(tmp_path / "nope.json"), 10, "0" * 64)), "frozen_file")


def test_frozen_file_relative_path_refused(tmp_path: Path) -> None:
    ref = freeze(tmp_path, dump(good()))
    rel = os.path.relpath(ref.path)
    invalid(run(FrozenRef(rel, ref.size, ref.sha256)), "frozen_file")


def test_frozen_file_nul_in_path() -> None:
    invalid(run(FrozenRef("/tmp/a\x00b", 3, "0" * 64)), "frozen_file")


def test_frozen_file_symlink_refused(tmp_path: Path) -> None:
    real = freeze(tmp_path, dump(good()), "real.json")
    link = tmp_path / "link.json"
    link.symlink_to(real.path)
    invalid(run(FrozenRef(str(link), real.size, real.sha256)), "frozen_file")


def test_frozen_file_dangling_and_looping_symlink(tmp_path: Path) -> None:
    (tmp_path / "dangling.json").symlink_to(tmp_path / "gone")
    (tmp_path / "loop.json").symlink_to("loop.json")
    for name in ("dangling.json", "loop.json"):
        invalid(run(FrozenRef(str(tmp_path / name), 5, "0" * 64)), "frozen_file")


def test_frozen_file_fifo_refused_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    box: list[ArtifactResult] = []
    worker = threading.Thread(
        target=lambda: box.append(run(FrozenRef(str(fifo), 5, "0" * 64))), daemon=True
    )
    worker.start()
    worker.join(timeout=10)
    if worker.is_alive():  # a daemon thread: a regression fails here instead of hanging the suite
        pytest.fail("validating a FIFO blocked")
    assert len(box) == 1
    invalid(box[0], "frozen_file")


def test_frozen_file_socket_refused(tmp_path: Path) -> None:
    sock_path = tmp_path / "s"
    s = socket.socket(socket.AF_UNIX)
    try:
        s.bind(str(sock_path))
        invalid(run(FrozenRef(str(sock_path), 5, "0" * 64)), "frozen_file")
    finally:
        s.close()


def test_frozen_file_directory_refused(tmp_path: Path) -> None:
    invalid(run(FrozenRef(str(tmp_path), 5, "0" * 64)), "frozen_file")


def test_frozen_file_dev_null_refused() -> None:
    invalid(run(FrozenRef("/dev/null", 0, hashlib.sha256(b"").hexdigest())), "frozen_file")


def test_frozen_file_size_mismatch_both_ways(tmp_path: Path) -> None:
    ref = freeze(tmp_path, dump(good()))
    invalid(run(FrozenRef(ref.path, ref.size - 1, ref.sha256)), "frozen_file")
    invalid(run(FrozenRef(ref.path, ref.size + 1, ref.sha256)), "frozen_file")


def test_frozen_file_sha_mismatch(tmp_path: Path) -> None:
    ref = freeze(tmp_path, dump(good()))
    invalid(run(FrozenRef(ref.path, ref.size, "0" * 64)), "frozen_file")


def test_frozen_file_huge_file_refused_before_reading(tmp_path: Path) -> None:
    path = tmp_path / "huge.json"
    with path.open("wb") as handle:
        handle.truncate(art.MAX_ARTIFACT_BYTES + 1)  # sparse: no disk, no memory
    invalid(run(FrozenRef(str(path), art.MAX_ARTIFACT_BYTES + 1, "0" * 64)), "frozen_file")


def test_frozen_file_one_byte_over_the_cap_is_refused_with_its_real_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    size = art.MAX_ARTIFACT_BYTES + 1
    path = tmp_path / "huge.json"
    sha = hashlib.sha256()
    with path.open("wb") as handle:
        handle.truncate(size)  # sparse: reads as zeros
    for _ in range(size // 65536):
        sha.update(bytes(65536))
    sha.update(bytes(size % 65536))

    def no_read(*_a: Any, **_k: Any) -> bytes:
        raise AssertionError("the file must not be read")

    monkeypatch.setattr(os, "read", no_read)
    result = run(FrozenRef(str(path), size, sha.hexdigest()))
    invalid(result, "frozen_file")
    assert "size" in result.message


def test_frozen_file_exactly_at_cap_is_read(tmp_path: Path) -> None:
    body = dump(good())
    raw = body + b" " * (art.MAX_ARTIFACT_BYTES - len(body))  # trailing whitespace is valid JSON
    assert len(raw) == art.MAX_ARTIFACT_BYTES
    result = run(freeze(tmp_path, raw))
    assert result.ok, result.message
    assert result.raw == raw


def test_frozen_file_read_in_several_chunks(tmp_path: Path) -> None:
    body = dump(good())
    raw = body + b" " * (3 * 65536 + 17)
    assert run(freeze(tmp_path, raw)).ok


def test_frozen_file_negative_size_message(tmp_path: Path) -> None:
    ref = freeze(tmp_path, dump(good()))
    result = run(FrozenRef(ref.path, -1, ref.sha256))
    invalid(result, "frozen_file")
    assert "size" in result.message


def test_frozen_file_negative_or_bad_ref_fields(tmp_path: Path) -> None:
    ref = freeze(tmp_path, dump(good()))
    invalid(run(FrozenRef(ref.path, -1, ref.sha256)), "frozen_file")
    invalid(run(FrozenRef(ref.path, ref.size, "ABC")), "frozen_file")
    invalid(run(FrozenRef("", ref.size, ref.sha256)), "frozen_file")


def test_frozen_file_grown_after_stat_is_not_read_past_size(tmp_path: Path) -> None:
    """A file whose real length differs from the reported size is refused, not trusted."""
    raw = dump(good())
    path = tmp_path / "a.json"
    path.write_bytes(raw + b" " * 50)
    ref = FrozenRef(str(path), len(raw), hashlib.sha256(raw).hexdigest())
    invalid(run(ref), "frozen_file")


class _FakeStat:
    """An fstat result that claims a regular file of a chosen size."""

    def __init__(self, size: int) -> None:
        self.st_mode = 0o100600
        self.st_size = size


def test_read_guard_stat_size_alone(tmp_path: Path) -> None:
    ref = freeze(tmp_path, dump(good()))
    result = run(FrozenRef(ref.path, ref.size + 3, ref.sha256))
    invalid(result, "frozen_file")
    assert "size differs" in result.message


def test_read_guard_length_after_read_alone_short_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fstat claims the reported size, but the file is shorter when read."""
    raw = dump(good())
    ref = freeze(tmp_path, raw)
    monkeypatch.setattr(os, "fstat", lambda _fd: _FakeStat(ref.size + 5))
    result = run(FrozenRef(ref.path, ref.size + 5, ref.sha256))
    invalid(result, "frozen_file")
    assert "length differs" in result.message


def test_read_guard_length_after_read_alone_grown_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fstat claims the reported size, but the file grew: one byte past it is read, then refused."""
    raw = dump(good())
    path = tmp_path / "grown.json"
    path.write_bytes(raw + b" " * 50)
    monkeypatch.setattr(os, "fstat", lambda _fd: _FakeStat(len(raw)))
    result = run(FrozenRef(str(path), len(raw), hashlib.sha256(raw).hexdigest()))
    invalid(result, "frozen_file")
    assert "length differs" in result.message


def test_read_loop_is_bounded_by_the_reported_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "long.json"
    path.write_bytes(b"x" * 300_000)
    monkeypatch.setattr(os, "fstat", lambda _fd: _FakeStat(100_000))
    real_read = os.read
    returned: list[int] = []

    def spy(fd: int, n: int) -> bytes:
        chunk = real_read(fd, n)
        returned.append(len(chunk))
        return chunk

    monkeypatch.setattr(os, "read", spy)
    result = run(FrozenRef(str(path), 100_000, "0" * 64))
    invalid(result, "frozen_file")
    assert sum(returned) == 100_001  # never more than the reported size plus one byte
    assert max(returned) <= 65536


def test_frozen_file_not_readable(tmp_path: Path) -> None:
    ref = freeze(tmp_path, dump(good()))
    os.chmod(ref.path, 0)
    try:
        if os.access(ref.path, os.R_OK):
            pytest.skip("running with privileges that ignore modes")
        invalid(run(ref), "frozen_file")
    finally:
        os.chmod(ref.path, 0o600)


def test_hard_linked_artifact_file_is_still_read(tmp_path: Path) -> None:
    """The frozen file's link count is not part of the interface; only paths in the lists are."""
    ref = freeze(tmp_path, dump(good()))
    os.link(ref.path, tmp_path / "second")
    assert run(ref).ok


# ---- json ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"   ",
        b"not json",
        b"{",
        b'{"a": 1,}',
        b"[]\x00",
        b'{"skill_marker": NaN}',
        b'{"skill_marker": Infinity}',
        b"\xef\xbb\xbf" + dump(good()),  # BOM
        b"\xff\xfe{\x00}\x00",  # UTF-16
        b"\xc3\x28",  # invalid UTF-8
        b'{"skill_marker": "a\xed\xa0\x80"}',  # encoded surrogate
        dump(good()) + b"\n{}",  # trailing data
        b'{"skill_marker": "x\ty"}',  # raw control character in a string
        b'{"a": 1e999999}',
        b"1" * 5000,  # integer too large to convert
    ],
)
def test_json_rejects(raw: bytes) -> None:
    invalid(parse(raw), "json")


def test_json_duplicate_keys_refused() -> None:
    text = dump(good()).decode()
    dup = text[:-1] + ', "commit_message": "second"}'
    invalid(parse(dup.encode()), "json")


def test_json_duplicate_key_in_nested_object() -> None:
    raw = (
        b'{"skill_marker": "'
        + MARKER.encode()
        + b'", "replies": [{"comment_id": "gh:rc:AAA", "body": "a", "body": "b"}], '
        + b'"changed_files": [], "deleted_files": [], "commit_message": ""}'
    )
    invalid(parse(raw), "json")


def test_json_duplicate_keys_that_differ_only_by_escape() -> None:
    raw = b'{"commit_message": "a", "commit\\u005fmessage": "b"}'
    invalid(parse(raw), "json")


def test_json_lone_surrogate_escape_refused() -> None:
    doc = good()
    text = dump(doc).decode().replace("Done, thanks.", "\\ud800")
    invalid(parse(text.encode()), "json")


@pytest.mark.parametrize("depth", [art.MAX_DEPTH + 1, 50, 5000, 400_000])
def test_json_deep_nesting_refused(depth: int) -> None:
    raw = b"[" * depth + b"]" * depth
    invalid(parse(raw), "json")


def test_json_deep_nesting_in_a_field_refused() -> None:
    nest = "[" * 9 + "]" * 9
    raw = f'{{"skill_marker": "{MARKER}", "replies": {nest}}}'.encode()
    invalid(parse(raw), "json")


def test_json_unterminated_deep_nesting_is_json_not_a_crash() -> None:
    invalid(parse(b'{"a":' * 100_000), "json")


def test_json_brackets_inside_strings_do_not_count_as_depth() -> None:
    doc = good()
    doc["commit_message"] = "[" * 1500 + "{" * 1500 + '\\"' + "]"
    assert parse(dump(doc)).ok


def test_json_depth_exactly_eight_passes_the_json_step() -> None:
    nest = "[" * 5 + "]" * 5  # object (1) + the field array (2) + 5 = 7 ... within the cap
    raw = f'{{"skill_marker": "{MARKER}", "replies": [], "changed_files": {nest}}}'.encode()
    result = parse(raw)
    assert result.detail != "json"  # it fails later, in the schema


# ---- schema -------------------------------------------------------------------------------


def test_schema_top_level_not_an_object() -> None:
    for raw in (b"[]", b'"x"', b"1", b"null", b"true"):
        invalid(parse(raw), "schema")


@pytest.mark.parametrize(
    "key", ["skill_marker", "replies", "changed_files", "deleted_files", "commit_message"]
)
def test_schema_missing_required_key(key: str) -> None:
    doc = good()
    del doc[key]
    invalid(parse(dump(doc)), "schema")


def test_schema_unknown_key_refused() -> None:
    invalid(parse(dump(good() | {"extra": 1})), "schema")
    invalid(parse(dump(good() | {"Skill_Marker": "x"})), "schema")


def test_schema_unknown_key_in_reply_refused() -> None:
    doc = good()
    doc["replies"][0]["resolve"] = True
    invalid(parse(dump(doc)), "schema")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("skill_marker", 5),
        ("skill_marker", ""),
        ("skill_marker", "x" * 257),
        ("skill_marker", None),
        ("replies", {}),
        ("replies", "x"),
        ("replies", [1]),
        ("replies", [{"comment_id": "gh:rc:AAA"}]),
        ("replies", [{"body": "x"}]),
        ("replies", [{"comment_id": 5, "body": "x"}]),
        ("replies", [{"comment_id": "gh:rc:AAA", "body": 5}]),
        ("replies", [{"comment_id": "", "body": "x"}]),
        ("replies", [{"comment_id": "gh:rc:AAA", "body": "x"}] * 1001),
        ("changed_files", "a.py"),
        ("changed_files", [1]),
        ("changed_files", [None]),
        ("changed_files", [["a"]]),
        ("deleted_files", {"a": 1}),
        ("commit_message", 5),
        ("commit_message", None),
        ("commit_message", ["x"]),
    ],
)
def test_schema_type_and_shape_errors(key: str, value: object) -> None:
    invalid(parse(dump(good() | {key: value})), "schema")


@pytest.mark.parametrize(
    "comment_id",
    [
        "-x",
        " gh:rc:AAA",
        "gh:rc:AAA ",
        "gh rc",
        "gh:rc:AAA\n",
        "x" * 257,
        "gа:rc:AAA",
        "gh:rc:A\x00",
    ],
)
def test_schema_comment_id_pattern(comment_id: str) -> None:
    doc = good()
    doc["replies"] = [{"comment_id": comment_id, "body": "x"}]
    invalid(parse(dump(doc), ids=frozenset({comment_id})), "schema")


def test_schema_bool_is_not_a_string_or_number() -> None:
    invalid(parse(dump(good() | {"commit_message": True})), "schema")


# ---- skill_marker -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "marker",
    [MARKER + " ", " " + MARKER, MARKER.upper(), MARKER[:-1], "other", "​" + MARKER, MARKER + "\n"],
)
def test_skill_marker_must_match_exactly(marker: str) -> None:
    invalid(parse(dump(good() | {"skill_marker": marker})), "skill_marker")


def test_skill_marker_forged_in_the_wrong_place_does_not_count() -> None:
    doc = good() | {"skill_marker": "none", "commit_message": MARKER}
    invalid(parse(dump(doc)), "skill_marker")


def test_skill_marker_checked_before_replies() -> None:
    doc = good() | {"skill_marker": "nope", "replies": [{"comment_id": "gh:rc:ZZZ", "body": "x"}]}
    invalid(parse(dump(doc)), "skill_marker")


# ---- replies ------------------------------------------------------------------------------


def test_replies_unknown_comment_id() -> None:
    doc = good() | {"replies": [{"comment_id": "gh:rc:ZZZ", "body": "x"}]}
    invalid(parse(dump(doc)), "replies")


def test_replies_forged_ids_look_alikes_are_unknown() -> None:
    for forged in ("gh:rc:AAA.", "gh:rc:aaa", "gh:rc:AAA=", "GH:rc:AAA", "gh:rc:AAA/x"):
        doc = good() | {"replies": [{"comment_id": forged, "body": "x"}]}
        invalid(parse(dump(doc)), "replies")


def test_replies_duplicate_id() -> None:
    doc = good() | {"replies": [{"comment_id": "gh:rc:AAA", "body": "x"}] * 2}
    invalid(parse(dump(doc)), "replies")


def test_replies_no_delivered_ids_means_no_replies_allowed() -> None:
    invalid(parse(dump(good()), ids=frozenset()), "replies")


@pytest.mark.parametrize("body", ["", " ", "\n\n", "\t \r\n"])
def test_replies_empty_body(body: str) -> None:
    doc = good() | {"replies": [{"comment_id": "gh:rc:AAA", "body": body}]}
    invalid(parse(dump(doc)), "replies")


def test_replies_body_over_the_byte_limit() -> None:
    doc = good() | {"replies": [{"comment_id": "gh:rc:AAA", "body": "a" * (REPLY_BYTES + 1)}]}
    invalid(parse(dump(doc)), "replies")


def test_replies_body_at_the_byte_limit_is_valid() -> None:
    doc = good() | {"replies": [{"comment_id": "gh:rc:AAA", "body": "a" * REPLY_BYTES}]}
    assert parse(dump(doc)).ok


def test_replies_limit_counts_utf8_bytes_not_characters() -> None:
    body = "é" * (
        REPLY_BYTES // 2 + 1
    )  # 2 bytes each: over the limit in bytes, under in characters
    doc = good() | {"replies": [{"comment_id": "gh:rc:AAA", "body": body}]}
    invalid(parse(dump(doc)), "replies")


def test_replies_body_over_the_schema_character_cap_with_a_huge_limit() -> None:
    body = "a" * 65537
    doc = good() | {"replies": [{"comment_id": "gh:rc:AAA", "body": body}]}
    result = parse_artifact_bytes(dump(doc), delivered_ids=IDS, marker=MARKER, reply_bytes=1 << 20)
    invalid(result, "replies")


@pytest.mark.parametrize(
    "ch",
    ["\x00", "\x01", "\x08", "\x0b", "\x0c", "\x0e", "\x1b", "\x1f", "\x7f", "\x80", "\x85"]
    + ["\x9b", "\x9f"],
)
def test_replies_control_characters_refused(ch: str) -> None:
    doc = good() | {"replies": [{"comment_id": "gh:rc:AAA", "body": f"a{ch}b"}]}
    invalid(parse(dump(doc)), "replies")


def test_replies_newline_tab_and_crlf_are_allowed() -> None:
    doc = good() | {"replies": [{"comment_id": "gh:rc:AAA", "body": "a\n\tb\r\nc"}]}
    assert parse(dump(doc)).ok


def test_reply_problem_is_shared_with_edit() -> None:
    assert reply_problem("fine", 10) is None
    assert reply_problem("", 10) is not None
    assert reply_problem("a" * 11, 10) is not None
    assert reply_problem("a\x00", 10) is not None


# ---- paths --------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(adversarial.HOSTILE_ARTIFACT_PATHS))
@pytest.mark.parametrize("field", ["changed_files", "deleted_files"])
def test_paths_hostile_fixtures_refused(name: str, field: str) -> None:
    path = adversarial.HOSTILE_ARTIFACT_PATHS[name]
    doc = good() | {field: [path]}
    result = parse(dump(doc))
    assert not result.ok
    assert (result.failure, result.detail) == ("artifact_invalid", "paths") or (
        name == "empty" and result.detail in ("paths", "schema")
    ), (name, result.message)


@pytest.mark.parametrize(
    "path",
    [
        "a/b/",
        "/a",
        "./a",
        "a/.",
        "a/..",
        "..",
        ".",
        "a\\b",
        "a:b",
        ".git",
        ".GiT/x",
        "x/.GIT",
        ".git ",
        ".git.",
        ".git. .",
        "x/.git /y",
        "GIT~1",
        "git~12/x",
        "sub/git~1",
    ],
)
def test_paths_rule_violations(path: str) -> None:
    invalid(parse(dump(good() | {"changed_files": [path]})), "paths")


@pytest.mark.parametrize("path", ["．git/x", ".gｉt/x", "x/﹒git"])
def test_paths_unicode_compat_forms_of_dot_git(path: str) -> None:
    # fullwidth and small full stops and fullwidth letters fold to ".git" under NFKC
    invalid(parse(dump(good() | {"changed_files": [path]})), "paths")


@pytest.mark.parametrize(
    "path",
    [
        "a\x00b",
        "a\nb",
        "a\tb",
        "a\x7fb",
        "a\x85b",
        "a‮b",  # bidi override
        "a​b",  # zero width space
        "a b",
        "a﻿b",
        "a️b",  # variation selector
        "a\U000e0041b",  # tag character
        "ab",  # private use
        "a\ud800b",
    ],
)
def test_paths_invisible_and_control_characters(path: str) -> None:
    raw = dump(good() | {"changed_files": [path]})
    result = parse(raw)
    assert not result.ok
    assert result.detail in ("paths", "json")  # a lone surrogate is a json problem


def test_paths_leading_dash_refused() -> None:
    invalid(parse(dump(good() | {"changed_files": ["-rf"]})), "paths")
    invalid(parse(dump(good() | {"deleted_files": ["--force"]})), "paths")
    assert parse(dump(good() | {"changed_files": ["sub/-rf"]})).ok


def test_paths_length_limits() -> None:
    ok_name = "n" * 255
    assert parse(dump(good() | {"changed_files": [ok_name]})).ok
    invalid(parse(dump(good() | {"changed_files": ["n" * 256]})), "paths")
    invalid(parse(dump(good() | {"changed_files": ["é" * 128]})), "paths")  # 256 bytes
    long_ok = "/".join(["d" * 100] * 40)  # 40*100 + 39 = 4039 bytes
    assert parse(dump(good() | {"changed_files": [long_ok]})).ok
    invalid(parse(dump(good() | {"changed_files": ["/".join(["d" * 100] * 41)]})), "paths")  # 4140


def test_paths_count_limit() -> None:
    many = [f"f{i}.txt" for i in range(1001)]
    invalid(parse(dump(good() | {"changed_files": many})), "paths")
    invalid(parse(dump(good() | {"deleted_files": many})), "paths")
    assert parse(dump(good() | {"changed_files": many[:1000]})).ok


def test_paths_exact_duplicate_refused() -> None:
    invalid(parse(dump(good() | {"changed_files": ["a.py", "a.py"]})), "paths")


def test_paths_case_fold_duplicates_refused() -> None:
    invalid(parse(dump(good() | {"changed_files": ["Readme", "README"]})), "paths")
    invalid(
        parse(dump(good() | {"changed_files": ["d/ß", "d/SS"]})), "paths"
    )  # sharp s folds to ss


def test_paths_unicode_normalization_duplicates_refused() -> None:
    invalid(parse(dump(good() | {"changed_files": ["café", "café"]})), "paths")
    invalid(parse(dump(good() | {"changed_files": ["cafÉ", "café"]})), "paths")


def test_key_applies_nfc_then_casefold_then_nfc() -> None:
    precomposed = "\u0390"  # iota with dialytika and tonos
    decomposed = "\u03b9\u0308\u0301"
    assert art._key(f"d/{precomposed}") == art._key(f"d/{decomposed}")  # pyright: ignore[reportPrivateUsage]
    invalid(parse(dump(good() | {"changed_files": [precomposed, decomposed]})), "paths")
    invalid(parse(dump(good() | {"changed_files": ["d/\u212b", "d/\u00e5"]})), "paths")


def test_fold_git_runs_nfkc_before_and_after_casefold() -> None:
    fold = art._fold_git  # pyright: ignore[reportPrivateUsage]
    # before: a modifier letter capital G only becomes "G" under NFKC, then must fold to "g"
    assert fold(".\u1d33it") == ".git"
    # after: casefolding "\u0390" yields text that is not NFKC-stable until the second pass
    for ch in ("\u0390", "\u03b0", "\u1e96", "\u01f0"):
        out = fold(ch)
        assert out == unicodedata.normalize("NFKC", out)
    invalid(parse(dump(good() | {"changed_files": [".\u1d33it/x"]})), "paths")


def test_paths_in_both_lists_refused() -> None:
    doc = good() | {"changed_files": ["a.py"], "deleted_files": ["A.py"]}
    invalid(parse(dump(doc)), "paths")


def test_paths_prefix_directory_of_another_refused() -> None:
    invalid(parse(dump(good() | {"changed_files": ["a", "a/b"]})), "paths")
    invalid(parse(dump(good() | {"changed_files": ["a/b"], "deleted_files": ["A"]})), "paths")
    invalid(parse(dump(good() | {"changed_files": ["x/y/z", "x/Y"]})), "paths")
    assert parse(
        dump(good() | {"changed_files": ["a", "ab", "a.b"]})
    ).ok  # a string prefix is not a directory


def test_paths_unicode_in_ids_does_not_pass_the_pattern() -> None:
    doc = good() | {"replies": [{"comment_id": "gh:rc:AАA", "body": "x"}]}
    invalid(parse(dump(doc), ids=frozenset({"gh:rc:AАA"})), "schema")


# ---- commit_message -----------------------------------------------------------------------


def test_commit_message_limits() -> None:
    assert parse(dump(good() | {"commit_message": "a" * 4096})).ok
    invalid(parse(dump(good() | {"commit_message": "a" * 4097})), "commit_message")
    invalid(parse(dump(good() | {"commit_message": "é" * 2049})), "commit_message")  # 4098 bytes
    assert parse(dump(good() | {"commit_message": "é" * 2048})).ok


@pytest.mark.parametrize(
    "ch",
    ["\x00", "\x01", "\x08", "\x0b", "\x0c", "\x0e", "\x1b", "\x1f", "\x7f", "\x80"]
    + ["\x9b", "\x9f"],
)
def test_commit_message_control_characters(ch: str) -> None:
    invalid(parse(dump(good() | {"commit_message": f"a{ch}b"})), "commit_message")


def test_commit_message_tab_newline_and_cr_are_allowed() -> None:
    assert parse(dump(good() | {"commit_message": "a\tb\nc\r\nd"})).ok


INVISIBLE_ONLY = [
    "\u200b", "\ufeff", "\u200b\ufeff", " \u200b\n\ufeff\t", "\u2028", "\u2029", "\u200d",
    "\ue000", "\u0378", "\x00", "\u3164", "\u2800", "\u115f\u1160\uffa0",
]  # fmt: skip
BIDI_AND_SEPARATORS = [
    "\u200e", "\u200f", "\u061c", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
    "\u2066", "\u2067", "\u2068", "\u2069", "\u2028", "\u2029",
]  # fmt: skip


@pytest.mark.parametrize("body", INVISIBLE_ONLY)
def test_replies_invisible_only_body_is_empty(body: str) -> None:
    assert reply_problem(body, REPLY_BYTES) == "the reply is empty"
    doc = good() | {"replies": [{"comment_id": "gh:rc:AAA", "body": body}]}
    invalid(parse(dump(doc)), "replies")


def test_reply_problem_checks_size_before_emptiness() -> None:
    assert reply_problem("\u200b" * 11, 10) == "the reply is larger than 10 bytes"
    assert reply_problem("\u200b" * 3, 10) == "the reply is empty"


@pytest.mark.parametrize("ch", BIDI_AND_SEPARATORS)
def test_replies_bidi_and_line_separators_refused(ch: str) -> None:
    problem = reply_problem(f"a{ch}b", REPLY_BYTES)
    assert problem is not None and "bidirectional" in problem
    doc = good() | {"replies": [{"comment_id": "gh:rc:AAA", "body": f"a{ch}b"}]}
    invalid(parse(dump(doc)), "replies")


@pytest.mark.parametrize("ch", BIDI_AND_SEPARATORS)
def test_commit_message_bidi_and_line_separators_refused(ch: str) -> None:
    problem = commit_message_problem(f"fix: a{ch}b")
    assert problem is not None and "bidirectional" in problem
    invalid(parse(dump(good() | {"commit_message": f"fix: a{ch}b"})), "commit_message")


def test_legitimate_unicode_still_passes_reply_and_message() -> None:
    text = "caf\u00e9 \u4e2d\u6587 \U0001f468\u200d\U0001f469\u200d\U0001f467 na\u00efve"
    assert reply_problem(text, REPLY_BYTES) is None
    assert commit_message_problem(text) is None
    assert reply_problem("\U0001f468\u200d\U0001f469", REPLY_BYTES) is None
    doc = good() | {
        "replies": [{"comment_id": "gh:rc:AAA", "body": text}],
        "commit_message": text,
    }
    assert parse(dump(doc)).ok


def test_reply_problem_lone_surrogate_is_not_utf8() -> None:
    problem = reply_problem("a\ud800", REPLY_BYTES)
    assert problem is not None and "not valid UTF-8" in problem


def test_commit_message_problem_is_shared_with_edit() -> None:
    assert commit_message_problem("ok\n\nbody") is None
    assert commit_message_problem("a" * 4097) is not None
    assert commit_message_problem("a\x00") is not None


# ---- order of the steps -------------------------------------------------------------------


def test_steps_run_in_the_interface_order() -> None:
    doc = good()
    doc["replies"] = [{"comment_id": "gh:rc:ZZZ", "body": "x"}]
    doc["changed_files"] = ["../x"]
    doc["commit_message"] = "a" * 5000
    invalid(parse(dump(doc)), "replies")
    doc["replies"] = []
    invalid(parse(dump(doc)), "paths")
    doc["changed_files"] = []
    invalid(parse(dump(doc)), "commit_message")


# ---- every detail is closed and reachable ------------------------------------------------


def test_every_detail_but_path_state_comes_out_of_validate(tmp_path: Path) -> None:
    seen: set[str] = set()
    seen.add(str(run(FrozenRef(str(tmp_path / "x"), 1, "0" * 64)).detail))
    for raw in (
        b"{",
        dump(good() | {"extra": 1}),
        dump(good() | {"skill_marker": "z"}),
        dump(good() | {"replies": [{"comment_id": "gh:rc:Q", "body": "x"}]}),
        dump(good() | {"changed_files": ["/a"]}),
        dump(good() | {"commit_message": "a" * 9000}),
    ):
        seen.add(str(parse(raw).detail))
    assert seen == set(FAILURE_DETAILS) - {"path_state"}


def test_failures_map_onto_the_run_end_contract(tmp_path: Path) -> None:
    results = [
        run(None),
        run(None, implicit=True),
        parse(b"{"),
        parse(dump(good() | {"commit_message": "a" * 9000})),
        check_path_state(tmp_path, changed=["nope"], deleted=[]),
    ]
    for result in results:
        end = RunEnd(command="wait", exit=0, artifact_failure=result.failure, detail=result.detail)
        assert end.artifact_failure == result.failure


def test_message_is_one_sanitized_line_even_for_hostile_input() -> None:
    doc = good() | {"replies": [{"comment_id": "gh:rc:A‮\x1b[31m", "body": "x"}]}
    result = parse(dump(doc), ids=frozenset())
    assert result.message
    assert "\x1b" not in result.message and "‮" not in result.message
    assert "\n" not in result.message
    hostile = good() | {"changed_files": ["a‮\x1b\nrm -rf"]}
    msg = parse(dump(hostile)).message
    assert "\x1b" not in msg and "‮" not in msg and "\n" not in msg


def test_result_type_invariants() -> None:
    ok = parse(dump(good()))
    assert ok.ok and ok.failure is None and ok.detail is None and ok.message == ""
    with pytest.raises(ValueError):
        ArtifactResult(failure="artifact_invalid", detail=None, message="x")
    with pytest.raises(ValueError):
        ArtifactResult(failure="artifact_missing", detail="json", message="x")
    with pytest.raises(ValueError):
        ArtifactResult(failure="artifact_invalid", detail="bogus", message="x")
    with pytest.raises(ValueError):
        ArtifactResult(failure="internal", detail=None, message="x")


# ---- path-state checks --------------------------------------------------------------------


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "throwaway"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x\n")
    (root / "b.py").write_text("y\n")
    return root


def ps(root: Path, changed: list[str], deleted: list[str]) -> ArtifactResult:
    return check_path_state(root, changed=changed, deleted=deleted)


def test_path_state_valid(tree: Path) -> None:
    result = ps(tree, ["src/a.py", "b.py"], ["gone.py", "nodir/gone.py"])
    assert result.ok and result.failure is None


def test_path_state_changed_missing(tree: Path) -> None:
    invalid(ps(tree, ["src/none.py"], []), "path_state")
    invalid(ps(tree, ["nodir/none.py"], []), "path_state")


def test_path_state_changed_directory(tree: Path) -> None:
    invalid(ps(tree, ["src"], []), "path_state")


def test_path_state_deleted_still_present(tree: Path) -> None:
    invalid(ps(tree, [], ["b.py"]), "path_state")
    invalid(ps(tree, [], ["src"]), "path_state")  # a directory is not absent


def test_path_state_deleted_present_as_symlink(tree: Path) -> None:
    (tree / "ln").symlink_to("b.py")
    (tree / "dang").symlink_to("nowhere")
    invalid(ps(tree, [], ["ln"]), "path_state")
    invalid(ps(tree, [], ["dang"]), "path_state")  # lstat sees the link: not ENOENT


def test_path_state_changed_symlink_refused(tree: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("s")
    (tree / "ln").symlink_to(secret)
    (tree / "ln2").symlink_to("b.py")
    invalid(ps(tree, ["ln"], []), "path_state")
    invalid(ps(tree, ["ln2"], []), "path_state")


def test_path_state_symlinked_directory_component_refused(tree: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("o")
    (tree / "dir").symlink_to(outside, target_is_directory=True)
    invalid(ps(tree, ["dir/f.txt"], []), "path_state")
    invalid(ps(tree, [], ["dir/f.txt"]), "path_state")
    invalid(ps(tree, [], ["dir/absent.txt"]), "path_state")


def test_path_state_hard_link_refused(tree: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.write_text("o")
    os.link(other, tree / "hl")
    invalid(ps(tree, ["hl"], []), "path_state")
    os.link(tree / "b.py", tree / "b2.py")  # both names now have two links
    invalid(ps(tree, ["b.py"], []), "path_state")


def test_path_state_deleted_stat_error_on_unreadable_parent(tree: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("running with privileges that ignore modes")
    parent = tree / "d"
    parent.mkdir()
    parent.chmod(0o444)  # readable (can be opened) but not searchable: stat of a child is EACCES
    try:
        invalid(ps(tree, [], ["d/gone"]), "path_state")
        parent.chmod(0)
        invalid(ps(tree, [], ["d/gone"]), "path_state")
    finally:
        parent.chmod(0o755)


@pytest.mark.parametrize("code", [errno.EACCES, errno.ENAMETOOLONG, errno.EIO, errno.ELOOP])
def test_path_state_deleted_final_stat_error_is_not_absent(
    tree: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    real_stat = os.stat

    def boom(path: Any, *a: Any, **k: Any) -> os.stat_result:
        if k.get("dir_fd") is None:  # only the walk's final-component stat fails
            return real_stat(path, *a, **k)
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(os, "stat", boom)
    invalid(ps(tree, [], ["gone.py"]), "path_state")
    invalid(ps(tree, [], ["src/gone.py"]), "path_state")


def test_path_state_special_files_refused(tree: Path) -> None:
    os.mkfifo(tree / "fifo")
    s = socket.socket(socket.AF_UNIX)
    try:
        s.bind(str(tree / "sock"))
        invalid(ps(tree, ["fifo"], []), "path_state")
        invalid(ps(tree, ["sock"], []), "path_state")
        invalid(ps(tree, [], ["fifo"]), "path_state")
    finally:
        s.close()


def test_path_state_file_where_a_directory_is_expected(tree: Path) -> None:
    invalid(ps(tree, ["b.py/x"], []), "path_state")
    invalid(ps(tree, [], ["b.py/x"]), "path_state")  # ENOTDIR is not ENOENT


def test_path_state_root_symlink_or_missing(tree: Path, tmp_path: Path) -> None:
    link = tmp_path / "rootlink"
    link.symlink_to(tree, target_is_directory=True)
    invalid(ps(link, ["b.py"], []), "path_state")
    invalid(ps(tmp_path / "gone", [], ["x"]), "path_state")


def test_path_state_rechecks_the_path_rules(tree: Path) -> None:
    """confirm calls this with lists from a stored record: a bad path never reaches the disk."""
    for bad in ("../x", "/etc/passwd", ".git/config", "a\x00b", ""):
        invalid(ps(tree, [bad], []), "path_state")
        invalid(ps(tree, [], [bad]), "path_state")


def test_path_state_special_paths_fixture(tmp_path: Path) -> None:
    sp = adversarial.make_special_paths(tmp_path / "t", tmp_path / "o")
    assert ps(sp.tree, ["regular.txt"], []).ok
    for name in (
        "link_outside",
        "link_dangling",
        "link_dotdot",
        "link_absolute_etc",
        "link_loop",
        "hardlink_outside",
        "hardlink_inside_a",
        "hardlink_inside_b",
        "fifo",
        "dir_symlink_outside",
        "socket",
    ):
        if name in sp.entries:
            invalid(ps(sp.tree, [name], []), "path_state")


# ---- reading is race-free in construction -------------------------------------------------


def test_read_uses_one_descriptor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The size check, the read and the hash come from one open file."""
    ref = freeze(tmp_path, dump(good()))
    opened: list[str] = []
    real = os.open

    def spy(path: Any, flags: int, *a: Any, **k: Any) -> int:
        opened.append(str(path))
        return real(path, flags, *a, **k)

    monkeypatch.setattr(os, "open", spy)
    assert run(ref).ok
    assert opened.count(ref.path) == 1


def test_swapped_file_after_validation_is_irrelevant(tmp_path: Path) -> None:
    ref = freeze(tmp_path, dump(good()))
    result = run(ref)
    Path(ref.path).write_bytes(b"swapped")
    assert result.ok and result.raw == dump(good())


# ---- the hand-written validator agrees with schemas/artifact.json -------------------------


def _schema_validator() -> Draft202012Validator:
    doc = json.loads((OWN_SCHEMAS / "artifact.json").read_text())
    return Draft202012Validator(doc, registry=registry())


HOSTILE_VALUES: list[object] = [
    None, True, False, 0, 1, -1, 1.5, "", " ", "x", "x" * 300, "x" * 70000, [], {}, [1], ["a"],
    {"a": 1}, [[]], [{}], "gh:rc:AAA", "‮", "../x", "/abs", ".git/x", "a\x00b",
]  # fmt: skip


def mutate(rng: random.Random, doc: dict[str, Any]) -> dict[str, Any]:
    doc = json.loads(json.dumps(doc))
    for _ in range(rng.randint(1, 3)):
        op = rng.choice(["drop", "replace", "add", "reply", "paths"])
        key = rng.choice(
            ["skill_marker", "replies", "changed_files", "deleted_files", "commit_message"]
        )
        if op == "drop":
            doc.pop(key, None)
        elif op == "replace":
            doc[key] = rng.choice(HOSTILE_VALUES)
        elif op == "add":
            doc[rng.choice(["x", "Skill_marker", "comment_id", ""])] = rng.choice(HOSTILE_VALUES)
        elif (
            op == "reply"
            and isinstance(doc.get("replies"), list)
            and doc["replies"]
            and isinstance(doc["replies"][0], dict)
        ):
            doc["replies"][0][rng.choice(["comment_id", "body", "z"])] = rng.choice(HOSTILE_VALUES)
        elif op == "paths":
            doc[rng.choice(["changed_files", "deleted_files"])] = [
                rng.choice(HOSTILE_VALUES) for _ in range(rng.randint(0, 3))
            ]
    return doc


def test_fuzz_lite_agrees_with_the_schema_and_never_raises() -> None:
    validator = _schema_validator()
    rng = random.Random(20260226)
    for _ in range(400):
        doc = mutate(rng, good())
        raw = dump(doc)
        result = parse(raw)  # must never raise
        assert result.failure in (None, "artifact_invalid")
        assert (result.detail is None) == result.ok
        if result.detail is not None:
            assert result.detail in FAILURE_DETAILS
        if validator.is_valid(doc) is False:
            assert not result.ok, doc  # the schema refuses it, so do we (any detail)
        if result.ok:
            assert validator.is_valid(doc), doc
        if result.detail == "schema":
            assert not validator.is_valid(doc), doc


PATH_TOKENS = [".", "/", "..", ".git", "a", ".GIT", "//", "/", "\n", "b/", "a/", ".git/", "~1"]


def _path_from_tokens(rng: random.Random) -> str:
    return "".join(rng.choice(PATH_TOKENS) for _ in range(rng.randint(1, 5)))


def test_fuzz_paths_schema_invalid_is_refused_and_reaches_the_paths_step() -> None:
    validator = _schema_validator()
    rng = random.Random(20260227)
    reached = 0
    for _ in range(400):
        changed = [_path_from_tokens(rng) for _ in range(rng.randint(0, 3))]
        deleted = [_path_from_tokens(rng) for _ in range(rng.randint(0, 2))]
        if rng.random() < 0.2 and changed:
            changed.append(changed[0])  # a duplicate (uniqueItems)
        doc = good() | {"changed_files": changed, "deleted_files": deleted}
        result = parse(dump(doc))
        assert result.failure in (None, "artifact_invalid")
        assert result.detail != "schema", doc  # strings only: the schema step cannot refuse these
        if not validator.is_valid(doc):
            assert not result.ok, doc
        if result.detail in ("paths", "commit_message") or result.ok:
            reached += 1
    assert reached >= 300


def test_fuzz_lite_random_bytes_never_raise() -> None:
    rng = random.Random(7)
    seeds = [dump(good()), b"{}", b"[", b'{"a":[{"b":null}]}']
    for _ in range(300):
        raw = bytearray(rng.choice(seeds))
        for _ in range(rng.randint(0, 6)):
            if raw and rng.random() < 0.6:
                raw[rng.randrange(len(raw))] = rng.randrange(256)
            elif rng.random() < 0.5:
                raw.insert(rng.randrange(len(raw) + 1), rng.choice(b'{}[]",:\\\x00\xff'))
            elif raw:
                del raw[rng.randrange(len(raw))]
        result = parse(bytes(raw))
        assert result.failure in (None, "artifact_invalid")
        assert (result.detail is None) == result.ok


def test_schema_constants_match_the_schema_file() -> None:
    schema = json.loads((OWN_SCHEMAS / "artifact.json").read_text())
    props = schema["properties"]
    assert set(props) == set(art.TOP_KEYS)
    assert props["skill_marker"]["maxLength"] == art.MAX_MARKER_CHARS
    assert props["replies"]["maxItems"] == art.MAX_REPLIES
    assert props["replies"]["items"]["properties"]["body"]["maxLength"] == art.MAX_BODY_CHARS
    assert props["changed_files"]["maxItems"] == art.MAX_PATHS == props["deleted_files"]["maxItems"]
    assert props["commit_message"]["maxLength"] == art.MAX_COMMIT_MESSAGE_BYTES
    defs = json.loads((OWN_SCHEMAS / "defs.json").read_text())["$defs"]
    assert defs["path"]["maxLength"] == art.MAX_PATH_BYTES
    assert defs["comment_id"]["pattern"] == art.COMMENT_ID_PATTERN
