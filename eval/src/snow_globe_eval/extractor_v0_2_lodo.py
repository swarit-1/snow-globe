"""Leave-one-day-out fits for extractor v0.2, and v0 on the same folds (PRD §13.8, §13.4, §13.9).

A **fold** holds out one local day: everything fitted (the reference pools and τ) comes from
the other days' frames and labels, and the held-out day's frames are then classed. Every day is
held out once, so every labelled frame gets exactly one out-of-day class per method.

Inside a fold, τ is fitted on the training days' frames, each classed the way a held-out frame
is: against references from neither its own day nor the held-out day (nested), so a training
ratio is never flattered by a same-day reference.

**τ fit** (:func:`fit_threshold`), per band: τ_hi maximises F1 of the event *visible* over the
training frames the extractor scores (labels: visible is the event, hidden and in_cloud are not,
partial, unusable and disagreements are left out), predicting visible when ratio ≥ τ_hi. Every
τ between two neighbouring training ratios gives the same F1, so ties are broken by the widest
gap in log ratio, and τ is the geometric mean of that gap's ends (the largest margin on the
ratio's multiplicative scale). τ_lo is fitted the same way for the event *hidden* (hidden and
in_cloud against visible and partial), predicting hidden when ratio < τ_lo, among values not
above τ_hi; with none, τ_lo = τ_hi (no partial band). A band whose training frames hold no
event, or no non-event, gets no τ, and its held-out frames no class.

**Methods**, all on the same folds and labels:

- ``v0.2``: :mod:`.extractor_v0_2` with the contrast model (the pre-registered primary);
- ``v0.2-A``: the same with no contrast model (references conditioned on D and Sun altitude
  only; the pre-registered ablation);
- ``v0``: extractor v0 as built (:mod:`.extractor_v0`): its clear-day pool from labels v0.1,
  the Sun limits, the fixed τ 0.6 and 0.35; only the held-out day is kept out of its pool;
- ``v0-fitted-tau``: v0's ratios with τ fitted per fold by the rule above.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal

from snow_globe_common.timeutil import parse_iso_utc

from .extractor import classify_ratio
from .extractor_v0 import BandEnergies, ReferenceFrameV0, V0Params, classify_frame
from .extractor_v0_2 import (
    FrameResultV02,
    Illumination,
    PoolFrame,
    Thresholds,
    V02Params,
    classify_frame_v02,
)
from .extractor_v0_2_labels import MergedLabels

__all__ = [
    "METHODS",
    "FoldFit",
    "FrameFeatures",
    "MethodSpec",
    "Prediction",
    "ThresholdFit",
    "fit_threshold",
    "folds",
    "run_lodo",
    "truth_hidden_event",
    "truth_visible_event",
    "v0_pool",
    "v02_pools",
]

Direction = Literal["above", "below"]
#: A frame's class and band ratios under one fold's references (no τ yet).
type RatioOf = Callable[[FrameFeatures], tuple[str, dict[str, float | None]]]


@dataclass(frozen=True)
class FrameFeatures:
    """What every method needs about one labelled frame, computed once (and cached)."""

    key: str
    sha256: str
    image_time_utc: str
    local_day: str
    sun_alt_deg: float
    sun_az_deg: float
    energies: BandEnergies
    light: Illumination
    gate_failure: str | None = None
    fit_allowed: bool = True
    gates: dict[str, Any] = field(default_factory=dict)
    same_view_shift: dict[str, Any] | None = None

    @property
    def image_time(self) -> datetime:
        """The image time as an aware datetime."""
        return parse_iso_utc(self.image_time_utc)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form (the cache line)."""
        return {
            "key": self.key,
            "sha256": self.sha256,
            "image_time_utc": self.image_time_utc,
            "local_day": self.local_day,
            "sun_alt_deg": self.sun_alt_deg,
            "sun_az_deg": self.sun_az_deg,
            "energies": self.energies.to_dict(),
            "light": self.light.to_dict(),
            "gate_failure": self.gate_failure,
            "fit_allowed": self.fit_allowed,
            "gates": self.gates,
            "same_view_shift": self.same_view_shift,
        }

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> FrameFeatures:
        """Inverse of :meth:`to_dict`."""
        e = doc["energies"]
        energies = BandEnergies(
            bands={k: float(v) for k, v in e["bands"].items()},
            global_energy=float(e["global_energy"]),
            rms_contrast=float(e["rms_contrast"]),
            dark_fraction=float(e["dark_fraction"]),
            bright_fraction=float(e["bright_fraction"]),
            luma_range=float(e["luma_range"]),
            analysis_size=(int(e["analysis_size"][0]), int(e["analysis_size"][1])),
        )
        return cls(
            key=str(doc["key"]),
            sha256=str(doc["sha256"]),
            image_time_utc=str(doc["image_time_utc"]),
            local_day=str(doc["local_day"]),
            sun_alt_deg=float(doc["sun_alt_deg"]),
            sun_az_deg=float(doc["sun_az_deg"]),
            energies=energies,
            light=Illumination.from_dict(doc["light"]),
            gate_failure=doc.get("gate_failure"),
            fit_allowed=bool(doc.get("fit_allowed", True)),
            gates=dict(doc.get("gates") or {}),
            same_view_shift=doc.get("same_view_shift"),
        )


