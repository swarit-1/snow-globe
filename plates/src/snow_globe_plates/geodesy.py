"""Where a surveyed point lies as seen from the camera (PRD §8.5 steps 3 and 7).

The pose solve and the DEM view need the direction from the camera to a point given by
latitude, longitude and height. This module converts both to Earth-centred coordinates on the
WGS84 ellipsoid and expresses the difference in the camera's local East-North-Up frame, the
frame the renderer uses (x east, y north, z up; ``renderer/src/math/camera.ts``). Earth's
curvature is therefore included exactly: a point 7.7 km away sits about 4.7 m lower than on
a flat Earth.

Heights are metres above mean sea level (NAVD88 for USGS sources), used as if they were
ellipsoidal heights. The geoid undulation near Mount Rainier (about −20 m) is nearly the
same at the camera and at every landmark within 20 km, so it cancels in the directions to
well under 0.01°. Atmospheric refraction is not applied: over 8 km it lifts a point by
about 0.005°, far below a pixel.
"""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import ArrayLike, NDArray

__all__ = [
    "EARTH_RADIUS_M",
    "WGS84_A",
    "WGS84_F",
    "az_el",
    "destination",
    "ecef",
    "enu",
    "initial_bearing_deg",
    "offset_position",
    "surface_distance_m",
]

WGS84_A = 6378137.0
WGS84_F = 1.0 / 298.257223563
_E2 = WGS84_F * (2.0 - WGS84_F)
#: Mean Earth radius for great-circle distances and destinations (as the renderer uses).
EARTH_RADIUS_M = 6371000.0
_M_PER_DEG = math.pi * EARTH_RADIUS_M / 180.0


def ecef(lat_deg: ArrayLike, lon_deg: ArrayLike, height_m: ArrayLike) -> NDArray[np.float64]:
    """Earth-centred, Earth-fixed coordinates (metres) on the WGS84 ellipsoid; ``(..., 3)``."""
    lat = np.radians(np.asarray(lat_deg, dtype=np.float64))
    lon = np.radians(np.asarray(lon_deg, dtype=np.float64))
    h = np.asarray(height_m, dtype=np.float64)
    n = WGS84_A / np.sqrt(1.0 - _E2 * np.sin(lat) ** 2)
    x = (n + h) * np.cos(lat) * np.cos(lon)
    y = (n + h) * np.cos(lat) * np.sin(lon)
    z = (n * (1.0 - _E2) + h) * np.sin(lat)
    return np.stack(np.broadcast_arrays(x, y, z), axis=-1)


def enu(
    origin: tuple[float, float, float],
    lat_deg: ArrayLike,
    lon_deg: ArrayLike,
    height_m: ArrayLike,
) -> NDArray[np.float64]:
    """East, north and up (metres) of points from ``origin`` = (lat, lon, height); ``(..., 3)``."""
    lat0, lon0, h0 = origin
    d = ecef(lat_deg, lon_deg, height_m) - ecef(lat0, lon0, h0)
    la, lo = math.radians(lat0), math.radians(lon0)
    sla, cla, slo, clo = math.sin(la), math.cos(la), math.sin(lo), math.cos(lo)
    east = -slo * d[..., 0] + clo * d[..., 1]
    north = -sla * clo * d[..., 0] - sla * slo * d[..., 1] + cla * d[..., 2]
    up = cla * clo * d[..., 0] + cla * slo * d[..., 1] + sla * d[..., 2]
    return np.stack([east, north, up], axis=-1)


def az_el(
    vectors: ArrayLike,
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    """Azimuth (clockwise from north, in [0, 360)), elevation angle and range of ENU vectors."""
    v = np.asarray(vectors, dtype=np.float64)
    e, n, u = v[..., 0], v[..., 1], v[..., 2]
    az = np.degrees(np.arctan2(e, n)) % 360.0
    el = np.degrees(np.arctan2(u, np.hypot(e, n)))
    return az, el, np.linalg.norm(v, axis=-1)


def destination(
    lat_deg: float, lon_deg: float, azimuth_deg: ArrayLike, distance_m: ArrayLike
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Latitude and longitude reached from a point along an initial bearing (spherical Earth)."""
    la0, lo0 = math.radians(lat_deg), math.radians(lon_deg)
    az = np.radians(np.asarray(azimuth_deg, dtype=np.float64))
    ang = np.asarray(distance_m, dtype=np.float64) / EARTH_RADIUS_M
    lat = np.arcsin(math.sin(la0) * np.cos(ang) + math.cos(la0) * np.sin(ang) * np.cos(az))
    lon = lo0 + np.arctan2(
        np.sin(az) * np.sin(ang) * math.cos(la0), np.cos(ang) - math.sin(la0) * np.sin(lat)
    )
    return np.degrees(lat), (np.degrees(lon) + 540.0) % 360.0 - 180.0


def surface_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (haversine on the mean sphere)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2.0 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, degrees clockwise from north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


def offset_position(
    lat_deg: float, lon_deg: float, east_m: float, north_m: float
) -> tuple[float, float]:
    """A position moved by small east and north offsets in metres on the mean sphere.

    Below ~2 km this is exact to the sphere's scale: on the WGS84 ellipsoid at 47° N the same
    offsets are 0.1–0.3 % longer, which no pose result here is sensitive to.
    """
    lat = lat_deg + north_m / _M_PER_DEG
    lon = lon_deg + east_m / (_M_PER_DEG * math.cos(math.radians(lat_deg)))
    return lat, lon
