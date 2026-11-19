"""Lambda entry point ``lookout_forecast.lambda_handler.handler`` (PRD §11.2, §11.3; D-014).

EventBridge Scheduler invokes it every 5 minutes (``rate(5 minutes)``, the default of
``forecast_schedule_expression`` in ``infra/terraform``; PRD §11.2). Each call processes,
newest first, every run from the last ``LOOKOUT_FORECAST_LOOKBACK_H`` hours that is not
written yet:

- a **complete** run is extracted for every viewpoint;
- a run still **incomplete** after ``LOOKOUT_FORECAST_STALE_H`` hours is extracted with the
  hours that exist, and its missing hours are recorded, so it is never retried forever;
- anything else is left for a later call.

It is idempotent: the state marker of a run says which viewpoints, fields and hours are
written, and a covered run costs one small storage read and no bucket request. New runs
stop being started when less than ``LOOKOUT_FORECAST_RESERVE_S`` seconds remain.

One run's failure never stops the others. A failed run gets an ``error`` event in
``forecast_log`` and a back-off record (:mod:`.backoff`): a cap refusal waits for the
settings to change, any other error is retried after 10, 20, 40, ... minutes (at most
4 h). If the extraction lock is busy (for example a timed-out call's S3 lock, until its
900 s expiry), or listing the bucket fails, the rest of the call is deferred to the next
tick. Every row reports the bytes that run cost in this call.

Environment (all optional): ``LOOKOUT_FORECAST_FIELDS`` (comma list, default all),
``LOOKOUT_FORECAST_MAX_FH``, ``LOOKOUT_FORECAST_LOOKBACK_H`` (12),
``LOOKOUT_FORECAST_STALE_H`` (4), ``LOOKOUT_FORECAST_MAX_RUNS`` (4),
``LOOKOUT_FORECAST_WORKERS`` (8), ``LOOKOUT_FORECAST_MAX_MB`` (per run, no default),
``LOOKOUT_FORECAST_RESERVE_S`` (180), ``LOOKOUT_FORECAST_TIMEOUT_S`` (600, used only
without a Lambda context); plus the ``lookout_common`` storage variables.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from lookout_common.manifest import Viewpoint, load_manifests
from lookout_common.paths import forecast_log_key
from lookout_common.storage import LockTimeoutError, Storage, storage_from_env
from lookout_common.timeutil import isoformat_z, utcnow

from . import backoff
from .bucket import BucketError, HrrrBucket
from .discovery import list_run
from .extract import (
    BandwidthCapError,
    ExtractOptions,
    Extractor,
    RunIncompleteError,
    RunNotFoundError,
    run_covered,
)
from .fields import resolve_fields
from .grid import HRRR_CONUS, LambertConformalGrid
from .hrrr import floor_hour, format_run_id, run_hours

__all__ = ["handler", "process_pending"]

log = logging.getLogger("lookout_forecast")


def _env_int(name: str, default: int | None) -> int | None:
    raw = (os.environ.get(name) or "").strip()
    return int(raw) if raw else default


def _env_float(name: str, default: float | None) -> float | None:
    raw = (os.environ.get(name) or "").strip()
    return float(raw) if raw else default


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _spent(bucket: HrrrBucket, before: Mapping[str, Any]) -> int:
    """Bytes the bucket client received since the ``before`` snapshot."""
    return int(bucket.ledger.since(before)["total_bytes"])


def process_pending(
    storage: Storage,
    bucket: HrrrBucket,
    manifests: Mapping[str, Viewpoint],
    *,
    now: datetime | None = None,
    options: ExtractOptions | None = None,
    lookback_h: int = 12,
    stale_h: float = 4.0,
    max_runs: int = 4,
    remaining_s: Callable[[], float] | None = None,
    reserve_s: float = 180.0,
    grid: LambertConformalGrid = HRRR_CONUS,
) -> dict[str, Any]:
    """Process every complete (or stale) run not yet written; return a JSON summary.

    ``now`` fixes the clock for tests (the extraction's fetch times use it too).
    """
    moment = now or utcnow()
    opts = options or ExtractOptions()
    clock = (lambda: moment) if now is not None else utcnow
    extractor = Extractor(storage, bucket, manifests, grid=grid, clock=clock)
    ids = [vp.id for vp in extractor.viewpoints(opts.viewpoint_ids)]
    names = [spec.name for spec in opts.fields]
    settings = backoff.settings_fingerprint(
        field_names=names, max_fh=opts.max_fh, max_bytes=opts.max_bytes, viewpoint_ids=ids
    )
    call_before = bucket.ledger.snapshot()
    newest = floor_hour(moment)
    runs: list[dict[str, Any]] = []
    extracted = 0
    halt: str | None = None  # once set, every remaining run waits for the next call

    def fail(
        run: datetime, row: dict[str, Any], exc: BaseException, kind: backoff.FailureKind | None
    ) -> None:
        """Log an ``error`` event, with a back-off record unless ``kind`` is None."""
        if kind == "cap":
            log.warning("run %s refused: %s", row["run"], exc)
        else:
            log.error("run %s failed: %s", row["run"], _error_text(exc), exc_info=exc)
        event: dict[str, Any] = {
            "event": "error",
            "run": row["run"],
            "error": _error_text(exc),
            "bytes": row.get("bytes", 0),
        }
        if kind is not None:
            failure = backoff.record_failure(
                storage, run, kind=kind, error=_error_text(exc), settings=settings, now=clock()
            )
            event.update(
                kind=failure.kind,
                attempts=failure.attempts,
                next_try_utc=(
                    isoformat_z(failure.next_try_utc, "seconds") if failure.next_try_utc else None
                ),
            )
        storage.append_jsonl(forecast_log_key(clock()), event)

    for back in range(lookback_h + 1):
        run = newest - timedelta(hours=back)
        run_id = format_run_id(run)
        hours = run_hours(run, opts.max_fh)
        if run_covered(
            storage, run, viewpoint_ids=ids, field_names=names, hours=hours, accept_missing=True
        ):
            runs.append({"run": run_id, "status": "written"})
            continue
        if halt is not None:
            runs.append({"run": run_id, "status": f"deferred: {halt}"})
            continue
        if extracted >= max_runs:
            runs.append({"run": run_id, "status": "deferred: run limit for this call"})
            continue
        if remaining_s is not None and remaining_s() < reserve_s:
            runs.append({"run": run_id, "status": "deferred: time budget"})
            continue
        failure = backoff.read_failure(storage, run)
        reason = failure.skip_reason(clock(), settings) if failure is not None else None
        if reason is not None:
            runs.append({"run": run_id, "status": f"backing off: {reason}"})
            continue
        before = bucket.ledger.snapshot()
        row: dict[str, Any] = {"run": run_id}
        try:
            listing = list_run(bucket, run, now=moment)
        except BucketError as exc:
            row.update(status=f"error: {_error_text(exc)}", bytes=_spent(bucket, before))
            fail(run, row, exc, None)
            runs.append(row)
            halt = "the bucket listing failed"
            continue
        wanted_missing = [fh for fh in hours if fh in listing.missing_hours]
        stale = moment - run >= timedelta(hours=stale_h)
        if not listing.exists:
            runs.append({"run": run_id, "status": "not on S3" + (" (stale)" if stale else "")})
            continue
        if wanted_missing and not stale:
            runs.append(
                {"run": run_id, "status": f"waiting: {len(wanted_missing)} hour(s) missing"}
            )
            continue
        run_opts = replace(opts, allow_incomplete=bool(wanted_missing))
        try:
            result = extractor.extract(run, run_opts)
        except (RunNotFoundError, RunIncompleteError) as exc:
            runs.append({"run": run_id, "status": f"not ready: {exc}"})
            continue
        except LockTimeoutError as exc:
            row.update(status=f"deferred: lock busy ({exc})")
            runs.append(row)
            halt = "lock busy"
            continue
        except BandwidthCapError as exc:
            row.update(status=f"error: {_error_text(exc)}", bytes=_spent(bucket, before))
            fail(run, row, exc, "cap")
            runs.append(row)
            continue
        except Exception as exc:  # one bad run (ecCodes, disk, a bug) must not stop the others
            row.update(status=f"error: {_error_text(exc)}", bytes=_spent(bucket, before))
            fail(run, row, exc, "error")
            runs.append(row)
            continue
        if failure is not None:
            backoff.resolve_failure(storage, run, now=clock())
        extracted += 1
        runs.append(
            {
                "run": run_id,
                "status": "extracted" + (" (stale, incomplete)" if wanted_missing else ""),
                "hours": len(result.hours),
                "files": len(result.written),
                "bytes": _spent(bucket, before),
                "grib_bytes": result.transfer["bytes"].get("grib", 0),
                "seconds": round(result.seconds_total, 1),
            }
        )
    return {
        "now_utc": isoformat_z(moment),
        "extracted": extracted,
        "runs": runs,
        "transfer": bucket.ledger.since(call_before),
    }


def handler(event: Mapping[str, Any] | None, context: Any) -> dict[str, Any]:
    """AWS Lambda handler: see the module docstring. ``event`` is ignored."""
    logging.getLogger().setLevel(logging.INFO)
    started = time.monotonic()
    timeout_s = _env_float("LOOKOUT_FORECAST_TIMEOUT_S", 600.0) or 600.0

    def remaining() -> float:
        if context is not None and hasattr(context, "get_remaining_time_in_millis"):
            return float(context.get_remaining_time_in_millis()) / 1000.0
        return timeout_s - (time.monotonic() - started)

    fields = resolve_fields(
        [os.environ["LOOKOUT_FORECAST_FIELDS"]]
        if os.environ.get("LOOKOUT_FORECAST_FIELDS")
        else None
    )
    max_mb = _env_float("LOOKOUT_FORECAST_MAX_MB", None)
    options = ExtractOptions(
        fields=tuple(fields),
        max_fh=_env_int("LOOKOUT_FORECAST_MAX_FH", None),
        workers=_env_int("LOOKOUT_FORECAST_WORKERS", 8) or 8,
        max_bytes=None if max_mb is None else int(max_mb * 1e6),
    )
    storage = storage_from_env()
    with HrrrBucket() as bucket:
        summary = process_pending(
            storage,
            bucket,
            load_manifests(),
            options=options,
            lookback_h=_env_int("LOOKOUT_FORECAST_LOOKBACK_H", 12) or 12,
            stale_h=_env_float("LOOKOUT_FORECAST_STALE_H", 4.0) or 4.0,
            max_runs=_env_int("LOOKOUT_FORECAST_MAX_RUNS", 4) or 4,
            remaining_s=remaining,
            reserve_s=_env_float("LOOKOUT_FORECAST_RESERVE_S", 180.0) or 180.0,
        )
    log.info("forecast worker: %s", summary)
    return summary
