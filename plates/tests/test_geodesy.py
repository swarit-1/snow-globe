"""Geodesy (PRD §8.5 step 3): ENU directions with Earth curvature, bearings, destinations."""

from __future__ import annotations

import math

import numpy as np
import pytest

from snow_globe_plates.geodesy import (
    az_el,
    destination,
    enu,
    initial_bearing_deg,
    offset_position,
    surface_distance_m,
)

CAM = (46.78, -121.74, 1650.0)


def test_point_due_north_is_at_azimuth_zero_and_drops_with_curvature() -> None:
    lat, lon = destination(CAM[0], CAM[1], 0.0, 10_000.0)
    v = enu(CAM, lat, lon, CAM[2])
    az, el, rng = az_el(v)
    assert float(az) == pytest.approx(0.0, abs=1e-6) or float(az) == pytest.approx(360.0, abs=1e-6)
    # Same height 10 km away sits d^2 / 2R = 7.8 m below the camera's horizontal plane.
    drop = 10_000.0**2 / (2 * 6_371_000.0)
    assert float(v[..., 2]) == pytest.approx(-drop, rel=0.02)
    assert float(el) == pytest.approx(-math.degrees(drop / 10_000.0), abs=2e-3)
    assert float(rng) == pytest.approx(10_000.0, rel=2e-3)


def test_elevation_angle_of_a_summit() -> None:
    lat, lon = destination(CAM[0], CAM[1], 90.0, 7_000.0)
    v = enu(CAM, lat, lon, CAM[2] + 2_000.0)
    _, el, _ = az_el(v)
    horizontal = float(np.hypot(v[0], v[1]))  # ellipsoidal metres (0.28 % more than spherical)
    assert horizontal == pytest.approx(7_000.0, rel=0.004)
    expected = math.degrees(math.atan2(2_000.0 - horizontal**2 / (2 * 6_380_000.0), horizontal))
    assert float(el) == pytest.approx(expected, abs=0.005)


@pytest.mark.parametrize("bearing", [0.0, 45.0, 135.0, 225.0, 300.0])
def test_destination_and_bearing_round_trip(bearing: float) -> None:
    lat, lon = destination(CAM[0], CAM[1], bearing, 8_000.0)
    got = initial_bearing_deg(CAM[0], CAM[1], float(lat), float(lon))
    assert abs((got - bearing + 180.0) % 360.0 - 180.0) < 1e-6
    assert surface_distance_m(CAM[0], CAM[1], float(lat), float(lon)) == pytest.approx(
        8_000.0, rel=1e-9
    )


def test_offset_position_moves_east_and_north() -> None:
    lat, lon = offset_position(CAM[0], CAM[1], 300.0, -400.0)
    v = enu(CAM, lat, lon, CAM[2])
    # Offsets are metres on the mean sphere; the ellipsoid's are up to 0.4 % longer here.
    assert float(v[0]) == pytest.approx(300.0, rel=0.004)
    assert float(v[1]) == pytest.approx(-400.0, rel=0.004)


def test_vectorised_shapes() -> None:
    lat = np.full((3, 4), CAM[0] + 0.01)
    lon = np.full((3, 4), CAM[1])
    v = enu(CAM, lat, lon, np.full((3, 4), CAM[2]))
    assert v.shape == (3, 4, 3)
    az, el, rng = az_el(v)
    assert az.shape == el.shape == rng.shape == (3, 4)
