"""Console script ``snow-globe-capture`` (PRD §11.2 jobs "Capture" and "Archive backfill").

Live capture (polls NPS):

    snow-globe-capture once [--only <viewpoint_id>]...
    snow-globe-capture loop [--interval 300] [--jitter 30] [--archive-queue PATH]
                            [--archive-budget 1000] [--archive-total-gb 5]

Archive (one request at a time, >= 5 s apart, resumable, size-capped, held-out guard):

    snow-globe-capture archive days <viewpoint_id>
    snow-globe-capture archive day <viewpoint_id> <YYYY-MM-DD> [--daytime-only] [--hires]
                                   [--dry-run]
    snow-globe-capture archive sample <viewpoint_id> <YYYY-MM-DD>... [--pick midday|hourly|all]
    snow-globe-capture archive-check [--viewpoint <id>]... [--rainier-page] [--budget 220]

No network (never contact nps.gov, never take the fetcher lock, safe beside a running loop):

    snow-globe-capture status [--json] [--archive-queue PATH]
    snow-globe-capture archive queue add <viewpoint_id> <YYYY-MM-DD>... [--hires] [--pick hourly]
    snow-globe-capture archive queue list | remove <id>... [--finished] [--all]
    snow-globe-capture archive queue resume <id>... [--all] [--max-gb G]
    snow-globe-capture cadence <viewpoint_id> [--hours 24] [--json]
    snow-globe-capture storage [--day YYYY-MM-DD] [--json]      (alias: storage-report)

Every network command runs inside the host-wide NPS-fetcher lock (:mod:`.fetcher_guard`): a
second fetcher, or a running Rainier stopgap, makes it exit with status 3 before anything
is sent (CLAUDE.md: never run two NPS fetchers at once). ``loop --archive-queue`` does the
queued archive backfill in the same process, between live passes (D-023,
:mod:`.archive_queue`). Archived frames dated on or after 1 Jun 2026 are refused (D-019)
unless ``archive day|sample --held-out-override "<reason>"`` is given, which only scoring
may do; it is logged. The queue never takes an override.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from snow_globe_common.manifest import Viewpoint, load_manifests
from snow_globe_common.storage import LocalStorage, Storage, storage_from_env
from snow_globe_common.timeutil import utcnow

__all__ = ["EXIT_FETCHER_BUSY", "build_parser", "main"]

#: Exit status when another NPS fetcher (or the stopgap) is active; nothing was sent.
EXIT_FETCHER_BUSY = 3


def _print(obj: Any, as_json: bool = True) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
    else:
        print(obj)


def _viewpoint(viewpoints: dict[str, Viewpoint], viewpoint_id: str) -> Viewpoint:
    try:
        return viewpoints[viewpoint_id]
    except KeyError:
        raise SystemExit(
            f"unknown viewpoint {viewpoint_id!r}; known: {', '.join(viewpoints)}"
        ) from None


def _archive_viewpoint(viewpoints: dict[str, Viewpoint], viewpoint_id: str) -> Viewpoint:
    vp = _viewpoint(viewpoints, viewpoint_id)
    if vp.truth_camera.archive_code is None:
        raise SystemExit(f"{viewpoint_id} has no NPS archive (CLAUDE.md viewpoint table)")
    return vp


# --- live --------------------------------------------------------------------------------


def _cmd_once(args: argparse.Namespace, storage: Storage, viewpoints: dict[str, Viewpoint]) -> int:
    from .live import CaptureRunner

    selected = _select(viewpoints, args.only)
    with CaptureRunner.default(storage, runner="cli-once") as runner:
        summary = runner.run_pass(selected, wait_for_due_s=args.wait_for_due)
    _print(summary)
    return 0 if summary.get("errors", 0) == 0 else 2


def _cmd_loop(args: argparse.Namespace, storage: Storage, viewpoints: dict[str, Viewpoint]) -> int:
    from .live import CaptureRunner

    selected = _select(viewpoints, args.only)
    guard = getattr(args, "guard", None)
    queue_runner = None
    if args.archive_queue:
        from .archive_queue import (
            DEFAULT_DAILY_BUDGET,
            DEFAULT_TOTAL_CAP_GB,
            ArchiveQueue,
            QueueRunner,
        )

        total_gb = DEFAULT_TOTAL_CAP_GB if args.archive_total_gb is None else args.archive_total_gb
        queue_runner = QueueRunner(
            storage,
            viewpoints,
            ArchiveQueue(_queue_path(args)),
            daily_budget=DEFAULT_DAILY_BUDGET
            if args.archive_budget is None
            else args.archive_budget,
            total_cap_gb=total_gb if total_gb > 0 else None,
        )
    try:
        with CaptureRunner.default(storage, runner="cli-loop") as runner:
            runner.loop(
                selected,
                interval_s=args.interval,
                jitter_s=args.jitter,
                max_passes=args.max_passes,
                before_pass=guard.check_foreign if guard is not None else None,
                idle=queue_runner.run_idle if queue_runner is not None else None,
            )
    finally:
        if queue_runner is not None:
            queue_runner.close()
    return 0


def _queue_path(args: argparse.Namespace) -> Path:
    """The queue file: as given, else ``state/archive/queue.json``; never in git or iCloud."""
    from snow_globe_common.paths import UnsafeDataDirError, check_data_dir

    from .archive_queue import default_queue_path

    given = getattr(args, "archive_queue", None) or getattr(args, "queue", None)
    path = Path(given).expanduser() if given else default_queue_path()
    try:  # D-017: data, the queue included, stays outside the repo and synced folders
        check_data_dir(path.parent)
    except UnsafeDataDirError as exc:
        raise SystemExit(f"refused queue file {path}: {exc}") from None
    return path.resolve()


def _cmd_status(
    args: argparse.Namespace, storage: Storage, viewpoints: dict[str, Viewpoint]
) -> int:
    from .archive_queue import ArchiveQueue, queue_summary
    from .live import status_report

    report = status_report(storage, list(viewpoints.values()), now=utcnow())
    queue_path = _queue_path(args)
    if queue_path.exists() or args.archive_queue:
        report["archive_queue"] = queue_summary(ArchiveQueue(queue_path))
    if args.json:
        _print(report)
        return 0
    print(f"capture status at {report['now']}  (storage: {report['storage']})")
    header = (
        f"{'viewpoint':<28} {'last poll':<21} {'last image (UTC)':<21} {'age':>7} "
        f"{'GETs/h':>6} {'fail':>4} {'policy':<14} {'staged':>6}  next poll"
    )
    print(header)
    switching = []
    for row in report["cameras"]:
        policy = row.get("frame_policy_recorded") or row.get("frame_policy") or "-"
        if row.get("frame_policy_recorded") not in (None, row.get("frame_policy")):
            policy += "*"
            switching.append(row["viewpoint_id"])
        print(
            f"{row['viewpoint_id']:<28} {row.get('last_poll_at') or '-':<21} "
            f"{row.get('last_image_time_utc') or '-':<21} "
            f"{_dash(row.get('last_image_age_min')):>7} "
            f"{row.get('gets_last_hour', 0):>6} {row.get('failures', 0):>4} "
            f"{policy:<14} {row.get('staged', 0):>6}  "
            f"{row.get('next_poll_at') or 'now'}"
        )
    if switching:
        print(
            "* policy: what the newest frame's writer did; this code would use "
            + ", ".join(
                f"{r['viewpoint_id']}={r['frame_policy']}"
                for r in report["cameras"]
                if r["viewpoint_id"] in switching
            )
        )
    bounded = [
        f"{r['viewpoint_id']} ({r['last_answer_bound']})"
        for r in report["cameras"]
        if r.get("last_answer_bound")
    ]
    if bounded:
        print("next poll counted from an answer bound, not a recorded answer:", ", ".join(bounded))
    legacy = [
        r["viewpoint_id"]
        for r in report["cameras"]
        if str(r.get("next_poll_basis") or "").startswith("legacy_state")
    ]
    if legacy:
        print(
            "no answer time recorded (state from older code): next poll shown is what this code "
            "would do if it took over now; the older code polls ~301 s after its own send:",
            ", ".join(legacy),
        )
    print("today's capture_log events:", json.dumps(report.get("events_today", {})))
    queue = report.get("archive_queue")
    if queue:
        _print_queue(queue)
    return 0


def _print_queue(summary: dict[str, Any]) -> None:
    print(f"archive queue {summary['path']}: {json.dumps(summary.get('by_status', {}))}")
    if summary.get("error"):
        print(f"  unreadable: {summary['error']}")
    runner = summary.get("runner") or {}
    if runner:
        print(
            f"  runner pid {runner.get('pid')} at {runner.get('updated_at')}, budget "
            f"{json.dumps(runner.get('budget'))}"
        )
    for row in summary.get("rows", []):
        pairs = row.get("pairs")
        print(
            f"  {row['status']:<8} {row['id']:<70} {_dash(row.get('next'))}/{_dash(pairs)} "
            f"{json.dumps(row.get('outcomes', {}))} {row.get('bytes', 0)} B "
            f"{row.get('requests', 0)} req {row.get('reason') or ''}"
        )


def _dash(value: Any) -> str:
    return "-" if value is None else str(value)


def _select(viewpoints: dict[str, Viewpoint], only: Sequence[str] | None) -> list[Viewpoint]:
    if not only:
        return list(viewpoints.values())
    return [_viewpoint(viewpoints, v) for v in only]


# --- archive ---------------------------------------------------------------------------


def _cmd_archive(
    args: argparse.Namespace, storage: Storage, viewpoints: dict[str, Viewpoint]
) -> int:
    from .archive import backfill_assets, cached_search, default_archive_client, pick_assets
    from .heldout import HELD_OUT_FROM, HeldOutOverride

    vp = _archive_viewpoint(viewpoints, args.viewpoint)
    code = vp.truth_camera.archive_code
    assert code is not None
    override: HeldOutOverride | None = None
    if args.archive_cmd != "days":
        if getattr(args, "held_out_override", None):
            try:
                override = HeldOutOverride(args.held_out_override)
            except ValueError as exc:
                raise SystemExit(str(exc)) from None
        late = [d for d in args.dates if date.fromisoformat(d) >= HELD_OUT_FROM]
        if late and override is None and not args.dry_run:
            raise SystemExit(
                f"refused: {', '.join(late)} is on or after {HELD_OUT_FROM.isoformat()}, the "
                "D-019 held-out guard. Archived frames from then are viewed only at scoring, "
                'with --held-out-override "<reason>" (logged). --dry-run reads metadata only.'
            )
    archive = default_archive_client(storage, runner="cli-archive")
    try:
        if args.archive_cmd == "days":
            days, _raw = archive.available_days(code)
            _print(
                {
                    "viewpoint_id": vp.id,
                    "code": code,
                    "min_day": days.min_day,
                    "max_day": days.max_day,
                    "days_per_year": {
                        str(y): days.days_in_year(y)
                        for y in range(days.min_day.year, days.max_day.year + 1)
                    },
                    "requests": archive.stats.to_dict(),
                }
            )
            return 0
        dates = [date.fromisoformat(d) for d in args.dates]
        kinds = ("hires",) if args.hires else ("thumbmedium",)
        if args.archive_cmd == "day" and len(dates) != 1:
            raise SystemExit("archive day takes exactly one date")
        pick = getattr(args, "pick", "all")
        min_sun = (
            args.min_sun_deg
            if args.min_sun_deg is not None
            else (0.0 if args.daytime_only else None)
        )
        max_bytes = None if args.max_gb is None else int(args.max_gb * 1e9)
        today = utcnow().date()
        results = []
        for day in dates:
            assets, meta = cached_search(storage, archive, code, day, today=today)
            chosen = pick_assets(assets, pick=pick, min_sun_deg=min_sun)
            result = backfill_assets(
                storage=storage,
                archive=archive,
                viewpoint=vp,
                assets=chosen,
                kinds=kinds,
                dry_run=args.dry_run,
                max_bytes=max_bytes,
                convention=args.convention,
                keep_original=args.keep_original,
                held_out_override=override,
            )
            row = result.to_dict()
            row.update(
                {
                    "date": day.isoformat(),
                    "assets_found": len(assets),
                    "assets_selected": len(chosen),
                    "search": meta,
                }
            )
            if args.dry_run:
                row["plan"] = result.plan
            row.pop("stored", None)
            results.append(row)
            if result.stopped:
                break
        _print({"results": results, "requests": archive.stats.to_dict()})
        return 0
    finally:
        archive.client.close()


#: ``archive queue add --through`` queues at most this many days at once.
MAX_QUEUE_RANGE_DAYS = 400


def _queue_days(dates: Sequence[str], through: str | None) -> list[date]:
    """The days to queue: the dates given, or every day from the one date through ``through``."""
    days = [date.fromisoformat(d) for d in dates]
    if through is None:
        return days
    if len(days) != 1:
        raise ValueError("--through takes exactly one start date")
    end = date.fromisoformat(through)
    count = (end - days[0]).days + 1
    if count < 1 or count > MAX_QUEUE_RANGE_DAYS:
        raise ValueError(
            f"--through must be 0-{MAX_QUEUE_RANGE_DAYS - 1} days after the start date"
        )
    return [date.fromordinal(days[0].toordinal() + i) for i in range(count)]


def _cmd_archive_queue(
    args: argparse.Namespace, storage: Storage, viewpoints: dict[str, Viewpoint]
) -> int:
    """``archive queue add|list|remove``: edit the queue file; never contacts nps.gov."""
    from .archive_queue import ArchiveQueue, make_entry, queue_summary

    queue = ArchiveQueue(_queue_path(args))
    if args.queue_cmd == "add":
        vp = _archive_viewpoint(viewpoints, args.viewpoint)
        kinds = [k for k, wanted in (("hires", args.hires), ("thumbmedium", args.thumbs)) if wanted]
        min_sun = (
            args.min_sun_deg
            if args.min_sun_deg is not None
            else (0.0 if args.daytime_only else None)
        )
        try:
            days = _queue_days(args.dates, args.through)
            entries = [
                make_entry(
                    vp,
                    day,
                    kinds=kinds or ["hires"],
                    pick=args.pick,
                    min_sun_deg=min_sun,
                    max_gb=args.max_gb,
                    convention=args.convention,
                )
                for day in days
            ]
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
        added, present = queue.add(entries)
        _print({"queue": str(queue.path), "added": added, "already_queued": present})
        return 0
    if args.queue_cmd == "remove":
        if not (args.ids or args.finished or args.all):
            raise SystemExit("archive queue remove: give ids, --finished or --all")
        removed = queue.remove(args.ids, finished=args.finished, everything=args.all)
        _print({"queue": str(queue.path), "removed": removed})
        return 0
    if args.queue_cmd == "resume":
        if not (args.ids or args.all):
            raise SystemExit("archive queue resume: give ids or --all")
        try:
            resumed = queue.resume(args.ids, every=args.all, max_gb=args.max_gb)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
        _print({"queue": str(queue.path), "resumed": resumed})
        return 0
    summary = queue_summary(queue)
    if args.json:
        _print(summary)
    else:
        _print_queue(summary)
    return 0


def _cmd_cadence(
    args: argparse.Namespace, storage: Storage, viewpoints: dict[str, Viewpoint]
) -> int:
    """``cadence``: the camera's update cadence from logs and sidecars on disk (no network)."""
    from .cadence import camera_cadence

    vp = _viewpoint(viewpoints, args.viewpoint)
    report = camera_cadence(storage, vp, now=utcnow(), hours=args.hours, max_gap_s=args.max_gap)
    if args.json:
        _print(report)
        return 0
    print(
        f"{vp.id} ({report['camera_kind']}) {report['window_utc'][0]} .. "
        f"{report['window_utc'][1]}: {report['polls']['total']} polls "
        f"{json.dumps(report['polls']['by_source'])}, spacing "
        f"{json.dumps(report['poll_spacing_s'])}, {report['gaps_over_max']} gaps over "
        f"{report['max_gap_s']:g} s"
    )
    for name, series in report["series"].items():
        stats = series["interval_s"]
        ages = series["age_at_poll_s"]
        print(
            f"  {name} ({series['what']}): {series['distinct_values']} distinct; pairs "
            f"{json.dumps(series['consecutive_pairs'])}; interval median {stats['median']} s, "
            f"p10 {stats['p10']} s, p90 {stats['p90']} s (n={stats['n']}, min {stats['min']}, "
            f"max {stats['max']}); histogram {json.dumps(series['histogram_10s'])}"
        )
        print(
            f"    age at poll: median {ages['median']} s, p90 {ages['p90']} s, min "
            f"{ages['min']} s, max {ages['max']} s (n={ages['n']}); 30 s bins "
            f"{json.dumps(series['age_histogram_30s'])}"
        )
        print(f"    => {series['cadence']['text']}")
    print(f"  note: {report['note']}")
    return 0


