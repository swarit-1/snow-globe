"""Daylight from the ephemeris only (PRD §8.6)."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FixedEphemeris, needs_oculus
from lookout_common.manifest import load_manifest

from lookout_eval import daylight
from lookout_eval.daylight import (
    CLOUD_FRACTION_MIN_SUN_ALT_DEG,
    P1_MIN_SUN_ALT_DEG,
    day_mask,
    is_day,
    viewpoint_day_mask,
    viewpoint_is_day,
)
from lookout_eval.ephemeris import NoaaBackend, refraction_deg

T = datetime(2026, 11, 17, 20, 0, tzinfo=UTC)


def test_prd_thresholds() -> None:
    assert P1_MIN_SUN_ALT_DEG == 3.0
    assert CLOUD_FRACTION_MIN_SUN_ALT_DEG == 10.0


def test_threshold_is_inclusive() -> None:
    assert is_day(T, 0, 0, min_sun_alt_deg=3.0, ephemeris=FixedEphemeris(3.0))
    assert not is_day(T, 0, 0, min_sun_alt_deg=3.0, ephemeris=FixedEphemeris(2.9999))


def test_geometric_by_default_refracted_on_request() -> None:
    eph = FixedEphemeris(2.85)
    assert 2.85 + refraction_deg(2.85) > 3.0
    assert not is_day(T, 0, 0, min_sun_alt_deg=3.0, ephemeris=eph)
    assert is_day(T, 0, 0, min_sun_alt_deg=3.0, refracted=True, ephemeris=eph)


def test_no_function_takes_an_image() -> None:
    for name in daylight.__all__:
        obj = getattr(daylight, name)
        if callable(obj):
            params = inspect.signature(obj).parameters
            assert not {"image", "frame", "pixels", "brightness"} & set(params), name


def test_point_reyes_noon_and_night_with_noaa() -> None:
    vp = load_manifest("point-reyes.beach")
    noaa = NoaaBackend()
    noon = T  # 12:00 PST, Sun ~ 32.9 deg
    night = datetime(2026, 11, 18, 8, 0, tzinfo=UTC)  # 00:00 PST
    for threshold in (P1_MIN_SUN_ALT_DEG, CLOUD_FRACTION_MIN_SUN_ALT_DEG):
        assert viewpoint_is_day(vp, noon, min_sun_alt_deg=threshold, ephemeris=noaa)
        assert not viewpoint_is_day(vp, night, min_sun_alt_deg=threshold, ephemeris=noaa)


def test_the_two_thresholds_differ_in_the_morning() -> None:
    vp = load_manifest("point-reyes.beach")
    noaa = NoaaBackend()
    t = datetime(2026, 11, 17, 15, 30, tzinfo=UTC)  # 07:30 PST, Sun ~ 5.3 deg
    assert viewpoint_is_day(vp, t, min_sun_alt_deg=P1_MIN_SUN_ALT_DEG, ephemeris=noaa)
    assert not viewpoint_is_day(
        vp, t, min_sun_alt_deg=CLOUD_FRACTION_MIN_SUN_ALT_DEG, ephemeris=noaa
    )


def test_masks_have_one_entry_per_time() -> None:
    times = [T + timedelta(hours=h) for h in range(24)]
    mask = day_mask(times, 37.99604, -123.02, min_sun_alt_deg=3.0, ephemeris=NoaaBackend())
    assert mask.shape == (24,) and mask.dtype == bool
    assert 8 <= int(mask.sum()) <= 11  # about 9.5 h above 3 deg in mid-November


@needs_oculus
def test_viewpoint_mask_with_oculus_matches_noaa_away_from_threshold() -> None:
    vp = load_manifest("rainier.paradise-mountain")
    times = [T + timedelta(minutes=20 * k) for k in range(72)]
    a = viewpoint_day_mask(vp, times, min_sun_alt_deg=3.0)
    b = viewpoint_day_mask(vp, times, min_sun_alt_deg=3.0, ephemeris=NoaaBackend())
    assert (a == b).mean() == pytest.approx(1.0)
