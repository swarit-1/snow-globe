"""A terrain model and what each pixel sees of it (PRD §8.5 steps 4, 6 and 7).

The plate's landmark bands are bands of *world height* ("summit, upper mountain, mid-slopes,
near ridge", PRD §13.3), so the builder needs, for every pixel, the height of the terrain the
pixel looks at. That is a ray cast through a digital elevation model (DEM), which is also how
the pose is checked against the whole skyline rather than a few points.

**DEM.** A USGS 3DEP bare-earth subset, exported once as an uncompressed float32 GeoTIFF on a
geographic (EPSG:4326) grid by the National Map's ``3DEPElevation/ImageServer/exportImage``
(public domain; the request and its SHA-256 are recorded in the plate manifest). Heights are
metres above NAVD88. Only that layout is read: a single-band float32 image with the GeoTIFF
pixel-scale and tie-point tags and "pixel is area" georeferencing; anything else is refused.

**Ray cast.** For each pixel's view ray (the renderer's pinhole, :mod:`.camera`) the terrain
is sampled along the ray's azimuth every ``step_m`` metres from ``min_distance_m`` out. A
sample's elevation angle from the camera (through :mod:`.geodesy`, so curvature is exact)
is compared with the ray's: the ray meets the terrain at the first sample whose elevation
angle, or any nearer sample's, is at least the ray's. A ray that meets nothing before leaving
the DEM is ``unknown``, not sky, unless it already clears the highest terrain the DEM holds
along that azimuth to ``max_distance_m``.

**Bare earth.** The DEM has no trees or buildings, so near occluders (the big tree, the
forest in front of the mountain) are not in it; the mask builder removes them with polygons
marked by viewing (:mod:`.masks`).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import ArrayLike, NDArray
from PIL import Image
from scipy import ndimage

from .camera import Pose, pixel_rays
from .geodesy import az_el, destination, enu, surface_distance_m

__all__ = ["Dem", "DemView", "horizon_profile", "render_view", "skyline_rows"]

_TAG_PIXEL_SCALE = 33550
_TAG_TIEPOINT = 33922
_TAG_GEOKEYS = 34735
_KEY_RASTER_TYPE = 1025
_KEY_GEOGRAPHIC_TYPE = 2048
_PIXEL_IS_AREA = 1
_WGS84 = 4326


def _geokeys(raw: Any) -> dict[int, int]:
    if raw is None:
        return {}
    values = [int(v) for v in raw]
    count = values[3]
    keys: dict[int, int] = {}
    for i in range(count):
        key, location, _, value = values[4 + 4 * i : 8 + 4 * i]
        if location == 0:
            keys[key] = value
    return keys


@dataclass(frozen=True)
class Dem:
    """Heights on a regular latitude-longitude grid; row 0 is the northern edge.

    ``lon0``/``lat0`` are the west and north edges of the first pixel; ``dlon``/``dlat`` the
    pixel size in degrees.
    """

    z: NDArray[np.float64]
    lon0: float
    lat0: float
    dlon: float
    dlat: float
    sha256: str = ""

    @classmethod
    def from_geotiff(cls, path: str | Path) -> Dem:
        """Read a 3DEP ``exportImage`` GeoTIFF (see the module docstring for the layout)."""
        file_path = Path(path)
        data = file_path.read_bytes()
        with Image.open(file_path) as im:
            if im.mode != "F":
                raise ValueError(f"{file_path}: expected one float32 band, got mode {im.mode}")
            tags = im.tag_v2
            scale = tags.get(_TAG_PIXEL_SCALE)
            tie = tags.get(_TAG_TIEPOINT)
            keys = _geokeys(tags.get(_TAG_GEOKEYS))
            z = np.asarray(im, dtype=np.float64)
        if scale is None or tie is None:
            raise ValueError(f"{file_path}: no GeoTIFF pixel-scale or tie-point tag")
        if keys.get(_KEY_GEOGRAPHIC_TYPE) != _WGS84:
            raise ValueError(f"{file_path}: not on the EPSG:4326 grid (GeoKey 2048)")
        if keys.get(_KEY_RASTER_TYPE, _PIXEL_IS_AREA) != _PIXEL_IS_AREA:
            raise ValueError(f"{file_path}: only 'pixel is area' georeferencing is read")
        if tie[0] != 0 or tie[1] != 0:
            raise ValueError(f"{file_path}: the tie point must be at pixel (0, 0)")
        return cls(
            z=z,
            lon0=float(tie[3]),
            lat0=float(tie[4]),
            dlon=float(scale[0]),
            dlat=float(scale[1]),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """(west, south, east, north) in degrees."""
        rows, cols = self.z.shape
        return (
            self.lon0,
            self.lat0 - rows * self.dlat,
            self.lon0 + cols * self.dlon,
            self.lat0,
        )

    def _rc(
        self, lat: ArrayLike, lon: ArrayLike
    ) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        row = (self.lat0 - np.asarray(lat, dtype=np.float64)) / self.dlat - 0.5
        col = (np.asarray(lon, dtype=np.float64) - self.lon0) / self.dlon - 0.5
        return row, col

    def sample(self, lat: ArrayLike, lon: ArrayLike) -> NDArray[np.float64]:
        """Bilinear height at points; NaN outside the grid."""
        row, col = self._rc(lat, lon)
        rows, cols = self.z.shape
        out = ndimage.map_coordinates(
            self.z, [row.ravel(), col.ravel()], order=1, mode="nearest"
        ).reshape(row.shape)
        outside = (row < -0.5) | (col < -0.5) | (row > rows - 0.5) | (col > cols - 0.5)
        return np.where(outside, np.nan, out)

    def max_near(self, lat: float, lon: float, radius_m: float) -> tuple[float, float, float]:
        """The highest grid cell within ``radius_m`` of a point: (height, lat, lon).

        A summit's published coordinates can sit a few metres off the DEM's highest cell, so
        a summit landmark's height is taken here, with the radius recorded.
        """
        row, col = (float(v) for v in self._rc(lat, lon))
        m_lat = self.dlat * math.pi * 6371000.0 / 180.0
        m_lon = m_lat * self.dlon / self.dlat * math.cos(math.radians(lat))
        rr, rc = radius_m / m_lat, radius_m / m_lon
        r0, r1 = max(0, math.floor(row - rr)), min(self.z.shape[0], math.ceil(row + rr) + 1)
        c0, c1 = max(0, math.floor(col - rc)), min(self.z.shape[1], math.ceil(col + rc) + 1)
        if r0 >= r1 or c0 >= c1:
            raise ValueError(f"({lat}, {lon}) is outside the DEM")
        rows, cols = np.mgrid[r0:r1, c0:c1]
        inside = ((rows - row) / max(rr, 1e-9)) ** 2 + ((cols - col) / max(rc, 1e-9)) ** 2 <= 1.0
        if not inside.any():
            inside[
                np.unravel_index(np.argmin((rows - row) ** 2 + (cols - col) ** 2), rows.shape)
            ] = True
        block = np.where(inside, self.z[r0:r1, c0:c1], -np.inf)
        k = np.unravel_index(int(np.argmax(block)), block.shape)
        best_lat = self.lat0 - (rows[k] + 0.5) * self.dlat
        best_lon = self.lon0 + (cols[k] + 0.5) * self.dlon
        return float(block[k]), float(best_lat), float(best_lon)

    def to_record(self) -> dict[str, Any]:
        """Provenance for the plate manifest."""
        west, south, east, north = self.bounds
        return {
            "sha256": self.sha256,
            "shape": list(self.z.shape),
            "bounds_deg": {"west": west, "south": south, "east": east, "north": north},
            "pixel_deg": [self.dlon, self.dlat],
            "height_range_m": [float(np.nanmin(self.z)), float(np.nanmax(self.z))],
        }


def _profiles(
    dem: Dem,
    camera: tuple[float, float, float],
    azimuths: NDArray[np.float64],
    distances: NDArray[np.float64],
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Elevation angle and height of the terrain along each azimuth at each distance."""
    lat, lon = destination(camera[0], camera[1], azimuths[:, None], distances[None, :])
    height = dem.sample(lat, lon)
    _, el, _ = az_el(enu(camera, lat, lon, np.nan_to_num(height, nan=-1e4)))
    el = np.where(np.isnan(height), -90.0, el)
    return el, height


