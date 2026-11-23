"""The hindcast mode end to end on the fake bucket: plan, fetch, cap, resume, compare (PRD §13.4).

The live lag model is fitted on recorded live lag events (``fixtures/hindcast/
live_lag_events.jsonl``, lines of the live log of 20 Nov 2026), put in the temp data dir's
``forecast_log/`` as the live worker writes them.
"""

from __future__ import annotations

import copy
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from snow_globe_common.jsonl import read_jsonl
from snow_globe_common.storage import LocalStorage

from snow_globe_forecast.availability import LagModel
from snow_globe_forecast.discovery import publication_floor
from snow_globe_forecast.extract import SKIP_WOULD_NARROW, ExtractOptions, RunResult
from snow_globe_forecast.hindcast.cachebucket import CachingBucket
from snow_globe_forecast.hindcast.compare import compare_availability
from snow_globe_forecast.hindcast.extractor import (
    HindcastExtractor,
    HindcastRunTooNewError,
    live_lag_model,
)
from snow_globe_forecast.hindcast.fetch import PROGRESS_KEY, covered_fn, fetch_plan
from snow_globe_forecast.hindcast.frames import frames_from_times
from snow_globe_forecast.hindcast.plan import (
    LIVE_FIELD_NAMES,
    HindcastStampError,
    build_plan,
    hindcast_availability,
)
from snow_globe_forecast.hindcast.storage import HindcastStorage, ReadOnlyStorage
from snow_globe_forecast.hrrr import format_run_id, object_key
from snow_globe_forecast.output import validate_document

from .conftest import FIXTURES, RAINIER
from .fakebucket import FakeBucket

R12 = datetime(2026, 11, 20, 12, tzinfo=UTC)
R18 = datetime(2026, 11, 20, 18, tzinfo=UTC)
NOW = datetime(2026, 11, 23, 12, 20, tzinfo=UTC)
#: Two daytime frames, a night frame and a frame no run can serve (before any run is listed).
FRAMES = [
    datetime(2026, 11, 21, 18, 21, tzinfo=UTC),  # issue 20 Nov 18:21: 18Z is not out, 12Z is
    datetime(2026, 11, 21, 20, 5, tzinfo=UTC),  # issue 20 Nov 20:05: 18Z f26-f27 are out
    datetime(2026, 11, 21, 9, 0, tzinfo=UTC),  # night: not paired
    datetime(2026, 11, 19, 20, 0, tzinfo=UTC),  # its runs are not in the fake bucket
]


def _lag(fh: int) -> float:
    return 3000.0 + 120.0 * fh  # f26: 106 min, f30: 110 min


@pytest.fixture
def world(data_root: Path, small_grid, manifests):
    """A fake bucket with two synoptic runs, the recorded live log, and hindcast clients."""
    fake = FakeBucket(grid=small_grid)
    for run in (R12, R18):
        fake.add_run(
            run,
            fields=list(LIVE_FIELD_NAMES),
            hours=range(24, 34),
            lag_s={fh: _lag(fh) for fh in range(49)},
        )
    log_dir = data_root / "forecast_log"
    log_dir.mkdir()
    shutil.copy(FIXTURES / "hindcast" / "live_lag_events.jsonl", log_dir / "2026-11-20.jsonl")
    hs = HindcastStorage.at(data_root)
    live = ReadOnlyStorage(LocalStorage(data_root))
    sleeps: list[float] = []

    def bucket() -> CachingBucket:
        return CachingBucket(
            hs.root, transport=fake.transport(), sleep=sleeps.append, clock=lambda: NOW
        )

    def extractor(client: CachingBucket) -> HindcastExtractor:
        return HindcastExtractor(
            hs,
            client,
            {RAINIER: manifests[RAINIER]},
            live_model=live_lag_model(live),
            live_storage=live,
            grid=small_grid,
            clock=lambda: NOW,
        )

    frames = frames_from_times(manifests[RAINIER], FRAMES)
    return {
        "fake": fake,
        "hs": hs,
        "live": live,
        "bucket": bucket,
        "extractor": extractor,
        "frames": frames,
        "sleeps": sleeps,
        "root": data_root,
    }


