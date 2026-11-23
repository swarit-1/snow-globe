"""What design P will need live: forecast hours per run and MB per day for its leads (PRD §13.2).

Design P pairs live captures in the sealed window (15 Dec 2026 to 14 Jan 2027) with live
forecast files at leads of 1, 3, 6, 12, 24 and 48 h. Lead = valid - availability (PRD §8.3,
D-008), and a live hour is available at the later of its S3 stamp and the moment the worker
finished fetching it (D-027). So for a valid time ``V`` and lead ``L``, the run used is the
newest whose hours bracketing ``V`` were available by ``V - L``
(:func:`..availability.select_run_for_issue`), and the worker must have fetched those hours.

:func:`hours_for_leads` replays that rule over a grid of valid times with modelled
availabilities: run start + a publication lag per (cycle hour, forecast hour) + a fetch delay.
Publication varies from run to run, and an early run is chosen with smaller forecast hours
than a late one, so it runs once per lag scenario (:func:`lag_scenarios`: each cell's
earliest logged lag with no fetch delay, and the lag model's conservative p95 rule with the
stand-in's delay) and takes the union. :func:`mb_per_day` prices the hours with ``.idx`` byte
ranges (:func:`price_tables`), at each of :data:`FIELD_PROFILES`: the stand-in's 8 fields
(D-050), the 12 the scene builder reads, and the catalogue's 22, which the deployed job
extracts (D-101). 48 h needs f48 or later of a run started at least 48 h plus its publication
lag before the valid time, which HRRR never has (its last hour is f48): that lead is reported
as never attainable rather than estimated.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from statistics import median
from typing import Any

from ..availability import HourAvailability, LagModel, select_run_for_issue
from ..fields import DEFAULT_FIELD_NAMES, TERRAIN_FIELD
from ..hrrr import SYNOPTIC_HOURS, format_run_id, max_fh
from ..laglog import cycle_hour, usable_lag
from .plan import LIVE_FIELD_NAMES
from .sun import solar_altitude_deg

__all__ = [
    "DESIGN_P_LEADS_H",
    "FIELD_PROFILES",
    "FIELD_PROFILE_NOTES",
    "STANDIN_FETCH_DELAY_S",
    "LagFn",
    "daytime_valid_times",
    "hours_for_leads",
    "lag_scenarios",
    "mb_per_day",
    "price_tables",
    "valid_times_every",
]

#: PRD §13.2, design P.
DESIGN_P_LEADS_H: tuple[float, ...] = (1.0, 3.0, 6.0, 12.0, 24.0, 48.0)
#: Fields eval's scene builder reads beyond the stand-in's 8 (``snow_globe_eval.p1dev_forecast``
#: fills them in as missing for a file of the live profile).
SCENE_EXTRA_FIELDS: tuple[str, ...] = (
    "HGT:cloud top",
    "PRATE:surface",
    "UGRD:10 m above ground",
    "VGRD:10 m above ground",
)
#: The field profiles design P's hours are priced at (each with HGT:surface once per run).
FIELD_PROFILES: dict[str, tuple[str, ...]] = {
    "standin-8": LIVE_FIELD_NAMES,
    "scene-12": (*LIVE_FIELD_NAMES, *SCENE_EXTRA_FIELDS),
    "catalogue-22": DEFAULT_FIELD_NAMES,
}
FIELD_PROFILE_NOTES: dict[str, str] = {
    "standin-8": "the local stand-in's 8 fields since 20 Nov (D-050), and the hindcast's",
    "scene-12": "the stand-in's 8 plus the 4 eval's scene builder also reads (HGT:cloud top, "
    "PRATE:surface, the 10 m winds)",
    "catalogue-22": "every catalogue field, the deployed forecast job's profile (D-101: "
    "Terraform sets no SNOW_GLOBE_FORECAST_* variable)",
}
#: The stand-in ticks every 300 s and extracts a light run in about 3-60 s (the live log's run
#: events), so an hour it fetches is usable up to about 6 min after its S3 stamp.
STANDIN_FETCH_DELAY_S = 360.0

#: ``lag_s(run_start, fh)``: seconds from run start to S3 publication.
LagFn = Callable[[datetime, int], float]


def valid_times_every(first: datetime, last: datetime, step_min: float = 5.0) -> list[datetime]:
    """Valid times from ``first`` to ``last`` inclusive, every ``step_min`` minutes."""
    out: list[datetime] = []
    moment = first
    while moment <= last:
        out.append(moment)
        moment += timedelta(minutes=step_min)
    return out


def daytime_valid_times(
    cameras: Sequence[tuple[float, float]],
    first_day: date,
    last_day: date,
    *,
    min_sun_alt_deg: float = 3.0,
    step_min: float = 5.0,
) -> list[datetime]:
    """Valid times of the days given (UTC) when the Sun is at least ``min_sun_alt_deg`` at one
    of ``cameras`` ((lat, lon) pairs), every ``step_min`` minutes."""
    start = datetime(first_day.year, first_day.month, first_day.day, tzinfo=UTC)
    end = datetime(last_day.year, last_day.month, last_day.day, 23, 59, tzinfo=UTC)
    return [
        moment
        for moment in valid_times_every(start, end, step_min)
        if any(solar_altitude_deg(moment, lat, lon) >= min_sun_alt_deg for lat, lon in cameras)
    ]


def lag_scenarios(
    events: Iterable[Mapping[str, Any]], model: LagModel
) -> dict[str, tuple[LagFn, float]]:
    """``name -> (lag function, fetch delay s)``: the earliest and the conservative case.

    - ``earliest``: each (cycle hour, forecast hour) cell's smallest logged lag (live or
      survey), else the smallest for that forecast hour over all cycles; no fetch delay.
    - ``p95-model``: :meth:`..availability.LagModel.lag_s` (the PRD's p95 rules, 120 min
      without data), plus :data:`STANDIN_FETCH_DELAY_S`.
    """
    by_cell: defaultdict[tuple[int, int], list[float]] = defaultdict(list)
    by_fh: defaultdict[int, list[float]] = defaultdict(list)
    for event in events:
        usable = usable_lag(event)
        if usable is None:
            continue
        run_id, fh, lag = usable
        by_cell[(cycle_hour(run_id), fh)].append(lag)
        by_fh[fh].append(lag)

    def earliest(run_start: datetime, fh: int) -> float:
        cell = by_cell.get((run_start.hour, fh)) or by_fh.get(fh)
        if not cell:
            return model.lag_s(run_start, fh)[0]
        return min(cell)

    def conservative(run_start: datetime, fh: int) -> float:
        return model.lag_s(run_start, fh)[0]

    return {"earliest": (earliest, 0.0), "p95-model": (conservative, STANDIN_FETCH_DELAY_S)}


def hours_for_leads(
    leads_h: Sequence[float],
    scenarios: Mapping[str, tuple[LagFn, float]],
    valid_times: Sequence[datetime],
    *,
    max_lookback_h: int = 72,
) -> dict[str, Any]:
    """Forecast hours each cycle must supply for ``leads_h`` over ``valid_times`` (see module).

    Returns, JSON-ready: per lead, the hours by cycle hour (union over the scenarios), the
    actual leads reached, and how many valid times no run could serve; and the union over
    the leads (``hours_by_cycle``).
    """
    per_lead: dict[float, defaultdict[int, set[int]]] = {lead: defaultdict(set) for lead in leads_h}
    actual: dict[float, list[float]] = {lead: [] for lead in leads_h}
    unattainable: Counter[float] = Counter()
    by_scenario: dict[str, dict[float, defaultdict[int, set[int]]]] = {}
    for name, (lag_s, delay_s) in scenarios.items():
        own = by_scenario.setdefault(name, {lead: defaultdict(set) for lead in leads_h})

        def availability(
            run: datetime, fh: int, lag_s: LagFn = lag_s, delay_s: float = delay_s
        ) -> HourAvailability:
            return (run + timedelta(seconds=lag_s(run, fh) + delay_s), "live-s3-lastmodified")

        for valid in valid_times:
            for lead in leads_h:
                choice = select_run_for_issue(
                    valid - timedelta(hours=lead),
                    valid,
                    availability=availability,
                    max_lookback_h=max_lookback_h,
                )
                if choice is None:
                    unattainable[lead] += 1
                    continue
                hours = {choice.fh_lo, choice.fh_hi}
                cycle = choice.run_start_utc.hour
                per_lead[lead][cycle].update(hours)
                own[lead][cycle].update(hours)
                actual[lead].append(choice.lead_h)
    union: defaultdict[int, set[int]] = defaultdict(set)
    for cycles in per_lead.values():
        for cycle, hours in cycles.items():
            union[cycle].update(hours)
    checks = len(valid_times) * len(scenarios)

    def as_json(cycles: Mapping[int, set[int]]) -> dict[str, list[int]]:
        return {f"{cycle:02d}": sorted(hours) for cycle, hours in sorted(cycles.items())}

    return {
        "leads_h": list(leads_h),
        "valid_times": len(valid_times),
        "scenarios": {name: delay for name, (_, delay) in scenarios.items()},
        "per_lead": {
            f"{lead:g}": {
                "hours_by_cycle": as_json(per_lead[lead]),
                "valid_times_unserved": int(unattainable[lead]),
                "of": checks,
                "lead_h_actual": (
                    {
                        "min": round(min(actual[lead]), 3),
                        "median": round(median(actual[lead]), 3),
                        "max": round(max(actual[lead]), 3),
                    }
                    if actual[lead]
                    else None
                ),
            }
            for lead in leads_h
        },
        "per_scenario": {
            name: {f"{lead:g}": as_json(cycles) for lead, cycles in leads.items()}
            for name, leads in by_scenario.items()
        },
        "hours_by_cycle": as_json(union),
    }


def _terrain_label(per_field: Mapping[str, Any]) -> str:
    for label in (TERRAIN_FIELD, f"{TERRAIN_FIELD} (terrain only)"):
        if label in per_field:
            return label
    raise ValueError(f"the plan has no {TERRAIN_FIELD} entry")


def price_tables(
    plans: Mapping[datetime, Mapping[str, Any]], profile_fields: Sequence[str]
) -> tuple[dict[bool, dict[int, float]], float, dict[str, Any]]:
    """``(bytes per hour, terrain bytes, detail)`` of one field profile, from ``.idx`` sizes.

    ``plans`` maps sample run starts to :func:`..extract.plan_run` results that planned at
    least ``profile_fields``. The table is ``{synoptic: {fh: mean bytes}}`` of the profile's
    fields without the terrain (a field absent from an inventory costs nothing there, as in
    an extraction, and is listed in ``detail["absent"]``); the terrain is its mean bytes once
    per run (the extractor fetches it with a run's first hour). ``detail`` also holds each
    run's total for the profile and the per-hour spread.
    """
    wanted = [name for name in dict.fromkeys(profile_fields) if name != TERRAIN_FIELD]
    samples: dict[bool, dict[int, list[int]]] = {True: {}, False: {}}
    terrain: list[int] = []
    totals: dict[str, int] = {}
    absent: Counter[str] = Counter()
    for run, sizes in sorted(plans.items()):
        per_field = sizes["bytes_by_field_by_fh"]
        unplanned = [name for name in wanted if name not in per_field]
        if unplanned:
            raise ValueError(f"run {format_run_id(run)} was not planned for {unplanned}")
        hours = [int(fh) for fh in sizes["hours"]]
        first = int(per_field[_terrain_label(per_field)][hours[0]])
        terrain.append(first)
        synoptic = run.hour in SYNOPTIC_HOURS
        total = first
        for fh in hours:
            row = 0
            for name in wanted:
                if fh in per_field[name]:
                    row += int(per_field[name][fh])
                else:
                    absent[name] += 1
            samples[synoptic].setdefault(fh, []).append(row)
            total += row
        totals[format_run_id(run)] = total
    if not terrain:
        raise ValueError("no sample run to price the hours from")
    table = {
        kind: {fh: sum(v) / len(v) for fh, v in sorted(per.items())}
        for kind, per in samples.items()
    }
    spread = {
        ("synoptic" if kind else "other"): {
            "hours": len(per),
            "mb_per_hour_min": round(min(min(v) for v in per.values()) / 1e6, 3) if per else None,
            "mb_per_hour_max": round(max(max(v) for v in per.values()) / 1e6, 3) if per else None,
        }
        for kind, per in samples.items()
    }
    detail = {
        "run_total_bytes": totals,
        "spread": spread,
        "absent": {name: n for name, n in sorted(absent.items())},
    }
    return table, sum(terrain) / len(terrain), detail


def mb_per_day(
    hours_by_cycle: Mapping[str, Sequence[int]],
    bytes_by_fh: Mapping[bool, Mapping[int, float]],
    terrain_bytes: float,
) -> dict[str, Any]:
    """Price the hours: ``bytes_by_fh[synoptic][fh]`` per forecast hour (one field profile's
    fields without the terrain, :func:`price_tables`) and ``terrain_bytes`` once per run that
    fetches anything."""
    per_cycle: dict[str, float] = {}
    hours = 0
    missing: list[str] = []
    for cycle_text, fhs in sorted(hours_by_cycle.items()):
        cycle = int(cycle_text)
        synoptic = cycle in SYNOPTIC_HOURS
        table = bytes_by_fh[synoptic]
        total = 0.0
        for fh in fhs:
            if fh > max_fh(datetime(2026, 12, 15, cycle, tzinfo=UTC)):
                raise ValueError(f"cycle {cycle:02d} has no f{fh:02d}")
            if fh not in table:
                missing.append(f"{cycle_text}:f{fh:02d}")
                continue
            total += float(table[fh])
        if fhs:
            total += terrain_bytes
        per_cycle[cycle_text] = round(total / 1e6, 3)
        hours += len(fhs)
    return {
        "runs_per_day": sum(1 for fhs in hours_by_cycle.values() if fhs),
        "forecast_hours_per_day": hours,
        "mb_by_cycle": per_cycle,
        "mb_per_day": round(sum(per_cycle.values()), 1),
        "unpriced_hours": missing,
    }
