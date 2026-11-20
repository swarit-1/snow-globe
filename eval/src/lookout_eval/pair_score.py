"""The score of one backtest pair: the P1 contingency cell and the ratio error (PRD §13.4, §13.5).

PRD §13.4 P1: a daytime frame is "visible" when the landmark band's edge ratio is ≥ τ,
"hidden" below a lower threshold, "partial" in between (excluded from P1 and reported);
frames from inside cloud are a separate class, counted as hidden in a sensitivity analysis.
The prediction is the same extractor run on the rendered frame.

For one pair that gives one cell of the 2×2 table, with the event "landmark visible":

- **forecast** visible ⇔ the render's class is ``visible`` (its ratio clears τ_hi); a
  partial, hidden or in-cloud render forecasts "not visible";
- **observed** ``visible`` is an event and ``hidden`` a non-event; a ``partial`` truth goes
  to the partial mask; an ``in_cloud`` truth is excluded in the primary table and counted
  as hidden in the sensitivity table; a truth frame that failed a quality gate (§8.6), or
  has no state, is excluded.

The cell becomes a :class:`~.scores.Contingency` of one pair, so the record carries the
same counts and explicit "undefined" reasons a full table would (PSS needs both events and
non-events, which one pair never has). The continuous error ``render ratio − truth ratio``
and its log ratio are reported beside it.
"""

from __future__ import annotations

import math
from typing import Any

from .extractor import CAMERA_FAULT_CLASS, LandmarkResult
from .scores import Contingency, contingency

__all__ = ["EVENT", "score_pair"]

#: P1's event (PRD §13.4).
EVENT = "visible"


_CELLS = (
    ("hits", "hit"),
    ("misses", "miss"),
    ("false_alarms", "false_alarm"),
    ("correct_negatives", "correct_negative"),
)


def _cell(table: Contingency) -> str | None:
    """The one cell a single-pair table fills, or None when the pair is masked out."""
    for field_name, label in _CELLS:
        if getattr(table, field_name):
            return label
    return None


def _table(forecast: bool, observed: bool | None, *, excluded: bool, partial: bool) -> Contingency:
    """A one-pair table; a pair with no observed value is excluded unless it is partial."""
    drop = excluded or (observed is None and not partial)
    return contingency(
        [forecast], [bool(observed)], excluded=[drop], partial=[partial and not drop]
    )


def score_pair(
    truth: LandmarkResult, render: LandmarkResult, *, truth_scored: bool = True
) -> dict[str, Any]:
    """One pair's P1 cell (event "visible"), its sensitivity cell and the ratio error.

    ``truth_scored`` is False when the truth frame failed a quality gate (PRD §8.6); the
    pair is then excluded from both tables.
    """
    observed_class = truth.p1_class
    forecast_class = render.p1_class
    forecast_visible = forecast_class == EVENT
    reasons: list[str] = []
    if not truth_scored:
        reasons.append("the truth frame failed a quality gate (PRD §8.6)")
    if forecast_class is None:
        reasons.append("the render has no class (no reference in its Sun bin)")
    elif forecast_class == CAMERA_FAULT_CLASS:
        reasons.append("the render is a camera-fault frame")
    if observed_class is None:
        reasons.append("the truth frame has no class (no reference in its Sun bin)")
    elif observed_class == CAMERA_FAULT_CLASS:
        reasons.append("the truth frame is a camera-fault frame")
    blocked = bool(reasons)

    partial = not blocked and observed_class == "partial"
    in_cloud = not blocked and observed_class == "in_cloud"
    observed: bool | None = None
    if not blocked and observed_class in ("visible", "hidden"):
        observed = observed_class == "visible"
    primary = _table(forecast_visible, observed, excluded=blocked or in_cloud, partial=partial)
    sensitivity_observed = False if in_cloud else observed
    sensitivity = _table(forecast_visible, sensitivity_observed, excluded=blocked, partial=partial)
    if partial:
        reasons.append("partial truth: excluded from P1 and reported (PRD §13.4)")
    if in_cloud:
        reasons.append("in-cloud truth: its own class; counted as hidden only in the sensitivity")

    ratio_error = log_ratio = None
    if truth.ratio is not None and render.ratio is not None:
        ratio_error = render.ratio - truth.ratio
        if truth.ratio > 0 and render.ratio > 0:
            log_ratio = math.log(render.ratio / truth.ratio)
    return {
        "event": EVENT,
        "forecast_class": forecast_class,
        "observed_class": observed_class,
        "forecast_visible": None if forecast_class is None else forecast_visible,
        "observed_visible": observed,
        "cell": _cell(primary),
        "cell_sensitivity_in_cloud_as_hidden": _cell(sensitivity),
        "notes": reasons,
        "contingency": primary.to_dict(),
        "contingency_sensitivity_in_cloud_as_hidden": sensitivity.to_dict(),
        "ratio_truth": truth.ratio,
        "ratio_render": render.ratio,
        "ratio_render_minus_truth": ratio_error,
        "log_ratio_render_over_truth": log_ratio,
        "classes_agree": (
            None
            if forecast_class is None or observed_class is None
            else forecast_class == observed_class
        ),
    }
