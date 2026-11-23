"""Landmark-band, sky and overlay masks (PRD §8.5 step 6; bands of §13.3).

PRD §13.3 scores landmark visibility per band and reads Rainier's obscuration height as the
highest visible band: "summit, upper mountain, mid-slopes, near ridge". Here a band is a range
of **world height** (and, for the near ridge, of distance), read from the DEM view through the
solved pose (:func:`.dem.render_view`):

- each :class:`BandSpec` keeps the terrain pixels whose height lies in ``[min_height_m,
  max_height_m)`` and whose distance lies in ``[min_distance_m, max_distance_m)``; where two
  specs would claim one pixel, the earlier band in spec order keeps it;
- pixels behind a near occluder are removed. The DEM is bare earth, so trees are not in it: the
  occluders are polygons in frame pixels recorded in the build spec (the big tree, the forest
  in front of the mountain), marked by viewing the base frame;
- with an **observed sky** mask (the clear geometry frame's sky, :mod:`.skyline`), a band keeps
  only pixels that frame shows as terrain, so a DEM ridge drawn above the real one never puts
  sky inside a band;
- a band whose terrain meets the sky also takes the sky pixels within ``skyline_margin_px``
  (taxicab distance) of its own terrain, so its silhouette edge, the strongest edge a clear day
  shows, lies inside the band. The margin grows into the observed sky from the observed
  terrain when an observed sky is given, else into the DEM's sky. A margin pixel near two
  bands goes to the nearer band's terrain (the earlier band on a tie), so **no pixel is in two
  bands** and each band's PNG agrees with the label image;
- the camera's caption strip (``overlay_box``) belongs to no band.

The label image ``bands.png`` holds 0 for no band and ``1 + index`` for each band in spec
order; each band is also written as its own PNG (255 inside) for readers that want one mask
per landmark band (extractor v0 reads those).

The sky mask is the clear-frame segmentation of :mod:`.skyline` (255 = sky, the renderer's
format, ``renderer/README.md`` "Inputs").
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageDraw
from scipy import ndimage

from .dem import DemView

__all__ = ["BandSpec", "band_masks", "band_terrain", "label_image", "polygon_mask"]


@dataclass(frozen=True)
class BandSpec:
    """One landmark band: a world-height range and an optional distance range."""

    name: str
    min_height_m: float | None = None
    max_height_m: float | None = None
    min_distance_m: float | None = None
    max_distance_m: float | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("-", "").isalnum():
            raise ValueError(f"band name {self.name!r} must be letters, digits and '-'")
        for lo, hi in (
            (self.min_height_m, self.max_height_m),
            (self.min_distance_m, self.max_distance_m),
        ):
            if lo is not None and hi is not None and not lo < hi:
                raise ValueError(f"band {self.name}: a range needs min < max")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BandSpec:
        """From the build spec."""
        return cls(
            name=str(data["name"]),
            min_height_m=data.get("min_height_m"),
            max_height_m=data.get("max_height_m"),
            min_distance_m=data.get("min_distance_m"),
            max_distance_m=data.get("max_distance_m"),
            description=str(data.get("description", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {
            "name": self.name,
            "min_height_m": self.min_height_m,
            "max_height_m": self.max_height_m,
            "min_distance_m": self.min_distance_m,
            "max_distance_m": self.max_distance_m,
            "description": self.description,
        }

    def select(
        self, height: NDArray[np.float64], distance: NDArray[np.float64]
    ) -> NDArray[np.bool_]:
        """Terrain pixels in this band (NaN height or distance is never in a band)."""
        ok = np.isfinite(height) & np.isfinite(distance)
        h = np.where(ok, height, 0.0)
        d = np.where(ok, distance, 0.0)
        if self.min_height_m is not None:
            ok &= h >= self.min_height_m
        if self.max_height_m is not None:
            ok &= h < self.max_height_m
        if self.min_distance_m is not None:
            ok &= d >= self.min_distance_m
        if self.max_distance_m is not None:
            ok &= d < self.max_distance_m
        return ok


def polygon_mask(
    polygons: Sequence[Sequence[Sequence[float]]], size: tuple[int, int]
) -> NDArray[np.bool_]:
    """Union of polygons (pixel vertices of a ``size`` = (width, height) frame) as a mask."""
    canvas = Image.new("L", size, 0)
    draw = ImageDraw.Draw(canvas)
    for poly in polygons:
        if len(poly) < 3:
            raise ValueError("an occluder polygon needs at least three vertices")
        draw.polygon([(float(x), float(y)) for x, y in poly], fill=255)
    return np.asarray(canvas) > 0


def band_terrain(
    view: DemView,
    bands: Sequence[BandSpec],
    *,
    occluder: NDArray[np.bool_],
    overlay: NDArray[np.bool_],
    observed_sky: NDArray[np.bool_] | None = None,
) -> dict[str, NDArray[np.bool_]]:
    """Each band's terrain pixels, disjoint (earlier band wins), minus occluders and overlay."""
    taken = np.zeros(view.distance_m.shape, dtype=bool)
    out: dict[str, NDArray[np.bool_]] = {}
    for band in bands:
        terrain = band.select(view.height_m, view.distance_m) & ~view.sky & ~occluder & ~overlay
        if observed_sky is not None:
            terrain &= ~observed_sky
        terrain &= ~taken
        taken |= terrain
        out[band.name] = terrain
    return out


