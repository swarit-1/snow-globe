"""The scripted replay utterances and their expected queries, read from PRD §7.3-7.6 and §10.6.

This file is the source of `scripted.tsv`. Each row holds the text the owner reads, its category
tags, the reference clock and follow-up context it is scored at, and the expected result as read
from PRD §7.4 (time rules) and §7.5 (places) by hand. The code below turns that reading into
times. It shares no code with snow-globe-core or oculus-core, so where the two disagree the
disagreement is real:

- wall times become instants with the system tz database (`zoneinfo`), under PRD §7.4's
  daylight-saving rule: a time in a spring-forward gap moves forward by the gap, and a repeated
  time takes the earlier offset (PEP 495 `fold=0` does both);
- solar events come from Meeus's low-accuracy Sun (Astronomical Algorithms, ch. 25, about 0.01°)
  with apparent sidereal time (ch. 12), the Sun's centre at the PRD §7.4 geometric altitudes, and
  a 1-minute scan bisected to well under a second.

Where PRD §7.4 is silent, the reading used is stated in the row's `prd_note`, and D-039's
proposed reading is followed when one exists. Rows tagged `owner-pending` are the exception:
their expected outcome rests on a judgement that neither PRD §7.5 nor D-039 makes (or goes
against D-039), so they are reported apart until the owner rules (bench/README.md). Run it from
the repository root (stdlib only):

    python3 -I -B bench/utterances/prd_reading.py          # writes scripted.tsv and clocks.tsv
    python3 -I -B bench/utterances/check.py --write        # then fills the parser columns

Regenerating keeps the parser's columns of a row whose text, clock and context are unchanged, and
blanks every `agreement` and `difference`, so `check.py` fails until it has rescored the rows.
`check.py` explains the columns. Nothing here records or reads audio.
"""

from __future__ import annotations

import contextlib
import csv
import json
import math
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
MANIFESTS = REPO / "viewpoints" / "manifests"
SCRIPT_TSV = HERE / "scripted.tsv"
CLOCKS_TSV = HERE / "clocks.tsv"

MACHINE_TZ = "America/Chicago"
SEVEN_DAYS = timedelta(days=7)
HRRR_HORIZON = timedelta(hours=48)
SOLAR_TOLERANCE_S = 60

# ---------------------------------------------------------------- reference clocks

#: Reference clocks: id -> (instant, what it is for). Every row is scored at one of them. They are
#: RFC 3339 instants written as Chicago wall times, because the asker's machine is in Chicago
#: (PRD §2.4, §7.6 cross-zone). C1 is the main clock; the others exist because the DST hours, the
#: evening rules and the summer "tonight" rule cannot be reached from one November noon within
#: the 7-day horizon.
CLOCKS: dict[str, tuple[str, str]] = {
    "C1": (
        "2026-11-19T12:00:00-06:00",
        "Main clock. Thu 19 Nov 2026, noon in Chicago (18:00Z): 10:00 PST at Rainier, Point Reyes "
        "and Yosemite, 11:00 MST at Grand Canyon and Grand Teton, 13:00 EST at Acadia and the "
        "Smokies, 12:00 CST at Big Bend. The PRD §7.3 example resolves to its exact JSON here.",
    ),
    "C2": (
        "2026-11-19T23:30:00-06:00",
        "Late evening. Thu 19 Nov, 23:30 in Chicago (05:30Z Fri): 21:30 PST, 22:30 MST, and "
        "already Fri 00:30 EST at Acadia and the Smokies. Passed evening events, midnight, and a "
        "place whose date differs from the asker's.",
    ),
    "C3": (
        "2026-10-31T12:00:00-05:00",
        "The day before the fall-back. Sat 31 Oct 2026, noon CDT (17:00Z): 10:00 PDT, 10:00 MST "
        "(Phoenix), 11:00 MDT, 13:00 EDT. Sun 1 Nov repeats 01:00-02:00 in every zone but Phoenix.",
    ),
    "C4": (
        "2026-11-01T01:15:00-08:00",
        "Inside Los Angeles's repeated hour. Sun 1 Nov 2026, 01:15 PST, the second pass (09:15Z; "
        "03:15 CST in Chicago).",
    ),
    "C5": (
        "2027-03-13T12:00:00-06:00",
        "The day before the spring-forward. Sat 13 Mar 2027, noon CST (18:00Z): 10:00 PST, "
        "11:00 MST, 13:00 EST. Sun 14 Mar skips 02:00-03:00 in every zone but Phoenix.",
    ),
    "C6": (
        "2027-06-21T12:00:00-05:00",
        "Midsummer. Mon 21 Jun 2027, noon CDT (17:00Z): 10:00 PDT, 10:00 MST, 13:00 EDT. Sunset "
        "is late enough that 'tonight' becomes 1 h after sunset (PRD §7.4) at some places.",
    ),
}


def clock_instant(clock_id: str) -> datetime:
    """The reference clock `clock_id` as an aware UTC instant."""
    return datetime.fromisoformat(CLOCKS[clock_id][0]).astimezone(UTC)


# ---------------------------------------------------------------- viewpoints (manifest data only)


@dataclass(frozen=True)
class Site:
    """A viewpoint's zone and camera position, read from its manifest."""

    viewpoint_id: str
    place_id: str
    tz: ZoneInfo
    lat: float
    lon: float
    elevation_m: float
    live: bool
    aliases: tuple[str, ...]


def load_sites() -> dict[str, Site]:
    """Read every manifest in viewpoints/manifests (data the parser also embeds; no parser code)."""
    sites: dict[str, Site] = {}
    for path in sorted(MANIFESTS.glob("*.json")):
        m = json.loads(path.read_text(encoding="utf-8"))
        sites[m["id"]] = Site(
            viewpoint_id=m["id"],
            place_id=m["place_id"],
            tz=ZoneInfo(m["time_zone"]),
            lat=float(m["camera"]["lat"]),
            lon=float(m["camera"]["lon"]),
            elevation_m=float(m["camera"]["elevation_m"]),
            live=bool(m["truth_camera"].get("image_url")),
            aliases=tuple(a.lower() for a in m["aliases"]),
        )
    return sites


SITES = load_sites()
PLACE_TO_VIEWPOINT = {s.place_id: s.viewpoint_id for s in SITES.values()}

# ---------------------------------------------------------------- the Sun (not oculus-core's)


def sun_altitude_deg(t: datetime, site: Site) -> float:
    """Geometric (unrefracted) altitude of the Sun's centre at `site`, degrees (Meeus ch. 25, 12).

    About 0.01° in position, so a few seconds near the horizon; the Sun's parallax (8.8") is
    subtracted, which matters by about a second.
    """
    jd = t.timestamp() / 86400.0 + 2440587.5
    jde = jd + 69.2 / 86400.0  # TT - UT, 2026-27
    tc = (jde - 2451545.0) / 36525.0
    l0 = (280.46646 + tc * (36000.76983 + 0.0003032 * tc)) % 360.0
    m = math.radians(357.52911 + tc * (35999.05029 - 0.0001537 * tc))
    c = (
        (1.914602 - tc * (0.004817 + 0.000014 * tc)) * math.sin(m)
        + (0.019993 - 0.000101 * tc) * math.sin(2 * m)
        + 0.000289 * math.sin(3 * m)
    )
    omega = math.radians(125.04 - 1934.136 * tc)
    lam = math.radians(l0 + c - 0.00569 - 0.00478 * math.sin(omega))
    eps0 = 23.0 + (26.0 + (21.448 - tc * (46.815 + tc * (0.00059 - tc * 0.001813))) / 60.0) / 60.0
    eps = math.radians(eps0 + 0.00256 * math.cos(omega))
    dec = math.asin(math.sin(eps) * math.sin(lam))
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    d = jd - 2451545.0
    tu = d / 36525.0
    gmst = 280.46061837 + 360.98564736629 * d + 0.000387933 * tu * tu - tu**3 / 38710000.0
    l_sun = math.radians(280.4665 + 36000.7698 * tc)
    l_moon = math.radians(218.3165 + 481267.8813 * tc)
    dpsi = (
        -17.20 * math.sin(omega)
        - 1.32 * math.sin(2 * l_sun)
        - 0.23 * math.sin(2 * l_moon)
        + 0.21 * math.sin(2 * omega)
    ) / 3600.0
    gast = gmst + dpsi * math.cos(eps)
    hour_angle = math.radians((gast + site.lon - math.degrees(ra)) % 360.0)
    phi = math.radians(site.lat)
    alt = math.asin(
        math.sin(phi) * math.sin(dec) + math.cos(phi) * math.cos(dec) * math.cos(hour_angle)
    )
    return math.degrees(alt) - (8.794 / 3600.0) * math.cos(alt)


