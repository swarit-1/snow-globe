"""The backtest pair's forecast run, chosen and extracted the worker's way (PRD §8.3, §16 M0.5).

PRD §13.2 design H pairs an archived frame with "archived HRRR runs (exact run times;
availability modelled)". This module does that for one pair with lookout-forecast's own
code, and nothing else:

- **Choice** (:func:`choose_run`): lookout-forecast's ``select_run_for_issue`` with its
  ``LagModel`` read from the live lag log in ``forecast_log/``. It returns the newest run
  whose bracketing forecast hours were available by the issue time; an archived run's
  availability is modelled as run start + the lag model's lag (p95 per cycle and hour once
  20 lags are logged, else a conservative 120 min). The choice records which rule applied.
- **Extraction** (:func:`extract_hours`): lookout-forecast's ``Extractor`` for one
  viewpoint, the fields the v−1 scene reads (:data:`SCENE_FIELDS`) and only the two
  bracketing hours, under a GRIB byte cap (default 60 MB) that the extractor checks against
  the ``.idx`` plan before any GRIB byte is fetched. GRIB subsets are deleted after decoding
  (the extractor does that, CLAUDE.md "NOAA").

**Why the hour list is substituted.** The worker's CLI (``lookout-forecast run --max-fh N``)
always extracts hours 0..N. A 24 h lead needs hours 24–33 of a run, so the CLI would fetch
25–34 hours. With these fields an hour of run 2024011412 planned 16.5 MB (f32), so f00–f32
would be about 545 MB, against 35 MB for the two hours. For the one call,
``lookout_forecast.extract.run_hours`` is replaced by the two hours the pair needs, and the
result is checked against them. If the substitution stopped working, the planner and the
extractor would see every hour of the run (f00–f48 for a 00/06/12/18Z run): the planned
hours would differ from the two asked for, and the byte cap would refuse the run, before
anything is downloaded (both are tested).
TODO(M1.5): lookout-forecast should accept an explicit hour list (``ExtractOptions.hours``).

**Availability is checked against the file.** The pair's leakage check uses the availability
the forecast file records for the bracketing hours, while the choice here is made with the lag
model as it is now. A reused file was written with the model of its day, which can move as
lags accumulate, so the summary is refused (:class:`PairForecastError`) unless the file's
recorded availability of the two hours equals the choice's; the summary carries it as
``file.availability_utc``.

Besides the forecast file (``forecast/hrrr/<viewpoint>/<run>z.json``), the extractor writes
what it always writes: its state marker (``state/forecast/hrrr/<run>z.json``), one ``run``
event in ``forecast_log/<day>.jsonl`` (an archived run writes no ``lag`` events) and its lock.
The lag readers ignore ``run`` events, but the event sits in the live worker log.
TODO(M1.5): read the lag log from the live data dir but let the extractor write to a scratch
storage, then copy only the forecast file into ``forecast/hrrr/``.

This module imports only the standard library, lookout-common and lookout-forecast, so it
runs inside lookout-forecast's environment, which has ecCodes. The M0.5 pair ran it from the
frozen snapshot of committed code that the background jobs use::

    cd workers/forecast
    PYTHONPATH=$RT/workers/forecast/src:$RT/workers/common/src:<repo>/eval/src \\
    LOOKOUT_MANIFEST_DIR=$RT/viewpoints/manifests \\
    uv run --no-sync python -m lookout_eval.pair_forecast \\
        --viewpoint grand-canyon.yavapai --valid 2024-01-15T19:45:00Z --lead 24

``--plan-only`` reads the listing and the two ``.idx`` inventories (45,515 bytes for run
2024011412) and writes nothing.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_MAX_MB",
    "SCENE_FIELDS",
    "PairForecastError",
    "choose_run",
    "extract_hours",
    "fetch_pair_forecast",
    "main",
    "only_hours",
    "stored_availability",
]

#: The HRRR fields the v−1 SceneSpec and S_rule read (:mod:`.scene`, :mod:`.forecast_io`),
#: plus ``PRES:cloud base``, the documented cross-check for ``HGT:cloud base``. The model
#: terrain (``HGT:surface``) is always fetched once, with the first hour.
SCENE_FIELDS: tuple[str, ...] = (
    "VIS:surface",
    "HGT:cloud ceiling",
    "HGT:cloud base",
    "PRES:cloud base",
    "HGT:cloud top",
    "LCDC:low cloud layer",
    "MCDC:middle cloud layer",
    "HCDC:high cloud layer",
    "PRATE:surface",
    "UGRD:10 m above ground",
    "VGRD:10 m above ground",
)
#: GRIB megabytes one pair's extraction may download (the M0.5 task's limit).
DEFAULT_MAX_MB = 60.0


class PairForecastError(RuntimeError):
    """No usable run, or the extraction did not produce what the pair needs."""


def _forecast_modules() -> dict[str, Any]:
    """lookout-forecast's modules, imported lazily (they need ecCodes)."""
    try:
        from lookout_forecast import __version__ as forecast_version
        from lookout_forecast import availability, extract, fields, hrrr
        from lookout_forecast.bucket import HrrrBucket
    except ImportError as exc:
        raise PairForecastError(
            "lookout-forecast is not importable here: run inside workers/forecast's environment "
            "(see this module's docstring) or `uv sync --group forecast` in eval/"
        ) from exc
    return {
        "version": forecast_version,
        "availability": availability,
        "extract": extract,
        "fields": fields,
        "hrrr": hrrr,
        "HrrrBucket": HrrrBucket,
    }


