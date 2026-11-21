"""The rename to Snow Globe (D-080): eval reads everything written under the old names.

Recorded fixtures, byte for byte:

- ``fixtures/capture_log_lookout_0.2.1_20261121T0030.jsonl``: every capture record the live loop
  (``lookout-capture/0.2.1``) logged from 00:30 to 01:00 UTC on 21 Nov 2026, and its first 5
  archive records; metadata only.
- ``workers/forecast/tests/fixtures/forecast_run_lookout_0.1.0_*.json``: a real forecast file
  (``kind: lookout.forecast-run``).
- The held-out split's version 1 and the landmark boxes, which keep their old ``kind``: their
  SHA-256 values are on record (D-038; the backtest pair's evidence), so their bytes never
  change. Version 2 of the split, the current one, is a new file under the new kind.

The gate's figures must not move with the rename: a capture stamp's new spelling alone is never
a restart, while a new release (``snow-globe-capture/0.3.0`` after ``lookout-capture/0.2.1``, as
the live switch will log) still is.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import FIXTURES
from test_data_report import ACADIA, RAINIER, Data, _old_loop_then, cap, pass_, poll, report, z

from snow_globe_eval.data_report import build_report, format_report
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
#: A window past the switch in :func:`_the_switch` (its new loop's first pass ends ~01:02:24).
SWITCH_UNTIL = datetime(2026, 11, 21, 1, 10, tzinfo=UTC)


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


# --- the live switch in capture's new record shapes (0.3.0: started_at, loop_started) ----------
def _the_switch(tmp_path: Path, name: str, cameras: Iterable[str]) -> tuple[Path, dict]:
    """The live switch as infra/ops/migrate-to-snow-globe.sh times it: the recorded 0.2.1 loop
    (:data:`CAPTURE_LOG`, the old record shape: no started_at, no loop_started), stopped
    after its last pass (00:57:23.299Z), then a 0.3.0 loop in the new shape 240 s later, whose
    first pass polls each of ``cameras`` 301 s after its last recorded answer. Returns the
    data dir and each camera's last recorded poll."""
    lines = CAPTURE_LOG.read_text(encoding="utf-8").splitlines()
    last: dict[str, dict] = {}
    for line in lines:
        r = json.loads(line)
        if r.get("event") == "request" and r.get("stage") in ("json", "head"):
            last[r["viewpoint_id"]] = r
    switch = datetime(2026, 11, 21, 0, 57, 23, 299000, tzinfo=UTC) + timedelta(seconds=240)
    records = [
        cap(
            switch + timedelta(milliseconds=1),
            "loop_started",
            None,
            pid=51234,
            started_at=z(switch - timedelta(milliseconds=9)),
            viewpoints=list(cameras),
            interval_s=300,
            jitter_s=30,
            capture_version=NEXT,
        ),
    ]
    for vp in cameras:
        sent = datetime.fromisoformat(last[vp]["answered_at"]) + timedelta(seconds=301)
        records.append(
            poll(
                sent + timedelta(milliseconds=80),
                vp,
                elapsed_ms=60.0,
                sent_at=z(sent),
                answered_at=z(sent + timedelta(milliseconds=60)),
                capture_version=NEXT,
            )
        )
    end = max(datetime.fromisoformat(r["t"]) for r in records) + timedelta(seconds=1)
    records.append(
        pass_(
            end,
            duration=round((end - switch).total_seconds() - 0.1, 2),
            started_at=z(switch + timedelta(milliseconds=100)),
            capture_version=NEXT,
        )
    )
    root = tmp_path / name / "capture_log"
    root.mkdir(parents=True)
    text = (
        "\n".join(lines)
        + "\n"
        + "".join(json.dumps(r) + "\n" for r in sorted(records, key=lambda r: r["t"]))
    )
    (root / "2026-11-21.jsonl").write_text(text, encoding="utf-8")
    return root.parent, last


