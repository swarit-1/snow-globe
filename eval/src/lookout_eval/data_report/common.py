"""Shared pieces of the data report: rules, log reading, intervals and exclusions (PRD §16).

Everything here reads; nothing writes. :class:`Reader` resolves every key under the data dir
and refuses any path eval's quarantine guard refuses (D-022), so a symlink cannot lead the
report into the quarantine.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any, NamedTuple

from lookout_common.jsonl import REASON_INVALID_JSON, REASON_NOT_OBJECT, REASON_UNTERMINATED
from lookout_common.timeutil import isoformat_z, parse_iso_utc

from ..heldout import QuarantineGuard

__all__ = [
    "DEFAULT_SINCE",
    "LEGACY_POLICY",
    "POLICY_EVERY",
    "POLICY_HOURLY",
    "REF_LIMIT",
    "REPORT_KIND",
    "SCHEDULED_RUNNERS",
    "Exclusions",
    "Interval",
    "Line",
    "Logs",
    "Reader",
    "ReportOptions",
    "Request",
    "Window",
    "clip",
    "close_time",
    "floor_hour",
    "floor_ms",
    "gap_stats",
    "hours",
    "iso",
    "max_in_window",
    "measure",
    "merge",
    "number",
    "parse_log",
    "provable_spans",
    "read_logs",
    "rounded",
    "run_id",
    "run_start",
    "sorted_counter",
    "timestamp",
    "without_home",
]

#: 12:24:23 UTC on 19 Nov 2026: ``lookout-capture loop`` took over all 8 cameras (D-023).
DEFAULT_SINCE = datetime(2026, 11, 19, 12, 24, 23, tzinfo=UTC)
#: Runners whose passes make the continuous capture record: the CLI loop and the AWS Lambda.
SCHEDULED_RUNNERS: frozenset[str] = frozenset({"cli-loop", "lambda"})
#: Capture 0.1.0 had no storage policy: every download was kept at full size, no thumbnail.
LEGACY_POLICY = "legacy-every"
POLICY_EVERY = "every"
POLICY_HOURLY = "hourly-nearest"
REPORT_KIND = "lookout.data-report"
#: How many example references (file:line, keys) an exclusion or a check keeps.
REF_LIMIT = 10

_DAY = timedelta(days=1)

type Interval = tuple[datetime, datetime]


@dataclass(frozen=True, slots=True)
class ReportOptions:
    """The report's rules; the defaults are the lead's for the M0.5 gate (package docstring)."""

    clock_hours: float = 72.0
    capture_gap_s: float = 540.0
    #: The loop's interval: an outage, and a restart's excess, lose their gap less one of it.
    capture_interval_s: float = 300.0
    #: PRD §15.3 and CLAUDE.md: a camera is polled at most every 5 minutes.
    min_poll_gap_s: float = 300.0
    forecast_gap_s: float = 900.0
    forecast_tick_s: float = 300.0
    #: Dates a cycle never written when no complete live run is in the logs read: D-027's
    #: conservative fallback lag (120 min).
    forecast_fallback_lag_s: float = 7200.0
    rolling_window_s: float = 3600.0
    max_gets_per_rolling_hour: int = 4
    restart_tolerance_s: float = 5.0
    scheduled_runners: frozenset[str] = SCHEDULED_RUNNERS


@dataclass(frozen=True, slots=True)
class Window:
    """The report's half-open window ``[since, until)`` (UTC)."""

    since: datetime
    until: datetime

    def __contains__(self, moment: object) -> bool:
        return isinstance(moment, datetime) and self.since <= moment < self.until

    @property
    def seconds(self) -> float:
        return (self.until - self.since).total_seconds()


@dataclass(frozen=True, slots=True)
class Line:
    """One JSONL record and where it came from (``source`` is relative to the data dir)."""

    source: str
    line: int
    t: datetime
    record: Mapping[str, Any]

    @property
    def ref(self) -> str:
        return f"{self.source}:{self.line}"


