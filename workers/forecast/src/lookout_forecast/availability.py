"""Availability, lead time and which run an issued prediction may use (PRD §8.3, §13.2, §13.9).

- **Lead** = valid time - availability time (PRD §8.3).
- **Live runs:** an hour's availability is its S3 ``LastModified`` (the later of the GRIB2
  file and its ``.idx``; see :mod:`.discovery`). Each live hour writes a ``lag`` event
  to ``forecast_log/<day>.jsonl``.
- **Archived runs:** availability is *modelled* as run start + a publication lag from
  :class:`LagModel`, and it must never come out early (an early time leaks a run into
  a prediction issued before it existed; PRD §13.9). The model is keyed by the run's
  cycle hour as well as the forecast hour, because cycles differ: in 124 runs
  (2026-11-14 06Z to 2026-11-19 09Z, every hour listed), all five 23Z runs published
  f06-f18 about 111 min after their start (medians 110.9-111.1 min), while the other
  cycles' medians were 62.6-85.1 min. The p95 pooled over every cycle (73.7-100.8 min)
  would have modelled those 23Z hours 10-37 min early.
- :func:`select_run_for_issue` picks, for an issue time and a valid time, the newest run
  whose needed hours were available by the issue time. 24 h leads can only come from the
  00/06/12/18Z runs (the others stop at 18 h), so their actual leads are 24-30 h; the
  actual lead is returned, never the nominal one. A missing run falls back to an older
  one and is never dropped. :func:`select_stored_run` does this against the forecast
  files in storage, which is what evaluation and the prediction issuer should use.
"""

from __future__ import annotations

import enum
import itertools
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from lookout_common.storage import Storage
from lookout_common.timeutil import isoformat_z, parse_iso_utc

from .discovery import RunListing
from .hrrr import as_run_start, floor_hour, format_run_id, max_fh

__all__ = [
    "FALLBACK_LAG_MIN",
    "MIN_SAMPLES",
    "TYPICAL_LAG_ANCHORS_MIN",
    "AvailabilityFn",
    "AvailabilitySource",
    "LagModel",
    "Lookup",
    "RunChoice",
    "availability_from_document",
    "lag_events",
    "p95_nearest_rank",
    "read_lag_events",
    "select_run_for_issue",
    "select_stored_run",
    "stored_availability",
    "stored_run_exists",
    "typical_lag_s",
]

AvailabilitySource = Literal["live-s3-lastmodified", "live-fetch-finished", "modeled"]

#: (forecast hour, minutes after run start): the *typical* lags in docs/research/forecast.md
#: §2 ("f00 ~52 min, f18 ~85 min, f48 ~108 min"). Not used for availability: 37.3 % of
#: the 2,986 hour-lags of the 124-run survey were later than this curve.
TYPICAL_LAG_ANCHORS_MIN: tuple[tuple[int, float], ...] = ((0, 52.0), (18, 85.0), (48, 108.0))
#: Lag used until the log can say better: 2 h for every hour. In the 124-run survey, 3 of
#: 2,986 hour-lags (0.10 %) were later (all from 2026-11-17 15Z, latest 131.2 min); every
#: per-hour p95 was below it (highest 116.7 min, f48), and so was every 23Z lag (latest
#: 114.7 min). The PRD's p95 rule itself accepts 5 % late hours.
FALLBACK_LAG_MIN = 120.0
FALLBACK_LAG_S = FALLBACK_LAG_MIN * 60.0
#: Logged lags a cell needs before its p95 is used.
MIN_SAMPLES = 20
LAG_EVENT = "lag"

#: How a modelled lag was chosen (``availability_model.method`` in forecast files).
METHOD_P95_CYCLE = "p95-cycle-hour"
METHOD_P95_POOLED = "p95-forecast-hour-or-cycle-max"
METHOD_FALLBACK = "conservative-fallback"


def typical_lag_s(fh: int) -> float:
    """Typical publication lag in seconds (forecast.md): piecewise linear through the anchors.

    Kept as a reference for evidence reports; availability never uses it.
    """
    if fh < 0:
        raise ValueError("forecast hour must be >= 0")
    anchors = TYPICAL_LAG_ANCHORS_MIN
    for (h0, m0), (h1, m1) in itertools.pairwise(anchors):
        if h0 <= fh <= h1:
            return 60.0 * (m0 + (m1 - m0) * (fh - h0) / (h1 - h0))
    last_h, last_m = anchors[-1]
    prev_h, prev_m = anchors[-2]
    return 60.0 * (last_m + (last_m - prev_m) * (fh - last_h) / (last_h - prev_h))