def _forecast_source() -> dict[str, Any]:
    """Where the lookout-forecast code that ran was loaded from (provenance for the record)."""
    import lookout_common
    import lookout_forecast

    return {
        "lookout_forecast_file": str(Path(lookout_forecast.__file__).resolve()),
        "lookout_forecast_version": getattr(lookout_forecast, "__version__", None),
        "lookout_common_file": str(Path(lookout_common.__file__).resolve()),
    }


@contextlib.contextmanager
def only_hours(hours: Sequence[int]) -> Iterator[list[int]]:
    """Make lookout-forecast's extractor and planner see only ``hours`` for one call.

    Replaces ``lookout_forecast.extract.run_hours`` (which returns 0..max_fh) by a function
    returning ``hours``, after checking that the run produces every one of them. The original
    is restored on exit. Not thread-safe; the extractor calls it once, before its threads.
    """
    extract = _forecast_modules()["extract"]
    original = extract.run_hours
    wanted = sorted({int(h) for h in hours})
    if not wanted:
        raise ValueError("no forecast hours given")

    def run_hours(run_start: datetime, limit: int | None = None) -> list[int]:
        produced = original(run_start, None)
        missing = [fh for fh in wanted if fh not in produced]
        if missing:
            raise PairForecastError(f"this run does not produce forecast hours {missing}")
        return [fh for fh in wanted if limit is None or fh <= limit]

    extract.run_hours = run_hours
    try:
        yield wanted
    finally:
        extract.run_hours = original


def _utc(value: datetime | str) -> datetime:
    """A datetime, or an ISO 8601 string, as an aware UTC datetime."""
    from lookout_common.timeutil import parse_iso_utc, to_utc

    return to_utc(value) if isinstance(value, datetime) else parse_iso_utc(str(value))


def stored_availability(doc: Mapping[str, Any], hours: Sequence[int]) -> datetime:
    """The later recorded availability of ``hours`` in a forecast file's ``hours[]`` (UTC).

    That is the value the pair's leakage check uses (``ForecastRun.availability``). Raises
    :class:`PairForecastError` when an hour, or its availability, is missing.
    """
    recorded = {
        int(entry["fh"]): entry
        for entry in doc.get("hours") or []
        if isinstance(entry, Mapping) and entry.get("fh") is not None
    }
    times = []
    for fh in hours:
        value = (recorded.get(int(fh)) or {}).get("availability_utc")
        if not value:
            raise PairForecastError(f"the forecast file records no availability for f{fh:02d}")
        times.append(_utc(value))
    if not times:
        raise PairForecastError("no forecast hours given")
    return max(times)


