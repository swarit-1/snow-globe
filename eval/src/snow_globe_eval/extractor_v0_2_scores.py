"""Scoring extractor classes against the merged labels: forms, days, bootstrap (PRD §13.8, §13.7).

The bar (PRD §13.8) is landmark-visibility F1 ≥ 0.85. Its event is *visible*.

**Primary form** (``primary``), per band, over every labelled frame:

- truth: the merged label (:mod:`.extractor_v0_2_labels`). ``visible`` is the event;
  ``hidden`` and ``in_cloud`` are not; ``partial`` and ``unusable`` are left out (P1 leaves
  partial out, PRD §13.4), and so is a band the labellers disagree on (counted);
- prediction: the event when the extractor's class for the band is ``visible``. Every other
  class is "not visible", including a frame the extractor does not class (night, camera
  fault, excluded, in cloud, no reference, no τ): an abstention on a visible summit is a miss.

F1 = 2H / (2H + M + F), precision = H / (H + F), recall (POD) = H / (H + M), each None when
its denominator is 0. Pooled scores add the days' tables.

**Other forms**, reported beside it: ``scored_only`` (frames the extractor did not class for
the band left out), ``partial_as_not_visible`` (label partial counts as not visible),
``in_cloud_excluded`` (label in_cloud left out), and the disagreement sensitivity: each band
the labellers disagree on scored with labeller ``a``'s label, with ``b``'s, and with whichever
of the two is worse (``worst``) or better (``best``) for the extractor's F1 (per frame, a false
alarm or a miss when one label gives it, else the label that leaves the frame out).

**Bootstrap** (PRD §13.7): whole local days resampled with replacement (moving blocks of one
day, :func:`.scores.block_bootstrap_indices`), the days' tables summed and F1 recomputed;
resamples with F1 undefined (no event and no false alarm) are dropped and counted. The interval
is the k-th smallest and k-th largest of the values, k = ceil(B · α / 2). The folds' fits are
not redone inside a resample, so the interval covers the day-to-day spread of the score, not
of the fit. With few days the resampled values take few distinct values; their count is given.

**Camera in cloud** (:func:`camera_in_cloud`, PRD §13.4): the extractor's frame class in_cloud
against the merged label in_cloud, counted apart from the F1.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from .extractor_v0_2_labels import MergedLabels
from .extractor_v0_2_lodo import Prediction, truth_visible_event
from .scores import block_bootstrap_indices

__all__ = [
    "FORMS",
    "IN_CLOUD_COUNTS",
    "BootstrapResult",
    "bootstrap_f1",
    "camera_in_cloud",
    "confusion",
    "day_tables",
    "paired_bootstrap_delta_f1",
    "table_scores",
]

Form = Literal[
    "primary",
    "scored_only",
    "partial_as_not_visible",
    "in_cloud_excluded",
    "disagreements_as_a",
    "disagreements_as_b",
    "disagreements_worst",
    "disagreements_best",
]
FORMS: tuple[Form, ...] = (
    "primary",
    "scored_only",
    "partial_as_not_visible",
    "in_cloud_excluded",
    "disagreements_as_a",
    "disagreements_as_b",
    "disagreements_worst",
    "disagreements_best",
)
#: Extractor classes that mean "the band was classed" (anything else is an abstention).
CLASSED = frozenset({"visible", "partial", "hidden"})


def _truth(label: str | None, form: Form) -> bool | None:
    if form == "partial_as_not_visible" and label == "partial":
        return False
    if form == "in_cloud_excluded" and label == "in_cloud":
        return None
    return truth_visible_event(label)


def _resolve(labels: MergedLabels, key: str, band: str, form: Form, predicted: bool) -> bool | None:
    """The truth of one frame's band under ``form`` (None: left out)."""
    frame = labels.frames[key]
    merged = frame.bands.get(band)
    if merged is not None or form in ("primary", "scored_only"):
        return _truth(merged, "primary" if form == "scored_only" else form)
    if form not in (
        "disagreements_as_a",
        "disagreements_as_b",
        "disagreements_worst",
        "disagreements_best",
    ):
        return _truth(merged, form)
    if form == "disagreements_as_a":
        return truth_visible_event(frame.label(band, "a"))
    if form == "disagreements_as_b":
        return truth_visible_event(frame.label(band, "b"))
    options = {truth_visible_event(frame.label(band, w)) for w in ("a", "b")}
    if form == "disagreements_worst":
        order = [False, None, True] if predicted else [True, False, None]
    else:
        order = [True, None, False] if predicted else [False, None, True]
    return next(o for o in order if o in options)


