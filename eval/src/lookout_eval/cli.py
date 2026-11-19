"""``lookout-eval``: command-line entry points (PRD §13, §16 M0.5).

- ``lookout-eval backtest-pair``: one v−1 backtest pair (see :mod:`.backtest_pair`).
- ``lookout-eval ephem-check``: the ephemeris against NPS's ``SolarElevationAngle``
  (see :mod:`.ephem_check`).

Neither touches the network, except ``backtest-pair --forecast auto``, which backfills one
HRRR run from NOAA's public bucket through lookout-forecast.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from lookout_common.manifest import load_manifest
from lookout_common.timeutil import parse_iso_utc
from PIL import Image

__all__ = ["main"]

DEFAULT_SEARCH = "~/lookout-data/samples/nps-json/search_pore_20261117.json"


def _size(text: str) -> tuple[int, int]:
    width, height = text.lower().split("x")
    return int(width), int(height)


def _day_range(text: str) -> tuple[date, date]:
    first, _, last = text.partition(":")
    try:
        return date.fromisoformat(first), date.fromisoformat(last)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD:YYYY-MM-DD, got {text!r}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lookout-eval", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    pair = sub.add_parser("backtest-pair", help="render and score one v-1 backtest pair")
    pair.add_argument(
        "--viewpoint", required=True, help="viewpoint id, e.g. rainier.paradise-mountain"
    )
    pair.add_argument("--valid", required=True, help="valid time, ISO 8601 with Z or an offset")
    pair.add_argument(
        "--lead", required=True, type=float, help="lead in hours; issue = valid - lead"
    )
    pair.add_argument("--truth", required=True, type=Path, help="truth frame (archive/...jpg)")
    pair.add_argument(
        "--forecast", required=True, help="forecast run JSON (forecast/hrrr/...) or 'auto'"
    )
    pair.add_argument(
        "--landmark-box",
        required=True,
        help="'x0,y0,x1,y1', a polygon 'x,y;x,y;x,y' or the JSON form",
    )
    pair.add_argument(
        "--landmark-frame-size",
        type=_size,
        help="WxH the box coordinates refer to (default: the truth frame's size)",
    )
    pair.add_argument("--reference", action="append", type=Path, default=[], help="clear-day frame")
    pair.add_argument("--references-file", type=Path, help="text file: one reference path per line")
    pair.add_argument("--plate", type=Path, help="plate (default: nearest-Sun reference frame)")
    pair.add_argument("--sky-mask", type=Path)
    pair.add_argument("--depth", type=Path)
    pair.add_argument("--landmark-name", help="manifest landmark for S_rule (default: the first)")
    pair.add_argument("--tau-hi", type=float, help="visible threshold (v-1 default 0.6)")
    pair.add_argument("--tau-lo", type=float, help="hidden threshold (v-1 default 0.35)")
    pair.add_argument(
        "--hfov", type=float, help="horizontal FOV without a solved pose (default 60)"
    )
    pair.add_argument("--pitch", type=float, help="pitch without a solved pose (default 0)")
    pair.add_argument("--heading", type=float, help="heading without a solved pose")
    pair.add_argument("--renderer-dir", type=Path, help="renderer/ (default: found in the repo)")
    pair.add_argument("--backend", choices=["swiftshader", "gpu"], default="swiftshader")
    pair.add_argument("--truth-tolerance-min", type=float, default=10.0)
    pair.add_argument("--scene-only", action="store_true", help="write the SceneSpec and stop")
    pair.add_argument(
        "--held-out",
        action="append",
        type=_day_range,
        default=[],
        metavar="FIRST:LAST",
        help=(
            "held-out local days YYYY-MM-DD:YYYY-MM-DD in the viewpoint's zone (repeatable); "
            "replaces the default, design P's sealed window 2026-12-15:2027-01-14"
        ),
    )

    check = sub.add_parser("ephem-check", help="oculus-core vs NPS SolarElevationAngle")
    check.add_argument("--search", type=Path, default=Path(DEFAULT_SEARCH).expanduser())
    check.add_argument("--viewpoint", default="point-reyes.beach")
    check.add_argument("--json", type=Path, help="also write the full report as JSON")
    return parser


def _references(args: argparse.Namespace) -> tuple[Path, ...]:
    paths = list(args.reference)
    if args.references_file is not None:
        for line in args.references_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                paths.append(Path(line))
    return tuple(p.expanduser() for p in paths)


def _backtest_pair(args: argparse.Namespace) -> int:
    from dataclasses import replace

    from .backtest_pair import PairInputs, run_pair
    from .extractor import ExtractorParams, Region
    from .leakage import HeldOut
    from .scene import SceneAssumptions

    with Image.open(args.truth.expanduser()) as image:
        truth_size = image.size
    region = Region.parse(args.landmark_box, frame_size=args.landmark_frame_size or truth_size)
    params = ExtractorParams()
    if args.tau_hi is not None or args.tau_lo is not None:
        params = replace(
            params,
            tau_hi=args.tau_hi if args.tau_hi is not None else params.tau_hi,
            tau_lo=args.tau_lo if args.tau_lo is not None else params.tau_lo,
        )
    assumptions = SceneAssumptions()
    overrides = {
        k: v
        for k, v in (
            ("hfov_deg", args.hfov),
            ("pitch_deg", args.pitch),
            ("heading_deg", args.heading),
        )
        if v is not None
    }
    if overrides:
        assumptions = replace(assumptions, **overrides)
    held_out = None
    if args.held_out:
        zone = load_manifest(args.viewpoint).time_zone
        held_out = HeldOut()
        for first, last in args.held_out:
            held_out = held_out | HeldOut.from_local_dates(first, last, zone)
    inputs = PairInputs(
        viewpoint_id=args.viewpoint,
        valid_time=parse_iso_utc(args.valid),
        lead_hours=args.lead,
        truth_path=args.truth,
        forecast=args.forecast,
        region=region,
        reference_paths=_references(args),
        plate_path=args.plate,
        sky_mask_path=args.sky_mask,
        depth_path=args.depth,
        landmark=args.landmark_name,
        truth_tolerance=timedelta(minutes=args.truth_tolerance_min),
        params=params,
        assumptions=assumptions,
        renderer_dir=args.renderer_dir,
        renderer_backend=args.backend,
        scene_only=args.scene_only,
        held_out=held_out,
    )
    record = run_pair(inputs)
    summary = {
        "status": record["status"],
        "pair_id": record["pair_id"],
        "record": record["record_path"],
        "truth": (record["extractor"] or {}).get("truth", {}).get("p1_class"),
        "render": (record["extractor"] or {}).get("render", {}).get("p1_class"),
        "s_rule": (record["s_rule"] or {}).get("value"),
        "excluded_by": (record["extractor"] or {}).get("excluded_by"),
        "notes": record["notes"],
    }
    print(json.dumps(summary, indent=2))
    return 1 if record["status"] in ("render-failed", "scene-rejected") else 0


def _ephem_check(args: argparse.Namespace) -> int:
    from .ephem_check import format_report, run_check

    report = run_check(args.search, load_manifest(args.viewpoint))
    print(format_report(report))
    if args.json is not None:
        args.json.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point of the ``lookout-eval`` console script."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "backtest-pair":
            return _backtest_pair(args)
        return _ephem_check(args)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"lookout-eval {args.command}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
