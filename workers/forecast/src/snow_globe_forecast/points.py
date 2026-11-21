"""Where each viewpoint samples the forecast (PRD §8.3, Appendix A ``forecast_samples``).

For each viewpoint the worker samples:

- ``camera``: the cell holding the camera;
- ``ray``: points along the camera's view azimuth every 1.5 km from 1.5 to 45 km, so
  that M2 can pick, for each cloud layer, the cell where the view ray meets that
  layer's height (PRD §8.3: "not at the camera's grid cell");
- ``landmark``: every manifest landmark (Rainier's summit);
- ``neighborhood``: the 5 x 5 cells centred on the camera cell (one 3 km cell can span
  a rim and a floor, so the spread matters).

Distances use a sphere of the IUGG mean radius, like ``snow_globe_common.manifest``; over
45 km the difference from the ellipsoid is far below one 3 km cell.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from snow_globe_common.manifest import Viewpoint, great_circle_km, initial_bearing_deg

from .grid import LambertConformalGrid

__all__ = [
    "NEIGHBORHOOD_RADIUS",
    "RAY_MAX_KM",
    "RAY_STEP_KM",
    "GriddedPoint",
    "PointKind",
    "destination",
    "flat_indices",
    "sample_points",
]

PointKind = Literal["camera", "ray", "landmark", "neighborhood"]

RAY_STEP_KM = 1.5
RAY_MAX_KM = 45.0
#: 2 cells either side of the camera cell: a 5 x 5 block.
NEIGHBORHOOD_RADIUS = 2
_EARTH_RADIUS_KM = 6371.0088


@dataclass(frozen=True, slots=True)
class GriddedPoint:
    """A sample point and the grid cell it falls in."""

    id: str
    kind: PointKind
    lat: float
    lon: float
    i: int | None
    j: int | None
    cell_lat: float | None
    cell_lon: float | None
    distance_km: float
    azimuth_deg: float | None
    name: str | None = None

    @property
    def in_grid(self) -> bool:
        """True if the point falls inside the model grid."""
        return self.i is not None and self.j is not None


def destination(
    lat: float, lon: float, bearing_deg: float, distance_km: float
) -> tuple[float, float]:
    """Point reached from ``(lat, lon)`` along an initial bearing (spherical direct problem)."""
    delta = distance_km / _EARTH_RADIUS_KM
    theta = math.radians(bearing_deg)
    phi1, lam1 = math.radians(lat), math.radians(lon)
    phi2 = math.asin(
        math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(theta)
    )
    lam2 = lam1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2),
    )
    return math.degrees(phi2), (math.degrees(lam2) + 540.0) % 360.0 - 180.0


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _grid_point(
    grid: LambertConformalGrid,
    point_id: str,
    kind: PointKind,
    lat: float,
    lon: float,
    distance_km: float,
    azimuth_deg: float | None,
    name: str | None = None,
) -> GriddedPoint:
    cell = grid.nearest_ij(lat, lon)
    if cell is None:
        return GriddedPoint(
            point_id, kind, lat, lon, None, None, None, None, distance_km, azimuth_deg, name
        )
    i, j = cell
    cell_lat, cell_lon = grid.ij_latlon(i, j)
    return GriddedPoint(
        point_id, kind, lat, lon, i, j, cell_lat, cell_lon, distance_km, azimuth_deg, name
    )


def _ray_distances(step_km: float, max_km: float) -> list[float]:
    count = round(max_km / step_km)
    return [round(step_km * k, 6) for k in range(1, count + 1)]


def sample_points(
    viewpoint: Viewpoint,
    grid: LambertConformalGrid,
    *,
    ray_step_km: float = RAY_STEP_KM,
    ray_max_km: float = RAY_MAX_KM,
    neighborhood_radius: int = NEIGHBORHOOD_RADIUS,
) -> list[GriddedPoint]:
    """All sample points of one viewpoint, in a fixed order (camera, ray, landmarks, block).

    The order is part of the file format: values are stored as ``[field][hour][point]``.
    """
    cam = viewpoint.camera
    azimuth = float(cam.view_azimuth_deg) % 360.0
    points: list[GriddedPoint] = [
        _grid_point(grid, "camera", "camera", cam.lat, cam.lon, 0.0, None)
    ]
    for distance in _ray_distances(ray_step_km, ray_max_km):
        lat, lon = destination(cam.lat, cam.lon, azimuth, distance)
        points.append(
            _grid_point(grid, f"ray:{distance:.1f}km", "ray", lat, lon, distance, azimuth)
        )
    for landmark in viewpoint.landmarks:
        points.append(
            _grid_point(
                grid,
                f"landmark:{_slug(landmark.name)}",
                "landmark",
                landmark.lat,
                landmark.lon,
                round(great_circle_km(cam.lat, cam.lon, landmark.lat, landmark.lon), 4),
                round(initial_bearing_deg(cam.lat, cam.lon, landmark.lat, landmark.lon), 3),
                landmark.name,
            )
        )
    centre = points[0]
    if centre.in_grid:
        assert centre.i is not None and centre.j is not None
        for dj in range(-neighborhood_radius, neighborhood_radius + 1):
            for di in range(-neighborhood_radius, neighborhood_radius + 1):
                i, j = centre.i + di, centre.j + dj
                if not grid.contains(i, j):
                    continue
                lat, lon = grid.ij_latlon(i, j)
                points.append(
                    GriddedPoint(
                        id=f"nb:{di:+d},{dj:+d}",
                        kind="neighborhood",
                        lat=lat,
                        lon=lon,
                        i=i,
                        j=j,
                        cell_lat=lat,
                        cell_lon=lon,
                        distance_km=round(great_circle_km(cam.lat, cam.lon, lat, lon), 4),
                        azimuth_deg=(
                            None
                            if di == 0 and dj == 0
                            else round(initial_bearing_deg(cam.lat, cam.lon, lat, lon), 3)
                        ),
                    )
                )
    ids = [point.id for point in points]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate sample point ids for {viewpoint.id}")
    return points


def flat_indices(grid: LambertConformalGrid, points: Sequence[GriddedPoint]) -> list[int | None]:
    """Value-array index of each point (None for points outside the grid)."""
    return [
        grid.flat_index(p.i, p.j) if p.i is not None and p.j is not None else None for p in points
    ]
