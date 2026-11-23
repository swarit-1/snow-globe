"""Synthetic test inputs: a GeoTIFF DEM with a cone mountain, frames, sidecars. No camera pixels."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image, TiffImagePlugin

LON0, LAT0, STEP = -121.80, 46.90, 0.0004


def cone_heights(
    shape: tuple[int, int] = (400, 300),
    peak=(46.85, -121.75),
    top=4000.0,
    base=1600.0,
    radius_m=6000.0,
) -> np.ndarray:
    rows, cols = np.mgrid[0 : shape[0], 0 : shape[1]]
    lat = LAT0 - (rows + 0.5) * STEP
    lon = LON0 + (cols + 0.5) * STEP
    dn = (lat - peak[0]) * 111195.0
    de = (lon - peak[1]) * 111195.0 * np.cos(np.radians(peak[0]))
    r = np.hypot(dn, de)
    return np.where(r < radius_m, base + (top - base) * (1 - r / radius_m), base).astype(np.float32)


def write_geotiff(
    path: Path,
    z: np.ndarray,
    *,
    lon0: float = LON0,
    lat0: float = LAT0,
    step: float = STEP,
    epsg: int = 4326,
) -> Path:
    """A float32 GeoTIFF with the pixel-scale, tie-point and GeoKey tags 3DEP's export carries."""
    ifd = TiffImagePlugin.ImageFileDirectory_v2()
    ifd[33550] = (step, step, 0.0)
    ifd.tagtype[33550] = 12
    ifd[33922] = (0.0, 0.0, 0.0, lon0, lat0, 0.0)
    ifd.tagtype[33922] = 12
    ifd[34735] = (1, 1, 0, 3, 1024, 0, 1, 2, 1025, 0, 1, 1, 2048, 0, 1, epsg)
    ifd.tagtype[34735] = 3
    Image.fromarray(z.astype(np.float32), mode="F").save(path, tiffinfo=ifd)
    return path


def write_frame(root: Path, viewpoint: str, stamp: str, rgb: np.ndarray, **sidecar: object) -> str:
    """Write a frame JPEG-named PNG-encoded? No: a real PNG under a .png key, plus its sidecar."""
    day = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
    folder = root / "frames" / viewpoint / day
    folder.mkdir(parents=True, exist_ok=True)
    import hashlib

    path = folder / f"{stamp}_{'0' * 12}.png"
    Image.fromarray(rgb.astype(np.uint8), "RGB").save(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    real = folder / f"{stamp}_{digest[:12]}.png"
    path.rename(real)
    image_time = (
        f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}Z"
    )
    side = {"image_time_utc": image_time, "sha256": digest, "stored_sha256": digest, **sidecar}
    real.with_suffix(".json").write_text(json.dumps(side))
    return f"frames/{viewpoint}/{day}/{real.name}"
