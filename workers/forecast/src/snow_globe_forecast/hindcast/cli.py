"""``snow-globe-forecast hindcast``: P1's 24 h-lead hindcast, design P's needs (PRD §13.2, §13.4).

- ``plan``: pair every captured daytime frame with the run a 24 h lead allows (``--lead-h``),
  and plan the download from the ``.idx`` byte ranges (listings and inventories only, no
  GRIB). Writes ``plan/p1-lead<lead>h.json`` (``plan/p1-lead24h.json`` for P1; a copy under
  ``plan/history/``) in the hindcast root. Exits 4 when what is left of the cap cannot cover
  the runs not written yet.
- ``fetch``: fetch the current plan at ``--lead-h`` (Rainier only), resumable, at most
  ``--max-total-mb`` (2,000) in all.
- ``compare``: S3 availability against the live lag model, hour by hour
  (``reports/availability-compare.json``).
- ``leads``: design P's hours per run and MB per day at leads 1, 3, 6, 12, 24, 48 h, priced at
  three field profiles (``reports/design-p-leads.json``); fetches a few ``.idx`` files.
- ``status``: what the hindcast holds (offline).

``plan``, ``fetch``, ``compare`` and ``leads`` take one lock (:data:`COMMAND_LOCK`), so no two
run at once: the hindcast sends one request at a time, and its download ledger has one
writer. Every request waits out the quiet window (``--quiet-start-minute`` 50 to
``--quiet-end-minute`` 5 past each hour by default) and is refused once the hindcast's whole
download has reached ``--max-total-mb``.

Everything is written under ``<data dir>/forecast/hindcast/``; the live data dir (frames,
thumbs, forecast files, ``forecast_log``) is only read.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from snow_globe_common.manifest import load_manifests
from snow_globe_common.paths import data_dir
from snow_globe_common.storage import LocalStorage, LockTimeoutError
from snow_globe_common.timeutil import isoformat_z, utcnow

from ..availability import LagModel
from ..extract import BandwidthCapError, plan_run
from ..fields import resolve_fields
from ..hrrr import SYNOPTIC_HOURS, floor_hour, format_run_id, parse_run_id
from ..laglog import read_lag_events
from .cachebucket import CachingBucket
from .compare import COMPARE_KEY, compare_availability
from .extractor import HindcastExtractor, live_lag_model
from .fetch import PROGRESS_KEY, covered_fn, fetch_plan, log_event
from .frames import SEALED_FIRST_DAY, SEALED_LAST_DAY, collect_frames
from .leads import (
    DESIGN_P_LEADS_H,
    FIELD_PROFILE_NOTES,
    FIELD_PROFILES,
    daytime_valid_times,
    hours_for_leads,
    lag_scenarios,
    mb_per_day,
    price_tables,
    valid_times_every,
)
from .plan import DEFAULT_CAP_MB, LIVE_FIELD_NAMES, P1_LEAD_H, build_plan, plan_key
from .quiet import DEFAULT_END_MINUTE, DEFAULT_START_MINUTE, QuietWindow
from .storage import HindcastStorage, ReadOnlyStorage

__all__ = ["COMMAND_LOCK", "LEADS_KEY", "add_parser", "main"]

RAINIER = "rainier.paradise-mountain"
LEADS_KEY = "reports/design-p-leads.json"
#: Held by every command but ``status``, so no two run at once.
COMMAND_LOCK = "hindcast-command"
#: How long a command waits for :data:`COMMAND_LOCK` before giving up.
COMMAND_LOCK_TIMEOUT_S = 5.0
EXIT_OK, EXIT_ERROR, EXIT_CAP = 0, 1, 4
#: The field profile whose prices keep the report's original keys (``bytes``, ``mb_per_day``).
DEFAULT_PROFILE = "standin-8"


def _add_quiet_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--quiet-start-minute",
        type=int,
        default=DEFAULT_START_MINUTE,
        help="no request from this minute past each hour (default 50)",
    )
    parser.add_argument(
        "--quiet-end-minute",
        type=int,
        default=DEFAULT_END_MINUTE,
        help="until this minute past the next hour (default 5)",
    )


def add_parser(sub: Any) -> None:
    """Register ``hindcast`` and its subcommands on the main parser's subparsers."""
    parser = sub.add_parser(
        "hindcast", help="P1's 24 h-lead hindcast (plan, fetch, compare) and design P's needs"
    )
    hsub = parser.add_subparsers(dest="hindcast_command", required=True)
    plan = hsub.add_parser("plan", help="pair daytime frames with runs; plan bytes (no GRIB)")
    plan.add_argument("--viewpoint", default=RAINIER)
    plan.add_argument("--lead-h", type=float, default=P1_LEAD_H)
    plan.add_argument("--max-total-mb", type=float, default=DEFAULT_CAP_MB)
    plan.add_argument("--json", action="store_true")
    _add_quiet_options(plan)
    fetch = hsub.add_parser("fetch", help="fetch the current plan (resumable, capped)")
    fetch.add_argument("--lead-h", type=float, default=P1_LEAD_H, help="which plan (default 24)")
    fetch.add_argument("--max-total-mb", type=float, default=DEFAULT_CAP_MB)
    fetch.add_argument("--max-errors", type=int, default=4)
    fetch.add_argument("--json", action="store_true")
    _add_quiet_options(fetch)
    compare = hsub.add_parser("compare", help="S3 availability against the live lag model")
    compare.add_argument("--viewpoint", default=RAINIER)
    compare.add_argument("--json", action="store_true")
    leads = hsub.add_parser("leads", help="design P: forecast hours per run and MB per day")
    leads.add_argument("--leads", nargs="+", type=float, default=list(DESIGN_P_LEADS_H))
    leads.add_argument(
        "--sample-runs",
        nargs="+",
        default=None,
        metavar="YYYYMMDDHH",
        help="runs whose .idx price the hours (default: two synoptic and two other runs, "
        "the newest at least 26 h old)",
    )
    leads.add_argument("--step-min", type=float, default=5.0)
    leads.add_argument("--max-total-mb", type=float, default=DEFAULT_CAP_MB)
    leads.add_argument("--json", action="store_true")
    _add_quiet_options(leads)
    status = hsub.add_parser("status", help="what the hindcast holds (offline)")
    status.add_argument("--json", action="store_true")


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _quiet(hstorage: HindcastStorage, args: argparse.Namespace) -> QuietWindow:
    def logged(now: datetime, seconds: float) -> None:
        log_event(hstorage, "hindcast-quiet-window", now, seconds=round(seconds, 1))
        print(f"quiet window: pausing {seconds / 60:.1f} min from {isoformat_z(now, 'seconds')}")

    return QuietWindow(
        start_minute=args.quiet_start_minute, end_minute=args.quiet_end_minute, on_pause=logged
    )