def day_tables(
    predictions: Mapping[str, Prediction],
    labels: MergedLabels,
    band: str,
    form: Form,
) -> tuple[dict[str, list[int]], dict[str, Any]]:
    """Per local day, [H, M, F, C] for the event visible under ``form``, and what was left out."""
    tables: dict[str, list[int]] = {}
    left_out: Counter[str] = Counter()
    for key in sorted(predictions):
        pred = predictions[key]
        if key not in labels.frames:
            continue
        cls = pred.classes.get(band)
        if form == "scored_only" and cls not in CLASSED:
            left_out["not classed by the extractor"] += 1
            continue
        predicted = cls == "visible"
        truth = _resolve(labels, key, band, form, predicted)
        table = tables.setdefault(pred.local_day, [0, 0, 0, 0])
        if truth is None:
            merged = labels.frames[key].bands.get(band)
            left_out["labellers disagree" if merged is None else f"label {merged}"] += 1
            continue
        index = (0 if predicted else 1) if truth else (2 if predicted else 3)
        table[index] += 1
    return tables, dict(left_out)


def _ratio(num: float, den: float) -> float | None:
    return None if den == 0 else round(num / den, 4)


def table_scores(table: Sequence[int]) -> dict[str, Any]:
    """Counts and scores of one [H, M, F, C] table."""
    h, m, f, c = (int(v) for v in table)
    pod = _ratio(h, h + m)
    pofd = _ratio(f, f + c)
    return {
        "n": h + m + f + c,
        "hits": h,
        "misses": m,
        "false_alarms": f,
        "correct_negatives": c,
        "f1": _ratio(2 * h, 2 * h + m + f),
        "precision": _ratio(h, h + f),
        "recall": pod,
        "pofd": pofd,
        "pss": None if pod is None or pofd is None else round(pod - pofd, 4),
    }


def _f1_vector(counts: NDArray[np.int64]) -> NDArray[np.float64]:
    h, m, f = (counts[..., i].astype(float) for i in range(3))
    den = 2 * h + m + f
    with np.errstate(invalid="ignore", divide="ignore"):
        out = 2 * h / den
    out[den == 0] = np.nan
    return out


class BootstrapResult(dict[str, Any]):
    """A JSON-ready dict: the point value, the interval, B, undefined and distinct counts."""


def _interval(values: NDArray[np.float64], level: float) -> tuple[float | None, float | None]:
    if values.size == 0:
        return None, None
    ordered = np.sort(values)
    k = max(1, math.ceil(round(ordered.size * (1.0 - level) / 2.0, 9)))
    k = min(k, ordered.size)
    return float(ordered[k - 1]), float(ordered[ordered.size - k])


def _resampled(
    per_day: Sequence[NDArray[np.int64]], n_boot: int, seed: int
) -> list[NDArray[np.int64]]:
    n_days = per_day[0].shape[0]
    rng = np.random.default_rng(seed)
    idx = block_bootstrap_indices(n_days, 1, n_boot, rng)
    flat = (idx + n_days * np.arange(n_boot)[:, None]).ravel()
    multiplicity = np.bincount(flat, minlength=n_boot * n_days).reshape(n_boot, n_days)
    return [multiplicity @ table for table in per_day]


def bootstrap_f1(
    tables: Mapping[str, Sequence[int]],
    *,
    days: Sequence[str],
    n_boot: int = 10_000,
    seed: int = 20261123,
    level: float = 0.95,
) -> BootstrapResult:
    """Day-block bootstrap interval of the pooled F1 (module docstring)."""
    per_day = np.array([list(tables.get(d, [0, 0, 0, 0])) for d in days], dtype=np.int64)
    point = _f1_vector(per_day.sum(axis=0)[None, :])[0]
    (sums,) = _resampled([per_day], n_boot, seed)
    values = _f1_vector(sums)
    defined = values[~np.isnan(values)]
    lo, hi = _interval(defined, level)
    return BootstrapResult(
        f1=None if math.isnan(point) else round(float(point), 4),
        interval=[None if lo is None else round(lo, 4), None if hi is None else round(hi, 4)],
        level=level,
        n_boot=n_boot,
        seed=seed,
        days=list(days),
        block_length_days=1,
        undefined_resamples=int(np.isnan(values).sum()),
        distinct_values=int(np.unique(np.round(defined, 9)).size),
        share_at_or_above_bar=None
        if defined.size == 0
        else round(float(np.mean(defined >= 0.85)), 4),
    )


def paired_bootstrap_delta_f1(
    first: Mapping[str, Sequence[int]],
    second: Mapping[str, Sequence[int]],
    *,
    days: Sequence[str],
    n_boot: int = 10_000,
    seed: int = 20261123,
    level: float = 0.95,
) -> BootstrapResult:
    """Pooled F1(first) − F1(second) on the same resampled days (paired)."""
    a = np.array([list(first.get(d, [0, 0, 0, 0])) for d in days], dtype=np.int64)
    b = np.array([list(second.get(d, [0, 0, 0, 0])) for d in days], dtype=np.int64)
    point = _f1_vector(a.sum(axis=0)[None, :])[0] - _f1_vector(b.sum(axis=0)[None, :])[0]
    sums_a, sums_b = _resampled([a, b], n_boot, seed)
    values = _f1_vector(sums_a) - _f1_vector(sums_b)
    defined = values[~np.isnan(values)]
    lo, hi = _interval(defined, level)
    return BootstrapResult(
        delta_f1=None if math.isnan(point) else round(float(point), 4),
        interval=[None if lo is None else round(lo, 4), None if hi is None else round(hi, 4)],
        level=level,
        n_boot=n_boot,
        seed=seed,
        undefined_resamples=int(np.isnan(values).sum()),
        share_above_zero=None if defined.size == 0 else round(float(np.mean(defined > 0)), 4),
    )


