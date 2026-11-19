"""Reading lookout-forecast run files for evaluation (PRD §8.3, §13.6).

The file format is lookout-forecast's ``lookout.forecast-run`` schema version 1
(``workers/forecast/src/lookout_forecast/schema/forecast_run.schema.json``): ``values`` is
``[field][forecast hour][point]``, each hour records its availability time (live S3
``LastModified`` or modelled), and each height field names its datum. This module reads the
file directly and does not import lookout-forecast.

- **Valid time between hours:** values are interpolated linearly (PRD §8.3). Where one of
  the two hours is null (for HRRR heights, null means "no ceiling" or "no cloud"), the
  nearer hour's value is used and the reading is flagged.
- **Availability:** the later of the two bracketing hours (a prediction needs both).
- **Datums:** heights come back in metres above MSL; a field measured above ground gets the
  point's model terrain added. Anything else is refused.
- **Winds** are stored grid-relative (``uvRelativeToGrid = 1``) and rotated to east/north
  with the Lambert cone constant.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lookout_common.manifest import Landmark, Viewpoint, initial_bearing_deg
from lookout_common.timeutil import parse_iso_utc

from .baselines import RaySample

__all__ = [
    "CEILING",
    "CLOUD_BASE",
    "CLOUD_TOP",
    "HIGH_CLOUD",
    "LOW_CLOUD",
    "MID_CLOUD",
    "PRECIP_RATE",
    "TERRAIN",
    "U_WIND",
    "VISIBILITY",
    "V_WIND",
    "ForecastFileError",
    "ForecastRun",
    "Reading",
    "summit_ray",
]

KIND = "lookout.forecast-run"
CEILING = "HGT:cloud ceiling"
CLOUD_BASE = "HGT:cloud base"
CLOUD_TOP = "HGT:cloud top"
LOW_CLOUD = "LCDC:low cloud layer"
MID_CLOUD = "MCDC:middle cloud layer"
HIGH_CLOUD = "HCDC:high cloud layer"
VISIBILITY = "VIS:surface"
PRECIP_RATE = "PRATE:surface"
U_WIND = "UGRD:10 m above ground"
V_WIND = "VGRD:10 m above ground"
TERRAIN = "HGT:surface"
_EARTH_RADIUS_M = 6_371_008.8


class ForecastFileError(ValueError):
    """A forecast file is malformed, or lacks what was asked of it."""


@dataclass(frozen=True, slots=True)
class Reading:
    """One field at one point and valid time.

    ``value`` is None when the field is null at the valid time; ``missing`` then says why:
    ``"null"`` (GRIB missing: for heights, no ceiling/cloud), ``"outside-grid"`` or
    ``"not-in-inventory"``. Only ``"null"`` may be read as "no ceiling".
    """

    value: float | None
    missing: str | None = None
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ForecastRun:
    """A parsed forecast file, with its path and SHA-256 when loaded from disk."""

    doc: Mapping[str, Any]
    path: Path | None = None
    sha256: str | None = None
    _points: dict[str, int] = field(default_factory=dict, repr=False)
    _fields: dict[str, int] = field(default_factory=dict, repr=False)
    _hours: dict[int, int] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        doc = self.doc
        if doc.get("kind") != KIND or doc.get("schema_version") != 1:
            raise ForecastFileError("not a lookout.forecast-run schema_version 1 document")
        try:
            points = [p["id"] for p in doc["points"]]
            names = list(doc["field_names"])
            hours = [int(h) for h in doc["forecast_hours"]]
            values = doc["values"]
        except (KeyError, TypeError) as exc:
            raise ForecastFileError(f"forecast file is missing {exc}") from None
        if len(values) != len(names) or any(len(v) != len(hours) for v in values):
            raise ForecastFileError("values is not [field][hour][point]")
        if any(len(per_hour) != len(points) for per_field in values for per_hour in per_field):
            raise ForecastFileError("values is not [field][hour][point]")
        if [int(h["fh"]) for h in doc["hours"]] != hours:
            raise ForecastFileError("hours[].fh differ from forecast_hours")
        self._points.update({pid: i for i, pid in enumerate(points)})
        self._fields.update({name: i for i, name in enumerate(names)})
        self._hours.update({fh: i for i, fh in enumerate(hours)})

    @classmethod
    def load(cls, path: str | Path) -> ForecastRun:
        """Read a forecast file and hash its bytes."""
        file_path = Path(path).expanduser()
        data = file_path.read_bytes()
        return cls(json.loads(data), file_path, hashlib.sha256(data).hexdigest())

    # -- identity -----------------------------------------------------------------------
    @property
    def viewpoint_id(self) -> str:
        """The viewpoint the file was extracted for."""
        return str(self.doc["viewpoint_id"])

    @property
    def run_id(self) -> str:
        """``YYYYMMDDHH`` of the run."""
        return str(self.doc["run_id"])

    @property
    def run_start(self) -> datetime:
        """Run start (initialisation) time, UTC."""
        return parse_iso_utc(self.doc["run_start_utc"])

    @property
    def run_start_utc(self) -> str:
        """Run start as written in the file (``YYYY-MM-DDTHH:00:00Z``)."""
        return str(self.doc["run_start_utc"])

    @property
    def forecast_hours(self) -> list[int]:
        """Forecast hours present in the file."""
        return sorted(self._hours)

    def point(self, point_id: str) -> Mapping[str, Any]:
        """A sample point by id."""
        try:
            return self.doc["points"][self._points[point_id]]
        except KeyError:
            raise ForecastFileError(f"no sample point {point_id!r}") from None

    def points_of_kind(self, kind: str) -> list[Mapping[str, Any]]:
        """Sample points of one kind (``camera``, ``ray``, ``landmark``, ``neighborhood``)."""
        return [p for p in self.doc["points"] if p["kind"] == kind]

    def landmark_point(self, landmark: Landmark) -> Mapping[str, Any]:
        """The sample point written for a manifest landmark (matched by name)."""
        for point in self.points_of_kind("landmark"):
            if point.get("name") == landmark.name:
                return point
        raise ForecastFileError(f"no sample point for landmark {landmark.name!r}")

    def datum(self, name: str) -> str:
        """The datum the field's values are measured from."""
        try:
            return str(self.doc["fields"][name]["datum"])
        except KeyError:
            raise ForecastFileError(f"field {name!r} is not in this file") from None

    # -- time ----------------------------------------------------------------------------
    def bracket(self, valid_time: datetime) -> tuple[int, int, float]:
        """``(fh_lo, fh_hi, weight_hi)`` around a valid time; both hours must be present."""
        if valid_time.tzinfo is None:
            raise ValueError("valid_time must be timezone-aware")
        offset_h = (valid_time.astimezone(UTC) - self.run_start).total_seconds() / 3600.0
        if offset_h < 0:
            raise ForecastFileError("valid time is before the run start")
        lo, hi = math.floor(offset_h + 1e-9), math.ceil(offset_h - 1e-9)
        for fh in {lo, hi}:
            if fh not in self._hours:
                raise ForecastFileError(f"run {self.run_id} has no forecast hour {fh}")
        weight = 0.0 if hi == lo else offset_h - lo
        return lo, hi, weight

    def hour(self, fh: int) -> Mapping[str, Any]:
        """The ``hours[]`` record of one forecast hour."""
        return self.doc["hours"][self._hours[fh]]

    def availability(self, valid_time: datetime) -> tuple[datetime, str]:
        """When the hours needed for ``valid_time`` were available, and how that is known."""
        lo, hi, _ = self.bracket(valid_time)
        times = [
            (
                parse_iso_utc(self.hour(fh)["availability_utc"]),
                str(self.hour(fh)["availability_source"]),
            )
            for fh in sorted({lo, hi})
        ]
        return max(times, key=lambda item: item[0])

    # -- values --------------------------------------------------------------------------
    def _raw(self, name: str, fh: int, point_id: str) -> tuple[float | None, str | None]:
        if name not in self._fields:
            raise ForecastFileError(f"field {name!r} is not in this file")
        point = self.point(point_id)
        if point.get("i") is None:
            return None, "outside-grid"
        if name in self.hour(fh).get("fields_missing_from_idx", []):
            return None, "not-in-inventory"
        value = self.doc["values"][self._fields[name]][self._hours[fh]][self._points[point_id]]
        return (None, "null") if value is None else (float(value), None)

    def reading(self, name: str, point_id: str, valid_time: datetime) -> Reading:
        """A field at a point and valid time, interpolated linearly between hours."""
        lo, hi, weight = self.bracket(valid_time)
        v_lo, why_lo = self._raw(name, lo, point_id)
        if hi == lo:
            return Reading(v_lo, why_lo)
        v_hi, why_hi = self._raw(name, hi, point_id)
        if v_lo is not None and v_hi is not None:
            return Reading(v_lo + weight * (v_hi - v_lo))
        for why in (why_lo, why_hi):
            if why in ("outside-grid", "not-in-inventory"):
                return Reading(None, why)
        nearer_value, nearer_why = (v_lo, why_lo) if weight < 0.5 else (v_hi, why_hi)
        flag = f"{name}: null at one bracketing hour; nearer hour used"
        return Reading(nearer_value, nearer_why, (flag,))

    def height_msl(self, name: str, point_id: str, valid_time: datetime) -> Reading:
        """A height field in metres above MSL (datum-corrected; PRD §8.3, §13.6 S_rule)."""
        datum = self.datum(name)
        reading = self.reading(name, point_id, valid_time)
        if datum == "m above MSL" or reading.value is None:
            if datum not in ("m above MSL", "m above ground"):
                raise ForecastFileError(f"{name} is not a height (datum {datum!r})")
            return reading
        if datum == "m above ground":
            terrain = self.point(point_id).get("model_terrain_m")
            if terrain is None:
                raise ForecastFileError(f"{name} is above ground but {point_id} has no terrain")
            return Reading(reading.value + float(terrain), None, reading.flags)
        raise ForecastFileError(f"{name} is not a height (datum {datum!r})")

    def earth_relative_wind(self, point_id: str, valid_time: datetime) -> tuple[Reading, Reading]:
        """10 m wind as (eastward, northward), rotated from the grid's axes."""
        u = self.reading(U_WIND, point_id, valid_time)
        v = self.reading(V_WIND, point_id, valid_time)
        if u.value is None or v.value is None:
            return u, v
        grid = self.doc["grid"]
        lat1, lat2 = math.radians(grid["latin1"]), math.radians(grid["latin2"])
        if abs(lat1 - lat2) < 1e-12:
            cone = math.sin(lat1)
        else:
            cone = math.log(math.cos(lat1) / math.cos(lat2)) / math.log(
                math.tan(math.pi / 4 + lat2 / 2) / math.tan(math.pi / 4 + lat1 / 2)
            )
        lon = float(self.point(point_id)["lon"])
        angle = cone * math.radians((lon - float(grid["lov"]) + 180.0) % 360.0 - 180.0)
        east = u.value * math.cos(angle) + v.value * math.sin(angle)
        north = -u.value * math.sin(angle) + v.value * math.cos(angle)
        return Reading(east, None, u.flags), Reading(north, None, v.flags)