def horizon_profile(
    dem: Dem,
    camera: tuple[float, float, float],
    azimuths: ArrayLike,
    *,
    min_distance_m: float,
    max_distance_m: float,
    step_m: float = 10.0,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """The highest terrain elevation angle along each azimuth between two distances, and where."""
    az = np.asarray(azimuths, dtype=np.float64)
    d = np.arange(min_distance_m, max_distance_m, step_m)
    el, _ = _profiles(dem, camera, az, d)
    k = np.argmax(el, axis=1)
    return el[np.arange(len(az)), k], d[k]


def _unwrapped(az: NDArray[np.float64]) -> NDArray[np.float64]:
    """Azimuths as a continuous range (a view across north gets negative values)."""
    if float(np.max(az)) - float(np.min(az)) > 180.0:
        return np.where(az > 180.0, az - 360.0, az)
    return az


def skyline_rows(
    dem: Dem,
    camera: tuple[float, float, float],
    pose: Pose,
    xs: ArrayLike,
    *,
    min_distance_m: float = 1500.0,
    max_distance_m: float = 16000.0,
    step_m: float = 10.0,
    az_step_deg: float = 0.02,
) -> NDArray[np.float64]:
    """For each pixel column x, the row where the DEM horizon meets the sky (bisection).

    Terrain nearer than ``min_distance_m`` is ignored, so the near forest's bare-earth slope
    does not stand in for the skyline.
    """
    x = np.asarray(xs, dtype=np.float64)
    corners = pixel_rays(
        np.array([0.0, pose.width, 0.0, pose.width, pose.width / 2]),
        np.array([0.0, 0.0, pose.height, pose.height, pose.height / 2]),
        pose,
    )
    az_c = _unwrapped(az_el(corners)[0])
    grid = np.arange(float(az_c.min()) - 2.0, float(az_c.max()) + 2.0, az_step_deg)
    hz, _ = horizon_profile(
        dem,
        camera,
        grid % 360.0,
        min_distance_m=min_distance_m,
        max_distance_m=max_distance_m,
        step_m=step_m,
    )
    lo = np.zeros_like(x)
    hi = np.full_like(x, float(pose.height))
    for _ in range(40):
        mid = (lo + hi) / 2.0
        az, el, _ = az_el(pixel_rays(x, mid, pose))
        az_u = np.where(az - grid[0] > 360.0 - 1e-9, az - 360.0, az)
        az_u = np.where(az_u < grid[0], az_u + 360.0, az_u)
        sky = el > np.interp(az_u, grid, hz)
        lo = np.where(sky, mid, lo)
        hi = np.where(sky, hi, mid)
    return (lo + hi) / 2.0


@dataclass(frozen=True)
class DemView:
    """What each pixel of a (possibly subsampled) grid sees of the DEM.

    ``distance_m`` is the horizontal great-circle distance to the terrain hit (NaN for sky or
    unknown), ``height_m`` the terrain height there, ``sky`` True where the ray clears all
    terrain, ``unknown`` True where it left the DEM first.
    """

    xs: NDArray[np.float64]
    ys: NDArray[np.float64]
    distance_m: NDArray[np.float64]
    height_m: NDArray[np.float64]
    sky: NDArray[np.bool_]
    unknown: NDArray[np.bool_]


def render_view(
    dem: Dem,
    camera: tuple[float, float, float],
    pose: Pose,
    *,
    step_px: int = 1,
    min_distance_m: float = 40.0,
    max_distance_m: float = 16000.0,
    step_m: float = 10.0,
    az_step_deg: float = 0.025,
    chunk: int = 400_000,
) -> DemView:
    """Ray-cast every ``step_px``-th pixel centre through the DEM (see the module docstring)."""
    xs = np.arange(step_px / 2.0, pose.width, step_px)
    ys = np.arange(step_px / 2.0, pose.height, step_px)
    gx, gy = np.meshgrid(xs, ys)
    az, el, _ = az_el(pixel_rays(gx.ravel(), gy.ravel(), pose))
    az_u = _unwrapped(az)
    grid = np.arange(
        float(az_u.min()) - az_step_deg, float(az_u.max()) + 2 * az_step_deg, az_step_deg
    )
    d = np.arange(min_distance_m, max_distance_m, step_m)
    el_p, h_p = _profiles(dem, camera, grid % 360.0, d)
    run = np.maximum.accumulate(el_p, axis=1)
    covered = ~np.isnan(h_p)
    # Distance index where each azimuth first leaves the DEM (len(d) when it never does).
    leaves = np.where(covered.all(axis=1), len(d), np.argmin(covered, axis=1))
    n = len(el)
    hit_index = np.empty(n, dtype=np.int64)
    ai_all = np.clip(np.round((az_u - grid[0]) / az_step_deg).astype(np.int64), 0, len(grid) - 1)
    for start in range(0, n, chunk):
        sl = slice(start, start + chunk)
        ai, e = ai_all[sl], el[sl]
        lo = np.zeros(len(e), dtype=np.int64)
        hi = np.full(len(e), len(d), dtype=np.int64)
        for _ in range(math.ceil(math.log2(len(d))) + 1):
            mid = (lo + hi) // 2
            midc = np.clip(mid, 0, len(d) - 1)
            cond = (run[ai, midc] >= e) & (mid < len(d))
            hi = np.where(cond, mid, hi)
            lo = np.where(cond, lo, mid + 1)
        hit_index[sl] = hi
    hit = hit_index < len(d)
    hc = np.clip(hit_index, 0, len(d) - 1)
    hit &= hit_index < leaves[ai_all]
    clears = el > run[ai_all, -1]
    unknown = ~hit & ~clears & (leaves[ai_all] < len(d))
    sky = ~hit & ~unknown
    dist = np.where(hit, d[hc], np.nan)
    height = np.where(hit, h_p[ai_all, hc], np.nan)
    shape = gx.shape
    return DemView(
        xs=xs,
        ys=ys,
        distance_m=dist.reshape(shape),
        height_m=height.reshape(shape),
        sky=sky.reshape(shape),
        unknown=unknown.reshape(shape),
    )


def check_inside(dem: Dem, lat: float, lon: float) -> None:
    """Raise ``ValueError`` when a point is outside the DEM grid (for the camera, landmarks)."""
    west, south, east, north = dem.bounds
    if not (west <= lon <= east and south <= lat <= north):
        raise ValueError(f"({lat}, {lon}) is outside the DEM ({west}..{east}, {south}..{north})")


def nearest_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (re-exported for the builder)."""
    return surface_distance_m(lat1, lon1, lat2, lon2)