#: PRD §7.4 solar events: name -> (geometric altitude of the rendered instant, rising?, rule).
#: Dawn and dusk are not in PRD §7.4; civil twilight (-6°) is the reading used (see the notes).
EVENTS: dict[str, tuple[float, bool, str]] = {
    "sunrise": (-0.833, True, "sunrise"),
    "sunset": (-0.833, False, "sunset"),
    "dawn": (-6.0, True, "dawn"),
    "dusk": (-6.0, False, "dusk"),
    "golden_morning": (2.0, True, "golden-hour"),
    "golden_evening": (2.0, False, "golden-hour"),
    "blue_morning": (-5.0, True, "blue-hour"),
    "blue_evening": (-5.0, False, "blue-hour"),
}


def find_crossing(site: Site, event: str, start: datetime, end: datetime) -> datetime | None:
    """First instant in [start, end) where the Sun crosses the event's altitude in its direction."""
    threshold, rising, _ = EVENTS[event]

    def f(t: datetime) -> float:
        return sun_altitude_deg(t, site) - threshold

    def crosses(a: float, b: float) -> bool:
        return (a < 0.0 <= b) if rising else (a >= 0.0 > b)

    t0, f0 = start, f(start)
    while t0 < end:
        t1 = min(t0 + timedelta(minutes=1), end)
        f1 = f(t1)
        if crosses(f0, f1):
            lo, hi, flo = t0, t1, f0
            while hi - lo > timedelta(milliseconds=5):
                mid = lo + (hi - lo) / 2
                fm = f(mid)
                if crosses(flo, fm):
                    hi = mid
                else:
                    lo, flo = mid, fm
            return lo + (hi - lo) / 2
        t0, f0 = t1, f1
    return None


def round_second(t: datetime) -> datetime:
    """Round to the nearest whole second."""
    return (t + timedelta(microseconds=500_000)).replace(microsecond=0)


def local_day(site: Site, d: date) -> tuple[datetime, datetime]:
    """[start, end) of the local calendar day `d` at the place, as UTC instants."""
    start = localize(site.tz, datetime(d.year, d.month, d.day))[0]
    nxt = d + timedelta(days=1)
    end = localize(site.tz, datetime(nxt.year, nxt.month, nxt.day))[0]
    return start.astimezone(UTC), end.astimezone(UTC)


def event_on(site: Site, event: str, d: date) -> datetime | None:
    """The event on local date `d` at the place."""
    start, end = local_day(site, d)
    t = find_crossing(site, event, start, end)
    return None if t is None else round_second(t)


def event_next(site: Site, event: str, now: datetime) -> datetime | None:
    """The next occurrence of the event at or after `now` (searching 48 h)."""
    t = find_crossing(site, event, now, now + timedelta(hours=48))
    return None if t is None else round_second(t)


# ---------------------------------------------------------------- wall times (PRD §7.4 DST rule)


def localize(tz: ZoneInfo, naive: datetime) -> tuple[datetime, str | None]:
    """A wall time at the place as an instant: a gap moves forward by the gap, a repeat takes the
    earlier offset (PRD §7.4). Returns the aware time and `dst_gap`, `dst_fold` or None."""
    first = naive.replace(tzinfo=tz, fold=0)
    second = naive.replace(tzinfo=tz, fold=1)
    if first.utcoffset() == second.utcoffset():
        return first, None
    back = first.astimezone(UTC).astimezone(tz)
    if back.replace(tzinfo=None) != naive:
        return back, "dst_gap"  # fold=0 in a gap uses the offset before it: forward by the gap
    return first, "dst_fold"


def wall(site: Site, d: date, hour: int, minute: int, now: datetime) -> tuple[datetime, str | None]:
    """The wall time `hour:minute` on local date `d`. Asked inside a repeated hour, a repeated time
    whose first pass is over takes its second pass (PRD §7.4 is silent; the next such time)."""
    naive = datetime(d.year, d.month, d.day, hour, minute)
    t, flag = localize(site.tz, naive)
    if flag == "dst_fold":
        later = naive.replace(tzinfo=site.tz, fold=1)
        if t.astimezone(UTC) < now <= later.astimezone(UTC):
            t = later
    return t, flag


# ---------------------------------------------------------------- the reading of PRD §7.4

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
PARTS = {"morning": 9, "afternoon": 15, "evening": 19, "night": 22}  # PRD §7.4 defaults


@dataclass
class Reading:
    """What a row should resolve to."""

    outcome: str  # query | miss | fallback | out-of-range-past | out-of-range-beyond
    viewpoint: str = ""  # viewpoint id, "no-match", or "a|b" for a close call
    rule: str = ""
    t: datetime | None = None
    chips: tuple[str, ...] = ()
    tolerance_s: int = 0


def fmt_local(t: datetime, site: Site) -> str:
    """RFC 3339 with the place's offset, whole seconds."""
    return t.astimezone(site.tz).isoformat(timespec="seconds")


def fmt_utc(t: datetime) -> str:
    """RFC 3339 in UTC with Z, whole seconds."""
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def first_on_or_after(d: date, weekday: int) -> date:
    """The first date on or after `d` that falls on `weekday` (Mon = 0)."""
    return d + timedelta(days=(weekday - d.weekday()) % 7)


def next_day_of_month(today: date, dom: int) -> date:
    """D-039: "the Nth" with no month is the next such day, this month or the first later month
    that has it, so "the 31st" asked on 19 Nov is 31 Dec (PRD §7.4 "a date: the next such day")."""
    y, m = today.year, today.month
    for _ in range(14):
        try:
            d = date(y, m, dom)
        except ValueError:  # this month has no such day
            d = None
        if d is not None and d >= today:
            return d
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    raise ValueError(f"no month has a day {dom}")


def nearest_date(today: date, month: int, dom: int) -> date:
    """D-039: a month and day without a year is the nearest such date, so a recent one reads as
    past ("November 18th" asked on 19 Nov is yesterday). A tie goes to the future."""
    candidates = []
    for y in (today.year - 1, today.year, today.year + 1):
        with contextlib.suppress(ValueError):  # 29 February outside a leap year
            candidates.append(date(y, month, dom))
    if not candidates:
        raise ValueError(f"no year near {today.year} has {month}-{dom}")
    return min(candidates, key=lambda d: (abs((d - today).days), d < today))


def day_date(day, today: date) -> tuple[date, str]:
    """A day as said -> (date, mode). Modes: fixed (a past result is out of range), weekday (a past
    result moves a week on: "the next such day"), rolling (no day: a past result moves to
    tomorrow). Dates follow D-039 (next_day_of_month, nearest_date)."""
    if day is None:
        return today, "rolling"
    if day == "today":
        return today, "fixed"
    if day == "tomorrow":
        return today + timedelta(days=1), "fixed"
    if day == "dayafter":
        return today + timedelta(days=2), "fixed"
    if day == "yesterday":
        return today - timedelta(days=1), "fixed"
    kind = day[0]
    if kind == "wd":
        return first_on_or_after(today, WEEKDAYS[day[1]]), "weekday"
    if kind == "next":
        # PRD §7.4 "a weekday ... the next such day"; "next" adds nothing to it (D-039 agrees).
        return first_on_or_after(today + timedelta(days=1), WEEKDAYS[day[1]]), "fixed"
    if kind == "date":
        _, month, dom = day
        if month is None:  # "the 22nd"
            return next_day_of_month(today, dom), "fixed"
        return nearest_date(today, month, dom), "fixed"  # "November 21st"
    raise ValueError(day)


