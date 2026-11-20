"""The data report's forecast part: live HRRR runs per viewpoint and the stand-in's outages.

PRD §8.3 and §16 (M0.5's forecast worker): one JSON per viewpoint per HRRR run, and the live
lags logged.

**Runs.** A run counts at a viewpoint when that viewpoint's *first* live write of the run falls
inside the window. A later write of the same run and viewpoint (a re-extraction, for example
after ``PRES:cloud base`` was added to the stand-in's fields at 02:39 UTC on 20 Nov) is a
re-extraction, never a run, whether the first write was inside the window or before it. A
``run`` record counts when it was the first write of at least one viewpoint, so when every
record writes every viewpoint, each viewpoint's runs equal the ``run`` records counted. A run
due before the window and first written inside it is a *catch-up* (for example 00Z and 01Z of
19 Nov, written at the stand-in's first ticks): it counts, and it is listed apart, since its
lateness belongs to the window it was due in. A file a later write replaced is flagged, because
its ``fetch_*`` times are then the later write's, not the first fetch's (the first fetch
survives only in ``forecast_log``).

**Outages** (package docstring): the stand-in leaves records only when it writes a run or
fails, so lateness is judged per HRRR cycle. A run is *due* at its fetchable time: the latest
S3 ``LastModified`` of the hours its first write extracted (run start + the largest of those
hours' ``s3_lags_s``). It is *late* when that write finished (``fetch_finished_utc``, the time
D-027 dates a live run's availability by) more than ``forecast_gap_s`` after it was due. The
stand-in was down from due + one tick until it next finished writing a live run, any run:
after a sleep it fetches the newest run first, so an older one can wait behind it. A fetch's
start and an ``error`` record end nothing: the Mac can sleep mid-fetch, and a failure just
after the due time says nothing of the hour after it. The span is an outage when it is longer
than ``forecast_gap_s``. A cycle with no live write gets an estimated due time, the cycle plus
the median fetchable lag of the window's complete runs, else of every complete run in the logs
read, else D-027's conservative 120 min; it is judged the same way and flagged as estimated.
Cycles due up to 6 h before ``since`` are judged too, and only the part of their down span
inside the window is lost, as with a capture gap that straddles ``since``. With no live run in
the logs at all, the whole window is one ongoing outage, as with capture's "no scheduled pass".
A run whose extracted hours lack a lag (a stale, incomplete run, written after waiting by
design) is left out of lateness. With no per-tick record these are lower bounds: a stand-in
down for less than the time between two due runs is not seen.
"""

from __future__ import annotations

import bisect
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any, NamedTuple

from .common import (
    REF_LIMIT,
    Exclusions,
    Interval,
    Line,
    Logs,
    Reader,
    ReportOptions,
    Window,
    clip,
    floor_hour,
    iso,
    measure,
    merge,
    number,
    rounded,
    run_id,
    run_start,
    sorted_counter,
    timestamp,
)

__all__ = ["fetchable_at", "forecast_section", "written_viewpoints"]

#: How far before the window's start the cycle scan begins (a cycle is due about an hour later).
_SCAN_BACK = timedelta(hours=6)
#: The outage kinds, as the rows and the table name them.
KIND_LATE = "no live run written while runs were due"
KIND_NO_RUN = "no live run in the logs"


@dataclass(slots=True)
class _Run:
    """Every live ``run`` record of one HRRR run id, oldest first, from every log file read."""

    run_id: str
    start: datetime
    writes: list[Line] = field(default_factory=list)
    #: Each viewpoint's first write of this run.
    first_by_vp: dict[str, Line] = field(default_factory=dict)

    @property
    def first(self) -> Line:
        return self.writes[0]

    def add(self, line: Line) -> None:
        """Add the next write (writes arrive oldest first)."""
        self.writes.append(line)
        for vp in written_viewpoints(line.record):
            self.first_by_vp.setdefault(vp, line)

    def due(self) -> datetime:
        """When the run was due: its fetchable time, or its start when no hour has a lag."""
        fetchable, _ = fetchable_at(self.first.record, self.start)
        return fetchable or self.start


class _Late(NamedTuple):
    due: datetime
    end: datetime
    run: str
    estimated: bool


def _fetch_started(line: Line) -> datetime:
    return timestamp(line.record.get("fetch_started_utc")) or line.t


