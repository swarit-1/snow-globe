"""The backtest pair (PRD §16 M0.5) with a stand-in renderer process.

The renderer here is a test double that writes a PNG and a metadata file the way
``pnpm render`` does. This tests the pair's plumbing (hashes, leakage checks, statuses); it is
not a measured run. The real end-to-end pair is the integration step's job. When Node 24 is
present, the scenes also go through the renderer's own validateScene().

The base world's truth frame is a live frame: a synthetic *archived* frame dated 19 Nov 2026
would be refused by the archive guard (D-019), as its own test shows.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from conftest import NODE_REASON, REPO_RENDERER, NoaaSunFixedMoon
from PIL import Image
from snow_globe_common.manifest import load_manifest
from snow_globe_common.timeutil import isoformat_z
from synth_forecast import low_deck, make_doc
from synth_images import PEAK_BOX, blurred, landscape, to_rgb

import snow_globe_eval.backtest_pair as backtest_pair
import snow_globe_eval.scene as scene_module
from snow_globe_eval.backtest_pair import PairError, PairInputs, run_pair
from snow_globe_eval.extractor import Region
from snow_globe_eval.forecast_io import ForecastRun
from snow_globe_eval.heldout import committed_split
from snow_globe_eval.leakage import HeldOut, LeakageError, SealedScoring

VP_ID = "rainier.paradise-mountain"
RUN = datetime(2026, 11, 18, 12, tzinfo=UTC)
VALID = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)  # f32 of the 12Z run
EPHEMERIS = NoaaSunFixedMoon()  # a real Sun, so the renderer's validator accepts the scene
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _frame(
    root: Path, kind: str, moment: datetime, tag: str, image, *, vp: str = VP_ID, **sidecar
) -> Path:
    folder = root / kind / vp / f"{moment:%Y-%m-%d}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{moment:%Y%m%dT%H%M%SZ}_{tag}.jpg"
    image.save(path, quality=92)
    path.with_suffix(".json").write_text(
        json.dumps(
            {
                "viewpoint_id": vp,
                "image_time_utc": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
                **sidecar,
            }
        )
    )
    return path


def _forecast(root: Path, vp: str, run: datetime, hours: list[int]) -> Path:
    path = root / "forecast" / "hrrr" / vp / f"{run:%Y%m%d%H}z.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(make_doc(load_manifest(vp), run, hours, low_deck)))
    return path


def _make_world(tmp_path: Path, vp: str = VP_ID) -> dict:
    root = tmp_path / "snow-globe-data"
    root.mkdir()
    refs = [
        _frame(root, "frames", VALID - timedelta(days=d), f"{d:012x}", to_rgb(landscape(d)), vp=vp)
        for d in (2, 3, 4)
    ]
    truth = _frame(root, "frames", VALID, "8a1c9d2b", to_rgb(blurred(landscape(9), 4)), vp=vp)
    forecast = _forecast(root, vp, RUN, [32, 33])
    renderer = tmp_path / "renderer"
    shutil.copytree(REPO_RENDERER / "src", renderer / "src")  # validateScene() runs from here
    (renderer / "package.json").write_text("{}")
    if (REPO_RENDERER / "scene.schema.json").is_file():
        shutil.copy(REPO_RENDERER / "scene.schema.json", renderer / "scene.schema.json")
    return {
        "vp": vp,
        "root": root,
        "refs": refs,
        "truth": truth,
        "forecast": forecast,
        "renderer": renderer,
    }


@pytest.fixture
def world(tmp_path: Path):
    return _make_world(tmp_path)


def _inputs(world, **changes) -> PairInputs:
    base = {
        "viewpoint_id": world["vp"],
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
    assert checks["held_out_source"].startswith(
        "committed split snow_globe_eval/heldout_periods_v2.json v2 (sha256 82574ceb38eb)"
    )
    # New records cite the current split, version 2 (the M0.5 records stay on version 1).
    assert checks["split"] == committed_split().to_record()
    assert (
        checks["split"]["version"] == 2 and checks["split"]["kind"] == "snow-globe.heldout-periods"
    )
    assert checks["held_out"] == [["2026-12-15T00:00:00-08:00", "2027-01-15T00:00:00-08:00"]]
    assert checks["fit_bound_utc"] == "2026-12-15T08:00:00Z" and checks["design"] is None
    assert checks["truth_held_out"] is False and checks["sealed_scoring"] is None
    assert checks["scoring_exclusion"] is None and checks["truth_archive"] is False
    assert not any(c["archive"] for c in checks["references"])
    # Eligible under the split and the gates, but no v-1 pair counts toward a metric.
    extractor = record["extractor"]
    assert extractor["eligible_for_metrics"] is True and extractor["counts_toward_metrics"] is False
    assert record["score"]["counts_toward_metrics"] is False
    gates = record["gates"]["truth"]
    assert gates["scored"] and "same_view" in gates["not_checked"]


def test_no_v1_pair_counts_toward_metrics(world) -> None:
    """Review finding: a v-1 record said counts_toward_metrics: true, so a later table builder
    keying on it would have counted an unvalidated extractor's pair (PRD §13.8)."""
    _needs_schema(world)
    record, _ = _run(world)
    extractor, score = record["extractor"], record["score"]
    assert record["label"] == "v-1" and extractor["version"] == "v-1"
    assert extractor["eligible_for_metrics"] is True and score["eligible_for_metrics"] is True
    assert extractor["counts_toward_metrics"] is False and score["counts_toward_metrics"] is False
    reasons = extractor["not_counted_because"]
    assert any("not validated against labels (PRD §13.8)" in r for r in reasons)
    assert any("pipeline test" in r for r in reasons)
    assert not any("quality gate" in r or "scoring exclusion" in r for r in reasons)
    assert not backtest_pair.VALIDATED_EXTRACTORS  # no extractor is validated yet (M4)


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


def _byte_copy(root: Path, source: Path, moment: datetime, tag: str) -> Path:
    """The same bytes under another, earlier frame name (with a sidecar)."""
    folder = root / "frames" / VP_ID / f"{moment:%Y-%m-%d}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{moment:%Y%m%dT%H%M%SZ}_{tag}.jpg"
    shutil.copyfile(source, path)
    side = {"viewpoint_id": VP_ID, "image_time_utc": moment.strftime("%Y-%m-%dT%H:%M:%SZ")}
    path.with_suffix(".json").write_text(json.dumps(side))
    return path