def _bucket(hstorage: HindcastStorage, args: argparse.Namespace) -> CachingBucket:
    return CachingBucket(
        hstorage.root, quiet=_quiet(hstorage, args), cap_bytes=int(args.max_total_mb * 1e6)
    )


def _cap_refused(exc: BandwidthCapError) -> int:
    print(f"cap reached: {exc}")
    return EXIT_CAP


def _plan(args: argparse.Namespace, hstorage: HindcastStorage, live: ReadOnlyStorage) -> int:
    viewpoint = load_manifests()[args.viewpoint]
    frames = collect_frames(live, viewpoint)
    key = plan_key(args.lead_h)
    try:
        with _bucket(hstorage, args) as bucket:
            plan = build_plan(
                frames,
                bucket,
                lead_h=args.lead_h,
                cap_mb=args.max_total_mb,
                spent_bytes=int(bucket.stored_ledger()["total_bytes"]),
                covered=covered_fn(hstorage, viewpoint.id, LIVE_FIELD_NAMES),
            )
            plan["hindcast_network_total"] = bucket.stored_ledger()
    except BandwidthCapError as exc:
        return _cap_refused(exc)
    hstorage.root.write_json(key, plan)
    stamp = plan["created_utc"].replace("-", "").replace(":", "")
    name = key.removeprefix("plan/").removesuffix(".json")
    hstorage.root.write_json(f"plan/history/{name}-{stamp}.json", plan)
    log_event(
        hstorage,
        "hindcast-plan",
        utcnow(),
        plan=key,
        runs=[run["run"] for run in plan["runs"]],
        planned_bytes=plan["totals"]["planned_bytes"],
        pending_planned_bytes=plan["totals"]["pending_planned_bytes"],
        transfer=plan["transfer"],
    )
    if args.json:
        _print({k: v for k, v in plan.items() if k != "pairs"})
    else:
        f = plan["frames"]
        print(
            f"frames: {f['frames']} unique ({f['daytime']} daytime, Sun >= "
            f"{f['min_sun_alt_deg']:g} deg), {f['first_utc']} to {f['last_utc']}; "
            f"{plan['unpaired']} daytime frames unpaired"
        )
        for run in plan["runs"]:
            print(
                f"  {run['run']}  f{run['hours'][0]:02d}-f{run['hours'][-1]:02d} "
                f"({len(run['hours'])} h) for {run['frames']} frames: "
                f"{run['planned_bytes'] / 1e6:.1f} MB"
            )
        t = plan["totals"]
        print(
            f"total: {t['runs']} runs, {t['forecast_hours']} forecast hours, "
            f"{t['planned_mb']:.1f} MB planned, {t['pending_planned_bytes'] / 1e6:.1f} MB of it "
            f"not written yet; {t['hindcast_bytes_spent'] / 1e6:.1f} MB downloaded so far (cap "
            f"{t['cap_bytes'] / 1e6:.0f} MB, within: {t['within_cap']}); lead "
            f"{plan['lead_h_achieved']} h; this plan downloaded "
            f"{plan['transfer']['total_bytes']:,} bytes in "
            f"{sum(plan['transfer']['requests'].values())} requests; written to {key}"
        )
    return EXIT_OK if plan["totals"]["within_cap"] else EXIT_CAP