def on_day(day, today: date, now: datetime, fn):
    """Apply `fn(date) -> (instant, flags)` on the day said, with the mode's rule for a past time.
    Returns (instant, flags, how) where how is '', 'rolled', 'next_week' or 'past'."""
    d, mode = day_date(day, today)
    t, flags = fn(d)
    if t.astimezone(UTC) >= now:
        return t, flags, ""
    if mode == "weekday":
        t, flags = fn(d + timedelta(days=7))
        return t, flags, "next_week"
    if mode == "rolling":
        t, flags = fn(d + timedelta(days=1))
        return t, flags, "rolled"
    return t, flags, "past"


def to24(hour12: int, pm: bool) -> int:
    """12-hour clock to 24-hour clock."""
    return hour12 % 12 + (12 if pm else 0)


def finish(site, now, rule, t, chips, alt=None, tolerance_s=0, how=""):
    """PRD §7.4 horizon: past -> out of range; > 7 days -> out of range; > 48 h -> GFS chip."""
    chips = list(chips)
    lead = t.astimezone(UTC) - now
    if how == "past" or lead < timedelta(0):
        return Reading("out-of-range-past", site.viewpoint_id, rule, t, (), tolerance_s)
    if lead > SEVEN_DAYS:
        return Reading("out-of-range-beyond", site.viewpoint_id, rule, t, (), tolerance_s)
    if lead > HRRR_HORIZON:
        chips.append("gfs_horizon")
    if alt is not None:
        meridiem, alt_t = alt
        alt_lead = alt_t.astimezone(UTC) - now
        if timedelta(0) <= alt_lead <= SEVEN_DAYS:  # a chip never offers an unservable time
            chips.insert(0, f"ampm:{meridiem}>{fmt_local(alt_t, site)}")
    if how == "rolled":
        chips.append("rolled_to_tomorrow")
    return Reading("query", site.viewpoint_id, rule, t, tuple(chips), tolerance_s)


def read_time(spec, site: Site, now: datetime) -> Reading:
    """Resolve one time spec at a place (the reading of PRD §7.4 described in the module doc)."""
    tz = site.tz
    today = now.astimezone(tz).date()
    kind = spec[0]

    def at(h, m):
        return lambda d: wall(site, d, h, m, now)

    def flags_of(flag):
        return [flag] if flag else []

    if kind in ("now", "notime"):
        chips = ("live",) if site.live else ()
        return Reading(
            "query", site.viewpoint_id, "now" if kind == "now" else "no-time", now, chips
        )

    if kind == "rel":
        return finish(site, now, "relative", now + timedelta(seconds=spec[1]), [])

    if kind == "bare":  # "at 9": the next 9:00 or 21:00, the other one on the AM/PM chip
        _, h, m = spec

        def nxt(h24):
            t, flag = wall(site, today, h24, m, now)
            if t.astimezone(UTC) >= now:
                return t, flag
            return wall(site, today + timedelta(days=1), h24, m, now)

        am, pm = nxt(to24(h, False)), nxt(to24(h, True))
        (t, flag), mer, alt = (am, "am", pm) if am[0] <= pm[0] else (pm, "pm", am)
        return finish(site, now, "bare-hour", t, flags_of(flag), alt=(mer, alt[0]))

    if kind == "explicit":  # "at 4 pm", "16:00": that time; if passed, tomorrow (date chip)
        _, h24, m, day = spec
        t, flag, how = on_day(day, today, now, at(h24, m))
        rule = "explicit-hour" if day is None else "day-plus-explicit-hour"
        return finish(site, now, rule, t, flags_of(flag), how=how)

    if kind == "night":  # "tonight at 1/5/11": D-039 (PRD §7.4 is silent on which half)
        _, h, m = spec
        local_now = now.astimezone(tz)
        alt = None
        if h == 12:
            t, flag = wall(site, today + timedelta(days=1), 0, m, now)
        elif 1 <= h <= 3:
            base = today if local_now.hour < 4 else today + timedelta(days=1)
            t, flag = wall(site, base, h, m, now)
        else:
            t, flag = wall(site, today, to24(h, True), m, now)
        how = ""
        if t.astimezone(UTC) < now:  # PRD §7.4: an hour with "tonight" that has passed: tomorrow
            t, flag = wall(
                site, t.astimezone(tz).date() + timedelta(days=1), t.astimezone(tz).hour, m, now
            )
            how = "rolled"
        if h in (4, 5):
            alt_t, _ = wall(site, t.astimezone(tz).date() + timedelta(days=1), h, m, now)
            alt = ("pm", alt_t)
        return finish(site, now, "explicit-hour", t, flags_of(flag), alt=alt, how=how)

    if kind == "daybare":  # "tomorrow at 4": 7-11 AM, 12 noon, 1-6 PM; chip shown
        _, day, h, m = spec
        pm = not (7 <= h <= 11)
        t, flag, how = on_day(day, today, now, at(to24(h, pm), m))
        alt_t, _ = wall(site, t.astimezone(tz).date(), to24(h, not pm), m, now)
        return finish(
            site,
            now,
            "day-plus-bare-hour",
            t,
            flags_of(flag),
            alt=("pm" if pm else "am", alt_t),
            how=how,
        )

    if kind == "noon":
        t, flag, how = on_day(spec[1], today, now, at(12, 0))
        return finish(site, now, "noon", t, flags_of(flag), how=how)

    if kind == "midnight":  # the midnight that ends the day said (or today)
        t, flag, how = on_day(
            spec[1], today, now, lambda d: wall(site, d + timedelta(days=1), 0, 0, now)
        )
        return finish(site, now, "midnight", t, flags_of(flag), how=how)

    if kind == "part":  # morning 9, afternoon 15, evening 19, night 22
        _, part, day = spec
        t, flag, how = on_day(day, today, now, at(PARTS[part], 0))
        return finish(site, now, "part-of-day", t, flags_of(flag), how=how)

    if kind == "tonight":  # 21:00, or 1 h after sunset if later; passed: the next whole hour
        base, flag = wall(site, today, 21, 0, now)
        sunset = event_on(site, "sunset", today)
        t, tol = base, 0
        if sunset is not None and sunset + timedelta(hours=1) > base:
            t, tol, flag = sunset + timedelta(hours=1), SOLAR_TOLERANCE_S, None
        if t.astimezone(UTC) < now:
            hour_start = now.astimezone(tz).replace(minute=0, second=0, microsecond=0)
            t = hour_start.astimezone(UTC) + timedelta(hours=1)
            tol, flag = 0, None
        return finish(site, now, "tonight", t, flags_of(flag), tolerance_s=tol)

    if kind == "solar":  # the next one, or the one on the day said
        _, event, day = spec
        rule = EVENTS[event][2]
        if day is None:
            t = event_next(site, event, now)
            return finish(site, now, rule, t, [], tolerance_s=SOLAR_TOLERANCE_S)

        def ev(d):
            return event_on(site, event, d), None

        t, _, how = on_day(day, today, now, ev)
        return finish(site, now, rule, t, [], tolerance_s=SOLAR_TOLERANCE_S, how=how)

    if kind == "dayonly":  # PRD §7.4 is silent on the hour; D-039 reads 12:00
        d, _ = day_date(spec[1], today)
        if d == today:  # D-039: a day said alone that names today is 12:00 while ahead, else now
            noon_t, flag = wall(site, today, 12, 0, now)
            if noon_t.astimezone(UTC) >= now:
                return finish(site, now, "day-only", noon_t, flags_of(flag))
            chips = ("live",) if site.live else ()
            return Reading("query", site.viewpoint_id, "day-only-now", now, chips)
        t, flag, how = on_day(spec[1], today, now, at(12, 0))
        return finish(site, now, "day-only", t, flags_of(flag), how=how)

    raise ValueError(spec)


# ---------------------------------------------------------------- the rows


@dataclass(frozen=True)
class Row:
    """One scripted utterance and the reading it is scored against."""

    split: str
    text: str
    tags: str
    clock: str
    viewpoint: str  # viewpoint id; "no-match"; "a|b" (close call); "" (no place said)
    spec: tuple
    context: str = ""  # place id of the previous query, for follow-ups
    outcome: str = "query"  # query | miss | fallback (the fast path should refuse)
    note: str = ""


RA, PR, GC, GT = (
    "rainier.paradise-mountain",
    "point-reyes.beach",
    "grand-canyon.yavapai",
    "grand-teton.science-center",
)
YO, AC, SM, BB = (
    "yosemite.turtleback",
    "acadia.mcfarland",
    "smokies.look-rock",
    "big-bend.panther-junction",
)

