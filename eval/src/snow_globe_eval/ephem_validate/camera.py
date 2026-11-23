"""The transform into each viewpoint's camera frame, measured (PRD §8.4 "Additions", §9.4).

PRD §8.4 adds "a transform into each viewpoint's camera frame, giving a pixel position and a
light direction". The transform the product uses is the renderer's pinhole camera,
``renderer/src/math/camera.ts`` (``altAzToDirection`` → ENU light direction; ``cameraBasis`` and
``projectDirection`` → pixel). This module measures it three ways:

1. **Against an independent implementation.** :func:`camera_axes` builds the camera's axes as a
   product of elementary rotations, ``Rz(−heading) · Rx(pitch) · Ry(roll)`` applied to a level
   north-looking camera (right = east, forward = north, up = zenith), and :func:`project` uses
   the intrinsic form ``px = W/2 + f·x/z``, ``py = H/2 − f·y/z`` with ``f = (W/2)/tan(hfov/2)``.
   This shares no code with ``camera.ts`` (which builds the basis from cross products); the two
   are run on the same directions through :data:`camera.mjs <_SCRIPT>` under Node 24 (an even
   pixel grid and its antipodes behind the camera, plus every body direction below) and must
   agree on the pixel and on which directions are in front: a direction one side puts behind
   and the other in front is counted (``parity_directions.front_mismatches``), never dropped.
   A round trip ``pixelRay(projectDirection(d))`` checks the renderer against itself.
2. **The ephemeris through it.** For every hourly epoch at which the Sun or Moon is inside a
   viewpoint's frame (by JPL's refracted position), the renderer's pixel for eval's refracted
   position is compared with its pixel for JPL's: the error a viewer would see, in pixels at
   the camera's native size.
3. **The SceneSpec convention.** ``scene.sun`` and ``scene.moon`` carry the *geometric*
   altitude (eval ``scene.py``; the renderer's ``ephemeris/sun.ts`` says the same), and the
   renderer derives both the disk's pixel and the light direction from it. The gap between that
   pixel and where the disk appears (JPL refracted) is measured too: it is the refraction.

**Poses.** Each viewpoint's pose is the one the renderer is given (``scene._pose`` with the
default ``SceneAssumptions``): the manifest's ``pose`` when it has one, else heading = view
azimuth, pitch 0, roll 0 and a 60° horizontal field of view (nominal; the M2 pose solve
replaces it). The frame size is the manifest's ``image_px`` (the camera's native size).
"""

from __future__ import annotations

import json
import math
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from snow_globe_common.manifest import Viewpoint

from .compare import BodyComparison
from .stats import ALT_BANDS, band_label, band_labels, separation_deg, summarize, summarize_by

__all__ = [
    "CameraPose",
    "RendererCameraUnavailableError",
    "camera_axes",
    "camera_validation",
    "enu",
    "grid_directions",
    "pose_for",
    "project",
    "run_renderer_camera",
]

_SCRIPT = Path(__file__).with_name("camera.mjs")
_BAND_ORDER = tuple(band_label(low, high) for low, high in ALT_BANDS)
_DEFAULT_SIZE = (1920, 1080)


class RendererCameraUnavailableError(RuntimeError):
    """Node 24 or the renderer's ``src/math/camera.ts`` is not available."""


@dataclass(frozen=True, slots=True)
class CameraPose:
    """A viewpoint's pose as the renderer gets it, and the frame size."""

    heading_deg: float
    pitch_deg: float
    roll_deg: float
    hfov_deg: float
    width: int
    height: int
    source: str

    def renderer_pose(self) -> dict[str, float | int]:
        """The ``CameraPose`` object ``camera.ts`` takes."""
        return {
            "width": self.width,
            "height": self.height,
            "heading_deg": self.heading_deg,
            "pitch_deg": self.pitch_deg,
            "roll_deg": self.roll_deg,
            "hfov_deg": self.hfov_deg,
        }

    @property
    def focal_px(self) -> float:
        """Focal length in pixels (square pixels): ``(W/2) / tan(hfov/2)``."""
        return self.width / 2.0 / math.tan(math.radians(self.hfov_deg) / 2.0)

    @property
    def px_per_deg(self) -> float:
        """Pixels per degree at the image centre."""
        return self.focal_px * math.pi / 180.0

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {**asdict(self), "px_per_deg_centre": round(self.px_per_deg, 4)}


