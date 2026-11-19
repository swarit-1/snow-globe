"""Statistics behind the datum decisions (lookout-forecast datum-check), on synthetic arrays."""

from __future__ import annotations

import numpy as np
import pytest

from lookout_forecast.evidence import (
    _by_band,
    cloud_base_pressure_check,
    haversine_km,
    implied_height_m,
)


def test_haversine() -> None:
    assert haversine_km(0.0, 0.0, 0.0, 1.0) == pytest.approx(111.195, abs=0.01)
    assert haversine_km(46.0, 359.5, 46.0, -0.5) == pytest.approx(0.0, abs=1e-9)


def _pressure_at(z: np.ndarray, psfc: np.ndarray, t2m: np.ndarray) -> np.ndarray:
    """Pressure at height z above the surface in a standard-lapse-rate atmosphere."""
    return psfc * (1 - 0.0065 * z / t2m) ** (9.80665 / (287.05 * 0.0065))


def test_implied_height_is_close_for_low_and_high_bases() -> None:
    psfc = np.array([101325.0, 101325.0, 80000.0])
    t2m = np.array([288.15, 288.15, 270.0])
    z = np.array([300.0, 8000.0, 6000.0])
    got = implied_height_m(psfc, _pressure_at(z, psfc, t2m), t2m)
    assert np.all(np.abs(got - z) / z < 0.01)


def test_cloud_base_pressure_check_separates_valid_and_invalid_bases() -> None:
    terrain = np.array([0.0, 1500.0, 2000.0, 2500.0, 10.0])
    psfc = np.array([101325.0, 85000.0, 80000.0, 75000.0, 101000.0])
    t2m = np.full(5, 280.0)
    implied = np.array([500.0, 8000.0, 8000.0, 50.0, 7000.0])
    pbase = _pressure_at(implied, psfc, t2m)
    height = terrain + implied
    height[2] = 900.0  # below the 2000 m terrain although the pressure says 8 km up
    height[4] = 600.0  # above the terrain, but 6.4 km lower than the pressure says
    missing = np.array([False, False, False, True, False])
    out = cloud_base_pressure_check(
        (height, missing), (pbase, np.zeros(5, bool)), terrain, psfc, t2m
    )
    assert out["cells_with_height_and_pressure"] == 4
    assert out["below_terrain_cells"] == 1
    assert out["below_terrain_but_above_ground_by_pressure"] == 1
    assert out["grossly_inconsistent_cells"] == 2  # the valid 8 km base passes
    assert out["grossly_inconsistent_below_terrain"] == 1
    assert out["grossly_inconsistent_at_or_above_terrain"] == 1


def test_by_band() -> None:
    terrain = np.array([100.0, 600.0, 2100.0, 2600.0])
    values = np.array([200.0, 500.0, 2000.0, 3000.0])
    out = _by_band(values, terrain, np.ones(4, bool))
    assert out["500..1000 m"] == {"valid": 1, "below": 1, "fraction": 1.0}
    assert out["2000..2500 m"]["below"] == 1
    assert out["2500..6000 m"]["below"] == 0
    assert out["1000..1500 m"]["fraction"] is None