def _idx_bytes(fake: FakeBucket, run: datetime, hours: list[int]) -> int:
    """The bytes the live fields take in the fake files, read from their .idx (terrain once)."""
    total = 0
    for k, fh in enumerate(hours):
        f = fake.files[object_key(run, fh)]
        rows = [line.split(":") for line in f.idx.strip().splitlines()]
        offsets = [int(r[1]) for r in rows] + [len(f.grib)]
        for n, row in enumerate(rows):
            name = f"{row[3]}:{row[4]}"
            if name in LIVE_FIELD_NAMES and (name != "HGT:surface" or k == 0):
                total += offsets[n + 1] - offsets[n]
    return total


def _plan(world, **kw):
    with world["bucket"]() as client:
        return build_plan(world["frames"], client, now=NOW, **kw)


def test_hindcast_availability_is_the_later_stamp_never_before_the_floor() -> None:
    run = R12
    grib, idx = run + timedelta(minutes=100), run + timedelta(minutes=100, seconds=4)
    assert hindcast_availability(run, grib, idx) == (idx, idx, ())
    late = run + timedelta(days=40)  # a rewritten .idx: later, so the later stamp wins
    available, floor, ignored = hindcast_availability(run, grib, late)
    assert available == late and floor == grib + timedelta(seconds=300) and ignored == ("idx",)
    assert available >= publication_floor(run, grib, late)[0]
    with pytest.raises(HindcastStampError):
        hindcast_availability(run, late, late)


def test_plan_pairs_daytime_frames_with_the_run_a_24_h_lead_allows(world) -> None:
    plan = _plan(world)
    pairs = {p["valid_utc"][:16]: p for p in plan["pairs"]}
    assert set(pairs) == {"2026-11-21T18:21", "2026-11-21T20:05", "2026-11-19T20:00"}
    first, second = pairs["2026-11-21T18:21"], pairs["2026-11-21T20:05"]
    assert (first["run"], first["fh_lo"], first["fh_hi"]) == ("2026112012", 30, 31)
    assert first["skipped"][0][0] == "2026112018"  # not out until 19:44 (f25 at 108 min)
    assert first["availability_utc"] == "2026-11-20T13:52:04Z"  # f31: 112 min + 4 s
    assert first["lead_h"] == pytest.approx(24 + (18 * 60 + 21 - 13 * 60 - 52 - 4 / 60) / 60)
    assert (second["run"], second["fh_lo"], second["fh_hi"]) == ("2026112018", 26, 27)
    assert pairs["2026-11-19T20:00"]["run"] is None and plan["unpaired"] == 1
    assert plan["frames"]["daytime"] == 3 and plan["frames"]["night"] == 1
    runs = {r["run"]: r for r in plan["runs"]}
    assert {k: v["hours"] for k, v in runs.items()} == {
        "2026112012": [30, 31],
        "2026112018": [26, 27],
    }
    assert runs["2026112012"]["planned_bytes"] == _idx_bytes(world["fake"], R12, [30, 31])
    assert runs["2026112018"]["planned_bytes"] == _idx_bytes(world["fake"], R18, [26, 27])
    assert plan["totals"]["planned_bytes"] == sum(r["planned_bytes"] for r in plan["runs"])
    assert plan["totals"]["within_cap"] and plan["fields"] == list(LIVE_FIELD_NAMES)
    assert not world["fake"].grib_requests()  # a plan downloads no GRIB
    # Listings and inventories are kept: planning again makes no request.
    before = len(world["fake"].requests)
    again = _plan(world)
    assert len(world["fake"].requests) == before and again["runs"] == plan["runs"]
    assert again["transfer"]["total_bytes"] == 0


