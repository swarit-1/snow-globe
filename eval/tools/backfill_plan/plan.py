#!/usr/bin/env python3
"""Design-H archive backfill plan: tranches, costs and queue scripts (offline only).

Feeds docs/research/archive-backfill-plan.md (PRD §8.5, §13.3-13.9, §15.3, §16; D-019,
D-022, D-024, D-029, D-038, D-042, D-052). It never sends a request: every socket connect,
datagram send and name lookup is replaced below before anything else runs (NETWORK_FUNCTIONS),
and it never runs a script it writes.

It plans from the queue's current state. The queue (T0 and T1a on 20 Nov 2026) comes first,
as queued; the tranches still to queue follow: T1b, T3pr, T2, T1c, and TQ, the owner's
question (dates T0 opened before D-024's spans).

Reads, read-only and metadata only:
- the archive queue (``--queue``; pass a copy; default
  $SNOW_GLOBE_DATA_DIR/state/archive/queue.json);
- $SNOW_GLOBE_DATA_DIR/cache/nps-archive/days/<code>-20261119.json (GetAvailableDays masks);
- $SNOW_GLOBE_DATA_DIR/cache/nps-archive/search/<code>/<date>.json, dates before 2026-06-01;
- $SNOW_GLOBE_DATA_DIR/state/archive/<viewpoint_id>.json (what is stored, bytes so far) and the
  day's budget file, state/archive/budget-archive-queue-<UTC date>.json;
- the viewpoint manifests in the repo (found from snow_globe_common's location).

Writes only under OUT: plan.json, analysis.json, hrrr-valid-hours.json, queue-plan.json (a
scratch queue: the queue read, then every planned entry through T1c, built with the real
make_entry and ArchiveQueue), queue-plan-t3pr-first.json (the same with T3pr before T1b),
ids-T1b.txt, and draft-T1b.sh, draft-T3pr.sh, draft-T2.sh, draft-T1c.sh and draft-TQ.sh.
It refuses (SystemExit, never an assert) to write under $SNOW_GLOBE_DATA_DIR. analysis.json holds
each tranche's PLAN.md target and measured miss, T3pr's costs in both orders, and the
schedule with TQ's Point Reyes block ahead of T2.

Run it from eval/ (it needs capture's queue code and eval's extractor and solar code):
    PYTHONPATH=src:../workers/common/src:../workers/capture/src \
        uv run --no-sync python tools/backfill_plan/plan.py <OUT> --queue <queue copy>
"""

from __future__ import annotations

import argparse
import json
import math
import shlex
import socket
import statistics as st
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