def _cmd_storage(
    args: argparse.Namespace, storage: Storage, viewpoints: dict[str, Viewpoint]
) -> int:
    """``storage``: bytes per frame and per day, old policy against PRD §8.7 (no network)."""
    from .storage_report import storage_report

    if not isinstance(storage, LocalStorage):
        raise SystemExit("storage reads files on local disk; unset SNOW_GLOBE_STORAGE=s3")
    day = date.fromisoformat(args.day) if args.day else utcnow().date()
    report = storage_report(storage, _select(viewpoints, args.only), day=day)
    if args.json:
        _print(report)
        return 0
    print(f"storage on {report['day']} (frames {report['frames_span_utc']})")
    for row in report["cameras"]:
        proj = row["projection"]
        print(
            f"  {row['viewpoint_id']:<28} {row['frame_policy']:<14} n={row['frames_on_disk']:<3} "
            f"frame {_dash(row['frame_bytes']['mean'])} B, thumb "
            f"{_dash(row['thumb_bytes_made_in_memory']['mean'])} B; per day old "
            f"{_dash(proj.get('old_bytes_per_day'))} B, "
            f"new {_dash(proj.get('new_bytes_per_day'))} B"
        )
    print(f"  totals per day: {json.dumps(report['totals_per_day'])}")
    return 0


