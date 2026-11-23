"""Pose and camera position from stars and landmarks together (PRD §8.5 steps 3 and 7).

:mod:`.pose` solves the four pose angles from landmarks with the camera position held fixed.
Rainier's manifest position is uncertain by ±500 m (it assumes the visitor centre), and with
every landmark 5–8 km away a wrong position is absorbed by a wrong heading and field of view
(:mod:`.stars` explains why). Here the position is solved too:

- **Parameters:** heading, pitch, roll, horizontal FOV, and the camera's east and north offset
  from a starting position. The lens height is the DEM ground under the camera plus a fixed
  height above ground (``agl_m``, recorded; :func:`agl_sensitivity` re-solves at other heights).
- **Residuals:** every matched star (pixel residual over ``star_sigma_px``) and every landmark
  (over its own mark uncertainty). Stars carry the orientation, landmarks the position.

**What is and is not a check.** The landmarks are *in* the fit: the camera's two position
offsets are fitted to them, so their reprojection angles (PRD §8.5 step 7's ≤ 1° bar) are fit
residuals, and with only three landmarks the position has little freedom left to show a wrong
mark. The pose solved from the landmarks alone at the solved position is likewise not
independent of them; it is reported beside the star-only pose as a consistency figure, and its
leave-one-out (which needs four or more landmarks) runs only when the spec gives four. The
independent check of the solved geometry is the DEM skyline against the observed one
(:mod:`.skyline`), which nothing here is fitted to. Every figure depends on the assumed lens
height; :func:`agl_sensitivity` reports how much.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray
from scipy import optimize

from .camera import Pose, pixel_rays, project
from .geodesy import offset_position
from .pose import Landmark, PoseSolution, landmark_directions, solve_pose
from .stars import StarObservation, star_directions

__all__ = [
    "JointSolution",
    "agl_sensitivity",
    "ray_difference_deg",
    "solve_joint",
    "solve_stars_only",
]


def ray_difference_deg(a: Pose, b: Pose, *, nx: int = 25, ny: int = 15) -> tuple[float, float]:
    """Median and largest angle between the two poses' rays over an ``nx`` × ``ny`` pixel grid."""
    gx, gy = np.meshgrid(np.linspace(0, a.width, nx), np.linspace(0, a.height, ny))
    ra = pixel_rays(gx.ravel(), gy.ravel(), a)
    rb = pixel_rays(gx.ravel(), gy.ravel(), b)
    ang = np.degrees(np.arccos(np.clip(np.sum(ra * rb, axis=-1), -1.0, 1.0)))
    return float(np.median(ang)), float(np.max(ang))


def _angles(pose: Pose, dirs: NDArray[np.float64], px: NDArray[np.float64]) -> NDArray[np.float64]:
    rays = pixel_rays(px[:, 0], px[:, 1], pose)
    return np.degrees(np.arccos(np.clip(np.sum(rays * dirs, axis=-1), -1.0, 1.0)))


def solve_stars_only(
    stars: Sequence[StarObservation],
    lat_deg: float,
    lon_deg: float,
    initial: Pose,
    *,
    sigma_px: float = 2.0,
) -> tuple[Pose, NDArray[np.float64]]:
    """Heading, pitch, roll and FOV from stars alone; returns the pose and pixel residuals."""
    if len(stars) < 3:
        raise ValueError("a star pose needs at least three stars")
    dirs = star_directions(stars, lat_deg, lon_deg)
    px = np.array([s.px for s in stars], dtype=np.float64)

    def residuals(v: NDArray[np.float64]) -> NDArray[np.float64]:
        uv, _ = project(dirs, initial.with_angles(*v))
        return ((uv - px) / sigma_px).ravel()

    x0 = [initial.heading_deg, initial.pitch_deg, initial.roll_deg, initial.hfov_deg]
    sol = optimize.least_squares(residuals, x0, xtol=1e-12, ftol=1e-12, gtol=1e-12)
    pose = initial.with_angles(float(sol.x[0]) % 360.0, *(float(v) for v in sol.x[1:]))
    uv, _ = project(dirs, pose)
    return pose, uv - px


