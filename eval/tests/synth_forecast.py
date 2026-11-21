"""Synthetic snow-globe-forecast run files (schema version 1) for eval tests.

The layout follows workers/forecast/src/snow_globe_forecast/schema/forecast_run.schema.json:
values are [field][hour][point]. Values come from a callback so each test sets the weather.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from snow_globe_common.manifest import Viewpoint, great_circle_km, initial_bearing_deg

FIELDS: dict[str, str] = {
    "VIS:surface": "n/a",
    "HGT:cloud ceiling": "m above MSL",
    "HGT:cloud base": "m above MSL",
    "HGT:cloud top": "m above MSL",
    "LCDC:low cloud layer": "n/a",
    "MCDC:middle cloud layer": "n/a",
    "HCDC:high cloud layer": "n/a",
    "PRATE:surface": "n/a",
    "UGRD:10 m above ground": "n/a",
    "VGRD:10 m above ground": "n/a",
    "HPBL:surface": "m above ground",
}
_GRID = {
    "type": "lambert_conformal_conic",
    "nx": 1799,
    "ny": 1059,
    "lat1": 21.138123,
    "lon1": 237.280472,
    "lad": 38.5,
    "lov": 262.5,
    "latin1": 38.5,
    "latin2": 38.5,
    "dx": 3000.0,
    "dy": 3000.0,
    "radius_m": 6371229.0,
    "scanning_mode": 64,
    "index_order": "j-major (row j, column i), i east, j north",
}
_R_KM = 6371.0088

Values = Callable[[str, int, dict[str, Any]], float | None]


def _z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _destination(lat: float, lon: float, bearing: float, km: float) -> tuple[float, float]:
    d, t = km / _R_KM, math.radians(bearing)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(t))
    l2 = l1 + math.atan2(
        math.sin(t) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2)
    )
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


def points_for(vp: Viewpoint, terrain_m: float = 1500.0) -> list[dict[str, Any]]:
    cam = vp.camera
    pts = [
        {
            "id": "camera",
            "kind": "camera",
            "lat": cam.lat,
            "lon": cam.lon,
            "i": 100,
            "j": 200,
            "cell_lat": cam.lat,
            "cell_lon": cam.lon,
            "model_terrain_m": terrain_m,
            "distance_km": 0.0,
            "azimuth_deg": None,
            "name": None,
        }
    ]
    for k in range(1, 31):
        km = 1.5 * k
        lat, lon = _destination(cam.lat, cam.lon, cam.view_azimuth_deg % 360.0, km)
        pts.append(
            {
                "id": f"ray:{km:.1f}km",
                "kind": "ray",
                "lat": lat,
                "lon": lon,
                "i": 100 + k,
                "j": 200,
                "cell_lat": lat,
                "cell_lon": lon,
                "model_terrain_m": terrain_m + 50 * k,
                "distance_km": km,
                "azimuth_deg": round(cam.view_azimuth_deg % 360.0, 3),
                "name": None,
            }
        )
    for lm in vp.landmarks:
        slug = re.sub(r"[^a-z0-9]+", "-", lm.name.lower()).strip("-")
        pts.append(
            {
                "id": f"landmark:{slug}",
                "kind": "landmark",
                "lat": lm.lat,
                "lon": lm.lon,
                "i": 140,
                "j": 210,
                "cell_lat": lm.lat,
                "cell_lon": lm.lon,
                "model_terrain_m": 3200.0,
                "distance_km": round(great_circle_km(cam.lat, cam.lon, lm.lat, lm.lon), 4),
                "azimuth_deg": round(initial_bearing_deg(cam.lat, cam.lon, lm.lat, lm.lon), 3),
                "name": lm.name,
            }
        )
    return pts


def make_doc(
    vp: Viewpoint,
    run_start: datetime,
    hours: list[int],
    values: Values,
    *,
    availability: Callable[[int], datetime] | None = None,
    source: str = "modeled",
    missing_from_idx: dict[int, list[str]] | None = None,
) -> dict[str, Any]:
    """A forecast-run document; ``values(field, fh, point)`` gives each value (None = null)."""
    avail = availability or (lambda fh: run_start + timedelta(minutes=52 + 1.866 * fh))
    points = points_for(vp)
    names = list(FIELDS)
    hour_records = []
    for fh in hours:
        url = (
            f"https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.{run_start:%Y%m%d}/conus/"
            f"hrrr.t{run_start:%H}z.wrfsfcf{fh:02d}.grib2"
        )
        available = avail(fh)
        hour_records.append(
            {
                "fh": fh,
                "valid_time_utc": _z(run_start + timedelta(hours=fh)),
                "url": url,
                "idx_url": url + ".idx",
                "grib_size_bytes": 140_000_000,
                "grib_last_modified_utc": _z(available),
                "idx_last_modified_utc": _z(available),
                "availability_utc": _z(available),
                "availability_source": source,
                "availability_model": (
                    None
                    if source != "modeled"
                    else {
                        "method": "default-forecast-md",
                        "lag_s": 3200.0,
                        "samples": 0,
                        "min_samples": 20,
                    }
                ),
                "lag_s": (available - run_start).total_seconds(),
                "fetched_utc": _z(available),
                "byte_ranges": [],
                "requests": [],
                "bytes_downloaded": 0,
                "fields_missing_from_idx": list((missing_from_idx or {}).get(fh, [])),
            }
        )
    fields = {
        name: {
            "shortName": name.split(":")[0].lower(),
            "name": name,
            "typeOfLevel": "test",
            "level": 0,
            "units": "test",
            "datum": datum,
            "datum_evidence": "synthetic test fixture, not HRRR",
            "grib": {
                "discipline": 0,
                "parameterCategory": 0,
                "parameterNumber": 0,
                "typeOfFirstFixedSurface": 1,
            },
            "uvRelativeToGrid": 1 if name.startswith(("UGRD", "VGRD")) else None,
            "description": "synthetic",
        }
        for name, datum in FIELDS.items()
    }
    return {
        "schema_version": 1,
        "kind": "snow-globe.forecast-run",
        "source": "noaa-hrrr-bdp-pds",
        "model": "hrrr-conus",
        "model_version": "v4",
        "model_version_note": "synthetic",
        "product": "wrfsfc",
        "viewpoint_id": vp.id,
        "run_id": f"{run_start:%Y%m%d%H}",
        "run_start_utc": _z(run_start),
        "run_max_fh": 48,
        "run_complete": True,
        "live": False,
        "listed_utc": _z(run_start + timedelta(days=3)),
        "fetch_started_utc": _z(run_start + timedelta(days=3)),
        "fetch_finished_utc": _z(run_start + timedelta(days=3, seconds=20)),
        "software": {"snow_globe_forecast": "test", "eccodes": "test"},
        "grid": _GRID,
        "forecast_hours": list(hours),
        "missing_hours": [],
        "hours": hour_records,
        "points": points,
        "field_names": names,
        "fields": fields,
        "values": [[[values(name, fh, p) for p in points] for fh in hours] for name in names],
        "notes": ["synthetic test fixture"],
    }


def clear_sky(field: str, fh: int, point: dict[str, Any]) -> float | None:
    """Clear: no ceiling or cloud, 50 km visibility, a light westerly."""
    return {
        "VIS:surface": 50_000.0,
        "HGT:cloud ceiling": None,
        "HGT:cloud base": None,
        "HGT:cloud top": None,
        "LCDC:low cloud layer": 0.0,
        "MCDC:middle cloud layer": 0.0,
        "HCDC:high cloud layer": 5.0,
        "PRATE:surface": 0.0,
        "UGRD:10 m above ground": 3.0,
        "VGRD:10 m above ground": 0.0,
        "HPBL:surface": 400.0,
    }[field]


def low_deck(field: str, fh: int, point: dict[str, Any]) -> float | None:
    """A 2,400–3,000 m MSL overcast deck: Rainier's summit hidden."""
    return {
        "VIS:surface": 20_000.0,
        "HGT:cloud ceiling": 2400.0,
        "HGT:cloud base": 2400.0,
        "HGT:cloud top": 3000.0,
        "LCDC:low cloud layer": 95.0,
        "MCDC:middle cloud layer": 10.0,
        "HCDC:high cloud layer": 0.0,
        "PRATE:surface": 0.0002,
        "UGRD:10 m above ground": 0.0,
        "VGRD:10 m above ground": 5.0,
        "HPBL:surface": 600.0,
    }[field]
