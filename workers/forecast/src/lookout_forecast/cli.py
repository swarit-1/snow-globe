"""``lookout-forecast``: the forecast worker's command line (PRD §8.3, §11.2).

Commands (all but ``status --offline`` read NOAA's bucket):

- ``latest``: extract the newest complete run.
- ``run YYYYMMDDHH``: extract one run.
- ``backfill --start --end``: extract archived runs (availability modelled).
- ``status``: what is written, the lag log, and (online) what is on S3.
- ``plan YYYYMMDDHH``: bytes per field per hour, from the inventories only.
- ``grid-check``, ``datum-check``: the evidence behind the grid mapping and the datums.
- ``lag-survey --start --end``: publication lags of past runs from S3 listings (no GRIB).

Shared options: ``--max-fh N``, ``--fields F ...`` (inventory names, aliases or unique
variables, e.g. ``VIS "HGT:cloud base" LCDC``), ``--only VIEWPOINT``, ``--max-mb N`` (refuse a
run whose planned download is larger), ``--force``, ``--json``. Without ``--fields``,
``--max-fh`` or ``--max-mb``, the Lambda's ``LOOKOUT_FORECAST_FIELDS``, ``LOOKOUT_FORECAST_MAX_FH``
and ``LOOKOUT_FORECAST_MAX_MB`` apply, so ``make forecast-latest`` can be limited from the
environment.

Downloads stay small by default (CLAUDE.md "NOAA"): with neither ``--max-mb`` nor
``LOOKOUT_FORECAST_MAX_MB``, a run whose plan exceeds :data:`DEFAULT_CLI_MAX_MB` (150 MB) is
refused before any GRIB byte is fetched. All 22 fields cost about 24 MB per forecast hour,
so a whole 18 h run (about 460 MB) needs an explicit ``--max-mb``, or fewer fields or
hours. ``backfill`` also stops at :data:`DEFAULT_BACKFILL_TOTAL_MB` (1,000 MB) of GRIB data
unless ``--max-total-mb`` says otherwise, and never lets one run go past what is left.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

from lookout_common.manifest import load_manifests
from lookout_common.paths import cache_dir
from lookout_common.storage import LockTimeoutError, Storage, storage_from_env
from lookout_common.timeutil import isoformat_z, utcnow

from .availability import LagModel, read_lag_events
from .bucket import BucketError, HrrrBucket
from .discovery import find_latest_complete, list_run
from .extract import (
    BandwidthCapError,
    ExtractOptions,
    Extractor,
    RunIncompleteError,
    RunNotFoundError,
    RunResult,
    marker_key,
    plan_run,
    run_covered,
)
from .fields import CATALOGUE, resolve_fields
from .hrrr import STORAGE_SOURCE, format_run_id, max_fh, parse_run_id, run_hours

__all__ = ["DEFAULT_BACKFILL_TOTAL_MB", "DEFAULT_CLI_MAX_MB", "build_parser", "main"]

log = logging.getLogger("lookout_forecast")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_READY = 3
EXIT_CAP = 4

#: Per-run cap (planned GRIB MB) when neither --max-mb nor LOOKOUT_FORECAST_MAX_MB is set.
DEFAULT_CLI_MAX_MB = 150.0
#: GRIB MB one ``backfill`` may download when --max-total-mb is not given.
DEFAULT_BACKFILL_TOTAL_MB = 1000.0
_CAP_HINT = (
    "raise the per-run cap with --max-mb or LOOKOUT_FORECAST_MAX_MB (default "
    f"{DEFAULT_CLI_MAX_MB:.0f} MB), or ask for fewer --fields or a lower --max-fh"
)


def _positive_float(text: str) -> float:
    value = float(text)
    if not value > 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {text}")
    return value


def _add_extract_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-fh", type=int, default=None, help="last forecast hour to extract")
    parser.add_argument(
        "--fields",
        nargs="+",
        default=None,
        metavar="FIELD",
        help=f"fields to extract (default: all {len(CATALOGUE)})",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        metavar="VIEWPOINT",
        help="only this viewpoint (repeatable)",
    )
    parser.add_argument(
        "--max-mb",
        type=_positive_float,
        default=None,
        help=(
            "refuse a run whose planned GRIB download exceeds this many MB (default: "
            f"LOOKOUT_FORECAST_MAX_MB, else {DEFAULT_CLI_MAX_MB:.0f})"
        ),
    )
    parser.add_argument("--workers", type=int, default=4, help="parallel hour downloads")
    parser.add_argument(
        "--live-window-h",
        type=float,
        default=24.0,
        help="runs processed within this many hours of their start are 'live'",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="rewrite files even if they already hold these fields and hours",
    )
    parser.add_argument("--json", action="store_true", help="print the result as JSON")


def build_parser() -> argparse.ArgumentParser:
    """The argument parser (exposed for tests)."""
    parser = argparse.ArgumentParser(prog="lookout-forecast", description=__doc__.split("\n")[0])
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    latest = sub.add_parser("latest", help="extract the newest complete run")
    _add_extract_options(latest)
    latest.add_argument(
        "--lookback-h", type=int, default=8, help="how many hours back to look for a complete run"
    )

    run = sub.add_parser("run", help="extract one run")
    run.add_argument("run_id", help="YYYYMMDDHH")
    _add_extract_options(run)
    run.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="extract the hours that exist even if some are missing",
    )

    backfill = sub.add_parser("backfill", help="extract archived runs (modelled availability)")
    backfill.add_argument("--start", required=True, help="first run, YYYYMMDDHH")
    backfill.add_argument("--end", required=True, help="last run, YYYYMMDDHH (inclusive)")
    backfill.add_argument("--every-h", type=int, default=1, help="step between runs, hours")
    backfill.add_argument(
        "--synoptic-only",
        action="store_true",
        help="only 00/06/12/18Z runs (the ones that reach 48 h)",
    )
    backfill.add_argument(
        "--max-total-mb",
        type=_positive_float,
        default=DEFAULT_BACKFILL_TOTAL_MB,
        help=(
            "GRIB MB this backfill may download in all; each run's cap is what is left "
            f"(default {DEFAULT_BACKFILL_TOTAL_MB:.0f})"
        ),
    )
    _add_extract_options(backfill)

    status = sub.add_parser("status", help="what is written, the lag log, and what is on S3")
    status.add_argument("--offline", action="store_true", help="do not contact the bucket")
    status.add_argument("--hours", type=int, default=6, help="runs to list on S3 (online)")
    status.add_argument("--json", action="store_true")

    plan = sub.add_parser("plan", help="bytes per field per hour, from the inventories only")
    plan.add_argument("run_id")
    plan.add_argument("--max-fh", type=int, default=None)
    plan.add_argument("--fields", nargs="+", default=None)
    plan.add_argument("--json", action="store_true")

    grid = sub.add_parser("grid-check", help="compare the grid mapping with ecCodes lat/lon")
    grid.add_argument("run_id")
    grid.add_argument("--fh", type=int, default=0)
    grid.add_argument("--write-fixture", type=Path, default=None, metavar="PATH")

    datum = sub.add_parser("datum-check", help="compare height fields with the model terrain")
    datum.add_argument("run_id")
    datum.add_argument("--fh", type=int, default=0)

    survey = sub.add_parser(
        "lag-survey", help="publication lags of past runs from S3 listings (no GRIB, no writes)"
    )
    survey.add_argument("--start", required=True, help="first run, YYYYMMDDHH")
    survey.add_argument("--end", required=True, help="last run, YYYYMMDDHH (inclusive)")
    survey.add_argument(
        "--pause-s", type=float, default=0.2, help="pause between listings (seconds)"
    )
    return parser


def _env(name: str) -> str | None:
    value = (os.environ.get(name) or "").strip()
    return value or None


def _options(args: argparse.Namespace, *, allow_incomplete: bool = False) -> ExtractOptions:
    """Flags win; without a flag, the Lambda's environment variables apply here too.

    The per-run cap falls back to :data:`DEFAULT_CLI_MAX_MB`, so a laptop run is never
    uncapped.
    """
    fields = args.fields
    if fields is None and _env("LOOKOUT_FORECAST_FIELDS"):
        fields = [_env("LOOKOUT_FORECAST_FIELDS")]
    max_fh = args.max_fh
    if max_fh is None and _env("LOOKOUT_FORECAST_MAX_FH"):
        max_fh = int(_env("LOOKOUT_FORECAST_MAX_FH") or 0)
    max_mb = args.max_mb
    raw_cap = _env("LOOKOUT_FORECAST_MAX_MB")
    if max_mb is None and raw_cap:
        max_mb = float(raw_cap)
        if not max_mb > 0:
            raise ValueError(f"LOOKOUT_FORECAST_MAX_MB must be > 0, got {raw_cap!r}")
    if max_mb is None:
        max_mb = DEFAULT_CLI_MAX_MB
    return ExtractOptions(
        fields=tuple(resolve_fields(fields)),
        max_fh=max_fh,
        viewpoint_ids=tuple(args.only) if args.only else None,
        live_window_h=args.live_window_h,
        allow_incomplete=allow_incomplete,
        max_bytes=int(max_mb * 1e6),
        workers=args.workers,
        force=args.force,
    )


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _summary(result: RunResult) -> str:
    transfer = result.transfer  # this run only
    requests = ", ".join(f"{n} {kind}" for kind, n in sorted(transfer["requests"].items()))
    lines = [
        f"run {result.run_id}: {'live' if result.live else 'archived'}, "
        f"{'complete' if result.run_complete else 'incomplete'} on S3; "
        f"hours f{result.hours[0]:02d}-f{result.hours[-1]:02d} ({len(result.hours)}), "
        f"{len(result.field_names)} fields, {len(result.viewpoints)} viewpoints",
        f"  downloaded {transfer['total_bytes'] / 1e6:.2f} MB in {requests} request(s) "
        f"({transfer['retries']} retries); {result.seconds_total:.1f} s total "
        f"(download {result.seconds_download:.1f} s summed over threads, decode "
        f"{result.seconds_decode:.1f} s)",
        f"  wrote {len(result.written)} file(s)"
        + (f"; skipped {len(result.skipped)}: {result.skipped}" if result.skipped else ""),
    ]
    lags = ", ".join(
        f"f{fh:02d} {lag / 60:.1f} min" for fh, lag in sorted(result.s3_lags_s.items())
    )
    lines.append(f"  S3 lag (LastModified - run start): {lags}")
    return "\n".join(lines)


def _extract(extractor: Extractor, run_start: Any, opts: ExtractOptions, as_json: bool) -> int:
    try:
        result = extractor.extract(run_start, opts)
    except RunIncompleteError as exc:
        print(f"not ready: {exc}", file=sys.stderr)
        return EXIT_NOT_READY
    except RunNotFoundError as exc:
        print(f"not found: {exc}", file=sys.stderr)
        return EXIT_NOT_READY
    except LockTimeoutError as exc:
        print(f"busy: another extraction holds the lock ({exc}); try again later", file=sys.stderr)
        return EXIT_NOT_READY
    except BandwidthCapError as exc:
        print(f"cap: {exc}; {_CAP_HINT}", file=sys.stderr)
        return EXIT_CAP
    if as_json:
        event = result.to_event()
        event["files"] = result.written
        _print(event)
    else:
        print(_summary(result))
    return EXIT_OK


def _cmd_latest(args: argparse.Namespace, storage: Storage, bucket: HrrrBucket) -> int:
    opts = _options(args)
    listing = find_latest_complete(bucket, lookback_h=args.lookback_h)
    if listing is None:
        print(f"no complete run in the last {args.lookback_h} h", file=sys.stderr)
        return EXIT_NOT_READY
    extractor = Extractor(storage, bucket, load_manifests())
    ids = [vp.id for vp in extractor.viewpoints(opts.viewpoint_ids)]
    hours = run_hours(listing.run_start, opts.max_fh)
    if not opts.force and run_covered(
        storage,
        listing.run_start,
        viewpoint_ids=ids,
        field_names=[f.name for f in opts.fields],
        hours=hours,
    ):
        print(
            f"run {listing.run_id} is already written for these fields and hours "
            "(use --force to rewrite)"
        )
        return EXIT_OK
    return _extract(extractor, listing.run_start, opts, args.json)


def _cmd_run(args: argparse.Namespace, storage: Storage, bucket: HrrrBucket) -> int:
    opts = _options(args, allow_incomplete=args.allow_incomplete)
    extractor = Extractor(storage, bucket, load_manifests())
    return _extract(extractor, parse_run_id(args.run_id), opts, args.json)


def _cmd_backfill(args: argparse.Namespace, storage: Storage, bucket: HrrrBucket) -> int:
    start, end = parse_run_id(args.start), parse_run_id(args.end)
    if end < start:
        print("--end is before --start", file=sys.stderr)
        return EXIT_ERROR
    if args.every_h < 1:
        print("--every-h must be >= 1", file=sys.stderr)
        return EXIT_ERROR
    opts = _options(args, allow_incomplete=True)
    extractor = Extractor(storage, bucket, load_manifests())
    ids = [vp.id for vp in extractor.viewpoints(opts.viewpoint_ids)]
    names = [f.name for f in opts.fields]
    per_run_cap = opts.max_bytes if opts.max_bytes is not None else int(DEFAULT_CLI_MAX_MB * 1e6)
    total_cap = int(args.max_total_mb * 1e6)
    before = bucket.ledger.snapshot()
    run = start
    results: list[dict[str, Any]] = []
    while run <= end:
        current, run = run, run + timedelta(hours=args.every_h)
        run_id = format_run_id(current)
        if args.synoptic_only and max_fh(current) != 48:
            continue
        hours = run_hours(current, opts.max_fh)
        if not opts.force and run_covered(
            storage, current, viewpoint_ids=ids, field_names=names, hours=hours, accept_missing=True
        ):
            results.append({"run": run_id, "status": "already written"})
            continue
        left = total_cap - int(bucket.ledger.since(before)["bytes"].get("grib", 0))
        if left <= 0:
            results.append({"run": run_id, "status": "stopped: total cap reached"})
            break
        cap = min(per_run_cap, left)  # one run can never overshoot what the total has left
        try:
            result = extractor.extract(current, replace(opts, max_bytes=cap))
        except BandwidthCapError as exc:
            if exc.planned_bytes <= per_run_cap:  # it fits a run, not what the total has left
                results.append({"run": run_id, "status": f"stopped: total cap reached ({exc})"})
                break
            log.warning("run %s: %s", run_id, exc)
            results.append({"run": run_id, "status": f"skipped: {exc}"})
            continue
        except LockTimeoutError as exc:
            results.append({"run": run_id, "status": f"stopped: lock busy ({exc})"})
            break
        except (RunNotFoundError, RunIncompleteError, BucketError) as exc:
            log.warning("run %s: %s", run_id, exc)
            results.append({"run": run_id, "status": f"skipped: {exc}"})
            continue
        except Exception as exc:  # one bad archived run must not end the backfill
            log.exception("run %s failed", run_id)
            results.append({"run": run_id, "status": f"error: {type(exc).__name__}: {exc}"})
            continue
        results.append(
            {
                "run": result.run_id,
                "status": "written",
                "files": len(result.written),
                "hours": len(result.hours),
                "live": result.live,
                "grib_bytes": result.transfer["bytes"].get("grib", 0),
            }
        )
        if not args.json:
            print(_summary(result))
    transfer = bucket.ledger.since(before)
    summary = {
        "runs": results,
        "transfer": transfer,
        "per_run_cap_bytes": per_run_cap,
        "total_cap_bytes": total_cap,
    }
    if args.json:
        _print(summary)
    else:
        print(
            f"backfill: {sum(r['status'] == 'written' for r in results)} written, "
            f"{transfer['total_bytes'] / 1e6:.1f} MB downloaded in all "
            f"({transfer['bytes'].get('grib', 0) / 1e6:.1f} MB GRIB of the "
            f"{total_cap / 1e6:.0f} MB total cap)"
        )
    return EXIT_OK


def _cache_bytes() -> int:
    root = cache_dir() / STORAGE_SOURCE
    if not root.exists():
        return 0
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def _cmd_status(args: argparse.Namespace, storage: Storage, bucket: HrrrBucket | None) -> int:
    manifests = load_manifests()
    files: dict[str, Any] = {}
    for vp_id in sorted(manifests):
        keys = [
            k
            for k in storage.list_prefix(f"forecast/{STORAGE_SOURCE}/{vp_id}/")
            if k.endswith("z.json")
        ]
        files[vp_id] = {"files": len(keys), "latest": keys[-1].rsplit("/", 1)[-1] if keys else None}
    events = read_lag_events(storage)
    model = LagModel.from_events(events)
    day = utcnow().astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    lag_table = {}
    for fh in (0, 1, 2, 6, 12, 18, 24, 36, 48):
        cycles = range(24) if fh <= 18 else range(0, 24, 6)
        rows = [model.lag_s(day.replace(hour=cycle), fh) for cycle in cycles]
        lag_table[f"f{fh:02d}"] = {
            "lag_min_lo": round(min(r[0] for r in rows) / 60, 1),
            "lag_min_hi": round(max(r[0] for r in rows) / 60, 1),
            "methods": dict(Counter(r[1] for r in rows)),
            "pooled_samples": int(model.pooled_samples.get(fh, 0)),
            "cycles": len(rows),
            "cycles_with_min_samples": sum(r[2] >= model.min_samples for r in rows),
        }
    out: dict[str, Any] = {
        "now_utc": isoformat_z(utcnow()),
        "storage": repr(storage),
        "forecast_files": files,
        "lag_events": len(events),
        "availability_model": lag_table,
        "cache_bytes": _cache_bytes(),
    }
    if bucket is not None:
        runs = []
        newest = utcnow().astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        for back in range(args.hours):
            listing = list_run(bucket, newest - timedelta(hours=back))
            marker = storage.read_json(marker_key(listing.run_start), default=None)
            runs.append(
                {
                    "run": listing.run_id,
                    "on_s3": f"{len(listing.ready_hours)}/{len(listing.expected)} hours",
                    "complete": listing.complete,
                    "written_viewpoints": sorted((marker or {}).get("viewpoints", {})),
                }
            )
        out["bucket_runs"] = runs
        out["bucket_transfer"] = bucket.ledger.snapshot()
    if args.json:
        _print(out)
    else:
        print(
            f"now {out['now_utc']}  storage {out['storage']}  "
            f"cache {out['cache_bytes'] / 1e6:.1f} MB"
        )
        for vp_id, info in files.items():
            print(f"  {vp_id:30s} {info['files']:5d} file(s), latest {info['latest']}")
        print(f"lag events logged: {out['lag_events']} (modelled lag over the cycles of each hour)")
        for fh, row in lag_table.items():
            methods = ", ".join(f"{m} x{n}" for m, n in sorted(row["methods"].items()))
            print(
                f"  {fh}: {row['lag_min_lo']:6.1f}-{row['lag_min_hi']:6.1f} min; "
                f"{row['pooled_samples']} lag(s) logged; {methods}"
            )
        for row in out.get("bucket_runs", []):
            print(
                f"  run {row['run']}: {row['on_s3']} on S3, complete={row['complete']}, "
                f"written for {len(row['written_viewpoints'])} viewpoint(s)"
            )
    return EXIT_OK


def _cmd_plan(args: argparse.Namespace, bucket: HrrrBucket) -> int:
    result = plan_run(
        bucket, parse_run_id(args.run_id), fields=resolve_fields(args.fields), max_fh=args.max_fh
    )
    if args.json:
        _print(result)
        return EXIT_OK
    hours = result["hours"]
    print(f"run {result['run']}: {len(hours)} hours on S3, missing {result['missing_hours']}")
    for name, per_fh in result["bytes_by_field_by_fh"].items():
        values = list(per_fh.values())
        if not values:
            print(f"  {name:34s} not in any inventory")
            continue
        mean = sum(values) / len(values) if values else 0
        print(
            f"  {name:34s} mean {mean / 1e6:6.3f} MB/h  min {min(values) / 1e6:6.3f}  "
            f"max {max(values) / 1e6:6.3f}  ({len(values)} hour(s))"
        )
    total = result["total_bytes"]
    print(
        f"  total {total / 1e6:.1f} MB for the run "
        f"({total / max(1, len(hours)) / 1e6:.2f} MB per hour, "
        f"{100 * (result['fraction_of_files'] or 0):.1f} % of the GRIB files)"
    )
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of the ``lookout-forecast`` console script."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.command == "status" and args.offline:
        return _cmd_status(args, storage_from_env(), None)
    with HrrrBucket() as bucket:
        try:
            if args.command == "plan":
                return _cmd_plan(args, bucket)
            if args.command == "lag-survey":
                from .evidence import lag_survey

                _print(
                    lag_survey(
                        bucket,
                        parse_run_id(args.start),
                        parse_run_id(args.end),
                        pause_s=args.pause_s,
                    )
                )
                return EXIT_OK
            if args.command in {"grid-check", "datum-check"}:
                from .evidence import datum_check, grid_check

                run_start = parse_run_id(args.run_id)
                if args.command == "grid-check":
                    _print(
                        grid_check(
                            bucket,
                            run_start,
                            fh=args.fh,
                            viewpoints=load_manifests(),
                            fixture_path=args.write_fixture,
                        )
                    )
                else:
                    _print(datum_check(bucket, run_start, fh=args.fh, viewpoints=load_manifests()))
                return EXIT_OK
            storage = storage_from_env()
            if args.command == "latest":
                return _cmd_latest(args, storage, bucket)
            if args.command == "run":
                return _cmd_run(args, storage, bucket)
            if args.command == "backfill":
                return _cmd_backfill(args, storage, bucket)
            if args.command == "status":
                return _cmd_status(args, storage, bucket)
        except (BucketError, ValueError, KeyError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR
    return EXIT_ERROR  # pragma: no cover - argparse rejects unknown commands


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