def test_truth_cannot_be_a_reference_or_the_plate(world) -> None:
    # The truth file itself is refused by the fitted-asset rules: it is taken at the valid time.
    with pytest.raises(LeakageError, match="not before the issue time"):
        _run(world, reference_paths=(world["truth"], *world["refs"]))
    with pytest.raises(LeakageError, match=r"plate .*not before the issue time"):
        _run(world, plate_path=world["truth"])
    # Its bytes under an earlier, legal-looking name are caught by the SHA-256 comparison.
    copy = _byte_copy(world["root"], world["truth"], VALID - timedelta(days=5), "c0c0c0c0c0c0")
    with pytest.raises(PairError, match="among the reference"):
        _run(world, reference_paths=(copy, *world["refs"]))
    with pytest.raises(PairError, match="plate is the truth"):
        _run(world, plate_path=copy)


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
        "frames",
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
    assert record["extractor"]["eligible_for_metrics"] is False
    assert "quality gate (PRD §8.6): usable_frame" in record["extractor"]["not_counted_because"][0]


def test_fresh_gate_compares_with_the_previous_capture(world) -> None:
    _needs_schema(world)
    world["truth"].unlink()
    world["truth"].with_suffix(".json").unlink()
    side = {"sha256": "cd" * 32}
    earlier = VALID - timedelta(minutes=15)
    _frame(world["root"], "frames", earlier, "6d6d6d6d", to_rgb(landscape(9)), **side)
    repeat = _frame(
        world["root"],
        "frames",
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


def test_auto_needs_snow_globe_forecast(world) -> None:
    pytest.importorskip("PIL")
    try:
        import snow_globe_forecast  # noqa: F401
    except ImportError:
        with pytest.raises(PairError, match="uv sync --group forecast"):
            _run(world, forecast="auto")
    else:
        pytest.skip("snow-globe-forecast is installed; 'auto' would reach the network")


# --- the committed held-out split (D-019, D-022, D-024) ---------------------------------------


class _ReadSpy:
    """Records every file the pair hashes or opens (frames, plate, sky mask, depth), to show
    the guard runs first."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.paths: list[Path] = []
        real_sha, real_open = backtest_pair.sha256_file, Image.open

        def sha(path):
            self.paths.append(Path(path).resolve())
            return real_sha(path)

        def opener(path, *args, **kwargs):
            self.paths.append(Path(path).resolve())
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(backtest_pair, "sha256_file", sha)
        monkeypatch.setattr(scene_module, "sha256_file", sha)  # PlateIdentity's hashes
        monkeypatch.setattr(backtest_pair.Image, "open", opener)


def test_an_archived_truth_frame_past_the_guard_is_refused_before_it_is_read(
    world, monkeypatch
) -> None:
    """D-019: an archived frame dated 1 Jun 2026 or later is refused at every viewpoint."""
    archived = _frame(world["root"], "archive", VALID, "9b9b9b9b", to_rgb(landscape(9)))
    spy = _ReadSpy(monkeypatch)
    with pytest.raises(LeakageError, match=r"archive guard \(D-019\): local date 2026-11-19"):
        _run(world, truth_path=archived)
    assert spy.paths == [] and not (world["root"] / "scores").exists()


def test_the_summer_standard_time_edge_decides_an_archived_reference(world) -> None:
    """ImageCreateDateTime stays on standard time in summer; the latest local date counts.

    Since the lead's held-out reading of 20 Nov (pending the owner's D-038), "dated 1 Jun 2026
    or later" is the camera's local date OR the UTC date, so a 31 May evening frame stamped
    1 Jun UTC is held out too; only a frame dated 31 May both ways may be read."""
    nps = {"source": "nps-archive", "location_code": "mora"}
    edge = _frame(
        world["root"],
        "archive",
        datetime(2026, 6, 1, 7, 30, tzinfo=UTC),
        "e1e1e1e1",
        to_rgb(landscape(11)),
        image_create_datetime_raw="05/31/2026 11:30:00 PM",
        local_time_string="6/1/2026 12:30 AM Pacific Daylight Time",
        **nps,
    )
    with pytest.raises(LeakageError, match=r"archive guard \(D-019\): local date 2026-06-01"):
        _run(world, reference_paths=(edge, *world["refs"]))
    # 23:45 PDT on 31 May is still May locally, but its UTC date (and folder) is 1 Jun.
    utc_june = _frame(
        world["root"],
        "archive",
        datetime(2026, 6, 1, 6, 45, tzinfo=UTC),
        "e2e2e2e2",
        to_rgb(landscape(12)),
        image_create_datetime_raw="05/31/2026 10:45:00 PM",
        local_time_string="5/31/2026 11:45 PM Pacific Daylight Time",
        **nps,
    )
    with pytest.raises(LeakageError, match=r"archive guard \(D-019\): UTC date 2026-06-01"):
        _run(world, reference_paths=(utc_june, *world["refs"]), scene_only=True)
    # 16:45 PDT on 31 May is 23:45 UTC on 31 May: dated 31 May both ways, so it may be read.
    may = _frame(
        world["root"],
        "archive",
        datetime(2026, 5, 31, 23, 45, tzinfo=UTC),
        "e3e3e3e3",
        to_rgb(landscape(13)),
        image_create_datetime_raw="05/31/2026 03:45:00 PM",
        local_time_string="5/31/2026 4:45 PM Pacific Daylight Time",
        **nps,
    )
    record, _ = _run(world, reference_paths=(may, *world["refs"]), scene_only=True)
    first = record["leakage_checks"]["references"][0]
    assert (
        first["passed"] and first["archive"] and first["image_time_utc"] == "2026-05-31T23:45:00Z"
    )


def test_quarantined_inputs_are_refused_before_anything_is_read(
    world, monkeypatch, tmp_path
) -> None:
    """D-022: anything under $SNOW_GLOBE_DATA_DIR/quarantine/ is refused on every path, the
    forecast summary and the landmark boxes file included (review finding)."""
    breach = world["root"] / "quarantine" / "held-out-breach-2026-11-19"
    quarantined = _frame(
        breach, "archive", VALID - timedelta(days=3, minutes=7), "0a0a0a0a", to_rgb(landscape(3))
    )
    link_dir = world["root"] / "frames" / VP_ID / "2026-11-14"
    link_dir.mkdir(parents=True)
    link = link_dir / "20261114T200000Z_0b0b0b0b0b0b.jpg"
    link.symlink_to(quarantined)  # a symlink into the quarantine resolves there
    summary = breach / "summary.json"
    summary.write_text(json.dumps(_summary_for(world)))  # a valid summary for this pair
    boxes = _boxes(tmp_path)
    hidden_boxes = replace(boxes, source=str(breach / "boxes.json"))
    spy = _ReadSpy(monkeypatch)
    for changes in (
        {"reference_paths": (quarantined, *world["refs"])},
        {"reference_paths": (link, *world["refs"])},
        {"plate_path": quarantined},
        {"truth_path": quarantined},
        {"sky_mask_path": quarantined},
        {"forecast": str(quarantined)},
        {"forecast_summary": summary},
        {"boxes": hidden_boxes, "region": boxes.primary_box.region()},
    ):
        with pytest.raises(LeakageError, match="quarantine"):
            _run(world, **changes)
    assert spy.paths == [] and not (world["root"] / "scores").exists()


def test_a_forecast_summary_under_quarantine_is_refused(world) -> None:
    """Review finding (scratch test 1): a valid summary under quarantine/ was read, embedded
    and the pair scored. It is refused before any frame is read, with the role named."""
    breach = world["root"] / "quarantine" / "held-out-breach-2026-11-19"
    breach.mkdir(parents=True)
    summary = breach / "summary.json"
    summary.write_text(json.dumps(_summary_for(world)))
    with pytest.raises(LeakageError, match=r"forecast summary .*quarantine"):
        _run(world, forecast_summary=summary, scene_only=True)
    # The same summary outside the quarantine is accepted (it describes this pair).
    outside = world["root"] / "summary.json"
    outside.write_text(summary.read_text())
    record, _ = _run(world, forecast_summary=outside, scene_only=True)
    assert record["inputs"]["forecast"]["summary"]["choice"]["run"] == "2026111812"


def test_an_auto_forecast_file_under_quarantine_is_refused(world, monkeypatch) -> None:
    """With --forecast auto the file's path is known only after the backfill: it is checked
    then, before the file is loaded (here the backfill is a stand-in; no network)."""
    breach = world["root"] / "quarantine" / "held-out-breach-2026-11-19"
    breach.mkdir(parents=True)
    hidden = breach / "2026111812z.json"
    shutil.copyfile(world["forecast"], hidden)
    loaded: list[Path] = []
    real_load = backtest_pair.ForecastRun.load

    def load(path):
        loaded.append(Path(path))
        return real_load(path)

    monkeypatch.setattr(backtest_pair, "auto_forecast", lambda *a, **k: (hidden, {"run": "x"}))
    monkeypatch.setattr(backtest_pair.ForecastRun, "load", staticmethod(load))
    with pytest.raises(LeakageError, match=r"forecast .*quarantine"):
        _run(world, forecast="auto", scene_only=True)
    assert loaded == [] and not (world["root"] / "scores").exists()
    # The same file outside the quarantine is used.
    monkeypatch.setattr(
        backtest_pair, "auto_forecast", lambda *a, **k: (world["forecast"], {"run": "2026111812"})
    )
    record, _ = _run(world, forecast="auto", scene_only=True)
    assert record["inputs"]["forecast"]["mode"] == "auto" and loaded == [world["forecast"]]


def test_a_boxes_file_under_quarantine_is_refused(world, tmp_path) -> None:
    """Review finding (scratch test 2): a boxes file under quarantine/ was loaded and used.
    load_boxes refuses it before reading, and the pair refuses boxes that name it."""
    from snow_globe_eval.landmark_boxes import load_boxes

    boxes = _boxes(tmp_path)
    breach = world["root"] / "quarantine" / "held-out-breach-2026-11-19"
    breach.mkdir(parents=True)
    hidden = breach / "boxes.json"
    hidden.write_text(Path(boxes.source).read_text())
    with pytest.raises(LeakageError, match=r"landmark boxes .*quarantine"):
        load_boxes(VP_ID, hidden, roots=(world["root"],))
    variant = world["root"] / "Quarantine" / hidden.relative_to(world["root"] / "quarantine")
    with pytest.raises(LeakageError, match="quarantine"):
        load_boxes(VP_ID, variant, roots=(world["root"],))  # another letter case, same folder
    with pytest.raises(LeakageError, match=r"landmark boxes .*quarantine"):
        _run(world, boxes=replace(boxes, source=str(hidden)), region=boxes.primary_box.region())
    assert not (world["root"] / "scores").exists()


def test_extra_held_out_periods_only_add_to_the_committed_split(world) -> None:
    """An empty PairInputs.held_out cannot reopen the sealed window."""
    sealed_day = datetime(2026, 12, 20, 20, tzinfo=UTC)
    truth = _frame(world["root"], "frames", sealed_day, "4e4e4e4e", to_rgb(landscape(9)))
    for extra in (None, HeldOut()):
        with pytest.raises(
            LeakageError, match=r"is held out: in the held-out period sealed-window"
        ):
            _run(world, truth_path=truth, valid_time=sealed_day, held_out=extra)


def test_only_the_sealed_scoring_reads_a_held_out_truth_frame(world) -> None:
    sealed_day = datetime(2026, 12, 20, 20, tzinfo=UTC)
    truth = _frame(world["root"], "frames", sealed_day, "4f4f4f4f", to_rgb(landscape(9)))
    refs = [
        _frame(world["root"], "frames", datetime(2026, 12, d, 20, tzinfo=UTC), f"{d:012x}", img)
        for d, img in ((10, to_rgb(landscape(2))), (11, to_rgb(landscape(3))))
    ]
    forecast = _forecast(world["root"], VP_ID, datetime(2026, 12, 19, 12, tzinfo=UTC), [32, 33])
    pair = {
        "truth_path": truth,
        "valid_time": sealed_day,
        "reference_paths": tuple(refs),
        "forecast": str(forecast),
        "scene_only": True,
    }
    with pytest.raises(LeakageError, match="read only by the sealed scoring"):
        _run(world, **pair)
    record, _ = _run(world, **pair, sealed=SealedScoring(COMMIT))
    checks = record["leakage_checks"]
    assert record["status"] == "scene-only" and checks["sealed_scoring"] == COMMIT
    assert checks["truth_held_out"] is True and "sealed-window" in checks["truth_held_out_reason"]
    assert checks["scoring_exclusion"] is None
    # fitted assets stay development-only under the sealed scoring
    inside_day = datetime(2026, 12, 16, 20, tzinfo=UTC)  # before the issue, in the window
    inside = _frame(world["root"], "frames", inside_day, "d" * 12, to_rgb(landscape(4)))
    with pytest.raises(
        LeakageError, match=r"reference frame .*: held out \(in the held-out period"
    ):
        _run(world, **{**pair, "reference_paths": (inside, *refs)}, sealed=SealedScoring(COMMIT))


def test_the_design_sets_the_fit_bound_at_point_reyes(tmp_path) -> None:
    """Point Reyes is held out in summer 2026 (H) and in the sealed window (P), so a design-P
    pair may fit on live frames from Nov 2026 and the strictest bound may not."""
    pr = _make_world(tmp_path, "point-reyes.beach")
    with pytest.raises(LeakageError, match=r"starts 2026-06-01T07:00:00Z \(PRD §13\.9\)"):
        _run(pr, scene_only=True)
    with pytest.raises(LeakageError, match=r"starts 2026-06-01T07:00:00Z \(PRD §13\.9\)"):
        _run(pr, scene_only=True, design="H")
    record, _ = _run(pr, scene_only=True, design="P")
    checks = record["leakage_checks"]
    assert checks["design"] == "P" and checks["fit_bound_utc"] == "2026-12-15T08:00:00Z"
    assert len(checks["held_out"]) == 2  # the sealed window and summer 2026


def test_a_design_the_viewpoint_lacks_is_refused(world) -> None:
    with pytest.raises(LeakageError, match="has no design H"):
        _run(world, design="H")
    with pytest.raises(ValueError, match="design must be one of"):
        _inputs(world, design="X")


def test_a_pair_at_grand_teton_never_counts_toward_metrics(tmp_path) -> None:
    """D-024: Grand Teton has no design; it is for development use only."""
    gt = _make_world(tmp_path, "grand-teton.science-center")
    record, _ = _run(gt, scene_only=True)
    exclusion = record["leakage_checks"]["scoring_exclusion"]
    assert exclusion and "excluded from scored metrics" in exclusion
    assert (
        record["leakage_checks"]["held_out"] == []
        and record["leakage_checks"]["fit_bound_utc"] is None
    )


def test_a_scored_pair_at_grand_teton_never_counts_toward_metrics(tmp_path) -> None:
    """Review finding: the Grand Teton test ran scene-only, so ``counts_toward_metrics`` was
    never seen. A scored pair there passes its gates and still never counts (D-024)."""
    gt = _make_world(tmp_path, "grand-teton.science-center")
    _needs_schema(gt)
    record, _ = _run(gt)
    assert record["status"] == "scored" and record["extractor"]["scored"] is True
    assert record["extractor"]["eligible_for_metrics"] is False
    assert record["extractor"]["counts_toward_metrics"] is False
    assert any("scoring exclusion" in r for r in record["extractor"]["not_counted_because"])
    assert "excluded from scored metrics" in record["leakage_checks"]["scoring_exclusion"]


def test_15_jul_2026_is_scored_but_never_counts_toward_metrics(tmp_path) -> None:
    """Review finding: nothing kept a 15 Jul 2026 pair (D-022) out of scored metrics. At Point
    Reyes that day is held out (summer 2026), so only the sealed scoring reads the truth; the
    pair is scored but never counts. 16 Jul counts."""
    pr = _make_world(tmp_path, "point-reyes.beach")
    _needs_schema(pr)
    root, vp = pr["root"], pr["vp"]
    refs = tuple(
        _frame(root, "frames", datetime(2026, 5, d, 20, tzinfo=UTC), f"{d:012x}", img, vp=vp)
        for d, img in ((20, to_rgb(landscape(2))), (21, to_rgb(landscape(3))))
    )

    def pair(day: int) -> dict:
        valid = datetime(2026, 7, day, 20, tzinfo=UTC)
        truth = _frame(root, "frames", valid, f"{day:08x}", to_rgb(blurred(landscape(9), 4)), vp=vp)
        forecast = _forecast(root, vp, valid - timedelta(hours=32), [32, 33])
        return {
            "truth_path": truth,
            "valid_time": valid,
            "reference_paths": refs,
            "forecast": str(forecast),
            "design": "H",
        }

    breach_day = pair(15)
    with pytest.raises(LeakageError, match="read only by the sealed scoring"):
        _run(pr, **breach_day)
    record, _ = _run(pr, **breach_day, sealed=SealedScoring(COMMIT))
    checks = record["leakage_checks"]
    assert record["status"] == "scored" and record["extractor"]["scored"] is True
    assert checks["truth_held_out"] is True and "summer-2026" in checks["truth_held_out_reason"]
    assert "D-022" in checks["scoring_exclusion"]
    assert record["extractor"]["eligible_for_metrics"] is False
    assert record["extractor"]["counts_toward_metrics"] is False
    next_day, _ = _run(pr, **pair(16), sealed=SealedScoring(COMMIT))
    assert next_day["status"] == "scored" and next_day["extractor"]["scored"] is True
    assert next_day["leakage_checks"]["scoring_exclusion"] is None
    assert next_day["extractor"]["eligible_for_metrics"] is True
    assert next_day["extractor"]["counts_toward_metrics"] is False  # v-1: never counts


def test_a_pre_guard_hindcast_pair_is_scored(tmp_path) -> None:
    """Review finding: no test ran an archived-truth (design H) pair to the end. Point Reyes,
    July 2025, with NPS's own time strings: scored, archived, nothing held out or excluded."""
    pr = _make_world(tmp_path, "point-reyes.beach")
    _needs_schema(pr)
    root, vp = pr["root"], pr["vp"]

    def archived(day: int, tag: str, image) -> Path:  # 20:00Z is 1:00 PM PDT, 12:00 PM PST
        return _frame(
            root,
            "archive",
            datetime(2025, 7, day, 20, tzinfo=UTC),
            tag,
            image,
            vp=vp,
            source="nps-archive",
            image_create_datetime_raw=f"07/{day:02d}/2025 12:00:00 PM",
            local_time_string=f"7/{day}/2025 1:00 PM Pacific Daylight Time",
        )

    refs = tuple(archived(d, f"{d:08x}", to_rgb(landscape(d))) for d in (10, 11, 12))
    truth = archived(15, "7a7a7a7a", to_rgb(blurred(landscape(9), 4)))
    valid = datetime(2025, 7, 15, 20, tzinfo=UTC)
    forecast = _forecast(root, vp, valid - timedelta(hours=32), [32, 33])
    record, _ = _run(
        pr,
        truth_path=truth,
        valid_time=valid,
        reference_paths=refs,
        forecast=str(forecast),
        design="H",
    )
    checks = record["leakage_checks"]
    assert record["status"] == "scored" and record["extractor"]["scored"] is True
    assert checks["truth_archive"] is True and checks["truth_held_out"] is False
    assert all(c["archive"] and c["passed"] for c in checks["references"])
    assert checks["fit_bound_utc"] == "2026-06-01T07:00:00Z" and checks["design"] == "H"
    assert checks["scoring_exclusion"] is None
    assert record["extractor"]["eligible_for_metrics"] is True
    assert record["extractor"]["counts_toward_metrics"] is False  # v-1: never counts
    assert not any("not under archive/" in note for note in record["notes"])


