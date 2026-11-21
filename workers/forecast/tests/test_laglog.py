"""The live and survey lag logs (PRD §8.3, §13.9; D-027), on a recorded S3 listing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from snow_globe_common.storage import LocalStorage

from snow_globe_forecast.availability import LagModel
from snow_globe_forecast.bucket import parse_list_page
from snow_globe_forecast.discovery import RunListing, group_runs
from snow_globe_forecast.laglog import (
    MAX_LAG_S,
    METHOD_LIVE,
    METHOD_SURVEY,
    SurveyLogger,
    lag_events,
    read_lag_events,
    read_lag_log,
    survey_log_key,
    usable_lag,
)

from .conftest import FIXTURES

R07 = datetime(2026, 11, 19, 7, tzinfo=UTC)
#: When tests/fixtures/list_t07z_full.xml was listed (19 Nov, 09:45 UTC).
LISTED = datetime(2026, 11, 19, 9, 45, tzinfo=UTC)
LIVE_KEY = "forecast_log/2026-11-19.jsonl"


def _listing() -> RunListing:
    """The recorded ListObjectsV2 answer for run 2026111907 (all 19 hours on S3)."""
    page = parse_list_page((FIXTURES / "list_t07z_full.xml").read_bytes())
    return group_runs(page.objects, listed_utc=LISTED)[R07]


@pytest.fixture
def store(tmp_path) -> LocalStorage:
    return LocalStorage(tmp_path / "data", check=False)


def test_events_from_the_recorded_listing() -> None:
    listing = _listing()
    live = lag_events(listing)
    survey = lag_events(listing, method=METHOD_SURVEY)
    assert len(live) == len(survey) == 19
    first = live[0]
    assert first["method"] == "live" and "listed_utc" not in first
    # f00: GRIB2 07:53:48, .idx 07:53:52; the later stamp is the hour's availability.
    assert first["last_modified"] == "2026-11-19T07:53:52Z" and first["lag_s"] == 3232.0
    assert survey[0]["method"] == "survey" and survey[0]["listed_utc"] == "2026-11-19T09:45:00Z"
    assert {k: v for k, v in survey[0].items() if k not in {"method", "listed_utc"}} == {
        k: v for k, v in first.items() if k != "method"
    }
    with pytest.raises(ValueError, match="unknown lag method"):
        lag_events(listing, method="guess")


def test_live_wins_and_old_events_without_a_method_count_as_live(store) -> None:
    listing = _listing()
    old_style = [{k: v for k, v in e.items() if k != "method"} for e in lag_events(listing)]
    store.append_jsonl_many(LIVE_KEY, old_style[:3])  # as the 19 Nov worker wrote them
    store.append_jsonl_many(
        survey_log_key(LISTED), lag_events(listing, method=METHOD_SURVEY)
    )  # all 19 hours, three of them already logged live
    log = read_lag_log(store)
    assert log.by_method == {"live": 3, "survey": 16}
    assert log.duplicates == {"survey": 3}
    assert log.files == {"live": 1, "survey": 1}
    assert {e["fh"]: e["method"] for e in log.events if e["fh"] < 4} == {
        0: "live", 1: "live", 2: "live", 3: "survey"
    }  # fmt: skip
    assert log.summary()["runs"] == 1
    assert len(read_lag_events(store, methods={METHOD_LIVE})) == 3
    assert len(read_lag_events(store)) == 19


def test_a_live_event_replaces_a_survey_event_read_before_it(store) -> None:
    listing = _listing()
    labelled = dict(lag_events(listing, method=METHOD_SURVEY)[0])
    store.append_jsonl("forecast_log/2026-11-19.jsonl", labelled)  # a survey label in a live log
    store.append_jsonl("forecast_log/2026-11-20.jsonl", lag_events(listing)[0])
    log = read_lag_log(store)
    assert [e["method"] for e in log.events] == ["live"]
    assert log.duplicates == {"survey": 1}


def test_unusable_records_are_counted_not_used(store) -> None:
    good = lag_events(_listing())[0]
    store.append_jsonl_many(
        LIVE_KEY,
        [
            dict(good, run="20261119xx"),
            dict(good, run=2026111907),  # a number, not a run id
            dict(good, fh=-1),
            dict(good, fh=1.5),  # review finding: int(1.5) made this f01
            dict(good, fh=True),
            dict(good, fh="3"),
            dict(good, lag_s=-5.0),  # a clock error must never look early
            dict(good, lag_s=None),
            dict(good, lag_s=True),
            dict(good, lag_s=MAX_LAG_S + 0.001),  # a rewritten object's stamp, not a publication
            dict(good, method="guess"),
            {"event": "run", "run": "2026111907"},  # other events are not lags
            good,
            dict(good, fh=4.0, lag_s=MAX_LAG_S),  # an integral float and exactly 24 h are fine
        ],
    )
    store.append_jsonl(survey_log_key(LISTED), dict(good, fh=5, method="live"))  # mislabelled
    log = read_lag_log(store)
    assert log.invalid == 12 and len(log.events) == 2
    assert [e["fh"] for e in log.events] == [0, 4] and isinstance(log.events[1]["fh"], int)
    assert usable_lag(dict(good, fh=1.5)) is None
    assert usable_lag(good) == ("2026111907", 0, 3232.0)
    # The model drops what the reader would count invalid, whoever passes the events.
    model = LagModel.from_events([dict(good, fh=1.5), dict(good, lag_s=MAX_LAG_S + 1), good])
    assert model.pooled_samples == {0: 1}


def test_since_skips_earlier_days_of_both_logs(store) -> None:
    live, survey = lag_events(_listing()), lag_events(_listing(), method=METHOD_SURVEY)
    store.append_jsonl("forecast_log/2026-11-18.jsonl", live[0])
    store.append_jsonl("forecast_log/lag_survey/2026-11-18.jsonl", survey[1])
    store.append_jsonl("forecast_log/lag_survey/2026-11-20.jsonl", survey[2])
    store.put_bytes("forecast_log/notes.txt", b"not a log")
    assert len(read_lag_log(store).events) == 3
    since = datetime(2026, 11, 19, tzinfo=UTC)
    assert [e["fh"] for e in read_lag_log(store, since=since).events] == [2]


def test_survey_logger_logs_each_pair_once_and_compares_the_rest(store) -> None:
    listing = _listing()
    store.append_jsonl_many(LIVE_KEY, lag_events(listing, hours=[0, 1, 2]))
    live_before = store.get_bytes(LIVE_KEY)
    clock = datetime(2026, 11, 20, 3, tzinfo=UTC)
    logger = SurveyLogger(store, clock=lambda: clock)
    assert logger.add(listing) == 16
    logger.flush()
    report = logger.report()
    assert report["logged"] == 16 and report["keys"] == ["forecast_log/lag_survey/2026-11-20.jsonl"]
    assert report["already_logged"] == {"live": 3, "survey": 0}
    # The survey's stamps equal what the live worker logged for the same hours.
    assert report["agreement_with_logged"]["live"] == {
        "compared": 3, "equal": 3, "survey_later": 0, "survey_earlier": 0, "max_abs_s": 0.0
    }  # fmt: skip
    assert store.get_bytes(LIVE_KEY) == live_before  # never the live log
    # A second survey (a new logger reading the log) logs nothing: idempotent.
    again = SurveyLogger(store, clock=lambda: clock + timedelta(hours=1))
    assert again.add(listing) == 0
    again.flush()
    assert again.report()["already_logged"] == {"live": 3, "survey": 16}
    assert again.report()["agreement_with_logged"]["survey"]["equal"] == 16
    assert read_lag_log(store).by_method == {"live": 3, "survey": 16}


def test_survey_logger_flushes_in_batches(store) -> None:
    listing = _listing()
    later = RunListing(listing.run_start + timedelta(hours=24), {}, LISTED)  # nothing listed
    logger = SurveyLogger(store, clock=lambda: LISTED, flush_every=2)
    logger.add(listing)
    assert not store.exists(survey_log_key(LISTED))  # buffered
    logger.add(later)  # the second listing flushes the batch
    assert len(read_lag_log(store).events) == 19 and logger.report()["not_flushed"] == 0


def test_survey_log_key() -> None:
    assert survey_log_key(datetime(2026, 11, 20, 23, 59, tzinfo=UTC)) == (
        "forecast_log/lag_survey/2026-11-20.jsonl"
    )
    with pytest.raises(ValueError):
        survey_log_key(datetime(2026, 11, 20))
