"""An archived hour is never available before its own S3 stamps (PRD §8.3, §13.9).

Review finding (20 Nov 2026, high): survey lags moved every f19-f48 cell onto rule 2, fitted
on November 2026 lags, and older seasons published some hours later. 2025071500 f46 was
published at 114.57 min (GRIB2 01:54:30Z, ``.idx`` 01:54:34Z) while the model said 109.6 min,
so its archived file said 01:49:36Z and ``select_stored_run`` chose the run at 01:52Z, before
it existed on S3. The availability is now the later of the model and the stamps, counting a
stamp only within 24 h of the run start (rewritten objects carry much later stamps: the
recorded 2020120312 listing has every ``.idx`` stamped 17 Feb 2021).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from lookout_forecast import availability, output
from lookout_forecast.availability import (
    ArchivedStamps,
    LagModel,
    archived_availability,
    listed_availability,
    select_run_for_issue,
    select_stored_run,
)
from lookout_forecast.bucket import HrrrBucket
from lookout_forecast.discovery import IDX_AFTER_GRIB_MARGIN_S, STAMP_TRUST_H, publication_floor
from lookout_forecast.evidence import lag_check, lag_survey
from lookout_forecast.extract import ExtractOptions, Extractor
from lookout_forecast.fields import resolve_fields
from lookout_forecast.hrrr import run_prefix
from lookout_forecast.laglog import SurveyLogger, read_lag_log
from lookout_forecast.output import DocumentError, load_schema, validate_document

from .conftest import FIXTURES, RAINIER

R0715 = datetime(2025, 7, 15, 0, tzinfo=UTC)
#: The real f46 stamps of 2025071500 (lag-check on 20 Nov 2026): GRIB2 and .idx.
F46_GRIB = datetime(2025, 7, 15, 1, 54, 30, tzinfo=UTC)
F46_IDX = datetime(2025, 7, 15, 1, 54, 34, tzinfo=UTC)
#: Rule 2 for f46 of the 00Z cycle in the review's scratch test: 109.6 min.
MODEL_F46_S = 109.6 * 60
#: Recorded by `lookout-forecast lag-check 2020120312 ... --save-listings` on 2026-11-20 at
#: 03:58 UTC: one ListObjectsV2 answer, 24,393 bytes, 98 objects. GRIB2 stamps are 54-108 min
#: after the run start; every .idx stamp is 2021-02-17T05:16:42-43Z (75.7 days later).
REWRITTEN = FIXTURES / "list_20201203_t12z_full.xml"
R1203 = datetime(2020, 12, 3, 12, tzinfo=UTC)


def _f46_model() -> LagModel:
    """20 survey lags of f46 at 109.6 min over the synoptic cycles: rule 2 gives 109.6 min."""
    events = [
        {
            "event": "lag",
            "run": f"202611{10 + k // 4:02d}{(k % 4) * 6:02d}",
            "fh": 46,
            "lag_s": MODEL_F46_S,
            "method": "survey",
        }
        for k in range(20)
    ]
    return LagModel.from_events(events)


def test_publication_floor_counts_only_stamps_within_24_h() -> None:
    run = R0715
    grib, idx = run + timedelta(minutes=95), run + timedelta(minutes=95, seconds=4)
    assert publication_floor(run, grib, idx) == (idx, ())
    late = run + timedelta(days=75)  # a rewritten object
    margin = timedelta(seconds=IDX_AFTER_GRIB_MARGIN_S)
    assert publication_floor(run, grib, late) == (grib + margin, ("idx",))
    assert publication_floor(run, late, idx) == (idx, ("grib",))  # an .idx follows its GRIB2
    assert publication_floor(run, late, late) == (None, ("grib", "idx"))
    assert publication_floor(run, None, None) == (None, ())
    edge = run + timedelta(hours=STAMP_TRUST_H)
    assert publication_floor(run, edge, edge) == (edge, ())  # exactly 24 h still counts
    beyond = edge + timedelta(seconds=1)
    assert publication_floor(run, beyond, beyond)[0] is None


def test_archived_availability_is_the_later_of_the_model_and_the_stamps() -> None:
    model = _f46_model()
    modelled = R0715 + timedelta(seconds=MODEL_F46_S)
    alone = archived_availability(model, R0715, 46)
    assert alone[:2] == (modelled, "modeled")
    assert "s3_floor_utc" not in alone[2]  # no stamps given: the model as before
    when, source, note = archived_availability(model, R0715, 46, ArchivedStamps(F46_GRIB, F46_IDX))
    assert (when, source) == (F46_IDX, "archived-s3-lastmodified")
    assert note["lag_s"] == MODEL_F46_S and note["method"] == "p95-forecast-hour-or-cycle-max"
    assert note["s3_floor_utc"] == "2025-07-15T01:54:34Z" and note["s3_stamps_ignored"] == []
    early = ArchivedStamps(R0715 + timedelta(minutes=90), R0715 + timedelta(minutes=90, seconds=4))
    assert archived_availability(model, R0715, 46, early)[:2] == (modelled, "modeled")


@pytest.fixture
def archived_0715(fake, bucket, storage, manifests, small_grid, data_root):
    """2025071500 f45-f46 extracted long after the run, with the f46 model of the review."""
    fake.add_run(
        R0715,
        fields=["VIS:surface", "HGT:surface"],
        hours=[45, 46],
        lag_s={46: (F46_GRIB - R0715).total_seconds()},
    )
    model = _f46_model()
    extractor = Extractor(
        storage,
        bucket,
        {RAINIER: manifests[RAINIER]},
        lag_model=model,
        grid=small_grid,
        cache_root=data_root / "cache" / "hrrr",
        clock=lambda: datetime(2026, 11, 20, 4, tzinfo=UTC),
    )
    options = ExtractOptions(
        fields=tuple(resolve_fields(["VIS", "TERRAIN"])), hours=(45, 46), viewpoint_ids=(RAINIER,)
    )
    result = extractor.extract(R0715, options)
    return result.documents[RAINIER], model


def test_the_archived_file_records_the_stamps_when_they_are_later(archived_0715) -> None:
    doc, _ = archived_0715
    validate_document(doc)
    f45, f46 = doc["hours"]
    assert f46["availability_utc"] == "2025-07-15T01:54:34Z"  # was 01:49:36Z, the model
    assert f46["availability_source"] == "archived-s3-lastmodified"
    assert f46["lag_s"] == 6874.0
    assert f46["availability_model"]["lag_s"] == MODEL_F46_S  # the model it beat
    assert f46["availability_model"]["s3_floor_utc"] == f46["idx_last_modified_utc"]
    # f45 has no logged lag: the 120 min fallback is later than its stamps (95 min).
    assert f45["availability_utc"] == "2025-07-15T02:00:00Z"
    assert f45["availability_source"] == "modeled"
    assert f45["availability_model"]["s3_floor_utc"] == "2025-07-15T01:35:04Z"
    assert any("never before the hour's own S3" in note for note in doc["notes"])


def test_a_stored_archived_run_is_never_chosen_before_it_was_on_s3(
    archived_0715, storage, bucket, fake
) -> None:
    _, model = archived_0715
    valid = R0715 + timedelta(hours=46)
    issue = datetime(2025, 7, 15, 1, 52, tzinfo=UTC)  # after the model, before the stamps
    assert select_stored_run(storage, RAINIER, issue, valid, lag_model=model) is None
    choice = select_stored_run(storage, RAINIER, F46_IDX, valid, lag_model=model)
    assert choice is not None and choice.run_id == "2025071500"
    assert choice.availability_utc == F46_IDX
    assert choice.availability_source == "archived-s3-lastmodified"
    assert choice.availability_model is not None
    assert choice.availability_model["s3_floor_utc"] == "2025-07-15T01:54:34Z"
    assert choice.to_dict()["availability_source"] == "archived-s3-lastmodified"

    # The model alone (nothing stored or listed) still says 01:49:36Z: that is why a pair
    # must choose with the stamps, and why the extracted file can disagree with it.
    alone = select_run_for_issue(issue, valid, lag_model=model)
    assert alone is not None and alone.availability_utc == R0715 + timedelta(seconds=6576)
    fake.requests.clear()
    listed = listed_availability(bucket)
    assert select_run_for_issue(issue, valid, lag_model=model, availability=listed) is None
    again = select_run_for_issue(F46_IDX, valid, lag_model=model, availability=listed)
    assert again is not None and again.availability_utc == F46_IDX
    assert again.to_dict() == choice.to_dict()
    assert len(fake.requests) == 1  # one listing, cached across both selections


def _replay(*runs: datetime) -> httpx.MockTransport:
    """Serve the recorded listing of each run (``tests/fixtures/list_<day>_t<HH>z_full.xml``)."""
    answers = {
        run_prefix(run): (FIXTURES / f"list_{run:%Y%m%d}_t{run:%H}z_full.xml").read_bytes()
        for run in runs
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=answers[request.url.params["prefix"]])

    return httpx.MockTransport(handler)


def _low_model(cycle: int) -> LagModel:
    """20 lags of 40 min for every hour of one cycle: earlier than any listed stamp (46.6+ min)."""
    return LagModel.from_events(
        {"event": "lag", "run": f"202611{day + 1:02d}{cycle:02d}", "fh": fh, "lag_s": 2400.0}
        for day in range(20)
        for fh in range(49)
    )


def test_rewritten_idx_stamps_are_ignored_on_a_real_listing(storage) -> None:
    assert REWRITTEN.is_file()
    with HrrrBucket(transport=_replay(R1203)) as bucket:
        fallback = lag_check(bucket, [R1203], LagModel())["runs"][0]
        low = lag_check(bucket, [R1203], _low_model(12))["runs"][0]
        survey = lag_survey(bucket, R1203, R1203, pause_s=0)
        logger = SurveyLogger(storage)
        lag_survey(bucket, R1203, R1203, pause_s=0, logger=logger)
    assert fallback["ready_hours"] == 49 and fallback["hours_with_ignored_stamps"] == 49
    assert {tuple(row["s3_stamps_ignored"]) for row in fallback["hours"]} == {("idx",)}
    assert fallback["hours_later_than_model"] == 49  # the raw lag is 75.7 days
    # Against the 120 min fallback the GRIB2 stamps (+5 min) never win ...
    assert fallback["hours_set_by_stamps"] == 0
    assert {row["availability_lag_min"] for row in fallback["hours"]} == {120.0}
    # ... against a 40 min model they all do, and the 17 Feb 2021 stamps never count.
    assert low["hours_set_by_stamps"] == 49
    f00 = low["hours"][0]
    assert f00["grib_last_modified"] == "2020-12-03T12:54:17Z"
    assert f00["availability_lag_min"] == round((54 * 60 + 17 + 300) / 60, 2)
    assert max(row["availability_lag_min"] for row in low["hours"]) < 120.0
    # A survey neither summarises nor logs these hours.
    assert survey["stamps_not_trusted"] == {"2020120312": list(range(49))}
    assert survey["lags"] == 0
    assert logger.report()["stamps_not_trusted"] == 49 and logger.written == 0
    assert read_lag_log(storage).events == ()


def test_runs_that_kept_their_stamps_are_set_by_them_only_where_the_model_is_earlier() -> None:
    runs = [datetime(2024, 1, 14, 12, tzinfo=UTC), datetime(2024, 1, 14, 18, tzinfo=UTC)]
    with HrrrBucket(transport=_replay(*runs)) as bucket:
        fallback = lag_check(bucket, runs, LagModel())
        low = lag_check(bucket, runs[:1], _low_model(12))
    for run in fallback["runs"]:
        assert run["hours_set_by_stamps"] == 0 and run["hours_with_ignored_stamps"] == 0
    twelve = low["runs"][0]
    assert twelve["hours_set_by_stamps"] == 49
    assert all(row["availability_lag_min"] == row["lag_min"] for row in twelve["hours"])


def test_validation_rejects_an_archived_time_before_the_stamps(archived_0715) -> None:
    doc, _ = archived_0715

    def broken(change) -> None:
        bad = {**doc, "hours": [dict(hour) for hour in doc["hours"]]}
        bad["hours"][1]["availability_model"] = dict(bad["hours"][1]["availability_model"])
        change(bad["hours"][1])
        with pytest.raises(DocumentError):
            validate_document(bad)

    broken(lambda hour: hour.update(availability_utc="2025-07-15T01:49:36Z"))
    broken(lambda hour: hour["availability_model"].update(s3_floor_utc=None))
    broken(lambda hour: hour.update(availability_model=None))


def test_the_archived_sources_are_the_file_format_s() -> None:
    assert (availability.SOURCE_MODELED, availability.SOURCE_ARCHIVED_STAMP) == (
        output.ARCHIVED_SOURCES
    )
    hour = load_schema()["$defs"]["hour"]["properties"]
    assert set(output.ARCHIVED_SOURCES) < set(hour["availability_source"]["enum"])


def test_listed_availability_marks_hours_missing_from_s3_unavailable(fake, bucket) -> None:
    fake.add_run(R0715, fields=["VIS:surface"], hours=[45])
    listed = listed_availability(bucket)
    assert listed(R0715, 46) is availability.Lookup.UNAVAILABLE
    assert isinstance(listed(R0715, 45), ArchivedStamps)
    assert listed(R0715 + timedelta(hours=6), 45) is availability.Lookup.UNAVAILABLE