def test_the_live_switch_in_both_record_shapes_is_split_exactly(tmp_path: Path) -> None:
    """Fixtures of both shapes: the recorded 0.2.1 log has neither started_at nor
    loop_started; the 0.3.0 loop after it has both. The split is the last 0.2.1 record before
    loop_started, each camera's gap runs from its last recorded poll, and the 8 gaps are
    301.06-301.135 s (the margin capture's scheduler keeps), so 1.135 s of excess."""
    cameras = sorted(committed_split().viewpoints)
    root, last = _the_switch(tmp_path, "switch", cameras)
    rep = build_report(root, since=SINCE, until=SWITCH_UNTIL, now=SWITCH_UNTIL)
    (row,) = rep["capture"]["restarts"]
    assert {r["capture_version"] for r in last.values()} == {OLD}
    spacing = (  # the recorded pass's start is t - duration_s: 00:57:23.299 - 16.05 s
        datetime(2026, 11, 21, 1, 1, 23, 399000, tzinfo=UTC)
        - datetime(2026, 11, 21, 0, 57, 7, 249000, tzinfo=UTC)
    ).total_seconds()
    assert row["evidence"] == [
        f"capture_version {OLD} -> {NEXT}",
        f"pass starts {spacing:.1f} s apart, under the 300 s interval of one loop",
        "loop_started at 2026-11-21T01:01:23.300Z (pid 51234)",
    ]
    assert row["split_by"] == "loop_started"
    assert row["stopped_after_utc"] == "2026-11-21T00:57:23.299Z"  # the last recorded pass
    assert row["loop_started_utc"] == "2026-11-21T01:01:23.290Z"
    assert (row["started_utc"], row["started_from"]) == ("2026-11-21T01:01:23.399Z", "started_at")
    gaps = {
        vp: round(
            (
                datetime.fromisoformat(last[vp]["answered_at"])
                + timedelta(seconds=301)
                - datetime.fromisoformat(last[vp]["sent_at"])
            ).total_seconds(),
            3,
        )
        for vp in cameras
    }
    assert row["poll_gaps_s"] == gaps and min(gaps.values()) == 301.06
    assert row["longest_poll_gap"]["viewpoint_id"] == RAINIER and gaps[RAINIER] == 301.135
    assert (row["excess_s"], row["not_polled_since"]) == (1.135, {})


def test_a_camera_the_new_loop_leaves_out_is_flagged_as_dropped(tmp_path: Path) -> None:
    """The same switch, but the new loop's loop_started lists 7 cameras: Grand Teton is not
    polled again, flagged as dropped (``in_latest_loop`` False), and left out of the excess.
    The recorded 0.2.1 passes name all 8 cameras in ``outcomes``; the newer loop_started
    record is the latest list (its pass here names none), so it decides."""
    teton = "grand-teton.science-center"
    cameras = [vp for vp in sorted(committed_split().viewpoints) if vp != teton]
    root, last = _the_switch(tmp_path, "dropped", cameras)
    until = SWITCH_UNTIL
    rep = build_report(root, since=SINCE, until=until, now=until)
    (row,) = rep["capture"]["restarts"]
    sent = datetime.fromisoformat(last[teton]["sent_at"])
    assert row["not_polled_since"] == {
        teton: {
            "last_poll_utc": last[teton]["sent_at"],
            "gap_to_window_end_s": round((until - sent).total_seconds(), 3),
            "in_latest_loop": False,
            "left_out": True,
        }
    }
    assert teton not in row["poll_gaps_s"] and row["excess_s"] == 1.135
    latest = rep["capture"]["latest_loop"]
    assert (latest["listed_by"], latest["cameras"]) == ("loop_started", cameras)
    flag = rep["capture"]["viewpoints"][teton]["not_polled_since_restart"]
    assert (flag["in_latest_loop"], flag["left_out"]) == (False, True)
    text = format_report(rep)
    assert f"  {teton}: not polled since the restart at 11-21 01:01:23 (dropped: not one of" in text


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
    """Version 1 keeps its bytes and its old kind; version 2, the current split, carries the
    new kind; the loader reads both kinds."""
    split = committed_split(1)
    assert split.sha256 == "c437544eecdd7a0d33b67c2374e1d388ebe45c36aeca4b2923b39c0d1d31de83"
    assert split.kind == LEGACY_SPLIT_KIND == "lookout.heldout-periods"
    assert split.to_record()["kind"] == LEGACY_SPLIT_KIND  # records name the split as written
    current = committed_split()
    assert current.version == 2 and current.kind == SPLIT_KIND == "snow-globe.heldout-periods"
    assert current.to_record()["kind"] == SPLIT_KIND
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
