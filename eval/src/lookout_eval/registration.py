"""How far a frame's aim differs from the plate's: a v−1 measurement for PRD §8.6 "Same view".

PRD §8.6 scores a frame only if "registration against the plate is within tolerance, which
catches a moved camera". The tolerance and the registration itself are M2 work (the gate in
:mod:`.gates` reports "not checked"). The backtest pair still needs to know whether its
truth frame and its plate share one aim, because the same landmark box is applied to both,
so :func:`estimate_shift` measures it and the pair records the result as a diagnostic.

**Method.** Both frames are reduced to Rec. 601 luma at a common analysis width, smoothed
(Gaussian sigma 1 px) and turned into Sobel gradient magnitude, so a change of light or
exposure matters less than the edges' positions. Inside a window of the plate (normalised
box; the sky and near objects are best left out), a Hann-tapered phase correlation gives the
translation. For each scale in ``scales`` the plate is first resized about its centre; the
scale and shift with the highest normalised cross-correlation (NCC) of the gradient images
win. NCC with no shift is reported beside it, so a reader sees how much aligning helped.

Positive ``dx`` means the frame's content sits to the right of the plate's; positive ``dy``,
lower. Shifts are integers on the analysis grid, reported in the frame's own pixels and in
normalised units. TODO(M2): sub-pixel registration, rotation, and the tolerance itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
from lookout_common.images import ImageSource, load_image
from numpy.typing import NDArray
from PIL import Image
from scipy import ndimage

__all__ = ["Shift", "estimate_shift"]

Box = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class Shift:
    """The best translation (and scale) of a frame against the plate."""

    dx_px: float
    dy_px: float
    dx_norm: float
    dy_norm: float
    scale: float
    ncc: float
    ncc_unshifted: float
    peak: float
    analysis_width: int
    window: Box
    scales_tried: tuple[float, ...]

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        out = asdict(self)
        out["window"] = list(self.window)
        out["scales_tried"] = list(self.scales_tried)
        out["method"] = (
            "phase correlation of Sobel gradient magnitude (Gaussian sigma 1 px) inside the "
            "window, best NCC over the scales tried; integer shifts on the analysis grid"
        )
        return out


def _gradient(image: Image.Image, size: tuple[int, int]) -> NDArray[np.float64]:
    luma = image.convert("L").resize(size, Image.Resampling.BOX)
    y = np.asarray(luma, dtype=np.float64) / 255.0
    y = ndimage.gaussian_filter(y, 1.0, mode="reflect")
    return np.hypot(
        ndimage.sobel(y, axis=1, mode="reflect"), ndimage.sobel(y, axis=0, mode="reflect")
    )


def _scaled(grad_source: Image.Image, size: tuple[int, int], scale: float) -> NDArray[np.float64]:
    """The plate's gradient image, resized by ``scale`` about its centre, on ``size``."""
    w, h = size
    if scale == 1.0:
        return _gradient(grad_source, size)
    big = _gradient(grad_source, (max(1, round(w * scale)), max(1, round(h * scale))))
    out = np.zeros((h, w))
    bh, bw = big.shape
    oy, ox = (bh - h) // 2, (bw - w) // 2
    src = big[max(oy, 0) : max(oy, 0) + min(h, bh), max(ox, 0) : max(ox, 0) + min(w, bw)]
    ty, tx = max(-oy, 0), max(-ox, 0)
    out[ty : ty + src.shape[0], tx : tx + src.shape[1]] = src
    return out


def _phase_shift(a: NDArray[np.float64], b: NDArray[np.float64]) -> tuple[int, int, float]:
    """Integer (dx, dy) with b(x) ≈ a(x − d), and the correlation peak."""
    taper = np.hanning(a.shape[0])[:, None] * np.hanning(a.shape[1])[None, :]
    fa, fb = np.fft.fft2(a * taper), np.fft.fft2(b * taper)
    cross = fb * np.conj(fa)
    cross /= np.abs(cross) + 1e-12
    surface = np.fft.ifft2(cross).real
    iy, ix = np.unravel_index(int(np.argmax(surface)), surface.shape)
    dy = iy - a.shape[0] if iy > a.shape[0] // 2 else iy
    dx = ix - a.shape[1] if ix > a.shape[1] // 2 else ix
    return int(dx), int(dy), float(surface.max())


def _ncc(a: NDArray[np.float64], b: NDArray[np.float64]) -> float:
    a, b = a.ravel() - a.mean(), b.ravel() - b.mean()
    denominator = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return 0.0 if denominator == 0.0 else float((a * b).sum() / denominator)


def estimate_shift(
    plate: ImageSource,
    frame: ImageSource,
    *,
    window: Box = (0.0, 0.0, 1.0, 1.0),
    analysis_width: int = 480,
    scales: Sequence[float] = (0.98, 0.985, 0.99, 0.995, 1.0, 1.005, 1.01, 1.015, 1.02),
) -> Shift:
    """The translation (and scale) that best maps ``plate`` onto ``frame`` (see module doc).

    Both images must have the same aspect ratio (within 1 %). ``window`` is the part of the
    plate compared, in normalised coordinates.
    """
    x0, y0, x1, y1 = window
    if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
        raise ValueError("window must satisfy 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1")
    plate_image, frame_image = load_image(plate), load_image(frame)
    pw, ph = plate_image.size
    fw, fh = frame_image.size
    if abs(pw / ph - fw / fh) > 0.01 * (fw / fh):
        raise ValueError(f"aspect ratios differ: plate {pw}x{ph}, frame {fw}x{fh}")
    size = (analysis_width, max(1, round(analysis_width * fh / fw)))
    target = _gradient(frame_image, size)
    r0, r1 = round(y0 * size[1]), round(y1 * size[1])
    c0, c1 = round(x0 * size[0]), round(x1 * size[0])
    if r1 - r0 < 16 or c1 - c0 < 16:
        raise ValueError("the window is too small to register")
    best: tuple[float, float, int, int, float] | None = None
    for scale in scales:
        source = _scaled(plate_image, size, float(scale))
        dx, dy, peak = _phase_shift(source[r0:r1, c0:c1], target[r0:r1, c0:c1])
        moved = ndimage.shift(source, (dy, dx), order=0, mode="constant")
        ncc = _ncc(moved[r0:r1, c0:c1], target[r0:r1, c0:c1])
        if best is None or ncc > best[0]:
            best = (ncc, float(scale), dx, dy, peak)
    assert best is not None
    ncc, scale, dx, dy, peak = best
    unshifted = _ncc(_gradient(plate_image, size)[r0:r1, c0:c1], target[r0:r1, c0:c1])
    per_px = fw / size[0]
    return Shift(
        dx_px=round(dx * per_px, 1),
        dy_px=round(dy * per_px, 1),
        dx_norm=round(dx / size[0], 4),
        dy_norm=round(dy / size[1], 4),
        scale=scale,
        ncc=round(ncc, 4),
        ncc_unshifted=round(unshifted, 4),
        peak=round(peak, 4),
        analysis_width=analysis_width,
        window=(x0, y0, x1, y1),
        scales_tried=tuple(float(s) for s in scales),
    )