TOMORROW, TODAY, DAYAFTER, YESTERDAY = "tomorrow", "today", "dayafter", "yesterday"


def wd(name):
    return ("wd", name)


def nxt(name):
    return ("next", name)


def on(month, dom):
    return ("date", month, dom)


N_PRD_SILENT_DUSK = (
    "PRD §7.4 has no dawn or dusk rule; read as civil twilight, the Sun's centre at -6°."
)
N_DAY_ONLY = "PRD §7.4 gives no hour for a day said alone; D-039 reads 12:00."
N_NEXT = (
    "'next <weekday>' read as PRD §7.4 'the next such day' (D-039 agrees); many people would "
    "mean the one after."
)
N_TONIGHT_SMALL = (
    "PRD §7.4 says 'an hour with tonight: that time' but not which half; D-039 reads "
    "1-3 as after midnight."
)
N_TONIGHT_PASSED = (
    "PRD §7.4 is silent once 'tonight' has passed; read as the next whole hour (the "
    "parser's reading, not in D-039)."
)
N_HORIZON = (
    "Horizons are read from now. Counted from the newest usable model run (PRD §8.3 lead time), "
    "this row changes; the owner's call (bench/README.md)."
)
N_PENDING_EXTRA_WORD = (
    "owner-pending: a place name plus a word left over. D-039 sends leftover words to the "
    "fallback, while this reading decides by which real place is meant; no stated rule gives "
    "this outcome, so the row is reported apart until the owner rules."
)
N_PENDING_FUZZY = (
    "owner-pending: a real place whose name is close to an alias. Whether a fuzzy match this "
    "close is painted, refused or offered on a chip is a threshold neither PRD §7.5 nor D-039 "
    "sets, so the row is reported apart until the owner rules."
)
N_BACK_REFERENCE = (
    "PRD §7.4 'a time with no place: the last place asked about'; '{phrase}' points back to it "
    "and names no new place. Asked right after {prev} (the row before), so the last place is "
    "{place}."
)
N_PASSED_HOUR = (
    "Asked at 21:30 at the place, after {hour}: PRD §7.4 row 2, read literally, moves an hour said "
    "with {word} that has passed to tomorrow, date chip highlighted."
)