def truth_visible_event(label: str | None) -> bool | None:
    """The bar's event: visible is True; hidden and in_cloud False; anything else left out."""
    if label == "visible":
        return True
    if label in ("hidden", "in_cloud"):
        return False
    return None


def truth_hidden_event(label: str | None) -> bool | None:
    """τ_lo's event: hidden and in_cloud True; visible and partial False; else left out."""
    if label in ("hidden", "in_cloud"):
        return True
    if label in ("visible", "partial"):
        return False
    return None


@dataclass(frozen=True)
class ThresholdFit:
    """One fitted threshold, the F1 it reached and the gap it sits in."""

    tau: float | None
    f1: float | None
    gap: tuple[float | None, float | None]
    n: int
    events: int
    why_none: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {
            "tau": None if self.tau is None else round(self.tau, 6),
            "f1": None if self.f1 is None else round(self.f1, 4),
            "gap": [None if v is None else round(v, 6) for v in self.gap],
            "n": self.n,
            "events": self.events,
            "why_none": self.why_none,
        }


def _f1(h: int, m: int, f: int) -> float:
    return 2 * h / (2 * h + m + f) if (2 * h + m + f) else 0.0


def fit_threshold(
    samples: Sequence[tuple[float, bool]],
    *,
    direction: Direction,
    upper_bound: float | None = None,
) -> ThresholdFit:
    """Fit one threshold by maximum F1, ties broken by the widest log-ratio gap (docstring).

    ``samples`` are (ratio, event) pairs. ``direction="above"`` predicts the event when
    ratio ≥ τ; ``"below"`` when ratio < τ. With ``upper_bound``, only τ ≤ it are allowed.
    """
    usable = [(r, e) for r, e in samples if math.isfinite(r) and r > 0]
    events = sum(1 for _, e in usable if e)
    if not usable or events == 0 or events == len(usable):
        why = "no training frames" if not usable else "training frames hold one class only"
        return ThresholdFit(None, None, (None, None), len(usable), events, why)
    values = sorted({r for r, _ in usable})
    best: tuple[float, float, float, float, float] | None = None  # f1, width, tau, lo, hi
    for j in range(len(values) - 1):
        lo, hi = values[j], values[j + 1]
        tau = math.sqrt(lo * hi)
        if upper_bound is not None and tau > upper_bound:
            continue
        h = m = f = 0
        for r, e in usable:
            predicted = r >= hi if direction == "above" else r <= lo
            h += predicted and e
            m += (not predicted) and e
            f += predicted and not e
        score = _f1(h, m, f)
        width = math.log(hi / lo)
        candidate = (score, width, tau, lo, hi)
        if best is None or (score, width) > (best[0], best[1]):
            best = candidate
    if best is None:
        return ThresholdFit(None, None, (None, None), len(usable), events, "no allowed gap")
    return ThresholdFit(best[2], best[0], (best[3], best[4]), len(usable), events)


