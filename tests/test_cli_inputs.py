"""Flag value grammars: durations, --since, actors, decision ids, text from files or stdin."""

from __future__ import annotations

import io
import json
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from review_respond.actor import MAX_DECISION_IDS, check_decision_ids, parse_actor
from review_respond.durations import parse_duration, parse_rfc3339, parse_since
from review_respond.errors import CliError
from review_respond.textin import read_text_arg

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0", timedelta(0)),
        ("0s", timedelta(0)),
        ("250ms", timedelta(milliseconds=250)),
        ("30s", timedelta(seconds=30)),
        ("5m", timedelta(minutes=5)),
        ("2h", timedelta(hours=2)),
        ("30d", timedelta(days=30)),
    ],
)
def test_duration_valid(text: str, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "5",  # a number needs a unit
        "-5s",
        "+5s",
        "1.5s",
        "5 s",
        " 5s",
        "5s\n",
        "5w",
        "5S",
        "nan",
        "inf",
        "s",
        "٣s",  # non-ASCII digit
        "1e3s",
        "9" * 16 + "s",
        "999999999999999d",  # overflows timedelta
        "5s5m",
    ],
)
def test_duration_invalid_names_flag_not_value(text: str) -> None:
    with pytest.raises(CliError) as exc:
        parse_duration(text, flag="--older-than")
    assert exc.value.code == "usage"
    assert "--older-than" in exc.value.message
    if len(text) > 3:
        assert text not in exc.value.message


def test_rfc3339_with_offset() -> None:
    parsed = parse_rfc3339("2026-11-25T10:30:00+02:00")
    assert parsed == datetime(2026, 11, 25, 8, 30, tzinfo=UTC)
    assert parsed.utcoffset() == timedelta(0)  # normalized to UTC
    assert parse_rfc3339("2026-11-25T10:30:00Z").utcoffset() == timedelta(0)
    assert parse_rfc3339("2026-11-25t10:30:00.123456789z").microsecond == 123456
    assert parse_rfc3339("2026-11-25T10:30:00-05:30") == datetime(
        2026, 11, 25, 10, 30, tzinfo=timezone(-timedelta(hours=5, minutes=30))
    )


@pytest.mark.parametrize(
    "text",
    [
        "2026-11-25T10:30:00",  # no offset
        "2026-11-25",
        "2026-11-25 10:30:00Z",
        "2026-13-25T10:30:00Z",
        "2026-11-31T10:30:00Z",
        "2026-11-25T25:30:00Z",
        "2026-11-25T10:30:00+25:00",
        "2026-11-25T10:30:00+0200",
        "2026-11-25T10:30:00Z ",
        "",
    ],
)
def test_rfc3339_invalid(text: str) -> None:
    with pytest.raises(CliError) as exc:
        parse_rfc3339(text, flag="--since")
    assert exc.value.code == "usage"


def test_rfc3339_leap_second() -> None:
    assert parse_rfc3339("2016-12-31T23:59:60Z") == datetime(2017, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "text", ["0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00", "9999-12-31T23:59:60Z"]
)
def test_rfc3339_out_of_range_after_utc_is_usage_error(text: str) -> None:
    with pytest.raises(CliError) as exc:
        parse_rfc3339(text, flag="--since")
    assert exc.value.code == "usage"


def test_duration_ms_is_exact_integer_arithmetic() -> None:
    assert parse_duration("999999999999999ms") == timedelta(milliseconds=999999999999999)
    assert parse_duration("1ms") == timedelta(microseconds=1000)


def test_duration_fraction_of_ten_digits_is_invalid() -> None:
    with pytest.raises(CliError):
        parse_duration("0." + "1" * 10 + "s")


@pytest.mark.parametrize("text", ["1" * 16 + unit for unit in ("ms", "s", "m", "h", "d")])
def test_duration_sixteen_digits_is_invalid(text: str) -> None:
    with pytest.raises(CliError):
        parse_duration(text)


def test_since_accepts_time_or_duration() -> None:
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    assert parse_since("2026-11-25T10:00:00Z", now=now) == datetime(2026, 11, 25, 10, tzinfo=UTC)
    assert parse_since("2h", now=now) == datetime(2026, 11, 25, 10, tzinfo=UTC)
    assert parse_since("0", now=now) == now


def test_since_duration_larger_than_the_epoch_is_a_usage_error() -> None:
    with pytest.raises(CliError) as exc:
        parse_since("999999999d", now=datetime(2026, 1, 1, tzinfo=UTC))
    assert exc.value.code == "usage"


@pytest.mark.parametrize(
    "text", ["human:alice", "auto:unknown", "auto:review-respond", "human:a.b_c@d-e"]
)
def test_actor_valid(text: str) -> None:
    assert parse_actor(text) == text


@pytest.mark.parametrize(
    "text",
    ["", "alice", "human:", "robot:x", "human:a b", "auto:a\nb", "human:" + "a" * 65, "AUTO:x"],
)
def test_actor_invalid(text: str) -> None:
    with pytest.raises(CliError) as exc:
        parse_actor(text)
    assert exc.value.code == "usage"
    if len(text) > 8:
        assert text not in exc.value.message


def test_decision_ids() -> None:
    assert check_decision_ids(["a"] * 16) == ["a"] * 16
    assert check_decision_ids(["a", "gate:1/x@y"]) == ["a", "gate:1/x@y"]
    with pytest.raises(CliError):
        check_decision_ids(["ok", ""])
    with pytest.raises(CliError):
        check_decision_ids(["a"] * 17)
    with pytest.raises(CliError):
        check_decision_ids(["x" * 129])


def test_decision_id_limit_matches_schema() -> None:
    schema = json.loads((ROOT / "schemas" / "defs.json").read_text())["$defs"]
    assert MAX_DECISION_IDS == schema["decision_ids"]["maxItems"]


def test_actor_trailing_newline_and_length_bounds() -> None:
    for text in ("human:alice\n", "human:" + "a" * 65, "auto:" + "a" * 65):
        with pytest.raises(CliError):
            parse_actor(text)
    assert parse_actor("human:" + "a" * 64) == "human:" + "a" * 64
    assert parse_actor("auto:" + "a" * 64) == "auto:" + "a" * 64


def test_read_text_from_file_and_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "note.txt"
    path.write_text("héllo\n", encoding="utf-8")
    assert read_text_arg(str(path), flag="--note-file", max_bytes=100) == "héllo\n"
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"from stdin")))
    assert read_text_arg("-", flag="--note-file", max_bytes=100) == "from stdin"


def test_read_text_limits_and_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * 9)
    assert read_text_arg(str(big), flag="--f", max_bytes=9) == "x" * 9
    for args in (str(big), str(tmp_path / "missing"), str(tmp_path), ""):
        with pytest.raises(CliError) as exc:
            read_text_arg(args, flag="--f", max_bytes=8)
        assert exc.value.code == "usage"
    bad = tmp_path / "bad.txt"
    bad.write_bytes(b"\xff\xfe")
    with pytest.raises(CliError, match="UTF-8"):
        read_text_arg(str(bad), flag="--f", max_bytes=8)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"y" * 100)))
    with pytest.raises(CliError, match="larger"):
        read_text_arg("-", flag="--f", max_bytes=8)
