"""The backtest pair (PRD §16 M0.5) with a stand-in renderer process.

The renderer here is a test double that writes a PNG and a metadata file the way
``pnpm render`` does. This tests the pair's plumbing (hashes, leakage checks, statuses); it is
not a measured run. The real end-to-end pair is the integration step's job. When Node 24 is
present, the scenes also go through the renderer's own validateScene().
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import NODE_REASON, REPO_RENDERER, NoaaSunFixedMoon
from lookout_common.manifest import load_manifest
from PIL import Image
from synth_forecast import low_deck, make_doc
from synth_images import PEAK_BOX, blurred, landscape, to_rgb

from lookout_eval.backtest_pair import PairError, PairInputs, run_pair
from lookout_eval.extractor import Region
from lookout_eval.leakage import HeldOut, LeakageError

VP_ID = "rainier.paradise-mountain"
RUN = datetime(2026, 11, 18, 12, tzinfo=UTC)
VALID = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)  # f32 of the 12Z run
EPHEMERIS = NoaaSunFixedMoon()  # a real Sun, so the renderer's validator accepts the scene


def _frame(root: Path, kind: str, moment: datetime, tag: str, image, **sidecar) -> Path:
    folder = root / kind / VP_ID / f"{moment:%Y-%m-%d}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{moment:%Y%m%dT%H%M%SZ}_{tag}.jpg"
    image.save(path, quality=92)
    path.with_suffix(".json").write_text(
        json.dumps(
            {
                "viewpoint_id": VP_ID,
                "image_time_utc": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
                **sidecar,
            }
        )
    )
    return path


@pytest.fixture
def world(tmp_path: Path):
    root = tmp_path / "lookout-data"
    root.mkdir()
    refs = [
        _frame(root, "frames", VALID - timedelta(days=d), f"{d:012x}", to_rgb(landscape(d)))
        for d in (2, 3, 4)
    ]
    truth = _frame(root, "archive", VALID, "8a1c9d2b", to_rgb(blurred(landscape(9), 4)))
    forecast = root / "forecast" / "hrrr" / VP_ID / "2026111812z.json"
    forecast.parent.mkdir(parents=True)
    forecast.write_text(json.dumps(make_doc(load_manifest(VP_ID), RUN, [32, 33], low_deck)))
    renderer = tmp_path / "renderer"
    shutil.copytree(REPO_RENDERER / "src", renderer / "src")  # validateScene() runs from here
    (renderer / "package.json").write_text("{}")
    if (REPO_RENDERER / "scene.schema.json").is_file():
        shutil.copy(REPO_RENDERER / "scene.schema.json", renderer / "scene.schema.json")
    return {"root": root, "refs": refs, "truth": truth, "forecast": forecast, "renderer": renderer}


def _inputs(world, **changes) -> PairInputs:
    base = {
        "viewpoint_id": VP_ID,
        "valid_time": VALID,
        "lead_hours": 24.0,
        "truth_path": world["truth"],
        "forecast": str(world["forecast"]),
        "region": Region.box(*PEAK_BOX, frame_size=(640, 360)),
        "reference_paths": tuple(world["refs"]),
        "renderer_dir": world["renderer"],
    }
    base.update(changes)
    return PairInputs(**base)


class FakeRenderer:
    """Writes the plate back as the 'render' (plus metadata), like `pnpm render` does."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, command, **kwargs) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        args = dict(zip(command[4::2], command[5::2], strict=False))
        if self.returncode == 0:
            Image.open(args["--plate"]).save(args["--out"])
            Path(args["--meta"]).write_text(
                json.dumps({"renderer_version": "test-double", "scene_hash": "0" * 64})
            )
        return subprocess.CompletedProcess(command, self.returncode, "{}", "boom" * self.returncode)


def _run(world, **changes):
    renderer = changes.pop("runner", FakeRenderer())
    ephemeris = changes.pop("ephemeris", EPHEMERIS)
    record = run_pair(
        _inputs(world, **changes), ephemeris=ephemeris, runner=renderer, root=world["root"]
    )
    return record, renderer


def _needs_schema(world) -> None:
    if not (world["renderer"] / "scene.schema.json").is_file():
        pytest.skip("renderer/scene.schema.json not present")