ROWS: list[Row] = [
    # ------------------------------------------------------------ dev (30)
    # d01, d02 and d06 were swapped in after the review (20 Nov): the dev rows for the rule
    # questions that test rows t036, t035/t075 and t074 raise, so those rules are developed here.
    Row(
        "dev",
        "Rainier Valley tomorrow at 9",
        "no-match,adversarial,extra-word,owner-pending",
        "C1",
        "no-match",
        (),
        outcome="miss",
        note=N_PENDING_EXTRA_WORD
        + " Rainier Valley is a Seattle neighbourhood; read, like 'Paradise Valley', as a miss.",
    ),
    Row(
        "dev",
        "Show me Tetonia at 8",
        "no-match,adversarial,carrier,owner-pending",
        "C1",
        "no-match",
        (),
        outcome="miss",
        note=N_PENDING_FUZZY
        + " Tetonia is a town in Idaho, close to the alias 'tetons'; read, like 'Tacoma', as a "
        "miss.",
    ),
    Row(
        "dev",
        "Mount Rainier at sunrise",
        "alias,sunrise,solar-passed",
        "C1",
        RA,
        ("solar", "sunrise", None),
    ),
    Row("dev", "Rainier at midnight", "alias,midnight", "C2", RA, ("midnight", None)),
    Row(
        "dev",
        "Point rays at sunrise tomorrow",
        "alias,phonetic,sunrise,tomorrow",
        "C1",
        PR,
        ("solar", "sunrise", TOMORROW),
        note="'point rays' is a manifest alias: how Point Reyes sounds.",
    ),
    Row(
        "dev",
        "That spot in half an hour",
        "follow-up,back-reference,relative",
        "C1",
        PR,
        ("rel", 1800),
        context="point-reyes",
        note=N_BACK_REFERENCE.format(
            phrase="that spot", prev="'Point rays at sunrise tomorrow'", place="Point Reyes"
        ),
    ),
    Row(
        "dev",
        "Point Reyes this morning at 8",
        "alias,explicit-hour,this-morning,rolled",
        "C1",
        PR,
        ("explicit", 8, 0, None),
        note="PRD §7.4: an hour with 'this morning' that has passed moves to tomorrow, date chip "
        "highlighted.",
    ),
    Row("dev", "Point Reyes tonight", "alias,tonight,summer,solar-based", "C6", PR, ("tonight",)),
    Row(
        "dev",
        "Grand Canyon at 9",
        "alias,bare-hour,ampm,cross-zone,phoenix",
        "C1",
        GC,
        ("bare", 9, 0),
    ),
    Row(
        "dev",
        "Yavapai tomorrow at 1:30 am",
        "alias,day-plus-explicit-hour,phoenix,dst-fall,cross-zone",
        "C3",
        GC,
        ("explicit", 1, 30, TOMORROW),
        note="Phoenix keeps MST, so 1 Nov 01:30 happens once: no DST chip.",
    ),
    Row(
        "dev",
        "South Rim tomorrow at 5",
        "alias,day-plus-bare-hour,ampm,phoenix",
        "C1",
        GC,
        ("daybare", TOMORROW, 5, 0),
    ),
    Row("dev", "Grand Teton at sunset", "alias,sunset", "C1", GT, ("solar", "sunset", None)),
    Row(
        "dev",
        "Jackson Hole this morning",
        "alias,part-of-day,this-morning,rolled",
        "C1",
        GT,
        ("part", "morning", None),
        note="PRD §7.4 gives morning = 9:00 but no rule once it has passed; read as tomorrow (the "
        "next such time).",
    ),
    Row(
        "dev",
        "Teton Science Center tomorrow at 2:30 am",
        "alias,day-plus-explicit-hour,dst-gap",
        "C5",
        GT,
        ("explicit", 2, 30, TOMORROW),
    ),
    Row(
        "dev",
        "Yosemite at sunset tomorrow",
        "alias,sunset,tomorrow",
        "C1",
        YO,
        ("solar", "sunset", TOMORROW),
    ),
    Row("dev", "Show me Yosemite", "alias,carrier,no-time", "C1", YO, ("notime",)),
    Row(
        "dev",
        "Yosemite tonight at 1",
        "alias,explicit-hour,tonight",
        "C1",
        YO,
        ("night", 1, 0),
        note=N_TONIGHT_SMALL,
    ),
    Row(
        "dev",
        "Acadia at sunrise",
        "alias,sunrise,solar-passed,eastern",
        "C1",
        AC,
        ("solar", "sunrise", None),
    ),
    Row("dev", "Bar Harbor at 2", "alias,bare-hour,ampm,eastern", "C1", AC, ("bare", 2, 0)),
    Row(
        "dev",
        "Acadia tomorrow at 9",
        "alias,day-plus-bare-hour,ampm,cross-zone,place-date",
        "C2",
        AC,
        ("daybare", TOMORROW, 9, 0),
        note="PRD-literal: 'tomorrow' is the place's (PRD §7.4), and Acadia is already on Fri 20 "
        "Nov, "
        "so this is Sat 21 Nov while the asker's Chicago date is still Thu 19 Nov.",
    ),
    Row(
        "dev", "The Smokies at sunset", "alias,sunset,eastern", "C1", SM, ("solar", "sunset", None)
    ),
    Row(
        "dev",
        "Great Smokies this evening",
        "alias,part-of-day",
        "C1",
        SM,
        ("part", "evening", None),
    ),
    Row(
        "dev",
        "Look Rock Tower live",
        "alias,now",
        "C1",
        SM,
        ("now",),
        note="'live' read as 'now' (PRD §7.4: the live frame).",
    ),
    Row(
        "dev",
        "Big Bend at 1 pm",
        "alias,explicit-hour,central",
        "C1",
        BB,
        ("explicit", 13, 0, None),
    ),
    Row(
        "dev",
        "Big Bend tomorrow at 2:30 am",
        "alias,day-plus-explicit-hour,dst-gap,central",
        "C5",
        BB,
        ("explicit", 2, 30, TOMORROW),
    ),
    Row(
        "dev",
        "Panther Junction golden hour",
        "alias,golden-hour",
        "C1",
        BB,
        ("solar", "golden_evening", None),
    ),
    Row(
        "dev",
        "What about noon tomorrow?",
        "follow-up,carrier,noon,tomorrow",
        "C1",
        BB,
        ("noon", TOMORROW),
        context="big-bend",
    ),
    Row("dev", "Mount Hood tomorrow morning", "no-match", "C1", "no-match", (), outcome="miss"),
    Row(
        "dev",
        "Grand at sunset",
        "close-call,generic-word,sunset,owner-pending",
        "C1",
        f"{GC}|{GT}",
        ("solar", "sunset", None),
        note="owner-pending: PRD §7.5 says two places within the margin get a disambiguation chip; "
        "D-039 "
        "instead reads a lone generic word as a miss. Expected time is Grand Canyon's (third in "
        "the PRD "
        "§8.2 build order).",
    ),
    Row(
        "dev",
        "Rainier this weekend",
        "fallback",
        "C1",
        RA,
        (),
        outcome="fallback",
        note="'weekend' is in no PRD §7.4 rule, so the fast path hands it to the fallback "
        "(TODO(M3)).",
    ),
    # ------------------------------------------------ test (100): the PRD §3.3 examples first
    Row(
        "test",
        "Is Rainier out tomorrow at 4?",
        "prd-example,alias,carrier,day-plus-bare-hour,ampm",
        "C1",
        RA,
        ("daybare", TOMORROW, 4, 0),
        note="The PRD §7.3 example JSON.",
    ),
    Row(
        "test",
        "Show me Rainier at 9",
        "prd-example,alias,carrier,bare-hour,ampm",
        "C1",
        RA,
        ("bare", 9, 0),
    ),
    Row(
        "test",
        "Point Reyes at sunset Friday",
        "prd-example,alias,sunset,weekday",
        "C1",
        PR,
        ("solar", "sunset", wd("fri")),
    ),
    Row(
        "test",
        "Grand Canyon golden hour",
        "prd-example,alias,golden-hour,phoenix",
        "C1",
        GC,
        ("solar", "golden_evening", None),
    ),
    Row(
        "test",
        "What about midnight?",
        "prd-example,follow-up,carrier,midnight,phoenix",
        "C1",
        GC,
        ("midnight", None),
        context="grand-canyon",
        note="PRD §3.3 asks this right after 'Grand Canyon golden hour' (the row before), so the "
        "last "
        "place asked about is Grand Canyon.",
    ),
    Row("test", "Point Reyes now", "prd-example,alias,now", "C1", PR, ("now",)),
    Row("test", "Golden Gate at 9", "prd-example,no-match", "C1", "no-match", (), outcome="miss"),
    # Rainier
    Row("test", "Is the mountain out right now?", "alias,carrier,now", "C1", RA, ("now",)),
    Row(
        "test",
        "What will Paradise look like at blue hour?",
        "alias,carrier,blue-hour",
        "C1",
        RA,
        ("solar", "blue_evening", None),
    ),
    Row(
        "test",
        "Mount Rainer tomorrow at noon",
        "alias,phonetic,noon,tomorrow",
        "C1",
        RA,
        ("noon", TOMORROW),
        note="'mount rainer' is a manifest alias: the common mispronunciation.",
    ),
    Row("test", "Tahoma in two hours", "alias,relative", "C1", RA, ("rel", 7200)),
    Row(
        "test",
        "Ranier on Saturday morning",
        "alias,spelling,part-of-day,weekday,horizon-hrrr",
        "C1",
        RA,
        ("part", "morning", wd("sat")),
        note="47 h ahead: still HRRR. " + N_HORIZON,
    ),
    Row(
        "test",
        "Mount Ranier the day after tomorrow at 11 am",
        "alias,spelling,day-plus-explicit-hour,horizon-gfs",
        "C1",
        RA,
        ("explicit", 11, 0, DAYAFTER),
        note="49 h ahead: the GFS horizon.",
    ),
    Row(
        "test",
        "Mt Rainer at a quarter to five",
        "alias,spelling,bare-hour,ampm,number-word",
        "C1",
        RA,
        ("bare", 4, 45),
    ),
    Row(
        "test",
        "Rainier Paradise golden hour tomorrow morning",
        "alias,golden-hour,morning-event,tomorrow",
        "C1",
        RA,
        ("solar", "golden_morning", TOMORROW),
    ),
    Row(
        "test",
        "Mt Rainier tomorrow at 7",
        "alias,spelling,day-plus-bare-hour,ampm,boundary",
        "C1",
        RA,
        ("daybare", TOMORROW, 7, 0),
        note="7 is the first AM hour of PRD §7.4's 7-11 rule.",
    ),
    Row(
        "test",
        "Rainier tomorrow at 6",
        "alias,day-plus-bare-hour,ampm,boundary",
        "C1",
        RA,
        ("daybare", TOMORROW, 6, 0),
        note="6 is the last PM hour of PRD §7.4's 1-6 rule.",
    ),
    Row(
        "test",
        "Rainier tomorrow at 12",
        "alias,day-plus-bare-hour,ampm,noon",
        "C1",
        RA,
        ("daybare", TOMORROW, 12, 0),
        note="PRD §7.4: 12 means noon; the chip offers the midnight that starts that day.",
    ),
    Row(
        "test",
        "Rainer at 11",
        "alias,spelling,bare-hour,ampm,cross-zone",
        "C1",
        RA,
        ("bare", 11, 0),
        note="11:00 PST is an hour ahead; read in the asker's Chicago time it would already have "
        "passed.",
    ),
    Row(
        "test",
        "Rainier from Paradise at four",
        "alias,from-viewpoint,bare-hour,ampm,number-word",
        "C1",
        RA,
        ("bare", 4, 0),
    ),
    Row(
        "test",
        "Mount Rainier National Park next Thursday",
        "alias,next-weekday,day-only,out-of-range",
        "C1",
        RA,
        ("dayonly", nxt("thu")),
        note=N_DAY_ONLY
        + " Thu 26 Nov 12:00 PST is 7 d 2 h ahead, so out of range under either reading of 'next'.",
    ),
    Row(
        "test",
        "Rainier Thursday at 9 am",
        "alias,day-plus-explicit-hour,weekday,weekday-wrap,horizon-gfs",
        "C1",
        RA,
        ("explicit", 9, 0, wd("thu")),
        note="Today is Thursday and 9 am has passed: the next such day is a week on (167 h ahead). "
        + N_HORIZON,
    ),
    Row(
        "test",
        "Rainier yesterday at noon",
        "alias,noon,out-of-range",
        "C1",
        RA,
        ("noon", YESTERDAY),
    ),
    Row(
        "test",
        "Rainier in 24 hours",
        "alias,relative,dst-fall",
        "C3",
        RA,
        ("rel", 86400),
        note="24 elapsed hours across the fall-back: 09:00 PST, an hour earlier on the wall clock "
        "than now.",
    ),
    Row(
        "test",
        "Rainier tomorrow at 1:30 am",
        "alias,day-plus-explicit-hour,dst-fold",
        "C3",
        RA,
        ("explicit", 1, 30, TOMORROW),
    ),
    Row(
        "test",
        "Rainier tomorrow at 9 am",
        "alias,day-plus-explicit-hour,dst-fall,offset-change",
        "C3",
        RA,
        ("explicit", 9, 0, TOMORROW),
        note="The valid time's offset (-08:00), not now's (-07:00).",
    ),
    Row(
        "test",
        "Rainier tomorrow at 2:30 am",
        "alias,day-plus-explicit-hour,dst-gap",
        "C5",
        RA,
        ("explicit", 2, 30, TOMORROW),
    ),
    Row(
        "test",
        "Rainier on Sunday at 9 am",
        "alias,day-plus-explicit-hour,weekday,dst-spring,offset-change",
        "C5",
        RA,
        ("explicit", 9, 0, wd("sun")),
        note="The valid time's offset (-07:00), not now's (-08:00).",
    ),
    Row(
        "test",
        "Rainier at 1:30",
        "alias,bare-hour,ampm,dst-fold,in-repeat",
        "C4",
        RA,
        ("bare", 1, 30),
        note="Asked at 01:15 PST in the repeated hour: the 01:30 PDT pass is over, so the next "
        "1:30 is the PST one. "
        "PRD §7.4 says only 'the earlier offset'; D-039 records this refinement.",
    ),
    # t030 replaced "Rainier tonight" (C6) after the review (20 Nov): no row had a passed hour
    # said with "tonight"; t082 keeps the midsummer "tonight" case.
    Row(
        "test",
        "Rainier tonight at 9",
        "alias,explicit-hour,tonight,rolled",
        "C2",
        RA,
        ("night", 9, 0),
        note=N_PASSED_HOUR.format(hour="21:00", word="'tonight'")
        + " Most people would mean the hour just gone; the owner's call.",
    ),
    Row("test", "Rainier sunset", "alias,sunset,solar-passed", "C2", RA, ("solar", "sunset", None)),
    # t032 and t034 swapped places after the review (20 Nov), so this follow-up comes straight
    # after a row that resolves to its context place.
    Row(
        "test",
        "And at sunrise?",
        "follow-up,sunrise,solar-passed",
        "C1",
        RA,
        ("solar", "sunrise", None),
        context="rainier",
        note="Asked right after 'Rainier sunset' (the row before), so the last place asked about "
        "is Rainier.",
    ),
    Row(
        "test",
        "Rainier from Longmire at noon",
        "unknown-viewpoint,from-viewpoint",
        "C1",
        "no-match",
        (),
        outcome="miss",
        note="Longmire is not a viewpoint; PRD §7.5 'never a guess' read as a miss, not the "
        "default viewpoint.",
    ),
    Row(
        "test",
        "Rainier or Point Reyes at 9",
        "close-call,alias,bare-hour,ampm",
        "C1",
        f"{RA}|{PR}",
        ("bare", 9, 0),
        note="PRD §7.5 close call: either place, with a chip for the other. Both are on Pacific "
        "time, "
        "so the time is the same.",
    ),
    Row(
        "test",
        "Paradise Valley at 9",
        "no-match,adversarial,extra-word,owner-pending",
        "C1",
        "no-match",
        (),
        outcome="miss",
        note=N_PENDING_EXTRA_WORD
        + " A real place (Arizona, Montana) that contains a Rainier alias; read as a miss.",
    ),
    Row(
        "test",
        "Tacoma at sunset",
        "no-match,adversarial,owner-pending",
        "C1",
        "no-match",
        (),
        outcome="miss",
        note=N_PENDING_FUZZY + " A real city one letter from the alias 'tahoma'; read as a miss.",
    ),
    # Point Reyes
    Row(
        "test",
        "Will Point Reyes be foggy at 8 tomorrow morning?",
        "alias,carrier,day-plus-explicit-hour",
        "C1",
        PR,
        ("explicit", 8, 0, TOMORROW),
    ),
    Row(
        "test",
        "Point Reyes Beach this afternoon",
        "alias,part-of-day",
        "C1",
        PR,
        ("part", "afternoon", None),
    ),
    Row(
        "test",
        "Point Reyes National Seashore in an hour and a half",
        "alias,relative",
        "C1",
        PR,
        ("rel", 5400),
    ),
    Row(
        "test",
        "Pt Reyes at 10:30 am",
        "alias,spelling,explicit-hour,cross-zone",
        "C1",
        PR,
        ("explicit", 10, 30, None),
        note="Half an hour ahead at the place; already past in the asker's Chicago time.",
    ),
    # t041 was "Point raise at 2 pm" at C1 until the review (20 Nov): no row had a passed PM hour.
    Row(
        "test",
        "Point raise at 8 pm",
        "alias,phonetic,explicit-hour,rolled",
        "C2",
        PR,
        ("explicit", 20, 0, None),
        note=N_PASSED_HOUR.format(hour="20:00", word="PM"),
    ),
    Row(
        "test",
        "Point ray blue hour tomorrow morning",
        "alias,phonetic,blue-hour,morning-event,tomorrow",
        "C1",
        PR,
        ("solar", "blue_morning", TOMORROW),
    ),
    Row(
        "test",
        "The Point Reyes lighthouse at dusk on Saturday",
        "alias,dusk,weekday,horizon-gfs",
        "C1",
        PR,
        ("solar", "dusk", wd("sat")),
        note=N_PRD_SILENT_DUSK,
    ),
    Row(
        "test",
        "Great Beach this morning at 7",
        "alias,explicit-hour,this-morning,rolled",
        "C1",
        PR,
        ("explicit", 7, 0, None),
        note="PRD §7.4: an hour with 'this morning' that has passed moves to tomorrow, date chip "
        "highlighted.",
    ),
    Row(
        "test",
        "Point Reyes Seashore tomorrow night",
        "alias,part-of-day,tomorrow",
        "C1",
        PR,
        ("part", "night", TOMORROW),
    ),
    Row(
        "test",
        "Point Reyes from the beach at sunset",
        "alias,from-viewpoint,sunset",
        "C1",
        PR,
        ("solar", "sunset", None),
    ),
    Row(
        "test",
        "Point Reys next Tuesday at sunset",
        "alias,spelling,sunset,next-weekday,horizon-gfs",
        "C1",
        PR,
        ("solar", "sunset", nxt("tue")),
        note=N_NEXT,
    ),
    Row(
        "test",
        "Point Reyes on Thanksgiving",
        "fallback,holiday",
        "C1",
        PR,
        (),
        outcome="fallback",
        note="A holiday is in no PRD §7.4 rule: the fallback's (TODO(M3)). Thanksgiving is Thu 26 "
        "Nov, "
        "past the 7-day horizon anyway.",
    ),
    Row(
        "test",
        "Point Raze tomorrow at 9",
        "phonetic,non-alias,day-plus-bare-hour,ampm",
        "C1",
        PR,
        ("daybare", TOMORROW, 9, 0),
        note="Not an alias: PRD §7.5's phonetic stage should still find Point Reyes.",
    ),
    # Grand Canyon (Phoenix: no DST)
    Row(
        "test",
        "Will the Grand Canyon be clear at sunrise?",
        "alias,carrier,sunrise,solar-passed,phoenix",
        "C1",
        GC,
        ("solar", "sunrise", None),
    ),
    Row(
        "test",
        "How does Grand Canyon National Park look?",
        "alias,carrier,no-time,phoenix",
        "C1",
        GC,
        ("notime",),
    ),
    Row(
        "test",
        "Grand canon at half past six",
        "alias,spelling,bare-hour,ampm,number-word,phoenix",
        "C1",
        GC,
        ("bare", 6, 30),
    ),
    Row(
        "test",
        "Grand Canyon South Rim at 11:30",
        "alias,bare-hour,ampm,cross-zone,phoenix",
        "C1",
        GC,
        ("bare", 11, 30),
        note="Half an hour ahead in Phoenix; already past in the asker's Chicago time.",
    ),
    Row(
        "test",
        "Grand Canyon from Yavapai Point on Sunday at sunset",
        "alias,from-viewpoint,sunset,weekday,horizon-gfs,phoenix",
        "C1",
        GC,
        ("solar", "sunset", wd("sun")),
    ),
    Row(
        "test",
        "The Grand Canyon tomorrow at 2:30 am",
        "alias,day-plus-explicit-hour,phoenix,dst-spring,cross-zone",
        "C5",
        GC,
        ("explicit", 2, 30, TOMORROW),
        note="Phoenix keeps MST, so 14 Mar 02:30 exists: no DST chip.",
    ),
    Row(
        "test",
        "Grand Canyon Yavapai tonight",
        "alias,tonight,summer,phoenix",
        "C6",
        GC,
        ("tonight",),
        note="Sunset + 1 h is before 21:00 here, so 21:00 (PRD §7.4).",
    ),
    Row(
        "test",
        "Grand cannon at nine o'clock",
        "alias,spelling,bare-hour,ampm,number-word,phoenix",
        "C2",
        GC,
        ("bare", 9, 0),
    ),
    # Grand Teton (Denver)
    Row(
        "test",
        "Show me the Tetons tomorrow at 8",
        "alias,carrier,day-plus-bare-hour,ampm,mountain",
        "C1",
        GT,
        ("daybare", TOMORROW, 8, 0),
    ),
    Row(
        "test",
        "Grand Titan in three hours",
        "alias,phonetic,relative,mountain",
        "C1",
        GT,
        ("rel", 10800),
        note="'grand titan' is a manifest alias: a mishearing of Grand Teton.",
    ),
    Row(
        "test",
        "Grand Teton National Park on Friday at 2 pm",
        "alias,day-plus-explicit-hour,weekday,mountain",
        "C1",
        GT,
        ("explicit", 14, 0, wd("fri")),
    ),
    Row(
        "test", "Grand Tetons at midnight", "alias,midnight,mountain", "C1", GT, ("midnight", None)
    ),
    Row(
        "test",
        "Teton Range tomorrow afternoon",
        "alias,part-of-day,tomorrow,mountain",
        "C1",
        GT,
        ("part", "afternoon", TOMORROW),
    ),
    Row(
        "test",
        "The Grand Teton Thursday at 3 pm",
        "alias,day-plus-explicit-hour,weekday,weekday-today,mountain",
        "C1",
        GT,
        ("explicit", 15, 0, wd("thu")),
        note="Today is Thursday and 3 pm is still ahead: today.",
    ),
    Row(
        "test",
        "Tetons tonight",
        "alias,tonight,tonight-passed,mountain",
        "C2",
        GT,
        ("tonight",),
        note=N_TONIGHT_PASSED,
    ),
    Row(
        "test",
        "Teton Science School at sunrise today",
        "alias,sunrise,today,out-of-range,mountain",
        "C1",
        GT,
        ("solar", "sunrise", TODAY),
        note="Today's sunrise has passed and 'today' was said: out of range (past).",
    ),
    Row(
        "test",
        "Teton at sunrise tomorrow",
        "phonetic,non-alias,sunrise,tomorrow,mountain",
        "C1",
        GT,
        ("solar", "sunrise", TOMORROW),
        note="Not an alias ('tetons' and 'grand teton' are): fuzzy matching should find Grand "
        "Teton.",
    ),
    # Yosemite
    Row(
        "test",
        "Turtleback at 6 am",
        "alias,explicit-hour,rolled",
        "C1",
        YO,
        ("explicit", 6, 0, None),
        note="PRD §7.4: an hour with AM that has passed moves to tomorrow, date chip highlighted.",
    ),
    Row(
        "test",
        "Yosemite National Park on Sunday",
        "alias,weekday,day-only,horizon-gfs",
        "C1",
        YO,
        ("dayonly", wd("sun")),
        note=N_DAY_ONLY,
    ),
    Row(
        "test",
        "Yosemity at a quarter past seven tomorrow evening",
        "alias,spelling,day-plus-explicit-hour,number-word",
        "C1",
        YO,
        ("explicit", 19, 15, TOMORROW),
    ),
    Row(
        "test",
        "Yosemite from Turtleback Dome at first light",
        "alias,from-viewpoint,dawn,solar-passed",
        "C1",
        YO,
        ("solar", "dawn", None),
        note=N_PRD_SILENT_DUSK + " 'First light' read as dawn.",
    ),
    Row(
        "test",
        "Yosemite Turtleback at 4 pm on November 21st",
        "alias,date,day-plus-explicit-hour,horizon-gfs",
        "C1",
        YO,
        ("explicit", 16, 0, on(11, 21)),
    ),
    Row(
        "test",
        "Yo Semite on December 1st",
        "alias,phonetic,date,day-only,out-of-range",
        "C1",
        YO,
        ("dayonly", on(12, 1)),
        note=N_DAY_ONLY + " 12 days ahead: out of range.",
    ),
    Row(
        "test",
        "Turtle back dome tomorrow at 2 am",
        "alias,spelling,day-plus-explicit-hour,dst-fall",
        "C3",
        YO,
        ("explicit", 2, 0, TOMORROW),
        note="02:00 on 1 Nov happens once (PST), just after the repeated hour: no DST chip.",
    ),
    # t074 and t075 swapped places after the review (20 Nov), so the follow-up comes straight after
    # a row that resolves to its context place.
    Row(
        "test",
        "Same place at dusk",
        "follow-up,back-reference,dusk",
        "C1",
        YO,
        ("solar", "dusk", None),
        context="yosemite",
        note=N_BACK_REFERENCE.format(
            phrase="same place", prev="'Turtle back dome tomorrow at 2 am'", place="Yosemite"
        )
        + " "
        + N_PRD_SILENT_DUSK,
    ),
    Row(
        "test",
        "Yosemite Valley at sunset",
        "phonetic,non-alias,extra-word,sunset,owner-pending",
        "C1",
        YO,
        ("solar", "sunset", None),
        note=N_PENDING_EXTRA_WORD
        + " The valley the camera looks over; read as Yosemite (PRD §7.5 token-set fuzzy).",
    ),
    # Acadia (New York time)
    Row(
        "test",
        "Arcadia tomorrow at 9",
        "alias,phonetic,day-plus-bare-hour,ampm,eastern",
        "C1",
        AC,
        ("daybare", TOMORROW, 9, 0),
        note="'arcadia' is a manifest alias: a mishearing of Acadia.",
    ),
    Row(
        "test",
        "McFarland Hill in the morning",
        "alias,part-of-day,rolled,eastern",
        "C1",
        AC,
        ("part", "morning", None),
        note="PRD §7.4 gives morning = 9:00 but no rule once it has passed; read as tomorrow.",
    ),
    Row(
        "test",
        "Acadia National Park blue hour",
        "alias,blue-hour,eastern",
        "C1",
        AC,
        ("solar", "blue_evening", None),
    ),
    Row(
        "test",
        "Mount Desert Island at noon",
        "alias,noon,rolled,eastern",
        "C1",
        AC,
        ("noon", None),
        note="Noon has passed at the place (13:00 EST); read as tomorrow's (PRD §7.4 is silent for "
        "noon).",
    ),
    Row(
        "test",
        "Acadia Maine on Monday at 10 am",
        "alias,day-plus-explicit-hour,weekday,horizon-gfs,eastern",
        "C1",
        AC,
        ("explicit", 10, 0, wd("mon")),
    ),
    Row(
        "test",
        "McFarland tomorrow at 1:30 am",
        "alias,day-plus-explicit-hour,dst-fold,eastern",
        "C3",
        AC,
        ("explicit", 1, 30, TOMORROW),
    ),
    Row(
        "test", "Acadia tonight", "alias,tonight,summer,solar-based,eastern", "C6", AC, ("tonight",)
    ),
    Row(
        "test",
        "Mac Farland Hill next Thursday",
        "alias,spelling,next-weekday,day-only,horizon-gfs,eastern",
        "C1",
        AC,
        ("dayonly", nxt("thu")),
        note=N_DAY_ONLY
        + " Thu 26 Nov 12:00 EST is 6 d 23 h ahead: inside the horizon, unlike the same ask at "
        "Rainier. " + N_HORIZON,
    ),
    # Smokies (New York time)
    Row("test", "Look Rock at 1:30", "alias,bare-hour,ampm,eastern", "C1", SM, ("bare", 1, 30)),
    Row(
        "test",
        "Great Smoky Mountains two hours from now",
        "alias,relative,eastern",
        "C1",
        SM,
        ("rel", 7200),
    ),
    Row(
        "test",
        "Smokey Mountains on the 22nd at sunrise",
        "alias,spelling,date,sunrise,horizon-gfs,eastern",
        "C1",
        SM,
        ("solar", "sunrise", on(None, 22)),
    ),
    Row(
        "test",
        "Great Smoky Mountains National Park Tuesday at noon",
        "alias,noon,weekday,horizon-gfs,eastern",
        "C1",
        SM,
        ("noon", wd("tue")),
    ),
    Row(
        "test",
        "Smokies tonight at 1",
        "alias,explicit-hour,tonight,place-date,eastern",
        "C2",
        SM,
        ("night", 1, 0),
        note=N_TONIGHT_SMALL
        + " Asked at 00:30 at the place: the night in progress, half an hour ahead.",
    ),
    Row(
        "test",
        "Smoky Mountains tonight at 11",
        "alias,explicit-hour,tonight,eastern",
        "C1",
        SM,
        ("night", 11, 0),
    ),
    Row(
        "test",
        "The Smokies after sunset",
        "fallback,eastern",
        "C1",
        SM,
        (),
        outcome="fallback",
        note="'after sunset' is an interval, in no PRD §7.4 rule: the fallback's (TODO(M3)).",
    ),
    Row(
        "test",
        "Look Rock next Friday at 9 am",
        "alias,next-weekday,day-plus-explicit-hour,eastern",
        "C1",
        SM,
        ("explicit", 9, 0, nxt("fri")),
        note=N_NEXT,
    ),
    Row(
        "test",
        "Smoky Mountain at noon",
        "phonetic,non-alias,noon,rolled,eastern",
        "C1",
        SM,
        ("noon", None),
        note="Singular, not an alias: fuzzy matching should find the Smokies. Noon has passed "
        "there: tomorrow's.",
    ),
    # Big Bend (Chicago time, the asker's own zone)
    Row(
        "test",
        "Big Bend Park at sunrise on Wednesday",
        "alias,sunrise,weekday,horizon-gfs,central",
        "C1",
        BB,
        ("solar", "sunrise", wd("wed")),
    ),
    Row(
        "test",
        "Big Bend National Park tomorrow at 11",
        "alias,day-plus-bare-hour,ampm,boundary,central",
        "C1",
        BB,
        ("daybare", TOMORROW, 11, 0),
        note="11 is the last AM hour of PRD §7.4's 7-11 rule.",
    ),
    Row("test", "Bigbend tonight", "alias,spelling,tonight,central", "C1", BB, ("tonight",)),
    Row(
        "test",
        "Big Bend Texas at 12",
        "alias,bare-hour,ampm,midnight,central",
        "C2",
        BB,
        ("bare", 12, 0),
        note="Bare 12 at 23:30: the next midnight is half an hour away; the chip offers noon.",
    ),
    Row(
        "test",
        "Big Ben at dawn",
        "phonetic,non-alias,dawn,solar-passed,central",
        "C1",
        BB,
        ("solar", "dawn", None),
        note="Not an alias: should still find Big Bend (PRD §7.5). " + N_PRD_SILENT_DUSK,
    ),
    Row(
        "test",
        "Big Bend tonight at 5",
        "alias,explicit-hour,tonight,ampm,central",
        "C1",
        BB,
        ("night", 5, 0),
        note="PRD §7.4 is silent on 4 and 5 with 'tonight'; D-039 reads PM with an AM/PM chip for "
        "the next morning.",
    ),
    Row(
        "test",
        "Big Sur at golden hour",
        "no-match,adversarial",
        "C1",
        "no-match",
        (),
        outcome="miss",
        note="A real coast two letters from nothing in the table but sharing 'big' with Big Bend; "
        "never a guess.",
    ),
    Row(
        "test",
        "Show me the sunset",
        "no-place,carrier,sunset",
        "C1",
        "",
        (),
        outcome="miss",
        note="No place and no earlier query: PRD §7.4 has no last place to use, so the No match "
        "state (PRD §5.2).",
    ),
]


