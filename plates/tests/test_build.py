"""End to end: build a plate set from a synthetic data dir (plates.build, plates.cli)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from synth import cone_heights, write_frame, write_geotiff

from snow_globe_plates import cli
from snow_globe_plates.build import build
from snow_globe_plates.camera import Pose, project
from snow_globe_plates.dem import Dem, render_view
from snow_globe_plates.geodesy import offset_position
from snow_globe_plates.pose import Landmark, landmark_directions
from snow_globe_plates.stars import StarObservation, star_directions

VP = "rainier.paradise-mountain"
START = (46.78, -121.75)
OFFSET = (120.0, -150.0)
W, H = 320, 180
TRUE = Pose(0.5, 12.0, 1.0, 60.0, W, H)
AGL = 10.0


def _setup(root: Path) -> Path:
    dem_rel = f"plates/{VP}/sources/dem.tif"
    (root / dem_rel).parent.mkdir(parents=True)
    write_geotiff(root / dem_rel, cone_heights())
    dem = Dem.from_geotiff(root / dem_rel)
    lat, lon = offset_position(START[0], START[1], *OFFSET)
    cam = (lat, lon, float(dem.sample([lat], [lon])[0]) + AGL)
    view = render_view(dem, cam, TRUE, step_px=1, max_distance_m=12_000)
    rng = np.random.default_rng(0)
    texture = rng.uniform(-20, 20, (H, W))
    img = np.where(
        view.sky[..., None],
        np.array([90.0, 130.0, 210.0]),
        (200.0 + texture)[..., None] * np.ones(3),
    )
    keys = [
        write_frame(root, VP, f"2026112{i}T182101Z", np.clip(img * g, 0, 255))
        for i, g in ((1, 1.0), (2, 0.9), (3, 1.1))
    ]
    geometry = write_frame(root, VP, "20261120T171659Z", img)
    # Three landmarks on the cone: the peak and two flank points, marked where they project.
    marks = []
    for name, (la, lo) in {
        "peak": (46.85, -121.75),
        "west": (46.84, -121.77),
        "east": (46.845, -121.735),
    }.items():
        h, la2, lo2 = dem.max_near(la, lo, 40.0)
        uv, _ = project(landmark_directions(cam, [Landmark(name, la2, lo2, h, (0, 0))]), TRUE)
        marks.append(
            {
                "name": name,
                "lat": la,
                "lon": lo,
                "height_from_dem_radius_m": 40.0,
                "px": [float(uv[0, 0]), float(uv[0, 1])],
                "sigma_px": 1.0,
                "band": "summit",
            }
        )
    night = write_frame(root, VP, "20261120T120405Z", np.zeros((H, W, 3)))
    t = datetime(2026, 11, 20, 12, 4, 5, tzinfo=UTC)
    stars = []
    for ra in np.linspace(0, 360, 400, endpoint=False):
        for dec in (40.0, 55.0, 70.0):
            s = StarObservation(float(ra), dec, 3.0, t, (0, 0))
            uv, z = project(star_directions([s], cam[0], cam[1]), TRUE)
            if z[0] > 0 and 5 < uv[0, 0] < W - 5 and 5 < uv[0, 1] < H * 0.4:
                stars.append(
                    {
                        "frame_key": night,
                        "ra_deg": float(ra),
                        "dec_deg": dec,
                        "v_mag": 3.0,
                        "px": [float(uv[0, 0]), float(uv[0, 1])],
                    }
                )
    assert len(stars) >= 5
    night_sha = hashlib.sha256((root / night).read_bytes()).hexdigest()
    match = {
        "kind": "snow-globe.plate-star-match",
        "viewpoint_id": VP,
        "catalogue": {"name": "synthetic"},
        "settings": {},
        "summary": {"kept": len(stars[:20])},
        "frames": [{"key": night, "sha256": night_sha, "image_time_utc": t.isoformat()}],
        "kept": [
            {
                **star,
                "catalogue_id": i,
                "image_time_utc": t.isoformat().replace("+00:00", "Z"),
                "peak_sigma": 50.0,
                "residual_px": 0.0,
            }
            for i, star in enumerate(stars[:20])
        ],
        "rejected": [],
        "unmatched": [],
    }
    match_rel = f"plates/{VP}/sources/stars.json"
    (root / match_rel).write_text(json.dumps(match))
    match_sha = hashlib.sha256((root / match_rel).read_bytes()).hexdigest()
    spec = {
        "kind": "snow-globe.plate-spec",
        "spec_version": 2,
        "viewpoint_id": VP,
        "plate_version": "v0",
        "frame_size": [W, H],
        "time_zone": "America/Los_Angeles",
        "camera": {
            "start": {"lat": START[0], "lon": START[1]},
            "agl_m": AGL,
            "agl_sensitivity_m": [5.0, 20.0],
        },
        "dem": {
            "path": dem_rel,
            "sha256": dem.sha256,
            "source": "synthetic",
            "licence": "test",
            "datum": "test",
        },
        "landmarks": marks,
        "stars": {"frames": [night], "file": match_rel, "sha256": match_sha},
        "initial_pose": {"heading_deg": 1.5, "pitch_deg": 11.0, "roll_deg": 0.0, "hfov_deg": 62.0},
        "base": {
            "frames": keys,
            "method": "median",
            "candidates": [{"key": keys[0], "verdict": "chosen", "reason": "test"}],
        },
        "geometry_frame": geometry,
        "overlay_box": [0, H - 6, W, H],
        "bands": [
            {"name": "summit", "min_height_m": 3500.0},
            {"name": "upper", "min_height_m": 2500.0, "max_height_m": 3500.0},
        ],
        "occluders": [{"name": "corner", "polygon": [[0, H], [40, H], [40, H - 40], [0, H - 40]]}],
        "band_registration_bar": {"p90_abs_deg": 1.0, "basis": "test"},
    }
    path = root / "spec.json"
    path.write_text(json.dumps(spec))
    return path


def test_build_recovers_the_pose_and_writes_a_hashed_plate_set(tmp_path: Path) -> None:
    spec = _setup(tmp_path)
    manifest = build(spec, data_root=tmp_path)
    cam = manifest["renderer_inputs"]["camera"]
    assert cam["heading_deg"] == pytest.approx(TRUE.heading_deg, abs=0.05)
    assert cam["hfov_deg"] == pytest.approx(TRUE.hfov_deg, abs=0.05)
    off = manifest["pose"]["camera"]["offset_from_start_m"]
    assert off["east"] == pytest.approx(OFFSET[0], abs=5.0) and off["north"] == pytest.approx(
        OFFSET[1], abs=5.0
    )
    assert manifest["pose_check"]["passes"] is True
    out = tmp_path / "plates" / VP / "v0"
    for name, info in manifest["files"].items():
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == info["sha256"]
    assert json.loads((out / "plate.json").read_text())["plate_id"] == f"{VP}/plate-v0"
    assert {b["name"]: b["pixels"] for b in manifest["bands"]}["summit"] > 0
    assert manifest["base"]["dehazed"] is False and any("TODO(M2)" in t for t in manifest["todo"])
    sky_check = manifest["pose_check"]["band_skyline"]["summit"]
    assert sky_check["columns_checked"] > 30 and sky_check["median_abs_px"] < 3.0
    assert all(b["valid"] for b in manifest["bands"] if b["name"] == "summit")
    roles = {f["role"] for f in manifest["inputs"]["frames"]}
    assert {"base", "night frame (star positions)"} <= roles
    assert [r["agl_m"] for r in manifest["pose_check"]["agl_sensitivity"]] == [5.0, 20.0]
    assert manifest["inputs"]["star_match"]["summary"]["kept"] == 20
    with pytest.raises(ValueError, match="never overwritten"):
        build(spec, data_root=tmp_path)


def test_build_refuses_a_repo_folder_and_a_changed_dem(tmp_path: Path) -> None:
    spec = _setup(tmp_path)
    repo = tmp_path / "checkout"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(ValueError, match="git checkout"):
        build(spec, data_root=tmp_path, out_dir=repo / "plates")
    doc = json.loads(spec.read_text())
    doc["dem"]["sha256"] = "0" * 64
    spec.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="SHA-256"):
        build(spec, data_root=tmp_path, write=False)


def test_cli_dry_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    spec = _setup(tmp_path)
    assert cli.main(["--data-dir", str(tmp_path), "build", "--spec", str(spec), "--dry-run"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["written"] is False and summary["pose_check_passes"] is True
    assert not (tmp_path / "plates" / VP / "v0" / "plate.json").exists()
    assert (
        cli.main(["--data-dir", str(tmp_path), "build", "--spec", str(tmp_path / "missing.json")])
        == 2
    )


def test_build_refuses_a_changed_match_file_and_inline_stars(tmp_path: Path) -> None:
    spec = _setup(tmp_path)
    doc = json.loads(spec.read_text())
    (tmp_path / doc["stars"]["file"]).write_text("{}")
    with pytest.raises(ValueError, match="SHA-256"):
        build(spec, data_root=tmp_path, write=False)
    doc["stars"]["observations"] = []
    spec.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="match file"):
        build(spec, data_root=tmp_path, write=False)


def test_spec_needs_three_landmarks(tmp_path: Path) -> None:
    spec = _setup(tmp_path)
    doc = json.loads(spec.read_text())
    doc["landmarks"] = doc["landmarks"][:2]
    spec.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="three landmarks"):
        build(spec, data_root=tmp_path, write=False)
