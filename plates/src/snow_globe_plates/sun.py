"""Sun position for describing base-frame candidates (PRD §8.5 step 1: "near midday").

The NOAA GML solar-position equations (after Meeus, *Astronomical Algorithms*, ch. 25):
geometric altitude, azimuth clockwise from north, and the time of local solar noon. Accurate
to about 0.01° here, which is plenty for ranking candidate frames; evaluation's own ephemeris
(oculus-core) is not a dependency of the plate builder.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

__all__ = ["solar_noon_utc", "sun_alt_az"]


def _terms(moment: datetime) -> tuple[float, float]:
    """Solar declination (radians) and the equation of time (minutes)."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("the Sun position needs a timezone-aware time")
    jd = moment.timestamp() / 86400.0 + 2440587.5
    t = (jd - 2451545.0) / 36525.0
    l0 = (280.46646 + t * (36000.76983 + 0.0003032 * t)) % 360.0
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    mr = math.radians(m)
    c = (
        math.sin(mr) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * mr) * (0.019993 - 0.000101 * t)
        + math.sin(3 * mr) * 0.000289
    )
    omega = 125.04 - 1934.136 * t
    lam = l0 + c - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    eps0 = 23.0 + (26.0 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60.0) / 60.0
    eps = math.radians(eps0 + 0.00256 * math.cos(math.radians(omega)))
    decl = math.asin(math.sin(eps) * math.sin(math.radians(lam)))
    y = math.tan(eps / 2.0) ** 2
    l0r = math.radians(l0)
    eot = 4.0 * math.degrees(
        y * math.sin(2 * l0r)
        - 2 * e * math.sin(mr)
        + 4 * e * y * math.sin(mr) * math.cos(2 * l0r)
        - 0.5 * y * y * math.sin(4 * l0r)
        - 1.25 * e * e * math.sin(2 * mr)
    )
    return decl, eot


def sun_alt_az(moment: datetime, lat_deg: float, lon_deg: float) -> tuple[float, float]:
    """Geometric Sun altitude and azimuth (degrees) at a place and time."""
    decl, eot = _terms(moment)
    utc = moment.astimezone(UTC)
    minutes = utc.hour * 60 + utc.minute + utc.second / 60.0 + utc.microsecond / 6e7
    true_solar = (minutes + eot + 4.0 * lon_deg) % 1440.0
    hour = math.radians(true_solar / 4.0 - 180.0)
    phi = math.radians(lat_deg)
    cos_zen = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(hour)
    zen = math.acos(max(-1.0, min(1.0, cos_zen)))
    alt = 90.0 - math.degrees(zen)
    az = math.degrees(
        math.atan2(math.sin(hour), math.cos(hour) * math.sin(phi) - math.tan(decl) * math.cos(phi))
    )
    return alt, (az + 180.0) % 360.0


def solar_noon_utc(day: datetime, lon_deg: float) -> datetime:
    """UTC time of local solar noon on the UTC date of ``day`` (two passes for the drift)."""
    start = day.astimezone(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    noon = start
    for _ in range(2):
        _, eot = _terms(noon)
        noon = start + timedelta(minutes=-4.0 * lon_deg - eot)
    return noon
