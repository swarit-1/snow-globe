"""Fetch a P1 hindcast plan: each planned run, under a cap on the whole download (PRD §13.4).

CLAUDE.md "NOAA" and the task's rules, as built:

- **Only NOAA**, anonymous HTTPS byte ranges from ``noaa-hrrr-bdp-pds.s3.amazonaws.com``
  (:class:`.cachebucket.CachingBucket`), **one request at a time** (``workers=1``), with the
  project User-Agent and the bucket client's retries with exponential back-off; a run that
  still fails is retried after 1, 2, 4, ... min (at most 15 min apart), and the fetch stops
  after ``max_errors`` failures.
- **The quiet window** (:class:`.quiet.QuietWindow`): no request between :50 and :05 past
  any hour, when the live stand-in fetches.
- **A cap on everything the hindcast downloads** (default 2,000 MB): listings, inventories and
  GRIB ranges, counted in the persisted ledger across commands and restarts. A plan that
  needs more is refused before any GRIB byte is fetched; a run is started only if its planned
  bytes fit in what is left, and the extractor refuses a run whose plan exceeds it.
- **Resumable**: a run already written (the hindcast state marker holds its fields and hours)
  is skipped without a request; listings and inventories come from the cache; a run cut off
  midway is fetched again from its first range (its subsets were deleted).
- **A file is widened, never narrowed**: one file holds each run, so when a plan (another
  lead, or new frames) needs hours of a run whose file holds others, the run is fetched for
  the union of both, and the file then serves both plans (the old hours are downloaded again:
  their subsets were deleted). A run whose extraction still writes nothing is never counted
  as written: it is listed in ``runs_not_written`` with the extractor's reason, and the
  status is ``not-written``.
- GRIB subsets are deleted as soon as they are decoded (the extractor), under
  ``forecast/hindcast/cache/grib/``.

Progress is written after each run to ``state/hindcast-fetch.json`` in the hindcast root, and
every run, error, pause and stop is a line in the hindcast's log (``forecast/hindcast/log/``).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from snow_globe_common.paths import forecast_key, forecast_log_key
from snow_globe_common.timeutil import isoformat_z, utcnow

from ..bucket import transfer_delta
from ..discovery import list_run
from ..extract import BandwidthCapError, ExtractOptions, marker_key, run_covered
from ..fields import resolve_fields
from ..hrrr import STORAGE_SOURCE, format_run_id
from .cachebucket import CachingBucket
from .extractor import HindcastExtractor
from .plan import DEFAULT_CAP_MB, CoveredFn, plan_run_starts
from .storage import HindcastStorage

__all__ = [
    "FETCH_LOCK",
    "PROGRESS_KEY",
    "covered_fn",
    "existing_file",
    "fetch_plan",
    "log_event",
]

#: Fetch progress, in the hindcast root (its ``state/`` is also the mapped state).
PROGRESS_KEY = "state/hindcast-fetch.json"
#: Held for the whole fetch, so two hindcast fetches never run at once.
FETCH_LOCK = "hindcast-fetch"


def log_event(storage: HindcastStorage, event: str, when: datetime, **fields: Any) -> None:
    """Append one ``hindcast-*`` event to the hindcast's own log (``log/<day>.jsonl``)."""
    record = {"event": event, "t": isoformat_z(when, "milliseconds"), **fields}
    storage.append_jsonl(forecast_log_key(when), record)


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def covered_fn(
    storage: HindcastStorage, viewpoint_id: str, field_names: Sequence[str]
) -> CoveredFn:
    """``covered(run, hours)``: True when the hindcast's file of the run already holds those
    hours and ``field_names`` (its state marker says so and the file exists). The plan's cap
    check and the fetch use the same test."""
    names = list(field_names)

    def covered(run: datetime, hours: Sequence[int]) -> bool:
        return run_covered(
            storage, run, viewpoint_ids=[viewpoint_id], field_names=names, hours=list(hours)
        )

    return covered


def existing_file(
    storage: HindcastStorage, run: datetime, viewpoint_id: str
) -> tuple[list[int], list[str]]:
    """``(hours, field names)`` the hindcast's file of a run holds, from its state marker
    (both empty when there is no file)."""
    marker = storage.read_json(marker_key(run), default=None)
    viewpoints = marker.get("viewpoints", {}) if isinstance(marker, Mapping) else {}
    entry = viewpoints.get(viewpoint_id) if isinstance(viewpoints, Mapping) else None
    if not isinstance(entry, Mapping) or not storage.exists(
        forecast_key(STORAGE_SOURCE, viewpoint_id, run)
    ):
        return [], []
    hours = sorted({int(fh) for fh in entry.get("forecast_hours", [])})
    return hours, [str(name) for name in entry.get("field_names", [])]


