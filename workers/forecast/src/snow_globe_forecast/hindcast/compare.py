"""S3 availability against the lag models, hour by hour, for the hindcast's runs (PRD §8.3, §13.9).

Each hindcast hour holds its S3 availability (``availability_utc``, the later of its two
``LastModified`` stamps) and the live lag model's prediction written beside it
(``availability_model``). This report sets them side by side, with:

- the live lag model **now** (``forecast_log/*.jsonl``'s live lags as they stand when the
  report runs), and the live + survey model the archived path uses;
- the lag the live log itself recorded for the same (run, hour), for runs the live worker
  saw while they were new: the same S3 stamps measured days apart, so the two should agree;
- whether the live worker stored the run (``forecast/hrrr/<viewpoint>/<run>z.json``).

A model prediction earlier than the S3 availability would have made the hour look available
before it was (PRD §13.9); the report counts those hours and how early they were. Each row
also gives the lags each model pools for the hour over all cycles, and the report the
:data:`..availability.MIN_SAMPLES` its p95 rules need, so a model that sits on its 120 min
fallback shows why (on 23 Nov 2026 the live log held 4 live lags for each of f25-f32: too few
for the rules, not none). Nothing is written to the live data dir; the report goes to
``reports/availability-compare.json`` in the hindcast root.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from statistics import median
from typing import Any

from snow_globe_common.paths import forecast_key
from snow_globe_common.storage import Storage
from snow_globe_common.timeutil import isoformat_z

from ..availability import MIN_SAMPLES, LagModel
from ..hrrr import STORAGE_SOURCE, parse_run_id
from ..laglog import METHOD_LIVE, read_lag_log
from .storage import HindcastStorage

__all__ = ["COMPARE_KEY", "compare_availability"]

COMPARE_KEY = "reports/availability-compare.json"


def _stats(values: Sequence[float]) -> dict[str, Any] | None:
    if not values:
        return None
    return {
        "n": len(values),
        "min": round(min(values), 3),
        "median": round(median(values), 3),
        "max": round(max(values), 3),
    }


def _model_side(rows: Sequence[Mapping[str, Any]], lag_key: str, method_key: str) -> dict[str, Any]:
    """How a model's predicted lags compare with the S3 lags (seconds; + = model later)."""
    margins = [float(r[lag_key]) - float(r["s3_lag_s"]) for r in rows]
    early = [m for m in margins if m < 0]
    by_method: dict[str, Any] = {}
    for method in sorted({str(r[method_key]) for r in rows}):
        part = [float(r[lag_key]) - float(r["s3_lag_s"]) for r in rows if r[method_key] == method]
        by_method[method] = {
            "hours": len(part),
            "early": sum(1 for m in part if m < 0),
            "margin_s": _stats(part),
        }
    return {
        "hours": len(margins),
        "early": len(early),
        "early_by_up_to_s": round(-min(early), 3) if early else 0.0,
        "margin_s": _stats(margins),
        "by_method": by_method,
    }


