"""The data report's capture part: per-camera counts, outages and restarts (PRD §15.3, §16).

Counted records are ``worker: capture`` records of a scheduled runner inside the window
(:func:`capture_exclusion` says why any other record does not count). Per camera it gives:

- **polls**: ``request`` records of the poll stage (``json`` at the ARD cameras, ``head`` at
  Rainier), failed ones included; **image GETs**: ``request`` records of stage ``image``;
- **gaps between polls**, send to send (:func:`send_time`: the record's ``sent_at`` from
  capture 0.2.1, else ``t − elapsed_ms``, since the record is written after the answer): the
  longest, and the shortest, which is how capture's scheduler spaces polls;
- **the most image GETs in any rolling hour**, on send times (half-open 3600 s windows; the
  rule is 4);
- **the polite-collection evidence at nps.gov** (PRD §15.3; the lead's rule of 20 Nov, which
  settles D-048's open question): nps.gov received a request no earlier than its send and no
  later than its answer. :func:`earliest_receipt` and :func:`latest_receipt` bound those from
  the record's millisecond stamps (``sent_at``, else ``t − elapsed_ms`` less 0.05 ms;
  ``answer_bound_at``, the bound capture logs for a request that failed after it may have
  left, else ``answered_at``, else ``t`` + 1 ms). So the least gap nps.gov can have seen
  between two consecutive polls is ``earliest(N+1) − latest(N)`` (``poll_gap_at_nps``), and
  the least span of 5 consecutive image GETs is ``earliest(5th) − latest(1st)``
  (``image_gets_at_nps``: 5 GETs within 3600 s would break the cap of 4 in any rolling hour).
  A pair or window whose bound is under the limit, or whose last request has no earliest
  receipt or first request no latest, is *unproven*: not shown to be at least 300 s (3600 s)
  apart at nps.gov, which is not the same as shown to be closer;
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

Outages and restarts come from the pass records (package docstring). A restart's *excess*
(the gate rule's lost time for it, :func:`restart_excess`) comes from the poll records: the
longest any camera went from its last poll before the restart to its first poll after, less
one 300 s interval. A poll is before the restart when the old loop wrote it: at or before the
old loop's last record (:func:`stopped_after`), which a ``loop_started`` record or a pass's
``started_at`` places exactly where capture logs them (unless the wall clock stepped back
across the restart). A camera with no poll after the restart is flagged on its row
(``not_polled_since_restart``; the lead's ruling of 20 Nov, D-078). Its gap so far counts, to
the window's end, while the latest loop in the logs lists it (not yet due, or in a back-off);
when that loop does not list it (dropped), it is left out of the excess (D-078).

TODO(M1): ``answer_bound`` events of kind ``unanswered_reservation`` (a poll or GET reserved
by a process that died before it saved the answer, so it may have been sent with no request
record) are not yet turned into requests for the bounds at nps.gov; none is in the logs so
far. The other ``answer_bound`` events (``legacy_state``, ``legacy_record``: capture 0.2.1's
first read of 0.2.0's state) bound requests whose own records are in the logs and bound them
more tightly, so they are not needed.
"""

from __future__ import annotations

import bisect
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
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
    LogKey,
    Logs,
    Reader,
    ReportOptions,
    Request,
    Window,
    capture_release,
    clip,
    floor_ms,
    gap_stats,
    iso,
    max_in_window,
    measure,
    number,
    provable_spans,
    rounded,
    sorted_counter,
    timestamp,
)

__all__ = [
    "POLL_STAGES",
    "SPLIT_LOOP_STARTED",
    "SPLIT_LOOP_STOPPED",
    "SPLIT_OLD_LOOP",
    "Pass",
    "capture_exclusion",
    "capture_section",
    "earliest_receipt",
    "latest_receipt",
    "restart_excess",
    "send_time",
    "stopped_after",
]

