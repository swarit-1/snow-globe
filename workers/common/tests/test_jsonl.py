from __future__ import annotations

import json
import math
import multiprocessing
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from snow_globe_common import jsonl

T_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def test_append_adds_utc_t_and_writes_one_compact_line(tmp_path: Path):
    path = tmp_path / "logs" / "2026-11-19.jsonl"
    row = jsonl.append_jsonl(path, {"vp": "point-reyes.beach", "event": "stored", "note": "Été"})
    assert T_RE.match(row["t"])
    assert next(iter(row)) == "t"
    text = path.read_text(encoding="utf-8")
    assert text.count("\n") == 1 and text.endswith("\n")
    assert "Été" in text and ": " not in text  # utf-8 kept, compact separators
    assert json.loads(text) == row


def test_append_keeps_given_t_and_converts_datetimes(tmp_path: Path):
    path = tmp_path / "log.jsonl"
    jsonl.append_jsonl(path, {"t": "2026-11-19T06:30:13.140912+00:00", "event": "a"})
    jsonl.append_jsonl(path, {"t": datetime(2026, 11, 19, 6, 35, tzinfo=UTC), "event": "b"})
    rows = jsonl.read_jsonl(path)
    assert rows[0]["t"] == "2026-11-19T06:30:13.140912+00:00"
    assert rows[1]["t"] == "2026-11-19T06:35:00.000Z"


def test_json_default_handles_common_types():
    line = jsonl.encode_line(
        {"when": datetime(2026, 11, 19, tzinfo=UTC), "day": date(2026, 11, 19), "p": Path("/x")}
    )
    assert json.loads(line) == {"when": "2026-11-19T00:00:00Z", "day": "2026-11-19", "p": "/x"}
    with pytest.raises(ValueError):
        jsonl.encode_line({"naive": datetime(2026, 11, 19)})
    with pytest.raises(TypeError):
        jsonl.encode_line({"obj": object()})


def test_newlines_inside_values_stay_on_one_line(tmp_path: Path):
    path = tmp_path / "log.jsonl"
    jsonl.append_jsonl(path, {"msg": "line one\nline two"})
    assert path.read_text().count("\n") == 1


def test_append_many_writes_in_order(tmp_path: Path):
    path = tmp_path / "log.jsonl"
    written = jsonl.append_jsonl_many(path, [{"i": i} for i in range(5)], fsync=True)
    assert [r["i"] for r in jsonl.read_jsonl(path)] == [0, 1, 2, 3, 4]
    assert len(written) == 5
    assert jsonl.append_jsonl_many(path, []) == []


def _writer(path: str, worker: int, count: int) -> None:
    for i in range(count):
        jsonl.append_jsonl(path, {"worker": worker, "i": i, "pad": "x" * 300})


def test_concurrent_processes_never_interleave(tmp_path: Path):
    path = tmp_path / "shared.jsonl"
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_writer, args=(str(path), w, 150)) for w in range(4)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
        assert proc.exitcode == 0
    rows = jsonl.read_jsonl(path, strict=True)
    assert len(rows) == 600
    for worker in range(4):
        assert [r["i"] for r in rows if r["worker"] == worker] == list(range(150))


def test_truncated_last_line_is_skipped_unless_strict(tmp_path: Path, caplog):
    path = tmp_path / "log.jsonl"
    path.write_bytes(b'{"t":"x","a":1}\n{"t":"y","a":2}\n{"t":"z","a"')
    bad: list[jsonl.BadLine] = []
    with caplog.at_level("WARNING"):
        assert [r["a"] for r in jsonl.read_jsonl(path, bad_lines=bad)] == [1, 2]
    assert bad == [jsonl.BadLine(3, jsonl.REASON_UNTERMINATED, b'{"t":"z","a"')]
    assert "damaged" not in caplog.text  # may be a write in progress: no warning
    with pytest.raises(json.JSONDecodeError):
        jsonl.read_jsonl(path, strict=True)