def _no_network(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("the backfill planner never opens a network connection")


#: Everything that could reach the network from Python: connecting, sending a datagram
#: without connecting, and every name or address lookup (which would ask the resolver even
#: though the connect after it fails). Replaced before anything else is imported.
NETWORK_FUNCTIONS = (
    (socket.socket, "connect"),
    (socket.socket, "connect_ex"),
    (socket.socket, "sendto"),
    (socket.socket, "sendmsg"),
    (socket, "create_connection"),
    (socket, "getaddrinfo"),
    (socket, "gethostbyname"),
    (socket, "gethostbyname_ex"),
    (socket, "gethostbyaddr"),
    (socket, "getnameinfo"),
)
for _owner, _name in NETWORK_FUNCTIONS:
    setattr(_owner, _name, _no_network)

from snow_globe_capture.archive import ArchiveAsset, AvailableDays, pick_assets  # noqa: E402
from snow_globe_capture.archive_queue import (  # noqa: E402
    MAX_SWEEPS,
    ArchiveQueue,
    entry_id,
    make_entry,
)
from snow_globe_capture.heldout import HELD_OUT_FROM  # noqa: E402
from snow_globe_capture.sun import solar_elevation_deg  # noqa: E402
from snow_globe_common.env import env_get  # noqa: E402
from snow_globe_common.manifest import load_manifests  # noqa: E402
from snow_globe_common.paths import default_data_dir  # noqa: E402

from snow_globe_eval.extractor import (  # noqa: E402
    ClearDayReference,
    EdgeStats,
    ExtractorParams,
    ReferenceEntry,
    Region,
)
from snow_globe_eval.noaa_solar import solar_position  # noqa: E402

#: The data dir as the workers resolve it, minus the safety check: ``$SNOW_GLOBE_DATA_DIR``, else
#: ``$LOOKOUT_DATA_DIR``, else ``~/snow-globe-data`` or ``~/lookout-data`` (D-080).
DATA = Path(env_get("SNOW_GLOBE_DATA_DIR") or default_data_dir()).expanduser()
DAYS_DIR = DATA / "cache" / "nps-archive" / "days"
SEARCH_DIR = DATA / "cache" / "nps-archive" / "search"
STATE_DIR = DATA / "state" / "archive"
QUARANTINE = DATA / "quarantine"
LIVE_QUEUE = STATE_DIR / "queue.json"


def refuse_under_data(path: Path, what: str) -> None:
    """Raise SystemExit if ``path`` is the data dir or inside it (quarantine included).

    An explicit check, not an ``assert``: ``python -O`` strips asserts, and this guard must
    hold however the planner is run. ``DATA`` is read at call time, so a test can point it
    at a scratch folder.
    """
    data = Path(DATA).expanduser().resolve()
    resolved = Path(path).expanduser().resolve()
    if resolved == data or data in resolved.parents:
        raise SystemExit(f"refused: {what} {resolved} is under the data dir {data}")


PORE = "point-reyes.beach"
GRCA = "grand-canyon.yavapai"
YOSE = "yosemite.turtleback"
ACAD = "acadia.mcfarland"
GRSM = "smokies.look-rock"
BIBE = "big-bend.panther-junction"
ORDER = [PORE, GRCA, YOSE, ACAD, GRSM, BIBE]
#: Queue order. TQ is the owner's question: it is never queued without the owner's answer.
TRANCHES = ("T0", "T1a", "T1b", "T3pr", "T2", "T1c", "TQ")

# --- measured inputs (measure_inputs.py re-measures each one) ----------------------------
#: Bytes of the hires frames the queue's hourly entries stored on 20 Nov (their
#: sidecars' content_length; n = 689, all 999x562, mostly June-July 2025): (mean, smallest,
#: largest) per viewpoint. The ranges multiply by the smallest and largest frame.
HIRES_BYTES = {
    PORE: (31_316, 14_603, 65_978),  # n = 191
    GRCA: (54_350, 24_704, 81_892),  # n = 106
    YOSE: (79_973, 29_447, 117_584),  # n = 105
    ACAD: (51_985, 16_093, 83_813),  # n = 112
    GRSM: (44_880, 16_815, 67_935),  # n = 91
    BIBE: (66_649, 37_175, 85_734),  # n = 84
}
#: All 689 together, for a viewpoint without its own figure.
HIRES_BYTES_POOLED = (51_734, 14_603, 117_584)
#: Mean bytes of the 168 successful day searches of the queue's first day (20 Nov log).
SEARCH_BYTES_MEAN = 50_671
#: Mean file size of the 822 sidecars the queue wrote on 20 Nov (archive/; capture 0.2.0).
SIDECAR_BYTES_MEAN = 1_835
#: Mean size of the 244 cached day searches before 1 Jun 2026 (cache/nps-archive/search/).
SEARCH_CACHE_BYTES_MEAN = 25_186
#: Capture-log bytes per asset GET (an archive_request line, mean 304 B, and a stored line,
#: mean 361 B) and per search (an archive_request line), 20 Nov log.
LOG_BYTES_PER_GET = 304 + 361
LOG_BYTES_PER_SEARCH = 304
#: Failed requests per request: the queue's first day (20 Nov: 7 connect and 3 read
#: timeouts in 1,000) and the archive check (19 Nov: 8 of 215, the 2 paging probes left out).
ERROR_RATES = {"20 Nov (10/1000)": 10 / 1000, "19 Nov (8/215)": 8 / 215}
#: Requests the queue may send per UTC day (archive_queue.DEFAULT_DAILY_BUDGET).
DAILY_BUDGET = 1000
#: The size caps the queue enforces (archive_queue: max_gb per entry's viewpoint, total cap).
MAX_GB_PER_VIEWPOINT = 1.0
TOTAL_CAP_GB = 5.0
#: Measured transfer of one HRRR forecast hour, 12 fields (the backtest pair's evidence).
HRRR_MB_PER_FORECAST_HOUR = 16.5
#: PLAN.md's dates for the backfill (M1.5, "Design-H archive backfill"): T1a due Tue 24 Nov;
#: "T0-T1b and T3pr by Sun 29 Nov, T2 by Thu 10 Dec and T1c by Tue 15 Dec". TQ has none.
PLAN_TARGETS = {
    "T1a": date(2026, 11, 24),
    "T1b": date(2026, 11, 29),
    "T3pr": date(2026, 11, 29),
    "T2": date(2026, 12, 10),
    "T1c": date(2026, 12, 15),
}

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

# --- the held-out edge (D-019, D-022, D-038) ------------------------------------------------
#: The UTC reading of the edge (lead's decision of 20 Nov, until the owner confirms D-038's
#: local-day reading): no planned date may yield a frame whose UTC date is on or after
#: HELD_OUT_FROM. The local reading (HELD_OUT_FROM itself) applies as well.
UTC_GUARD = datetime.combine(HELD_OUT_FROM, time(0), tzinfo=UTC)

# --- spans (D-024, D-042, D-052) -------------------------------------------------------------


@dataclass(frozen=True)
class Span:
    """One camera's 16:9 dates for design-H development data (D-024, D-042, D-052).

    - ``last_32``, ``first_169``: T0's last 3:2 and first 16:9 midday frame (local dates,
      from the sidecars' frame heights; D-052). The 16:9 span starts at ``first_169``.
    - ``d024_start``: where D-024's usable span starts, "at the first 16:9 sample before the
      guard" (the archive check's sample; D-024's table writes the month).
    - ``design_start``: a later start D-024 sets for a design reason (Big Bend's view check),
      or None.

    The regular tranches (T1b, T2, T1c) start at the later of D-024's start and the design
    start. The dates T0 opened before D-024's start are the owner's question (tranche TQ),
    unless a design reason holds the whole span back.
    """

    last_32: date
    first_169: date
    d024_start: date
    design_start: date | None = None
    note: str = ""

    @property
    def regular_start(self) -> date:
        """The first date of the regular tranches."""
        return max(self.first_169, self.d024_start, self.design_start or self.d024_start)

    @property
    def owner_window(self) -> tuple[date, date] | None:
        """(first, last) date T0 opened before D-024's span, or None (nothing, or held back)."""
        if self.design_start is not None or self.first_169 >= self.d024_start:
            return None
        return self.first_169, self.d024_start - timedelta(days=1)


SPANS = {
    PORE: Span(date(2023, 3, 27), date(2023, 4, 10), date(2023, 7, 15)),
    GRCA: Span(
        date(2024, 4, 24),
        date(2024, 5, 20),
        date(2024, 7, 15),
        note="no archive frames 26 Apr - 19 May 2024; the 3:2 epoch is never planned (D-042)",
    ),
    YOSE: Span(date(2023, 8, 22), date(2023, 9, 5), date(2024, 1, 15)),
    ACAD: Span(date(2023, 8, 12), date(2023, 8, 26), date(2024, 1, 15)),
    GRSM: Span(date(2024, 3, 11), date(2024, 3, 25), date(2024, 7, 15)),
    BIBE: Span(
        date(2023, 12, 2),
        date(2023, 12, 16),
        date(2024, 1, 15),
        design_start=date(2025, 1, 13),
        note="D-024's view check of the 'framed lower' 2024-01-15 frame: the week of 2025-01-15, "
        "which matches 2026-01-15, until eval registers T0's 2024 frames",
    ),
}
#: Dates held out of every tranche for a design reason. D-024: check Grand Canyon's
#: 2025-01-15 on full frames before using winter 2024/25 (T0 fetched the weekly frames;
#: eval's registration has not reported).
HOLDS: dict[str, tuple[tuple[date, date], ...]] = {
    GRCA: ((date(2024, 12, 1), date(2025, 2, 28)),),
}
#: The archive check's last 3:2 and first 16:9 samples (docs/research/nps-archive-check.md),
#: which T0's epoch grid bracketed (tranche0 rebuilds T0 from them).
CHECK_SAMPLES = {
    PORE: (date(2023, 1, 16), date(2023, 7, 15)),
    GRCA: (date(2024, 1, 15), date(2024, 7, 15)),
    YOSE: (date(2023, 7, 15), date(2024, 1, 15)),
    ACAD: (date(2023, 7, 15), date(2024, 1, 15)),
    GRSM: (date(2024, 1, 15), date(2024, 7, 15)),
    BIBE: (date(2023, 7, 15), date(2024, 1, 15)),
}
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


def utc_of_stamp(stamp: str, tz: ZoneInfo) -> datetime:
    """UTC instant of an ImageCreateDateTime stamp read as local standard time."""
    wall = datetime.strptime(stamp, "%m/%d/%Y %I:%M:%S %p")
    return (wall - std_offset(tz)).replace(tzinfo=UTC)


def latest_stamp_utc(tz: ZoneInfo, day: date) -> datetime:
    """The latest instant an NPS stamp on local date ``day`` can name, in UTC.

    A day's Search returns the frames whose ImageCreateDateTime falls on that date (20,351 of
    20,351 cached assets before 1 Jun 2026), and NPS keeps ImageCreateDateTime on local
    standard time all year, to the second. So 23:59:59 standard time bounds every frame an
    entry for that date can yield, whatever its pick (a few real stamps are off the 15-min
    cadence, such as 23:56).
    """
    return (datetime.combine(day, time(23, 59, 59)) - std_offset(tz)).replace(tzinfo=UTC)


def asset_instants_utc(tz: ZoneInfo, asset: ArchiveAsset) -> list[datetime]:
    """Every UTC instant an archive asset can be read as (empty if none parses).

    Capture's reading (snow_globe_common's parse_nps_archive_time, from LocalTimeString) and
    ImageCreateDateTime read as local standard time.
    """
    out = []
    with suppress(ValueError, KeyError):
        out.append(asset.image_time_utc(str(tz.key)))
    with suppress(ValueError):
        out.append(utc_of_stamp(asset.image_create_datetime, tz))
    return out


def latest_frame_utc(tz: ZoneInfo, day: date, assets: Iterable[ArchiveAsset] = ()) -> datetime:
    """The latest UTC instant a frame of local date ``day`` can carry.

    The later of :func:`latest_stamp_utc` and every instant of ``assets`` (the date's cached
    search, if there is one), so a real asset that reads later than its date still counts.
    """
    latest = latest_stamp_utc(tz, day)
    for asset in assets:
        latest = max([latest, *asset_instants_utc(tz, asset)])
    return latest


def plannable_in(tz: ZoneInfo, day: date, assets: Iterable[ArchiveAsset] = ()) -> bool:
    """True if a queue entry for local date ``day`` can yield only frames before the edge.

    Both readings of D-019's "dated 1 Jun 2026 or later" (D-022, D-038; the lead's decision
    of 20 Nov until the owner confirms D-038), each applied to every frame the date can
    yield, judged by the latest of them (:func:`latest_frame_utc`):

    - local: the entry's date is before HELD_OUT_FROM, and so is the latest frame's civil
      (wall-clock) date in the camera's zone. In daylight time a standard-time stamp late on
      31 May is already 1 Jun on the wall clock (capture's four-date reading, D-047);
    - UTC: the latest frame's instant is before UTC_GUARD.

    The civil date and the instant both grow with the instant, so the latest frame decides.
    At the six US cameras the UTC bound comes first; the civil date binds east of Greenwich
    in daylight time (Paris).
    """
    latest = latest_frame_utc(tz, day, assets)
    return (
        day < HELD_OUT_FROM and latest < UTC_GUARD and latest.astimezone(tz).date() < HELD_OUT_FROM
    )


def last_plannable_in(
    tz: ZoneInfo, cached: Mapping[date, Sequence[ArchiveAsset]] | None = None
) -> date:
    """The last local date before the edge that passes :func:`plannable_in` in ``tz``."""
    cached = cached or {}
    day = HELD_OUT_FROM - timedelta(days=1)
    for _ in range(3):  # the UTC reading can take back at most a day or two
        if plannable_in(tz, day, cached.get(day, ())):
            return day
        day -= timedelta(days=1)
    raise RuntimeError(f"no plannable date found before {HELD_OUT_FROM} in {tz}")


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
    bytes_downloaded: int = 0

    def available(self, day: date) -> bool:
        """True if the 19 Nov mask lists frames on ``day``."""
        return self.avail.is_available(day)

    def plannable(self, day: date) -> bool:
        """True if an entry for ``day`` here yields frames only before the edge (both readings)."""
        return plannable_in(self.tz, day, self.cached.get(day, ()))

    def last_plannable(self) -> date:
        """The last date this camera may be planned on (30 May 2026 at all six cameras)."""
        return last_plannable_in(self.tz, self.cached)

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
        return utc_of_stamp(asset.image_create_datetime, self.tz)


def load_cameras() -> dict[str, Camera]:
    """Manifests, availability masks, pre-guard cached searches and what is stored."""
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
        cam.bytes_downloaded = int(state.get("bytes_downloaded") or 0)
        cams[vid] = cam
    return cams


def windows_of(cams: Mapping[str, Camera], which: str = "regular") -> dict[str, tuple[date, date]]:
    """(first, last) planning dates per camera: the regular tranches' or the owner's (TQ)."""
    out = {}
    for vid in ORDER:
        if vid not in cams:
            continue
        span = SPANS[vid]
        if which == "regular":
            out[vid] = (span.regular_start, cams[vid].last_plannable())
        elif span.owner_window is not None:
            out[vid] = span.owner_window
    return out


def held(vid: str, day: date) -> bool:
    """True if ``day`` lies in one of the camera's design holds (HOLDS)."""
    return any(a <= day <= b for a, b in HOLDS.get(vid, ()))


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
    """Frames each queue option selects on the cached (real) pre-guard day searches.

    ``completeness_hourly_sun2`` is the mean share of a complete day's hourly Sun >= 2 pick
    that the real days hold, over every cached day; ``..._16x9`` over the days on or after
    T0's first 16:9 date only (the epoch every later tranche plans in).
    """
    out: dict[str, Any] = {}
    ratios_all: list[float] = []
    ratios_169: list[float] = []
    for vid, cam in cams.items():
        rows = []
        ratios = []
        ratios_new = []
        for day, assets in sorted(cam.cached.items()):
            row: dict[str, Any] = {"date": day.isoformat(), "assets": len(assets)}
            for name, (pick, sun) in OPTIONS.items():
                row[name] = len(pick_assets(assets, pick=pick, min_sun_deg=sun))
            syn = len(pick_assets(cam.synthetic_day(day), pick="hourly", min_sun_deg=2.0))
            row["hourly/sun>=2 if complete"] = syn
            if syn:
                ratios.append(row["hourly/sun>=2"] / syn)
                if day >= SPANS[vid].first_169:
                    ratios_new.append(row["hourly/sun>=2"] / syn)
            rows.append(row)
        ratios_all.extend(ratios)
        ratios_169.extend(ratios_new)
        out[vid] = {
            "days": rows,
            "completeness_hourly_sun2": round(st.mean(ratios), 3) if ratios else None,
            "completeness_hourly_sun2_16x9": round(st.mean(ratios_new), 3) if ratios_new else None,
            "n_days": len(ratios),
            "n_days_16x9": len(ratios_new),
        }
    out["pooled_completeness_hourly_sun2"] = round(st.mean(ratios_all), 3)
    out["pooled_completeness_n_days"] = len(ratios_all)
    out["pooled_completeness_hourly_sun2_16x9"] = round(st.mean(ratios_169), 3)
    out["pooled_completeness_n_days_16x9"] = len(ratios_169)
    return out


#: A camera's own 16:9 share of a complete day is used once it rests on this many days.
MIN_COMPLETENESS_DAYS = 10


def completeness_of(measured: Mapping[str, Any]) -> dict[str, float]:
    """Each camera's share of a complete day: its 16:9 days, else all its cached days."""
    out = {}
    for vid in ORDER:
        row = measured[vid]
        if row["n_days_16x9"] >= MIN_COMPLETENESS_DAYS:
            out[vid] = float(row["completeness_hourly_sun2_16x9"])
        else:
            out[vid] = float(row["completeness_hourly_sun2"] or 1.0)
    return out


# --- cost of one entry ---------------------------------------------------------------------


@dataclass
class Planned:
    """One queue entry, queued or planned, and the work it still has.

    ``status`` is "planned" for an entry not queued yet, else its status in the queue read.
    ``search_requests`` and ``asset_requests`` are what it still needs (expected);
    ``spent_requests`` and ``spent_bytes`` what the queue has already sent and received.
    """

    tranche: str
    vid: str
    day: date
    opts: dict[str, Any]
    why: str
    search_requests: int = 0
    asset_requests: float = 0.0
    selected_if_complete: int = 0
    status: str = "planned"
    spent_requests: int = 0
    spent_bytes: int = 0

    @property
    def id(self) -> str:
        """The queue's entry id for this entry."""
        return entry_id(
            self.vid,
            self.day.isoformat(),
            list(self.opts["kinds"]),
            self.opts["pick"],
            self.opts["min_sun_deg"],
        )

    @property
    def open(self) -> bool:
        """True while it still has work: planned, or pending or active in the queue."""
        return self.status in ("planned", "pending", "active")


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


def options_of(entry: Mapping[str, Any]) -> dict[str, Any]:
    """The module's option set an entry uses (compared by value), or a new one."""
    kinds = tuple(entry.get("kinds") or ("hires",))
    key = (kinds, entry.get("pick"), entry.get("min_sun_deg"))
    for opts in (CORE, MAP, NIGHT, EVENING):
        if (opts["kinds"], opts["pick"], opts["min_sun_deg"]) == key:
            return opts
    return {"kinds": kinds, "pick": entry.get("pick"), "min_sun_deg": entry.get("min_sun_deg")}


def remaining_of_queued(
    cam: Camera,
    entry: Mapping[str, Any],
    opts: dict[str, Any],
    completeness: float,
    stored_by: Sequence[dict[str, Any]],
) -> tuple[int, float, int]:
    """(searches, expected GETs, frames selected) a queued entry still needs (queue file).

    Settled (done, refused, failed, stopped): nothing (a failed or stopped entry needs
    ``archive queue resume`` first). Searched (``selected`` in its progress): the selected
    frames from ``next`` on that are not stored yet, plus one retry for each failed one if
    another sweep is left. Not searched yet: the plan's own cost (:func:`cost`).
    """
    progress = entry.get("progress") or {}
    selected = progress.get("selected")
    if entry.get("status") not in ("pending", "active"):
        count = progress.get("selected_count")
        return 0, 0.0, int(count) if isinstance(count, int) else 0
    if isinstance(selected, list):
        kinds = list(entry.get("kinds") or ["hires"])
        pairs = [(str(a.get("AssetID")), k) for a in selected for k in kinds]
        nxt = int(progress.get("next", 0))
        left = [p for p in pairs[nxt:] if not (p[1] == "hires" and p[0] in cam.stored_hires)]
        outcomes = progress.get("outcomes") or {}
        retries = sum(1 for v in outcomes.values() if v == "failed")
        if int(progress.get("sweep", 1)) >= MAX_SWEEPS:
            retries = 0
        return 0, float(len(left) + retries), len(selected)
    return cost(cam, entry_day(entry), opts, completeness, stored_by)


def entry_day(entry: Mapping[str, Any]) -> date:
    """A queue entry's local date."""
    return date.fromisoformat(str(entry["date"]))


# --- the tranches --------------------------------------------------------------------------


def nearest_available(cam: Camera, target: date, reach: int = 3) -> date | None:
    """The available day nearest ``target`` within ``reach`` days (earlier first on ties)."""
    for step in range(reach + 1):
        for cand in (target - timedelta(days=step), target + timedelta(days=step)):
            if cam.plannable(cand) and cam.available(cand):
                return cand
    return None


def grid_days(cam: Camera, start: date, end: date, step: int) -> list[date]:
    """One available day near each grid point from ``start`` to ``end`` (PRD §13.9 epochs)."""
    out = []
    day = start
    while day <= end:
        got = nearest_available(cam, day)
        if got is not None and cam.plannable(got):
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
            candidates = [d for d in inner if cam.plannable(d) and cam.available(d)]
            if candidates:
                middle = a + timedelta(days=(b - a).days // 2)
                best = min(candidates, key=lambda d: (abs((d - middle).days), d))
                anchors = sorted({*anchors, best})
                added.append(best)
                break
        else:
            return sorted(added)


def tranche0(cams: dict[str, Camera]) -> list[tuple[str, date, dict[str, Any], str]]:
    """T0 as queued on 20 Nov: midday hires on a 14-day grid in each switch bracket (§13.9).

    Done since 20 Nov (D-052); the planner rebuilds it only to check that the queue's
    midday entries are exactly this rule's.
    """
    plan: list[tuple[str, date, dict[str, Any], str]] = []
    epoch_why = "T0 epoch: date the 3:2 -> 16:9 switch from the frame size in the sidecar"
    for vid in ORDER:
        cam = cams[vid]
        last32, first169 = CHECK_SAMPLES[vid]
        days = grid_days(cam, last32 + timedelta(days=14), first169 - timedelta(days=1), 14)
        days = sorted({*days, *fill_gaps(cam, days, last32, first169)})
        plan.extend((vid, d, MAP, epoch_why) for d in days)
        plan.append(
            (vid, first169, MAP, "T0 epoch: 16:9 hires reference for registration (search cached)")
        )
    # Big Bend (D-024): "check 2024-01 for a framing change": the year after the switch.
    bibe_why = "T0 view: Big Bend framing from 2024-01 until it matches 2025-01/2026-01 (D-024)"
    plan.extend(
        (BIBE, d, MAP, bibe_why)
        for d in grid_days(cams[BIBE], date(2024, 1, 29), date(2025, 1, 12), 14)
    )
    plan.append((BIBE, date(2025, 1, 15), MAP, "T0 view: 16:9 reference (cached)"))
    plan.append((BIBE, date(2026, 1, 15), MAP, "T0 view: 16:9 reference (cached)"))
    # Grand Canyon (D-024): 2025-01-15 lacks the bottom-left wedge; weekly Nov-Mar.
    plan.extend(
        (GRCA, d, MAP, "T0 view: Grand Canyon winter 2024/25 possible temporary re-aim (D-024)")
        for d in grid_days(cams[GRCA], date(2024, 11, 4), date(2025, 3, 31), 7)
    )
    plan.append((GRCA, date(2025, 1, 15), MAP, "T0 view: the odd 2025-01-15 frame (cached)"))
    plan.append((GRCA, date(2025, 7, 15), MAP, "T0 view: 16:9 reference (cached)"))
    plan.append((GRCA, date(2026, 1, 15), MAP, "T0 view: 16:9 reference (cached)"))
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


def core_weeks(
    cams: Mapping[str, Camera], windows: Mapping[str, tuple[date, date]] | None = None
) -> list[tuple[str, str, str, list[date], str]]:
    """One ISO week per month inside each camera's window (PRD §13.9 dev-fit/dev-check weeks).

    The week holding the 15th in odd years and the week after it in even years: the ISO
    week of the 15th has the same number every year for most months (June 24, August 33),
    so without the shift an ISO-parity split would put whole months on one side. In the
    window's first month a week that starts before the window gives way to the first full
    week on or after its start. May 2026 also gets the last week before the edge.
    ``windows`` maps a viewpoint to (first, last) date; the default is the regular windows.
    Days must be available and plannable (both readings of the edge), and outside the
    camera's design holds. Returns (level, slot, vid, days, why), in no particular order.
    """
    windows = windows_of(cams) if windows is None else windows
    last_local = HELD_OUT_FROM - timedelta(days=1)
    weeks = []
    for vid in ORDER:
        if vid not in windows:
            continue
        cam = cams[vid]
        start, end = windows[vid]
        year, month = start.year, start.month
        while date(year, month, 1) <= end:
            days = iso_week(date(year, month, 15))
            if year % 2 == 0:
                days = [d + timedelta(days=7) for d in days]
            if days[0] < start:  # the window's first month: the first full week on or after
                days = iso_week(start + timedelta(days=(7 - start.weekday()) % 7))
            slots = [days]
            if (year, month) == (last_local.year, last_local.month):
                slots.append(iso_week(last_local))  # also the last week before the edge
            for w in slots:
                kept = [
                    d
                    for d in w
                    if start <= d <= end
                    and cam.available(d)
                    and cam.plannable(d)
                    and not held(vid, d)
                ]
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


def weeks_in_order(
    weeks: Sequence[tuple[str, str, str, list[date], str]], level: str
) -> list[tuple[str, date, dict[str, Any], str]]:
    """The weeks of one level as (vid, day, CORE, why): by slot_rank, then camera order."""
    rows = [w for w in weeks if w[0] == level]
    out = []
    for slot in sorted({w[1] for w in rows}, key=lambda s: slot_rank(level, s)):
        for vid in ORDER:
            for _lvl, sl, v, days, why in rows:
                if sl == slot and v == vid:
                    out.extend((vid, d, CORE, why) for d in days)
    return out


def summer_completion(
    cams: Mapping[str, Camera],
    taken: set[tuple[str, date]],
    windows: Mapping[str, tuple[date, date]] | None = None,
) -> list[tuple[str, date, dict[str, Any], str]]:
    """T2: every remaining available June-August day inside each regular window.

    Point Reyes first (2025, 2024, 2023), then the other cameras, 2025 then 2024.
    """
    windows = windows_of(cams) if windows is None else windows
    out = []
    order = [(PORE, y) for y in (2025, 2024, 2023)]
    order += [(vid, y) for y in (2025, 2024, 2023) for vid in ORDER if vid != PORE]
    for vid, year in order:
        if vid not in windows:
            continue
        cam = cams[vid]
        start, end = windows[vid]
        for day in daterange(date(year, 6, 1), date(year, 8, 31)):
            if not start <= day <= end or (vid, day) in taken or held(vid, day):
                continue
            if not cam.available(day) or not cam.plannable(day):
                continue
            out.append((vid, day, CORE, f"T2 summer completion {year}"))
    return out


def owner_tranche(
    cams: Mapping[str, Camera], taken: set[tuple[str, date]]
) -> list[tuple[str, date, dict[str, Any], str]]:
    """TQ: the dates T0 opened before D-024's spans, sampled as the regular tranches are.

    For each camera with an owner window (:attr:`Span.owner_window`): one core week per month
    (:func:`core_weeks`), then every remaining available June-August day. Camera by camera, so
    the lead can queue any one camera's block once the owner has answered for it.
    """
    out = []
    windows = windows_of(cams, "owner")
    weeks = core_weeks(cams, windows)
    for vid in ORDER:
        if vid not in windows:
            continue
        cam = cams[vid]
        mine = sorted((w for w in weeks if w[2] == vid), key=lambda w: w[1])
        in_weeks: set[date] = set()
        for _lvl, slot, _v, days, _why in mine:
            why = f"TQ owner question: {vid} before its D-024 span, week of {slot}"
            for d in days:
                if (vid, d) not in taken:
                    out.append((vid, d, CORE, why))
                    in_weeks.add(d)
        start, end = windows[vid]
        for d in daterange(start, end):
            if d.month not in (6, 7, 8) or d in in_weeks or (vid, d) in taken:
                continue
            if cam.available(d) and cam.plannable(d) and not held(vid, d):
                out.append((vid, d, CORE, f"TQ owner question: {vid}, other summer days"))
    return out


def season_of(day: date) -> str:
    """The season (SEASONS) whose months hold ``day``."""
    return next(name for name, months in SEASONS.items() if day.month in months)


def t3_point_reyes(
    planned: Sequence[Planned],
    *,
    evenings: int = T3PR_EVENINGS,
    seasons_queued: Iterable[str] = (),
    evening_days_queued: Iterable[date] = (),
) -> list[tuple[str, date, dict[str, Any], str]]:
    """T3pr: Point Reyes plate inputs for M2 (PRD §8.5 steps 2 and 8; PLAN M2), on T1 days.

    - **Nights:** one day per season, ``--pick hourly`` with no Sun limit: the night and
      twilight hours of the newest Point Reyes T1 week of the season (its Wednesday, or the
      planned day nearest it). Its daylight hours are stored by T1 already. A season with no
      Point Reyes T1 week gets no night, and so does one in ``seasons_queued`` (a night the
      queue already holds).
    - **Clear days (allowance):** ``evenings`` days (:data:`T3PR_EVENINGS` until some are
      queued) of every 15-min frame with NPS Sun >= -2 deg, for the sunrise and sunset pose
      check. The real days are clear ones chosen from T1 frames after T1a lands; the cost is
      modelled here on the Wednesdays of the newest Point Reyes T1 weeks in June-October,
      skipping ``evening_days_queued`` (clear days the queue already holds).
    """
    t1 = sorted({p.day for p in planned if p.vid == PORE and p.tranche in ("T1a", "T1b")})
    mondays = sorted({d - timedelta(days=d.weekday()) for d in t1})
    skip_seasons = set(seasons_queued)
    skip_days = set(evening_days_queued)

    def midweek(monday: date) -> date:
        week = [d for d in t1 if monday <= d < monday + timedelta(days=7)]
        wednesday = monday + timedelta(days=2)
        return min(week, key=lambda d: (abs((d - wednesday).days), d))

    out = []
    for name, months in SEASONS.items():
        in_season = [m for m in mondays if (m + timedelta(days=2)).month in months]
        if in_season and name not in skip_seasons:
            out.append((PORE, midweek(max(in_season)), NIGHT, f"T3pr night frames, {name}"))
    warm = [m for m in mondays if (m + timedelta(days=2)).month in range(6, 11)]
    chosen: list[date] = []
    for monday in reversed(warm):  # the newest warm weeks first
        if len(chosen) >= evenings:
            break
        if midweek(monday) not in skip_days:
            chosen.append(monday)
    for monday in sorted(chosen):
        out.append((PORE, midweek(monday), EVENING, "T3pr clear day (cost model)"))
    return out


def queued_label(
    entry: Mapping[str, Any], opts: dict[str, Any], generated: Mapping[str, str]
) -> str:
    """The tranche a queued entry belongs to.

    The plan's own label when the plan makes the same entry; else T0 (midday), T1a (hourly,
    Sun >= 2), or T3pr for a Point Reyes night or clear-day entry (the clear days are chosen
    by hand, so their ids never match the plan's cost-model days).
    """
    if entry["id"] in generated:
        return generated[entry["id"]]
    if opts is MAP:
        return "T0"
    if opts is CORE:
        return "T1a"
    if str(entry.get("viewpoint_id")) == PORE and (opts is NIGHT or opts is EVENING):
        return "T3pr"
    return "other"


#: The order the tranches still to queue are appended in (plan section 5).
QUEUE_ORDER = ("T1b", "T3pr", "T2", "T1c", "TQ")


def build(
    cams: dict[str, Camera],
    completeness: dict[str, float],
    queue_entries: Sequence[Mapping[str, Any]] = (),
    order: Sequence[str] = QUEUE_ORDER,
) -> list[Planned]:
    """The queue read, in its order, then every tranche still to queue, in ``order``.

    The regular tranches (T1b, T3pr, T2, T1c) are planned inside each camera's regular window
    (:class:`Span`), TQ inside its owner window; ``order`` changes only the order they are
    appended (and so what each entry finds stored), never which dates are planned. An entry
    the queue already holds keeps its place and status. Every planned date passes both
    readings of the edge (:func:`plannable_in`); an open queued entry that does not raises
    ValueError, and so does a queued entry for a viewpoint the planner does not plan (its
    requests would be missing from the schedule).

    T3pr entries the queue holds count against T3pr's own: a season whose night is queued
    gets no other night, and each queued clear day (``EVENING`` at Point Reyes, chosen by
    hand) takes the place of one cost-model clear day, so a re-plan after T3pr is queued
    plans no T3pr twice.
    """
    unknown = sorted({str(e["viewpoint_id"]) for e in queue_entries} - set(cams))
    if unknown:
        raise ValueError(
            f"the queue holds entries for viewpoints this plan does not plan: {unknown}"
        )
    weeks = core_weeks(cams)
    queued_core = {
        (str(e["viewpoint_id"]), entry_day(e)) for e in queue_entries if options_of(e) is CORE
    }
    t1b = weeks_in_order(weeks, "B")
    t1c = weeks_in_order(weeks, "C")
    # T3pr needs the T1 days: the queue's T1a (its CORE entries) and T1b. Only days that pass
    # both readings of the edge: a queued entry settled before the UTC reading (say, a done
    # 31 May) must not lead a T3pr night onto its date.
    t1_days = [
        Planned("T1a", str(e["viewpoint_id"]), entry_day(e), CORE, "queued")
        for e in queue_entries
        if options_of(e) is CORE and cams[str(e["viewpoint_id"])].plannable(entry_day(e))
    ] + [Planned("T1b", v, d, o, w) for v, d, o, w in t1b]
    # What of T3pr the queue already holds (any status: a done one needs no second).
    pore_queued = [e for e in queue_entries if str(e["viewpoint_id"]) == PORE]
    nights_queued = [entry_day(e) for e in pore_queued if options_of(e) is NIGHT]
    evenings_queued = [entry_day(e) for e in pore_queued if options_of(e) is EVENING]
    t3pr = t3_point_reyes(
        t1_days,
        evenings=max(0, T3PR_EVENINGS - len(evenings_queued)),
        seasons_queued={season_of(d) for d in nights_queued},
        evening_days_queued=evenings_queued,
    )
    taken = queued_core | {(v, d) for v, d, _, _ in t1b}
    t2 = summer_completion(cams, taken)
    taken |= {(v, d) for v, d, _, _ in t2} | {(v, d) for v, d, _, _ in t1c}
    tq = owner_tranche(cams, taken)

    generated: dict[str, str] = {}
    to_queue: list[tuple[str, str, date, dict[str, Any], str]] = []
    tranches = {"T1b": t1b, "T3pr": t3pr, "T2": t2, "T1c": t1c, "TQ": tq}
    assert sorted(order) == sorted(tranches), order
    for tranche in order:
        for vid, day, opts, why in tranches[tranche]:
            item = Planned(tranche, vid, day, opts, why)
            if item.id in generated:
                continue  # one entry per id, like the queue
            generated[item.id] = tranche
            to_queue.append((tranche, vid, day, opts, why))

    planned: list[Planned] = []
    bad = []
    for entry in queue_entries:
        vid, day = str(entry["viewpoint_id"]), entry_day(entry)
        opts = options_of(entry)
        earlier = [p.opts for p in planned if p.vid == vid and p.day == day]
        s, g, n = remaining_of_queued(cams[vid], entry, opts, completeness[vid], earlier)
        progress = entry.get("progress") or {}
        item = Planned(
            queued_label(entry, opts, generated),
            vid,
            day,
            opts,
            f"queued {entry.get('added_at', '')}".strip(),
            s,
            g,
            n,
            status=str(entry.get("status")),
            spent_requests=int(progress.get("requests") or 0),
            spent_bytes=int(progress.get("bytes") or 0),
        )
        if item.open and not cams[vid].plannable(day):
            bad.append(item.id)
        planned.append(item)
    if bad:
        raise ValueError(
            "open queue entries fail the held-out edge (local date or UTC instant); "
            f"remove them first: {bad}"
        )
    queued_ids = {str(e["id"]) for e in queue_entries}
    for tranche, vid, day, opts, why in to_queue:
        item = Planned(tranche, vid, day, opts, why)
        if item.id in queued_ids:
            continue
        if not cams[vid].plannable(day):  # never happens: every builder filters on it
            raise AssertionError(f"planned {item.id} fails the held-out edge")
        earlier = [p.opts for p in planned if p.vid == vid and p.day == day]
        s, g, n = cost(cams[vid], day, opts, completeness[vid], earlier)
        item.search_requests, item.asset_requests, item.selected_if_complete = s, g, n
        planned.append(item)
    return planned


# --- throughput and schedule ----------------------------------------------------------------


def loop_throughput(
    pass_s: float = 9.3,
    request_s: float = 0.62,
    interval: float = 300.0,
    jitter: float = 30.0,
    spacing_s: float = 5.0,
) -> dict[str, float]:
    """Archive requests per hour of loop uptime with no errors, analytically (PRD §15.3).

    The loop's next pass is due ``interval`` + U(0, ``jitter``) s after the last one started
    (live.py), so the idle window is that minus the pass. A request starts only while the
    limiter's wait + 30 s + 5 s fits (STEP_MAX_S + STEP_MARGIN_S). The first one needs 35 s,
    every later one waits ``spacing_s`` (the limiter's 5 s) after the previous one ended, so
    request k starts if (k - 1) * request_s + (k - 2) * spacing_s <= window - 35 - spacing_s
    (window - 40 at 5 s). Errors are left out here: each costs its own timeout plus a
    limiter deferral of 30 s that doubles on consecutive failures (archive.py ``_send``);
    loop_sim.py measures that on the real runner.
    """
    window = interval + jitter / 2 - pass_s
    per_window = math.floor((window - 35.0 + spacing_s + request_s) / (spacing_s + request_s))
    windows_per_hour = 3600.0 / (interval + jitter / 2)
    per_hour = per_window * windows_per_hour
    return {
        "idle_window_s": round(window, 1),
        "requests_per_window": per_window,
        "windows_per_hour": round(windows_per_hour, 2),
        "requests_per_hour": round(per_hour),
        "hours_for_daily_budget": round(DAILY_BUDGET / per_hour, 2),
    }


def bytes_of(vid: str) -> tuple[int, int, int]:
    """(mean, smallest, largest) bytes of one hires frame at ``vid`` (HIRES_BYTES)."""
    return HIRES_BYTES.get(vid, HIRES_BYTES_POOLED)


def finish_day(cum: float, first_day: date, first_capacity: int, useful_share: float) -> date:
    """The UTC day the queue's cumulative requests reach ``cum``.

    The first day has ``first_capacity`` requests left in its budget, each later day
    DAILY_BUDGET; a share ``useful_share`` of each day's requests does work (the rest fail
    and are retried).
    """
    first = first_capacity * useful_share
    if cum <= first:
        return first_day
    return first_day + timedelta(days=math.ceil((cum - first) / (DAILY_BUDGET * useful_share)))


def capacity_through(day: date, first_day: date, first_capacity: int, useful_share: float) -> float:
    """Useful requests the queue can send from ``first_day`` through ``day`` (UTC days)."""
    if day < first_day:
        return 0.0
    later_days = (day - first_day).days
    return (first_capacity + later_days * DAILY_BUDGET) * useful_share


def target_fields(
    tranche: str, items: Sequence[Planned], cum: float, first_day: date, first_capacity: int
) -> dict[str, Any]:
    """PLAN.md's date for ``tranche`` and the measured miss (PLAN_TARGETS; none for TQ).

    ``days_after_plan_target`` is the finish day minus the target, per error rate (0 or less
    is on time); ``requests_short_by_target`` is how many more useful requests the days
    through the target would need (0 when they suffice).
    """
    target = PLAN_TARGETS.get(tranche)
    if target is None or not any(p.open for p in items):
        return {"plan_target": target.isoformat() if target else None}
    late = {}
    short = {}
    for name, rate in ERROR_RATES.items():
        late[name] = (finish_day(cum, first_day, first_capacity, 1 - rate) - target).days
        room = capacity_through(target, first_day, first_capacity, 1 - rate)
        short[name] = max(0, round(cum - room))
    return {
        "plan_target": target.isoformat(),
        "days_after_plan_target": late,
        "requests_short_by_target": short,
    }


def schedule(planned: Sequence[Planned], first_day: date, first_capacity: int) -> dict[str, Any]:
    """Remaining requests per tranche, in queue order, and the UTC day each finishes.

    The queue is worked in file order: ``planned`` is in that order (the queue read, then the
    tranches as the drafts would append them; TQ last, as if queued after T1c). A tranche
    finishes when the requests up to its last open entry are spent. Finish days at each
    measured error rate (ERROR_RATES), at the full budget every UTC day (which needs at least
    2.2 h of loop time a day; loop_sim.py).
    """
    rows = []
    cum_at_end: dict[str, float] = {}
    running = 0.0
    for p in planned:
        running += p.search_requests + p.asset_requests
        if p.open:
            cum_at_end[p.tranche] = running
    for tranche in dict.fromkeys(p.tranche for p in planned):
        items = [p for p in planned if p.tranche == tranche]
        searches = sum(p.search_requests for p in items)
        gets = sum(p.asset_requests for p in items)
        reqs = searches + gets
        cum = cum_at_end.get(tranche, 0.0)
        mean = sum(p.asset_requests * bytes_of(p.vid)[0] for p in items)
        low = sum(p.asset_requests * bytes_of(p.vid)[1] for p in items)
        high = sum(p.asset_requests * bytes_of(p.vid)[2] for p in items)
        rows.append(
            {
                "tranche": tranche,
                "entries": len(items),
                "open_entries": sum(1 for p in items if p.open),
                "by_status": dict(Counter(p.status for p in items)),
                "viewpoint_days": len({(p.vid, p.day) for p in items}),
                "search_requests": searches,
                "asset_requests": round(gets),
                "requests": round(reqs),
                "frames_expected": round(gets),
                "cumulative_requests": round(cum),
                "spent_requests": sum(p.spent_requests for p in items),
                "spent_bytes": sum(p.spent_bytes for p in items),
                "asset_bytes_mean": round(mean),
                "asset_bytes_low": round(low),
                "asset_bytes_high": round(high),
                "search_bytes": round(searches * SEARCH_BYTES_MEAN),
                "disk_bytes_mean": round(
                    mean
                    + gets * (SIDECAR_BYTES_MEAN + LOG_BYTES_PER_GET)
                    + searches * (SEARCH_CACHE_BYTES_MEAN + LOG_BYTES_PER_SEARCH)
                ),
                "finishes_utc_day": {
                    name: "done"
                    if not any(p.open for p in items)
                    else finish_day(cum, first_day, first_capacity, 1 - rate).isoformat()
                    for name, rate in ERROR_RATES.items()
                },
                "budget_days_cumulative": {
                    name: round(cum / (DAILY_BUDGET * (1 - rate)), 2)
                    for name, rate in ERROR_RATES.items()
                },
                **target_fields(tranche, items, cum, first_day, first_capacity),
            }
        )
    open_items = [p for p in planned if p.open and p.tranche != "TQ"]
    gets = sum(p.asset_requests for p in open_items)
    searches = sum(p.search_requests for p in open_items)
    mean = sum(p.asset_requests * bytes_of(p.vid)[0] for p in open_items)
    totals = {
        "scope": "open work through T1c (TQ left out)",
        "entries": len(open_items),
        "requests": round(gets + searches),
        "asset_requests": round(gets),
        "search_requests": searches,
        "asset_gb_mean": round(mean / 1e9, 3),
        "asset_gb_low": round(
            sum(p.asset_requests * bytes_of(p.vid)[1] for p in open_items) / 1e9, 3
        ),
        "asset_gb_high": round(
            sum(p.asset_requests * bytes_of(p.vid)[2] for p in open_items) / 1e9, 3
        ),
        "search_gb": round(searches * SEARCH_BYTES_MEAN / 1e9, 3),
        "sidecar_mb": round(gets * SIDECAR_BYTES_MEAN / 1e6, 1),
        "search_cache_mb": round(searches * SEARCH_CACHE_BYTES_MEAN / 1e6, 1),
        "log_mb": round((gets * LOG_BYTES_PER_GET + searches * LOG_BYTES_PER_SEARCH) / 1e6, 1),
    }
    return {
        "first_day": first_day.isoformat(),
        "first_day_capacity": first_capacity,
        "useful_requests_per_day": {
            name: round(DAILY_BUDGET * (1 - rate)) for name, rate in ERROR_RATES.items()
        },
        "rows": rows,
        "totals": totals,
    }


def with_block_before(
    planned: Sequence[Planned], vid: str, tranche: str, before: str, label: str
) -> list[Planned]:
    """``planned`` with ``tranche``'s open entries at ``vid`` moved ahead of ``before``.

    The moved entries, relabelled ``label``, go just ahead of the first entry of ``before``,
    so :func:`schedule` gives their own finish day. Their costs stay exact only if no entry
    for the same viewpoint and day lies between their old and new places (that entry would
    find other frames stored); ValueError otherwise.
    """
    moving = [p.vid == vid and p.tranche == tranche and p.open for p in planned]
    moved = [p for p, m in zip(planned, moving, strict=True) if m]
    rest = [p for p, m in zip(planned, moving, strict=True) if not m]
    at = next((i for i, p in enumerate(rest) if p.tranche == before), len(rest))
    start = next((i for i, p in enumerate(planned) if p.tranche == before), len(planned))
    last_old = max((i for i, m in enumerate(moving) if m), default=-1)
    days = {(p.vid, p.day) for p in moved}
    crossed = [
        p.id
        for p, m in zip(planned[start:last_old], moving[start:last_old], strict=True)
        if not m and (p.vid, p.day) in days
    ]
    if crossed:
        raise ValueError(f"moving the block would change the cost of {crossed[:3]}")
    relabelled = [replace(p, tranche=label) for p in moved]
    return [*rest[:at], *relabelled, *rest[at:]]


def t3pr_costs(planned: Sequence[Planned]) -> dict[str, Any]:
    """T3pr's entries and what each still costs (searches, expected GETs), and the ranges."""
    rows = [
        {
            "id": p.id,
            "pick": p.opts["pick"],
            "search_requests": p.search_requests,
            "asset_requests": round(p.asset_requests, 2),
        }
        for p in planned
        if p.tranche == "T3pr"
    ]
    out: dict[str, Any] = {"entries": rows}
    for pick, name in (("hourly", "night"), ("all", "clear_day")):
        gets = [r["asset_requests"] for r in rows if r["pick"] == pick]
        if gets:
            out[f"{name}_gets_range"] = [min(gets), max(gets)]
        out[f"{name}_entries_paying_a_search"] = [
            r["id"] for r in rows if r["pick"] == pick and r["search_requests"]
        ]
    return out


def removed_31_may_cost(cams: Mapping[str, Camera], completeness: Mapping[str, float]) -> float:
    """Expected requests of the six removed 31 May 2026 hourly entries at today's shares.

    Each is one search plus its complete day's hourly Sun >= 2 frames times the camera's
    share of a complete day (31 May was never searched). For D-038: what confirming the
    local-day reading would add back. Planning only; 31 May itself is never planned.
    """
    day = HELD_OUT_FROM - timedelta(days=1)
    total = 0.0
    for vid in ORDER:
        searches, gets, _ = cost(cams[vid], day, CORE, completeness[vid])
        total += searches + gets
    return round(total, 1)


def caps(planned: Sequence[Planned], cams: Mapping[str, Camera]) -> dict[str, Any]:
    """Bytes per viewpoint and in all against the queue's caps, after each tranche (§15.3).

    Starts from each viewpoint's lifetime ``bytes_downloaded`` (state/archive/<id>.json),
    which is what the caps compare, and adds the open work's expected frame bytes (mean and
    largest frame), tranche by tranche in queue order (TQ last).
    """
    out: dict[str, Any] = {}
    running_mean = {vid: float(cams[vid].bytes_downloaded) for vid in ORDER}
    running_high = dict(running_mean)
    for tranche in dict.fromkeys(p.tranche for p in planned):
        for p in planned:
            if p.tranche == tranche and p.open:
                running_mean[p.vid] += p.asset_requests * bytes_of(p.vid)[0]
                running_high[p.vid] += p.asset_requests * bytes_of(p.vid)[2]
        out[f"after {tranche}"] = {
            "per_viewpoint_gb_mean": {v: round(b / 1e9, 3) for v, b in running_mean.items()},
            "per_viewpoint_gb_high": {v: round(b / 1e9, 3) for v, b in running_high.items()},
            "total_gb_mean": round(sum(running_mean.values()) / 1e9, 3),
            "total_gb_high": round(sum(running_high.values()) / 1e9, 3),
            "max_gb_per_viewpoint": MAX_GB_PER_VIEWPOINT,
            "total_cap_gb": TOTAL_CAP_GB,
        }
    return out


def per_viewpoint(planned: list[Planned]) -> dict[str, Any]:
    """Days and requests per viewpoint and tranche, and each viewpoint's open frame bytes."""
    out: dict[str, Any] = {}
    for p in planned:
        row = out.setdefault(p.vid, Counter())
        if p.opts is CORE:
            row[f"{p.tranche}:days"] += 1
        row[f"{p.tranche}:entries"] += 1
        row[f"{p.tranche}:requests"] += p.search_requests + p.asset_requests
        if p.tranche != "TQ":
            row["asset_requests_open_through_T1c"] += p.asset_requests
    for vid, row in out.items():
        mean, _low, high = bytes_of(vid)
        row["asset_gb_mean_open_through_T1c"] = row["asset_requests_open_through_T1c"] * mean / 1e9
        row["asset_gb_high_open_through_T1c"] = row["asset_requests_open_through_T1c"] * high / 1e9
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
    reference: ClearDayReference, held_out: Sequence[tuple[float, float]]
) -> list[tuple[int, int, int]]:
    """Per held-out time: (frames in its exact bin, frames in the bin the extractor uses,
    bin steps). The extractor's lookup takes the exact bin when it holds any frame, else the
    nearest non-empty bin within ``max_bin_steps``; it never pools bins. No bin: (0, 0, -1).
    """
    exact = Counter(sun_bin(e.sun_alt_deg, e.sun_az_deg) for e in reference.entries)
    cache: dict[tuple[int, int], tuple[int, int, int]] = {}
    out = []
    for alt, az in held_out:
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
        held_out = held_out_times(cam)
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
        for sampling, times in held_out.items():
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
    """Persistence-ready days, ISO-week parity per summer month, days per camera and month."""
    out: dict[str, Any] = {"previous_day_planned": {}, "t1a_parity_by_month": {}}
    for vid in ORDER:
        row = {}
        for label, tranches in (("T1a", ("T1a",)), ("through_T2", ("T1a", "T1b", "T2"))):
            days = hourly_days(planned, vid, tranches)
            after = sum(1 for d in days if d - timedelta(days=1) in days)
            row[label] = round(after / len(days), 3) if days else None
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
    cumulative = {}
    for vid in ORDER:
        row = {}
        upto: list[str] = []
        for tranche in ("T1a", "T1b", "T2", "T1c", "TQ"):
            upto.append(tranche)
            row[f"after {tranche}"] = len(hourly_days(planned, vid, upto))
        cumulative[vid] = row
    out["development_days_cumulative"] = cumulative
    tq: dict[str, Any] = {}
    for vid in ORDER:
        days = sorted(hourly_days(planned, vid, ("TQ",)))
        if days:
            tq[vid] = {
                "days": len(days),
                "first": days[0].isoformat(),
                "last": days[-1].isoformat(),
                "jun_jul_aug_days": sum(1 for d in days if d.month in (6, 7, 8)),
                "by_month": dict(Counter(f"{d:%Y-%m}" for d in days)),
            }
    out["tq_days"] = tq
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
    spans = (
        ("T1a-T2", ("T1a", "T1b", "T2")),
        ("T1a-T1c", ("T1a", "T1b", "T2", "T1c")),
        ("T1a-T1c+TQ", ("T1a", "T1b", "T2", "T1c", "TQ")),
    )
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
        if not sizes:
            result[label] = {"distinct_local_dates": 0}
            continue
        result[label] = {
            "distinct_local_dates": len(per_date),
            "valid_hours_per_date_mean": round(st.mean(sizes), 2),
            "valid_hours_per_date_min": min(sizes),
            "valid_hours_per_date_max": max(sizes),
            "distinct_utc_valid_hours": len(hours),
            "transfer_gb_at_24h_lead": round(len(hours) * HRRR_MB_PER_FORECAST_HOUR / 1000, 1),
            "latest_valid_hour": max(hours).strftime("%Y-%m-%dT%H:00Z"),
        }
        if label == "T1a-T1c":
            listing = {
                d.isoformat(): sorted(h.strftime("%Y-%m-%dT%H:00Z") for h in per_date[d])
                for d in sorted(per_date)
            }
    return {"summary": result, "per_date": listing}


def utc_edge(cams: Mapping[str, Camera]) -> dict[str, Any]:
    """The 31 May edge per camera: the last plannable date and what 31 May would have yielded.

    For 31 May 2026 at each camera: the UTC instant that bounds the date's frames, and the
    UTC times of the frames the hourly Sun >= 2 pick selects on a complete day (from the
    ephemeris; no metadata of 31 May exists on disk, since its entries were never worked).
    """
    out = {}
    last_local = HELD_OUT_FROM - timedelta(days=1)
    for vid in ORDER:
        cam = cams[vid]
        picks = pick_assets(cam.synthetic_day(last_local), pick="hourly", min_sun_deg=2.0)
        times = sorted(cam.utc_of(a) for a in picks)
        late = [t for t in times if t >= UTC_GUARD]
        out[vid] = {
            "zone": str(cam.tz.key),
            "last_plannable": cam.last_plannable().isoformat(),
            "last_local_bound_utc": latest_stamp_utc(cam.tz, last_local).isoformat(),
            "hourly_sun2_frames_31_may": len(times),
            "of_them_on_1_jun_utc": len(late),
            "latest_31_may_frame_utc": times[-1].isoformat() if times else None,
        }
    return out


def queue_checks(
    planned: Sequence[Planned], queue_doc: Mapping[str, Any], cams: dict[str, Camera]
) -> dict[str, Any]:
    """What the queue read holds, and the checks against it (no entry planned twice)."""
    entries = queue_doc.get("entries") or []
    ids = [str(e["id"]) for e in entries]
    queued = set(ids)
    rule_t0 = [Planned("T0", v, d, o, w).id for v, d, o, w in tranche0(cams)]
    queue_t0 = [p.id for p in planned if p.tranche == "T0"]
    by_tranche = {}
    for tranche in TRANCHES:
        items = [p for p in planned if p.tranche == tranche]
        by_tranche[tranche] = {
            "planned_not_queued": sum(1 for p in items if p.status == "planned"),
            "queued": dict(Counter(p.status for p in items if p.status != "planned")),
        }
    t1b_ids = {p.id for p in planned if p.tranche == "T1b" and p.status == "planned"}
    return {
        "entries": len(entries),
        "by_status": dict(Counter(str(e.get("status")) for e in entries)),
        "dates": [min(str(e["date"]) for e in entries), max(str(e["date"]) for e in entries)]
        if entries
        else None,
        "updated_at": queue_doc.get("updated_at"),
        "runner": queue_doc.get("runner"),
        "duplicate_ids_in_queue": len(ids) - len(queued),
        "by_tranche": by_tranche,
        "t1b_entries_already_in_queue": len(t1b_ids & queued),
        "planned_entries_already_in_queue": sum(
            1 for p in planned if p.status == "planned" and p.id in queued
        ),
        "queued_t0_equals_the_t0_rule": queue_t0 == rule_t0,
        "open_queued_entries_failing_the_edge": [
            p.id
            for p in planned
            if p.status != "planned" and p.open and not cams[p.vid].plannable(p.day)
        ],
        "latest_open_date": max((p.day for p in planned if p.open), default=None),
    }


# --- the scratch queue and the CLI commands --------------------------------------------


def make_entries(planned: Sequence[Planned], cams: Mapping[str, Camera]) -> list[dict[str, Any]]:
    """Capture's own queue entries for ``planned`` (make_entry raises on a held-out day)."""
    return [
        make_entry(
            cams[p.vid].vp,
            p.day,
            kinds=p.opts["kinds"],
            pick=p.opts["pick"],
            min_sun_deg=p.opts["min_sun_deg"],
        )
        for p in planned
    ]


def write_queue(
    path: Path,
    planned: list[Planned],
    cams: dict[str, Camera],
    seed_entries: Sequence[Mapping[str, Any]] = (),
) -> list[str]:
    """A scratch queue (never the live one): ``seed_entries`` as they are, then ``planned``.

    Built with the real make_entry and ArchiveQueue, so make_entry's own guard applies.
    Refuses (SystemExit) a path under the data dir, so it can never touch the live queue.
    """
    refuse_under_data(path, "scratch queue")
    if path.exists():
        path.unlink()
    lock = path.with_name(path.name + ".lock")
    if lock.exists():
        lock.unlink()
    queue = ArchiveQueue(path)
    if seed_entries:
        with queue.edit() as doc:
            doc["entries"] = [dict(e) for e in seed_entries]
    added, present = queue.add(make_entries(planned, cams))
    if present:
        raise ValueError(f"planned entries already in the seed queue: {present[:5]}")
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
    "# Q must be set: Q=$HOME/lookout-data/state/archive/queue.json for the live queue (D-080).",
    ': "${Q:?set Q to the queue file, e.g. Q=$HOME/lookout-data/state/archive/queue.json}"',
    "# Q must name an existing queue file by an absolute path: 'archive queue add' would start",
    "# a new queue at a mistyped path, which the loop never works. Start a new queue with",
    "# 'snow-globe-capture archive queue add' itself, never with a draft.",
    'case "$Q" in /*) ;; *) echo "refused: Q=$Q is not an absolute path" >&2; exit 1 ;; esac',
    'if [ ! -f "$Q" ] || [ ! -s "$Q" ]; then',
    '  echo "refused: Q=$Q is not an existing queue file" >&2',
    "  exit 1",
    "fi",
    "# Run it from inside the repo, or set REPO to the repo's root.",
    "REPO=${REPO:-$(git rev-parse --show-toplevel)}",
    'cd "$REPO/workers/capture"',
    'run() { PYTHONPATH=src:../common/src uv run --no-sync snow-globe-capture "$@" --queue "$Q"; }',
]