def test_fetch_writes_hindcast_files_in_the_live_format(world, manifests) -> None:
    plan = _plan(world)
    with world["bucket"]() as client:
        report = fetch_plan(
            plan, storage=world["hs"], bucket=client, extractor=world["extractor"](client)
        )
    assert report["status"] == "done", report
    assert report["runs_written"] == ["2026112012", "2026112018"] and not report["errors"]
    root = world["root"]
    model = LagModel.from_storage(world["live"], methods={"live"})
    for run, hours in ((R12, [30, 31]), (R18, [26, 27])):
        path = root / "forecast/hindcast/hrrr" / RAINIER / f"{run:%Y%m%d%H}z.json"
        doc = json.loads(path.read_text())
        validate_document(doc)
        assert doc["live"] is False and doc["forecast_hours"] == hours
        assert doc["field_names"] == list(LIVE_FIELD_NAMES)
        assert [p["id"] for p in doc["points"] if p["kind"] == "landmark"] == [
            "landmark:mount-rainier-summit-columbia-crest",
            "landmark:gibraltar-rock",
            "landmark:anvil-rock",
            "landmark:little-tahoma-peak",
        ]
        for hour in doc["hours"]:
            fh = hour["fh"]
            stamp = run + timedelta(seconds=_lag(fh) + 4)
            assert hour["availability_utc"] == stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
            assert hour["idx_last_modified_utc"] == hour["availability_utc"]
            assert hour["availability_source"] == "archived-s3-lastmodified"
            assert hour["lag_s"] == _lag(fh) + 4
            note = hour["availability_model"]
            assert note["s3_floor_utc"] == hour["availability_utc"]
            assert note["lag_s"] == pytest.approx(model.lag_s(run, fh)[0])
            assert note["samples_by_method"]["survey"] == 0
        assert any(n.startswith("Hindcast file") for n in doc["notes"])
        assert any("live worker has no file" in n for n in doc["notes"])
    # Only the hindcast tree was written: no live file, log, state or GRIB cache.
    for live in ("forecast/hrrr", "state", "cache"):
        assert not (root / live).exists(), live
    assert sorted(p.name for p in (root / "forecast_log").iterdir()) == ["2026-11-20.jsonl"]
    grib_left = list((root / "forecast/hindcast/cache/grib").rglob("*.grib2*"))
    assert grib_left == []
    events = read_jsonl(root / "forecast/hindcast/log" / "2026-11-23.jsonl")
    kinds = [e["event"] for e in events]
    assert kinds.count("run") == 2 and kinds.count("hindcast-run") == 2
    assert all(e["live"] is False for e in events if e["event"] == "run")
    assert "lag" not in kinds  # a hindcast never logs lag events
    progress = world["hs"].root.read_json(PROGRESS_KEY)
    assert sorted(progress["runs"]) == ["2026112012", "2026112018"]
    grib = report["transfer"]["bytes"]["grib"]
    assert grib == plan["totals"]["planned_bytes"]
    assert report["transfer"]["requests"].get("idx", 0) == 0  # the plan's inventories, kept

    # Resume: everything is written, so nothing is requested at all.
    before = len(world["fake"].requests)
    with world["bucket"]() as client:
        again = fetch_plan(
            plan, storage=world["hs"], bucket=client, extractor=world["extractor"](client)
        )
    assert again["status"] == "done" and again["runs_written"] == []
    assert again["runs_already_written"] == ["2026112012", "2026112018"]
    assert len(world["fake"].requests) == before and again["transfer"]["total_bytes"] == 0

    # Compare: S3 availability against the live lag model, hour by hour.
    compared = compare_availability(world["hs"], world["live"], RAINIER, now=NOW)
    assert compared["hours"] == 4
    side = compared["live_model_at_write"]
    margins = [r["live_model_at_write_lag_s"] - r["s3_lag_s"] for r in compared["rows"]]
    assert side["early"] == sum(1 for m in margins if m < 0)
    assert compared["runs"][0]["live_worker_stored_run"] is False


def test_fetch_refuses_a_plan_over_the_cap_before_any_grib(world) -> None:
    plan = _plan(world, cap_mb=0.01)
    assert plan["totals"]["within_cap"] is False
    with world["bucket"]() as client:
        report = fetch_plan(
            plan,
            storage=world["hs"],
            bucket=client,
            extractor=world["extractor"](client),
            cap_mb=0.01,
        )
    assert report["status"] == "over-cap" and report["runs_left"] == ["2026112012", "2026112018"]
    assert not world["fake"].grib_requests()


def test_fetch_stops_when_the_next_run_no_longer_fits(world) -> None:
    plan = _plan(world)
    with world["bucket"]() as client:
        spent = client.stored_ledger()["total_bytes"]  # the plan's listings and inventories
    cap_mb = (spent + plan["totals"]["planned_bytes"] + 1) / 1e6  # exactly what the plan needs
    # f30 is fetched, then f31 fails all four attempts: the retry fetches f30 a second time,
    # so the second run no longer fits in what is left.
    world["fake"].fail_next[object_key(R12, 31)] = 4
    with world["bucket"]() as client:
        report = fetch_plan(
            plan,
            storage=world["hs"],
            bucket=client,
            extractor=world["extractor"](client),
            cap_mb=cap_mb,
            sleep=world["sleeps"].append,
        )
    assert report["status"] == "cap-reached", report
    assert report["runs_written"] == ["2026112012"] and report["runs_left"] == ["2026112018"]
    assert len(report["errors"]) == 1
    with world["bucket"]() as client:
        assert client.stored_ledger()["total_bytes"] <= cap_mb * 1e6


