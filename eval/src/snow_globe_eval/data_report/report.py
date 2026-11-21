"""The data report's assembly and the 72 h clock (PRD §16 M0.5; PLAN.md M0.5 gate).

**The clock** starts when the loop took over all 8 cameras, and lost time extends it, as
outages extend the sealed window. Covered = elapsed − lost, and the gate closes when covered
reaches 72 h; until then the earliest close is the moment covered would reach 72 h if nothing
more is lost.

**The gate rule** (the headline): the lead's decision of 20 Nov, which settles D-061, takes the
stricter reading of PRD §16's "≥ 72 h of worker and capture logs". Lost = the union of the
capture loop's outages, the forecast stand-in's outages (:mod:`.forecast`) and each capture
restart's excess (:func:`.capture.restart_excess`: the longest any camera went from its last
poll before the restart to its first after, less one 300 s interval), an overlap counted once.

**Beside it, labelled:** PLAN.md's capture-only figure (the capture loop's outages alone, the
reading behind PLAN.md's 8.2 h for 19 Nov, which counts no restart that is not an outage), and
each job's own: capture (outages and restart excess) and forecast (outages).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from snow_globe_common.jsonl import REASON_UNTERMINATED
from snow_globe_common.timeutil import utcnow

from .capture import capture_section
from .common import (
    DEFAULT_SINCE,
    REF_LIMIT,
    REPORT_KIND,
    Exclusions,
    Interval,
    Logs,
    Reader,
    ReportOptions,
    Window,
    clip,
    close_time,
    hours,
    iso,
    measure,
    merge,
    read_logs,
    without_home,
)
from .forecast import forecast_section

__all__ = ["SCHEMA_VERSION", "build_report", "clock"]

#: 2: the clock's headline is the gate rule (it was PLAN.md's capture-only figure, now
#: ``plan_md_capture_only``); restarts carry their excess; per camera, the bounds at nps.gov.
SCHEMA_VERSION = 2

#: The clock's rules (module docstring).
GATE_RULE = (
    "gate rule (the lead's decision of 20 Nov, settling D-061; PRD §16's worker and capture "
    "logs): lost = the union of the capture loop's outages, the forecast stand-in's outages "
    "and each capture restart's excess (the longest any camera went from its last poll before "
    "the restart to its first after, less one {interval:g} s interval), an overlap counted "
    "once; covered = elapsed - lost; the gate needs {hours:g} h covered"
)
PLAN_RULE = (
    "PLAN.md's capture-only figure, for comparison: lost = the capture loop's outages alone "
    "(gaps over {gap:g} s between passes, less one {interval:g} s interval; PLAN.md's 8.2 h for "
    "19 Nov is this reading); a restart that is not an outage loses nothing"
)
CAPTURE_RULE = "capture alone: its outages and its restarts' excess (the gate rule's capture part)"
FORECAST_RULE = "forecast alone: the stand-in's outages (the gate rule's forecast part)"
#: The time basis of each per-camera figure (:mod:`.capture`), printed in the report's rules.
BASES = {
    "send": (
        "sent_at when logged (capture 0.2.1), else t - elapsed_ms, else t: the send as "
        "capture's scheduler counted it"
    ),
    "earliest_receipt": (
        "the earliest nps.gov can have received a request: sent_at (truncated to the ms, read "
        "before the request), else t - elapsed_ms - 0.05 ms (elapsed_ms is rounded to 0.1 ms; "
        "this assumes t was read right after the request's timer stopped, as capture does, "
        "and that the Mac did not sleep mid-request, since elapsed_ms is on the monotonic "
        "clock); a record with neither has none"
    ),
    "latest_receipt": (
        "the latest nps.gov can have received a request, its answer: answered_at (rounded up "
        "to the ms), else t + 1 ms (t is written after the answer, truncated to the ms)"
    ),
    "poll_gap_s": "send to send: how capture's scheduler spaced the polls",
    "max_image_gets_in_rolling_hour": "send times, half-open rolling windows",
    "poll_gap_at_nps": (
        "earliest receipt of the next poll - latest receipt of the previous, over consecutive "
        "polls: the least gap nps.gov can have seen; unproven counts the pairs not provably "
        "min_poll_gap_s apart (under_limit: a least gap under it; unbounded: the next poll's "
        "send cannot be placed). Unproven is not shown closer"
    ),
    "image_gets_at_nps": (
        "earliest receipt of the 5th - latest receipt of the 1st, over every 5 consecutive "
        "image GETs: the least span nps.gov can have seen; unproven counts the windows not "
        "provably rolling_window_s apart (under_limit, unbounded as for polls)"
    ),
    "restart_excess": (
        "the longest any camera went from its last poll before the restart to its first after, "
        "send to send, less one capture_interval_s; a poll is before the restart when its "
        "record was written by the old loop's last record (the row's stopped_after_utc)"
    ),
}


def clock(
    window: Window,
    capture: Sequence[Interval],
    restarts: Sequence[Interval],
    forecast: Sequence[Interval],
    options: ReportOptions,
) -> dict[str, Any]:
    """The 72 h clock by the gate rule, with PLAN.md's figure and each job's own beside it.

    ``capture`` and ``forecast`` are the intervals the two jobs' outages lost, ``restarts``
    those the capture restarts' excess lost (module docstring).
    """
    required = options.clock_hours * 3600.0

    def figure(intervals: Sequence[Interval]) -> dict[str, Any]:
        lost_parts = merge(clip(intervals, window))
        lost = measure(lost_parts)
        covered = window.seconds - lost
        close = close_time(window.since, lost_parts, required)
        met = close <= window.until
        return {
            "lost_h": hours(lost),
            "covered_h": hours(covered),
            "remaining_h": hours(max(0.0, required - covered)),
            "met": met,
            "met_at_utc": iso(close) if met else None,
            "earliest_close_utc": None if met else iso(close),
        }

    return {
        "rule": GATE_RULE.format(hours=options.clock_hours, interval=options.capture_interval_s),
        "start_utc": iso(window.since),
        "until_utc": iso(window.until),
        "required_h": options.clock_hours,
        "elapsed_h": hours(window.seconds),
        **figure([*capture, *restarts, *forecast]),
        "plan_md_capture_only": {
            "rule": PLAN_RULE.format(
                gap=options.capture_gap_s, interval=options.capture_interval_s
            ),
            **figure(capture),
        },
        "by_job": {
            "capture": {"rule": CAPTURE_RULE, **figure([*capture, *restarts])},
            "forecast": {"rule": FORECAST_RULE, **figure(forecast)},
        },
        # Each part on its own, inside the window; the union above counts an overlap once.
        "parts_lost_h": {
            "capture_outages": hours(measure(clip(capture, window))),
            "capture_restart_excess": hours(measure(clip(restarts, window))),
            "forecast_outages": hours(measure(clip(forecast, window))),
        },
    }


def _note_unread(logs: Logs, name: str, exclusions: Exclusions) -> None:
    for bad in logs.bad:
        reason = (
            "unterminated last line (a write in progress)"
            if bad["reason"] == REASON_UNTERMINATED
            else f"damaged log line ({bad['reason']})"
        )
        exclusions.add(name, reason, None, ref=f"{bad['source']}:{bad['line']}")
    for key in logs.files_skipped:
        exclusions.add(name, "log file outside the window (not read)", None, key=key)


def build_report(
    root: Path,
    *,
    since: datetime = DEFAULT_SINCE,
    until: datetime | None = None,
    viewpoint_ids: Iterable[str] | None = None,
    options: ReportOptions | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The M0.5 gate's log evidence for ``[since, until)`` from the data dir ``root``.

    Read-only (package docstring). ``viewpoint_ids`` are viewpoints that must appear even
    with no record (the manifests' ids, so a silent camera shows as zeros); viewpoints found
    in the data are always added. ``now`` is when the report is made (default: the clock):
    ``until`` defaults to it, and the state files are read as they are then. Every string in
    the result shows the home directory as ``~``.
    """
    opts = options or ReportOptions()
    made = now if now is not None else utcnow()
    end = until if until is not None else made
    if since.tzinfo is None or end.tzinfo is None or made.tzinfo is None:
        raise ValueError("since, until and now must be timezone-aware")
    window = Window(since.astimezone(UTC), end.astimezone(UTC))
    if window.until <= window.since:
        raise ValueError(f"until ({iso(window.until)}) must be after since ({iso(window.since)})")
    reader = Reader(root)
    exclusions = Exclusions()
    capture_logs = read_logs(reader, "capture_log", window)
    forecast_logs = read_logs(reader, "forecast_log", window)
    survey_logs = read_logs(reader, "forecast_log/lag_survey", window)
    ids = set(viewpoint_ids or ())
    ids.update(
        str(line.record["viewpoint_id"])
        for line in capture_logs.lines
        if line.t in window
        and line.record.get("worker") == "capture"
        and isinstance(line.record.get("viewpoint_id"), str)
    )
    ids.update(reader.folders("frames"), reader.folders("thumbs"))
    ordered = sorted(ids)
    capture, capture_lost, restart_lost = capture_section(
        reader, capture_logs, window, ordered, opts, exclusions, now=made.astimezone(UTC)
    )
    forecast, forecast_lost = forecast_section(
        reader, forecast_logs, survey_logs, window, ordered, opts, exclusions
    )
    for logs, name in (
        (capture_logs, "capture_log"),
        (forecast_logs, "forecast_log"),
        (survey_logs, "forecast_log/lag_survey"),
    ):
        _note_unread(logs, name, exclusions)
    for row in capture["outages"]:
        exclusions.add(
            "outages",
            f"capture outage: its lost time extends the {opts.clock_hours:g} h clock "
            "(the gate rule and PLAN.md's figure)",
            None,
            start=row["start_utc"],
            end=row["end_utc"],
            lost_s=row["lost_s"],
        )
    for row in capture["restarts"]:
        gap = row["longest_poll_gap"]
        if gap is not None and row["lost_s"] > 0:
            exclusions.add(
                "outages",
                f"capture restart excess: its lost time extends the {opts.clock_hours:g} h "
                "clock (the gate rule)",
                None,
                start=gap["from_utc"],
                end=gap["to_utc"],
                lost_s=row["lost_s"],
                viewpoint_id=gap["viewpoint_id"],
            )
    for row in forecast["outages"]:
        exclusions.add(
            "outages",
            f"forecast outage: its lost time extends the {opts.clock_hours:g} h clock "
            "(the gate rule)",
            None,
            start=row["start_utc"],
            end=row["end_utc"],
            lost_s=row["lost_s"],
        )
    report = {
        "kind": REPORT_KIND,
        "schema_version": SCHEMA_VERSION,
        "generated_utc": iso(made),
        "data_dir": str(root),
        "window": {
            "since_utc": iso(window.since),
            "until_utc": iso(window.until),
            "hours": hours(window.seconds),
        },
        "rules": {
            "scheduled_runners": sorted(opts.scheduled_runners),
            "capture_outage_gap_s": opts.capture_gap_s,
            "capture_interval_s": opts.capture_interval_s,
            "restart_pass_spacing_under_s": opts.capture_interval_s - opts.restart_tolerance_s,
            "forecast_outage_delay_s": opts.forecast_gap_s,
            "forecast_tick_s": opts.forecast_tick_s,
            "forecast_fallback_lag_s": opts.forecast_fallback_lag_s,
            "min_poll_gap_s": opts.min_poll_gap_s,
            "rolling_window_s": opts.rolling_window_s,
            "max_gets_per_rolling_hour": opts.max_gets_per_rolling_hour,
            "clock_hours": opts.clock_hours,
            "bases": BASES,
        },
        "inputs": {
            "capture_log": capture_logs.files_read,
            "forecast_log": forecast_logs.files_read,
            "lag_survey": survey_logs.files_read,
            "unreadable_count": len(reader.unreadable),
            "unreadable": reader.unreadable[:REF_LIMIT],
        },
        "clock": clock(window, capture_lost, restart_lost, forecast_lost, opts),
        "capture": capture,
        "forecast": forecast,
        "exclusions": exclusions.to_list(),
    }
    result: dict[str, Any] = without_home(report)
    return result
