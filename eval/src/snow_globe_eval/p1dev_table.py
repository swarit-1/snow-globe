"""The dev-only P1 table: PSS per method and the paired ΔPSS (PLAN M1.5; PRD §13.4, §13.5, §13.7).

Built from the per-frame records the batch writes (:mod:`.p1dev`). The event is "summit band
visible" (PRD §13.4 P1), every forecast is a yes/no for it, and the truth is the extractor's
class of the real frame:

- ``visible`` is an event and ``hidden`` a non-event;
- ``partial`` is **excluded from P1 and counted** (the ``partial`` mask of
  :func:`.scores.contingency`);
- ``in_cloud`` (the camera inside cloud) is **its own class**: left out of the primary table and
  counted, and **counted as hidden** in the sensitivity table (§13.4);
- a frame the extractor could not score (no clear-day reference near its Sun position, a
  failed §8.6 gate, a camera fault) is excluded and counted by reason;
- a truth frame that is **one of the plate's own input frames** is excluded from every row and
  counted (the backtest pair refuses a plate that is the truth frame; :mod:`.p1dev_assets`).

**Fitted assets** (:mod:`.p1dev_assets`). The primary renders over plate v0.1 and measures with
extractor v0's whole clear-day pool, assets that postdate many frames' issue times. Two
sensitivity blocks restrict the frames: to those whose plate inputs and clear-day references
all pass the backtest pair's rule (taken and available before the issue time, off the valid
local day), and to those whose plate inputs all lie off the valid local day. The counts are in
``fitted_assets``.

A frame is scored for a method only when that method made a forecast; the paired ΔPSS uses
the frames where **both** forecasts exist, so both sides see one set of masks (§13.5:
exclusions apply identically to the model and every baseline). The model's forecast is
"visible" when the extractor calls the render's summit ``visible`` (a ``partial``, ``hidden`` or
``in_cloud`` render forecasts "not visible", as :mod:`.pair_score` reads one pair); a render the
extractor cannot classify gives no forecast.

**Statistics** (§13.4, §13.7): PSS = POD − POFD (:class:`.scores.Contingency`), with a 95 %
interval from :func:`.scores.bootstrap_pss`; the paired ΔPSS (model − baseline) from
:func:`.scores.paired_delta_pss`. Both resample whole **local days of the valid time** in
moving blocks of ``block_length_days`` (at least 1; the M2 freeze sets it from development
autocorrelation: TODO(M2)), wrapped, with a fixed seed. The effective sample size is the
number of days with scored frames, reported beside every frame count.

**Which persistence.** PRD §13.6 compares with "the stronger of (a) and (b)". The table picks
it with :func:`.baselines.choose_persistence` on the **dev-fit** weeks only and applies that
choice to every subset (:mod:`.p1dev_weeks`), so the dev-check rows never see the data that
chose their baseline; the ΔPSS against (a) and against (b) is reported too.

Every table is **provisional** while any reason is listed (the extractor below its §13.8 bar,
τ not fitted, the bootstrap block not set, ...): ``provisional`` is true and the reasons are
listed at the top of the JSON and the Markdown, whose first sentence follows the extractor's
validation.

**Availability rule.** The header names the rule the forecast choice used (:mod:`.p1dev_forecast`),
and ``forecast_choice_under_other_rule`` lists the frames whose run would differ under the other.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

import numpy as np
from numpy.typing import NDArray

from .p1dev_weeks import DEV_CHECK, DEV_FIT
from .scores import bootstrap_pss, paired_delta_pss

__all__ = [
    "METHODS",
    "METHOD_NAMES",
    "SUBSETS",
    "WHITE_OUT_FAULTS",
    "Case",
    "build_table",
    "case_from_record",
    "delta_row",
    "faults_as_not_visible",
    "method_row",
    "to_markdown",
]

METHODS: tuple[str, ...] = ("model", "persistence_a", "persistence_b", "climatology", "s_rule")
METHOD_NAMES: dict[str, str] = {
    "model": "Snow Globe: the extractor on the render",
    "persistence_a": "persistence (a): latest frame at issue",
    "persistence_b": "persistence (b): frame at valid - 24 h",
    "climatology": "climatology: month and hour, dev frames by issue",
    "s_rule": "S_rule: ceiling above the summit ray, low + mid < 50 %",
}
SUBSETS: tuple[str, ...] = ("all", DEV_FIT, DEV_CHECK)
#: Truth classes that are an event or a non-event.
OBSERVED: dict[str, bool] = {"visible": True, "hidden": False}
#: Truth classes P1 handles itself (the rest cannot be scored).
P1_TRUTHS: tuple[str, ...] = ("visible", "hidden", "partial", "in_cloud")


@dataclass(frozen=True)
class Case:
    """One frame of the table: its local day, week role, truth class and every forecast."""

    key: str
    local_day: date
    role: str
    truth: str | None
    forecasts: Mapping[str, bool | None]
    has_forecast_run: bool = False
    lead_from_availability_h: float | None = None
    #: The extractor's camera-fault kind for the render (``saturated``, ``blank``, ``dark``).
    render_fault: str | None = None
    #: Why the frame is left out of every row (a plate input frame as truth), or None.
    excluded_because: str | None = None
    #: The fitted-asset check (:mod:`.p1dev_assets`): every plate input and reference passes.
    assets_pass: bool | None = None
    #: No plate input frame lies on the valid time's local day.
    plate_off_valid_day: bool | None = None
    #: A dev-check frame measured with an asset from a dev-check week.
    dev_check_asset: bool = False
    #: Why each method made no forecast (method -> reason), for the methods that made none.
    no_forecast_reasons: Mapping[str, str] = field(default_factory=dict)


def _model_no_forecast_reason(record: Mapping[str, Any]) -> str:
    status = str(record.get("status"))
    if status != "scored":
        return {"no-forecast": "no-forecast-run"}.get(status, status)
    prediction = record.get("prediction") or {}
    cls = prediction.get("p1_class")
    if cls == "camera_fault":
        return f"render-camera-fault-{(prediction.get('detail') or {}).get('camera_fault')}"
    return f"render-{cls}"


def _no_forecast_reasons(record: Mapping[str, Any]) -> dict[str, str]:
    forecasts = record["p1"]["forecasts"]
    out = {}
    for method in METHODS:
        if forecasts.get(method) is not None:
            continue
        if method == "model":
            out[method] = _model_no_forecast_reason(record)
            continue
        baseline = (record.get("baselines") or {}).get(method) or {}
        flags = list(baseline.get("flags") or [])
        out[method] = flags[0] if flags else "no-forecast"
    return out


def case_from_record(record: Mapping[str, Any]) -> Case:
    """A :class:`Case` from a per-frame record (:mod:`.p1dev`)."""
    frame = record["frame"]
    forecast = record.get("forecast")
    prediction = record.get("prediction") or {}
    fault = None
    if prediction.get("frame_class") == "camera_fault":
        fault = (prediction.get("detail") or {}).get("camera_fault") or "unknown"
    assets = record.get("fitted_assets")
    return Case(
        key=str(frame["key"]),
        local_day=date.fromisoformat(frame["local_day"]),
        role=str(frame["dev_week"]["role"]),
        truth=record["p1"]["truth_class"],
        forecasts=dict(record["p1"]["forecasts"]),
        has_forecast_run=forecast is not None,
        lead_from_availability_h=None
        if forecast is None
        else float(forecast["lead_from_availability_h"]),
        render_fault=fault,
        excluded_because=record["p1"].get("excluded_because"),
        assets_pass=None if not isinstance(assets, Mapping) else bool(assets.get("passes")),
        plate_off_valid_day=None
        if not isinstance(assets, Mapping)
        else bool(assets.get("plate_off_valid_day")),
        dev_check_asset=isinstance(assets, Mapping)
        and str(frame["dev_week"]["role"]) == DEV_CHECK
        and bool(assets.get("uses_dev_check_week_asset")),
        no_forecast_reasons=_no_forecast_reasons(record),
    )


#: Render camera faults the diagnostic reads as the model forecasting "not visible".
WHITE_OUT_FAULTS: tuple[str, ...] = ("saturated", "blank")


def faults_as_not_visible(cases: Sequence[Case]) -> list[Case]:
    """``cases`` with a saturated or blank render read as the model forecasting "not visible".

    A diagnostic, reported beside the primary table and never in its place: renderer 0.3.0
    draws a camera inside a cloud slab as a white-out, which extractor v0 calls a camera fault
    (D-037), so the primary reading (:mod:`.pair_score`) has the model make no forecast there.
    """
    out = []
    for case in cases:
        if case.render_fault in WHITE_OUT_FAULTS and case.forecasts.get("model") is None:
            case = replace(case, forecasts={**case.forecasts, "model": False})
        out.append(case)
    return out


def _subset(cases: Sequence[Case], name: str) -> list[Case]:
    return list(cases) if name == "all" else [c for c in cases if c.role == name]


def _observed(case: Case, in_cloud_as_hidden: bool) -> bool | None:
    if case.truth == "in_cloud" and in_cloud_as_hidden:
        return False
    return OBSERVED.get(str(case.truth))


def _arrays(
    cases: Sequence[Case], methods: Sequence[str], *, in_cloud_as_hidden: bool
) -> tuple[
    NDArray[np.bool_],
    NDArray[np.bool_],
    NDArray[np.bool_],
    dict[str, NDArray[np.bool_]],
    list[date],
]:
    """Observed values, the partial and excluded masks, the forecasts and the day labels."""
    n = len(cases)
    observed = np.zeros(n, dtype=bool)
    partial = np.zeros(n, dtype=bool)
    excluded = np.zeros(n, dtype=bool)
    forecasts = {m: np.zeros(n, dtype=bool) for m in methods}
    for i, case in enumerate(cases):
        obs = _observed(case, in_cloud_as_hidden)
        is_partial = case.truth == "partial"
        if (
            case.excluded_because is not None
            or any(case.forecasts.get(m) is None for m in methods)
            or (obs is None and not is_partial)
        ):
            excluded[i] = True
        elif is_partial:
            partial[i] = True
        else:
            observed[i] = bool(obs)
        for m in methods:
            forecasts[m][i] = bool(case.forecasts.get(m))
    return observed, partial, excluded, forecasts, [c.local_day for c in cases]


def _breakdown(
    cases: Sequence[Case], methods: Sequence[str], *, in_cloud_as_hidden: bool
) -> dict[str, int]:
    """Why each frame of a row was or was not scored (each frame counted once, in this order)."""
    counts: Counter[str] = Counter()
    for case in cases:
        if case.excluded_because is not None:
            counts["excluded_truth_is_plate_input"] += 1
        elif case.truth not in P1_TRUTHS:
            counts[f"excluded_truth_{case.truth or 'unclassified'}"] += 1
        elif case.truth == "in_cloud" and not in_cloud_as_hidden:
            counts["excluded_truth_in_cloud_own_class"] += 1
        elif any(case.forecasts.get(m) is None for m in methods):
            counts["excluded_no_forecast"] += 1
        elif case.truth == "partial":
            counts["excluded_truth_partial"] += 1
        else:
            counts["scored"] += 1
    return dict(sorted(counts.items()))


def _short(result: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in result.items() if k not in ("level", "method", "circular", "seed")}


def method_row(
    cases: Sequence[Case],
    method: str,
    *,
    in_cloud_as_hidden: bool,
    seed: int,
    n_boot: int,
    block_length_days: int,
) -> dict[str, Any]:
    """One method's PSS with its day-block bootstrap interval and the frame breakdown."""
    observed, partial, excluded, forecasts, days = _arrays(
        cases, [method], in_cloud_as_hidden=in_cloud_as_hidden
    )
    result = bootstrap_pss(
        forecasts[method],
        observed,
        days,
        seed=seed,
        excluded=excluded,
        partial=partial,
        block_length_days=block_length_days,
        n_boot=n_boot,
    )
    return {
        "method": method,
        "name": METHOD_NAMES[method],
        "frames_in_subset": len(cases),
        "breakdown": _breakdown(cases, [method], in_cloud_as_hidden=in_cloud_as_hidden),
        **_short(result.to_dict()),
    }


