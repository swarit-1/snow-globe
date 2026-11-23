"""Base-frame median combine and candidate rows (plates.base), on synthetic frames."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from snow_globe_plates.base import candidate_rows, integer_shift, median_combine
from snow_globe_plates.frames import FrameInput
from snow_globe_plates.sun import solar_noon_utc, sun_alt_az


def _scene(seed: int = 0, shape=(270, 480)) -> np.ndarray:
    rng = np.random.default_rng(seed)
    from scipy import ndimage

    base = ndimage.gaussian_filter(rng.uniform(0, 1, shape), 3.0)
    base = (base - base.min()) / (base.max() - base.min())
    return np.stack([base * 180 + 30, base * 170 + 35, base * 160 + 40], axis=-1)


def test_median_undoes_exposure_steps_and_drops_a_transient() -> None:
    scene = _scene()
    a = scene.copy()
    b = scene * 0.8
    c = scene * 1.15
    c[100:110, 200:210] = 255.0  # a passer-by in one frame only
    mask = np.ones(scene.shape[:2], bool)
    base, report = median_combine([np.clip(x, 0, 255).astype(np.uint8) for x in (a, b, c)], mask)
    assert np.abs(base.astype(float) - np.clip(scene, 0, 255)).mean() < 1.5
    assert np.abs(base[100:110, 200:210].astype(float) - scene[100:110, 200:210]).mean() < 3.0
    assert [r["shift_px"] for r in report] == [[0, 0], [0, 0], [0, 0]]
    assert report[1]["gains_rgb"][0] == pytest.approx(1.25, abs=0.02)


def test_a_moved_camera_is_refused() -> None:
    scene = np.clip(_scene(), 0, 255).astype(np.uint8)
    moved = np.roll(scene, 6, axis=1)
    assert integer_shift(scene.astype(float), moved.astype(float), width=480) == (6, 0)
    with pytest.raises(ValueError, match="aim moved"):
        median_combine([scene, moved], np.ones(scene.shape[:2], bool))
    with pytest.raises(ValueError):
        median_combine([], np.ones((2, 2), bool))


def test_candidate_rows_report_sun_and_noon_offset() -> None:
    noon = solar_noon_utc(datetime(2026, 11, 21, 12, tzinfo=UTC), -121.74)
    assert noon.hour == 19 and 45 <= noon.minute <= 59  # 11:5x PST
    frames = [
        FrameInput(
            "k1", Path("x"), {"luma_mean": 130.0}, datetime(2026, 11, 21, 18, 21, tzinfo=UTC), {}
        ),
        FrameInput("k2", Path("y"), {}, datetime(2026, 11, 21, 6, 0, tzinfo=UTC), {}),
    ]
    rows = candidate_rows(frames, 46.78, -121.74)
    assert [r["key"] for r in rows] == ["k1"]  # night frame left out
    alt, az = sun_alt_az(frames[0].image_time, 46.78, -121.74)
    assert rows[0]["sun_alt_deg"] == pytest.approx(alt, abs=0.01)
    assert 19.0 < alt < 21.0 and 150.0 < az < 175.0
    assert -100.0 < rows[0]["minutes_from_solar_noon"] < -85.0