# ---------------------------------------------------------------- TSV

COLUMNS = [
    "id",
    "split",
    "text",
    "tags",
    "clock",
    "context",
    "expected_outcome",
    "expected_viewpoint_id",
    "expected_rule",
    "expected_local",
    "expected_utc",
    "expected_chips",
    "tolerance_s",
    "prd_note",
    "parser_outcome",
    "parser_viewpoint_id",
    "parser_rule",
    "parser_local",
    "parser_utc",
    "parser_chips",
    "agreement",
    "difference",
]
PARSER_COLUMNS = COLUMNS[14:]
#: What snow-globe-parse printed; `agreement` and `difference` (the last two) are derived from it.
PARSER_OUTPUT_COLUMNS = COLUMNS[14:20]
VERDICT_COLUMNS = COLUMNS[20:]


def reading_of(row: Row) -> Reading:
    """The expected result of a row at its clock."""
    if row.outcome in ("miss", "fallback"):
        vp = "no-match" if row.outcome == "miss" else row.viewpoint
        return Reading(row.outcome, vp)
    now = clock_instant(row.clock)
    if "|" in row.viewpoint:  # close call: the first is the reading's pick
        first = row.viewpoint.split("|")[0]
        r = read_time(row.spec, SITES[first], now)
        r.viewpoint = row.viewpoint
        r.chips = (*r.chips, "place:*")
        return r
    return read_time(row.spec, SITES[row.viewpoint], now)


