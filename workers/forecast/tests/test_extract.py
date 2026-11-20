"""One run end to end against the fake bucket (PRD §8.3, §11.2; CLAUDE.md "NOAA")."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from lookout_common.jsonl import read_jsonl

from lookout_forecast.decode import DecodeError
from lookout_forecast.discovery import list_run
from lookout_forecast.extract import (
    SKIP_ALREADY_WRITTEN,
    SKIP_WOULD_NARROW,
    BandwidthCapError,
    ExtractOptions,
    Extractor,
    RunIncompleteError,
    RunNotFoundError,
    marker_key,
    plan_run,
    requested_hours,
    run_covered,
    write_decisions,
)
from lookout_forecast.fields import resolve_fields
from lookout_forecast.grid import HRRR_CONUS
from lookout_forecast.hrrr import object_key
from lookout_forecast.idx import IdxError
from lookout_forecast.output import validate_document

from .conftest import RAINIER, RUN
from .fakebucket import FakeBucket, expected_value, missing_cell

FIELDS = ["VIS:surface", "HGT:cloud base", "LCDC:low cloud layer", "HGT:surface"]
KEY = "forecast/hrrr/rainier.paradise-mountain/2026111906z.json"


def _options(**kw) -> ExtractOptions:
    base = {
        "fields": tuple(resolve_fields(FIELDS)),
        "max_fh": 2,
        "viewpoint_ids": (RAINIER,),
        "workers": 2,
    }
    base.update(kw)
    return ExtractOptions(**base)


def _extractor(storage, bucket, manifests, small_grid, data_root, *, hours_after: float = 2.0):
    clock_value = RUN + timedelta(hours=hours_after)
    return Extractor(
        storage,
        bucket,
        manifests,
        grid=small_grid,
        cache_root=data_root / "cache" / "hrrr",
        clock=lambda: clock_value,
    )


@pytest.fixture
def run_in_bucket(fake: FakeBucket) -> FakeBucket:
    fake.add_run(RUN, fields=FIELDS, hours=range(49))
    return fake


def test_live_run_end_to_end(
    run_in_bucket, bucket, storage, manifests, small_grid, data_root
) -> None:
    extractor = _extractor(storage, bucket, manifests, small_grid, data_root)
    result = extractor.extract(RUN, _options())
    assert result.written == [KEY] and result.live and result.run_complete
    assert result.hours == [0, 1, 2]

    doc = json.loads((data_root / KEY).read_text())
    validate_document(doc)
    assert doc == json.loads(json.dumps(result.documents[RAINIER]))
    assert doc["field_names"] == FIELDS
    assert doc["source"] == "noaa-hrrr-bdp-pds" and doc["model"] == "hrrr-conus"
    assert doc["run_start_utc"] == "2026-11-19T06:00:00Z" and doc["run_max_fh"] == 48

    points = doc["points"]
    assert [p["kind"] for p in points].count("ray") == 30
    for f_index, name in enumerate(FIELDS):
        for h_index, fh in enumerate([0, 1, 2]):
            for p_index, point in enumerate(points):
                # The static terrain is decoded once, from f00, and repeated for every hour.
                source_fh = 0 if name == "HGT:surface" else fh
                want = expected_value(f_index, source_fh, point["i"], point["j"])
                got = doc["values"][f_index][h_index][p_index]
                if name == "HGT:cloud base" and missing_cell(point["i"], point["j"]):
                    assert got is None
                else:
                    assert got == want, (name, fh, point["id"])
    terrain_slot = FIELDS.index("HGT:surface")
    for point in points:
        assert point["model_terrain_m"] == expected_value(terrain_slot, 0, point["i"], point["j"])
    assert doc["fields"]["HGT:cloud base"]["datum"] == "m above MSL"
    assert doc["fields"]["HGT:cloud base"]["typeOfLevel"] == "cloudBase"
    assert doc["fields"]["VIS:surface"]["shortName"] == "vis"
    assert any("HGT:cloud base is m above MSL but often invalid" in note for note in doc["notes"])

    hour = doc["hours"][1]
    files = run_in_bucket.files[object_key(RUN, 1)]
    assert (
        hour["availability_source"] == "live-s3-lastmodified" and hour["availability_model"] is None
    )
    assert hour["availability_utc"] == files.idx_modified.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert hour["lag_s"] == (files.idx_modified - RUN).total_seconds()
    assert hour["url"].endswith("hrrr.t06z.wrfsfcf01.grib2")
    # Messages: filler, VIS, cloud base, filler, LCDC, terrain, filler. f00 fetches two
    # adjacent pairs; later hours skip the static terrain: VIS + cloud base, then LCDC.
    assert len(hour["requests"]) == 2
    assert [r["field"] for r in hour["byte_ranges"]] == FIELDS[:3]
    assert [r["field"] for r in doc["hours"][0]["byte_ranges"]] == FIELDS
    assert not any(r["terrain_only"] for r in doc["hours"][0]["byte_ranges"])
    assert sum(r["bytes"] for r in hour["byte_ranges"]) == hour["bytes_downloaded"]
    assert set(result.bytes_by_field["HGT:surface"]) == {0}
    assert "static" in doc["fields"]["HGT:surface"]["description"]
    assert any(note.startswith("HGT:surface is the model's static") for note in doc["notes"])

    grib = run_in_bucket.grib_requests()
    assert grib and all("range" in r.headers for r in grib)
    assert result.transfer["bytes"]["grib"] == sum(h["bytes_downloaded"] for h in doc["hours"])
    assert result.bytes_by_field["VIS:surface"][0] == doc["hours"][0]["byte_ranges"][0]["bytes"]

    assert not [p for p in (data_root / "cache").rglob("*") if p.is_file()]  # GRIB deleted
    log = read_jsonl(data_root / "forecast_log" / f"{RUN + timedelta(hours=2):%Y-%m-%d}.jsonl")
    assert [r["event"] for r in log] == ["lag"] * 49 + ["run"]  # every listed hour
    assert log[0]["run"] == "2026111906" and log[0]["fh"] == 0 and log[0]["lag_s"] > 0
    assert storage.read_json(marker_key(RUN))["viewpoints"][RAINIER]["forecast_hours"] == [0, 1, 2]


def test_archived_run_has_modeled_availability(
    run_in_bucket, bucket, storage, manifests, small_grid, data_root
) -> None:
    extractor = _extractor(storage, bucket, manifests, small_grid, data_root, hours_after=72)
    result = extractor.extract(RUN, _options(max_fh=1))
    assert not result.live
    doc = result.documents[RAINIER]
    for hour in doc["hours"]:
        assert hour["availability_source"] == "modeled"
        assert hour["availability_model"]["method"] == "conservative-fallback"
        assert hour["lag_s"] == 120 * 60  # nothing logged yet: the conservative 2 h
    assert any("Archived run" in note for note in doc["notes"])
    log = read_jsonl(data_root / "forecast_log" / f"{RUN + timedelta(hours=72):%Y-%m-%d}.jsonl")
    assert [r["event"] for r in log] == ["run"]  # no lag events from archived runs


def test_rerun_is_idempotent_and_never_narrows_a_file(
    run_in_bucket, bucket, storage, manifests, small_grid, data_root
) -> None:
    extractor = _extractor(storage, bucket, manifests, small_grid, data_root)
    extractor.extract(RUN, _options())
    again = extractor.extract(RUN, _options())
    assert again.written == [] and "already written" in again.skipped[RAINIER]
    narrower = extractor.extract(RUN, _options(fields=tuple(resolve_fields(["VIS"])), max_fh=3))
    assert narrower.written == [] and "lacks" in narrower.skipped[RAINIER]
    wider = extractor.extract(RUN, _options(max_fh=3))
    assert wider.written == [KEY]
    assert json.loads((data_root / KEY).read_text())["forecast_hours"] == [0, 1, 2, 3]
    forced = extractor.extract(RUN, _options(max_fh=1, force=True))
    assert forced.written == [KEY]
    assert run_covered(storage, RUN, viewpoint_ids=[RAINIER], field_names=FIELDS, hours=[0, 1])
    assert not run_covered(
        storage, RUN, viewpoint_ids=[RAINIER], field_names=FIELDS, hours=[0, 1, 2]
    )


def test_incomplete_and_missing_runs(
    run_in_bucket, bucket, storage, manifests, small_grid, data_root
) -> None:
    run_in_bucket.remove_hour(RUN, 1)
    extractor = _extractor(storage, bucket, manifests, small_grid, data_root)
    with pytest.raises(RunIncompleteError) as info:
        extractor.extract(RUN, _options())
    assert info.value.missing == [1]
    result = extractor.extract(RUN, _options(allow_incomplete=True))
    doc = result.documents[RAINIER]
    assert doc["forecast_hours"] == [0, 2] and 1 in doc["missing_hours"]
    assert not doc["run_complete"]
    assert storage.read_json(marker_key(RUN))["missing_hours"] == [1]
    assert run_covered(
        storage,
        RUN,
        viewpoint_ids=[RAINIER],
        field_names=FIELDS,
        hours=[0, 1, 2],
        accept_missing=True,
    )
    with pytest.raises(RunNotFoundError):
        extractor.extract(RUN + timedelta(hours=1), _options())


def test_bandwidth_cap_stops_before_any_grib_request(
    run_in_bucket, bucket, storage, manifests, small_grid, data_root
) -> None:
    extractor = _extractor(storage, bucket, manifests, small_grid, data_root)
    with pytest.raises(BandwidthCapError):
        extractor.extract(RUN, _options(max_bytes=1000))
    assert run_in_bucket.grib_requests() == []
    assert not (data_root / KEY).exists()


def test_field_missing_from_one_inventory(
    fake, bucket, storage, manifests, small_grid, data_root
) -> None:
    fake.add_run(RUN, fields=FIELDS, hours=range(49), drop_from_idx={1: ["LCDC:low cloud layer"]})
    extractor = _extractor(storage, bucket, manifests, small_grid, data_root)
    doc = extractor.extract(RUN, _options()).documents[RAINIER]
    lcdc = FIELDS.index("LCDC:low cloud layer")
    assert all(v is None for v in doc["values"][lcdc][1])
    assert all(v is not None for v in doc["values"][lcdc][0])
    assert doc["hours"][1]["fields_missing_from_idx"] == ["LCDC:low cloud layer"]
    assert any("absent from some inventories" in note for note in doc["notes"])


def test_terrain_is_fetched_once_when_not_requested(
    run_in_bucket, bucket, storage, manifests, small_grid, data_root
) -> None:
    extractor = _extractor(storage, bucket, manifests, small_grid, data_root)
    result = extractor.extract(RUN, _options(fields=tuple(resolve_fields(["VIS", "LCDC"]))))
    doc = result.documents[RAINIER]
    assert doc["field_names"] == ["VIS:surface", "LCDC:low cloud layer"]
    terrain = [r for r in doc["hours"][0]["byte_ranges"] if r["field"] == "HGT:surface"]
    assert len(terrain) == 1 and terrain[0]["terrain_only"]
    assert not any(r["field"] == "HGT:surface" for r in doc["hours"][1]["byte_ranges"])
    assert doc["points"][0]["model_terrain_m"] == expected_value(
        3, 0, doc["points"][0]["i"], doc["points"][0]["j"]
    )
    assert "HGT:surface (terrain only)" in result.bytes_by_field


def test_transient_errors_are_retried(
    run_in_bucket, bucket, storage, manifests, small_grid, data_root
) -> None:
    run_in_bucket.fail_next[object_key(RUN, 0) + ".idx"] = 1
    run_in_bucket.fail_next[object_key(RUN, 2)] = 2
    extractor = _extractor(storage, bucket, manifests, small_grid, data_root)
    result = extractor.extract(RUN, _options())
    assert result.written == [KEY] and result.transfer["retries"] == 3


def test_a_mislabelled_message_writes_nothing(
    fake, bucket, storage, manifests, small_grid, data_root
) -> None:
    fields = ["VIS:surface", "LCDC:low cloud layer", "HGT:surface"]
    fake.add_run(
        RUN,
        fields=fields,
        hours=range(49),
        rename_in_idx={"LCDC:low cloud layer": "MCDC:middle cloud layer"},
    )
    extractor = _extractor(storage, bucket, manifests, small_grid, data_root)
    with pytest.raises(DecodeError, match="identity"):
        extractor.extract(RUN, _options(fields=tuple(resolve_fields(["VIS", "MCDC", "TERRAIN"]))))
    assert not (data_root / KEY).exists()
    assert not [p for p in (data_root / "cache").rglob("*") if p.is_file()]


def test_all_eight_viewpoints_on_the_real_grid(bucket, storage, manifests, data_root) -> None:
    fake = FakeBucket(grid=HRRR_CONUS)
    fake.add_run(RUN, fields=["VIS:surface", "HGT:surface"], hours=[0], fillers=False)
    from lookout_forecast.bucket import HrrrBucket

    with HrrrBucket(transport=fake.transport()) as real_grid_bucket:
        extractor = Extractor(
            storage,
            real_grid_bucket,
            manifests,
            cache_root=data_root / "cache" / "hrrr",
            clock=lambda: RUN + timedelta(hours=1),
        )
        result = extractor.extract(
            RUN,
            ExtractOptions(
                fields=tuple(resolve_fields(["VIS", "TERRAIN"])), max_fh=0, allow_incomplete=True
            ),
        )
    assert len(result.written) == 8
    for vp_id, doc in result.documents.items():
        camera = doc["points"][0]
        cell = HRRR_CONUS.nearest_ij(manifests[vp_id].camera.lat, manifests[vp_id].camera.lon)
        assert (camera["i"], camera["j"]) == cell
        assert doc["values"][0][0][0] == expected_value(0, 0, *cell)
        assert len(doc["points"]) == 56 + len(manifests[vp_id].landmarks)


def test_each_run_reports_only_its_own_transfer(
    fake, bucket, storage, manifests, small_grid, data_root
) -> None:
    """Review finding 2 (T1): run events used to carry the bucket's lifetime totals."""
    runs = [RUN + timedelta(hours=1), RUN + timedelta(hours=2), RUN + timedelta(hours=3)]
    for run in runs:
        fake.add_run(run, fields=FIELDS, hours=range(19))
    extractor = _extractor(storage, bucket, manifests, small_grid, data_root, hours_after=4)
    results = [extractor.extract(run, _options()) for run in runs]
    grib = [r.transfer["bytes"]["grib"] for r in results]
    assert grib[0] == grib[1] == grib[2] == results[0].planned_bytes
    assert [r.transfer["requests"] for r in results] == [results[0].transfer["requests"]] * 3
    log = read_jsonl(data_root / "forecast_log" / f"{RUN + timedelta(hours=4):%Y-%m-%d}.jsonl")
    logged = [r["transfer"]["bytes"]["grib"] for r in log if r["event"] == "run"]
    assert logged == grib
    assert bucket.ledger.snapshot()["bytes"]["grib"] == sum(grib)