def delta_row(
    cases: Sequence[Case],
    reference: str,
    *,
    in_cloud_as_hidden: bool,
    seed: int,
    n_boot: int,
    block_length_days: int,
    model: str = "model",
) -> dict[str, Any]:
    """The paired ΔPSS, ``model`` − ``reference``, on the frames where both forecast."""
    observed, partial, excluded, forecasts, days = _arrays(
        cases, [model, reference], in_cloud_as_hidden=in_cloud_as_hidden
    )
    result = paired_delta_pss(
        forecasts[model],
        forecasts[reference],
        observed,
        days,
        seed=seed,
        excluded=excluded,
        partial=partial,
        block_length_days=block_length_days,
        n_boot=n_boot,
    )
    return {
        "model": model,
        "reference": reference,
        "name": f"{METHOD_NAMES[model]} - {METHOD_NAMES[reference]}",
        "frames_in_subset": len(cases),
        "breakdown": _breakdown(cases, [model, reference], in_cloud_as_hidden=in_cloud_as_hidden),
        **_short(result.to_dict()),
    }


def _no_forecasts(cases: Sequence[Case]) -> dict[str, dict[str, dict[str, int]]]:
    """Frames without a forecast, per method: reason -> local day -> count."""
    out: dict[str, dict[str, dict[str, int]]] = {}
    for method in METHODS:
        by_reason: dict[str, Counter[str]] = {}
        for case in cases:
            reason = case.no_forecast_reasons.get(method)
            if reason is not None:
                by_reason.setdefault(reason, Counter())[case.local_day.isoformat()] += 1
        out[method] = {r: dict(sorted(c.items())) for r, c in sorted(by_reason.items())}
    return out