def test_corrupt_middle_line_is_skipped_and_reported_unless_strict(tmp_path: Path, caplog):
    path = tmp_path / "log.jsonl"
    path.write_bytes(b'{"a":1}\nnot json\n{"a":3}\n')
    bad: list[jsonl.BadLine] = []
    with caplog.at_level("WARNING"):
        assert [r["a"] for r in jsonl.read_jsonl(path, bad_lines=bad)] == [1, 3]
    assert bad == [jsonl.BadLine(2, jsonl.REASON_INVALID_JSON, b"not json")]
    assert "skipped 1 damaged JSONL line(s); first at line 2" in caplog.text
    with pytest.raises(json.JSONDecodeError):
        jsonl.read_jsonl(path, strict=True)


def test_non_object_line_is_skipped_unless_strict():
    bad: list[jsonl.BadLine] = []
    assert list(jsonl.iter_jsonl_bytes(b'[1,2]\n{"a":1}\n', bad_lines=bad)) == [{"a": 1}]
    assert [b.reason for b in bad] == [jsonl.REASON_NOT_OBJECT]
    with pytest.raises(ValueError, match="line 1 is not an object"):
        list(jsonl.iter_jsonl_bytes(b"[1,2]\n", strict=True))


def test_line_torn_inside_a_utf8_character_is_skipped():
    # A crash can cut a write between the two bytes of 'é'; that is a decode error, not
    # a JSON error, and must be skipped like any other torn line.
    torn = '{"t":"x","note":"Été"}\n'.encode()[:-4]
    assert torn.endswith(b"\xc3")
    bad: list[jsonl.BadLine] = []
    assert list(jsonl.iter_jsonl_bytes(b'{"a":1}\n' + torn, bad_lines=bad)) == [{"a": 1}]
    assert [b.reason for b in bad] == [jsonl.REASON_UNTERMINATED]
    with pytest.raises(UnicodeDecodeError):
        list(jsonl.iter_jsonl_bytes(b'{"a":1}\n' + torn, strict=True))


def test_bad_lines_are_reported_even_if_the_reader_stops_early():
    bad: list[jsonl.BadLine] = []
    records = jsonl.iter_jsonl_bytes(b'oops\n{"a":1}\n{"a":2}\n', bad_lines=bad)
    assert next(records) == {"a": 1}
    assert [b.line for b in bad] == [1]


def test_local_append_after_a_torn_tail_keeps_the_log_readable(tmp_path: Path):
    """Review finding: a torn line used to swallow the next record and break the log."""
    path = tmp_path / "forecast_log" / "2026-11-19.jsonl"
    path.parent.mkdir()
    path.write_bytes(b'{"t":"2026-11-19T06:00:00.000Z","a":1}\n{"t":"2026-11-19T06:05')
    jsonl.append_jsonl(path, {"a": 2})
    jsonl.append_jsonl_many(path, [{"a": 3}, {"a": 4}])
    raw = path.read_bytes()
    # record 1, the fragment (now ended by the writer's leading newline), records 2-4
    assert raw.count(b"\n") == 5 and raw.endswith(b'"a":4}\n')
    bad: list[jsonl.BadLine] = []
    assert [r["a"] for r in jsonl.read_jsonl(path, bad_lines=bad)] == [1, 2, 3, 4]
    assert [(b.line, b.reason) for b in bad] == [(2, jsonl.REASON_INVALID_JSON)]
    with pytest.raises(json.JSONDecodeError):
        jsonl.read_jsonl(path, strict=True)  # audits still see the damage


def test_append_adds_no_blank_line_to_a_healthy_or_empty_file(tmp_path: Path):
    path = tmp_path / "log.jsonl"
    path.write_bytes(b"")
    jsonl.append_jsonl(path, {"a": 1})
    jsonl.append_jsonl(path, {"a": 2})
    assert path.read_bytes().count(b"\n") == 2 and b"\n\n" not in path.read_bytes()


def test_non_finite_floats_are_written_as_null(tmp_path: Path):
    line = jsonl.encode_line(
        {"lag_s": math.nan, "range": [math.inf, 1.5], "nested": {"x": -math.inf, "y": 2.0}}
    )

    def refuse(constant: str) -> None:
        raise ValueError(f"non-standard JSON constant {constant}")

    # A strict parser (jq, JavaScript, serde_json) must accept it.
    assert json.loads(line, parse_constant=refuse) == {
        "lag_s": None,
        "range": [None, 1.5],
        "nested": {"x": None, "y": 2.0},
    }
    assert jsonl.dumps_json({"v": math.nan}, indent=2) == '{\n  "v": null\n}'
