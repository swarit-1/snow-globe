"""Opt-in contract check: the real renderer CLI accepts a SceneSpec built by snow-globe-eval.

Runs ``pnpm --dir renderer render`` on the renderer's procedural synthetic plate (no camera
pixels), writing only to a temporary folder. It needs pnpm and Playwright's Chromium, so it
runs only with ``SNOW_GLOBE_EVAL_RENDER=1``. The default suite runs the renderer's validateScene()
under Node instead (tests/test_scene.py).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import needs_oculus
from snow_globe_common.env import env_get
from snow_globe_common.manifest import load_manifest
from synth_forecast import low_deck, make_doc

from snow_globe_eval.ephemeris import moon_position, sun_position
from snow_globe_eval.forecast_io import ForecastRun
from snow_globe_eval.scene import (
    PlateIdentity,
    build_scene,
    find_renderer_dir,
    renderer_version,
    validate_scene,
)

pytestmark = pytest.mark.skipif(
    (env_get("SNOW_GLOBE_EVAL_RENDER") or "").strip() != "1" or shutil.which("pnpm") is None,
    reason="set SNOW_GLOBE_EVAL_RENDER=1 (needs pnpm and Playwright Chromium) to run the renderer",
)


@needs_oculus
def test_real_renderer_accepts_an_eval_scene(tmp_path: Path) -> None:
    renderer = find_renderer_dir()
    plate = renderer / "goldens" / "plate" / "synthetic-plate.png"
    vp = load_manifest("rainier.paradise-mountain")
    valid = datetime(2026, 11, 19, 20, tzinfo=UTC)
    run = ForecastRun(make_doc(vp, datetime(2026, 11, 18, 12, tzinfo=UTC), [32], low_deck))
    cam = vp.camera
    scene = build_scene(
        viewpoint=vp,
        valid_time=valid,
        run=run,
        sun=sun_position(valid, cam.lat, cam.lon, cam.elevation_m),
        moon=moon_position(valid, cam.lat, cam.lon, cam.elevation_m),
        width=640,
        height=360,
        renderer_version=renderer_version(renderer),
        plate=PlateIdentity.from_files(vp.id, plate),
    )
    validate_scene(scene, renderer)
    scene_path = tmp_path / "scene.json"
    scene_path.write_text(json.dumps(scene))
    out, meta = tmp_path / "render.png", tmp_path / "render.json"
    done = subprocess.run(
        [
            "pnpm",
            "--dir",
            str(renderer),
            "render",
            "--scene",
            str(scene_path),
            "--plate",
            str(plate),
            "--out",
            str(out),
            "--meta",
            str(meta),
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    record = json.loads(meta.read_text())
    assert record["renderer_version"] == scene["renderer_version"]
    assert record["inputs"]["plate"]["sha256"] == scene["plate"]["sha256"]
    assert out.stat().st_size > 0
    timing = record["timing_ms"]
    print(f"render {timing['render']:.0f} ms, total {timing['total']:.0f} ms")