def _model_scored(case: Case) -> bool:
    return (
        case.excluded_because is None
        and case.forecasts.get("model") is not None
        and case.truth in ("visible", "hidden")
    )


def _frame_counts(cases: Sequence[Case]) -> dict[str, Any]:
    leads = [c.lead_from_availability_h for c in cases if c.lead_from_availability_h is not None]
    days = sorted({c.local_day for c in cases})
    checked = [c for c in cases if c.assets_pass is not None]
    return {
        "daytime_frames": len(cases),
        "with_forecast_run": sum(c.has_forecast_run for c in cases),
        "excluded_truth_is_plate_input": sum(c.excluded_because is not None for c in cases),
        "fitted_assets_checked": len(checked),
        "fitted_assets_pass": sum(bool(c.assets_pass) for c in checked),
        "fitted_assets_pass_model_scored": sum(
            bool(c.assets_pass) and _model_scored(c) for c in checked
        ),
        "plate_off_valid_day": sum(bool(c.plate_off_valid_day) for c in checked),
        "model_scored": sum(_model_scored(c) for c in cases),
        "dev_check_frames_with_dev_check_assets": sum(c.dev_check_asset for c in cases),
        "forecasts_made": {m: sum(c.forecasts.get(m) is not None for c in cases) for m in METHODS},
        "no_forecast_by_reason_and_day": _no_forecasts(cases),
        "truth_classes": dict(sorted(Counter(str(c.truth) for c in cases).items())),
        "render_camera_faults": dict(
            sorted(Counter(c.render_fault for c in cases if c.render_fault).items())
        ),
        "render_white_outs_with_usable_truth": sum(
            c.render_fault in WHITE_OUT_FAULTS and c.truth in ("visible", "hidden") for c in cases
        ),
        "local_days": [d.isoformat() for d in days],
        "n_local_days": len(days),
        "lead_from_availability_h": None
        if not leads
        else {
            "min": round(min(leads), 3),
            "median": round(statistics.median(leads), 3),
            "max": round(max(leads), 3),
        },
    }


