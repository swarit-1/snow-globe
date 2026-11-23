"""The dev P1 table's baselines (PRD §13.6, §13.1; D-036): only frames available at the issue time.

Synthetic frame records (no pixels) and synthetic live-profile runs; the store logs every
access, and the audit asserts that no baseline read a frame imaged or fetched after its issue.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
from snow_globe_common.manifest import load_manifest
from synth import key_for
from synth_forecast import clear_sky, low_deck
from synth_p1dev import live_doc

from snow_globe_eval.baselines import DevCase
from snow_globe_eval.forecast_io import ForecastRun
from snow_globe_eval.frames import FrameRecord
from snow_globe_eval.heldout import LeakageError
from snow_globe_eval.p1dev_baselines import (
    baselines_for,
    label_store,
    s_rule_for,
    stronger_persistence,
)

VP = "rainier.paradise-mountain"
ZONE = "America/Los_Angeles"
FIRST = datetime(2026, 11, 19, 6, 30, tzinfo=UTC)  # the first capture, at night
STEP = timedelta(minutes=16)


def _daytime(t: datetime) -> bool:
    return 16 <= t.hour < 24  # about 08:00-16:00 PST in late November


def _frames(days: int = 4, delay: timedelta = timedelta(seconds=60)) -> list[FrameRecord]:
    out = []
    t = FIRST
    while t < FIRST + timedelta(days=days):
        out.append(FrameRecord(VP, t, key_for(t, VP), "live", None, t + delay))
        t += STEP
    return out


def _classes(frames: list[FrameRecord], seed: int = 1) -> dict[str, str]:
    """Night at night; by day a summit visible on even UTC days, hidden or in cloud on odd."""
    rng = random.Random(seed)
    out = {}
    for r in frames:
        if not _daytime(r.image_time):
            out[r.key] = "night"
        elif r.image_time.day % 2 == 0:
            out[r.key] = rng.choice(["visible", "visible", "partial"])
        else:
            out[r.key] = rng.choice(["hidden", "in_cloud", "no_reference"])
    return out


def test_no_baseline_reads_a_frame_after_its_issue_time() -> None:
    frames = _frames()
    store = label_store(frames, _classes(frames), label="summit", record_accesses=True)
    rng = random.Random(5)
    made = reads = 0
    for record in rng.sample([r for r in frames if _daytime(r.image_time)], 80):
        valid = record.image_time
        for lead_h in (1, 6, 24, 25, 48):
            issue = valid - timedelta(hours=lead_h)
            store.clear_log()
            out = baselines_for(
                store,
                viewpoint_id=VP,
                time_zone=ZONE,
                issue_time=issue,
                valid_time=valid,
                label="summit",
            )
            made += sum(p.value is not None for p in out.values())
            reads += len(store.accesses)
            for access in store.accesses:
                assert access.image_time <= issue and access.available_at <= issue, access
                assert not access.held_out
    assert made > 300 and reads > 1000  # the audit covers real forecasts, not only refusals


def test_the_first_day_has_no_persistence_and_no_climatology() -> None:
    frames = _frames()
    store = label_store(frames, _classes(frames), label="summit")
    valid = datetime(2026, 11, 19, 20, tzinfo=UTC)  # first day; issue 18 Nov, before capture
    out = baselines_for(
        store,
        viewpoint_id=VP,
        time_zone=ZONE,
        issue_time=valid - timedelta(hours=24),
        valid_time=valid,
        label="summit",
    )
    assert out["persistence_a"].value is None
    assert out["persistence_a"].flags == ("no-usable-frame-before-issue",)
    assert out["persistence_b"].value is None
    assert "no-usable-frame-near-target" in out["persistence_b"].flags
    assert out["climatology"].value is None
    assert out["climatology"].flags == ("no-development-frames",)
    assert out["s_rule"].value is None and out["s_rule"].flags == ("no-forecast-run",)


def test_persistence_b_is_the_frame_at_valid_minus_24h_available_by_the_issue() -> None:
    t0 = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)
    frames = [
        # imaged 2 min before the target (= the issue time) but fetched 1 min after it
        FrameRecord(
            VP,
            t0 - timedelta(minutes=2),
            key_for(t0 - timedelta(minutes=2), VP),
            "live",
            None,
            t0 + timedelta(minutes=1),
        ),
        FrameRecord(
            VP,
            t0 - timedelta(minutes=20),
            key_for(t0 - timedelta(minutes=20), VP),
            "live",
            None,
            t0 - timedelta(minutes=19),
        ),
        FrameRecord(
            VP,
            t0 + timedelta(minutes=5),
            key_for(t0 + timedelta(minutes=5), VP),
            "live",
            None,
            t0 + timedelta(minutes=6),
        ),
    ]
    classes = {frames[0].key: "visible", frames[1].key: "hidden", frames[2].key: "visible"}
    store = label_store(frames, classes, label="summit")
    valid = t0 + timedelta(hours=24)
    out = baselines_for(
        store, viewpoint_id=VP, time_zone=ZONE, issue_time=t0, valid_time=valid, label="summit"
    )
    b = out["persistence_b"]
    assert b.value is False and b.detail["frame_time_utc"] == "2026-11-20T17:40:00Z"
    assert out["persistence_a"].detail["frame_time_utc"] == "2026-11-20T17:40:00Z"
    # beyond 24 h (D-036): valid - 48 h, flagged
    later = baselines_for(
        store,
        viewpoint_id=VP,
        time_zone=ZONE,
        issue_time=t0 + timedelta(hours=1),
        valid_time=t0 + timedelta(hours=49),
        label="summit",
    )
    assert "valid-minus-48h" in later["persistence_b"].flags


def test_climatology_counts_only_frames_available_by_the_issue() -> None:
    frames = _frames()
    classes = _classes(frames)
    store = label_store(frames, classes, label="summit")
    valid = datetime(2026, 11, 21, 20, tzinfo=UTC)
    issue = valid - timedelta(hours=24)
    out = baselines_for(
        store, viewpoint_id=VP, time_zone=ZONE, issue_time=issue, valid_time=valid, label="summit"
    )
    usable = [
        r
        for r in frames
        if classes[r.key] in ("visible", "hidden", "in_cloud") and r.available_at <= issue
    ]
    assert out["climatology"].detail["n_frames"] == len(usable)
    assert out["climatology"].value is not None


def test_held_out_frames_carry_no_labels() -> None:
    sealed = datetime(2026, 12, 20, 20, tzinfo=UTC)  # design P's sealed window at Rainier
    record = FrameRecord(VP, sealed, key_for(sealed, VP), "live", None, sealed)
    with pytest.raises(LeakageError, match="held-out frame"):
        label_store([record], {record.key: "visible"}, label="summit")


@pytest.fixture(scope="module")
def vp():
    return load_manifest(VP)


def test_s_rule_from_one_run(vp) -> None:
    run_start = datetime(2026, 11, 19, 12, tzinfo=UTC)
    valid = run_start + timedelta(hours=30, minutes=21)
    issue = valid - timedelta(hours=24)
    common = {
        "viewpoint": vp,
        "landmark": vp.landmarks[0],
        "issue_time": issue,
        "valid_time": valid,
        "available": run_start + timedelta(hours=2),
        "camera_height_m_msl": vp.camera.elevation_m + 10.0,
    }
    clear = s_rule_for(run=ForecastRun(live_doc(vp, run_start, [30, 31], clear_sky)), **common)
    assert clear.value is True and clear.detail["ray"]["landmark"] == vp.landmarks[0].name
    deck = s_rule_for(run=ForecastRun(live_doc(vp, run_start, [30, 31], low_deck)), **common)
    assert deck.value is False and deck.detail["crossing_distance_km"] is not None
    doc = live_doc(vp, run_start, [30, 31], clear_sky)
    doc["points"] = [p for p in doc["points"] if p["kind"] != "landmark"]
    for field in doc["values"]:
        for hour in field:
            del hour[len(doc["points"]) :]
    gap = s_rule_for(run=ForecastRun(doc), **common)
    assert gap.value is None and gap.flags == ("forecast-field-unavailable",)
    with pytest.raises(LeakageError):  # a run published after the issue time
        s_rule_for(
            run=ForecastRun(live_doc(vp, run_start, [30, 31], clear_sky)),
            **{**common, "available": issue + timedelta(minutes=1)},
        )


def _daily_pattern(frames: list[FrameRecord]) -> dict[str, str]:
    """Every day the same: summit visible 16-20Z, hidden 20-24Z, night otherwise."""
    return {
        r.key: "night"
        if not _daytime(r.image_time)
        else ("visible" if r.image_time.hour < 20 else "hidden")
        for r in frames
    }


def test_the_stronger_persistence_is_chosen_on_the_cases_given() -> None:
    """At a 30 h lead (b) looks back 48 h to the same time of day, which repeats, while (a) sees
    the frame 30 h back, often the other half of the day: (b) must win, with PSS 1."""
    frames = _frames(days=6)
    classes = _daily_pattern(frames)
    store = label_store(frames, classes, label="summit")
    cases = [
        DevCase(VP, r.image_time - timedelta(hours=30), r.image_time, classes[r.key] == "visible")
        for r in frames
        if _daytime(r.image_time) and r.image_time.day >= 21
    ]
    choice, record = stronger_persistence(store, cases, label="summit")
    assert choice.chosen == "b" and record["chosen"] == "b"
    assert choice.pss_b == pytest.approx(1.0)
    assert choice.pss_a is not None and choice.pss_a < 0.5
    assert record["n_paired"] == choice.n_paired > 100
    assert choice.n_b_beyond_24h == choice.n_paired  # every (b) went back two days
    assert any("more than one day" in flag for flag in choice.flags)
    assert not any("tie" in flag for flag in choice.flags)


def test_a_tie_keeps_persistence_a_by_rule() -> None:
    """At a 24 h lead both variants read the same frame whenever both forecast: a tie."""
    frames = _frames(days=6)
    classes = _daily_pattern(frames)
    store = label_store(frames, classes, label="summit")
    cases = [
        DevCase(VP, r.image_time - timedelta(hours=24), r.image_time, classes[r.key] == "visible")
        for r in frames
        if _daytime(r.image_time) and r.image_time.day >= 21
    ]
    choice, record = stronger_persistence(store, cases, label="summit")
    assert choice.pss_a == choice.pss_b and choice.pss_a is not None
    assert choice.chosen == "a" and "tie; (a) kept by rule" in choice.flags
    assert record["n_paired"] > 100 and record["table_a"] == record["table_b"]