def test_missing_hours_lists_only_the_hours_asked_for(
    run_in_bucket, bucket, storage, manifests, small_grid, data_root
) -> None:
    """Review T7: an hour beyond --max-fh was reported missing in the file."""
    run_in_bucket.remove_hour(RUN, 10)
    run_in_bucket.remove_hour(RUN, 1)
    extractor = _extractor(storage, bucket, manifests, small_grid, data_root)
    doc = extractor.extract(RUN, _options(allow_incomplete=True)).documents[RAINIER]
    assert doc["forecast_hours"] == [0, 2] and doc["missing_hours"] == [1]
    assert storage.read_json(marker_key(RUN))["missing_hours"] == [1]


def test_terrain_absent_from_the_first_inventory(
    fake, bucket, storage, manifests, small_grid, data_root
) -> None:
    fake.add_run(RUN, fields=FIELDS, hours=range(49), drop_from_idx={0: ["HGT:surface"]})
    extractor = _extractor(storage, bucket, manifests, small_grid, data_root)
    with pytest.raises(IdxError, match="first hour"):
        extractor.extract(RUN, _options())  # asked for, and only f00 would be fetched
    doc = extractor.extract(RUN, _options(fields=tuple(resolve_fields(["VIS"])))).documents[RAINIER]
    assert all(point["model_terrain_m"] is None for point in doc["points"])
    assert any("model_terrain_m is null" in note for note in doc["notes"])