def _references(stronger: str) -> list[str]:
    chosen = f"persistence_{stronger}"
    other = "persistence_b" if chosen == "persistence_a" else "persistence_a"
    return [chosen, other, "climatology", "s_rule"]


def _block(
    cases: Sequence[Case],
    *,
    stronger: str,
    in_cloud_as_hidden: bool,
    seed: int,
    n_boot: int,
    block_length_days: int,
) -> dict[str, Any]:
    kwargs = {
        "in_cloud_as_hidden": in_cloud_as_hidden,
        "seed": seed,
        "n_boot": n_boot,
        "block_length_days": block_length_days,
    }
    out: dict[str, Any] = {"pss": {}, "delta": {}}
    for name in SUBSETS:
        chosen = _subset(cases, name)
        out["pss"][name] = [method_row(chosen, m, **kwargs) for m in METHODS]
        out["delta"][name] = [delta_row(chosen, r, **kwargs) for r in _references(stronger)]
    return out


def _other_rule_listing(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Frames whose forecast run differs under the other availability rule (:mod:`.p1dev`)."""
    rows = []
    checked = 0
    other = None
    for record in records:
        alt = record.get("forecast_other_rule")
        if not isinstance(alt, Mapping):
            continue
        checked += 1
        other = alt.get("availability_rule") or other
        this = (record.get("forecast") or {}).get("run")
        if alt.get("run") == this:
            continue
        rows.append(
            {
                "frame": record["frame"]["key"],
                "valid_time_utc": record.get("valid_time_utc"),
                "issue_time_utc": record.get("issue_time_utc"),
                "run": this,
                "available_utc": (record.get("forecast") or {}).get("available_utc"),
                "run_under_other_rule": alt.get("run"),
                "available_under_other_rule_utc": alt.get("available_utc"),
                "truth_class": record["p1"]["truth_class"],
                "model": record["p1"]["forecasts"].get("model"),
                "s_rule": record["p1"]["forecasts"].get("s_rule"),
            }
        )
    return {
        "other_rule": other,
        "frames_checked": checked,
        "frames_whose_run_differs": len(rows),
        "frames_that_lose_their_run": sum(r["run_under_other_rule"] is None for r in rows),
        "frames_that_gain_a_run": sum(r["run"] is None for r in rows),
        "frames": rows,
    }


def _fitted_assets_summary(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Counts of the fitted-asset check over the records (:mod:`.p1dev_assets`)."""
    checks = [
        (r, r["fitted_assets"]) for r in records if isinstance(r.get("fitted_assets"), Mapping)
    ]
    scored = [(r, c) for r, c in checks if _model_scored(case_from_record(r))]

    def counts(items: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]]) -> dict[str, int]:
        return {
            "frames": len(items),
            "pass": sum(bool(c.get("passes")) for _, c in items),
            "plate_fails": sum(not c["plate"]["passes"] for _, c in items),
            "plate_base_at_or_after_issue": sum(
                c["plate"]["base_at_or_after_issue"] > 0 for _, c in items
            ),
            "plate_input_on_valid_local_day": sum(
                c["plate"]["on_valid_local_day"] > 0 for _, c in items
            ),
            "plate_base_on_valid_local_day": sum(
                c["plate"].get("base_on_valid_local_day", 0) > 0 for _, c in items
            ),
            "with_references": sum(c["references"]["n"] > 0 for _, c in items),
            "reference_at_or_after_issue": sum(
                c["references"]["at_or_after_issue"] > 0 for _, c in items
            ),
            "truth_is_plate_input": sum(
                c.get("truth_is_plate_input") is not None for _, c in items
            ),
        }

    rule = next((c.get("rule") for _, c in checks), None)
    return {"rule": rule, "all": counts(checks), "model_scored": counts(scored)}