def test_fetch_backs_off_and_resumes_after_errors(world) -> None:
    plan = _plan(world)
    path = object_key(R12, 30)
    world["fake"].fail_next[path] = 4  # the bucket's 4 attempts all fail once
    with world["bucket"]() as client:
        report = fetch_plan(
            plan,
            storage=world["hs"],
            bucket=client,
            extractor=world["extractor"](client),
            max_errors=1,
            sleep=world["sleeps"].append,
        )
    assert report["status"] == "errors" and report["runs_left"] == ["2026112012", "2026112018"]
    assert report["errors"][0]["run"] == "2026112012"
    assert "BucketError" in report["errors"][0]["error"]
    assert report["transfer"]["retries"] == 3
    # Run again: the failure has passed, so both runs are written; the error stays on record.
    world["fake"].fail_next[path] = 4
    with world["bucket"]() as client:
        report = fetch_plan(
            plan,
            storage=world["hs"],
            bucket=client,
            extractor=world["extractor"](client),
            max_errors=3,
            backoff_s=60.0,
            sleep=world["sleeps"].append,
        )
    assert report["status"] == "done" and len(report["errors"]) == 1
    assert world["sleeps"][-1] == 60.0
    progress = world["hs"].root.read_json(PROGRESS_KEY)
    assert len(progress["errors"]) == 2 and progress["runs"]["2026112012"]["attempts"] == 2


def test_hindcast_extractor_refuses_new_runs_and_live_storage(world, manifests, small_grid) -> None:
    with world["bucket"]() as client:
        extractor = world["extractor"](client)
        extractor.clock = lambda: R18 + timedelta(hours=23)
        with pytest.raises(HindcastRunTooNewError):
            extractor.extract(R18, ExtractOptions(hours=(26,), viewpoint_ids=(RAINIER,)))
        with pytest.raises(TypeError):
            HindcastExtractor(
                LocalStorage(world["root"]),  # type: ignore[arg-type]
                client,
                {RAINIER: manifests[RAINIER]},
                live_model=LagModel(),
                grid=small_grid,
            )


def _fetch(world, plan, **kw):
    with world["bucket"]() as client:
        return fetch_plan(
            plan, storage=world["hs"], bucket=client, extractor=world["extractor"](client), **kw
        )


def test_a_plan_needing_other_hours_of_a_written_run_widens_its_file(world) -> None:
    plan = _plan(world)
    assert _fetch(world, plan)["status"] == "done"
    # Another plan (another lead, or new frames) needs f32-f33 of the 12Z run, whose file holds
    # f30-f31: fetching only f32-f33 would narrow the file, which the extractor refuses.
    other = copy.deepcopy(plan)
    (run12,) = [r for r in other["runs"] if r["run"] == "2026112012"]
    run12.update(hours=[32, 33], planned_bytes=_idx_bytes(world["fake"], R12, [32, 33]))
    other["runs"] = [run12]
    grib_before = len(world["fake"].grib_requests())
    report = _fetch(world, other)
    assert report["status"] == "done" and report["runs_written"] == ["2026112012"], report
    assert report["runs_not_written"] == [] and report["runs_left"] == []
    assert report["runs_widened"] == {
        "2026112012": {
            "planned_hours": [32, 33],
            "file_hours": [30, 31],
            "fetched_hours": [30, 31, 32, 33],
            "fields_added": [],
        }
    }
    path = world["root"] / "forecast/hindcast/hrrr" / RAINIER / "2026112012z.json"
    doc = json.loads(path.read_text())
    validate_document(doc)
    assert doc["forecast_hours"] == [30, 31, 32, 33]
    assert len(world["fake"].grib_requests()) > grib_before  # f30-f31 again: subsets are deleted
    # The file now serves both plans: fetching either again sends no request.
    before = len(world["fake"].requests)
    for again in (_fetch(world, plan), _fetch(world, other)):
        assert again["status"] == "done" and again["runs_written"] == []
    assert len(world["fake"].requests) == before


