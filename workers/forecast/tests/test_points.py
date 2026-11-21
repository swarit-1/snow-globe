"""Sample points per viewpoint (PRD §8.3): camera, view ray, landmarks, 5 x 5 block."""

from __future__ import annotations

import pytest
from snow_globe_common.manifest import great_circle_km, initial_bearing_deg

from snow_globe_forecast.grid import HRRR_CONUS
from snow_globe_forecast.points import destination, flat_indices, sample_points

from .conftest import RAINIER


def test_destination_round_trip() -> None:
    for bearing in (0.0, 45.0, 348.8, 200.0):
        for distance in (1.5, 22.5, 45.0):
            lat, lon = destination(46.78455, -121.74065, bearing, distance)
            assert great_circle_km(46.78455, -121.74065, lat, lon) == pytest.approx(
                distance, rel=1e-9
            )
            got = initial_bearing_deg(46.78455, -121.74065, lat, lon)
            assert abs((got - bearing + 180) % 360 - 180) < 1e-6


def test_rainier_points(manifests) -> None:
    vp = manifests[RAINIER]
    points = sample_points(vp, HRRR_CONUS)
    kinds = [p.kind for p in points]
    assert kinds.count("camera") == 1
    assert kinds.count("ray") == 30
    assert kinds.count("landmark") == 1
    assert kinds.count("neighborhood") == 25
    rays = [p for p in points if p.kind == "ray"]
    assert [p.distance_km for p in rays] == [pytest.approx(1.5 * k) for k in range(1, 31)]
    assert all(p.azimuth_deg == pytest.approx(348.8) for p in rays)
    landmark = next(p for p in points if p.kind == "landmark")
    assert landmark.id == "landmark:mount-rainier-summit-columbia-crest"
    assert landmark.distance_km == pytest.approx(7.74, abs=0.05)
    camera = points[0]
    assert (camera.i, camera.j) == (284, 919)
    centre = next(p for p in points if p.id == "nb:+0,+0")
    assert (centre.i, centre.j) == (camera.i, camera.j) and centre.azimuth_deg is None
    corner = next(p for p in points if p.id == "nb:-2,+2")
    assert (corner.i, corner.j) == (282, 921)
    assert len({p.id for p in points}) == len(points)


def test_every_viewpoint_is_inside_hrrr(manifests) -> None:
    for vp in manifests.values():
        points = sample_points(vp, HRRR_CONUS)
        assert all(p.in_grid for p in points), vp.id
        assert len(points) == 1 + 30 + len(vp.landmarks) + 25
        flats = flat_indices(HRRR_CONUS, points)
        assert all(isinstance(k, int) and 0 <= k < 1799 * 1059 for k in flats)


def test_ray_cells_follow_the_azimuth(manifests) -> None:
    vp = manifests["point-reyes.beach"]  # looks NNE (22.5 deg)
    points = sample_points(vp, HRRR_CONUS)
    camera = points[0]
    far = next(p for p in points if p.id == "ray:45.0km")
    assert far.j > camera.j  # north
    assert far.i > camera.i  # and east
    assert abs(far.cell_lat - far.lat) < 0.03 and abs(far.cell_lon - far.lon) < 0.03
