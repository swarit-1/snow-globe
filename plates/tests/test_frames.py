"""Plate inputs behind the committed split (plates.frames): held out, fit bound, quarantine."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
from synth import write_frame

from snow_globe_plates.frames import HeldOutFrameError, frame_input

VP = "rainier.paradise-mountain"
PORE = "point-reyes.beach"
PIXELS = np.zeros((4, 4, 3))


def test_development_frame_passes(tmp_path: Path) -> None:
    key = write_frame(tmp_path, VP, "20261121T182101Z", PIXELS)
    frame = frame_input(tmp_path, key)
    assert frame.recorded_sha256 and frame.image_time.isoformat().startswith("2026-11-21T18:21:01")
    assert frame.verdict["development"] and frame.verdict["fit"]


@pytest.mark.parametrize("stamp", ["20261215T120000Z", "20270114T200000Z", "20261215T090000Z"])
def test_sealed_window_is_refused(tmp_path: Path, stamp: str) -> None:
    key = write_frame(tmp_path, VP, stamp, PIXELS)
    with pytest.raises(HeldOutFrameError, match="held out"):
        frame_input(tmp_path, key)


def test_local_date_in_the_window_is_refused_too(tmp_path: Path) -> None:
    # 15 Jan 2027 05:00 UTC is still 14 Jan in Pacific time.
    key = write_frame(tmp_path, VP, "20270115T050000Z", PIXELS)
    with pytest.raises(HeldOutFrameError, match="held out"):
        frame_input(tmp_path, key)


def test_frames_after_the_fit_bound_are_refused(tmp_path: Path) -> None:
    # After the sealed window: a development frame, but a fit may not use it (PRD §13.9).
    late = write_frame(tmp_path, VP, "20270120T180000Z", PIXELS)
    with pytest.raises(HeldOutFrameError, match="not for fits"):
        frame_input(tmp_path, late)
    # Point Reyes's earliest held-out period is design H's summer 2026, so its fits stop at
    # 1 Jun 2026 even for a November 2026 frame.
    pore = write_frame(tmp_path, PORE, "20261121T200000Z", PIXELS)
    with pytest.raises(HeldOutFrameError, match="not for fits"):
        frame_input(tmp_path, pore)
    early = write_frame(tmp_path, PORE, "20260115T200000Z", PIXELS)
    assert frame_input(tmp_path, early).verdict["fit"]


def test_archive_guard(tmp_path: Path) -> None:
    folder = tmp_path / "archive" / PORE / "2026-06-01"
    folder.mkdir(parents=True)
    (folder / "20260601T000000Z_abcdef12.jpg").write_bytes(b"x")
    (folder / "20260601T000000Z_abcdef12.json").write_text("{}")
    with pytest.raises(HeldOutFrameError, match="held out"):
        frame_input(tmp_path, f"archive/{PORE}/2026-06-01/20260601T000000Z_abcdef12.jpg")


def _quarantined_frame(tmp_path: Path, folder_name: str) -> Path:
    """A real development frame and its sidecar moved into ``<root>/<folder_name>/``."""
    key = write_frame(tmp_path / "staging", VP, "20261121T182101Z", PIXELS)
    target = tmp_path / folder_name
    target.mkdir(parents=True)
    source = tmp_path / "staging" / key
    for path in (source, source.with_suffix(".json")):
        os.replace(path, target / path.name)
    return target / source.name


def test_quarantine_is_refused_through_a_parent_step(tmp_path: Path) -> None:
    frame = _quarantined_frame(tmp_path, "quarantine")
    (tmp_path / "frames").mkdir()
    # Four parts and a valid file name: only the quarantine guard can refuse this key.
    with pytest.raises(HeldOutFrameError, match="D-022"):
        frame_input(tmp_path, f"frames/../quarantine/{frame.name}")


def test_quarantine_is_refused_through_a_linked_folder_in_any_case(tmp_path: Path) -> None:
    frame = _quarantined_frame(tmp_path, "QUARANTINE")
    day = tmp_path / "frames" / VP / "2026-11-21"
    day.parent.mkdir(parents=True)
    day.symlink_to(frame.parent, target_is_directory=True)
    with pytest.raises(HeldOutFrameError, match="D-022"):
        frame_input(tmp_path, f"frames/{VP}/2026-11-21/{frame.name}")


def test_a_sidecar_time_far_from_the_key_is_refused(tmp_path: Path) -> None:
    key = write_frame(
        tmp_path, VP, "20261121T182101Z", PIXELS, image_time_utc="2026-11-21T19:21:01Z"
    )
    with pytest.raises(HeldOutFrameError, match="from the key's stamp"):
        frame_input(tmp_path, key)


def test_bad_keys_and_missing_files(tmp_path: Path) -> None:
    with pytest.raises(HeldOutFrameError):
        frame_input(tmp_path, "frames/x.y/2026-11-21/bad.jpg")
    with pytest.raises(HeldOutFrameError, match="not a"):
        frame_input(tmp_path, "renders/x")
    with pytest.raises(FileNotFoundError):
        frame_input(tmp_path, f"frames/{VP}/2026-11-21/20261121T182101Z_abcdef12.jpg")
    unknown = write_frame(tmp_path, "nowhere.view", "20261121T182101Z", PIXELS)
    with pytest.raises(HeldOutFrameError, match="cannot be placed"):
        frame_input(tmp_path, unknown)