def _cmd_archive_check(
    args: argparse.Namespace, storage: Storage, viewpoints: dict[str, Viewpoint]
) -> int:
    from .archive import RequestBudget, default_archive_client
    from .archive_check import DEFAULT_BUDGET, merge_summary, run_viewpoint_check
    from .rainier_page import check_rainier_page

    check_date = date.fromisoformat(args.check_date) if args.check_date else utcnow().date()
    budget = RequestBudget(
        storage, f"archive-check-{check_date.isoformat()}", args.budget or DEFAULT_BUDGET
    )
    selected = (
        [_archive_viewpoint(viewpoints, v) for v in args.viewpoint]
        if args.viewpoint
        else ([] if args.rainier_page else [v for v in viewpoints.values() if v.has_archive])
    )
    archive = default_archive_client(storage, budget=budget, runner="cli-archive-check")
    try:
        for vp in selected:
            archive.stats.__init__()  # per-viewpoint request counts
            result = run_viewpoint_check(
                storage=storage,
                archive=archive,
                viewpoint=vp,
                check_date=check_date,
                paging_probe=args.paging_probe,
            )
            doc = merge_summary(
                storage,
                check_date,
                viewpoint=result,
                budget_used=budget.used(),
                budget_limit=budget.limit,
            )
            print(
                json.dumps(
                    {
                        "viewpoint_id": vp.id,
                        "requests_this_run": result["requests_this_run"],
                        "budget_used": budget.used(),
                        "notes": result["notes"],
                        "summary": doc
                        and "archive/archive_check_" + check_date.isoformat() + ".json",
                    },
                    default=str,
                )
            )
        if args.rainier_page:
            archive.stats.__init__()
            finding = check_rainier_page(archive)
            finding["requests_this_run"] = archive.stats.to_dict()
            merge_summary(
                storage,
                check_date,
                extra={"rainier_park_camera": finding},
                run={
                    "viewpoint_id": "rainier-page",
                    "finished_at": utcnow().isoformat(),
                    "requests": finding["requests_this_run"],
                },
                budget_used=budget.used(),
                budget_limit=budget.limit,
            )
            _print({k: v for k, v in finding.items() if k != "links"})
        return 0
    finally:
        archive.client.close()


