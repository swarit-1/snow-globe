"""The publication-lag log: live and survey ``lag`` events (PRD §8.3, §11.2, §13.9; D-008, D-027).

A ``lag`` event records when one forecast hour of one HRRR run appeared on S3: the later
``LastModified`` of its GRIB2 file and its ``.idx`` (:attr:`.discovery.HourFiles.availability_utc`)
minus the run start. :class:`.availability.LagModel` turns these events into the modelled
availability of archived runs. D-027 (amending D-008) lets lags measured from S3 listings of
past runs feed the model as well as the worker's own, so two append-only logs hold them:

- ``forecast_log/<YYYY-MM-DD>.jsonl``: **live** events, written by the extractor (and so by
  the Lambda) for every listed hour of a run it processes while the run is new. Events
  written before D-027 carry no ``method``; they count as ``"live"``.
- ``forecast_log/lag_survey/<YYYY-MM-DD>.jsonl``: **survey** events (``"method": "survey"``),
  written only by ``snow-globe-forecast lag-survey --log`` (:class:`SurveyLogger`). They get a
  file of their own because the live worker appends to the day's live log while a survey
  runs, and on S3 an append is a read-modify-write that two writers must not share.

The date in a file name is the UTC day the events were written, which is never before the
run's own day, so reading the files dated from day D onwards finds every event of the runs
of day D and later. Each (run, forecast hour) is logged once by a survey: it skips every pair
already logged, live or survey. The live log can hold a pair twice (a run re-extracted after
a field-set change logs its hours again, with the same stamps); readers keep one event per
pair and prefer a live one. Both measure the same S3 stamps, and a later listing can only show
the same stamps or, after a re-upload, later ones, so a survey can only err late.

A stamp more than :data:`.discovery.STAMP_TRUST_H` (24 h) after the run start is when an
object was rewritten, not when the hour was published (the ``.idx`` files of 2020120312 and
2021011500 carry 17 Feb 2021 stamps). A survey never logs such an hour, and readers count a
lag above 24 h as invalid: one such lag would otherwise set its cell's lag (rules 2 and 3 of
:class:`.availability.LagModel` never go below a cell's largest lag) to weeks.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from snow_globe_common.jsonl import iter_jsonl_bytes
from snow_globe_common.paths import validate_key
from snow_globe_common.storage import Storage
from snow_globe_common.timeutil import isoformat_z, utcnow

from .discovery import STAMP_TRUST_H, RunListing
from .hrrr import SOURCE, max_fh

__all__ = [
    "LAG_EVENT",
    "LIVE_LOG_PREFIX",
    "MAX_LAG_S",
    "METHODS",
    "METHOD_LIVE",
    "METHOD_SURVEY",
    "SURVEY_LOCK",
    "SURVEY_LOG_PREFIX",
    "LagLog",
    "SurveyLogger",
    "cycle_hour",
    "lag_events",
    "read_lag_events",
    "read_lag_log",
    "survey_log_key",
    "usable_lag",
]

LAG_EVENT = "lag"
#: How a lag was logged: by the worker while the run was new, or by ``lag-survey --log``.
METHOD_LIVE = "live"
METHOD_SURVEY = "survey"
METHODS: tuple[str, ...] = (METHOD_LIVE, METHOD_SURVEY)
LIVE_LOG_PREFIX = "forecast_log/"
SURVEY_LOG_PREFIX = "forecast_log/lag_survey/"
#: Storage lock a logging survey holds, so two surveys never log the same pair twice.
SURVEY_LOCK = "forecast-lag-survey"
#: The largest lag that can be a publication lag: :data:`.discovery.STAMP_TRUST_H`.
MAX_LAG_S = STAMP_TRUST_H * 3600.0
_LIVE_KEY_RE = re.compile(r"^forecast_log/(\d{4}-\d{2}-\d{2})\.jsonl$")
_SURVEY_KEY_RE = re.compile(r"^forecast_log/lag_survey/(\d{4}-\d{2}-\d{2})\.jsonl$")

Pair = tuple[str, int]  # (run id, forecast hour)


def survey_log_key(day: date | datetime) -> str:
    """Key of the survey log of one UTC day: ``forecast_log/lag_survey/<YYYY-MM-DD>.jsonl``."""
    if isinstance(day, datetime):
        if day.tzinfo is None:
            raise ValueError("survey_log_key needs a timezone-aware datetime")
        day = day.astimezone(UTC).date()
    return validate_key(f"{SURVEY_LOG_PREFIX}{day:%Y-%m-%d}.jsonl")


def cycle_hour(run_id: str) -> int:
    """The cycle hour (0-23) of a ``YYYYMMDDHH`` run id; ``ValueError`` if it is not one."""
    if len(run_id) != 10 or not run_id.isdigit() or int(run_id[8:]) > 23:
        raise ValueError(f"bad run id {run_id!r}")
    return int(run_id[8:])


def lag_events(
    listing: RunListing, hours: Iterable[int] | None = None, *, method: str = METHOD_LIVE
) -> list[dict[str, Any]]:
    """``lag`` records for the ready hours of a listed run (all of them unless ``hours``).

    Each record holds ``run``, ``fh``, ``last_modified`` (the later of the GRIB2 and
    ``.idx`` stamps), ``lag_s``, both stamps, the GRIB2 size, ``synoptic`` and ``method``.
    A survey record also says when the listing was made (``listed_utc``).
    """
    if method not in METHODS:
        raise ValueError(f"unknown lag method {method!r}; known: {', '.join(METHODS)}")
    wanted = set(listing.ready_hours if hours is None else hours)
    events: list[dict[str, Any]] = []
    for fh in listing.ready_hours:
        if fh not in wanted:
            continue
        files = listing.hour(fh)
        available = files.availability_utc
        assert available is not None and files.grib is not None and files.idx is not None
        event: dict[str, Any] = {
            "event": LAG_EVENT,
            "source": SOURCE,
            "run": listing.run_id,
            "fh": fh,
            "last_modified": isoformat_z(available),
            "lag_s": round((available - listing.run_start).total_seconds(), 3),
            "grib_last_modified": isoformat_z(files.grib.last_modified),
            "idx_last_modified": isoformat_z(files.idx.last_modified),
            "grib_size": files.grib.size,
            "synoptic": max_fh(listing.run_start) == 48,
            "method": method,
        }
        if method == METHOD_SURVEY:
            event["listed_utc"] = isoformat_z(listing.listed_utc, "seconds")
        events.append(event)
    return events


@dataclass(frozen=True)
class LagLog:
    """What :func:`read_lag_log` found: one event per (run, forecast hour), with counts."""

    #: The kept events, each a copy with ``method`` set.
    events: tuple[dict[str, Any], ...]
    #: Kept events by method.
    by_method: Mapping[str, int]
    #: Events dropped because their (run, fh) was already kept, by the dropped one's method.
    duplicates: Mapping[str, int]
    #: ``lag`` records that could not be used (bad run id, hour, lag or method).
    invalid: int
    #: Log files read, by the method their location implies.
    files: Mapping[str, int]

    @property
    def by_pair(self) -> dict[Pair, dict[str, Any]]:
        """``(run, fh) -> kept event``."""
        return {(str(e["run"]), int(e["fh"])): e for e in self.events}

    @property
    def logged(self) -> dict[Pair, str]:
        """``(run, fh) -> method`` of every kept event."""
        return {pair: str(event["method"]) for pair, event in self.by_pair.items()}

    def summary(self) -> dict[str, Any]:
        """JSON-ready counts (for ``status`` and survey reports)."""
        return {
            "events": len(self.events),
            "by_method": {m: int(self.by_method.get(m, 0)) for m in METHODS},
            "duplicates_dropped": {m: int(self.duplicates.get(m, 0)) for m in METHODS},
            "invalid": self.invalid,
            "files": {m: int(self.files.get(m, 0)) for m in METHODS},
            "runs": len({str(e["run"]) for e in self.events}),
        }


def _log_files(storage: Storage, since: datetime | None) -> list[tuple[str, str]]:
    """``(key, location method)`` of every live log file, then every survey file, from ``since``."""
    first_day = since.astimezone(UTC).strftime("%Y-%m-%d") if since is not None else None
    live: list[str] = []
    survey: list[str] = []
    for key in storage.list_prefix(LIVE_LOG_PREFIX):
        for pattern, keys in ((_LIVE_KEY_RE, live), (_SURVEY_KEY_RE, survey)):
            match = pattern.match(key)
            if match is not None and (first_day is None or match[1] >= first_day):
                keys.append(key)
    return [(key, METHOD_LIVE) for key in sorted(live)] + [
        (key, METHOD_SURVEY) for key in sorted(survey)
    ]


def _number(value: Any) -> float | None:
    """``value`` as a finite float if it is a JSON number (not a bool), else None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def usable_lag(record: Mapping[str, Any]) -> tuple[str, int, float] | None:
    """``(run, fh, lag_s)`` of a usable ``lag`` record, else None.

    Usable: ``run`` is a ``YYYYMMDDHH`` string; ``fh`` is a JSON integer of 0 or more (an
    integral float such as ``3.0`` counts; ``1.5``, ``true`` and ``"3"`` do not); ``lag_s`` is
    a finite number from 0 to :data:`MAX_LAG_S`. A negative lag is a clock or listing error
    and must never look early; a lag above 24 h comes from a rewritten object, not from a
    publication.
    """
    run, fh, lag = record.get("run"), _number(record.get("fh")), _number(record.get("lag_s"))
    if not isinstance(run, str) or fh is None or lag is None:
        return None
    try:
        cycle_hour(run)
    except ValueError:
        return None
    if fh < 0 or not fh.is_integer() or not 0.0 <= lag <= MAX_LAG_S:
        return None
    return run, int(fh), lag