def test_full_pair_with_a_stand_in_renderer(world) -> None:
    _needs_schema(world)
    record, renderer = _run(world)
    assert record["status"] == "scored" and record["label"] == "v-1"
    stored = json.loads(Path(record["record_path"]).read_text())
    assert stored["pair_id"] == record["pair_id"]
    assert Path(record["record_path"]).is_relative_to(world["root"] / "scores" / "pairs" / VP_ID)
    inputs = record["inputs"]
    assert len(inputs["truth"]["sha256"]) == 64 and inputs["forecast"]["sha256"]
    assert inputs["forecast"]["fh_lo"] == 32 and inputs["forecast"]["weight_hi"] == 0.0
    assert inputs["plate"]["source"].startswith("the reference frame nearest")
    assert inputs["plate"]["id"] == f"{VP_ID}/plate-{inputs['plate']['sha256'][:12]}"
    assert all(len(r["sha256"]) == 64 for r in inputs["references"])
    assert record["lead_from_run_start_h"] == pytest.approx(32.0)
    assert record["versions"]["renderer_version_ts"] and record["versions"]["git"]["commit"]
    command = renderer.calls[0]
    assert command[:4] == ["pnpm", "--dir", str(world["renderer"].resolve()), "render"]
    # truth is the blurred peak (hidden); the stand-in render is a clear reference frame
    assert record["extractor"]["truth"]["state"] == "hidden"
    assert record["extractor"]["render"]["state"] == "visible"
    assert record["extractor"]["agree"] is False and record["extractor"]["scored"] is True
    assert record["s_rule"]["value"] is False  # the 2,400 m deck crosses the summit ray
    scene = json.loads(Path(record["scene"]["path"]).read_text())
    assert scene["meta"]["pair_id"] == record["pair_id"]
    # the scene names the plate the renderer was given, and the run it was issued from
    assert scene["plate"]["sha256"] == inputs["plate"]["sha256"]
    assert scene["plate"]["id"] == inputs["plate"]["id"]
    assert scene["run_init_utc"] == "2026-11-18T12:00:00Z" == record["scene"]["run_init_utc"]
    validation = record["scene"]["renderer_validation"]
    if NODE_REASON is None:
        assert validation == {"ok": True, "issues": [], "reason": None}
    else:
        assert validation["ok"] is None
    # the leakage checks are in the record
    checks = record["leakage_checks"]
    assert checks["result"] == "passed" and len(checks["references"]) == 3
    assert all(c["passed"] for c in checks["references"]) and checks["plate"]["passed"]
    assert checks["held_out_source"].startswith("design P sealed window")
    gates = record["gates"]["truth"]
    assert gates["scored"] and "same_view" in gates["not_checked"]


def test_rerun_is_deterministic(world) -> None:
    _needs_schema(world)
    first, _ = _run(world, scene_only=True)
    second, _ = _run(world, scene_only=True)
    assert first["pair_id"] == second["pair_id"]
    assert first["scene"]["sha256"] == second["scene"]["sha256"]


def test_scene_only_when_the_schema_is_missing(world) -> None:
    (world["renderer"] / "scene.schema.json").unlink(missing_ok=True)
    record, renderer = _run(world)
    assert record["status"] == "scene-only" and renderer.calls == []
    assert Path(record["scene"]["path"]).is_file()
    assert any("scene.schema.json" in note for note in record["notes"])


def test_render_failure_is_recorded(world) -> None:
    _needs_schema(world)
    record, _ = _run(world, runner=FakeRenderer(returncode=1))
    assert record["status"] == "render-failed" and record["render"]["returncode"] == 1
    assert record["extractor"] is None


@pytest.mark.skipif(NODE_REASON is not None, reason=str(NODE_REASON))
def test_a_scene_the_renderer_rejects_is_not_rendered(world) -> None:
    _needs_schema(world)
    from conftest import FixedEphemeris

    record, renderer = _run(world, ephemeris=FixedEphemeris(22.0, 100.0))  # wrong Sun
    assert record["status"] == "scene-rejected" and renderer.calls == []
    assert any("scene.sun" in issue for issue in record["scene"]["renderer_validation"]["issues"])


def test_forecast_available_after_issue_is_a_leak(world) -> None:
    with pytest.raises(LeakageError, match="after the issue time"):
        _run(world, lead_hours=31.5)  # issue 12:30Z, but f32 is modelled available ~13:52Z


def test_truth_must_be_at_the_valid_time(world) -> None:
    with pytest.raises(PairError, match="from the valid time"):
        _run(world, valid_time=VALID + timedelta(hours=1), lead_hours=25.0)


def test_truth_cannot_be_a_reference_or_the_plate(world) -> None:
    with pytest.raises(PairError, match="among the reference"):
        _run(world, reference_paths=(world["truth"], *world["refs"]))
    with pytest.raises(PairError, match="plate is the truth"):
        _run(world, plate_path=world["truth"])


def test_a_reference_after_the_issue_time_is_refused(world) -> None:
    """Review finding: a re-encoded copy of the truth, 5 min after the valid time, used to be
    accepted as a reference and chosen as the plate (it relit the truth scene)."""
    copy_time = VALID + timedelta(minutes=5)
    with Image.open(world["truth"]) as truth:
        copy = _frame(world["root"], "frames", copy_time, "0f0f0f0f0f0f", truth.convert("RGB"))
    with pytest.raises(LeakageError, match="not before the issue time"):
        _run(world, reference_paths=(copy, *world["refs"]))
    with pytest.raises(LeakageError, match=r"plate .*not before the issue time"):
        _run(world, plate_path=copy)
    assert not (world["root"] / "scores").exists()  # nothing was written


def test_a_reference_on_the_valid_day_is_refused(world) -> None:
    morning = VALID - timedelta(hours=4)  # 08:00 PST on the valid day, before a 6 h issue
    early = _frame(world["root"], "frames", morning, "1e1e1e1e1e1e", to_rgb(landscape(5)))
    with pytest.raises(LeakageError, match="valid time's local day"):
        _run(world, reference_paths=(early, *world["refs"]), lead_hours=2.0)


