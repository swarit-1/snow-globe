"""Console script ``lookout-capture`` (PRD §11.2 jobs "Capture" and "Archive backfill").

Live capture (polls NPS):

    lookout-capture once [--only <viewpoint_id>]...
    lookout-capture loop [--interval 300] [--jitter 30]
    lookout-capture status [--json]

Archive (one request at a time, >= 5 s apart, resumable, size-capped, held-out guard):

    lookout-capture archive days <viewpoint_id>
    lookout-capture archive day <viewpoint_id> <YYYY-MM-DD> [--daytime-only] [--hires] [--dry-run]
    lookout-capture archive sample <viewpoint_id> <YYYY-MM-DD>... [--pick midday|hourly|all]
    lookout-capture archive-check [--viewpoint <id>]... [--rainier-page] [--budget 220]

Every command except ``status`` sends requests to www.nps.gov, so it runs inside the
host-wide NPS-fetcher lock (:mod:`.fetcher_guard`): a second fetcher, or a running Rainier
stopgap, makes it exit with status 3 before anything is sent (CLAUDE.md: never run two NPS
fetchers at once). Archived frames dated on or after 1 Jun 2026 are refused (D-019) unless
``--held-out-override "<reason>"`` is given, which only scoring may do; it is logged.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import date
from typing import Any

from lookout_common.manifest import Viewpoint, load_manifests
from lookout_common.storage import Storage, storage_from_env
from lookout_common.timeutil import utcnow

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
    with CaptureRunner.default(storage, runner="cli-loop") as runner:
        runner.loop(
            selected,
            interval_s=args.interval,
            jitter_s=args.jitter,
            max_passes=args.max_passes,
            before_pass=guard.check_foreign if guard is not None else None,
        )
    return 0


def _cmd_status(
    args: argparse.Namespace, storage: Storage, viewpoints: dict[str, Viewpoint]
) -> int:
    from .live import status_report

    report = status_report(storage, list(viewpoints.values()), now=utcnow())
    if args.json:
        _print(report)
        return 0
    print(f"capture status at {report['now']}  (storage: {report['storage']})")
    header = (
        f"{'viewpoint':<28} {'last poll':<21} {'last image (UTC)':<21} {'age':>7} "
        f"{'GETs/h':>6} {'fail':>4}  next poll"
    )
    print(header)
    for row in report["cameras"]:
        print(
            f"{row['viewpoint_id']:<28} {row.get('last_poll_at') or '-':<21} "
            f"{row.get('last_image_time_utc') or '-':<21} "
            f"{_dash(row.get('last_image_age_min')):>7} "
            f"{row.get('gets_last_hour', 0):>6} {row.get('failures', 0):>4}  "
            f"{row.get('next_poll_at') or 'now'}"
        )
    print("today's capture_log events:", json.dumps(report.get("events_today", {})))
    return 0


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
    archive = default_archive_client(storage)
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
    archive = default_archive_client(storage, budget=budget)
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
        prog="lookout-capture",
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
        help="wait up to S seconds for a camera whose 5-min poll slot is near",
    )
    once.set_defaults(func=_cmd_once, nps_fetcher=True)

    loop = sub.add_parser("loop", help="capture forever, one pass per interval (Ctrl-C stops)")
    loop.add_argument("--interval", type=float, default=300.0)
    loop.add_argument("--jitter", type=float, default=30.0, help="extra random 0..J s per pass")
    loop.add_argument("--only", action="append", metavar="VIEWPOINT_ID")
    loop.add_argument("--max-passes", type=int, default=None, help=argparse.SUPPRESS)
    loop.set_defaults(func=_cmd_loop, nps_fetcher=True)

    status = sub.add_parser("status", help="show persisted capture state (no network)")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=_cmd_status)

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
    """Entry point of the ``lookout-capture`` console script.

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
        with nps_fetcher_lock("lookout-capture " + " ".join(words)) as guard:
            args.guard = guard
            return int(args.func(args, storage, viewpoints) or 0)
    except FetcherBusy as exc:
        print(f"lookout-capture: refused, nothing was sent: {exc}", file=sys.stderr)
        return EXIT_FETCHER_BUSY


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
