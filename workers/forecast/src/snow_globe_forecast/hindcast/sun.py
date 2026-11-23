"""The Sun's geometric altitude, for P1's daytime rule (PRD §13.4: "Sun >= 3 deg by ephemeris").

The NOAA Solar Calculator equations (Meeus, *Astronomical Algorithms*, ch. 25 and 28), the
same transcription capture records in every sidecar (``snow_globe_capture.sun``) and eval keeps
as its cross-check of oculus-core (``snow_globe_eval.noaa_solar``); this package cannot import
either. Good to about 0.01 deg, under 10 s of time near the 3 deg threshold at Rainier in
November. The altitude is **geometric** (no refraction), as eval's P1 daylight gate uses it
(``snow_globe_eval.gates.daylight_gate``, ``refracted=False``).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

__all__ = ["solar_altitude_deg"]


def solar_altitude_deg(moment: datetime, lat_deg: float, lon_deg: float) -> float:
    """Geometric Sun altitude in degrees at ``moment`` (aware) for a site (east longitude +)."""
    if moment.tzinfo is None:
        raise ValueError("solar_altitude_deg needs a timezone-aware datetime")
    utc = moment.astimezone(UTC)
    jd = utc.timestamp() / 86400.0 + 2440587.5
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
    omega = math.radians(125.04 - 1934.136 * jc)
    app_long = l0 + center - 0.00569 - 0.00478 * math.sin(omega)
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
    minutes = utc.hour * 60.0 + utc.minute + utc.second / 60.0 + utc.microsecond / 6e7
    true_solar = (minutes + eq_time_min + 4.0 * lon_deg) % 1440.0
    hour_angle = true_solar / 4.0 - 180.0
    lat = math.radians(lat_deg)
    cos_zenith = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(
        math.radians(hour_angle)
    )
    return 90.0 - math.degrees(math.acos(max(-1.0, min(1.0, cos_zenith))))