def summit_ray(
    run: ForecastRun,
    viewpoint: Viewpoint,
    landmark: Landmark,
    valid_time: datetime,
    *,
    camera_height_m_msl: float | None = None,
    max_bearing_offset_deg: float = 1.0,
) -> tuple[list[RaySample], dict[str, Any]]:
    """S_rule's ceiling samples along the straight camera-to-landmark ray.

    Uses the camera point, the ``ray`` points closer than the landmark (only if the ray was
    sampled within ``max_bearing_offset_deg`` of the landmark's bearing) and the landmark's
    own point. The ray's height includes the chord's sag below a straight line in height
    over a spherical Earth, ``d (D − d) / 2R`` (about 1 m for Rainier). Ceilings that are
    null for a reason other than "no ceiling" raise :class:`ForecastFileError`.
    """
    cam = viewpoint.camera
    h0 = cam.elevation_m if camera_height_m_msl is None else camera_height_m_msl
    h1 = landmark.height_m
    total_km = landmark.distance_km
    bearing = initial_bearing_deg(cam.lat, cam.lon, landmark.lat, landmark.lon)
    chosen: list[tuple[float, str]] = [(0.0, run.points_of_kind("camera")[0]["id"])]
    used_ray = False
    for point in run.points_of_kind("ray"):
        azimuth = point.get("azimuth_deg")
        if azimuth is None or point["distance_km"] >= total_km:
            continue
        if abs((float(azimuth) - bearing + 180.0) % 360.0 - 180.0) <= max_bearing_offset_deg:
            chosen.append((float(point["distance_km"]), point["id"]))
            used_ray = True
    chosen.append((total_km, run.landmark_point(landmark)["id"]))
    samples = []
    flags: list[str] = []
    for distance_km, point_id in chosen:
        reading = run.height_msl(CEILING, point_id, valid_time)
        if reading.missing not in (None, "null"):
            raise ForecastFileError(f"ceiling at {point_id} is unavailable ({reading.missing})")
        flags.extend(reading.flags)
        d_m, total_m = distance_km * 1000.0, total_km * 1000.0
        sag = d_m * (total_m - d_m) / (2.0 * _EARTH_RADIUS_M)
        height = h0 + (h1 - h0) * (distance_km / total_km) - sag
        samples.append(RaySample(distance_km, height, reading.value, point_id))
    detail = {
        "landmark": landmark.name,
        "landmark_bearing_deg": round(bearing, 3),
        "points": [pid for _, pid in chosen],
        "used_view_ray_points": used_ray,
        "camera_height_m_msl": h0,
        "flags": sorted(set(flags)),
    }
    return samples, detail


def cover_pct(run: ForecastRun, name: str, point_id: str, valid_time: datetime) -> float | None:
    """A cloud-cover field (percent) at a point, or None when unavailable."""
    return run.reading(name, point_id, valid_time).value


def ray_points_every(
    run: ForecastRun, step_km: float, max_knots: int
) -> Sequence[Mapping[str, Any]]:
    """The camera point plus ``ray`` points every ``step_km``, at most ``max_knots`` in all."""
    out = [run.points_of_kind("camera")[0]]
    for point in run.points_of_kind("ray"):
        distance = float(point["distance_km"])
        if abs(distance / step_km - round(distance / step_km)) < 1e-6:
            out.append(point)
        if len(out) >= max_knots:
            break
    return out
