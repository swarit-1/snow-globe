"""The data report as plain-text tables (PRD §16 M0.5); the JSON report has every field."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["format_report"]


def _short(stamp: str | None) -> str:
    """``2026-11-19T12:56:00.123Z`` as ``11-19 12:56:00``."""
    return "-" if not stamp else f"{stamp[5:10]} {stamp[11:19]}"


def _minutes(seconds: float | None) -> str:
    return "-" if seconds is None else f"{seconds / 60:.1f} min"


#: Short names for the table: capture 0.1.0's keep-everything, and D-028's two policies.
_POLICY_SHORT = {"legacy-every": "legacy", "every": "every", "hourly-nearest": "hourly"}


def _policies(by_policy: Sequence[Mapping[str, Any]]) -> str:
    """Downloads per policy, e.g. ``legacy 25, hourly 1``."""
    totals: Counter[str] = Counter()
    for acc in by_policy:
        totals[_POLICY_SHORT.get(str(acc["policy"]), str(acc["policy"]))] += int(acc["downloads"])
    return ", ".join(f"{name} {n}" for name, n in totals.items()) or "-"


def _status(figure: Mapping[str, Any]) -> str:
    if figure["met"]:
        return f"met at {figure['met_at_utc']}"
    return f"earliest close {figure['earliest_close_utc']} if nothing more is lost"


def _clock_lines(report: Mapping[str, Any]) -> list[str]:
    """PLAN.md's clock (capture outages) first, then the both-jobs proposal (report.py)."""
    clock = report["clock"]
    both, forecast = clock["both_jobs_proposed"], clock["by_job"]["forecast"]
    return [
        f"{clock['required_h']:g} h clock (PLAN.md M0.5 gate: the capture loop's outages extend "
        f"it): {clock['covered_h']:.2f} h covered of {clock['elapsed_h']:.2f} h elapsed; "
        f"{clock['lost_h']:.2f} h lost; {_status(clock)}",
        f"  both jobs (proposed, not a recorded rule; PRD §16 asks for worker and capture logs): "
        f"{both['covered_h']:.2f} h covered; {both['lost_h']:.2f} h lost (the union of capture "
        f"{clock['lost_h']:.2f} h and forecast {forecast['lost_h']:.2f} h); {_status(both)}",
        f"  forecast alone: {forecast['covered_h']:.2f} h covered; {_status(forecast)}",
    ]


def _capture_lines(capture: Mapping[str, Any]) -> list[str]:
    passes = capture["passes"]
    versions = ", ".join(f"{k} {v}" for k, v in passes["capture_versions"].items()) or "-"
    lines = [
        f"Capture: {passes['count']} passes by {', '.join(passes['runners']) or '-'} ({versions});"
        f" median spacing {passes['spacing_s']['median']} s",
        f"{'viewpoint':<28}{'polls':>6}{'GETs':>6}{'full':>6}{'thumb':>6}  "
        f"{'downloads by policy':<24}{'capped':>7}{'errors':>7}{'longest gap':>13}"
        f"{'min send gap':>14}{'GETs/h':>7}",
    ]
    notes, later = [], []
    for vp, row in capture["viewpoints"].items():
        gaps = row["poll_gap_s"]
        longest = gaps["longest"]["s"] if gaps["longest"] else None
        shortest = gaps["shortest_on_send_times"]
        min_gap = "-" if shortest is None else f"{shortest['s']:.1f} s"
        frames = row["frames"]
        lines.append(
            f"{vp:<28}{row['polls']:>6}{row['image_gets']:>6}{frames['full_size']:>6}"
            f"{frames['thumbnails']:>6}  {_policies(frames['by_policy']):<24}"
            f"{row['rate_capped']['total']:>7}{row['errors']['error_records']:>7}"
            f"{_minutes(longest):>13}{min_gap:>14}{row['max_image_gets_in_rolling_hour']['count']:>7}"
        )
        for kind, check in frames["disk"].items():
            if check["logged_missing"] or check["on_disk_not_logged"]:
                notes.append(
                    f"  {vp} {kind}: {check['logged_missing']} logged but not on disk, "
                    f"{check['on_disk_not_logged']} on disk (fetched in the window) but not logged"
                )
            if check["on_disk_logged_after_window"]:
                later.append(
                    f"  {vp} {kind}: {check['on_disk_logged_after_window']} fetched in the window "
                    "and logged only after it (an hourly frame promoted later)"
                )
    lines.extend(notes or ["  disk: every logged frame and thumbnail is on disk, none unlogged"])
    return lines + later


