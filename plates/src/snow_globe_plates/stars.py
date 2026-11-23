"""Star directions at the camera: an independent check of the pose (PRD §8.5 steps 3, 7, 8).

PRD §8.5 solves the pose from surveyed landmarks and checks it against the Sun where the Sun is
in frame. The Sun never enters Rainier's north-facing view, but the camera's infrared night
frames show stars. A star's direction is known to far better than a pixel and does not depend
on where the camera stands, so stars fix the heading, pitch, roll and field of view on their
own, and the landmarks are then left to fix the camera's position, which the viewpoint manifest
gives only to ±500 m. (With landmarks alone, position and field of view trade off: three
mountain landmarks 5–8 km away fit equally well from several camera positions.)

**Positions.** Catalogue right ascension and declination (ICRS, J2000.0) are precessed to the
frame's date with the IAU 1976 angles (Lieske et al. 1977; Meeus, *Astronomical Algorithms*,
eq. 21.4), turned into hour angle with Greenwich mean sidereal time (IAU 1982; Meeus eq.
12.4) and the camera's longitude, and into altitude and azimuth for the camera's latitude.
Nutation (≤ 0.005°) and annual aberration (≤ 0.006°) are left out; both are under a tenth of
a pixel here.

**Refraction** lifts a star by 2–5′ at the altitudes in frame (10–30°), which is 1–2 pixels, so
it is applied: Sæmundsson's formula (Meeus eq. 16.4) scaled by pressure and temperature.
The defaults are for Paradise (about 835 hPa at 1,700 m and −3 °C on a November night).

The catalogue itself is not read here. :mod:`.starmatch` reads it (offline, from the owner's
local copy) and writes the matched stars, with their catalogue positions, to a match file in
the data dir; the build spec cites that file by SHA-256, so no catalogue position is in the
repo.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np
from numpy.typing import ArrayLike, NDArray

__all__ = [
    "J2000_JD",
    "StarObservation",
    "apparent_alt_az",
    "gmst_deg",
    "julian_date",
    "precess_j2000",
    "refraction_deg",
    "star_directions",
]

J2000_JD = 2451545.0


def julian_date(moment: datetime) -> float:
    """Julian date (UTC used for UT1; the 0.1 s difference moves a star by 0.0004°)."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("star positions need a timezone-aware time")
    return moment.timestamp() / 86400.0 + 2440587.5


def gmst_deg(jd: float) -> float:
    """Greenwich mean sidereal time in degrees (IAU 1982, Meeus eq. 12.4)."""
    t = (jd - J2000_JD) / 36525.0
    value = (
        280.46061837 + 360.98564736629 * (jd - J2000_JD) + 0.000387933 * t**2 - t**3 / 38710000.0
    )
    return value % 360.0


def precess_j2000(
    ra_deg: ArrayLike, dec_deg: ArrayLike, jd: float
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Mean right ascension and declination of date from J2000.0 (IAU 1976 angles)."""
    t = (jd - J2000_JD) / 36525.0
    arcsec = math.pi / 180.0 / 3600.0
    zeta = (2306.2181 * t + 0.30188 * t**2 + 0.017998 * t**3) * arcsec
    z = (2306.2181 * t + 1.09468 * t**2 + 0.018203 * t**3) * arcsec
    theta = (2004.3109 * t - 0.42665 * t**2 - 0.041833 * t**3) * arcsec
    ra = np.radians(np.asarray(ra_deg, dtype=np.float64))
    dec = np.radians(np.asarray(dec_deg, dtype=np.float64))
    a = np.cos(dec) * np.sin(ra + zeta)
    b = math.cos(theta) * np.cos(dec) * np.cos(ra + zeta) - math.sin(theta) * np.sin(dec)
    c = math.sin(theta) * np.cos(dec) * np.cos(ra + zeta) + math.cos(theta) * np.sin(dec)
    return (np.degrees(np.arctan2(a, b) + z) % 360.0, np.degrees(np.arcsin(np.clip(c, -1.0, 1.0))))


def refraction_deg(
    altitude_deg: ArrayLike, *, pressure_hpa: float = 835.0, temperature_c: float = -3.0
) -> NDArray[np.float64]:
    """Refraction to add to a true altitude (Sæmundsson); 0 below −1°."""
    h = np.asarray(altitude_deg, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        arcmin = 1.02 / np.tan(np.radians(h + 10.3 / (h + 5.11)))
    scale = (pressure_hpa / 1010.0) * (283.0 / (273.0 + temperature_c))
    return np.where(h > -1.0, np.maximum(arcmin, 0.0) * scale / 60.0, 0.0)


def apparent_alt_az(
    ra_deg: ArrayLike,
    dec_deg: ArrayLike,
    moment: datetime,
    lat_deg: float,
    lon_deg: float,
    *,
    pressure_hpa: float = 835.0,
    temperature_c: float = -3.0,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Azimuth (clockwise from north) and refracted altitude of J2000 stars, in degrees."""
    jd = julian_date(moment)
    ra, dec = precess_j2000(ra_deg, dec_deg, jd)
    hour = np.radians((gmst_deg(jd) + lon_deg - ra) % 360.0)
    d = np.radians(dec)
    phi = math.radians(lat_deg)
    alt = np.arcsin(
        np.clip(math.sin(phi) * np.sin(d) + math.cos(phi) * np.cos(d) * np.cos(hour), -1.0, 1.0)
    )
    az = np.arctan2(
        -np.cos(d) * np.sin(hour),
        np.sin(d) * math.cos(phi) - np.cos(d) * math.sin(phi) * np.cos(hour),
    )
    true_alt = np.degrees(alt)
    apparent = true_alt + refraction_deg(
        true_alt, pressure_hpa=pressure_hpa, temperature_c=temperature_c
    )
    return np.degrees(az) % 360.0, apparent


@dataclass(frozen=True, slots=True)
class StarObservation:
    """A catalogue star matched to a pixel of a night frame."""

    ra_deg: float
    dec_deg: float
    v_mag: float
    image_time: datetime
    px: tuple[float, float]
    frame_key: str = ""


def star_directions(
    stars: Sequence[StarObservation], lat_deg: float, lon_deg: float
) -> NDArray[np.float64]:
    """Unit ENU directions of the stars at their own frame times; shape ``(n, 3)``."""
    out = np.empty((len(stars), 3))
    for i, star in enumerate(stars):
        az, alt = apparent_alt_az(star.ra_deg, star.dec_deg, star.image_time, lat_deg, lon_deg)
        a, h = math.radians(float(az)), math.radians(float(alt))
        out[i] = (math.sin(a) * math.cos(h), math.cos(a) * math.cos(h), math.sin(h))
    return out
