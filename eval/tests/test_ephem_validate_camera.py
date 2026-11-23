"""The camera-frame transform: conventions, and parity with the renderer's camera.ts (PRD §8.4)."""

from __future__ import annotations

import math

import numpy as np
import pytest
from conftest import NODE_REASON, REPO_RENDERER
from snow_globe_common.manifest import load_manifests

from snow_globe_eval.ephem_validate.camera import (
    CameraPose,
    camera_axes,
    camera_validation,
    enu,
    grid_directions,
    in_frame,
    pose_for,
    project,
    run_renderer_camera,
)
from snow_globe_eval.ephem_validate.compare import BodyComparison, refraction_array
from snow_globe_eval.ephem_validate.report_md import camera_section

needs_renderer_camera = pytest.mark.skipif(
    NODE_REASON is not None or not (REPO_RENDERER / "src" / "math" / "camera.ts").is_file(),
    reason=str(NODE_REASON or "renderer/src/math/camera.ts not present"),
)


def _pose(**kw: float) -> CameraPose:
    base = {"heading_deg": 0.0, "pitch_deg": 0.0, "roll_deg": 0.0, "hfov_deg": 60.0}
    base.update(kw)
    return CameraPose(width=320, height=180, source="test", **base)  # type: ignore[arg-type]


def test_axes_follow_the_renderer_conventions() -> None:
    """camera.test.ts's cases: heading clockwise from north, pitch up, roll clockwise."""
    for heading, pitch, forward in (
        (0, 0, (0, 1, 0)),
        (90, 0, (1, 0, 0)),
        (180, 0, (0, -1, 0)),
        (270, 0, (-1, 0, 0)),
        (0, 90, (0, 0, 1)),
    ):
        _, f, _ = camera_axes(_pose(heading_deg=heading, pitch_deg=pitch))
        assert f == pytest.approx(forward, abs=1e-12)
    right, forward, up = camera_axes(_pose(heading_deg=0.0, roll_deg=90.0))
    assert up == pytest.approx((1, 0, 0), abs=1e-12)  # top of frame tilts to the right
    assert right[2] == pytest.approx(-1.0, abs=1e-12)
    for k in range(20):
        r, f, u = camera_axes(
            _pose(heading_deg=k * 37.3, pitch_deg=-80 + k * 7.1, roll_deg=-170 + k * 17.0)
        )
        assert np.cross(r, u) == pytest.approx(-f, abs=1e-12)  # right x up = backward


def test_projection_centre_edges_and_round_trip() -> None:
    pose = _pose(heading_deg=211.0, pitch_deg=7.0, roll_deg=-3.0)
    _, forward, _ = camera_axes(pose)
    px, py, depth = project(pose, forward[None, :])
    assert (px[0], py[0], depth[0] > 0) == (pytest.approx(160.0), pytest.approx(90.0), True)
    level = _pose(hfov_deg=60.0)
    px, py, _ = project(level, enu([0.0, 0.0], [-30.0, 30.0]))
    assert px.tolist() == pytest.approx([0.0, 320.0], abs=1e-9)
    alt_az = grid_directions(pose, 9, 5)
    px, py, depth = project(pose, enu(alt_az[:, 0], alt_az[:, 1]))
    assert in_frame(pose, px, py, depth).all()
    xs = np.linspace(0.5, 319.5, 9)
    assert sorted(set(np.round(px, 6))) == pytest.approx(xs.tolist(), abs=1e-6)
    behind = project(pose, -forward[None, :])[2]
    assert behind[0] < 0


def test_pose_for_is_the_scene_builders_pose() -> None:
    manifests = load_manifests()
    vp = manifests["point-reyes.beach"]
    pose = pose_for(vp)
    if vp.raw.get("pose") is None:
        assert pose.heading_deg == vp.camera.view_azimuth_deg
        assert (pose.pitch_deg, pose.roll_deg, pose.hfov_deg) == (0.0, 0.0, 60.0)
        assert pose.source.startswith("nominal")
    assert (pose.width, pose.height) == tuple(vp.truth_camera.image_px or (1920, 1080))
    assert pose.px_per_deg == pytest.approx(
        pose.width / 2 / math.tan(math.radians(pose.hfov_deg / 2)) * math.pi / 180
    )


