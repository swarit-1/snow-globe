"""``snow-globe-eval``: command-line entry points (PRD §13, §16 M0.5).

- ``snow-globe-eval backtest-pair``: one v−1 backtest pair (see :mod:`.backtest_pair`).
- ``snow-globe-eval ephem-check``: the ephemeris against NPS's ``SolarElevationAngle``
  (see :mod:`.ephem_check`).
- ``snow-globe-eval heldout``: the committed held-out split (PRD §13.2, :mod:`.heldout`; the
  current version in full, and the identity of each earlier one, which older records cite),
  and with ``--check`` what it lets given frame files be used for (metadata only).
- ``snow-globe-eval data-report``: the M0.5 gate's log evidence, per viewpoint, with outages,
  exclusions and the 72 h clock (PRD §16 M0.5, :mod:`.data_report`); offline and read-only.
- ``snow-globe-eval extractor-v0``: extractor v0 over every capture of a viewpoint against its
  plate's landmark bands, and its validation on the hand labels (PRD §13.3, §13.8;
  :mod:`.extractor_v0_run`); offline, writes under ``<data dir>/scores/extractor-v0/``.
- ``snow-globe-eval extractor-v0.2``: extractor v0.2's leave-one-day-out check on the merged
  two-labeller labels v0.2, with v0 on the same folds (PRD §13.3, §13.8;
  :mod:`.extractor_v0_2_run`); refuses to score without the pre-registration; offline,
  resumable, writes under ``<data dir>/scores/extractor-v0.2/``, never over a finished run.
  ``--merge-labels DIR --labels OUT`` writes a new merged labels file instead.
- ``snow-globe-eval p1dev``: PLAN M1.5's dev-only P1 table (PRD §13.4–§13.7; :mod:`.p1dev`):
  every Rainier daytime frame rendered from its 24 h hindcast run and scored against
  persistence, climatology and S_rule; offline, resumable, writes under
  ``<data dir>/scores/p1dev/`` and ``<data dir>/renders/p1dev/``.
- ``snow-globe-eval ephem-validate``: PLAN M2's ephemeris validation (PRD §8.4;
  :mod:`.ephem_validate`): oculus-core and eval's refraction against JPL Horizons (8 viewpoints,
  hourly for 12 months), 1,000 Skyfield samples and NOAA sunrise/sunset, and the camera-frame
  transform; writes under ``<data dir>/reports/ephem/``.

None touches the network, except ``backtest-pair --forecast auto``, which backfills one
HRRR run from NOAA's public bucket through snow-globe-forecast, and the first
``ephem-validate``, which fetches the Horizons tables and the DE421 kernel once into
``<data dir>/cache/ephem/`` and builds Skyfield's isolated environment with uv (``--offline``
then re-runs from the cache).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from PIL import Image
from snow_globe_common.manifest import load_manifest
from snow_globe_common.timeutil import parse_iso_utc

__all__ = ["main"]

#: The saved archive search, relative to the data dir (``data_dir()``: the variables,
#: then ``~/snow-globe-data`` or, until it is moved, ``~/lookout-data``; D-080). Resolved when
#: ``ephem-check`` runs, so ``$SNOW_GLOBE_DATA_DIR`` and ``$LOOKOUT_DATA_DIR`` apply.
DEFAULT_SEARCH = "samples/nps-json/search_pore_20261117.json"


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
    parser = argparse.ArgumentParser(prog="snow-globe-eval", description=__doc__.splitlines()[0])
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
        help="the JSON printed by `python -m snow_globe_eval.pair_forecast` for this forecast file",
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
            "(snow_globe_eval/landmark_boxes_v1.json) or a file of that form; the primary box "
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
            "(repeatable), added to the committed split (snow-globe-eval heldout); it never "
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
    check.add_argument(
        "--search",
        type=Path,
        default=None,
        help=f"the saved archive search (default: <data dir>/{DEFAULT_SEARCH})",
    )
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

    report = sub.add_parser(
        "data-report",
        help="the M0.5 gate's log evidence: per viewpoint, outages, exclusions, the 72 h clock",
    )
    report.add_argument(
        "--since",
        help="window start, ISO 8601 with Z or an offset (default 2026-11-19T12:24:23Z, when "
        "the capture loop took over all 8 cameras)",
    )
    report.add_argument("--until", help="window end, ISO 8601 (default: now)")
    report.add_argument(
        "--json",
        nargs="?",
        const="-",
        metavar="PATH",
        help="print the JSON report instead of the table; with PATH, write the JSON there and "
        "print the table",
    )
    report.add_argument(
        "--capture-gap-s",
        type=float,
        default=540.0,
        help="a gap between capture passes longer than this is an outage (default 540)",
    )
    report.add_argument(
        "--forecast-gap-s",
        type=float,
        default=900.0,
        help="a run whose first write finished this much after it was fetchable is late, and "
        "a span this long with no live run written while one was due is an outage (default 900)",
    )
    report.add_argument(
        "--clock-hours", type=float, default=72.0, help="hours the gate needs (default 72)"
    )

    v0 = sub.add_parser(
        "extractor-v0",
        help="extractor v0 over every capture of a viewpoint, validated on the hand labels",
    )
    v0.add_argument("--viewpoint", default="rainier.paradise-mountain")
    v0.add_argument(
        "--plate",
        type=Path,
        help="plate folder (default: <data dir>/plates/<viewpoint>/v0.1, the current plate)",
    )
    v0.add_argument(
        "--labels",
        type=Path,
        help="labels file (default: the committed snow_globe_eval/labels_rainier_v0_1.json)",
    )
    v0.add_argument(
        "--out",
        type=Path,
        help="output folder (default: <data dir>/scores/extractor-v0/<viewpoint>/<UTC stamp>)",
    )
    v0.add_argument(
        "--tau-hi",
        type=float,
        help="visible threshold (v0's fixed 0.6); any other value marks the run as overridden, "
        "and its validation never meets the bar",
    )
    v0.add_argument(
        "--tau-lo",
        type=float,
        help="hidden threshold (v0's fixed 0.35); any other value marks the run as overridden",
    )

    from .p1dev import add_arguments as add_p1dev_arguments

    add_p1dev_arguments(
        sub.add_parser("p1dev", help="PLAN M1.5's dev-only P1 table (resumable; --run-id)")
    )

    from .ephem_validate.cli import register as register_ephem_validate

    register_ephem_validate(sub)

    from .extractor_v0_2_run import register as register_extractor_v0_2

    register_extractor_v0_2(sub)
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

    from snow_globe_common.paths import data_dir

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
    from snow_globe_common.paths import data_dir

    from .ephem_check import format_report, run_check

    search = args.search if args.search is not None else data_dir() / DEFAULT_SEARCH
    report = run_check(search, load_manifest(args.viewpoint))
    print(format_report(report))
    if args.json is not None:
        args.json.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    return 0


def _heldout(args: argparse.Namespace) -> int:
    from .heldout import committed_split, committed_splits
    from .leakage import frame_file_verdict

    split = committed_split()
    if not args.check:
        # The current split in full, and the identity of each earlier committed version,
        # which older records cite (the M0.5 pair's records cite version 1).
        earlier = [s.to_record() for v, s in committed_splits().items() if v < split.version]
        print(json.dumps({**split.summary(), "earlier_versions": earlier}, indent=2))
        return 0
    from snow_globe_common.paths import data_dir

    root = data_dir()
    verdicts = [frame_file_verdict(p, data_root=root, design=args.design) for p in args.check]
    report = {"split": split.to_record(), "design": args.design, "frames": verdicts}
    print(json.dumps(report, indent=2))
    return 0 if all(v["development"] for v in verdicts) else 1


def _manifest_ids() -> list[str] | None:
    """The viewpoint ids of the manifests, so a camera with no record still gets a row."""
    from snow_globe_common.manifest import load_manifests

    try:
        return sorted(load_manifests())
    except (OSError, ValueError, RuntimeError):  # no manifests here: the data's ids are used
        return None


def _data_report(args: argparse.Namespace) -> int:
    from snow_globe_common.paths import data_dir

    from .data_report import DEFAULT_SINCE, ReportOptions, build_report, format_report

    options = ReportOptions(
        capture_gap_s=args.capture_gap_s,
        forecast_gap_s=args.forecast_gap_s,
        clock_hours=args.clock_hours,
    )
    report = build_report(
        data_dir(),
        since=parse_iso_utc(args.since) if args.since else DEFAULT_SINCE,
        until=parse_iso_utc(args.until) if args.until else None,
        viewpoint_ids=_manifest_ids(),
        options=options,
    )
    text = json.dumps(report, indent=2)
    if args.json == "-":
        print(text)
        return 0
    if args.json is not None:
        Path(args.json).expanduser().write_text(text + "\n", encoding="utf-8")
    print(format_report(report), end="")
    return 0


def _extractor_v0(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from snow_globe_common.paths import data_dir

    from .extractor_v0 import V0Params
    from .extractor_v0_run import DEFAULT_PLATE_VERSION, run, with_thresholds

    root = data_dir()
    plate = args.plate or root / "plates" / args.viewpoint / DEFAULT_PLATE_VERSION
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    runs = root / "scores" / "extractor-v0" / args.viewpoint
    out = args.out or runs / stamp
    if args.out is None and out.exists():
        raise ValueError(f"{out} exists; runs are never overwritten")
    params = with_thresholds(V0Params(), tau_hi=args.tau_hi, tau_lo=args.tau_lo)
    report = run(
        data_root=root,
        viewpoint_id=args.viewpoint,
        plate_dir=plate,
        labels_path=args.labels,
        out_dir=out,
        params=params,
        run_log=runs / "runs.jsonl",
    )
    summary, validation = report["summary"], report["validation"]
    print(
        json.dumps(
            {
                "out": str(out),
                "frame_classes": summary["frame_classes"],
                "daytime_frames": summary["daytime_frames"],
                "daytime_without_usable_reference": summary["daytime_without_usable_reference"],
                "band_classes_of_scored_frames": summary["band_classes_of_scored_frames"],
                "validation_f1_label_partial_excluded": {
                    b: v["label_partial_excluded"]["f1"] for b, v in validation["bands"].items()
                },
                "validation_n": {
                    b: v["label_partial_excluded"]["n"] for b, v in validation["bands"].items()
                },
                "meets_f1_bar": {b: v["meets_f1_bar"] for b, v in validation["bands"].items()},
                "reference_pool_frames_left_out": validation["reference_pool_frames_left_out"],
                "thresholds_overridden": validation["thresholds_overridden"],
                "publishable": validation["publishable"],
            },
            indent=2,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point of the ``snow-globe-eval`` console script."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "backtest-pair":
            return _backtest_pair(args)
        if args.command == "heldout":
            return _heldout(args)
        if args.command == "data-report":
            return _data_report(args)
        if args.command == "extractor-v0":
            return _extractor_v0(args)
        if args.command == "p1dev":
            from .p1dev import run_from_args

            return run_from_args(args)
        if args.command == "ephem-validate":
            return args.handler(args)
        if args.command == "extractor-v0.2":
            return args.handler(args)
        return _ephem_check(args)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"snow-globe-eval {args.command}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