def build_table(
    records: Sequence[Mapping[str, Any]],
    *,
    stronger: str,
    persistence_choice: Mapping[str, Any],
    provisional_because: Sequence[str],
    seed: int,
    n_boot: int,
    block_length_days: int,
    header: Mapping[str, Any],
) -> dict[str, Any]:
    """The table as a JSON-ready dict (see the module docstring)."""
    if stronger not in ("a", "b"):
        raise ValueError("stronger must be 'a' or 'b'")
    cases = [case_from_record(r) for r in records]
    kwargs = {"stronger": stronger, "seed": seed, "n_boot": n_boot}

    def restricted(keep: Callable[[Case], bool], note: str) -> dict[str, Any]:
        chosen = [c for c in cases if keep(c)]
        return {
            "note": note,
            "frames_kept": len(chosen),
            **_block(
                chosen, in_cloud_as_hidden=False, block_length_days=block_length_days, **kwargs
            ),
        }

    return {
        "kind": "snow-globe.p1dev-table",
        "schema_version": 2,
        "provisional": bool(provisional_because),
        "provisional_because": list(provisional_because),
        **dict(header),
        "event": "summit band visible (PRD §13.4 P1)",
        "bootstrap": {
            "unit": "local calendar day of the valid time",
            "method": "moving blocks, wrapped (circular)",
            "block_length_days": block_length_days,
            "block_length_note": "at least 1 day (PRD §13.4); fitted from development "
            "autocorrelation at the M2 freeze: TODO(M2)",
            "n_boot": n_boot,
            "seed": seed,
            "level": 0.95,
            "interval": "percentile, from order statistics (scores._percentile_interval)",
        },
        "persistence_choice": {
            "stronger": f"persistence_{stronger}",
            "chosen_on": f"{DEV_FIT} weeks only (applied to every subset)",
            **dict(persistence_choice),
        },
        "frames": {name: _frame_counts(_subset(cases, name)) for name in SUBSETS},
        "fitted_assets": _fitted_assets_summary(records),
        "forecast_choice_under_other_rule": _other_rule_listing(records),
        "primary": _block(
            cases, in_cloud_as_hidden=False, block_length_days=block_length_days, **kwargs
        ),
        "sensitivity_in_cloud_as_hidden": _block(
            cases, in_cloud_as_hidden=True, block_length_days=block_length_days, **kwargs
        ),
        "sensitivity_fitted_assets_before_issue": restricted(
            lambda c: bool(c.assets_pass),
            "Only frames whose plate input frames and clear-day references all pass the backtest "
            "pair's fitted-asset rule (taken and available before the issue time, off the valid "
            "local day, not held out, before the fit bound; p1dev_assets).",
        ),
        "sensitivity_plate_off_valid_day": restricted(
            lambda c: bool(c.plate_off_valid_day),
            "Only frames whose valid local day holds none of the plate's input frames (the "
            "plate's base frames share no day's weather with the truth).",
        ),
        "diagnostic_render_white_out_as_not_visible": {
            "note": "Added after the first full run (20261123T1050Z-v0) showed renders of a camera "
            "inside a cloud slab saturating to white, which extractor v0 classes as a camera "
            "fault (D-037), so the primary has the model make no forecast there. Here a "
            "saturated or blank render counts as the model forecasting 'not visible'. A "
            "diagnostic of that renderer limit, never the primary result.",
            **_block(
                faults_as_not_visible(cases),
                in_cloud_as_hidden=False,
                block_length_days=block_length_days,
                **kwargs,
            ),
        },
        "not_computed": [
            {
                "method": "s_mos",
                "why": "TODO(M2): logistic regression on HRRR fields, fitted on development "
                "data at the pre-registration freeze (PRD §13.6, PLAN M2)",
            }
        ],
    }


