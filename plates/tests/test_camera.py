"""The renderer's pinhole conventions (renderer/src/math/camera.ts and its tests), in Python."""

from __future__ import annotations

import math

import numpy as np
import pytest

from snow_globe_plates.camera import Pose, basis, pixel_rays, pixels_per_degree, project


def pose(**o: float) -> Pose:
    base = {"heading_deg": 0.0, "pitch_deg": 0.0, "roll_deg": 0.0, "hfov_deg": 60.0}
    base.update(o)
    return Pose(
        base["heading_deg"], base["pitch_deg"], base["roll_deg"], base["hfov_deg"], 320, 180
    )


@pytest.mark.parametrize(
    ("h", "p", "f"),
    [
        (0, 0, (0, 1, 0)),
        (90, 0, (1, 0, 0)),
        (180, 0, (0, -1, 0)),
        (270, 0, (-1, 0, 0)),
        (0, 90, (0, 0, 1)),
    ],
)
def test_optical_axis_by_heading_and_pitch(
    h: float, p: float, f: tuple[float, float, float]
) -> None:
    fwd, _, _ = basis(pose(heading_deg=h, pitch_deg=p))
    assert np.allclose(fwd, f, atol=1e-12)


def test_level_camera_right_is_clockwise_of_heading() -> None:
    _, right, up = basis(pose(heading_deg=30))
    assert up[2] == pytest.approx(1.0)
    assert math.degrees(math.atan2(right[0], right[1])) % 360 == pytest.approx(120.0)


def test_positive_roll_turns_the_camera_clockwise_from_behind() -> None:
    _, right, up = basis(pose(roll_deg=90))
    assert up[0] == pytest.approx(1.0)
    assert right[2] == pytest.approx(-1.0)


def test_field_of_view_spans_the_frame_edges() -> None:
    b = pose(hfov_deg=60)
    left, right = pixel_rays([0.0, 320.0], [90.0, 90.0], b)
    assert math.degrees(math.acos(float(left @ right))) == pytest.approx(60.0, abs=1e-9)
    top, bottom = pixel_rays([160.0, 160.0], [0.0, 180.0], b)
    vfov = 2 * math.degrees(math.atan(math.tan(math.radians(30)) * 180 / 320))
    assert math.degrees(math.acos(float(top @ bottom))) == pytest.approx(vfov, abs=1e-9)


def test_projection_inverts_pixel_rays_and_rejects_behind() -> None:
    b = pose(heading_deg=211, pitch_deg=7, roll_deg=-3)
    px = np.array([0.5, 100.25, 319.5, 160.0])
    py = np.array([0.5, 33.75, 179.5, 90.0])
    uv, z = project(pixel_rays(px, py, b), b)
    assert np.allclose(uv[:, 0], px, atol=1e-9) and np.allclose(uv[:, 1], py, atol=1e-9)
    assert np.all(z > 0)
    fwd, _, _ = basis(b)
    _, zb = project(-fwd, b)
    assert float(zb) < 0


def test_pixels_per_degree_and_validation() -> None:
    assert pixels_per_degree(pose(hfov_deg=90)) == pytest.approx(160.0 * math.pi / 180.0)
    with pytest.raises(ValueError):
        Pose(0, 0, 0, 0.0, 10, 10)
    with pytest.raises(ValueError):
        Pose(0, 95, 0, 60, 10, 10)
    assert Pose.from_dict(pose(hfov_deg=50).to_dict()) == pose(hfov_deg=50)
