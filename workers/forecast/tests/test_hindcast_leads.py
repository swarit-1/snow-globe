"""Design P's live needs: forecast hours per run and MB per day by lead (PRD §13.2, §8.3)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from snow_globe_forecast.availability import LagModel
from snow_globe_forecast.hindcast.leads import (
    FIELD_PROFILES,
    STANDIN_FETCH_DELAY_S,
    daytime_valid_times,
    hours_for_leads,
    lag_scenarios,
    mb_per_day,
    price_tables,
    valid_times_every,
)
from snow_globe_forecast.hindcast.sun import solar_altitude_deg

DAY = datetime(2026, 12, 20, tzinfo=UTC)
ALL_DAY = valid_times_every(DAY, DAY + timedelta(hours=23, minutes=55), 5.0)


def _constant(minutes: float) -> dict:
    return {"constant": (lambda run, fh: minutes * 60.0, 0.0)}


def _hours(estimate: dict, lead: str) -> set[int]:
    return {h for hours in estimate["per_lead"][lead]["hours_by_cycle"].values() for h in hours}


def test_valid_time_grid() -> None:
    assert len(ALL_DAY) == 288 and ALL_DAY[1] - ALL_DAY[0] == timedelta(minutes=5)


def test_leads_with_a_constant_55_minute_lag() -> None:
    """Every hour published 55 min after its run: a lead L uses fh in [L + 55 min, L + 1 h 55 min),
    so its two bracketing hours run from floor(L + 0.92) to ceil(L + 1.92)."""
    est = hours_for_leads([1.0, 12.0, 24.0, 48.0], _constant(55.0), ALL_DAY)
    assert _hours(est, "1") == {1, 2, 3}
    assert _hours(est, "12") == {12, 13, 14}
    # Only the synoptic runs reach 24 h ahead, so only they serve the 24 h lead.
    assert sorted(est["per_lead"]["24"]["hours_by_cycle"]) == ["00", "06", "12", "18"]
    assert _hours(est, "24") == set(range(24, 32))
    # f48 is HRRR's last hour, and nothing is published at its run start: 48 h is never served.
    assert est["per_lead"]["48"]["hours_by_cycle"] == {}
    assert est["per_lead"]["48"]["valid_times_unserved"] == 288
    lead = est["per_lead"]["24"]["lead_h_actual"]
    assert lead["min"] >= 24.0 and lead["max"] < 30.0 + 1e-9
    assert est["hours_by_cycle"]["00"] == sorted({1, 2, 3, 12, 13, 14, *range(24, 32)})
    assert est["hours_by_cycle"]["01"] == [1, 2, 3, 12, 13, 14]


def test_a_later_lag_needs_later_hours() -> None:
    early = hours_for_leads([3.0], _constant(50.0), ALL_DAY)
    late = hours_for_leads([3.0], {"late": (lambda run, fh: 50.0 * 60.0, 1800.0)}, ALL_DAY)
    assert _hours(early, "3") == {3, 4, 5}
    assert _hours(late, "3") == {4, 5, 6}  # 50 min + a 30 min delay: fh in [4.33, 5.33)
    both = hours_for_leads(
        [3.0], {"a": (lambda r, f: 3000.0, 0.0), "b": (lambda r, f: 3000.0, 1800.0)}, ALL_DAY
    )
    assert _hours(both, "3") == {3, 4, 5, 6}  # the union over the scenarios


def test_lag_scenarios_from_logged_lags() -> None:
    events = [
        {"event": "lag", "run": "2026112012", "fh": 2, "lag_s": 3000.0},
        {"event": "lag", "run": "2026111912", "fh": 2, "lag_s": 3400.0},
        {"event": "lag", "run": "2026111913", "fh": 2, "lag_s": 2900.0},
    ]
    model = LagModel.from_events(events)
    scenarios = lag_scenarios(events, model)
    earliest, delay = scenarios["earliest"]
    assert delay == 0.0
    assert earliest(datetime(2026, 12, 20, 12, tzinfo=UTC), 2) == 3000.0  # the cell's least
    assert earliest(datetime(2026, 12, 20, 5, tzinfo=UTC), 2) == 2900.0  # the hour's least
    assert earliest(datetime(2026, 12, 20, 5, tzinfo=UTC), 7) == model.lag_s(DAY, 7)[0]
    conservative, delay = scenarios["p95-model"]
    assert delay == STANDIN_FETCH_DELAY_S
    assert conservative(datetime(2026, 12, 20, 12, tzinfo=UTC), 2) == 7200.0  # few lags: 120 min


def test_daytime_valid_times_keep_the_sun_above_3_deg() -> None:
    cams = [(46.78783, -121.73704)]
    times = daytime_valid_times(cams, date(2026, 12, 20), date(2026, 12, 20), step_min=10.0)
    assert times and all(solar_altitude_deg(t, *cams[0]) >= 3.0 for t in times)
    assert 6 * 6 < len(times) < 8 * 6  # about 7 h of Sun above 3 deg in late December
    hours = {t.hour for t in times}
    assert hours <= set(range(16, 24))


def test_mb_per_day_prices_hours_and_one_terrain_per_run() -> None:
    table = {True: {1: 1e6, 2: 2e6}, False: {1: 3e6}}
    out = mb_per_day({"00": [1, 2], "01": [1], "02": []}, table, 0.5e6)
    assert out["mb_by_cycle"] == {"00": 3.5, "01": 3.5, "02": 0.0}
    assert out["mb_per_day"] == 7.0 and out["runs_per_day"] == 2
    assert out["forecast_hours_per_day"] == 3
    missing = mb_per_day({"01": [1, 5]}, table, 0.0)
    assert missing["unpriced_hours"] == ["01:f05"]
    with pytest.raises(ValueError):
        mb_per_day({"01": [19]}, table, 0.0)


def _sizes(hours: list[int], per_field: dict[str, dict[int, int]]) -> dict:
    """A ``plan_run``-shaped result (only the keys ``price_tables`` reads)."""
    return {"hours": hours, "missing_hours": [], "bytes_by_field_by_fh": per_field}


def test_price_tables_sum_a_profile_and_the_terrain_once() -> None:
    syn = datetime(2026, 11, 22, 0, tzinfo=UTC)
    other = datetime(2026, 11, 22, 7, tzinfo=UTC)
    plans = {
        syn: _sizes(
            [0, 1],
            {
                "VIS:surface": {0: 100, 1: 110},
                "HGT:cloud top": {1: 50},  # absent from the f00 inventory
                "HGT:surface": {0: 1000},  # the terrain, with the first hour only
            },
        ),
        other: _sizes(
            [0, 1],
            {
                "VIS:surface": {0: 300, 1: 310},
                "HGT:cloud top": {0: 7, 1: 9},
                "HGT:surface": {0: 3000},
            },
        ),
    }
    table, terrain, detail = price_tables(plans, ["VIS:surface", "HGT:surface"])
    assert table == {True: {0: 100.0, 1: 110.0}, False: {0: 300.0, 1: 310.0}}
    assert terrain == 2000.0 and detail["absent"] == {}
    assert detail["run_total_bytes"] == {"2026112200": 1210, "2026112207": 3610}
    table, _, detail = price_tables(plans, ["VIS:surface", "HGT:cloud top", "HGT:surface"])
    assert table[True] == {0: 100.0, 1: 160.0} and table[False] == {0: 307.0, 1: 319.0}
    assert detail["absent"] == {"HGT:cloud top": 1}
    with pytest.raises(ValueError, match="not planned"):
        price_tables(plans, ["VIS:surface", "PRATE:surface"])


def test_field_profiles_grow_from_the_standin_to_the_catalogue() -> None:
    standin, scene, catalogue = (
        FIELD_PROFILES[k] for k in ("standin-8", "scene-12", "catalogue-22")
    )
    assert len(standin) == 8 and len(scene) == 12 and len(catalogue) == 22
    assert set(standin) < set(scene) < set(catalogue)
    assert all("HGT:surface" in fields for fields in FIELD_PROFILES.values())
