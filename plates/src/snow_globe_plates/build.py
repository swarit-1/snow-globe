"""Build a viewpoint's plate set from its spec (PRD §8.5; Appendix A "plate").

``build(spec)`` runs PRD §8.5's steps that v0 covers and writes the plate set to
``<data dir>/plates/<viewpoint_id>/<plate_version>/``, never inside the repo:

1. **Base frame** (:mod:`.base`): the spec's frames, checked against the committed split
   (:mod:`.frames`: development frames a fit may use) and their recorded SHA-256,
   median-combined. Not dehazed: TODO(M2).
3. **Pose** (:mod:`.joint`): heading, pitch, roll and field of view, with the camera position,
   from the spec's GNIS landmarks (marked by viewing) and its matched stars. The stars are read
   from the match file that ``snow-globe-plates match-stars`` wrote to the data dir
   (:mod:`.starmatch`), checked against the SHA-256 the spec records; the spec itself holds no
   catalogue positions.
6. **Masks** (:mod:`.masks`, :mod:`.skyline`): the sky from the clear geometry frame; the
   landmark bands from the 3DEP DEM through the solved pose, minus the spec's occluders,
   clipped to the terrain the geometry frame shows, with a sky margin grown from the observed
   skyline and given to one band only; and the caption strip.
7. **Pose check, part 1**: each landmark's reprojection angle (≤ 1°; a fit residual, since the
   camera position is fitted to the landmarks), the landmark-only and star-only poses compared
   over the frame, the lens-height sensitivity, and the independent check: the DEM skyline
   against the observed one **per band over every column the band owns**
   (:func:`.skyline.band_skyline_check`). A band whose skyline misses the spec's bar is written
   but marked ``valid: false`` with the reason, and readers (extractor v0) report it.

Steps 2 (night frame, lights mask), 4–5 (depth, world height, normals, horizon maps), 8
(sunset-shadow timing) and 9 (camera response) are not built: TODO(M2). The plate manifest
``plate.json`` names every input file with its SHA-256, the spec's SHA-256, the solved pose
and every check, and lists what is missing.
"""

from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PIL import Image

from . import __version__
from .base import candidate_rows, median_combine
from .camera import Pose
from .dem import Dem, render_view
from .frames import FrameInput, frame_input, sha256_file
from .joint import JointSolution, agl_sensitivity, solve_joint
from .masks import BandSpec, band_masks, band_terrain, label_image, polygon_mask
from .pose import Landmark
from .skyline import band_skyline_check, sky_segmentation
from .starmatch import observations_from_record
from .stars import StarObservation

__all__ = ["PLATE_KIND", "SPEC_KIND", "build", "load_spec", "plate_dir"]

SPEC_KIND = "snow-globe.plate-spec"
PLATE_KIND = "snow-globe.plate"
_TODO = [
    "TODO(M2): dehaze the base with the per-viewpoint clear-air haze profile (PRD §9.3); "
    "the base is not an albedo proxy until then",
    "TODO(M2): night frame and lights mask (PRD §8.5 step 2)",
    "TODO(M2): depth and world height from a DEM ray cast plus a near-field model, normals and "
    "DEM horizon maps (PRD §8.5 steps 4-5); this plate's DEM view is used only for the bands",
    "TODO(M2): pose check part 2, the summit's sunset-shadow timing (PRD §8.5 step 8)",
    "TODO(M2): camera response (PRD §8.5 step 9, §9.4)",
    "TODO(M2): sky segmentation beyond one clear frame's blue sky (PRD §8.5 step 6)",
    "TODO(M2): a camera model the stars fit: about half the stars matched in the last pass sit "
    "4-9 px from the pinhole pose (see pose.star_match), and per-frame aims, radial and "
    "tangential distortion and a principal-point offset do not remove it",
]


def load_spec(path: str | Path) -> tuple[dict[str, Any], str]:
    """The build spec and its SHA-256; refuses another kind or version."""
    data = Path(path).read_bytes()
    spec = json.loads(data)
    if spec.get("kind") != SPEC_KIND or spec.get("spec_version") != 2:
        raise ValueError(f"{path}: not a {SPEC_KIND} version 2 file")
    for field in ("viewpoint_id", "plate_version", "frame_size", "time_zone", "landmarks"):
        if field not in spec:
            raise ValueError(f"{path}: missing {field!r}")
    if len(spec["landmarks"]) < 3:
        raise ValueError(f"{path}: the pose needs at least three landmarks (PRD §8.5 step 3)")
    stars = spec.get("stars", {})
    if "observations" in stars:
        raise ValueError(
            f"{path}: star positions belong in the data dir's match file, not in the spec"
        )
    for field in ("file", "sha256"):
        if field not in stars:
            raise ValueError(f"{path}: stars.{field} is missing (run match-stars first)")
    return spec, hashlib.sha256(data).hexdigest()


