"""Reproducible evidence for the grid mapping and the height datums (PRD §8.3, §16.1).

- :func:`grid_check` downloads one message, compares this package's Lambert conformal
  mapping with the latitude/longitude arrays ecCodes decodes from it (the error is
  reported in km), and can write the small test fixture: the grid-definition keys plus a
  handful of true ``(i, j, lat, lon)`` tuples, never the GRIB file.
- :func:`datum_check` downloads the height fields of one hour and compares them with the
  model terrain over the whole grid. A height above mean sea level can never be below
  the terrain; a depth above ground over high terrain mostly is.
- :func:`lag_survey` lists past runs (no GRIB) and summarises their publication lags:
  the evidence for the lag model's cycle keying and its 120 min fallback (§8.3, §13.9).

The first two delete their GRIB downloads before returning.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
from lookout_common.manifest import Viewpoint
from lookout_common.paths import cache_dir
from lookout_common.timeutil import isoformat_z, utcnow

from .bucket import HrrrBucket
from .decode import ecc_version, iter_handles, message_metadata, values_and_mask
from .discovery import list_run
from .fields import field_by_name
from .grid import GRID_KEYS, HRRR_CONUS, LambertConformalGrid
from .hrrr import format_run_id, object_url
from .idx import group_contiguous, message_ranges, parse_idx

__all__ = [
    "COVER_FIELDS",
    "CROSS_CHECK_FIELDS",
    "DATUM_FIELDS",
    "cloud_base_pressure_check",
    "datum_check",
    "grid_check",
    "haversine_km",
    "implied_height_m",
    "lag_survey",
    "summarise_lags",
]

DATUM_FIELDS: tuple[str, ...] = (
    "HGT:surface",
    "HGT:cloud base",
    "HGT:cloud ceiling",
    "HGT:cloud top",
    "HPBL:surface",
)
#: Extra messages for the cloud-base pressure cross-check, with their GRIB2 identity
#: (discipline, category, number, typeOfFirstFixedSurface).
CROSS_CHECK_FIELDS: dict[str, tuple[int, int, int, int]] = {
    "PRES:cloud base": (0, 3, 0, 2),
    "PRES:surface": (0, 3, 0, 1),
    "TMP:2 m above ground": (0, 0, 0, 103),
    "LCDC:low cloud layer": (0, 6, 3, 214),
    "MCDC:middle cloud layer": (0, 6, 4, 224),
    "HCDC:high cloud layer": (0, 6, 5, 234),
}
COVER_FIELDS: tuple[str, ...] = (
    "LCDC:low cloud layer",
    "MCDC:middle cloud layer",
    "HCDC:high cloud layer",
)
_RD = 287.05  # J kg-1 K-1, dry air
_G = 9.80665
_EARTH_KM = 6371.0088


def haversine_km(
    lat1: npt.ArrayLike, lon1: npt.ArrayLike, lat2: npt.ArrayLike, lon2: npt.ArrayLike
) -> npt.NDArray[np.float64]:
    """Great-circle distance in km (vectorised)."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = p2 - p1
    dl = np.radians((np.asarray(lon2) - np.asarray(lon1) + 180.0) % 360.0 - 180.0)
    h = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * _EARTH_KM * np.arcsin(np.minimum(1.0, np.sqrt(h)))


def _download_fields(
    bucket: HrrrBucket, run_start: datetime, fh: int, names: Sequence[str], work: Path
) -> tuple[Path, dict[str, Any]]:
    listing = list_run(bucket, run_start)
    files = listing.hour(fh)
    if not files.ready or files.grib is None or files.idx is None:
        raise RuntimeError(f"f{fh:02d} of run {listing.run_id} is not on S3")
    entries = parse_idx(bucket.get_text(files.idx.key))
    ranges = message_ranges(entries, names, file_size=files.grib.size)
    path = work / f"{format_run_id(run_start)}_f{fh:02d}.evidence.grib2"
    with path.open("wb") as handle:
        for group in group_contiguous(ranges):
            handle.write(bucket.get_range(files.grib.key, group.start, group.end))
    info = {
        "url": object_url(files.grib.key),
        "byte_ranges": {r.field: [r.start, r.end] for r in ranges},
        "bytes": sum(r.length for r in ranges),
        "idx_lines": {r.field: r.idx_line for r in ranges},
    }
    return path, info


