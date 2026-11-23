"""The dev P1 table's forecast choice (PRD §13.1, §8.3; D-008): never a run available after issue.

Synthetic hindcast files in the live field profile (tests/synth_p1dev.py), written to a
temporary folder; no network, no real forecast file.
"""

from __future__ import annotations

import json
import random
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import REPO_RENDERER, FixedEphemeris
from snow_globe_common.manifest import load_manifest
from synth_forecast import clear_sky, low_deck
from synth_p1dev import live_doc, write_run

from snow_globe_eval.ephemeris import moon_position, sun_position
from snow_globe_eval.forecast_io import ForecastFileError, ForecastRun
from snow_globe_eval.heldout import LeakageError
from snow_globe_eval.p1dev_forecast import (
    SCENE_OPTIONAL_FIELDS,
    ForecastChoice,
    HindcastCatalogue,
    hindcast_dir,
    other_rule,
    publication_floor,
    scene_run,
)
from snow_globe_eval.scene import PlateIdentity, build_scene, renderer_version, validate_scene

VP = "rainier.paradise-mountain"


@pytest.fixture(scope="module")
def vp():
    return load_manifest(VP)


def _lag(run_start: datetime, minutes_per_hour: float = 2.0, base: float = 50.0):
    return lambda fh: run_start + timedelta(minutes=base + minutes_per_hour * fh)


def _catalogue(tmp_path: Path, vp, runs, hours=range(0, 49), **kw) -> HindcastCatalogue:
    folder = hindcast_dir(tmp_path, VP)
    for run in runs:
        write_run(folder, live_doc(vp, run, list(hours), clear_sky, availability=_lag(run, **kw)))
    return HindcastCatalogue.scan(folder, viewpoint_id=VP, roots=(tmp_path,))


