"""Independent NOAA solar-position algorithm: fallback and cross-check for oculus-core (PRD §8.4).

A transcription of the NOAA Global Monitoring Laboratory solar calculator spreadsheet
(``NOAA_Solar_Calculations_day.xls``), which follows Meeus, *Astronomical Algorithms*
(1991), ch. 25 (low-accuracy Sun) and ch. 28 (equation of time). It shares no code with
oculus-core (Standish 1992 mean elements, nutation, aberration, topocentric parallax), so
agreement between the two is evidence that both are right.

Differences from oculus-core, all small at Snow Globe's thresholds:

- no ΔT (UT is used as dynamical time: about 69 s of time, < 0.001° of solar longitude);
- no topocentric parallax (the Sun's is at most 8.8″ = 0.0024°);
- low-accuracy nutation and aberration terms.

NOAA states its calculator is good to about a minute for sunrise and sunset between ±72°
latitude. The refraction model here is the spreadsheet's piecewise one
(:func:`noaa_refraction_deg`), kept separate so the geometric altitude is available.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

__all__ = [
    "NoaaSolarPosition",
    "julian_day",
    "noaa_refraction_deg",
    "solar_position",
    "solar_position_jd",
]

_UNIX_EPOCH_JD = 2440587.5


def julian_day(moment: datetime) -> float:
    """Julian Day (UT) of an aware datetime."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("solar position needs a timezone-aware datetime")
    return _UNIX_EPOCH_JD + moment.timestamp() / 86400.0


def noaa_refraction_deg(elevation_deg: float) -> float:
    """The NOAA spreadsheet's approximate refraction (degrees) for a geometric elevation.

    Piecewise: 0 above 85°; a tan series above 5°; a polynomial down to −0.575°; and
    ``−20.772 / tan(h)`` arcseconds below that, which stays positive and small for a Sun
    far below the horizon (0.033° at −10°).
    """
    h = elevation_deg
    if h > 85.0:
        arcsec = 0.0
    elif h > 5.0:
        t = math.tan(math.radians(h))
        arcsec = 58.1 / t - 0.07 / t**3 + 0.000086 / t**5
    elif h > -0.575:
        arcsec = 1735.0 + h * (-518.2 + h * (103.4 + h * (-12.79 + h * 0.711)))
    else:
        arcsec = -20.772 / math.tan(math.radians(h))
    return arcsec / 3600.0


@dataclass(frozen=True, slots=True)
class NoaaSolarPosition:
    """Solar position from the NOAA algorithm; angles in degrees, azimuth clockwise from N."""

    julian_day: float
    apparent_longitude_deg: float
    right_ascension_deg: float
    declination_deg: float
    equation_of_time_min: float
    hour_angle_deg: float
    elevation_deg: float  # geometric: no refraction
    refraction_deg: float  # NOAA piecewise model
    azimuth_deg: float

    @property
    def apparent_elevation_deg(self) -> float:
        """Elevation corrected with the NOAA refraction model."""
        return self.elevation_deg + self.refraction_deg


def _clamp(value: float) -> float:
    return max(-1.0, min(1.0, value))


def solar_position_jd(jd: float, lat_deg: float, lon_deg: float) -> NoaaSolarPosition:
    """Solar position at Julian Day ``jd`` (UT) for a site (longitude positive east)."""
    if not -90.0 <= lat_deg <= 90.0:
        raise ValueError(f"latitude {lat_deg} out of range")
    rad, deg = math.radians, math.degrees
    jc = (jd - 2451545.0) / 36525.0
    mean_long = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360.0
    mean_anom = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    ecc = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    centre = (
        math.sin(rad(mean_anom)) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
        + math.sin(rad(2 * mean_anom)) * (0.019993 - 0.000101 * jc)
        + math.sin(rad(3 * mean_anom)) * 0.000289
    )
    true_long = mean_long + centre
    omega = 125.04 - 1934.136 * jc
    app_long = true_long - 0.00569 - 0.00478 * math.sin(rad(omega))
    seconds = 21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))
    mean_obliq = 23.0 + (26.0 + seconds / 60.0) / 60.0
    obliq = mean_obliq + 0.00256 * math.cos(rad(omega))
    ra = deg(math.atan2(math.cos(rad(obliq)) * math.sin(rad(app_long)), math.cos(rad(app_long))))
    dec = deg(math.asin(_clamp(math.sin(rad(obliq)) * math.sin(rad(app_long)))))
    var_y = math.tan(rad(obliq / 2.0)) ** 2
    eq_time = 4.0 * deg(
        var_y * math.sin(2 * rad(mean_long))
        - 2 * ecc * math.sin(rad(mean_anom))
        + 4 * ecc * var_y * math.sin(rad(mean_anom)) * math.cos(2 * rad(mean_long))
        - 0.5 * var_y * var_y * math.sin(4 * rad(mean_long))
        - 1.25 * ecc * ecc * math.sin(2 * rad(mean_anom))
    )
    minutes_utc = ((jd + 0.5) % 1.0) * 1440.0
    true_solar_time = (minutes_utc + eq_time + 4.0 * lon_deg) % 1440.0
    # The spreadsheet's IF(TST/4 < 0, TST/4 + 180, TST/4 - 180); TST is already in [0, 1440).
    hour_angle = true_solar_time / 4.0 - 180.0
    cos_zenith = math.sin(rad(lat_deg)) * math.sin(rad(dec)) + math.cos(rad(lat_deg)) * math.cos(
        rad(dec)
    ) * math.cos(rad(hour_angle))
    zenith = deg(math.acos(_clamp(cos_zenith)))
    elevation = 90.0 - zenith
    denominator = math.cos(rad(lat_deg)) * math.sin(rad(zenith))
    if abs(denominator) < 1e-12:
        azimuth = 180.0 if lat_deg >= dec else 0.0
    else:
        numerator = math.sin(rad(lat_deg)) * math.cos(rad(zenith)) - math.sin(rad(dec))
        acos_term = deg(math.acos(_clamp(numerator / denominator)))
        azimuth = (acos_term + 180.0) % 360.0 if hour_angle > 0 else (540.0 - acos_term) % 360.0
    return NoaaSolarPosition(
        julian_day=jd,
        apparent_longitude_deg=app_long % 360.0,
        right_ascension_deg=ra % 360.0,
        declination_deg=dec,
        equation_of_time_min=eq_time,
        hour_angle_deg=hour_angle,
        elevation_deg=elevation,
        refraction_deg=noaa_refraction_deg(elevation),
        azimuth_deg=azimuth,
    )


def solar_position(moment: datetime, lat_deg: float, lon_deg: float) -> NoaaSolarPosition:
    """Solar position for an aware datetime at a site (longitude positive east)."""
    return solar_position_jd(julian_day(moment), lat_deg, lon_deg)