def _fetch_finished(line: Line) -> datetime:
    return timestamp(line.record.get("fetch_finished_utc")) or line.t


def fetchable_at(record: Mapping[str, Any], start: datetime) -> tuple[datetime | None, bool]:
    """When the hours a ``run`` record extracted were all on S3, and whether each had a lag."""
    lags, wanted = record.get("s3_lags_s"), record.get("hours")
    if not isinstance(lags, Mapping) or not isinstance(wanted, list) or not wanted:
        return None, False
    values = [number(lags.get(str(fh))) for fh in wanted]
    known = [v for v in values if v is not None]
    if not known:
        return None, False
    return start + timedelta(seconds=max(known)), len(known) == len(values)


def written_viewpoints(record: Mapping[str, Any]) -> list[str]:
    """The viewpoints a ``run`` record wrote (its ``forecast/hrrr/<id>/<run>z.json`` keys)."""
    written = record.get("written")
    if isinstance(written, list):
        out = set()
        for item in written:
            if isinstance(item, str):
                parts = item.split("/")
                out.add(parts[2] if len(parts) >= 4 and parts[0] == "forecast" else item)
        return sorted(out)
    viewpoints, skipped = record.get("viewpoints"), record.get("skipped")
    skip = skipped if isinstance(skipped, Mapping) else {}
    if isinstance(viewpoints, list):
        return sorted({v for v in viewpoints if isinstance(v, str) and v not in skip})
    return []


def _fields(record: Mapping[str, Any], name: str = "fields") -> list[str]:
    value = record.get(name)
    return [str(v) for v in value] if isinstance(value, list) else []


def _hours(value: Any) -> list[int]:
    return [v for v in value if isinstance(v, int)] if isinstance(value, list) else []


def _lag_key(record: Mapping[str, Any]) -> tuple[str, int | None]:
    fh = number(record.get("fh"))
    return str(record.get("run")), None if fh is None else int(fh)


def forecast_section(
    reader: Reader,
    logs: Logs,
    survey: Logs,
    window: Window,
    viewpoint_ids: Sequence[str],
    options: ReportOptions,
    exclusions: Exclusions,
) -> tuple[dict[str, Any], list[Interval]]:
    """The forecast part of the report, and the intervals its outages lost (for the clock)."""
    runs: dict[str, _Run] = {}
    stand_in: list[datetime] = []  # every record the stand-in leaves: live runs, lags, errors
    first_lags: set[str] = set()
    seen_lags: set[tuple[str, int | None]] = set()
    for line in logs.lines:  # time order, every file read
        r, event = line.record, line.record.get("event")
        live = event == "run" and r.get("live") is True
        if live or event in ("lag", "error"):
            stand_in.append(line.t)
        start = run_start(r.get("run")) if live else None
        if start is not None:
            runs.setdefault(run_id(start), _Run(run_id(start), start)).add(line)
        if event == "lag" and _lag_key(r) not in seen_lags:
            seen_lags.add(_lag_key(r))
            first_lags.add(line.ref)
    first_writes = {line.ref for run in runs.values() for line in run.first_by_vp.values()}

    counted: Counter[str] = Counter()
    errors: list[dict[str, Any]] = []
    for line in logs.lines:
        r, event = line.record, line.record.get("event")
        if line.t not in window:
            reason = "before the window" if line.t < window.since else "after the window"
            exclusions.add("forecast_log", reason, line)
            continue
        if event == "run" and r.get("live") is not True:
            exclusions.add(
                "forecast_log",
                "archived forecast run (live is not true): not live-worker evidence",
                line,
                run=r.get("run"),
                viewpoints=written_viewpoints(r),
            )
            continue
        if event == "run":
            run = runs.get(str(r.get("run")))
            if run is None:
                exclusions.add("forecast_log", "live run record without a valid run id", line)
                continue
            if line.ref not in first_writes:
                if not written_viewpoints(r):
                    exclusions.add(
                        "forecast_log", "live run record that wrote no forecast file", line
                    )
                    continue
                exclusions.add(
                    "forecast_log",
                    "re-extraction of a run already written (the run counts once)",
                    line,
                    run=r.get("run"),
                    first_written=iso(run.first.t),
                    first_written_in_window=run.first.t in window,
                    fields_added=sorted(set(_fields(r)) - set(_fields(run.first.record))),
                )
                continue
        elif event == "lag":
            if line.ref not in first_lags:
                exclusions.add("forecast_log", "duplicate lag (its run and hour were logged)", line)
                continue
        elif event == "error":
            errors.append({"t": iso(line.t), "run": r.get("run"), "error": r.get("error")})
        else:
            exclusions.add("forecast_log", f"unknown forecast event {event!r}", line)
            continue
        counted[str(event)] += 1
    for line in survey.lines:
        exclusions.add(
            "forecast_log/lag_survey", "survey lag (an S3 survey): not live-worker evidence", line
        )

    # What ends a down span: the stand-in finishing the write of a live run, any run.
    finishes = sorted(_fetch_finished(line) for run in runs.values() for line in run.writes)
    cycles = _cycles(runs, finishes, window, options)
    outages, lost = _outages(cycles.down, window, options) if runs else _no_run_outage(window)
    section = {
        "records_counted": sorted_counter(counted),
        "lags_distinct_in_window": counted.get("lag", 0),
        "error_count": len(errors),
        "errors": errors[:REF_LIMIT],
        "median_fetchable_lag_s": rounded(cycles.window_median),
        "lag_for_cycles_never_written": {
            "s": rounded(cycles.lag),
            "source": cycles.lag_source,
        },
        "runs": _run_rows(runs, window, set(cycles.late_runs)),
        "runs_late": cycles.late_runs,
        "runs_incomplete": cycles.incomplete,
        "cycles_waiting": cycles.waiting,
        "cycles_not_written_stand_in_active": cycles.skipped,
        "viewpoints": _viewpoints(
            reader, runs, window, viewpoint_ids, cycles.considered, exclusions
        ),
        "outages": outages,
        "settings_changes": _settings_changes(runs, window),
        "quiet_gaps_over_threshold": [
            {"from_utc": iso(a), "to_utc": iso(b), "s": round((b - a).total_seconds(), 3)}
            for a, b in pairwise(stand_in)
            if (b - a).total_seconds() > options.forecast_gap_s
            and b > window.since
            and a < window.until
        ],
        "survey_lags": len(survey.lines),
    }
    return section, lost


