"""Sunrise and sunset against NOAA to ±1 min (PRD §8.4 "Checks"; PRD §7.4 solar rules).

**Ours** is ``app/core``'s definition (``snow_globe_core::solar``), run on the same oculus-core
through eval's binding: sunrise and sunset are the first instants of the local calendar day at
which the Sun centre's *geometric* altitude crosses −0.833° (upper limb on the horizon after
34 arcmin of refraction), rising or setting. :func:`find_crossing` is ``find_crossing`` from
``app/core/src/solar.rs``: a grid of whole minutes from the start of the day that skips points
which cannot hold a crossing (the altitude changes by at most 0.26° a minute), and a bisection
of the bracketing minute to 1 ms.

**NOAA** is the NOAA GML solar calculator's hour-angle method (:func:`noaa_event`): the Sun's
declination and equation of time from :mod:`snow_globe_eval.noaa_solar` (the spreadsheet's
equations), evaluated as the NOAA web calculator does at the estimated event time and
iterated, exactly as ``app/core/tests/common`` does for its 25-date check. NOAA states its
calculator is good to about a minute for sunrise and sunset between ±72° latitude.

**Skyfield** (DE421; :mod:`.skyfield_ref`'s ``find_risings``/``find_settings`` with the horizon at
−0.833°) is the third opinion: it tells which of the two is nearer the truth.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
from snow_globe_common.timeutil import isoformat_z

from ..ephemeris import EphemerisBackend, get_ephemeris
from ..noaa_solar import julian_day, solar_position_jd
from .plan import Site
from .stats import summarize

__all__ = [
    "EVENT_TOLERANCE_S",
    "MAX_ALTITUDE_RATE_DEG_PER_MIN",
    "SUNRISE_SUNSET_DEG",
    "EventRow",
    "event_rows",
    "find_crossing",
    "local_day_bounds",
    "noaa_event",
    "summarize_events",
]

#: The Sun's centre at −0.833° geometric altitude (``app/core`` ``SUNRISE_SUNSET_DEG``).
SUNRISE_SUNSET_DEG = -0.833
#: ``app/core`` ``MAX_ALTITUDE_RATE_DEG_PER_MIN``: a safe bound on |d(altitude)/dt|.
MAX_ALTITUDE_RATE_DEG_PER_MIN = 0.26
#: PRD §8.4: sunrise and sunset against NOAA to ±1 min.
EVENT_TOLERANCE_S = 60.0
_MS = timedelta(milliseconds=1)


def local_day_bounds(zone: ZoneInfo, day: date) -> tuple[datetime, datetime]:
    """The local calendar day ``day`` as UTC ``[start, end)`` (DST days are 23 or 25 h)."""
    start = datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(UTC)
    nxt = day + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=zone).astimezone(UTC)
    return start, end


def _crosses(rising: bool, before: float, after: float) -> bool:
    return (before < 0.0 <= after) if rising else (after < 0.0 <= before)


def find_crossing(
    altitude: Callable[[datetime], float],
    threshold_deg: float,
    rising: bool,
    start: datetime,
    end: datetime,
) -> datetime | None:
    """First instant in ``[start, end)`` at which ``altitude`` crosses the threshold.

    ``app/core``'s ``find_crossing``: whole-minute grid from ``start``, skipping
    ``floor(|f| / 0.26)`` minutes when that is at least 2, then bisection to 1 ms.
    """
    if end <= start:
        return None

    def f(moment: datetime) -> float:
        return altitude(moment) - threshold_deg

    t_prev, f_prev = start, f(start)
    while True:
        safe = math.floor(abs(f_prev) / MAX_ALTITUDE_RATE_DEG_PER_MIN)
        step = min(safe, 24 * 60) if safe >= 2 else 1
        t_next = min(t_prev + timedelta(minutes=step), end)
        f_next = f(t_next)
        if _crosses(rising, f_prev, f_next):
            lo, hi, f_lo = t_prev, t_next, f_prev
            while hi - lo > _MS:
                mid = lo + (hi - lo) / 2
                f_mid = f(mid)
                if _crosses(rising, f_lo, f_mid):
                    hi = mid
                else:
                    lo, f_lo = mid, f_mid
            return lo + (hi - lo) / 2
        if t_next >= end:
            return None
        t_prev, f_prev = t_next, f_next


def noaa_event(
    day: date, lat_deg: float, lon_deg: float, alt_deg: float, rising: bool
) -> datetime | None:
    """NOAA's hour-angle sunrise or sunset (UTC) for the solar day of ``day`` at a site.

    ``event_utc`` of ``app/core/tests/common``: start from that UTC date's solar noon
    estimate (720 − 4·longitude minutes), take the declination and equation of time there,
    solve ``cos H = (sin h − sin φ sin δ) / (cos φ cos δ)`` and repeat at the new estimate, six
    times. None when the Sun does not reach ``alt_deg`` that day.
    """
    base = datetime(day.year, day.month, day.day, tzinfo=UTC)
    jd0 = julian_day(base)
    minutes = 720.0 - 4.0 * lon_deg
    rad = math.radians
    for _ in range(6):
        p = solar_position_jd(jd0 + minutes / 1440.0, lat_deg, lon_deg)
        decl = rad(p.declination_deg)
        cos_ha = (math.sin(rad(alt_deg)) - math.sin(rad(lat_deg)) * math.sin(decl)) / (
            math.cos(rad(lat_deg)) * math.cos(decl)
        )
        if not -1.0 <= cos_ha <= 1.0:
            return None
        ha = math.degrees(math.acos(cos_ha))
        noon = 720.0 - 4.0 * lon_deg - p.equation_of_time_min
        minutes = noon - 4.0 * ha if rising else noon + 4.0 * ha
    return base + timedelta(milliseconds=round(minutes * 60_000.0))


class EventRow(dict[str, Any]):
    """One sunrise or sunset: site, local date, kind and the three times (UTC ISO)."""


def event_rows(
    sites: Sequence[Site],
    first_day: date,
    last_day: date,
    *,
    ephemeris: EphemerisBackend | None = None,
    threshold_deg: float = SUNRISE_SUNSET_DEG,
) -> list[EventRow]:
    """Ours and NOAA's sunrise and sunset for every local day ``first_day``…``last_day``.

    A NOAA event whose local date is not the day's (none at these sites) is recorded as
    missing, never paired with another day's event.
    """
    backend = ephemeris or get_ephemeris("oculus")
    rows: list[EventRow] = []
    for site in sites:
        zone = ZoneInfo(site.time_zone)

        def altitude(moment: datetime, site: Site = site) -> float:
            return backend.sun_alt_az(moment, site.lat_deg, site.lon_deg, site.height_m)[0]

        day = first_day
        while day <= last_day:
            start, end = local_day_bounds(zone, day)
            for kind, rising in (("sunrise", True), ("sunset", False)):
                ours = find_crossing(altitude, threshold_deg, rising, start, end)
                noaa = noaa_event(day, site.lat_deg, site.lon_deg, threshold_deg, rising)
                if noaa is not None and noaa.astimezone(zone).date() != day:
                    noaa = None
                row = EventRow(
                    site_id=site.viewpoint_id,
                    date=day.isoformat(),
                    kind=kind,
                    ours_utc=None if ours is None else isoformat_z(ours),
                    noaa_utc=None if noaa is None else isoformat_z(noaa),
                    ours_ms=None if ours is None else ours.timestamp() * 1000.0,
                    noaa_ms=None if noaa is None else noaa.timestamp() * 1000.0,
                )
                if ours is not None and noaa is not None:
                    row["ours_minus_noaa_s"] = (ours - noaa).total_seconds()
                rows.append(row)
            day += timedelta(days=1)
    return rows


def attach_skyfield(rows: Sequence[EventRow], events: Sequence[Mapping[str, Any]]) -> None:
    """Pair each row with Skyfield's event of the same site and kind within ±30 min."""
    by_key: dict[tuple[str, str], np.ndarray] = {}
    for site in {r["site_id"] for r in rows}:
        for kind, sk in (("sunrise", "rise"), ("sunset", "set")):
            times = [
                float(e["unix_ms"])
                for e in events
                if e["site"] == site and e["kind"] == sk and e.get("crosses", True)
            ]
            by_key[(site, kind)] = np.array(sorted(times))
    for row in rows:
        times = by_key.get((row["site_id"], row["kind"]))
        anchor = row["ours_ms"] if row["ours_ms"] is not None else row["noaa_ms"]
        if times is None or times.size == 0 or anchor is None:
            continue
        i = int(np.argmin(np.abs(times - anchor)))
        if abs(times[i] - anchor) > 30 * 60_000.0:
            continue
        row["skyfield_utc"] = isoformat_z(datetime.fromtimestamp(times[i] / 1000.0, tz=UTC))
        if row["ours_ms"] is not None:
            row["ours_minus_skyfield_s"] = (row["ours_ms"] - times[i]) / 1000.0
        if row["noaa_ms"] is not None:
            row["noaa_minus_skyfield_s"] = (row["noaa_ms"] - times[i]) / 1000.0