#: ``request`` stages that are a poll: the ARD cameras' JSON GET, Rainier's HEAD.
POLL_STAGES: frozenset[str] = frozenset({"json", "head"})
IMAGE_STAGE = "image"
#: Capture writes ``t`` and ``sent_at`` to the millisecond, truncated (``isoformat_z``).
STAMP_RESOLUTION = timedelta(milliseconds=1)
#: Capture logs ``elapsed_ms`` rounded to 0.1 ms, so it can be up to 0.05 ms short.
ELAPSED_ROUNDING = timedelta(microseconds=50)


@dataclass(frozen=True, slots=True)
class Pass:
    """One scheduled ``pass`` record, and when the pass began.

    ``start`` is the record's own ``started_at``, the pass's wall-clock start, where capture
    logs one (``exact_start``); one later than the record's ``t`` (a wall-clock step back
    during the pass) is not used. Otherwise, as for every record of capture 0.1.0 to 0.2.1,
    ``start`` is ``t`` less ``duration_s``, which is on the monotonic clock (it does not count
    sleep) and rounded to 0.01 s, so it can fall after the pass's true start.
    """

    line: Line
    start: datetime
    version: str
    runner: str
    exact_start: bool = False

    @property
    def t(self) -> datetime:
        return self.line.t

    @classmethod
    def of(cls, line: Line) -> Pass:
        version = str(line.record.get("capture_version") or "unknown")
        runner = str(line.record.get("runner") or "unknown")
        started = timestamp(line.record.get("started_at"))
        if started is not None and started <= line.t:
            return cls(line, started, version, runner, exact_start=True)
        duration = number(line.record.get("duration_s"))
        start = line.t - timedelta(seconds=duration) if duration and duration > 0 else line.t
        return cls(line, start, version, runner)


def _elapsed_ms(record: Mapping[str, Any]) -> float | None:
    """The record's ``elapsed_ms`` when it is a finite number >= 0 (not a bool), else None."""
    value = number(record.get("elapsed_ms"))
    return value if value is not None and value >= 0 else None


def send_time(line: Line) -> datetime:
    """When a request was sent, as capture's scheduler counted it: ``sent_at``, else ``t`` minus
    ``elapsed_ms``, else ``t``.

    Capture 0.2.1 writes ``sent_at``, the clock reading it also stamps the poll and the image
    GET with, so gaps read from it are the ones capture's scheduler kept, to the millisecond.
    Older records have only ``t``, written after the answer; ``t`` minus ``elapsed_ms`` is
    within about 1 ms of the send (measured: -1.0 to +0.9 ms in capture's 8 h schedule test).
    A record with neither is placed at ``t``, after its answer: close enough for the
    send-to-send columns and a restart's excess, never a bound at nps.gov
    (:func:`earliest_receipt`).
    """
    sent = timestamp(line.record.get("sent_at"))
    if sent is not None:
        return sent
    elapsed = _elapsed_ms(line.record)
    return line.t - timedelta(milliseconds=elapsed) if elapsed is not None else line.t


def earliest_receipt(line: Line) -> datetime | None:
    """The earliest nps.gov can have received a request (PRD §15.3), or None if the record
    cannot say.

    - ``sent_at`` (capture 0.2.1): a clock reading taken right before the request, truncated
      to the millisecond, so never later than the send.
    - Else ``t`` − ``elapsed_ms`` − 0.05 ms. Capture reads ``t`` right after the request's
      timer stops and truncates it, and rounds ``elapsed_ms`` to 0.1 ms. The moment between
      the two readings is not logged: a few statements in capture 0.1.0 to 0.2.1 (measured:
      median 0.9 us, at most 0.4 ms in 10^6 stand-alone runs of that code, 20 Nov). This
      also assumes the Mac did not sleep during the request: ``elapsed_ms`` is on the
      monotonic clock, which does not count sleep, so a request that spanned a sleep would
      be placed too late.
    - Else None: with no ``sent_at`` and no ``elapsed_ms`` (a number >= 0), only ``t`` is
      left, written after the answer, which would place the send up to the whole request
      (a 30 s timeout) too late.
    """
    sent = timestamp(line.record.get("sent_at"))
    if sent is not None:
        return sent
    elapsed = _elapsed_ms(line.record)
    if elapsed is None:
        return None
    return line.t - timedelta(milliseconds=elapsed) - ELAPSED_ROUNDING