def confusion(
    predictions: Mapping[str, Prediction], labels: MergedLabels, band: str
) -> dict[str, dict[str, int]]:
    """Merged label (``disagree`` where the labellers differ) against the extractor's class."""
    table: dict[str, Counter[str]] = {}
    for key, pred in predictions.items():
        if key not in labels.frames:
            continue
        merged = labels.frames[key].bands.get(band)
        row = "disagree" if merged is None else merged
        table.setdefault(row, Counter())[str(pred.classes.get(band))] += 1
    return {row: dict(sorted(cells.items())) for row, cells in sorted(table.items())}


#: The counts :func:`camera_in_cloud` returns, in order.
IN_CLOUD_COUNTS = (
    "labelled_and_flagged",
    "labelled_not_flagged",
    "labelled_not_tested",
    "flagged_not_labelled",
    "neither",
    "not_labelled_not_tested",
    "labellers_split_one_in_cloud",
)


def camera_in_cloud(
    predictions: Mapping[str, Prediction], labels: MergedLabels, band: str
) -> dict[str, Any]:
    """The frame class ``in_cloud`` against the merged label ``in_cloud`` of ``band`` (§13.4).

    PRD §13.4 makes frames from inside cloud a class of their own, kept out of P1's primary
    and counted as hidden only in a sensitivity analysis, so how often an extractor flags them
    matters beside its visibility F1 (where in_cloud counts as not visible). A frame is
    *tested* when its record carries both ratios of the in-cloud test (whole-frame energy and
    RMS contrast against its references); frames the method classes night, camera fault or
    excluded, or that have no reference, are counted apart, as are frames whose labellers
    split on the band with one of them saying in_cloud.
    """
    counts: Counter[str] = Counter()
    for key in sorted(predictions):
        if key not in labels.frames:
            continue
        frame = labels.frames[key]
        merged = frame.bands.get(band)
        if merged is None:
            if "in_cloud" in (frame.label(band, "a"), frame.label(band, "b")):
                counts["labellers_split_one_in_cloud"] += 1
            continue
        record = predictions[key].record
        labelled = merged == "in_cloud"
        flagged = record.get("frame_class") == "in_cloud"
        tested = (
            record.get("global_energy_ratio") is not None
            and record.get("contrast_ratio") is not None
        )
        if not tested:
            counts["labelled_not_tested" if labelled else "not_labelled_not_tested"] += 1
        elif labelled:
            counts["labelled_and_flagged" if flagged else "labelled_not_flagged"] += 1
        else:
            counts["flagged_not_labelled" if flagged else "neither"] += 1
    out: dict[str, Any] = {"band": band, **{name: counts[name] for name in IN_CLOUD_COUNTS}}
    labelled_total = (
        out["labelled_and_flagged"] + out["labelled_not_flagged"] + out["labelled_not_tested"]
    )
    out["labelled_in_cloud"] = labelled_total
    out["flagged_share_of_labelled"] = (
        None if labelled_total == 0 else round(out["labelled_and_flagged"] / labelled_total, 4)
    )
    return out


def score_method(
    predictions: Mapping[str, Prediction],
    labels: MergedLabels,
    bands: Sequence[str],
    *,
    n_boot: int = 10_000,
    seed: int = 20261123,
    forms: Sequence[Form] = FORMS,
    on_band: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Every form's pooled and per-day scores per band, with the primary form's bootstrap."""
    days = sorted({p.local_day for p in predictions.values()})
    out: dict[str, Any] = {"days": days, "bands": {}}
    for band in bands:
        if on_band is not None:
            on_band(band)
        item: dict[str, Any] = {}
        for form in forms:
            tables, left_out = day_tables(predictions, labels, band, form)
            pooled = [sum(t[i] for t in tables.values()) for i in range(4)]
            item[form] = {
                "pooled": table_scores(pooled),
                "per_day": {d: table_scores(tables.get(d, [0, 0, 0, 0])) for d in days},
                "left_out": left_out,
                "tables": {d: tables.get(d, [0, 0, 0, 0]) for d in days},
            }
        item["bootstrap_primary"] = bootstrap_f1(
            item["primary"]["tables"], days=days, n_boot=n_boot, seed=seed
        )
        item["confusion"] = confusion(predictions, labels, band)
        out["bands"][band] = item
    return out