@dataclass(slots=True)
class _Cycles:
    """What :func:`_cycles` found; the lists name cycles due inside the window."""

    down: list[_Late] = field(default_factory=list)  # spans with no live run written while due
    late_runs: list[str] = field(default_factory=list)  # first write finished > gap after due
    considered: list[str] = field(default_factory=list)  # due more than gap before the end
    waiting: list[str] = field(default_factory=list)  # not written, not overdue yet
    skipped: list[str] = field(default_factory=list)  # never written, other runs written meanwhile
    incomplete: list[str] = field(default_factory=list)  # an extracted hour had no lag
    window_median: float | None = None  # median fetchable lag of the window's complete runs
    lag: float = 0.0  # the lag that dates a cycle never written
    lag_source: str = ""


def _lag_for_unwritten(
    runs: Mapping[str, _Run], window: Window, options: ReportOptions
) -> tuple[float | None, float, str]:
    """The window's median fetchable lag, and the lag (and its source) for unwritten cycles."""
    in_window: list[float] = []
    anywhere: list[float] = []
    for run in runs.values():
        fetchable, complete = fetchable_at(run.first.record, run.start)
        if fetchable is None or not complete:
            continue
        lag = (fetchable - run.start).total_seconds()
        anywhere.append(lag)
        if fetchable in window:
            in_window.append(lag)
    if in_window:
        median = statistics.median(in_window)
        return median, median, "median fetchable lag of the window's complete runs"
    if anywhere:
        return None, statistics.median(anywhere), "median of every complete run in the logs read"
    return (
        None,
        options.forecast_fallback_lag_s,
        "D-027's conservative fallback: no complete live run in the logs read",
    )