def test_quarantine_variants_are_refused_before_anything_is_read(world, monkeypatch) -> None:
    """Review finding: on a case-insensitive disk <root>/QUARANTINE/... is the quarantine, but
    it passed every pair check: a sky mask was hashed and, under the sealed scoring, a
    quarantined truth was accepted."""
    root = world["root"]
    breach = root / "quarantine" / "held-out-breach-2026-11-19"
    ref = _frame(
        breach, "frames", VALID - timedelta(days=3, minutes=7), "0a0a0a0a0a0a", to_rgb(landscape(3))
    )
    truth = _frame(breach, "frames", VALID, "0c0c0c0c", to_rgb(landscape(9)))
    mask = breach / "mask.png"
    Image.new("L", (640, 360), 255).save(mask)

    def variant(path: Path, spelling: str) -> Path:
        return root / spelling / path.relative_to(root / "quarantine")

    spy = _ReadSpy(monkeypatch)
    for changes in (
        {"sky_mask_path": variant(mask, "QUARANTINE"), "scene_only": True},
        {"depth_path": variant(mask, "Quarantine"), "scene_only": True},
        {"reference_paths": (variant(ref, "Quarantine"), *world["refs"])},
        {"plate_path": variant(ref, "QUARANTINE")},
        {"truth_path": variant(truth, "Quarantine"), "sealed": SealedScoring(COMMIT)},
    ):
        with pytest.raises(LeakageError, match="quarantine"):
            _run(world, **changes)
    assert spy.paths == [] and not (root / "scores").exists()


