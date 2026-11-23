"""DEM reading and the per-pixel ray cast (plates.dem), on a synthetic cone mountain."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
from synth import LAT0, LON0, STEP, cone_heights, write_geotiff

from snow_globe_plates.camera import Pose, pixel_rays
from snow_globe_plates.dem import Dem, horizon_profile, render_view, skyline_rows
from snow_globe_plates.geodesy import az_el, enu, initial_bearing_deg

CAM = (46.78, -121.75, 1610.0)  # 7.8 km due south of the peak at (46.85, -121.75), outside the cone


@pytest.fixture
def dem(tmp_path: Path) -> Dem:
    return Dem.from_geotiff(write_geotiff(tmp_path / "dem.tif", cone_heights()))


def test_reads_the_grid_and_samples(dem: Dem) -> None:
    assert dem.z.shape == (400, 300) and dem.lon0 == LON0 and dem.lat0 == LAT0
    west, _south, east, north = dem.bounds
    assert (west, north) == (LON0, LAT0) and east == pytest.approx(LON0 + 300 * STEP)
    assert float(dem.sample([46.85], [-121.75])[0]) == pytest.approx(4000.0, abs=20.0)
    assert math.isnan(float(dem.sample([10.0], [10.0])[0]))
    h, lat, lon = dem.max_near(46.8502, -121.7503, 100.0)
    assert (
        h == pytest.approx(4000.0, abs=20.0)
        and abs(lat - 46.85) < 0.0005
        and abs(lon + 121.75) < 0.0005
    )
    assert len(dem.sha256) == 64 and dem.to_record()["shape"] == [400, 300]


def test_refuses_other_layouts(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="EPSG:4326"):
        Dem.from_geotiff(write_geotiff(tmp_path / "x.tif", cone_heights(), epsg=3857))
    from PIL import Image

    Image.fromarray(np.zeros((4, 4), np.uint8)).save(tmp_path / "y.tif")
    with pytest.raises(ValueError, match="float32"):
        Dem.from_geotiff(tmp_path / "y.tif")


def test_horizon_and_skyline_meet_the_cone_top(dem: Dem) -> None:
    peak_dir = enu(CAM, 46.85, -121.75, 4000.0)
    az, el, _ = az_el(peak_dir)
    hz, _ = horizon_profile(
        dem, CAM, [float(az)], min_distance_m=500, max_distance_m=12_000, step_m=10
    )
    assert float(hz[0]) == pytest.approx(float(el), abs=0.15)
    pose = Pose(float(az), 15.0, 0.0, 60.0, 640, 360)
    rows = skyline_rows(dem, CAM, pose, np.array([320.0]), min_distance_m=500)
    _, el_row, _ = az_el(pixel_rays(np.array([320.0]), rows, pose))
    assert float(el_row[0]) == pytest.approx(float(el), abs=0.2)


def test_render_view_heights_distances_and_sky(dem: Dem) -> None:
    az = initial_bearing_deg(CAM[0], CAM[1], 46.85, -121.75)
    pose = Pose(az, 15.0, 0.0, 60.0, 320, 180)
    view = render_view(dem, CAM, pose, step_px=2, max_distance_m=12_000)
    assert view.distance_m.shape == (90, 160)
    # The top rows are sky; the centre column's terrain height rises toward the peak.
    assert view.sky[0].all()
    col = view.height_m[:, 80]
    terrain = np.isfinite(col)
    assert terrain.any() and np.nanmax(col) <= 4000.0 + 1.0
    top = np.argmax(terrain)
    assert col[top] > 3500.0  # the first terrain row under the sky is near the summit
    assert not view.unknown[:, 80].any()