def build_rows() -> list[dict[str, str]]:
    """The TSV rows, ids assigned in order (d01-d30, then t001-t100)."""
    out = []
    n = {"dev": 0, "test": 0}
    for row in ROWS:
        n[row.split] += 1
        rid = f"d{n['dev']:02d}" if row.split == "dev" else f"t{n['test']:03d}"
        r = reading_of(row)
        site = (
            SITES.get(row.viewpoint.split("|")[0])
            if row.viewpoint not in ("", "no-match")
            else None
        )
        rec = {c: "" for c in COLUMNS}
        rec.update(
            id=rid,
            split=row.split,
            text=row.text,
            tags=row.tags,
            clock=row.clock,
            context=row.context,
            expected_outcome=r.outcome,
            expected_viewpoint_id=r.viewpoint,
            expected_rule=r.rule,
            expected_local=fmt_local(r.t, site) if (r.t is not None and site) else "",
            expected_utc=fmt_utc(r.t) if r.t is not None else "",
            expected_chips=";".join(r.chips),
            tolerance_s=str(r.tolerance_s),
            prd_note=row.note,
        )
        out.append(rec)
    return out


def write_tsv(path: Path, rows: list[dict[str, str]], columns: list[str]) -> None:
    """Write a tab-separated file with a header line and LF line ends."""
    for r in rows:
        for c in columns:
            if "\t" in r[c] or "\n" in r[c]:
                raise ValueError(f"{r.get('id')}: tab or newline in {c}")
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=columns,
            delimiter="\t",
            lineterminator="\n",
            quoting=csv.QUOTE_NONE,
            escapechar=None,
        )
        w.writeheader()
        w.writerows(rows)


