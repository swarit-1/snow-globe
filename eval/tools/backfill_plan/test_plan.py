"""Tests of the backfill planner (plan.py): T0 gap filling, ISO weeks, costs, the extractor's
lookup rule, T3pr and the generated scripts. No data dir, no network (plan.py disables
sockets on import).

Run from eval/:
    PYTHONPATH=src:../workers/common/src:../workers/capture/src \
        uv run --no-sync pytest -q -p no:cacheprovider <this file>
"""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_spec = importlib.util.spec_from_file_location("plan", Path(__file__).with_name("plan.py"))
assert _spec is not None and _spec.loader is not None
plan = importlib.util.module_from_spec(_spec)
sys.modules["plan"] = plan
_spec.loader.exec_module(plan)


class Mask:
    """An availability mask from a set of days."""

    def __init__(self, days: set[date]) -> None:
        self.days = days

    def is_available(self, day: date) -> bool:
        return day in self.days


def camera(days: set[date] | None = None, code: str = "pore") -> plan.Camera:
    """Point Reyes's place and zone with a chosen mask (every day if None)."""
    every = set(plan.daterange(date(2020, 1, 1), date(2026, 12, 31)))
    return plan.Camera(
        vp=None,
        code=code,
        tz=ZoneInfo("America/Los_Angeles"),
        lat=38.0,
        lon=-122.9,
        avail=Mask(every if days is None else days),
    )


def runs(*pairs: tuple[date, date]) -> set[date]:
    return {d for a, b in pairs for d in plan.daterange(a, b)}


# --- T0 -------------------------------------------------------------------------------------


def test_fill_gaps_splits_yosemites_wide_brackets_where_the_mask_has_days():
    # Yosemite's 19 Nov mask around its 2023 switch (runs of available days).
    mask = runs(
        (date(2023, 7, 16), date(2023, 8, 15)),
        (date(2023, 8, 22), date(2023, 8, 22)),
        (date(2023, 9, 5), date(2023, 9, 21)),
        (date(2023, 9, 25), date(2023, 11, 4)),
        (date(2023, 11, 6), date(2023, 12, 9)),
        (date(2023, 12, 14), date(2024, 1, 14)),
    )
    cam = camera(mask)
    lo, hi = date(2023, 7, 15), date(2024, 1, 15)
    grid = plan.grid_days(cam, lo + timedelta(days=14), hi - timedelta(days=1), 14)
    assert date(2023, 8, 26) not in grid and date(2023, 9, 9) in grid  # 26 Aug has no day near
    added = plan.fill_gaps(cam, grid, lo, hi)
    assert added == [date(2023, 8, 22), date(2023, 9, 5), date(2023, 9, 29)]
    anchors = sorted({lo, hi, *grid, *added})
    assert max((b - a).days for a, b in pairwise(anchors)) <= 14


def test_fill_gaps_leaves_a_gap_with_no_usable_day_and_keeps_away_from_its_ends():
    # Grand Canyon: no frames from 26 Apr to 19 May 2024; 25 Apr is 1 day from an anchor.
    mask = runs((date(2024, 4, 24), date(2024, 4, 25)), (date(2024, 5, 20), date(2024, 5, 25)))
    cam = camera(mask)
    assert plan.fill_gaps(cam, [], date(2024, 4, 24), date(2024, 5, 20)) == []


def test_fill_gaps_never_adds_a_held_out_day():
    cam = camera()
    added = plan.fill_gaps(cam, [], date(2026, 5, 20), date(2026, 6, 30))
    assert added and all(d < plan.HELD_OUT_FROM for d in added)


# --- T1 weeks ---------------------------------------------------------------------------------


