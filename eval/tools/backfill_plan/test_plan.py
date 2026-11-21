"""Tests of the backfill planner (plan.py): T0 gap filling, ISO weeks, costs, the extractor's
lookup rule, T3pr and the generated scripts; the spans from T0's switch dates, the UTC
reading of the held-out edge, and planning from the queue's state; and the review fixes of
20 Nov: the drafts run by /bin/sh with a stub uv, a days-mode run of loop_sim.py on the real
QueueRunner, measure_inputs.py's exact spacing, PLAN.md's targets and the T3pr re-plan.
No data dir (the manifests come from the repo), no network (plan.py disables sockets and
name lookups on import).

Run from eval/:
    PYTHONPATH=src:../workers/common/src:../workers/capture/src \
        uv run --no-sync python -m pytest -q -p no:cacheprovider <this file>
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
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


# --- spans from T0's measured switches (D-024, D-042, D-052) ----------------------------------


def six_cameras(days: set[date] | None = None) -> dict[str, plan.Camera]:
    """The six archive cameras from their manifests, with a chosen mask (every day if None)."""
    viewpoints = plan.load_manifests()
    every = set(plan.daterange(date(2020, 1, 1), date(2026, 12, 31)))
    out = {}
    for vid in plan.ORDER:
        vp = viewpoints[vid]
        out[vid] = plan.Camera(
            vp=vp,
            code=vp.truth_camera.archive_code,
            tz=ZoneInfo(vp.time_zone),
            lat=vp.camera.lat,
            lon=vp.camera.lon,
            avail=Mask(every if days is None else days),
        )
    return out


def queue_entry(p: plan.Planned, status: str = "pending", **progress: object) -> dict:
    """A queue-file entry for a planned item, as make_entry and the runner write it."""
    return {
        "id": p.id,
        "viewpoint_id": p.vid,
        "date": p.day.isoformat(),
        "kinds": list(p.opts["kinds"]),
        "pick": p.opts["pick"],
        "min_sun_deg": p.opts["min_sun_deg"],
        "status": status,
        "progress": dict(progress),
    }


@pytest.fixture(scope="module")
def built() -> tuple[dict[str, plan.Camera], list[plan.Planned]]:
    """The whole plan on cameras with every day available, behind a queue holding T1a."""
    cams = six_cameras()
    t1a = plan.weeks_in_order(plan.core_weeks(cams), "A")
    queue = [queue_entry(plan.Planned("T1a", v, d, o, w)) for v, d, o, w in t1a]
    return cams, plan.build(cams, dict.fromkeys(plan.ORDER, 0.95), queue)


def test_spans_take_t0s_switch_dates_and_keep_d024s_starts_for_the_regular_tranches():
    for vid, span in plan.SPANS.items():
        bracket = (span.first_169 - span.last_32).days
        assert bracket == (26 if vid == plan.GRCA else 14), vid  # D-052's measured brackets
        assert span.first_169 < span.d024_start  # T0 dated every switch before D-024's sample
        assert span.regular_start == max(span.d024_start, span.design_start or span.d024_start)
    assert plan.SPANS[plan.PORE].owner_window == (date(2023, 4, 10), date(2023, 7, 14))
    assert plan.SPANS[plan.GRCA].owner_window == (date(2024, 5, 20), date(2024, 7, 14))
    assert plan.SPANS[plan.YOSE].owner_window == (date(2023, 9, 5), date(2024, 1, 14))
    assert plan.SPANS[plan.ACAD].owner_window == (date(2023, 8, 26), date(2024, 1, 14))
    assert plan.SPANS[plan.GRSM].owner_window == (date(2024, 3, 25), date(2024, 7, 14))
    # Big Bend: D-024's view check holds the whole span back, so nothing goes to the owner.
    assert plan.SPANS[plan.BIBE].regular_start == date(2025, 1, 13)
    assert plan.SPANS[plan.BIBE].owner_window is None


def test_regular_tranches_stay_in_their_windows_and_tq_holds_the_dates_t0_opened(built):
    cams, planned = built
    windows = plan.windows_of(cams)
    owner = plan.windows_of(cams, "owner")
    for p in planned:
        span = plan.SPANS[p.vid]
        assert p.day >= span.first_169, p.id  # never the 3:2 epoch (D-024, D-042)
        if p.tranche == "TQ":
            assert owner[p.vid][0] <= p.day <= owner[p.vid][1], p.id
        else:
            assert windows[p.vid][0] <= p.day <= windows[p.vid][1], p.id
    # D-024: Grand Canyon's winter 2024/25 waits for the re-aim check; other winters do not.
    grca = [p.day for p in planned if p.vid == plan.GRCA]
    assert not [d for d in grca if date(2024, 12, 1) <= d <= date(2025, 2, 28)]
    assert any(date(2025, 12, 1) <= d <= date(2026, 2, 28) for d in grca)
    tq = {p.vid for p in planned if p.tranche == "TQ"}
    assert tq == {plan.PORE, plan.GRCA, plan.YOSE, plan.ACAD, plan.GRSM}
    # Acadia's first TQ week starts after its 26 Aug 2023 switch: the first full week on or after.
    acadia = sorted(p.day for p in planned if p.tranche == "TQ" and p.vid == plan.ACAD)
    assert acadia[0] == date(2023, 8, 26) and date(2023, 8, 28) in acadia
    assert date(2023, 8, 25) not in acadia


def test_t2_skips_the_queues_t1_days_and_point_reyes_comes_first(built):
    _cams, planned = built
    t1a = {(p.vid, p.day) for p in planned if p.tranche == "T1a"}
    t2 = [p for p in planned if p.tranche == "T2"]
    assert t2 and not {(p.vid, p.day) for p in t2} & t1a
    first_other = next(i for i, p in enumerate(t2) if p.vid != plan.PORE)
    assert all(p.vid == plan.PORE for p in t2[:first_other])
    assert all(p.vid != plan.PORE for p in t2[first_other:])


# --- the UTC reading of the held-out edge (D-038 pending; the lead's rule of 20 Nov) ---------


def test_the_utc_rule_refuses_31_may_in_all_six_zones_and_allows_30_may():
    for vid, cam in six_cameras().items():
        assert not cam.plannable(date(2026, 5, 31)), vid  # its evening is 1 Jun in UTC
        assert cam.plannable(date(2026, 5, 30)), vid
        assert cam.last_plannable() == date(2026, 5, 30), vid
        assert not cam.plannable(date(2026, 6, 1)), vid  # the local reading, as before


def test_the_utc_rule_reads_the_instant_not_a_list_of_dates():
    la = ZoneInfo("America/Los_Angeles")
    # 23:59:59 PST on 30 May is 07:59:59 UTC on 31 May: before the edge.
    assert plan.latest_stamp_utc(la, date(2026, 5, 30)).isoformat() == "2026-05-31T07:59:59+00:00"
    # On or east of Greenwich without daylight time, all of 31 May's local frames are before
    # 1 Jun 00:00 UTC and on 31 May by the wall clock.
    for name in ("Etc/UTC", "Asia/Tokyo"):
        assert plan.plannable_in(ZoneInfo(name), date(2026, 5, 31)), name
        assert not plan.plannable_in(ZoneInfo(name), date(2026, 6, 1)), name  # local reading
    # Paris: 23:00-23:59:59 CET on 31 May is before 1 Jun in UTC, but it is 00:00-00:59 CEST on
    # 1 Jun by the wall clock (capture's civil-date reading, D-047), so the local reading
    # refuses the date (review finding: the entry's date alone let it through).
    paris = ZoneInfo("Europe/Paris")
    latest = plan.latest_frame_utc(paris, date(2026, 5, 31))
    assert latest < plan.UTC_GUARD and latest.astimezone(paris).date() == date(2026, 6, 1)
    assert not plan.plannable_in(paris, date(2026, 5, 31))
    assert plan.plannable_in(paris, date(2026, 5, 30))
    # Far west (UTC-12), 30 May still ends before the edge and 31 May does not.
    west = ZoneInfo("Etc/GMT+12")
    assert plan.last_plannable_in(west) == date(2026, 5, 30)


def test_a_cached_asset_that_reads_past_the_edge_refuses_its_date():
    cam = camera()
    day = date(2026, 5, 30)
    cam.cached[day] = cam.synthetic_day(day)
    assert cam.plannable(day)
    # A stamp off its date (none of the 20,351 cached assets is) that reads 1 Jun 07:30 UTC.
    odd = plan.ArchiveAsset("a1b2c3d4-odd", "pore", "06/01/2026 12:30:00 AM", "", 5.0, "day")
    cam.cached[day] = [*cam.cached[day], odd]
    assert not cam.plannable(day)


def test_31_may_hourly_entries_would_have_yielded_frames_dated_1_jun_in_utc():
    cams = six_cameras()
    late = {}
    for vid, cam in cams.items():
        picks = plan.pick_assets(
            cam.synthetic_day(date(2026, 5, 31)), pick="hourly", min_sun_deg=2.0
        )
        late[vid] = sum(1 for a in picks if cam.utc_of(a) >= plan.UTC_GUARD)
        on_30 = plan.pick_assets(
            cam.synthetic_day(date(2026, 5, 30)), pick="hourly", min_sun_deg=2.0
        )
        assert all(cam.utc_of(a) < plan.UTC_GUARD for a in on_30), vid
    assert late[plan.PORE] > 0 and late[plan.GRCA] > 0 and late[plan.BIBE] > 0


def test_no_planned_date_at_or_after_the_boundary(built, tmp_path):
    cams, planned = built
    assert planned and max(p.day for p in planned) == date(2026, 5, 30)
    for p in planned:
        assert plan.latest_frame_utc(cams[p.vid].tz, p.day) < plan.UTC_GUARD, p.id
        assert p.day < plan.HELD_OUT_FROM
    plan.write_scripts(tmp_path, planned, cams, "a test plan")
    queued_days = 0
    for script_path in sorted(tmp_path.glob("draft-*.sh")):
        text = script_path.read_text()
        assert "/Users/" not in text and "/private/" not in text
        for line in (raw.strip() for raw in text.splitlines()):  # TQ's lines are indented
            if not line.startswith("run archive queue add"):
                continue  # comments name the edge itself (2026-06-01)
            for day in re.findall(r"\b\d{4}-\d{2}-\d{2}\b", line):
                assert date.fromisoformat(day) <= date(2026, 5, 30), (script_path.name, day)
                queued_days += 1
    assert queued_days == sum(
        1 for p in planned if p.status == "planned" and p.opts is not plan.EVENING
    )
    # capture's own make_entry accepts every planned entry (it refuses 1 Jun local and later).
    to_queue = [p for p in planned if p.status == "planned"]
    added = plan.write_queue(tmp_path / "queue.json", to_queue, cams)
    assert len(added) == len(to_queue)


def test_an_open_queued_entry_past_the_edge_stops_the_plan():
    cams = six_cameras()
    bad = plan.Planned("T1a", plan.PORE, date(2026, 5, 31), plan.CORE, "w")
    with pytest.raises(ValueError, match="held-out edge"):
        plan.build(cams, dict.fromkeys(plan.ORDER, 0.95), [queue_entry(bad)])
    # A settled one (say, done before the rule) does not stop it: it has no work left.
    plan.build(cams, dict.fromkeys(plan.ORDER, 0.95), [queue_entry(bad, "done")])


def test_a_queued_entry_for_a_viewpoint_outside_the_plan_stops_it():
    # Its requests would be missing from the schedule, so the planner refuses to guess.
    cams = six_cameras()
    teton = plan.Planned("T1a", "grand-teton.science-center", date(2025, 7, 1), plan.CORE, "w")
    with pytest.raises(ValueError, match=re.escape("grand-teton.science-center")):
        plan.build(cams, dict.fromkeys(plan.ORDER, 0.95), [queue_entry(teton)])


def _check_script(tmp_path: Path, lines: list[str]) -> Path:
    path = tmp_path / "check.sh"
    path.write_text("\n".join(["set -eu", *lines, "echo ok"]) + "\n")
    return path


def _sh(path: Path, cwd: Path | None = None, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", str(path)],
        env={"PATH": "/usr/bin:/bin", **env},
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def test_the_evenings_check_refuses_31_may_and_other_spellings(tmp_path):
    allowed = [date(2025, 7, 16), date(2026, 5, 30), date(2026, 5, 31)]  # 31 May: see below
    check = _check_script(tmp_path, plan.evenings_check(date(2026, 5, 30), allowed=allowed))
    assert _sh(check, EVENINGS="2025-07-16 2026-05-30").returncode == 0
    # 2026-05-31 is refused by the date bound even though this (deliberately wrong) list
    # allows it: the bound stands on its own.
    for bad in ("2026-05-31", "2025-07-16 2026-06-01", "20260530", "2026-W22-6", "*"):
        assert _sh(check, EVENINGS=bad).returncode == 1, bad


# --- planning from the queue's state --------------------------------------------------------------


def test_a_tranche_entry_the_queue_already_holds_keeps_its_place_and_is_not_planned_again():
    cams = six_cameras()
    first_t1b = plan.weeks_in_order(plan.core_weeks(cams), "B")[0]
    queued = plan.Planned("T1b", *first_t1b)
    planned = plan.build(cams, dict.fromkeys(plan.ORDER, 0.95), [queue_entry(queued)])
    same = [p for p in planned if p.id == queued.id]
    assert len(same) == 1 and same[0].tranche == "T1b" and same[0].status == "pending"
    assert planned[0].id == queued.id  # the queue's own order comes first


def test_the_work_left_in_a_queued_entry_comes_from_its_progress():
    cam = camera()
    day = date(2025, 7, 19)
    p = plan.Planned("T1a", plan.PORE, day, plan.CORE, "w")
    done = queue_entry(p, "done", selected_count=15, requests=16)
    assert plan.remaining_of_queued(cam, done, plan.CORE, 0.95, []) == (0, 0.0, 15)
    selected = [{"AssetID": f"{i:08x}-0000"} for i in range(15)]
    outcomes = {f"{i:08x}-0000:hires": "stored" for i in range(11)}
    active = queue_entry(p, "active", selected=selected, next=11, sweep=1, outcomes=outcomes)
    assert plan.remaining_of_queued(cam, active, plan.CORE, 0.95, []) == (0, 4.0, 15)
    cam.stored_hires.add("0000000c-0000")  # one of the four is stored already
    assert plan.remaining_of_queued(cam, active, plan.CORE, 0.95, []) == (0, 3.0, 15)
    resumed = queue_entry(p, "pending", requests=3)  # a search that failed three times
    assert plan.remaining_of_queued(cam, resumed, plan.CORE, 0.95, []) == plan.cost(
        cam, day, plan.CORE, 0.95
    )


def test_tranches_finish_in_queue_order_at_the_daily_budget():
    def item(tranche: str, n: float) -> plan.Planned:
        return plan.Planned(tranche, plan.PORE, date(2025, 7, 1), plan.CORE, "w", 0, n)

    planned = [item("T1a", 3864), item("T1b", 4812), item("T3pr", 271)]
    rows = {r["tranche"]: r for r in plan.schedule(planned, date(2026, 11, 21), 1000)["rows"]}
    rate = "20 Nov (10/1000)"
    assert rows["T1a"]["finishes_utc_day"][rate] == "2026-11-24"  # 3,864 / 990 a day
    assert rows["T1b"]["finishes_utc_day"][rate] == "2026-11-29"  # 8,676 / 990
    assert rows["T3pr"]["finishes_utc_day"][rate] == "2026-11-30"  # 8,947 / 990
    # The same requests with T3pr first: it lands on the fifth day.
    reordered = [item("T1a", 3864), item("T3pr", 271), item("T1b", 4812)]
    rows = {r["tranche"]: r for r in plan.schedule(reordered, date(2026, 11, 21), 1000)["rows"]}
    assert rows["T3pr"]["finishes_utc_day"][rate] == "2026-11-25"
    assert plan.finish_day(990, date(2026, 11, 21), 1000, 0.99) == date(2026, 11, 21)
    assert plan.finish_day(991, date(2026, 11, 21), 1000, 0.99) == date(2026, 11, 22)


def test_t3pr_skips_a_season_without_a_point_reyes_t1_week():
    weeks = [*plan.iso_week(date(2025, 9, 15)), *plan.iso_week(date(2025, 10, 13))]
    items = plan.t3_point_reyes(_planned(weeks, "T1b"))
    nights = [d for _, d, opts, _ in items if opts is plan.NIGHT]
    assert nights == [date(2025, 10, 15)]  # autumn only: no winter, spring or summer week


# --- review fixes of 20 Nov (fixer): one test per finding ----------------------------------------


def _load(name: str):
    """Import a sibling tool (loop_sim.py, measure_inputs.py) as a module."""
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def loop_sim():
    """loop_sim.py, sharing this file's copy of plan.py."""
    return _load("loop_sim")