def run_lines(items: Sequence[Planned], indent: str = "") -> list[str]:
    """``run archive queue add`` lines for ``items``, each group under its reason."""
    lines = []
    last_why = None
    for why, args in cli_commands(items):
        if why != last_why:
            lines.append(f"{indent}# {why}")
            last_why = why
        lines.append(f"{indent}run " + " ".join(shlex.quote(a) for a in args))
    return lines


def script(header: Sequence[str], items: Sequence[Planned], extra: Sequence[str] = ()) -> str:
    """A POSIX sh script of ``archive queue add`` commands (no request to nps.gov)."""
    lines = ["#!/bin/sh", *header, *SCRIPT_BODY, *extra, *run_lines(items)]
    lines.append("run archive queue list")
    return "\n".join(lines) + "\n"


def word_list(name: str, words: Sequence[str], per_line: int = 8) -> list[str]:
    """sh lines that set ``name`` to ``words``, space-separated, a few words a line."""
    lines = [f"{name}=''"]
    for i in range(0, len(words), per_line):
        lines.append(f'{name}="${name} {" ".join(words[i : i + per_line])}"')
    return lines


def evenings_check(last_day: date, *, allowed: Iterable[date]) -> list[str]:
    """sh lines that refuse a bad EVENINGS list before anything is queued.

    Every word must be spelled YYYY-MM-DD, must be one of ``allowed`` (the plan's Point Reyes
    T1 days: T1a as queued, T1b as planned, listed in the script as ``T3PR_DAYS``), and must
    be on or before ``last_day``, and the list must name at least one day. So a typo, a date
    that does not exist (2025-02-30), a day of the 3:2 epoch or of the owner's question (TQ)
    is refused. ``last_day`` is the last Point Reyes date whose frames all fall before the
    edge by both readings (:meth:`Camera.last_plannable`), checked on its own as well, so a
    clear day can never reach a frame dated 1 Jun 2026 in UTC even if ``allowed`` were wrong;
    make_entry alone refuses only 1 Jun local and later. ``set -f`` keeps the list from being
    expanded as file names. With no ``allowed`` day every EVENINGS day is refused.
    """
    bound = last_day.strftime("%Y%m%d")
    days = sorted({d.isoformat() for d in allowed})
    return [
        "# EVENINGS: Point Reyes's clear days, chosen from T1 frames. Each must be YYYY-MM-DD,",
        "# one of the plan's Point Reyes T1 days (T3PR_DAYS: T1a as queued, T1b as planned),",
        f"# and on or before {last_day.isoformat()}: the last Point Reyes date whose frames all",
        "# fall before 2026-06-01 00:00 UTC (plan section 9). make_entry alone would accept",
        "# 2026-05-31, a date of the 3:2 epoch or of the owner's question (TQ).",
        "set -f  # EVENINGS is split into words only, never expanded as file names",
        *word_list("T3PR_DAYS", days),
        "n=0",
        "# shellcheck disable=SC2086",
        "for d in $EVENINGS; do",
        '  case "$d" in',
        "    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;",
        '    *) echo "refused: EVENINGS day $d is not YYYY-MM-DD" >&2; exit 1 ;;',
        "  esac",
        '  case " $T3PR_DAYS " in',
        '    *" $d "*) ;;',
        '    *) echo "refused: EVENINGS day $d is not a Point Reyes T1 day of this plan'
        ' (T3PR_DAYS)" >&2; exit 1 ;;',
        "  esac",
        "  b=$(printf '%s\\n' \"$d\" | sed 's/-//g')",
        f'  if [ "$b" -gt {bound} ]; then',
        f'    echo "refused: $d is after {last_day.isoformat()}: its frames reach'
        ' 2026-06-01 UTC (D-038 pending)" >&2',
        "    exit 1",
        "  fi",
        "  n=$((n + 1))",
        "done",
        'if [ "$n" -eq 0 ]; then',
        '  echo "refused: EVENINGS names no day" >&2',
        "  exit 1",
        "fi",
    ]