# --- parser ---------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The argument parser (also used by the tests)."""
    parser = argparse.ArgumentParser(
        prog="snow-globe-capture",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--manifests", help="viewpoint manifest folder (default: found)")
    sub = parser.add_subparsers(dest="command", required=True)

    once = sub.add_parser("once", help="run one capture pass over the manifest cameras")
    once.add_argument("--only", action="append", metavar="VIEWPOINT_ID")
    once.add_argument(
        "--wait-for-due",
        type=float,
        default=0.0,
        metavar="S",
        help="wait for a camera whose poll slot (its last answer + 5 min) comes within S seconds "
        "of the pass's start",
    )
    once.set_defaults(func=_cmd_once, nps_fetcher=True)

    loop = sub.add_parser("loop", help="capture forever, one pass per interval (Ctrl-C stops)")
    loop.add_argument("--interval", type=float, default=300.0)
    loop.add_argument("--jitter", type=float, default=30.0, help="extra random 0..J s per pass")
    loop.add_argument("--only", action="append", metavar="VIEWPOINT_ID")
    loop.add_argument("--max-passes", type=int, default=None, help=argparse.SUPPRESS)
    loop.add_argument(
        "--archive-queue",
        metavar="PATH",
        default=None,
        help="also work this archive queue between live passes, one request at a time "
        "(see 'archive queue'); default: no archive work",
    )
    loop.add_argument(
        "--archive-budget",
        type=int,
        default=None,
        metavar="N",
        help="archive requests the queue may send per UTC day (default 1000; 0 = no budget)",
    )
    loop.add_argument(
        "--archive-total-gb",
        type=float,
        default=None,
        metavar="G",
        help="stop queued archive work once every viewpoint's backfill together has "
        "downloaded G GB (default 5; 0 = only the per-viewpoint --max-gb caps)",
    )
    loop.set_defaults(func=_cmd_loop, nps_fetcher=True)

    status = sub.add_parser("status", help="show persisted capture state (no network)")
    status.add_argument("--json", action="store_true")
    status.add_argument(
        "--archive-queue",
        metavar="PATH",
        default=None,
        help="archive queue to report (default: state/archive/queue.json if it exists)",
    )
    status.set_defaults(func=_cmd_status)

    cadence = sub.add_parser(
        "cadence", help="a camera's update cadence from logs and sidecars on disk (no network)"
    )
    cadence.add_argument("viewpoint")
    cadence.add_argument("--hours", type=float, default=24.0, help="look back this far (24)")
    cadence.add_argument(
        "--max-gap",
        type=float,
        default=None,
        metavar="S",
        help="consecutive polls further apart than this break a pair (1.8 poll intervals)",
    )
    cadence.add_argument("--json", action="store_true")
    cadence.set_defaults(func=_cmd_cadence)

    store = sub.add_parser(
        "storage",
        aliases=["storage-report"],  # the name CLAUDE.md gives the offline command
        help="bytes per frame and per day, old policy against PRD §8.7 (no network)",
    )
    store.add_argument("--day", default=None, help="UTC day whose daylight weights (today)")
    store.add_argument("--only", action="append", metavar="VIEWPOINT_ID")
    store.add_argument("--json", action="store_true")
    store.set_defaults(func=_cmd_storage)

    archive = sub.add_parser("archive", help="NPS archive: availability and backfill")
    asub = archive.add_subparsers(dest="archive_cmd", required=True)
    days = asub.add_parser("days", help="GetAvailableDays for one viewpoint")
    days.add_argument("viewpoint")
    days.set_defaults(func=_cmd_archive, nps_fetcher=True)
    for name, help_text, nargs in (
        ("day", "download one day's frames (thumbmedium unless --hires)", 1),
        ("sample", "download picked frames (default: midday) of several days", "+"),
    ):
        p = asub.add_parser(name, help=help_text)
        p.add_argument("viewpoint")
        p.add_argument("dates", nargs=nargs, metavar="YYYY-MM-DD")
        p.add_argument(
            "--hires", action="store_true", help="full-size frames instead of thumbmedium"
        )
        p.add_argument(
            "--daytime-only",
            action="store_true",
            help="only frames with NPS SolarElevationAngle >= 0",
        )
        p.add_argument("--min-sun-deg", type=float, default=None)
        p.add_argument("--dry-run", action="store_true", help="search only; download nothing")
        p.add_argument(
            "--max-gb",
            type=float,
            default=1.0,
            help="stop once this viewpoint's backfill has downloaded this many GB",
        )
        p.add_argument("--convention", choices=("label", "wall"), default="label")
        p.add_argument(
            "--keep-original",
            action="store_true",
            help="store hires bytes as downloaded instead of <= 1920 px",
        )
        p.add_argument(
            "--held-out-override",
            metavar="REASON",
            default=None,
            help="scoring only: release frames on or after the D-019 guard (2026-06-01); "
            "the reason is logged with every frame",
        )
        if name == "sample":
            p.add_argument("--pick", choices=("midday", "hourly", "all"), default="midday")
        else:
            p.add_argument("--pick", choices=("all", "hourly", "midday"), default="all")
        p.set_defaults(func=_cmd_archive, nps_fetcher=True)

    queue = asub.add_parser(
        "queue", help="edit the backfill queue that 'loop --archive-queue' works (no network)"
    )
    qsub = queue.add_subparsers(dest="queue_cmd", required=True)
    add = qsub.add_parser("add", help="queue days to backfill at one viewpoint")
    add.add_argument("viewpoint")
    add.add_argument("dates", nargs="+", metavar="YYYY-MM-DD")
    add.add_argument(
        "--through",
        metavar="YYYY-MM-DD",
        default=None,
        help="queue every day from the one date given through this one (a season)",
    )
    add.add_argument("--hires", action="store_true", help="full-size frames (the default)")
    add.add_argument("--thumbs", action="store_true", help="thumbmedium (with --hires: both)")
    add.add_argument("--pick", choices=("all", "hourly", "midday"), default="hourly")
    add.add_argument(
        "--daytime-only", action="store_true", help="only frames with NPS SolarElevationAngle >= 0"
    )
    add.add_argument("--min-sun-deg", type=float, default=None)
    add.add_argument(
        "--max-gb",
        type=float,
        default=1.0,
        help="stop once this viewpoint's backfill has downloaded this many GB in all",
    )
    add.add_argument("--convention", choices=("label", "wall"), default="label")
    listing = qsub.add_parser("list", help="show the queue and each entry's progress")
    listing.add_argument("--json", action="store_true")
    remove = qsub.add_parser("remove", help="remove entries (what they stored stays)")
    remove.add_argument("ids", nargs="*", metavar="ID")
    remove.add_argument(
        "--finished", action="store_true", help="every done, refused, failed or stopped one"
    )
    remove.add_argument("--all", action="store_true")
    resume = qsub.add_parser("resume", help="set stopped (size cap) or failed entries back to work")
    resume.add_argument("ids", nargs="*", metavar="ID")
    resume.add_argument("--all", action="store_true", help="every stopped or failed entry")
    resume.add_argument(
        "--max-gb",
        type=float,
        default=None,
        help="raise the entries' per-viewpoint size cap to this many GB (a stopped entry needs it)",
    )
    for p in (add, listing, remove, resume):
        p.add_argument(
            "--queue", metavar="PATH", default=None, help="queue file (state/archive/queue.json)"
        )
        p.set_defaults(func=_cmd_archive_queue)

    check = sub.add_parser("archive-check", help="the M0.5 archive depth/epoch check")
    check.add_argument("--viewpoint", action="append", metavar="VIEWPOINT_ID")
    check.add_argument(
        "--rainier-page",
        action="store_true",
        help="also look once at the Mount Rainier webcam page",
    )
    check.add_argument("--budget", type=int, default=None, help="total requests (default 220)")
    check.add_argument("--check-date", default=None, help="label of the check (default: today UTC)")
    check.add_argument(
        "--paging-probe",
        action="store_true",
        help="search 2026-11-17 (metadata only) with 50 per page to exercise paging",
    )
    check.set_defaults(func=_cmd_archive_check, nps_fetcher=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of the ``snow-globe-capture`` console script.

    Commands that fetch from nps.gov run inside the host-wide NPS-fetcher lock; if another
    fetcher is active the command prints why and returns :data:`EXIT_FETCHER_BUSY`.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    storage = storage_from_env()
    viewpoints = load_manifests(args.manifests) if args.manifests else load_manifests()
    if not getattr(args, "nps_fetcher", False):
        return int(args.func(args, storage, viewpoints) or 0)
    from .fetcher_guard import FetcherBusy, nps_fetcher_lock

    words = list(argv) if argv is not None else sys.argv[1:]
    try:
        with nps_fetcher_lock("snow-globe-capture " + " ".join(words)) as guard:
            args.guard = guard
            return int(args.func(args, storage, viewpoints) or 0)
    except FetcherBusy as exc:
        print(f"snow-globe-capture: refused, nothing was sent: {exc}", file=sys.stderr)
        return EXIT_FETCHER_BUSY


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
