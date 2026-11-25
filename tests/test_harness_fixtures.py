"""The golden-file helper, the adversarial fixtures and the scenario builders."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from jsonschema import ValidationError

from review_respond.gateway import Gateway

from .harness import adversarial, builders, golden
from .harness.fakes import Fakes

PR = builders.PR


def error_doc() -> dict[str, object]:
    return {"error": {"code": "usage", "message": "unknown flag"}}


def pre_round_payload() -> dict[str, object]:
    return {
        "tool": "review-respond",
        "hook": "pre-round",
        "object_type": "pr",
        "object_id": PR,
        "repo": builders.REPO,
        "labels": ["app=review-respond"],
        "pr": PR,
        "round_number": 1,
        "head_sha": builders.HEAD_A,
        "comment_count": 2,
        "trigger": "check",
        "forced": False,
        "reads_third_party": True,
    }


def test_golden_cli_output_and_hook_payload_match_their_files(golden) -> None:  # type: ignore[no-untyped-def]
    golden.check("harness/error-envelope", error_doc(), "review-respond/cli/error.json")
    golden.hook("pre-round", pre_round_payload())


def test_golden_rejects_an_invalid_document_before_comparing(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        golden.check(
            "harness/never-written",
            {"error": {"code": "no_such_code"}},
            "review-respond/cli/error.json",
        )
    assert not (golden.GOLDEN / "harness" / "never-written.json").exists()


def test_golden_fails_on_a_missing_file_and_on_a_difference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(golden, "GOLDEN", tmp_path)
    schema = "review-respond/cli/error.json"
    with pytest.raises(AssertionError, match="missing golden"):
        golden.check("x", error_doc(), schema)
    golden.check("x", error_doc(), schema, update=True)
    golden.check("x", error_doc(), schema)
    other = {"error": {"code": "usage", "message": "another message"}}
    with pytest.raises(AssertionError, match="differs"):
        golden.check("x", other, schema)


def test_golden_files_are_rendered_stably() -> None:
    text = golden.render({"b": 1, "a": "é"})
    assert text == '{\n  "a": "\\u00e9",\n  "b": 1\n}\n'


def test_the_hook_payload_schema_rejects_a_wrong_payload() -> None:
    payload = pre_round_payload()
    payload["hook"] = "post-pending"
    with pytest.raises(ValidationError):
        golden.check_hook("pre-round", payload)


@pytest.mark.parametrize(
    "group",
    [
        adversarial.FENCE_BREAKOUTS,
        adversarial.CONTROL_CHARACTERS,
        adversarial.INVISIBLE_CHARACTERS,
        adversarial.INJECTION_SHAPED,
        adversarial.SIZE_EXTREMES,
        adversarial.FORGED_AGENT_IDS,
        adversarial.FORGED_LABELS,
        adversarial.HOSTILE_ARTIFACT_PATHS,
        adversarial.HIDDEN_CHANGE_DIFFS,
    ],
)
def test_adversarial_groups_are_not_empty_and_distinct(group: dict[str, str]) -> None:
    assert len(group) >= 5
    values = list(group.values())
    assert len(set(values)) == len(values)


def test_hostile_texts_cover_fences_controls_invisibles_and_undecodable() -> None:
    texts = adversarial.HOSTILE_COMMENT_TEXTS
    assert "```" in texts["backtick_fence"]
    assert any(ord(c) < 0x20 and c not in "\n\t" for t in texts.values() for c in t)
    assert any("\u202e" in t for t in texts.values())
    assert any(0xE0000 <= ord(c) <= 0xE007F for t in texts.values() for c in t)
    assert "\udcff" in texts["undecodable"]
    with pytest.raises(UnicodeEncodeError):
        texts["undecodable"].encode("utf-8")
    assert texts["undecodable"].encode("utf-8", "surrogateescape")


def test_planted_config_lands_where_a_tool_would_trust_it(tmp_path: Path) -> None:
    written = adversarial.plant_config(tmp_path)
    rel = {p.relative_to(tmp_path).as_posix() for p in written}
    assert {".git/hooks/pre-commit", ".git/config", ".claude/settings.json", ".mcp.json"} <= rel
    assert {"CLAUDE.md", ".gitattributes", ".husky/pre-commit"} <= rel


def test_special_paths_include_links_fifos_and_odd_names(tmp_path: Path) -> None:
    made = adversarial.make_special_paths(tmp_path / "tree", tmp_path / "outside")
    entries = made.entries
    for name in ("link_outside", "link_dangling", "link_loop", "dir_symlink_outside"):
        assert entries[name].is_symlink(), name
    assert os.stat(entries["hardlink_outside"]).st_nlink >= 2
    assert os.stat(entries["hardlink_outside"]).st_ino == os.stat(made.outside).st_ino
    assert (
        os.stat(entries["hardlink_inside_b"]).st_ino == os.stat(entries["hardlink_inside_a"]).st_ino
    )
    assert stat.S_ISFIFO(os.lstat(entries["fifo"]).st_mode)
    assert "newline\nname.txt" in entries and "-leading-dash.txt" in entries
    assert "Case.txt" in entries and ".GIT/config" in entries


def test_binary_and_hidden_change_files(tmp_path: Path) -> None:
    files = adversarial.write_binary_and_hidden(tmp_path / "t")
    assert b"\x00" in files["binary"].read_bytes()
    assert b"\x00" in files["nul_text"].read_bytes()
    assert "-diff" in files["attributes"].read_text()
    assert adversarial.BINARY_DIFF.startswith("diff --git")
    assert "GIT binary patch" in adversarial.GIT_BINARY_PATCH


def test_moved_head_deleted_comment_and_resolved_thread_scenarios_flow_through_fakes(
    gateway: Gateway, fakes: Fakes
) -> None:
    before, after = builders.scenario_moved_head()
    fakes.script("forgectl", "show", json=before)
    fakes.script("forgectl", "show", json=after)
    first, second = gateway.forgectl.show(PR), gateway.forgectl.show(PR)
    assert first.json["head"]["sha"] != second.json["head"]["sha"]

    life = builders.scenario_comment_lifecycle()
    for name in ("fresh", "deleted_comment", "resolved_thread"):
        fakes.script("forgectl", "comments", json=life[name])
    seen = [gateway.forgectl.comments(PR).json for _ in range(3)]
    assert len(seen[0]["threads"]) == 1 and seen[1]["threads"] == []
    assert seen[2]["threads"][0]["resolved"] is True


def test_a_comment_edited_back_to_its_old_text_differs_only_in_updated_at() -> None:
    life = builders.scenario_comment_lifecycle()
    fresh = life["fresh"]["threads"][0]["comments"][0]
    restored = life["edit_restored"]["threads"][0]["comments"][0]
    assert fresh["body"] == restored["body"] and fresh["updated_at"] != restored["updated_at"]