def t3pr_script(
    items: Sequence[Planned], last_day: date | None = None, allowed: Iterable[date] = ()
) -> str:
    """The T3pr draft: the night entries as planned, the clear days from ``$EVENINGS``.

    ``last_day`` and ``allowed`` bound the EVENINGS days (:func:`evenings_check`); by default
    Point Reyes's last plannable date in its manifest zone, and no allowed day (so the draft
    refuses every EVENINGS day until it is written with the plan's T1 days).
    """
    if last_day is None:
        last_day = last_plannable_in(ZoneInfo(load_manifests()[PORE].time_zone))
    nights = [p for p in items if p.opts is NIGHT]
    header = [
        "# DRAFT T3pr of the design-H archive backfill: Point Reyes plate inputs for M2",
        "# (PRD 8.5 steps 2 and 8), due Sun 29 Nov. The queue works its entries in file order:",
        "# behind all of T1b it would land on 30 Nov, so queue it as soon as the clear days are",
        "# chosen, ahead of T1b's pending entries, and before T2 (plan section 8). EVENINGS",
        "# names the clear days chosen from T1a frames (or T1b's, as they land).",
        "# docs/research/archive-backfill-plan.md. Edits the queue file only: no request to",
        "# nps.gov. Every planned date passes both readings of the held-out edge.",
    ]
    extra = [
        ': "${EVENINGS:?set EVENINGS to the clear Point Reyes days chosen from T1 frames}"',
        *evenings_check(last_day, allowed=allowed),
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


def tq_script(header: Sequence[str], items: Sequence[Planned]) -> str:
    """The TQ draft: one block per camera, each queued only if ``$TQ_CAMERAS`` names it.

    TQ is the owner's question (plan section 10): the draft refuses to run without
    TQ_CAMERAS, refuses a name that has no block here, and skips every block it does not
    name, so one approved camera can be queued without editing the script.
    """
    cameras = list(dict.fromkeys(p.vid for p in items))
    lines = [
        "#!/bin/sh",
        *header,
        *SCRIPT_BODY,
        "# OWNER QUESTION: TQ_CAMERAS names the viewpoint ids whose block the owner approved,",
        "# space-separated (e.g. TQ_CAMERAS='point-reyes.beach'). Every other block is skipped.",
        ': "${TQ_CAMERAS:?OWNER QUESTION: set TQ_CAMERAS to the cameras the owner approved}"',
        "set -f  # TQ_CAMERAS is split into words only, never expanded as file names",
        *word_list("TQ_BLOCKS", cameras),
        "n=0",
        "# shellcheck disable=SC2086",
        "for c in $TQ_CAMERAS; do",
        '  case " $TQ_BLOCKS " in',
        '    *" $c "*) n=$((n + 1)) ;;',
        '    *) echo "refused: TQ_CAMERAS names $c, which has no TQ block (TQ_BLOCKS)" >&2;'
        " exit 1 ;;",
        "  esac",
        "done",
        'if [ "$n" -eq 0 ]; then',
        '  echo "refused: TQ_CAMERAS names no camera" >&2',
        "  exit 1",
        "fi",
        'approved() { case " $TQ_CAMERAS " in *" $1 "*) return 0 ;; esac; return 1; }',
    ]
    for vid in cameras:
        lines.append(f"if approved {vid}; then")
        lines += run_lines([p for p in items if p.vid == vid], indent="  ")
        lines.append("fi")
    lines.append("run archive queue list")
    return "\n".join(lines) + "\n"


#: What each draft's header says about when to queue it.
WHEN = {
    "T1b": [
        "Queue it behind T1a: right after draft-T3pr.sh if T3pr's clear days are chosen,",
        "or now, moving its entries behind T3pr later (plan section 8, ways a and b).",
    ],
    "T2": ["Queue it only after T3pr (plan section 5)."],
    "T1c": ["Queue it only after T3pr and T2 (plan section 5)."],
    "TQ": [
        "OWNER QUESTION (plan section 10): it refuses to run unless TQ_CAMERAS names the",
        "cameras the owner approved, and queues only their blocks. After T1c, except Point",
        "Reyes's block, which goes before T2 to land before the 14 Dec freeze (plan section 10).",
    ],
}


def t3pr_allowed_days(planned: Sequence[Planned], cams: Mapping[str, Camera]) -> list[date]:
    """Point Reyes's T1 days a T3pr clear day may be chosen from (T1a queued, T1b planned).

    Every one passes both readings of the edge; a refused entry's day is left out.
    """
    return sorted(
        {
            p.day
            for p in planned
            if p.vid == PORE
            and p.opts is CORE
            and p.tranche in ("T1a", "T1b")
            and p.status != "refused"
            and cams[PORE].plannable(p.day)
        }
    )


def write_scripts(
    out: Path, planned: Sequence[Planned], cams: Mapping[str, Camera], note: str
) -> None:
    """The drafts for T1b, T3pr, T2, T1c and TQ: only the entries the queue does not hold."""
    for tranche in ("T1b", "T2", "T1c", "TQ"):
        draft = [
            f"# DRAFT {tranche} of the design-H archive backfill",
            "# (docs/research/archive-backfill-plan.md), planned from",
            *(f"# {line}" for line in note.splitlines()),
            "# Edits the queue file only: no request to nps.gov. Every date passes both",
            "# readings of the held-out edge (local date and UTC instant before 2026-06-01;",
            "# plan section 9).",
            *(f"# {line}" for line in WHEN[tranche]),
        ]
        items = [p for p in planned if p.tranche == tranche and p.status == "planned"]
        text = tq_script(draft, items) if tranche == "TQ" else script(draft, items)
        (out / f"draft-{tranche}.sh").write_text(text)
    t3pr = [p for p in planned if p.tranche == "T3pr" and p.status == "planned"]
    allowed = t3pr_allowed_days(planned, cams)
    (out / "draft-T3pr.sh").write_text(t3pr_script(t3pr, cams[PORE].last_plannable(), allowed))


def budget_state(today: date) -> dict[str, Any]:
    """Today's archive-queue budget (state/archive/budget-archive-queue-<date>.json), if any."""
    path = STATE_DIR / f"budget-archive-queue-{today.isoformat()}.json"
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        return {"file": path.name, "used": 0, "limit": DAILY_BUDGET}
    return {"file": path.name, "used": int(doc.get("used", 0)), "limit": int(doc.get("limit", 0))}


def main(argv: Sequence[str] | None = None) -> None:
    """Build the plan from the queue read; write the analysis, a scratch queue and the drafts."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path, help="output folder (never under the data dir)")
    parser.add_argument("--queue", type=Path, default=LIVE_QUEUE, help="queue file (read only)")
    parser.add_argument("--today", type=date.fromisoformat, default=None, help="UTC date")
    args = parser.parse_args(argv)
    out = Path(args.out)
    refuse_under_data(out, "output folder")  # quarantine/ is inside the data dir
    out.mkdir(parents=True, exist_ok=True)
    today = args.today or datetime.now(UTC).date()
    queue_doc = json.loads(Path(args.queue).read_text())
    queue_entries = queue_doc.get("entries") or []

    cams = load_cameras()
    measured = picks_on_cached_days(cams)
    completeness = completeness_of(measured)
    planned = build(cams, completeness, queue_entries)

    # Both readings of the edge, again, on everything still to do (explicit: -O strips asserts).
    past_edge = [p.id for p in planned if p.open and not cams[p.vid].plannable(p.day)]
    if past_edge:
        raise SystemExit(f"refused: open entries past the held-out edge: {past_edge[:5]}")
    budget = budget_state(today)
    left_today = max(0, budget["limit"] - budget["used"])
    first_day, first_capacity = (
        (today, left_today)
        if left_today
        else (
            today + timedelta(days=1),
            DAILY_BUDGET,
        )
    )

    t3pr_first = build(cams, completeness, queue_entries, ("T3pr", "T1b", "T2", "T1c", "TQ"))
    hrrr = hrrr_valid_hours(cams, planned)
    note = (
        f"the queue as read (updated {queue_doc.get('updated_at')}) and T0's measured\n"
        f"16:9 switches (D-052), on {today.isoformat()}."
    )
    analysis = {
        "planned_on": today.isoformat(),
        "queue": queue_checks(planned, queue_doc, cams),
        "budget_today": budget,
        "spans": {
            vid: {
                "last_3x2": s.last_32.isoformat(),
                "first_16x9": s.first_169.isoformat(),
                "d024_start": s.d024_start.isoformat(),
                "design_start": s.design_start.isoformat() if s.design_start else None,
                "regular_window": [d.isoformat() for d in windows_of(cams)[vid]],
                "owner_window": [d.isoformat() for d in s.owner_window] if s.owner_window else None,
                "holds": [[a.isoformat(), b.isoformat()] for a, b in HOLDS.get(vid, ())],
                "note": s.note,
            }
            for vid, s in SPANS.items()
        },
        "utc_edge": utc_edge(cams),
        "picks_on_cached_days": measured,
        "completeness_used": completeness,
        "hourly_sun2_frames_on_the_15th_complete_day": frames_per_month(cams),
        "loop_throughput_no_errors": loop_throughput(),
        "loop_throughput_no_errors_20nov_inputs": loop_throughput(pass_s=11.32, request_s=0.284),
        # What a 50 ms margin on the limiter's 5 s would cost (plan section 10, item 12).
        "loop_throughput_no_errors_20nov_inputs_5_05_s": loop_throughput(
            pass_s=11.32, request_s=0.284, spacing_s=5.05
        ),
        "schedule": schedule(planned, first_day, first_capacity),
        # T3pr is due Sun 29 Nov (M2's Point Reyes plate): the same dates, T3pr before T1b.
        "schedule_t3pr_before_t1b": schedule(t3pr_first, first_day, first_capacity),
        # TQ's Point Reyes block (the owner's question), if approved, ahead of T2 (section 10).
        "schedule_t3pr_first_tq_point_reyes_before_t2": schedule(
            with_block_before(t3pr_first, PORE, "TQ", "T2", "TQ point-reyes.beach"),
            first_day,
            first_capacity,
        ),
        "t3pr_costs": {"plan_order": t3pr_costs(planned), "t3pr_first": t3pr_costs(t3pr_first)},
        "removed_31_may_entries_expected_requests": removed_31_may_cost(cams, completeness),
        "caps": caps(planned, cams),
        "per_viewpoint": per_viewpoint(planned),
        "structure": structure(planned),
        "hrrr": hrrr["summary"],
        "coverage": coverage(cams, planned),
    }
    # Capture's own make_entry must accept every entry still to queue, TQ included (it raises
    # on a day past the local guard).
    planned_new = [p for p in planned if p.status == "planned"]
    analysis["queue"]["make_entry_accepts"] = (
        f"{len(make_entries(planned_new, cams))} of {len(planned_new)}"
    )
    (out / "analysis.json").write_text(json.dumps(analysis, indent=2, default=str) + "\n")
    (out / "hrrr-valid-hours.json").write_text(json.dumps(hrrr["per_date"], indent=1) + "\n")
    (out / "plan.json").write_text(
        json.dumps(
            [
                {
                    "id": p.id,
                    "tranche": p.tranche,
                    "status": p.status,
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
                    "spent_requests": p.spent_requests,
                    "spent_bytes": p.spent_bytes,
                }
                for p in planned
            ],
            indent=1,
        )
        + "\n"
    )
    to_queue = [p for p in planned if p.status == "planned" and p.tranche != "TQ"]
    added = write_queue(out / "queue-plan.json", to_queue, cams, queue_entries)
    alt = [p for p in t3pr_first if p.status == "planned" and p.tranche != "TQ"]
    write_queue(out / "queue-plan-t3pr-first.json", alt, cams, queue_entries)
    write_scripts(out, planned, cams, note)
    (out / "ids-T1b.txt").write_text(
        "".join(f"{p.id}\n" for p in planned if p.tranche == "T1b" and p.status == "planned")
    )

    for row in analysis["schedule"]["rows"]:
        keep = ("tranche", "entries", "open_entries", "requests", "frames_expected")
        print({k: row[k] for k in keep}, row["finishes_utc_day"])
    print("queue read:", len(queue_entries), "planned and written to queue-plan.json:", len(added))
    print("completeness used:", completeness)


if __name__ == "__main__":
    main(sys.argv[1:])