@dataclass(frozen=True)
class JointSolution:
    """The joint fit and its checks (see the module docstring)."""

    pose: Pose
    camera: tuple[float, float, float]
    offset_m: tuple[float, float]
    agl_m: float
    star_count: int
    star_residual_px: tuple[float, float]
    star_angle_deg: tuple[float, float]
    landmark_angles_deg: dict[str, float]
    landmark_predicted_px: dict[str, tuple[float, float]]
    landmark_only: PoseSolution
    star_only: Pose
    landmark_vs_star_deg: tuple[float, float]
    joint_vs_star_deg: tuple[float, float]

    @property
    def max_landmark_angle_deg(self) -> float:
        """The largest landmark reprojection angle (PRD §8.5 step 7: ≤ 1°)."""
        return max(self.landmark_angles_deg.values())

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {
            "pose": self.pose.to_dict(),
            "camera": {
                "lat": round(self.camera[0], 7),
                "lon": round(self.camera[1], 7),
                "lens_elevation_m": round(self.camera[2], 2),
                "agl_m": self.agl_m,
                "offset_from_start_m": {
                    "east": round(self.offset_m[0], 1),
                    "north": round(self.offset_m[1], 1),
                },
            },
            "stars": {
                "count": self.star_count,
                "residual_px_median": round(self.star_residual_px[0], 3),
                "residual_px_max": round(self.star_residual_px[1], 3),
                "angle_deg_median": round(self.star_angle_deg[0], 4),
                "angle_deg_max": round(self.star_angle_deg[1], 4),
            },
            "landmarks": {
                name: {
                    "angle_deg": round(angle, 4),
                    "predicted_px": [round(v, 2) for v in self.landmark_predicted_px[name]],
                }
                for name, angle in self.landmark_angles_deg.items()
            },
            "landmark_angles_are": "fit residuals (the camera position is fitted to them)",
            "max_landmark_angle_deg": round(self.max_landmark_angle_deg, 4),
            "landmark_reprojection_passes_1deg": self.max_landmark_angle_deg <= 1.0,
            "landmark_only_at_solved_position": self.landmark_only.to_dict(),
            "star_only_pose": self.star_only.to_dict(),
            "landmark_only_vs_star_only_ray_deg": {
                "median": round(self.landmark_vs_star_deg[0], 4),
                "max": round(self.landmark_vs_star_deg[1], 4),
            },
            "joint_vs_star_only_ray_deg": {
                "median": round(self.joint_vs_star_deg[0], 4),
                "max": round(self.joint_vs_star_deg[1], 4),
            },
        }


