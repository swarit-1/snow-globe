"""Cross-check of the ephemeris against the NPS archive's ``SolarElevationAngle`` (PRD §8.4).

Each NPS archive asset (``/airwebcams/api/Search/Execute``) carries a ``SolarElevationAngle``
computed by NPS. This module recomputes it for every asset of a saved search response,
at the viewpoint's manifest position, and compares five models:

- ``geometric``: oculus-core, no refraction;
- ``refracted``: oculus-core + Sæmundsson refraction as used by Lookout (cut off below −1°);
- ``saemundsson_unclipped``: the same formula applied at every altitude, with no cutoff;
- ``noaa_refraction``: oculus-core + the NOAA spreadsheet's refraction model;
- ``noaa_geometric``: the independent NOAA algorithm, no refraction.

It also fits ``NPS − model = b · dh/dt + a`` to separate a timestamp offset (``b``, in
minutes) from a model difference. The verdict says whether NPS's value looks refracted or
geometric, from the daytime residuals (Sun ≥ 0°), where the two differ by 0.03–0.5°.

Times come from :func:`lookout_common.timeutil.parse_nps_archive_time` (the "label"
convention). The check needs no network: it reads a saved response.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
from lookout_common.manifest import Viewpoint
from lookout_common.timeutil import isoformat_z, parse_nps_archive_time

from . import noaa_solar
from .ephemeris import (
    EphemerisBackend,
    get_ephemeris,
    refraction_deg,
    sun_rate_deg_per_min,
)

__all__ = [
    "AssetComparison",
    "CheckReport",
    "ModelSummary",
    "compare_assets",
    "format_report",
    "run_check",
    "saemundsson_unclipped_deg",
]

MODELS = ("geometric", "refracted", "saemundsson_unclipped", "noaa_refraction", "noaa_geometric")


def saemundsson_unclipped_deg(geometric_alt_deg: float) -> float:
    """Sæmundsson refraction (1010 hPa, 10 °C) with no horizon cutoff, in degrees.

    Not physical below the horizon (it changes sign below −5.11°, where the formula has a
    pole); it exists to test whether NPS applies the formula at every altitude.
    """
    h = geometric_alt_deg
    denominator = h + 5.11
    if abs(denominator) < 1e-9:
        return math.nan
    return (1.02 / math.tan(math.radians(h + 10.3 / denominator)) + 0.0019279) / 60.0


@dataclass(frozen=True, slots=True)
class AssetComparison:
    """One archive asset: NPS's value and each model's, in degrees."""

    asset_id: str
    time_utc: str
    time_of_day: str
    nps_deg: float
    geometric: float
    refracted: float
    saemundsson_unclipped: float
    noaa_refraction: float
    noaa_geometric: float
    rate_deg_per_min: float


@dataclass(frozen=True, slots=True)
class ModelSummary:
    """NPS − model over a set of assets."""

    model: str
    n: int
    max_abs_diff_deg: float | None
    mean_diff_deg: float | None
    rms_diff_deg: float | None
    worst_time_utc: str | None
    worst_geometric_alt_deg: float | None


@dataclass(frozen=True)
class CheckReport:
    """The whole cross-check, JSON-ready via :meth:`to_dict`."""

    viewpoint_id: str
    camera_lat: float
    camera_lon: float
    camera_elevation_m: float
    source: str
    ephemeris: str
    n_assets: int
    all_assets: tuple[ModelSummary, ...]
    sun_up: tuple[ModelSummary, ...]
    below_horizon: tuple[ModelSummary, ...]
    oculus_vs_noaa_max_abs_deg: float
    timing_fit: Mapping[str, float]
    verdict: str
    verdict_detail: str
    assets: tuple[AssetComparison, ...]

    def to_dict(self) -> dict[str, Any]:
        """Plain dict for JSON output."""
        return {
            "viewpoint_id": self.viewpoint_id,
            "camera": {
                "lat": self.camera_lat,
                "lon": self.camera_lon,
                "elevation_m": self.camera_elevation_m,
            },
            "source": self.source,
            "ephemeris": self.ephemeris,
            "n_assets": self.n_assets,
            "all_assets": [asdict(s) for s in self.all_assets],
            "sun_up": [asdict(s) for s in self.sun_up],
            "below_horizon": [asdict(s) for s in self.below_horizon],
            "oculus_vs_noaa_max_abs_deg": self.oculus_vs_noaa_max_abs_deg,
            "timing_fit": dict(self.timing_fit),
            "verdict": self.verdict,
            "verdict_detail": self.verdict_detail,
            "assets": [asdict(a) for a in self.assets],
        }


def _assets(search_response: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    results = search_response.get("Results")
    if not isinstance(results, list):
        raise ValueError("not a Search/Execute response: no 'Results' list")
    return [item["Asset"] for item in results if isinstance(item, Mapping) and "Asset" in item]


def compare_assets(
    search_response: Mapping[str, Any],
    viewpoint: Viewpoint,
    *,
    ephemeris: EphemerisBackend | None = None,
) -> list[AssetComparison]:
    """Recompute every asset's solar elevation at the viewpoint's camera position.

    The response is checked before the ephemeris backend is resolved, so a bad input is
    reported as such even where oculus-core is not built.
    """
    assets = _assets(search_response)
    backend = ephemeris or get_ephemeris()
    lat, lon, height = viewpoint.camera.lat, viewpoint.camera.lon, viewpoint.camera.elevation_m
    out: list[AssetComparison] = []
    for asset in assets:
        if asset.get("SolarElevationAngle") is None:
            continue
        moment = parse_nps_archive_time(
            asset["ImageCreateDateTime"], asset["LocalTimeString"], viewpoint.time_zone
        )
        geometric, _ = backend.sun_alt_az(moment, lat, lon, height)
        noaa = noaa_solar.solar_position(moment, lat, lon)
        out.append(
            AssetComparison(
                asset_id=str(asset.get("AssetID", "")),
                time_utc=isoformat_z(moment),
                time_of_day=str(asset.get("TimeOfDay", "")),
                nps_deg=float(asset["SolarElevationAngle"]),
                geometric=geometric,
                refracted=geometric + refraction_deg(geometric),
                saemundsson_unclipped=geometric + saemundsson_unclipped_deg(geometric),
                noaa_refraction=geometric + refraction_deg(geometric, model="noaa"),
                noaa_geometric=noaa.elevation_deg,
                rate_deg_per_min=sun_rate_deg_per_min(moment, lat, lon, height, ephemeris=backend),
            )
        )
    return out


def _summary(model: str, rows: Sequence[AssetComparison]) -> ModelSummary:
    diffs = [row.nps_deg - getattr(row, model) for row in rows]
    pairs = [(d, row) for d, row in zip(diffs, rows, strict=True) if not math.isnan(d)]
    if not pairs:
        return ModelSummary(model, 0, None, None, None, None, None)
    values = np.array([d for d, _ in pairs])
    worst_row = max(pairs, key=lambda item: abs(item[0]))[1]
    return ModelSummary(
        model=model,
        n=len(pairs),
        max_abs_diff_deg=float(np.abs(values).max()),
        mean_diff_deg=float(values.mean()),
        rms_diff_deg=float(math.sqrt(float((values**2).mean()))),
        worst_time_utc=worst_row.time_utc,
        worst_geometric_alt_deg=worst_row.geometric,
    )


def _timing_fit(rows: Sequence[AssetComparison], model: str) -> dict[str, float]:
    """Least-squares ``NPS − model = b · rate + a``; ``b`` in minutes is a clock offset."""
    diffs = np.array([row.nps_deg - getattr(row, model) for row in rows])
    rate = np.array([row.rate_deg_per_min for row in rows])
    keep = ~np.isnan(diffs)
    design = np.vstack([rate[keep], np.ones(int(keep.sum()))]).T
    (slope, intercept), *_ = np.linalg.lstsq(design, diffs[keep], rcond=None)
    residual = diffs[keep] - design @ np.array([slope, intercept])
    return {
        "model": model,  # type: ignore[dict-item]
        "offset_s": float(slope * 60.0),
        "intercept_deg": float(intercept),
        "residual_max_abs_deg": float(np.abs(residual).max()),
    }


def _verdict(sun_up: Sequence[ModelSummary]) -> tuple[str, str]:
    by_model = {s.model: s for s in sun_up}
    geo, refr = by_model["geometric"], by_model["refracted"]
    if geo.rms_diff_deg is None or refr.rms_diff_deg is None:
        return "unknown", "no asset with the Sun above the horizon"
    if refr.rms_diff_deg * 10.0 < geo.rms_diff_deg:
        verdict = "refracted"
    elif geo.rms_diff_deg * 10.0 < refr.rms_diff_deg:
        verdict = "geometric"
    else:
        verdict = "unclear"
    detail = (
        f"Sun >= 0 deg ({geo.n} assets): RMS(NPS - geometric) = {geo.rms_diff_deg:.4f} deg, "
        f"max {geo.max_abs_diff_deg:.4f}; RMS(NPS - refracted) = {refr.rms_diff_deg:.4f} deg, "
        f"max {refr.max_abs_diff_deg:.4f}."
    )
    return verdict, detail


def run_check(
    search_path: str | Path,
    viewpoint: Viewpoint,
    *,
    ephemeris: EphemerisBackend | None = None,
) -> CheckReport:
    """Run the full cross-check on a saved Search/Execute response.

    The file is read and checked first; the ephemeris backend is resolved only for a valid
    response (missing file: ``FileNotFoundError``; not a search response: ``ValueError``).
    """
    path = Path(search_path).expanduser()
    response = json.loads(path.read_text(encoding="utf-8"))
    _assets(response)
    backend = ephemeris or get_ephemeris()
    rows = compare_assets(response, viewpoint, ephemeris=backend)
    if not rows:
        raise ValueError(f"{path} has no assets with a SolarElevationAngle")
    up = [row for row in rows if row.geometric >= 0.0]
    down = [row for row in rows if row.geometric < 0.0]
    verdict, detail = _verdict([_summary(m, up) for m in MODELS])
    unclipped = _summary("saemundsson_unclipped", rows)
    if verdict == "refracted" and unclipped.max_abs_diff_deg is not None:
        detail += (
            " Below the horizon NPS keeps applying the same Saemundsson formula with no "
            f"cutoff: over all {unclipped.n} assets NPS - (geometric + unclipped Saemundsson)"
            f" is at most {unclipped.max_abs_diff_deg:.4f} deg."
        )
    oculus_vs_noaa = max(abs(row.geometric - row.noaa_geometric) for row in rows)
    return CheckReport(
        viewpoint_id=viewpoint.id,
        camera_lat=viewpoint.camera.lat,
        camera_lon=viewpoint.camera.lon,
        camera_elevation_m=viewpoint.camera.elevation_m,
        source=str(path),
        ephemeris=f"{backend.name} {backend.version}",
        n_assets=len(rows),
        all_assets=tuple(_summary(m, rows) for m in MODELS),
        sun_up=tuple(_summary(m, up) for m in MODELS),
        below_horizon=tuple(_summary(m, down) for m in MODELS),
        oculus_vs_noaa_max_abs_deg=float(oculus_vs_noaa),
        timing_fit=_timing_fit(rows, "saemundsson_unclipped"),
        verdict=verdict,
        verdict_detail=detail,
        assets=tuple(rows),
    )


def format_report(report: CheckReport) -> str:
    """A short plain-text table of the cross-check."""
    lines = [
        f"NPS SolarElevationAngle vs {report.ephemeris} at {report.viewpoint_id} "
        f"({report.camera_lat}, {report.camera_lon}, {report.camera_elevation_m:g} m)",
        f"source: {report.source}  assets: {report.n_assets}",
        "",
        f"{'model':<24}{'set':<10}{'n':>4}{'max|d| deg':>12}{'mean d':>10}{'rms d':>10}",
    ]
    for label, summaries in (
        ("all", report.all_assets),
        ("sun>=0", report.sun_up),
        ("sun<0", report.below_horizon),
    ):
        for s in summaries:
            if s.n == 0 or s.max_abs_diff_deg is None:
                continue
            lines.append(
                f"{s.model:<24}{label:<10}{s.n:>4}{s.max_abs_diff_deg:>12.4f}"
                f"{s.mean_diff_deg:>+10.4f}{s.rms_diff_deg:>10.4f}"
            )
    fit = report.timing_fit
    lines += [
        "",
        f"oculus-core vs NOAA (geometric), max |d|: {report.oculus_vs_noaa_max_abs_deg:.4f} deg",
        f"timing fit on NPS - unclipped Saemundsson: offset {fit['offset_s']:+.2f} s, "
        f"intercept {fit['intercept_deg']:+.5f} deg, "
        f"residual max {fit['residual_max_abs_deg']:.5f} deg",
        f"verdict: {report.verdict}. {report.verdict_detail}",
    ]
    return "\n".join(lines)