def band_masks(
    view: DemView,
    bands: Sequence[BandSpec],
    *,
    occluder: NDArray[np.bool_],
    overlay_box: tuple[int, int, int, int] | None = None,
    skyline_margin_px: int = 6,
    observed_sky: NDArray[np.bool_] | None = None,
) -> dict[str, NDArray[np.bool_]]:
    """One full-resolution mask per band, no two sharing a pixel (module docstring).

    ``view`` must be a full-resolution DEM view (step 1); ``observed_sky``, when given, is the
    clear geometry frame's sky mask.
    """
    shape = view.distance_m.shape
    if occluder.shape != shape:
        raise ValueError(f"occluder {occluder.shape} and DEM view {shape} differ in size")
    if observed_sky is not None and observed_sky.shape != shape:
        raise ValueError(f"observed sky {observed_sky.shape} and DEM view {shape} differ in size")
    if len(view.xs) != shape[1] or view.xs[0] != 0.5:
        raise ValueError("band masks need a full-resolution DEM view (step_px = 1)")
    names = [b.name for b in bands]
    if len(set(names)) != len(names):
        raise ValueError(f"band names repeat: {names}")
    overlay = np.zeros(shape, dtype=bool)
    if overlay_box is not None:
        x0, y0, x1, y1 = overlay_box
        overlay[y0:y1, x0:x1] = True
    terrain = band_terrain(
        view, bands, occluder=occluder, overlay=overlay, observed_sky=observed_sky
    )
    out = {name: mask.copy() for name, mask in terrain.items()}
    if skyline_margin_px <= 0:
        return out
    sky = observed_sky if observed_sky is not None else view.sky
    far = np.iinfo(np.int32).max
    distances = []
    for mask in terrain.values():
        if mask.any():
            distances.append(
                ndimage.distance_transform_cdt(~mask, metric="taxicab").astype(np.int64)
            )
        else:
            distances.append(np.full(shape, far, dtype=np.int64))
    stack = np.stack(distances)
    nearest = np.argmin(stack, axis=0)  # ties go to the earlier band
    reach = np.min(stack, axis=0)
    margin = sky & ~occluder & ~overlay & (reach > 0) & (reach <= skyline_margin_px)
    for k, name in enumerate(terrain):
        out[name] |= margin & (nearest == k)
    return out


def label_image(masks: dict[str, NDArray[np.bool_]]) -> NDArray[np.uint8]:
    """0 for no band, ``1 + index`` in insertion order (the first band would win an overlap,
    though :func:`band_masks` leaves none)."""
    if len(masks) > 254:
        raise ValueError("at most 254 bands fit a label image")
    shape = next(iter(masks.values())).shape
    lab = np.zeros(shape, dtype=np.uint8)
    for k, mask in enumerate(masks.values(), start=1):
        lab[mask & (lab == 0)] = k
    return lab