def plate_dir(root: Path, spec: dict[str, Any]) -> Path:
    """``<data dir>/plates/<viewpoint_id>/<plate_version>``."""
    return root / "plates" / spec["viewpoint_id"] / spec["plate_version"]


def _refuse_repo(path: Path) -> None:
    """Plates are data: refuse an output folder inside a git checkout (CLAUDE.md "Data")."""
    resolved = path.resolve()
    for parent in (resolved, *resolved.parents):
        if (parent / ".git").exists():
            raise ValueError(
                f"{path} is inside the git checkout {parent}; plates live in the data dir"
            )


def _png_bytes(array: NDArray[np.uint8]) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(array).save(buffer, format="PNG", optimize=False, compress_level=6)
    return buffer.getvalue()


def _load_rgb(frame: FrameInput) -> NDArray[np.uint8]:
    with Image.open(frame.path) as im:
        return np.asarray(im.convert("RGB"), dtype=np.uint8)


def _checked(root: Path, key: str) -> FrameInput:
    frame = frame_input(root, key)
    recorded = frame.recorded_sha256
    actual = sha256_file(frame.path)
    if recorded is not None and recorded != actual:
        raise ValueError(f"{key}: file SHA-256 {actual} differs from its sidecar's {recorded}")
    return frame


def _landmarks(spec: dict[str, Any], dem: Dem) -> tuple[list[Landmark], list[dict[str, Any]]]:
    out, record = [], []
    for item in spec["landmarks"]:
        radius = float(item.get("height_from_dem_radius_m", 40.0))
        height, lat, lon = dem.max_near(float(item["lat"]), float(item["lon"]), radius)
        out.append(
            Landmark(
                name=item["name"],
                lat=lat,
                lon=lon,
                height_m=height,
                px=(float(item["px"][0]), float(item["px"][1])),
                sigma_px=float(item["sigma_px"]),
                band=item.get("band"),
                sources=tuple(item.get("sources", ())),
            )
        )
        record.append(
            {
                "name": item["name"],
                "gnis_id": item.get("gnis_id"),
                "gnis_lat_lon": [item["lat"], item["lon"]],
                "dem_highest_cell": {
                    "lat": round(lat, 7),
                    "lon": round(lon, 7),
                    "height_m": round(height, 2),
                },
                "radius_m": radius,
                "px": item["px"],
                "sigma_px": item["sigma_px"],
                "band": item.get("band"),
                "identified_by": item.get("identified_by", ""),
                "marked_on": item.get("marked_on"),
                "sources": item.get("sources", []),
            }
        )
    return out, record


def _stars(
    spec: dict[str, Any], root: Path
) -> tuple[list[StarObservation], list[FrameInput], dict[str, Any]]:
    """The kept stars of the spec's match file, its frames checked, and the file's summary."""
    path = root / spec["stars"]["file"]
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != spec["stars"]["sha256"]:
        raise ValueError(f"{path}: SHA-256 {digest} is not the spec's {spec['stars']['sha256']}")
    record = json.loads(data)
    frames = []
    for item in record["frames"]:
        frame = frame_input(root, item["key"])
        if sha256_file(frame.path) != item["sha256"]:
            raise ValueError(f"{item['key']}: SHA-256 differs from the match file's")
        frames.append(frame)
    stars = observations_from_record(record)
    keys = {f.key for f in frames}
    if any(s.frame_key not in keys for s in stars):
        raise ValueError(f"{path}: a kept star names a frame the file does not list")
    info = {
        "file": spec["stars"]["file"],
        "sha256": digest,
        "catalogue": record["catalogue"],
        "settings": record["settings"],
        "summary": record["summary"],
        "rejected_after_last_pass": [
            {k: r[k] for k in ("frame_key", "catalogue_id", "v_mag", "peak_sigma", "residual_px")}
            for r in record["rejected"]
        ],
    }
    return stars, frames, info


