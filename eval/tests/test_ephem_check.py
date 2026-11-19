"""The NPS SolarElevationAngle cross-check (PRD §8.4), on a recorded subset and the full sample."""

from __future__ import annotations

import json

import pytest
from conftest import needs_oculus, sample
from lookout_common.manifest import load_manifest

from lookout_eval import ephem_check
from lookout_eval.ephem_check import format_report, run_check, saemundsson_unclipped_deg
from lookout_eval.ephemeris import EphemerisUnavailableError, NoaaBackend, refraction_deg


def test_unclipped_formula_matches_lookout_refraction_above_minus_one() -> None:
    for h in (-1.0, 0.0, 3.0, 10.0, 45.0):
        assert saemundsson_unclipped_deg(h) == pytest.approx(refraction_deg(h), abs=1e-12)
    assert saemundsson_unclipped_deg(-6.0) < 0.0  # sign flip below the pole at -5.11 deg


def _summary(report, group: str, model: str):
    return next(s for s in getattr(report, group) if s.model == model)


@needs_oculus
def test_subset_verdict_is_refracted(tmp_path, nps_subset) -> None:
    path = tmp_path / "subset.json"
    path.write_text(json.dumps(nps_subset))
    report = run_check(path, load_manifest("point-reyes.beach"))
    assert report.n_assets == len(nps_subset["Results"])
    assert report.verdict == "refracted"
    assert _summary(report, "sun_up", "refracted").max_abs_diff_deg < 0.005
    assert _summary(report, "sun_up", "geometric").max_abs_diff_deg > 0.1
    assert _summary(report, "all_assets", "saemundsson_unclipped").max_abs_diff_deg < 0.005
    assert report.oculus_vs_noaa_max_abs_deg < 0.02
    assert "verdict: refracted" in format_report(report)
    json.dumps(report.to_dict())


@needs_oculus
def test_full_local_sample() -> None:
    path = sample("nps-json/search_pore_20261117.json")
    report = run_check(path, load_manifest("point-reyes.beach"))
    assert report.n_assets == 88
    assert report.verdict == "refracted"
    assert _summary(report, "sun_up", "refracted").max_abs_diff_deg < 0.005
    assert _summary(report, "all_assets", "saemundsson_unclipped").max_abs_diff_deg < 0.005


def test_rejects_non_search_documents(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"hello": 1}))
    with pytest.raises(ValueError, match="Results"):
        run_check(path, load_manifest("point-reyes.beach"), ephemeris=NoaaBackend())


def test_input_is_checked_before_the_backend(tmp_path, monkeypatch) -> None:
    """Without oculus-core (a CI venv synced with --no-group oculus), bad input still says so."""

    def unavailable(name=None):
        raise EphemerisUnavailableError("oculus-core is not importable (test)")

    monkeypatch.setattr(ephem_check, "get_ephemeris", unavailable)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"hello": 1}))
    with pytest.raises(ValueError, match="Results"):
        run_check(bad, load_manifest("point-reyes.beach"))
    with pytest.raises(FileNotFoundError):
        run_check(tmp_path / "missing.json", load_manifest("point-reyes.beach"))
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"Results": []}))
    with pytest.raises(EphemerisUnavailableError):
        run_check(good, load_manifest("point-reyes.beach"))


def test_noaa_backend_runs_the_check_without_oculus(tmp_path, nps_subset) -> None:
    path = tmp_path / "subset.json"
    path.write_text(json.dumps(nps_subset))
    report = run_check(path, load_manifest("point-reyes.beach"), ephemeris=NoaaBackend())
    assert report.verdict == "refracted" and report.ephemeris.startswith("noaa")
    assert _summary(report, "sun_up", "refracted").max_abs_diff_deg < 0.02