def pose_for(viewpoint: Viewpoint) -> CameraPose:
    """The pose ``scene.build_scene`` gives the renderer for ``viewpoint``, and its frame size."""
    from ..scene import SceneAssumptions, _pose

    pose, source = _pose(viewpoint, SceneAssumptions())
    width, height = viewpoint.truth_camera.image_px or _DEFAULT_SIZE
    return CameraPose(
        heading_deg=float(pose["heading_deg"]),
        pitch_deg=float(pose["pitch_deg"]),
        roll_deg=float(pose["roll_deg"]),
        hfov_deg=float(pose["hfov_deg"]),
        width=int(width),
        height=int(height),
        source=source,
    )


def _rz(angle: float) -> NDArray[np.float64]:
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def _rx(angle: float) -> NDArray[np.float64]:
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]])


def _ry(angle: float) -> NDArray[np.float64]:
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])


def camera_axes(
    pose: CameraPose,
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    """Right, forward and up in ENU, from ``Rz(−heading) · Rx(pitch) · Ry(roll)``.

    The level north-looking camera has right = x (east), forward = y (north), up = z. ``Ry``
    turns its up toward its right (positive roll: clockwise seen from behind), ``Rx`` tips
    forward up (pitch), ``Rz(−heading)`` turns north clockwise to the heading.
    """
    m = (
        _rz(-math.radians(pose.heading_deg))
        @ _rx(math.radians(pose.pitch_deg))
        @ _ry(math.radians(pose.roll_deg))
    )
    return m[:, 0], m[:, 1], m[:, 2]


def enu(alt_deg: Any, az_deg: Any) -> NDArray[np.float64]:
    """Unit ENU vectors ``(..., 3)`` of altitude/azimuth pairs (azimuth from north, clockwise)."""
    alt = np.radians(np.asarray(alt_deg, dtype=np.float64))
    az = np.radians(np.asarray(az_deg, dtype=np.float64))
    c = np.cos(alt)
    return np.stack([c * np.sin(az), c * np.cos(az), np.sin(alt)], axis=-1)


def project(
    pose: CameraPose, directions: NDArray[np.float64]
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    """Pixel x, pixel y and depth of ENU directions (top-left origin; behind: depth ≤ 0)."""
    right, forward, up = camera_axes(pose)
    d = np.asarray(directions, dtype=np.float64)
    x, y, z = d @ right, d @ up, d @ forward
    f = pose.focal_px
    with np.errstate(divide="ignore", invalid="ignore"):
        px = pose.width / 2.0 + f * x / z
        py = pose.height / 2.0 - f * y / z
    return px, py, z


def in_frame(pose: CameraPose, px: Any, py: Any, depth: Any) -> NDArray[np.bool_]:
    """Directions in front of the camera that land inside the frame."""
    px, py, depth = (np.asarray(v, dtype=np.float64) for v in (px, py, depth))
    return (depth > 0) & (px >= 0) & (px <= pose.width) & (py >= 0) & (py <= pose.height)


def grid_directions(pose: CameraPose, nx: int = 25, ny: int = 15) -> NDArray[np.float64]:
    """Altitude/azimuth pairs ``(n, 2)`` through an even grid of pixels spanning the frame."""
    right, forward, up = camera_axes(pose)
    f = pose.focal_px
    xs = np.linspace(0.5, pose.width - 0.5, nx)
    ys = np.linspace(0.5, pose.height - 0.5, ny)
    gx, gy = np.meshgrid(xs, ys)
    rays = (
        forward[None, :]
        + ((gx.ravel() - pose.width / 2.0) / f)[:, None] * right[None, :]
        + ((pose.height / 2.0 - gy.ravel()) / f)[:, None] * up[None, :]
    )
    rays /= np.linalg.norm(rays, axis=1, keepdims=True)
    alt = np.degrees(np.arcsin(np.clip(rays[:, 2], -1.0, 1.0)))
    az = np.degrees(np.arctan2(rays[:, 0], rays[:, 1])) % 360.0
    return np.stack([alt, az], axis=1)


def run_renderer_camera(
    cases: Sequence[Mapping[str, Any]],
    *,
    renderer_dir: Path | None = None,
    node: str | None = None,
    timeout_s: float = 300.0,
) -> list[dict[str, Any]]:
    """Run ``camera.ts`` on ``cases`` (``pose``, ``alt_az``) under Node; one result per case."""
    from ..scene import (
        NODE_MIN_MAJOR,
        RendererValidatorUnavailableError,
        _node_major,
        find_renderer_dir,
        node_executable,
    )

    executable = node or node_executable()
    if executable is None:
        raise RendererCameraUnavailableError("node is not on the PATH")
    try:
        if _node_major(executable) < NODE_MIN_MAJOR:
            raise RendererCameraUnavailableError(f"Node {NODE_MIN_MAJOR}+ is needed")
    except RendererValidatorUnavailableError as exc:
        raise RendererCameraUnavailableError(str(exc)) from exc
    folder = renderer_dir or find_renderer_dir()
    if not (folder / "src" / "math" / "camera.ts").is_file():
        raise RendererCameraUnavailableError(f"{folder}/src/math/camera.ts is missing")
    with tempfile.TemporaryDirectory(prefix="ephem-camera-") as scratch:
        job = Path(scratch) / "job.json"
        out = Path(scratch) / "out.json"
        job.write_text(json.dumps({"cases": list(cases)}), encoding="utf-8")
        try:
            done = subprocess.run(
                [executable, str(_SCRIPT), str(folder), str(job), str(out)],
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RendererCameraUnavailableError(str(exc)) from exc
        if done.returncode != 0 or not out.is_file():
            raise RuntimeError(f"camera.mjs exited {done.returncode}: {done.stderr[-2000:]}")
        result = json.loads(out.read_text(encoding="utf-8"))["cases"]
    if len(result) != len(cases):
        raise RuntimeError("camera.mjs returned a different number of cases")
    return result


def _px(case: Mapping[str, Any]) -> NDArray[np.float64]:
    return np.array(
        [p if p is not None else (math.nan, math.nan) for p in case["px"]], dtype=np.float64
    ).reshape(-1, 2)


def _back(case: Mapping[str, Any]) -> NDArray[np.float64]:
    return np.array(
        [b if b is not None else (math.nan, math.nan) for b in case["back"]], dtype=np.float64
    ).reshape(-1, 2)


def _angle_between(a: NDArray[np.float64], b: NDArray[np.float64]) -> NDArray[np.float64]:
    """Angles (degrees) between rows of two arrays of vectors, precise at tiny angles."""
    cross = np.linalg.norm(np.cross(a, b), axis=-1)
    dot = np.einsum("ij,ij->i", a, b)
    return np.degrees(np.arctan2(cross, dot))


def _strip(summary: dict[str, Any]) -> dict[str, Any]:
    summary.pop("worst_index", None)
    return summary


def camera_validation(
    poses: Mapping[str, CameraPose],
    comparisons: Sequence[BodyComparison],
    *,
    renderer_dir: Path | None = None,
    node: str | None = None,
) -> dict[str, Any]:
    """Measure the camera-frame transform and the ephemeris through it (module docstring).

    ``comparisons`` are the Horizons comparisons (hourly, per site and body). Pixel numbers
    are at each camera's native size; "in frame" means JPL's refracted position lands inside
    the frame with the reference's geometric altitude at or above −1°. The parity summary is
    over the directions both transforms put in front; ``parity_directions`` counts those in
    front, those behind and the ones where the two disagree.
    """
    cases: list[dict[str, Any]] = []
    plan: list[tuple[str, str, str, int]] = []  # (site, body or "grid", variant, case index)
    selections: dict[tuple[str, str], NDArray[np.intp]] = {}
    for site_id, pose in sorted(poses.items()):
        grid = grid_directions(pose)
        behind = np.stack([-grid[:, 0], (grid[:, 1] + 180.0) % 360.0], axis=1)
        cases.append({"pose": pose.renderer_pose(), "alt_az": np.vstack([grid, behind]).tolist()})
        plan.append((site_id, "grid", "grid", len(cases) - 1))
        for c in comparisons:
            if c.site_id != site_id:
                continue
            px, py, depth = project(pose, enu(c.ref_alt_refr, c.ref_az_refr))
            keep = np.flatnonzero(in_frame(pose, px, py, depth) & c.refraction_domain)
            selections[(site_id, c.body)] = keep
            variants = {
                "ours_geometric": (c.ours_alt[keep], c.ours_az[keep]),
                "ours_refracted": (c.ours_alt_refr[keep], c.ours_az[keep]),
                "ref_refracted": (c.ref_alt_refr[keep], c.ref_az_refr[keep]),
                "ref_geometric": (c.ref_alt[keep], c.ref_az[keep]),
            }
            for variant, (alt, az) in variants.items():
                cases.append(
                    {"pose": pose.renderer_pose(), "alt_az": np.stack([alt, az], axis=1).tolist()}
                )
                plan.append((site_id, c.body, variant, len(cases) - 1))
    out: dict[str, Any] = {
        "poses": {k: v.to_dict() for k, v in sorted(poses.items())},
        "in_frame_rule": "JPL refracted position inside the frame and JPL geometric alt >= -1 deg",
    }
    try:
        results = run_renderer_camera(cases, renderer_dir=renderer_dir, node=node)
    except RendererCameraUnavailableError as exc:
        out["renderer"] = {"ran": False, "why": str(exc)}
        return out
    out["renderer"] = {
        "ran": True,
        "script": _SCRIPT.name,
        "camera_ts": "renderer/src/math/camera.ts",
    }
    parity_px: list[float] = []
    parity_enu: list[float] = []
    round_trip: list[float] = []
    counts = {"in_front": 0, "behind": 0, "front_mismatches": 0}
    sites: dict[str, Any] = {}
    by_key = {(s, b, v): results[i] for s, b, v, i in plan}
    for site_id, _body, _variant, i in plan:
        case, result = cases[i], results[i]
        pose = poses[site_id]
        alt_az = np.asarray(case["alt_az"], dtype=np.float64).reshape(-1, 2)
        if alt_az.size == 0:
            continue
        ours_px, ours_py, depth = project(pose, enu(alt_az[:, 0], alt_az[:, 1]))
        node_px = _px(result)
        front = depth > 0
        node_front = ~np.isnan(node_px[:, 0])
        counts["in_front"] += int(front.sum())
        counts["behind"] += int((~front).sum())
        counts["front_mismatches"] += int(np.sum(front != node_front))
        both = front & node_front
        parity_px.extend(
            np.hypot(node_px[both, 0] - ours_px[both], node_px[both, 1] - ours_py[both]).tolist()
        )
        parity_enu.extend(
            _angle_between(np.asarray(result["enu"]), enu(alt_az[:, 0], alt_az[:, 1])).tolist()
        )
        back = _back(result)
        ok = ~np.isnan(back[:, 0])
        round_trip.extend(
            separation_deg(back[ok, 0], back[ok, 1], alt_az[ok, 0], alt_az[ok, 1]).tolist()
        )
    comparison = {(c.site_id, c.body): c for c in comparisons}
    for (site_id, body), keep in sorted(selections.items()):
        entry: dict[str, Any] = {"n_in_frame": int(keep.size)}
        if keep.size:
            px = {
                v: _px(by_key[(site_id, body, v)])
                for v in ("ours_geometric", "ours_refracted", "ref_refracted", "ref_geometric")
            }
            ephem_px = np.hypot(*(px["ours_refracted"] - px["ref_refracted"]).T)
            scene_px = np.hypot(*(px["ours_geometric"] - px["ref_refracted"]).T)
            light = {
                v: np.asarray(by_key[(site_id, body, v)]["enu"], dtype=np.float64)
                for v in ("ours_geometric", "ref_geometric", "ref_refracted")
            }
            entry.update(
                {
                    "ephemeris_px": _strip(summarize(ephem_px)),
                    "scene_geometric_vs_apparent_px": _strip(summarize(scene_px)),
                    "scene_geometric_vs_apparent_px_by_band": {
                        k: _strip(v)
                        for k, v in summarize_by(
                            scene_px,
                            band_labels(comparison[(site_id, body)].ref_alt[keep]),
                            order=_BAND_ORDER,
                        ).items()
                        if v.get("n")
                    },
                    "light_direction_vs_geometric_deg": _strip(
                        summarize(_angle_between(light["ours_geometric"], light["ref_geometric"]))
                    ),
                    "light_direction_vs_apparent_deg": _strip(
                        summarize(_angle_between(light["ours_geometric"], light["ref_refracted"]))
                    ),
                }
            )
        sites.setdefault(site_id, {})[body] = entry
    out.update(
        {
            "parity_directions": counts,
            "parity_renderer_vs_independent_px": _strip(summarize(np.asarray(parity_px))),
            "parity_light_direction_deg": _strip(summarize(np.asarray(parity_enu))),
            "renderer_round_trip_deg": _strip(summarize(np.asarray(round_trip))),
            "sites": sites,
        }
    )
    return out
