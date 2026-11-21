"""The v−1 landmark boxes (PRD §16 M0.5): the committed file and its loader."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from snow_globe_eval.landmark_boxes import BOXES_V1, LandmarkBox, load_boxes

GC = "grand-canyon.yavapai"


def test_the_committed_boxes_load() -> None:
    boxes = load_boxes(GC)
    assert boxes.version == "v-1" and boxes.primary == "north-rim-cliffs"
    assert [b.name for b in boxes.boxes] == ["north-rim-cliffs", "mid-canyon"]
    assert [b.name for b in boxes.others] == ["mid-canyon"]
    assert boxes.drawn_on["size"] == [999, 666]
    assert len(boxes.source_sha256) == 64 and boxes.source == str(BOXES_V1)
    region = boxes.primary_box.region()
    assert region.kind == "box" and region.frame_size == (1, 1)
    record = boxes.to_record()
    assert record["primary"] == "north-rim-cliffs" and len(record["boxes"]) == 2


def test_the_committed_file_says_the_pose_solve_replaces_it() -> None:
    doc = json.loads(BOXES_V1.read_text())
    assert "M2 pose solve" in doc["note"] and "not in the viewpoint manifest" in doc["note"]
    assert doc["viewpoints"][GC]["drawn_on"]["key"].startswith(f"archive/{GC}/2021-01-15/")


def test_the_manifest_has_no_landmarks_from_this_file() -> None:
    """The boxes stay out of the viewpoint manifest (the task's rule)."""
    from snow_globe_common.manifest import load_manifest

    assert load_manifest(GC).landmarks == ()


def test_a_normalised_box_fits_any_frame_size() -> None:
    region = LandmarkBox("b", (0.25, 0.5, 0.75, 0.75)).region()
    small = region.mask((100, 40), (999, 666))  # analysis grid, original size (unused)
    big = region.mask((200, 80), (3840, 2160))
    assert small.sum() * 4 == big.sum() == 2000
    rows, cols = np.nonzero(small)
    assert (rows.min(), rows.max(), cols.min(), cols.max()) == (20, 29, 25, 74)


def test_shifted_boxes_clip_to_the_frame() -> None:
    box = LandmarkBox("b", (0.9, 0.1, 0.99, 0.2))
    moved = box.shifted(0.05, -0.15)
    assert moved.box == pytest.approx((0.95, 0.0, 1.0, 0.05))
    with pytest.raises(ValueError):
        box.shifted(0.2, 0.0)  # leaves the frame entirely: x0 = x1 = 1


@pytest.mark.parametrize(
    "box",
    [
        (0.5, 0.1, 0.4, 0.2),
        (0.1, 0.1, 1.2, 0.2),
        (-0.1, 0.1, 0.2, 0.2),
        (0.1, float("nan"), 0.2, 0.3),
    ],
)
def test_bad_boxes_are_refused(box) -> None:
    with pytest.raises(ValueError):
        LandmarkBox("b", box)


def _write(tmp_path: Path, entry: dict, kind: str = "snow-globe.landmark-boxes") -> Path:
    path = tmp_path / "boxes.json"
    path.write_text(json.dumps({"kind": kind, "version": "v-1", "viewpoints": {GC: entry}}))
    return path


def test_a_quarantined_boxes_file_is_refused_unread(tmp_path: Path) -> None:
    """D-022: a boxes file under <data root>/quarantine/ is refused before it is opened (it is
    unreadable here, so a read would fail with another error)."""
    from snow_globe_eval.heldout import LeakageError

    root = tmp_path / "snow-globe-data"
    good = {"primary": "a", "boxes": [{"name": "a", "box": [0.1, 0.1, 0.2, 0.2]}]}
    folder = root / "quarantine" / "held-out-breach-2026-11-19"
    folder.mkdir(parents=True)
    path = _write(folder, good)
    path.chmod(0)
    try:
        with pytest.raises(LeakageError, match="D-022"):
            load_boxes(GC, path, roots=(root,))
    finally:
        path.chmod(0o600)
    assert load_boxes(GC, _write(tmp_path, good), roots=(root,)).primary == "a"


def test_a_file_must_be_well_formed(tmp_path: Path) -> None:
    good = {"primary": "a", "boxes": [{"name": "a", "box": [0.1, 0.1, 0.2, 0.2]}]}
    assert load_boxes(GC, _write(tmp_path, good)).primary == "a"
    with pytest.raises(ValueError, match=r"not a snow-globe\.landmark-boxes"):
        load_boxes(GC, _write(tmp_path, good, kind="other"))
    with pytest.raises(KeyError):
        load_boxes("point-reyes.beach", _write(tmp_path, good))
    missing_primary = {**good, "primary": "z"}
    with pytest.raises(ValueError, match="primary box"):
        load_boxes(GC, _write(tmp_path, missing_primary))
    repeated = {**good, "boxes": good["boxes"] * 2}
    with pytest.raises(ValueError, match="repeat"):
        load_boxes(GC, _write(tmp_path, repeated))
    bad_name = {"primary": "A b", "boxes": [{"name": "A b", "box": [0.1, 0.1, 0.2, 0.2]}]}
    with pytest.raises(ValueError, match="lower-case"):
        load_boxes(GC, _write(tmp_path, bad_name))
