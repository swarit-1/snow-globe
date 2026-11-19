"""Leakage tests (PRD §13.1 "Leakage", §13.9): baselines see only what existed at issue time."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta

import pytest
from synth import START, VP, hourly_records, key_for, label_by, make_store, records_every

from lookout_eval.baselines import (
    analog,
    climatology,
    persistence_latest,
    persistence_valid_minus_24h,
)
from lookout_eval.frames import FrameRecord
from lookout_eval.leakage import FrameStore, HeldOut, LeakageError, SealedScoring

HELD = HeldOut(((START + timedelta(days=5), START + timedelta(days=7)),))
COMMIT = "0123456789abcdef0123456789abcdef01234567"
TZ = "America/Los_Angeles"


def _labels(records):
    def fn(r: FrameRecord):
        hour = r.image_time.hour
        if hour < 15 or hour > 23:  # roughly night at Rainier in UTC
            return {}
        state = "visible" if (r.image_time.day + hour) % 3 else "hidden"
        return {"summit": state, "weather_class": "clear" if state == "visible" else "cloudy"}

    return label_by(records, fn)


def _dev_labels(records, held: HeldOut):
    """Labels for development frames only: held-out frames are absent (PRD §13.1)."""
    return {k: v for k, v in _labels(records).items() if not held.contains(_time_of(records, k))}


def _time_of(records, key):
    return next(r.image_time for r in records if r.key == key)


@pytest.fixture
def store() -> FrameStore:
    records = hourly_records()
    return make_store(records, _dev_labels(records, HELD), held_out=HELD)


def test_listing_stops_at_issue_time_and_availability(store) -> None:
    issue = START + timedelta(days=2, hours=18, minutes=1)  # frame at 18:00 fetched 18:02
    view = store.as_of(issue, purpose="test")
    records = view.records(VP)
    assert records[-1].image_time == START + timedelta(days=2, hours=17)
    assert all(r.available_at <= issue for r in records)


def test_a_frame_imaged_before_but_available_after_issue_is_refused_everywhere() -> None:
    """Review finding: get(), label() and read_bytes() used to check the image time only."""
    t = datetime(2026, 11, 21, 18, 0, tzinfo=UTC)
    record = FrameRecord(VP, t, key_for(t), "live", None, t + timedelta(minutes=2))
    s = make_store([record], {record.key: {"summit": "hidden"}})
    view = s.as_of(t + timedelta(minutes=1), purpose="availability")
    assert view.records(VP) == [] and view.latest(VP) is None
    assert view.nearest(VP, t, tolerance=timedelta(minutes=30)) is None
    for read in (lambda: view.get(record.key), lambda: view.label(record, "summit")):
        with pytest.raises(LeakageError, match="not available until"):
            read()
    with pytest.raises(LeakageError, match="not available until"):
        view.read_bytes(record)
    # once it is available, every path works
    later = s.as_of(t + timedelta(minutes=2), purpose="availability")
    assert later.get(record.key) == record and later.label(record, "summit") == "hidden"


def test_climatology_skips_a_frame_available_after_issue() -> None:
    """Review finding: the fit view behind climatology never checked availability."""
    t = datetime(2026, 11, 21, 18, 0, tzinfo=UTC)
    record = FrameRecord(VP, t, key_for(t), "live", None, t + timedelta(minutes=2))
    s = make_store([record], {record.key: {"summit": "hidden"}})
    issue = t + timedelta(minutes=1)
    pred = climatology(
        s, VP, issue_time=issue, valid_time=issue + timedelta(hours=24), time_zone=TZ, min_count=1
    )
    assert s.accesses == () and pred.value is None and "no-development-frames" in pred.flags
    with pytest.raises(LeakageError, match="available by"):
        s.for_fit(purpose="climatology", until=issue).label(record, "summit")


def test_post_issue_reads_raise(store) -> None:
    issue = START + timedelta(days=2)
    view = store.as_of(issue, purpose="test")
    later = START + timedelta(days=2, hours=1)
    record = FrameRecord(VP, later, key_for(later), "live")
    with pytest.raises(LeakageError, match="after the issue time"):
        view.get(key_for(later))
    with pytest.raises(LeakageError):
        view.label(record, "summit")
    with pytest.raises(LeakageError):
        view.read_bytes(record)


def test_a_forged_record_is_checked_by_its_stored_times(store) -> None:
    issue = START + timedelta(days=2)
    later = START + timedelta(days=2, hours=1)
    forged = FrameRecord(VP, START, key_for(later), "live")  # real key, fake early time
    with pytest.raises(LeakageError, match="after the issue time"):
        store.as_of(issue, purpose="forgery").label(forged, "summit")


def test_held_out_frames_are_refused_unless_sealed(store) -> None:
    issue = START + timedelta(days=6)
    held_key = key_for(START + timedelta(days=5, hours=3))
    with pytest.raises(LeakageError, match="held out"):
        store.as_of(issue, purpose="dev").get(held_key)
    assert all(
        not HELD.contains(r.image_time) for r in store.as_of(issue, purpose="dev").records(VP)
    )
    with pytest.raises(LeakageError, match="sealed scoring"):
        store.as_of(issue, purpose="sealed-scoring", allow_held_out=True)
    records = hourly_records()
    sealed = make_store(records, _labels(records), held_out=HELD, sealed=SealedScoring(COMMIT))
    scoring = sealed.as_of(issue, purpose="sealed-scoring", allow_held_out=True)
    assert scoring.get(held_key).key == held_key


def test_held_out_frames_are_absent_from_the_labelling_store() -> None:
    """PRD §13.1 Leakage: "held-out frames are absent from the labelling store"."""
    sealed_window = HeldOut.sealed_window(TZ)
    frames = records_every(datetime(2026, 12, 20, 18, tzinfo=UTC), timedelta(hours=1), 4)
    labels = {r.key: {"summit": "visible"} for r in frames}
    with pytest.raises(LeakageError, match="absent from the labelling store"):
        FrameStore(frames, held_out=sealed_window, labels=labels)
    # records without labels are fine; labels need an explicit sealed-scoring store
    assert FrameStore(frames, held_out=sealed_window).viewpoints() == (VP,)
    sealed = FrameStore(frames, held_out=sealed_window, labels=labels, sealed=SealedScoring(COMMIT))
    view = sealed.as_of(datetime(2026, 12, 21, tzinfo=UTC), purpose="m4", allow_held_out=True)
    assert view.label(frames[0], "summit") == "visible"
    with pytest.raises(ValueError, match="40-character"):
        SealedScoring("abc123")


def test_labels_must_belong_to_frames_of_the_store() -> None:
    records = hourly_records(hours=2)
    with pytest.raises(ValueError, match="not a frame of this store"):
        FrameStore(records, labels={key_for(START + timedelta(days=9)): {"summit": "visible"}})


def test_fit_view_refuses_held_out_later_and_unavailable_frames(store) -> None:
    fit = store.for_fit(purpose="tau")
    allowed = fit.records(VP)
    assert allowed and all(r.image_time < HELD.earliest_start for r in allowed)
    with pytest.raises(LeakageError, match="held-out"):
        fit.get(key_for(START + timedelta(days=5, hours=3)))
    until = START + timedelta(days=1)
    bounded = store.for_fit(purpose="climatology", until=until)
    assert max(r.image_time for r in bounded.records(VP)) <= until
    with pytest.raises(LeakageError, match="limited to frames up to"):
        bounded.label(store.for_fit(purpose="x").get(key_for(until + timedelta(hours=1))), "summit")


def test_fits_never_see_frames_after_the_held_out_season() -> None:
    """Review finding: for_fit() without `until` handed out frames from after the season."""
    held = HeldOut(
        ((datetime(2025, 12, 1, tzinfo=UTC), datetime(2025, 12, 1, tzinfo=UTC) + timedelta(90)),)
    )
    before = records_every(datetime(2024, 12, 1, tzinfo=UTC), timedelta(hours=1), 24)
    after = records_every(datetime(2026, 11, 1, tzinfo=UTC), timedelta(hours=1), 48)
    s = FrameStore(before + after, held_out=held)
    fit = s.for_fit(purpose="tau")
    assert fit.records(VP) == before
    with pytest.raises(LeakageError, match=r"PRD §13\.9"):
        fit.get(after[0].key)
    # a bound later than the held-out start does not reopen it
    assert s.for_fit(purpose="clim", until=datetime(2026, 11, 3, tzinfo=UTC)).records(VP) == before


def test_nearest_is_cut_at_the_issue_time() -> None:
    """Review finding: at a 24 h lead, persistence (b) read the frame 5 min after the issue."""
    frames = records_every(datetime(2026, 11, 20, tzinfo=UTC), timedelta(minutes=15), 4 * 24 * 3)
    s = make_store(frames, {r.key: {"summit": "visible"} for r in frames})
    issue = datetime(2026, 11, 21, 19, 10, tzinfo=UTC)
    view = s.as_of(issue, purpose="b")
    nearest = view.nearest(VP, issue, tolerance=timedelta(minutes=30))
    assert nearest is not None and nearest.image_time == datetime(2026, 11, 21, 19, 0, tzinfo=UTC)
    b = persistence_valid_minus_24h(view, VP, valid_time=issue + timedelta(hours=24))
    assert b.detail["frame_time_utc"] == "2026-11-21T19:00:00Z" and b.flags == ()
    assert all(a.image_time <= issue and a.available_at <= issue for a in s.accesses)
    with pytest.raises(LeakageError, match="after the issue time"):
        view.nearest(VP, issue + timedelta(hours=3), tolerance=timedelta(minutes=30))


def test_a_leaky_baseline_is_caught(store) -> None:
    """A baseline that peeks at the valid-time frame raises instead of scoring."""

    def leaky(view, valid_time):
        return view.label(view.get(key_for(valid_time)), "summit") == "visible"

    issue = START + timedelta(days=1, hours=18)
    with pytest.raises(LeakageError):
        leaky(store.as_of(issue, purpose="leaky"), issue + timedelta(hours=24))


def test_held_out_from_local_dates() -> None:
    sealed = HeldOut.from_local_dates(date(2026, 12, 15), date(2027, 1, 14), TZ)
    assert sealed == HeldOut.sealed_window(TZ)
    assert sealed.contains(datetime(2026, 12, 15, 8, 0, tzinfo=UTC))  # 00:00 PST
    assert not sealed.contains(datetime(2026, 12, 15, 7, 59, tzinfo=UTC))
    assert sealed.contains(datetime(2027, 1, 15, 7, 59, tzinfo=UTC))
    assert not sealed.contains(datetime(2027, 1, 15, 8, 0, tzinfo=UTC))
    assert sealed.earliest_start == datetime(2026, 12, 15, 8, 0, tzinfo=UTC)
    assert HeldOut().earliest_start is None
    with pytest.raises(ValueError):
        HeldOut(((datetime(2026, 1, 2, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)),))


def test_duplicate_keys_are_rejected() -> None:
    records = hourly_records(hours=2)
    with pytest.raises(ValueError, match="duplicate"):
        FrameStore([*records, records[0]])


def _sun_of(record: FrameRecord) -> tuple[float, float]:
    hour = record.image_time.hour + record.image_time.minute / 60
    return 30.0 - abs(hour - 20.0) * 4.0, 180.0 + (hour - 20.0) * 15.0


def test_audit_every_baseline_over_random_issue_times() -> None:
    """Over many issue times, no baseline read a frame unavailable at its issue time.

    Frames every 15 minutes, each available 2 minutes after capture, so availability matters
    at every issue time; persistence (b) is audited with the others.
    """
    frames = records_every(START, timedelta(minutes=15), 4 * 24 * 7)
    s = make_store(frames, _dev_labels(frames, HELD), held_out=HELD)
    rng = random.Random(7)
    for _ in range(60):
        issue = START + timedelta(minutes=rng.randrange(30 * 60, (24 * 5 - 30) * 60))
        lead = timedelta(hours=rng.choice([1, 3, 6, 12, 24, 27, 29, 48]))
        valid = issue + lead
        s.clear_log()
        view = s.as_of(issue, purpose="audit")
        a = persistence_latest(view, VP, valid_time=valid)
        b = persistence_valid_minus_24h(view, VP, valid_time=valid)
        c = climatology(s, VP, issue_time=issue, valid_time=valid, time_zone=TZ, min_count=1)
        g = analog(
            view,
            VP,
            valid_time=valid,
            sun_alt_deg=20.0,
            sun_az_deg=170.0,
            forecast_class="clear",
            sun_of=_sun_of,
        )
        assert s.accesses
        for access in s.accesses:
            assert not access.held_out, access
            assert access.image_time <= issue, access
            assert access.available_at <= issue, access
            assert not access.post_issue, access
        assert a.frames and g.frames and c.value is not None
        assert b.detail["days_back"] == (1 if lead <= timedelta(hours=24) else 2)


def test_answers_do_not_depend_on_frames_after_issue() -> None:
    """Rewriting every label after the issue time leaves every baseline unchanged."""
    frames = records_every(START, timedelta(minutes=15), 4 * 24 * 7)
    base = _labels(frames)
    issue = START + timedelta(days=3, hours=19, minutes=10)
    flipped = {
        key: (
            {k: ({"visible": "hidden", "hidden": "visible"}.get(v, v)) for k, v in labels.items()}
            if _time_of(frames, key) > issue
            else labels
        )
        for key, labels in base.items()
    }
    for lead_h in (6, 24, 27, 48):
        valid = issue + timedelta(hours=lead_h)
        answers = []
        for labels in (base, flipped):
            s = make_store(frames, labels)
            view = s.as_of(issue, purpose="invariance")
            answers.append(
                (
                    persistence_latest(view, VP, valid_time=valid).value,
                    persistence_valid_minus_24h(view, VP, valid_time=valid).frames,
                    persistence_valid_minus_24h(view, VP, valid_time=valid).value,
                    climatology(
                        s, VP, issue_time=issue, valid_time=valid, time_zone=TZ, min_count=1
                    ).probability,
                    analog(
                        view,
                        VP,
                        valid_time=valid,
                        sun_alt_deg=22.0,
                        sun_az_deg=175.0,
                        forecast_class="clear",
                        sun_of=_sun_of,
                    ).frames,
                )
            )
        assert answers[0] == answers[1], lead_h
