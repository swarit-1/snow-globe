from __future__ import annotations

import json
import multiprocessing
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from lookout_common import jsonl

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


def test_truncated_last_line_is_skipped_unless_strict(tmp_path: Path):
    path = tmp_path / "log.jsonl"
    path.write_bytes(b'{"t":"x","a":1}\n{"t":"y","a":2}\n{"t":"z","a"')
    assert [r["a"] for r in jsonl.read_jsonl(path)] == [1, 2]
    with pytest.raises(json.JSONDecodeError):
        jsonl.read_jsonl(path, strict=True)


def test_corrupt_middle_line_raises(tmp_path: Path):
    path = tmp_path / "log.jsonl"
    path.write_bytes(b'{"a":1}\nnot json\n{"a":3}\n')
    with pytest.raises(json.JSONDecodeError):
        jsonl.read_jsonl(path)


def test_non_object_line_raises():
    with pytest.raises(ValueError):
        list(jsonl.iter_jsonl_bytes(b"[1,2]\n"))
