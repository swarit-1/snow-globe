"""Lambert conformal mapping, checked against true (i, j, lat, lon) tuples from a real message."""

from __future__ import annotations

import math

import pytest

from lookout_forecast.grid import HRRR_CONUS, GridMismatchError, LambertConformalGrid
from lookout_forecast.points import destination


def _lon_diff(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def test_fixture_grid_is_the_built_in_grid(grid_fixture: dict) -> None:
    grid = LambertConformalGrid.from_grib_keys(grid_fixture["grid_keys"])
    assert grid == HRRR_CONUS
    HRRR_CONUS.check_same(grid_fixture["grid_keys"])


def test_cell_centres_match_ecc_codes(grid_fixture: dict) -> None:
    worst = 0.0
    for cell in grid_fixture["cells"]:
        lat, lon = HRRR_CONUS.ij_latlon(cell["i"], cell["j"])
        assert abs(lat - cell["lat"]) < 1e-9
        assert _lon_diff(lon, cell["lon"]) < 1e-9
        worst = max(worst, abs(lat - cell["lat"]), _lon_diff(lon, cell["lon"]))
    assert worst < 1e-9


def test_true_lat_lon_maps_back_to_its_cell(grid_fixture: dict) -> None:
    for cell in grid_fixture["cells"]:
        fi, fj = HRRR_CONUS.fractional_ij(cell["lat"], cell["lon"])
        assert abs(fi - cell["i"]) < 1e-6 and abs(fj - cell["j"]) < 1e-6
        assert HRRR_CONUS.nearest_ij(cell["lat"], cell["lon"]) == (cell["i"], cell["j"])
        assert HRRR_CONUS.flat_index(cell["i"], cell["j"]) == cell["j"] * 1799 + cell["i"]


def test_fixture_records_the_whole_grid_measurement(grid_fixture: dict) -> None:
    measured = grid_fixture["measured"]
    assert measured["cells"] == 1799 * 1059
    assert measured["inverse_round_trip_cells_ok"] == measured["cells"]
    assert measured["forward_max_error_km"] < 1e-6


def test_vectorised_and_scalar_paths_agree() -> None:
    lat, lon = HRRR_CONUS.latlon_arrays()
    for i, j in [(0, 0), (1798, 1058), (284, 919), (900, 3)]:
        k = j * 1799 + i
        assert (lat[k], lon[k]) == pytest.approx(HRRR_CONUS.ij_latlon(i, j), abs=1e-9)
    fi, fj = HRRR_CONUS.fractional_ij_arrays([46.78455], [-121.74065])
    assert (fi[0], fj[0]) == pytest.approx(HRRR_CONUS.fractional_ij(46.78455, -121.74065))


def test_points_outside_the_grid() -> None:
    assert HRRR_CONUS.nearest_ij(0.0, -100.0) is None
    assert HRRR_CONUS.nearest_ij(65.0, -150.0) is None
    with pytest.raises(IndexError):
        HRRR_CONUS.flat_index(1799, 0)


def test_grid_mismatch_is_detected(grid_fixture: dict) -> None:
    keys = dict(grid_fixture["grid_keys"])
    keys["DxInMetres"] = 2999.0
    with pytest.raises(GridMismatchError):
        HRRR_CONUS.check_same(keys)
    keys = dict(grid_fixture["grid_keys"], gridType="regular_ll")
    with pytest.raises(GridMismatchError):
        HRRR_CONUS.check_same(keys)


def test_only_scanning_mode_64_is_supported() -> None:
    with pytest.raises(NotImplementedError):
        LambertConformalGrid(
            10, 10, 21.0, 237.0, 38.5, 262.5, 38.5, 38.5, 3000, 3000, 6371229, scanning_mode=0
        )


def test_secant_cone_formula_is_consistent() -> None:
    grid = LambertConformalGrid(50, 50, 30.0, 250.0, 40.0, 260.0, 33.0, 45.0, 3000, 3000, 6371229)
    lat, lon = grid.ij_latlon(17.5, 21.25)
    assert grid.fractional_ij(lat, lon) == pytest.approx((17.5, 21.25), abs=1e-9)


@pytest.mark.parametrize("lon", [-122.0, -97.5, -70.0])
def test_earth_relative_wind_rotation(lon: float) -> None:
    """A grid-relative wind along +j must point along the grid's local +y direction."""
    lat = 40.0
    fi, fj = HRRR_CONUS.fractional_ij(lat, lon)
    lat2, lon2 = HRRR_CONUS.ij_latlon(fi, fj + 0.01)
    east = math.radians(lon2 - lon) * math.cos(math.radians(lat))
    north = math.radians(lat2 - lat)
    bearing_of_plus_y = math.degrees(math.atan2(east, north)) % 360.0
    u, v = HRRR_CONUS.earth_relative_wind(0.0, 1.0, lon)
    bearing_of_wind = math.degrees(math.atan2(u, v)) % 360.0
    assert abs((bearing_of_wind - bearing_of_plus_y + 180) % 360 - 180) < 0.01
    assert math.hypot(u, v) == pytest.approx(1.0)
    if lon == -97.5:
        assert (u, v) == pytest.approx((0.0, 1.0))


def test_destination_is_used_with_the_grid() -> None:
    lat, lon = destination(46.78455, -121.74065, 0.0, 3.0)
    fi0, fj0 = HRRR_CONUS.fractional_ij(46.78455, -121.74065)
    fi1, fj1 = HRRR_CONUS.fractional_ij(lat, lon)
    assert math.hypot(fi1 - fi0, fj1 - fj0) == pytest.approx(1.0, abs=0.02)  # 3 km = 1 cell