def _fetch(args: argparse.Namespace, hstorage: HindcastStorage, live: ReadOnlyStorage) -> int:
    key = plan_key(args.lead_h)
    plan = hstorage.root.read_json(key, default=None)
    if not isinstance(plan, Mapping):
        print(f"no plan at {key}: run `snow-globe-forecast hindcast plan` first")
        return EXIT_ERROR
    manifests = load_manifests()
    with _bucket(hstorage, args) as bucket:
        extractor = HindcastExtractor(
            hstorage,
            bucket,
            {plan["viewpoint_id"]: manifests[plan["viewpoint_id"]]},
            live_model=live_lag_model(live),
            live_storage=live,
        )
        report = fetch_plan(
            plan,
            storage=hstorage,
            bucket=bucket,
            extractor=extractor,
            cap_mb=args.max_total_mb,
            max_errors=args.max_errors,
        )
    report["plan_key"] = key
    hstorage.root.write_json("reports/fetch-last.json", report)
    if args.json:
        _print(report)
    else:
        t = report["transfer"]
        print(
            f"{report['status']}: {len(report['runs_written'])} runs written now, "
            f"{len(report['runs_already_written'])} already, {len(report['runs_left'])} left; "
            f"{t['total_bytes']:,} bytes in {sum(t['requests'].values())} requests "
            f"({t['requests']}), {t['retries']} retries, {len(report['errors'])} errors, "
            f"{report['seconds_wall']:.1f} s wall ({report['seconds_quiet_window']:.0f} s in "
            f"the quiet window)"
        )
        for item in report["runs_not_written"]:
            print(f"  not written: {item['run']} ({item['skipped']})")
    return {"done": EXIT_OK, "over-cap": EXIT_CAP, "cap-reached": EXIT_CAP}.get(
        str(report["status"]), EXIT_ERROR
    )


def _compare(args: argparse.Namespace, hstorage: HindcastStorage, live: ReadOnlyStorage) -> int:
    report = compare_availability(hstorage, live, args.viewpoint, now=utcnow())
    hstorage.root.write_json(COMPARE_KEY, report)
    if args.json:
        _print({k: v for k, v in report.items() if k != "rows"})
    else:
        for side in ("live_model_at_write", "live_model_now", "full_model_now"):
            s = report[side]
            print(
                f"{side}: {s['hours']} hours, {s['early']} early (by up to "
                f"{s['early_by_up_to_s']} s); margin model - S3 {s['margin_s']} s"
            )
        print(f"live log against S3: {report['logged_against_s3']}")
    return EXIT_OK


