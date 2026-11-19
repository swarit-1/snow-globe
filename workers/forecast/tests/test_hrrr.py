"""Bucket layout and run arithmetic (PRD §8.3)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from lookout_forecast import hrrr


def test_run_ids_round_trip() -> None:
    run = hrrr.parse_run_id("2026111906")
    assert run == datetime(2026, 11, 19, 6, tzinfo=UTC)
    assert hrrr.format_run_id(run) == "2026111906"
    assert hrrr.parse_run_id("2026111906z") == run
    with pytest.raises(ValueError):
        hrrr.parse_run_id("20261119")


def test_synoptic_runs_reach_48_hours() -> None:
    assert [
        hrrr.max_fh(datetime(2026, 11, 19, h, tzinfo=UTC)) for h in (0, 5, 6, 7, 12, 18, 23)
    ] == [
        48,
        18,
        48,
        18,
        48,
        48,
        18,
    ]
    run = datetime(2026, 11, 19, 7, tzinfo=UTC)
    assert hrrr.run_hours(run) == list(range(19))
    assert hrrr.run_hours(run, 2) == [0, 1, 2]
    assert hrrr.run_hours(datetime(2026, 11, 19, 6, tzinfo=UTC), 60)[-1] == 48


def test_keys_and_urls() -> None:
    run = datetime(2026, 11, 19, 6, tzinfo=UTC)
    assert hrrr.object_key(run, 7) == "hrrr.20261119/conus/hrrr.t06z.wrfsfcf07.grib2"
    assert hrrr.idx_key(run, 7).endswith(".grib2.idx")
    assert hrrr.run_prefix(run) == "hrrr.20261119/conus/hrrr.t06z.wrfsfcf"
    assert hrrr.day_prefix(date(2026, 11, 19)) == "hrrr.20261119/conus/"
    assert hrrr.object_url("a/b") == "https://noaa-hrrr-bdp-pds.s3.amazonaws.com/a/b"
    assert hrrr.valid_time(run, 30) == run + timedelta(hours=30)


def test_parse_object_key() -> None:
    name = hrrr.parse_object_key("hrrr.20261119/conus/hrrr.t23z.wrfsfcf18.grib2.idx")
    assert name is not None and name.fh == 18 and name.is_idx
    assert name.run_start == datetime(2026, 11, 19, 23, tzinfo=UTC)
    assert hrrr.parse_object_key("hrrr.20261119/conus/hrrr.t23z.wrfprsf18.grib2") is None
    assert hrrr.parse_object_key("hrrr.20261119/conus/hrrr.t23z.wrfsubhf01.grib2") is None


def test_run_start_must_be_aware_and_on_the_hour() -> None:
    with pytest.raises(ValueError):
        hrrr.as_run_start(datetime(2026, 11, 19, 6))
    with pytest.raises(ValueError):
        hrrr.as_run_start(datetime(2026, 11, 19, 6, 30, tzinfo=UTC))
    est = timezone(timedelta(hours=-5))
    assert hrrr.as_run_start(datetime(2026, 11, 19, 1, tzinfo=est)).hour == 6