# --- Markdown --------------------------------------------------------------------------------


def _num(value: Any, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        if math.isnan(value):
            return "n/a"
        return f"{value:+.{digits}f}" if digits == 3 else f"{value:.{digits}f}"
    return str(value)


def _ci(row: Mapping[str, Any]) -> str:
    if row.get("ci_low") is None or row.get("ci_high") is None:
        return "n/a"
    return f"[{row['ci_low']:+.3f}, {row['ci_high']:+.3f}]"


def _pss_lines(rows: Sequence[Mapping[str, Any]], subset: str) -> list[str]:
    out = []
    for row in rows:
        t = row["table"]
        out.append(
            f"| {subset} | {row['method']} | {row['n_pairs']} | {row['n_days_with_pairs']} | "
            f"{t['hits']} | {t['misses']} | {t['false_alarms']} | {t['correct_negatives']} | "
            f"{_num(t['pod'], 2)} | {_num(t['pofd'], 2)} | {_num(row['pss'])} | {_ci(row)} | "
            f"{t['partial']} | {row['breakdown'].get('excluded_truth_in_cloud_own_class', 0)} | "
            f"{row['breakdown'].get('excluded_no_forecast', 0)} | {_flags(row)} |"
        )
    return out


def _delta_lines(rows: Sequence[Mapping[str, Any]], subset: str) -> list[str]:
    out = []
    for row in rows:
        excludes = row.get("excludes_zero")
        out.append(
            f"| {subset} | {row['reference']} | {row['n_pairs']} | {row['n_days_with_pairs']} | "
            f"{_num(row['pss_model'])} | {_num(row['pss_reference'])} | {_num(row['delta'])} | "
            f"{_ci(row)} | {_num(row['p_value'], 2)} | "
            f"{'n/a' if excludes is None else ('yes' if excludes else 'no')} | {_flags(row)} |"
        )
    return out


_PSS_HEAD = (
    "| subset | method | n frames | n days | H | M | F | C | POD | POFD | PSS | 95 % CI | "
    "partial (excl.) | in cloud (own class) | no forecast | bootstrap |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
)
_DELTA_HEAD = (
    "| subset | model - reference | n frames | n days | PSS model | PSS ref | ΔPSS | 95 % CI | "
    "p | CI excludes 0 | bootstrap |\n|---|---|---|---|---|---|---|---|---|---|---|"
)


def _flags(row: Mapping[str, Any]) -> str:
    """The bootstrap's caveats for a row: undefined resamples, and few days."""
    notes = []
    if row.get("n_undefined"):
        notes.append(f"{row['n_undefined']}/{row['n_boot']} resamples undefined")
    days = row.get("n_days_with_pairs") or 0
    if 0 < days < 5:
        notes.append(f"only {days} day block(s) to resample")
    return "; ".join(notes) or "-"


def _section(block: Mapping[str, Any]) -> list[str]:
    lines = ["**PSS per method**", "", _PSS_HEAD]
    for subset in SUBSETS:
        lines += _pss_lines(block["pss"][subset], subset)
    lines += ["", "**Paired ΔPSS, the model minus each baseline (same frames, same masks)**", ""]
    lines.append(_DELTA_HEAD)
    for subset in SUBSETS:
        lines += _delta_lines(block["delta"][subset], subset)
    return lines


def _headline(table: Mapping[str, Any]) -> str:
    """The first sentence: what keeps the table provisional, from the extractor's validation."""
    validation = (table.get("extractor") or {}).get("validation") or {}
    name = (table.get("extractor") or {}).get("extractor")
    if not table.get("provisional"):
        return "**Not provisional:** no reason is listed."
    if validation.get("publishable") is True:
        return (
            f"**Provisional: not a result.** Extractor {name} meets its PRD §13.8 bar, but the "
            "reasons below keep this dev-only table provisional:"
        )
    if validation:
        return (
            f"**Provisional: not a result.** Extractor {name} is below its PRD §13.8 bar, so no "
            "number here counts toward a metric or a claim. Why:"
        )
    return "**Provisional: not a result.** Why:"


def _no_forecast_lines(frames: Mapping[str, Any]) -> list[str]:
    lines = []
    for method, reasons in (frames.get("no_forecast_by_reason_and_day") or {}).items():
        for reason, days in reasons.items():
            per_day = ", ".join(f"{day} ({n})" for day, n in days.items())
            lines.append(f"- {method}, {reason}: {sum(days.values())} frames: {per_day}")
    return lines or ["- every method forecast every frame"]


def _first_day_line(frames: Mapping[str, Any]) -> str | None:
    days = frames.get("local_days") or []
    if not days:
        return None
    first = days[0]
    parts = []
    for method in ("persistence_a", "persistence_b", "climatology"):
        reasons = (frames.get("no_forecast_by_reason_and_day") or {}).get(method) or {}
        missing = sum(day_counts.get(first, 0) for day_counts in reasons.values())
        if missing:
            why = "; ".join(r for r, d in reasons.items() if d.get(first))
            parts.append(f"{method} none on {missing} frames ({why})")
    if not parts:
        return None
    return (
        f"- First local day {first}: " + ", ".join(parts) + ". Nothing captured before their "
        "issue times could make these forecasts (PRD §13.1)."
    )


def _other_rule_lines(table: Mapping[str, Any]) -> list[str]:
    listing = table.get("forecast_choice_under_other_rule") or {}
    if not listing.get("frames_checked"):
        return []
    lines = [
        f"- Under the other availability rule (`{listing.get('other_rule')}`), "
        f"{listing['frames_whose_run_differs']} of {listing['frames_checked']} frames would use "
        f"another run ({listing['frames_that_lose_their_run']} would have none, "
        f"{listing['frames_that_gain_a_run']} would gain one):"
    ]
    for row in listing.get("frames") or []:
        lines.append(
            f"  - `{row['frame'].rsplit('/', 1)[-1]}`: {row['run']} (available "
            f"{row['available_utc']}) here, {row['run_under_other_rule']} (available "
            f"{row['available_under_other_rule_utc']}) under the other; truth "
            f"{row['truth_class']}, model {row['model']}, S_rule {row['s_rule']}"
        )
    return lines


def _assets_lines(table: Mapping[str, Any]) -> list[str]:
    summary = table.get("fitted_assets") or {}
    every, scored = summary.get("all") or {}, summary.get("model_scored") or {}
    if not every.get("frames"):
        return []

    def text(c: Mapping[str, Any]) -> str:
        return (
            f"{c['pass']} of {c['frames']} pass; the plate fails for {c['plate_fails']} "
            f"({c['plate_base_at_or_after_issue']} with base frames at or after the issue time, "
            f"{c['plate_input_on_valid_local_day']} with an input frame on the valid local day, "
            f"{c.get('plate_base_on_valid_local_day', 0)} of them a base frame); "
            f"{c['reference_at_or_after_issue']} of {c['with_references']} frames with "
            f"references have one at or after the issue time; {c['truth_is_plate_input']} truth "
            "frames are plate inputs (excluded from P1)"
        )

    return [
        f"- Fitted assets, by {summary.get('rule')}: all frames: {text(every)}. Model-scored "
        f"frames: {text(scored)}. The primary uses these assets anyway (the brief's plate v0.1 "
        "and extractor v0's whole clear-day pool) until the lead rules; the sensitivity blocks "
        "below restrict to the frames that pass.",
    ]


def to_markdown(table: Mapping[str, Any]) -> str:
    """The table as Markdown, headed by its provisional status."""
    frames = table["frames"]
    availability = table.get("availability") or {}
    first_day = _first_day_line(frames["all"])
    lines = [
        f"# Dev-only P1 table, run {table.get('run_id')}"
        + (": PROVISIONAL" if table.get("provisional") else ""),
        "",
        _headline(table),
        "",
        *(f"- {reason}" for reason in table["provisional_because"]),
        "",
        f"- Viewpoint `{table.get('viewpoint_id')}`; event: {table['event']}; nominal lead "
        f"{table.get('lead_nominal_h')} h (issue = valid - lead; lead from availability, D-008, "
        "in the frame counts).",
        f"- Forecast availability rule `{availability.get('rule')}`: "
        f"{availability.get('description')}.",
        *_other_rule_lines(table),
        f"- Extractor {table.get('extractor', {}).get('extractor')} "
        f"(params {table.get('extractor', {}).get('tau')}); renderer "
        f"{table.get('renderer', {}).get('renderer_version')} "
        f"({table.get('renderer', {}).get('backend')}); plate "
        f"{table.get('plate', {}).get('plate_id')}.",
        f"- Held-out split v{table.get('split', {}).get('version')} "
        f"(sha256 {str(table.get('split', {}).get('sha256'))[:12]}): every frame passed the "
        "guard; frames refused without being opened: "
        f"{table.get('refused_by_the_guard')}; frames the extractor could not read: "
        f"{table.get('unreadable_frames', 0)}.",
        "- Baselines' labels are made as of each issue time: every frame a baseline reads is "
        "classified with clear-day references imaged and available by the issue time "
        "(p1dev_baselines.AsOfLabelStore, PRD §13.1).",
        *_assets_lines(table),
        *([first_day] if first_day else []),
        f"- Dev weeks: {table.get('dev_split_rule')}",
        f"- Bootstrap: {table['bootstrap']['method']} of {table['bootstrap']['block_length_days']} "
        f"day(s), {table['bootstrap']['n_boot']} resamples, seed {table['bootstrap']['seed']}; "
        "the effective n is the days.",
        f"- Stronger persistence: `{table['persistence_choice']['stronger']}`, chosen on "
        f"{table['persistence_choice']['chosen_on']} "
        f"(PSS a {_num(table['persistence_choice'].get('pss_a'))}, b "
        f"{_num(table['persistence_choice'].get('pss_b'))} on "
        f"{table['persistence_choice'].get('n_paired')} paired frames; flags: "
        f"{', '.join(table['persistence_choice'].get('flags') or []) or 'none'}).",
        "",
        "## Frames",
        "",
        "| | " + " | ".join(SUBSETS) + " |",
        "|---|" + "---|" * len(SUBSETS),
    ]

    def row(label: str, values: Sequence[Any]) -> str:
        return f"| {label} | " + " | ".join(str(v) for v in values) + " |"

    lines.append(
        row("daytime frames (Sun >= 3 deg)", [frames[s]["daytime_frames"] for s in SUBSETS])
    )
    lines.append(row("with a hindcast run", [frames[s]["with_forecast_run"] for s in SUBSETS]))
    lines.append(row("local days", [frames[s]["n_local_days"] for s in SUBSETS]))
    lines.append(
        row(
            "excluded: truth is a plate input frame",
            [frames[s].get("excluded_truth_is_plate_input", 0) for s in SUBSETS],
        )
    )
    lines.append(
        row(
            "fitted assets pass (backtest pair's rule)",
            [frames[s].get("fitted_assets_pass", 0) for s in SUBSETS],
        )
    )
    lines.append(row("model-scored frames", [frames[s].get("model_scored", 0) for s in SUBSETS]))
    truth_names = sorted({k for s in SUBSETS for k in frames[s]["truth_classes"]})
    for name in truth_names:
        lines.append(
            row(f"truth {name}", [frames[s]["truth_classes"].get(name, 0) for s in SUBSETS])
        )
    for method in METHODS:
        lines.append(
            row(f"forecasts by {method}", [frames[s]["forecasts_made"][method] for s in SUBSETS])
        )
    lines.append(
        row(
            "renders the extractor calls a camera fault",
            [sum(frames[s]["render_camera_faults"].values()) for s in SUBSETS],
        )
    )
    lines.append(
        row(
            "lead from availability, h (min/median/max)",
            [
                "n/a"
                if frames[s]["lead_from_availability_h"] is None
                else "/".join(
                    str(frames[s]["lead_from_availability_h"][k]) for k in ("min", "median", "max")
                )
                for s in SUBSETS
            ],
        )
    )
    lines += [
        "",
        "## Frames without a forecast, by method, reason and local day",
        "",
        *_no_forecast_lines(frames["all"]),
        "",
        "## Primary: partial excluded and counted, in-cloud its own class",
        "",
        *_section(table["primary"]),
        "",
        "## Sensitivity: in-cloud counted as hidden",
        "",
        *(
            [
                "No truth frame is in cloud in this run, so these rows equal the primary's.",
                "",
            ]
            if not any(frames[s]["truth_classes"].get("in_cloud") for s in SUBSETS)
            else []
        ),
        *_section(table["sensitivity_in_cloud_as_hidden"]),
        "",
    ]
    for name, title in (
        ("sensitivity_fitted_assets_before_issue", "fitted assets before the issue time"),
        ("sensitivity_plate_off_valid_day", "no plate input frame on the valid local day"),
    ):
        if name in table:
            block = table[name]
            lines += [
                f"## Sensitivity: {title}",
                "",
                f"{block['note']} Frames kept: {block['frames_kept']}.",
                "",
                *_section(block),
                "",
            ]
    lines += [
        *(
            [
                "## Diagnostic: white-out renders read as 'not visible' (not the primary)",
                "",
                table["diagnostic_render_white_out_as_not_visible"]["note"],
                f"Renders affected: {frames['all']['render_camera_faults']}; with a visible or "
                f"hidden truth: {frames['all']['render_white_outs_with_usable_truth']}.",
                "",
                *_section(table["diagnostic_render_white_out_as_not_visible"]),
                "",
            ]
            if "diagnostic_render_white_out_as_not_visible" in table
            else []
        ),
        "## Not computed",
        "",
        *(f"- {item['method']}: {item['why']}" for item in table["not_computed"]),
        "",
        "## Commands (every invocation of this run, oldest first)",
        "",
        *(
            f"- {c.get('started_utc')} ({'table only' if c.get('table_only') else 'frames'}): "
            f"`{c.get('command_line')}`"
            for c in table.get("commands", [])
        ),
        "",
    ]
    return "\n".join(lines)
