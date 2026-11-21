"""The rename to Snow Globe (D-080): eval reads everything written under the old names.

Recorded fixtures, byte for byte:

- ``fixtures/capture_log_lookout_0.2.1_20261121T0030.jsonl``: every capture record the live loop
  (``lookout-capture/0.2.1``) logged from 00:30 to 01:00 UTC on 21 Nov 2026, and its first 5
  archive records; metadata only.
- ``workers/forecast/tests/fixtures/forecast_run_lookout_0.1.0_*.json``: a real forecast file
  (``kind: lookout.forecast-run``).
- The committed held-out split and landmark boxes, which keep their old ``kind``: their SHA-256
  values are on record (D-038; the backtest pair's evidence), so their bytes never change.

The gate's figures must not move with the rename: a capture stamp's new spelling alone is never
a restart, while a new release (``snow-globe-capture/0.3.0`` after ``lookout-capture/0.2.1``, as
the live switch will log) still is.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import FIXTURES
from test_data_report import ACADIA, Data, _old_loop_then, report

from snow_globe_eval.data_report import build_report
from snow_globe_eval.data_report.common import CAPTURE_STAMP_PREFIXES, capture_release
from snow_globe_eval.forecast_io import KIND, KINDS, LEGACY_KIND, ForecastFileError, ForecastRun
from snow_globe_eval.heldout import KIND as SPLIT_KIND
from snow_globe_eval.heldout import LEGACY_KIND as LEGACY_SPLIT_KIND
from snow_globe_eval.heldout import committed_split, load_split
from snow_globe_eval.landmark_boxes import BOXES_KIND, BOXES_V1, LEGACY_BOXES_KIND, load_boxes

REPO = Path(__file__).resolve().parents[2]
CAPTURE_LOG = FIXTURES / "capture_log_lookout_0.2.1_20261121T0030.jsonl"
FORECAST_FILE = (
    REPO
    / "workers/forecast/tests/fixtures"
    / "forecast_run_lookout_0.1.0_smokies.look-rock_2026111908z.json"
)
SINCE = datetime(2026, 11, 21, 0, 30, tzinfo=UTC)
UNTIL = datetime(2026, 11, 21, 1, 0, tzinfo=UTC)
OLD, RENAMED, NEXT = "lookout-capture/0.2.1", "snow-globe-capture/0.2.1", "snow-globe-capture/0.3.0"


# --- capture stamps in the data report -----------------------------------------------------------
@pytest.mark.parametrize(
    ("stamp", "release"),
    [
        ("lookout-capture/0.2.1", "capture/0.2.1"),
        ("snow-globe-capture/0.2.1", "capture/0.2.1"),
        ("snow-globe-capture/0.3.0", "capture/0.3.0"),
        ("unknown", "unknown"),
        ("stopgap-0", "stopgap-0"),
    ],
)
def test_capture_release_spells_both_names_alike(stamp: str, release: str) -> None:
    assert CAPTURE_STAMP_PREFIXES == ("snow-globe-capture/", "lookout-capture/")
    assert capture_release(stamp) == release


def _logs(tmp_path: Path, name: str, rewrite) -> Path:
    """A data dir holding the recorded log, each line passed through ``rewrite(index, line)``."""
    root = tmp_path / name
    (root / "capture_log").mkdir(parents=True)
    lines = CAPTURE_LOG.read_text(encoding="utf-8").splitlines(keepends=True)
    (root / "capture_log" / "2026-11-21.jsonl").write_text(
        "".join(rewrite(i, line) for i, line in enumerate(lines)), encoding="utf-8"
    )
    return root


def _report(root: Path) -> dict[str, Any]:
    return build_report(root, since=SINCE, until=UNTIL, now=UNTIL)


def _spelled_old(doc: dict[str, Any], root: Path, old_root: Path) -> str:
    return json.dumps(doc, sort_keys=True).replace(RENAMED, OLD).replace(str(root), str(old_root))


def test_the_recorded_log_is_read_as_six_scheduled_passes(tmp_path: Path) -> None:
    rep = _report(_logs(tmp_path, "old", lambda _i, line: line))
    passes = rep["capture"]["passes"]
    assert passes["capture_versions"] == {OLD: 6}
    assert rep["capture"]["restarts"] == []


def test_the_new_spelling_alone_moves_no_figure(tmp_path: Path) -> None:
    """The same records with every stamp spelled ``snow-globe-capture/0.2.1``: the report is the
    same, figure for figure, once the stamps are spelled back."""
    old_root = _logs(tmp_path, "old", lambda _i, line: line)
    new_root = _logs(tmp_path, "new", lambda _i, line: line.replace(OLD, RENAMED))
    old, new = _report(old_root), _report(new_root)
    assert new["capture"]["passes"]["capture_versions"] == {RENAMED: 6}
    assert _spelled_old(new, new_root, old_root) == json.dumps(old, sort_keys=True)


def test_a_rename_halfway_without_a_new_release_is_no_restart(tmp_path: Path) -> None:
    lines = CAPTURE_LOG.read_text(encoding="utf-8").splitlines()
    half = len(lines) // 2
    root = _logs(
        tmp_path, "mixed", lambda i, line: line.replace(OLD, RENAMED) if i >= half else line
    )
    rep = _report(root)
    assert rep["capture"]["restarts"] == []
    assert sum(rep["capture"]["passes"]["capture_versions"].values()) == 6


def test_a_new_release_is_still_a_restart(tmp_path: Path) -> None:
    """The live switch logs ``snow-globe-capture/0.3.0`` after ``lookout-capture/0.2.1``: a
    restart (a real one), which the report lists with the stamps as logged. The new loop here
    is timed as the migration script times it: its passes keep the 300 s cadence, so no camera
    waits beyond one interval and the restart's excess is nil."""
    data = Data(tmp_path / "d")
    _old_loop_then(
        data,
        new_from=1800.0,
        new_loop={"capture_version": NEXT},
    )
    rep = report(data)
    (row,) = rep["capture"]["restarts"]
    assert row["evidence"] == ["capture_version lookout-capture/0.1.0 -> snow-globe-capture/0.3.0"]
    assert (row["from_version"], row["to_version"]) == ("lookout-capture/0.1.0", NEXT)
    assert row["poll_gaps_s"] == {ACADIA: 300.0}
    assert (row["excess_s"], row["lost_s"]) == (0.0, 0.0)


