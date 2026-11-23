"""``snow-globe-forecast``: the forecast worker's command line (PRD §8.3, §11.2; D-027).

Commands (all but ``status --offline`` read NOAA's bucket):

- ``latest``: extract the newest complete run.
- ``run YYYYMMDDHH``: extract one run (``--hours 24 25`` for exactly those hours).
- ``backfill --start --end``: extract archived runs (availability modelled).
- ``tick``: one call of the Lambda handler, the code path the scheduled Lambda runs, with
  the ``SNOW_GLOBE_FORECAST_*`` environment; prints its summary and exits 1 if a run failed in
  the call (4 if every failure was a cap refusal), else 0.
- ``status``: what is written, the lag log by method, and (online) what is on S3.
- ``plan YYYYMMDDHH``: bytes per field per hour, from the inventories only.
- ``grid-check``, ``datum-check``: the evidence behind the grid mapping and the datums.
- ``lag-survey --start [--end] [--log]``: publication lags of past runs from S3 listings
  (no GRIB). With ``--log``, every listed hour not logged yet (live or survey) becomes a
  survey ``lag`` event in ``forecast_log/lag_survey/<today>.jsonl`` (D-027).
- ``lag-check RUN ...``: chosen runs' S3 stamps next to the lag model (no GRIB, no writes).
- ``hindcast plan|fetch|compare|leads|status``: P1's 24 h-lead hindcast, written only under
  ``forecast/hindcast/`` (:mod:`.hindcast.cli`).

Shared options: ``--max-fh N``, ``--fields F ...`` (inventory names, aliases or unique
variables, e.g. ``VIS "HGT:cloud base" LCDC``), ``--only VIEWPOINT``, ``--max-mb N`` (refuse a
run whose planned download is larger), ``--force``, ``--json``. Without ``--fields``,
``--max-fh`` or ``--max-mb``, the Lambda's ``SNOW_GLOBE_FORECAST_FIELDS``,
``SNOW_GLOBE_FORECAST_MAX_FH`` and ``SNOW_GLOBE_FORECAST_MAX_MB`` apply (or their deprecated
``LOOKOUT_`` names), so ``make forecast-latest`` can be limited from the environment.

Downloads stay small by default (CLAUDE.md "NOAA"): with neither ``--max-mb`` nor
``SNOW_GLOBE_FORECAST_MAX_MB``, a run whose plan exceeds :data:`DEFAULT_CLI_MAX_MB` (150 MB) is
refused before any GRIB byte is fetched. All 22 fields cost about 24 MB per forecast hour,
so a whole 18 h run (about 460 MB) needs an explicit ``--max-mb``, or fewer fields or
hours. ``backfill`` also stops at :data:`DEFAULT_BACKFILL_TOTAL_MB` (1,000 MB) of GRIB data
unless ``--max-total-mb`` says otherwise, and never lets one run go past what is left.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

from snow_globe_common.env import env_get
from snow_globe_common.manifest import load_manifests
from snow_globe_common.paths import cache_dir
from snow_globe_common.storage import LockTimeoutError, Storage, storage_from_env
from snow_globe_common.timeutil import isoformat_z, utcnow

from .availability import LagModel
from .bucket import BucketError, HrrrBucket
from .discovery import RunListing, find_latest_complete, list_run
from .extract import (
    BandwidthCapError,
    ExtractOptions,
    Extractor,
    RunIncompleteError,
    RunNotFoundError,
    RunResult,
    marker_key,
    plan_run,
    requested_hours,
    run_covered,
)
from .fields import CATALOGUE, resolve_fields
from .hrrr import STORAGE_SOURCE, format_run_id, max_fh, parse_run_id
from .laglog import METHOD_LIVE, METHODS, SURVEY_LOCK, SurveyLogger, read_lag_log

__all__ = ["DEFAULT_BACKFILL_TOTAL_MB", "DEFAULT_CLI_MAX_MB", "build_parser", "main"]

log = logging.getLogger("snow_globe_forecast")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_READY = 3
EXIT_CAP = 4

#: Per-run cap (planned GRIB MB) when neither --max-mb nor SNOW_GLOBE_FORECAST_MAX_MB is set.
DEFAULT_CLI_MAX_MB = 150.0
#: GRIB MB one ``backfill`` may download when --max-total-mb is not given.
DEFAULT_BACKFILL_TOTAL_MB = 1000.0
_CAP_HINT = (
    "raise the per-run cap with --max-mb or SNOW_GLOBE_FORECAST_MAX_MB (default "
    f"{DEFAULT_CLI_MAX_MB:.0f} MB), or ask for fewer --fields or a lower --max-fh"
)


def _positive_float(text: str) -> float:
    value = float(text)
    if not value > 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {text}")
    return value


def _hour(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"a forecast hour is >= 0, got {text}")
    return value


def _add_hours_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--hours",
        nargs="+",
        type=_hour,
        default=None,
        metavar="FH",
        help="exactly these forecast hours (e.g. --hours 24 25), instead of 0..--max-fh",
    )


def _add_save_listings_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--save-listings",
        type=Path,
        default=None,
        metavar="DIR",
        help="also save every ListObjectsV2 answer as received, one XML file per page",
    )


def _listing_saver(folder: Path) -> Callable[[str, bytes], None]:
    """A :attr:`.bucket.HrrrBucket.listing_sink` that saves each page as received.

    ``hrrr.20240114/conus/hrrr.t12z.wrfsfcf`` becomes ``hrrr.20240114_conus_hrrr.t12z.wrfsfcf.xml``;
    a prefix answered again (a second listing, or a continuation page) gets ``.2.xml``,
    ``.3.xml`` and so on. For recorded test fixtures.
    """
    folder.mkdir(parents=True, exist_ok=True)
    answers: Counter[str] = Counter()

    def save(prefix: str, body: bytes) -> None:
        name = prefix.strip("/").replace("/", "_") or "root"
        answers[name] += 1
        suffix = "" if answers[name] == 1 else f".{answers[name]}"
        (folder / f"{name}{suffix}.xml").write_bytes(body)

    return save


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
            f"SNOW_GLOBE_FORECAST_MAX_MB, else {DEFAULT_CLI_MAX_MB:.0f})"
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
    parser = argparse.ArgumentParser(prog="snow-globe-forecast", description=__doc__.split("\n")[0])
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
    _add_hours_option(run)
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

    tick = sub.add_parser(
        "tick", help="one call of the Lambda handler with the SNOW_GLOBE_FORECAST_* environment"
    )
    tick.add_argument(
        "--uncapped",
        action="store_true",
        help="run even without SNOW_GLOBE_FORECAST_MAX_MB (the handler then has no per-run cap)",
    )
    tick.add_argument("--json", action="store_true", help="print the handler's summary as JSON")

    status = sub.add_parser("status", help="what is written, the lag log, and what is on S3")
    status.add_argument("--offline", action="store_true", help="do not contact the bucket")
    status.add_argument("--hours", type=int, default=6, help="runs to list on S3 (online)")
    status.add_argument("--json", action="store_true")

    plan = sub.add_parser("plan", help="bytes per field per hour, from the inventories only")
    plan.add_argument("run_id")
    plan.add_argument("--max-fh", type=int, default=None)
    _add_hours_option(plan)
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
        "lag-survey",
        help="publication lags of past runs from S3 listings (no GRIB); --log logs them",
    )
    survey.add_argument("--start", required=True, help="first run, YYYYMMDDHH")
    survey.add_argument(
        "--end",
        default=None,
        help="last run, YYYYMMDDHH (inclusive; default: the newest complete run)",
    )
    survey.add_argument(
        "--lookback-h",
        type=int,
        default=8,
        help="without --end: how many hours back to look for the newest complete run",
    )
    survey.add_argument(
        "--pause-s", type=float, default=0.2, help="pause between listings (seconds)"
    )
    survey.add_argument(
        "--log",
        action="store_true",
        help=(
            "log every listed hour not logged yet (live or survey) as a survey lag event in "
            "forecast_log/lag_survey/<today>.jsonl; without it nothing is written"
        ),
    )
    _add_save_listings_option(survey)

    check = sub.add_parser(
        "lag-check", help="chosen runs' S3 stamps next to the lag model (listings only, no writes)"
    )
    check.add_argument("run_ids", nargs="+", metavar="YYYYMMDDHH")
    _add_save_listings_option(check)

    from .hindcast.cli import add_parser as add_hindcast_parser

    add_hindcast_parser(sub)
    return parser


def _env(name: str) -> str | None:
    """``name`` (a ``SNOW_GLOBE_*`` variable, else its deprecated ``LOOKOUT_*`` form), stripped."""
    value = (env_get(name) or "").strip()
    return value or None


def _options(args: argparse.Namespace, *, allow_incomplete: bool = False) -> ExtractOptions:
    """Flags win; without a flag, the Lambda's environment variables apply here too.

    The per-run cap falls back to :data:`DEFAULT_CLI_MAX_MB`, so a laptop run is never
    uncapped. ``--hours`` (``run`` only) replaces ``--max-fh`` and the environment's
    ``SNOW_GLOBE_FORECAST_MAX_FH``; giving it with ``--max-fh`` is an error.
    """
    fields = args.fields
    if fields is None and _env("SNOW_GLOBE_FORECAST_FIELDS"):
        fields = [_env("SNOW_GLOBE_FORECAST_FIELDS")]
    hours = getattr(args, "hours", None)
    max_fh = args.max_fh
    if hours is not None and max_fh is not None:
        raise ValueError("give --hours or --max-fh, not both")
    if hours is None and max_fh is None and _env("SNOW_GLOBE_FORECAST_MAX_FH"):
        max_fh = int(_env("SNOW_GLOBE_FORECAST_MAX_FH") or 0)
    max_mb = args.max_mb
    raw_cap = _env("SNOW_GLOBE_FORECAST_MAX_MB")
    if max_mb is None and raw_cap:
        max_mb = float(raw_cap)
        if not max_mb > 0:
            raise ValueError(f"SNOW_GLOBE_FORECAST_MAX_MB must be > 0, got {raw_cap!r}")
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
        hours=tuple(hours) if hours is not None else None,
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


def _nothing_written(result: RunResult) -> str:
    reasons = "; ".join(f"{vp}: {why}" for vp, why in sorted(result.skipped.items()))
    return (
        f"run {result.run_id}: nothing to write, nothing downloaded ({reasons}); "
        "use --force to rewrite"
    )


def _extract(
    extractor: Extractor,
    run_start: Any,
    opts: ExtractOptions,
    as_json: bool,
    *,
    listing: RunListing | None = None,
) -> int:
    try:
        if listing is None:
            result = extractor.extract(run_start, opts)
        else:  # a listing of this run made moments ago: it is not listed again
            result = extractor.extract(run_start, opts, listing=listing)
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
    elif not result.hours:
        print(_nothing_written(result))
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
    hours = requested_hours(listing.run_start, opts)
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
    return _extract(extractor, listing.run_start, opts, args.json, listing=listing)


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
        hours = requested_hours(current, opts)
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
        if not result.hours:  # every file holds fields or hours this request lacks
            results.append({"run": run_id, "status": f"kept: {result.skipped}"})
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


def _by_method(counts: Any) -> dict[str, int]:
    return {m: int((counts or {}).get(m, 0)) for m in METHODS}


def _cmd_status(args: argparse.Namespace, storage: Storage, bucket: HrrrBucket | None) -> int:
    """What is written, the lag log (counted by method) and the lag model, plus S3 if online."""
    manifests = load_manifests()
    files: dict[str, Any] = {}
    for vp_id in sorted(manifests):
        keys = [
            k
            for k in storage.list_prefix(f"forecast/{STORAGE_SOURCE}/{vp_id}/")
            if k.endswith("z.json")
        ]
        files[vp_id] = {"files": len(keys), "latest": keys[-1].rsplit("/", 1)[-1] if keys else None}
    lag_log = read_lag_log(storage)
    model = LagModel.from_events(lag_log.events)
    live_model = LagModel.from_events(lag_log.events, methods={METHOD_LIVE})
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
            "pooled_samples_by_method": _by_method(model.pooled_samples_by_method.get(fh)),
            "cycles": len(rows),
            "cycles_with_min_samples": sum(r[2] >= model.min_samples for r in rows),
        }
    out: dict[str, Any] = {
        "now_utc": isoformat_z(utcnow()),
        "storage": repr(storage),
        "forecast_files": files,
        # One per (run, hour): a run re-extracted after a field-set change is counted once.
        "lag_events": len(lag_log.events),
        "lag_log": lag_log.summary(),
        "availability_model": lag_table,
        "lag_model_cells": model.cell_summary(),
        "lag_model_cells_live_only": live_model.cell_summary(),
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
        summary = out["lag_log"]
        by_method = ", ".join(f"{m} {n}" for m, n in summary["by_method"].items())
        dropped = ", ".join(f"{m} {n}" for m, n in summary["duplicates_dropped"].items())
        print(
            f"lag events: {summary['events']} ({by_method}; duplicates dropped: {dropped}; "
            f"invalid {summary['invalid']}); modelled lag over the cycles of each hour:"
        )
        for fh, row in lag_table.items():
            methods = ", ".join(f"{m} x{n}" for m, n in sorted(row["methods"].items()))
            print(
                f"  {fh}: {row['lag_min_lo']:6.1f}-{row['lag_min_hi']:6.1f} min; "
                f"{row['pooled_samples']} lag(s) logged; {methods}"
            )
        cells = out["lag_model_cells"]
        print(
            f"cells (cycle hour, forecast hour): {cells['left_fallback']} of {cells['cells']} "
            f"have left the {cells['fallback_lag_min']:.0f} min fallback "
            f"({out['lag_model_cells_live_only']['left_fallback']} on live lags alone)"
        )
        for row in out.get("bucket_runs", []):
            print(
                f"  run {row['run']}: {row['on_s3']} on S3, complete={row['complete']}, "
                f"written for {len(row['written_viewpoints'])} viewpoint(s)"
            )
    return EXIT_OK


def _cmd_plan(args: argparse.Namespace, bucket: HrrrBucket) -> int:
    result = plan_run(
        bucket,
        parse_run_id(args.run_id),
        fields=resolve_fields(args.fields),
        max_fh=args.max_fh,
        hours=args.hours,
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


def _tick_lines(summary: dict[str, Any]) -> str:
    transfer = summary.get("transfer", {})
    requests = ", ".join(f"{n} {k}" for k, n in sorted(transfer.get("requests", {}).items()))
    rows = summary.get("runs", [])
    lines = [
        f"tick {summary.get('now_utc')}: extracted {summary.get('extracted')} run(s); "
        f"downloaded {transfer.get('total_bytes', 0) / 1e6:.2f} MB"
        + (f" in {requests} request(s)" if requests else "")
    ]
    for row in rows:
        if row.get("status") == "written":
            continue
        extra = (
            f", {row['hours']} hour(s), {row['files']} file(s), "
            f"{row['bytes'] / 1e6:.2f} MB, {row['seconds']} s"
            if "files" in row
            else ""
        )
        lines.append(f"  run {row['run']}: {row['status']}{extra}")
    written = sum(row.get("status") == "written" for row in rows)
    lines.append(f"  {written} run(s) already written")
    return "\n".join(lines)


def _cmd_tick(args: argparse.Namespace) -> int:
    """One call of :func:`.lambda_handler.handler`, the call the scheduled Lambda makes.

    Nothing is changed on the way: the handler reads ``SNOW_GLOBE_FORECAST_FIELDS``, ``_MAX_FH``,
    ``_MAX_MB``, ``_LOOKBACK_H``, ``_STALE_H``, ``_MAX_RUNS``, ``_WORKERS``, ``_RESERVE_S``,
    ``_TIMEOUT_S`` and the storage variables itself, with no Lambda context (so its time
    budget comes from ``SNOW_GLOBE_FORECAST_TIMEOUT_S``). The only thing added is a refusal to
    start without ``SNOW_GLOBE_FORECAST_MAX_MB`` unless ``--uncapped``: the handler has no
    per-run cap of its own, and a laptop must keep NOAA downloads small (CLAUDE.md).

    The exit status says whether a run failed in this call (:func:`_tick_exit`), so a loop
    that calls ``tick`` can tell without parsing the summary.
    """
    if not _env("SNOW_GLOBE_FORECAST_MAX_MB") and not args.uncapped:
        print(
            "tick: SNOW_GLOBE_FORECAST_MAX_MB is not set, so the handler would extract whole runs "
            "with no per-run cap (all 22 fields cost about 450 MB for an 18 h run); set it "
            "(the local stand-in uses 40) or pass --uncapped",
            file=sys.stderr,
        )
        return EXIT_ERROR
    from . import lambda_handler

    summary = lambda_handler.handler({}, None)
    if args.json:
        _print(summary)
    else:
        print(_tick_lines(summary))
    return _tick_exit(summary)


def _tick_exit(summary: dict[str, Any]) -> int:
    """``tick``'s exit status: 0 unless a run of this call failed (an ``error:`` row).

    :data:`EXIT_CAP` when every failure was a cap refusal (``BandwidthCapError``), else
    :data:`EXIT_ERROR`. Runs that are waiting, deferred or backing off from an earlier
    failure are not failures of this call.
    """
    errors = [
        str(row.get("status", ""))
        for row in summary.get("runs", [])
        if str(row.get("status", "")).startswith("error:")
    ]
    if not errors:
        return EXIT_OK
    if all(status.startswith("error: BandwidthCapError") for status in errors):
        return EXIT_CAP
    return EXIT_ERROR


def _cmd_lag_survey(args: argparse.Namespace, bucket: HrrrBucket) -> int:
    """``lag-survey``: list past runs (no GRIB); with ``--log``, log the hours not logged yet."""
    from .evidence import lag_survey

    start = parse_run_id(args.start)
    before = bucket.ledger.snapshot()
    searched: dict[Any, RunListing] = {}
    if args.end is not None:
        end, end_rule = parse_run_id(args.end), "given"
    else:
        latest = find_latest_complete(bucket, lookback_h=args.lookback_h)
        if latest is None:
            print(f"no complete run in the last {args.lookback_h} h; give --end", file=sys.stderr)
            return EXIT_NOT_READY
        end, end_rule = latest.run_start, "the newest complete run"
        searched[latest.run_start] = latest  # the survey re-uses it instead of listing it again
    if end < start:
        print("--end is before --start", file=sys.stderr)
        return EXIT_ERROR
    if not args.log:
        out = lag_survey(bucket, start, end, pause_s=args.pause_s, listings=searched)
    else:
        storage = storage_from_env()
        try:
            with storage.lock(SURVEY_LOCK, timeout_s=5.0, ttl_s=6 * 3600.0):
                cells_before = LagModel.from_storage(storage).cell_summary()
                logger = SurveyLogger(storage, since=start)
                out = lag_survey(
                    bucket, start, end, pause_s=args.pause_s, logger=logger, listings=searched
                )
                out["lag_model_cells"] = {
                    "before": cells_before,
                    "after": LagModel.from_storage(storage).cell_summary(),
                }
        except LockTimeoutError as exc:
            print(f"busy: another logging survey holds the lock ({exc})", file=sys.stderr)
            return EXIT_NOT_READY
    out["end_rule"] = end_rule
    # ``transfer`` is the survey's own listings; this adds the newest-complete search.
    out["transfer_with_end_search"] = bucket.ledger.since(before)
    _print(out)
    return EXIT_OK


def _cmd_lag_check(args: argparse.Namespace, bucket: HrrrBucket) -> int:
    """``lag-check``: each run's S3-stamp lags next to the lag model (live + survey, live only)."""
    from .evidence import lag_check

    runs = [parse_run_id(run_id) for run_id in args.run_ids]
    events = read_lag_log(storage_from_env()).events
    _print(
        lag_check(
            bucket,
            runs,
            LagModel.from_events(events),
            live_model=LagModel.from_events(events, methods={METHOD_LIVE}),
        )
    )
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of the ``snow-globe-forecast`` console script."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        if args.command == "hindcast":
            from .hindcast.cli import main as hindcast_main

            return hindcast_main(args)  # its own storage root and bucket client
        if args.command == "tick":
            return _cmd_tick(args)  # the handler opens its own bucket client and storage
        if args.command == "status" and args.offline:
            return _cmd_status(args, storage_from_env(), None)
        with HrrrBucket() as bucket:
            if getattr(args, "save_listings", None) is not None:
                bucket.listing_sink = _listing_saver(args.save_listings)
            if args.command == "plan":
                return _cmd_plan(args, bucket)
            if args.command == "lag-survey":
                return _cmd_lag_survey(args, bucket)
            if args.command == "lag-check":
                return _cmd_lag_check(args, bucket)
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
