"""The dev P1 table's batch renders (PRD §13.2, §9.5) through ``renderer/scripts/render-batch.ts``.

The default tests replace the Node process with a fake. The real batch render of an eval-built
scene on the renderer's procedural synthetic plate (no camera pixels, a temporary folder) runs
only with ``SNOW_GLOBE_EVAL_RENDER=1``, as the other real-render contract test does.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import needs_oculus
from snow_globe_common.env import env_get
from snow_globe_common.manifest import load_manifest
from synth_forecast import low_deck
from synth_p1dev import live_doc

from snow_globe_eval.ephemeris import moon_position, sun_position
from snow_globe_eval.forecast_io import ForecastRun
from snow_globe_eval.p1dev_forecast import scene_run
from snow_globe_eval.p1dev_render import (
    RenderJob,
    batch_command,
    identity_digest,
    render_batch,
    renderer_identity,
)
from snow_globe_eval.scene import (
    PlateIdentity,
    RendererValidatorUnavailableError,
    build_scene,
    find_renderer_dir,
    renderer_version,
)


def _renderer(tmp_path: Path) -> Path:
    folder = tmp_path / "renderer"
    (folder / "scripts").mkdir(parents=True)
    (folder / "scripts" / "render-batch.ts").write_text("// fake\n")
    return folder


def _job(tmp_path: Path, name: str) -> RenderJob:
    return RenderJob(
        id=name,
        scene=tmp_path / f"{name}.json",
        plate=tmp_path / "plate.png",
        sky_mask=None,
        depth=None,
        out=tmp_path / name / "render.png",
        meta=tmp_path / name / "render.json",
    )


class Fake:
    def __init__(self, lines: list[dict[str, Any]], code: int = 0, stderr: str = "") -> None:
        self.lines, self.code, self.stderr = lines, code, stderr
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command[1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "v24.13.0\n", "")
        text = "noise from pnpm\n" + "".join(json.dumps(x) + "\n" for x in self.lines)
        return subprocess.CompletedProcess(command, self.code, text, self.stderr)


def test_results_come_back_by_job_and_a_missing_line_is_a_failure(tmp_path: Path) -> None:
    renderer = _renderer(tmp_path)
    jobs = [_job(tmp_path, "a"), _job(tmp_path, "b")]
    fake = Fake(
        [
            {"kind": "render-batch-start", "renderer_version": "0.3.0", "digest": "e" * 64},
            {"kind": "render-batch-result", "id": "a", "ok": True, "sha256": "f" * 64},
        ],
        code=2,
        stderr="Chromium crashed",
    )
    out = render_batch(
        jobs, renderer_dir=renderer, log_path=tmp_path / "logs" / "b1.log", node="n", runner=fake
    )
    assert out["results"]["a"]["ok"] is True
    assert out["results"]["b"]["ok"] is False and "Chromium crashed" in out["results"]["b"]["error"]
    assert out["start"]["digest"] == "e" * 64 and out["returncode"] == 2
    lines = (tmp_path / "logs" / "b1.jobs.jsonl").read_text().splitlines()
    assert [json.loads(x)["id"] for x in lines] == ["a", "b"]
    assert json.loads(lines[0])["out"] == str((tmp_path / "a" / "render.png").resolve())
    command = fake.commands[-1]
    assert command[1].endswith("scripts/render-batch.ts") and command[-2:] == [
        "--backend",
        "swiftshader",
    ]
    assert "Chromium crashed" in (tmp_path / "logs" / "b1.log").read_text()
    assert render_batch([], renderer_dir=renderer, log_path=tmp_path / "x.log")["results"] == {}


def test_the_identity_comes_from_the_scripts_start_line(tmp_path: Path) -> None:
    renderer = _renderer(tmp_path)
    fake = Fake(
        [
            {
                "kind": "render-batch-start",
                "renderer_version": "0.3.0",
                "digest": "1" * 64,
                "jobs": 0,
                "backend": "swiftshader",
            }
        ]
    )
    identity = renderer_identity(renderer, node="n", runner=fake)
    assert identity == {"renderer_version": "0.3.0", "digest": "1" * 64}
    assert identity_digest(identity) == "1" * 64 and identity_digest(None) is None
    with pytest.raises(RuntimeError):
        renderer_identity(renderer, node="n", runner=Fake([], code=2))


def test_node_and_the_script_are_checked(tmp_path: Path) -> None:
    renderer = _renderer(tmp_path)

    def old_node(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "v20.1.0\n", "")

    with pytest.raises(RendererValidatorUnavailableError, match="need 24"):
        batch_command(renderer, node="n", runner=old_node)
    (renderer / "scripts" / "render-batch.ts").unlink()
    with pytest.raises(RendererValidatorUnavailableError, match="not found"):
        batch_command(renderer, node="n", runner=Fake([]))


@needs_oculus
@pytest.mark.skipif(
    (env_get("SNOW_GLOBE_EVAL_RENDER") or "").strip() != "1" or shutil.which("node") is None,
    reason="set SNOW_GLOBE_EVAL_RENDER=1 (needs Node 24 and Playwright Chromium) to render",
)
def test_the_real_batch_script_renders_an_eval_scene(tmp_path: Path) -> None:
    renderer = find_renderer_dir()
    plate = renderer / "goldens" / "plate" / "synthetic-plate.png"
    vp = load_manifest("rainier.paradise-mountain")
    valid = datetime(2026, 11, 20, 20, 21, tzinfo=UTC)
    run, _ = scene_run(
        ForecastRun(live_doc(vp, datetime(2026, 11, 19, 12, tzinfo=UTC), [32, 33], low_deck))
    )
    cam = vp.camera
    scene = build_scene(
        viewpoint=vp,
        valid_time=valid,
        run=run,
        sun=sun_position(valid, cam.lat, cam.lon, cam.elevation_m),
        moon=moon_position(valid, cam.lat, cam.lon, cam.elevation_m),
        width=320,
        height=180,
        renderer_version=renderer_version(renderer),
        plate=PlateIdentity.from_files(vp.id, plate),
    )
    jobs = []
    for name in ("one", "two"):
        (tmp_path / f"{name}.json").write_text(json.dumps(scene))
        jobs.append(
            RenderJob(
                name,
                tmp_path / f"{name}.json",
                plate,
                None,
                None,
                tmp_path / name / "render.png",
                tmp_path / name / "render.json",
            )
        )
    out = render_batch(jobs, renderer_dir=renderer, log_path=tmp_path / "batch.log")
    assert out["returncode"] == 0, (tmp_path / "batch.log").read_text()[-2000:]
    assert all(out["results"][n]["ok"] for n in ("one", "two"))
    # the same scene twice: byte-identical PNGs (PRD §9.5), from one runner
    assert out["results"]["one"]["sha256"] == out["results"]["two"]["sha256"]
    assert out["start"]["renderer_version"] == renderer_version(renderer)
    assert out["summary"]["ok"] == 2 and out["summary"]["launch_ms"] > 0
