"""Scoring extractor v0.2 against the merged labels (PRD §13.8, §13.7): forms and bootstrap."""

from __future__ import annotations

import pytest

from snow_globe_eval.extractor_v0_2_labels import MergedFrame, MergedLabels
from snow_globe_eval.extractor_v0_2_lodo import Prediction
from snow_globe_eval.extractor_v0_2_scores import (
    bootstrap_f1,
    camera_in_cloud,
    confusion,
    day_tables,
    paired_bootstrap_delta_f1,
    score_method,
    table_scores,
)

D1, D2 = "2026-11-20", "2026-11-21"


def _case(rows: list[tuple[str, str, str, str, str]]) -> tuple[dict[str, Prediction], MergedLabels]:
    """rows: (key, day, label a, label b, predicted class) for the summit."""
    frames, preds = {}, {}
    for key, day, a, b, cls in rows:
        merged = a if a == b else None
        frames[key] = MergedFrame(
            key,
            "0" * 64,
            f"{day}T18:00:00Z",
            "s000",
            {"summit": merged},
            {"a": {"summit": a}, "b": {"summit": b}},
        )
        preds[key] = Prediction(key, day, {"summit": cls}, {"summit": None}, {})
    return preds, MergedLabels("vp", "t", {}, ("summit",), frames, "t", "0" * 64)


ROWS = [
    ("k1", D1, "visible", "visible", "visible"),  # hit
    ("k2", D1, "visible", "visible", "no_reference"),  # an abstention on a visible summit: miss
    ("k3", D1, "hidden", "hidden", "visible"),  # false alarm
    ("k4", D1, "in_cloud", "in_cloud", "in_cloud"),  # correct negative
    ("k5", D2, "partial", "partial", "hidden"),  # left out (label partial)
    ("k6", D2, "visible", "partial", "visible"),  # labellers disagree
    ("k7", D2, "hidden", "visible", "hidden"),  # labellers disagree
    ("k8", D2, "hidden", "hidden", "partial"),  # correct negative
]


def test_the_primary_form_counts_abstentions_as_not_visible() -> None:
    preds, labels = _case(ROWS)
    tables, left_out = day_tables(preds, labels, "summit", "primary")
    assert tables[D1] == [1, 1, 1, 1] and tables[D2] == [0, 0, 0, 1]
    assert left_out == {"label partial": 1, "labellers disagree": 2}
    pooled = table_scores([1, 1, 1, 2])
    assert pooled["f1"] == 0.5 and pooled["precision"] == 0.5 and pooled["recall"] == 0.5
    assert table_scores([0, 0, 0, 3])["f1"] is None


def test_the_other_forms() -> None:
    preds, labels = _case(ROWS)
    scored, out = day_tables(preds, labels, "summit", "scored_only")
    assert scored[D1] == [1, 0, 1, 0]  # k2 and k4 were not classed
    assert out["not classed by the extractor"] == 2
    partial, _ = day_tables(preds, labels, "summit", "partial_as_not_visible")
    assert partial[D2] == [0, 0, 0, 2]  # k5 joins as a correct negative
    no_cloud, _ = day_tables(preds, labels, "summit", "in_cloud_excluded")
    assert no_cloud[D1] == [1, 1, 1, 0]


def test_disagreements_both_ways() -> None:
    preds, labels = _case(ROWS)
    as_a, _ = day_tables(preds, labels, "summit", "disagreements_as_a")
    assert as_a[D2] == [1, 0, 0, 2]  # k6 visible (hit), k7 hidden (correct negative)
    as_b, _ = day_tables(preds, labels, "summit", "disagreements_as_b")
    assert as_b[D2] == [0, 1, 0, 1]  # k6 partial (left out), k7 visible (miss)
    worst, _ = day_tables(preds, labels, "summit", "disagreements_worst")
    assert worst[D2] == [0, 1, 0, 1]  # k6: left out beats a hit; k7: a miss
    best, _ = day_tables(preds, labels, "summit", "disagreements_best")
    assert best[D2] == [1, 0, 0, 2]