def latest_receipt(line: Line) -> datetime | None:
    """The latest nps.gov can have received a request: no later than its answer (PRD §15.3),
    or None if the record cannot say.

    - ``answer_bound_at`` when the record carries one. Capture 0.2.1 adds it, with
      ``answer_bound: "failed_after_send"``, to a request that failed after it may have left
      this machine, with no response head back (a read timeout, a reset). nps.gov may still
      receive such a request after the failure, so capture counts it as answered then:
      ``FAILED_AFTER_SEND_BOUND_S`` (300 s) after the failure, rounded up to the millisecond,
      the stamp its scheduler waits from. The later of it and ``answered_at`` (the failure's
      own reading) is used, so a damaged record cannot make the bound earlier.
    - Else ``answered_at`` when capture logs it (0.2.1: one clock reading once the response
      was read in full or the request failed, rounded *up* to the millisecond).
    - Else ``t`` + 1 ms: capture reads ``t`` after the answer, but truncates it to the
      millisecond, so the reading behind it can be up to 1 ms later than the stamp.
    - None when the record names an ``answer_bound`` but has no readable ``answer_bound_at``:
      it says the request may have reached nps.gov after its stamps, without saying how late,
      so a pair or window that starts with it proves nothing.

    Records before 0.2.1 name no such bound, so a request of theirs that failed after its
    send is bounded at its failure, as before.
    """
    bound = timestamp(line.record.get("answer_bound_at"))
    answered = timestamp(line.record.get("answered_at"))
    if bound is not None:
        return bound if answered is None else max(bound, answered)
    if line.record.get("answer_bound"):
        return None
    return answered if answered is not None else line.t + STAMP_RESOLUTION


def _request(line: Line) -> Request:
    return Request(send_time(line), earliest_receipt(line), latest_receipt(line))


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


def _poll_request(line: Line, runners: frozenset[str]) -> bool:
    """A scheduled runner's poll: the JSON GET at an ARD camera or the HEAD at Rainier."""
    r = line.record
    return (
        r.get("event") == "request"
        and r.get("worker") == "capture"
        and r.get("runner") in runners
        and r.get("stage") in POLL_STAGES
        and isinstance(r.get("viewpoint_id"), str)
    )


@dataclass(slots=True)
class _Camera:
    polls: list[Request] = field(default_factory=list)
    gets: list[Request] = field(default_factory=list)
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
                self.polls.append(_request(line))
            elif stage == IMAGE_STAGE:
                self.gets.append(_request(line))
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


