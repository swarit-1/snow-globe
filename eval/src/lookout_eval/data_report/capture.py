"""The data report's capture part: per-camera counts, outages and restarts (PRD §15.3, §16).

Counted records are ``worker: capture`` records of a scheduled runner inside the window
(:func:`capture_exclusion` says why any other record does not count). Per camera it gives:

- **polls**: ``request`` records of the poll stage (``json`` at the ARD cameras, ``head`` at
  Rainier), failed ones included; **image GETs**: ``request`` records of stage ``image``;
- **gaps between polls**, on send times (:func:`send_time`: the record's ``sent_at`` from
  capture 0.2.1, else ``t − elapsed_ms``, since the record is written after the answer): the
  longest, and the shortest, which PRD §15.3's 5-minute rule bounds;
- **the most image GETs in any rolling hour** (half-open 3600 s windows; the rule is 4);
- **frames stored**, by the policy that applied (PRD §8.7, D-028): capture 0.1.0 kept every
  download at full size with no thumbnail (``legacy-every``); 0.2.0 keeps every frame at the
  design-P cameras (``every``) and, elsewhere, only the frame nearest each UTC hour
  (``hourly-nearest``: staged first, full size only once ``promoted``), with a thumbnail for
  every download. The logged keys are checked against the disk, and frame and thumbnail
  sidecars on disk with ``fetched_at_utc`` in the window are counted, so files the log does
  not mention show up (a file only a record after the window names, such as an hourly frame
  promoted later, is listed apart: the disk is read as it is now);
- rate caps and errors, and what ``state/capture/<id>.json`` says *now*, when the report is
  made (``state_now``): a state file holds only its latest state, so for a ``--until`` in the
  past this block describes the time of reading, not the window's end.

Outages and restarts come from the pass records (package docstring).
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

from .common import (
    LEGACY_POLICY,
    POLICY_HOURLY,
    REF_LIMIT,
    Exclusions,
    Interval,
    Line,
    Logs,
    Reader,
    ReportOptions,
    Window,
    clip,
    gap_stats,
    iso,
    max_in_window,
    measure,
    number,
    rounded,
    sorted_counter,
    timestamp,
)

__all__ = ["POLL_STAGES", "Pass", "capture_exclusion", "capture_section", "send_time"]

#: ``request`` stages that are a poll: the ARD cameras' JSON GET, Rainier's HEAD.
POLL_STAGES: frozenset[str] = frozenset({"json", "head"})
IMAGE_STAGE = "image"


@dataclass(frozen=True, slots=True)
class Pass:
    """One scheduled ``pass`` record; ``start`` is its ``t`` minus ``duration_s``."""

    line: Line
    start: datetime
    version: str
    runner: str

    @property
    def t(self) -> datetime:
        return self.line.t

    @classmethod
    def of(cls, line: Line) -> Pass:
        duration = number(line.record.get("duration_s"))
        start = line.t - timedelta(seconds=duration) if duration and duration > 0 else line.t
        return cls(
            line,
            start,
            str(line.record.get("capture_version") or "unknown"),
            str(line.record.get("runner") or "unknown"),
        )


def send_time(line: Line) -> datetime:
    """When a request was sent: its ``sent_at``, or else ``t`` minus ``elapsed_ms``.

    Capture 0.2.1 writes ``sent_at``, the clock reading it also stamps the poll and the image
    GET with, so gaps read from it are the ones capture's scheduler kept, to the millisecond.
    Older records have only ``t``, written after the answer; ``t`` minus ``elapsed_ms`` is
    within about 1 ms of the send (measured: -1.0 to +0.9 ms in capture's 8 h schedule test).
    """
    sent = timestamp(line.record.get("sent_at"))
    if sent is not None:
        return sent
    elapsed = number(line.record.get("elapsed_ms"))
    return (
        line.t - timedelta(milliseconds=elapsed) if elapsed is not None and elapsed > 0 else line.t
    )


def capture_exclusion(line: Line, window: Window, runners: frozenset[str]) -> str | None:
    """Why a ``capture_log`` record does not count, or None when it counts."""
    if line.t < window.since:
        return "before the window"
    if line.t >= window.until:
        return "after the window"
    worker = line.record.get("worker")
    if worker == "archive":
        return "archive event (worker: archive), not live capture"
    if worker is None:
        return "no worker field: the retired Rainier stopgap's records (D-021)"
    if worker != "capture":
        return f"worker {worker!r} is not live capture"
    runner = line.record.get("runner")
    if runner not in runners:
        return f"runner {runner!r} is not a scheduled capture runner"
    return None


def _scheduled_pass(line: Line, runners: frozenset[str]) -> bool:
    r = line.record
    return r.get("event") == "pass" and r.get("worker") == "capture" and r.get("runner") in runners


def _policy(record: dict[str, Any] | Any) -> str:
    value = record.get("frame_policy")
    return value if isinstance(value, str) and value else LEGACY_POLICY


def _frame_key(record: Any, policy: str) -> str | None:
    """The full-size ``frames/`` key a ``stored`` record wrote (None for a staged frame)."""
    if policy == POLICY_HOURLY:
        return None  # staged; it reaches frames/ only through a later "promoted" record
    for name in ("frame_key", "key"):
        value = record.get(name)
        if isinstance(value, str) and value.startswith("frames/"):
            return value
    return None


def _thumb_key(record: Any) -> str | None:
    value = record.get("thumb_key")
    return value if isinstance(value, str) and value.startswith("thumbs/") else None


@dataclass(slots=True)
class _Camera:
    polls: list[datetime] = field(default_factory=list)
    gets: list[datetime] = field(default_factory=list)
    get_status: Counter[str] = field(default_factory=Counter)
    failed_requests: int = 0
    events: Counter[str] = field(default_factory=Counter)
    rate_capped: Counter[str] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)
    unchanged: Counter[str] = field(default_factory=Counter)
    full_keys: set[str] = field(default_factory=set)
    thumb_keys: set[str] = field(default_factory=set)
    policies: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    runners: Counter[str] = field(default_factory=Counter)

    def policy(self, policy: str, version: str, moment: datetime) -> dict[str, Any]:
        acc = self.policies.setdefault(
            (policy, version),
            {
                "policy": policy,
                "capture_version": version,
                "downloads": 0,
                "full_size": 0,
                "thumbnails": 0,
                "first_utc": moment,
                "last_utc": moment,
            },
        )
        acc["first_utc"] = min(acc["first_utc"], moment)
        acc["last_utc"] = max(acc["last_utc"], moment)
        return acc

    def add(self, line: Line) -> None:
        r = line.record
        event = str(r.get("event"))
        self.events[event] += 1
        self.runners[str(r.get("runner"))] += 1
        version = str(r.get("capture_version") or "unknown")
        if event == "request":
            stage, status = r.get("stage"), r.get("status")
            if stage in POLL_STAGES:
                self.polls.append(send_time(line))
            elif stage == IMAGE_STAGE:
                self.gets.append(send_time(line))
                self.get_status[str(status)] += 1
            if r.get("error") or not (isinstance(status, int) and 200 <= status < 400):
                self.failed_requests += 1
        elif event == "rate_capped":
            self.rate_capped[str(r.get("reason") or "unknown")] += 1
        elif event == "error":
            self.errors[str(r.get("stage") or "unknown")] += 1
        elif event == "unchanged":
            self.unchanged[str(r.get("reason") or "unknown")] += 1
        elif event == "stored":
            policy = _policy(r)
            acc = self.policy(policy, version, line.t)
            acc["downloads"] += 1
            frame, thumb = _frame_key(r, policy), _thumb_key(r)
            if frame is not None and frame not in self.full_keys:
                self.full_keys.add(frame)
                acc["full_size"] += 1
            if thumb is not None and thumb not in self.thumb_keys:
                self.thumb_keys.add(thumb)
                acc["thumbnails"] += 1
        elif event == "promoted":
            key = r.get("key")
            if isinstance(key, str) and key.startswith("frames/") and key not in self.full_keys:
                self.full_keys.add(key)
                self.policy(POLICY_HOURLY, version, line.t)["full_size"] += 1
        elif event == "thumb_repaired":  # a thumbnail made later for a stored frame
            thumb = _thumb_key(r)
            if thumb is not None:
                self.thumb_keys.add(thumb)


def _outages(
    passes: Sequence[Pass], window: Window, options: ReportOptions
) -> tuple[list[dict[str, Any]], list[Interval]]:
    """Gaps over ``capture_gap_s`` between scheduled passes, and the time each one lost."""
    gap_s, interval = options.capture_gap_s, timedelta(seconds=options.capture_interval_s)
    normal = [
        (b.t - a.t).total_seconds()
        for a, b in pairwise(passes)
        if a.t in window and b.t in window and (b.t - a.t).total_seconds() <= gap_s
    ]
    spacing = statistics.median(normal) if normal else options.capture_interval_s
    rows: list[dict[str, Any]] = []
    lost: list[Interval] = []

    def add(start: datetime, end: datetime, lost_from: datetime, kind: str) -> None:
        gap = (end - start).total_seconds()
        part = clip([(lost_from, end)], window)
        lost.extend(part)
        rows.append(
            {
                "job": "capture",
                "kind": kind,
                "start_utc": iso(start),
                "end_utc": iso(end),
                "gap_s": round(gap, 3),
                "lost_s": round(measure(part), 3),
                "missed_passes_est": max(0, round(gap / spacing) - 1),
                "ongoing": end >= window.until,
            }
        )

    if not passes:
        add(window.since, window.until, window.since, "no scheduled pass in the logs")
        return rows, lost
    first, last = passes[0].t, passes[-1].t
    lead_end = min(first, window.until)
    if first > window.since and (lead_end - window.since).total_seconds() > gap_s:
        add(window.since, lead_end, window.since, "no pass yet")
    for a, b in pairwise(passes):
        if (b.t - a.t).total_seconds() > gap_s and b.t > window.since and a.t < window.until:
            add(a.t, b.t, a.t + interval, "gap between passes")
    if last < window.until and (window.until - last).total_seconds() > gap_s:
        add(last, window.until, last + interval, "no pass since")  # also a loop dead before since
    return rows, lost


def _restarts(
    passes: Sequence[Pass], counted: Sequence[Line], window: Window, options: ReportOptions
) -> list[dict[str, Any]]:
    """Loop stops and starts seen in the log (see the package docstring for the signs)."""
    stops = [line for line in counted if line.record.get("event") == "loop_stopped"]
    shortest = options.capture_interval_s - options.restart_tolerance_s
    rows: list[dict[str, Any]] = []
    for a, b in pairwise(passes):
        if b.start not in window:
            continue
        evidence = []
        if a.version != b.version:
            evidence.append(f"capture_version {a.version} -> {b.version}")
        if a.runner != b.runner:
            evidence.append(f"runner {a.runner} -> {b.runner}")
        spacing = (b.start - a.start).total_seconds()
        if spacing < shortest:
            evidence.append(
                f"pass starts {spacing:.1f} s apart, under the "
                f"{options.capture_interval_s:g} s interval of one loop"
            )
        evidence.extend(
            f"loop_stopped at {iso(s.t)}: {s.record.get('error')}"
            for s in stops
            if a.t <= s.t <= b.start
        )
        if not evidence:
            continue
        old = [
            line.t
            for line in counted
            if a.t <= line.t < b.start
            and str(line.record.get("capture_version") or "unknown") == a.version
        ]
        stopped_after = max(old, default=a.t)
        rows.append(
            {
                "job": "capture",
                "kind": "restart",
                "stopped_after_utc": iso(stopped_after),
                "started_utc": iso(b.start),
                "duration_s": round((b.start - stopped_after).total_seconds(), 3),
                "from_version": a.version,
                "to_version": b.version,
                "last_pass_utc": iso(a.t),
                "first_pass_utc": iso(b.t),
                "pass_gap_s": round((b.t - a.t).total_seconds(), 3),
                "an_outage": (b.t - a.t).total_seconds() > options.capture_gap_s,
                "evidence": evidence,
            }
        )
    return rows


def _day_in_range(name: str, window: Window) -> bool:
    try:
        day = datetime.strptime(name, "%Y-%m-%d").date()
    except ValueError:
        return False
    return window.since.date() - timedelta(days=1) <= day <= window.until.date()


def named_keys(record: Any) -> set[str]:
    """The ``frames/`` and ``thumbs/`` keys a capture record names (stored, promoted, repaired)."""
    keys = set()
    for name in ("key", "frame_key", "thumb_key"):
        value = record.get(name)
        if isinstance(value, str) and value.startswith(("frames/", "thumbs/")):
            keys.add(value)
    return keys


def _disk_check(
    reader: Reader, viewpoint_id: str, cam: _Camera, window: Window, later: set[str]
) -> dict[str, Any]:
    """The logged keys against the disk, and sidecars on disk that the log does not name.

    The disk is read as it is now, so for a window that ended earlier, a file fetched inside
    it can have reached the disk later: an hourly frame promoted after ``until``. A file that
    only a record after the window names (``later``) is ``logged_after_window``, not unlogged.
    """
    out: dict[str, Any] = {}
    for kind, folder, logged in (
        ("full_size", "frames", cam.full_keys),
        ("thumbnails", "thumbs", cam.thumb_keys),
    ):
        missing = sorted(key for key in logged if not reader.exists(key))
        in_window: list[str] = []
        policies: Counter[str] = Counter()
        without_time = 0
        base = f"{folder}/{viewpoint_id}"
        for day in (d for d in reader.folders(base) if _day_in_range(d, window)):
            for name in reader.files(f"{base}/{day}", ".json"):
                doc = reader.json(f"{base}/{day}/{name}")
                fetched = timestamp(doc.get("fetched_at_utc")) if isinstance(doc, dict) else None
                if fetched is None:
                    without_time += 1
                elif fetched in window:
                    in_window.append(f"{base}/{day}/{name.removesuffix('.json')}.jpg")
                    policies[str(doc.get("frame_policy") or LEGACY_POLICY)] += 1
        after = sorted(key for key in in_window if key not in logged and key in later)
        unlogged = sorted(key for key in in_window if key not in logged and key not in later)
        out[kind] = {
            "logged": len(logged),
            "logged_present": len(logged) - len(missing),
            "logged_missing": len(missing),
            "missing_examples": missing[:REF_LIMIT],
            "on_disk_in_window": len(in_window),
            "on_disk_by_policy": sorted_counter(policies),
            "on_disk_logged_after_window": len(after),
            "logged_after_window_examples": after[:REF_LIMIT],
            "on_disk_not_logged": len(unlogged),
            "not_logged_examples": unlogged[:REF_LIMIT],
            "sidecars_without_time": without_time,
        }
    return out


def _camera_state(reader: Reader, viewpoint_id: str, now: datetime) -> dict[str, Any] | None:
    """What ``state/capture/<id>.json`` says when read at ``now`` (module docstring)."""
    doc = reader.json(f"state/capture/{viewpoint_id}.json")
    if not isinstance(doc, dict):
        return None
    last_poll = timestamp(doc.get("last_poll_at"))
    gets = [timestamp(g.get("t")) for g in doc.get("gets") or [] if isinstance(g, dict)]
    hour_ago = now - timedelta(hours=1)
    staged = doc.get("staged")
    age = (now - last_poll).total_seconds() if last_poll is not None else None
    return {
        "read_utc": iso(now),
        "last_poll_utc": iso(last_poll),
        "last_poll_age_s": round(age, 3) if age is not None and age >= 0 else None,
        "polls_total": doc.get("polls"),
        "gets_in_hour_before_read": sum(1 for g in gets if g is not None and hour_ago < g <= now),
        "staged_now": len(staged) if isinstance(staged, list) else None,
        "failures": doc.get("failures"),
        "not_before": doc.get("not_before"),
        "last_error": doc.get("last_error"),
    }


def _camera_row(
    reader: Reader,
    vp: str,
    cam: _Camera,
    window: Window,
    options: ReportOptions,
    now: datetime,
    later: set[str],
) -> dict[str, Any]:
    polls = gap_stats(cam.polls)
    most, most_from = max_in_window(cam.gets, options.rolling_window_s)
    by_policy = sorted(cam.policies.values(), key=lambda acc: acc["first_utc"])
    return {
        "polls": len(cam.polls),
        "image_gets": len(cam.gets),
        "image_get_status": sorted_counter(cam.get_status),
        "downloads_stored": sum(acc["downloads"] for acc in by_policy),
        "frames": {
            "full_size": len(cam.full_keys),
            "thumbnails": len(cam.thumb_keys),
            "promoted": cam.events.get("promoted", 0),
            "staging_dropped": cam.events.get("staging_dropped", 0),
            "by_policy": [
                {**acc, "first_utc": iso(acc["first_utc"]), "last_utc": iso(acc["last_utc"])}
                for acc in by_policy
            ],
            "disk": _disk_check(reader, vp, cam, window, later),
        },
        "unchanged": sorted_counter(cam.unchanged),
        "duplicates": cam.events.get("duplicate", 0),
        "rate_capped": {
            "total": sum(cam.rate_capped.values()),
            "by_reason": sorted_counter(cam.rate_capped),
        },
        "errors": {
            "error_records": sum(cam.errors.values()),
            "by_stage": sorted_counter(cam.errors),
            "failed_requests": cam.failed_requests,
            "backoffs": cam.events.get("backoff", 0),
        },
        "poll_gap_s": {
            "longest": polls["longest"],
            "shortest_on_send_times": polls["shortest"],
        },
        "max_image_gets_in_rolling_hour": {
            "count": most,
            "window_from_utc": iso(most_from),
            "rule_max": options.max_gets_per_rolling_hour,
            "within_rule": most <= options.max_gets_per_rolling_hour,
        },
        "events": sorted_counter(cam.events),
        "runners": sorted_counter(cam.runners),
        "state_now": _camera_state(reader, vp, now),
    }


def capture_section(
    reader: Reader,
    logs: Logs,
    window: Window,
    viewpoint_ids: Sequence[str],
    options: ReportOptions,
    exclusions: Exclusions,
    *,
    now: datetime,
) -> tuple[dict[str, Any], list[Interval]]:
    """The capture part of the report, and the intervals its outages lost (for the clock).

    ``now`` is when the report is made: the state files and the fetcher lock are read as
    they are then.
    """
    runners = options.scheduled_runners
    counted: list[Line] = []
    later: set[str] = set()  # files that records after the window name (_disk_check)
    for line in logs.lines:
        reason = capture_exclusion(line, window, runners)
        if reason is None:
            counted.append(line)
        else:
            exclusions.add("capture_log", reason, line)
            if line.t >= window.until and line.record.get("worker") == "capture":
                later |= named_keys(line.record)
    passes = sorted(
        (Pass.of(line) for line in logs.lines if _scheduled_pass(line, runners)),
        key=lambda p: p.t,
    )
    in_window = [p for p in passes if p.t in window]
    cams: dict[str, _Camera] = {vp: _Camera() for vp in viewpoint_ids}
    without_viewpoint: Counter[str] = Counter()
    for line in counted:
        event, vp = line.record.get("event"), line.record.get("viewpoint_id")
        if event == "pass":
            continue
        if isinstance(vp, str):
            cams.setdefault(vp, _Camera()).add(line)
        else:
            without_viewpoint[str(event)] += 1
    outages, lost = _outages(passes, window, options)
    spacing = [(b.t - a.t).total_seconds() for a, b in pairwise(in_window)]
    lock = reader.json("state/locks/nps-fetcher.lock")
    section = {
        "passes": {
            "count": len(in_window),
            "first_utc": iso(in_window[0].t) if in_window else None,
            "last_utc": iso(in_window[-1].t) if in_window else None,
            "spacing_s": {
                "median": rounded(statistics.median(spacing)) if spacing else None,
                "min": rounded(min(spacing)) if spacing else None,
                "max": rounded(max(spacing)) if spacing else None,
            },
            "capture_versions": sorted_counter(Counter(p.version for p in in_window)),
            "runners": sorted_counter(Counter(p.runner for p in in_window)),
        },
        "records_counted": len(counted),
        "records_without_viewpoint": sorted_counter(without_viewpoint),
        "viewpoints": {
            vp: _camera_row(reader, vp, cams[vp], window, options, now, later)
            for vp in sorted(cams)
        },
        "outages": outages,
        "restarts": _restarts(passes, counted, window, options),
        "current_fetcher_lock": (
            {key: lock.get(key) for key in ("pid", "command", "started_at")}
            if isinstance(lock, dict)
            else None
        ),
    }
    return section, lost