def p95_nearest_rank(values: Sequence[float]) -> float:
    """The 95th percentile by the nearest-rank method (never below the data's own values)."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    rank = math.ceil(0.95 * len(ordered))
    return ordered[max(rank, 1) - 1]


def lag_events(listing: RunListing, hours: Iterable[int] | None = None) -> list[dict[str, Any]]:
    """``lag`` log records (run, fh, last_modified, lag_s) for the ready hours of a live run."""
    wanted = set(listing.ready_hours if hours is None else hours)
    events: list[dict[str, Any]] = []
    for fh in listing.ready_hours:
        if fh not in wanted:
            continue
        files = listing.hour(fh)
        available = files.availability_utc
        assert available is not None and files.grib is not None and files.idx is not None
        events.append(
            {
                "event": LAG_EVENT,
                "source": "noaa-hrrr-bdp-pds",
                "run": listing.run_id,
                "fh": fh,
                "last_modified": isoformat_z(available),
                "lag_s": round((available - listing.run_start).total_seconds(), 3),
                "grib_last_modified": isoformat_z(files.grib.last_modified),
                "idx_last_modified": isoformat_z(files.idx.last_modified),
                "grib_size": files.grib.size,
                "synoptic": max_fh(listing.run_start) == 48,
            }
        )
    return events


def read_lag_events(storage: Storage, *, since: datetime | None = None) -> list[dict[str, Any]]:
    """Every ``lag`` event in ``forecast_log/``, de-duplicated by (run, fh), first kept.

    ``since`` skips log days before that date (the file name is the UTC day it was written).
    Records without a parseable ``YYYYMMDDHH`` run, an integer hour or a numeric lag are
    skipped.
    """
    from lookout_common.jsonl import iter_jsonl_bytes

    seen: set[tuple[str, int]] = set()
    out: list[dict[str, Any]] = []
    for key in storage.list_prefix("forecast_log/"):
        if not key.endswith(".jsonl"):
            continue
        day = key.removeprefix("forecast_log/").removesuffix(".jsonl")
        if since is not None and day < since.astimezone(UTC).strftime("%Y-%m-%d"):
            continue
        for record in iter_jsonl_bytes(storage.get_bytes(key)):
            if record.get("event") != LAG_EVENT:
                continue
            try:
                ident = (str(record["run"]), int(record["fh"]))
                float(record["lag_s"])
                _cycle_hour(ident[0])
            except (KeyError, TypeError, ValueError):
                continue
            if ident in seen:
                continue
            seen.add(ident)
            out.append(record)
    return out


def _cycle_hour(run_id: str) -> int:
    """The cycle hour (0-23) of a ``YYYYMMDDHH`` run id."""
    if len(run_id) != 10 or not run_id.isdigit() or int(run_id[8:]) > 23:
        raise ValueError(f"bad run id {run_id!r}")
    return int(run_id[8:])


Cell = tuple[int, int]  # (cycle hour, forecast hour)


@dataclass(frozen=True)
class LagModel:
    """Per-(cycle hour, forecast hour) publication lag, never knowingly early.

    For hour ``fh`` of a run started at cycle hour ``c``:

    1. once (c, fh) has :data:`MIN_SAMPLES` logged live lags: their p95
       (``p95-cycle-hour``);
    2. else, once fh has :data:`MIN_SAMPLES` lags over all cycles and (c, fh) has at
       least one: the later of that pooled p95 (the PRD §8.3 rule) and the largest lag
       logged for (c, fh) (``p95-forecast-hour-or-cycle-max``), so a cycle that runs late,
       like 23Z, shows up after its first logged run;
    3. else :data:`FALLBACK_LAG_MIN`, or the largest lag logged for (c, fh) if that is
       later (``conservative-fallback``).
    """

    p95_s: Mapping[Cell, float] = field(default_factory=dict)
    max_s: Mapping[Cell, float] = field(default_factory=dict)
    samples: Mapping[Cell, int] = field(default_factory=dict)
    pooled_p95_s: Mapping[int, float] = field(default_factory=dict)
    pooled_samples: Mapping[int, int] = field(default_factory=dict)
    min_samples: int = MIN_SAMPLES

    @classmethod
    def from_events(
        cls, events: Iterable[Mapping[str, Any]], *, min_samples: int = MIN_SAMPLES
    ) -> LagModel:
        """Build from ``lag`` events (as written by :func:`lag_events`)."""
        by_cell: dict[Cell, list[float]] = {}
        by_fh: dict[int, list[float]] = {}
        for event in events:
            lag = float(event["lag_s"])
            if lag < 0:
                continue  # a clock or listing error; never let it make data look early
            fh = int(event["fh"])
            by_cell.setdefault((_cycle_hour(str(event["run"])), fh), []).append(lag)
            by_fh.setdefault(fh, []).append(lag)
        return cls(
            p95_s={cell: p95_nearest_rank(lags) for cell, lags in by_cell.items()},
            max_s={cell: max(lags) for cell, lags in by_cell.items()},
            samples={cell: len(lags) for cell, lags in by_cell.items()},
            pooled_p95_s={fh: p95_nearest_rank(lags) for fh, lags in by_fh.items()},
            pooled_samples={fh: len(lags) for fh, lags in by_fh.items()},
            min_samples=min_samples,
        )

    @classmethod
    def from_storage(
        cls, storage: Storage, *, since: datetime | None = None, min_samples: int = MIN_SAMPLES
    ) -> LagModel:
        """Build from the forecast log in ``storage``."""
        return cls.from_events(read_lag_events(storage, since=since), min_samples=min_samples)

    def lag_s(self, run_start: datetime, fh: int) -> tuple[float, str, int]:
        """(seconds, method, samples logged for this cycle and hour) for one forecast hour."""
        if fh < 0:
            raise ValueError("forecast hour must be >= 0")
        cell = (as_run_start(run_start).hour, fh)
        n = int(self.samples.get(cell, 0))
        if n >= self.min_samples and cell in self.p95_s:
            return float(self.p95_s[cell]), METHOD_P95_CYCLE, n
        largest = self.max_s.get(cell)
        if (
            largest is not None
            and self.pooled_samples.get(fh, 0) >= self.min_samples
            and fh in self.pooled_p95_s
        ):
            return max(float(self.pooled_p95_s[fh]), float(largest)), METHOD_P95_POOLED, n
        return max(FALLBACK_LAG_S, float(largest or 0.0)), METHOD_FALLBACK, n

    def availability(self, run_start: datetime, fh: int) -> datetime:
        """Modelled availability of one hour of an archived run."""
        return as_run_start(run_start) + timedelta(seconds=self.lag_s(run_start, fh)[0])

    def describe(self, run_start: datetime, fh: int) -> dict[str, Any]:
        """JSON note on how an hour's modelled availability was computed."""
        seconds, method, n = self.lag_s(run_start, fh)
        return {
            "method": method,
            "lag_s": round(seconds, 3),
            "samples": n,
            "pooled_samples": int(self.pooled_samples.get(fh, 0)),
            "min_samples": self.min_samples,
        }