def restart_excess(
    split: LogKey,
    polls: Mapping[str, Sequence[tuple[LogKey, datetime]]],
    window: Window,
    options: ReportOptions,
    listed: frozenset[str] | None = None,
) -> tuple[dict[str, Any], list[Interval]]:
    """A capture restart's excess: what the gate rule loses for it, and the interval lost.

    The lead's rule for the 72 h clock (20 Nov; settles D-061): each camera's gap runs from
    its last poll before the restart to its first poll after, and the excess is the longest of
    those gaps less one ``capture_interval_s``, when positive. The interval lost is that
    camera's last poll + one interval to its next poll, clipped to the window.

    A poll is before the restart when its record is at or before ``split`` in the logs (the
    old loop's last record, :func:`stopped_after`, by :attr:`.common.Line.key`). A record's
    ``t`` follows its answer, so the new loop's first poll comes after the split even when it
    was sent within milliseconds of the new pass's start.

    - A camera with no poll before the restart in the logs read is left out.
    - A camera with no poll after it in the logs read is listed in ``not_polled_since`` with
      its gap so far, from its last poll to the window's end (D-078's flag). What it counts
      depends on ``listed``, the cameras of the latest capture loop in the logs read
      (:func:`_latest_loop`), the loop that would poll it next (``in_latest_loop``):

      - **listed** (``left_out`` False): not yet due (after an ordinary restart, every camera
        until the new loop's first polls) or held back by a back-off. Its gap so far counts,
        to the window's end (``ongoing``), as before the lead's ruling, so the excess never
        shrinks as the window runs on: the clock cannot show met before the next poll and
        lose it after.
      - **not listed** (dropped from the loop), or ``listed`` None (no record names the
        loop's cameras): left out of the excess (``left_out``; the lead's ruling of 20 Nov,
        D-078). Measured to the window's end it would grow with the window, so one dropped
        camera could keep the 72 h clock from ever closing.

      Once the logs hold its next poll it counts like any other camera, its whole gap
      included (D-069 as written): a back-off set before the restart, and the time a camera
      dropped at it was out, from the moment a later loop lists it again.

    ``polls`` are each camera's polls as ``(key, send)``, sorted, from every log read (the
    last poll before a restart can be before ``since``, and the first after it after
    ``until``). Gaps are send to send (:func:`send_time`), as the ``longest`` poll gap is.
    """
    interval = timedelta(seconds=options.capture_interval_s)
    gaps: dict[str, tuple[datetime, datetime]] = {}
    unpolled: dict[str, datetime] = {}
    for vp, rows in polls.items():
        index = bisect.bisect_right(rows, split, key=lambda row: row[0])
        if index == 0:
            continue  # no poll before the restart in the logs read
        last = rows[index - 1][1]
        if index < len(rows):
            gaps[vp] = (last, rows[index][1])
            continue
        unpolled[vp] = last
        if listed is not None and vp in listed:  # the latest loop polls it: its gap so far counts
            gaps[vp] = (last, max(window.until, last))
    fields: dict[str, Any] = {
        "poll_gaps_s": {
            vp: round((end - last).total_seconds(), 3) for vp, (last, end) in sorted(gaps.items())
        },
        "not_polled_since": {
            vp: {
                "last_poll_utc": iso(last),
                "gap_to_window_end_s": round(max(0.0, (window.until - last).total_seconds()), 3),
                "in_latest_loop": None if listed is None else vp in listed,
                "left_out": listed is None or vp not in listed,
            }
            for vp, last in sorted(unpolled.items())
        },
        "longest_poll_gap": None,
        "excess_s": 0.0,
        "lost_s": 0.0,
    }
    if not gaps:
        return fields, []
    vp, (last, end) = max(gaps.items(), key=lambda item: item[1][1] - item[1][0])
    gap = (end - last).total_seconds()
    lost = clip([(last + interval, end)], window)
    fields.update(
        longest_poll_gap={
            "viewpoint_id": vp,
            "s": round(gap, 3),
            "from_utc": iso(last),
            "to_utc": iso(end),
            "ongoing": end >= window.until,
        },
        excess_s=round(max(0.0, gap - options.capture_interval_s), 3),
        lost_s=round(measure(lost), 3),
    )
    return fields, lost


def _version(line: Line) -> str:
    return str(line.record.get("capture_version") or "unknown")


def _runner(line: Line) -> str:
    return str(line.record.get("runner") or "unknown")


#: How :func:`stopped_after` found the old loop's last record (a restart row's ``split_by``).
SPLIT_LOOP_STARTED = "loop_started"
SPLIT_LOOP_STOPPED = "loop_stopped"
SPLIT_OLD_LOOP = "old_loop_last_record"