def grid_check(
    bucket: HrrrBucket,
    run_start: datetime,
    *,
    fh: int = 0,
    viewpoints: Mapping[str, Viewpoint] | None = None,
    fixture_path: Path | None = None,
    grid: LambertConformalGrid = HRRR_CONUS,
    work_root: Path | None = None,
) -> dict[str, Any]:
    """Compare the mapping with ecCodes' latitude/longitude arrays for one real message."""
    work = (work_root or cache_dir() / "hrrr") / f"evidence-grid-{format_run_id(run_start)}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        path, info = _download_fields(bucket, run_start, fh, ["HGT:surface"], work)
        import eccodes

        for gid in iter_handles(path):
            meta = message_metadata(gid)
            lats = np.asarray(eccodes.codes_get_array(gid, "latitudes"), dtype=np.float64)
            lons = np.asarray(eccodes.codes_get_array(gid, "longitudes"), dtype=np.float64)
            break
        else:
            raise RuntimeError("the downloaded subset holds no GRIB message")
        md5 = meta["md5GridSection"]
    finally:
        shutil.rmtree(work, ignore_errors=True)
    grid.check_same(meta["grid"])
    my_lat, my_lon = grid.latlon_arrays()
    err_km = haversine_km(lats, lons, my_lat, my_lon)
    fi, fj = grid.fractional_ij_arrays(lats, lons)
    ii, jj = np.meshgrid(np.arange(grid.nx), np.arange(grid.ny))
    ii, jj = ii.ravel(), jj.ravel()
    di, dj = np.abs(fi - ii), np.abs(fj - jj)
    round_trip_ok = int(np.sum((np.rint(fi) == ii) & (np.rint(fj) == jj)))
    worst = int(np.argmax(err_km))
    result: dict[str, Any] = {
        "run": format_run_id(run_start),
        "fh": fh,
        "url": info["url"],
        "bytes_downloaded": info["bytes"],
        "eccodes": ecc_version(),
        "md5GridSection": md5,
        "cells": int(lats.size),
        "forward_max_error_km": float(err_km.max()),
        "forward_mean_error_km": float(err_km.mean()),
        "worst_cell": {"i": int(ii[worst]), "j": int(jj[worst])},
        "inverse_max_abs_di": float(di.max()),
        "inverse_max_abs_dj": float(dj.max()),
        "inverse_round_trip_cells_ok": round_trip_ok,
        "checked_utc": isoformat_z(utcnow()),
    }
    if fixture_path is not None:
        tuples = _fixture_cells(grid, viewpoints or {})
        rows = [
            {
                "i": i,
                "j": j,
                "lat": float(lats[j * grid.nx + i]),
                "lon": float(lons[j * grid.nx + i]),
                "why": why,
            }
            for i, j, why in tuples
        ]
        fixture = {
            "about": (
                "HRRR CONUS grid definition and true (i, j, lat, lon) tuples, decoded by ecCodes "
                "from one real wrfsfc message. Written by `lookout-forecast grid-check "
                "--write-fixture`. Never the GRIB file itself."
            ),
            "source_url": info["url"],
            "byte_range": info["byte_ranges"]["HGT:surface"],
            "eccodes": ecc_version(),
            "md5GridSection": md5,
            "grid_keys": {key: meta["grid"][key] for key in GRID_KEYS},
            "cells": rows,
            "measured": {
                k: result[k]
                for k in (
                    "forward_max_error_km",
                    "inverse_max_abs_di",
                    "inverse_max_abs_dj",
                    "inverse_round_trip_cells_ok",
                    "cells",
                )
            },
        }
        fixture_path.write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
        result["fixture"] = str(fixture_path)
    return result