def choose_run(
    issue_time: datetime, valid_time: datetime, *, storage: Any, lag_model: Any | None = None
) -> tuple[Any, Any]:
    """lookout-forecast's run choice for (issue, valid): ``(RunChoice, LagModel)``.

    Every hour is modelled (an archived run), with the lag model read from ``storage``'s
    forecast log unless one is given. Raises :class:`PairForecastError` when no run within
    the selector's look-back covers the valid time.
    """
    availability = _forecast_modules()["availability"]
    model = lag_model if lag_model is not None else availability.LagModel.from_storage(storage)
    choice = availability.select_run_for_issue(issue_time, valid_time, lag_model=model)
    if choice is None:
        raise PairForecastError(
            "no HRRR run covers the valid time with modelled availability by the issue time"
        )
    return choice, model


def extract_hours(
    *,
    storage: Any,
    bucket: Any,
    manifests: Any,
    run_start: datetime,
    hours: Sequence[int],
    viewpoint_id: str,
    field_names: Sequence[str] = SCENE_FIELDS,
    max_mb: float = DEFAULT_MAX_MB,
    lag_model: Any | None = None,
    plan_only: bool = False,
    extractor_options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Plan, and unless ``plan_only`` extract, ``hours`` of one run for one viewpoint.

    Returns ``{"plan": ..., "result": ...}``: the plan from the ``.idx`` inventories
    (``lookout_forecast.extract.plan_run``) and the extractor's ``run`` event (None when
    ``plan_only``). Raises :class:`PairForecastError` if the plan exceeds ``max_mb`` or the
    extraction returns other hours than asked for. ``extractor_options`` go to the
    ``Extractor`` constructor (tests pass a small grid, a cache folder and a clock).
    """
    modules = _forecast_modules()
    extract, fields = modules["extract"], modules["fields"]
    specs = tuple(fields.resolve_fields(list(field_names)))
    with only_hours(hours) as wanted:
        plan = extract.plan_run(bucket, run_start, fields=specs)
        if plan["hours"] != wanted:
            raise PairForecastError(f"planned hours {plan['hours']} differ from {wanted}")
        if plan["total_bytes"] > max_mb * 1e6:
            raise PairForecastError(
                f"planned {plan['total_bytes'] / 1e6:.1f} MB of GRIB exceeds the cap of "
                f"{max_mb:g} MB; nothing downloaded"
            )
        if plan_only:
            return {"plan": plan, "result": None}
        extractor = extract.Extractor(
            storage, bucket, manifests, lag_model=lag_model, **dict(extractor_options or {})
        )
        options = extract.ExtractOptions(
            fields=specs,
            viewpoint_ids=(viewpoint_id,),
            max_bytes=int(max_mb * 1e6),
            workers=2,
        )
        result = extractor.extract(run_start, options)
    if list(result.hours) != wanted:
        raise PairForecastError(f"extracted hours {result.hours} differ from {wanted}")
    event = result.to_event()
    event["files"] = list(result.written)
    return {"plan": plan, "result": event}


def fetch_pair_forecast(
    viewpoint_id: str,
    valid_time: datetime,
    lead_hours: float,
    *,
    storage: Any,
    manifests: Any | None = None,
    bucket: Any | None = None,
    field_names: Sequence[str] = SCENE_FIELDS,
    max_mb: float = DEFAULT_MAX_MB,
    plan_only: bool = False,
) -> dict[str, Any]:
    """Choose the run for (valid − lead, valid) and extract its two bracketing hours.

    Returns a JSON-ready summary: the choice (with the availability rule), the plan, the
    extractor's run event, the forecast file's path, SHA-256 and recorded availability, the
    code that ran and wall times. An existing forecast file that already holds these fields
    and hours is reused (the extractor skips it and says so), but only when the availability
    it records for the two hours equals the choice's (see the module docstring); otherwise
    :class:`PairForecastError`.
    """
    from lookout_common.manifest import load_manifests
    from lookout_common.paths import forecast_key
    from lookout_common.timeutil import isoformat_z

    modules = _forecast_modules()
    hrrr = modules["hrrr"]
    issue_time = valid_time - timedelta(hours=lead_hours)
    started = time.monotonic()
    choice, model = choose_run(issue_time, valid_time, storage=storage)
    chose_s = time.monotonic() - started
    hours = sorted({choice.fh_lo, choice.fh_hi})
    key = forecast_key(hrrr.STORAGE_SOURCE, viewpoint_id, choice.run_start_utc)
    # A file that already holds these fields and hours is reused: never download twice.
    reused = not plan_only and modules["extract"].run_covered(
        storage,
        choice.run_start_utc,
        viewpoint_ids=[viewpoint_id],
        field_names=list(field_names),
        hours=hours,
    )
    opened = None
    if bucket is None:
        opened = modules["HrrrBucket"]()
        bucket = opened.__enter__()
    try:
        t0 = time.monotonic()
        done = extract_hours(
            storage=storage,
            bucket=bucket,
            manifests=manifests if manifests is not None else load_manifests(),
            run_start=choice.run_start_utc,
            hours=hours,
            viewpoint_id=viewpoint_id,
            field_names=field_names,
            max_mb=max_mb,
            lag_model=model,
            plan_only=plan_only or reused,
        )
        extract_s = time.monotonic() - t0
    finally:
        if opened is not None:
            opened.__exit__(None, None, None)
    file_info = None
    if not plan_only:
        if not storage.exists(key):
            raise PairForecastError(f"the extractor wrote no {key}")
        data = storage.get_bytes(key)
        stored = stored_availability(json.loads(data), hours)
        chosen = choice.to_dict()["availability_utc"]
        if stored != _utc(chosen):
            how = "reused" if reused else "written"
            raise PairForecastError(
                f"{key} ({how}) records f{hours[0]:02d}-f{hours[-1]:02d} available at "
                f"{isoformat_z(stored)}, but the lag model now gives {chosen}; a pair's "
                "leakage check uses the file's value. Re-extract the run (the extractor's "
                "force option) before pairing it with a summary"
            )
        file_info = {
            "key": key,
            "path": str(storage.path(key)),
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "availability_utc": isoformat_z(stored),
        }
    return {
        "kind": "lookout.pair-forecast",
        "viewpoint_id": viewpoint_id,
        "valid_time_utc": isoformat_z(valid_time),
        "issue_time_utc": isoformat_z(issue_time),
        "lead_nominal_h": lead_hours,
        "choice": choice.to_dict(),
        "hours": hours,
        "fields": list(field_names),
        "max_mb": max_mb,
        "plan": {
            "total_bytes": done["plan"]["total_bytes"],
            "bytes_by_fh": done["plan"]["bytes_by_fh"],
            "bytes_by_field_by_fh": done["plan"]["bytes_by_field_by_fh"],
            "fields_missing_from_idx": done["plan"]["fields_missing_from_idx"],
            "transfer": done["plan"]["transfer"],
        },
        "result": done["result"],
        "reused_existing_file": reused,
        "file": file_info,
        "code": _forecast_source(),
        "timings_s": {"choose": round(chose_s, 3), "plan_and_extract": round(extract_s, 3)},
    }


def _parse_time(text: str) -> datetime:
    from lookout_common.timeutil import parse_iso_utc

    return parse_iso_utc(text)


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m lookout_eval.pair_forecast``: print the pair forecast summary as JSON."""
    parser = argparse.ArgumentParser(
        prog="python -m lookout_eval.pair_forecast",
        description="Choose and extract the HRRR run a backtest pair needs (NOAA, network).",
    )
    parser.add_argument("--viewpoint", required=True)
    parser.add_argument("--valid", required=True, type=_parse_time, help="valid time, ISO 8601")
    parser.add_argument(
        "--lead", required=True, type=float, help="lead in hours (issue = valid - lead)"
    )
    parser.add_argument("--max-mb", type=float, default=DEFAULT_MAX_MB, help="GRIB cap, MB")
    parser.add_argument(
        "--fields",
        nargs="+",
        default=list(SCENE_FIELDS),
        help="inventory names (default: the scene's)",
    )
    parser.add_argument(
        "--plan-only", action="store_true", help="read the inventories only; write nothing"
    )
    args = parser.parse_args(argv)
    from lookout_common.storage import storage_from_env

    try:
        summary = fetch_pair_forecast(
            args.viewpoint,
            args.valid,
            args.lead,
            storage=storage_from_env(),
            field_names=args.fields,
            max_mb=args.max_mb,
            plan_only=args.plan_only,
        )
    except PairForecastError as exc:
        print(f"pair_forecast: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