def _default_samples(now: datetime) -> list[datetime]:
    """Two synoptic and two other runs, the newest at least 26 h old (their listings are kept)."""
    newest = floor_hour(now) - timedelta(hours=26)
    synoptic: list[datetime] = []
    other: list[datetime] = []
    moment = newest
    while len(synoptic) < 2 or len(other) < 2:
        group = synoptic if moment.hour in SYNOPTIC_HOURS else other
        if len(group) < 2:
            group.append(moment)
        moment -= timedelta(hours=1)
    return sorted(synoptic + other)


def _sample_plans(
    bucket: CachingBucket, runs: Sequence[datetime]
) -> dict[datetime, dict[str, Any]]:
    """Each sample run planned for every profile's fields at once (listings and ``.idx`` only)."""
    names = list(dict.fromkeys(name for fields in FIELD_PROFILES.values() for name in fields))
    specs = resolve_fields(names)
    return {run: plan_run(bucket, run, fields=specs, workers=1) for run in runs}


def _leads(args: argparse.Namespace, hstorage: HindcastStorage, live: ReadOnlyStorage) -> int:
    now = utcnow()
    events = read_lag_events(live)
    model = LagModel.from_events(events)
    scenarios = lag_scenarios(events, model)
    leads = list(args.leads)
    day = datetime(2026, 12, 20, tzinfo=UTC)
    all_day = valid_times_every(day, day + timedelta(hours=23, minutes=55), args.step_min)
    msv = [vp for vp in load_manifests().values() if vp.is_msv]
    cameras = [(vp.camera.lat, vp.camera.lon) for vp in msv]
    daytime = daytime_valid_times(
        cameras, SEALED_FIRST_DAY, SEALED_LAST_DAY, step_min=args.step_min
    )
    estimates = {
        "all_valid_times": hours_for_leads(leads, scenarios, all_day),
        "daytime_at_design_p_cameras": hours_for_leads(leads, scenarios, daytime),
    }
    runs = (
        [parse_run_id(text) for text in args.sample_runs]
        if args.sample_runs
        else _default_samples(now)
    )
    try:
        with _bucket(hstorage, args) as bucket:
            before = bucket.ledger.snapshot()
            plans = _sample_plans(bucket, runs)
            transfer = bucket.ledger.since(before)
    except BandwidthCapError as exc:
        return _cap_refused(exc)
    standin = {f"{c:02d}": [0, 1, 2] for c in range(24)}
    profiles: dict[str, Any] = {}
    for profile, fields in FIELD_PROFILES.items():
        table, terrain, detail = price_tables(plans, fields)
        priced: dict[str, Any] = {}
        for name, estimate in estimates.items():
            need = estimate["hours_by_cycle"]
            with_standin = {
                cycle: sorted(set(need.get(cycle, [])) | set(standin[cycle])) for cycle in standin
            }
            priced[name] = {
                "design_p_only": mb_per_day(need, table, terrain),
                "with_the_standin_f00_f02": mb_per_day(with_standin, table, terrain),
            }
        profiles[profile] = {
            "fields": list(fields),
            "note": FIELD_PROFILE_NOTES[profile],
            "terrain_bytes_per_run": round(terrain),
            "mb_per_hour_synoptic": {str(fh): round(b / 1e6, 3) for fh, b in table[True].items()},
            "mb_per_hour_other": {str(fh): round(b / 1e6, 3) for fh, b in table[False].items()},
            **detail,
            "mb_per_day": priced,
        }
    base = profiles[DEFAULT_PROFILE]
    report = {
        "kind": "snow-globe.design-p-leads",
        "created_utc": isoformat_z(now, "seconds"),
        "leads_h": leads,
        "fields": list(LIVE_FIELD_NAMES),
        "fields_profile": DEFAULT_PROFILE,
        "fields_note": (
            "bytes and mb_per_day price the hours at the stand-in's 8 fields (D-050); "
            "field_profiles prices the same hours at each profile"
        ),
        "rule": (
            "lead = valid - availability (PRD 8.3, D-008); a live hour is available at its S3 "
            "stamp plus the worker's fetch delay (D-027); the run for (valid V, lead L) is "
            "select_run_for_issue(V - L, V): both hours bracketing V"
        ),
        "lag_log": {"events": len(events), "model_cells": model.cell_summary()},
        "sealed_window": [SEALED_FIRST_DAY.isoformat(), SEALED_LAST_DAY.isoformat()],
        "design_p_cameras": [vp.id for vp in msv],
        "estimates": estimates,
        "bytes": {
            "sample_runs": [format_run_id(r) for r in runs],
            "terrain_bytes_per_run": base["terrain_bytes_per_run"],
            "mb_per_hour_synoptic": base["mb_per_hour_synoptic"],
            "mb_per_hour_other": base["mb_per_hour_other"],
            "runs": {
                format_run_id(run): {
                    "hours": len(sizes["hours"]),
                    "total_bytes": base["run_total_bytes"][format_run_id(run)],
                    "missing_hours": sizes["missing_hours"],
                }
                for run, sizes in sorted(plans.items())
            },
            "spread": base["spread"],
            "transfer": transfer,
        },
        "mb_per_day": base["mb_per_day"],
        "field_profiles": profiles,
    }
    hstorage.root.write_json(LEADS_KEY, report)
    if args.json:
        _print(report)
    else:
        for name, estimate in estimates.items():
            print(f"{name}: {estimate['valid_times']} valid times")
            for lead, part in estimate["per_lead"].items():
                hours = sorted({h for hs in part["hours_by_cycle"].values() for h in hs})
                print(
                    f"  {lead:>3} h: f{hours[0]:02d}-f{hours[-1]:02d} over "
                    f"{len(part['hours_by_cycle'])} cycles, actual lead {part['lead_h_actual']}"
                    if hours
                    else f"  {lead:>3} h: never served ({part['valid_times_unserved']} of "
                    f"{part['of']})"
                )
            for profile, priced_profile in profiles.items():
                p = priced_profile["mb_per_day"][name]
                print(
                    f"  MB/day at {profile}: {p['design_p_only']['mb_per_day']} (design P "
                    f"alone), {p['with_the_standin_f00_f02']['mb_per_day']} (with the "
                    f"stand-in's f00-f02)"
                )
    return EXIT_OK