@dataclass(frozen=True)
class MethodSpec:
    """One method of the comparison."""

    name: str
    family: Literal["v0.2", "v0"]
    fit_tau: bool
    v02: V02Params | None = None
    v0: V0Params | None = None


METHODS: tuple[MethodSpec, ...] = (
    MethodSpec("v0.2", "v0.2", True, v02=V02Params()),
    MethodSpec("v0.2-A", "v0.2", True, v02=V02Params(contrast_model=False)),
    MethodSpec("v0", "v0", False, v0=V0Params()),
    MethodSpec("v0-fitted-tau", "v0", True, v0=V0Params()),
)


def _pool_eligible(feat: FrameFeatures, min_sun_alt_deg: float) -> bool:
    return (
        feat.fit_allowed
        and feat.gate_failure is None
        and feat.sun_alt_deg >= min_sun_alt_deg
        and feat.light.d_index is not None
    )


def v02_pools(
    features: Mapping[str, FrameFeatures],
    labels: MergedLabels,
    order: Sequence[str],
    params: V02Params,
) -> dict[str, list[PoolFrame]]:
    """Per band, the frames whose merged label is visible and that may enter a fitted asset.

    A frame whose Ẽ is undefined (C(D) under the floor) cannot serve a skyline band, and a
    frame whose D is undefined serves no band, since a reference's distance needs its D.
    """
    from .extractor_v0 import camera_fault
    from .extractor_v0_2 import normalised_energy

    pools: dict[str, list[PoolFrame]] = {band: [] for band in order}
    for key, feat in sorted(features.items()):
        if key not in labels.frames or not _pool_eligible(feat, params.min_sun_alt_deg):
            continue
        if camera_fault(feat.energies, params.energy_params()) is not None:
            continue
        for band in order:
            if band not in labels.bands or labels.frames[key].bands.get(band) != "visible":
                continue
            energy = feat.energies.bands[band]
            norm = normalised_energy(band, energy, feat.light, params)
            if norm is None or feat.light.d_index is None:
                continue
            pools[band].append(
                PoolFrame(
                    key=key,
                    local_day=feat.local_day,
                    sun_alt_deg=feat.sun_alt_deg,
                    d_index=feat.light.d_index,
                    energy=energy,
                    normalised=norm,
                    global_energy=feat.energies.global_energy,
                    rms_contrast=feat.energies.rms_contrast,
                )
            )
    return pools


def v0_pool(
    features: Mapping[str, FrameFeatures], clear_day_keys: Sequence[str], params: V0Params
) -> list[ReferenceFrameV0]:
    """v0's reference pool, as its run builds it: clear-day frames fits may use, gates passed."""
    from .extractor_v0 import camera_fault

    pool = []
    for key in sorted(clear_day_keys):
        feat = features.get(key)
        if feat is None or not feat.fit_allowed or feat.gate_failure is not None:
            continue
        if feat.sun_alt_deg < params.min_sun_alt_deg or camera_fault(feat.energies, params):
            continue
        pool.append(
            ReferenceFrameV0(
                key,
                feat.sha256,
                feat.image_time,
                feat.sun_alt_deg,
                feat.sun_az_deg,
                feat.local_day,
                feat.energies,
            )
        )
    return pool


@dataclass(frozen=True)
class Prediction:
    """One frame's out-of-day result under one method: its class per band, and the record."""

    key: str
    local_day: str
    classes: dict[str, str | None]
    ratios: dict[str, float | None]
    record: dict[str, Any]


def _v02_result(
    feat: FrameFeatures,
    method: MethodSpec,
    pools: Mapping[str, Sequence[PoolFrame]],
    order: Sequence[str],
    exclude_days: Sequence[str],
    thresholds: Mapping[str, Thresholds | None],
) -> FrameResultV02:
    assert method.v02 is not None
    return classify_frame_v02(
        feat.energies,
        feat.light,
        sun_alt_deg=feat.sun_alt_deg,
        sun_az_deg=feat.sun_az_deg,
        order=order,
        pools=pools,
        thresholds=thresholds,
        params=method.v02,
        local_day=feat.local_day,
        exclude_days=exclude_days,
        exclude_keys=(feat.key,),
        excluded_because=feat.gate_failure,
    )