def _fixture_cells(
    grid: LambertConformalGrid, viewpoints: Mapping[str, Viewpoint]
) -> list[tuple[int, int, str]]:
    cells: list[tuple[int, int, str]] = [
        (0, 0, "first grid point"),
        (grid.nx - 1, 0, "south-east corner"),
        (0, grid.ny - 1, "north-west corner"),
        (grid.nx - 1, grid.ny - 1, "last grid point"),
        (grid.nx // 2, grid.ny // 2, "centre"),
        (1, 1, "next to the first point"),
    ]
    for vp_id in sorted(viewpoints):
        cam = viewpoints[vp_id].camera
        cell = grid.nearest_ij(cam.lat, cam.lon)
        if cell is not None:
            cells.append((cell[0], cell[1], f"camera cell of {vp_id}"))
    rng = np.random.default_rng(20261119)
    for _ in range(6):
        cells.append(
            (int(rng.integers(grid.nx)), int(rng.integers(grid.ny)), "random (seed 20261119)")
        )
    return cells


def _stats(diff: npt.NDArray[np.float64]) -> dict[str, float | None]:
    if diff.size == 0:
        return {"min": None, "p01": None, "p50": None, "p99": None, "max": None}
    q = np.percentile(diff, [0, 1, 50, 99, 100])
    return {
        "min": float(q[0]),
        "p01": float(q[1]),
        "p50": float(q[2]),
        "p99": float(q[3]),
        "max": float(q[4]),
    }


_BANDS_M: tuple[tuple[float, float], ...] = (
    (-1000.0, 500.0),
    (500.0, 1000.0),
    (1000.0, 1500.0),
    (1500.0, 2000.0),
    (2000.0, 2500.0),
    (2500.0, 6000.0),
)


def _by_band(
    values: npt.NDArray[np.float64], terrain: npt.NDArray[np.float64], valid: npt.NDArray[np.bool_]
) -> dict[str, dict[str, float | int | None]]:
    out: dict[str, dict[str, float | int | None]] = {}
    for lo, hi in _BANDS_M:
        band = valid & (terrain >= lo) & (terrain < hi)
        n = int(band.sum())
        below = int(np.sum(values[band] < terrain[band] - 1.0)) if n else 0
        out[f"{lo:.0f}..{hi:.0f} m"] = {
            "valid": n,
            "below": below,
            "fraction": (below / n) if n else None,
        }
    return out


def _below_terrain(
    name: str,
    arrays: Mapping[str, tuple[npt.NDArray[np.float64], npt.NDArray[np.bool_]]],
    terrain: npt.NDArray[np.float64],
    valid: npt.NDArray[np.bool_],
) -> dict[str, Any]:
    """Describe the cells where a height field is below the model terrain."""
    values = arrays[name][0]
    below = valid & (values < terrain - 1.0)
    out: dict[str, Any] = {
        "cells": int(below.sum()),
        "value_m": _stats(values[below]),
        "terrain_m": _stats(terrain[below]),
        "fraction_value_nonnegative": float(np.mean(values[below] >= 0)) if below.any() else None,
    }
    for other in ("HGT:cloud ceiling", "HGT:cloud top"):
        if other == name or other not in arrays:
            continue
        o_values, o_missing = arrays[other]
        both = below & ~o_missing
        normal = valid & ~below & ~o_missing
        out[f"vs {other}"] = {
            "cells_with_both": int(both.sum()),
            "other_minus_value_m": _stats(o_values[both] - values[both]),
            "other_minus_terrain_minus_value_m": _stats(
                o_values[both] - terrain[both] - values[both]
            ),
            "normal_cells_with_both": int(normal.sum()),
            "normal_other_minus_value_m": _stats(o_values[normal] - values[normal]),
        }
    return out


def implied_height_m(
    surface_pressure: npt.NDArray[np.float64],
    pressure: npt.NDArray[np.float64],
    t2m: npt.NDArray[np.float64],
    *,
    lapse_k_per_m: float = 0.0065,
    iterations: int = 4,
) -> npt.NDArray[np.float64]:
    """Height of ``pressure`` above the surface, hypsometric with a standard lapse rate.

    The layer's mean temperature is ``T2m - lapse * z / 2``; solved by fixed-point iteration
    (a few % from the true thickness up to ~11 km, enough to tell 10 m from 9 km).
    """
    log_ratio = np.log(surface_pressure / pressure)
    z = _RD * t2m / _G * log_ratio
    for _ in range(iterations):
        z = _RD * (t2m - lapse_k_per_m * z / 2.0) / _G * log_ratio
    return z


def cloud_base_pressure_check(
    height: tuple[npt.NDArray[np.float64], npt.NDArray[np.bool_]],
    pressure: tuple[npt.NDArray[np.float64], npt.NDArray[np.bool_]],
    terrain: npt.NDArray[np.float64],
    surface_pressure: npt.NDArray[np.float64],
    t2m: npt.NDArray[np.float64],
    *,
    tolerance_m: float = 500.0,
    tolerance_fraction: float = 0.25,
    cover: Mapping[str, npt.NDArray[np.float64]] | None = None,
) -> dict[str, Any]:
    """Compare ``HGT:cloud base`` with the height ``PRES:cloud base`` implies.

    If HGT is above MSL, ``HGT - terrain - implied`` is near zero. A cell is *grossly
    inconsistent* when that gap exceeds ``max(tolerance_m, tolerance_fraction * implied)``,
    a margin far wider than the error of :func:`implied_height_m`.
    """
    h, h_missing = height
    p, p_missing = pressure
    both = ~h_missing & ~p_missing & np.isfinite(h) & np.isfinite(p) & (p > 0)
    implied = np.full(h.shape, np.nan)
    implied[both] = implied_height_m(surface_pressure[both], p[both], t2m[both])
    delta = h - terrain - implied
    below = both & (h < terrain - 1.0)
    normal = both & ~below
    limit = np.maximum(tolerance_m, tolerance_fraction * np.abs(implied))
    bad = both & (np.abs(delta) > limit)
    n = int(both.sum())
    good = both & ~bad
    cover_report: dict[str, Any] = {}
    for name, values in (cover or {}).items():
        cover_report[name] = {
            "inconsistent_cells_mean_pct": float(values[bad].mean()) if bad.any() else None,
            "inconsistent_cells_with_any_pct": float(np.mean(values[bad] > 0))
            if bad.any()
            else None,
            "consistent_cells_mean_pct": float(values[good].mean()) if good.any() else None,
            "consistent_low_bases_mean_pct": (
                float(values[good & (implied < 2000.0)].mean())
                if np.any(good & (implied < 2000.0))
                else None
            ),
        }
    return {
        "cloud_cover_comparison": cover_report,
        "cells_with_height_and_pressure": n,
        "below_terrain_cells": int(below.sum()),
        "below_terrain_but_above_ground_by_pressure": int(
            np.sum(p[below] < surface_pressure[below])
        ),
        "implied_height_above_surface_m_below_terrain_cells": _stats(implied[below]),
        "hgt_minus_terrain_minus_implied_m_normal_cells": _stats(delta[normal]),
        "hgt_minus_terrain_minus_implied_m_below_terrain_cells": _stats(delta[below]),
        "grossly_inconsistent_cells": int(bad.sum()),
        "grossly_inconsistent_fraction": (int(bad.sum()) / n) if n else None,
        "grossly_inconsistent_below_terrain": int(np.sum(bad & below)),
        "grossly_inconsistent_at_or_above_terrain": int(np.sum(bad & normal)),
        "gap_of_inconsistent_cells_m": _stats(delta[bad]),
        "tolerance": f"max({tolerance_m:g} m, {tolerance_fraction:g} * implied height)",
    }


def datum_check(
    bucket: HrrrBucket,
    run_start: datetime,
    *,
    fh: int = 0,
    viewpoints: Mapping[str, Viewpoint] | None = None,
    grid: LambertConformalGrid = HRRR_CONUS,
    work_root: Path | None = None,
    high_terrain_m: float = 2000.0,
) -> dict[str, Any]:
    """Whole-grid comparison of each height field with the model terrain (``HGT:surface``)."""
    work = (work_root or cache_dir() / "hrrr") / f"evidence-datum-{format_run_id(run_start)}"
    work.mkdir(parents=True, exist_ok=True)
    arrays: dict[str, tuple[npt.NDArray[np.float64], npt.NDArray[np.bool_]]] = {}
    metas: dict[str, dict[str, Any]] = {}
    try:
        wanted = [*DATUM_FIELDS, *CROSS_CHECK_FIELDS]
        path, info = _download_fields(bucket, run_start, fh, wanted, work)
        order = sorted(wanted, key=lambda name: info["byte_ranges"][name][0])
        for name, gid in zip(order, iter_handles(path), strict=True):
            meta = message_metadata(gid)
            if name in CROSS_CHECK_FIELDS:
                want = CROSS_CHECK_FIELDS[name]
            else:
                spec = field_by_name(name)
                want = (spec.discipline, spec.category, spec.number, spec.level_type)
            got = (
                meta["discipline"],
                meta["parameterCategory"],
                meta["parameterNumber"],
                meta["typeOfFirstFixedSurface"],
            )
            if got != want:
                raise RuntimeError(f"{name}: unexpected GRIB identity {got}, expected {want}")
            grid.check_same(meta["grid"])
            values, missing = values_and_mask(gid)
            arrays[name] = (values, missing)
            metas[name] = meta
    finally:
        shutil.rmtree(work, ignore_errors=True)
    terrain, _ = arrays["HGT:surface"]
    high = terrain > high_terrain_m
    fields: dict[str, Any] = {}
    for name in DATUM_FIELDS[1:]:
        values, missing = arrays[name]
        valid = ~missing & np.isfinite(values)
        diff = values[valid] - terrain[valid]
        below = int(np.sum(diff < -1.0))
        high_valid = valid & high
        meta = metas[name]
        fields[name] = {
            "grib": {
                "shortName": meta["shortName"],
                "name": meta["name"],
                "units": meta["units"],
                "typeOfLevel": meta["typeOfLevel"],
                "level": meta["level"],
                "discipline": meta["discipline"],
                "parameterCategory": meta["parameterCategory"],
                "parameterNumber": meta["parameterNumber"],
                "typeOfFirstFixedSurface": meta["typeOfFirstFixedSurface"],
                "bitmapPresent": meta["bitmapPresent"],
                "numberOfMissing": meta["numberOfMissing"],
                "packingType": meta["packingType"],
                "bitsPerValue": meta["bitsPerValue"],
                "decimalScaleFactor": meta["decimalScaleFactor"],
                "binaryScaleFactor": meta["binaryScaleFactor"],
            },
            "idx_line": info["idx_lines"][name],
            "valid_cells": int(valid.sum()),
            "missing_cells": int(missing.sum()),
            "cells_below_terrain_by_more_than_1m": below,
            "fraction_below_terrain": (below / int(valid.sum())) if valid.any() else None,
            "value_minus_terrain_m": _stats(diff),
            "value_m": _stats(values[valid]),
            "high_terrain_cells_valid": int(high_valid.sum()),
            "high_terrain_fraction_below_terrain": (
                float(np.mean(values[high_valid] < terrain[high_valid]))
                if high_valid.any()
                else None
            ),
            "high_terrain_median_value_m": (
                float(np.median(values[high_valid])) if high_valid.any() else None
            ),
            "high_terrain_median_terrain_m": (
                float(np.median(terrain[high_valid])) if high_valid.any() else None
            ),
            "below_terrain": _below_terrain(name, arrays, terrain, valid),
            "fraction_below_terrain_by_terrain_band": _by_band(values, terrain, valid),
        }
    fields["HGT:cloud base"]["pressure_cross_check"] = cloud_base_pressure_check(
        arrays["HGT:cloud base"],
        arrays["PRES:cloud base"],
        terrain,
        arrays["PRES:surface"][0],
        arrays["TMP:2 m above ground"][0],
        cover={name: arrays[name][0] for name in COVER_FIELDS},
    )
    cameras: dict[str, Any] = {}
    for vp_id, vp in sorted((viewpoints or {}).items()):
        cell = grid.nearest_ij(vp.camera.lat, vp.camera.lon)
        if cell is None:
            continue
        k = grid.flat_index(*cell)
        cameras[vp_id] = {
            "camera_elevation_m": vp.camera.elevation_m,
            "model_terrain_m": float(terrain[k]),
            "terrain_minus_camera_m": float(terrain[k] - vp.camera.elevation_m),
            **{
                name: (None if arrays[name][1][k] else float(arrays[name][0][k]))
                for name in DATUM_FIELDS[1:]
            },
        }
    terrain_meta = metas["HGT:surface"]
    return {
        "run": format_run_id(run_start),
        "fh": fh,
        "url": info["url"],
        "bytes_downloaded": info["bytes"],
        "eccodes": ecc_version(),
        "terrain": {
            "shortName": terrain_meta["shortName"],
            "units": terrain_meta["units"],
            "min_m": float(terrain.min()),
            "max_m": float(terrain.max()),
            "high_terrain_threshold_m": high_terrain_m,
            "high_terrain_cells": int(high.sum()),
        },
        "fields": fields,
        "camera_cells": cameras,
        "checked_utc": isoformat_z(utcnow()),
        "reading": (
            "A field in m above MSL has ~0 cells below the terrain; a depth above ground has "
            "most high-terrain values below the terrain height."
        ),
    }


def summarise_lags(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Per-hour and per-cycle statistics of publication lags (``run``, ``fh``, ``lag_s`` rows).

    Compares them with the typical forecast.md curve and with the lag model's
    conservative fallback (:data:`.availability.FALLBACK_LAG_MIN`).
    """
    from statistics import median

    from .availability import FALLBACK_LAG_S, p95_nearest_rank, typical_lag_s

    by_fh: dict[int, list[float]] = {}
    by_cycle: dict[int, dict[int, list[float]]] = {}
    for row in rows:
        fh, lag = int(row["fh"]), float(row["lag_s"])
        by_fh.setdefault(fh, []).append(lag)
        by_cycle.setdefault(fh, {}).setdefault(int(str(row["run"])[8:10]), []).append(lag)
    lags = [float(row["lag_s"]) for row in rows]
    per_fh: dict[int, dict[str, Any]] = {}
    for fh in sorted(by_fh):
        values = sorted(by_fh[fh])
        per_fh[fh] = {
            "n": len(values),
            "median_min": round(median(values) / 60, 2),
            "p95_min": round(p95_nearest_rank(values) / 60, 2),
            "max_min": round(values[-1] / 60, 2),
            "typical_min": round(typical_lag_s(fh) / 60, 2),
            "later_than_typical": sum(v > typical_lag_s(fh) for v in values),
            "median_by_cycle_min": {
                cycle: round(median(v) / 60, 2) for cycle, v in sorted(by_cycle[fh].items())
            },
        }
    return {
        "lags": len(lags),
        "runs": len({str(row["run"]) for row in rows}),
        "later_than_typical": sum(
            float(row["lag_s"]) > typical_lag_s(int(row["fh"])) for row in rows
        ),
        "later_than_fallback": sum(lag > FALLBACK_LAG_S for lag in lags),
        "fallback_min": FALLBACK_LAG_S / 60,
        "max_lag_min": round(max(lags) / 60, 2) if lags else None,
        "per_fh": per_fh,
    }


def lag_survey(
    bucket: HrrrBucket,
    start: datetime,
    end: datetime,
    *,
    pause_s: float = 0.2,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Publication lags of past runs from S3 ``LastModified`` (one listing per run, no GRIB).

    For each hourly run from ``start`` to ``end`` (inclusive), every hour whose GRIB2 and
    ``.idx`` are both listed gives lag = the later ``LastModified`` - run start, the
    quantity the live ``lag`` events record. A re-uploaded object only makes its lag
    later. Nothing is written: the lag model reads live ``lag`` events only (PRD §8.3).
    """
    import time

    from .discovery import list_run as _list_run
    from .hrrr import as_run_start

    pause = sleep or time.sleep
    run = as_run_start(start)
    last = as_run_start(end)
    if last < run:
        raise ValueError("end is before start")
    before = bucket.ledger.snapshot()
    rows: list[dict[str, Any]] = []
    incomplete: dict[str, list[int]] = {}
    while run <= last:
        listing = _list_run(bucket, run)
        if listing.missing_hours:
            incomplete[listing.run_id] = listing.missing_hours
        for fh in listing.ready_hours:
            available = listing.hour(fh).availability_utc
            assert available is not None
            rows.append(
                {"run": listing.run_id, "fh": fh, "lag_s": (available - run).total_seconds()}
            )
        run += timedelta(hours=1)
        if run <= last and pause_s > 0:
            pause(pause_s)
    return {
        "first_run": format_run_id(as_run_start(start)),
        "last_run": format_run_id(last),
        "incomplete_runs": incomplete,
        **summarise_lags(rows),
        "transfer": bucket.ledger.since(before),
        "checked_utc": isoformat_z(utcnow()),
    }
