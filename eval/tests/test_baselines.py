"""Baselines (PRD §13.6) on synthetic frame stores."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from synth import START, VP, hourly_records, label_by, make_store

from snow_globe_eval.baselines import (
    AnalogWeights,
    DevCase,
    RaySample,
    analog,
    choose_persistence,
    climatology,
    fit_climatology,
    local_standard_month_hour,
    persistence_days_back,
    persistence_latest,
    persistence_valid_minus_24h,
    s_rule,
    state_is_visible,
)
from snow_globe_eval.frames import FrameRecord
from snow_globe_eval.leakage import HeldOut, LeakageError

TZ = "America/Los_Angeles"


def test_state_mapping() -> None:
    assert state_is_visible("visible") is True
    assert state_is_visible("hidden") is False
    assert state_is_visible("in_cloud") is False
    assert state_is_visible("partial") is None
    assert state_is_visible(None) is None


def test_local_standard_time_ignores_dst() -> None:
    # 20:00 UTC on 1 July is 13:00 PDT but 12:00 PST
    assert local_standard_month_hour(datetime(2026, 7, 1, 20, tzinfo=UTC), TZ) == (7, 12)
    assert local_standard_month_hour(datetime(2026, 12, 1, 20, tzinfo=UTC), TZ) == (12, 12)
    # month follows the local standard date
    assert local_standard_month_hour(datetime(2026, 12, 1, 3, tzinfo=UTC), TZ) == (11, 19)


def _daylight_labels(state_fn):
    def fn(r: FrameRecord):
        if not 16 <= r.image_time.hour <= 23:
            return {}  # night: no label
        return {"summit": state_fn(r)}

    return fn


def test_persistence_latest_skips_unusable_frames() -> None:
    records = hourly_records(hours=48)
    labels = label_by(records, _daylight_labels(lambda r: "visible"))
    # the newest daytime frame before the issue is partial: (a) must skip it
    labels[records[23].key] = {"summit": "partial"}
    labels[records[22].key] = {"summit": "hidden"}
    store = make_store(records, labels)
    issue = START + timedelta(hours=30)  # 06:00 UTC, night
    p = persistence_latest(
        store.as_of(issue, purpose="t"), VP, valid_time=issue + timedelta(hours=24)
    )
    assert p.value is False and p.frames == (records[22].key,)
    assert p.detail["state"] == "hidden" and p.detail["age_h"] == pytest.approx(8.0)
    stale = persistence_latest(
        store.as_of(issue, purpose="t"),
        VP,
        valid_time=issue + timedelta(hours=24),
        max_age=timedelta(hours=6),
    )
    assert stale.value is None and "stale" in stale.flags


def test_persistence_latest_with_no_frames() -> None:
    store = make_store(hourly_records(hours=5), {})
    p = persistence_latest(
        store.as_of(START + timedelta(hours=4), purpose="t"),
        VP,
        valid_time=START + timedelta(hours=10),
    )
    assert p.value is None and p.flags == ("no-usable-frame-before-issue",)


def test_persistence_b_uses_only_frames_available_at_issue() -> None:
    records = hourly_records(hours=24 * 5)
    labels = label_by(
        records, _daylight_labels(lambda r: "visible" if r.image_time.day % 2 else "hidden")
    )
    store = make_store(records, labels)
    issue = START + timedelta(days=1, hours=17, minutes=10)  # the 17:00 frame is out at 17:02
    view = store.as_of(issue, purpose="t")
    at_24 = persistence_valid_minus_24h(view, VP, valid_time=issue + timedelta(hours=24))
    assert at_24.detail["frame_time_utc"] == "2026-11-21T17:00:00Z"
    assert at_24.detail["offset_min"] == pytest.approx(-10.0)
    assert at_24.detail["days_back"] == 1 and at_24.flags == ()
    short = persistence_valid_minus_24h(view, VP, valid_time=issue + timedelta(hours=6))
    assert short.detail["frame_time_utc"] == "2026-11-20T23:00:00Z" and short.flags == ()
    longer = persistence_valid_minus_24h(view, VP, valid_time=issue + timedelta(hours=27))
    assert longer.detail["target_utc"] == "2026-11-20T20:10:00Z"
    assert longer.detail["frame_time_utc"] == "2026-11-20T20:00:00Z"
    assert longer.detail["days_back"] == 2 and longer.flags == ("valid-minus-48h",)
    assert all(a.image_time <= issue and a.available_at <= issue for a in store.accesses)
    # issued on the hour, the frame of that hour is not out yet: no (b) forecast at 24 h
    on_the_hour = store.as_of(START + timedelta(days=1, hours=17), purpose="t")
    none = persistence_valid_minus_24h(
        on_the_hour, VP, valid_time=START + timedelta(days=2, hours=17)
    )
    assert none.value is None and none.flags == ("no-usable-frame-near-target",)


def test_persistence_days_back() -> None:
    issue = START
    assert persistence_days_back(issue, issue + timedelta(hours=1)) == 1
    assert persistence_days_back(issue, issue + timedelta(hours=24)) == 1
    assert persistence_days_back(issue, issue + timedelta(hours=24, seconds=1)) == 2
    assert persistence_days_back(issue, issue + timedelta(hours=48)) == 2
    assert persistence_days_back(issue, issue) == 1


def test_camera_fault_frames_are_never_used() -> None:
    records = hourly_records(hours=48)
    labels = label_by(records, _daylight_labels(lambda r: "visible"))
    newest_day = records[23]  # 23:00 on the first day: the newest labelled frame before issue
    labels[newest_day.key] = {"summit": "camera_fault", "weather_class": "clear"}
    store = make_store(records, labels)
    issue = START + timedelta(hours=30)
    view = store.as_of(issue, purpose="t")
    a = persistence_latest(view, VP, valid_time=issue + timedelta(hours=24))
    assert a.frames == (records[22].key,)
    b = persistence_valid_minus_24h(view, VP, valid_time=newest_day.image_time + timedelta(1))
    assert b.value is None  # the only frame near the target is a camera fault
    g = analog(
        view,
        VP,
        valid_time=issue + timedelta(hours=24),
        sun_alt_deg=30.0 - 3 * 4.0,
        sun_az_deg=180.0 + 3 * 15.0,
        forecast_class="clear",
        sun_of=_sun_of,
    )
    assert newest_day.key not in g.frames


def test_choose_persistence_prefers_b_on_a_diurnal_pattern() -> None:
    # visible every morning, hidden every afternoon: yesterday-same-time is perfect, the
    # latest frame before a pre-dawn issue (the previous evening, hidden) is wrong every
    # morning. Lead 6 h: at 24 h, (a) and (b) read nearly the same frame by definition.
    records = hourly_records(hours=24 * 8)
    labels = label_by(
        records, _daylight_labels(lambda r: "visible" if r.image_time.hour < 20 else "hidden")
    )
    store = make_store(records, labels, record_accesses=False)
    cases = []
    for day in range(2, 7):
        for hour in (16, 17, 18, 21, 22):
            valid = START + timedelta(days=day, hours=hour)
            issue = valid - timedelta(hours=6)
            observed = state_is_visible(
                labels[next(r.key for r in records if r.image_time == valid)]["summit"]
            )
            cases.append(DevCase(VP, issue, valid, observed))
    choice = choose_persistence(store, cases)
    assert choice.chosen == "b"
    assert choice.pss_b == pytest.approx(1.0)
    assert choice.pss_a is not None and choice.pss_a < choice.pss_b
    assert choice.n_paired == len(cases) and choice.n_b_beyond_24h == 0
    assert choice.to_dict()["n_b_beyond_24h"] == 0


def test_choose_persistence_refuses_held_out_cases() -> None:
    records = hourly_records(hours=24 * 3)
    held = HeldOut(((START + timedelta(days=2), START + timedelta(days=3)),))
    store = make_store(records, {}, held_out=held)
    case = DevCase(
        VP, START + timedelta(days=1, hours=18), START + timedelta(days=2, hours=18), True
    )
    with pytest.raises(LeakageError, match="development data only"):
        choose_persistence(store, [case])


def test_choose_persistence_keeps_a_when_undefined() -> None:
    store = make_store(hourly_records(hours=10), {})
    choice = choose_persistence(store, [])
    assert choice.chosen == "a" and choice.pss_a is None and choice.flags


def test_climatology_by_month_hour_with_fallbacks() -> None:
    records = hourly_records(hours=24 * 6)
    labels = label_by(
        records,
        _daylight_labels(lambda r: "visible" if r.image_time.hour in (16, 17) else "hidden"),
    )
    store = make_store(records, labels)
    issue = START + timedelta(days=5)
    table = fit_climatology(store.for_fit(purpose="clim", until=issue), VP, TZ)
    assert table.overall[1] == 5 * 8  # 5 full days x 8 daytime frames, up to the issue time
    # 16:00 UTC is 08:00 PST in November
    p, level, n = table.probability(datetime(2026, 11, 26, 16, tzinfo=UTC), min_count=3)
    assert (p, level, n) == (1.0, "month-hour", 5)
    p, level, _ = table.probability(datetime(2026, 12, 26, 16, tzinfo=UTC), min_count=3)
    assert (p, level) == (1.0, "hour")  # no December frames: falls back to the hour
    p, level, _ = table.probability(datetime(2026, 12, 26, 3, tzinfo=UTC), min_count=3)
    assert level == "overall" and p == pytest.approx(10 / 40)

    pred = climatology(
        store,
        VP,
        issue_time=issue,
        valid_time=issue + timedelta(hours=16),
        time_zone=TZ,
        min_count=3,
    )
    assert pred.value is True and pred.probability == 1.0
    assert pred.detail["threshold"] == pytest.approx(0.25)
    fixed = climatology(
        store,
        VP,
        issue_time=issue,
        valid_time=issue + timedelta(hours=18),
        time_zone=TZ,
        min_count=3,
        threshold=0.5,
    )
    assert fixed.value is False


def test_climatology_never_reads_after_issue_or_held_out() -> None:
    records = hourly_records(hours=24 * 6)
    held = HeldOut(((START + timedelta(days=4), START + timedelta(days=6)),))
    labels = label_by(
        [r for r in records if not held.contains(r.image_time)],
        _daylight_labels(lambda r: "visible"),
    )
    store = make_store(records, labels, held_out=held)
    issue = START + timedelta(days=3, hours=12)
    climatology(store, VP, issue_time=issue, valid_time=issue + timedelta(hours=24), time_zone=TZ)
    assert store.accesses and max(a.image_time for a in store.accesses) <= issue
    later = START + timedelta(days=5)
    climatology(store, VP, issue_time=later, valid_time=later + timedelta(hours=24), time_zone=TZ)
    assert not any(a.held_out for a in store.accesses)


def _ray(ceilings: list[float | None], cam=1643.0, summit=4392.0, distance=7.74):
    samples = []
    for i, ceiling in enumerate(ceilings):
        d = distance * (i + 1) / len(ceilings)
        samples.append(RaySample(d, cam + (summit - cam) * d / distance, ceiling, f"p{i}"))
    return samples


def _s_rule(samples, low=10.0, mid=10.0, available=None):
    issue = datetime(2026, 11, 20, 18, tzinfo=UTC)
    return s_rule(
        viewpoint_id=VP,
        issue_time=issue,
        valid_time=issue + timedelta(hours=24),
        forecast_available_time=available or issue - timedelta(hours=1),
        ray_samples=samples,
        summit_height_m_msl=4392.0,
        low_cover_pct=low,
        mid_cover_pct=mid,
    )


def test_s_rule_cases() -> None:
    assert _s_rule(_ray([None] * 5)).value is True  # no ceiling, little cloud
    assert _s_rule(_ray([6000.0] * 5)).value is True
    low_deck = _s_rule(_ray([2500.0] * 5))
    assert low_deck.value is False and low_deck.detail["crossing_distance_km"] == pytest.approx(
        7.74 * 2 / 5
    )
    cap = _s_rule(_ray([None, None, None, None, 4300.0]))  # cap cloud just below the summit
    assert cap.value is False
    assert _s_rule(_ray([None] * 5), low=30.0, mid=25.0).value is False  # 55 % >= 50 %
    assert _s_rule(_ray([None] * 5), low=30.0, mid=19.9).value is True
    assert _s_rule(_ray([None] * 5), low=None).value is None


def test_s_rule_refuses_a_forecast_from_after_the_issue_time() -> None:
    issue = datetime(2026, 11, 20, 18, tzinfo=UTC)
    with pytest.raises(LeakageError):
        _s_rule(_ray([None]), available=issue + timedelta(minutes=5))


def _sun_of(record: FrameRecord) -> tuple[float, float]:
    hour = record.image_time.hour
    return 30.0 - abs(hour - 20) * 4.0, 180.0 + (hour - 20) * 15.0


def test_analog_matches_class_then_sun() -> None:
    records = hourly_records(hours=24 * 3)

    def fn(r: FrameRecord):
        if not 16 <= r.image_time.hour <= 23:
            return {}
        cloudy = r.image_time.day == 21
        return {
            "summit": "hidden" if cloudy else "visible",
            "weather_class": "cloudy" if cloudy else "clear",
        }

    store = make_store(records, label_by(records, fn))
    issue = START + timedelta(days=2, hours=12)
    view = store.as_of(issue, purpose="t")
    query = {"sun_alt_deg": 26.0, "sun_az_deg": 165.0}  # like 19:00 UTC
    clear = analog(
        view,
        VP,
        valid_time=issue + timedelta(hours=24),
        forecast_class="clear",
        sun_of=_sun_of,
        **query,
    )
    assert clear.value is True and clear.detail["frame_time_utc"].endswith("T19:00:00Z")
    assert clear.flags == ()
    cloudy = analog(
        view,
        VP,
        valid_time=issue + timedelta(hours=24),
        forecast_class="cloudy",
        sun_of=_sun_of,
        **query,
    )
    assert cloudy.value is False and cloudy.detail["frame_time_utc"].startswith("2026-11-21")
    foggy = analog(
        view,
        VP,
        valid_time=issue + timedelta(hours=24),
        forecast_class="fog",
        sun_of=_sun_of,
        **query,
    )
    assert "no-close-analog" in foggy.flags and "sun-only-match" in foggy.flags
    unknown = analog(
        view,
        VP,
        valid_time=issue + timedelta(hours=24),
        forecast_class=None,
        sun_of=_sun_of,
        **query,
    )
    assert "forecast-class-unknown" in unknown.flags
    far = analog(
        view,
        VP,
        valid_time=issue + timedelta(hours=24),
        forecast_class="clear",
        sun_of=_sun_of,
        sun_alt_deg=-50.0,
        sun_az_deg=0.0,
        max_distance_deg=5.0,
        weights=AnalogWeights(alt=1.0, az=0.0),
    )
    assert "no-close-analog" in far.flags


def test_valid_before_issue_is_an_error() -> None:
    store = make_store(hourly_records(hours=5), {})
    view = store.as_of(START + timedelta(hours=4), purpose="t")
    with pytest.raises(ValueError, match="before issue_time"):
        persistence_latest(view, VP, valid_time=START)
