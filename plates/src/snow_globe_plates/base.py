"""The plate's base frame (PRD §8.5 step 1).

PRD §8.5: "A bright-overcast frame near midday with every landmark visible ... Several such
frames can be median-combined. The frame is dehazed with the per-viewpoint haze model (§9.3),
so it serves as an albedo proxy."

**Candidates** are listed from the capture sidecars (:func:`candidate_rows`): the Sun's altitude
and azimuth and the minutes from local solar noon (:mod:`.sun`), with capture's own luma and
sharpness. Whether every landmark is visible and whether the light is soft are judged by
viewing the frames; those judgements, and the reason each candidate was taken or left, are
recorded in the build spec, not computed here.

**Median combine** (:func:`median_combine`):

1. every input must share the first input's aim: the integer shift that best aligns their
   gradient images (phase correlation inside ``window``) must be zero, else the build stops;
2. each input is scaled per channel by the gain that matches its median to the first input's
   median over ``gain_mask`` (static terrain: no sky, no caption), which removes the camera's
   auto-exposure steps between frames;
3. the per-pixel, per-channel median of the scaled inputs, rounded to 8 bits.

**Dehazing** needs the clear-air extinction profile of §9.3, fitted on clear development frames
from landmark contrast against distance. It does not exist yet, so the base is *not* dehazed:
TODO(M2) — dehaze with the fitted haze profile before the base is used as an albedo proxy.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import numpy as np
from numpy.typing import NDArray
from scipy import ndimage

from .frames import FrameInput
from .sun import solar_noon_utc, sun_alt_az

__all__ = ["candidate_rows", "integer_shift", "median_combine"]


def candidate_rows(
    frames: Sequence[FrameInput], lat_deg: float, lon_deg: float, *, min_sun_deg: float = 3.0
) -> list[dict[str, Any]]:
    """One row per daytime frame (Sun ≥ ``min_sun_deg``): Sun, noon offset, capture stats."""
    rows = []
    for frame in frames:
        alt, az = sun_alt_az(frame.image_time, lat_deg, lon_deg)
        if alt < min_sun_deg:
            continue
        noon = solar_noon_utc(frame.image_time, lon_deg)
        rows.append(
            {
                "key": frame.key,
                "sha256": frame.recorded_sha256,
                "image_time_utc": frame.image_time.isoformat().replace("+00:00", "Z"),
                "sun_alt_deg": round(alt, 2),
                "sun_az_deg": round(az, 2),
                "minutes_from_solar_noon": round(
                    (frame.image_time - noon).total_seconds() / 60.0, 1
                ),
                "luma_mean": frame.sidecar.get("luma_mean"),
                "sharpness_lapvar": frame.sidecar.get("sharpness_lapvar"),
            }
        )
    return rows


def _gradient(rgb: NDArray[np.float64], width: int) -> NDArray[np.float64]:
    luma = rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114
    factor = rgb.shape[1] / width
    small = ndimage.zoom(luma, 1.0 / factor, order=1)
    small = ndimage.gaussian_filter(small, 1.0)
    return np.hypot(ndimage.sobel(small, axis=1), ndimage.sobel(small, axis=0))


def integer_shift(
    a: NDArray[np.float64],
    b: NDArray[np.float64],
    *,
    window: tuple[float, float, float, float] = (0.2, 0.2, 0.85, 0.55),
    width: int = 960,
) -> tuple[int, int]:
    """Integer (dx, dy) in full-frame pixels that best moves ``a``'s content onto ``b``'s.

    Phase correlation of Hann-tapered gradient images at ``width`` pixels across, inside a
    normalised ``window``; one analysis pixel is ``frame width / width`` frame pixels.
    """
    if a.shape != b.shape:
        raise ValueError(f"frames differ in size: {a.shape} vs {b.shape}")
    ga, gb = _gradient(a, width), _gradient(b, width)
    h, w = ga.shape
    x0, y0, x1, y1 = window
    r0, r1, c0, c1 = round(y0 * h), round(y1 * h), round(x0 * w), round(x1 * w)
    pa, pb = ga[r0:r1, c0:c1], gb[r0:r1, c0:c1]
    taper = np.hanning(pa.shape[0])[:, None] * np.hanning(pa.shape[1])[None, :]
    cross = np.fft.fft2(pb * taper) * np.conj(np.fft.fft2(pa * taper))
    cross /= np.abs(cross) + 1e-12
    surface = np.fft.ifft2(cross).real
    iy, ix = np.unravel_index(int(np.argmax(surface)), surface.shape)
    dy = iy - pa.shape[0] if iy > pa.shape[0] // 2 else iy
    dx = ix - pa.shape[1] if ix > pa.shape[1] // 2 else ix
    scale = a.shape[1] / w
    return round(dx * scale), round(dy * scale)


def median_combine(
    images: Sequence[NDArray[np.uint8]],
    gain_mask: NDArray[np.bool_],
    *,
    names: Sequence[str] | None = None,
) -> tuple[NDArray[np.uint8], list[dict[str, Any]]]:
    """Median of exposure-matched, co-registered RGB frames (see the module docstring).

    Returns the base image and, per input, its shift and per-channel gains.
    """
    if not images:
        raise ValueError("no base frames")
    names = list(names) if names is not None else [str(i) for i in range(len(images))]
    stack = [np.asarray(im, dtype=np.float64) for im in images]
    ref = stack[0]
    if gain_mask.shape != ref.shape[:2] or not gain_mask.any():
        raise ValueError("gain_mask must match the frames and select some pixels")
    ref_med = np.median(ref[gain_mask], axis=0)
    scaled = []
    report = []
    for name, im in zip(names, stack, strict=True):
        shift = integer_shift(ref, im) if im is not ref else (0, 0)
        if shift != (0, 0):
            raise ValueError(
                f"base frame {name} is shifted by {shift} px from the first: the aim moved"
            )
        med = np.median(im[gain_mask], axis=0)
        gains = ref_med / np.maximum(med, 1e-6)
        scaled.append(im * gains)
        report.append(
            {
                "frame": name,
                "shift_px": list(shift),
                "gains_rgb": [round(float(g), 4) for g in gains],
            }
        )
    base = np.median(np.stack(scaled, axis=0), axis=0)
    return np.clip(np.round(base), 0, 255).astype(np.uint8), report


def hours_between(a: datetime, b: datetime) -> float:
    """Signed hours from ``a`` to ``b``."""
    return (b - a).total_seconds() / 3600.0
