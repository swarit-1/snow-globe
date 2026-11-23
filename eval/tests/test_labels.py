"""Hand labels (PRD §13.8): the committed Rainier file, and refusals of held-out labels."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from snow_globe_eval.heldout import LeakageError
from snow_globe_eval.labels import LABELS_RAINIER, LABELS_RAINIER_V0, load_labels


def test_committed_rainier_labels_v0_1() -> None:
    labels = load_labels()
    assert labels.version == "v0.1" and labels.source == str(LABELS_RAINIER)
    first = load_labels(LABELS_RAINIER_V0)
    assert set(labels.frames) == set(first.frames)
    for key, frame in labels.frames.items():
        assert frame.bands["summit"] == first.frames[key].bands["summit"]  # summit kept
        assert frame.first_pass == {b: first.frames[key].bands[b] for b in ("upper", "mid", "near")}
        assert frame.clear_day_reference == first.frames[key].clear_day_reference
        if frame.clear_day_reference:
            assert set(frame.bands.values()) == {"visible"}
    assert labels.protocol["relabel"]["bands"] == ["upper", "mid", "near"]
    assert labels.protocol["relabel_pass"] is None and labels.protocol["labelled_renders"] == 0


def test_committed_rainier_labels() -> None:
    labels = load_labels(LABELS_RAINIER_V0)
    assert labels.viewpoint_id == "rainier.paradise-mountain" and labels.bands == (
        "summit",
        "upper",
        "mid",
        "near",
    )
    assert len(labels.frames) == 105 and len(labels.clear_day_keys) == 18
    assert "before_any_ratio" in labels.protocol and labels.protocol["labeller"]
    summit = [f.bands["summit"] for f in labels.frames.values()]
    assert (
        summit.count("visible") == 24
        and summit.count("partial") == 13
        and summit.count("hidden") == 68
    )
    assert all(f.bands == {**f.bands} for f in labels.frames.values())
    assert all(len(f.sha256) == 64 for f in labels.frames.values())
    for key in labels.clear_day_keys:
        assert set(labels.frames[key].bands.values()) == {"visible"}


def _file(tmp_path: Path, frames: list[dict]) -> Path:
    doc = json.loads(LABELS_RAINIER_V0.read_text())
    doc["frames"] = frames
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(doc))
    return path


def _frame(day: str, stamp: str, **bands: str) -> dict:
    return {
        "key": f"frames/rainier.paradise-mountain/{day}/{stamp}_{'a' * 12}.jpg",
        "sha256": "a" * 64,
        "image_time_utc": f"{day}T{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}Z",
        "bands": {
            "summit": "visible",
            "upper": "visible",
            "mid": "visible",
            "near": "visible",
            **bands,
        },
        "camera_in_cloud": False,
        "sky": "clear",
        "light": "direct",
        "clear_day_reference": True,
    }


def test_a_label_on_a_sealed_window_frame_refuses_the_file(tmp_path: Path) -> None:
    path = _file(
        tmp_path,
        [_frame("2026-11-20", "20261120T180000Z"), _frame("2026-12-20", "20261220T200000Z")],
    )
    with pytest.raises(LeakageError, match="held-out"):
        load_labels(path)


def test_the_key_time_is_checked_too(tmp_path: Path) -> None:
    # a sealed-window key whose time field names a development day (a copy-paste slip)
    slip = _frame("2026-12-20", "20261220T200000Z")
    slip["image_time_utc"] = "2026-11-20T20:00:00Z"
    with pytest.raises(LeakageError, match="held-out"):
        load_labels(_file(tmp_path, [slip]))
    # both development days, but an hour apart: the label may sit on the wrong frame
    off = _frame("2026-11-20", "20261120T180000Z")
    off["image_time_utc"] = "2026-11-20T19:00:00Z"
    with pytest.raises(ValueError, match="from the time in the key"):
        load_labels(_file(tmp_path, [off]))
    # the key's stamp is the image time truncated to the second: a fraction is fine
    fine = _frame("2026-11-20", "20261120T180000Z")
    fine["image_time_utc"] = "2026-11-20T18:00:00.912Z"
    assert len(load_labels(_file(tmp_path, [fine])).frames) == 1


def test_bad_states_duplicates_and_quarantine(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="states"):
        load_labels(_file(tmp_path, [_frame("2026-11-20", "20261120T180000Z", summit="maybe")]))
    one = _frame("2026-11-20", "20261120T180000Z")
    with pytest.raises(ValueError, match="twice"):
        load_labels(_file(tmp_path, [one, one]))
    q = tmp_path / "quarantine"
    q.mkdir()
    path = q / "labels.json"
    path.write_text(_file(tmp_path, [one]).read_text())
    with pytest.raises(LeakageError):
        load_labels(path, roots=(tmp_path,))
