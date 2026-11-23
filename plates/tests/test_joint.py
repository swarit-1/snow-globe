"""Joint pose and position from stars and landmarks (plates.joint), on synthetic data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from snow_globe_plates.camera import Pose, project
from snow_globe_plates.geodesy import destination, offset_position
from snow_globe_plates.joint import ray_difference_deg, solve_joint, solve_stars_only
from snow_globe_plates.pose import Landmark, landmark_directions
from snow_globe_plates.stars import StarObservation, star_directions

START = (46.78, -121.74)
TRUE_OFFSET = (260.0, 340.0)
TRUE = Pose(2.0, 10.0, 1.7, 68.5, 1920, 1080)
AGL = 10.0
SPOTS = [
    (-3.0, 7_200.0, 3_860.0),
    (12.0, 5_300.0, 2_920.0),
    (17.0, 7_700.0, 3_380.0),
    (-20.0, 7_600.0, 4_300.0),
]


def ground(lat: float, lon: float) -> float:
    """A gently sloping synthetic ground."""
    return 1_650.0 + (lat - START[0]) * 20_000.0


def _true_camera() -> tuple[float, float, float]:
    lat, lon = offset_position(START[0], START[1], *TRUE_OFFSET)
    return (lat, lon, ground(lat, lon) + AGL)


def _stars() -> list[StarObservation]:
    cam = _true_camera()
    t0 = datetime(2026, 11, 20, 2, tzinfo=UTC)
    out = []
    rng = np.random.default_rng(1)
    while len(out) < 30:
        ra, dec = float(rng.uniform(0, 360)), float(rng.uniform(30, 89))
        t = t0 + timedelta(hours=float(rng.uniform(0, 12)))
        s = StarObservation(ra, dec, 3.0, t, (0.0, 0.0))
        uv, z = project(star_directions([s], cam[0], cam[1]), TRUE)
        x, y = uv[0]
        if z[0] > 0 and 50 < x < 1870 and 20 < y < 500:
            out.append(StarObservation(ra, dec, 3.0, t, (float(x), float(y))))
    return out


def _landmarks() -> list[Landmark]:
    cam = _true_camera()
    out = []
    for i, (az, dist, h) in enumerate(SPOTS):
        lat, lon = destination(cam[0], cam[1], az, dist)
        lm = Landmark(f"l{i}", float(lat), float(lon), h, (0.0, 0.0), 3.0)
        uv, _ = project(landmark_directions(cam, [lm]), TRUE)
        out.append(
            Landmark(f"l{i}", float(lat), float(lon), h, (float(uv[0, 0]), float(uv[0, 1])), 3.0)
        )
    return out


def test_stars_alone_fix_the_orientation_wherever_the_camera_is() -> None:
    cam = _true_camera()
    pose, res = solve_stars_only(_stars(), cam[0], cam[1], Pose(0.0, 8.0, 0.0, 75.0, 1920, 1080))
    assert np.abs(res).max() < 1e-4
    assert pose.hfov_deg == pytest.approx(TRUE.hfov_deg, abs=1e-5)
    # 500 m away the stars barely move (the local vertical turns by 0.0045 deg).
    far_pose, _ = solve_stars_only(_stars(), START[0], START[1], pose)
    assert ray_difference_deg(far_pose, pose)[1] < 0.01


def test_joint_solve_recovers_pose_and_position() -> None:
    sol = solve_joint(
        START, ground, _landmarks(), _stars(), Pose(1.0, 9.0, 1.0, 70.0, 1920, 1080), agl_m=AGL
    )
    assert sol.offset_m[0] == pytest.approx(TRUE_OFFSET[0], abs=1.0)
    assert sol.offset_m[1] == pytest.approx(TRUE_OFFSET[1], abs=1.0)
    for name in ("heading_deg", "pitch_deg", "roll_deg", "hfov_deg"):
        assert getattr(sol.pose, name) == pytest.approx(getattr(TRUE, name), abs=1e-3)
    assert sol.max_landmark_angle_deg < 0.01 and sol.star_angle_deg[1] < 0.01
    assert sol.landmark_vs_star_deg[1] < 0.01
    d = sol.to_dict()
    assert d["landmark_reprojection_passes_1deg"] is True and d["stars"]["count"] == 30


def test_joint_solve_needs_enough_inputs() -> None:
    with pytest.raises(ValueError, match="three landmarks"):
        solve_joint(START, ground, _landmarks()[:2], _stars(), TRUE)
    with pytest.raises(ValueError, match="three stars"):
        solve_joint(START, ground, _landmarks(), _stars()[:2], TRUE)
