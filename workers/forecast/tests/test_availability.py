"""Lag model, lag events and run selection for issued predictions (PRD §8.3, §13.2, §13.9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from lookout_common.storage import LocalStorage

from lookout_forecast.availability import (
    FALLBACK_LAG_MIN,
    ArchivedStamps,
    LagModel,
    Lookup,
    availability_from_document,
    lag_events,
    p95_nearest_rank,
    read_lag_events,
    select_run_for_issue,
    select_stored_run,
    stored_availability,
    stored_run_exists,
    typical_lag_s,
)
from lookout_forecast.bucket import S3Object
from lookout_forecast.discovery import group_runs
from lookout_forecast.extract import ExtractOptions, Extractor
from lookout_forecast.fields import resolve_fields

from .conftest import RAINIER

T = datetime(2026, 11, 19, 0, tzinfo=UTC)
FALLBACK = timedelta(minutes=FALLBACK_LAG_MIN)
#: Live lags logged for run 2026111908 (forecast_log/2026-11-19.jsonl, 09:54 UTC), seconds.
LOGGED_08Z = {
    0: 3200.0, 1: 3310.0, 2: 3431.0, 3: 3565.0, 4: 3630.0, 5: 4410.0, 6: 3878.0,
    7: 3996.0, 8: 4065.0, 9: 4782.0, 10: 4310.0, 11: 4436.0, 12: 4555.0, 13: 4624.0,
    14: 4802.0, 15: 4867.0, 16: 4995.0, 17: 5122.0, 18: 5182.0,
}  # fmt: skip


def _events(run: str, lags_s: dict[int, float]) -> list[dict]:
    return [{"event": "lag", "run": run, "fh": fh, "lag_s": lag} for fh, lag in lags_s.items()]


def test_typical_lags_follow_the_research_note() -> None:
    assert typical_lag_s(0) == 52 * 60
    assert typical_lag_s(18) == 85 * 60
    assert typical_lag_s(48) == 108 * 60
    assert typical_lag_s(9) == pytest.approx((52 + 33 / 2) * 60)
    with pytest.raises(ValueError):
        typical_lag_s(-1)


def test_p95_nearest_rank() -> None:
    assert p95_nearest_rank([5.0]) == 5.0
    assert p95_nearest_rank(list(range(1, 101))) == 95
    assert p95_nearest_rank(list(range(1, 21))) == 19
    with pytest.raises(ValueError):
        p95_nearest_rank([])


def test_fallback_is_later_than_every_logged_live_lag() -> None:
    """Review finding 1: the forecast.md curve was earlier than all 19 lags of 08Z."""
    model = LagModel()
    run = datetime(2026, 11, 19, 8, tzinfo=UTC)
    assert all(typical_lag_s(fh) < lag for fh, lag in LOGGED_08Z.items())  # the old default
    for fh, lag in LOGGED_08Z.items():
        seconds, method, n = model.lag_s(run, fh)
        assert method == "conservative-fallback" and n == 0
        assert seconds == FALLBACK_LAG_MIN * 60 > lag
    # With those 19 lags logged (one per cell), each cell keeps at least its own lag.
    logged = LagModel.from_events(_events("2026111908", LOGGED_08Z))
    assert logged.lag_s(run, 5) == (FALLBACK_LAG_MIN * 60, "conservative-fallback", 1)


def test_fallback_never_below_a_late_logged_lag() -> None:
    late = LagModel.from_events(_events("2026111715", {12: 131.2 * 60}))
    run = datetime(2026, 11, 20, 15, tzinfo=UTC)
    assert late.lag_s(run, 12) == (131.2 * 60, "conservative-fallback", 1)
    assert late.lag_s(run + timedelta(hours=1), 12)[0] == FALLBACK_LAG_MIN * 60  # other cycle


def test_cycle_hour_p95_once_a_cell_has_enough_lags() -> None:
    events = []
    for day in range(25):
        events += _events(f"202611{day + 1:02d}06", {3: 3000.0 + day})
        events += _events(f"202611{day + 1:02d}07", {3: 4000.0 + day})
    events.append({"event": "lag", "run": "2026112606", "fh": 3, "lag_s": -5.0})  # ignored
    model = LagModel.from_events(events)
    six = datetime(2026, 11, 30, 6, tzinfo=UTC)
    assert model.lag_s(six, 3) == (3023.0, "p95-cycle-hour", 25)
    assert model.lag_s(six + timedelta(hours=1), 3) == (4023.0, "p95-cycle-hour", 25)
    assert model.availability(six, 3) == six + timedelta(seconds=3023)
    assert model.describe(six, 3) == {
        "method": "p95-cycle-hour",
        "lag_s": 3023.0,
        "samples": 25,
        "samples_by_method": {"live": 25, "survey": 0},  # events without a method are live
        "pooled_samples": 50,
        "pooled_samples_by_method": {"live": 50, "survey": 0},
        "min_samples": 20,
    }
    # 08Z has no logged lag of its own: fallback, even though f03 has 50 pooled lags.
    assert model.lag_s(six + timedelta(hours=2), 3)[1] == "conservative-fallback"


def test_a_late_cycle_is_never_modelled_with_the_pooled_p95() -> None:
    """In the 124-run survey, 23Z published f06-f18 at ~111 min; other cycles ~62-85 min."""
    events = []
    for k in range(22):  # 22 ordinary runs: pooled f06 has enough lags
        events += _events(f"20261118{k:02d}", {6: 62.5 * 60})
    events += _events("2026111823", {6: 111.0 * 60})
    model = LagModel.from_events(events)
    pooled_only = model.pooled_p95_s[6]
    assert pooled_only == 62.5 * 60
    z23 = datetime(2026, 11, 19, 23, tzinfo=UTC)
    assert model.lag_s(z23, 6) == (111.0 * 60, "p95-forecast-hour-or-cycle-max", 1)
    ordinary = datetime(2026, 11, 19, 5, tzinfo=UTC)
    assert model.lag_s(ordinary, 6) == (62.5 * 60, "p95-forecast-hour-or-cycle-max", 1)


def test_lag_events_and_reading_them_back(tmp_path) -> None:
    objects = []
    for fh in range(3):
        key = f"hrrr.20261119/conus/hrrr.t00z.wrfsfcf{fh:02d}.grib2"
        objects.append(S3Object(key, T + timedelta(minutes=55 + fh), 100, "e"))
        objects.append(S3Object(key + ".idx", T + timedelta(minutes=55 + fh, seconds=4), 10, "e"))
    listing = group_runs(objects, listed_utc=T)[T]
    events = lag_events(listing)
    assert [e["fh"] for e in events] == [0, 1, 2]
    assert events[1]["lag_s"] == 56 * 60 + 4  # the later of GRIB and .idx
    assert events[1]["last_modified"] == "2026-11-19T00:56:04Z"
    assert events[0]["synoptic"] is True
    storage = LocalStorage(tmp_path / "data", check=False)
    storage.append_jsonl_many(
        "forecast_log/2026-11-19.jsonl",
        [*events, events[0], {"event": "run"}, dict(events[0], run="20261119xx")],
    )
    storage.append_jsonl_many("forecast_log/2026-11-18.jsonl", [dict(events[0], run="2026111800")])
    back = read_lag_events(storage)
    assert len(back) == 4  # duplicate (run, fh) and an unparseable run dropped
    assert len(read_lag_events(storage, since=datetime(2026, 11, 19, tzinfo=UTC))) == 3


def test_one_hour_lead_with_the_fallback_uses_an_older_run() -> None:
    issue = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
    choice = select_run_for_issue(issue, issue + timedelta(hours=1))
    # 120 min for every hour: 10Z f01 at 12:00, 09Z f02 at 11:00, 08Z f03 at 10:00 -> 08Z.
    assert choice is not None and choice.run_id == "2026111908"
    assert (choice.fh_lo, choice.fh_hi, choice.weight_hi) == (3, 3, 0.0)
    assert choice.availability_source == "modeled"
    assert choice.availability_model is not None
    assert choice.availability_model["method"] == "conservative-fallback"
    assert choice.availability_utc == issue
    assert choice.lead_h == 1.0 and choice.nominal_lead_h == 1.0
    assert [run for run, _ in choice.skipped] == ["2026111910", "2026111909"]
    assert choice.to_dict()["availability_model"]["method"] == "conservative-fallback"


def test_24_hour_leads_come_from_synoptic_runs_with_actual_leads_24_to_30_h() -> None:
    leads = []
    for minute in range(0, 24 * 60, 20):
        issue = datetime(2026, 11, 19, tzinfo=UTC) + timedelta(minutes=minute)
        choice = select_run_for_issue(issue, issue + timedelta(hours=24))
        assert choice is not None
        assert choice.run_start_utc.hour in {0, 6, 12, 18}
        assert choice.availability_utc <= issue
        leads.append(choice.lead_h)
    assert min(leads) >= 24.0 and max(leads) <= 30.0
    assert max(leads) > 28.0  # actual leads really spread; they are kept, not rounded


def test_missing_runs_fall_back_and_are_never_dropped() -> None:
    issue = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    gone = {datetime(2026, 11, 19, 10, tzinfo=UTC), datetime(2026, 11, 19, 9, tzinfo=UTC)}
    choice = select_run_for_issue(
        issue, issue + timedelta(hours=3), run_exists=lambda r: r not in gone
    )
    assert choice is not None and choice.run_id == "2026111908"
    assert ("2026111910", "run missing") in choice.skipped
    assert ("2026111909", "run missing") in choice.skipped
    assert choice.lead_from_run_start_h == 7.0


def test_between_hours_needs_both_hours() -> None:
    issue = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    choice = select_run_for_issue(issue, issue + timedelta(hours=2, minutes=45))
    assert choice is not None and (choice.fh_lo + 1 == choice.fh_hi)
    assert choice.weight_hi == pytest.approx(0.75)


def test_known_availability_overrides_the_model_and_unavailable_skips() -> None:
    issue = datetime(2026, 11, 19, 10, 50, tzinfo=UTC)
    run10 = datetime(2026, 11, 19, 10, tzinfo=UTC)
    run09 = run10 - timedelta(hours=1)

    def known(run: datetime, fh: int):
        if run == run10:
            return run10 + timedelta(minutes=45 + fh), "live-s3-lastmodified"
        return None

    choice = select_run_for_issue(issue, issue + timedelta(hours=1), availability=known)
    assert choice is not None and choice.run_id == "2026111910"
    assert choice.availability_source == "live-s3-lastmodified"
    assert choice.availability_model is None

    def no_10z(run: datetime, fh: int):
        return Lookup.UNAVAILABLE if run == run10 else Lookup.USE_MODEL

    choice = select_run_for_issue(issue, issue + timedelta(hours=1), availability=no_10z)
    assert choice is not None and choice.run_start_utc < run09  # 09Z f02 is modelled at 11:00
    assert ("2026111910", "f01 not stored") in choice.skipped


def test_nothing_covers_the_valid_time() -> None:
    issue = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
    assert select_run_for_issue(issue, issue + timedelta(hours=60)) is None
    with pytest.raises(ValueError):
        select_run_for_issue(datetime(2026, 11, 19), issue)


def _doc(*, live: bool, fetched: str, hours: dict[int, str]) -> dict:
    return {
        "live": live,
        "fetch_finished_utc": fetched,
        "hours": [
            {
                "fh": fh,
                "availability_utc": when,
                "availability_source": "live-s3-lastmodified" if live else "modeled",
            }
            for fh, when in hours.items()
        ],
    }


def test_availability_from_document() -> None:
    live = _doc(
        live=True,
        fetched="2026-11-19T07:20:00.000Z",
        hours={0: "2026-11-19T06:53:49Z", 18: "2026-11-19T07:26:00Z"},
    )
    out = availability_from_document(live)
    assert out[0] == (datetime(2026, 11, 19, 7, 20, tzinfo=UTC), "live-fetch-finished")
    assert out[18] == (datetime(2026, 11, 19, 7, 26, tzinfo=UTC), "live-s3-lastmodified")
    plain = availability_from_document(live, use_fetch_time=False)
    assert plain[0] == (datetime(2026, 11, 19, 6, 53, 49, tzinfo=UTC), "live-s3-lastmodified")
    archived = _doc(live=False, fetched="2026-11-22T07:20:00Z", hours={0: "2026-11-19T06:52Z"})
    assert availability_from_document(archived) == {0: Lookup.USE_MODEL}  # no stamps recorded
    archived["hours"][0].update(
        grib_last_modified_utc="2026-11-19T06:51:56Z", idx_last_modified_utc="2026-11-19T06:52:00Z"
    )
    assert availability_from_document(archived) == {
        0: ArchivedStamps(
            datetime(2026, 11, 19, 6, 51, 56, tzinfo=UTC), datetime(2026, 11, 19, 6, 52, tzinfo=UTC)
        )
    }


@pytest.fixture
def stored(fake, bucket, storage, manifests, small_grid):
    """Factory: extract one run of the fake bucket into storage at a chosen clock time."""

    def make(run: datetime, *, max_fh: int, clock: datetime) -> dict:
        fake.add_run(run, fields=["VIS:surface", "HGT:surface"], hours=range(19))
        extractor = Extractor(
            storage, bucket, {RAINIER: manifests[RAINIER]}, grid=small_grid, clock=lambda: clock
        )
        options = ExtractOptions(fields=tuple(resolve_fields(["VIS", "TERRAIN"])), max_fh=max_fh)
        return extractor.extract(run, options).documents[RAINIER]

    return make


def test_stored_runs_only_and_live_hours_wait_for_the_fetch(stored, storage) -> None:
    """Review finding 3 (T2): no file, or no such hour in it, means the run is unavailable."""
    r07 = datetime(2026, 11, 19, 7, tzinfo=UTC)
    issue = datetime(2026, 11, 19, 9, 40, tzinfo=UTC)
    doc = stored(r07, max_fh=4, clock=issue - timedelta(minutes=5))
    lookup = stored_availability(storage, RAINIER)
    assert lookup(r07 + timedelta(hours=1), 2) is Lookup.UNAVAILABLE  # no 08Z file
    assert lookup(r07, 6) is Lookup.UNAVAILABLE  # the 07Z file holds f00-f04
    fetched = datetime(2026, 11, 19, 9, 35, tzinfo=UTC)
    assert doc["fetch_finished_utc"] == "2026-11-19T09:35:00.000Z"
    assert lookup(r07, 3) == (fetched, "live-fetch-finished")  # S3 had it at 07:53:04

    choice = select_stored_run(storage, RAINIER, issue, issue + timedelta(hours=1))
    assert choice is not None and choice.run_id == "2026111907"
    assert (choice.fh_lo, choice.fh_hi) == (3, 4)
    assert choice.availability_utc == fetched
    assert choice.availability_source == "live-fetch-finished"
    assert ("2026111908", "f02 not stored") in choice.skipped
    assert choice.lead_h == pytest.approx((issue + timedelta(hours=1) - fetched).seconds / 3600)
    # Before the fetch finished, Lookout did not have the run, whatever S3 said.
    early = select_stored_run(storage, RAINIER, fetched - timedelta(minutes=1), issue)
    assert early is None
    # A valid time past the stored hours: 07Z cannot serve it and nothing older is stored.
    late_valid = datetime(2026, 11, 19, 13, tzinfo=UTC)
    assert select_stored_run(storage, RAINIER, issue, late_valid) is None
    exists = stored_run_exists(storage, RAINIER)
    assert exists(r07) and not exists(r07 + timedelta(hours=1))


def test_archived_files_are_remodelled_from_the_current_log(stored, storage) -> None:
    """Review finding 1 (T3): a stored modelled time is never reused once the log knows more."""
    run = datetime(2026, 11, 16, 7, tzinfo=UTC)
    doc = stored(run, max_fh=6, clock=datetime(2026, 11, 19, 9, 40, tzinfo=UTC))
    hour5 = doc["hours"][5]
    assert hour5["availability_source"] == "modeled"
    assert hour5["availability_model"]["method"] == "conservative-fallback"
    assert hour5["availability_utc"] == "2026-11-16T09:00:00Z"  # run start + 120 min
    # The fake published f05 at 07:55:00 (.idx 07:55:04): earlier than the model.
    assert hour5["availability_model"]["s3_floor_utc"] == "2026-11-16T07:55:04Z"
    stamps = ArchivedStamps(run + timedelta(minutes=55), run + timedelta(minutes=55, seconds=4))
    assert stored_availability(storage, RAINIER)(run, 5) == stamps

    # 25 live 07Z runs later, f05 of 07Z has a p95 of 73.5 min: selection uses it.
    later = LagModel.from_events(
        e for k in range(25) for e in _events(f"202611{k + 1:02d}07", {5: 73.5 * 60})
    )
    issue = run + timedelta(minutes=73.5)
    choice = select_stored_run(storage, RAINIER, issue, run + timedelta(hours=5), lag_model=later)
    assert choice is not None and choice.run_id == "2026111607"
    assert choice.availability_utc == run + timedelta(minutes=73.5)
    assert choice.availability_source == "modeled"
    assert choice.availability_model is not None
    assert choice.availability_model["method"] == "p95-cycle-hour"
    assert choice.availability_model["s3_floor_utc"] == "2026-11-16T07:55:04Z"
    # The stored file still says 09:00 (what was known when it was written).
    assert (
        storage.read_json(f"forecast/hrrr/{RAINIER}/2026111607z.json")["hours"][5][
            "availability_utc"
        ]
        == "2026-11-16T09:00:00Z"
    )


def test_survey_lags_enter_the_model_and_live_wins_for_the_same_hour(tmp_path) -> None:
    """D-027: survey lags count like live ones; per (run, hour) the live one is kept."""
    storage = LocalStorage(tmp_path / "data", check=False)
    survey = [
        {
            "event": "lag",
            "run": f"202611{k + 1:02d}23",
            "fh": 9,
            "lag_s": 111.0 * 60,
            "method": "survey",
        }
        for k in range(20)
    ]
    storage.append_jsonl_many("forecast_log/lag_survey/2026-11-21.jsonl", survey)
    # The live worker saw 2026110123 f09 at 110 min; that lag replaces the survey's.
    storage.append_jsonl(
        "forecast_log/2026-11-01.jsonl", dict(survey[0], lag_s=110.0 * 60, method=None)
    )
    model = LagModel.from_storage(storage)
    z23 = datetime(2026, 11, 22, 23, tzinfo=UTC)
    assert model.lag_s(z23, 9) == (111.0 * 60, "p95-cycle-hour", 20)
    assert model.describe(z23, 9)["samples_by_method"] == {"live": 1, "survey": 19}
    assert model.lags_by_method == {"live": 1, "survey": 19}
    live_only = LagModel.from_storage(storage, methods={"live"})
    assert live_only.lag_s(z23, 9) == (FALLBACK_LAG_MIN * 60, "conservative-fallback", 1)
    with pytest.raises(ValueError, match="unknown lag method"):
        LagModel.from_events([dict(survey[0], method="guess")])


def test_cell_summary_counts_the_576_cells() -> None:
    empty = LagModel().cell_summary()
    assert empty["cells"] == 24 * 19 + 4 * 30 == 576
    assert empty["left_fallback"] == 0 and empty["fallback_cells_at_fallback_lag"] == 576
    events = [e for k in range(20) for e in _events(f"20261118{k:02d}", {0: 3000.0})]
    events += _events("2026111715", {12: 131.2 * 60})  # later than the fallback
    summary = LagModel.from_events(events).cell_summary()
    # f00 has 20 pooled lags: the 20 cycles with a lag of their own leave the fallback.
    assert summary["by_method"]["p95-forecast-hour-or-cycle-max"] == 20
    assert summary["left_fallback"] == 20
    assert summary["fallback_cells_above_fallback_lag"] == 1
