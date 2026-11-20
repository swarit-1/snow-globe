"""``lookout-eval``: command-line entry points (PRD §13, §16 M0.5).

- ``lookout-eval backtest-pair``: one v−1 backtest pair (see :mod:`.backtest_pair`).
- ``lookout-eval ephem-check``: the ephemeris against NPS's ``SolarElevationAngle``
  (see :mod:`.ephem_check`).
- ``lookout-eval heldout``: the committed held-out split (PRD §13.2, :mod:`.heldout`), and
  with ``--check`` what it lets given frame files be used for (metadata only).

None touches the network, except ``backtest-pair --forecast auto``, which backfills one
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
        "--forecast-summary",
        type=Path,
        help="the JSON printed by `python -m lookout_eval.pair_forecast` for this forecast file",
    )
    boxes = pair.add_mutually_exclusive_group(required=True)
    boxes.add_argument(
        "--landmark-box",
        help="'x0,y0,x1,y1', a polygon 'x,y;x,y;x,y' or the JSON form",
    )
    boxes.add_argument(
        "--boxes",
        metavar="v-1|FILE",
        help=(
            "named landmark boxes: 'v-1' for the committed by-eye boxes "
            "(lookout_eval/landmark_boxes_v1.json) or a file of that form; the primary box "
            "is the scored region and every other box is reported beside it"
        ),
    )
    pair.add_argument(
        "--tag",
        help="label of a repeat run: output names get '.<tag>'; the pair id and scene do not",
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
            "extra held-out local days YYYY-MM-DD:YYYY-MM-DD in the viewpoint's zone "
            "(repeatable), added to the committed split (lookout-eval heldout); it never "
            "removes a committed period"
        ),
    )
    pair.add_argument(
        "--design",
        choices=["P", "H"],
        help=(
            "the design the pair is for: its held-out period sets the fit bound for the "
            "references and plate (default: the viewpoint's earliest period, the strictest)"
        ),
    )

    check = sub.add_parser("ephem-check", help="oculus-core vs NPS SolarElevationAngle")
    check.add_argument("--search", type=Path, default=Path(DEFAULT_SEARCH).expanduser())
    check.add_argument("--viewpoint", default="point-reyes.beach")
    check.add_argument("--json", type=Path, help="also write the full report as JSON")

    held = sub.add_parser("heldout", help="the committed held-out split; --check frame files")
    held.add_argument(
        "--check",
        action="append",
        type=Path,
        default=[],
        metavar="FRAME",
        help="a frame file to check (repeatable); exit 1 if any may not be used for development",
    )
    held.add_argument(
        "--design", choices=["P", "H"], help="fit bound of this design (default: the strictest)"
    )
    return parser


def _refuse_quarantined_file(role: str, path: Path, root: Path) -> None:
    """Refuse a file under the quarantine folder before reading it (D-022)."""
    from .heldout import LeakageError, path_quarantine_reason

    reason = path_quarantine_reason(path.expanduser(), roots=(root,))
    if reason is not None:
        raise LeakageError(f"{role} {path}: {reason}")


def _references(args: argparse.Namespace, root: Path) -> tuple[Path, ...]:
    paths = list(args.reference)
    if args.references_file is not None:
        _refuse_quarantined_file("references file", args.references_file, root)
        for line in args.references_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                paths.append(Path(line))
    return tuple(p.expanduser() for p in paths)


def _backtest_pair(args: argparse.Namespace) -> int:
    from dataclasses import replace

    from lookout_common.paths import data_dir

    from .backtest_pair import PairInputs, check_truth_frame, run_pair
    from .extractor import ExtractorParams, Region
    from .landmark_boxes import load_boxes
    from .leakage import HeldOut
    from .scene import SceneAssumptions

    root = data_dir()
    held_out = None
    if args.held_out:
        zone = load_manifest(args.viewpoint).time_zone
        held_out = HeldOut()
        for first, last in args.held_out:
            held_out = held_out | HeldOut.from_local_dates(first, last, zone)
    # The truth frame's held-out checks come before it is opened for its size (PRD §13.2).
    check_truth_frame(args.truth, viewpoint_id=args.viewpoint, held_out=held_out, data_root=root)
    boxes = None
    if args.boxes is not None:
        # load_boxes refuses a quarantined file before reading it (D-022).
        try:
            boxes = load_boxes(
                args.viewpoint, None if args.boxes == "v-1" else Path(args.boxes), roots=(root,)
            )
        except KeyError as exc:  # the file has no boxes for this viewpoint
            raise ValueError(exc.args[0] if exc.args else str(exc)) from exc
        region = boxes.primary_box.region()
    else:
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
    inputs = PairInputs(
        viewpoint_id=args.viewpoint,
        valid_time=parse_iso_utc(args.valid),
        lead_hours=args.lead,
        truth_path=args.truth,
        forecast=args.forecast,
        region=region,
        reference_paths=_references(args, root),
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
        design=args.design,
        boxes=boxes,
        tag=args.tag,
        forecast_summary=args.forecast_summary,
    )
    record = run_pair(inputs)
    score = record.get("score") or {}
    summary = {
        "status": record["status"],
        "pair_id": record["pair_id"],
        "record": record["record_path"],
        "render": ((record.get("render") or {}).get("png") or {}).get("path"),
        "truth_class": (record["extractor"] or {}).get("truth", {}).get("p1_class"),
        "render_class": (record["extractor"] or {}).get("render", {}).get("p1_class"),
        "cell": score.get("cell"),
        "boxes": {
            name: {
                "truth": box.get("observed_class"),
                "render": box.get("forecast_class"),
                "ratio_truth": box.get("ratio_truth"),
                "ratio_render": box.get("ratio_render"),
                "cell": box.get("cell"),
            }
            for name, box in (score.get("boxes") or {}).items()
        },
        "s_rule": (record["s_rule"] or {}).get("value"),
        "excluded_by": (record["extractor"] or {}).get("excluded_by"),
        "timings_s": record.get("timings_s"),
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


def _heldout(args: argparse.Namespace) -> int:
    from .heldout import committed_split
    from .leakage import frame_file_verdict

    split = committed_split()
    if not args.check:
        print(json.dumps(split.summary(), indent=2))
        return 0
    from lookout_common.paths import data_dir

    root = data_dir()
    verdicts = [frame_file_verdict(p, data_root=root, design=args.design) for p in args.check]
    report = {"split": split.to_record(), "design": args.design, "frames": verdicts}
    print(json.dumps(report, indent=2))
    return 0 if all(v["development"] for v in verdicts) else 1


def main(argv: list[str] | None = None) -> int:
    """Entry point of the ``lookout-eval`` console script."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "backtest-pair":
            return _backtest_pair(args)
        if args.command == "heldout":
            return _heldout(args)
        return _ephem_check(args)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"lookout-eval {args.command}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
