"""Hand-computed checks for lookout_eval.scores (PRD §13.5, §13.7)."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest

from lookout_eval.scores import (
    Contingency,
    _percentile_interval,
    _two_sided_p,
    block_bootstrap_indices,
    bootstrap_pss,
    contingency,
    csi,
    far,
    frequency_bias,
    holm_bonferroni,
    local_day_ordinals,
    paired_delta_pss,
    period_skill_scores,
    pod,
    pofd,
    pss,
    skill_score,
    wilson_interval,
)

# forecast/observed pairs worked by hand: H = 2 (0, 4), F = 2 (1, 6), M = 1 (2), C = 3 (3, 5, 7)
F8 = [True, True, False, False, True, False, True, False]
O8 = [True, False, True, False, True, False, False, False]


def test_hand_computed_table_and_scores() -> None:
    t = contingency(F8, O8)
    assert (t.hits, t.misses, t.false_alarms, t.correct_negatives) == (2, 1, 2, 3)
    assert t.n == 8 and t.excluded == 0 and t.partial == 0
    assert pod(t) == pytest.approx(2 / 3)
    assert pofd(t) == pytest.approx(2 / 5)
    assert pss(t) == pytest.approx(2 / 3 - 2 / 5)  # 4/15
    assert far(t) == pytest.approx(0.5)
    assert csi(t) == pytest.approx(2 / 5)
    assert frequency_bias(t) == pytest.approx(4 / 3)
    assert t.base_rate == pytest.approx(3 / 8)
    assert t.undefined() == {}


def test_finley_1884_tornado_table() -> None:
    # Finley's tornado forecasts, the textbook case for PSS (H=28, M=23, F=72, C=2680).
    t = Contingency(hits=28, misses=23, false_alarms=72, correct_negatives=2680)
    assert t.pod == pytest.approx(28 / 51)
    assert t.pofd == pytest.approx(72 / 2752)
    assert t.pss == pytest.approx(0.5229, abs=5e-5)
    assert t.far == pytest.approx(0.72)
    assert t.csi == pytest.approx(28 / 123)
    assert t.bias == pytest.approx(100 / 51)


def test_masks_exclude_and_count() -> None:
    excluded = [True, False, False, False, False, False, False, False]
    partial = [False, False, True, False, False, False, False, False]
    t = contingency(F8, O8, excluded=excluded, partial=partial)
    # pair 0 (a hit) is excluded and pair 2 (a miss) is partial
    assert (t.hits, t.misses, t.false_alarms, t.correct_negatives) == (1, 0, 2, 3)
    assert (t.excluded, t.partial) == (1, 1)
    assert t.pss == pytest.approx(1.0 - 2 / 5)


def test_pair_in_both_masks_counts_as_excluded_only() -> None:
    both = [True] + [False] * 7
    t = contingency(F8, O8, excluded=both, partial=both)
    assert (t.excluded, t.partial, t.n) == (1, 0, 7)


def test_no_observed_events_makes_pss_undefined() -> None:
    t = contingency([True, False, False], [False, False, False])
    assert t.pss is None and t.pod is None and t.bias is None
    assert t.pofd == pytest.approx(1 / 3)
    assert t.far == 1.0
    assert t.csi == 0.0
    assert set(t.undefined()) == {"pod", "bias", "pss"}


def test_all_events_makes_pss_undefined() -> None:
    t = contingency([True, False, True], [True, True, True])
    assert t.pss is None and t.pofd is None
    assert t.pod == pytest.approx(2 / 3)
    assert "pss" in t.undefined() and "pofd" in t.undefined()


def test_empty_table_is_all_undefined() -> None:
    t = contingency([], [])
    assert t.n == 0
    assert all(value is None for value in t.scores().values())
    assert t.to_dict()["undefined"]["base_rate"] == "no scored pairs"


def test_never_forecast_far_undefined() -> None:
    t = contingency([False, False], [True, False])
    assert t.far is None and t.pss == pytest.approx(0.0)


def test_contingency_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError, match="differ in length"):
        contingency([True], [True, False])
    with pytest.raises(TypeError, match="boolean"):
        contingency([0.5, 1.0], [True, False])
    with pytest.raises(TypeError, match="boolean"):
        contingency(np.array([True, None], dtype=object), [True, False])
    with pytest.raises(ValueError):
        Contingency(-1, 0, 0, 0)
    assert contingency([1, 0], [1, 1]).hits == 1  # exact 0/1 integers are accepted


def test_tables_add_for_pooling() -> None:
    a = Contingency(1, 2, 3, 4, excluded=1)
    b = Contingency(10, 20, 30, 40, partial=2)
    s = a + b
    assert (s.hits, s.misses, s.false_alarms, s.correct_negatives) == (11, 22, 33, 44)
    assert (s.excluded, s.partial) == (1, 2)


def test_to_dict_is_json_ready() -> None:
    import json

    d = contingency(F8, O8).to_dict()
    assert json.loads(json.dumps(d))["pss"] == pytest.approx(4 / 15)


def test_wilson_reference_values() -> None:
    low, high = wilson_interval(0, 10)
    assert low == 0.0 and high == pytest.approx(0.27753, abs=5e-5)
    low, high = wilson_interval(5, 10)
    assert low == pytest.approx(0.23659, abs=5e-5) and high == pytest.approx(0.76341, abs=5e-5)
    low, high = wilson_interval(10, 10)
    assert low == pytest.approx(0.72247, abs=5e-5) and high == 1.0
    assert wilson_interval(0, 0) is None
    with pytest.raises(ValueError):
        wilson_interval(11, 10)
    with pytest.raises(TypeError):
        wilson_interval(1.5, 10)  # type: ignore[arg-type]


def test_wilson_level_changes_width() -> None:
    narrow = wilson_interval(30, 100, level=0.80)
    wide = wilson_interval(30, 100, level=0.99)
    assert narrow is not None and wide is not None
    assert (wide[1] - wide[0]) > (narrow[1] - narrow[0])


def test_skill_score_is_ratio_of_totals() -> None:
    assert skill_score([1, 2, 3], [2, 2, 2]) == pytest.approx(0.0)
    assert skill_score([0, 1], [2, 2]) == pytest.approx(0.75)
    # a mean of per-pair ratios would give 1 - (0/1 + 4/1000)/2 = 0.998; totals give 0.996
    assert skill_score([0, 4], [1, 1000]) == pytest.approx(1 - 4 / 1001)
    assert skill_score([1, 1], [0, 0]) is None
    assert skill_score([], []) is None
    assert skill_score([5, 1], [1, 1], mask=[False, True]) == pytest.approx(0.0)
    with pytest.raises(ValueError, match="non-negative"):
        skill_score([-1], [1])
    with pytest.raises(ValueError, match="non-finite"):
        skill_score([math.nan], [1])


def test_period_skill_scores_use_whole_periods() -> None:
    out = period_skill_scores([1, 1, 0, 2], [2, 2, 1, 1], ["dec", "dec", "jan", "jan"])
    assert out == {"dec": pytest.approx(0.5), "jan": pytest.approx(0.0)}


def test_holm_hand_example() -> None:
    entries = holm_bonferroni({"K1": 0.01, "K2": 0.04, "K3": 0.03, "K4": 0.005}, alpha=0.05)
    by_name = {e.name: e for e in entries}
    assert by_name["K4"].adjusted == pytest.approx(0.02)
    assert by_name["K1"].adjusted == pytest.approx(0.03)
    assert by_name["K3"].adjusted == pytest.approx(0.06)
    assert by_name["K2"].adjusted == pytest.approx(0.06)  # max(0.04, previous 0.06)
    assert [e.reject for e in entries] == [True, False, False, True]
    assert [e.name for e in entries] == ["K1", "K2", "K3", "K4"]


def test_holm_caps_and_validates() -> None:
    entries = holm_bonferroni([0.6, 0.9])
    assert [e.adjusted for e in entries] == [pytest.approx(1.0), pytest.approx(1.0)]
    assert holm_bonferroni([]) == []
    with pytest.raises(ValueError):
        holm_bonferroni([1.2])
    with pytest.raises(ValueError):
        holm_bonferroni([math.nan])


def test_local_day_ordinals_follow_local_dates() -> None:
    # 16:00 UTC and 00:30 UTC next day are both 18 Nov in Pacific time.
    times = [datetime(2026, 11, 18, 16, 0, tzinfo=UTC), datetime(2026, 11, 19, 0, 30, tzinfo=UTC)]
    days = local_day_ordinals(times, "America/Los_Angeles")
    assert days[0] == days[1] == date(2026, 11, 18).toordinal()
    with pytest.raises(ValueError):
        local_day_ordinals([datetime(2026, 11, 18)], "America/Los_Angeles")


def test_block_indices_moving_circular_are_consecutive_runs() -> None:
    rng = np.random.default_rng(1)
    idx = block_bootstrap_indices(10, 3, 50, rng, method="moving", circular=True)
    assert idx.shape == (50, 10)
    assert idx.min() >= 0 and idx.max() <= 9
    # inside each block of 3, consecutive (mod 10)
    for row in idx:
        for start in range(0, 9, 3):
            block = row[start : start + 3]
            assert all((block[k + 1] - block[k]) % 10 == 1 for k in range(len(block) - 1))


def test_block_indices_moving_noncircular_stay_in_range() -> None:
    rng = np.random.default_rng(2)
    idx = block_bootstrap_indices(7, 3, 200, rng, method="moving", circular=False)
    for row in idx:
        for start in range(0, 6, 3):
            block = row[start : start + 3]
            assert all(block[k + 1] - block[k] == 1 for k in range(len(block) - 1))
    # end days are under-sampled without wrapping: day 0 only starts a block
    counts = np.bincount(idx.ravel(), minlength=7)
    assert counts[0] < counts[3]


def test_block_indices_stationary_wraps_and_has_runs() -> None:
    rng = np.random.default_rng(3)
    idx = block_bootstrap_indices(20, 4, 100, rng, method="stationary")
    assert idx.shape == (100, 20)
    steps = (np.diff(idx, axis=1) % 20 == 1).mean()
    assert 0.6 < steps < 0.9  # continuation probability is 1 - 1/4, plus chance continuations


def test_block_indices_validate() -> None:
    rng = np.random.default_rng(0)
    with pytest.raises(ValueError, match="at least 1 day"):
        block_bootstrap_indices(10, 0, 5, rng)
    with pytest.raises(TypeError):
        block_bootstrap_indices(10, 1.5, 5, rng)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown"):
        block_bootstrap_indices(10, 1, 5, rng, method="iid")  # type: ignore[arg-type]


def _daily_pairs(n_days: int, per_day: int, seed: int):
    """Synthetic pairs with events and non-events on every day."""
    rng = np.random.default_rng(seed)
    days, observed = [], []
    for d in range(n_days):
        obs = np.zeros(per_day, dtype=bool)
        obs[: per_day // 2] = True
        rng.shuffle(obs)
        days.extend([d] * per_day)
        observed.extend(obs.tolist())
    return np.array(days), np.array(observed)


def test_paired_delta_identical_forecasts_is_zero() -> None:
    days, obs = _daily_pairs(12, 6, seed=4)
    rng = np.random.default_rng(5)
    fc = rng.random(obs.size) < 0.5
    r = paired_delta_pss(fc, fc, obs, days, seed=7, n_boot=500, block_length_days=2)
    assert r.delta == pytest.approx(0.0)
    assert (r.ci_low, r.ci_high) == (pytest.approx(0.0), pytest.approx(0.0))
    assert r.p_value == 1.0 and r.excludes_zero is False


def test_paired_delta_perfect_vs_inverted() -> None:
    days, obs = _daily_pairs(10, 4, seed=8)
    r = paired_delta_pss(obs, ~obs, obs, days, seed=1, n_boot=300)
    assert r.pss_model == 1.0 and r.pss_reference == -1.0 and r.delta == 2.0
    assert r.ci_low == pytest.approx(2.0) and r.ci_high == pytest.approx(2.0)
    assert r.n_undefined == 0 and r.excludes_zero is True and r.p_value == 0.0
    assert r.n_pairs == 40 and r.n_days_spanned == 10 and r.n_days_with_pairs == 10


def test_paired_delta_is_seeded_and_deterministic() -> None:
    days, obs = _daily_pairs(20, 5, seed=9)
    rng = np.random.default_rng(10)
    a = np.where(rng.random(obs.size) < 0.8, obs, ~obs)
    b = np.where(rng.random(obs.size) < 0.6, obs, ~obs)
    r1 = paired_delta_pss(a, b, obs, days, seed=123, n_boot=400, block_length_days=3)
    r2 = paired_delta_pss(a, b, obs, days, seed=123, n_boot=400, block_length_days=3)
    r3 = paired_delta_pss(a, b, obs, days, seed=124, n_boot=400, block_length_days=3)
    assert (r1.ci_low, r1.ci_high) == (r2.ci_low, r2.ci_high)
    assert (r1.ci_low, r1.ci_high) != (r3.ci_low, r3.ci_high)
    assert r1.delta == pytest.approx(contingency(a, obs).pss - contingency(b, obs).pss)
    assert r1.ci_low <= r1.delta <= r1.ci_high


def test_paired_delta_counts_undefined_resamples() -> None:
    # events only on day 0 and non-events only on day 1: many resamples lack one class
    days = np.array([0, 0, 1, 1])
    obs = np.array([True, True, False, False])
    fc = np.array([True, False, False, True])
    r = paired_delta_pss(fc, ~fc, obs, days, seed=3, n_boot=400)
    assert r.n_undefined > 0
    assert any("undefined" in flag for flag in r.flags)


def test_paired_delta_masks_apply_to_both() -> None:
    days, obs = _daily_pairs(6, 4, seed=11)
    fc = obs.copy()
    excluded = np.zeros(obs.size, dtype=bool)
    excluded[:4] = True
    r = paired_delta_pss(fc, ~fc, obs, days, seed=1, n_boot=100, excluded=excluded)
    assert r.table_model.excluded == r.table_reference.excluded == 4
    assert r.n_pairs == obs.size - 4


def test_paired_delta_calendar_gaps_become_empty_days() -> None:
    days = np.array([0, 0, 0, 0, 5, 5, 5, 5])  # an outage on days 1-4
    obs = np.array([True, False, True, False] * 2)
    r = paired_delta_pss(obs, ~obs, obs, days, seed=1, n_boot=200)
    assert r.n_days_spanned == 6 and r.n_days_with_pairs == 2


def test_paired_delta_accepts_dates_and_rejects_datetimes() -> None:
    base = date(2026, 12, 15)
    days = [base + timedelta(days=i // 2) for i in range(8)]
    obs = np.array([True, False] * 4)
    r = paired_delta_pss(obs, obs, obs, days, seed=1, n_boot=50)
    assert r.n_days_spanned == 4
    with pytest.raises(TypeError, match="local_day_ordinals"):
        paired_delta_pss(obs, obs, obs, [datetime(2026, 12, 15, tzinfo=UTC)] * 8, seed=1)


def test_datetime64_day_labels_need_the_day_unit() -> None:
    """Review finding: datetime64[m] used to be floored to UTC dates without a word."""
    obs = np.array([True, False])
    times = np.array(["2026-12-15T23:30", "2026-12-16T00:30"], dtype="datetime64[m]")
    with pytest.raises(TypeError, match="local_day_ordinals"):
        paired_delta_pss(obs, ~obs, obs, times, seed=1, n_boot=50)
    with pytest.raises(TypeError, match="unit 'D'"):
        paired_delta_pss(obs, ~obs, obs, np.array(["2026-12", "2026-12"], "datetime64[M]"), seed=1)
    with pytest.raises(ValueError, match="NaT"):
        paired_delta_pss(obs, ~obs, obs, np.array(["2026-12-15", "NaT"], "datetime64[D]"), seed=1)
    days = np.array(["2026-12-15", "2026-12-15"], dtype="datetime64[D]")
    assert paired_delta_pss(obs, ~obs, obs, days, seed=1, n_boot=50).n_days_spanned == 1
    # the same two times as local Pacific days: one day
    utc = [datetime(2026, 12, 15, 23, 30, tzinfo=UTC), datetime(2026, 12, 16, 0, 30, tzinfo=UTC)]
    local = local_day_ordinals(utc, "America/Los_Angeles")
    assert paired_delta_pss(obs, ~obs, obs, local, seed=1, n_boot=50).n_days_spanned == 1


def test_interval_and_p_value_agree_at_the_boundary() -> None:
    """Order statistics, not interpolation: p < alpha exactly when the interval excludes 0.

    200 resamples at 95 %: k = 5, so the interval runs from the 5th smallest to the 5th
    largest value.
    """
    four = np.concatenate([np.full(4, -0.1), np.full(196, 0.2)])
    assert _two_sided_p(four) == pytest.approx(0.04)
    assert _percentile_interval(four, 0.95) == (0.2, 0.2)  # excludes 0, p < 0.05
    five = np.concatenate([np.full(5, -0.1), np.full(195, 0.2)])
    assert _two_sided_p(five) == pytest.approx(0.05)
    low, _ = _percentile_interval(five, 0.95)
    assert low == -0.1  # p = alpha: not below it, and the interval does not exclude 0
    ties = np.concatenate([np.zeros(3), np.full(197, 0.2)])  # zeros count in both tails
    assert _two_sided_p(ties) == pytest.approx(0.03) and _percentile_interval(ties, 0.95)[0] > 0
    upper = -five
    assert (
        _two_sided_p(upper) == pytest.approx(0.05) and _percentile_interval(upper, 0.95)[1] == 0.1
    )


def test_paired_delta_no_scored_pairs() -> None:
    obs = np.array([True, False])
    r = paired_delta_pss(obs, obs, obs, [0, 0], seed=1, excluded=[True, True])
    assert r.delta is None and r.ci_low is None and "no scored pairs" in r.flags


def test_bootstrap_single_pss_interval_contains_estimate() -> None:
    days, obs = _daily_pairs(15, 6, seed=12)
    rng = np.random.default_rng(13)
    fc = np.where(rng.random(obs.size) < 0.75, obs, ~obs)
    r = bootstrap_pss(fc, obs, days, seed=5, n_boot=600, block_length_days=2)
    assert r.ci_low <= r.pss <= r.ci_high
    assert r.table.n == obs.size and r.n_days_with_pairs == 15


def test_stationary_and_moving_give_similar_widths() -> None:
    days, obs = _daily_pairs(30, 6, seed=14)
    rng = np.random.default_rng(15)
    a = np.where(rng.random(obs.size) < 0.8, obs, ~obs)
    b = np.where(rng.random(obs.size) < 0.65, obs, ~obs)
    mb = paired_delta_pss(a, b, obs, days, seed=2, n_boot=2000, block_length_days=3)
    sb = paired_delta_pss(
        a, b, obs, days, seed=2, n_boot=2000, block_length_days=3, method="stationary"
    )
    width_mb, width_sb = mb.ci_high - mb.ci_low, sb.ci_high - sb.ci_low
    assert 0.6 < width_sb / width_mb < 1.6


def test_interval_coverage_on_iid_days_is_near_nominal() -> None:
    """Calibration check: 95 % intervals for a known ΔPSS cover it in roughly 95 % of trials.

    Days are iid here, so a 1-day block is correct. 200 trials x 1,000 resamples, fixed
    seeds; the binomial 99.9 % band for 200 trials at p = 0.95 is about [0.89, 0.99].
    """
    n_trials, covered = 200, 0
    true_delta = 0.6 - 0.2  # PSS of a = 2*0.8-1, of b = 2*0.6-1, with balanced classes
    for trial in range(n_trials):
        rng = np.random.default_rng(1000 + trial)
        n_days, per_day = 30, 8
        obs = np.tile(np.array([True, False] * (per_day // 2)), n_days)
        days = np.repeat(np.arange(n_days), per_day)
        a = np.where(rng.random(obs.size) < 0.8, obs, ~obs)
        b = np.where(rng.random(obs.size) < 0.6, obs, ~obs)
        r = paired_delta_pss(a, b, obs, days, seed=trial, n_boot=1000)
        covered += int(r.ci_low <= true_delta <= r.ci_high)
    assert 0.89 <= covered / n_trials <= 0.99
