#!/usr/bin/env python3
"""Design-H archive backfill plan: costs, tranches and scratch queues (offline only).

Feeds docs/research/archive-backfill-plan.md (PRD §8.5, §13.3-13.9, §15.3, §16; D-024,
D-029, D-042). It never sends a request: sockets are disabled below before anything else
runs.

Reads, read-only and metadata only:
- ~/lookout-data/cache/nps-archive/days/<code>-20261119.json (GetAvailableDays masks);
- ~/lookout-data/cache/nps-archive/search/<code>/<date>.json, dates before 2026-06-01 only;
- ~/lookout-data/state/archive/<viewpoint_id>.json (what is already stored);
- the viewpoint manifests in the repo (found from lookout_common's location).

Writes only under OUT (argv[1]) and its parent folder: plan.json, analysis.json,
hrrr-valid-hours.json, first-tranche.sh and the draft scripts in OUT; queue.json (the first
tranche) and queue-plan-all.json (every tranche) in OUT's parent. It refuses to write a
queue file under ~/lookout-data.

It needs capture's queue code and eval's extractor and solar code, so run it from eval/:
    PYTHONPATH=src:../workers/common/src:../workers/capture/src \
        uv run --no-sync python <this file> <OUT>
"""

from __future__ import annotations

import json
import math
import shlex
import socket
import statistics as st
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