def test_core_weeks_shift_even_years_clip_the_first_month_and_double_may_2026(monkeypatch):
    cams = {vid: camera() for vid in plan.ORDER}
    weeks = [w for w in plan.core_weeks(cams) if w[2] == plan.PORE]
    mondays = {date.fromisoformat(w[1]) for w in weeks}
    # Odd year: the ISO week holding the 15th. Even year: the week after it.
    assert date(2025, 6, 9) in mondays and date(2024, 6, 17) in mondays
    assert date(2024, 6, 10) not in mondays
    # The span starts 2023-07-15 (a Saturday): July 2023 takes the first full week after.
    assert date(2023, 7, 17) in mondays and date(2023, 7, 10) not in mondays
    # May 2026: the shifted week (18-24) and the last full week before the guard (25-31).
    assert {date(2026, 5, 18), date(2026, 5, 25)} <= mondays
    assert all(d < plan.HELD_OUT_FROM for w in weeks for d in w[3])
    # Both ISO parities in every summer month.
    for month in (6, 7, 8):
        in_month = [m for m in mondays if (m + timedelta(days=3)).month == month]
        parities = {m.isocalendar()[1] % 2 for m in in_month}
        assert parities == {0, 1}, month


# --- costs ------------------------------------------------------------------------------------


def test_cost_counts_a_search_and_the_expected_share_of_frames_on_an_unsearched_day():
    cam = camera()
    day = date(2025, 7, 16)
    searches, gets, complete = plan.cost(cam, day, plan.CORE, 0.9)
    assert searches == 1 and 12 <= complete <= 16
    assert gets == pytest.approx(complete * 0.9)
    # midday is one frame, whatever the share of a complete day.
    assert plan.cost(cam, day, plan.MAP, 0.5) == (1, 1.0, 1)


def test_cost_of_a_second_entry_on_a_day_skips_its_search_and_stored_frames():
    cam = camera()
    day = date(2025, 7, 16)
    syn = cam.synthetic_day(day)
    core = {a.asset_id for a in plan.pick_assets(syn, pick="hourly", min_sun_deg=2.0)}
    night = {a.asset_id for a in plan.pick_assets(syn, pick="hourly", min_sun_deg=None)}
    searches, gets, n = plan.cost(cam, day, plan.NIGHT, 1.0, [plan.CORE])
    assert searches == 0 and n == 24  # every hour of the day
    # The daylight hours are stored already. Near sunrise and sunset the Sun >= 2 pick may
    # stand for its hour with a :15 or :45 frame, so the any-Sun :00 frame is still new.
    assert gets == pytest.approx(len(night - core))
    assert 24 - len(core) <= gets <= 24 - len(core) + 2
    searches, gets, _ = plan.cost(cam, day, plan.EVENING, 1.0, [plan.CORE, plan.NIGHT])
    assert searches == 0 and 30 <= gets <= 50


def test_cost_on_a_cached_day_is_exact_and_skips_stored_hires():
    cam = camera()
    day = date(2025, 1, 15)
    assets = cam.synthetic_day(day)
    cam.cached[day] = assets[::2]  # a partial real day: the :00 and :30 frames
    chosen = plan.pick_assets(cam.cached[day], pick="hourly", min_sun_deg=2.0)
    cam.stored_hires.add(chosen[0].asset_id)
    searches, gets, n = plan.cost(cam, day, plan.CORE, 0.5)
    assert (searches, gets, n) == (0, float(len(chosen) - 1), len(chosen))


# --- the extractor's lookup rule ----------------------------------------------------------------


def _ref(*positions: tuple[float, float]) -> plan.ClearDayReference:
    entries = tuple(
        plan.ReferenceEntry(f"p{i}", f"s{i}", "t", alt, az, plan._NO_STATS)
        for i, (alt, az) in enumerate(positions)
    )
    return plan.ClearDayReference(plan.Region.box(0, 0, 1, 1), plan.PARAMS, entries)


def test_lookup_depth_is_one_bin_never_a_sum_over_neighbours():
    # Query bin (6, 9): 30-35 deg, 180-200 deg. It is empty; (5, 9) holds 2, (6, 10) holds 5.
    ref = _ref(*[(27.0, 190.0)] * 2, *[(32.0, 205.0)] * 5)
    [(exact, chosen, steps)] = plan.lookup_depths(ref, [(32.0, 190.0)])
    assert (exact, steps) == (0, 1)
    assert chosen in (2, 5) and chosen != 7  # one neighbour's frames, never pooled
    looked = ref.lookup(32.0, 190.0)
    assert looked is not None and looked.n == chosen  # the extractor's own answer


