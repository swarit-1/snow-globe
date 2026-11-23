"""Availability, lead time and which run an issued prediction may use (PRD §8.3, §13.2, §13.9).

- **Lead** = valid time - availability time (PRD §8.3).
- **Live runs:** an hour's availability is its S3 ``LastModified`` (the later of the GRIB2
  file and its ``.idx``; see :mod:`.discovery`). Each live hour writes a ``lag`` event
  to ``forecast_log/<day>.jsonl``.
- **Archived runs:** availability is *modelled* as run start + a publication lag from
  :class:`LagModel`, and it must never come out early (an early time leaks a run into
  a prediction issued before it existed; PRD §13.9). The model reads the live lags and
  the survey lags that ``lag-survey --log`` takes from S3 listings of past runs
  (:mod:`.laglog`; D-027, amending D-008), and counts the two apart. It is keyed by the
  run's cycle hour as well as the forecast hour, because cycles differ: in 124 runs
  (2026-11-14 06Z to 2026-11-19 09Z, every hour listed), all five 23Z runs published
  f06-f18 about 111 min after their start (medians 110.9-111.1 min), while the other
  cycles' medians were 62.6-85.1 min. The p95 pooled over every cycle (73.7-100.8 min)
  would have modelled those 23Z hours 10-37 min early.
- **An archived hour is never available before its own S3 stamps say it was published**
  (:func:`archived_availability`): the later of the model and
  :func:`.discovery.publication_floor`, which counts a stamp only within 24 h of the run
  start. The model is fitted on November 2026 lags, and older seasons published some hours
  later: on 20 Nov 2026 the model was earlier than the stamps of 2021031512 (18 of 49 hours,
  by up to 6.4 min), 2023071500 (17, up to 5.1 min) and 2025071500 f46 (5.1 min). Such an
  hour gets ``archived-s3-lastmodified`` and the stamp. Without stamps (a run neither stored
  nor listed), :func:`select_run_for_issue` can only model; :func:`listed_availability`
  lists each candidate run to apply the floor.
- **Survey lags enter the p95** like live ones. They are the same measurement (on 594
  (run, hour) pairs logged both ways on 20 Nov, the two lags were equal in all 594), a
  later listing can only show the same stamps or later ones, and the live log alone is
  censored: the local stand-in processes a run as soon as f00-f02 are up, so a later hour
  is logged live only if it was published before that moment (2026111911, processed at
  78.0 min, lacks f09, which came at 79.1 min, after f10-f13). Where a pair has both, the
  live lag is kept.
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
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from snow_globe_common.storage import Storage
from snow_globe_common.timeutil import isoformat_z, parse_iso_utc

from .bucket import HrrrBucket
from .discovery import RunListing, list_run, publication_floor
from .hrrr import as_run_start, floor_hour, format_run_id, max_fh, run_hours
from .laglog import (
    LAG_EVENT,
    METHOD_LIVE,
    METHODS,
    cycle_hour,
    lag_events,
    read_lag_events,
    usable_lag,
)
from .output import ARCHIVED_SOURCES

__all__ = [
    "ARCHIVED_SOURCES",
    "FALLBACK_LAG_MIN",
    "LAG_EVENT",
    "MIN_SAMPLES",
    "SOURCE_ARCHIVED_STAMP",
    "SOURCE_MODELED",
    "TYPICAL_LAG_ANCHORS_MIN",
    "ArchivedStamps",
    "AvailabilityFn",
    "AvailabilitySource",
    "LagModel",
    "Lookup",
    "RunChoice",
    "archived_availability",
    "availability_from_document",
    "lag_events",
    "listed_availability",
    "p95_nearest_rank",
    "read_lag_events",
    "select_run_for_issue",
    "select_stored_run",
    "stored_availability",
    "stored_run_exists",
    "typical_lag_s",
]

AvailabilitySource = Literal[
    "live-s3-lastmodified", "live-fetch-finished", "modeled", "archived-s3-lastmodified"
]
#: An archived hour whose modelled availability is not earlier than its trusted stamps.
SOURCE_MODELED: AvailabilitySource = "modeled"
#: An archived hour whose availability is its own trusted S3 stamps: in the worker's archived
#: files because they are later than the model (the stamps win, D-054); in a hindcast file
#: (``snow-globe-forecast hindcast``, under ``forecast/hindcast/``) always, by the hindcast's
#: rule, with ``availability_model`` the live lag model's prediction kept for comparison, which
#: may be later. The two are :data:`.output.ARCHIVED_SOURCES`, the file format's list (tested).
SOURCE_ARCHIVED_STAMP: AvailabilitySource = "archived-s3-lastmodified"

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


Cell = tuple[int, int]  # (cycle hour, forecast hour)


def _by_method(counts: Mapping[str, int] | None) -> dict[str, int]:
    """Counts for every known method, zeros included (a stable JSON shape)."""
    return {method: int((counts or {}).get(method, 0)) for method in METHODS}


@dataclass(frozen=True)
class LagModel:
    """Per-(cycle hour, forecast hour) publication lag, never knowingly early.

    For hour ``fh`` of a run started at cycle hour ``c``:

    1. once (c, fh) has :data:`MIN_SAMPLES` logged lags: their p95 (``p95-cycle-hour``);
    2. else, once fh has :data:`MIN_SAMPLES` lags over all cycles and (c, fh) has at
       least one: the later of that pooled p95 (the PRD §8.3 rule) and the largest lag
       logged for (c, fh) (``p95-forecast-hour-or-cycle-max``), so a cycle that runs late,
       like 23Z, shows up after its first logged run;
    3. else :data:`FALLBACK_LAG_MIN`, or the largest lag logged for (c, fh) if that is
       later (``conservative-fallback``).

    "Logged" means live and survey lags alike (D-027); the rules do not tell them apart
    (see the module docstring for why), but :attr:`samples_by_method` and
    :attr:`pooled_samples_by_method` count them apart, and ``methods`` in
    :meth:`from_events` can leave one out (``{"live"}`` gives the live-only model).
    Rules 2 and 3 never give a lag earlier than the latest one logged for the cell; rule 1
    is the PRD's p95, which accepts 5 % late hours.
    """

    p95_s: Mapping[Cell, float] = field(default_factory=dict)
    max_s: Mapping[Cell, float] = field(default_factory=dict)
    samples: Mapping[Cell, int] = field(default_factory=dict)
    pooled_p95_s: Mapping[int, float] = field(default_factory=dict)
    pooled_samples: Mapping[int, int] = field(default_factory=dict)
    min_samples: int = MIN_SAMPLES
    #: ``samples`` and ``pooled_samples`` split by method (``live``, ``survey``).
    samples_by_method: Mapping[Cell, Mapping[str, int]] = field(default_factory=dict)
    pooled_samples_by_method: Mapping[int, Mapping[str, int]] = field(default_factory=dict)

    @classmethod
    def from_events(
        cls,
        events: Iterable[Mapping[str, Any]],
        *,
        min_samples: int = MIN_SAMPLES,
        methods: Collection[str] | None = None,
    ) -> LagModel:
        """Build from ``lag`` events (as :func:`.laglog.lag_events` writes them).

        An event without ``method`` is live (every event logged before D-027).
        ``methods`` keeps only events of those methods; an unknown method raises
        ``ValueError``. Events that :func:`.laglog.read_lag_log` would count invalid
        (:func:`.laglog.usable_lag`: a negative lag, which must never make data look early,
        a lag above 24 h, which is a rewritten object's stamp, or a bad run or hour) are
        dropped. The events are used as given: :func:`.laglog.read_lag_events` has already
        kept one per (run, fh).
        """
        by_cell: dict[Cell, list[float]] = {}
        by_fh: dict[int, list[float]] = {}
        cell_methods: dict[Cell, Counter[str]] = {}
        fh_methods: dict[int, Counter[str]] = {}
        for event in events:
            method = str(event.get("method") or METHOD_LIVE)
            if method not in METHODS:
                raise ValueError(f"unknown lag method {method!r} in {dict(event)!r}")
            if methods is not None and method not in methods:
                continue
            usable = usable_lag(event)
            if usable is None:
                continue
            run_id, fh, lag = usable
            cell = (cycle_hour(run_id), fh)
            by_cell.setdefault(cell, []).append(lag)
            by_fh.setdefault(fh, []).append(lag)
            cell_methods.setdefault(cell, Counter())[method] += 1
            fh_methods.setdefault(fh, Counter())[method] += 1
        return cls(
            p95_s={cell: p95_nearest_rank(lags) for cell, lags in by_cell.items()},
            max_s={cell: max(lags) for cell, lags in by_cell.items()},
            samples={cell: len(lags) for cell, lags in by_cell.items()},
            pooled_p95_s={fh: p95_nearest_rank(lags) for fh, lags in by_fh.items()},
            pooled_samples={fh: len(lags) for fh, lags in by_fh.items()},
            min_samples=min_samples,
            samples_by_method={cell: dict(c) for cell, c in cell_methods.items()},
            pooled_samples_by_method={fh: dict(c) for fh, c in fh_methods.items()},
        )

    @classmethod
    def from_storage(
        cls,
        storage: Storage,
        *,
        since: datetime | None = None,
        min_samples: int = MIN_SAMPLES,
        methods: Collection[str] | None = None,
    ) -> LagModel:
        """Build from the live and survey lag logs in ``storage`` (:mod:`.laglog`)."""
        return cls.from_events(
            read_lag_events(storage, since=since, methods=methods), min_samples=min_samples
        )

    @property
    def lags_by_method(self) -> dict[str, int]:
        """How many logged lags the model holds, by method."""
        total: Counter[str] = Counter()
        for counts in self.pooled_samples_by_method.values():
            total.update(counts)
        return _by_method(total)

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
        cell = (as_run_start(run_start).hour, fh)
        return {
            "method": method,
            "lag_s": round(seconds, 3),
            "samples": n,
            "samples_by_method": _by_method(self.samples_by_method.get(cell)),
            "pooled_samples": int(self.pooled_samples.get(fh, 0)),
            "pooled_samples_by_method": _by_method(self.pooled_samples_by_method.get(fh)),
            "min_samples": self.min_samples,
        }

    def cell_summary(self) -> dict[str, Any]:
        """How each (cycle hour, forecast hour) cell HRRR produces is modelled.

        24 cycles x f00-f18 plus the four synoptic cycles' f19-f48: 576 cells. Counts the
        cells by method, how many have left the conservative fallback, and how many use the
        fallback method at a lag above :data:`FALLBACK_LAG_MIN` (a logged lag was later).
        """
        by_method: Counter[str] = Counter()
        above = 0
        day = datetime(2000, 1, 1, tzinfo=UTC)
        for cycle in range(24):
            run = day.replace(hour=cycle)
            for fh in run_hours(run):
                seconds, method, _ = self.lag_s(run, fh)
                by_method[method] += 1
                if method == METHOD_FALLBACK and seconds > FALLBACK_LAG_S:
                    above += 1
        cells = sum(by_method.values())
        return {
            "cells": cells,
            "by_method": {
                m: int(by_method.get(m, 0))
                for m in (METHOD_P95_CYCLE, METHOD_P95_POOLED, METHOD_FALLBACK)
            },
            "left_fallback": cells - int(by_method.get(METHOD_FALLBACK, 0)),
            "fallback_lag_min": FALLBACK_LAG_MIN,
            "fallback_cells_at_fallback_lag": int(by_method.get(METHOD_FALLBACK, 0)) - above,
            "fallback_cells_above_fallback_lag": above,
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
    #: For an archived hour (``modeled`` or ``archived-s3-lastmodified``), the note of
    #: :func:`archived_availability` for the hour that set the availability; else None.
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
    #: The hour cannot be used: no stored file for the run, the file lacks the hour, or (for
    #: :func:`listed_availability`) the hour is not on S3. The skip reason reads "not stored".
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ArchivedStamps:
    """An archived hour's S3 ``LastModified`` stamps, as a file records them or S3 lists them.

    An availability callback returns this for an hour to be modelled but never placed before
    what its stamps prove (:func:`archived_availability`).
    """

    grib_utc: datetime | None
    idx_utc: datetime | None


HourAvailability = tuple[datetime, AvailabilitySource] | ArchivedStamps | Lookup | None
AvailabilityFn = Callable[[datetime, int], HourAvailability]


def archived_availability(
    model: LagModel, run_start: datetime, fh: int, stamps: ArchivedStamps | None = None
) -> tuple[datetime, AvailabilitySource, dict[str, Any]]:
    """``(availability, source, note)`` of one hour of an archived run (PRD §8.3, §13.9).

    The modelled time (run start + :meth:`LagModel.lag_s`), unless the hour's own stamps
    prove it was published later: then the stamp floor of
    :func:`.discovery.publication_floor` (``archived-s3-lastmodified``). A stamp more than
    24 h after the run start is a rewritten object and is ignored. ``note`` is
    :meth:`LagModel.describe`, plus, when ``stamps`` are given, ``s3_floor_utc`` (None when no
    stamp is trusted) and ``s3_stamps_ignored``. Without ``stamps`` the result is the model
    alone, as before stamps were consulted.
    """
    start = as_run_start(run_start)
    modelled = model.availability(start, fh)
    note = model.describe(start, fh)
    if stamps is None:
        return modelled, SOURCE_MODELED, note
    floor, ignored = publication_floor(start, stamps.grib_utc, stamps.idx_utc)
    note["s3_floor_utc"] = isoformat_z(floor) if floor is not None else None
    note["s3_stamps_ignored"] = list(ignored)
    if floor is not None and floor > modelled:
        return floor, SOURCE_ARCHIVED_STAMP, note
    return modelled, SOURCE_MODELED, note


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
    :class:`ArchivedStamps` to model the hour but never before its stamps
    (:func:`archived_availability`), :attr:`Lookup.USE_MODEL` or None to model the hour
    with ``lag_model`` alone, or :attr:`Lookup.UNAVAILABLE` to skip the run.
    ``run_exists(run)`` returning False also skips a run. A skipped run falls back to an
    older one. Without ``availability``, every hour is modelled from the lag model alone (no
    storage or listing is consulted), which can be earlier than an archived run's own stamps:
    use :func:`select_stored_run` to choose among the runs actually stored, or pass
    :func:`listed_availability` to check the stamps on S3. Returns None if no run within
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
                times.append(archived_availability(model, run, fh))
            elif isinstance(known, ArchivedStamps):
                times.append(archived_availability(model, run, fh, known))
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
      file's ``fetch_finished_utc`` when that is later (``live-fetch-finished``): Snow Globe
      could not use the run before it had fetched it (design P, PRD §13.2).
    - Hours of an **archived** file: :class:`ArchivedStamps` from the hour's recorded
      ``grib_last_modified_utc`` and ``idx_last_modified_utc`` (:attr:`Lookup.USE_MODEL`
      if the file records neither). The availability stored in the file is what the lag
      model said when the file was written; it is recomputed from the current log instead,
      so a file written before the log had enough lags never keeps an early time, and the
      stamps keep the recomputed time from coming out before the hour was published.

    Hours absent from the file are absent from the result.
    """
    out: dict[int, HourAvailability] = {}
    live = bool(doc.get("live"))
    fetched = parse_iso_utc(doc["fetch_finished_utc"]) if live and use_fetch_time else None
    for hour in doc.get("hours", []):
        fh = int(hour["fh"])
        if not live or hour.get("availability_source") != "live-s3-lastmodified":
            out[fh] = _recorded_stamps(hour)
            continue
        s3 = parse_iso_utc(hour["availability_utc"])
        if fetched is not None and fetched > s3:
            out[fh] = (fetched, "live-fetch-finished")
        else:
            out[fh] = (s3, "live-s3-lastmodified")
    return out


def _recorded_stamps(hour: Mapping[str, Any]) -> ArchivedStamps | Lookup:
    """The S3 stamps an archived hour of a forecast file records (USE_MODEL if none)."""
    grib = hour.get("grib_last_modified_utc")
    idx = hour.get("idx_last_modified_utc")
    if not grib and not idx:
        return Lookup.USE_MODEL
    return ArchivedStamps(
        parse_iso_utc(grib) if grib else None, parse_iso_utc(idx) if idx else None
    )


def listed_availability(bucket: HrrrBucket) -> AvailabilityFn:
    """An ``availability`` callback that lists each run it is asked about (one request each).

    For choosing an archived run before it is stored (a backtest pair): every listed hour is
    answered with its :class:`ArchivedStamps`, so :func:`select_run_for_issue` models it
    but never before its stamps; an hour that is not on S3 is :attr:`Lookup.UNAVAILABLE`.
    The extractor applies the same rule when it writes the run
    (:func:`archived_availability`), so the file then records the same availability.
    Listings are cached for the callback's life. Not for live runs: choose among stored live
    files with :func:`select_stored_run`.
    """
    cache: dict[datetime, RunListing] = {}

    def lookup(run: datetime, fh: int) -> HourAvailability:
        start = as_run_start(run)
        if start not in cache:
            cache[start] = list_run(bucket, start)
        listing = cache[start]
        if fh not in listing.ready_hours:
            return Lookup.UNAVAILABLE
        files = listing.hour(fh)
        assert files.grib is not None and files.idx is not None
        return ArchivedStamps(files.grib.last_modified, files.idx.last_modified)

    return lookup


def stored_availability(
    storage: Storage, viewpoint_id: str, *, use_fetch_time: bool = True
) -> AvailabilityFn:
    """An ``availability`` callback backed by the stored forecast files of one viewpoint.

    Returns :attr:`Lookup.UNAVAILABLE` when the run has no stored file or the file lacks
    the hour, so :func:`select_run_for_issue` falls back to an older run; never a modelled
    time for a run Snow Globe does not hold. See :func:`availability_from_document` for live
    and archived files (an archived hour is modelled, but never before its recorded
    stamps). Files are read once per run and cached for the callback's life, so make a new
    callback for each batch of selections.
    """
    from snow_globe_common.paths import forecast_key

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
    from snow_globe_common.paths import forecast_key

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
    use ``lag_model``, read from the storage's forecast log when not given, and never come
    out before the stamps the file records (:func:`archived_availability`). Runs or hours
    that are not stored are skipped.
    """
    return select_run_for_issue(
        issue_time_utc,
        valid_time_utc,
        lag_model=lag_model if lag_model is not None else LagModel.from_storage(storage),
        availability=stored_availability(storage, viewpoint_id, use_fetch_time=use_fetch_time),
        max_lookback_h=max_lookback_h,
    )