def _t(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _hour_time(run_start: datetime, hour: dict, rule: str) -> datetime:
    """One hour's availability, written out again from the files (no shared code)."""
    stamps = [_t(hour["grib_last_modified_utc"]), _t(hour["idx_last_modified_utc"])]
    if rule == "s3-stamps":
        return _t(hour["availability_utc"])
    model = hour["availability_model"]
    modelled = run_start + timedelta(seconds=model["lag_s"])
    return max(modelled, _t(model["s3_floor_utc"]), *stamps)  # synthetic stamps are < 24 h


def _expected(
    catalogue: HindcastCatalogue, issue: datetime, valid: datetime, rule: str = "d054"
) -> tuple[str, datetime] | None:
    """The same rule, brute force over every file: the newest run available by the issue."""
    best = None
    for item in catalogue.files:
        offset = (valid - item.run_start).total_seconds() / 3600.0
        if offset < 0:
            continue
        lo, hi = int(offset // 1), int(-(-offset // 1))
        hours = {int(h["fh"]): h for h in item.run.doc["hours"]}
        if lo not in hours or hi not in hours:
            continue
        available = max(_hour_time(item.run_start, hours[fh], rule) for fh in {lo, hi})
        if available <= issue and (best is None or item.run_start > best[0].run_start):
            best = (item, available)
    return None if best is None else (best[0].run_id, best[1])


def test_the_newest_run_available_by_the_issue_time_is_chosen(tmp_path: Path, vp) -> None:
    runs = [datetime(2026, 11, 18, 0, tzinfo=UTC) + timedelta(hours=6 * k) for k in range(13)]
    catalogue = _catalogue(tmp_path, vp, runs)
    valid = datetime(2026, 11, 20, 18, 21, 1, tzinfo=UTC)
    issue = valid - timedelta(hours=24)  # 19 Nov 18:21
    choice = catalogue.choose(issue, valid)
    # 19 Nov 18Z needs f24-f25, published 19:38-19:40Z: after the issue. 12Z's f30-f31 at
    # 13:50-13:52Z is the newest before it.
    assert choice.file is not None and choice.file.run_id == "2026111912"
    assert (choice.fh_lo, choice.fh_hi) == (30, 31)
    assert choice.available == datetime(2026, 11, 19, 13, 52, tzinfo=UTC)
    assert choice.passed_over[0] == ("2026112100", "starts after the valid time")
    newer = ["2026112018", "2026112012", "2026112006", "2026112000", "2026111918"]
    assert [run for run, _ in choice.passed_over[1:]] == newer
    assert all("after the issue time" in why for _, why in choice.passed_over[1:])
    record = choice.to_record()
    assert record["lead_from_availability_h"] == pytest.approx(28.4836, abs=1e-3)
    assert record["available_before_issue_by_h"] > 0
    assert [h["fh"] for h in record["hours"]] == [30, 31]


def test_an_issue_time_equal_to_the_availability_may_use_the_run(tmp_path: Path, vp) -> None:
    run = datetime(2026, 11, 19, 12, tzinfo=UTC)
    catalogue = _catalogue(tmp_path, vp, [run])
    valid = run + timedelta(hours=30)
    available = run + timedelta(minutes=50 + 2 * 30)
    assert catalogue.choose(available, valid).file is not None
    late = catalogue.choose(available - timedelta(seconds=1), valid)
    assert late.file is None and "after the issue time" in late.passed_over[0][1]


@pytest.mark.parametrize("rule", ["d054", "s3-stamps"])
def test_no_forecast_available_after_the_issue_time_is_ever_used(
    tmp_path: Path, vp, rule: str
) -> None:
    """The leakage property, over random issue times and leads, against a brute-force rule.

    Each run's recorded lag model is random against its stamps: equal, 20 min earlier (the
    floor wins under d054), or 10 or 70 min later (the model wins), as in the real files.
    """
    runs = [datetime(2026, 11, 18, 0, tzinfo=UTC) + timedelta(hours=k) for k in range(0, 72)]
    folder = hindcast_dir(tmp_path, VP)
    rng = random.Random(20261123)
    shifts: dict[str, float | None] = {}
    for run in runs:
        hours = list(range(0, 49 if run.hour % 6 == 0 else 19))
        # lags from 45 to 150 min, some hours later than their neighbours
        base, per_hour = rng.uniform(45.0, 150.0), rng.uniform(0, 3)
        shift = rng.choice([None, -20.0, 10.0, 70.0])
        shifts[f"{run:%Y%m%d%H}"] = shift
        model = (
            None
            if shift is None
            else (lambda fh, b=base, m=per_hour, s=shift: 60.0 * (b + s + m * fh))
        )
        write_run(
            folder,
            live_doc(
                vp, run, hours, clear_sky, availability=_lag(run, per_hour, base), model_lag_s=model
            ),
        )
    catalogue = HindcastCatalogue.scan(folder, viewpoint_id=VP, roots=(tmp_path,), rule=rule)
    chosen = differs = 0
    for _ in range(3000):
        valid = datetime(2026, 11, 18, tzinfo=UTC) + timedelta(seconds=rng.uniform(0, 4 * 86400))
        lead = timedelta(hours=rng.choice([1, 3, 6, 12, 24, 36, 48]) + rng.uniform(0, 0.5))
        issue = valid - lead
        choice = catalogue.choose(issue, valid)
        expected = _expected(catalogue, issue, valid, rule)
        assert choice.rule == rule
        assert (None if choice.file is None else choice.file.run_id) == (
            None if expected is None else expected[0]
        )
        if choice.file is not None:
            assert expected is not None
            chosen += 1
            assert choice.available is not None and choice.available <= issue
            assert choice.available == expected[1]
        other = catalogue.choose(issue, valid, rule="s3-stamps" if rule == "d054" else "d054")
        differs += other.run_id != choice.run_id
    assert chosen > 1500  # the property is exercised, not vacuous
    assert differs > 50  # and the two rules really differ on these files
    assert {s for s in shifts.values()} == {None, -20.0, 10.0, 70.0}


def test_d054_dates_an_hour_by_the_later_of_its_lag_model_and_its_stamps(
    tmp_path: Path, vp
) -> None:
    """The real files' case: the recorded model (7,200 s) is later than the S3 stamps."""
    run = datetime(2026, 11, 19, 18, tzinfo=UTC)
    older = datetime(2026, 11, 19, 12, tzinfo=UTC)
    folder = hindcast_dir(tmp_path, VP)
    write_run(
        folder,
        live_doc(
            vp,
            run,
            [25, 26],
            low_deck,
            availability=lambda fh: run + timedelta(minutes=82 + fh),
            model_lag_s=lambda fh: 7200.0,
        ),
    )
    write_run(folder, live_doc(vp, older, [31, 32], clear_sky, model_lag_s=lambda fh: 7200.0))
    d054 = HindcastCatalogue.scan(folder, viewpoint_id=VP, roots=(tmp_path,))
    stamps = HindcastCatalogue.scan(folder, viewpoint_id=VP, roots=(tmp_path,), rule="s3-stamps")
    # 18Z's f25-f26 are stamped 19:47-19:48Z and modelled at 20:00Z; issue between the two
    issue = run + timedelta(minutes=110)  # 19:50Z
    valid = issue + timedelta(hours=24)
    by_stamps = stamps.choose(issue, valid)
    by_model = d054.choose(issue, valid)
    assert by_stamps.run_id == "2026111918"
    assert by_stamps.available == run + timedelta(minutes=82 + 26)  # 19:48Z, f26's stamp
    assert by_model.run_id == "2026111912"  # 18Z is modelled at 20:00Z, after the issue
    assert by_model.available == older + timedelta(seconds=7200)
    assert by_model.availability_source.startswith("d054-lag-model")
    assert any(run_id == "2026111918" and "(d054)" in why for run_id, why in by_model.passed_over)
    record = by_model.to_record()
    assert record["availability_rule"] == "d054"
    hour = record["hours"][0]
    assert hour["available_under_d054"]["utc"] == "2026-11-19T14:00:00Z"
    assert hour["available_under_s3_stamps"]["source"].startswith("s3-stamps")
    assert d054.choose(issue, valid, rule="s3-stamps").run_id == "2026111918"


def test_the_stamps_floor_wins_when_the_model_is_earlier(tmp_path: Path, vp) -> None:
    run = datetime(2026, 11, 19, 12, tzinfo=UTC)
    folder = hindcast_dir(tmp_path, VP)
    write_run(folder, live_doc(vp, run, [30, 31], clear_sky, model_lag_s=lambda fh: 1800.0))
    catalogue = HindcastCatalogue.scan(folder, viewpoint_id=VP, roots=(tmp_path,))
    valid = run + timedelta(hours=30, minutes=21)
    stamp = run + timedelta(minutes=50 + 2 * 31)  # f31, the later of the two
    assert catalogue.choose(stamp - timedelta(seconds=1), valid).file is None
    choice = catalogue.choose(stamp, valid)
    assert choice.available == stamp and choice.availability_source.startswith("d054-s3-floor")


def test_an_archived_hour_without_a_recorded_model_is_unusable_under_d054(
    tmp_path: Path, vp
) -> None:
    run = datetime(2026, 11, 19, 12, tzinfo=UTC)
    folder = hindcast_dir(tmp_path, VP)
    write_run(folder, live_doc(vp, run, [30, 31], clear_sky, record_model=False))
    catalogue = HindcastCatalogue.scan(folder, viewpoint_id=VP, roots=(tmp_path,))
    assert any("unusable under d054" in p and "lag model" in p for p in catalogue.problems)
    valid = run + timedelta(hours=30, minutes=21)
    issue = valid - timedelta(hours=24)
    assert catalogue.choose(issue, valid).file is None
    assert "unusable under d054" in catalogue.choose(issue, valid).passed_over[0][1]
    assert catalogue.choose(issue, valid, rule="s3-stamps").file is not None


def test_a_live_hour_is_available_at_its_fetch_finish(tmp_path: Path, vp) -> None:
    run = datetime(2026, 11, 19, 12, tzinfo=UTC)
    doc = live_doc(vp, run, [30, 31], clear_sky, source="live-s3-lastmodified")
    doc["live"] = True
    doc["fetch_finished_utc"] = "2026-11-19T15:00:00Z"  # after both stamps (13:50-13:52Z)
    folder = hindcast_dir(tmp_path, VP)
    write_run(folder, doc)
    catalogue = HindcastCatalogue.scan(folder, viewpoint_id=VP, roots=(tmp_path,))
    valid = run + timedelta(hours=30, minutes=21)
    choice = catalogue.choose(datetime(2026, 11, 19, 15, tzinfo=UTC), valid)
    assert choice.available == datetime(2026, 11, 19, 15, tzinfo=UTC)
    assert "fetch-finished" in str(choice.availability_source)
    assert catalogue.choose(datetime(2026, 11, 19, 14, 59, tzinfo=UTC), valid).file is None


def test_the_publication_floor_follows_d054() -> None:
    start = datetime(2026, 11, 19, 12, tzinfo=UTC)
    grib, idx = start + timedelta(minutes=80), start + timedelta(minutes=80, seconds=4)
    assert publication_floor(start, grib, idx) == idx
    assert publication_floor(start, None, idx) == idx
    assert publication_floor(start, grib, None) == grib + timedelta(seconds=300)
    rewritten = start + timedelta(days=90)  # a rewritten object: never trusted
    assert publication_floor(start, grib, rewritten) == grib + timedelta(seconds=300)
    assert publication_floor(start, rewritten, rewritten) is None
    assert other_rule("d054") == "s3-stamps" and other_rule("s3-stamps") == "d054"
    with pytest.raises(ValueError):
        other_rule("model")
    with pytest.raises(ValueError):
        HindcastCatalogue(Path("x"), VP, (), rule="model")


def test_a_run_without_the_bracketing_hours_is_passed_over(tmp_path: Path, vp) -> None:
    run = datetime(2026, 11, 19, 12, tzinfo=UTC)
    older = datetime(2026, 11, 19, 6, tzinfo=UTC)
    folder = hindcast_dir(tmp_path, VP)
    write_run(folder, live_doc(vp, run, [30], clear_sky))  # f31 missing
    write_run(folder, live_doc(vp, older, [36, 37], low_deck))
    catalogue = HindcastCatalogue.scan(folder, viewpoint_id=VP, roots=(tmp_path,))
    valid = run + timedelta(hours=30, minutes=21)
    choice = catalogue.choose(valid - timedelta(hours=24), valid)
    assert choice.file is not None and choice.file.run_id == "2026111906"
    assert "no forecast hour 31" in choice.passed_over[0][1]


def test_an_hour_dated_before_its_own_s3_stamp_is_unusable(tmp_path: Path, vp) -> None:
    run = datetime(2026, 11, 19, 12, tzinfo=UTC)
    doc = live_doc(vp, run, [30, 31, 32], clear_sky)
    doc["hours"][1]["grib_last_modified_utc"] = "2026-11-20T23:00:00Z"  # f31: later than its
    folder = hindcast_dir(tmp_path, VP)
    write_run(folder, doc)
    catalogue = HindcastCatalogue.scan(folder, viewpoint_id=VP, roots=(tmp_path,))
    assert any("f31 unusable" in p and "D-054" in p for p in catalogue.problems)
    needs_31 = run + timedelta(hours=30, minutes=30)
    assert catalogue.choose(needs_31 - timedelta(hours=24), needs_31).file is None
    exact_30 = run + timedelta(hours=30)  # needs f30 only
    assert catalogue.choose(exact_30 - timedelta(hours=24), exact_30).file is not None


def test_bad_files_are_listed_and_duplicates_refused(tmp_path: Path, vp) -> None:
    folder = hindcast_dir(tmp_path, VP)
    run = datetime(2026, 11, 19, 12, tzinfo=UTC)
    write_run(folder, live_doc(vp, run, [30, 31], clear_sky))
    (folder / "broken.json").write_text("{not json")
    other = live_doc(load_manifest("point-reyes.beach"), run, [30, 31], clear_sky)
    (folder / "other.json").write_text(json.dumps(other))
    catalogue = HindcastCatalogue.scan(folder, viewpoint_id=VP, roots=(tmp_path,))
    assert [f.run_id for f in catalogue.files] == ["2026111912"]
    assert any(p.startswith("broken.json: not read") for p in catalogue.problems)
    assert any("for point-reyes.beach" in p for p in catalogue.problems)
    shutil.copy(folder / "2026111912z.json", folder / "copy.json")
    with pytest.raises(ValueError, match="two hindcast files hold run 2026111912"):
        HindcastCatalogue.scan(folder, viewpoint_id=VP, roots=(tmp_path,))


def test_an_absent_folder_is_an_empty_catalogue(tmp_path: Path) -> None:
    catalogue = HindcastCatalogue.scan(tmp_path / "nope", viewpoint_id=VP)
    assert catalogue.files == () and "no hindcast folder" in catalogue.problems[0]
    now = datetime(2026, 11, 20, 18, tzinfo=UTC)
    assert catalogue.choose(now - timedelta(hours=24), now).file is None


def test_the_quarantine_is_refused(tmp_path: Path, vp) -> None:
    quarantined = tmp_path / "quarantine" / "hrrr"
    write_run(quarantined, live_doc(vp, datetime(2026, 11, 19, 12, tzinfo=UTC), [30], clear_sky))
    with pytest.raises(LeakageError):
        HindcastCatalogue.scan(quarantined, viewpoint_id=VP, roots=(tmp_path,))
    folder = hindcast_dir(tmp_path, VP)
    folder.mkdir(parents=True)
    (folder / "linked.json").symlink_to(next(quarantined.glob("*.json")))
    with pytest.raises(LeakageError):
        HindcastCatalogue.scan(folder, viewpoint_id=VP, roots=(tmp_path,))


def test_a_choice_cannot_hold_a_run_available_after_issue(tmp_path: Path, vp) -> None:
    catalogue = _catalogue(tmp_path, vp, [datetime(2026, 11, 19, 12, tzinfo=UTC)])
    item = catalogue.files[0]
    issue = datetime(2026, 11, 19, 13, tzinfo=UTC)
    with pytest.raises(LeakageError, match="after the issue time"):
        ForecastChoice(issue, issue + timedelta(hours=24), item, 25, 25, 0.0, issue + timedelta(1))


@pytest.fixture
def renderer_dir(tmp_path: Path) -> Path:
    if not (REPO_RENDERER / "scene.schema.json").is_file():
        pytest.skip("renderer/scene.schema.json not present")
    folder = tmp_path / "renderer"
    (folder / "src").mkdir(parents=True)
    shutil.copy(REPO_RENDERER / "scene.schema.json", folder / "scene.schema.json")
    shutil.copy(REPO_RENDERER / "src" / "version.ts", folder / "src" / "version.ts")
    return folder


def test_a_scene_builds_from_the_live_field_profile(renderer_dir: Path, vp) -> None:
    run_start = datetime(2026, 11, 19, 12, tzinfo=UTC)
    run = ForecastRun(live_doc(vp, run_start, [30, 31], low_deck))
    valid = run_start + timedelta(hours=30, minutes=21)
    eph = FixedEphemeris(20.0, 160.0)
    cam = vp.camera
    common = {
        "viewpoint": vp,
        "valid_time": valid,
        "sun": sun_position(valid, cam.lat, cam.lon, cam.elevation_m, ephemeris=eph),
        "moon": moon_position(valid, cam.lat, cam.lon, cam.elevation_m, ephemeris=eph),
        "width": 1920,
        "height": 1080,
        "renderer_version": renderer_version(renderer_dir),
        "plate": PlateIdentity("rainier.paradise-mountain/plate-v0.1", "a" * 64),
    }
    with pytest.raises(ForecastFileError, match="is not in this file"):
        build_scene(run=run, **common)  # why scene_run exists: PRATE, cloud top, winds
    adapted, absent = scene_run(run)
    assert set(absent) == set(SCENE_OPTIONAL_FIELDS)
    assert adapted.sha256 == run.sha256 and adapted.path == run.path
    scene = build_scene(run=adapted, **common)
    validate_scene(scene, renderer_dir)
    flags = scene["meta"]["flags"]
    assert "10 m wind unavailable; 0 used" in flags
    assert "precipitation rate unavailable; 0 used" in flags
    low = next(layer for layer in scene["forecast"]["layers"] if layer["name"] == "low")
    assert low["base_m_msl"] == pytest.approx(2400.0)  # the cloud base, kept
    assert low["base_m_msl"] + low["thickness_m"] == pytest.approx(3686.0)  # band top, no cap
    assert scene["forecast"]["ceiling_m_msl"] == pytest.approx(2400.0)
    # the original values are untouched, and a complete file is returned as it is
    assert ForecastRun(live_doc(vp, run_start, [30, 31], low_deck)).doc == run.doc
    full, nothing = scene_run(adapted)
    assert full is adapted and nothing == ()