def _status(args: argparse.Namespace, hstorage: HindcastStorage) -> int:
    files = [k for k in hstorage.list_prefix("forecast/hrrr/") if k.endswith("z.json")]
    plans = {}
    for key in hstorage.root.list_prefix("plan/"):
        if key.startswith("plan/history/") or not key.endswith(".json"):
            continue
        plan = hstorage.root.read_json(key, default=None)
        if isinstance(plan, Mapping):
            plans[key] = {"created_utc": plan.get("created_utc"), "totals": plan.get("totals")}
    progress = hstorage.root.read_json(PROGRESS_KEY, default=None)
    ledger = hstorage.root.read_json("state/network-ledger.json", default=None)
    out = {
        "files": files,
        "plans": plans,
        "progress_runs": sorted((progress or {}).get("runs", {})),
        "errors": len((progress or {}).get("errors", [])),
        "network_total": ledger,
    }
    _print(out)
    return EXIT_OK


def main(args: argparse.Namespace) -> int:
    """Run one ``hindcast`` subcommand (the main CLI hands over its parsed arguments)."""
    root = data_dir()
    hstorage = HindcastStorage.at(root)
    live = ReadOnlyStorage(LocalStorage(root))
    command = args.hindcast_command
    if command == "status":
        return _status(args, hstorage)
    try:
        with hstorage.lock(COMMAND_LOCK, timeout_s=COMMAND_LOCK_TIMEOUT_S, ttl_s=3600.0):
            if command == "plan":
                return _plan(args, hstorage, live)
            if command == "fetch":
                return _fetch(args, hstorage, live)
            if command == "compare":
                return _compare(args, hstorage, live)
            if command == "leads":
                return _leads(args, hstorage, live)
    except LockTimeoutError:
        print("another hindcast command is running")
        return EXIT_ERROR
    return EXIT_ERROR  # pragma: no cover - argparse rejects unknown commands
