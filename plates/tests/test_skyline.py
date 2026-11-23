"""The per-band skyline check (plates.skyline.band_skyline_check) on a synthetic DEM view."""

from __future__ import annotations

import numpy as np
import pytest

from snow_globe_plates.camera import Pose, pixels_per_degree
from snow_globe_plates.dem import DemView
from snow_globe_plates.masks import BandSpec, band_terrain
from snow_globe_plates.skyline import band_skyline_check, observed_skyline

H, W = 60, 80
POSE = Pose(0.0, 0.0, 0.0, 60.0, W, H)


def _view(dem_top_right: int) -> DemView:
    """Summit-high terrain in columns 0-39 from row 10, upper-high in 40-79 from a given row."""
    rows = np.arange(H)[:, None] * np.ones((1, W))
    cols = np.ones((H, 1)) * np.arange(W)[None, :]
    sky = np.where(cols < 40, rows < 10, rows < dem_top_right)
    height = np.where(sky, np.nan, np.where(cols < 40, 4100.0, 3500.0))
    distance = np.where(sky, np.nan, 7000.0)
    return DemView(np.arange(W) + 0.5, np.arange(H) + 0.5, distance, height, sky, sky & False)


BANDS = [BandSpec("summit", min_height_m=4000.0), BandSpec("upper", max_height_m=4000.0)]


def test_each_band_is_checked_over_every_column_it_owns() -> None:
    view = _view(dem_top_right=6)  # the DEM puts the right half's ridge 4 rows too high
    observed = np.arange(H)[:, None] * np.ones((1, W)) < 10
    luma = np.where(observed, 0.6, 0.8)
    luma[:, 70:] = np.where(observed[:, 70:], 0.6, 0.1)  # dark tree tops on the far right
    terrain = band_terrain(
        view, BANDS, occluder=np.zeros((H, W), bool), overlay=np.zeros((H, W), bool)
    )
    out = band_skyline_check(view, terrain, observed, luma, POSE, bar_p90_deg=1.0)
    summit, upper = out["summit"], out["upper"]
    assert summit.columns_owned == 40 and summit.columns_checked == 40
    assert summit.abs_px == (0.0, 0.0, 0.0) and summit.passes is True
    assert upper.columns_owned == 40 and upper.columns_checked == 30
    assert upper.columns_skipped["tree_top_or_dark"] == 10
    assert upper.bias_px == -4.0 and upper.abs_px is not None
    assert upper.abs_px[1] / pixels_per_degree(POSE) > 1.0 and upper.passes is False
    record = upper.to_dict()
    assert record["x_range"] == [40, 79] and record["passes"] is False
    assert record["p90_abs_deg"] == pytest.approx(4.0 / pixels_per_degree(POSE), abs=1e-3)


def test_a_band_with_no_checkable_column_is_unchecked_not_passed() -> None:
    view = _view(dem_top_right=10)
    observed = np.arange(H)[:, None] * np.ones((1, W)) < 10
    luma = np.where(observed, 0.6, 0.1)  # every skyline column is a dark tree top
    terrain = band_terrain(
        view, BANDS, occluder=np.zeros((H, W), bool), overlay=np.zeros((H, W), bool)
    )
    out = band_skyline_check(view, terrain, observed, luma, POSE, bar_p90_deg=1.0)
    assert out["summit"].passes is None and out["summit"].to_dict()["columns_checked"] == 0
    assert observed_skyline(observed)[0] == 10.0
