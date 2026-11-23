"""The dev P1 table end to end on a synthetic data dir (PLAN M1.5; PRD §13.4–§13.7, §13.9).

Procedural frames, plate and live-profile hindcast runs under a temporary data dir. The
renderer's batch process is replaced by a fake that writes procedural PNGs (a recorded-fixture
unit test, never a measured run): it draws the plate's base, veiled over the summit when the
scene's low layer covers more than half the sky. Nothing touches the network or the real data.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from conftest import REPO_RENDERER
from PIL import Image
from snow_globe_common.manifest import load_manifest
from synth_forecast import clear_sky, low_deck
from synth_images import fogged, landscape, to_rgb
from synth_p1dev import live_doc, write_run
from test_extractor_v0 import peak_and_ground, write_plate
from test_extractor_v0_run import _DayNight, _frame, _label, _write_labels

from snow_globe_eval import p1dev
from snow_globe_eval.heldout import LeakageError
from snow_globe_eval.p1dev import P1DevOptions, model_forecast, plate_viewpoint, run_p1dev
from snow_globe_eval.p1dev_extract import Reading
from snow_globe_eval.p1dev_forecast import hindcast_dir

VP = "rainier.paradise-mountain"
SCENE = landscape(1)


def _veil(luma: np.ndarray) -> np.ndarray:
    return np.where(np.arange(luma.shape[0])[:, None] < 210, fogged(luma, 0.9), luma)


class FakeRenderer:
    """Stands in for ``node scripts/render-batch.ts``: identity, then one PNG per job.

    ``digest`` is the rendering code's identity it reports, at ``--identity`` and at the start
    of every batch; a test changes it to play a renderer edited during a run.
    """

    def __init__(self, version: str) -> None:
        self.version = version
        self.rendered: list[str] = []
        self.batches = 0
        self.digest = "d" * 64

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "v24.13.0\n", "")
        start = {"kind": "render-batch-start", "renderer_version": self.version}
        start["digest"] = self.digest
        if "--identity" in command:
            return subprocess.CompletedProcess(command, 0, json.dumps(start) + "\n", "")
        jobs = Path(command[command.index("--jobs") + 1]).read_text().splitlines()
        self.batches += 1
        out = [json.dumps(start)]
        for line in jobs:
            job = json.loads(line)
            scene = json.loads(Path(job["scene"]).read_text())
            low = next(x for x in scene["forecast"]["layers"] if x["name"] == "low")
            cover = max(k["cover_0_1"] for k in low["cover_profile"])
            luma = _veil(SCENE) if cover > 0.5 else SCENE
            png = Path(job["out"])
            png.parent.mkdir(parents=True, exist_ok=True)
            to_rgb(luma).save(png, format="PNG")
            sha = hashlib.sha256(png.read_bytes()).hexdigest()
            Path(job["meta"]).write_text(json.dumps({"scene_hash": "h", "output": {"sha256": sha}}))
            self.rendered.append(job["id"])
            out.append(
                json.dumps(
                    {
                        "kind": "render-batch-result",
                        "id": job["id"],
                        "ok": True,
                        "out": job["out"],
                        "meta": job["meta"],
                        "sha256": sha,
                        "scene_hash": "h",
                        "render_ms": 1.0,
                        "total_ms": 2.0,
                        "error": None,
                    }
                )
            )
        out.append(json.dumps({"kind": "render-batch-summary", "jobs": len(jobs), "failed": 0}))
        return subprocess.CompletedProcess(command, 0, "\n".join(out) + "\n", "")


def _plate(root: Path) -> Path:
    vp = load_manifest(VP)
    folder = write_plate(
        root / "plates" / VP / "v0.1", peak_and_ground(), viewpoint=VP, base=to_rgb(SCENE)
    )
    sky = np.zeros(SCENE.shape, bool)
    sky[:40] = True
    Image.fromarray((sky * 255).astype(np.uint8)).save(folder / "sky.png")
    doc = json.loads((folder / "plate.json").read_text())
    doc["plate_id"] = f"{VP}/plate-v0.1"
    doc["plate_version"] = "v0.1"
    doc["files"]["sky.png"] = {
        "sha256": hashlib.sha256((folder / "sky.png").read_bytes()).hexdigest()
    }
    doc["renderer_inputs"] = {
        "plate": "base.png",
        "sky_mask": "sky.png",
        "depth": None,
        "camera": {
            "lat": vp.camera.lat + 0.001,
            "lon": vp.camera.lon,
            "elev_m_msl": 1697.95,
            "heading_deg": 2.137,
            "pitch_deg": 9.9936,
            "roll_deg": 1.6426,
            "hfov_deg": 68.5637,
            "agl_m": 10.0,
        },
    }
    (folder / "plate.json").write_text(json.dumps(doc))
    return folder


def _renderer(tmp_path: Path) -> Path:
    if not (REPO_RENDERER / "scene.schema.json").is_file():
        pytest.skip("renderer/scene.schema.json not present")
    folder = tmp_path / "renderer"
    (folder / "src").mkdir(parents=True)
    (folder / "scripts").mkdir()
    shutil.copy(REPO_RENDERER / "scene.schema.json", folder / "scene.schema.json")
    shutil.copy(REPO_RENDERER / "src" / "version.ts", folder / "src" / "version.ts")
    (folder / "scripts" / "render-batch.ts").write_text("// stand-in; the test runs a fake\n")
    (folder / "package.json").write_text("{}")
    return folder


def _version(renderer: Path) -> str:
    from snow_globe_eval.scene import renderer_version

    return renderer_version(renderer)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    root = tmp_path / "data"
    monkeypatch.setenv("SNOW_GLOBE_DATA_DIR", str(root))
    monkeypatch.setenv("SNOW_GLOBE_NODE", "fake-node")
    _plate(root)
    labels = []
    keys: dict[str, str] = {}
    for k, stamp in enumerate(("20261120T180000Z", "20261120T181500Z", "20261120T183000Z")):
        key, sha = _frame(root, stamp, to_rgb(np.clip(SCENE + 0.01 * k, 0, 1)))
        labels.append(_label(key, sha, stamp, "visible", True))
        keys[stamp] = key
    for stamp, luma in (
        ("20261121T180000Z", np.clip(SCENE + 0.005, 0, 1)),
        ("20261121T181500Z", _veil(SCENE)),
        ("20261122T180000Z", np.clip(SCENE + 0.015, 0, 1)),
        ("20261122T181500Z", _veil(np.clip(SCENE + 0.002, 0, 1))),
        ("20261121T060000Z", landscape(11)),  # night
        ("20261220T180000Z", np.clip(SCENE + 0.02, 0, 1)),  # design P's sealed window
    ):
        keys[stamp], _ = _frame(root, stamp, to_rgb(luma))
    vp = load_manifest(VP)
    folder = hindcast_dir(root, VP)
    for run, weather in (
        (datetime(2026, 11, 19, 12, tzinfo=UTC), clear_sky),
        (datetime(2026, 11, 20, 12, tzinfo=UTC), low_deck),
    ):
        write_run(folder, live_doc(vp, run, [29, 30, 31, 32], weather))
    renderer = _renderer(tmp_path)
    return {
        "root": root,
        "keys": keys,
        "labels": _write_labels(tmp_path / "labels.json", labels),
        "renderer": renderer,
        "fake": FakeRenderer(_version(renderer)),
        "vp": vp,
    }


def _options(world: dict[str, Any], **kw: Any) -> P1DevOptions:
    return P1DevOptions(
        run_id=kw.pop("run_id", "t1"),
        band="peak",
        labels_path=kw.pop("labels_path", world["labels"]),
        renderer_dir=world["renderer"],
        n_boot=400,
        batch_size=kw.pop("batch_size", 3),
        **kw,
    )


def _run(world: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return run_p1dev(
        _options(world, **kw),
        command=["snow-globe-eval", "p1dev", "--run-id", "t1"],
        ephemeris=_DayNight(),
        runner=world["fake"],
        root=world["root"],
    )


def _records(root: Path, run_id: str = "t1") -> dict[str, dict[str, Any]]:
    folder = root / "scores" / "p1dev" / run_id / "frames"
    return {
        json.loads(p.read_text())["frame"]["key"]: json.loads(p.read_text())
        for p in folder.glob("*.json")
    }


def test_the_table_runs_end_to_end_and_resumes(world: dict[str, Any]) -> None:
    root, keys, fake = world["root"], world["keys"], world["fake"]
    summary = _run(world)
    assert summary["daytime_frames"] == 7 and summary["provisional"] is True
    records = _records(root)
    assert set(records) == {keys[s] for s in keys if s[9:11] == "18" and not s.startswith("202612")}
    assert keys["20261220T180000Z"] not in records and keys["20261121T060000Z"] not in records
    by_day = {k: r["status"] for k, r in records.items()}
    assert {by_day[keys[s]] for s in ("20261122T180000Z", "20261122T181500Z")} == {"no-forecast"}
    assert all(
        by_day[keys[s]] == "scored" for s in keys if s.startswith(("20261120T18", "20261121T18"))
    )
    assert sorted(fake.rendered) == sorted(
        Path(keys[s]).stem for s in keys if s.startswith(("20261120T18", "20261121T18"))
    )

    deck = records[keys["20261121T181500Z"]]
    assert deck["forecast"]["run"] == "2026112012" and deck["forecast"]["fh_lo"] == 30
    issue = datetime.fromisoformat(deck["issue_time_utc"].replace("Z", "+00:00"))
    available = datetime.fromisoformat(deck["forecast"]["available_utc"].replace("Z", "+00:00"))
    assert available <= issue and deck["forecast"]["lead_from_availability_h"] >= 24
    assert deck["valid_time_utc"] == "2026-11-21T18:15:00Z" and deck["lead_nominal_h"] == 24.0
    assert deck["truth"]["p1_class"] == "hidden" and deck["prediction"]["p1_class"] == "hidden"
    assert deck["same_references"] is True and deck["truth"]["references"]
    assert deck["p1"]["forecasts"]["model"] is False
    assert deck["baselines"]["s_rule"]["value"] is False  # a 2,400 m deck under the summit
    # day 1 has no clear-day reference on another day, so nothing usable precedes day 2
    assert deck["baselines"]["persistence_b"]["value"] is None
    assert "no-usable-frame-near-target" in deck["baselines"]["persistence_b"]["flags"]
    assert deck["frame"]["dev_week"] == {"iso_year": 2026, "iso_week": 47, "role": "dev-fit"}
    assert deck["heldout_guard"]["development"] is True
    scene = json.loads(Path(deck["scene"]["path"]).read_text())
    camera = json.loads((root / "plates" / VP / "v0.1" / "plate.json").read_text())[
        "renderer_inputs"
    ]["camera"]
    for name in ("lat", "lon", "elev_m_msl", "heading_deg", "pitch_deg", "roll_deg", "hfov_deg"):
        assert scene["camera"][name] == pytest.approx(camera[name])
    assert "precipitation rate unavailable; 0 used" in scene["meta"]["flags"]
    assert scene["meta"]["forecast_fields_absent_read_as_missing"]

    first_day = records[keys["20261120T180000Z"]]
    assert first_day["truth"]["p1_class"] == "no_reference"  # its pool is all from its own day
    assert first_day["p1"]["forecasts"]["persistence_a"] is None  # nothing before 19 Nov

    # the availability rule, the other rule's choice, provenance and the fitted-asset check
    assert deck["availability_rule"] == "d054" and deck["forecast"]["availability_rule"] == "d054"
    assert deck["forecast_other_rule"]["availability_rule"] == "s3-stamps"
    assert deck["forecast_other_rule"]["same_run"] is True  # the fixture's model = its stamps
    assert set(deck["provenance"]) >= {"record_code_digest", "manifest_sha256", "extractor_digest"}
    assert deck["fitted_assets"]["references"]["n"] == 3
    # its references are 20 Nov 18:00, 18:15 and 18:30: two not before the 18:15 issue
    assert deck["fitted_assets"]["references"]["passes"] is False
    assert deck["fitted_assets"]["references"]["at_or_after_issue"] == 2
    assert deck["fitted_assets"]["passes"] is False
    assert deck["p1"]["excluded_because"] is None and deck["schema_version"] == 2
    assert "label" not in deck["baselines"]["persistence_a"]  # no persistence forecast here

    out = root / "scores" / "p1dev" / "t1"
    table = json.loads((out / "table.json").read_text())
    assert table["provisional"] is True and table["refused_by_the_guard"] == 1
    assert table["unreadable_frames"] == 0
    assert table["availability"]["rule"] == "d054"
    assert table["forecast_choice_under_other_rule"]["frames_whose_run_differs"] == 0
    assert table["frames"]["all"]["with_forecast_run"] == 5
    assert table["persistence_choice"]["chosen_on"].startswith("dev-fit")
    assert table["frames"]["all"]["no_forecast_by_reason_and_day"]["persistence_a"]
    assert "sensitivity_fitted_assets_before_issue" in table
    assert not any("v-1's placeholders, not fitted" in r for r in table["provisional_because"])
    assert any("v-1's placeholders, kept fixed" in r for r in table["provisional_because"])
    text = (out / "table.md").read_text()
    assert text.startswith("# Dev-only P1 table, run t1: PROVISIONAL")
    assert "First local day 2026-11-20" in text and "no-usable-frame-before-issue" in text
    assert "is below its PRD §13.8 bar" in text
    run = json.loads((out / "run.json").read_text())
    assert run["invocations"][0]["command"][:2] == ["snow-globe-eval", "p1dev"]
    assert "snow-globe-eval p1dev --run-id t1" in (out / "command.txt").read_text()
    assert run["invocations"][0]["inputs"]["plate"]["camera"]["heading_deg"] == 2.137
    assert run["invocations"][0]["inputs"]["hindcast"]["n_files"] == 2
    assert (
        run["invocations"][0]["code"]["record_code_digest"]
        == deck["provenance"]["record_code_digest"]
    )
    assert run["options"]["availability"] == "d054"
    first_code = run["code"]

    # resume: the day-3 run lands; only day 3 is rendered, every other record is kept
    write_run(
        hindcast_dir(root, VP),
        live_doc(world["vp"], datetime(2026, 11, 21, 12, tzinfo=UTC), [29, 30, 31, 32], clear_sky),
    )
    before = {k: r["created_utc"] for k, r in records.items()}
    fake.rendered.clear()
    _run(world)
    again = _records(root)
    assert sorted(fake.rendered) == sorted(
        Path(keys[s]).stem for s in ("20261122T180000Z", "20261122T181500Z")
    )
    changed = {k for k, r in again.items() if r["created_utc"] != before[k]}
    assert changed <= {keys["20261122T180000Z"], keys["20261122T181500Z"]}
    assert again[keys["20261122T181500Z"]]["status"] == "scored"
    day3 = again[keys["20261122T181500Z"]]["baselines"]
    assert day3["persistence_b"]["detail"]["frame_time_utc"] == "2026-11-21T18:15:00Z"
    assert day3["persistence_b"]["value"] is False  # yesterday's veiled summit
    assert day3["persistence_a"]["detail"]["frame_time_utc"] == "2026-11-21T18:15:00Z"
    # the label is made with references available by the issue time, and listed
    label = day3["persistence_a"]["label"]
    assert label["frame"] == keys["20261121T181500Z"] and label["label_as_of_issue"] == "hidden"
    assert label["references"] and label["latest_reference_utc"] <= "2026-11-21T18:15:00Z"
    assert all(ref.split("/")[2] == "2026-11-20" for ref in label["references"])
    assert again[keys["20261122T181500Z"]]["frame"]["dev_week"]["role"] == "dev-fit"
    run = json.loads((out / "run.json").read_text())
    assert len(run["invocations"]) == 2 and run["code"] == first_code  # never overwritten
    assert all("code" in inv for inv in run["invocations"])

    # a resume with other result options is refused, the availability rule among them
    with pytest.raises(ValueError, match="other options"):
        _run(world, lead_hours=12.0)
    with pytest.raises(ValueError, match="other options"):
        _run(world, availability="s3-stamps")


def _relabel(world: dict[str, Any], tmp_path: Path, stamps: list[str]) -> Path:
    """A labels file whose only clear-day frames are ``stamps`` (all visible)."""
    labels = []
    for stamp in stamps:
        key = world["keys"][stamp]
        sha = hashlib.sha256((world["root"] / key).read_bytes()).hexdigest()
        labels.append(_label(key, sha, stamp, "visible", True))
    return _write_labels(tmp_path / f"labels-{len(stamps)}.json", labels)


def test_a_baseline_never_reads_a_label_made_with_the_valid_frame(
    world: dict[str, Any], tmp_path: Path
) -> None:
    """The review's case end to end: the only clear-day frame is the valid frame itself.

    The 21 Nov 18:00 frame's whole-run class is "visible", against 22 Nov 18:00 as its only
    reference; persistence for 22 Nov 18:00 (issued 21 Nov 18:00) must not see that class.
    """
    keys = world["keys"]
    labels = _relabel(world, tmp_path, ["20261122T180000Z"])
    _run(world, run_id="t5", labels_path=labels)
    records = _records(world["root"], "t5")
    earlier, valid = records[keys["20261121T180000Z"]], records[keys["20261122T180000Z"]]
    assert earlier["truth"]["p1_class"] == "visible"  # the outcome, against the valid frame
    assert earlier["truth"]["references"] == [keys["20261122T180000Z"]]
    a = valid["baselines"]["persistence_a"]
    assert a["value"] is None and a["flags"] == ["no-usable-frame-before-issue"]
    assert valid["baselines"]["persistence_b"]["value"] is None
    assert valid["baselines"]["climatology"]["value"] is None  # nothing classifiable by then
    # the next day's frames are labelled with it only once it exists: 22 Nov 18:15 at 21 Nov
    # 18:15 has no reference either
    assert records[keys["20261122T181500Z"]]["baselines"]["persistence_a"]["value"] is None


def test_a_renderer_change_during_a_run_stops_it_and_a_resume_renders_again(
    world: dict[str, Any],
) -> None:
    fake = world["fake"]
    real = fake.__call__

    def changing(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        done = real(command, **kwargs)
        if "--jobs" in command:
            fake.digest = "e" * 64  # another package edits renderer/src after the first batch
        return done

    with pytest.raises(RuntimeError, match="the renderer changed during the run"):
        run_p1dev(
            _options(world, run_id="t6", batch_size=1),
            command=["snow-globe-eval", "p1dev"],
            ephemeris=_DayNight(),
            runner=changing,
            root=world["root"],
        )
    first = _records(world["root"], "t6")
    assert len(first) == 1  # the first batch only; the second rendered with other code
    jobs = list((world["root"] / "renders" / "p1dev" / "t6").glob("*/job.json"))
    assert len(jobs) == 1 and json.loads(jobs[0].read_text())["renderer_digest"] == "d" * 64
    fake.rendered.clear()
    run_p1dev(
        _options(world, run_id="t6", batch_size=1),
        command=["snow-globe-eval", "p1dev"],
        ephemeris=_DayNight(),
        runner=world["fake"],
        root=world["root"],
    )
    again = _records(world["root"], "t6")
    scored = [r for r in again.values() if r["status"] == "scored"]
    assert len(scored) == 5 and len(fake.rendered) == 5  # every render made again, new code
    assert {r["render"]["renderer_digest"] for r in scored} == {"e" * 64}


def test_a_code_change_redoes_the_records_and_reuses_the_renders(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(world)
    before = _records(world["root"])
    fake = world["fake"]
    fake.rendered.clear()
    real = p1dev._code_identity

    def changed() -> dict[str, Any]:
        return {**real(), "record_code_digest": "f" * 64}

    monkeypatch.setattr(p1dev, "_code_identity", changed)
    _run(world)
    after = _records(world["root"])
    assert fake.rendered == []  # same scenes, same renderer: the renders are reused
    assert {r["provenance"]["record_code_digest"] for r in after.values()} == {"f" * 64}
    assert all(r["provenance"] != before[k]["provenance"] for k, r in after.items())
    assert all(r["render"]["reused"] is True for r in after.values() if r["status"] == "scored")


def test_a_missing_scene_schema_stops_the_run_and_a_scene_error_is_tried_again(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from snow_globe_eval.scene import SceneSchemaMissingError

    def missing(scene: Any, renderer_dir: Path) -> None:
        raise SceneSchemaMissingError(str(renderer_dir / "scene.schema.json"))

    monkeypatch.setattr(p1dev, "validate_scene", missing)
    with pytest.raises(FileNotFoundError):
        _run(world, run_id="t7")
    assert not any(r["status"] == "scene-failed" for r in _records(world["root"], "t7").values())

    def rejected(scene: Any, renderer_dir: Path) -> None:
        raise ValueError("SceneSpec fails renderer/scene.schema.json: test")

    monkeypatch.setattr(p1dev, "validate_scene", rejected)
    _run(world, run_id="t7")
    failed = [r for r in _records(world["root"], "t7").values() if r["status"] == "scene-failed"]
    assert len(failed) == 5 and "scene.schema.json" in failed[0]["scene"]["error"]
    monkeypatch.undo()
    _run(world, run_id="t7")  # tried again, and now scored
    assert {r["status"] for r in _records(world["root"], "t7").values()} == {
        "scored",
        "no-forecast",
    }


def test_a_truth_frame_that_is_a_plate_input_is_excluded_and_counted(
    world: dict[str, Any],
) -> None:
    keys, root = world["keys"], world["root"]
    plate_json = root / "plates" / VP / "v0.1" / "plate.json"
    doc = json.loads(plate_json.read_text())
    doc["inputs"] = {
        "frames": [
            {
                "key": keys["20261121T180000Z"],
                "role": "base",
                "image_time_utc": "2026-11-21T18:00:00Z",
            }
        ]
    }
    doc["base"] = {"frames": [{"frame": keys["20261121T180000Z"]}]}
    plate_json.write_text(json.dumps(doc))
    _run(world, run_id="t8")
    records = _records(root, "t8")
    base = records[keys["20261121T180000Z"]]
    assert base["fitted_assets"]["truth_is_plate_input"] == "base"
    assert "plate's input frames" in base["p1"]["excluded_because"]
    later = records[keys["20261122T181500Z"]]  # issued 21 Nov 18:15, after the base frame
    assert later["fitted_assets"]["plate"]["passes"] is True
    deck = records[keys["20261121T181500Z"]]  # issued 20 Nov 18:15, before it; same day
    assert deck["fitted_assets"]["plate"]["at_or_after_issue"] == 1
    assert deck["fitted_assets"]["plate"]["on_valid_local_day"] == 1
    assert deck["fitted_assets"]["plate"]["base_on_valid_local_day"] == 1
    assert deck["fitted_assets"]["plate_off_valid_day"] is False
    table = json.loads((root / "scores" / "p1dev" / "t8" / "table.json").read_text())
    assert table["frames"]["all"]["excluded_truth_is_plate_input"] == 1
    model = table["primary"]["pss"]["all"][0]
    assert model["method"] == "model" and model["breakdown"]["excluded_truth_is_plate_input"] == 1
    assert table["fitted_assets"]["all"]["truth_is_plate_input"] == 1
    kept = table["sensitivity_plate_off_valid_day"]["frames_kept"]
    assert kept == table["frames"]["all"]["plate_off_valid_day"] == 5  # all but 21 Nov's two
    text = (root / "scores" / "p1dev" / "t8" / "table.md").read_text()
    assert "excluded: truth is a plate input frame" in text and "Fitted assets, by" in text


def test_a_frame_that_fails_the_held_out_guard_stops_the_run(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = p1dev.frame_file_verdict
    target = world["keys"]["20261121T180000Z"]

    def guard(path: Path, **kw: Any) -> dict[str, Any]:
        verdict = real(path, **kw)
        if str(path).endswith(Path(target).name):
            return {**verdict, "development": False, "reasons": ["held out: test"]}
        return verdict

    monkeypatch.setattr(p1dev, "frame_file_verdict", guard)
    with pytest.raises(LeakageError, match="the run refuses it"):
        _run(world, run_id="t2")


def test_the_model_forecast_follows_the_render_class() -> None:
    def reading(cls: str | None) -> Reading:
        return Reading("v0", "scored", cls, None, None)

    assert model_forecast(reading("visible")) is True
    assert model_forecast(reading("partial")) is False  # as pair_score reads one pair
    assert (
        model_forecast(reading("hidden")) is False and model_forecast(reading("in_cloud")) is False
    )
    for cls in (None, "camera_fault", "no_reference", "excluded", "night"):
        assert model_forecast(reading(cls)) is None
    assert model_forecast(None) is None


def test_the_scene_viewpoint_takes_the_plates_camera() -> None:
    vp = load_manifest(VP)
    camera = {
        "lat": 46.787833,
        "lon": -121.7370365,
        "elev_m_msl": 1697.95,
        "heading_deg": 2.137,
        "pitch_deg": 9.9936,
        "roll_deg": 1.6426,
        "hfov_deg": 68.5637,
        "agl_m": 10.0,
    }
    moved = plate_viewpoint(vp, camera)
    assert (moved.camera.lat, moved.camera.lon) == (46.787833, -121.7370365)
    assert moved.camera.elevation_m == pytest.approx(1687.95)
    assert moved.raw["pose"] == {
        "heading_deg": 2.137,
        "pitch_deg": 9.9936,
        "roll_deg": 1.6426,
        "fov_deg": 68.5637,
    }
    assert vp.raw.get("pose") is None  # the manifest itself is untouched
    assert moved.landmarks == vp.landmarks and moved.time_zone == vp.time_zone


def test_options_are_checked() -> None:
    with pytest.raises(ValueError):
        P1DevOptions(extractor="v9")
    with pytest.raises(ValueError):
        P1DevOptions(block_length_days=0)
    fixed = P1DevOptions(run_id="a", batch_size=5).result_options()
    assert "run_id" not in fixed and "batch_size" not in fixed and fixed["lead_hours"] == 24.0


def test_a_timeline_without_daylight_frames_still_writes_a_table(world: dict[str, Any]) -> None:
    summary = _run(world, run_id="t3", limit=0)
    assert summary["daytime_frames"] == 0
    table = json.loads((world["root"] / "scores" / "p1dev" / "t3" / "table.json").read_text())
    assert table["frames"]["all"]["daytime_frames"] == 0


def test_a_module_run_is_recorded_as_a_runnable_command() -> None:
    import sys

    argv = ["/x/src/snow_globe_eval/cli.py", "p1dev", "--run-id", "r1"]
    assert p1dev._runnable(argv) == [
        sys.executable,
        "-m",
        "snow_globe_eval.cli",
        "p1dev",
        "--run-id",
        "r1",
    ]
    assert p1dev._runnable(["snow-globe-eval", "p1dev"]) == ["snow-globe-eval", "p1dev"]
    assert p1dev._command_line(["snow-globe-eval", "p1dev"]).startswith("cd ")


def test_table_only_renders_nothing_and_needs_no_node(world: dict[str, Any]) -> None:
    _run(world)
    rendered = list(world["fake"].rendered)

    def no_process(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"table-only ran {command}")

    run_p1dev(
        _options(world, table_only=True),
        command=["snow-globe-eval", "p1dev", "--table-only"],
        ephemeris=_DayNight(),
        runner=no_process,
        root=world["root"],
    )
    assert world["fake"].rendered == rendered
    run = json.loads((world["root"] / "scores" / "p1dev" / "t1" / "run.json").read_text())
    assert [inv["table_only"] for inv in run["invocations"]] == [False, True]