def compare_availability(
    hstorage: HindcastStorage,
    live_storage: Storage,
    viewpoint_id: str,
    *,
    now: datetime,
) -> dict[str, Any]:
    """The comparison report for every hindcast file of ``viewpoint_id`` (see module doc)."""
    log = read_lag_log(live_storage)
    logged = log.by_pair
    live_model = LagModel.from_events([e for e in log.events if e["method"] == METHOD_LIVE])
    full_model = LagModel.from_events(log.events)
    rows: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    prefix = f"forecast/{STORAGE_SOURCE}/{viewpoint_id}/"
    for key in hstorage.list_prefix(prefix):
        if not key.endswith("z.json"):
            continue
        doc = hstorage.read_json(key)
        run_id = str(doc["run_id"])
        run_start = parse_run_id(run_id)
        live_key = forecast_key(STORAGE_SOURCE, viewpoint_id, run_start)
        live_doc = live_storage.read_json(live_key, default=None)
        live_saw = isinstance(live_doc, Mapping)
        run_rows = 0
        for hour in doc["hours"]:
            fh = int(hour["fh"])
            model = hour["availability_model"] or {}
            live_now, live_now_method, live_now_n = live_model.lag_s(run_start, fh)
            full_now, full_now_method, full_now_n = full_model.lag_s(run_start, fh)
            event = logged.get((run_id, fh))
            row: dict[str, Any] = {
                "run": run_id,
                "fh": fh,
                "valid_utc": hour["valid_time_utc"],
                "s3_availability_utc": hour["availability_utc"],
                "s3_lag_s": float(hour["lag_s"]),
                "grib_last_modified_utc": hour["grib_last_modified_utc"],
                "idx_last_modified_utc": hour["idx_last_modified_utc"],
                "live_model_at_write_lag_s": model.get("lag_s"),
                "live_model_at_write_method": model.get("method"),
                "live_model_at_write_samples": model.get("samples"),
                "live_model_lag_s": round(live_now, 3),
                "live_model_method": live_now_method,
                "live_model_samples": live_now_n,
                "live_model_pooled_samples": int(live_model.pooled_samples.get(fh, 0)),
                "full_model_lag_s": round(full_now, 3),
                "full_model_method": full_now_method,
                "full_model_samples": full_now_n,
                "full_model_pooled_samples": int(full_model.pooled_samples.get(fh, 0)),
                "live_worker_stored_run": live_saw,
                "logged_lag_s": None if event is None else float(event["lag_s"]),
                "logged_method": None if event is None else str(event["method"]),
            }
            if event is not None:
                row["logged_minus_s3_s"] = round(float(event["lag_s"]) - row["s3_lag_s"], 3)
            rows.append(row)
            run_rows += 1
        runs.append(
            {
                "run": run_id,
                "hours": doc["forecast_hours"],
                "live_worker_stored_run": live_saw,
                "live_file_hours": live_doc.get("forecast_hours") if live_saw else None,
                "live_file_fetch_finished_utc": (
                    live_doc.get("fetch_finished_utc") if live_saw else None
                ),
                "hindcast_fetch_finished_utc": doc["fetch_finished_utc"],
                "rows": run_rows,
            }
        )
    seen = [r for r in rows if r["live_worker_stored_run"]]
    with_log = [r for r in rows if r["logged_lag_s"] is not None]
    at_write = [r for r in rows if r["live_model_at_write_lag_s"] is not None]
    return {
        "kind": "snow-globe.hindcast-availability-compare",
        "created_utc": isoformat_z(now, "seconds"),
        "viewpoint_id": viewpoint_id,
        "what": (
            "per hindcast hour: the S3 availability the file records (later LastModified of "
            "GRIB2 and .idx) against the live lag model (forecast_log/*.jsonl live lags only: "
            "at write time, from the file, and now) and the live + survey model; margins are "
            "model lag - S3 lag in seconds, negative = the model is early"
        ),
        "lag_log": log.summary(),
        "min_samples": MIN_SAMPLES,
        "live_lags_pooled_by_fh": {
            str(fh): int(live_model.pooled_samples.get(fh, 0))
            for fh in sorted({int(r["fh"]) for r in rows})
        },
        "runs": runs,
        "hours": len(rows),
        "live_model_at_write": _model_side(
            at_write, "live_model_at_write_lag_s", "live_model_at_write_method"
        ),
        "live_model_now": _model_side(rows, "live_model_lag_s", "live_model_method"),
        "live_model_now_runs_the_live_worker_stored": _model_side(
            seen, "live_model_lag_s", "live_model_method"
        ),
        "full_model_now": _model_side(rows, "full_model_lag_s", "full_model_method"),
        "logged_against_s3": {
            "hours": len(with_log),
            "by_method": dict(Counter(str(r["logged_method"]) for r in with_log)),
            "equal": sum(1 for r in with_log if r["logged_minus_s3_s"] == 0.0),
            "diff_s": _stats([float(r["logged_minus_s3_s"]) for r in with_log]),
        },
        "rows": rows,
    }
