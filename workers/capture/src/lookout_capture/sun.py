"""Solar elevation from the NOAA Solar Calculator equations (Meeus, *Astronomical Algorithms*).

Used by the archive check, to test which UTC reading of an NPS archive time stamp
reproduces NPS's own ``SolarElevationAngle`` (PRD §13.2: hindcast pairs need exact UTC
times), and by capture to record the Sun in every sidecar for the Daylight gate
(:func:`lookout_capture.frames.sun_position`, PRD §8.6). Accuracy is about 0.01° for
1950-2050, far below the ~10° a one-hour error produces at mid-morning. The product's
ephemeris is ``oculus-core`` (PRD §8.4); scoring recomputes the Sun with it.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

__all__ = ["julian_day", "refraction_deg", "solar_elevation_deg"]


def julian_day(moment: datetime) -> float:
    """Julian Day (UT) of an aware datetime."""
    if moment.tzinfo is None:
        raise ValueError("julian_day needs an aware datetime")
    utc = moment.astimezone(UTC)
    unix = utc.timestamp()
    return unix / 86400.0 + 2440587.5


def refraction_deg(elevation_deg: float) -> float:
    """Approximate atmospheric refraction (NOAA spreadsheet formula), degrees."""
    e = elevation_deg
    if e > 85.0:
        return 0.0
    te = math.tan(math.radians(e))
    if e > 5.0:
        arcsec = 58.1 / te - 0.07 / te**3 + 0.000086 / te**5
    elif e > -0.575:
        arcsec = 1735.0 + e * (-518.2 + e * (103.4 + e * (-12.79 + e * 0.711)))
    else:
        arcsec = -20.772 / te
    return arcsec / 3600.0


def solar_elevation_deg(
    moment: datetime, lat_deg: float, lon_deg: float, *, refracted: bool = False
) -> float:
    """Sun elevation above the horizon in degrees at ``moment`` for a site (east longitude +)."""
    jd = julian_day(moment)
    jc = (jd - 2451545.0) / 36525.0
    l0 = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360.0
    m = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    ecc = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    mr = math.radians(m)
    center = (
        math.sin(mr) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
        + math.sin(2 * mr) * (0.019993 - 0.000101 * jc)
        + math.sin(3 * mr) * 0.000289
    )
    true_long = l0 + center
    omega = math.radians(125.04 - 1934.136 * jc)
    app_long = true_long - 0.00569 - 0.00478 * math.sin(omega)
    mean_obliq = (
        23.0 + (26.0 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60.0) / 60.0
    )
    obliq = mean_obliq + 0.00256 * math.cos(omega)
    decl = math.asin(math.sin(math.radians(obliq)) * math.sin(math.radians(app_long)))
    y = math.tan(math.radians(obliq / 2.0)) ** 2
    l0r = math.radians(l0)
    eq_time_min = 4.0 * math.degrees(
        y * math.sin(2 * l0r)
        - 2 * ecc * math.sin(mr)
        + 4 * ecc * y * math.sin(mr) * math.cos(2 * l0r)
        - 0.5 * y * y * math.sin(4 * l0r)
        - 1.25 * ecc * ecc * math.sin(2 * mr)
    )
    utc = moment.astimezone(UTC)
    minutes = utc.hour * 60.0 + utc.minute + utc.second / 60.0 + utc.microsecond / 6e7
    true_solar = (minutes + eq_time_min + 4.0 * lon_deg) % 1440.0
    hour_angle = true_solar / 4.0 - 180.0 if true_solar >= 0 else true_solar / 4.0 + 180.0
    lat = math.radians(lat_deg)
    cos_zenith = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(
        math.radians(hour_angle)
    )
    zenith = math.degrees(math.acos(max(-1.0, min(1.0, cos_zenith))))
    elevation = 90.0 - zenith
    if refracted:
        elevation += refraction_deg(elevation)
    return elevation