def stopped_after(
    a: Pass,
    b: Pass,
    scheduled: Sequence[Line],
    stops: Sequence[Line],
    starts: Sequence[Line] = (),
) -> tuple[Line, str]:
    """The old loop's last record before the restart between passes ``a`` and ``b``, and how
    it was found.

    It splits each camera's polls into before and after (:func:`restart_excess`):

    1. ``loop_started``: when a ``loop_started`` record lies between the two pass records,
       the last scheduled record before the first of them. A loop writes it as it starts,
       holding the NPS fetcher lock, so the old loop has exited and everything it wrote was
       written before it. That makes the split exact while the wall clock runs forward
       across the restart. It is not exact when the clock steps back across it: records are
       ordered by ``(t, source, line)`` (:attr:`.common.Line.key`), not by when they were
       written, so a ``loop_started`` stamped before the old loop's last records sorts
       before them. A loop that started and died before its first pass record (a second
       ``loop_started`` before ``b``) is on the new side.
    2. ``loop_stopped``: else the last ``loop_stopped`` record from ``a`` to ``b``'s start (a
       loop writes one as it exits: capture 0.2.x when its pre-pass check fails, 0.3.0 on
       every clean stop).
    3. ``old_loop_last_record``: else the last record of ``a``'s release and runner before
       ``b``'s start, else ``a`` itself. ``b``'s start is exact when its record carries
       ``started_at`` (:class:`Pass`): the new loop sends nothing before it. For every record
       of capture 0.1.0 to 0.2.1 it is ``t`` less ``duration_s``, on the monotonic clock, so
       when the new loop's first pass spanned a sleep its computed start falls after the polls
       it made before the sleep: a change of version or runner still tells the two loops'
       records apart, and with neither those polls count as the old loop's.
    """
    new = [s for s in starts if a.line.key < s.key < b.line.key]
    if new:
        first = min(new, key=lambda s: s.key)
        before = [line for line in scheduled if a.line.key <= line.key < first.key]
        return max(before, key=lambda line: line.key, default=a.line), SPLIT_LOOP_STARTED
    stopped = [s for s in stops if a.t <= s.t <= b.start]
    if stopped:
        return max(stopped, key=lambda s: s.key), SPLIT_LOOP_STOPPED
    old = [
        line
        for line in scheduled
        if a.t <= line.t < b.start
        and capture_release(_version(line)) == capture_release(a.version)
        and _runner(line) == a.runner
    ]
    return max(old, key=lambda line: line.key, default=a.line), SPLIT_OLD_LOOP


def _loop_started_evidence(line: Line) -> str:
    pid = line.record.get("pid")
    known = isinstance(pid, int) and not isinstance(pid, bool)
    return f"loop_started at {iso(line.t)}" + (f" (pid {pid})" if known else "")


def _loop_stopped_evidence(line: Line) -> str:
    """``loop_stopped at <t>: <why>``: its ``error`` (every loop_stopped record of capture 0.2.x
    carries one), else its ``reason`` and ``signal`` (0.3.0 on, which logs every clean stop)."""
    record = line.record
    why = record.get("error")
    if not why:
        why = " ".join(str(v) for v in (record.get("reason"), record.get("signal")) if v)
    return f"loop_stopped at {iso(line.t)}: {why or None}"


def _camera_list(line: Line) -> frozenset[str] | None:
    """The cameras a record says its loop polls, or None for a record that names none.

    A ``loop_started`` record's ``viewpoints`` (capture 0.3.0 on), or the cameras a ``pass``
    record names in ``outcomes``: every capture release so far logs one outcome per camera of
    the pass, one that was not due or was skipped (``stopping``, ``time_left``) included.
    """
    event = line.record.get("event")
    if event == "loop_started":
        cameras = line.record.get("viewpoints")
        if isinstance(cameras, list) and all(isinstance(vp, str) for vp in cameras):
            return frozenset(cameras)
    elif event == "pass":
        outcomes = line.record.get("outcomes")
        if isinstance(outcomes, Mapping):
            return frozenset(str(vp) for vp in outcomes)
    return None


def _latest_loop(scheduled: Sequence[Line]) -> tuple[frozenset[str] | None, Line | None]:
    """The cameras of the latest capture loop in the logs read, and the record that lists them.

    That is the last scheduled record, by place in the logs, that lists its loop's cameras
    (:func:`_camera_list`): the running loop's latest pass record, or the ``loop_started``
    record of a loop that has written no pass yet. It is the loop that would poll a camera
    next, so it decides whether a camera not polled since a restart is waiting for its next
    poll or was dropped (:func:`restart_excess`), whichever loop's restart it is: a camera an
    earlier loop listed but the latest one dropped is left out, so it cannot hold the clock
    open through the earlier restart. ``(None, None)`` when no record lists cameras.
    """
    for line in reversed(scheduled):
        cameras = _camera_list(line)
        if cameras is not None:
            return cameras, line
    return None, None