def _v0_ratios(
    feat: FrameFeatures,
    method: MethodSpec,
    pool: Sequence[ReferenceFrameV0],
    order: Sequence[str],
    exclude_days: Sequence[str],
) -> tuple[str, dict[str, float | None], dict[str, str | None], dict[str, Any]]:
    """v0's frame class, band ratios and fixed-τ states, its pool kept off ``exclude_days``."""
    assert method.v0 is not None
    if feat.gate_failure is not None and feat.sun_alt_deg >= method.v0.min_sun_alt_deg:
        from .extractor_v0 import camera_fault

        if camera_fault(feat.energies, method.v0) is None:
            ratios: dict[str, float | None] = {b: None for b in order}
            states: dict[str, str | None] = {b: None for b in order}
            return "excluded", ratios, states, {"frame_class": "excluded"}
    blocked = set(exclude_days)
    allowed = [r for r in pool if r.local_day not in blocked]
    result = classify_frame(
        feat.energies,
        sun_alt_deg=feat.sun_alt_deg,
        sun_az_deg=feat.sun_az_deg,
        order=order,
        pool=allowed,
        params=method.v0,
        local_day=feat.local_day,
        exclude_keys=(feat.key,),
    )
    ratios = {b: result.bands[b].ratio for b in order}
    states = {b: result.bands[b].state for b in order}
    return result.frame_class, ratios, states, result.to_dict()


def _training_samples(
    frames: Sequence[FrameFeatures],
    ratio_of: RatioOf,
    labels: MergedLabels,
    band: str,
    truth: Callable[[str | None], bool | None],
) -> list[tuple[float, bool]]:
    out = []
    for feat in frames:
        frame_class, ratios = ratio_of(feat)
        ratio = ratios.get(band)
        event = truth(labels.frames[feat.key].bands.get(band))
        if frame_class != "scored" or ratio is None or event is None:
            continue
        out.append((ratio, event))
    return out


@dataclass(frozen=True)
class FoldFit:
    """One fold of one method: the held-out day, the fit days, τ per band and the fits."""

    method: str
    test_day: str
    fit_days: tuple[str, ...]
    thresholds: dict[str, Thresholds | None]
    fits: dict[str, dict[str, Any]]
    pool_sizes: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {
            "method": self.method,
            "test_day": self.test_day,
            "fit_days": list(self.fit_days),
            "thresholds": {
                b: None
                if t is None
                else {"tau_hi": round(t.tau_hi, 6), "tau_lo": round(t.tau_lo, 6)}
                for b, t in self.thresholds.items()
            },
            "fits": self.fits,
            "pool_sizes_on_fit_days": self.pool_sizes,
        }


def folds(days: Sequence[str]) -> list[tuple[str, tuple[str, ...]]]:
    """One fold per day: (held-out day, the other days). A day is never in its own fit."""
    ordered = sorted(set(days))
    if len(ordered) < 2:
        raise ValueError("leave-one-day-out needs at least two days")
    return [(d, tuple(x for x in ordered if x != d)) for d in ordered]


def _fit_bands(
    method: MethodSpec,
    train: Sequence[FrameFeatures],
    ratio_of: RatioOf,
    labels: MergedLabels,
    judged: Sequence[str],
) -> tuple[dict[str, Thresholds | None], dict[str, dict[str, Any]]]:
    thresholds: dict[str, Thresholds | None] = {}
    fits: dict[str, dict[str, Any]] = {}
    cache = {f.key: ratio_of(f) for f in train}

    def cached(feat: FrameFeatures) -> tuple[str, dict[str, float | None]]:
        return cache[feat.key]

    for band in judged:
        hi = fit_threshold(
            _training_samples(train, cached, labels, band, truth_visible_event), direction="above"
        )
        if hi.tau is None:
            thresholds[band] = None
            fits[band] = {"tau_hi": hi.to_dict(), "tau_lo": None}
            continue
        lo = fit_threshold(
            _training_samples(train, cached, labels, band, truth_hidden_event),
            direction="below",
            upper_bound=hi.tau,
        )
        tau_lo = hi.tau if lo.tau is None else lo.tau
        thresholds[band] = Thresholds(hi.tau, tau_lo)
        fits[band] = {"tau_hi": hi.to_dict(), "tau_lo": lo.to_dict(), "tau_lo_used": tau_lo}
    return thresholds, fits


