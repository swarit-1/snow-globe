"""Pose from landmarks (PRD §8.5 step 3) and its reprojection check (step 7), on synthetic data."""

from __future__ import annotations

import numpy as np
import pytest

from snow_globe_plates.camera import Pose, project
from snow_globe_plates.geodesy import destination
from snow_globe_plates.pose import Landmark, landmark_directions, solve_pose

CAM = (46.78, -121.74, 1650.0)
TRUE = Pose(4.0, 9.0, 1.5, 70.0, 1920, 1080)
SPOTS = [
    (-25.0, 7_000.0, 4_300.0),
    (-5.0, 7_200.0, 3_850.0),
    (12.0, 5_300.0, 2_900.0),
    (17.0, 7_700.0, 3_380.0),
    (30.0, 3_000.0, 2_250.0),
]


def _landmarks(noise: float = 0.0, seed: int = 0) -> list[Landmark]:
    rng = np.random.default_rng(seed)
    out = []
    for i, (az, dist, h) in enumerate(SPOTS):
        lat, lon = destination(CAM[0], CAM[1], az, dist)
        d = landmark_directions(CAM, [Landmark(f"l{i}", float(lat), float(lon), h, (0, 0))])
        uv, _ = project(d, TRUE)
        px = uv[0] + rng.normal(0.0, noise, 2) if noise else uv[0]
        out.append(Landmark(f"l{i}", float(lat), float(lon), h, (float(px[0]), float(px[1])), 3.0))
    return out


def test_exact_marks_recover_the_pose() -> None:
    sol = solve_pose(CAM, _landmarks(), Pose(0.0, 5.0, 0.0, 80.0, 1920, 1080))
    for name in ("heading_deg", "pitch_deg", "roll_deg", "hfov_deg"):
        assert getattr(sol.pose, name) == pytest.approx(getattr(TRUE, name), abs=1e-6)
    assert sol.rms_px < 1e-6 and sol.max_angle_deg < 1e-6 and sol.passes_reprojection
    assert sol.max_loo_angle_deg is not None and sol.max_loo_angle_deg < 1e-6


def test_noisy_marks_stay_well_inside_one_degree() -> None:
    sol = solve_pose(CAM, _landmarks(noise=3.0, seed=4), Pose(0.0, 5.0, 0.0, 80.0, 1920, 1080))
    assert sol.max_angle_deg < 0.5 and sol.passes_reprojection
    assert abs(sol.pose.heading_deg - TRUE.heading_deg) < 0.5
    d = sol.to_dict()
    assert d["reprojection_limit_deg"] == 1.0 and len(d["landmarks"]) == 5


def test_a_wrong_mark_shows_up_left_out() -> None:
    marks = _landmarks()
    bad = marks[2]
    marks[2] = Landmark(
        bad.name, bad.lat, bad.lon, bad.height_m, (bad.px[0] + 120.0, bad.px[1]), 3.0
    )
    sol = solve_pose(CAM, marks, Pose(0.0, 5.0, 0.0, 80.0, 1920, 1080))
    loo = {f.name: f.loo_angle_deg for f in sol.landmarks}
    assert loo["l2"] is not None and loo["l2"] > 1.0
    assert not sol.passes_reprojection


def test_needs_three_distinct_landmarks() -> None:
    marks = _landmarks()
    with pytest.raises(ValueError, match="three"):
        solve_pose(CAM, marks[:2], TRUE)
    with pytest.raises(ValueError, match="repeat"):
        solve_pose(CAM, [marks[0], marks[0], marks[1]], TRUE)
    with pytest.raises(ValueError):
        Landmark("x", 0.0, 0.0, 0.0, (1.0, 2.0), sigma_px=0.0)