def _restarts(
    passes: Sequence[Pass],
    scheduled: Sequence[Line],
    window: Window,
    options: ReportOptions,
    polls: Mapping[str, Sequence[tuple[LogKey, datetime]]],
    listed: frozenset[str] | None = None,
) -> tuple[list[dict[str, Any]], list[Interval]]:
    """Loop stops and starts seen in the log (package docstring), and what their excess lost.

    A restart is listed when its new loop's first pass starts inside the window, when its
    excess (:func:`restart_excess`) loses time inside it (a restart just before ``since``), or
    when a camera has not been polled since it. ``scheduled`` are the scheduled runners'
    capture records of every log read, so a ``loop_stopped`` record or the old loop's last
    record can be before ``since``. ``listed`` are the latest loop's cameras
    (:func:`_latest_loop`).
    """
    stops = [line for line in scheduled if line.record.get("event") == "loop_stopped"]
    starts = [line for line in scheduled if line.record.get("event") == "loop_started"]
    shortest = options.capture_interval_s - options.restart_tolerance_s
    rows: list[dict[str, Any]] = []
    lost: list[Interval] = []
    for a, b in pairwise(passes):
        if b.start >= window.until:
            continue
        evidence = []
        if capture_release(a.version) != capture_release(b.version):
            evidence.append(f"capture_version {a.version} -> {b.version}")
        if a.runner != b.runner:
            evidence.append(f"runner {a.runner} -> {b.runner}")
        spacing = (b.start - a.start).total_seconds()
        if spacing < shortest:
            evidence.append(
                f"pass starts {spacing:.1f} s apart, under the "
                f"{options.capture_interval_s:g} s interval of one loop"
            )
        evidence.extend(_loop_stopped_evidence(s) for s in stops if a.t <= s.t <= b.start)
        new_loops = [s for s in starts if a.line.key < s.key < b.line.key]
        evidence.extend(_loop_started_evidence(s) for s in new_loops)
        if not evidence:
            continue
        split, split_by = stopped_after(a, b, scheduled, stops, starts)
        excess, excess_lost = restart_excess(split.key, polls, window, options, listed)
        if b.start not in window and not excess_lost and not excess["not_polled_since"]:
            continue  # before the window, and its excess loses nothing inside it
        lost.extend(excess_lost)
        loop_started = (
            None
            if not new_loops
            else timestamp(new_loops[0].record.get("started_at")) or new_loops[0].t
        )
        rows.append(
            {
                "job": "capture",
                "kind": "restart",
                "stopped_after_utc": iso(split.t),
                "split_by": split_by,
                "loop_started_utc": iso(loop_started),
                "started_utc": iso(b.start),
                "started_from": "started_at" if b.exact_start else "t - duration_s",
                "duration_s": round((b.start - split.t).total_seconds(), 3),
                "from_version": a.version,
                "to_version": b.version,
                "last_pass_utc": iso(a.t),
                "first_pass_utc": iso(b.t),
                "pass_gap_s": round((b.t - a.t).total_seconds(), 3),
                "an_outage": (b.t - a.t).total_seconds() > options.capture_gap_s,
                "evidence": evidence,
                **excess,
            }
        )
    return rows, lost


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