def _no_network(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("the backfill planner never opens a network connection")


socket.socket.connect = _no_network  # type: ignore[method-assign]
socket.socket.connect_ex = _no_network  # type: ignore[method-assign]
socket.create_connection = _no_network  # type: ignore[assignment]

from lookout_capture.archive import ArchiveAsset, AvailableDays, pick_assets  # noqa: E402
from lookout_capture.archive_queue import ArchiveQueue, make_entry  # noqa: E402
from lookout_capture.heldout import HELD_OUT_FROM  # noqa: E402
from lookout_capture.sun import solar_elevation_deg  # noqa: E402
from lookout_common.manifest import load_manifests  # noqa: E402

from lookout_eval.extractor import (  # noqa: E402
    ClearDayReference,
    EdgeStats,
    ExtractorParams,
    ReferenceEntry,
    Region,
)
from lookout_eval.noaa_solar import solar_position  # noqa: E402

DATA = Path.home() / "lookout-data"
DAYS_DIR = DATA / "cache" / "nps-archive" / "days"
SEARCH_DIR = DATA / "cache" / "nps-archive" / "search"
STATE_DIR = DATA / "state" / "archive"
QUARANTINE = DATA / "quarantine"

# --- measured inputs (scripts/measure_inputs.py re-measures each one) ----------------------
#: Mean bytes of the 9 pre-guard hires assets on disk (sidecars' content_length).
HIRES_BYTES_MEAN = 54_141
HIRES_BYTES_MIN = 10_419
HIRES_BYTES_MAX = 101_326
#: Mean bytes of the 92 successful day searches of the 19 Nov archive check (capture log).
SEARCH_BYTES_MEAN = 49_372
#: Mean file size of the 85 pre-guard archive sidecars (1,618.7 B).
SIDECAR_BYTES_MEAN = 1_619
#: Mean size of the 76 pre-guard cached day searches (cache/nps-archive/search/).
SEARCH_CACHE_BYTES_MEAN = 24_462
#: Capture-log bytes per asset GET (one archive_request line, mean 299 B, and one stored
#: line, mean 376 B) and per search (one archive_request line), 19 Nov log.
LOG_BYTES_PER_GET = 299 + 376
LOG_BYTES_PER_SEARCH = 299
#: Errors per request in the 19 Nov check, leaving out the 2 deliberate paging probes: 8/215.
ERROR_RATE = 8 / 215
#: Requests the queue may send per UTC day (archive_queue.DEFAULT_DAILY_BUDGET).
DAILY_BUDGET = 1000
#: The first UTC day of queue work.
FIRST_DAY = date(2026, 11, 20)
#: Measured transfer of one HRRR forecast hour, 12 fields (the backtest pair's evidence).
HRRR_MB_PER_FORECAST_HOUR = 16.5

#: The queue options of every scored-data entry: full frames, the frame nearest each
#: local hour, NPS (geometric) Sun >= 2 deg. The daylight rule is apparent >= 3 deg, about
#: geometric >= 2.75 deg, so 2 deg keeps every frame P1/K2 could score.
CORE: dict[str, Any] = {"kinds": ("hires",), "pick": "hourly", "min_sun_deg": 2.0}
#: Epoch and view checks: the single highest-Sun frame of the day, full size.
MAP: dict[str, Any] = {"kinds": ("hires",), "pick": "midday", "min_sun_deg": None}
#: T3pr night frames (PRD §8.5 step 2): every hour of the day, any Sun.
NIGHT: dict[str, Any] = {"kinds": ("hires",), "pick": "hourly", "min_sun_deg": None}
#: T3pr clear days (PRD §8.5 step 8, pose check part 2): every 15-min frame with NPS Sun >= -2.
EVENING: dict[str, Any] = {"kinds": ("hires",), "pick": "all", "min_sun_deg": -2.0}
#: Clear days the T3pr allowance plans for (chosen later from T1 frames).
T3PR_EVENINGS = 6

PORE = "point-reyes.beach"
ORDER = [
    PORE,
    "grand-canyon.yavapai",
    "yosemite.turtleback",
    "acadia.mcfarland",
    "smokies.look-rock",
    "big-bend.panther-junction",
]
TRANCHES = ("T0", "T1a", "T1b", "T3pr", "T2", "T1c")

#: Archive check (docs/research/nps-archive-check.md): last 3:2 and first 16:9 sample, and
#: the first date this plan treats as certainly inside the current 16:9 view.
EPOCH = {
    PORE: (date(2023, 1, 16), date(2023, 7, 15), date(2023, 7, 15)),
    "grand-canyon.yavapai": (date(2024, 1, 15), date(2024, 7, 15), date(2024, 7, 15)),
    "yosemite.turtleback": (date(2023, 7, 15), date(2024, 1, 15), date(2024, 1, 15)),
    "acadia.mcfarland": (date(2023, 7, 15), date(2024, 1, 15), date(2024, 1, 15)),
    "smokies.look-rock": (date(2024, 1, 15), date(2024, 7, 15), date(2024, 7, 15)),
    # Big Bend: 16:9 by 2024-01-15, but that frame is "framed lower" (D-024); 2025-01-15
    # and 2026-01-15 match, so the plan's bulk starts with the week of 2025-01-15.
    "big-bend.panther-junction": (date(2023, 7, 15), date(2024, 1, 15), date(2025, 1, 13)),
}
LAST_DAY = HELD_OUT_FROM - timedelta(days=1)  # 2026-05-31
#: T0's epoch grid aims to bracket each switch to within this many days.
T0_MAX_GAP_DAYS = 14
#: A day added to split a wide T0 gap keeps at least this many days from both ends.
T0_MIN_SPLIT_DAYS = 3

SEASONS = {
    "winter": (12, 1, 2),
    "spring": (3, 4, 5),
    "summer": (6, 7, 8),
    "autumn": (9, 10, 11),
}


# --- small helpers ------------------------------------------------------------------------


def daterange(a: date, b: date) -> list[date]:
    """Every day from a to b inclusive."""
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


def iso_week(day: date) -> list[date]:
    """The ISO week (Monday to Sunday) holding ``day``."""
    monday = day - timedelta(days=day.weekday())
    return [monday + timedelta(days=i) for i in range(7)]


def std_offset(tz: ZoneInfo) -> timedelta:
    """The zone's standard-time offset (NPS ImageCreateDateTime is on standard time)."""
    probe = datetime(2026, 1, 15, 12, tzinfo=tz)
    offset = probe.utcoffset()
    assert offset is not None
    return offset - (probe.dst() or timedelta(0))


def nps_stamp(wall: datetime) -> str:
    """``MM/DD/YYYY hh:mm:ss AM`` as NPS writes ImageCreateDateTime."""
    return wall.strftime("%m/%d/%Y %I:%M:%S %p")


@dataclass
class Camera:
    """What the planner knows about one archive camera."""

    vp: Any
    code: str
    tz: ZoneInfo
    lat: float
    lon: float
    avail: AvailableDays
    cached: dict[date, list[ArchiveAsset]] = field(default_factory=dict)
    stored_hires: set[str] = field(default_factory=set)

    def available(self, day: date) -> bool:
        """True if the 19 Nov mask lists frames on ``day``."""
        return self.avail.is_available(day)

    def synthetic_day(self, day: date) -> list[ArchiveAsset]:
        """A complete day at the 15-min cadence: NPS-style stamps, geometric Sun."""
        off = std_offset(self.tz)
        out = []
        for minute in range(0, 1440, 15):
            wall = datetime(day.year, day.month, day.day) + timedelta(minutes=minute)
            utc = (wall - off).replace(tzinfo=UTC)
            out.append(
                ArchiveAsset(
                    asset_id=f"syn-{self.code}-{wall:%Y%m%dT%H%M}",
                    location_code=self.code,
                    image_create_datetime=nps_stamp(wall),
                    local_time_string="",
                    solar_elevation_deg=solar_elevation_deg(utc, self.lat, self.lon),
                    time_of_day=None,
                )
            )
        return out

    def day_assets(self, day: date) -> tuple[list[ArchiveAsset], bool]:
        """The cached real search for ``day`` if there is one, else a synthetic complete day."""
        if day in self.cached:
            return self.cached[day], True
        return self.synthetic_day(day), False

    def utc_of(self, asset: ArchiveAsset) -> datetime:
        """UTC image time of an asset (ImageCreateDateTime is local standard time)."""
        wall = datetime.strptime(asset.image_create_datetime, "%m/%d/%Y %I:%M:%S %p")
        return (wall - std_offset(self.tz)).replace(tzinfo=UTC)


def load_cameras() -> dict[str, Camera]:
    """Manifests, availability masks, pre-guard cached searches and stored hires."""
    viewpoints = load_manifests()
    cams: dict[str, Camera] = {}
    for vid in ORDER:
        vp = viewpoints[vid]
        code = vp.truth_camera.archive_code
        raw = json.loads((DAYS_DIR / f"{code}-20261119.json").read_text())
        cam = Camera(
            vp=vp,
            code=code,
            tz=ZoneInfo(vp.time_zone),
            lat=vp.camera.lat,
            lon=vp.camera.lon,
            avail=AvailableDays.from_json(code, raw),
        )
        for path in sorted((SEARCH_DIR / code).glob("*.json")):
            day = date.fromisoformat(path.stem)
            if day >= HELD_OUT_FROM:
                continue  # metadata is allowed for any date, but nothing here needs it
            doc = json.loads(path.read_text())
            cam.cached[day] = [ArchiveAsset.from_result({"Asset": a}) for a in doc["assets"]]
        state = json.loads((STATE_DIR / f"{vid}.json").read_text())
        for asset_id, kinds in (state.get("completed") or {}).items():
            if "hires" in kinds:
                cam.stored_hires.add(asset_id)
        cams[vid] = cam
    return cams


# --- measurement: picks per day ---------------------------------------------------------

OPTIONS = {
    "hourly/any-sun": ("hourly", None),
    "hourly/sun>=0": ("hourly", 0.0),
    "hourly/sun>=2": ("hourly", 2.0),
    "hourly/sun>=3": ("hourly", 3.0),
    "all/sun>=0": ("all", 0.0),
    "all/sun>=2": ("all", 2.0),
    "all/any-sun": ("all", None),
    "midday": ("midday", None),
}


def picks_on_cached_days(cams: dict[str, Camera]) -> dict[str, Any]:
    """Frames each queue option selects on the cached (real) pre-guard day searches."""
    out: dict[str, Any] = {}
    ratios_all: list[float] = []
    for vid, cam in cams.items():
        rows = []
        ratios = []
        for day, assets in sorted(cam.cached.items()):
            row: dict[str, Any] = {"date": day.isoformat(), "assets": len(assets)}
            for name, (pick, sun) in OPTIONS.items():
                row[name] = len(pick_assets(assets, pick=pick, min_sun_deg=sun))
            syn = len(pick_assets(cam.synthetic_day(day), pick="hourly", min_sun_deg=2.0))
            row["hourly/sun>=2 if complete"] = syn
            if syn:
                ratios.append(row["hourly/sun>=2"] / syn)
            rows.append(row)
        ratios_all.extend(ratios)
        out[vid] = {
            "days": rows,
            "completeness_hourly_sun2": round(st.mean(ratios), 3) if ratios else None,
        }
    out["pooled_completeness_hourly_sun2"] = round(st.mean(ratios_all), 3)
    out["pooled_completeness_n_days"] = len(ratios_all)
    return out


# --- cost of one entry ---------------------------------------------------------------------


@dataclass
class Planned:
    """One planned queue entry and its expected cost."""

    tranche: str
    vid: str
    day: date
    opts: dict[str, Any]
    why: str
    search_requests: int = 0
    asset_requests: float = 0.0
    selected_if_complete: int = 0


def cost(
    cam: Camera,
    day: date,
    opts: dict[str, Any],
    completeness: float,
    stored_by: Sequence[dict[str, Any]] = (),
) -> tuple[int, float, int]:
    """(search requests, expected asset GETs, assets if the day were complete).

    ``stored_by`` lists the options of earlier entries for the same day: their search is
    cached by then (no search request), and frames they stored cost no GET (the queue
    marks them ``existing``).
    """
    pick, sun = opts["pick"], opts["min_sun_deg"]
    assets, real = cam.day_assets(day)
    chosen = pick_assets(assets, pick=pick, min_sun_deg=sun)
    have = {a.asset_id for a in chosen if a.asset_id in cam.stored_hires}
    for earlier in stored_by:
        have |= {
            a.asset_id
            for a in pick_assets(assets, pick=earlier["pick"], min_sun_deg=earlier["min_sun_deg"])
        }
    new = [a for a in chosen if a.asset_id not in have]
    searches = 0 if (real or stored_by) else 1
    if real:  # a usable cached search: exact selection
        return searches, float(len(new)), len(chosen)
    factor = 1.0 if pick == "midday" else completeness
    return searches, len(new) * factor, len(chosen)


# --- the tranches --------------------------------------------------------------------------


def nearest_available(cam: Camera, target: date, reach: int = 3) -> date | None:
    """The available day nearest ``target`` within ``reach`` days (earlier first on ties)."""
    for step in range(reach + 1):
        for cand in (target - timedelta(days=step), target + timedelta(days=step)):
            if cand <= LAST_DAY and cam.available(cand):
                return cand
    return None


def grid_days(cam: Camera, start: date, end: date, step: int) -> list[date]:
    """One available day near each grid point from ``start`` to ``end`` (PRD §13.9 epochs)."""
    out = []
    day = start
    while day <= end:
        got = nearest_available(cam, day)
        if got is not None and got < HELD_OUT_FROM:
            out.append(got)
        day += timedelta(days=step)
    return out


def fill_gaps(
    cam: Camera,
    days: Iterable[date],
    lo: date,
    hi: date,
    *,
    max_gap: int = T0_MAX_GAP_DAYS,
    min_split: int = T0_MIN_SPLIT_DAYS,
) -> list[date]:
    """Available days that split every gap longer than ``max_gap`` where the mask allows.

    The anchors are ``lo`` and ``hi`` (the archive check's last 3:2 and first 16:9 samples)
    and ``days``. While a gap is too wide and an available day lies at least ``min_split``
    days inside it, the one nearest the gap's middle is added. Returns the added days.
    """
    anchors = sorted({lo, hi, *days})
    added: list[date] = []
    while True:
        for a, b in pairwise(anchors):
            if (b - a).days <= max_gap:
                continue
            inner = daterange(a + timedelta(days=min_split), b - timedelta(days=min_split))
            candidates = [d for d in inner if d < HELD_OUT_FROM and cam.available(d)]
            if candidates:
                middle = a + timedelta(days=(b - a).days // 2)
                best = min(candidates, key=lambda d: (abs((d - middle).days), d))
                anchors = sorted({*anchors, best})
                added.append(best)
                break
        else:
            return sorted(added)


def tranche0(cams: dict[str, Camera]) -> list[tuple[str, date, dict[str, Any], str]]:
    """Epoch switch dating and view checks: midday hires on a 14-day grid (PRD §13.9)."""
    plan: list[tuple[str, date, dict[str, Any], str]] = []
    epoch_why = "T0 epoch: date the 3:2 -> 16:9 switch from the frame size in the sidecar"
    for vid in ORDER:
        cam = cams[vid]
        last32, first169, _safe = EPOCH[vid]
        days = grid_days(cam, last32 + timedelta(days=14), first169 - timedelta(days=1), 14)
        days = sorted({*days, *fill_gaps(cam, days, last32, first169)})
        plan.extend((vid, d, MAP, epoch_why) for d in days)
        plan.append(
            (vid, first169, MAP, "T0 epoch: 16:9 hires reference for registration (search cached)")
        )
    # Big Bend (D-024): "check 2024-01 for a framing change": the year after the switch.
    bibe = "big-bend.panther-junction"
    bibe_why = "T0 view: Big Bend framing from 2024-01 until it matches 2025-01/2026-01 (D-024)"
    plan.extend(
        (bibe, d, MAP, bibe_why)
        for d in grid_days(cams[bibe], date(2024, 1, 29), date(2025, 1, 12), 14)
    )
    plan.append((bibe, date(2025, 1, 15), MAP, "T0 view: 16:9 reference (cached)"))
    plan.append((bibe, date(2026, 1, 15), MAP, "T0 view: 16:9 reference (cached)"))
    # Grand Canyon (D-024): 2025-01-15 lacks the bottom-left wedge; weekly Nov-Mar.
    grca = "grand-canyon.yavapai"
    plan.extend(
        (grca, d, MAP, "T0 view: Grand Canyon winter 2024/25 possible temporary re-aim (D-024)")
        for d in grid_days(cams[grca], date(2024, 11, 4), date(2025, 3, 31), 7)
    )
    plan.append((grca, date(2025, 1, 15), MAP, "T0 view: the odd 2025-01-15 frame (cached)"))
    plan.append((grca, date(2025, 7, 15), MAP, "T0 view: 16:9 reference (cached)"))
    plan.append((grca, date(2026, 1, 15), MAP, "T0 view: 16:9 reference (cached)"))
    seen = set()
    out = []
    for item in plan:
        key = (item[0], item[1], item[2]["pick"])
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def level_of(vid: str, year: int, month: int) -> str:
    """A: the held-out season's months; B: shoulders (+ Point Reyes Dec/Jan); C: winter."""
    if month in (6, 7, 8) or (year, month) == (2026, 5):
        return "A"
    if month in (4, 5, 9, 10) or (vid == PORE and month in (12, 1)):
        return "B"
    return "C"


def core_weeks(cams: dict[str, Camera]) -> list[tuple[str, str, str, list[date], str]]:
    """One ISO week per month inside each safe 16:9 span (PRD §13.9 dev-fit/dev-check weeks).

    The week holding the 15th in odd years and the week after it in even years: the ISO
    week of the 15th has the same number every year for most months (June 24, August 33),
    so without the shift an ISO-parity split would put whole months on one side.
    Returns (level, slot, vid, available days, why), in no particular order.
    """
    weeks = []
    for vid in ORDER:
        cam = cams[vid]
        safe = EPOCH[vid][2]
        year, month = safe.year, safe.month
        while date(year, month, 1) <= LAST_DAY:
            days = iso_week(date(year, month, 15))
            if year % 2 == 0:
                days = [d + timedelta(days=7) for d in days]
            if days[0] < safe:  # the span's first month: the first full week on or after
                days = iso_week(safe + timedelta(days=(7 - safe.weekday()) % 7))
            slots = [days]
            if (year, month) == (2026, 5):  # also the last full week before the guard
                slots.append(iso_week(date(2026, 5, 31)))
            for w in slots:
                kept = [d for d in w if d <= LAST_DAY and cam.available(d)]
                if kept:
                    lvl = level_of(vid, year, month)
                    slot = f"{w[0].isoformat()}"
                    weeks.append((lvl, slot, vid, kept, f"T1{lvl.lower()} core week of {slot}"))
            month += 1
            if month == 13:
                year, month = year + 1, 1
    return weeks


def slot_rank(level: str, slot: str) -> tuple[int, ...]:
    """Order inside a level: newest summer first (closest to the held-out season)."""
    d = date.fromisoformat(slot)
    if level == "A":
        if d.year == 2026:
            return (1, -d.toordinal())  # May 2026 after summer 2025
        return (0 if d.year == 2025 else 2 if d.year == 2024 else 3, d.month, d.toordinal())
    return (-d.year, d.month, d.toordinal())


def summer_completion(
    cams: dict[str, Camera], taken: set[tuple[str, date]]
) -> list[tuple[str, date, dict[str, Any], str]]:
    """T2: every other available day of June-August inside each safe span."""
    out = []
    order = [(PORE, y) for y in (2025, 2024, 2023)]
    order += [(vid, y) for y in (2025, 2024) for vid in ORDER if vid != PORE]
    for vid, year in order:
        cam = cams[vid]
        safe = EPOCH[vid][2]
        for day in daterange(date(year, 6, 1), date(year, 8, 31)):
            if day < safe or (vid, day) in taken or not cam.available(day):
                continue
            out.append((vid, day, CORE, f"T2 summer completion {year}"))
    return out


def t3_point_reyes(planned: Sequence[Planned]) -> list[tuple[str, date, dict[str, Any], str]]:
    """T3pr: Point Reyes plate inputs for M2 (PRD §8.5 steps 2 and 8; PLAN M2), on T1 days.

    - **Nights:** one day per season, ``--pick hourly`` with no Sun limit: the night and
      twilight hours of the newest Point Reyes T1 week of the season (its Wednesday, or the
      planned day nearest it). Its daylight hours are stored by T1 already.
    - **Clear days (allowance):** :data:`T3PR_EVENINGS` days of every 15-min frame with NPS
      Sun >= -2 deg, for the sunrise and sunset pose check. The real days are clear ones
      chosen from T1 frames after T1a lands; the cost is modelled here on the Wednesdays of
      the newest Point Reyes T1 weeks in June-October.
    """
    t1 = sorted({p.day for p in planned if p.vid == PORE and p.tranche in ("T1a", "T1b")})
    mondays = sorted({d - timedelta(days=d.weekday()) for d in t1})

    def midweek(monday: date) -> date:
        week = [d for d in t1 if monday <= d < monday + timedelta(days=7)]
        wednesday = monday + timedelta(days=2)
        return min(week, key=lambda d: (abs((d - wednesday).days), d))

    out = []
    for name, months in SEASONS.items():
        newest = max(m for m in mondays if (m + timedelta(days=2)).month in months)
        out.append((PORE, midweek(newest), NIGHT, f"T3pr night frames, {name}"))
    warm = [m for m in mondays if (m + timedelta(days=2)).month in range(6, 11)]
    for monday in sorted(warm)[-T3PR_EVENINGS:]:
        out.append((PORE, midweek(monday), EVENING, "T3pr clear day (cost model)"))
    return out


def build(cams: dict[str, Camera], completeness: dict[str, float]) -> list[Planned]:
    """Every tranche in queue order: T0, T1a, T1b, T3pr, T2, T1c."""
    planned: list[Planned] = []

    def add(tranche: str, vid: str, day: date, opts: dict[str, Any], why: str) -> None:
        earlier = [p.opts for p in planned if p.vid == vid and p.day == day]
        s, g, n = cost(cams[vid], day, opts, completeness[vid], earlier)
        planned.append(Planned(tranche, vid, day, opts, why, s, g, n))

    for vid, day, opts, why in tranche0(cams):
        add("T0", vid, day, opts, why)

    weeks = core_weeks(cams)
    for level, tranche in (("A", "T1a"), ("B", "T1b")):
        rows = [w for w in weeks if w[0] == level]
        slots = sorted({w[1] for w in rows}, key=lambda s: slot_rank(level, s))
        for slot in slots:
            for vid in ORDER:
                for _lvl, sl, v, days, why in rows:
                    if sl == slot and v == vid:
                        for d in days:
                            add(tranche, vid, d, CORE, why)
    for vid, day, opts, why in t3_point_reyes(planned):
        add("T3pr", vid, day, opts, why)
    taken = {(p.vid, p.day) for p in planned if p.tranche.startswith("T1")}
    for vid, day, opts, why in summer_completion(cams, taken):
        add("T2", vid, day, opts, why)
    rows = [w for w in weeks if w[0] == "C"]
    slots = sorted({w[1] for w in rows}, key=lambda s: slot_rank("C", s))
    for slot in slots:
        for vid in ORDER:
            for _lvl, sl, v, days, why in rows:
                if sl == slot and v == vid:
                    # Grand Canyon's Dec 2024 - Feb 2025 wait for T0's re-aim check.
                    if vid == "grand-canyon.yavapai" and (
                        date(2024, 11, 25) <= days[0] <= date(2025, 2, 28)
                    ):
                        continue
                    for d in days:
                        add("T1c", vid, d, CORE, why)
    return planned


# --- throughput ---------------------------------------------------------------------------


def loop_throughput(
    pass_s: float = 9.3, request_s: float = 0.62, interval: float = 300.0, jitter: float = 30.0
) -> dict[str, float]:
    """Archive requests per hour of loop uptime with no errors, analytically (PRD §15.3).

    The loop's next pass is due ``interval`` + U(0, ``jitter``) s after the last one started
    (live.py), so the idle window is that minus the pass. A request starts only while the
    limiter's wait + 30 s + 5 s fits (STEP_MAX_S + STEP_MARGIN_S). The first one needs 35 s,
    every later one waits 5 s after the previous one ended, so request k starts if
    (k - 1) * request_s + (k - 2) * 5 <= window - 40. Errors are left out here: each costs its
    own timeout plus a limiter deferral of 30 s that doubles on consecutive failures
    (archive.py ``_send``); scripts/loop_sim.py measures that on the real runner.
    """
    window = interval + jitter / 2 - pass_s
    per_window = math.floor((window - 30.0 + request_s) / (5.0 + request_s))
    windows_per_hour = 3600.0 / (interval + jitter / 2)
    per_hour = per_window * windows_per_hour
    return {
        "idle_window_s": round(window, 1),
        "requests_per_window": per_window,
        "windows_per_hour": round(windows_per_hour, 2),
        "requests_per_hour": round(per_hour),
        "hours_for_daily_budget": round(DAILY_BUDGET / per_hour, 2),
    }


def schedule(planned: list[Planned]) -> dict[str, Any]:
    """Cumulative requests per tranche and the UTC day each finishes at 1,000 a day."""
    rows = []
    cum = 0.0
    per_day_useful = DAILY_BUDGET * (1 - ERROR_RATE)
    for tranche in TRANCHES:
        items = [p for p in planned if p.tranche == tranche]
        searches = sum(p.search_requests for p in items)
        gets = sum(p.asset_requests for p in items)
        reqs = searches + gets
        cum += reqs
        days_needed = cum / per_day_useful
        finish = FIRST_DAY + timedelta(days=math.ceil(days_needed) - 1)
        rows.append(
            {
                "tranche": tranche,
                "entries": len(items),
                "viewpoint_days": len({(p.vid, p.day) for p in items}),
                "search_requests": searches,
                "asset_requests": round(gets),
                "requests": round(reqs),
                "cumulative_requests": round(cum),
                "asset_bytes_mean": round(gets * HIRES_BYTES_MEAN),
                "asset_bytes_low": round(gets * HIRES_BYTES_MIN),
                "asset_bytes_high": round(gets * HIRES_BYTES_MAX),
                "search_bytes": round(searches * SEARCH_BYTES_MEAN),
                "disk_bytes_mean": round(
                    gets * (HIRES_BYTES_MEAN + SIDECAR_BYTES_MEAN + LOG_BYTES_PER_GET)
                    + searches * (SEARCH_CACHE_BYTES_MEAN + LOG_BYTES_PER_SEARCH)
                ),
                "budget_days_cumulative": round(days_needed, 2),
                "finishes_utc_day": finish.isoformat(),
            }
        )
    totals = {
        "entries": len(planned),
        "requests": round(sum(p.search_requests + p.asset_requests for p in planned)),
        "asset_requests": round(sum(p.asset_requests for p in planned)),
        "search_requests": sum(p.search_requests for p in planned),
    }
    gets = sum(p.asset_requests for p in planned)
    searches = sum(p.search_requests for p in planned)
    totals |= {
        "asset_gb_mean": round(gets * HIRES_BYTES_MEAN / 1e9, 3),
        "asset_gb_low": round(gets * HIRES_BYTES_MIN / 1e9, 3),
        "asset_gb_high": round(gets * HIRES_BYTES_MAX / 1e9, 3),
        "sidecar_mb": round(gets * SIDECAR_BYTES_MEAN / 1e6, 1),
        "search_cache_mb": round(searches * SEARCH_CACHE_BYTES_MEAN / 1e6, 1),
        "log_mb": round((gets * LOG_BYTES_PER_GET + searches * LOG_BYTES_PER_SEARCH) / 1e6, 1),
    }
    return {"useful_requests_per_day": round(per_day_useful), "rows": rows, "totals": totals}


def per_viewpoint(planned: list[Planned]) -> dict[str, Any]:
    """Days and requests per viewpoint and tranche, and each viewpoint's frame bytes."""
    out: dict[str, Any] = {}
    for p in planned:
        row = out.setdefault(p.vid, Counter())
        if p.opts is CORE:
            row[f"{p.tranche}:days"] += 1
        row[f"{p.tranche}:entries"] += 1
        row[f"{p.tranche}:requests"] += p.search_requests + p.asset_requests
        row["asset_requests_all"] += p.asset_requests
    for vid, row in out.items():
        row["asset_gb_mean_all"] = row["asset_requests_all"] * HIRES_BYTES_MEAN / 1e9
        row["asset_gb_high_all"] = row["asset_requests_all"] * HIRES_BYTES_MAX / 1e9
        out[vid] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in row.items()}
    return out


# --- Sun-bin coverage of the held-out season -------------------------------------------

#: The extractor's own parameters (5 x 20 deg bins, one bin step of fallback).
PARAMS = ExtractorParams()
#: Placeholder energies: the coverage needs only the Sun positions of the entries.
_NO_STATS = EdgeStats(0.0, 0.0, 0.0, 0.0, 0.0, 0, (960, 540), (999, 562))


def sun_bin(alt: float, az: float, params: ExtractorParams = PARAMS) -> tuple[int, int]:
    """The extractor's Sun bin (ClearDayReference._bin's rule)."""
    return (math.floor(alt / params.alt_bin_deg), math.floor((az % 360.0) / params.az_bin_deg))


def sun_of(cam: Camera, asset: ArchiveAsset) -> tuple[float, float]:
    """Geometric Sun altitude and azimuth at the asset's time (eval bins on geometric alt)."""
    pos = solar_position(cam.utc_of(asset), cam.lat, cam.lon)
    return pos.elevation_deg, pos.azimuth_deg % 360.0


def reference_of(cam: Camera, assets: Sequence[ArchiveAsset]) -> ClearDayReference:
    """A ClearDayReference whose entries are the planned frames (energies unused)."""
    entries = []
    for a in assets:
        alt, az = sun_of(cam, a)
        entries.append(
            ReferenceEntry(
                path=a.asset_id,
                sha256=a.asset_id,
                image_time_utc=cam.utc_of(a).isoformat(),
                sun_alt_deg=alt,
                sun_az_deg=az,
                stats=_NO_STATS,
            )
        )
    return ClearDayReference(Region.box(0, 0, 1, 1), PARAMS, tuple(entries))


def lookup_depths(
    reference: ClearDayReference, held: Sequence[tuple[float, float]]
) -> list[tuple[int, int, int]]:
    """Per held-out time: (frames in its exact bin, frames in the bin the extractor uses,
    bin steps). The extractor's lookup takes the exact bin when it holds any frame, else the
    nearest non-empty bin within ``max_bin_steps``; it never pools bins. No bin: (0, 0, -1).
    """
    exact = Counter(sun_bin(e.sun_alt_deg, e.sun_az_deg) for e in reference.entries)
    cache: dict[tuple[int, int], tuple[int, int, int]] = {}
    out = []
    for alt, az in held:
        key = sun_bin(alt, az)
        if key not in cache:
            found = reference.lookup(alt, az)
            if found is None:
                cache[key] = (exact.get(key, 0), 0, -1)
            else:
                assert found.query_bin == key
                cache[key] = (exact.get(key, 0), found.n, found.bin_steps)
        out.append(cache[key])
    return out


def held_out_times(cam: Camera) -> dict[str, list[tuple[float, float]]]:
    """Sun positions of held-out frame times, Jun-Aug 2026, from the ephemeris alone.

    ``hourly``: the frame nearest each local hour with NPS-style geometric Sun >= 2.75 deg
    (apparent >= 3 deg, PRD §13.4; §13.7 counts hourly predictions). ``15min``: every frame
    with apparent Sun >= 3 deg. No frame and no metadata of those dates is read.
    """
    hourly: list[tuple[float, float]] = []
    every: list[tuple[float, float]] = []
    for day in daterange(date(2026, 6, 1), date(2026, 8, 31)):
        syn = cam.synthetic_day(day)
        for a in syn:
            pos = solar_position(cam.utc_of(a), cam.lat, cam.lon)
            if pos.apparent_elevation_deg >= 3.0:
                every.append((pos.elevation_deg, pos.azimuth_deg % 360.0))
        hourly.extend(sun_of(cam, a) for a in pick_assets(syn, pick="hourly", min_sun_deg=2.75))
    return {"hourly": hourly, "15min": every}


def share(rows: Sequence[tuple[int, int, int]], test: Any) -> float:
    """Share of held-out times whose (exact, chosen, steps) row passes ``test``."""
    return round(sum(1 for r in rows if test(r)) / len(rows), 3)


def coverage(cams: dict[str, Camera], planned: list[Planned]) -> dict[str, Any]:
    """Sun-bin coverage of the held-out season by planned development frames (PRD §13.3).

    For each held-out time: the frames in its exact 5 x 20 deg bin, and the frames in the
    one bin the extractor's ``ClearDayReference.lookup`` would use (the exact bin if it holds
    any frame, else the nearest non-empty bin within one step). Development frames are the
    hourly Sun >= 2 deg picks of the planned days, counted as complete days, of any weather.
    """
    out = {}
    for vid in ORDER:
        cam = cams[vid]
        held = held_out_times(cam)
        res: dict[str, Any] = {}
        refs = {}
        for label, tranches in (
            ("T1a", {"T1a"}),
            ("T1a+T1b", {"T1a", "T1b"}),
            ("T1a+T1b+T2", {"T1a", "T1b", "T2"}),
        ):
            dev: list[ArchiveAsset] = []
            for p in planned:
                if p.vid == vid and p.tranche in tranches and p.opts is CORE:
                    dev.extend(
                        pick_assets(cam.synthetic_day(p.day), pick="hourly", min_sun_deg=2.0)
                    )
            refs[label] = (len(dev), reference_of(cam, dev))
        for sampling, times in held.items():
            block: dict[str, Any] = {
                "held_out_frame_times": len(times),
                "held_out_bins": len({sun_bin(a, z) for a, z in times}),
            }
            for label, (n_dev, ref) in refs.items():
                rows = lookup_depths(ref, times)
                block[label] = {
                    "dev_frames_if_complete": n_dev,
                    "exact_bin_ge": {
                        str(k): share(rows, lambda r, k=k: r[0] >= k) for k in (1, 3, 10, 30)
                    },
                    "extractor_lookup_bin_ge": {
                        str(k): share(rows, lambda r, k=k: r[1] >= k) for k in (1, 3, 10, 30)
                    },
                    "share_fallback": share(rows, lambda r: r[2] > 0),
                    "share_no_reference": share(rows, lambda r: r[2] < 0),
                    "median_exact_per_held_out_time": st.median(r[0] for r in rows),
                    "median_lookup_bin_per_held_out_time": st.median(r[1] for r in rows),
                }
            res[sampling] = block
        out[vid] = res
    return out


def frames_per_month(cams: dict[str, Camera]) -> dict[str, dict[str, int]]:
    """Hourly picks with Sun >= 2 deg on the 15th of each month (a complete day)."""
    out = {}
    for vid in ORDER:
        cam = cams[vid]
        out[vid] = {
            f"{m:02d}": len(
                pick_assets(cam.synthetic_day(date(2025, m, 15)), pick="hourly", min_sun_deg=2.0)
            )
            for m in range(1, 13)
        }
    return out


# --- other properties of the plan ---------------------------------------------------------


def hourly_days(planned: Sequence[Planned], vid: str, tranches: Iterable[str]) -> set[date]:
    """Days with a scored-data (CORE) entry at ``vid`` in ``tranches``."""
    wanted = set(tranches)
    return {p.day for p in planned if p.vid == vid and p.tranche in wanted and p.opts is CORE}


def structure(planned: Sequence[Planned]) -> dict[str, Any]:
    """Persistence-ready days, ISO-week parity per summer month, Point Reyes by year."""
    out: dict[str, Any] = {"previous_day_planned": {}, "t1a_parity_by_month": {}}
    for vid in ORDER:
        row = {}
        for label, tranches in (("T1a", ("T1a",)), ("through_T2", ("T1a", "T1b", "T2"))):
            days = hourly_days(planned, vid, tranches)
            row[label] = round(sum(1 for d in days if d - timedelta(days=1) in days) / len(days), 3)
        out["previous_day_planned"][vid] = row
    for month in (6, 7, 8):
        counts = Counter(
            "even" if p.day.isocalendar()[1] % 2 == 0 else "odd"
            for p in planned
            if p.tranche == "T1a" and p.day.month == month
        )
        out["t1a_parity_by_month"][f"{month:02d}"] = dict(counts)
    cells: dict[str, Any] = {}
    for vid in ORDER:
        days = hourly_days(planned, vid, ("T1a", "T1b", "T2"))
        cells[vid] = {f"{m:02d}": sum(1 for d in days if d.month == m) for m in (6, 7, 8)}
    out["jun_jul_aug_days_through_T2"] = cells
    pore = hourly_days(planned, PORE, ("T1a", "T1b", "T2"))
    out["point_reyes_jul_aug_days_by_year_through_T2"] = {
        f"{y}-{m:02d}": sum(1 for d in pore if d.year == y and d.month == m)
        for y in (2023, 2024, 2025)
        for m in (7, 8)
    }
    return out


def t0_brackets(planned: Sequence[Planned]) -> dict[str, Any]:
    """Gaps between consecutive T0 dates in each epoch bracket and view grid."""
    out: dict[str, Any] = {}
    for vid in ORDER:
        last32, first169, _ = EPOCH[vid]
        epoch = sorted(
            {last32, first169}
            | {p.day for p in planned if p.vid == vid and p.why.startswith("T0 epoch")}
        )
        gaps = [((b - a).days, a.isoformat(), b.isoformat()) for a, b in pairwise(epoch)]
        out[vid] = {
            "epoch_dates": len(epoch) - 2,
            "max_gap_days": max(g[0] for g in gaps),
            "gaps_over_14": [g for g in gaps if g[0] > T0_MAX_GAP_DAYS],
        }
    for vid, prefix in (
        ("big-bend.panther-junction", "T0 view: Big Bend"),
        ("grand-canyon.yavapai", "T0 view: Grand Canyon"),
    ):
        days = sorted(p.day for p in planned if p.vid == vid and p.why.startswith(prefix))
        gaps = [((b - a).days, a.isoformat(), b.isoformat()) for a, b in pairwise(days)]
        out[f"{vid} view grid"] = {
            "dates": len(days),
            "max_gap_days": max(g[0] for g in gaps),
            "widest": max(gaps),
        }
    return out


def hrrr_valid_hours(cams: dict[str, Camera], planned: Sequence[Planned]) -> dict[str, Any]:
    """UTC valid hours that scored development frames need an archived HRRR run for.

    For every planned (camera, day) with a CORE entry: the hourly frames with apparent Sun
    >= 3 deg (the scored daytime frames, PRD §13.4), each at its slot's whole UTC hour (a
    frame at :15 or :45 near sunrise or sunset stands for the hour pick_assets gave it; every
    zone here is a whole number of hours from UTC). One CONUS GRIB set per valid hour serves
    every camera, so the union over cameras is what the forecast package fetches; at a
    single lead (24 h) each valid hour is one forecast hour of one run.
    """
    result: dict[str, Any] = {}
    listing: dict[str, list[str]] = {}
    spans = (("T1a-T2", ("T1a", "T1b", "T2")), ("T1a-T1c", ("T1a", "T1b", "T2", "T1c")))
    for label, tranches in spans:
        hours: set[datetime] = set()
        per_date: dict[date, set[datetime]] = {}
        for p in planned:
            if p.tranche not in tranches or p.opts is not CORE:
                continue
            cam = cams[p.vid]
            for a in pick_assets(cam.synthetic_day(p.day), pick="hourly", min_sun_deg=2.0):
                utc = cam.utc_of(a)
                if solar_position(utc, cam.lat, cam.lon).apparent_elevation_deg < 3.0:
                    continue
                valid = utc.replace(minute=0, second=0, microsecond=0)
                if utc.minute > 30:  # pick_assets' rule: past the half hour is the next slot
                    valid += timedelta(hours=1)
                hours.add(valid)
                per_date.setdefault(p.day, set()).add(valid)
        sizes = [len(v) for v in per_date.values()]
        result[label] = {
            "distinct_local_dates": len(per_date),
            "valid_hours_per_date_mean": round(st.mean(sizes), 2),
            "valid_hours_per_date_min": min(sizes),
            "valid_hours_per_date_max": max(sizes),
            "distinct_utc_valid_hours": len(hours),
            "transfer_gb_at_24h_lead": round(len(hours) * HRRR_MB_PER_FORECAST_HOUR / 1000, 1),
        }
        if label == "T1a-T1c":
            listing = {
                d.isoformat(): sorted(h.strftime("%Y-%m-%dT%H:00Z") for h in per_date[d])
                for d in sorted(per_date)
            }
    return {"summary": result, "per_date": listing}


# --- the scratch queues and the CLI commands --------------------------------------------


def write_queue(path: Path, planned: list[Planned], cams: dict[str, Camera]) -> list[str]:
    """A scratch queue built with the real make_entry and ArchiveQueue (never the live one)."""
    resolved = path.resolve()
    assert not str(resolved).startswith(str(DATA.resolve())), f"refusing {resolved}"
    if path.exists():
        path.unlink()
    lock = path.with_name(path.name + ".lock")
    if lock.exists():
        lock.unlink()
    entries = [
        make_entry(
            cams[p.vid].vp,
            p.day,
            kinds=p.opts["kinds"],
            pick=p.opts["pick"],
            min_sun_deg=p.opts["min_sun_deg"],
        )
        for p in planned
    ]
    added, present = ArchiveQueue(path).add(entries)
    assert not present, present
    return added


def cli_commands(planned: Sequence[Planned]) -> list[tuple[str, list[str]]]:
    """Group consecutive entries of one viewpoint and one option set into one add command."""
    groups: list[tuple[str, dict[str, Any], list[date], str]] = []
    for p in planned:
        if groups and groups[-1][0] == p.vid and groups[-1][1] is p.opts and groups[-1][3] == p.why:
            groups[-1][2].append(p.day)
        else:
            groups.append((p.vid, p.opts, [p.day], p.why))
    out = []
    for vid, opts, days, why in groups:
        args = ["archive", "queue", "add", vid, *[d.isoformat() for d in days], "--hires"]
        args += ["--pick", opts["pick"]]
        if opts["min_sun_deg"] is not None:
            args += ["--min-sun-deg", f"{opts['min_sun_deg']:g}"]
        out.append((why, args))
    return out


#: Lines every generated script starts with after its own comment block.
SCRIPT_BODY = [
    "set -eu",
    "# Q must be set: Q=$HOME/lookout-data/state/archive/queue.json for the live queue.",
    ': "${Q:?set Q to the queue file, e.g. Q=$HOME/lookout-data/state/archive/queue.json}"',
    "# Run it from inside the repo, or set REPO to the repo's root.",
    "REPO=${REPO:-$(git rev-parse --show-toplevel)}",
    'cd "$REPO/workers/capture"',
    'run() { PYTHONPATH=src:../common/src uv run --no-sync lookout-capture "$@" --queue "$Q"; }',
]


def script(header: Sequence[str], items: Sequence[Planned], extra: Sequence[str] = ()) -> str:
    """A POSIX sh script of ``archive queue add`` commands (no request to nps.gov)."""
    lines = ["#!/bin/sh", *header, *SCRIPT_BODY, *extra]
    last_why = None
    for why, args in cli_commands(items):
        if why != last_why:
            lines.append(f"# {why}")
            last_why = why
        lines.append("run " + " ".join(shlex.quote(a) for a in args))
    lines.append("run archive queue list")
    return "\n".join(lines) + "\n"


def t3pr_script(items: Sequence[Planned]) -> str:
    """The T3pr draft: the night entries as planned, the clear days from ``$EVENINGS``."""
    nights = [p for p in items if p.opts is NIGHT]
    header = [
        "# DRAFT T3pr of the design-H archive backfill: Point Reyes plate inputs for M2",
        "# (PRD 8.5 steps 2 and 8). Queue it after T1b and before T2: the queue works its",
        "# entries in file order. EVENINGS names the clear days chosen from T1a/T1b frames.",
        "# docs/research/archive-backfill-plan.md. Edits the queue file only: no request to",
        "# nps.gov. Every date is before 2026-06-01; make_entry refuses any later one.",
    ]
    extra = [
        ': "${EVENINGS:?set EVENINGS to the clear Point Reyes days chosen from T1 frames}"',
    ]
    body = script(header, nights, extra).rstrip("\n").split("\n")
    tail = body.pop()  # "run archive queue list"
    body += [
        "# T3pr clear days: every 15-min frame with NPS Sun >= -2 deg (sunrise and sunset)",
        "# shellcheck disable=SC2086",
        "run archive queue add point-reyes.beach $EVENINGS --hires --pick all --min-sun-deg -2",
        tail,
    ]
    return "\n".join(body) + "\n"


def write_scripts(out: Path, planned: Sequence[Planned]) -> None:
    """first-tranche.sh and the drafts for T1b, T3pr, T2 and T1c."""
    first = [p for p in planned if p.tranche in ("T0", "T1a")]
    header = [
        "# First tranche (T0 map + T1a summer weeks) of the design-H archive backfill.",
        "# docs/research/archive-backfill-plan.md. Edits the queue file only: no request to",
        "# nps.gov. The running 'lookout-capture loop --archive-queue' works it in its idle",
        "# time (D-029). Every date is before 2026-06-01; make_entry refuses any later one.",
    ]
    (out / "first-tranche.sh").write_text(script(header, first))
    for tranche in ("T1b", "T2", "T1c"):
        draft = [
            f"# DRAFT {tranche} of the design-H archive backfill: assumes the safe spans of",
            "# 20 Nov 2026. Re-run plan.py after T0 reports (epoch dates, Big Bend and Grand",
            "# Canyon view checks) before queueing it. Queue T2 and T1c only after T3pr.",
        ]
        items = [p for p in planned if p.tranche == tranche]
        (out / f"draft-{tranche}.sh").write_text(script(draft, items))
    (out / "draft-T3pr.sh").write_text(t3pr_script([p for p in planned if p.tranche == "T3pr"]))


def main(out_dir: str) -> None:
    """Build the plan, write the analysis, the scratch queues and the scripts."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    assert not str(out.resolve()).startswith(str(QUARANTINE)), "never write to quarantine"
    assert not str(out.resolve()).startswith(str(DATA.resolve())), "never write under the data"
    cams = load_cameras()
    measured = picks_on_cached_days(cams)
    completeness = {vid: float(measured[vid]["completeness_hourly_sun2"] or 1.0) for vid in ORDER}
    planned = build(cams, completeness)

    # Every planned day is before the guard, and make_entry agrees (write_queue).
    assert all(p.day < HELD_OUT_FROM for p in planned)
    first = [p for p in planned if p.tranche in ("T0", "T1a")]

    hrrr = hrrr_valid_hours(cams, planned)
    analysis = {
        "picks_on_cached_days": measured,
        "hourly_sun2_frames_on_the_15th_complete_day": frames_per_month(cams),
        "loop_throughput_no_errors": loop_throughput(),
        "schedule": schedule(planned),
        "per_viewpoint": per_viewpoint(planned),
        "structure": structure(planned),
        "t0_brackets": t0_brackets(planned),
        "hrrr": hrrr["summary"],
        "coverage": coverage(cams, planned),
    }
    (out / "analysis.json").write_text(json.dumps(analysis, indent=2, default=str) + "\n")
    (out / "hrrr-valid-hours.json").write_text(json.dumps(hrrr["per_date"], indent=1) + "\n")
    (out / "plan.json").write_text(
        json.dumps(
            [
                {
                    "tranche": p.tranche,
                    "viewpoint_id": p.vid,
                    "date": p.day.isoformat(),
                    "kinds": list(p.opts["kinds"]),
                    "pick": p.opts["pick"],
                    "min_sun_deg": p.opts["min_sun_deg"],
                    "why": p.why,
                    "iso_week": p.day.isocalendar()[1],
                    "search_requests": p.search_requests,
                    "asset_requests_expected": round(p.asset_requests, 2),
                    "assets_if_complete": p.selected_if_complete,
                }
                for p in planned
            ],
            indent=1,
        )
        + "\n"
    )
    ids_first = write_queue(out.parent / "queue.json", first, cams)
    ids_all = write_queue(out.parent / "queue-plan-all.json", planned, cams)
    (out / "queue-first-ids.txt").write_text("\n".join(ids_first) + "\n")
    write_scripts(out, planned)

    print(json.dumps(analysis["loop_throughput_no_errors"], indent=1))
    for row in analysis["schedule"]["rows"]:
        print(row)
    print("entries first tranche:", len(ids_first), "all:", len(ids_all))
    print("pooled completeness:", measured["pooled_completeness_hourly_sun2"])


if __name__ == "__main__":
    main(sys.argv[1])