def _nested_v02(
    method: MethodSpec,
    pools: Mapping[str, Sequence[PoolFrame]],
    order: Sequence[str],
    test_day: str,
) -> RatioOf:
    """A training frame's ratios: references off its own day and off the held-out day."""
    no_tau: dict[str, Thresholds | None] = {b: None for b in order}

    def ratio_of(feat: FrameFeatures) -> tuple[str, dict[str, float | None]]:
        res = _v02_result(feat, method, pools, order, (test_day, feat.local_day), no_tau)
        return res.frame_class, {b: res.bands[b].ratio for b in order}

    return ratio_of


def _nested_v0(
    method: MethodSpec, pool: Sequence[ReferenceFrameV0], order: Sequence[str], test_day: str
) -> RatioOf:
    """v0's training-frame ratios, its pool kept off the frame's day and the held-out day."""

    def ratio_of(feat: FrameFeatures) -> tuple[str, dict[str, float | None]]:
        frame_class, ratios, _, _ = _v0_ratios(
            feat, method, pool, order, (test_day, feat.local_day)
        )
        return frame_class, ratios

    return ratio_of


def _fold_v02(
    method: MethodSpec,
    all_pools: Mapping[str, Sequence[PoolFrame]],
    train: Sequence[FrameFeatures],
    test: Sequence[FrameFeatures],
    labels: MergedLabels,
    order: Sequence[str],
    judged: Sequence[str],
    test_day: str,
    fit_days: Sequence[str],
) -> tuple[FoldFit, dict[str, Prediction]]:
    pools = {b: [p for p in all_pools[b] if p.local_day in fit_days] for b in order}
    thresholds, fits = _fit_bands(
        method, train, _nested_v02(method, pools, order, test_day), labels, judged
    )
    full = {b: thresholds.get(b) for b in order}
    predictions = {}
    for feat in test:
        res = _v02_result(feat, method, pools, order, (test_day,), full)
        predictions[feat.key] = Prediction(
            feat.key,
            test_day,
            {b: res.band_class(b) for b in order},
            {b: res.bands[b].ratio for b in order},
            res.to_dict(),
        )
    fold = FoldFit(
        method.name,
        test_day,
        tuple(fit_days),
        {b: thresholds.get(b) for b in judged},
        fits,
        {b: len(pools[b]) for b in order},
    )
    return fold, predictions


def _fold_v0(
    method: MethodSpec,
    v0p: Sequence[ReferenceFrameV0],
    train: Sequence[FrameFeatures],
    test: Sequence[FrameFeatures],
    labels: MergedLabels,
    order: Sequence[str],
    judged: Sequence[str],
    test_day: str,
    fit_days: Sequence[str],
) -> tuple[FoldFit, dict[str, Prediction]]:
    assert method.v0 is not None
    pool = [r for r in v0p if r.local_day in fit_days]
    if method.fit_tau:
        thresholds, fits = _fit_bands(
            method, train, _nested_v0(method, pool, order, test_day), labels, judged
        )
    else:
        fixed = Thresholds(method.v0.tau_hi, method.v0.tau_lo)
        thresholds = {b: fixed for b in judged}
        fits = {b: {"fixed": True} for b in judged}
    predictions = {}
    for feat in test:
        frame_class, ratios, states, record = _v0_ratios(feat, method, pool, order, (test_day,))
        classes: dict[str, str | None] = {}
        for b in order:
            ratio = ratios[b]
            t = thresholds.get(b)
            if frame_class != "scored":
                classes[b] = frame_class
            elif ratio is None:
                classes[b] = "no_ratio"
            elif not method.fit_tau:
                classes[b] = states[b]
            elif t is None:
                classes[b] = "no_threshold"
            else:
                classes[b] = classify_ratio(ratio, tau_hi=t.tau_hi, tau_lo=t.tau_lo)
        predictions[feat.key] = Prediction(feat.key, test_day, classes, ratios, record)
    fold = FoldFit(
        method.name,
        test_day,
        tuple(fit_days),
        {b: thresholds.get(b) for b in judged},
        fits,
        {"clear_day": len(pool)},
    )
    return fold, predictions