def solve_joint(
    start: tuple[float, float],
    ground_height: Callable[[float, float], float],
    landmarks: Sequence[Landmark],
    stars: Sequence[StarObservation],
    initial: Pose,
    *,
    agl_m: float = 10.0,
    star_sigma_px: float = 2.0,
    initial_offset_m: tuple[float, float] = (0.0, 0.0),
) -> JointSolution:
    """Solve the pose and the camera's horizontal position (see the module docstring).

    ``start`` is the (lat, lon) offsets are measured from; ``ground_height(lat, lon)`` gives
    the ground under a candidate position (the DEM). Needs at least three landmarks (PRD §8.5
    step 3) and three stars.
    """
    if len(landmarks) < 3:
        raise ValueError("the pose needs at least three landmarks (PRD §8.5 step 3)")
    if len(stars) < 3:
        raise ValueError("the joint solve needs at least three stars")
    px_l = np.array([lm.px for lm in landmarks], dtype=np.float64)
    sg_l = np.array([lm.sigma_px for lm in landmarks], dtype=np.float64)
    px_s = np.array([s.px for s in stars], dtype=np.float64)

    def camera_at(east_m: float, north_m: float) -> tuple[float, float, float]:
        lat, lon = offset_position(start[0], start[1], east_m, north_m)
        return (lat, lon, float(ground_height(lat, lon)) + agl_m)

    def fit(star_dirs: NDArray[np.float64], x0: list[float]) -> NDArray[np.float64]:
        def residuals(v: NDArray[np.float64]) -> NDArray[np.float64]:
            pose = initial.with_angles(v[0], v[1], v[2], v[3])
            uv_s, _ = project(star_dirs, pose)
            cam = camera_at(v[4] * 100.0, v[5] * 100.0)
            uv_l, _ = project(landmark_directions(cam, landmarks), pose)
            return np.concatenate(
                [((uv_s - px_s) / star_sigma_px).ravel(), ((uv_l - px_l) / sg_l[:, None]).ravel()]
            )

        sol = optimize.least_squares(
            residuals, x0, diff_step=[1e-6] * 4 + [1e-4] * 2, xtol=1e-12, ftol=1e-12, gtol=1e-12
        )
        return np.asarray(sol.x, dtype=np.float64)

    x0 = [
        initial.heading_deg,
        initial.pitch_deg,
        initial.roll_deg,
        initial.hfov_deg,
        initial_offset_m[0] / 100.0,
        initial_offset_m[1] / 100.0,
    ]
    v = fit(star_directions(stars, start[0], start[1]), x0)
    # Star directions depend (very slightly) on where the camera stands: refit once there.
    cam = camera_at(v[4] * 100.0, v[5] * 100.0)
    star_dirs = star_directions(stars, cam[0], cam[1])
    v = fit(star_dirs, list(v))
    cam = camera_at(v[4] * 100.0, v[5] * 100.0)
    pose = initial.with_angles(float(v[0]) % 360.0, float(v[1]), float(v[2]), float(v[3]))
    uv_s, _ = project(star_dirs, pose)
    r_s = np.hypot(*(uv_s - px_s).T)
    a_s = _angles(pose, star_dirs, px_s)
    dirs_l = landmark_directions(cam, landmarks)
    uv_l, _ = project(dirs_l, pose)
    a_l = _angles(pose, dirs_l, px_l)
    landmark_only = solve_pose(cam, landmarks, pose, leave_one_out=True)
    star_only, _ = solve_stars_only(stars, cam[0], cam[1], pose, sigma_px=star_sigma_px)
    return JointSolution(
        pose=pose,
        camera=cam,
        offset_m=(float(v[4] * 100.0), float(v[5] * 100.0)),
        agl_m=agl_m,
        star_count=len(stars),
        star_residual_px=(float(np.median(r_s)), float(np.max(r_s))),
        star_angle_deg=(float(np.median(a_s)), float(np.max(a_s))),
        landmark_angles_deg={lm.name: float(a) for lm, a in zip(landmarks, a_l, strict=True)},
        landmark_predicted_px={
            lm.name: (float(uv_l[i, 0]), float(uv_l[i, 1])) for i, lm in enumerate(landmarks)
        },
        landmark_only=landmark_only,
        star_only=star_only,
        landmark_vs_star_deg=ray_difference_deg(landmark_only.pose, star_only),
        joint_vs_star_deg=ray_difference_deg(pose, star_only),
    )


def offset_between(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float]:
    """East and north metres from ``a`` to ``b`` (small distances)."""
    m_per_deg = math.pi * 6371000.0 / 180.0
    north = (b[0] - a[0]) * m_per_deg
    east = (b[1] - a[1]) * m_per_deg * math.cos(math.radians(a[0]))
    return east, north


def agl_sensitivity(
    start: tuple[float, float],
    ground_height: Callable[[float, float], float],
    landmarks: Sequence[Landmark],
    stars: Sequence[StarObservation],
    initial: Pose,
    heights_m: Sequence[float],
    *,
    star_sigma_px: float = 2.0,
) -> list[dict[str, Any]]:
    """The joint solve repeated at other assumed lens heights above ground, for the manifest."""
    rows = []
    for agl in heights_m:
        sol = solve_joint(
            start,
            ground_height,
            landmarks,
            stars,
            initial,
            agl_m=float(agl),
            star_sigma_px=star_sigma_px,
        )
        rows.append(
            {
                "agl_m": float(agl),
                "offset_from_start_m": {
                    "east": round(sol.offset_m[0], 1),
                    "north": round(sol.offset_m[1], 1),
                },
                "lens_elevation_m": round(sol.camera[2], 2),
                "pose": {
                    k: round(v, 4) for k, v in sol.pose.to_dict().items() if k.endswith("_deg")
                },
                "max_landmark_angle_deg": round(sol.max_landmark_angle_deg, 4),
                "landmark_only_vs_star_only_ray_deg_max": round(sol.landmark_vs_star_deg[1], 4),
                "star_residual_px_median": round(sol.star_residual_px[0], 3),
            }
        )
    return rows
