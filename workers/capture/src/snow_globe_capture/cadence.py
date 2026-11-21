"""A camera's update cadence, measured from what capture already logged (no network).

PLAN M0.5 asks for Rainier's real update cadence (PRD §8.1: "cadence to be measured"), and
PRD §15.1 says never to capture faster than the camera's own update. This module measures
it from records already on disk, without a request to nps.gov:

- ``capture_log/<UTC date>.jsonl``: every poll's outcome. The capture worker logs the
  park camera's HEAD ``Last-Modified`` in its poll outcomes (``http_last_modified`` and,
  since 0.2.0, ``head_last_modified``) and the ARD feed's JSON ``Last-Modified``
  (``json_last_modified``; before 0.2.0 only in ``unchanged`` records) and ``imagedate``.
  Each poll's ``request`` record gives the time the server answered. The D-021 stopgap
  logged one record per poll (``"vp"``, no ``"worker"``).
- Sidecars (``frames/``, ``thumbs/``, ``cache/staging/``): the validators of every stored
  download (``head_last_modified``, ``http_last_modified``, ``json_last_modified``,
  ``json_imagedate_raw``), including the stopgap's frames.

Each poll yields at most one observed value. Two measures come from them:

- **Intervals.** The change of value between two *consecutive* polls at most ``max_gap_s``
  apart (1.8 poll intervals by default: no poll missed in between). A longer gap (the laptop
  asleep, a back-off) breaks the pair, and so does a poll whose value was not logged. An HTTP
  304 repeats the value of the poll just before it, and only when that one is known.
- **Age at poll:** the poll's answer time minus the value it saw. The value is the time of
  the camera's newest update, so every age is a lower bound on the gap between updates that
  contained the poll; for updates every ``P`` s, ages spread over ``[0, P)``.

If (nearly) every consecutive pair changed, the camera updates at least as often as it is
polled, and the intervals only measure the poll spacing: the series is ``poll_limited``, and
the age at poll is the measurement instead. Its maximum says how old the newest update was
at worst, over the polls sampled; its median estimates half the update interval.
"""

from __future__ import annotations

import itertools
import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from snow_globe_common.jsonl import iter_jsonl_bytes
from snow_globe_common.manifest import Viewpoint
from snow_globe_common.paths import capture_log_key
from snow_globe_common.storage import KeyNotFoundError, Storage
from snow_globe_common.timeutil import isoformat_z, parse_nps_imagedate

from .state import parse_time

__all__ = [
    "MINUTE_GRID_SHARE",
    "POLL_LIMITED_SAME_FRACTION",
    "Poll",
    "camera_cadence",
    "quantile",
    "summarise",
]

_POLL_EVENTS = frozenset({"unchanged", "rate_capped", "duplicate", "stored"})
_SIDECAR_ROOTS = ("frames", "thumbs", "cache/staging/frames")
#: A series is poll-limited when at most this share of its decided pairs saw no change.
POLL_LIMITED_SAME_FRACTION = 0.1
#: A poll's ``request`` record counts as its answer time if logged at most this long before.
_REQUEST_MATCH_S = 120.0
#: Values are on a one-minute grid when at least this share of intervals (n >= 10) is within
#: 3 s of whole minutes; unaligned values would give about 12 %.
MINUTE_GRID_SHARE = 0.5


@dataclass
class Poll:
    """One poll of the camera and what it showed."""

    t: datetime
    source: str  # "capture" | "stopgap" | "sidecar"
    event: str
    value: datetime | None = None  # HEAD (park camera) or JSON (ARD) Last-Modified
    same_as_previous: bool = False  # an HTTP 304: unchanged since the last value sent
    imagedate: datetime | None = None  # ARD JSON imagedate (the image time, to the minute)
    failed: bool = False
    #: When the server answered the poll's HEAD or JSON GET (its ``request`` record), if known.
    answered_at: datetime | None = None
    #: When that request was sent (``sent_at``; for records before 0.2.1, ``t`` minus
    #: ``elapsed_ms``), if known.
    sent_at: datetime | None = None

    @property
    def at(self) -> datetime:
        """The best time for the poll's value: the answer time, else the record's time."""
        return self.answered_at or self.t

    @property
    def sent(self) -> datetime:
        """The best time for the poll's send: the request's send, else the record's time."""
        return self.sent_at or self.t