def test_a_non_empty_exact_bin_wins_over_a_deep_neighbour():
    ref = _ref((32.0, 190.0), *[(32.0, 205.0)] * 50)
    assert plan.lookup_depths(ref, [(33.0, 185.0)]) == [(1, 1, 0)]


def test_no_bin_within_one_step_is_no_reference():
    ref = _ref((60.0, 100.0))
    assert plan.lookup_depths(ref, [(32.0, 190.0)]) == [(0, 0, -1)]


# --- T3pr and the scripts -----------------------------------------------------------------------


def _planned(days: list[date], tranche: str) -> list[plan.Planned]:
    return [plan.Planned(tranche, plan.PORE, d, plan.CORE, "t") for d in days]


def test_t3pr_takes_one_night_per_season_and_six_clear_days_on_t1_days():
    def weeks(*mondays: date) -> list[date]:
        return [d for m in mondays for d in plan.iso_week(m)]

    t1a = weeks(date(2025, 6, 9), date(2025, 7, 14), date(2025, 8, 11), date(2026, 5, 25))
    t1b = weeks(date(2025, 9, 15), date(2025, 10, 13), date(2026, 1, 19), date(2024, 10, 21))
    planned = _planned(t1a, "T1a") + _planned(t1b, "T1b")
    items = plan.t3_point_reyes(planned)
    nights = [d for _, d, opts, _ in items if opts is plan.NIGHT]
    clear = [d for _, d, opts, _ in items if opts is plan.EVENING]
    assert nights == [date(2026, 1, 21), date(2026, 5, 27), date(2025, 8, 13), date(2025, 10, 15)]
    assert len(clear) == plan.T3PR_EVENINGS
    assert set(nights + clear) <= set(t1a + t1b)
    assert all(d < plan.HELD_OUT_FROM for d in nights + clear)


def test_scripts_need_q_use_no_absolute_home_path_and_stay_before_the_guard():
    items = [
        plan.Planned("T1a", plan.PORE, date(2026, 5, 30), plan.CORE, "w"),
        plan.Planned("T1a", plan.PORE, date(2026, 5, 31), plan.CORE, "w"),
    ]
    text = plan.script(["# header"], items)
    assert "/Users/" not in text and "/private/" not in text
    assert ': "${Q:?' in text and 'cd "$REPO/workers/capture"' in text
    assert "REPO=${REPO:-$(git rev-parse --show-toplevel)}" in text
    adds = [line for line in text.splitlines() if line.startswith("run archive queue add")]
    assert adds == [
        "run archive queue add point-reyes.beach 2026-05-30 2026-05-31 --hires --pick hourly "
        "--min-sun-deg 2"
    ]
    for day in re.findall(r"\d{4}-\d{2}-\d{2}", text):
        assert date.fromisoformat(day) < plan.HELD_OUT_FROM


def test_the_t3pr_draft_needs_evenings_and_adds_them_after_the_nights():
    items = [plan.Planned("T3pr", plan.PORE, date(2026, 1, 21), plan.NIGHT, "T3pr night frames")]
    text = plan.t3pr_script(items)
    lines = text.splitlines()
    assert ': "${EVENINGS:?' in text
    night = lines.index("run archive queue add point-reyes.beach 2026-01-21 --hires --pick hourly")
    clear = lines.index(
        "run archive queue add point-reyes.beach $EVENINGS --hires --pick all --min-sun-deg -2"
    )
    assert night < clear < lines.index("run archive queue list")


def test_loop_throughput_counts_the_first_request_of_a_window():
    # 305.7 s window, 0.62 s requests: the first needs 35 s, each later one 40 s plus 5.62 s.
    assert plan.loop_throughput()["requests_per_window"] == 49
