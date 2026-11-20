"""Baselines for P1 and the renderer claims (PRD §13.6, §9.1, §13.4).

Every baseline takes an explicit issue time and reads frames only through the leakage guard
(:mod:`.leakage`), so it can never see a frame imaged or made available after the issue time
(PRD §13.1). There is no exception.

Frame labels are the extractor's per-frame classes for a landmark (``label="summit"`` by
default): ``"visible"``, ``"hidden"``, ``"partial"``, ``"in_cloud"`` (camera inside cloud) or
``"camera_fault"`` (a black, saturated or blank frame; see :mod:`.extractor`). For a forecast
of "summit visible", ``in_cloud`` counts as hidden; ``partial`` and ``camera_fault`` are
unusable, so no baseline forecasts from them.

- **Persistence (a):** the latest frame available at the issue time.
- **Persistence (b):** the frame at valid − 24 h, or, when the lead exceeds 24 h, at the same
  time of day on the latest earlier day not after the issue time (valid − 24·k h; see
  :func:`persistence_valid_minus_24h`).
- :func:`choose_persistence` picks the stronger of the two on **development data only**.
- **Climatology:** visible frequency by local-standard-time month and hour, from development
  frames up to the issue time.
- **S_rule:** "visible" if HRRR's ceiling stays above the summit ray all the way to the summit
  (datum-corrected to MSL) and low + mid cover is below 50 %.
- **Analog (Tier 0):** the closest earlier real frame by Sun position and weather class.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any, Literal
from zoneinfo import ZoneInfo

import numpy as np
from lookout_common.manifest import Viewpoint
from lookout_common.timeutil import isoformat_z

from .ephemeris import EphemerisBackend, get_ephemeris
from .frames import FrameRecord
from .leakage import FitView, FrameStore, IssueTimeView, LeakageError
from .scores import Contingency, contingency

__all__ = [
    "CAMERA_FAULT",
    "HIDDEN",
    "IN_CLOUD",
    "PARTIAL",
    "USABLE_STATES",
    "VISIBLE",
    "AnalogWeights",
    "ClimatologyTable",
    "DevCase",
    "PersistenceChoice",
    "Prediction",
    "RaySample",
    "analog",
    "choose_persistence",
    "climatology",
    "fit_climatology",
    "local_standard_month_hour",
    "persistence_days_back",
    "persistence_latest",
    "persistence_valid_minus_24h",
    "s_rule",
    "state_is_visible",
    "sun_lookup",
]

VISIBLE = "visible"
HIDDEN = "hidden"
PARTIAL = "partial"
IN_CLOUD = "in_cloud"
CAMERA_FAULT = "camera_fault"
#: States a baseline can turn into a yes/no forecast of "visible" (never ``partial`` or
#: ``camera_fault``).
USABLE_STATES: tuple[str, ...] = (VISIBLE, HIDDEN, IN_CLOUD)


def state_is_visible(state: Any) -> bool | None:
    """``visible`` → True; ``hidden`` or ``in_cloud`` → False; anything else → None.

    ``partial`` and ``camera_fault`` give None: no forecast is made from them.
    """
    if state == VISIBLE:
        return True
    if state in (HIDDEN, IN_CLOUD):
        return False
    return None


def _aware(moment: datetime, what: str) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware")
    return moment


def _check_times(issue_time: datetime, valid_time: datetime) -> None:
    _aware(issue_time, "issue_time")
    _aware(valid_time, "valid_time")
    if valid_time < issue_time:
        raise ValueError("valid_time is before issue_time")


@dataclass(frozen=True)
class Prediction:
    """One baseline forecast of "visible" for one (issue, valid) pair.

    ``value`` is None when the baseline cannot forecast (no usable frame, missing input);
    the pair is then excluded for every forecast in a paired comparison.
    """

    baseline: str
    viewpoint_id: str
    issue_time: datetime
    valid_time: datetime
    value: bool | None
    probability: float | None = None
    frames: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        """True when the baseline produced a yes/no forecast."""
        return self.value is not None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {
            "baseline": self.baseline,
            "viewpoint_id": self.viewpoint_id,
            "issue_time_utc": isoformat_z(self.issue_time),
            "valid_time_utc": isoformat_z(self.valid_time),
            "value": self.value,
            "probability": self.probability,
            "frames": list(self.frames),
            "flags": list(self.flags),
            "detail": dict(self.detail),
        }


# --- persistence -------------------------------------------------------------------------


def persistence_latest(
    view: IssueTimeView,
    viewpoint_id: str,
    *,
    valid_time: datetime,
    label: str = "summit",
    usable: Collection[str] = USABLE_STATES,
    max_age: timedelta | None = None,
) -> Prediction:
    """Persistence (a): the latest frame available at the issue time (PRD §13.6).

    Night frames carry no label, so this is the latest *daytime* frame with a usable state.
    With ``max_age``, an older frame gives no forecast (flag ``stale``).
    """
    issue = view.issue_time
    _check_times(issue, valid_time)
    record = view.latest(viewpoint_id, label=label, values=usable)
    if record is None:
        return Prediction(
            "persistence_a",
            viewpoint_id,
            issue,
            valid_time,
            None,
            flags=("no-usable-frame-before-issue",),
        )
    state = view.label(record, label)
    age = issue - record.image_time
    detail = {
        "frame_time_utc": isoformat_z(record.image_time),
        "age_h": round(age.total_seconds() / 3600.0, 4),
        "state": state,
    }
    if max_age is not None and age > max_age:
        return Prediction(
            "persistence_a",
            viewpoint_id,
            issue,
            valid_time,
            None,
            frames=(record.key,),
            flags=("stale",),
            detail=detail,
        )
    return Prediction(
        "persistence_a",
        viewpoint_id,
        issue,
        valid_time,
        state_is_visible(state),
        frames=(record.key,),
        detail=detail,
    )


def persistence_days_back(issue_time: datetime, valid_time: datetime) -> int:
    """Whole days persistence (b) looks back: ``ceil(lead / 24 h)``, at least 1."""
    whole, rest = divmod(valid_time - issue_time, timedelta(hours=24))
    return max(1, whole + (1 if rest else 0))


def persistence_valid_minus_24h(
    view: IssueTimeView,
    viewpoint_id: str,
    *,
    valid_time: datetime,
    label: str = "summit",
    usable: Collection[str] = USABLE_STATES,
    tolerance: timedelta = timedelta(minutes=30),
) -> Prediction:
    """Persistence (b): the frame at valid − 24 h, from frames available at issue (PRD §13.6).

    With a lead of 24 h or less, valid − 24 h is at or before the issue time. With a longer
    lead it is after the issue time, and reading that frame would break PRD §13.1 ("Baselines
    see only frames available at issue time"). So (b) goes back whole days: the target is
    valid − 24·k h with k = ceil(lead / 24 h), the latest moment at the valid time's time of
    day that is not after the issue time. Predictions with k > 1 carry the flag
    ``valid-minus-<24k>h``. (This reading of §13.6 is recorded for the owner; the other
    option is no (b) forecast beyond 24 h.)

    The frame nearest the target within ``tolerance`` is used, among frames available at
    the issue time: the window is cut at the issue time, so at a 24 h lead only frames
    from the ``tolerance`` before the issue count.
    """
    issue = view.issue_time
    _check_times(issue, valid_time)
    days_back = persistence_days_back(issue, valid_time)
    target = valid_time - timedelta(hours=24 * days_back)
    flags = (f"valid-minus-{24 * days_back}h",) if days_back > 1 else ()
    record = view.nearest(viewpoint_id, target, tolerance=tolerance, label=label, values=usable)
    if record is None:
        return Prediction(
            "persistence_b",
            viewpoint_id,
            issue,
            valid_time,
            None,
            flags=("no-usable-frame-near-target", *flags),
            detail={"target_utc": isoformat_z(target), "days_back": days_back},
        )
    state = view.label(record, label)
    return Prediction(
        "persistence_b",
        viewpoint_id,
        issue,
        valid_time,
        state_is_visible(state),
        frames=(record.key,),
        flags=flags,
        detail={
            "target_utc": isoformat_z(target),
            "days_back": days_back,
            "frame_time_utc": isoformat_z(record.image_time),
            "offset_min": round((record.image_time - target).total_seconds() / 60.0, 3),
            "state": state,
        },
    )


@dataclass(frozen=True)
class DevCase:
    """One development pair for choosing the persistence variant.

    ``observed`` is the truth at the valid time (True = visible); None means not scored
    (partial, night or no frame).
    """

    viewpoint_id: str
    issue_time: datetime
    valid_time: datetime
    observed: bool | None


@dataclass(frozen=True)
class PersistenceChoice:
    """Which persistence variant is the stronger one on development data, and why."""

    chosen: Literal["a", "b"]
    pss_a: float | None
    pss_b: float | None
    table_a: Contingency
    table_b: Contingency
    n_cases: int
    n_paired: int
    n_b_beyond_24h: int
    flags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form, for DECISIONS.md and the pre-registration."""
        return {
            "chosen": self.chosen,
            "pss_a": self.pss_a,
            "pss_b": self.pss_b,
            "table_a": self.table_a.to_dict(),
            "table_b": self.table_b.to_dict(),
            "n_cases": self.n_cases,
            "n_paired": self.n_paired,
            "n_b_beyond_24h": self.n_b_beyond_24h,
            "flags": list(self.flags),
        }


def choose_persistence(
    store: FrameStore,
    cases: Sequence[DevCase],
    *,
    label: str = "summit",
    usable: Collection[str] = USABLE_STATES,
    tolerance: timedelta = timedelta(minutes=30),
    max_age: timedelta | None = None,
) -> PersistenceChoice:
    """Pick the stronger persistence variant by PSS on development cases only (PRD §13.6).

    Raises :class:`LeakageError` if any case's issue or valid time is held out at its
    viewpoint (the committed split, plus the store's extra periods). Both variants
    are scored on the same pairs: those where both forecast and the truth is known. Ties and
    undefined scores keep (a). Pass the cases of one lead at a time: the stronger variant can
    differ by lead. ``n_b_beyond_24h`` counts paired cases where (b) went back more than one
    day (lead > 24 h).
    """
    forecasts_a: list[bool] = []
    forecasts_b: list[bool] = []
    observed: list[bool] = []
    n_b_beyond_24h = 0
    for case in cases:
        for moment in (case.issue_time, case.valid_time):
            if store.is_held_out(case.viewpoint_id, moment):
                raise LeakageError(
                    "choose_persistence uses development data only; a case at "
                    f"{moment.isoformat()} is held out"
                )
        view = store.as_of(case.issue_time, purpose="persistence-choice", allow_held_out=False)
        a = persistence_latest(
            view,
            case.viewpoint_id,
            valid_time=case.valid_time,
            label=label,
            usable=usable,
            max_age=max_age,
        )
        b = persistence_valid_minus_24h(
            view,
            case.viewpoint_id,
            valid_time=case.valid_time,
            label=label,
            usable=usable,
            tolerance=tolerance,
        )
        if a.value is None or b.value is None or case.observed is None:
            continue
        forecasts_a.append(a.value)
        forecasts_b.append(b.value)
        observed.append(case.observed)
        n_b_beyond_24h += int(b.detail.get("days_back", 1) > 1)
    table_a = contingency(forecasts_a, observed)
    table_b = contingency(forecasts_b, observed)
    flags: list[str] = []
    pss_a, pss_b = table_a.pss, table_b.pss
    if pss_a is None or pss_b is None:
        chosen: Literal["a", "b"] = "a"
        flags.append("a PSS is undefined on the development pairs; (a) kept by rule")
    elif pss_b > pss_a:
        chosen = "b"
    else:
        chosen = "a"
        if pss_b == pss_a:
            flags.append("tie; (a) kept by rule")
    if n_b_beyond_24h:
        flags.append(f"(b) went back more than one day for {n_b_beyond_24h} pairs (lead > 24 h)")
    return PersistenceChoice(
        chosen=chosen,
        pss_a=pss_a,
        pss_b=pss_b,
        table_a=table_a,
        table_b=table_b,
        n_cases=len(cases),
        n_paired=len(observed),
        n_b_beyond_24h=n_b_beyond_24h,
        flags=tuple(flags),
    )


# --- climatology -------------------------------------------------------------------------


def local_standard_month_hour(moment: datetime, time_zone: str) -> tuple[int, int]:
    """Month and hour in the zone's *standard* time (no DST shift mid-season)."""
    _aware(moment, "moment")
    local = moment.astimezone(ZoneInfo(time_zone))
    offset = (local.utcoffset() or timedelta(0)) - (local.dst() or timedelta(0))
    standard = moment.astimezone(UTC) + offset
    return standard.month, standard.hour


@dataclass(frozen=True)
class ClimatologyTable:
    """Counts of usable frames and of visible ones, by (month, hour) of local standard time."""

    viewpoint_id: str
    label: str
    time_zone: str
    month_hour: Mapping[tuple[int, int], tuple[int, int]]
    hour: Mapping[int, tuple[int, int]]
    overall: tuple[int, int]
    fitted_until: datetime | None

    @property
    def base_rate(self) -> float | None:
        """Overall visible frequency, or None with no frames."""
        events, total = self.overall
        return None if total == 0 else events / total

    def probability(self, valid_time: datetime, *, min_count: int) -> tuple[float | None, str, int]:
        """Visible frequency for the valid time's month and hour.

        Falls back to the hour over all months, then to the overall rate, when a bin has
        fewer than ``min_count`` frames (design P's development data hold no January).
        Returns ``(probability, level, n)``.
        """
        month, hour = local_standard_month_hour(valid_time, self.time_zone)
        for level, counts in (
            ("month-hour", self.month_hour.get((month, hour))),
            ("hour", self.hour.get(hour)),
        ):
            if counts is not None and counts[1] >= min_count:
                return counts[0] / counts[1], level, counts[1]
        events, total = self.overall
        if total == 0:
            return None, "none", 0
        return events / total, "overall", total

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {
            "viewpoint_id": self.viewpoint_id,
            "label": self.label,
            "time_zone": self.time_zone,
            "time_basis": "local standard time",
            "month_hour": {
                f"{m:02d}-{h:02d}": list(v) for (m, h), v in sorted(self.month_hour.items())
            },
            "hour": {f"{h:02d}": list(v) for h, v in sorted(self.hour.items())},
            "overall": list(self.overall),
            "fitted_until": None if self.fitted_until is None else isoformat_z(self.fitted_until),
        }


def fit_climatology(
    view: FitView,
    viewpoint_id: str,
    time_zone: str,
    *,
    label: str = "summit",
    usable: Collection[str] = USABLE_STATES,
) -> ClimatologyTable:
    """Count visible frames by month and hour from the development frames the view allows."""
    month_hour: dict[tuple[int, int], list[int]] = {}
    hour: dict[int, list[int]] = {}
    overall = [0, 0]
    for record in view.records(viewpoint_id, label=label, values=usable):
        visible = state_is_visible(view.label(record, label))
        if visible is None:
            continue
        m, h = local_standard_month_hour(record.image_time, time_zone)
        for counts in (month_hour.setdefault((m, h), [0, 0]), hour.setdefault(h, [0, 0]), overall):
            counts[0] += int(visible)
            counts[1] += 1
    return ClimatologyTable(
        viewpoint_id=viewpoint_id,
        label=label,
        time_zone=time_zone,
        month_hour={k: (v[0], v[1]) for k, v in month_hour.items()},
        hour={k: (v[0], v[1]) for k, v in hour.items()},
        overall=(overall[0], overall[1]),
        fitted_until=view.until,
    )


def climatology(
    store: FrameStore,
    viewpoint_id: str,
    *,
    issue_time: datetime,
    valid_time: datetime,
    time_zone: str,
    label: str = "summit",
    usable: Collection[str] = USABLE_STATES,
    min_count: int = 10,
    threshold: float | Literal["base_rate"] = "base_rate",
    design: str | None = None,
) -> Prediction:
    """Climatology (PRD §13.6): the visible frequency for that month and hour.

    Fitted on development frames imaged and available by the issue time, and before the
    viewpoint's fit bound (:meth:`.leakage.FrameStore.fit_bound`): the earliest held-out
    period by default, or ``design``'s period for a fit of one design. So it is valid on
    development pairs too. The yes/no forecast is
    ``p >= threshold``. The default threshold is the fitted overall base rate, the cut that
    maximises PSS for a calibrated probability (Manzato 2007, *Wea. Forecasting* 22:1076).
    At 0.5, a mostly-hidden winter summit would give "hidden" every hour and a PSS of
    exactly 0.
    """
    _check_times(issue_time, valid_time)
    view = store.for_fit(purpose="climatology", until=issue_time, design=design)
    table = fit_climatology(view, viewpoint_id, time_zone, label=label, usable=usable)
    p, level, n = table.probability(valid_time, min_count=min_count)
    cut = table.base_rate if threshold == "base_rate" else float(threshold)
    detail = {
        "level": level,
        "n": n,
        "threshold": cut,
        "threshold_rule": "base_rate" if threshold == "base_rate" else "fixed",
        "base_rate": table.base_rate,
        "n_frames": table.overall[1],
    }
    if p is None or cut is None:
        return Prediction(
            "climatology",
            viewpoint_id,
            issue_time,
            valid_time,
            None,
            flags=("no-development-frames",),
            detail=detail,
        )
    flags = () if level == "month-hour" else (f"fallback-{level}",)
    return Prediction(
        "climatology",
        viewpoint_id,
        issue_time,
        valid_time,
        p >= cut,
        probability=p,
        flags=flags,
        detail=detail,
    )


# --- S_rule --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RaySample:
    """The forecast ceiling at one point of the camera-to-summit ray (metres above MSL).

    ``ceiling_m_msl`` None means HRRR diagnosed no ceiling there. ``ray_height_m_msl`` is the
    ray's own height at that distance.
    """

    distance_km: float
    ray_height_m_msl: float
    ceiling_m_msl: float | None
    point_id: str = ""


def s_rule(
    *,
    viewpoint_id: str,
    issue_time: datetime,
    valid_time: datetime,
    forecast_available_time: datetime,
    ray_samples: Sequence[RaySample],
    summit_height_m_msl: float,
    low_cover_pct: float | None,
    mid_cover_pct: float | None,
    cover_threshold_pct: float = 50.0,
) -> Prediction:
    """S_rule (PRD §13.6): visible if the ceiling stays above the summit ray, and low + mid < 50 %.

    "HRRR's ceiling where the summit ray crosses it is above the summit": walking out from the
    camera, the first sample whose ceiling is at or below the ray's height is where the ray
    enters the deck, and the summit is hidden. If no sample crosses, the ray stays under the
    ceiling to the summit and the ceiling at the summit end is above the summit. Ceilings must
    already be in metres above MSL (datum-corrected by the caller). The forecast must have been
    available by the issue time, or :class:`LeakageError` is raised.
    """
    _check_times(issue_time, valid_time)
    _aware(forecast_available_time, "forecast_available_time")
    if forecast_available_time > issue_time:
        raise LeakageError(
            f"S_rule: the forecast was available at {forecast_available_time.isoformat()}, "
            f"after the issue time {issue_time.isoformat()}"
        )
    if not ray_samples:
        raise ValueError("S_rule needs at least one ray sample")
    samples = sorted(ray_samples, key=lambda s: s.distance_km)
    for sample in samples:
        for value in (sample.distance_km, sample.ray_height_m_msl, sample.ceiling_m_msl):
            if value is not None and not math.isfinite(value):
                raise ValueError("ray samples must be finite (None means no ceiling)")
    crossing = next(
        (
            s
            for s in samples
            if s.ceiling_m_msl is not None and s.ceiling_m_msl <= s.ray_height_m_msl
        ),
        None,
    )
    summit_end = samples[-1]
    detail: dict[str, Any] = {
        "summit_height_m_msl": summit_height_m_msl,
        "ceiling_at_summit_end_m_msl": summit_end.ceiling_m_msl,
        "summit_end_distance_km": summit_end.distance_km,
        "crossing_distance_km": None if crossing is None else crossing.distance_km,
        "crossing_point": None if crossing is None else crossing.point_id,
        "low_cover_pct": low_cover_pct,
        "mid_cover_pct": mid_cover_pct,
        "cover_threshold_pct": cover_threshold_pct,
        "n_samples": len(samples),
    }
    if low_cover_pct is None or mid_cover_pct is None:
        return Prediction(
            "s_rule",
            viewpoint_id,
            issue_time,
            valid_time,
            None,
            flags=("cover-missing",),
            detail=detail,
        )
    cover_ok = (low_cover_pct + mid_cover_pct) < cover_threshold_pct
    ceiling_ok = crossing is None and (
        summit_end.ceiling_m_msl is None or summit_end.ceiling_m_msl > summit_height_m_msl
    )
    detail["ceiling_ok"] = ceiling_ok
    detail["cover_ok"] = cover_ok
    return Prediction(
        "s_rule",
        viewpoint_id,
        issue_time,
        valid_time,
        bool(ceiling_ok and cover_ok),
        detail=detail,
    )


# --- analog (Tier 0) -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AnalogWeights:
    """Weights of the analog distance, in "degrees" per unit.

    Placeholders for v−1. TODO(M2): fitted on development data and frozen in the
    pre-registration (PRD §13.4 R3, §16 "analog weights").
    """

    alt: float = 1.0
    az: float = 0.25
    day_of_year: float = 0.0


def _wrap180(degrees: float) -> float:
    return (degrees + 180.0) % 360.0 - 180.0


def _day_of_year_gap(a: datetime, b: datetime) -> float:
    days = abs(a.timetuple().tm_yday - b.timetuple().tm_yday)
    return float(min(days, 365.25 - days))


def sun_lookup(
    viewpoint: Viewpoint, ephemeris: EphemerisBackend | None = None
) -> Callable[[FrameRecord], tuple[float, float]]:
    """A memoised ``frame -> (Sun altitude, azimuth)`` at the camera (geometric altitude)."""
    backend = ephemeris or get_ephemeris()
    cam = viewpoint.camera

    @lru_cache(maxsize=200_000)
    def at(moment: datetime) -> tuple[float, float]:
        return backend.sun_alt_az(moment, cam.lat, cam.lon, cam.elevation_m)

    return lambda record: at(record.image_time)


def analog(
    view: IssueTimeView,
    viewpoint_id: str,
    *,
    valid_time: datetime,
    sun_alt_deg: float,
    sun_az_deg: float,
    forecast_class: str | None,
    sun_of: Callable[[FrameRecord], tuple[float, float]],
    label: str = "summit",
    class_label: str = "weather_class",
    usable: Collection[str] = USABLE_STATES,
    weights: AnalogWeights = AnalogWeights(),
    max_distance_deg: float = 5.0,
) -> Prediction:
    """Analog / Tier 0 (PRD §9.1, §13.4 R3): the closest earlier real frame.

    Candidates are frames available at the issue time with a usable state. The query is the
    valid time's Sun position and the forecast's weather class; a candidate's class is the
    extractor's label for that frame. The nearest same-class frame by weighted Sun distance
    wins. If none lies within ``max_distance_deg``, or the class is unknown, the match falls
    back to Sun position only and is flagged (``no-close-analog`` is R3's pre-registered
    stratum). The forecast is the matched frame's state.
    """
    issue = view.issue_time
    _check_times(issue, valid_time)
    candidates = view.records(viewpoint_id, label=label, values=usable)
    if not candidates:
        return Prediction(
            "analog",
            viewpoint_id,
            issue,
            valid_time,
            None,
            flags=("no-usable-frame-before-issue",),
        )

    def distance(record: FrameRecord) -> float:
        alt, az = sun_of(record)
        return math.sqrt(
            (weights.alt * (alt - sun_alt_deg)) ** 2
            + (weights.az * _wrap180(az - sun_az_deg)) ** 2
            + (weights.day_of_year * _day_of_year_gap(record.image_time, valid_time)) ** 2
        )

    distances = np.array([distance(record) for record in candidates])
    flags: list[str] = []
    chosen_index: int | None = None
    n_same_class = 0
    if forecast_class is None:
        flags.append("forecast-class-unknown")
    else:
        same = [
            i
            for i, record in enumerate(candidates)
            if view.label(record, class_label) == forecast_class
        ]
        n_same_class = len(same)
        if same:
            best = min(same, key=lambda i: (distances[i], -candidates[i].image_time.timestamp()))
            if distances[best] <= max_distance_deg:
                chosen_index = best
        if chosen_index is None:
            flags.append("no-close-analog")
    if chosen_index is None:
        flags.append("sun-only-match")
        chosen_index = min(
            range(len(candidates)),
            key=lambda i: (distances[i], -candidates[i].image_time.timestamp()),
        )
    record = candidates[chosen_index]
    state = view.label(record, label)
    alt, az = sun_of(record)
    return Prediction(
        "analog",
        viewpoint_id,
        issue,
        valid_time,
        state_is_visible(state),
        frames=(record.key,),
        flags=tuple(flags),
        detail={
            "frame_time_utc": isoformat_z(record.image_time),
            "distance_deg": float(distances[chosen_index]),
            "frame_sun_alt_deg": alt,
            "frame_sun_az_deg": az,
            "query_sun_alt_deg": sun_alt_deg,
            "query_sun_az_deg": sun_az_deg,
            "forecast_class": forecast_class,
            "n_candidates": len(candidates),
            "n_same_class": n_same_class,
            "state": state,
            "weights": {"alt": weights.alt, "az": weights.az, "day_of_year": weights.day_of_year},
        },
    )