def quantile(values: Iterable[float], q: float) -> float | None:
    """The ``q`` quantile with linear interpolation between order statistics (numpy default)."""
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def summarise(values: Iterable[float]) -> dict[str, Any]:
    """n, median, p10, p90, min, max and mean of some seconds (None when empty)."""
    data = sorted(values)
    if not data:
        return {"n": 0, "median": None, "p10": None, "p90": None, "min": None, "max": None}
    return {
        "n": len(data),
        "median": round(quantile(data, 0.5) or 0.0, 1),
        "p10": round(quantile(data, 0.1) or 0.0, 1),
        "p90": round(quantile(data, 0.9) or 0.0, 1),
        "min": round(data[0], 1),
        "max": round(data[-1], 1),
        "mean": round(sum(data) / len(data), 1),
    }


def _http_time(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else None


def _imagedate(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return parse_nps_imagedate(value)
    except ValueError:
        return None


def _name(key: str) -> str:
    return key.rsplit("/", 1)[-1].rsplit(".", 1)[0]


class _Sidecars:
    """The camera's sidecars in the window's date folders, read on demand."""

    def __init__(self, storage: Storage, viewpoint_id: str, days: Iterable[date]) -> None:
        self.storage = storage
        self.keys: dict[str, str] = {}  # file stem -> first sidecar key found
        for root in _SIDECAR_ROOTS:
            for day in days:
                for key in storage.list_prefix(f"{root}/{viewpoint_id}/{day.isoformat()}/"):
                    if key.endswith(".json"):
                        self.keys.setdefault(_name(key), key)
        self._docs: dict[str, dict[str, Any] | None] = {}

    def doc(self, stem: str) -> dict[str, Any] | None:
        if stem not in self._docs:
            key = self.keys.get(stem)
            doc: Any = None
            if key is not None:
                try:
                    doc = self.storage.read_json(key)
                except (KeyNotFoundError, ValueError):
                    doc = None
            self._docs[stem] = doc if isinstance(doc, dict) else None
        return self._docs[stem]


def _sidecar_value(
    doc: Mapping[str, Any] | None, ard: bool
) -> tuple[datetime | None, datetime | None]:
    if not doc:
        return None, None
    if ard:
        return _http_time(doc.get("json_last_modified")), _imagedate(doc.get("json_imagedate_raw"))
    return _http_time(doc.get("head_last_modified") or doc.get("http_last_modified")), None


def _request_sent(record: Mapping[str, Any], when: datetime) -> datetime | None:
    """A ``request`` record's send: ``sent_at`` (0.2.1), else ``t`` minus ``elapsed_ms``."""
    sent = parse_time(record.get("sent_at"))
    if sent is not None:
        return sent
    elapsed = record.get("elapsed_ms")
    if isinstance(elapsed, int | float) and not isinstance(elapsed, bool):
        return when - timedelta(milliseconds=float(elapsed))
    return None


def _log_polls(
    records: Iterable[Mapping[str, Any]],
    viewpoint_id: str,
    ard: bool,
    sidecars: _Sidecars,
    start: datetime,
    end: datetime,
    referenced: set[str],
) -> list[Poll]:
    polls: list[Poll] = []
    asked: datetime | None = None  # the newest HEAD/JSON request record of this camera
    asked_sent: datetime | None = None  # and when that request was sent
    for record in records:
        when = parse_time(record.get("t"))
        if when is None or not start <= when <= end:
            continue
        event = str(record.get("event"))
        if record.get("vp") == viewpoint_id and "worker" not in record:  # the D-021 stopgap
            poll = Poll(when, "stopgap", event)
            if event == "stored" and isinstance(record.get("file"), str):
                stem = _name(record["file"])
                referenced.add(stem)
                doc = sidecars.doc(stem)
                poll.value, _ = _sidecar_value(doc, ard=False)
                poll.answered_at = parse_time((doc or {}).get("fetched_at_utc"))
            polls.append(poll)
            continue
        if record.get("worker") != "capture" or record.get("viewpoint_id") != viewpoint_id:
            continue
        stage = record.get("stage")
        if event == "request":
            if stage in ("head", "json"):
                asked = when
                asked_sent = _request_sent(record, when)
            continue
        lag = (when - asked).total_seconds() if asked is not None else None
        matched = lag is not None and 0 <= lag <= _REQUEST_MATCH_S
        answered = asked if matched else None
        sent = asked_sent if matched else None
        if event == "error":
            if stage not in ("head", "json", "image", "store"):
                continue
            if stage != "store":  # a store failure saw a new value: read it below
                failed = stage in ("head", "json")
                polls.append(Poll(when, "capture", event, failed=failed, sent_at=sent))
                asked = asked_sent = None
                continue
        elif event not in _POLL_EVENTS:
            continue
        poll = Poll(when, "capture", event, answered_at=answered, sent_at=sent)
        asked = asked_sent = None
        if isinstance(record.get("key"), str):  # stored, or a duplicate of a stored key
            stem = _name(record["key"])
            referenced.add(stem)
            poll.value, poll.imagedate = _sidecar_value(sidecars.doc(stem), ard)
        if ard:
            if event == "unchanged" and record.get("reason") == "json_not_modified":
                poll.same_as_previous = True
            if record.get("json_last_modified"):
                poll.value = _http_time(record.get("json_last_modified"))
            poll.imagedate = poll.imagedate or _imagedate(record.get("imagedate_raw"))
        elif poll.value is None:
            poll.value = _http_time(
                record.get("head_last_modified") or record.get("http_last_modified")
            )
        polls.append(poll)
    return polls


def _series(polls: list[Poll], attr: str, max_gap_s: float) -> dict[str, Any]:
    """Intervals and ages at poll of the values seen (see the module docstring)."""
    values: list[datetime | None] = []
    previous: datetime | None = None  # the value of the poll just before (None: unknown)
    for poll in polls:
        value = getattr(poll, attr)
        if value is None and attr == "value" and poll.same_as_previous:
            value = previous  # a 304 repeats the poll just before, only if that one is known
        values.append(value)
        previous = value
    changed: list[float] = []
    pairs: Counter[str] = Counter()
    for (a, va), (b, vb) in itertools.pairwise(zip(polls, values, strict=True)):
        if (b.t - a.t).total_seconds() > max_gap_s:
            pairs["gap"] += 1
        elif va is None or vb is None:
            pairs["unknown"] += 1
        elif vb == va:
            pairs["same"] += 1
        elif vb > va:
            pairs["changed"] += 1
            changed.append((vb - va).total_seconds())
        else:
            pairs["backwards"] += 1
    ages = [(p.at - v).total_seconds() for p, v in zip(polls, values, strict=True) if v]
    distinct = sorted({v for v in values if v is not None})
    histogram = Counter(int(round(s / 10.0) * 10) for s in changed)
    near_minute = [s for s in changed if abs(s - 60.0 * round(s / 60.0)) <= 3.0]
    decided = pairs["same"] + pairs["changed"]
    poll_limited = pairs["changed"] > 0 and pairs["same"] <= POLL_LIMITED_SAME_FRACTION * decided
    age_stats = summarise(ages)
    # By chance 7 s in 60 (12 %) of intervals would be within 3 s of whole minutes.
    minute_grid = len(changed) >= 10 and len(near_minute) >= MINUTE_GRID_SHARE * len(changed)
    return {
        "distinct_values": len(distinct),
        "first": isoformat_z(distinct[0], "seconds") if distinct else None,
        "last": isoformat_z(distinct[-1], "seconds") if distinct else None,
        "consecutive_pairs": dict(sorted(pairs.items())),
        "unchanged_pair_fraction": round(pairs["same"] / decided, 3) if decided else None,
        "interval_s": summarise(changed),
        "histogram_10s": {str(k): v for k, v in sorted(histogram.items())},
        # Values on a one-minute grid would put every interval within a few s of whole
        # minutes; the seconds-of-minute spread shows how tight that grid is.
        "intervals_within_3s_of_whole_minutes": f"{len(near_minute)}/{len(changed)}",
        "second_of_minute": summarise(float(v.second) for v in distinct),
        # Writes at fixed minute marks (the ARD JSON: twice every 10 min) show up here.
        "minute_mod_10_of_values": {
            str(k): n for k, n in sorted(Counter(v.minute % 10 for v in distinct).items())
        },
        "all_distinct_interval_s": summarise(
            (b - a).total_seconds() for a, b in itertools.pairwise(distinct)
        ),
        "age_at_poll_s": age_stats,
        "age_histogram_30s": {
            str(k): n for k, n in sorted(Counter(int(a // 30) * 30 for a in ages).items())
        },
        "poll_limited": poll_limited,
        "values_on_minute_grid": minute_grid,
        "cadence": _verdict(poll_limited, minute_grid, summarise(changed), age_stats),
    }


def _verdict(
    poll_limited: bool,
    minute_grid: bool,
    intervals: Mapping[str, Any],
    ages: Mapping[str, Any],
) -> dict[str, Any]:
    """What the series says about the camera's update interval, and on what basis.

    No period is derived from the ages: polls can lock to the camera's phase (the stopgap
    polled at the same second of every minute), so ages need not spread evenly.
    """
    if poll_limited and ages.get("n"):
        grid = (
            "values lie on a one-minute grid, so updates come at whole minutes; "
            if minute_grid
            else ""
        )
        return {
            "basis": "age_at_poll",
            "updates_at_least_every_s": ages["max"],
            "age_p90_s": ages["p90"],
            "text": (
                f"(nearly) every consecutive pair changed, so the intervals only measure the "
                f"poll spacing; {grid}at all {ages['n']} polls with a value the newest update "
                f"was at most {ages['max']:g} s old (median {ages['median']:g} s, p90 "
                f"{ages['p90']:g} s): at the times sampled the camera updated at least every "
                f"{ages['max']:g} s"
            ),
        }
    if intervals.get("n"):
        return {
            "basis": "intervals",
            "update_interval_median_s": intervals["median"],
            "text": (
                f"some consecutive polls saw no change, so the camera updates more slowly than "
                f"it is polled: median interval {intervals['median']:g} s (n={intervals['n']}); "
                "histogram_10s and minute_mod_10_of_values show whether there is one cadence"
            ),
        }
    return {"basis": None, "text": "no consecutive pair with known values"}


def camera_cadence(
    storage: Storage,
    viewpoint: Viewpoint,
    *,
    now: datetime,
    hours: float = 24.0,
    max_gap_s: float | None = None,
) -> dict[str, Any]:
    """Measure one camera's update cadence over the last ``hours`` (module docstring)."""
    end = now
    start = now - timedelta(hours=hours)
    ard = viewpoint.truth_camera.kind == "ard-aircam"
    poll_s = max(300.0, float(viewpoint.truth_camera.polite_caps.poll_interval_s))
    gap_limit = max_gap_s if max_gap_s is not None else 1.8 * poll_s
    days = [start.date() + timedelta(days=i) for i in range((end.date() - start.date()).days + 1)]
    sidecars = _Sidecars(storage, viewpoint.id, days)
    records: list[dict[str, Any]] = []
    logs_read = []
    for day in days:
        key = capture_log_key(day)
        try:
            data = storage.get_bytes(key)
        except KeyNotFoundError:
            continue
        logs_read.append(key)
        records.extend(iter_jsonl_bytes(data))
    referenced: set[str] = set()
    polls = _log_polls(records, viewpoint.id, ard, sidecars, start, end, referenced)
    # Stored downloads with no log record (a lost log): one poll each, from the sidecar.
    for stem in sorted(set(sidecars.keys) - referenced):
        doc = sidecars.doc(stem)
        fetched = parse_time((doc or {}).get("fetched_at_utc"))
        if fetched is None or not start <= fetched <= end:
            continue
        value, imagedate = _sidecar_value(doc, ard)
        polls.append(Poll(fetched, "sidecar", "stored", value=value, imagedate=imagedate))
    polls.sort(key=lambda p: p.t)
    # Spacing is measured between sends (each poll's request record), not between the outcome
    # records, which come after a poll's answer and any image GET it led to.
    gaps = [(b.sent - a.sent).total_seconds() for a, b in itertools.pairwise(polls)]
    series = {
        "last_modified": {
            "what": "JSON feed Last-Modified" if ard else "image HEAD Last-Modified",
            **_series(polls, "value", gap_limit),
        }
    }
    if ard:
        series["imagedate"] = {
            "what": "JSON feed imagedate",
            **_series(polls, "imagedate", gap_limit),
        }
    return {
        "viewpoint_id": viewpoint.id,
        "camera_kind": viewpoint.truth_camera.kind,
        "window_utc": [isoformat_z(start, "seconds"), isoformat_z(end, "seconds")],
        "hours": hours,
        "logs_read": logs_read,
        "sidecars_found": len(sidecars.keys),
        "polls": {
            "total": len(polls),
            "by_source": dict(sorted(Counter(p.source for p in polls).items())),
            "by_event": dict(sorted(Counter(p.event for p in polls).items())),
            "with_value": sum(1 for p in polls if p.value is not None or p.same_as_previous),
            "with_answer_time": sum(1 for p in polls if p.answered_at is not None),
            "with_send_time": sum(1 for p in polls if p.sent_at is not None),
            "failed": sum(1 for p in polls if p.failed),
        },
        # Send to send (the request records; a poll without one counts from its record).
        "poll_spacing_s": summarise(gaps),
        "max_gap_s": gap_limit,
        "gaps_over_max": sum(1 for g in gaps if g > gap_limit),
        "series": series,
        "note": (
            "interval = change of value between consecutive polls <= max_gap_s apart; when "
            "(nearly) every pair changed (poll_limited) the intervals only measure the poll "
            f"spacing ({poll_s:g} s) and age_at_poll_s (answer time minus value) measures the "
            "camera instead: its max is how old the newest update was at worst, at the polls "
            "sampled. Server Last-Modified is compared with this machine's clock."
        ),
    }