def _at_nps(requests: Sequence[Request], count: int, limit_s: float) -> dict[str, Any]:
    """The least spans of ``count`` consecutive requests at nps.gov, and how many are unproven.

    ``measured`` runs (:func:`.common.provable_spans`), the ``shortest`` least span (rounded
    down to the ms, with the two bounds it runs between), and ``unproven``: the runs not
    provably at least ``limit_s`` apart at nps.gov. They are the ``under_limit`` runs, whose
    least possible span is under ``limit_s`` (exactly ``limit_s`` is not under), and the
    ``unbounded`` ones, whose last request's record cannot place its send or first request's
    record cannot bound its answer. An unproven run is not shown to have kept the rule; it is
    not shown to have broken it either.
    """
    spans = provable_spans(requests, count)
    bounded = [(seconds, first, last) for seconds, first, last in spans if seconds is not None]
    shortest = min(bounded, key=lambda span: span[0], default=None)
    under = sum(1 for seconds, _, _ in bounded if seconds < limit_s)
    unbounded = len(spans) - len(bounded)
    return {
        "measured": len(spans),
        "shortest": (
            None
            if shortest is None
            else {
                "s": floor_ms(shortest[0]),
                "first_answer_bound_utc": iso(shortest[1].latest),
                "last_send_bound_utc": iso(shortest[2].earliest),
            }
        ),
        "unproven": under + unbounded,
        "under_limit": under,
        "unbounded": unbounded,
        "limit_s": limit_s,
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
    polls = gap_stats(request.send for request in cam.polls)
    most, most_from = max_in_window(
        (request.send for request in cam.gets), options.rolling_window_s
    )
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
        # PRD §15.3 as nps.gov saw it: "measured" consecutive pairs (5-GET windows), and the
        # "unproven" ones, not provably at least the rule's limit apart (module docstring).
        "poll_gap_at_nps": _at_nps(cam.polls, 2, options.min_poll_gap_s),
        "image_gets_at_nps": _at_nps(
            cam.gets, options.max_gets_per_rolling_hour + 1, options.rolling_window_s
        ),
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
) -> tuple[dict[str, Any], list[Interval], list[Interval]]:
    """The capture part of the report, and what its outages and its restarts' excess lost.

    Returns ``(section, outage_lost, restart_lost)``: the intervals the clock counts
    (:mod:`.report`). ``now`` is when the report is made: the state files and the fetcher
    lock are read as they are then.
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
    # Every log read, not only the window: a restart's last poll before it can predate since.
    scheduled = [
        line
        for line in logs.lines
        if line.record.get("worker") == "capture" and line.record.get("runner") in runners
    ]
    polls: dict[str, list[tuple[LogKey, datetime]]] = {}
    for line in scheduled:
        if _poll_request(line, runners):
            polls.setdefault(str(line.record["viewpoint_id"]), []).append(
                (line.key, send_time(line))
            )
    for rows in polls.values():
        rows.sort()
    listed, listed_by = _latest_loop(scheduled)
    restarts, restart_lost = _restarts(passes, scheduled, window, options, polls, listed)
    # D-078: a camera not polled since a restart is flagged on its row (the first such
    # restart, oldest first), and gets a row even with no record in the window.
    unpolled: dict[str, dict[str, Any]] = {}
    for row in restarts:
        for vp, gap in row["not_polled_since"].items():
            unpolled.setdefault(vp, {"restart_started_utc": row["started_utc"], **gap})
            cams.setdefault(vp, _Camera())
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
            vp: {
                **_camera_row(reader, vp, cams[vp], window, options, now, later),
                "not_polled_since_restart": unpolled.get(vp),
            }
            for vp in sorted(cams)
        },
        "outages": outages,
        "restarts": restarts,
        # The latest loop in the logs read, which decides whether a camera not polled since a
        # restart counts to the window's end or is left out (restart_excess).
        "latest_loop": {
            "cameras": sorted(listed) if listed is not None else None,
            "listed_by": None if listed_by is None else listed_by.record.get("event"),
            "record_utc": None if listed_by is None else iso(listed_by.t),
            "capture_version": None if listed_by is None else _version(listed_by),
        },
        "current_fetcher_lock": (
            {key: lock.get(key) for key in ("pid", "command", "started_at")}
            if isinstance(lock, dict)
            else None
        ),
    }
    return section, lost, restart_lost