def build(
    spec_path: str | Path,
    *,
    data_root: Path,
    out_dir: Path | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """Build the plate set (see the module docstring); returns the plate manifest."""
    spec, spec_sha = load_spec(spec_path)
    width, height = (int(v) for v in spec["frame_size"])
    target = out_dir if out_dir is not None else plate_dir(data_root, spec)
    _refuse_repo(target)
    if write and (target / "plate.json").exists():
        raise ValueError(f"{target} already holds a plate; plates are never overwritten")
    dem_path = data_root / spec["dem"]["path"]
    dem = Dem.from_geotiff(dem_path)
    if dem.sha256 != spec["dem"]["sha256"]:
        raise ValueError(
            f"{dem_path}: SHA-256 {dem.sha256} is not the spec's {spec['dem']['sha256']}"
        )
    landmarks, landmark_record = _landmarks(spec, dem)
    stars, star_frames, star_info = _stars(spec, data_root)
    start = (float(spec["camera"]["start"]["lat"]), float(spec["camera"]["start"]["lon"]))
    initial = Pose(
        float(spec["initial_pose"]["heading_deg"]),
        float(spec["initial_pose"]["pitch_deg"]),
        float(spec["initial_pose"]["roll_deg"]),
        float(spec["initial_pose"]["hfov_deg"]),
        width,
        height,
    )

    def ground(lat: float, lon: float) -> float:
        return float(dem.sample([lat], [lon])[0])

    agl = float(spec["camera"]["agl_m"])
    solution: JointSolution = solve_joint(start, ground, landmarks, stars, initial, agl_m=agl)
    sensitivity = agl_sensitivity(
        start,
        ground,
        landmarks,
        stars,
        solution.pose,
        [float(v) for v in spec["camera"].get("agl_sensitivity_m", ())],
    )
    view = render_view(dem, solution.camera, solution.pose, step_px=1)

    geometry = _checked(data_root, spec["geometry_frame"])
    geometry_rgb = _load_rgb(geometry)
    if geometry_rgb.shape[:2] != (height, width):
        raise ValueError(
            f"{geometry.key} is {geometry_rgb.shape[1]}x{geometry_rgb.shape[0]}, "
            f"not {width}x{height}"
        )
    sky = sky_segmentation(geometry_rgb)
    luma = (geometry_rgb.astype(np.float64) @ np.array([0.299, 0.587, 0.114])) / 255.0

    x0, y0, x1, y1 = (int(v) for v in spec["overlay_box"])
    overlay = np.zeros((height, width), dtype=bool)
    overlay[y0:y1, x0:x1] = True
    occluder = polygon_mask([o["polygon"] for o in spec["occluders"]], (width, height))
    bands = [BandSpec.from_dict(b) for b in spec["bands"]]
    bar_deg = float(spec["band_registration_bar"]["p90_abs_deg"])
    terrain = band_terrain(view, bands, occluder=occluder, overlay=overlay)
    registration = band_skyline_check(view, terrain, sky, luma, solution.pose, bar_p90_deg=bar_deg)
    margin_px = int(spec.get("skyline_margin_px", 6))
    masks = band_masks(
        view,
        bands,
        occluder=occluder,
        overlay_box=(x0, y0, x1, y1),
        skyline_margin_px=margin_px,
        observed_sky=sky,
    )
    labels = label_image(masks)

    base_frames = [_checked(data_root, key) for key in spec["base"]["frames"]]
    images = [_load_rgb(f) for f in base_frames]
    for frame, image in zip(base_frames, images, strict=True):
        if image.shape[:2] != (height, width):
            raise ValueError(f"{frame.key} is not {width}x{height}")
    base, base_report = median_combine(images, ~sky & ~overlay, names=[f.key for f in base_frames])

    cand_frames = [_checked(data_root, c["key"]) for c in spec["base"]["candidates"]]
    rows = {
        r["key"]: r
        for r in candidate_rows(
            cand_frames, solution.camera[0], solution.camera[1], min_sun_deg=0.0
        )
    }
    candidates = [
        {**rows.get(c["key"], {"key": c["key"]}), "verdict": c["verdict"], "reason": c["reason"]}
        for c in spec["base"]["candidates"]
    ]

    files: dict[str, bytes] = {
        "base.png": _png_bytes(base),
        "sky.png": _png_bytes((sky * 255).astype(np.uint8)),
        "bands.png": _png_bytes(labels),
        "occluder.png": _png_bytes((occluder * 255).astype(np.uint8)),
        "overlay.png": _png_bytes((overlay * 255).astype(np.uint8)),
    }
    for name, mask in masks.items():
        files[f"band_{name}.png"] = _png_bytes((mask * 255).astype(np.uint8))

    inputs = [
        {
            "key": f.key,
            "role": "base",
            "sha256": sha256_file(f.path),
            "image_time_utc": f.sidecar.get("image_time_utc"),
        }
        for f in base_frames
    ]
    inputs.append(
        {
            "key": geometry.key,
            "role": "geometry (sky segmentation, skyline check)",
            "sha256": sha256_file(geometry.path),
            "image_time_utc": geometry.sidecar.get("image_time_utc"),
        }
    )
    for frame in sorted(star_frames, key=lambda f: f.key):
        inputs.append(
            {
                "key": frame.key,
                "role": "night frame (star positions)",
                "sha256": sha256_file(frame.path),
                "image_time_utc": frame.sidecar.get("image_time_utc"),
            }
        )
    band_records = []
    for k, band in enumerate(bands, start=1):
        check = registration[band.name]
        ok = check.passes
        reason = None
        if ok is None:
            reason = "no column where this band forms the skyline could be checked"
        elif not ok:
            p90 = check.abs_px[1] / check.px_per_deg if check.abs_px else float("nan")
            reason = (
                f"its DEM skyline is off the observed skyline by {p90:.2f} deg at the 90th "
                f"percentile of {check.columns_checked} columns (bar {bar_deg} deg): the band "
                "is clipped to the observed terrain, but its other edges come from a "
                "misregistered DEM view (TODO(M2): refit the camera position on the near field)"
            )
        band_records.append(
            {
                **band.to_dict(),
                "label": k,
                "pixels": int(masks[band.name].sum()),
                "file": f"band_{band.name}.png",
                "registration": check.to_dict(),
                "valid": bool(ok),
                "not_valid_because": reason,
            }
        )
    manifest: dict[str, Any] = {
        "kind": PLATE_KIND,
        "viewpoint_id": spec["viewpoint_id"],
        "plate_version": spec["plate_version"],
        "plate_id": f"{spec['viewpoint_id']}/plate-{spec['plate_version']}",
        "builder": f"snow-globe-plates/{__version__}",
        "built_at_utc": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "spec": {"path": str(Path(spec_path)), "sha256": spec_sha},
        "frame_size": [width, height],
        "files": {
            name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
            for name, data in files.items()
        },
        "renderer_inputs": {
            "plate": "base.png",
            "sky_mask": "sky.png",
            "depth": None,
            "camera": {
                "lat": round(solution.camera[0], 7),
                "lon": round(solution.camera[1], 7),
                "elev_m_msl": round(solution.camera[2], 2),
                "heading_deg": round(solution.pose.heading_deg, 4),
                "pitch_deg": round(solution.pose.pitch_deg, 4),
                "roll_deg": round(solution.pose.roll_deg, 4),
                "hfov_deg": round(solution.pose.hfov_deg, 4),
                "agl_m": solution.agl_m,
            },
        },
        "inputs": {
            "frames": inputs,
            "dem": {
                "path": spec["dem"]["path"],
                **dem.to_record(),
                "source": spec["dem"]["source"],
                "licence": spec["dem"]["licence"],
                "datum": spec["dem"]["datum"],
            },
            "star_match": star_info,
        },
        "base": {
            "method": spec["base"]["method"],
            "frames": base_report,
            "dehazed": False,
            "candidates_note": spec["base"].get("candidates_note", ""),
            "candidates": candidates,
        },
        "pose": {**solution.to_dict(), "star_match": star_info["summary"]},
        "pose_check": {
            "landmark_reprojection_limit_deg": 1.0,
            "max_landmark_angle_deg": round(solution.max_landmark_angle_deg, 4),
            "landmark_angles_are": "fit residuals: the camera position is fitted to them",
            "passes": solution.max_landmark_angle_deg <= 1.0,
            "independent_check": "the DEM skyline against the observed one, per band",
            "band_skyline": {name: check.to_dict() for name, check in registration.items()},
            "band_registration_bar": spec["band_registration_bar"],
            "agl_sensitivity": sensitivity,
        },
        "landmarks": landmark_record,
        "bands": band_records,
        "masks": {
            "sky": {
                "file": "sky.png",
                "pixels": int(sky.sum()),
                "from": geometry.key,
                "method": "blue sky connected to the top edge (plates.skyline)",
            },
            "occluder": {
                "file": "occluder.png",
                "pixels": int(occluder.sum()),
                "how": [o.get("how", "") for o in spec["occluders"]],
            },
            "overlay": {
                "file": "overlay.png",
                "box": [x0, y0, x1, y1],
                "what": "the camera's caption strip",
            },
            "bands_label_image": "bands.png (0 none, 1.. in the order of 'bands')",
            "bands_clipped_to": "terrain in the geometry frame (sky.png is false)",
            "skyline_margin_px": margin_px,
            "skyline_margin_from": "the observed skyline; each margin pixel in one band only",
        },
        "todo": list(_TODO),
    }
    if write:
        target.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (target / name).write_bytes(data)
        (target / "plate.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest
