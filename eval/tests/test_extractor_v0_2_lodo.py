"""Leave-one-day-out fits (PRD §13.8, §13.9): the τ fit, the folds, and no day in its own fit."""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

import snow_globe_eval.extractor_v0_2_lodo as lodo
from snow_globe_eval.extractor_v0 import BandEnergies
from snow_globe_eval.extractor_v0_2 import Illumination, expected_contrast
from snow_globe_eval.extractor_v0_2_labels import MergedFrame, MergedLabels
from snow_globe_eval.extractor_v0_2_lodo import (
    METHODS,
    FrameFeatures,
    deployed_fit,
    fit_threshold,
    folds,
    run_lodo,
    truth_hidden_event,
    truth_visible_event,
)

DAYS = ("2026-11-19", "2026-11-20", "2026-11-21", "2026-11-22")
ORDER = ("summit", "upper")


def test_fit_threshold_takes_the_geometric_middle_of_the_best_gap() -> None:
    fit = fit_threshold([(0.1, False), (0.2, False), (0.8, True), (1.0, True)], direction="above")
    assert fit.tau == pytest.approx(math.sqrt(0.2 * 0.8)) and fit.f1 == 1.0
    assert fit.gap == (0.2, 0.8) and (fit.n, fit.events) == (4, 2)


def test_fit_threshold_breaks_ties_by_the_widest_log_gap() -> None:
    # gaps (2, 3) and (9, 10) both give F1 2/3; (2, 3) is the wider in log ratio
    samples = list(
        zip((1.0, 2.0, 3.0, 5.0, 9.0, 10.0), (False, False, True, False, False, True), strict=True)
    )
    fit = fit_threshold(samples, direction="above")
    assert fit.f1 == pytest.approx(2 / 3) and fit.gap == (2.0, 3.0)
    assert fit.tau == pytest.approx(math.sqrt(6.0))


def test_fit_threshold_below_respects_its_upper_bound() -> None:
    samples = [(0.05, True), (0.1, True), (0.3, False), (0.6, False), (2.0, False)]
    free = fit_threshold(samples, direction="below")
    assert free.tau == pytest.approx(math.sqrt(0.1 * 0.3)) and free.f1 == 1.0
    # only the gap (0.05, 0.1) has its middle, 0.0707, under the bound
    bounded = fit_threshold(samples, direction="below", upper_bound=0.08)
    assert bounded.tau == pytest.approx(math.sqrt(0.05 * 0.1))
    assert bounded.f1 == pytest.approx(2 / 3)
    assert fit_threshold(samples, direction="below", upper_bound=0.06).tau is None


def test_fit_threshold_needs_both_classes() -> None:
    assert fit_threshold([(0.5, True), (0.9, True)], direction="above").tau is None
    assert fit_threshold([], direction="above").why_none == "no training frames"
    assert fit_threshold([(0.0, True), (1.0, False)], direction="above").tau is None


def test_truth_events() -> None:
    assert [truth_visible_event(s) for s in ("visible", "partial", "hidden", "in_cloud", None)] == [
        True,
        None,
        False,
        False,
        None,
    ]
    assert [truth_hidden_event(s) for s in ("visible", "partial", "hidden", "in_cloud", None)] == [
        False,
        False,
        True,
        True,
        None,
    ]


def test_folds_never_fit_on_the_held_out_day() -> None:
    out = folds(["b", "a", "c", "a"])
    assert out == [("a", ("b", "c")), ("b", ("a", "c")), ("c", ("a", "b"))]
    assert all(test not in fit for test, fit in out)
    with pytest.raises(ValueError):
        folds(["a"])


def _features() -> dict[str, FrameFeatures]:
    """Four days, each with sunny and overcast frames, visible and hidden summits.

    A visible summit's energy follows the expected contrast C(D); a hidden one is a constant
    noise floor, as on a camera.
    """
    out = {}
    for day in DAYS:
        for i, (d, visible) in enumerate(
            ((0.55, True), (0.55, True), (0.8, True), (0.8, False), (0.55, False), (0.8, False))
        ):
            c = expected_contrast(d)
            summit = 0.06 * c / expected_contrast(0.55) if visible else 0.015
            energies = BandEnergies(
                {"summit": summit, "upper": 0.05 if visible else 0.004},
                0.07,
                0.3,
                0.0,
                0.0,
                0.9,
                (960, 540),
            )
            light = Illumination(d, 0.5, 0.9, 0.0, c)
            key = f"frames/vp/{day}/{day.replace('-', '')}T18{i:02d}00Z_000000000000.jpg"
            out[key] = FrameFeatures(
                key,
                "0" * 64,
                f"{day}T18:{i:02d}:00.000Z",
                day,
                10.0 + i * 0.5,
                160.0,
                energies,
                light,
            )
    return out


def _labels(features: dict[str, FrameFeatures], flip_day: str | None = None) -> MergedLabels:
    frames = {}
    for key, feat in features.items():
        visible = feat.energies.bands["upper"] > 0.01
        state = "visible" if visible else "hidden"
        if feat.local_day == flip_day:
            state = "hidden" if visible else "visible"
        bands = {"summit": state, "upper": state}
        frames[key] = MergedFrame(
            key,
            feat.sha256,
            feat.image_time_utc,
            "s000",
            bands,
            {"a": dict(bands), "b": dict(bands)},
        )
    return MergedLabels("vp", "t", {}, ORDER, frames, "test", "0" * 64)


def _clear_day_keys(features: dict[str, FrameFeatures]) -> list[str]:
    return sorted(k for k, f in features.items() if f.energies.bands["upper"] > 0.01)


@pytest.mark.parametrize("method", METHODS, ids=[m.name for m in METHODS])
def test_a_held_out_day_never_shapes_its_own_fold(method: lodo.MethodSpec) -> None:
    """Relabel the held-out day upside down: its fold's τ and its classes must not move."""
    features = _features()
    clear = _clear_day_keys(features)
    fits, preds = run_lodo(
        method, features, _labels(features), ORDER, judged=ORDER, clear_day_keys=clear
    )
    assert len(preds) == len(features) and len(fits) == len(DAYS)
    for day in DAYS:
        flipped_fits, flipped = run_lodo(
            method,
            features,
            _labels(features, flip_day=day),
            ORDER,
            judged=ORDER,
            clear_day_keys=clear,
        )
        fold = next(f for f in fits if f.test_day == day)
        again = next(f for f in flipped_fits if f.test_day == day)
        assert fold.thresholds == again.thresholds
        for key, pred in preds.items():
            if pred.local_day == day:
                assert flipped[key].classes == pred.classes


def test_references_and_pools_avoid_the_held_out_and_own_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str | None, tuple[str, ...], set[str]]] = []
    original = lodo.classify_frame_v02

    def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        pool_days = {p.local_day for pool in kwargs["pools"].values() for p in pool}
        calls.append((kwargs["local_day"], tuple(kwargs["exclude_days"]), pool_days))
        return original(*args, **kwargs)

    monkeypatch.setattr(lodo, "classify_frame_v02", spy)
    features = _features()
    _, preds = run_lodo(METHODS[0], features, _labels(features), ORDER, judged=ORDER)
    assert calls
    for own_day, excluded, pool_days in calls:
        held_out = excluded[0]
        assert held_out not in pool_days  # the pools hold fit days only
        if len(excluded) == 2:  # a training frame's nested ratio
            assert excluded[1] == own_day and own_day != held_out
        else:  # a held-out frame
            assert own_day == held_out
    for pred in preds.values():
        for band in ORDER:
            refs = pred.record["bands"][band]["references"]
            assert all(f"/{pred.local_day}/" not in r["key"] for r in refs)


def test_the_contrast_model_reads_overcast_summits_against_sunny_references() -> None:
    """With only sunny visible summits on the fit days, v0.2 still classes overcast ones."""
    features = _features()
    # keep visible overcast summits on 21 Nov only: no fold but 21 Nov's own has them
    features = {
        k: f
        for k, f in features.items()
        if not (
            f.light.d_index == 0.8
            and f.energies.bands["upper"] > 0.01
            and f.local_day != "2026-11-21"
        )
    }
    labels = _labels(features)
    _, primary = run_lodo(METHODS[0], features, labels, ORDER, judged=ORDER)
    _, ablation = run_lodo(METHODS[1], features, labels, ORDER, judged=ORDER)
    overcast_visible = [
        k
        for k, f in features.items()
        if f.local_day == "2026-11-21"
        and f.light.d_index == 0.8
        and f.energies.bands["upper"] > 0.01
    ]
    assert overcast_visible
    for key in overcast_visible:
        assert primary[key].classes["summit"] == "visible"
        assert ablation[key].classes["summit"] != "visible"


def test_v0_as_built_keeps_its_fixed_tau_and_pool_off_the_held_out_day() -> None:
    features = _features()
    clear = _clear_day_keys(features)
    v0 = next(m for m in METHODS if m.name == "v0")
    fits, preds = run_lodo(
        v0, features, _labels(features), ORDER, judged=ORDER, clear_day_keys=clear
    )
    for fold in fits:
        assert all(
            t is not None and (t.tau_hi, t.tau_lo) == (0.6, 0.35) for t in fold.thresholds.values()
        )
        assert fold.pool_sizes["clear_day"] == len([k for k in clear if fold.test_day not in k])
    for pred in preds.values():
        assert all(f"/{pred.local_day}/" not in r["key"] for r in pred.record["references"])


def test_deployed_fit_uses_every_day() -> None:
    features = _features()
    doc = deployed_fit(METHODS[0], features, _labels(features), ORDER, judged=ORDER)
    assert doc["thresholds"]["summit"] is not None
    assert doc["pool_sizes"]["summit"] == 3 * len(DAYS)
    with pytest.raises(ValueError):
        deployed_fit(METHODS[2], features, _labels(features), ORDER, judged=ORDER)


def test_a_frame_with_d_undefined_serves_no_pool() -> None:
    features = _features()
    dark = Illumination(None, 0.01, 0.01, 0.0, None)
    unlit = {k: replace(f, light=dark) for k, f in features.items()}
    pools = lodo.v02_pools(unlit, _labels(features), ORDER, METHODS[0].v02)  # type: ignore[arg-type]
    assert all(not p for p in pools.values())  # upper too, not only the skyline band


def test_frames_a_fit_may_not_use_stay_out_of_the_pool() -> None:
    features = _features()
    barred = {k: replace(f, fit_allowed=False) for k, f in features.items()}
    pools = lodo.v02_pools(barred, _labels(features), ORDER, METHODS[0].v02)  # type: ignore[arg-type]
    assert all(not p for p in pools.values())
    gated = {k: replace(f, gate_failure="same_view: moved") for k, f in features.items()}
    assert all(
        not p for p in lodo.v02_pools(gated, _labels(features), ORDER, METHODS[0].v02).values()
    )  # type: ignore[arg-type]