def test_a_reference_outside_the_layout_is_dated_by_its_nps_strings(world) -> None:
    """Review finding: a frame outside frames/ and archive/ keeps its sidecar's NPS times, and
    they bring the archive guard, with or without an nps-archive source."""
    loose = world["root"] / "loose"
    loose.mkdir()
    path = loose / "reference.jpg"
    to_rgb(landscape(5)).save(path, quality=92)
    side = {
        "viewpoint_id": VP_ID,
        "image_time_utc": "2026-05-20T20:00:00Z",  # before the guard; its NPS times are not
        "image_create_datetime_raw": "06/15/2026 12:00:00 PM",
        "local_time_string": "6/15/2026 1:00 PM Pacific Daylight Time",
    }
    for source in ({}, {"source": "nps-archive"}):
        path.with_suffix(".json").write_text(json.dumps({**side, **source}))
        with pytest.raises(LeakageError, match=r"archive guard \(D-019\): local date 2026-06-15"):
            _run(world, reference_paths=(path, *world["refs"]), scene_only=True)
    # Without NPS times it is an ordinary development frame of 20 May 2026.
    plain = {k: v for k, v in side.items() if not k.endswith(("_raw", "_string"))}
    path.with_suffix(".json").write_text(json.dumps(plain))
    record, _ = _run(world, reference_paths=(path, *world["refs"]), scene_only=True)
    first = record["leakage_checks"]["references"][0]
    assert first["passed"] and first["archive"] is False
    assert first["image_time_utc"] == "2026-05-20T20:00:00Z"


