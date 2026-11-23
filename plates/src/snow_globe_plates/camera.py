"""The renderer's pinhole camera, in Python (PRD §8.5 step 3, §9.4 "Camera model").

The plate's pose must mean exactly what the renderer means by it, so this module copies the
conventions of ``renderer/src/math/camera.ts``:

- local East-North-Up frame at the camera (x east, y north, z up);
- ``heading_deg``: azimuth of the optical axis, clockwise from true north;
- ``pitch_deg``: elevation of the optical axis, up positive;
- ``roll_deg``: rotation about the optical axis, positive = camera turned clockwise as seen
  from behind it (so the image content appears rotated counter-clockwise);
- ``hfov_deg``: full horizontal field of view; pixels are square, so the vertical half-angle
  tangent is ``tan(hfov / 2) · height / width``;
- pixel coordinates from the top-left corner, x right, y down; pixel (i, j) has its centre at
  (i + 0.5, j + 0.5).

A test projects the renderer's own worked cases. The renderer has no lens distortion (TODO(M2)
in ``renderer/README.md``), so neither does a plate's pose: the solve reports how well a pure
pinhole fits, and what is left over is reported, not hidden.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
from numpy.typing import ArrayLike, NDArray

__all__ = ["Pose", "basis", "pixel_rays", "pixels_per_degree", "project"]


@dataclass(frozen=True, slots=True)
class Pose:
    """A camera orientation and field of view for a frame of ``width`` × ``height`` pixels."""

    heading_deg: float
    pitch_deg: float
    roll_deg: float
    hfov_deg: float
    width: int
    height: int

    def __post_init__(self) -> None:
        if not 0.0 < self.hfov_deg < 179.0:
            raise ValueError("hfov_deg must be in (0, 179)")
        if not -90.0 <= self.pitch_deg <= 90.0:
            raise ValueError("pitch_deg must be in [-90, 90]")
        if self.width < 1 or self.height < 1:
            raise ValueError("width and height must be positive")

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Pose:
        """Inverse of :meth:`to_dict`."""
        return cls(
            float(data["heading_deg"]),
            float(data["pitch_deg"]),
            float(data["roll_deg"]),
            float(data["hfov_deg"]),
            int(data["width"]),
            int(data["height"]),
        )

    def with_angles(self, heading: float, pitch: float, roll: float, hfov: float) -> Pose:
        """The same frame size with other angles."""
        return Pose(heading, pitch, roll, hfov, self.width, self.height)


def basis(pose: Pose) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    """Unit forward, image-right and image-up vectors in ENU (``cameraBasis`` in camera.ts)."""
    h, p, r = (math.radians(v) for v in (pose.heading_deg, pose.pitch_deg, pose.roll_deg))
    forward = np.array([math.sin(h) * math.cos(p), math.cos(h) * math.cos(p), math.sin(p)])
    right0 = np.array([math.cos(h), -math.sin(h), 0.0])
    up0 = np.cross(right0, forward)
    cr, sr = math.cos(r), math.sin(r)
    right = right0 * cr - up0 * sr
    up = up0 * cr + right0 * sr
    return forward, right, up


def _tans(pose: Pose) -> tuple[float, float]:
    th = math.tan(math.radians(pose.hfov_deg) / 2.0)
    return th, th * pose.height / pose.width


def project(directions: ArrayLike, pose: Pose) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Pixel coordinates ``(..., 2)`` of ENU directions, and their depth along the axis.

    A direction with depth ≤ 0 is behind the camera; its pixel is meaningless
    (``projectDirection`` in camera.ts returns null there).
    """
    d = np.asarray(directions, dtype=np.float64)
    forward, right, up = basis(pose)
    z = d @ forward
    with np.errstate(divide="ignore", invalid="ignore"):
        x = (d @ right) / z
        y = (d @ up) / z
    th, tv = _tans(pose)
    px = (x / th + 1.0) * pose.width / 2.0
    py = (1.0 - y / tv) * pose.height / 2.0
    return np.stack([px, py], axis=-1), z


def pixel_rays(px: ArrayLike, py: ArrayLike, pose: Pose) -> NDArray[np.float64]:
    """Unit ENU view rays through pixel coordinates (``pixelRay`` in camera.ts); ``(..., 3)``."""
    x = np.asarray(px, dtype=np.float64)
    y = np.asarray(py, dtype=np.float64)
    forward, right, up = basis(pose)
    th, tv = _tans(pose)
    sx = (2.0 * x / pose.width - 1.0) * th
    sy = (1.0 - 2.0 * y / pose.height) * tv
    d = forward + sx[..., None] * right + sy[..., None] * up
    return d / np.linalg.norm(d, axis=-1, keepdims=True)


def pixels_per_degree(pose: Pose) -> float:
    """Pixels per degree at the image centre (``width / 2 / tan(hfov / 2)`` per radian)."""
    th, _ = _tans(pose)
    return pose.width / 2.0 / th * math.pi / 180.0