def test_a_live_reference_fetched_after_the_issue_time_is_refused(world) -> None:
    taken = VALID - timedelta(days=1, minutes=10)
    late = _frame(
        world["root"],
        "frames",
        taken,
        "2a2a2a2a2a2a",
        to_rgb(landscape(6)),
        fetched_at_utc=(VALID - timedelta(hours=23)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    with pytest.raises(LeakageError, match="available only at"):
        _run(world, reference_paths=(late, *world["refs"]))


def test_held_out_references_and_truth_are_refused(world) -> None:
    ref_time = VALID - timedelta(days=3)
    inside = HeldOut(((ref_time - timedelta(hours=1), ref_time + timedelta(hours=1)),))
    with pytest.raises(LeakageError, match=r"000000000003\.jpg: held out"):
        _run(world, held_out=inside, reference_paths=(world["refs"][1],))
    with pytest.raises(LeakageError, match=r"000000000002\.jpg: not before the held-out"):
        _run(world, held_out=inside)  # the newer reference comes after the held-out start
    # a held-out period before every reference: fits only from frames that precede it
    before = HeldOut(((VALID - timedelta(days=10), VALID - timedelta(days=9)),))
    with pytest.raises(LeakageError, match=r"PRD §13\.9"):
        _run(world, held_out=before)
    truth_held = HeldOut(((VALID - timedelta(hours=1), VALID + timedelta(hours=1)),))
    with pytest.raises(LeakageError, match=r"truth frame .* is held out"):
        _run(world, held_out=truth_held)


def test_a_reference_in_the_sealed_window_is_refused_by_default(world) -> None:
    sealed_day = datetime(2026, 12, 20, 20, tzinfo=UTC)
    held = _frame(world["root"], "frames", sealed_day, "3b3b3b3b3b3b", to_rgb(landscape(7)))
    with pytest.raises(LeakageError, match=r"not before the issue time.*held out"):
        _run(world, reference_paths=(held, *world["refs"]))


def test_a_camera_fault_truth_frame_is_not_scored(world) -> None:
    _needs_schema(world)
    black = _frame(
        world["root"],
        "archive",
        VALID + timedelta(minutes=1),
        "5c5c5c5c",
        Image.new("RGB", (640, 360), (0, 0, 0)),
        sha256="ab" * 32,
    )
    world["truth"].unlink()
    world["truth"].with_suffix(".json").unlink()
    record, _ = _run(world, truth_path=black)
    assert record["status"] == "scored"
    assert record["extractor"]["truth"]["p1_class"] == "camera_fault"
    assert record["extractor"]["scored"] is False and record["extractor"]["agree"] is None
    assert record["extractor"]["excluded_by"] == ["usable_frame"]


def test_fresh_gate_compares_with_the_previous_capture(world) -> None:
    _needs_schema(world)
    world["truth"].unlink()
    world["truth"].with_suffix(".json").unlink()
    side = {"sha256": "cd" * 32}
    earlier = VALID - timedelta(minutes=15)
    _frame(world["root"], "archive", earlier, "6d6d6d6d", to_rgb(landscape(9)), **side)
    repeat = _frame(
        world["root"],
        "archive",
        VALID + timedelta(minutes=2),
        "7e7e7e7e",
        to_rgb(blurred(landscape(9), 4)),
        **side,
    )
    record, _ = _run(world, truth_path=repeat)
    fresh = next(r for r in record["gates"]["truth"]["results"] if r["gate"] == "fresh")
    assert fresh["passed"] is False and "SHA-256" in fresh["reason"]
    assert record["extractor"]["excluded_by"] == ["fresh"]


def test_frames_from_another_viewpoint_are_refused(world) -> None:
    folder = world["root"] / "frames" / "point-reyes.beach" / "2026-11-16"
    folder.mkdir(parents=True)
    stranger = folder / "20261116T200000Z_4d4d4d4d4d4d.jpg"
    to_rgb(landscape(8)).save(stranger, quality=92)
    with pytest.raises(PairError, match=r"from viewpoint point-reyes\.beach"):
        _run(world, reference_paths=(stranger, *world["refs"]))
    with pytest.raises(PairError, match=r"from viewpoint point-reyes\.beach"):
        _run(world, plate_path=stranger)


def test_forecast_for_another_viewpoint_is_refused(world, tmp_path) -> None:
    other = tmp_path / "other.json"
    other.write_text(json.dumps(make_doc(load_manifest("point-reyes.beach"), RUN, [32], low_deck)))
    with pytest.raises(PairError, match="forecast file is for"):
        _run(world, forecast=str(other))


def test_auto_needs_lookout_forecast(world) -> None:
    pytest.importorskip("PIL")
    try:
        import lookout_forecast  # noqa: F401
    except ImportError:
        with pytest.raises(PairError, match="uv sync --group forecast"):
            _run(world, forecast="auto")
    else:
        pytest.skip("lookout-forecast is installed; 'auto' would reach the network")
