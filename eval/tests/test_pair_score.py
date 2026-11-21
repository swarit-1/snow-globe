"""One pair's P1 cell (PRD §13.4): event "visible", partial and in-cloud handling."""

from __future__ import annotations

import math

import pytest

from snow_globe_eval.extractor import EdgeStats, ExtractorParams, LandmarkResult
from snow_globe_eval.pair_score import score_pair

_STATS = EdgeStats(1.0, 1.0, 1.0, 1.0, 0.5, 100, (960, 540), (1920, 1080))


def _result(
    state: str | None, ratio: float | None, *, in_cloud: bool = False, fault: str | None = None
) -> LandmarkResult:
    return LandmarkResult(
        state=state,  # type: ignore[arg-type]
        ratio=ratio,
        camera_in_cloud=in_cloud,
        global_energy_ratio=None,
        contrast_ratio=None,
        stats=_STATS,
        sun_alt_deg=30.0,
        sun_az_deg=180.0,
        reference=None,
        params=ExtractorParams(),
        camera_fault=fault,
    )


VISIBLE, HIDDEN, PARTIAL = _result("visible", 0.9), _result("hidden", 0.2), _result("partial", 0.5)


@pytest.mark.parametrize(
    ("render", "truth", "cell"),
    [
        (VISIBLE, VISIBLE, "hit"),
        (HIDDEN, VISIBLE, "miss"),
        (PARTIAL, VISIBLE, "miss"),  # a partial render does not forecast "visible"
        (VISIBLE, HIDDEN, "false_alarm"),
        (HIDDEN, HIDDEN, "correct_negative"),
    ],
)
def test_the_four_cells(render, truth, cell) -> None:
    score = score_pair(truth, render)
    assert score["cell"] == cell == score["cell_sensitivity_in_cloud_as_hidden"]
    table = score["contingency"]
    assert table["n"] == 1 and table["excluded"] == 0 and table["partial"] == 0
    assert table["pss"] is None and "pss" in table["undefined"]  # one pair never has both


def test_a_partial_truth_is_masked_and_reported() -> None:
    score = score_pair(PARTIAL, VISIBLE)
    assert score["cell"] is None and score["observed_class"] == "partial"
    assert score["contingency"]["partial"] == 1 and score["contingency"]["n"] == 0
    assert any("partial truth" in note for note in score["notes"])


def test_in_cloud_is_its_own_class_and_hidden_only_in_the_sensitivity() -> None:
    truth = _result("hidden", 0.05, in_cloud=True)
    score = score_pair(truth, VISIBLE)
    assert score["observed_class"] == "in_cloud" and score["cell"] is None
    assert score["contingency"]["excluded"] == 1
    assert score["cell_sensitivity_in_cloud_as_hidden"] == "false_alarm"


def test_gated_faulty_or_unclassed_truth_is_excluded() -> None:
    gated = score_pair(VISIBLE, VISIBLE, truth_scored=False)
    assert gated["cell"] is None and gated["contingency"]["excluded"] == 1
    assert gated["cell_sensitivity_in_cloud_as_hidden"] is None
    fault = score_pair(_result(None, 0.1, fault="dark"), VISIBLE)
    assert fault["cell"] is None and "camera-fault" in fault["notes"][0]
    unclassed = score_pair(_result(None, None), VISIBLE)
    assert unclassed["cell"] is None and unclassed["ratio_render_minus_truth"] is None


def test_ratio_error() -> None:
    score = score_pair(_result("partial", 0.5), _result("visible", 0.8))
    assert score["ratio_render_minus_truth"] == pytest.approx(0.3)
    assert score["log_ratio_render_over_truth"] == pytest.approx(math.log(1.6))
    assert score["classes_agree"] is False and score["forecast_visible"] is True