@dataclass(frozen=True)
class RunChoice:
    """The run (and hours) a prediction issued at ``issue_time_utc`` may use."""

    issue_time_utc: datetime
    valid_time_utc: datetime
    run_start_utc: datetime
    fh_lo: int
    fh_hi: int
    weight_hi: float
    availability_utc: datetime
    availability_source: AvailabilitySource
    lead_h: float
    lead_from_run_start_h: float
    nominal_lead_h: float
    skipped: tuple[tuple[str, str], ...] = ()
    #: :meth:`LagModel.describe` of the hour that set a modelled availability (else None).
    availability_model: Mapping[str, Any] | None = None

    @property
    def run_id(self) -> str:
        """``YYYYMMDDHH`` of the chosen run."""
        return format_run_id(self.run_start_utc)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form for prediction logs."""
        return {
            "issue_time_utc": isoformat_z(self.issue_time_utc),
            "valid_time_utc": isoformat_z(self.valid_time_utc),
            "run": self.run_id,
            "fh_lo": self.fh_lo,
            "fh_hi": self.fh_hi,
            "weight_hi": self.weight_hi,
            "availability_utc": isoformat_z(self.availability_utc),
            "availability_source": self.availability_source,
            "availability_model": (
                dict(self.availability_model) if self.availability_model is not None else None
            ),
            "lead_h": self.lead_h,
            "lead_from_run_start_h": self.lead_from_run_start_h,
            "nominal_lead_h": self.nominal_lead_h,
            "skipped": [list(item) for item in self.skipped],
        }


class Lookup(enum.Enum):
    """What an availability callback returns when it has no known time for an hour."""

    #: Model the hour with the lag model (an archived run's hour). ``None`` means the same.
    USE_MODEL = "use-model"
    #: The hour cannot be used: no stored file for the run, or the file lacks the hour.
    UNAVAILABLE = "unavailable"


HourAvailability = tuple[datetime, AvailabilitySource] | Lookup | None
AvailabilityFn = Callable[[datetime, int], HourAvailability]


def select_run_for_issue(
    issue_time_utc: datetime,
    valid_time_utc: datetime,
    *,
    lag_model: LagModel | None = None,
    availability: AvailabilityFn | None = None,
    run_exists: Callable[[datetime], bool] | None = None,
    max_lookback_h: int = 60,
) -> RunChoice | None:
    """The newest run whose needed forecast hours were available by the issue time.

    ``valid_time_utc`` may fall between hours: then both bracketing hours are needed
    (values are interpolated linearly, PRD §8.3) and the later availability counts.
    ``availability(run, fh)`` gives a known time (e.g. from a stored live file),
    :attr:`Lookup.USE_MODEL` or None to model the hour with ``lag_model``, or
    :attr:`Lookup.UNAVAILABLE` to skip the run. ``run_exists(run)`` returning False also
    skips a run. A skipped run falls back to an older one. Without ``availability``,
    every hour is modelled (no storage is consulted): use :func:`select_stored_run` to
    choose among the runs actually stored. Returns None if no run within
    ``max_lookback_h`` covers the valid time.
    """
    for label, moment in (("issue_time_utc", issue_time_utc), ("valid_time_utc", valid_time_utc)):
        if moment.tzinfo is None:
            raise ValueError(f"{label} must be timezone-aware")
    issue = issue_time_utc.astimezone(UTC)
    valid = valid_time_utc.astimezone(UTC)
    model = lag_model or LagModel()
    skipped: list[tuple[str, str]] = []
    newest = floor_hour(issue)
    for back in range(max_lookback_h + 1):
        run = newest - timedelta(hours=back)
        run_id = format_run_id(run)
        offset_h = (valid - run).total_seconds() / 3600.0
        if offset_h < 0:
            skipped.append((run_id, "valid time before run start"))
            continue
        fh_lo = math.floor(offset_h + 1e-9)
        fh_hi = math.ceil(offset_h - 1e-9)
        if fh_hi > max_fh(run):
            continue  # this cycle stops short of the valid time (e.g. 24 h from a 07Z run)
        if run_exists is not None and not run_exists(run):
            skipped.append((run_id, "run missing"))
            continue
        times: list[tuple[datetime, AvailabilitySource, Mapping[str, Any] | None]] = []
        unavailable: int | None = None
        for fh in sorted({fh_lo, fh_hi}):
            known = availability(run, fh) if availability is not None else None
            if known is Lookup.UNAVAILABLE:
                unavailable = fh
                break
            if known is None or known is Lookup.USE_MODEL:
                times.append((model.availability(run, fh), "modeled", model.describe(run, fh)))
            else:
                times.append((known[0], known[1], None))
        if unavailable is not None:
            skipped.append((run_id, f"f{unavailable:02d} not stored"))
            continue
        available, source, how = max(times, key=lambda item: item[0])
        if available > issue:
            skipped.append((run_id, f"not available until {isoformat_z(available)}"))
            continue
        weight = 0.0 if fh_hi == fh_lo else round(offset_h - fh_lo, 9)
        return RunChoice(
            issue_time_utc=issue,
            valid_time_utc=valid,
            run_start_utc=run,
            fh_lo=fh_lo,
            fh_hi=fh_hi,
            weight_hi=weight,
            availability_utc=available,
            availability_source=source,
            lead_h=round((valid - available).total_seconds() / 3600.0, 6),
            lead_from_run_start_h=round(offset_h, 6),
            nominal_lead_h=round((valid - issue).total_seconds() / 3600.0, 6),
            skipped=tuple(skipped),
            availability_model=how,
        )
    return None


def availability_from_document(
    doc: Mapping[str, Any], *, use_fetch_time: bool = True
) -> dict[int, HourAvailability]:
    """Per-hour availability a forecast file supports (for :func:`select_run_for_issue`).

    - Hours of a **live** file: the S3 availability, or, with ``use_fetch_time``, the
      file's ``fetch_finished_utc`` when that is later (``live-fetch-finished``): Lookout
      could not use the run before it had fetched it (design P, PRD §13.2).
    - Hours of an **archived** file: :attr:`Lookup.USE_MODEL`. The modelled time stored
      in the file is the lag model when the file was written; it is recomputed from the
      current log instead, so a file written before the log had enough lags never keeps
      an early time.

    Hours absent from the file are absent from the result.
    """
    out: dict[int, HourAvailability] = {}
    live = bool(doc.get("live"))
    fetched = parse_iso_utc(doc["fetch_finished_utc"]) if live and use_fetch_time else None
    for hour in doc.get("hours", []):
        fh = int(hour["fh"])
        if not live or hour.get("availability_source") != "live-s3-lastmodified":
            out[fh] = Lookup.USE_MODEL
            continue
        s3 = parse_iso_utc(hour["availability_utc"])
        if fetched is not None and fetched > s3:
            out[fh] = (fetched, "live-fetch-finished")
        else:
            out[fh] = (s3, "live-s3-lastmodified")
    return out


def stored_availability(
    storage: Storage, viewpoint_id: str, *, use_fetch_time: bool = True
) -> AvailabilityFn:
    """An ``availability`` callback backed by the stored forecast files of one viewpoint.

    Returns :attr:`Lookup.UNAVAILABLE` when the run has no stored file or the file lacks
    the hour, so :func:`select_run_for_issue` falls back to an older run; never a modelled
    time for a run Lookout does not hold. See :func:`availability_from_document` for live
    and archived files. Files are read once per run and cached for the callback's life,
    so make a new callback for each batch of selections.
    """
    from lookout_common.paths import forecast_key

    from .hrrr import STORAGE_SOURCE

    cache: dict[str, dict[int, HourAvailability] | None] = {}

    def lookup(run: datetime, fh: int) -> HourAvailability:
        run_id = format_run_id(run)
        if run_id not in cache:
            doc = storage.read_json(forecast_key(STORAGE_SOURCE, viewpoint_id, run), default=None)
            cache[run_id] = (
                availability_from_document(doc, use_fetch_time=use_fetch_time)
                if isinstance(doc, Mapping)
                else None
            )
        hours = cache[run_id]
        if hours is None or fh not in hours:
            return Lookup.UNAVAILABLE
        return hours[fh]

    return lookup


def stored_run_exists(storage: Storage, viewpoint_id: str) -> Callable[[datetime], bool]:
    """A ``run_exists`` callback: True when the viewpoint has a stored file for the run."""
    from lookout_common.paths import forecast_key

    from .hrrr import STORAGE_SOURCE

    def exists(run: datetime) -> bool:
        return storage.exists(forecast_key(STORAGE_SOURCE, viewpoint_id, run))

    return exists


def select_stored_run(
    storage: Storage,
    viewpoint_id: str,
    issue_time_utc: datetime,
    valid_time_utc: datetime,
    *,
    lag_model: LagModel | None = None,
    use_fetch_time: bool = True,
    max_lookback_h: int = 60,
) -> RunChoice | None:
    """:func:`select_run_for_issue` over the runs stored for ``viewpoint_id``.

    Live hours use their stored availability (bounded by the fetch time); archived hours
    use ``lag_model``, read from the storage's forecast log when not given. Runs or
    hours that are not stored are skipped.
    """
    return select_run_for_issue(
        issue_time_utc,
        valid_time_utc,
        lag_model=lag_model if lag_model is not None else LagModel.from_storage(storage),
        availability=stored_availability(storage, viewpoint_id, use_fetch_time=use_fetch_time),
        max_lookback_h=max_lookback_h,
    )
