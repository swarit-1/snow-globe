"""HRRR's Lambert conformal grid: latitude/longitude to grid cell and back (PRD §8.3).

The grid is read from the GRIB2 grid definition (template 3.30), never assumed: a
message whose grid differs from the one the sample points were mapped on is rejected.
HRRR CONUS uses a sphere of radius 6,371,229 m (``shapeOfTheEarth = 6``), a tangent
cone at 38.5° N (``Latin1 = Latin2``), orientation ``LoV = 262.5°`` and 3 km cells,
scanned west to east, then south to north (``scanningMode = 64``), so the value of
cell ``(i, j)`` sits at index ``j * Nx + i``.

The formulas are the spherical Lambert conformal conic of Snyder, *Map Projections: A
Working Manual* (USGS PP 1395, 1987), eqs. 15-1 to 15-5 and 14-4. ``i`` and ``j`` are
zero-based. The mapping was checked against the latitude and longitude arrays ecCodes
decodes from a real message; the README records the maximum error.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt

__all__ = [
    "GRID_KEYS",
    "HRRR_CONUS",
    "GridMismatchError",
    "LambertConformalGrid",
]

#: ecCodes keys that define the grid; the same names are used in the test fixture.
GRID_KEYS: tuple[str, ...] = (
    "gridType",
    "Nx",
    "Ny",
    "latitudeOfFirstGridPointInDegrees",
    "longitudeOfFirstGridPointInDegrees",
    "LaDInDegrees",
    "LoVInDegrees",
    "Latin1InDegrees",
    "Latin2InDegrees",
    "DxInMetres",
    "DyInMetres",
    "scanningMode",
    "shapeOfTheEarth",
    "radius",
)

_FLOAT_TOLERANCE = 1e-6


class GridMismatchError(ValueError):
    """A GRIB message is on a different grid from the one the points were mapped on."""


@dataclass(frozen=True)
class LambertConformalGrid:
    """A Lambert conformal conic grid on a sphere (GRIB2 grid template 3.30)."""

    nx: int
    ny: int
    lat1: float
    lon1: float
    lad: float
    lov: float
    latin1: float
    latin2: float
    dx: float
    dy: float
    radius_m: float
    scanning_mode: int = 64
    _derived: dict[str, float] = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.scanning_mode != 64:
            raise NotImplementedError(
                f"scanningMode {self.scanning_mode} is not supported (HRRR uses 64)"
            )
        phi1, phi2 = math.radians(self.latin1), math.radians(self.latin2)
        if abs(self.latin1 - self.latin2) < 1e-9:
            n = math.sin(phi1)
        else:
            n = math.log(math.cos(phi1) / math.cos(phi2)) / math.log(
                math.tan(math.pi / 4 + phi2 / 2) / math.tan(math.pi / 4 + phi1 / 2)
            )
        big_f = math.cos(phi1) * math.tan(math.pi / 4 + phi1 / 2) ** n / n
        self._derived.update(n=n, F=big_f)
        rho0 = self._rho(self.lad)
        self._derived["rho0"] = rho0
        x1, y1 = self.forward(self.lat1, self.lon1)
        self._derived.update(x1=x1, y1=y1)

    # -- construction ---------------------------------------------------------------
    @classmethod
    def from_grib_keys(cls, keys: Mapping[str, Any]) -> LambertConformalGrid:
        """Build the grid from ecCodes keys (see :data:`GRID_KEYS`)."""
        if keys.get("gridType") != "lambert":
            raise GridMismatchError(f"expected a lambert grid, got {keys.get('gridType')!r}")
        if int(keys["shapeOfTheEarth"]) != 6 and "radius" not in keys:
            raise GridMismatchError("only spherical earth shapes with a radius are supported")
        return cls(
            nx=int(keys["Nx"]),
            ny=int(keys["Ny"]),
            lat1=float(keys["latitudeOfFirstGridPointInDegrees"]),
            lon1=float(keys["longitudeOfFirstGridPointInDegrees"]),
            lad=float(keys["LaDInDegrees"]),
            lov=float(keys["LoVInDegrees"]),
            latin1=float(keys["Latin1InDegrees"]),
            latin2=float(keys["Latin2InDegrees"]),
            dx=float(keys["DxInMetres"]),
            dy=float(keys["DyInMetres"]),
            radius_m=float(keys["radius"]),
            scanning_mode=int(keys["scanningMode"]),
        )

    def grib_keys(self) -> dict[str, Any]:
        """The grid as ecCodes keys (the inverse of :meth:`from_grib_keys`)."""
        return {
            "gridType": "lambert",
            "Nx": self.nx,
            "Ny": self.ny,
            "latitudeOfFirstGridPointInDegrees": self.lat1,
            "longitudeOfFirstGridPointInDegrees": self.lon1,
            "LaDInDegrees": self.lad,
            "LoVInDegrees": self.lov,
            "Latin1InDegrees": self.latin1,
            "Latin2InDegrees": self.latin2,
            "DxInMetres": self.dx,
            "DyInMetres": self.dy,
            "scanningMode": self.scanning_mode,
            "shapeOfTheEarth": 6,
            "radius": self.radius_m,
        }

    def to_dict(self) -> dict[str, Any]:
        """Plain-JSON description written into every forecast file."""
        out = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        out["type"] = "lambert_conformal_conic"
        out["index_order"] = "flat = j * nx + i; i west to east, j south to north, zero-based"
        return out

    def check_same(self, keys: Mapping[str, Any]) -> None:
        """Raise :class:`GridMismatchError` unless ``keys`` describe this grid."""
        other = LambertConformalGrid.from_grib_keys(keys)
        mine, theirs = self.grib_keys(), other.grib_keys()
        for key, value in mine.items():
            got = theirs[key]
            if isinstance(value, float):
                if abs(float(got) - value) > _FLOAT_TOLERANCE * max(1.0, abs(value)):
                    raise GridMismatchError(f"grid key {key}: expected {value}, got {got}")
            elif got != value:
                raise GridMismatchError(f"grid key {key}: expected {value!r}, got {got!r}")

    # -- projection -------------------------------------------------------------------
    @property
    def cone_constant(self) -> float:
        """Snyder's ``n``: also the factor that turns grid-relative winds earth-relative."""
        return self._derived["n"]

    def _rho(self, lat_deg: float) -> float:
        n, big_f = self._derived["n"], self._derived["F"]
        phi = math.radians(lat_deg)
        return self.radius_m * big_f / math.tan(math.pi / 4 + phi / 2) ** n

    def forward(self, lat: float, lon: float) -> tuple[float, float]:
        """Project to metres on the cone (origin at ``LaD``, ``LoV``)."""
        n = self._derived["n"]
        rho = self._rho(lat)
        theta = n * math.radians(_wrap180(lon - self.lov))
        return rho * math.sin(theta), self._derived["rho0"] - rho * math.cos(theta)

    def inverse(self, x: float, y: float) -> tuple[float, float]:
        """Inverse of :meth:`forward`; longitude in [-180, 180)."""
        n, big_f, rho0 = self._derived["n"], self._derived["F"], self._derived["rho0"]
        dy = rho0 - y
        rho = math.copysign(math.hypot(x, dy), n)
        theta = math.atan2(x, dy) if n > 0 else math.atan2(-x, -dy)
        lat = math.degrees(2 * math.atan((self.radius_m * big_f / rho) ** (1 / n)) - math.pi / 2)
        lon = _wrap180(self.lov + math.degrees(theta / n))
        return lat, lon

    def fractional_ij(self, lat: float, lon: float) -> tuple[float, float]:
        """Zero-based, fractional grid coordinates of a point."""
        x, y = self.forward(lat, lon)
        return (x - self._derived["x1"]) / self.dx, (y - self._derived["y1"]) / self.dy

    def contains(self, i: int, j: int) -> bool:
        """True if ``(i, j)`` is a cell of the grid."""
        return 0 <= i < self.nx and 0 <= j < self.ny

    def nearest_ij(self, lat: float, lon: float) -> tuple[int, int] | None:
        """The cell whose centre is nearest in grid space, or None outside the grid."""
        fi, fj = self.fractional_ij(lat, lon)
        i, j = round(fi), round(fj)
        return (i, j) if self.contains(i, j) else None

    def ij_latlon(self, i: float, j: float) -> tuple[float, float]:
        """Latitude and longitude of cell ``(i, j)`` (fractional values allowed)."""
        return self.inverse(self._derived["x1"] + i * self.dx, self._derived["y1"] + j * self.dy)

    def flat_index(self, i: int, j: int) -> int:
        """Index of cell ``(i, j)`` in the decoded value array (scanning mode 64)."""
        if not self.contains(i, j):
            raise IndexError(f"cell ({i}, {j}) is outside the {self.nx}x{self.ny} grid")
        return j * self.nx + i

    def latlon_arrays(self) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
        """Latitude and longitude of every cell in value order (vectorised inverse)."""
        n, big_f, rho0 = self._derived["n"], self._derived["F"], self._derived["rho0"]
        ii, jj = np.meshgrid(np.arange(self.nx), np.arange(self.ny))
        x = self._derived["x1"] + ii.ravel() * self.dx
        y = self._derived["y1"] + jj.ravel() * self.dy
        dy = rho0 - y
        rho = np.copysign(np.hypot(x, dy), n)
        theta = np.arctan2(x, dy) if n > 0 else np.arctan2(-x, -dy)
        lat = np.degrees(2 * np.arctan((self.radius_m * big_f / rho) ** (1 / n)) - np.pi / 2)
        lon = (self.lov + np.degrees(theta / n) + 180.0) % 360.0 - 180.0
        return lat, lon

    def fractional_ij_arrays(
        self, lat: npt.ArrayLike, lon: npt.ArrayLike
    ) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
        """Vectorised :meth:`fractional_ij` (used to check the mapping on a whole grid)."""
        n, big_f, rho0 = self._derived["n"], self._derived["F"], self._derived["rho0"]
        phi = np.radians(np.asarray(lat, dtype=np.float64))
        dlon = (np.asarray(lon, dtype=np.float64) - self.lov + 180.0) % 360.0 - 180.0
        rho = self.radius_m * big_f / np.tan(np.pi / 4 + phi / 2) ** n
        theta = n * np.radians(dlon)
        x = rho * np.sin(theta)
        y = rho0 - rho * np.cos(theta)
        return (x - self._derived["x1"]) / self.dx, (y - self._derived["y1"]) / self.dy

    def earth_relative_wind(self, u: float, v: float, lon: float) -> tuple[float, float]:
        """Rotate a grid-relative wind (``uvRelativeToGrid = 1``) to east/north components.

        On a Lambert conformal grid the grid's y axis points to true north only along
        ``LoV``; elsewhere it is turned by ``n * (lon - LoV)``.
        """
        angle = self.cone_constant * math.radians(_wrap180(lon - self.lov))
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        return u * cos_a + v * sin_a, -u * sin_a + v * cos_a


def _wrap180(degrees: float) -> float:
    return (degrees + 180.0) % 360.0 - 180.0


#: The operational HRRR CONUS grid, as read from ``hrrr.t06z.wrfsfcf00.grib2`` of
#: 2026-11-19 (``md5GridSection 78367561440d7c7b608b8532a02e4780``). Every decoded message
#: is checked against it.
HRRR_CONUS = LambertConformalGrid(
    nx=1799,
    ny=1059,
    lat1=21.138123,
    lon1=237.280472,
    lad=38.5,
    lov=262.5,
    latin1=38.5,
    latin2=38.5,
    dx=3000.0,
    dy=3000.0,
    radius_m=6371229.0,
    scanning_mode=64,
)
