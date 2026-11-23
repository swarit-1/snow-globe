"""Error measures and summaries for the ephemeris validation (PRD §8.4 targets).

- :func:`separation_deg`: the great-circle angle between two (altitude, azimuth) directions,
  by Vincenty's formula, which keeps full precision for the sub-arcsecond angles measured here
  (``arccos`` of a dot product would not).
- :func:`summarize`: n, worst, p99, p95, median, mean and RMS of an error array, with the
  index of the worst. Percentiles use the "higher" method, so p99 is an observed value, never
  an interpolation below one.
- Altitude bands (:data:`ALT_BANDS`) split errors by the reference's geometric altitude; near
  the horizon the refraction dominates, so the bands below 5° are narrow.

Targets (PRD §8.4): Sun ≤ 0.05°, Moon ≤ 0.3° (:data:`TARGET_DEG`).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np
from numpy.typing import ArrayLike, NDArray

__all__ = [
    "ALT_BANDS",
    "TARGET_DEG",
    "band_label",
    "band_labels",
    "hour_angle_dec_deg",
    "separation_deg",
    "summarize",
    "summarize_by",
    "wrap180",
]

TARGET_DEG: Mapping[str, float] = {"sun": 0.05, "moon": 0.3}
#: Geometric-altitude bands of the reference, degrees: [low, high).
ALT_BANDS: tuple[tuple[float, float], ...] = (
    (-90.0, -18.0),
    (-18.0, -6.0),
    (-6.0, -1.0),
    (-1.0, 0.0),
    (0.0, 2.0),
    (2.0, 5.0),
    (5.0, 15.0),
    (15.0, 45.0),
    (45.0, 90.0),
)


def band_label(low: float, high: float) -> str:
    """A band's name, for example ``"-1..0"``."""
    return f"{low:g}..{high:g}"


def band_labels(alt_deg: ArrayLike) -> NDArray[np.str_]:
    """The band name of every altitude (the 90° zenith falls in the top band)."""
    alt = np.asarray(alt_deg, dtype=np.float64)
    out = np.full(alt.shape, "", dtype=object)
    for low, high in ALT_BANDS:
        top = high if high < 90.0 else math.inf
        out[(alt >= low) & (alt < top)] = band_label(low, high)
    return out.astype(str)


def wrap180(deg: ArrayLike) -> NDArray[np.float64]:
    """Angles folded into [-180, 180)."""
    d = np.asarray(deg, dtype=np.float64)
    return (d + 180.0) % 360.0 - 180.0


def separation_deg(
    alt1_deg: ArrayLike, az1_deg: ArrayLike, alt2_deg: ArrayLike, az2_deg: ArrayLike
) -> NDArray[np.float64]:
    """Great-circle angle (degrees) between directions given as altitude and azimuth."""
    p1 = np.radians(np.asarray(alt1_deg, dtype=np.float64))
    p2 = np.radians(np.asarray(alt2_deg, dtype=np.float64))
    dl = np.radians(np.asarray(az2_deg, dtype=np.float64) - np.asarray(az1_deg, dtype=np.float64))
    s1, c1, s2, c2 = np.sin(p1), np.cos(p1), np.sin(p2), np.cos(p2)
    num = np.hypot(c2 * np.sin(dl), c1 * s2 - s1 * c2 * np.cos(dl))
    den = s1 * s2 + c1 * c2 * np.cos(dl)
    return np.degrees(np.arctan2(num, den))


def hour_angle_dec_deg(
    alt_deg: ArrayLike, az_deg: ArrayLike, lat_deg: float
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Local hour angle (west positive) and declination of alt/az directions at a latitude.

    Used to split a difference between two references into an Earth-rotation part (hour
    angle: UT1, polar motion) and the rest (declination).
    """
    alt = np.radians(np.asarray(alt_deg, dtype=np.float64))
    az = np.radians(np.asarray(az_deg, dtype=np.float64))
    lat = math.radians(lat_deg)
    dec = np.arcsin(
        np.clip(math.sin(lat) * np.sin(alt) + math.cos(lat) * np.cos(alt) * np.cos(az), -1, 1)
    )
    ha = np.arctan2(
        -np.sin(az) * np.cos(alt),
        np.sin(alt) * math.cos(lat) - np.cos(alt) * np.cos(az) * math.sin(lat),
    )
    return np.degrees(ha), np.degrees(dec)


def summarize(values: ArrayLike, *, absolute: bool = True) -> dict[str, Any]:
    """n, worst, p99, p95, median, mean and RMS of ``values`` (NaNs dropped).

    With ``absolute`` the order statistics are of ``|values|``; ``mean`` is always the signed
    mean (a bias), and ``worst_index`` indexes the input array.
    """
    v = np.asarray(values, dtype=np.float64)
    keep = np.flatnonzero(~np.isnan(v))
    if keep.size == 0:
        return {"n": 0}
    x = v[keep]
    mag = np.abs(x) if absolute else x
    worst = int(keep[int(np.argmax(mag))])
    return {
        "n": int(x.size),
        "worst": float(mag.max()),
        "p99": float(np.percentile(mag, 99, method="higher")),
        "p95": float(np.percentile(mag, 95, method="higher")),
        "median": float(np.percentile(mag, 50, method="higher")),
        "mean": float(x.mean()),
        "rms": float(math.sqrt(float(np.mean(x * x)))),
        "worst_index": worst,
    }


def summarize_by(
    values: ArrayLike, groups: ArrayLike, *, order: tuple[str, ...] | None = None
) -> dict[str, dict[str, Any]]:
    """:func:`summarize` per group label (in ``order`` when given, else sorted)."""
    v = np.asarray(values, dtype=np.float64)
    g = np.asarray(groups).astype(str)
    labels = order if order is not None else tuple(sorted(set(g.tolist())))
    out: dict[str, dict[str, Any]] = {}
    for label in labels:
        idx = np.flatnonzero(g == label)
        summary = summarize(v[idx])
        if summary["n"]:
            summary["worst_index"] = int(idx[summary["worst_index"]])
        out[label] = summary
    return out
