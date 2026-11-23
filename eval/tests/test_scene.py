"""SceneSpec construction (PRD §9.2, §9.5) against the renderer's own schema and validator."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import REPO_RENDERER, FixedEphemeris, NoaaSunFixedMoon, needs_node
from snow_globe_common.manifest import load_manifest
from synth_forecast import clear_sky, low_deck, make_doc

from snow_globe_eval.ephemeris import moon_position, sun_position
from snow_globe_eval.forecast_io import ForecastRun
from snow_globe_eval.scene import (
    LOW_TOP_M,
    PlateIdentity,
    RendererValidatorUnavailableError,
    SceneAssumptions,
    SceneSchemaMissingError,
    build_scene,
    derive_seed,
    find_renderer_dir,
    renderer_validate,
    renderer_version,
    validate_scene,
)

RUN = datetime(2026, 11, 18, 12, tzinfo=UTC)
VALID = RUN + timedelta(hours=24)
PLATE = PlateIdentity(id="rainier.paradise-mountain/plate-test", sha256="ab" * 32)


def test_seed_matches_the_renderers_derive_seed() -> None:
    # Golden values printed by renderer/src/scene.ts deriveSeed() under Node 24, 2026-11-19.
    assert derive_seed("rainier.paradise-mountain", "2026-11-18T12:00:00Z") == "bb92ad3f8256416c"
    assert derive_seed("point-reyes.beach", "2026-11-17T18:00:00Z") == "6559fcc591a30de0"


@pytest.fixture
def renderer_dir(tmp_path: Path) -> Path:
    if not (REPO_RENDERER / "scene.schema.json").is_file():
        pytest.skip("renderer/scene.schema.json not present")
    folder = tmp_path / "renderer"
    (folder / "src").mkdir(parents=True)
    shutil.copy(REPO_RENDERER / "scene.schema.json", folder / "scene.schema.json")
    shutil.copy(REPO_RENDERER / "src" / "version.ts", folder / "src" / "version.ts")
    (folder / "package.json").write_text("{}")
    return folder


def _scene(
    weather,
    *,
    vp_id="rainier.paradise-mountain",
    assumptions=None,
    plate=PLATE,
    ephemeris=None,
    valid=VALID,
    hours=(24,),
):
    vp = load_manifest(vp_id)
    eph = ephemeris or FixedEphemeris(20.0, 200.0)
    cam = vp.camera
    run = ForecastRun(make_doc(vp, RUN, list(hours), weather))
    return vp, build_scene(
        viewpoint=vp,
        valid_time=valid,
        run=run,
        sun=sun_position(valid, cam.lat, cam.lon, cam.elevation_m, ephemeris=eph),
        moon=moon_position(valid, cam.lat, cam.lon, cam.elevation_m, ephemeris=eph),
        width=1920,
        height=1080,
        renderer_version=renderer_version(REPO_RENDERER),
        plate=plate,
        assumptions=assumptions,
    )


def test_clear_scene_validates_and_has_expected_fields(renderer_dir) -> None:
    vp, scene = _scene(clear_sky)
    validate_scene(scene, renderer_dir)
    assert scene["run_init_utc"] == "2026-11-18T12:00:00Z"
    assert scene["seed"] == derive_seed(vp.id, "2026-11-18T12:00:00Z")
    assert scene["valid_time_utc"] == "2026-11-19T12:00:00Z"
    forecast = scene["forecast"]
    assert forecast["ceiling_m_msl"] is None and forecast["visibility_m"] == 50_000.0
    assert [layer["name"] for layer in forecast["layers"]] == ["low", "mid", "high"]
    for layer in forecast["layers"]:
        distances = [k["distance_km"] for k in layer["cover_profile"]]
        assert distances == sorted(set(distances)) and len(distances) <= 16
        assert distances[0] == 0.0 and distances[-1] == pytest.approx(45.0)
        assert layer["thickness_m"] >= 100.0
    assert scene["camera"]["heading_deg"] == vp.camera.view_azimuth_deg
    assert scene["camera"]["elev_m_msl"] == vp.camera.elevation_m + 10.0
    assert scene["plate"] == {
        "id": PLATE.id,
        "sha256": PLATE.sha256,
        "has_sky_mask": False,
        "has_depth": False,
        "nominal_depth_m": pytest.approx(7440.0),
    }
    assert scene["meta"]["pose_source"].startswith("nominal")
    assert scene["sun"] == {"alt_deg": 20.0, "az_deg": 200.0}


def test_plate_hashes_follow_the_plate_set(renderer_dir, tmp_path: Path) -> None:
    files = {}
    for name in ("plate.jpg", "sky.png", "depth.png"):
        files[name] = tmp_path / name
        files[name].write_bytes(name.encode())
    plate = PlateIdentity.from_files(
        "rainier.paradise-mountain",
        files["plate.jpg"],
        sky_mask=files["sky.png"],
        depth=files["depth.png"],
    )
    assert plate.id == f"rainier.paradise-mountain/plate-{plate.sha256[:12]}"
    _, scene = _scene(low_deck, plate=plate)
    validate_scene(scene, renderer_dir)
    assert scene["plate"]["has_sky_mask"] and scene["plate"]["has_depth"]
    assert scene["plate"]["sky_mask_sha256"] == plate.sky_mask_sha256
    assert scene["plate"]["depth_sha256"] == plate.depth_sha256
    assert "nominal_depth_m" not in scene["plate"]
    with pytest.raises(ValueError, match="asset id"):
        PlateIdentity(id="bad id with spaces", sha256="ab" * 32)
    with pytest.raises(ValueError, match="64 lower-case hex"):
        PlateIdentity(id="x/plate", sha256="AB" * 32)


def test_low_deck_scene(renderer_dir) -> None:
    _, scene = _scene(low_deck)
    validate_scene(scene, renderer_dir)
    forecast = scene["forecast"]
    assert forecast["ceiling_m_msl"] == 2400.0
    low = forecast["layers"][0]
    assert low["base_m_msl"] == 2400.0 and low["thickness_m"] == pytest.approx(600.0)
    assert low["cover_profile"][0]["cover_0_1"] == pytest.approx(0.95)
    assert forecast["precip_rate_mm_h"] == pytest.approx(0.72)
    assert forecast["wind_v_ms"] != 0.0


def test_scene_without_landmarks_and_with_overrides(renderer_dir) -> None:
    _, scene = _scene(
        clear_sky,
        vp_id="point-reyes.beach",
        assumptions=SceneAssumptions(hfov_deg=45.0, pitch_deg=-2.0),
        plate=PlateIdentity(id="point-reyes.beach/plate-test", sha256="cd" * 32),
    )
    validate_scene(scene, renderer_dir)
    assert "nominal_depth_m" not in scene["plate"]
    assert scene["camera"]["hfov_deg"] == 45.0 and scene["camera"]["pitch_deg"] == -2.0
    mid = scene["forecast"]["layers"][1]
    assert mid["base_m_msl"] == LOW_TOP_M


def test_missing_schema_is_reported(tmp_path: Path) -> None:
    _, scene = _scene(clear_sky)
    with pytest.raises(SceneSchemaMissingError):
        validate_scene(scene, tmp_path)


def test_invalid_scene_is_refused(renderer_dir) -> None:
    _, scene = _scene(clear_sky)
    scene["camera"]["hfov_deg"] = 0.0
    with pytest.raises(ValueError, match=r"scene\.schema\.json"):
        validate_scene(scene, renderer_dir)


def test_cross_field_rules_are_checked(renderer_dir) -> None:
    _, scene = _scene(clear_sky)
    scene["seed"] = "0" * 16
    with pytest.raises(ValueError, match="deriveSeed"):
        validate_scene(scene, renderer_dir)
    _, scene = _scene(clear_sky)
    scene["run_init_utc"] = "2026-11-20T12:00:00Z"  # after the valid time
    with pytest.raises(ValueError, match="0 to 384 h"):
        validate_scene(scene, renderer_dir)


def test_lead_beyond_the_renderers_range_is_refused() -> None:
    with pytest.raises(ValueError, match="0 to 384 h"):
        _scene(clear_sky, valid=RUN + timedelta(hours=385), hours=(385,))


def test_renderer_discovery(renderer_dir, monkeypatch: pytest.MonkeyPatch) -> None:
    assert renderer_version(renderer_dir) == renderer_version(REPO_RENDERER)
    monkeypatch.setenv("SNOW_GLOBE_RENDERER_DIR", str(renderer_dir))
    assert find_renderer_dir() == renderer_dir.resolve()
    monkeypatch.delenv("SNOW_GLOBE_RENDERER_DIR")
    assert find_renderer_dir() == REPO_RENDERER.resolve()


def _cirrus(field, fh, point):
    if field == "HGT:cloud base":
        return 10_000.0
    if field == "HGT:cloud top":
        return 11_000.0
    if field == "HCDC:high cloud layer":
        return 70.0
    return clear_sky(field, fh, point)


def test_high_cloud_base_above_the_nominal_band_is_kept(renderer_dir) -> None:
    _, scene = _scene(_cirrus)
    validate_scene(scene, renderer_dir)
    _low, mid, high = scene["forecast"]["layers"]
    assert high["base_m_msl"] == 10_000.0 and high["thickness_m"] == 1500.0
    assert mid["base_m_msl"] == LOW_TOP_M  # the cirrus base does not move the mid layer
    assert high["cover_profile"][0]["cover_0_1"] == pytest.approx(0.7)


@needs_node
def test_the_renderers_validator_accepts_eval_scenes(tmp_path: Path) -> None:
    """Contract: renderer/src/scene.ts validateScene() accepts what build_scene() writes.

    Runs the renderer's own code under Node (no browser), with real Sun positions, for both
    MSV viewpoints, several weathers, and plates with and without mask and depth. It fails
    when scene.ts changes in a way the schema does not show yet (the review's finding).
    """
    mask_plate = PlateIdentity(
        id="rainier.paradise-mountain/plate-v1",
        sha256="ab" * 32,
        sky_mask_sha256="cd" * 32,
        depth_sha256="ef" * 32,
    )
    eph = NoaaSunFixedMoon()
    afternoon = RUN + timedelta(hours=32)  # 20:00Z, the Sun is up at both cameras
    scenes = [
        _scene(clear_sky, ephemeris=eph, valid=afternoon, hours=(32,))[1],
        _scene(low_deck, ephemeris=eph, valid=afternoon, hours=(32,), plate=mask_plate)[1],
        _scene(_cirrus, ephemeris=eph, valid=VALID)[1],
        _scene(
            low_deck,
            vp_id="point-reyes.beach",
            ephemeris=eph,
            valid=afternoon,
            hours=(32,),
            plate=PlateIdentity(id="point-reyes.beach/plate-v1", sha256="12" * 32),
        )[1],
    ]
    assert renderer_validate(scenes, REPO_RENDERER) == [[], [], [], []]
    # the harness is live: the renderer rejects a broken scene and a Sun from a test double
    broken = dict(scenes[0], plate={**scenes[0]["plate"], "sha256": "nope"})
    wrong_sun = _scene(clear_sky, valid=afternoon, hours=(32,))[1]  # FixedEphemeris Sun
    issues = renderer_validate([broken, wrong_sun], REPO_RENDERER)
    assert any("plate.sha256" in issue for issue in issues[0])
    assert any("scene.sun" in issue for issue in issues[1])
    assert renderer_validate([wrong_sun], REPO_RENDERER, allow_synthetic_sun=True) == [[]]


def test_renderer_validator_reports_a_missing_renderer(tmp_path: Path) -> None:
    _, scene = _scene(clear_sky)
    with pytest.raises(RendererValidatorUnavailableError):
        renderer_validate([scene], tmp_path, node=str(tmp_path / "no-such-node"))