def _summary(rows: Sequence[EventRow], field: str) -> dict[str, Any]:
    values = np.array([r.get(field, math.nan) for r in rows], dtype=np.float64)
    out = summarize(values)
    if out.get("n"):
        worst = rows[out.pop("worst_index")]
        out["worst_site"] = worst["site_id"]
        out["worst_date"] = worst["date"]
        out["worst_kind"] = worst["kind"]
        out["beyond_60_s"] = int(np.sum(np.abs(values[~np.isnan(values)]) > EVENT_TOLERANCE_S))
    return out


def summarize_events(rows: Sequence[EventRow]) -> dict[str, Any]:
    """Seconds of difference: ours − NOAA (the check), ours − Skyfield and NOAA − Skyfield."""
    fields = ("ours_minus_noaa_s", "ours_minus_skyfield_s", "noaa_minus_skyfield_s")
    out: dict[str, Any] = {
        "tolerance_s": EVENT_TOLERANCE_S,
        "threshold_deg": SUNRISE_SUNSET_DEG,
        "n_rows": len(rows),
        "missing_ours": sum(1 for r in rows if r["ours_ms"] is None),
        "missing_noaa": sum(1 for r in rows if r["noaa_ms"] is None),
        "all": {f: _summary(rows, f) for f in fields},
        "by_kind": {},
        "by_site": {},
    }
    for kind in ("sunrise", "sunset"):
        part = [r for r in rows if r["kind"] == kind]
        out["by_kind"][kind] = {f: _summary(part, f) for f in fields}
    for site in sorted({r["site_id"] for r in rows}):
        part = [r for r in rows if r["site_id"] == site]
        out["by_site"][site] = {
            kind: {f: _summary([r for r in part if r["kind"] == kind], f) for f in fields}
            for kind in ("sunrise", "sunset")
        }
    return out
