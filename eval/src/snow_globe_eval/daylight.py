"""Daylight, defined only by the ephemeris Sun altitude (PRD §8.6 "Daylight" gate).

"Defined by the ephemeris Sun altitude, never by frame brightness, because brightness
correlates with weather." Nothing in this module accepts an image.

Thresholds are parameters; the two the PRD names are constants here:

- :data:`P1_MIN_SUN_ALT_DEG` = 3°: a P1 frame is daytime when the Sun is at or above 3°
  (PRD §13.4).
- :data:`CLOUD_FRACTION_MIN_SUN_ALT_DEG` = 10°: cloud fraction is scored for Sun ≥ 10°
  (PRD §13.3).

The comparison is inclusive (``>=``). The altitude is **geometric** (oculus-core, no
refraction) unless ``refracted=True``. Geometric needs no atmosphere model, so the
definition depends only on time and place; at 3° the two differ by 0.24°, about 1.4
minutes of time in a Point Reyes winter. Which one P1 uses is fixed in the
pre-registration (DECISIONS.md, M2).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

import numpy as np
from numpy.typing import NDArray
from snow_globe_common.manifest import Viewpoint

from .ephemeris import EphemerisBackend, get_ephemeris, sun_position

__all__ = [
    "CLOUD_FRACTION_MIN_SUN_ALT_DEG",
    "P1_MIN_SUN_ALT_DEG",
    "day_mask",
    "is_day",
    "sun_altitude_deg",
    "viewpoint_day_mask",
    "viewpoint_is_day",
]

P1_MIN_SUN_ALT_DEG = 3.0
CLOUD_FRACTION_MIN_SUN_ALT_DEG = 10.0


def sun_altitude_deg(
    moment: datetime,
    lat_deg: float,
    lon_deg: float,
    height_m: float = 0.0,
    *,
    refracted: bool = False,
    ephemeris: EphemerisBackend | None = None,
) -> float:
    """The Sun's altitude in degrees at a place and time (geometric unless ``refracted``)."""
    return sun_position(moment, lat_deg, lon_deg, height_m, ephemeris=ephemeris).altitude(
        refracted=refracted
    )


def is_day(
    moment: datetime,
    lat_deg: float,
    lon_deg: float,
    *,
    min_sun_alt_deg: float,
    height_m: float = 0.0,
    refracted: bool = False,
    ephemeris: EphemerisBackend | None = None,
) -> bool:
    """True when the Sun's altitude is at or above ``min_sun_alt_deg`` (PRD §8.6)."""
    altitude = sun_altitude_deg(
        moment, lat_deg, lon_deg, height_m, refracted=refracted, ephemeris=ephemeris
    )
    return altitude >= min_sun_alt_deg


def day_mask(
    times: Iterable[datetime],
    lat_deg: float,
    lon_deg: float,
    *,
    min_sun_alt_deg: float,
    height_m: float = 0.0,
    refracted: bool = False,
    ephemeris: EphemerisBackend | None = None,
) -> NDArray[np.bool_]:
    """A boolean mask over ``times``: True where the Sun is at or above the threshold."""
    backend = ephemeris or get_ephemeris()
    return np.array(
        [
            is_day(
                t,
                lat_deg,
                lon_deg,
                min_sun_alt_deg=min_sun_alt_deg,
                height_m=height_m,
                refracted=refracted,
                ephemeris=backend,
            )
            for t in times
        ],
        dtype=bool,
    )


def viewpoint_is_day(
    viewpoint: Viewpoint,
    moment: datetime,
    *,
    min_sun_alt_deg: float,
    refracted: bool = False,
    ephemeris: EphemerisBackend | None = None,
) -> bool:
    """:func:`is_day` at the viewpoint's camera position and elevation."""
    cam = viewpoint.camera
    return is_day(
        moment,
        cam.lat,
        cam.lon,
        min_sun_alt_deg=min_sun_alt_deg,
        height_m=cam.elevation_m,
        refracted=refracted,
        ephemeris=ephemeris,
    )


def viewpoint_day_mask(
    viewpoint: Viewpoint,
    times: Iterable[datetime],
    *,
    min_sun_alt_deg: float,
    refracted: bool = False,
    ephemeris: EphemerisBackend | None = None,
) -> NDArray[np.bool_]:
    """:func:`day_mask` at the viewpoint's camera position and elevation."""
    cam = viewpoint.camera
    return day_mask(
        times,
        cam.lat,
        cam.lon,
        min_sun_alt_deg=min_sun_alt_deg,
        height_m=cam.elevation_m,
        refracted=refracted,
        ephemeris=ephemeris,
    )
