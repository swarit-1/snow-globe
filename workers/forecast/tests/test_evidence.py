"""The evidence commands: datum statistics on synthetic arrays, and the lag survey's log."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import numpy as np
import pytest

from snow_globe_forecast.availability import LagModel
from snow_globe_forecast.bucket import BucketError, HrrrBucket
from snow_globe_forecast.evidence import (
    _by_band,
    cloud_base_pressure_check,
    haversine_km,
    implied_height_m,
    lag_check,
    lag_survey,
)
from snow_globe_forecast.hrrr import run_prefix
from snow_globe_forecast.laglog import SurveyLogger, read_lag_log

from .conftest import FIXTURES


def test_haversine() -> None:
    assert haversine_km(0.0, 0.0, 0.0, 1.0) == pytest.approx(111.195, abs=0.01)
    assert haversine_km(46.0, 359.5, 46.0, -0.5) == pytest.approx(0.0, abs=1e-9)


def _pressure_at(z: np.ndarray, psfc: np.ndarray, t2m: np.ndarray) -> np.ndarray:
    """Pressure at height z above the surface in a standard-lapse-rate atmosphere."""
    return psfc * (1 - 0.0065 * z / t2m) ** (9.80665 / (287.05 * 0.0065))


def test_implied_height_is_close_for_low_and_high_bases() -> None:
    psfc = np.array([101325.0, 101325.0, 80000.0])
    t2m = np.array([288.15, 288.15, 270.0])
    z = np.array([300.0, 8000.0, 6000.0])
    got = implied_height_m(psfc, _pressure_at(z, psfc, t2m), t2m)
    assert np.all(np.abs(got - z) / z < 0.01)


def test_cloud_base_pressure_check_separates_valid_and_invalid_bases() -> None:
    terrain = np.array([0.0, 1500.0, 2000.0, 2500.0, 10.0])
    psfc = np.array([101325.0, 85000.0, 80000.0, 75000.0, 101000.0])
    t2m = np.full(5, 280.0)
    implied = np.array([500.0, 8000.0, 8000.0, 50.0, 7000.0])
    pbase = _pressure_at(implied, psfc, t2m)
    height = terrain + implied
    height[2] = 900.0  # below the 2000 m terrain although the pressure says 8 km up
    height[4] = 600.0  # above the terrain, but 6.4 km lower than the pressure says
    missing = np.array([False, False, False, True, False])
    out = cloud_base_pressure_check(
        (height, missing), (pbase, np.zeros(5, bool)), terrain, psfc, t2m
    )
    assert out["cells_with_height_and_pressure"] == 4
    assert out["below_terrain_cells"] == 1
    assert out["below_terrain_but_above_ground_by_pressure"] == 1
    assert out["grossly_inconsistent_cells"] == 2  # the valid 8 km base passes
    assert out["grossly_inconsistent_below_terrain"] == 1
    assert out["grossly_inconsistent_at_or_above_terrain"] == 1


def test_by_band() -> None:
    terrain = np.array([100.0, 600.0, 2100.0, 2600.0])
    values = np.array([200.0, 500.0, 2000.0, 3000.0])
    out = _by_band(values, terrain, np.ones(4, bool))
    assert out["500..1000 m"] == {"valid": 1, "below": 1, "fraction": 1.0}
    assert out["2000..2500 m"]["below"] == 1
    assert out["2500..6000 m"]["below"] == 0
    assert out["1000..1500 m"]["fraction"] is None


def test_a_logging_survey_keeps_what_it_listed_when_a_listing_fails(fake, storage) -> None:
    """The survey flushes its log on the way out, so a re-run only lists the rest."""
    r05 = datetime(2026, 11, 19, 5, tzinfo=UTC)
    r06 = r05.replace(hour=6)
    fake.add_run(r05, fields=["VIS:surface"], hours=range(19))
    fake.add_run(r06, fields=["VIS:surface"], hours=range(49))
    attempts = {"06Z": 0}

    def flaky(request: httpx.Request) -> httpx.Response:  # every 06Z listing answers 503
        if request.url.params.get("prefix", "") == run_prefix(r06):
            attempts["06Z"] += 1
            return httpx.Response(503, text="slow down")
        return fake.handler(request)

    stamp = datetime(2026, 11, 20, 3, tzinfo=UTC)
    with HrrrBucket(transport=httpx.MockTransport(flaky), sleep=lambda s: None) as bucket:
        logger = SurveyLogger(storage, clock=lambda: stamp, flush_every=100)
        with pytest.raises(BucketError):
            lag_survey(bucket, r05, r05.replace(hour=7), pause_s=0, logger=logger)
    assert attempts["06Z"] == 4  # one listing, four attempts
    assert read_lag_log(storage).by_method == {"survey": 19}
    assert logger.report()["not_flushed"] == 0


#: Listings recorded by `snow-globe-forecast lag-check 2024011412 2024011418 --save-listings`
#: on 2026-11-20 at 03:13 UTC (one ListObjectsV2 answer each, 24,393 bytes, 98 objects).
ARCHIVED = {
    "hrrr.20240114/conus/hrrr.t12z.wrfsfcf": FIXTURES / "list_20240114_t12z_full.xml",
    "hrrr.20240114/conus/hrrr.t18z.wrfsfcf": FIXTURES / "list_20240114_t18z_full.xml",
}


def test_archived_runs_keep_their_publication_stamps() -> None:
    """Evidence for D-027: S3 still holds the January 2024 runs' publication times.

    1,040 days after the runs, every stamp lies within 2 h of the run start, with the same
    shape as live runs (f00 about 50 min, f48 about 108 min), and none is later than the
    conservative fallback.
    """

    def replay(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ARCHIVED[request.url.params["prefix"]].read_bytes())

    runs = [datetime(2024, 1, 14, 12, tzinfo=UTC), datetime(2024, 1, 14, 18, tzinfo=UTC)]
    with HrrrBucket(transport=httpx.MockTransport(replay)) as bucket:
        out = lag_check(bucket, runs, LagModel())
    twelve, eighteen = out["runs"]
    assert (twelve["ready_hours"], twelve["missing_hours"]) == (49, [])
    assert twelve["lag_min"] == {"min": 49.88, "median": 81.57, "max": 107.22}
    assert twelve["earliest_last_modified"] == "2024-01-14T12:49:49Z"
    assert twelve["latest_last_modified"] == "2024-01-14T13:47:13Z"
    assert eighteen["lag_min"] == {"min": 50.83, "median": 82.08, "max": 108.95}
    # The pair's hours: 12Z f31-f32 (chosen under the fallback) and 18Z f25-f26.
    by_fh = {row["fh"]: row for row in eighteen["hours"]}
    assert by_fh[26]["idx_last_modified"] == "2024-01-14T19:24:33Z"
    assert by_fh[25]["lag_min"] == 83.53 and by_fh[26]["lag_min"] == 84.55
    for run in out["runs"]:
        assert run["hours_later_than_model"] == 0
        assert {row["model_method"] for row in run["hours"]} == {"conservative-fallback"}
    assert out["transfer"]["requests"] == {"listing": 2}
