"""The backtest pair's forecast run, chosen and extracted the worker's way (PRD §8.3, §16 M0.5).

PRD §13.2 design H pairs an archived frame with "archived HRRR runs (exact run times;
availability modelled)". This module does that for one pair with snow-globe-forecast's own
code, and nothing else:

- **Choice** (:func:`choose_run`): snow-globe-forecast's ``select_run_for_issue`` with its
  ``LagModel`` read from the live lag log in ``forecast_log/`` and its
  ``listed_availability`` (from lookout-forecast 0.2.0, D-054). It returns the newest run
  whose bracketing forecast hours were available by the issue time. An archived hour's
  availability is modelled as run start + the lag model's lag (rule 1: p95 per cycle and
  hour once 20 lags are logged; rule 2: the later of the hour's pooled p95 and the cell's
  largest lag; else a conservative 120 min), but never before the hour's own trusted S3
  stamps: each candidate run is listed (one ListObjectsV2 request a run), an hour whose
  stamps are later than the model takes them (``archived-s3-lastmodified``), and an hour not
  on S3 skips the run. The extractor applies the same rule when it writes the file. The
  choice records which rule applied and the stamp floor (``availability_model``).
- **Extraction** (:func:`extract_hours`): snow-globe-forecast's ``Extractor`` for one
  viewpoint, the fields the v−1 scene reads (:data:`SCENE_FIELDS`) and only the two
  bracketing hours, under a GRIB byte cap (default 60 MB) that the extractor checks against
  the ``.idx`` plan before any GRIB byte is fetched. GRIB subsets are deleted after decoding
  (the extractor does that, CLAUDE.md "NOAA").

**Why the hour list is substituted.** The worker's CLI (``snow-globe-forecast run --max-fh N``)
always extracts hours 0..N. A 24 h lead needs hours 24–33 of a run, so the CLI would fetch
25–34 hours. With these fields an hour of run 2024011412 planned 16.5 MB (f32), so f00–f32
would be about 545 MB, against 35 MB for the two hours. For the one call,
``snow_globe_forecast.extract.run_hours`` is replaced by the two hours the pair needs, and the
result is checked against them. If the substitution stopped working, the planner and the
extractor would see every hour of the run (f00–f48 for a 00/06/12/18Z run): the planned
hours would differ from the two asked for, and the byte cap would refuse the run, before
anything is downloaded (both are tested).
TODO(M1.5): use the forecast worker's explicit hour list (``ExtractOptions.hours``,
``plan_run(hours=)``; from lookout-forecast 0.2.0, D-053), which the pair now requires anyway,
instead of the substitution.

**Availability is checked against the file.** The pair's leakage check uses the availability
the forecast file records for the bracketing hours, while the choice here is made with the lag
model as it is now. A reused file was written with the model of its day, which can move as
lags accumulate, or by lookout-forecast 0.1.0, before the stamp floor, so the summary is
refused (:class:`PairForecastError`) unless the file's recorded availability of the two hours
equals the choice's; the summary carries it as ``file.availability_utc``. The refusal names
each side's source, lag and stamp floor, and says what would make them agree: re-extracting a
reused archived file, never a file written in the same call or a live run's file, which
re-extraction would write the same way again.

Besides the forecast file (``forecast/hrrr/<viewpoint>/<run>z.json``), the extractor writes
what it always writes: its state marker (``state/forecast/hrrr/<run>z.json``), one ``run``
event in ``forecast_log/<day>.jsonl`` (an archived run writes no ``lag`` events) and its lock.
The lag readers ignore ``run`` events, but the event sits in the live worker log.
TODO(M1.5): read the lag log from the live data dir but let the extractor write to a scratch
storage, then copy only the forecast file into ``forecast/hrrr/``.

This module imports only the standard library, snow-globe-common and snow-globe-forecast, so it
runs inside snow-globe-forecast's environment, which has ecCodes. It needs ``listed_availability``
(committed in 08a996c: lookout-forecast 0.2.0, which is snow-globe-forecast 0.3.0 after the
rename, D-080), and refuses a worker without it. The M0.5 pair ran it, before the stamp floor,
from the frozen snapshot of committed code that the background jobs use; that snapshot (034c46b)
holds lookout-forecast 0.1.0, so a new run needs a snapshot of a later commit::

    cd workers/forecast
    PYTHONPATH=$RT/workers/forecast/src:$RT/workers/common/src:<repo>/eval/src \\
    SNOW_GLOBE_MANIFEST_DIR=$RT/viewpoints/manifests \\
    uv run --no-sync python -m snow_globe_eval.pair_forecast \\
        --viewpoint grand-canyon.yavapai --valid 2024-01-15T19:45:00Z --lead 24

``--plan-only`` reads the listing and the two ``.idx`` inventories (45,515 bytes for run
2024011412) and writes nothing. Since the stamp floor, the choice also lists each candidate run
it weighs: for the M0.5 pair 2024011418 and 2024011412, two requests (their recorded listings
are 24,393 bytes each). The summary counts those apart, as ``choice_transfer``.
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
    """snow-globe-forecast's modules, imported lazily (they need ecCodes)."""
    try:
        from snow_globe_forecast import __version__ as forecast_version
        from snow_globe_forecast import availability, extract, fields, hrrr
        from snow_globe_forecast.bucket import HrrrBucket
    except ImportError as exc:
        raise PairForecastError(
            "snow-globe-forecast is not importable here: run inside workers/forecast's environment "
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
    """Where the snow-globe-forecast code that ran was loaded from (provenance for the record)."""
    import snow_globe_common
    import snow_globe_forecast

    return {
        "snow_globe_forecast_file": str(Path(snow_globe_forecast.__file__).resolve()),
        "snow_globe_forecast_version": getattr(snow_globe_forecast, "__version__", None),
        "snow_globe_common_file": str(Path(snow_globe_common.__file__).resolve()),
    }


@contextlib.contextmanager
def only_hours(hours: Sequence[int]) -> Iterator[list[int]]:
    """Make snow-globe-forecast's extractor and planner see only ``hours`` for one call.

    Replaces ``snow_globe_forecast.extract.run_hours`` (which returns 0..max_fh) by a function
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
    from snow_globe_common.timeutil import parse_iso_utc, to_utc

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
    issue_time: datetime,
    valid_time: datetime,
    *,
    storage: Any,
    bucket: Any,
    lag_model: Any | None = None,
) -> tuple[Any, Any]:
    """snow-globe-forecast's run choice for (issue, valid): ``(RunChoice, LagModel)`` (PRD §8.3).

    Every hour is an archived run's: modelled with the lag model, read from ``storage``'s
    forecast log unless one is given, but never before the hour's own trusted S3 stamps
    (D-054; PRD §13.9: an early time would leak a run into a prediction issued before it was
    published). ``listed_availability(bucket)`` lists each candidate run once, so a run whose
    stamps are later than the model is chosen by its stamps, or skipped for an older run if
    they are later than the issue time; an hour not on S3 skips the run. That is the rule the
    extractor applies when it writes the run, so a file it writes records the choice's
    availability. Raises :class:`PairForecastError` when no run within the selector's
    look-back covers the valid time, or when snow-globe-forecast predates the stamp floor (0.1.0
    has no ``listed_availability``).
    """
    modules = _forecast_modules()
    availability = modules["availability"]
    listed = getattr(availability, "listed_availability", None)
    if listed is None:
        raise PairForecastError(
            f"snow-globe-forecast {modules['version']} has no listed_availability: the pair's "
            "choice needs 0.2.0 or later, which never dates an archived hour before its own S3 "
            "stamps (D-054)"
        )
    model = lag_model if lag_model is not None else availability.LagModel.from_storage(storage)
    choice = availability.select_run_for_issue(
        issue_time, valid_time, lag_model=model, availability=listed(bucket)
    )
    if choice is None:
        raise PairForecastError(
            "no HRRR run covers the valid time with availability by the issue time (modelled, "
            "and never before the run's own S3 stamps)"
        )
    return choice, model


def _hour_note(entry: Mapping[str, Any] | None) -> str:
    """How one recorded hour (or the choice's deciding hour) got its availability."""
    if not entry:
        return "not recorded"
    model = entry.get("availability_model") or {}
    lag = model.get("lag_s")
    if "s3_floor_utc" not in model:
        floor = "S3 floor not recorded"  # written before the stamp floor, or a live hour
    elif model["s3_floor_utc"] is None:
        floor = "no trusted S3 stamp"
    else:
        floor = f"S3 floor {model['s3_floor_utc']}"
    return ", ".join(
        [
            str(entry.get("availability_source") or "source not recorded"),
            f"model {model.get('method')} {lag:g} s"
            if isinstance(lag, int | float)
            else "no model",
            floor,
        ]
    )


def _mismatch(
    key: str,
    how: str,
    doc: Mapping[str, Any],
    hours: Sequence[int],
    stored: datetime,
    choice: Mapping[str, Any],
) -> str:
    """Why a forecast file's availability differs from the choice's, and what would fix it.

    Re-extracting helps only a reused archived file: one written with another lag model, or by
    snow-globe-forecast 0.1.0, before the stamp floor (D-054). A file written in this call used
    the same model and the same rule, and a live run's file is dated by its stamps alone;
    re-extracting either would write the same availability again.
    """
    from snow_globe_common.timeutil import isoformat_z

    recorded = {
        int(entry["fh"]): entry
        for entry in doc.get("hours") or []
        if isinstance(entry, Mapping) and entry.get("fh") is not None
    }
    deciding = max(
        (fh for fh in hours if recorded.get(int(fh), {}).get("availability_utc")),
        key=lambda fh: _utc(recorded[int(fh)]["availability_utc"]),
        default=hours[-1],
    )
    text = (
        f"{key} ({how}) records f{hours[0]:02d}-f{hours[-1]:02d} available at "
        f"{isoformat_z(stored)} (f{deciding:02d}: {_hour_note(recorded.get(int(deciding)))}), but "
        f"the choice gives {choice['availability_utc']} ({_hour_note(choice)}); a pair's leakage "
        "check uses the file's value. "
    )
    if doc.get("live") is True:
        return text + (
            "The extractor dated the run as live (within its live window of the run start), by "
            "its S3 stamps alone; re-extracting would write the same. A backtest pair needs an "
            "archived run"
        )
    if how == "written":
        return text + (
            "The extractor wrote it in this call with the same lag model and the same stamp "
            "rule, so re-extracting would write the same; the two can differ only if the run's "
            "S3 stamps changed between the choice's listing and the extractor's. Choose again "
            "before pairing it"
        )
    return text + (
        "It was written with another lag model, or before the stamp floor (snow-globe-forecast "
        "0.1.0): re-extract the run (the extractor's force option), which records the choice's "
        "availability, before pairing it with a summary"
    )


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
    (``snow_globe_forecast.extract.plan_run``) and the extractor's ``run`` event (None when
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
    extractor_options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Choose the run for (valid − lead, valid) and extract its two bracketing hours.

    Returns a JSON-ready summary: the choice (with the availability rule and stamp floor),
    the listing requests the choice made (``choice_transfer``), the plan, the extractor's run
    event, the forecast file's path, SHA-256 and recorded availability, the code that ran and
    wall times. An existing forecast file that already holds these fields and hours is reused
    (the extractor skips it and says so), but only when the availability it records for the
    two hours equals the choice's (see the module docstring); otherwise
    :class:`PairForecastError`, saying why they differ. ``extractor_options`` go to the
    ``Extractor`` (:func:`extract_hours`; tests pass a small grid, a cache folder and a clock).
    """
    from snow_globe_common.manifest import load_manifests
    from snow_globe_common.paths import forecast_key
    from snow_globe_common.timeutil import isoformat_z

    modules = _forecast_modules()
    hrrr = modules["hrrr"]
    issue_time = valid_time - timedelta(hours=lead_hours)
    with contextlib.ExitStack() as stack:
        if bucket is None:
            bucket = stack.enter_context(modules["HrrrBucket"]())
        ledger = getattr(bucket, "ledger", None)  # snow-globe-forecast's HrrrBucket counts requests
        before = ledger.snapshot() if ledger is not None else None
        started = time.monotonic()
        choice, model = choose_run(issue_time, valid_time, storage=storage, bucket=bucket)
        chose_s = time.monotonic() - started
        choice_transfer = ledger.since(before) if ledger is not None else None
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
            extractor_options=extractor_options,
        )
        extract_s = time.monotonic() - t0
    file_info = None
    if not plan_only:
        if not storage.exists(key):
            raise PairForecastError(f"the extractor wrote no {key}")
        data = storage.get_bytes(key)
        doc = json.loads(data)
        stored = stored_availability(doc, hours)
        chosen = choice.to_dict()
        if stored != _utc(chosen["availability_utc"]):
            how = "reused" if reused else "written"
            raise PairForecastError(_mismatch(key, how, doc, hours, stored, chosen))
        file_info = {
            "key": key,
            "path": str(storage.path(key)),
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "availability_utc": isoformat_z(stored),
        }
    return {
        "kind": "snow-globe.pair-forecast",
        "viewpoint_id": viewpoint_id,
        "valid_time_utc": isoformat_z(valid_time),
        "issue_time_utc": isoformat_z(issue_time),
        "lead_nominal_h": lead_hours,
        "choice": choice.to_dict(),
        "choice_transfer": choice_transfer,
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
    from snow_globe_common.timeutil import parse_iso_utc

    return parse_iso_utc(text)


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m snow_globe_eval.pair_forecast``: print the pair forecast summary as JSON."""
    parser = argparse.ArgumentParser(
        prog="python -m snow_globe_eval.pair_forecast",
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
    from snow_globe_common.storage import storage_from_env

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