class _WritesNothing:
    """An extractor whose every extraction writes nothing (as for a file it would narrow)."""

    def __init__(self) -> None:
        self.hours: list[tuple[int, ...] | None] = []

    def extract(self, run_start, options: ExtractOptions | None = None, *, listing=None):
        self.hours.append(options.hours if options else None)
        return RunResult(
            run_id=format_run_id(run_start),
            live=False,
            run_complete=True,
            hours=[],
            field_names=list(LIVE_FIELD_NAMES),
            viewpoints=[RAINIER],
            written=[],
            planned_bytes=0,
            transfer={},
            bytes_by_field={},
            seconds_total=0.0,
            seconds_download=0.0,
            seconds_decode=0.0,
            s3_lags_s={},
            fetch_started_utc=NOW,
            fetch_finished_utc=NOW,
            skipped={RAINIER: SKIP_WOULD_NARROW},
        )


def test_a_run_whose_extraction_writes_nothing_is_never_reported_as_written(world) -> None:
    plan = _plan(world)
    stub = _WritesNothing()
    with world["bucket"]() as client:
        report = fetch_plan(plan, storage=world["hs"], bucket=client, extractor=stub)  # type: ignore[arg-type]
    assert stub.hours == [(30, 31), (26, 27)]  # both runs were tried
    assert report["status"] == "not-written" and report["runs_written"] == []
    assert report["runs_not_written"] == [
        {"run": "2026112012", "skipped": {RAINIER: SKIP_WOULD_NARROW}},
        {"run": "2026112018", "skipped": {RAINIER: SKIP_WOULD_NARROW}},
    ]
    assert report["runs_left"] == ["2026112012", "2026112018"]
    progress = world["hs"].root.read_json(PROGRESS_KEY)
    assert {e["status"] for e in progress["runs"].values()} == {"nothing written"}


def test_the_plan_cap_check_counts_bytes_spent_and_runs_written(world) -> None:
    plan = _plan(world)  # spends its listings and inventories
    planned, spent = plan["totals"]["planned_bytes"], plan["totals"]["hindcast_bytes_spent"]
    assert spent == plan["transfer"]["total_bytes"] > 0
    assert plan["totals"]["pending_planned_bytes"] == planned
    # The plan alone fits this cap, but not with what the hindcast has already downloaded.
    tight_mb = (planned + spent - 1) / 1e6
    tight = _plan(world, cap_mb=tight_mb, spent_bytes=spent)  # answered from the cache
    assert tight["transfer"]["total_bytes"] == 0
    assert tight["totals"]["plan_alone_within_cap"] and not tight["totals"]["within_cap"]
    assert _fetch(world, tight, cap_mb=tight_mb)["status"] == "over-cap"  # the same rule
    assert _fetch(world, plan)["status"] == "done"
    with world["bucket"]() as client:
        after = client.stored_ledger()["total_bytes"]
    # Written runs need nothing more, so a cap of exactly what is spent is enough.
    covered = covered_fn(world["hs"], RAINIER, LIVE_FIELD_NAMES)
    done = _plan(world, cap_mb=after / 1e6, spent_bytes=after, covered=covered)
    assert done["totals"]["runs_not_written"] == [] and done["totals"]["pending_planned_bytes"] == 0
    assert done["totals"]["within_cap"]


def test_hours_without_a_trusted_stamp_are_unavailable_and_listed(
    data_root: Path, small_grid, manifests
) -> None:
    fake = FakeBucket(grid=small_grid)
    fake.add_run(
        R12,
        fields=list(LIVE_FIELD_NAMES),
        hours=range(24, 34),
        lag_s={fh: _lag(fh) for fh in range(49)},
    )
    # Every object of the 18Z run rewritten 25 h after its start: neither stamp is trusted.
    fake.add_run(
        R18, fields=list(LIVE_FIELD_NAMES), hours=range(24, 34), lag_s=dict.fromkeys(range(49), 9e4)
    )
    hs = HindcastStorage.at(data_root)
    with CachingBucket(
        hs.root, transport=fake.transport(), sleep=lambda _: None, clock=lambda: NOW
    ) as client:
        plan = build_plan(frames_from_times(manifests[RAINIER], FRAMES[:2]), client, now=NOW)
    assert plan["untrusted_hours"] == [["2026112018", 24], ["2026112018", 26]]
    assert [r["run"] for r in plan["runs"]] == ["2026112012"]  # both frames fall back to 12Z
    pairs = {p["valid_utc"][:16]: p for p in plan["pairs"]}
    later = pairs["2026-11-21T20:05"]
    assert (later["run"], later["fh_lo"], later["fh_hi"]) == ("2026112012", 32, 33)
    assert ["2026112018", "f26 not stored"] in later["skipped"]
    assert plan["unpaired"] == 0