def run_lodo(
    method: MethodSpec,
    features: Mapping[str, FrameFeatures],
    labels: MergedLabels,
    order: Sequence[str],
    *,
    judged: Sequence[str],
    clear_day_keys: Sequence[str] = (),
) -> tuple[list[FoldFit], dict[str, Prediction]]:
    """Every fold of one method: fit on the other days, class the held-out day's frames.

    Returns the fold fits and one prediction per labelled frame (its held-out day's class).
    """
    labelled = {k: f for k, f in features.items() if k in labels.frames}
    days = sorted({f.local_day for f in labelled.values()})
    fold_fits: list[FoldFit] = []
    predictions: dict[str, Prediction] = {}
    for test_day, fit_days in folds(days):
        train = [f for f in labelled.values() if f.local_day in fit_days]
        test = [f for f in labelled.values() if f.local_day == test_day]
        if method.family == "v0.2":
            assert method.v02 is not None
            fold, preds = _fold_v02(
                method,
                v02_pools(labelled, labels, order, method.v02),
                train,
                test,
                labels,
                order,
                judged,
                test_day,
                fit_days,
            )
        else:
            assert method.v0 is not None
            fold, preds = _fold_v0(
                method,
                v0_pool(features, clear_day_keys, method.v0),
                train,
                test,
                labels,
                order,
                judged,
                test_day,
                fit_days,
            )
        fold_fits.append(fold)
        predictions.update(preds)
    return fold_fits, predictions


def deployed_fit(
    method: MethodSpec,
    features: Mapping[str, FrameFeatures],
    labels: MergedLabels,
    order: Sequence[str],
    *,
    judged: Sequence[str],
) -> dict[str, Any]:
    """τ fitted on every labelled day (each frame against other days' references).

    This is what the extractor would use from now on; it is an in-sample fit, not a check.
    """
    if method.family != "v0.2" or method.v02 is None:
        raise ValueError("only v0.2 methods have a deployed fit")
    labelled = {k: f for k, f in features.items() if k in labels.frames}
    pools = v02_pools(labelled, labels, order, method.v02)
    no_tau: dict[str, Thresholds | None] = {b: None for b in order}
    v02 = method.v02

    def own_day(feat: FrameFeatures) -> tuple[str, dict[str, float | None]]:
        res = classify_frame_v02(
            feat.energies,
            feat.light,
            sun_alt_deg=feat.sun_alt_deg,
            sun_az_deg=feat.sun_az_deg,
            order=order,
            pools=pools,
            thresholds=no_tau,
            params=v02,
            local_day=feat.local_day,
            exclude_keys=(feat.key,),
            excluded_because=feat.gate_failure,
        )
        return res.frame_class, {b: res.bands[b].ratio for b in order}

    thresholds, fits = _fit_bands(method, list(labelled.values()), own_day, labels, judged)
    return {
        "method": method.name,
        "params": replace(v02).to_dict(),
        "thresholds": {
            b: None if t is None else {"tau_hi": round(t.tau_hi, 6), "tau_lo": round(t.tau_lo, 6)}
            for b, t in thresholds.items()
        },
        "fits": fits,
        "pool_sizes": {b: len(pools[b]) for b in order},
        "pool": {b: sorted(p.key for p in pools[b]) for b in order},
        "note": "in-sample: tau fitted on every labelled day; for use from now on, not a check",
    }