def run_draft(tmp_path: Path, text: str, **env: str) -> tuple[int, list[str], str]:
    """Run a generated draft with a stub ``uv`` that only records its arguments.

    Capture never runs and no queue is written: the stub appends each call to a log. The
    draft runs from ``tmp_path`` with REPO pointing at an empty fake repo.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "uv"
    stub.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$FAKE_UV_LOG"\n')
    stub.chmod(0o755)
    repo = tmp_path / "repo"
    (repo / "workers" / "capture").mkdir(parents=True, exist_ok=True)
    draft = tmp_path / "draft.sh"
    draft.write_text(text)
    log = tmp_path / "uv.log"
    log.unlink(missing_ok=True)
    proc = subprocess.run(
        ["/bin/sh", str(draft)],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "REPO": str(repo), "FAKE_UV_LOG": str(log), **env},
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return proc.returncode, [c.split(" --queue ")[0] for c in calls], proc.stderr


def _queue_file(tmp_path: Path) -> Path:
    path = tmp_path / "queue.json"
    path.write_text('{"schema": "test", "entries": []}\n')
    return path


ADD = "run --no-sync snow-globe-capture archive queue add"
LIST = "run --no-sync snow-globe-capture archive queue list"


def test_measure_inputs_bins_end_to_start_gaps_exactly():
    # Finding 1: the spacing table must come from exact counts. Gaps of 4.9944, 5.0008,
    # 5.0009 and 5.0109 s (stamps to the ms, elapsed to 0.1 ms): rounding to 3 decimals
    # would put 5.0008 with 5.0009 and above.
    measure = _load("measure_inputs")

    def rec(t: str, elapsed_ms: float) -> dict:
        return {"event": "archive_request", "t": f"2026-11-20T10:00:{t}Z", "elapsed_ms": elapsed_ms}

    reqs = [rec("01.000", 100.0), rec("06.194", 199.6), rec("11.495", 300.2)]
    reqs += [rec("16.746", 250.1), rec("22.057", 300.1)]
    got = measure.end_to_start_spacing(reqs)
    assert [x["gap_s"] for x in got["lowest"]] == ["4.9944", "5.0008", "5.0009", "5.0109"]
    assert got["bins_s"] == {
        "[0, 5.000)": 1,
        "[5.000, 5.0009)": 1,
        "[5.0009, 5.005)": 1,
        "[5.005, 5.05)": 1,
        "[5.05, inf)": 0,
    }
    rows = [{**r, "kind": "search", "status": 200, "bytes": 10} for r in reqs]
    assert measure.queue_requests(rows)["hours_to_1000th"] is None  # fewer than one budget


def test_days_mode_counts_a_failed_entry_as_unfinished_and_resumes_it_daily(
    tmp_path, monkeypatch, loop_sim
):
    # Finding 2, on the real QueueRunner: the first entry's search fails three times (the
    # replayed triple of read timeouts), so it is failed, not finished; T2 holds no entry of
    # this queue and is never reported. With the lead's daily resume it finishes a day later.
    monkeypatch.setenv("SNOW_GLOBE_DATA_DIR", str(tmp_path / "unused"))  # _setup sets its own
    monkeypatch.setattr(
        loop_sim.SimNPS, "_outcome", lambda self: "read_timeout" if len(self.log) < 3 else "ok"
    )
    cams = six_cameras()
    flaky = plan.Planned("T1b", plan.PORE, date(2025, 9, 17), plan.CORE, "w")
    good = plan.Planned("T1a", plan.PORE, date(2025, 7, 16), plan.CORE, "w")
    entries = [queue_entry(flaky), queue_entry(good)]
    labels = {flaky.id: "T1b", good.id: "T1a", "elsewhere": "T2"}
    inputs = {"search_s": [0.25], "get_s": [0.25], "sequence_20nov": ["ok"], "pass_s_v2": [10.0]}

    def run(folder: str, resume: bool) -> dict:
        return loop_sim.run_days(
            tmp_path / folder, 0, "20nov", [10.0], inputs, cams, plan.load_manifests(), entries,
            dict.fromkeys(plan.ORDER, 1.0), labels, ["T1a", "T1b"], 2.2, 3, resume_failed=resume,
        )  # fmt: skip

    alone = run("alone", False)
    assert alone["finished_utc_day"] == {"T1a": "2026-11-21"}
    assert alone["settled_utc_day"] == {"T1a": "2026-11-21", "T1b": "2026-11-21"}
    assert alone["needs_resume_at_end"] == {flaky.id: "T1b"}
    assert alone["days"][0]["needs_resume_by_tranche"] == {"T1b": 1}
    resumed = run("resumed", True)
    assert resumed["finished_utc_day"] == {"T1a": "2026-11-21", "T1b": "2026-11-22"}
    assert [d["resumed_at_start"] for d in resumed["days"]] == [0, 1]
    assert resumed["needs_resume_at_end"] == {} and resumed["entry_status"] == {"done": 2}


def test_tranche_progress_reports_only_the_queues_tranches(loop_sim):
    entries = [
        {"id": "a", "status": "done"},
        {"id": "b", "status": "failed"},
        {"id": "c", "status": "stopped"},
        {"id": "d", "status": "pending"},
        {"id": "e", "status": "refused"},
    ]
    labels = {"a": "T1a", "b": "T1a", "c": "T1b", "d": "T1b", "e": "T3pr", "zz": "T2"}
    progress = loop_sim.tranche_progress(entries, labels)
    assert progress == {
        "T1a": {"open": 0, "needs_resume": 1, "settled": 1},
        "T1b": {"open": 1, "needs_resume": 1, "settled": 0},
        "T3pr": {"open": 0, "needs_resume": 0, "settled": 1},
    }
    finished: dict[str, str] = {}
    settled: dict[str, str] = {}
    loop_sim.note_finished(progress, "2026-11-21", finished, settled)
    assert finished == {"T3pr": "2026-11-21"}
    assert settled == {"T1a": "2026-11-21", "T3pr": "2026-11-21"}


def test_the_t3pr_draft_refuses_a_bad_evenings_list_before_queuing_anything(tmp_path):
    # Finding 3: a lower bound, a calendar check and a non-empty list, all before the nights.
    night = plan.Planned("T3pr", plan.PORE, date(2026, 1, 21), plan.NIGHT, "T3pr night, winter")
    allowed = [date(2025, 7, 16), date(2025, 7, 17), date(2026, 5, 30)]
    text = plan.t3pr_script([night], date(2026, 5, 30), allowed)
    q = str(_queue_file(tmp_path))
    # A glob in EVENINGS must not expand to a file named like an allowed day: the decoy sits
    # where the draft runs, in the repo's workers/capture (it cds there before the check).
    workdir = tmp_path / "repo" / "workers" / "capture"
    workdir.mkdir(parents=True)
    (workdir / "2025-07-17").write_text("")
    bad = ("2022-07-15", "2023-06-15", "2019-01-01", "2025-02-30", " ", "2025-07-1?", "*")
    for evenings in (*bad, "2026-05-31", "2025-07-16 2026-06-01", "2025-07-16 --through"):
        rc, calls, err = run_draft(tmp_path, text, Q=q, EVENINGS=evenings)
        assert rc == 1 and calls == [] and "refused" in err, evenings
    rc, calls, _ = run_draft(tmp_path, text, Q=q, EVENINGS="2025-07-16 2026-05-30")
    assert rc == 0
    assert calls == [
        f"{ADD} point-reyes.beach 2026-01-21 --hires --pick hourly",
        f"{ADD} point-reyes.beach 2025-07-16 2026-05-30 --hires --pick all --min-sun-deg -2",
        LIST,
    ]
    # Written without the plan's T1 days, the draft refuses every day (fails closed).
    rc, calls, _ = run_draft(tmp_path, plan.t3pr_script([night]), Q=q, EVENINGS="2025-07-16")
    assert rc == 1 and calls == []


def test_the_t3pr_draft_allows_only_point_reyes_t1_days_that_pass_the_edge(built, tmp_path):
    cams, planned = built
    allowed = plan.t3pr_allowed_days(planned, cams)
    t1 = {p.day for p in planned if p.vid == plan.PORE and p.tranche in ("T1a", "T1b")}
    assert allowed and set(allowed) == t1 and max(allowed) == date(2026, 5, 30)
    assert min(allowed) >= plan.SPANS[plan.PORE].regular_start  # never TQ or the 3:2 epoch
    plan.write_scripts(tmp_path, planned, cams, "a test plan")
    text = (tmp_path / "draft-T3pr.sh").read_text()
    words = set(re.findall(r"\d{4}-\d{2}-\d{2}", "".join(re.findall(r'T3PR_DAYS="(.*)"', text))))
    assert words == {d.isoformat() for d in allowed}


def test_drafts_refuse_a_q_that_is_not_an_existing_queue_file(tmp_path):
    # Finding 8: 'archive queue add' would start a new queue at a mistyped path.
    items = [plan.Planned("T1b", plan.PORE, date(2025, 9, 17), plan.CORE, "w")]
    text = plan.script(["# test"], items)
    q = _queue_file(tmp_path)
    for bad in (str(tmp_path / "q dir" / "queue.json"), "queue.json", str(tmp_path)):
        rc, calls, err = run_draft(tmp_path, text, Q=bad)  # missing, relative, a folder
        assert rc == 1 and calls == [] and "refused" in err, bad
    empty = tmp_path / "empty.json"
    empty.write_text("")
    assert run_draft(tmp_path, text, Q=str(empty))[:2] == (1, [])
    rc, calls, _ = run_draft(tmp_path, text, Q=str(q))
    assert rc == 0
    assert calls == [
        f"{ADD} point-reyes.beach 2025-09-17 --hires --pick hourly --min-sun-deg 2",
        LIST,
    ]


def test_the_tq_draft_queues_only_the_cameras_the_owner_approved(tmp_path):
    # Finding 9: TQ is the owner's question; nothing is queued without TQ_CAMERAS.
    items = [
        plan.Planned("TQ", plan.PORE, date(2023, 6, 14), plan.CORE, "TQ owner question: pore"),
        plan.Planned("TQ", plan.GRCA, date(2024, 6, 19), plan.CORE, "TQ owner question: grca"),
    ]
    text = plan.tq_script(["# test"], items)
    q = str(_queue_file(tmp_path))
    rc, calls, _ = run_draft(tmp_path, text, Q=q)  # TQ_CAMERAS unset
    assert rc != 0 and calls == []
    for bad in ("", " ", plan.BIBE, f"{plan.PORE} grand-teton.science-center", "*"):
        rc, calls, _ = run_draft(tmp_path, text, Q=q, TQ_CAMERAS=bad)
        assert rc != 0 and calls == [], bad
    pore = f"{ADD} point-reyes.beach 2023-06-14 --hires --pick hourly --min-sun-deg 2"
    grca = f"{ADD} grand-canyon.yavapai 2024-06-19 --hires --pick hourly --min-sun-deg 2"
    assert run_draft(tmp_path, text, Q=q, TQ_CAMERAS=plan.PORE)[:2] == (0, [pore, LIST])
    both = run_draft(tmp_path, text, Q=q, TQ_CAMERAS=f"{plan.GRCA} {plan.PORE}")
    assert both[:2] == (0, [pore, grca, LIST])


def test_schedule_reports_plan_targets_and_the_measured_misses():
    # Finding 4: PLAN.md's dates beside the finish days, with T3pr ahead of T1b.
    def item(tranche: str, n: float) -> plan.Planned:
        return plan.Planned(tranche, plan.PORE, date(2025, 7, 1), plan.CORE, "w", 0, n)

    planned = [item("T1a", 3864), item("T3pr", 318), item("T1b", 4765), item("T2", 10975)]
    planned += [item("T1c", 4715), item("TQ", 3253)]
    rows = {r["tranche"]: r for r in plan.schedule(planned, date(2026, 11, 21), 1000)["rows"]}
    low, high = plan.ERROR_RATES  # 1.0 % and 3.7 %
    assert rows["T1a"]["plan_target"] == "2026-11-24"
    assert rows["T1a"]["days_after_plan_target"] == {low: 0, high: 1}
    assert rows["T3pr"]["days_after_plan_target"][low] == -4  # 25 Nov, due 29 Nov
    assert rows["T1b"]["days_after_plan_target"] == {low: 1, high: 1}  # 30 Nov
    # Nine UTC days through 29 Nov give 8,910 useful requests; T1a + T3pr + T1b need 8,947.
    assert rows["T1b"]["requests_short_by_target"][low] == 37
    assert rows["T2"]["days_after_plan_target"] == {low: 1, high: 1}  # 11 Dec, due 10 Dec
    assert rows["T1c"]["days_after_plan_target"] == {low: 0, high: 1}
    assert rows["T3pr"]["requests_short_by_target"][low] == 0
    assert rows["TQ"]["plan_target"] is None and "days_after_plan_target" not in rows["TQ"]


def test_a_tq_block_moved_ahead_of_t2_gets_its_own_finish_day():
    # Finding 5: TQ's Point Reyes block, if the owner approves it, before T2.
    def item(tranche: str, vid: str, day: date, n: float) -> plan.Planned:
        return plan.Planned(tranche, vid, day, plan.CORE, "w", 0, n)

    june = date(2023, 6, 14)
    planned = [
        item("T1a", plan.PORE, date(2025, 7, 1), 3864),
        item("T3pr", plan.PORE, date(2025, 7, 2), 318),
        item("T1b", plan.PORE, date(2025, 9, 17), 4765),
        item("T2", plan.GRCA, date(2025, 7, 3), 10975),
        item("TQ", plan.PORE, june, 857),
        item("TQ", plan.GRCA, date(2024, 6, 19), 767),
    ]
    moved = plan.with_block_before(planned, plan.PORE, "TQ", "T2", "TQ point-reyes.beach")
    assert [p.tranche for p in moved] == ["T1a", "T3pr", "T1b", "TQ point-reyes.beach", "T2", "TQ"]
    rows = {r["tranche"]: r for r in plan.schedule(moved, date(2026, 11, 21), 1000)["rows"]}
    assert rows["TQ point-reyes.beach"]["finishes_utc_day"][next(iter(plan.ERROR_RATES))] == (
        "2026-11-30"  # 9,804 requests / 990 a day
    )
    # An entry for the same day between the two places would change the block's cost.
    crossing = [*planned[:4], item("T2", plan.PORE, june, 12), *planned[4:]]
    with pytest.raises(ValueError, match="change the cost"):
        plan.with_block_before(crossing, plan.PORE, "TQ", "T2", "TQ point-reyes.beach")


def test_replanning_after_t3pr_is_queued_plans_no_t3pr_twice():
    # Finding 6: once the nights and some real clear days are queued, a re-plan labels them
    # T3pr and plans only the clear days still missing.
    cams = six_cameras()
    comp = dict.fromkeys(plan.ORDER, 0.95)
    t1a = plan.weeks_in_order(plan.core_weeks(cams), "A")
    queue = [queue_entry(plan.Planned("T1a", v, d, o, w)) for v, d, o, w in t1a]
    before = plan.build(cams, comp, queue)
    t3 = [p for p in before if p.tranche == "T3pr"]
    queue += [queue_entry(p) for p in before if p.tranche == "T1b"]
    queue += [queue_entry(p) for p in t3 if p.opts is plan.NIGHT]
    model = {p.day for p in t3 if p.opts is plan.EVENING}
    real = [d for v, d, _o, _w in t1a if v == plan.PORE and d not in model]
    for chosen, planned_left in ((2, 4), (6, 0), (7, 0)):
        evenings = [plan.Planned("T3pr", plan.PORE, d, plan.EVENING, "real") for d in real[:chosen]]
        after = plan.build(cams, comp, queue + [queue_entry(p) for p in evenings])
        pore = [p for p in after if p.vid == plan.PORE and p.opts in (plan.NIGHT, plan.EVENING)]
        assert {p.tranche for p in pore} == {"T3pr"}, chosen
        assert sum(1 for p in pore if p.opts is plan.NIGHT) == 4  # the queued four only
        assert all(p.status != "planned" for p in pore if p.opts is plan.NIGHT)
        clear = [p for p in pore if p.opts is plan.EVENING]
        assert sum(1 for p in clear if p.status == "planned") == planned_left, chosen
        assert len(clear) == max(plan.T3PR_EVENINGS, chosen)
        assert not [p for p in after if p.tranche == "other"]


def test_t3pr_clear_days_come_from_the_newest_warm_weeks_and_can_be_none():
    # Finding 11: the cost model's clear days are the Wednesdays of the six newest warm weeks.
    mondays = [date(2024, 6, 17), date(2024, 7, 22), date(2024, 8, 19), date(2025, 6, 9)]
    mondays += [date(2025, 7, 14), date(2025, 8, 11), date(2025, 9, 15), date(2025, 10, 13)]
    planned = _planned([d for m in mondays for d in plan.iso_week(m)], "T1a")
    clear = [d for _, d, opts, _ in plan.t3_point_reyes(planned) if opts is plan.EVENING]
    assert clear == [m + timedelta(days=2) for m in mondays[2:]]
    none = plan.t3_point_reyes(planned, evenings=0, seasons_queued={"summer"})
    assert not [d for _, d, opts, _ in none if opts is plan.EVENING]  # not all of them ([-0:])
    assert [why for *_, why in none] == ["T3pr night frames, autumn"]  # summer's is queued


def test_completeness_uses_a_cameras_16x9_days_once_there_are_ten():
    measured = {
        vid: {
            "n_days_16x9": plan.MIN_COMPLETENESS_DAYS,
            "completeness_hourly_sun2_16x9": 0.95,
            "completeness_hourly_sun2": 0.80,
        }
        for vid in plan.ORDER
    }
    measured[plan.BIBE] = {**measured[plan.BIBE], "n_days_16x9": plan.MIN_COMPLETENESS_DAYS - 1}
    got = plan.completeness_of(measured)
    assert got[plan.PORE] == 0.95 and got[plan.BIBE] == 0.80


def test_a_queued_entry_on_its_last_sweep_gets_no_retry():
    cam = camera()
    p = plan.Planned("T1a", plan.PORE, date(2025, 7, 19), plan.CORE, "w")
    selected = [{"AssetID": f"{i:08x}-0000"} for i in range(15)]
    outcomes = {f"{i:08x}-0000:hires": ("failed" if i < 2 else "stored") for i in range(15)}

    def entry(sweep: int) -> dict:
        return queue_entry(p, "active", selected=selected, next=15, sweep=sweep, outcomes=outcomes)

    assert plan.remaining_of_queued(cam, entry(2), plan.CORE, 0.95, []) == (0, 2.0, 15)
    assert plan.remaining_of_queued(cam, entry(plan.MAX_SWEEPS), plan.CORE, 0.95, []) == (
        0,
        0.0,
        15,
    )


def test_the_planner_blocks_name_lookups_and_datagrams_as_well_as_connections():
    # Finding 12: a name lookup would reach the resolver even though the connect fails.
    import socket

    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "create_connection"):
        assert getattr(socket, name) is plan._no_network, name
    for name in ("connect", "connect_ex", "sendto", "sendmsg"):
        assert getattr(socket.socket, name) is plan._no_network, name
    with pytest.raises(RuntimeError, match="never opens a network connection"):
        socket.getaddrinfo("localhost", 80)


def test_the_data_dir_guards_hold_under_python_dash_o(tmp_path, monkeypatch):
    # Finding 12: explicit checks, so python -O (which strips asserts) cannot skip them.
    data = tmp_path / "data"
    data.mkdir()
    out = data / "out"
    code = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('plan', sys.argv[1])\n"
        "m = importlib.util.module_from_spec(spec); sys.modules['plan'] = m\n"
        "spec.loader.exec_module(m)\n"
        "m.main([sys.argv[2], '--queue', sys.argv[3]])\n"
    )
    plan_py = str(Path(__file__).with_name("plan.py"))
    env = {**os.environ, "SNOW_GLOBE_DATA_DIR": str(data)}
    args = [sys.executable, "-O", "-c", code, plan_py, str(out), str(tmp_path / "q.json")]
    proc = subprocess.run(args, env=env, capture_output=True, text=True)
    assert proc.returncode == 1 and "refused: output folder" in proc.stderr, proc.stderr[-400:]
    assert not out.exists()
    monkeypatch.setattr(plan, "DATA", data)
    with pytest.raises(SystemExit, match="refused: scratch queue"):
        plan.write_queue(data / "state" / "archive" / "queue.json", [], {})
    plan.refuse_under_data(tmp_path / "data-other" / "x", "a sibling is not inside")  # no raise