def read_lag_log(storage: Storage, *, since: datetime | None = None) -> LagLog:
    """Read the ``lag`` events of the live and the survey logs, one per (run, forecast hour).

    ``since`` skips the files of earlier UTC days. A record's ``method`` defaults to where
    it was found: ``"live"`` in a live log (no event written before D-027 has one),
    ``"survey"`` in the survey log, which holds survey events only (a record there that says
    otherwise is invalid). Where a pair has a live and a survey event, the first live one is
    kept, else the first survey one. Records that :func:`usable_lag` rejects (a run that is
    not a ``YYYYMMDDHH`` string, an hour that is not a non-negative integer, a lag that is
    not a number from 0 to 24 h) or with an unknown method are skipped and counted in
    :attr:`LagLog.invalid`.
    """
    kept: dict[Pair, dict[str, Any]] = {}
    duplicates: Counter[str] = Counter()
    files: Counter[str] = Counter()
    invalid = 0
    for key, location in _log_files(storage, since):
        files[location] += 1
        for record in iter_jsonl_bytes(storage.get_bytes(key)):
            if record.get("event") != LAG_EVENT:
                continue
            usable = usable_lag(record)
            if usable is None:
                invalid += 1
                continue
            pair = (usable[0], usable[1])
            method = record.get("method") or location
            if method not in METHODS or (location == METHOD_SURVEY and method != METHOD_SURVEY):
                invalid += 1
                continue
            event = {**record, "fh": pair[1], "method": method}
            previous = kept.get(pair)
            if previous is None:
                kept[pair] = event
            elif method == METHOD_LIVE and previous["method"] != METHOD_LIVE:
                duplicates[previous["method"]] += 1
                kept[pair] = event
            else:
                duplicates[method] += 1
    events = tuple(kept.values())
    return LagLog(
        events=events,
        by_method=dict(Counter(str(e["method"]) for e in events)),
        duplicates=dict(duplicates),
        invalid=invalid,
        files=dict(files),
    )


