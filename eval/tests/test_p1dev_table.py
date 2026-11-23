"""The dev P1 table's scores (PRD §13.4, §13.5, §13.7): PSS, masks, paired ΔPSS, bootstrap."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from snow_globe_eval.p1dev_table import (
    METHODS,
    SUBSETS,
    build_table,
    case_from_record,
    delta_row,
    method_row,
    to_markdown,
)
from snow_globe_eval.scores import contingency

KW = {"seed": 7, "n_boot": 2000, "block_length_days": 1}


def record(
    i: int,
    truth: str | None,
    *,
    day: date,
    model: bool | None = None,
    pa: bool | None = None,
    pb: bool | None = None,
    clim: bool | None = None,
    rule: bool | None = None,
    role: str = "dev-fit",
    lead: float | None = 28.5,
) -> dict[str, Any]:
    return {
        "frame": {
            "key": f"frames/v/{day}/{i:04d}.jpg",
            "local_day": day.isoformat(),
            "dev_week": {"iso_year": 2026, "iso_week": 47, "role": role},
        },
        "forecast": None if lead is None else {"lead_from_availability_h": lead},
        "p1": {
            "truth_class": truth,
            "forecasts": {
                "model": model,
                "persistence_a": pa,
                "persistence_b": pb,
                "climatology": clim,
                "s_rule": rule,
            },
        },
    }


D1, D2, D3 = date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 21)


def sample() -> list[dict[str, Any]]:
    """4 visible and 4 hidden scored frames, one partial, one in cloud, one unscorable, one
    without a model forecast; persistence (a) missing on the first day."""
    rows = [
        record(0, "visible", day=D1, model=True, pa=None, pb=True),
        record(1, "visible", day=D1, model=True, pa=None, pb=False),
        record(2, "hidden", day=D1, model=True, pa=None, pb=False),
        record(3, "partial", day=D1, model=True, pa=None, pb=True),
        record(4, "visible", day=D2, model=True, pa=True, pb=True),
        record(5, "visible", day=D2, model=False, pa=True, pb=False),
        record(6, "hidden", day=D2, model=False, pa=True, pb=False),
        record(7, "in_cloud", day=D2, model=False, pa=False, pb=False),
        record(8, "hidden", day=D3, model=False, pa=False, pb=True),
        record(9, "hidden", day=D3, model=False, pa=False, pb=False),
        record(10, "no_reference", day=D3, model=True, pa=False, pb=False),
        record(11, "visible", day=D3, model=None, pa=False, pb=True, lead=None),
    ]
    return rows


def test_pss_per_method_with_partial_excluded_and_in_cloud_its_own_class() -> None:
    cases = [case_from_record(r) for r in sample()]
    row = method_row(cases, "model", in_cloud_as_hidden=False, **KW)
    t = row["table"]
    assert (t["hits"], t["misses"], t["false_alarms"], t["correct_negatives"]) == (3, 1, 1, 3)
    assert t["partial"] == 1 and row["pss"] == pytest.approx(0.75 - 0.25)
    assert row["breakdown"] == {
        "excluded_no_forecast": 1,
        "excluded_truth_in_cloud_own_class": 1,
        "excluded_truth_no_reference": 1,
        "excluded_truth_partial": 1,
        "scored": 8,
    }
    assert row["n_pairs"] == 8 and row["n_days_with_pairs"] == 3 and row["frames_in_subset"] == 12
    # the same as scores.contingency on the eight scored frames (0, 1, 2, 4, 5, 6, 8, 9)
    direct = contingency(
        [True, True, True, True, False, False, False, False],
        [True, True, False, True, True, False, False, False],
    )
    assert direct.pss == pytest.approx(t["pss"]) and direct.n == t["n"]


def test_in_cloud_counts_as_hidden_in_the_sensitivity_row() -> None:
    cases = [case_from_record(r) for r in sample()]
    row = method_row(cases, "model", in_cloud_as_hidden=True, **KW)
    t = row["table"]
    assert (t["hits"], t["misses"], t["false_alarms"], t["correct_negatives"]) == (3, 1, 1, 4)
    assert row["pss"] == pytest.approx(0.75 - 0.2)
    assert "excluded_truth_in_cloud_own_class" not in row["breakdown"]


def test_a_method_without_a_forecast_loses_only_its_own_frames() -> None:
    cases = [case_from_record(r) for r in sample()]
    a = method_row(cases, "persistence_a", in_cloud_as_hidden=False, **KW)
    assert a["breakdown"]["excluded_no_forecast"] == 4  # the first day's four frames, partial too
    assert a["n_pairs"] == 6 and a["n_days_with_pairs"] == 2
    b = method_row(cases, "persistence_b", in_cloud_as_hidden=False, **KW)
    assert b["n_pairs"] == 9 and "excluded_no_forecast" not in b["breakdown"]


def test_the_paired_delta_uses_only_frames_both_forecast() -> None:
    cases = [case_from_record(r) for r in sample()]
    row = delta_row(cases, "persistence_a", in_cloud_as_hidden=False, **KW)
    assert row["n_pairs"] == 5  # D2's three and D3's two; the first day has no (a)
    tm, tr = row["table_model"], row["table_reference"]
    assert tm["n"] == tr["n"] == 5 and tm["partial"] == tr["partial"] == 0
    assert row["delta"] == pytest.approx(row["pss_model"] - row["pss_reference"])
    assert row["n_days_with_pairs"] == 2
    again = delta_row(cases, "persistence_a", in_cloud_as_hidden=False, **KW)
    assert (again["ci_low"], again["ci_high"]) == (row["ci_low"], row["ci_high"])


def test_identical_forecasts_give_a_zero_delta_and_an_interval_at_zero() -> None:
    rows = [
        record(
            i,
            "visible" if i % 2 else "hidden",
            day=D1 + timedelta(days=i % 5),
            model=bool(i % 3),
            pa=bool(i % 3),
        )
        for i in range(40)
    ]
    row = delta_row(
        [case_from_record(r) for r in rows], "persistence_a", in_cloud_as_hidden=False, **KW
    )
    assert row["delta"] == 0.0 and row["ci_low"] == 0.0 and row["ci_high"] == 0.0
    assert row["excludes_zero"] is False


def test_the_table_has_every_subset_method_and_comparison_and_says_it_is_provisional() -> None:
    rows = sample()
    for r in rows[8:]:
        r["frame"]["dev_week"]["role"] = "dev-check"
    table = build_table(
        rows,
        stronger="b",
        persistence_choice={"pss_a": 0.1, "pss_b": 0.2, "n_paired": 5, "flags": []},
        provisional_because=["extractor v0: below its PRD §13.8 bar"],
        header={"run_id": "test", "viewpoint_id": "v", "lead_nominal_h": 24.0},
        **KW,
    )
    assert table["provisional"] is True and table["provisional_because"]
    assert table["persistence_choice"]["stronger"] == "persistence_b"
    for block in ("primary", "sensitivity_in_cloud_as_hidden"):
        for subset in SUBSETS:
            assert [r["method"] for r in table[block]["pss"][subset]] == list(METHODS)
            refs = [r["reference"] for r in table[block]["delta"][subset]]
            assert refs == ["persistence_b", "persistence_a", "climatology", "s_rule"]
    frames = table["frames"]
    assert frames["all"]["daytime_frames"] == 12
    assert frames["dev-fit"]["daytime_frames"] == 8 and frames["dev-check"]["daytime_frames"] == 4
    assert frames["all"]["with_forecast_run"] == 11
    assert frames["all"]["truth_classes"]["partial"] == 1
    assert frames["all"]["lead_from_availability_h"] == {"min": 28.5, "median": 28.5, "max": 28.5}
    assert table["not_computed"][0]["method"] == "s_mos"
    text = to_markdown(table)
    assert text.startswith("# Dev-only P1 table, run test: PROVISIONAL")
    assert "below its PRD §13.8 bar" in text and "in-cloud counted as hidden" in text
    assert "| all | model |" in text and "| dev-check | persistence_b |" in text


def test_an_empty_subset_has_no_scores_but_does_not_fail() -> None:
    rows = sample()
    table = build_table(
        rows,
        stronger="a",
        persistence_choice={},
        provisional_because=["x"],
        header={"run_id": "t"},
        **KW,
    )
    check = table["primary"]["pss"]["dev-check"][0]
    assert check["frames_in_subset"] == 0 and check["pss"] is None
    assert "no scored pairs" in check["flags"]
    with pytest.raises(ValueError):
        build_table(
            rows, stronger="c", persistence_choice={}, provisional_because=[], header={}, **KW
        )


def test_white_out_renders_count_as_not_visible_only_in_the_diagnostic() -> None:
    rows = sample()
    white = record(12, "hidden", day=D3, model=None, pa=False, pb=False)
    white["prediction"] = {"frame_class": "camera_fault", "detail": {"camera_fault": "saturated"}}
    dark = record(13, "visible", day=D3, model=None, pa=True, pb=True)
    dark["prediction"] = {"frame_class": "camera_fault", "detail": {"camera_fault": "dark"}}
    rows += [white, dark]
    table = build_table(
        rows,
        stronger="a",
        persistence_choice={},
        provisional_because=["x"],
        header={"run_id": "t"},
        **KW,
    )
    primary = table["primary"]["pss"]["all"][0]
    diagnostic = table["diagnostic_render_white_out_as_not_visible"]["pss"]["all"][0]
    assert primary["method"] == diagnostic["method"] == "model"
    assert diagnostic["table"]["correct_negatives"] == primary["table"]["correct_negatives"] + 1
    assert diagnostic["breakdown"]["excluded_no_forecast"] == 2  # frame 11 and the dark render
    assert table["frames"]["all"]["render_camera_faults"] == {"dark": 1, "saturated": 1}
    assert table["frames"]["all"]["render_white_outs_with_usable_truth"] == 1
    text = to_markdown(table)
    assert "Diagnostic: white-out renders" in text and "(not the primary)" in text
    assert "resamples undefined" in text or "day(s)" in text


def _assets(passes: bool, off_day: bool = True) -> dict[str, Any]:
    return {
        "rule": "test rule",
        "truth_is_plate_input": None,
        "plate": {
            "inputs": 1,
            "passes": passes,
            "failing": [],
            "at_or_after_issue": 0 if passes else 1,
            "base_at_or_after_issue": 0 if passes else 1,
            "on_valid_local_day": 0 if off_day else 1,
        },
        "references": {"n": 2, "passes": True, "failing": [], "at_or_after_issue": 0},
        "uses_dev_check_week_asset": False,
        "plate_off_valid_day": off_day,
        "passes": passes,
    }


def test_a_plate_input_truth_is_excluded_from_every_row_and_counted() -> None:
    rows = sample()
    rows[0]["p1"]["excluded_because"] = "the truth frame is one of the plate's input frames"
    cases = [case_from_record(r) for r in rows]
    for method in METHODS:
        row = method_row(cases, method, in_cloud_as_hidden=False, **KW)
        assert row["breakdown"].get("excluded_truth_is_plate_input") == 1
    model = method_row(cases, "model", in_cloud_as_hidden=False, **KW)
    assert model["n_pairs"] == 7  # frame 0 (a visible hit) is gone
    delta = delta_row(cases, "persistence_b", in_cloud_as_hidden=False, **KW)
    assert delta["breakdown"]["excluded_truth_is_plate_input"] == 1


def test_the_fitted_asset_blocks_keep_only_the_frames_that_pass() -> None:
    rows = sample()
    for i, r in enumerate(rows):
        r["fitted_assets"] = _assets(passes=i >= 4, off_day=i >= 2)
    table = build_table(
        rows,
        stronger="a",
        persistence_choice={},
        provisional_because=["x"],
        header={"run_id": "t"},
        **KW,
    )
    strict = table["sensitivity_fitted_assets_before_issue"]
    assert strict["frames_kept"] == 8 and strict["pss"]["all"][0]["frames_in_subset"] == 8
    assert table["sensitivity_plate_off_valid_day"]["frames_kept"] == 10
    assert table["frames"]["all"]["fitted_assets_pass"] == 8
    assert table["fitted_assets"]["all"]["frames"] == 12
    assert table["fitted_assets"]["all"]["plate_base_at_or_after_issue"] == 4
    text = to_markdown(table)
    assert "## Sensitivity: fitted assets before the issue time" in text
    assert "Frames kept: 8" in text and "Fitted assets, by test rule" in text


def test_frames_without_a_forecast_are_listed_by_method_reason_and_day() -> None:
    rows = sample()
    for r in rows[:4]:  # the first day: persistence (a) had nothing before its issue time
        r["baselines"] = {"persistence_a": {"flags": ["no-usable-frame-before-issue"]}}
    rows[11]["status"] = "no-forecast"
    table = build_table(
        rows,
        stronger="a",
        persistence_choice={},
        provisional_because=["x"],
        header={"run_id": "t"},
        **KW,
    )
    missing = table["frames"]["all"]["no_forecast_by_reason_and_day"]
    assert missing["persistence_a"] == {"no-usable-frame-before-issue": {"2026-11-19": 4}}
    assert missing["model"] == {"no-forecast-run": {"2026-11-21": 1}}
    assert missing["climatology"] == {"no-forecast": {d.isoformat(): 4 for d in (D1, D2, D3)}}
    text = to_markdown(table)
    assert "persistence_a, no-usable-frame-before-issue: 4 frames: 2026-11-19 (4)" in text
    assert "First local day 2026-11-19: persistence_a none on 4 frames" in text


def test_the_headline_follows_the_extractor_validation_and_the_reasons() -> None:
    def table(validation: dict[str, Any], reasons: list[str]) -> dict[str, Any]:
        return build_table(
            sample(),
            stronger="a",
            persistence_choice={},
            provisional_because=reasons,
            header={"run_id": "t", "extractor": {"extractor": "v0.2", "validation": validation}},
            **KW,
        )

    below = to_markdown(table({"publishable": False}, ["x"]))
    assert "Extractor v0.2 is below its PRD §13.8 bar" in below
    meets = table({"publishable": True}, ["the bootstrap block is not set (TODO(M2))"])
    assert meets["provisional"] is True
    text = to_markdown(meets)
    assert "meets its PRD §13.8 bar, but the reasons below" in text
    assert "is below its PRD §13.8 bar" not in text
    final = table({"publishable": True}, [])
    assert final["provisional"] is False
    assert not to_markdown(final).splitlines()[0].endswith("PROVISIONAL")


def test_the_other_availability_rule_is_listed_where_its_run_differs() -> None:
    rows = sample()
    for r in rows:
        r["forecast_other_rule"] = {"availability_rule": "s3-stamps", "run": None}
        r["forecast"] = {**(r["forecast"] or {}), "run": None} if r["forecast"] else None
    rows[0]["forecast"] = {"lead_from_availability_h": 28.0, "run": "2026111912"}
    rows[0]["forecast_other_rule"] = {"availability_rule": "s3-stamps", "run": "2026111918"}
    rows[1]["forecast"] = None
    rows[1]["forecast_other_rule"] = {"availability_rule": "s3-stamps", "run": "2026111918"}
    listing = build_table(
        rows,
        stronger="a",
        persistence_choice={},
        provisional_because=["x"],
        header={"run_id": "t"},
        **KW,
    )["forecast_choice_under_other_rule"]
    assert listing["other_rule"] == "s3-stamps" and listing["frames_checked"] == 12
    assert listing["frames_whose_run_differs"] == 2
    assert listing["frames_that_gain_a_run"] == 1 and listing["frames_that_lose_their_run"] == 0
    assert listing["frames"][0]["run_under_other_rule"] == "2026111918"
