"""The sky in a clear frame, and the skyline check of the pose (PRD §8.5 steps 6 and 7).

**Sky (segmentation).** In a clear-sky frame the sky is the blue region connected to the top
edge: pixels whose blue-red contrast ``(B − R) / (B + R + 1)`` exceeds ``blue_min`` and whose
blue channel exceeds ``blue_floor``, opened once to drop single pixels, the components that
touch the top row kept, and holes filled. Snow in shade is bluish too, which is why only the
region connected to the top counts. Trees standing against the sky are excluded by it, so this
mask is the plate's sky mask (the DEM knows no trees).

**Skyline check, per band** (:func:`band_skyline_check`). For each column the observed skyline
is the first non-sky row. Every column of the frame is given to the band whose terrain forms
the DEM's skyline there (the first terrain row of the full-resolution DEM view through the
solved pose, after occluders), and each band is checked over **all** its columns, so no part of
a band escapes the check. A column whose DEM skyline lies behind an occluder or on terrain in
no band belongs to no band; within a band's columns, those whose observed skyline is a tree top
(dark just below it) or touches the frame's foot are counted and left out, with the reason. The
median, 90th-percentile and largest absolute differences are reported in pixels and degrees;
they are a check, not an input to the fit. A band passes when the 90th percentile is within the
spec's bar; a band with no checkable column is reported as unchecked, not passed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray
from scipy import ndimage

from .camera import Pose, pixels_per_degree
from .dem import DemView

__all__ = ["BandSkyline", "band_skyline_check", "observed_skyline", "sky_segmentation"]


def sky_segmentation(
    rgb: NDArray[np.uint8], *, blue_min: float = 0.08, blue_floor: float = 90.0
) -> NDArray[np.bool_]:
    """Boolean sky mask of a clear-sky RGB frame (see the module docstring)."""
    a = rgb.astype(np.float64)
    r, b = a[..., 0], a[..., 2]
    candidate = ((b - r) / (b + r + 1.0) > blue_min) & (b > blue_floor)
    candidate = ndimage.binary_opening(candidate, iterations=1)
    labels, _ = ndimage.label(candidate)
    top = np.unique(labels[0][labels[0] > 0])
    sky = np.isin(labels, top)
    return np.asarray(ndimage.binary_fill_holes(sky), dtype=bool)


def observed_skyline(sky: NDArray[np.bool_]) -> NDArray[np.float64]:
    """Per column, the first row that is not sky (the frame height if the column is all sky)."""
    not_sky = ~sky
    first = np.argmax(not_sky, axis=0).astype(np.float64)
    first[~not_sky.any(axis=0)] = float(sky.shape[0])
    return first


@dataclass(frozen=True)
class BandSkyline:
    """One band's DEM skyline against the observed skyline, over every column it owns."""

    band: str
    columns_owned: int
    columns_checked: int
    columns_skipped: dict[str, int]
    x_range: tuple[int, int] | None
    abs_px: tuple[float, float, float] | None  # median, 90th percentile, max
    bias_px: float | None
    px_per_deg: float
    bar_p90_deg: float
    worst_columns: tuple[tuple[int, float], ...]

    @property
    def passes(self) -> bool | None:
        """p90 within the bar; None when no column could be checked."""
        if self.abs_px is None:
            return None
        return self.abs_px[1] / self.px_per_deg <= self.bar_p90_deg

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form, in pixels and degrees."""
        out: dict[str, Any] = {
            "columns_owned": self.columns_owned,
            "columns_checked": self.columns_checked,
            "columns_skipped": dict(self.columns_skipped),
            "x_range": None if self.x_range is None else list(self.x_range),
            "bar_p90_abs_deg": self.bar_p90_deg,
            "passes": self.passes,
        }
        if self.abs_px is not None:
            med, p90, worst = self.abs_px
            out.update(
                median_abs_px=round(med, 2),
                p90_abs_px=round(p90, 2),
                max_abs_px=round(worst, 2),
                median_abs_deg=round(med / self.px_per_deg, 3),
                p90_abs_deg=round(p90 / self.px_per_deg, 3),
                max_abs_deg=round(worst / self.px_per_deg, 3),
                bias_px_dem_minus_observed=round(float(self.bias_px or 0.0), 2),
                worst_columns=[[x, round(d, 1)] for x, d in self.worst_columns],
            )
        return out


def band_skyline_check(
    view: DemView,
    terrain: dict[str, NDArray[np.bool_]],
    sky: NDArray[np.bool_],
    luma: NDArray[np.float64],
    pose: Pose,
    *,
    bar_p90_deg: float,
    bright_min: float = 0.45,
) -> dict[str, BandSkyline]:
    """Per-band skyline check over every column (see the module docstring).

    ``view`` is the full-resolution DEM view, ``terrain`` each band's DEM terrain after the
    occluders (:func:`.masks.band_terrain` without the observed sky), ``sky`` the observed sky
    of the clear geometry frame and ``luma`` that frame's luma in [0, 1].
    """
    height, width = sky.shape
    dem_sky = view.sky
    has_terrain = (~dem_sky).any(axis=0)
    dem_row = np.argmax(~dem_sky, axis=0)
    observed = observed_skyline(sky)
    ppd = pixels_per_degree(pose)
    owner = np.full(width, -1, dtype=np.int64)
    names = list(terrain)
    for k, name in enumerate(names):
        hit = has_terrain & terrain[name][dem_row, np.arange(width)]
        owner[hit & (owner < 0)] = k
    out: dict[str, BandSkyline] = {}
    for k, name in enumerate(names):
        cols = np.nonzero(owner == k)[0]
        skipped: dict[str, int] = {"tree_top_or_dark": 0, "frame_edge": 0}
        diffs: list[tuple[int, float]] = []
        for x in cols:
            y = int(observed[x])
            if y + 6 >= height:
                skipped["frame_edge"] += 1
                continue
            if float(luma[y + 2 : y + 6, x].mean()) <= bright_min:
                skipped["tree_top_or_dark"] += 1
                continue
            diffs.append((int(x), float(dem_row[x]) - float(observed[x])))
        if diffs:
            d = np.array([v for _, v in diffs])
            a = np.abs(d)
            stats: tuple[float, float, float] | None = (
                float(np.median(a)),
                float(np.percentile(a, 90)),
                float(a.max()),
            )
            bias: float | None = float(np.median(d))
            worst = tuple(sorted(diffs, key=lambda item: -abs(item[1]))[:5])
        else:
            stats, bias, worst = None, None, ()
        out[name] = BandSkyline(
            band=name,
            columns_owned=len(cols),
            columns_checked=len(diffs),
            columns_skipped=skipped,
            x_range=None if len(cols) == 0 else (int(cols.min()), int(cols.max())),
            abs_px=stats,
            bias_px=bias,
            px_per_deg=ppd,
            bar_p90_deg=bar_p90_deg,
            worst_columns=worst,
        )
    return out