@needs_renderer_camera
def test_renderer_camera_matches_the_independent_transform() -> None:
    poses = [
        _pose(heading_deg=346.2),
        _pose(heading_deg=45.0, pitch_deg=3.0, roll_deg=1.5, hfov_deg=68.45),
        CameraPose(2.11, 10.0, 1.69, 68.45, 1920, 1080, "plate v0.1 initial pose"),
    ]
    cases = [{"pose": p.renderer_pose(), "alt_az": grid_directions(p).tolist()} for p in poses]
    results = run_renderer_camera(cases)
    for pose, case, result in zip(poses, cases, results, strict=True):
        alt_az = np.asarray(case["alt_az"])
        px, py, _ = project(pose, enu(alt_az[:, 0], alt_az[:, 1]))
        node = np.asarray(result["px"], dtype=np.float64)
        assert np.max(np.hypot(node[:, 0] - px, node[:, 1] - py)) < 1e-9
        light = np.asarray(result["enu"])
        assert light == pytest.approx(enu(alt_az[:, 0], alt_az[:, 1]), abs=1e-14)
        back = np.asarray(result["back"])
        assert back[:, 0] == pytest.approx(alt_az[:, 0], abs=1e-9)


def _synthetic_body(body: str, alt: list[float], az: list[float]) -> BodyComparison:
    """A comparison whose ephemeris is exact (ours = reference) and whose reference refracts
    with eval's formula: the only gap left is the SceneSpec's geometric altitude."""
    ref_alt = np.asarray(alt, dtype=np.float64)
    ref_az = np.asarray(az, dtype=np.float64)
    r = refraction_array(ref_alt)
    n = ref_alt.size
    return BodyComparison(
        source="synthetic",
        site_id="s",
        body=body,
        unix_ms=np.arange(n, dtype=np.float64) * 3.6e6,
        ref_alt=ref_alt,
        ref_az=ref_az,
        ref_alt_refr=ref_alt + r,
        ref_az_refr=ref_az,
        ours_alt=ref_alt.copy(),
        ours_az=ref_az.copy(),
        ours_alt_refr=ref_alt + r,
        ours_refr_at_ref_alt=r,
        radec_sep=np.full(n, math.nan),
        illum_diff_pct=np.full(n, math.nan),
    )


@needs_renderer_camera
def test_camera_validation_measures_the_bodies_in_frame() -> None:
    """Review finding: the in-frame branch (ephemeris px, SceneSpec gap, light direction)."""
    pose = CameraPose(200.0, 4.0, 2.5, 60.0, 1920, 1080, "test: tilted and rolled")
    #       in frame ×3          | behind | in frame but below the -1 deg domain
    alt = [10.0, 0.5, 3.0, 10.0, -3.0]
    az = [200.0, 190.0, 215.0, 20.0, 200.0]
    sun = _synthetic_body("sun", alt, az)
    out = camera_validation({"s": pose}, [sun])
    assert out["renderer"]["ran"], out["renderer"]
    entry = out["sites"]["s"]["sun"]
    px, py, depth = project(pose, enu(sun.ref_alt_refr, sun.ref_az_refr))
    keep = in_frame(pose, px, py, depth) & sun.refraction_domain
    assert entry["n_in_frame"] == int(keep.sum()) == 3
    assert entry["ephemeris_px"]["worst"] < 1e-9  # identical ephemeris: no pixel error
    gx, gy, _ = project(pose, enu(sun.ours_alt[keep], sun.ours_az[keep]))
    ax, ay, _ = project(pose, enu(sun.ref_alt_refr[keep], sun.ref_az_refr[keep]))
    gap = np.hypot(gx - ax, gy - ay)
    assert entry["scene_geometric_vs_apparent_px"]["worst"] == pytest.approx(gap.max(), abs=1e-6)
    r = refraction_array(sun.ref_alt[keep])
    assert gap == pytest.approx(r * pose.px_per_deg, rel=0.1)  # the refraction, in pixels
    assert entry["light_direction_vs_apparent_deg"]["worst"] == pytest.approx(r.max(), abs=1e-9)
    assert entry["light_direction_vs_geometric_deg"]["worst"] < 1e-9
    assert set(entry["scene_geometric_vs_apparent_px_by_band"]) == {"0..2", "2..5", "5..15"}
    counts = out["parity_directions"]
    assert counts["front_mismatches"] == 0
    assert counts["behind"] == 375  # the grid's antipodes
    assert counts["in_front"] == 375 + 4 * 3  # the grid, and four variants of the 3 in frame
    assert out["parity_renderer_vs_independent_px"]["n"] == counts["in_front"]
    assert out["parity_renderer_vs_independent_px"]["worst"] < 1e-9
    text = "\n".join(camera_section(out))
    assert "| s | sun | 3 |" in text and "disagree on in front or behind for 0" in text
