"""Procedural scenes for extractor v0.2's tests: a snow dome's skyline under two kinds of light.

No camera pixels: every image is generated from a seed. The geometry is a 640 × 360 frame with
a dome whose skyline peaks at (320, 120), a summit band hugging the skyline (with a 4 px sky
margin, as the real plate's), an upper band below it, the sky above, and a near field of snow
and dark trees in the lower frame (rows 200-341), with a 6 px caption strip at the bottom.

- ``sunny``: blue-ish sky darker than the lit snow (sky 0.50, dome 0.93, near snow 0.95);
- ``overcast``: the sky lights the snow and is nearly as bright (sky 0.78, dome 0.86, near snow
  0.88), so a perfectly clear skyline is faint;
- ``cloud=True``: everything above row 190 (sky and mountain) replaced by flat cloud at the
  sky's level, with a little noise.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import numpy as np
from PIL import Image
from scipy import ndimage

W, H = 640, 360
VALLEY = 190
LIGHTS = {
    "sunny": {"sky": 0.50, "dome": 0.93, "snow": 0.95, "trees": 0.18},
    "overcast": {"sky": 0.78, "dome": 0.86, "snow": 0.88, "trees": 0.30},
}


def skyline() -> np.ndarray:
    """Row of the skyline per column (inf where there is no mountain)."""
    cols = np.arange(W, dtype=float)
    rows = 120.0 + 0.25 * np.abs(cols - 320.0)
    return np.where((cols >= 160) & (cols <= 480), rows, np.inf)


def masks() -> dict[str, np.ndarray]:
    """Summit, upper and sky masks, the caption overlay and the near-field rows."""
    rows = np.arange(H, dtype=float)[:, None]
    line = skyline()[None, :]
    cols = np.arange(W)[None, :]
    mountain = (rows >= line) & (rows < VALLEY)
    summit = (rows >= line - 4) & (rows <= line + 8) & (cols >= 240) & (cols <= 400)
    upper = mountain & (rows > line + 8) & (rows <= line + 50) & (cols >= 200) & (cols <= 440)
    upper &= ~summit
    sky = (rows < np.minimum(line, VALLEY)) & ~(mountain)
    overlay = np.zeros((H, W), bool)
    overlay[H - 6 :] = True
    return {"summit": summit, "upper": upper, "sky": sky, "overlay": overlay}


def scene(light: Literal["sunny", "overcast"], *, cloud: bool = False, seed: int = 0) -> np.ndarray:
    """Luma in [0, 1] (module docstring)."""
    rng = np.random.default_rng(seed)
    level = LIGHTS[light]
    rows = np.arange(H, dtype=float)[:, None]
    line = skyline()[None, :]
    image = np.full((H, W), level["sky"]) + 0.02 * (rows / H)
    mountain = (rows >= line) & (rows < VALLEY)
    texture = ndimage.gaussian_filter(rng.standard_normal((H, W)), 1.5)
    dome = level["dome"] + 0.02 * texture / texture.std()
    image = np.where(mountain, dome, image)
    ground = np.broadcast_to(rows >= VALLEY, (H, W))
    blobs = ndimage.gaussian_filter(rng.standard_normal((H, W)), 6.0)
    trees = blobs > 0.3 * blobs.std()
    near = np.where(trees, level["trees"], level["snow"])
    image = np.where(ground, near, image)
    if cloud:
        flat = level["sky"] + 0.004 * rng.standard_normal((H, W))
        image = np.where(np.broadcast_to(rows < VALLEY, (H, W)), flat, image)
    image = image + 0.003 * rng.standard_normal((H, W))
    return np.clip(image, 0.0, 1.0)


def to_rgb(luma: np.ndarray, *, blue: float = 0.0) -> Image.Image:
    """Luma to RGB; ``blue`` tints the sky-bright part toward blue."""
    rgb = np.stack([luma * (1.0 - blue), luma, luma * (1.0 + blue)], axis=-1)
    return Image.fromarray(np.clip(rgb * 255.0 + 0.5, 0, 255).astype(np.uint8), "RGB")


def write_plate(
    folder: Path, *, viewpoint: str = "test.view", base: Image.Image | None = None
) -> Path:
    """A plate folder: bands (summit, upper), overlay, sky mask and plate.json with SHA-256s."""
    folder.mkdir(parents=True, exist_ok=True)
    m = masks()
    files: dict[str, dict[str, str]] = {}
    bands = []
    for name in ("summit", "upper"):
        path = folder / f"band_{name}.png"
        Image.fromarray((m[name] * 255).astype(np.uint8)).save(path)
        files[path.name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        bands.append({"name": name, "file": path.name})
    for name, file in (("overlay", "overlay.png"), ("sky", "sky.png")):
        Image.fromarray((m[name] * 255).astype(np.uint8)).save(folder / file)
        files[file] = {"sha256": hashlib.sha256((folder / file).read_bytes()).hexdigest()}
    if base is not None:
        base.save(folder / "base.png")
        files["base.png"] = {
            "sha256": hashlib.sha256((folder / "base.png").read_bytes()).hexdigest()
        }
    doc = {
        "kind": "snow-globe.plate",
        "viewpoint_id": viewpoint,
        "plate_id": f"{viewpoint}/plate-test",
        "frame_size": [W, H],
        "files": files,
        "bands": bands,
    }
    (folder / "plate.json").write_text(json.dumps(doc))
    return folder