def _cycles(
    runs: Mapping[str, _Run], finishes: Sequence[datetime], window: Window, options: ReportOptions
) -> _Cycles:
    """Each hourly cycle due before the window's end: on time, late, or never written."""
    out = _Cycles()
    out.window_median, out.lag, out.lag_source = _lag_for_unwritten(runs, window, options)
    gap = options.forecast_gap_s
    cycle = floor_hour(window.since) - _SCAN_BACK
    while cycle <= floor_hour(window.until):
        this, cycle = cycle, cycle + timedelta(hours=1)
        rid = run_id(this)
        run = runs.get(rid)
        fetched: datetime | None = None
        if run is not None:
            due, complete = fetchable_at(run.first.record, run.start)
            if due is None or not complete:
                if any(line.t in window for line in run.writes):
                    out.incomplete.append(rid)
                continue
            fetched, estimated = _fetch_finished(run.first), False
        else:
            due, estimated = this + timedelta(seconds=out.lag), True
        if due >= window.until:
            continue
        judged = due >= window.since  # an earlier cycle only loses the part inside the window
        if judged and (window.until - due).total_seconds() > gap:
            out.considered.append(rid)
        index = bisect.bisect_right(finishes, due)
        after = finishes[index] if index < len(finishes) else None
        if fetched is not None:
            if (fetched - due).total_seconds() <= gap:
                continue
            if judged:
                out.late_runs.append(rid)
            end = fetched if after is None else min(after, fetched)
        else:
            end = window.until if after is None else min(after, window.until)
        if (end - due).total_seconds() > gap:
            out.down.append(_Late(due, end, rid, estimated))
        elif fetched is None and judged:
            (out.waiting if after is None else out.skipped).append(rid)
    return out


def _outages(
    down: Sequence[_Late], window: Window, options: ReportOptions
) -> tuple[list[dict[str, Any]], list[Interval]]:
    """The down spans merged into outages; time is lost from one tick after the first due."""
    tick = timedelta(seconds=options.forecast_tick_s)
    rows: list[dict[str, Any]] = []
    lost: list[Interval] = []
    for start, end in merge((item.due + tick, item.end) for item in down):
        part = clip([(start, end)], window)
        if not part:  # wholly before the window: a cycle of the scan-back
            continue
        members = [item for item in down if item.due + tick < end and item.end > start]
        first_due = min(item.due for item in members)
        lost.extend(part)
        rows.append(
            {
                "job": "forecast",
                "kind": KIND_LATE,
                "start_utc": iso(first_due),
                "end_utc": iso(end),
                "gap_s": round((end - first_due).total_seconds(), 3),
                "lost_s": round(measure(part), 3),
                "runs": sorted({item.run for item in members}),
                "estimated": any(item.estimated for item in members),
                "ongoing": end >= window.until,
            }
        )
    return rows, lost


def _no_run_outage(window: Window) -> tuple[list[dict[str, Any]], list[Interval]]:
    """No live run in any log read: the whole window is lost, as with no capture pass."""
    row = {
        "job": "forecast",
        "kind": KIND_NO_RUN,
        "start_utc": iso(window.since),
        "end_utc": iso(window.until),
        "gap_s": round(window.seconds, 3),
        "lost_s": round(window.seconds, 3),
        "runs": [],
        "estimated": False,
        "ongoing": True,
    }
    return [row], [(window.since, window.until)]


def _run_rows(
    runs: Mapping[str, _Run], window: Window, late_runs: set[str]
) -> list[dict[str, Any]]:
    """One row per run with a live write inside the window, as its first write recorded it."""
    rows = []
    for run in sorted(runs.values(), key=lambda r: r.start):
        writes = [line for line in run.writes if line.t in window]
        if not writes:
            continue
        fetchable, complete = fetchable_at(run.first.record, run.start)
        finished = _fetch_finished(run.first)
        rows.append(
            {
                "run": run.run_id,
                "first_written_utc": iso(run.first.t),
                "first_written_in_window": run.first.t in window,
                "fetchable_utc": iso(fetchable),
                "fetchable_complete": complete,
                "catch_up": run.due() < window.since,
                "first_fetch_started_utc": iso(_fetch_started(run.first)),
                "first_fetch_finished_utc": iso(finished),
                "fetch_delay_s": (
                    None if fetchable is None else round((finished - fetchable).total_seconds(), 3)
                ),
                "late": run.run_id in late_runs,
                "writes_in_window": len(writes),
                "hours": _hours(run.first.record.get("hours")),
                "fields_first": _fields(run.first.record),
                "fields_last": _fields(run.writes[-1].record),
                "viewpoints": len(written_viewpoints(run.first.record)),
                "ref": run.first.ref,
            }
        )
    return rows


