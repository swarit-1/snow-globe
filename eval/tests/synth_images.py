"""Procedural test images for the extractor: a textured peak under a sky gradient.

No camera pixels: every image is generated from a seed.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image
from scipy import ndimage

W, H = 640, 360
PEAK_BOX = (250, 50, 390, 200)  # x0, y0, x1, y1 around the summit


def landscape(seed: int, width: int = W, height: int = H) -> np.ndarray:
    """Luma in [0, 1]: sky gradient, a textured triangular peak, darker foreground."""
    rng = np.random.default_rng(seed)
    rows = np.arange(height)[:, None] * (H / height)
    cols = np.arange(width)[None, :] * (W / width)
    sky = 0.70 + 0.15 * rows / H + 0.0 * cols
    texture = ndimage.gaussian_filter(rng.standard_normal((height, width)), 1.2)
    texture = 0.12 * texture / texture.std()
    ridges = 0.08 * np.sin(cols / 7.0 + rows / 11.0)
    peak_x, peak_y, base_y = 320.0, 60.0, 260.0
    half_width = (rows - peak_y) / (base_y - peak_y) * 200.0
    mountain = (rows >= peak_y) & (np.abs(cols - peak_x) <= half_width)
    image = np.where(mountain, 0.45 + texture + ridges, sky)
    ground = rows > base_y
    image = np.where(ground, 0.28 + 0.8 * texture, image)
    return np.clip(image, 0.0, 1.0)


def to_rgb(luma: np.ndarray) -> Image.Image:
    """Grey luma to an RGB image with a slight warm tint."""
    rgb = np.stack([luma * 1.02, luma, luma * 0.97], axis=-1)
    return Image.fromarray(np.clip(rgb * 255.0 + 0.5, 0, 255).astype(np.uint8), "RGB")


def blurred(luma: np.ndarray, sigma: float) -> np.ndarray:
    return ndimage.gaussian_filter(luma, sigma)


def fogged(luma: np.ndarray, alpha: float, veil: float = 0.92) -> np.ndarray:
    """Koschmieder-style airlight: L = L0 * (1 - alpha) + veil * alpha."""
    return luma * (1.0 - alpha) + veil * alpha


def fogged_far(luma: np.ndarray, alpha: float, veil: float = 0.92) -> np.ndarray:
    """A veil over the distant peak and sky only; the foreground below the base stays clear."""
    rows = np.arange(luma.shape[0])[:, None] * (H / luma.shape[0])
    far = np.broadcast_to(rows <= 260.0, luma.shape)
    return np.where(far, fogged(luma, alpha, veil), luma)


def in_cloud(seed: int, level: float = 0.86) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.clip(level + 0.004 * rng.standard_normal((H, W)), 0.0, 1.0)


def encoded(image: Image.Image, fmt: str, quality: int = 90) -> bytes:
    buffer = io.BytesIO()
    if fmt == "JPEG":
        image.save(buffer, format="JPEG", quality=quality)
    else:
        image.save(buffer, format="PNG")
    return buffer.getvalue()