def test_terrain_only_request_fetches_one_hour(
    run_in_bucket, bucket, storage, manifests, small_grid, data_root
) -> None:
    extractor = _extractor(storage, bucket, manifests, small_grid, data_root)
    result = extractor.extract(RUN, _options(fields=tuple(resolve_fields(["TERRAIN"]))))
    doc = result.documents[RAINIER]
    assert [len(hour["byte_ranges"]) for hour in doc["hours"]] == [1, 0, 0]
    assert len(run_in_bucket.grib_requests()) == 1
    terrain = [expected_value(3, 0, p["i"], p["j"]) for p in doc["points"]]
    assert doc["values"][0] == [terrain, terrain, terrain]
    assert not [p for p in (data_root / "cache").rglob("*") if p.is_file()]


def _requests(fake: FakeBucket, kind: str) -> int:
    """Requests of one kind the fake bucket served: listing, idx or grib."""
    paths = [r.url.path for r in fake.requests]
    if kind == "listing":
        return sum(path in {"", "/"} for path in paths)
    if kind == "idx":
        return sum(path.endswith(".idx") for path in paths)
    return sum(path.endswith(".grib2") for path in paths)


def test_options_hours_are_validated_and_normalised() -> None:
    assert ExtractOptions(hours=(25, 24, 25)).hours == (24, 25)
    for bad in ((), (-1,), (True,), (2.0,)):
        with pytest.raises(ValueError):
            ExtractOptions(hours=bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not both"):
        ExtractOptions(hours=(1,), max_fh=2)
    assert requested_hours(RUN, ExtractOptions(max_fh=2)) == [0, 1, 2]
    assert requested_hours(RUN, ExtractOptions(hours=(48, 24))) == [24, 48]
    with pytest.raises(ValueError, match="stops at f18"):
        requested_hours(RUN + timedelta(hours=1), ExtractOptions(hours=(17, 24)))


def test_explicit_hours_fetch_only_those_hours(
    run_in_bucket, bucket, storage, manifests, small_grid, data_root
) -> None:
    """The backtest pair's request: the two hours that bracket one valid time (f24, f25)."""
    extractor = _extractor(storage, bucket, manifests, small_grid, data_root, hours_after=72)
    opts = _options(max_fh=None, hours=(25, 24))
    plan = plan_run(bucket, RUN, fields=opts.fields, hours=opts.hours)
    assert plan["hours"] == [24, 25]
    result = extractor.extract(RUN, opts)
    assert result.hours == [24, 25] and result.written == [KEY]
    doc = result.documents[RAINIER]
    assert doc["forecast_hours"] == [24, 25] and doc["missing_hours"] == []
    asked = {r.url.path.lstrip("/") for r in run_in_bucket.grib_requests()}
    assert asked == {object_key(RUN, 24), object_key(RUN, 25)}
    # The static terrain comes with the first hour asked for, f24.
    assert [r["field"] for r in doc["hours"][0]["byte_ranges"]] == FIELDS
    assert result.transfer["bytes"]["grib"] == plan["total_bytes"]
    assert run_covered(storage, RUN, viewpoint_ids=[RAINIER], field_names=FIELDS, hours=[24, 25])
    with pytest.raises(ValueError, match="stops at f18"):
        extractor.extract(RUN + timedelta(hours=1), _options(max_fh=None, hours=(24,)))


def test_the_module_run_hours_still_decides_without_explicit_hours(
    run_in_bucket, bucket, storage, manifests, small_grid, data_root, monkeypatch
) -> None:
    """eval's pair_forecast.only_hours replaces extract.run_hours for one call (until it moves
    to ExtractOptions.hours); planner and extractor must keep going through that global."""
    from lookout_forecast import extract

    monkeypatch.setattr(extract, "run_hours", lambda run_start, limit=None: [31, 32])
    assert plan_run(bucket, RUN, fields=_options().fields)["hours"] == [31, 32]
    result = _extractor(storage, bucket, manifests, small_grid, data_root).extract(
        RUN, _options(max_fh=None)
    )
    assert result.hours == [31, 32]


def test_nothing_is_downloaded_when_nothing_would_be_written(
    run_in_bucket, bucket, storage, manifests, small_grid, data_root
) -> None:
    extractor = _extractor(storage, bucket, manifests, small_grid, data_root)
    extractor.extract(RUN, _options())
    for opts, reason in (
        (_options(), SKIP_ALREADY_WRITTEN),
        (_options(fields=tuple(resolve_fields(["VIS", "MCDC"]))), SKIP_WOULD_NARROW),
    ):
        run_in_bucket.requests.clear()
        result = extractor.extract(RUN, opts)
        assert result.hours == [] and result.written == [] and result.skipped == {RAINIER: reason}
        assert (_requests(run_in_bucket, "idx"), _requests(run_in_bucket, "grib")) == (0, 0)
        assert result.transfer["requests"] == {"listing": 1}
    assert write_decisions(
        storage, RUN, viewpoint_ids=[RAINIER], field_names=FIELDS, hours=[0, 1, 2], force=True
    ) == {RAINIER: None}
    log = read_jsonl(data_root / "forecast_log" / f"{RUN + timedelta(hours=2):%Y-%m-%d}.jsonl")
    assert [r["event"] for r in log].count("run") == 1  # the no-ops logged nothing


def test_a_fresh_listing_is_used_instead_of_listing_again(
    run_in_bucket, bucket, storage, manifests, small_grid, data_root
) -> None:
    listing = list_run(bucket, RUN, now=RUN + timedelta(hours=2))
    run_in_bucket.requests.clear()
    extractor = _extractor(storage, bucket, manifests, small_grid, data_root)
    result = extractor.extract(RUN, _options(), listing=listing)
    assert result.written == [KEY] and _requests(run_in_bucket, "listing") == 0
    assert "listing" not in result.transfer["requests"]
    with pytest.raises(ValueError, match="listing given is of run"):
        extractor.extract(RUN + timedelta(hours=1), _options(), listing=listing)
