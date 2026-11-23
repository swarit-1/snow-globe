"""oculus-core and eval's refraction against recorded Horizons tables (PRD §8.4 targets)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest
from conftest import FIXTURES, needs_oculus

from snow_globe_eval.ephem_validate.compare import (
    REFRACTION_DOMAIN_MIN_DEG,
    BodyComparison,
    beyond_target_rows,
    compare_body,
    count_beyond,
    dip_band,
    horizon_dip_deg,
    oculus_positions,
    refraction_array,
    summarize_comparisons,
)
from snow_globe_eval.ephem_validate.horizons import parse_result
from snow_globe_eval.ephem_validate.stats import (
    TARGET_DEG,
    band_labels,
    hour_angle_dec_deg,
    separation_deg,
    summarize,
    summarize_by,
    wrap180,
)
from snow_globe_eval.ephemeris import refraction_deg

HORIZONS = FIXTURES / "ephem" / "horizons"


def _table(name: str):
    (path,) = sorted(HORIZONS.glob(f"fixture.rainier.{name}.*.json"))
    record = json.loads(Path(path).read_text(encoding="utf-8"))
    return record["query"], parse_result(record["response"]["result"])


def _comparison(body: str, tag: str) -> BodyComparison:
    query, air = _table(f"{body}.airless.{tag}")
    _, refr = _table(f"{body}.refracted.{tag}")
    ms = np.array([t.timestamp() * 1000.0 for t in air.times_utc])
    pos = oculus_positions(ms, query["lat_deg"], query["lon_deg"], query["height_m"])
    return compare_body(
        source="horizons",
        site_id="rainier.fixture",
        body=body,
        unix_ms=ms,
        ref_alt=air.values["el_deg"],
        ref_az=air.values["az_deg"],
        ref_alt_refr=refr.values["el_deg"],
        ref_az_refr=refr.values["az_deg"],
        positions=pos,
        ref_ra=air.values["ra_deg"],
        ref_dec=air.values["dec_deg"],
        ref_illum_pct=air.values["illum_pct"],
    )


# --- stats ---------------------------------------------------------------------------------


def test_separation_is_exact_for_known_angles_and_tiny_ones() -> None:
    assert separation_deg(0.0, 0.0, 0.0, 90.0) == pytest.approx(90.0, abs=1e-12)
    assert separation_deg(90.0, 0.0, 0.0, 123.0) == pytest.approx(90.0, abs=1e-12)
    assert separation_deg(10.0, 350.0, 10.0, 10.0) == pytest.approx(
        math.degrees(
            math.acos(
                math.sin(math.radians(10)) ** 2
                + math.cos(math.radians(10)) ** 2 * math.cos(math.radians(20))
            )
        ),
        abs=1e-10,
    )
    one_mas = 1.0 / 3600.0 / 1000.0
    assert separation_deg(30.0, 100.0, 30.0 + one_mas, 100.0) == pytest.approx(one_mas, rel=1e-6)


def test_wrap_bands_and_summaries() -> None:
    assert wrap180([359.0, 181.0, -181.0]).tolist() == pytest.approx([-1.0, -179.0, 179.0])
    assert band_labels([-30.0, -1.0, -0.5, 0.0, 1.9, 90.0]).tolist() == [
        "-90..-18",
        "-1..0",
        "-1..0",
        "0..2",
        "0..2",
        "45..90",
    ]
    s = summarize([1.0, -3.0, 2.0, math.nan])
    assert s["n"] == 3 and s["worst"] == 3.0 and s["worst_index"] == 1
    assert s["p99"] == 3.0  # "higher": an observed value
    assert s["mean"] == pytest.approx(0.0)
    by = summarize_by([1.0, 5.0, 2.0], ["a", "b", "a"])
    assert by["a"]["worst"] == 2.0 and by["a"]["worst_index"] == 2 and by["b"]["n"] == 1
    assert summarize([]) == {"n": 0}


def test_hour_angle_and_declination_of_the_meridian() -> None:
    # Due south at altitude 90 - lat + dec: hour angle 0, declination dec.
    ha, dec = hour_angle_dec_deg([90.0 - 40.0 + 10.0], [180.0], 40.0)
    assert ha[0] == pytest.approx(0.0, abs=1e-9) and dec[0] == pytest.approx(10.0, abs=1e-9)
    # West of the meridian the hour angle is positive.
    ha, _ = hour_angle_dec_deg([20.0], [250.0], 40.0)
    assert ha[0] > 0


# --- against recorded Horizons ---------------------------------------------------------------


def test_horizons_refraction_is_eval_saemundsson_above_minus_one_and_held_below() -> None:
    """The recorded tables show Horizons' refraction model: eval's formula, held below −1°.

    That is what makes the refraction domain (reference geometric altitude ≥ −1°) the place
    to judge the refracted target, and the band below it a convention.
    """
    for tag in ("20261121", "20261121T15-16"):
        _, air = _table(f"sun.airless.{tag}")
        _, refr = _table(f"sun.refracted.{tag}")
        alt = air.values["el_deg"]
        horizons_r = refr.values["el_deg"] - alt
        ours = np.array([refraction_deg(float(a)) for a in alt])
        held = refraction_deg(REFRACTION_DOMAIN_MIN_DEG)
        for a, h, o in zip(alt, horizons_r, ours, strict=True):
            if a >= REFRACTION_DOMAIN_MIN_DEG:
                assert h == pytest.approx(o, abs=1e-5), a
            else:
                assert o == 0.0
                assert h == pytest.approx(held, abs=1e-5), a
        assert np.array_equal(refr.values["az_deg"], air.values["az_deg"])


@needs_oculus
@pytest.mark.parametrize("body", ["sun", "moon"])
def test_oculus_meets_the_targets_on_the_recorded_day(body: str) -> None:
    c = _comparison(body, "20261121")
    target = TARGET_DEG[body]
    assert len(c) == 25
    assert float(np.max(c.sep_geo)) <= target
    domain = c.refraction_domain
    assert float(np.max(c.sep_refr[domain])) <= target
    # Below the cutoff the conventions differ by Horizons' held value, nothing else.
    held = refraction_deg(REFRACTION_DOMAIN_MIN_DEG)
    if (~domain).any():
        assert np.allclose(c.sep_refr[~domain], held, atol=0.01)
    print(body, "geometric worst arcsec", float(np.max(c.sep_geo)) * 3600.0)


@needs_oculus
def test_sunrise_window_in_two_minute_steps() -> None:
    c = _comparison("sun", "20261121T15-16")
    assert len(c) == 31
    assert c.ref_alt.min() < -3.0 and c.ref_alt.max() > 5.0
    assert float(np.max(c.sep_geo)) <= TARGET_DEG["sun"]
    assert float(np.max(np.abs(c.drefraction_own_alt[c.refraction_domain]))) < 1e-3
    assert not c.cutoff_straddle.any()


@needs_oculus
def test_refraction_models_alone_against_each_at_its_own_altitude() -> None:
    """Review finding: the models are compared at one altitude; the other number is the slope.

    On the recorded sunrise window (the Sun from −3° to +5°, through the −1..0 band) eval's
    formula at Horizons' own geometric altitude gives Horizons' refraction to 1e-5″, while
    each model at its own altitude differs by the refraction slope times oculus-core's altitude
    error, which is up to about 0.2 of that error near the horizon and never more.
    """
    c = _comparison("sun", "20261121T15-16")
    dom = c.refraction_domain & ~c.cutoff_straddle
    assert dom.sum() > 15
    model = c.drefraction_model[dom]
    assert float(np.max(np.abs(model))) * 3600.0 < 1e-4
    own = c.drefraction_own_alt[dom]
    dalt = np.abs(c.dalt_geo[dom])
    eps = 1e-4
    slope = np.array(
        [abs(refraction_deg(a + eps) - refraction_deg(a - eps)) / (2 * eps) for a in c.ref_alt[dom]]
    )
    assert slope.max() < 0.2
    # own − model is R(ours) − R(ref): the slope times the altitude error, to first order.
    assert np.all(np.abs(own - model) <= 1.05 * slope * dalt + 1e-9)
    assert float(np.max(np.abs(own))) > 10.0 * float(np.max(np.abs(model)))
    assert np.array_equal(c.ours_refr_at_ref_alt, refraction_array(c.ref_alt))


def test_horizon_dip_and_the_band_above_it() -> None:
    assert horizon_dip_deg(0.0) == 0.0
    assert horizon_dip_deg(-5.0) == 0.0
    assert horizon_dip_deg(2150.0) == pytest.approx(1.488, abs=0.001)  # Grand Canyon
    assert horizon_dip_deg(150.0) == pytest.approx(0.393, abs=0.001)  # Point Reyes
    #   in band, toward view | in band, away | below the dip | in the domain | in band, toward
    c = _synthetic([-1.2, -1.3, -1.6, -0.5, -1.05], [-1.2, -1.3, -1.6, -0.5, -1.05])
    c = BodyComparison(**{**c.__dict__, "ref_az": np.array([200.0, 20.0, 200.0, 200.0, 229.0])})
    out = dip_band([c], {"s": (2150.0, 200.0, 60.0)})
    entry = out["s"]["sun"]
    assert out["s"]["dip_deg"] == pytest.approx(1.488, abs=0.001)
    assert (entry["n"], entry["n_toward_view"], entry["beyond_target"]) == (3, 2, 3)
    assert entry["refracted_worst_deg"] == pytest.approx(refraction_deg(-1.0), abs=1e-3)
    assert len(entry["toward_view_times_utc"]) == 2
    assert dip_band([c], {"elsewhere": (2150.0, 200.0, 60.0)}) == {}
    low = dip_band([c], {"s": (150.0, 200.0, 60.0)})["s"]["sun"]
    assert low["n"] == 0 and low["refracted_worst_deg"] is None


@needs_oculus
def test_summaries_and_beyond_target_rows() -> None:
    items = [_comparison("sun", "20261121"), _comparison("moon", "20261121")]
    summary = summarize_comparisons(items)
    for body in ("sun", "moon"):
        block = summary[body]["all_sites"]
        assert block["n"] == 25
        assert block["geometric"]["worst"] <= TARGET_DEG[body]
        assert block["geometric"]["worst_site"] == "rainier.fixture"
        assert "worst_index" not in block["geometric"]
        assert set(block["geometric_by_band"]) <= {
            "-90..-18",
            "-18..-6",
            "-6..-1",
            "-1..0",
            "0..2",
            "2..5",
            "5..15",
            "15..45",
            "45..90",
        }
    assert summary["moon"]["all_sites"]["moon_illum_pct"]["worst"] < 0.1
    rows = beyond_target_rows(items)
    assert all(r["domain"] == "outside" for r in rows)
    assert count_beyond(rows) == {
        f"horizons/{b}/refracted/outside": sum(1 for r in rows if r["body"] == b)
        for b in sorted({r["body"] for r in rows})
    }


def _synthetic(ref_alt: list[float], ours_alt: list[float]) -> BodyComparison:
    n = len(ref_alt)
    ref = np.asarray(ref_alt, dtype=np.float64)
    ours = np.asarray(ours_alt, dtype=np.float64)

    def refr(a: np.ndarray, *, hold: bool) -> np.ndarray:
        floor = REFRACTION_DOMAIN_MIN_DEG
        return np.array(
            [x + refraction_deg(max(x, floor) if hold else float(x)) for x in a], dtype=np.float64
        )

    return BodyComparison(
        source="synthetic",
        site_id="s",
        body="sun",
        unix_ms=np.arange(n, dtype=np.float64) * 3.6e6,
        ref_alt=ref,
        ref_az=np.full(n, 200.0),
        ref_alt_refr=refr(ref, hold=True),
        ref_az_refr=np.full(n, 200.0),
        ours_alt=ours,
        ours_az=np.full(n, 200.0),
        ours_alt_refr=refr(ours, hold=False),
        ours_refr_at_ref_alt=refraction_array(ref),
        radec_sep=np.full(n, math.nan),
        illum_diff_pct=np.full(n, math.nan),
    )


def test_a_cutoff_straddle_is_reported_inside_the_domain_with_its_cause() -> None:
    c = _synthetic([-0.9999, 10.0, -5.0], [-1.0003, 10.0, -5.0])
    assert c.cutoff_straddle.tolist() == [True, False, False]
    rows = beyond_target_rows([c])
    inside = [r for r in rows if r["domain"] == "target"]
    assert len(inside) == 1 and "straddle" in inside[0]["cause"]
    assert inside[0]["sep_deg"] == pytest.approx(0.647, abs=0.001)
    outside = [r for r in rows if r["domain"] == "outside"]
    assert len(outside) == 1 and "convention" in outside[0]["cause"]
    assert "out of sight" not in outside[0]["cause"]  # the review: -1 deg is not a view limit
    # The models alone see no straddle: both are evaluated at the reference's altitude.
    assert abs(float(c.drefraction_model[0])) < abs(float(c.drefraction_own_alt[0])) / 100.0