def read_lag_events(
    storage: Storage, *, since: datetime | None = None, methods: Collection[str] | None = None
) -> list[dict[str, Any]]:
    """The events :func:`read_lag_log` keeps (each with ``method``), of ``methods`` if given.

    The filter applies after de-duplication, so ``methods={"live"}`` gives every live
    event: the lag log as it was before survey events existed.
    """
    return [
        dict(event)
        for event in read_lag_log(storage, since=since).events
        if methods is None or event["method"] in methods
    ]


class SurveyLogger:
    """Appends survey ``lag`` events for the (run, fh) pairs not logged yet, live or survey.

    It writes only ``forecast_log/lag_survey/<day>.jsonl`` (the UTC day of the write), never
    the live log the worker appends to. Events are buffered and appended every
    ``flush_every`` listings and on :meth:`flush`: one write per batch, which on S3 is one
    read-modify-write. An interrupted survey keeps what it flushed, and running it again
    logs only the rest. ``since`` limits which log days are read to find what is already
    logged (events of a run are never in a file dated before the run's own day). For every
    listed pair that was already logged, the survey's lag is compared with the logged one
    (``agreement_with_logged`` in :meth:`report`): the evidence that a survey measures what
    the live worker measured. An hour whose GRIB2 or ``.idx`` stamp is more than
    :data:`.discovery.STAMP_TRUST_H` after the run start is never logged, only counted
    (``stamps_not_trusted``): its lag is when an object was rewritten. Hold
    :data:`SURVEY_LOCK` around a logging survey (the CLI does), so two surveys never log the
    same pair.
    """

    def __init__(
        self,
        storage: Storage,
        *,
        since: datetime | None = None,
        clock: Callable[[], datetime] | None = None,
        flush_every: int = 24,
    ) -> None:
        self.storage = storage
        self.clock = clock if clock is not None else utcnow
        self.flush_every = max(1, flush_every)
        before = read_lag_log(storage, since=since)
        #: The log as it was before this survey wrote anything.
        self.log_before = before.summary()
        self._logged: dict[Pair, tuple[str, float]] = {
            pair: (str(event["method"]), float(event["lag_s"]))
            for pair, event in before.by_pair.items()
        }
        self._pending: list[dict[str, Any]] = []
        self._listings_since_flush = 0
        #: Events this logger appended, and the keys it appended them to.
        self.written = 0
        self.keys: list[str] = []
        #: Listed hours skipped because their pair was already logged, by the logged method.
        self.already_logged: Counter[str] = Counter()
        #: Listed hours never logged because a stamp is more than 24 h after the run start.
        self.stamps_not_trusted = 0
        self._agreement: dict[str, dict[str, float]] = {}

    def _compare(self, logged_method: str, logged_lag_s: float, survey_lag_s: float) -> None:
        stats = self._agreement.setdefault(
            logged_method,
            {"compared": 0, "equal": 0, "survey_later": 0, "survey_earlier": 0, "max_abs_s": 0.0},
        )
        diff = round(survey_lag_s - logged_lag_s, 3)
        stats["compared"] += 1
        if diff == 0:
            stats["equal"] += 1
        elif diff > 0:
            stats["survey_later"] += 1
        else:
            stats["survey_earlier"] += 1
        stats["max_abs_s"] = max(stats["max_abs_s"], abs(diff))

    def add(self, listing: RunListing) -> int:
        """Queue the survey events of one listing that are not logged yet; return how many."""
        new = 0
        for event in lag_events(listing, method=METHOD_SURVEY):
            pair = (str(event["run"]), int(event["fh"]))
            if not listing.hour(pair[1]).stamps_trusted(listing.run_start):
                self.stamps_not_trusted += 1
                continue
            seen = self._logged.get(pair)
            if seen is not None:
                self.already_logged[seen[0]] += 1
                self._compare(seen[0], seen[1], float(event["lag_s"]))
                continue
            self._logged[pair] = (METHOD_SURVEY, float(event["lag_s"]))
            self._pending.append(event)
            new += 1
        self._listings_since_flush += 1
        if self._listings_since_flush >= self.flush_every:
            self.flush()
        return new

    def flush(self) -> None:
        """Append the queued events (one write; nothing if none are queued)."""
        self._listings_since_flush = 0
        if not self._pending:
            return
        key = survey_log_key(self.clock())
        self.storage.append_jsonl_many(key, self._pending)
        self.written += len(self._pending)
        if key not in self.keys:
            self.keys.append(key)
        self._pending = []

    def report(self) -> dict[str, Any]:
        """JSON-ready account of what was logged, what was skipped and how lags agreed."""
        return {
            "logged": self.written,
            "not_flushed": len(self._pending),
            "already_logged": {m: int(self.already_logged.get(m, 0)) for m in METHODS},
            "stamps_not_trusted": self.stamps_not_trusted,
            "agreement_with_logged": {
                m: {k: (round(v, 3) if k == "max_abs_s" else int(v)) for k, v in stats.items()}
                for m, stats in sorted(self._agreement.items())
            },
            "keys": list(self.keys),
            "log_before": self.log_before,
        }