def _settings_changes(runs: Mapping[str, _Run], window: Window) -> list[dict[str, Any]]:
    """The stand-in's fields or hours changing between consecutive live writes (a restart)."""
    writes = sorted((line for run in runs.values() for line in run.writes), key=lambda x: x.t)
    rows = []
    for a, b in pairwise(writes):
        fields_a, fields_b = set(_fields(a.record)), set(_fields(b.record))
        hours_a, hours_b = _hours(a.record.get("hours")), _hours(b.record.get("hours"))
        if (fields_a != fields_b or hours_a != hours_b) and b.t in window:
            rows.append(
                {
                    "job": "forecast",
                    "kind": "settings changed",
                    "last_old_utc": iso(a.t),
                    "first_new_utc": iso(_fetch_started(b)),
                    "fields_added": sorted(fields_b - fields_a),
                    "fields_removed": sorted(fields_a - fields_b),
                    "hours_from": hours_a,
                    "hours_to": hours_b,
                    "ref": b.ref,
                }
            )
    return rows


def _viewpoints(
    reader: Reader,
    runs: Mapping[str, _Run],
    window: Window,
    viewpoint_ids: Sequence[str],
    considered: Sequence[str],
    exclusions: Exclusions,
) -> dict[str, Any]:
    """Live runs first written per viewpoint in the window, checked against the files on disk."""
    firsts: dict[str, dict[str, Line]] = {vp: {} for vp in viewpoint_ids}
    rewrites: Counter[str] = Counter()
    ever: dict[str, set[str]] = {}
    for run in runs.values():
        for vp, line in run.first_by_vp.items():
            ever.setdefault(vp, set()).add(run.run_id)
            if line.t in window:
                firsts.setdefault(vp, {})[run.run_id] = line
        for line in run.writes:
            if line.t not in window:
                continue
            for vp in written_viewpoints(line.record):
                if run.first_by_vp[vp] is not line:
                    rewrites[vp] += 1
    out: dict[str, Any] = {}
    for vp in sorted(set(firsts) | set(rewrites) | set(reader.folders("forecast/hrrr"))):
        first_writes = firsts.get(vp, {})
        hours: Counter[str] = Counter()
        on_disk: Counter[tuple[str, ...]] = Counter()
        at_first: Counter[tuple[str, ...]] = Counter()
        rewritten, missing, not_live, catch_ups = [], [], [], []
        for rid in sorted(first_writes):
            first = first_writes[rid]
            if runs[rid].due() < window.since:
                catch_ups.append(rid)
            at_first[tuple(_fields(first.record))] += 1
            doc = reader.json(f"forecast/hrrr/{vp}/{rid}z.json")
            if not isinstance(doc, dict):
                missing.append(rid)
                hours[",".join(map(str, _hours(first.record.get("hours"))))] += 1
                continue
            hours[",".join(map(str, _hours(doc.get("forecast_hours"))))] += 1
            on_disk[tuple(_fields(doc, "field_names"))] += 1
            if doc.get("live") is not True:
                not_live.append(rid)
            if timestamp(doc.get("fetch_started_utc")) != _fetch_started(first):
                rewritten.append(rid)
        for name in reader.files(f"forecast/hrrr/{vp}", "z.json"):
            rid = name.removesuffix("z.json")
            if rid in first_writes:
                continue
            doc = reader.json(f"forecast/hrrr/{vp}/{name}")
            reason = (
                "live forecast file whose run was not first written inside the window"
                if isinstance(doc, dict) and doc.get("live") is True
                else "archived forecast file (live is not true): not live-worker evidence"
            )
            exclusions.add("forecast/hrrr", reason, None, key=f"forecast/hrrr/{vp}/{name}")
        ordered = sorted(first_writes)
        out[vp] = {
            "runs": len(first_writes),
            "runs_due_in_window": len(first_writes) - len(catch_ups),
            "catch_ups": catch_ups,
            "first_run": ordered[0] if ordered else None,
            "last_run": ordered[-1] if ordered else None,
            "re_extractions": rewrites.get(vp, 0),
            "hours": sorted_counter(hours),
            "field_sets_on_disk": [
                {"fields": list(f), "runs": n} for f, n in on_disk.most_common()
            ],
            "field_sets_at_first_write": [
                {"fields": list(f), "runs": n} for f, n in at_first.most_common()
            ],
            "files_rewritten_by_later_writes": rewritten,
            "files_missing": missing,
            "files_not_live": not_live,
            "cycles_never_written": [rid for rid in considered if rid not in ever.get(vp, set())],
        }
    return out