def fetch_plan(
    plan: Mapping[str, Any],
    *,
    storage: HindcastStorage,
    bucket: CachingBucket,
    extractor: HindcastExtractor,
    cap_mb: float = DEFAULT_CAP_MB,
    max_errors: int = 4,
    backoff_s: float = 60.0,
    max_backoff_s: float = 900.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] = utcnow,
) -> dict[str, Any]:
    """Fetch every run of ``plan`` not written yet (see the module docstring); return a report.

    ``status`` is ``done`` (every planned run written), ``over-cap`` (the plan needs more than
    the cap; nothing fetched), ``cap-reached`` (stopped before a run that no longer fits),
    ``errors`` (``max_errors`` failures), or ``not-written`` (every run was tried, but the
    extractor wrote nothing for some: ``runs_not_written`` says which and why).
    ``runs_left`` lists every planned run that is not written, for whatever reason.
    """
    started = clock()
    t0 = time.monotonic()
    viewpoint = str(plan["viewpoint_id"])
    names = [str(name) for name in plan["fields"]]
    runs = plan_run_starts(plan)
    cap_bytes = int(cap_mb * 1e6)
    session_before = bucket.ledger.snapshot()
    paused_before = bucket.quiet.paused_s if bucket.quiet is not None else 0.0
    progress: dict[str, Any] = storage.root.read_json(PROGRESS_KEY, default=None) or {
        "kind": "snow-globe.hindcast-fetch-progress",
        "runs": {},
        "errors": [],
    }
    progress["plan_created_utc"] = plan.get("created_utc")
    report: dict[str, Any] = {
        "status": "done",
        "viewpoint_id": viewpoint,
        "plan_created_utc": plan.get("created_utc"),
        "lead_h": plan.get("lead_h"),
        "cap_bytes": cap_bytes,
        "runs_planned": [format_run_id(run) for run, _, _ in runs],
        "runs_written": [],
        "runs_widened": {},
        "runs_already_written": [],
        "runs_not_written": [],
        "runs_left": [],
        "errors": [],
    }
    planned_total = sum(planned for _, _, planned in runs)
    before_total = int(bucket.stored_ledger()["total_bytes"])
    covered = covered_fn(storage, viewpoint, names)
    pending = [(run, hours, planned) for run, hours, planned in runs if not covered(run, hours)]
    report["runs_already_written"] = [
        format_run_id(run) for run, _, _ in runs if all(run != p[0] for p in pending)
    ]
    pending_bytes = sum(planned for _, _, planned in pending)
    report["planned_bytes"] = planned_total
    report["pending_planned_bytes"] = pending_bytes
    report["hindcast_bytes_before"] = before_total
    if before_total + pending_bytes > cap_bytes:
        report["status"] = "over-cap"
        report["runs_left"] = [format_run_id(run) for run, _, _ in pending]
        log_event(
            storage,
            "hindcast-over-cap",
            clock(),
            planned_bytes=pending_bytes,
            already_bytes=before_total,
            cap_bytes=cap_bytes,
        )
        return _finish(report, bucket, session_before, paused_before, t0, started, clock)
    errors = 0
    stopped_at: int | None = None  # index of the first run a stop left untried
    with storage.lock(FETCH_LOCK, timeout_s=5.0, ttl_s=3600.0):
        for index, (run, hours, planned) in enumerate(pending):
            run_id = format_run_id(run)
            # Widen, never narrow: the run's file may hold hours (or fields) of another plan.
            old_hours, old_names = existing_file(storage, run, viewpoint)
            fetch_hours = sorted(set(hours) | set(old_hours))
            fetch_names = names + [name for name in old_names if name not in names]
            specs = tuple(resolve_fields(fetch_names))
            if fetch_hours != sorted(set(hours)) or fetch_names != names:
                report["runs_widened"][run_id] = {
                    "planned_hours": sorted(set(hours)),
                    "file_hours": old_hours,
                    "fetched_hours": fetch_hours,
                    "fields_added": fetch_names[len(names) :],
                }
            attempt = 0
            while True:
                attempt += 1
                # What is left, counted again before every attempt: a failed attempt may have
                # downloaded part of the run already.
                remaining = cap_bytes - int(bucket.stored_ledger()["total_bytes"])
                if planned > remaining:
                    report["status"] = "cap-reached"
                    stopped_at = index
                    log_event(
                        storage,
                        "hindcast-cap-reached",
                        clock(),
                        run=run_id,
                        planned_bytes=planned,
                        remaining_bytes=remaining,
                    )
                    break
                before = bucket.ledger.snapshot()
                run_t0 = time.monotonic()
                try:
                    listed = bucket.listing_fetched_utc(run)
                    listing = list_run(bucket, run, now=listed or clock())
                    result = extractor.extract(
                        run,
                        ExtractOptions(
                            fields=specs,
                            hours=tuple(fetch_hours),
                            viewpoint_ids=(viewpoint,),
                            max_bytes=remaining,
                            workers=1,
                        ),
                        listing=listing,
                    )
                except BandwidthCapError as exc:
                    report["status"] = "cap-reached"
                    stopped_at = index
                    log_event(storage, "hindcast-cap-reached", clock(), run=run_id, error=str(exc))
                    break
                except Exception as exc:  # any failure of one run: log, back off, retry
                    errors += 1
                    delta = transfer_delta(before, bucket.ledger.snapshot())
                    item = {
                        "run": run_id,
                        "attempt": attempt,
                        "utc": isoformat_z(clock(), "seconds"),
                        "error": _error_text(exc),
                        "bytes": delta["total_bytes"],
                    }
                    report["errors"].append(item)
                    progress["errors"].append(item)
                    storage.root.write_json(PROGRESS_KEY, progress)
                    log_event(storage, "hindcast-error", clock(), **item)
                    if errors >= max_errors:
                        report["status"] = "errors"
                        stopped_at = index
                        break
                    delay = min(backoff_s * 2 ** (attempt - 1), max_backoff_s)
                    log_event(storage, "hindcast-backoff", clock(), run=run_id, seconds=delay)
                    sleep(delay)
                    continue
                delta = transfer_delta(before, bucket.ledger.snapshot())
                entry = {
                    "status": "written" if result.written else "nothing written",
                    "written_utc": isoformat_z(result.fetch_finished_utc, "milliseconds"),
                    "hours": result.hours,
                    "planned_hours": sorted(set(hours)),
                    "written": result.written,
                    "skipped": result.skipped,
                    "planned_bytes": planned,
                    "extractor_planned_bytes": result.planned_bytes,
                    "transfer": delta,
                    "seconds_wall": round(time.monotonic() - run_t0, 3),
                    "seconds_download": round(result.seconds_download, 3),
                    "seconds_decode": round(result.seconds_decode, 3),
                    "attempts": attempt,
                }
                progress["runs"][run_id] = entry
                progress["updated_utc"] = isoformat_z(clock(), "seconds")
                storage.root.write_json(PROGRESS_KEY, progress)
                log_event(storage, "hindcast-run", clock(), run=run_id, **entry)
                if result.written:
                    report["runs_written"].append(run_id)
                else:  # never reported as written: the file lacks what the plan needs
                    report["runs_not_written"].append({"run": run_id, "skipped": result.skipped})
                break
            if report["status"] != "done":
                break
    not_written = [item["run"] for item in report["runs_not_written"]]
    untried = [] if stopped_at is None else [format_run_id(r) for r, _, _ in pending[stopped_at:]]
    report["runs_left"] = not_written + untried
    if not_written and report["status"] == "done":
        report["status"] = "not-written"
    return _finish(report, bucket, session_before, paused_before, t0, started, clock)


def _finish(
    report: dict[str, Any],
    bucket: CachingBucket,
    session_before: Mapping[str, Any],
    paused_before: float,
    t0: float,
    started: datetime,
    clock: Callable[[], datetime],
) -> dict[str, Any]:
    report["started_utc"] = isoformat_z(started, "seconds")
    report["finished_utc"] = isoformat_z(clock(), "seconds")
    report["seconds_wall"] = round(time.monotonic() - t0, 3)
    paused = (bucket.quiet.paused_s if bucket.quiet is not None else 0.0) - paused_before
    report["seconds_quiet_window"] = round(paused, 3)
    report["transfer"] = transfer_delta(session_before, bucket.ledger.snapshot())
    report["cache_hits"] = dict(bucket.cache_hits)
    report["hindcast_network_total"] = bucket.stored_ledger()
    return report