def test_bootstrap_is_reproducible_and_counts_undefined_resamples() -> None:
    tables = {D1: [3, 0, 0, 2], D2: [0, 0, 0, 4]}
    first = bootstrap_f1(tables, days=[D1, D2], n_boot=2000, seed=7)
    again = bootstrap_f1(tables, days=[D1, D2], n_boot=2000, seed=7)
    assert first == again and first["f1"] == 1.0
    # a resample of D2 twice has no event and no false alarm: F1 undefined, dropped
    assert 400 < first["undefined_resamples"] < 600
    assert first["interval"] == [1.0, 1.0] and first["distinct_values"] == 1
    mixed = bootstrap_f1({D1: [2, 1, 0, 1], D2: [1, 0, 2, 1]}, days=[D1, D2], n_boot=4000, seed=1)
    low, high = mixed["interval"]
    assert low <= mixed["f1"] <= high and low < high


def test_paired_delta_uses_the_same_days() -> None:
    better = {D1: [3, 0, 0, 2], D2: [2, 0, 0, 3]}
    worse = {D1: [1, 2, 0, 2], D2: [1, 1, 0, 3]}
    delta = paired_bootstrap_delta_f1(better, worse, days=[D1, D2], n_boot=2000, seed=3)
    assert delta["delta_f1"] == pytest.approx(1.0 - 2 * 2 / (2 * 2 + 3), abs=1e-4)
    assert delta["interval"][0] > 0 and delta["share_above_zero"] == 1.0


def test_camera_in_cloud_counts_flags_against_labels() -> None:
    def pred(key: str, frame_class: str, tested: bool = True) -> Prediction:
        ratios = {"global_energy_ratio": 0.2, "contrast_ratio": 0.3} if tested else {}
        return Prediction(key, D1, {}, {}, {"frame_class": frame_class, **ratios})

    preds, labels = _case(
        [
            ("c1", D1, "in_cloud", "in_cloud", "in_cloud"),
            ("c2", D1, "in_cloud", "in_cloud", "hidden"),
            ("c3", D1, "in_cloud", "in_cloud", "hidden"),
            ("c4", D1, "hidden", "hidden", "in_cloud"),
            ("c5", D1, "visible", "visible", "visible"),
            ("c6", D1, "in_cloud", "hidden", "hidden"),
            ("c7", D1, "visible", "partial", "visible"),
        ]
    )
    preds.update(
        c1=pred("c1", "in_cloud"),
        c2=pred("c2", "scored"),
        c3=pred("c3", "night", tested=False),
        c4=pred("c4", "in_cloud"),
        c5=pred("c5", "scored"),
        c6=pred("c6", "scored"),
        c7=pred("c7", "scored"),
    )
    out = camera_in_cloud(preds, labels, "summit")
    assert (out["labelled_and_flagged"], out["labelled_not_flagged"]) == (1, 1)
    assert (out["labelled_not_tested"], out["flagged_not_labelled"], out["neither"]) == (1, 1, 1)
    assert out["labellers_split_one_in_cloud"] == 1  # c6; c7's split has no in_cloud
    assert out["labelled_in_cloud"] == 3 and out["flagged_share_of_labelled"] == 0.3333


def test_score_method_and_confusion() -> None:
    preds, labels = _case(ROWS)
    out = score_method(preds, labels, ("summit",), n_boot=500, seed=2)
    summit = out["bands"]["summit"]
    assert summit["primary"]["pooled"]["hits"] == 1 and out["days"] == [D1, D2]
    assert summit["primary"]["per_day"][D2]["n"] == 1
    assert summit["bootstrap_primary"]["n_boot"] == 500
    table = confusion(preds, labels, "summit")
    assert table["disagree"] == {"hidden": 1, "visible": 1}
    assert table["visible"] == {"no_reference": 1, "visible": 1}
