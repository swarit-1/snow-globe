"""The data report's assembly and the 72 h clock (PRD §16 M0.5; PLAN.md M0.5 gate).

**The clock, as PLAN.md states it:** it starts when the loop took over all 8 cameras, and the
capture loop's outages extend it, as outages extend the sealed window (PLAN.md's 8.2 h for
19 Nov is this reading). So covered = elapsed − the capture outages' lost time
(:mod:`.capture`), and the gate closes when covered reaches 72 h; until then the earliest close
is the moment covered would reach 72 h if nothing more is lost.

**Both jobs, proposed beside it:** PRD §16 asks for "≥ 72 h of worker and capture logs", which
can be read to count the forecast stand-in's outages (:mod:`.forecast`) too: lost = the union
of both jobs' lost time, an overlap counted once. Neither PLAN.md nor DECISIONS.md records
that rule yet, so the report gives it as a proposal, with each job's own figure, and the lead
chooses.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lookout_common.jsonl import REASON_UNTERMINATED
from lookout_common.timeutil import utcnow

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

SCHEMA_VERSION = 1

#: The clock rule PLAN.md states, and the one proposed beside it (module docstring).
CLOCK_RULE = (
    "PLAN.md M0.5 gate: the clock runs from the loop's start and the capture loop's outages "
    "extend it (PLAN.md's 8.2 h for 19 Nov is this rule): covered = elapsed - the capture "
    "outages' lost time; the gate needs {hours:g} h covered"
)
BOTH_JOBS_RULE = (
    "proposed, not recorded in PLAN.md or DECISIONS.md: PRD §16 asks for 72 h of worker and "
    "capture logs, so lost = the union of the capture and forecast outages' lost time (an "
    "overlap counts once)"
)


def clock(
    window: Window,
    capture: Sequence[Interval],
    forecast: Sequence[Interval],
    options: ReportOptions,
) -> dict[str, Any]:
    """The 72 h clock as PLAN.md states it, the both-jobs figure proposed, and each job's own."""
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
        "rule": CLOCK_RULE.format(hours=options.clock_hours),
        "start_utc": iso(window.since),
        "until_utc": iso(window.until),
        "required_h": options.clock_hours,
        "elapsed_h": hours(window.seconds),
        **figure(capture),
        "both_jobs_proposed": {"rule": BOTH_JOBS_RULE, **figure([*capture, *forecast])},
        "by_job": {"capture": figure(capture), "forecast": figure(forecast)},
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
    capture, capture_lost = capture_section(
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
            f"capture outage: its lost time extends the {opts.clock_hours:g} h clock",
            None,
            start=row["start_utc"],
            end=row["end_utc"],
            lost_s=row["lost_s"],
        )
    for row in forecast["outages"]:
        exclusions.add(
            "outages",
            "forecast outage: its lost time extends the both-jobs clock (proposed)",
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
            "rolling_window_s": opts.rolling_window_s,
            "max_gets_per_rolling_hour": opts.max_gets_per_rolling_hour,
            "clock_hours": opts.clock_hours,
        },
        "inputs": {
            "capture_log": capture_logs.files_read,
            "forecast_log": forecast_logs.files_read,
            "lag_survey": survey_logs.files_read,
            "unreadable_count": len(reader.unreadable),
            "unreadable": reader.unreadable[:REF_LIMIT],
        },
        "clock": clock(window, capture_lost, forecast_lost, opts),
        "capture": capture,
        "forecast": forecast,
        "exclusions": exclusions.to_list(),
    }
    result: dict[str, Any] = without_home(report)
    return result
