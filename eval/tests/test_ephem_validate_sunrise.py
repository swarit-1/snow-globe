"""Sunrise and sunset: the crossing search, NOAA's hour-angle method, ±1 min (PRD §8.4)."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import needs_oculus

from snow_globe_eval.ephem_validate.plan import Site
from snow_globe_eval.ephem_validate.sunrise import (
    EVENT_TOLERANCE_S,
    SUNRISE_SUNSET_DEG,
    attach_skyfield,
    event_rows,
    find_crossing,
    local_day_bounds,
    noaa_event,
    summarize_events,
)
from snow_globe_eval.noaa_solar import solar_position

RAINIER = Site("rainier.test", 46.78455, -121.74065, 1643.0, "America/Los_Angeles", 348.8)
PHOENIX = Site("grand-canyon.test", 36.065981, -112.118236, 2150.0, "America/Phoenix", 315.0)


def test_local_day_bounds_follow_dst() -> None:
    zone = ZoneInfo("America/Los_Angeles")
    start, end = local_day_bounds(zone, date(2026, 11, 1))
    assert end - start == timedelta(hours=25)
    start, end = local_day_bounds(zone, date(2027, 3, 14))
    assert end - start == timedelta(hours=23)
    start, _ = local_day_bounds(zone, date(2026, 11, 21))
    assert start == datetime(2026, 11, 21, 8, tzinfo=UTC)


def test_find_crossing_on_an_analytic_curve() -> None:
    """A sine with a known root: the search finds it to the millisecond, both directions."""
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    period = 86400.0

    def altitude(moment: datetime) -> float:
        x = (moment - t0).total_seconds() / period * 2.0 * math.pi
        return 40.0 * math.sin(x)

    root_up = t0 + timedelta(seconds=period * math.asin(10.0 / 40.0) / (2 * math.pi))
    found = find_crossing(altitude, 10.0, True, t0, t0 + timedelta(days=1))
    assert found is not None and abs((found - root_up).total_seconds()) < 0.002
    root_down = t0 + timedelta(seconds=period * (math.pi - math.asin(10.0 / 40.0)) / (2 * math.pi))
    found = find_crossing(altitude, 10.0, False, t0, t0 + timedelta(days=1))
    assert found is not None and abs((found - root_down).total_seconds()) < 0.002
    assert find_crossing(altitude, 50.0, True, t0, t0 + timedelta(days=1)) is None
    assert find_crossing(altitude, 0.0, True, t0, t0) is None


@pytest.mark.parametrize("rising", [True, False])
def test_noaa_event_is_where_noaa_elevation_is_minus_0833(rising: bool) -> None:
    """The hour-angle solution agrees with NOAA's own elevation formula at that instant."""
    for day in (date(2026, 7, 1), date(2026, 12, 21), date(2027, 3, 20)):
        when = noaa_event(day, RAINIER.lat_deg, RAINIER.lon_deg, SUNRISE_SUNSET_DEG, rising)
        assert when is not None
        elevation = solar_position(when, RAINIER.lat_deg, RAINIER.lon_deg).elevation_deg
        assert elevation == pytest.approx(SUNRISE_SUNSET_DEG, abs=0.005)
        assert when.astimezone(ZoneInfo(RAINIER.time_zone)).date() == day


def test_noaa_event_none_in_polar_night() -> None:
    assert noaa_event(date(2026, 12, 21), 80.0, 15.0, SUNRISE_SUNSET_DEG, True) is None


@needs_oculus
def test_ours_against_noaa_within_a_minute() -> None:
    rows = event_rows([RAINIER, PHOENIX], date(2026, 11, 20), date(2026, 11, 22))
    assert len(rows) == 2 * 3 * 2
    for row in rows:
        assert abs(row["ours_minus_noaa_s"]) <= EVENT_TOLERANCE_S, row
    summary = summarize_events(rows)
    assert summary["missing_ours"] == 0 and summary["missing_noaa"] == 0
    assert summary["all"]["ours_minus_noaa_s"]["beyond_60_s"] == 0
    assert set(summary["by_site"]) == {RAINIER.viewpoint_id, PHOENIX.viewpoint_id}


@needs_oculus
def test_skyfield_events_pair_by_site_and_kind() -> None:
    rows = event_rows([RAINIER], date(2026, 11, 21), date(2026, 11, 21))
    sunrise = next(r for r in rows if r["kind"] == "sunrise")
    events = [
        {"site": RAINIER.viewpoint_id, "kind": "rise", "unix_ms": sunrise["ours_ms"] + 500.0},
        {"site": RAINIER.viewpoint_id, "kind": "set", "unix_ms": sunrise["ours_ms"] + 4 * 3.6e6},
        {"site": "elsewhere", "kind": "rise", "unix_ms": sunrise["ours_ms"]},
    ]
    attach_skyfield(rows, events)
    assert sunrise["ours_minus_skyfield_s"] == pytest.approx(-0.5, abs=1e-6)
    sunset = next(r for r in rows if r["kind"] == "sunset")
    assert "skyfield_utc" not in sunset  # about 5 h before our sunset: not paired