def test_the_same_release_renamed_in_a_synthetic_log_is_no_restart(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    _old_loop_then(data, new_from=1800.0, new_loop={"capture_version": "snow-globe-capture/0.1.0"})
    rep = report(data)
    assert rep["capture"]["restarts"] == []
    assert rep["clock"]["parts_lost_h"]["capture_restart_excess"] == 0.0


# --- forecast files, the held-out split and the landmark boxes -----------------------------------
def test_a_forecast_file_written_before_the_rename_is_read() -> None:
    assert KINDS == (KIND, LEGACY_KIND) == ("snow-globe.forecast-run", "lookout.forecast-run")
    run = ForecastRun.load(FORECAST_FILE)
    assert run.doc["kind"] == LEGACY_KIND and run.run_id == "2026111908"
    renamed = {**run.doc, "kind": KIND}
    assert ForecastRun(renamed).run_id == "2026111908"
    with pytest.raises(ForecastFileError):
        ForecastRun({**run.doc, "kind": "lookout.something-else"})


def test_the_committed_split_keeps_its_bytes_and_its_kind(tmp_path: Path) -> None:
    split = committed_split()
    assert split.sha256 == "c437544eecdd7a0d33b67c2374e1d388ebe45c36aeca4b2923b39c0d1d31de83"
    assert split.kind == LEGACY_SPLIT_KIND == "lookout.heldout-periods"
    assert split.to_record()["kind"] == LEGACY_SPLIT_KIND  # records name the split as written
    source = Path(__file__).resolve().parents[1] / "src/snow_globe_eval/heldout_periods.json"
    doc = json.loads(source.read_text(encoding="utf-8"))
    for kind, ok in ((SPLIT_KIND, True), (LEGACY_SPLIT_KIND, True), ("lookout.other", False)):
        path = tmp_path / f"{kind}.json"
        path.write_text(json.dumps({**doc, "kind": kind}), encoding="utf-8")
        if ok:
            assert load_split(path).kind == kind
        else:
            with pytest.raises(ValueError, match="kind must be"):
                load_split(path)


def test_the_committed_boxes_keep_their_bytes_and_load(tmp_path: Path) -> None:
    data = BOXES_V1.read_bytes()
    assert hashlib.sha256(data).hexdigest() == (
        "697bb3c2f44230571ead4fade966edc60a07af7a3d06d2e0a949e7f36b820330"
    )
    doc = json.loads(data)
    assert doc["kind"] == LEGACY_BOXES_KIND == "lookout.landmark-boxes"
    viewpoint = next(iter(doc["viewpoints"]))
    assert load_boxes(viewpoint).source_sha256 == hashlib.sha256(data).hexdigest()
    for kind, ok in ((BOXES_KIND, True), ("lookout.other", False)):
        path = tmp_path / "boxes.json"
        path.write_text(json.dumps({**doc, "kind": kind}), encoding="utf-8")
        if ok:
            assert load_boxes(viewpoint, path).viewpoint_id == viewpoint
        else:
            with pytest.raises(ValueError, match=r"not a snow-globe\.landmark-boxes file"):
                load_boxes(viewpoint, path)
