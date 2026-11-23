"""Camera pose from surveyed landmarks (PRD §8.5 step 3) and its reprojection check (step 7).

**Inputs.** The camera position (latitude, longitude, lens height above sea level) and three or
more landmarks, each with a surveyed position (USGS GNIS coordinates and a USGS 3DEP height)
and the pixel where it was marked by viewing a frame, with that mark's uncertainty in pixels.

**Solve.** Heading, pitch, roll and horizontal field of view minimise the landmarks' pixel
residuals divided by their mark uncertainties (weighted least squares, ``scipy``'s trust-region
solver). Each landmark gives two equations, so three landmarks over-determine the four angles.

**Check (step 7).** For every landmark, the angle between its surveyed direction and the view
ray through its marked pixel; the PRD's bar is 1°. A fit can pass its own residuals by bending
to fit a wrong mark, so each landmark is also predicted by the pose solved *without* it
(leave one out), which needs at least three other landmarks; that angle is reported beside it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray
from scipy import optimize

from .camera import Pose, pixel_rays, pixels_per_degree, project
from .geodesy import az_el, enu

__all__ = [
    "PRD_REPROJECTION_LIMIT_DEG",
    "Landmark",
    "LandmarkFit",
    "PoseSolution",
    "landmark_directions",
    "solve_pose",
]

#: PRD §8.5 step 7: landmark reprojection ≤ 1°.
PRD_REPROJECTION_LIMIT_DEG = 1.0


@dataclass(frozen=True)
class Landmark:
    """A surveyed point and where it was marked in the frame.

    ``px`` is (x, y) in full-size frame pixels; ``sigma_px`` is the marking uncertainty (one
    standard deviation, pixels), set from how sharp the feature is.
    """

    name: str
    lat: float
    lon: float
    height_m: float
    px: tuple[float, float]
    sigma_px: float = 4.0
    band: str | None = None
    sources: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    note: str = ""

    def __post_init__(self) -> None:
        if self.sigma_px <= 0:
            raise ValueError(f"landmark {self.name}: sigma_px must be positive")
        if len(self.px) != 2 or not all(math.isfinite(v) for v in self.px):
            raise ValueError(f"landmark {self.name}: px must be two finite numbers")


def landmark_directions(
    camera: tuple[float, float, float], landmarks: Sequence[Landmark]
) -> NDArray[np.float64]:
    """Unit ENU directions from the camera to each landmark; shape ``(n, 3)``."""
    lat = np.array([lm.lat for lm in landmarks])
    lon = np.array([lm.lon for lm in landmarks])
    h = np.array([lm.height_m for lm in landmarks])
    v = enu(camera, lat, lon, h)
    return v / np.linalg.norm(v, axis=-1, keepdims=True)


@dataclass(frozen=True)
class LandmarkFit:
    """One landmark's residuals under the solved pose (and under the pose solved without it)."""

    name: str
    marked_px: tuple[float, float]
    predicted_px: tuple[float, float]
    residual_px: tuple[float, float]
    angle_deg: float
    azimuth_deg: float
    elevation_deg: float
    range_m: float
    loo_angle_deg: float | None
    loo_predicted_px: tuple[float, float] | None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form (rounded for reading)."""
        return {
            "name": self.name,
            "marked_px": [round(v, 2) for v in self.marked_px],
            "predicted_px": [round(v, 2) for v in self.predicted_px],
            "residual_px": [round(v, 2) for v in self.residual_px],
            "angle_deg": round(self.angle_deg, 4),
            "azimuth_deg": round(self.azimuth_deg, 4),
            "elevation_deg": round(self.elevation_deg, 4),
            "range_m": round(self.range_m, 1),
            "leave_one_out_angle_deg": (
                None if self.loo_angle_deg is None else round(self.loo_angle_deg, 4)
            ),
            "leave_one_out_predicted_px": (
                None
                if self.loo_predicted_px is None
                else [round(v, 2) for v in self.loo_predicted_px]
            ),
        }


@dataclass(frozen=True)
class PoseSolution:
    """The solved pose, with per-landmark residuals and the step-7 check."""

    pose: Pose
    camera: tuple[float, float, float]
    landmarks: tuple[LandmarkFit, ...]
    rms_px: float
    max_angle_deg: float
    max_loo_angle_deg: float | None
    weighted_cost: float
    converged: bool

    @property
    def passes_reprojection(self) -> bool:
        """Every landmark within 1° (PRD §8.5 step 7), in the fit and, if computed, left out."""
        if self.max_angle_deg > PRD_REPROJECTION_LIMIT_DEG:
            return False
        return self.max_loo_angle_deg is None or (
            self.max_loo_angle_deg <= PRD_REPROJECTION_LIMIT_DEG
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {
            "pose": self.pose.to_dict(),
            "camera": {
                "lat": self.camera[0],
                "lon": self.camera[1],
                "elevation_m": round(self.camera[2], 2),
            },
            "pixels_per_degree_at_centre": round(pixels_per_degree(self.pose), 3),
            "rms_px": round(self.rms_px, 3),
            "max_angle_deg": round(self.max_angle_deg, 4),
            "max_leave_one_out_angle_deg": (
                None if self.max_loo_angle_deg is None else round(self.max_loo_angle_deg, 4)
            ),
            "reprojection_limit_deg": PRD_REPROJECTION_LIMIT_DEG,
            "passes_reprojection": self.passes_reprojection,
            "weighted_cost": round(self.weighted_cost, 4),
            "converged": self.converged,
            "landmarks": [lm.to_dict() for lm in self.landmarks],
        }


def _angles(pose: Pose, dirs: NDArray[np.float64], px: NDArray[np.float64]) -> NDArray[np.float64]:
    """The angle, in degrees, between each marked pixel's ray and its landmark's true direction
    (the reprojection check of PRD §8.5 step 7). atan2 of |ray × dir| and ray · dir keeps full
    precision near zero, where arccos of the dot product bottoms out at about 1e-6 degrees in
    float64."""
    rays = pixel_rays(px[:, 0], px[:, 1], pose)
    sin = np.linalg.norm(np.cross(rays, dirs), axis=-1)
    cos = np.sum(rays * dirs, axis=-1)
    return np.degrees(np.arctan2(sin, cos))


def _fit(
    dirs: NDArray[np.float64],
    px: NDArray[np.float64],
    sigma: NDArray[np.float64],
    initial: Pose,
) -> tuple[Pose, float, bool]:
    def residuals(v: NDArray[np.float64]) -> NDArray[np.float64]:
        pose = initial.with_angles(*v)
        uv, z = project(dirs, pose)
        r = (uv - px) / sigma[:, None]
        # A landmark behind the camera gets a large, smooth penalty instead of a wrapped pixel.
        r = np.where((z > 0)[:, None], r, 1e4)
        return r.ravel()

    x0 = np.array(
        [initial.heading_deg, initial.pitch_deg, initial.roll_deg, initial.hfov_deg], float
    )
    sol = optimize.least_squares(
        residuals,
        x0,
        bounds=([-720.0, -89.0, -179.0, 1.0], [720.0, 89.0, 179.0, 178.0]),
        x_scale=[1.0, 1.0, 1.0, 1.0],
        xtol=1e-12,
        ftol=1e-12,
        gtol=1e-12,
    )
    heading = float(sol.x[0]) % 360.0
    pose = initial.with_angles(heading, float(sol.x[1]), float(sol.x[2]), float(sol.x[3]))
    return pose, float(2.0 * sol.cost), bool(sol.success)


def solve_pose(
    camera: tuple[float, float, float],
    landmarks: Sequence[Landmark],
    initial: Pose,
    *,
    leave_one_out: bool = True,
) -> PoseSolution:
    """Solve heading, pitch, roll and FOV from ``landmarks`` (see the module docstring).

    ``initial`` gives the frame size and the starting angles; a start within ~20° of the truth
    converges. Raises ``ValueError`` with fewer than three landmarks (PRD §8.5 step 3).
    """
    if len(landmarks) < 3:
        raise ValueError("the pose needs at least three landmarks (PRD §8.5 step 3)")
    names = [lm.name for lm in landmarks]
    if len(set(names)) != len(names):
        raise ValueError(f"landmark names repeat: {names}")
    dirs = landmark_directions(camera, landmarks)
    px = np.array([lm.px for lm in landmarks], dtype=np.float64)
    sigma = np.array([lm.sigma_px for lm in landmarks], dtype=np.float64)
    pose, cost, ok = _fit(dirs, px, sigma, initial)
    uv, _ = project(dirs, pose)
    angles = _angles(pose, dirs, px)
    az, el, rng = az_el(
        enu(
            camera,
            [lm.lat for lm in landmarks],
            [lm.lon for lm in landmarks],
            [lm.height_m for lm in landmarks],
        )
    )
    loo_angles: list[float | None] = [None] * len(landmarks)
    loo_px: list[tuple[float, float] | None] = [None] * len(landmarks)
    if leave_one_out and len(landmarks) >= 4:
        for i in range(len(landmarks)):
            keep = np.arange(len(landmarks)) != i
            pose_i, _, _ = _fit(dirs[keep], px[keep], sigma[keep], pose)
            uv_i, _ = project(dirs[i : i + 1], pose_i)
            loo_px[i] = (float(uv_i[0, 0]), float(uv_i[0, 1]))
            loo_angles[i] = float(_angles(pose_i, dirs[i : i + 1], px[i : i + 1])[0])
    fits = tuple(
        LandmarkFit(
            name=lm.name,
            marked_px=(float(px[i, 0]), float(px[i, 1])),
            predicted_px=(float(uv[i, 0]), float(uv[i, 1])),
            residual_px=(float(uv[i, 0] - px[i, 0]), float(uv[i, 1] - px[i, 1])),
            angle_deg=float(angles[i]),
            azimuth_deg=float(az[i]),
            elevation_deg=float(el[i]),
            range_m=float(rng[i]),
            loo_angle_deg=loo_angles[i],
            loo_predicted_px=loo_px[i],
        )
        for i, lm in enumerate(landmarks)
    )
    residual = uv - px
    computed_loo = [a for a in loo_angles if a is not None]
    return PoseSolution(
        pose=pose,
        camera=camera,
        landmarks=fits,
        rms_px=float(math.sqrt(float(np.mean(np.sum(residual**2, axis=-1))))),
        max_angle_deg=float(np.max(angles)),
        max_loo_angle_deg=max(computed_loo) if computed_loo else None,
        weighted_cost=cost,
        converged=ok,
    )
