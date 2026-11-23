"""``snow-globe-plates``: build a plate set, match its stars, list base candidates (PRD §8.5).

- ``snow-globe-plates match-stars --spec plates/specs/<viewpoint>.<version>.json --catalogue
  <BSC5 catalog file>``: finds and matches the stars of the spec's night frames
  (:mod:`.starmatch`) and writes the match file the spec names, in the data dir (it holds
  catalogue positions, which stay out of the repo); prints its SHA-256 for the spec. An
  existing match file is never overwritten.
- ``snow-globe-plates build --spec plates/specs/<viewpoint>.<version>.json``: builds the plate
  set into ``<data dir>/plates/<viewpoint>/<version>/`` and prints a summary (:mod:`.build`).
  ``--dry-run`` solves and checks without writing; an existing plate is never overwritten.
- ``snow-globe-plates candidates --viewpoint ID``: one line per daytime capture (Sun ≥ 3°),
  from the sidecars only, with the Sun and the minutes from solar noon (:mod:`.base`).

Offline: no command touches the network. The data dir is snow-globe-common's ``data_dir()``
unless ``--data-dir`` is given.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

__all__ = ["main"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="snow-globe-plates", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--data-dir", type=Path, help="data dir (default: snow-globe-common's data_dir())"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="build a plate set from a spec")
    build.add_argument("--spec", type=Path, required=True)
    build.add_argument(
        "--out", type=Path, help="output folder (default: <data dir>/plates/<viewpoint>/<version>)"
    )
    build.add_argument("--dry-run", action="store_true", help="solve and check, write nothing")
    stars = sub.add_parser("match-stars", help="match the spec's night-frame stars (offline)")
    stars.add_argument("--spec", type=Path, required=True)
    stars.add_argument(
        "--catalogue", type=Path, required=True, help="CDS V/50 (BSC5) catalog file, plain or gzip"
    )
    cand = sub.add_parser("candidates", help="list daytime captures for choosing a base frame")
    cand.add_argument("--viewpoint", required=True)
    cand.add_argument("--min-sun", type=float, default=3.0)
    return parser


def _root(args: argparse.Namespace) -> Path:
    if args.data_dir is not None:
        return Path(args.data_dir).expanduser()
    from snow_globe_common.paths import data_dir

    return data_dir()


def _build(args: argparse.Namespace) -> int:
    from .build import build

    manifest = build(args.spec, data_root=_root(args), out_dir=args.out, write=not args.dry_run)
    pose = manifest["pose"]
    summary = {
        "plate_id": manifest["plate_id"],
        "written": not args.dry_run,
        "camera": manifest["renderer_inputs"]["camera"],
        "max_landmark_angle_deg": manifest["pose_check"]["max_landmark_angle_deg"],
        "landmark_angles_are": manifest["pose_check"]["landmark_angles_are"],
        "pose_check_passes": manifest["pose_check"]["passes"],
        "stars": pose["stars"],
        "star_match": pose["star_match"],
        "landmark_only_vs_star_only_ray_deg": pose["landmark_only_vs_star_only_ray_deg"],
        "band_skyline": manifest["pose_check"]["band_skyline"],
        "agl_sensitivity": manifest["pose_check"]["agl_sensitivity"],
        "bands": {
            b["name"]: {"pixels": b["pixels"], "valid": b["valid"]} for b in manifest["bands"]
        },
        "files": sorted(manifest["files"]),
    }
    print(json.dumps(summary, indent=2))
    return 0


def _match_stars(args: argparse.Namespace) -> int:
    import hashlib
    from datetime import UTC, datetime

    import numpy as np
    from PIL import Image

    from . import __version__
    from .camera import Pose
    from .frames import frame_input, sha256_file
    from .skyline import observed_skyline, sky_segmentation
    from .starmatch import (
        NightFrame,
        match_record,
        match_stars,
        read_bsc5,
        settings_from_spec,
    )

    root = _root(args)
    spec = _spec_for_matching(args.spec)
    target = root / spec["stars"]["file"]
    if target.exists():
        raise ValueError(f"{target} exists; match files are never overwritten")
    settings = settings_from_spec(spec["stars"])
    catalogue = read_bsc5(args.catalogue)
    geometry = frame_input(root, spec["geometry_frame"])
    with Image.open(geometry.path) as im:
        rgb = np.asarray(im.convert("RGB"), dtype=np.uint8)
    skyline = observed_skyline(sky_segmentation(rgb))
    frames = []
    for key in spec["stars"]["frames"]:
        frame = frame_input(root, key)
        frames.append(
            NightFrame.from_file(
                key,
                frame.path,
                sha256_file(frame.path),
                frame.image_time,
                noise_box=settings.noise_box,
                median_px=settings.median_px,
            )
        )
    width, height = (int(v) for v in spec["frame_size"])
    ip = spec["initial_pose"]
    initial = Pose(
        float(ip["heading_deg"]),
        float(ip["pitch_deg"]),
        float(ip["roll_deg"]),
        float(ip["hfov_deg"]),
        width,
        height,
    )
    start = (float(spec["camera"]["start"]["lat"]), float(spec["camera"]["start"]["lon"]))
    match = match_stars(frames, catalogue, start, initial, skyline, settings)
    record = match_record(
        match,
        viewpoint_id=spec["viewpoint_id"],
        catalogue=catalogue,
        catalogue_path=str(args.catalogue),
        frames=frames,
        camera_lat_lon=start,
        initial=initial,
        settings=settings,
        skyline_from={"key": geometry.key, "sha256": sha256_file(geometry.path)},
        builder=f"snow-globe-plates/{__version__}",
        made_at_utc=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )
    data = (json.dumps(record, indent=1) + "\n").encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    print(
        json.dumps(
            {
                "written": str(target),
                "sha256": hashlib.sha256(data).hexdigest(),
                "summary": record["summary"],
            },
            indent=2,
        )
    )
    return 0


def _spec_for_matching(path: Path) -> dict[str, Any]:
    """The spec for ``match-stars``: its stars entry names the frames and the file to write."""
    spec = json.loads(Path(path).read_bytes())
    if spec.get("kind") != "snow-globe.plate-spec" or spec.get("spec_version") != 2:
        raise ValueError(f"{path}: not a snow-globe.plate-spec version 2 file")
    stars = spec.get("stars", {})
    for field in ("file", "frames"):
        if field not in stars:
            raise ValueError(f"{path}: stars.{field} is missing")
    return dict(spec)


def _candidates(args: argparse.Namespace) -> int:
    from snow_globe_common.manifest import load_manifest

    from .base import candidate_rows
    from .frames import HeldOutFrameError, frame_input

    root = _root(args)
    viewpoint = load_manifest(args.viewpoint)
    frames = []
    skipped = 0
    for sidecar in sorted((root / "frames" / args.viewpoint).glob("*/*.json")):
        key = f"frames/{args.viewpoint}/{sidecar.parent.name}/{sidecar.with_suffix('.jpg').name}"
        try:
            frames.append(frame_input(root, key))
        except (HeldOutFrameError, FileNotFoundError):
            skipped += 1
    rows = candidate_rows(
        frames, viewpoint.camera.lat, viewpoint.camera.lon, min_sun_deg=args.min_sun
    )
    for row in rows:
        print(json.dumps(row))
    print(f"{len(rows)} daytime frames; {skipped} skipped (held out or missing)", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point of the ``snow-globe-plates`` console script."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            return _build(args)
        if args.command == "match-stars":
            return _match_stars(args)
        return _candidates(args)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"snow-globe-plates {args.command}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