def test_a_previous_capture_linked_into_the_quarantine_is_refused(world) -> None:
    """The Fresh gate reads the previous capture's sidecar: one that links into the
    quarantine is refused, before the pair writes anything (D-022)."""
    breach = world["root"] / "quarantine" / "held-out-breach-2026-11-19"
    earlier = VALID - timedelta(minutes=15)
    quarantined = _frame(breach, "frames", earlier, "6d6d6d6d", to_rgb(landscape(9)))
    previous = world["truth"].parent / quarantined.name
    shutil.copyfile(quarantined, previous)  # the frame itself is a development copy
    previous.with_suffix(".json").symlink_to(quarantined.with_suffix(".json"))
    with pytest.raises(LeakageError, match=r"previous capture's sidecar .*D-022"):
        _run(world)
    assert not (world["root"] / "scores").exists()


def test_a_reference_outside_the_layout_that_cannot_be_dated_is_refused(world) -> None:
    """Outside frames/ and archive/, a frame is dated by its sidecar or its name. A broken
    sidecar, or none and a name that is not a frame key, is a PairError, before any read."""
    loose = world["root"] / "loose"
    loose.mkdir()
    path = loose / "reference.jpg"
    to_rgb(landscape(5)).save(path, quality=92)
    path.with_suffix(".json").write_text("{not json")
    with pytest.raises(PairError, match="cannot tell when"):
        _run(world, reference_paths=(path, *world["refs"]), scene_only=True)
    path.with_suffix(".json").unlink()
    with pytest.raises(PairError, match="cannot tell when"):
        _run(world, reference_paths=(path, *world["refs"]), scene_only=True)


# --- the M0.5 pair's outputs: renders/pairs, tags, timings, boxes, score -------------------


def _boxes(tmp_path: Path, vp: str = VP_ID):
    """Rainier boxes for the synthetic world: the peak (primary) and a foreground strip."""
    from snow_globe_eval.landmark_boxes import load_boxes

    x0, y0, x1, y1 = PEAK_BOX
    path = tmp_path / "boxes.json"
    path.write_text(
        json.dumps(
            {
                "kind": "snow-globe.landmark-boxes",
                "version": "v-1",
                "viewpoints": {
                    vp: {
                        "primary": "peak",
                        "boxes": [
                            {"name": "peak", "box": [x0 / 640, y0 / 360, x1 / 640, y1 / 360]},
                            {"name": "foreground", "box": [0.05, 0.8, 0.95, 0.95]},
                        ],
                    }
                },
            }
        )
    )
    return load_boxes(vp, path)


def test_the_render_goes_under_renders_pairs_and_the_record_under_scores_pairs(world) -> None:
    _needs_schema(world)
    record, renderer = _run(world)
    root, name = world["root"], record["output_name"]
    assert name == record["pair_id"] and record["tag"] is None
    render_dir = root / "renders" / "pairs" / VP_ID / name
    assert Path(record["render"]["png"]["path"]) == render_dir / "render.png"
    assert Path(record["render"]["log"]) == render_dir / "renderer.log"
    assert (render_dir / "render.json").is_file()
    assert Path(record["record_path"]) == root / "scores" / "pairs" / VP_ID / f"{name}.json"
    assert Path(record["scene"]["path"]) == root / "scores" / "pairs" / VP_ID / name / "scene.json"
    assert record["outputs"]["render_dir"] == str(render_dir)
    assert not (root / "scores" / "pairs" / VP_ID / name / "render.png").exists()
    command = renderer.calls[0]
    assert command[command.index("--out") + 1] == str(render_dir / "render.png")
    assert record["scene"]["document"] == json.loads(Path(record["scene"]["path"]).read_text())


def test_every_stage_is_timed(world) -> None:
    _needs_schema(world)
    record, _ = _run(world)
    timings = record["timings_s"]
    for stage in (
        "leakage_checks",
        "forecast",
        "hash_inputs",
        "scene_build",
        "scene_schema_check",
        "scene_renderer_check",
        "render",
        "clear_day_reference",
        "extract_truth",
        "extract_render",
        "gates",
        "score",
        "registration",
        "total",
    ):
        assert timings[stage] >= 0.0, stage
    assert timings["total"] >= max(v for k, v in timings.items() if k != "total")
    stored = json.loads(Path(record["record_path"]).read_text())
    assert stored["timings_s"] == timings


def test_every_input_names_its_image_time_and_availability(world) -> None:
    _needs_schema(world)
    record, _ = _run(world)
    inputs = record["inputs"]
    for item in (inputs["truth"], inputs["plate"], *inputs["references"]):
        assert item["image_time_utc"] and item["available_utc"] and item["available_rule"]
        assert len(item["sha256"]) == 64
    forecast = inputs["forecast"]
    assert [h["fh"] for h in forecast["hours"]] == [32]
    assert forecast["hours"][0]["availability_utc"] == forecast["available_utc"]
    assert forecast["run"] == "2026111812" and forecast["summary"] is None
    issue = record["issue_time_utc"]
    assert forecast["available_utc"] <= issue
    assert inputs["plate"]["available_utc"] <= issue
    assert all(r["available_utc"] <= issue for r in inputs["references"])


def test_a_tagged_repeat_keeps_the_pair_id_and_scene(world) -> None:
    _needs_schema(world)
    first, _ = _run(world, tag="run1")
    second, _ = _run(world, tag="run2")
    assert first["pair_id"] == second["pair_id"]
    assert first["output_name"] == f"{first['pair_id']}.run1"
    assert second["output_name"] == f"{first['pair_id']}.run2"
    assert first["record_path"] != second["record_path"]
    assert first["scene"]["sha256"] == second["scene"]["sha256"]
    assert first["render"]["png"]["sha256"] == second["render"]["png"]["sha256"]
    assert Path(first["record_path"]).is_file() and Path(second["record_path"]).is_file()
    assert "run1" not in json.dumps(first["scene"]["document"])
    with pytest.raises(ValueError, match="tag must be"):
        _inputs(world, tag="Run 1")


def test_named_boxes_are_all_scored(world, tmp_path) -> None:
    _needs_schema(world)
    boxes = _boxes(tmp_path)
    record, _ = _run(world, boxes=boxes, region=boxes.primary_box.region())
    extractor, score = record["extractor"], record["score"]
    assert extractor["primary_box"] == "peak" and set(extractor["boxes"]) == {"peak", "foreground"}
    assert extractor["truth"] == extractor["boxes"]["peak"]["truth"]
    assert extractor["boxes"]["foreground"]["region"]["points"] == [[0.05, 0.8], [0.95, 0.95]]
    # truth: the blurred peak (hidden); the stand-in render is a clear frame (visible)
    assert score["primary_box"] == "peak" and score["cell"] == "false_alarm"
    assert score["observed_class"] == "hidden" and score["forecast_class"] == "visible"
    assert set(score["boxes"]) == {"peak", "foreground"} and score["label"] == "v-1"
    assert record["inputs"]["boxes"]["primary"] == "peak"
    registration = record["diagnostics"]["registration"]
    assert registration is not None and registration["window"][1] == pytest.approx(50 / 360)
    assert set(record["diagnostics"]["aligned_truth"]) == {"peak", "foreground"}
    # naming boxes changes the pair id (the boxes are part of what the pair depends on)
    plain, _ = _run(world, region=boxes.primary_box.region())
    assert plain["pair_id"] != record["pair_id"] and plain["score"]["primary_box"] == "landmark"


def _patch_scene(width: int = 960, height: int = 640, seed: int = 11):
    """Luma: a faint smooth background with one strongly textured patch at x 300-600, y 250-400.

    Edge energy is concentrated in the patch, so a box that slides off it loses energy.
    """
    rng = np.random.default_rng(seed)
    rows = np.arange(height)[:, None] / height
    image = 0.45 + 0.1 * rows + 0.01 * rng.standard_normal((height, width))
    blocks = np.kron(rng.random((150 // 6 + 1, 300 // 6 + 1)), np.ones((6, 6)))[:150, :300]
    image[250:400, 300:600] = 0.15 + 0.7 * blocks
    return np.clip(image, 0.0, 1.0)


def test_the_moved_box_lands_on_the_same_content(tmp_path) -> None:
    """Review finding: no test checked the direction in which the measured shift moves the
    boxes, on which the record's 'moved box' diagnostic depends. The truth is the plate with
    its content moved 36 px right and 24 px up: the registration says +36/-24, the box moved
    by it covers the same content (ratio 1.00), and moving it the other way does worse than
    not moving it at all."""
    from snow_globe_eval.extractor import ClearDayReference, ExtractorParams, ReferenceFrame
    from snow_globe_eval.registration import estimate_shift

    plate_luma = _patch_scene()
    truth_luma = np.roll(np.roll(plate_luma, -24, axis=0), 36, axis=1)  # up 24, right 36
    plate, truth = tmp_path / "plate.png", tmp_path / "truth.png"
    to_rgb(plate_luma).save(plate)
    to_rgb(truth_luma).save(truth)
    params = ExtractorParams()  # analysis width 960: one analysis pixel per frame pixel
    region = Region.box(300 / 960, 250 / 640, 600 / 960, 400 / 640, frame_size=(1, 1))
    sun = (30.0, 180.0)
    reference = ClearDayReference.build(
        [ReferenceFrame(plate, VALID - timedelta(days=30))],
        region=region,
        sun_of=lambda moment: sun,
        params=params,
    )
    shift = estimate_shift(
        plate, truth, window=(0.2, 0.2, 0.8, 0.8), analysis_width=params.analysis_width
    ).to_dict()
    assert (shift["dx_px"], shift["dy_px"]) == (36.0, -24.0)
    assert shift["dx_norm"] == pytest.approx(36 / 960, abs=1e-4)
    assert shift["dy_norm"] == pytest.approx(-24 / 640, abs=1e-4)

    def ratio(move: dict) -> float:
        aligned = backtest_pair._aligned_truth(
            truth, region, reference, sun, move, (960, 640), params
        )
        assert aligned["reason"] is None
        return aligned["ratio"]

    moved = ratio(shift)
    unmoved = ratio({**shift, "dx_norm": 0.0, "dy_norm": 0.0})
    opposite = ratio({**shift, "dx_norm": -shift["dx_norm"], "dy_norm": -shift["dy_norm"]})
    assert moved == pytest.approx(1.0, abs=0.02)
    assert abs(opposite - 1.0) > abs(unmoved - 1.0) > abs(moved - 1.0)
    aligned_box = backtest_pair._aligned_truth(
        truth, region, reference, sun, shift, (960, 640), params
    )["region"]["points"]
    assert aligned_box[0] == pytest.approx([336 / 960, 226 / 640], abs=1e-3)


def test_the_region_must_be_the_primary_box(world, tmp_path) -> None:
    boxes = _boxes(tmp_path)
    with pytest.raises(ValueError, match="primary landmark box"):
        _inputs(world, boxes=boxes)  # the base region is in pixels, not the box's
    with pytest.raises(ValueError, match="the boxes are for"):
        _inputs(world, boxes=_boxes(tmp_path, "point-reyes.beach"))


# --- one view, one aspect ratio (D-024's 3:2 and 16:9 epochs) -------------------------------


def test_a_truth_frame_of_another_aspect_ratio_is_not_scored_silently(world, tmp_path) -> None:
    """Review finding (scratch test 4): a 640x427 truth with the 640x360 plate and references
    and normalised boxes was scored (cell 'hit'); only the registration note said the aspect
    ratios differed. It is refused before anything is written."""
    world["truth"].unlink()
    world["truth"].with_suffix(".json").unlink()
    tall = _frame(
        world["root"], "frames", VALID, "8a1c9d2b", to_rgb(blurred(landscape(9, 640, 427), 4))
    )
    boxes = _boxes(tmp_path)
    with pytest.raises(PairError, match=r"truth frame is 640x427, but the plate .* is 640x360"):
        _run(world, truth_path=tall, boxes=boxes, region=boxes.primary_box.region())
    assert not (world["root"] / "scores").exists()


def test_every_frame_and_box_must_share_the_truth_s_aspect_ratio(world, tmp_path) -> None:
    boxes = _boxes(tmp_path)
    named = {"boxes": boxes, "region": boxes.primary_box.region(), "scene_only": True}
    square = _frame(
        world["root"], "frames", VALID - timedelta(days=5), "5" * 12, to_rgb(landscape(5, 480, 480))
    )
    with pytest.raises(PairError, match=r"the reference frame .*555555555555\.jpg is 480x480"):
        _run(world, reference_paths=(*world["refs"], square), **named)
    with pytest.raises(PairError, match=r"the plate .*555555555555\.jpg is 480x480"):
        _run(world, plate_path=square, **named)
    drawn = replace(boxes, drawn_on={"size": [999, 666]})  # boxes drawn on a 3:2 frame
    with pytest.raises(PairError, match="boxes were drawn on a 999x666 frame"):
        _run(world, **{**named, "boxes": drawn})
    garbled = replace(boxes, drawn_on={"size": "999x666"})  # a hand-written file's typo
    with pytest.raises(PairError, match=r"drawn_on\.size '999x666' is not a frame size"):
        _run(world, **{**named, "boxes": garbled})
    with pytest.raises(PairError, match="region landmark is in the pixels of a 640x480 frame"):
        _run(world, region=Region.box(*PEAK_BOX, frame_size=(640, 480)), scene_only=True)
    assert not (world["root"] / "scores").exists()
    # Another resolution of the same aspect ratio is the same view: 1280x720 against 640x360,
    # and boxes drawn on a 1920x1080 frame.
    wide = _frame(
        world["root"],
        "frames",
        VALID - timedelta(days=6),
        "6" * 12,
        to_rgb(landscape(6, 1280, 720)),
    )
    hd = replace(boxes, drawn_on={"size": [1920, 1080]})
    record, _ = _run(world, reference_paths=(*world["refs"], wide), **{**named, "boxes": hd})
    sizes = [r["size"] for r in record["inputs"]["references"]]
    assert sizes == [[640, 360]] * 3 + [[1280, 720]] and record["inputs"]["plate"]["size"]


def _summary_for(world, lead_hours: float = 24.0) -> dict:
    """A pair_forecast summary that describes the base world's pair (valid 20:00Z, f32)."""
    forecast = ForecastRun.load(world["forecast"])
    available, _ = forecast.availability(VALID)
    issue = isoformat_z(VALID - timedelta(hours=lead_hours))
    return {
        "kind": "snow-globe.pair-forecast",
        "viewpoint_id": world["vp"],
        "valid_time_utc": isoformat_z(VALID),
        "issue_time_utc": issue,
        "lead_nominal_h": lead_hours,
        "choice": {
            "issue_time_utc": issue,
            "valid_time_utc": isoformat_z(VALID),
            "run": "2026111812",
            "fh_lo": 32,
            "fh_hi": 32,
            "availability_utc": isoformat_z(available),
            "availability_model": {"method": "conservative-fallback"},
        },
        "hours": [32],
        "file": {"sha256": forecast.sha256},
        "plan": {"total_bytes": 123},
        "result": {"transfer": {"bytes": {"grib": 123}}, "seconds_total": 1.5},
        "code": {"snow_globe_forecast_file": "/snapshot/snow_globe_forecast/__init__.py"},
        "timings_s": {"choose": 0.01},
    }


def test_the_forecast_summary_must_name_this_forecast_file(world, tmp_path) -> None:
    _needs_schema(world)
    summary = tmp_path / "summary.json"
    good = _summary_for(world)
    summary.write_text(json.dumps(good))
    record, _ = _run(world, forecast_summary=summary)
    embedded = record["inputs"]["forecast"]["summary"]
    assert embedded["choice"]["run"] == "2026111812" and embedded["planned_grib_bytes"] == 123
    assert len(embedded["sha256"]) == 64
    summary.write_text(json.dumps({**good, "file": {"sha256": "0" * 64}}))
    with pytest.raises(PairError, match="names run 2026111812 with file sha256 0000"):
        _run(world, forecast_summary=summary)


def test_a_summary_written_before_the_rename_is_read(world, tmp_path) -> None:
    """The rename (D-080): pair_forecast wrote ``kind: lookout.pair-forecast`` and ``lookout_*``
    code keys until it was renamed (the 19 Nov summary on disk has both); it reads as before."""
    summary = tmp_path / "summary.json"
    old = {
        **_summary_for(world),
        "kind": "lookout.pair-forecast",
        "code": {
            "lookout_forecast_file": "/snapshot/lookout_forecast/__init__.py",
            "lookout_forecast_version": "0.1.0",
            "lookout_common_file": "/snapshot/lookout_common/__init__.py",
        },
    }
    summary.write_text(json.dumps(old))
    record, _ = _run(world, forecast_summary=summary, scene_only=True)
    assert record["status"] == "scene-only"
    assert record["inputs"]["forecast"]["summary"]["code"] == old["code"]
    summary.write_text(json.dumps({**old, "kind": "lookout.pair-forecast-v2"}))
    with pytest.raises(PairError, match=r"is not a snow-globe\.pair-forecast summary"):
        _run(world, forecast_summary=summary, scene_only=True)


def test_a_summary_for_another_issue_time_is_refused(world, tmp_path) -> None:
    """Review finding (scratch test 3): only the file hash and run were compared, so this
    pair's summary was accepted for a 12 h lead from the same file, and the record claimed
    snow-globe-forecast chose that run for an issue time it never considered."""
    summary = tmp_path / "summary.json"
    good = _summary_for(world)
    summary.write_text(json.dumps(good))
    with pytest.raises(
        PairError, match=r"not for this pair: issue_time_utc '2026-11-18T20:00:00Z'"
    ):
        _run(world, lead_hours=12.0, forecast_summary=summary, scene_only=True)
    choice = good["choice"]
    for broken, expected in (
        ({**good, "viewpoint_id": "point-reyes.beach"}, "viewpoint_id 'point-reyes.beach'"),
        ({**good, "valid_time_utc": "2026-11-19T21:00:00Z"}, "valid_time_utc"),
        (
            {**good, "choice": {**choice, "issue_time_utc": "2026-11-19T08:00:00Z"}},
            "choice.issue_time_utc '2026-11-19T08:00:00Z'",
        ),
        ({**good, "choice": {**choice, "valid_time_utc": None}}, "choice.valid_time_utc None"),
        ({**good, "hours": [32, 33]}, r"hours \[32, 33\], not \[32\]"),
        ({**good, "choice": {**choice, "fh_hi": 33}}, "choice hours f32/f33, not f32/f32"),
        (
            {**good, "choice": {**choice, "availability_utc": "2026-11-18T14:00:00Z"}},
            "choice.availability_utc '2026-11-18T14:00:00Z', not 2026-11-18T13:51:4",
        ),
    ):
        summary.write_text(json.dumps(broken))
        with pytest.raises(PairError, match=expected):
            _run(world, forecast_summary=summary, scene_only=True)
    assert not (world["root"] / "scores").exists()  # nothing was written
    # The same instants written another way are the same summary.
    summary.write_text(json.dumps({**good, "valid_time_utc": "2026-11-19T12:00:00-08:00"}))
    record, _ = _run(world, forecast_summary=summary, scene_only=True)
    assert record["status"] == "scene-only"


def test_records_written_before_the_rename_are_pair_records(world) -> None:
    """The rename (D-080): the M0.5 pair's records of 19 Nov say ``kind: lookout.backtest-pair``.
    The fixture is the header of the recorded run1 record (scores/pairs/grand-canyon.yavapai/
    20240115T194500Z_L24h_7838502574.run1.json), trimmed to the fields without local paths."""
    from snow_globe_eval.backtest_pair import RECORD_KIND, RECORD_KINDS, is_pair_record

    fixture = Path(__file__).parent / "fixtures" / "backtest_pair_lookout_run1_header.json"
    recorded = json.loads(fixture.read_text())
    assert recorded["kind"] == "lookout.backtest-pair" and recorded["versions"]["lookout_eval"]
    assert is_pair_record(recorded)
    record, _ = _run(world, scene_only=True)
    assert record["kind"] == RECORD_KIND and is_pair_record(record)
    assert RECORD_KINDS == ("snow-globe.backtest-pair", "lookout.backtest-pair")
    for broken in (
        {**recorded, "kind": "lookout.pair-forecast"},
        {**recorded, "schema_version": 2},
        [recorded],
        None,
    ):
        assert not is_pair_record(broken)