def _forecast_lines(report: Mapping[str, Any]) -> list[str]:
    forecast = report["forecast"]
    lines = [
        "Forecast: live HRRR runs first written in the window, per viewpoint (a later write of "
        "a run is a re-extraction, never a run; runs include the catch-ups, due before the window)",
        f"{'viewpoint':<28}{'runs':>5}{'catch-up':>9}  {'first':<11}{'last':<11}{'hours':<8}"
        f"{'fields on disk (runs)':<24}{'re-extr':>8}{'rewritten':>10}{'never':>6}",
    ]
    for vp, row in forecast["viewpoints"].items():
        hours = "; ".join(row["hours"]) or "-"
        fields = "; ".join(f"{len(s['fields'])} ({s['runs']})" for s in row["field_sets_on_disk"])
        lines.append(
            f"{vp:<28}{row['runs']:>5}{len(row['catch_ups']):>9}  "
            f"{row['first_run'] or '-':<11}{row['last_run'] or '-':<11}"
            f"{hours:<8}{fields or '-':<24}{row['re_extractions']:>8}"
            f"{len(row['files_rewritten_by_later_writes']):>10}{len(row['cycles_never_written']):>6}"
        )
    quiet = len(forecast["quiet_gaps_over_threshold"])
    unwritten = forecast["lag_for_cycles_never_written"]
    lines.append(
        f"  {forecast['lags_distinct_in_window']} distinct live lags; {forecast['error_count']} "
        f"errors; median fetchable lag {forecast['median_fetchable_lag_s']} s; {quiet} quiet gaps "
        f"over {report['rules']['forecast_outage_delay_s']:g} s between stand-in records (normal "
        "between hourly runs, not outages)"
    )
    if forecast["median_fetchable_lag_s"] is None:
        lines.append(
            f"  cycles never written are dated by {unwritten['s']} s ({unwritten['source']})"
        )
    return lines


def _outage_lines(report: Mapping[str, Any]) -> list[str]:
    rows = [*report["capture"]["outages"], *report["forecast"]["outages"]]
    lines = [
        "Outages (lost = the part that extends the clock)",
        f"{'job':<10}{'start':<16}{'end':<16}{'gap':>12}{'lost':>12}  note",
    ]
    for row in rows:
        note = row["kind"] + (" (ongoing)" if row.get("ongoing") else "")
        if row.get("runs"):
            note += f": runs {', '.join(row['runs'])}"
            note += " (estimated)" if row.get("estimated") else ""
        lines.append(
            f"{row['job']:<10}{_short(row['start_utc']):<16}{_short(row['end_utc']):<16}"
            f"{_minutes(row['gap_s']):>12}{_minutes(row['lost_s']):>12}  {note}"
        )
    if not rows:
        lines.append("  none")
    return lines


def _restart_lines(report: Mapping[str, Any]) -> list[str]:
    capture, forecast = report["capture"], report["forecast"]
    lines = ["Starts, stops and settings changes"]
    for row in capture["restarts"]:
        outage = ", an outage" if row["an_outage"] else ""
        lines.append(
            f"  capture restart: stopped after {_short(row['stopped_after_utc'])}, started "
            f"{_short(row['started_utc'])} ({row['duration_s']:.1f} s; passes "
            f"{row['pass_gap_s']:.1f} s apart{outage}): {'; '.join(row['evidence'])}"
        )
    for row in forecast["settings_changes"]:
        lines.append(
            f"  forecast settings changed from {_short(row['first_new_utc'])}: fields added "
            f"{row['fields_added'] or '-'}, removed {row['fields_removed'] or '-'}, hours "
            f"{row['hours_from']} -> {row['hours_to']}"
        )
    lock = capture.get("current_fetcher_lock")
    if lock:
        lines.append(f"  NPS fetcher lock now: started_at {lock.get('started_at')}")
    if len(lines) == 1:
        lines.append("  none seen")
    return lines


def _exclusion_lines(report: Mapping[str, Any]) -> list[str]:
    lines = ["Exclusions (they do not count)"]
    for row in report["exclusions"]:
        events = ", ".join(f"{k} {v}" for k, v in row["events"].items())
        span = (
            f" {_short(row['first_utc'])} to {_short(row['last_utc'])}" if row["first_utc"] else ""
        )
        refs = "; ".join(
            str(ref.get("ref") or ref.get("key") or ref.get("start") or "")
            for ref in row["refs"][:3]
        )
        lines.append(
            f"  {row['log']}: {row['reason']}: {row['count']}{span}"
            + (f" [{events}]" if events else "")
            + (f" e.g. {refs}" if refs else "")
        )
    return lines


def format_report(report: Mapping[str, Any]) -> str:
    """Render :func:`~lookout_eval.data_report.build_report`'s result as text."""
    window = report["window"]
    sections = [
        [
            f"Lookout data report (PRD §16 M0.5, PLAN.md gate 1): {window['since_utc']} to "
            f"{window['until_utc']} ({window['hours']:.2f} h)",
            f"data dir: {report['data_dir']}",
        ],
        _clock_lines(report),
        _capture_lines(report["capture"]),
        _forecast_lines(report),
        _outage_lines(report),
        _restart_lines(report),
        _exclusion_lines(report),
    ]
    return "\n\n".join("\n".join(section) for section in sections) + "\n"