def read_tsv(path: Path) -> list[dict[str, str]]:
    """Read a file written by write_tsv."""
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE))


def merge_parser_output(rows: list[dict[str, str]], old: list[dict[str, str]]) -> list[str]:
    """Copy snow-globe-parse's columns from `old` rows into freshly built `rows`.

    A row keeps them only if an old row had the same text, clock and context, the inputs the
    parser saw (so reordering rows keeps them). `agreement` and `difference` are always blanked:
    they depend on the expected columns just rebuilt, and check.py fails until it has rescored
    them. Returns the ids of rows left without parser output."""
    by_input = {(r["text"], r["clock"], r["context"]): r for r in old}
    unrun = []
    for r in rows:
        prev = by_input.get((r["text"], r["clock"], r["context"]))
        for c in PARSER_OUTPUT_COLUMNS:
            r[c] = prev.get(c, "") if prev else ""
        for c in VERDICT_COLUMNS:
            r[c] = ""
        if not r["parser_outcome"]:
            unrun.append(r["id"])
    return unrun


def main(argv: list[str]) -> int:
    """Regenerate scripted.tsv (keeping parser output already there, blanking the verdicts) and
    clocks.tsv."""
    rows = build_rows()
    old = read_tsv(SCRIPT_TSV) if SCRIPT_TSV.exists() and "--blank-parser" not in argv else []
    unrun = merge_parser_output(rows, old)
    write_tsv(SCRIPT_TSV, rows, COLUMNS)
    clocks = [
        {
            "clock": k,
            "now": v[0],
            "now_utc": fmt_utc(clock_instant(k)),
            "machine_tz": MACHINE_TZ,
            "what": v[1],
        }
        for k, v in CLOCKS.items()
    ]
    write_tsv(CLOCKS_TSV, clocks, ["clock", "now", "now_utc", "machine_tz", "what"])
    n_dev = sum(r["split"] == "dev" for r in rows)
    print(f"wrote {SCRIPT_TSV.name}: {len(rows)} rows ({n_dev} dev, {len(rows) - n_dev} test)")
    print(f"wrote {CLOCKS_TSV.name}: {len(clocks)} clocks")
    if unrun:
        print(f"no parser output yet for: {', '.join(unrun)}")
    print(
        "agreement and difference are blank: run check.py --write (or check.py --no-parser --write "
        "to rescore against the stored parser output)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