@dataclass(slots=True)
class Logs:
    """The records of one log folder, oldest first, plus what could not be read."""

    lines: list[Line] = field(default_factory=list)
    bad: list[dict[str, Any]] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)
    files_skipped: list[str] = field(default_factory=list)


class Request(NamedTuple):
    """One logged request to nps.gov (PRD §15.3), with the bounds on when nps.gov received it.

    ``send`` is the send as capture's scheduler counted it (:func:`.capture.send_time`): it
    orders a camera's requests and gives the send-to-send figures. nps.gov received the request
    no earlier than ``earliest`` and no later than ``latest`` (:func:`.capture.earliest_receipt`,
    :func:`.capture.latest_receipt`), whatever the network did, which is what makes
    :func:`provable_spans` a bound at nps.gov. ``earliest`` is None when the record cannot place
    the send (no ``sent_at`` and no usable ``elapsed_ms``).
    """

    send: datetime
    earliest: datetime | None
    latest: datetime


# --- values -----------------------------------------------------------------------------------


def iso(moment: datetime | None) -> str | None:
    """RFC 3339 UTC with milliseconds, or None."""
    return None if moment is None else isoformat_z(moment, "milliseconds")


def timestamp(value: Any) -> datetime | None:
    """A record's timestamp as UTC, or None if it is missing, naive or not a timestamp."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return parse_iso_utc(value)
    except ValueError:
        return None


def number(value: Any) -> float | None:
    """A finite JSON number as a float (booleans are not numbers), else None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def rounded(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def hours(seconds: float) -> float:
    """Seconds as hours, to 3 decimals (3.6 s)."""
    return round(seconds / 3600.0, 3)


def run_start(value: Any) -> datetime | None:
    """An HRRR run id ``YYYYMMDDHH`` as the run's start (UTC), or None."""
    if not isinstance(value, str) or len(value) != 10 or not value.isdigit():
        return None
    try:
        return datetime.strptime(value, "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError:
        return None


def run_id(start: datetime) -> str:
    return start.astimezone(UTC).strftime("%Y%m%d%H")


def floor_hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


def sorted_counter(counter: Mapping[str, int]) -> dict[str, int]:
    return {key: counter[key] for key in sorted(counter)}


def without_home(value: Any, home: Path | None = None) -> Any:
    """``value`` with the home directory written as ``~`` in every string, at any depth.

    The report is gate evidence that gets pasted into the planning docs, so it carries no
    account name: the data dir, a lock's command line or an error text that names a path under
    the home directory show it as ``~`` (CLAUDE.md "Privacy").
    """
    prefix = str(home if home is not None else Path.home()).rstrip("/")
    if not prefix:
        return value

    def scrub(item: Any) -> Any:
        if isinstance(item, str):
            return "~" if item == prefix else item.replace(prefix + "/", "~/")
        if isinstance(item, Mapping):
            return {key: scrub(v) for key, v in item.items()}
        if isinstance(item, list | tuple):
            return [scrub(v) for v in item]
        return item

    return scrub(value)


# --- intervals --------------------------------------------------------------------------------


def merge(intervals: Iterable[Interval]) -> list[Interval]:
    """Sort and merge overlapping or touching intervals; empty ones are dropped."""
    merged: list[Interval] = []
    for start, end in sorted(i for i in intervals if i[1] > i[0]):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def clip(intervals: Iterable[Interval], window: Window) -> list[Interval]:
    """The parts of ``intervals`` inside the window."""
    parts = ((max(a, window.since), min(b, window.until)) for a, b in intervals)
    return [(a, b) for a, b in parts if b > a]


def measure(intervals: Iterable[Interval]) -> float:
    """Seconds covered by the union of ``intervals``."""
    return sum((b - a).total_seconds() for a, b in merge(intervals))


def close_time(start: datetime, lost: Sequence[Interval], required_s: float) -> datetime:
    """When the time covered since ``start`` reaches ``required_s``, if nothing more is lost."""
    moment, remaining = start, required_s
    for a, b in merge(lost):
        if b <= moment:
            continue
        free = (max(a, moment) - moment).total_seconds()
        if free >= remaining:
            return moment + timedelta(seconds=remaining)
        remaining -= free
        moment = b
    return moment + timedelta(seconds=remaining)


def gap_stats(times: Iterable[datetime]) -> dict[str, Any]:
    """The longest and shortest gaps between consecutive times, with where they fall."""
    ordered = sorted(times)
    if len(ordered) < 2:
        return {"longest": None, "shortest": None}
    gaps = [((b - a).total_seconds(), a, b) for a, b in pairwise(ordered)]

    def row(gap: tuple[float, datetime, datetime]) -> dict[str, Any]:
        return {"s": round(gap[0], 3), "from_utc": iso(gap[1]), "to_utc": iso(gap[2])}

    return {
        "longest": row(max(gaps, key=lambda g: g[0])),
        "shortest": row(min(gaps, key=lambda g: g[0])),
    }


def max_in_window(times: Iterable[datetime], width_s: float) -> tuple[int, datetime | None]:
    """The most times inside any half-open window ``[s, s + width_s)``, and that window's s."""
    ordered = sorted(times)
    best, best_start, j = 0, None, 0
    for i, start in enumerate(ordered):
        j = max(j, i)
        while j < len(ordered) and (ordered[j] - start).total_seconds() < width_s:
            j += 1
        if j - i > best:
            best, best_start = j - i, start
    return best, best_start


def floor_ms(seconds: float) -> float:
    """``seconds`` rounded *down* to the millisecond, for a lower bound shown to 3 decimals.

    Rounding to nearest could show a bound of 299.9996 s as 300.000 s while it counts as under
    300 s. The 1 ns allowance absorbs binary error in values that are whole microseconds.
    """
    return math.floor(seconds * 1000.0 + 1e-6) / 1000.0


def provable_spans(
    requests: Iterable[Request], count: int
) -> list[tuple[float | None, Request, Request]]:
    """For each run of ``count`` consecutive requests (in send order), the least span at nps.gov.

    PRD §15.3's rules are about what nps.gov sees. It received the run's first request no later
    than ``first.latest`` and its last no earlier than ``last.earliest``, so it saw them at least
    ``last.earliest - first.latest`` apart, whatever the network did. ``count`` 2 gives the gap
    between consecutive polls (the 5-minute rule); ``count`` 5 the span of 5 consecutive image
    GETs (the cap of 4 in any rolling hour holds when no 5 fall within 3600 s). Returns
    ``(seconds, first, last)`` for every run, oldest first, with ``seconds`` None when the last
    request's send cannot be placed (``last.earliest`` is None): such a run proves nothing.
    Fewer than ``count`` requests give none.
    """
    if count < 2:
        raise ValueError("a span needs at least 2 requests")
    ordered = sorted(requests, key=lambda request: (request.send, request.latest))
    return [
        (
            None if last.earliest is None else (last.earliest - first.latest).total_seconds(),
            first,
            last,
        )
        for first, last in zip(ordered, ordered[count - 1 :], strict=False)
    ]


# --- reading ----------------------------------------------------------------------------------


class Reader:
    """Reads files under the data dir, refusing any path the quarantine guard refuses."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.guard = QuarantineGuard((root,), cache_dirs=True)
        self.unreadable: list[dict[str, Any]] = []

    def path(self, key: str) -> Path | None:
        """The path of ``key``, or None (and a note) if the quarantine guard refuses it."""
        path = self.root / key
        reason = self.guard.reason(path)
        if reason is not None:  # never true for the folders the report reads; a hard stop
            self.unreadable.append({"key": key, "reason": reason})
            return None
        return path

    def json(self, key: str) -> Any:
        """The parsed JSON file at ``key``, or None if it is missing or cannot be read."""
        path = self.path(key)
        if path is None or not path.is_file():
            return None
        try:
            return json.loads(path.read_bytes())
        except (OSError, ValueError) as exc:  # a file being written, or damaged
            self.unreadable.append({"key": key, "reason": type(exc).__name__})
            return None

    def exists(self, key: str) -> bool:
        path = self.path(key)
        return path is not None and path.is_file()

    def folders(self, key: str) -> list[str]:
        """The names of the folders directly under ``key``, sorted."""
        path = self.path(key)
        if path is None or not path.is_dir():
            return []
        return sorted(p.name for p in path.iterdir() if p.is_dir())

    def files(self, key: str, suffix: str) -> list[str]:
        """The names of the files directly under ``key`` that end in ``suffix``, sorted."""
        path = self.path(key)
        if path is None or not path.is_dir():
            return []
        return sorted(p.name for p in path.iterdir() if p.is_file() and p.name.endswith(suffix))


def parse_log(data: bytes, source: str, out: Logs) -> None:
    """Add every JSON-object line with a valid ``t`` to ``out``; the rest go to ``out.bad``.

    The reasons are lookout_common.jsonl's. An unterminated last line is most likely a write
    still in progress in the running job.
    """
    chunks = data.split(b"\n")
    last_index = len(chunks) - 1
    for index, raw in enumerate(chunks):
        if not raw.strip():
            continue
        reason: str | None = None
        value: Any = None
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            reason = REASON_UNTERMINATED if index == last_index else REASON_INVALID_JSON
        if reason is None and not isinstance(value, dict):
            reason = REASON_NOT_OBJECT
        moment = timestamp(value.get("t")) if reason is None else None
        if moment is None:
            out.bad.append({"source": source, "line": index + 1, "reason": reason or "no-valid-t"})
            continue
        out.lines.append(Line(source, index + 1, moment, value))


def read_logs(reader: Reader, folder: str, window: Window) -> Logs:
    """Read the day logs ``<folder>/<YYYY-MM-DD>.jsonl`` that can hold the window's records.

    A record goes in the log of its UTC day, so a day more on each side is enough. The other
    day logs are listed in ``files_skipped`` without being read.
    """
    out = Logs()
    first = window.since.date() - _DAY
    last = (window.until - timedelta(microseconds=1)).date() + _DAY
    for name in reader.files(folder, ".jsonl"):
        key = f"{folder}/{name}"
        try:
            day = date.fromisoformat(name.removesuffix(".jsonl"))
        except ValueError:
            out.files_skipped.append(key)
            continue
        path = reader.path(key)
        if path is None or not first <= day <= last:
            out.files_skipped.append(key)
            continue
        out.files_read.append(key)
        parse_log(path.read_bytes(), key, out)
    out.lines.sort(key=lambda line: (line.t, line.source, line.line))
    return out


class Exclusions:
    """Records that do not count, grouped by (log, reason), with a few references each."""

    def __init__(self) -> None:
        self.groups: dict[tuple[str, str], dict[str, Any]] = {}

    def add(self, log: str, reason: str, line: Line | None = None, **detail: Any) -> None:
        """Count one excluded record (or file, or outage); keep ``detail`` for the first few."""
        group = self.groups.setdefault(
            (log, reason),
            {
                "log": log,
                "reason": reason,
                "count": 0,
                "events": Counter(),
                "sources": Counter(),
                "first_utc": None,
                "last_utc": None,
                "refs": [],
            },
        )
        group["count"] += 1
        if line is not None:
            group["events"][str(line.record.get("event"))] += 1
            group["sources"][line.source] += 1
            first, last = group["first_utc"], group["last_utc"]
            group["first_utc"] = line.t if first is None or line.t < first else first
            group["last_utc"] = line.t if last is None or line.t > last else last
        if (detail or line is not None) and len(group["refs"]) < REF_LIMIT:
            ref = {"ref": line.ref, "t": iso(line.t)} if line is not None else {}
            if detail or not group["refs"]:
                group["refs"].append({**ref, **detail})

    def to_list(self) -> list[dict[str, Any]]:
        return [
            {
                **group,
                "events": sorted_counter(group["events"]),
                "sources": sorted_counter(group["sources"]),
                "first_utc": iso(group["first_utc"]),
                "last_utc": iso(group["last_utc"]),
            }
            for group in self.groups.values()
        ]
