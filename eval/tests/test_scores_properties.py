"""Property tests (hypothesis) for snow_globe_eval.scores (PRD §13.1 "Property", §13.5)."""

from __future__ import annotations

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from snow_globe_eval.scores import (
    Contingency,
    block_bootstrap_indices,
    contingency,
    holm_bonferroni,
    paired_delta_pss,
    wilson_interval,
)

bool_pairs = st.integers(min_value=0, max_value=60).flatmap(
    lambda n: st.tuples(
        st.lists(st.booleans(), min_size=n, max_size=n),
        st.lists(st.booleans(), min_size=n, max_size=n),
        st.lists(st.booleans(), min_size=n, max_size=n),
        st.lists(st.booleans(), min_size=n, max_size=n),
    )
)

tables = st.builds(
    Contingency,
    st.integers(0, 500),
    st.integers(0, 500),
    st.integers(0, 500),
    st.integers(0, 500),
)


@given(bool_pairs)
def test_counts_partition_the_pairs(data) -> None:
    f, o, ex, pa = data
    t = contingency(f, o, excluded=ex, partial=pa)
    assert t.n + t.excluded + t.partial == len(f)


@given(tables)
def test_scores_stay_in_range_when_defined(t: Contingency) -> None:
    for name in ("pod", "pofd", "far", "csi", "base_rate"):
        value = getattr(t, name)
        assert value is None or 0.0 <= value <= 1.0
    assert t.pss is None or -1.0 <= t.pss <= 1.0
    assert t.bias is None or t.bias >= 0.0
    assert (t.pss is None) == (t.n_events == 0 or t.n_non_events == 0)


@given(tables)
def test_pss_is_invariant_to_relabelling_events(t: Contingency) -> None:
    # swapping event and non-event maps (H, M, F, C) to (C, F, M, H); PSS is symmetric
    swapped = Contingency(t.correct_negatives, t.false_alarms, t.misses, t.hits)
    if t.pss is None:
        assert swapped.pss is None
    else:
        assert abs(swapped.pss - t.pss) < 1e-12


@given(st.lists(st.booleans(), min_size=2, max_size=80))
def test_perfect_and_inverted_forecasts(obs: list[bool]) -> None:
    o = np.array(obs)
    perfect, inverted = contingency(o, o), contingency(~o, o)
    if o.any() and not o.all():
        assert perfect.pss == 1.0 and inverted.pss == -1.0
    else:
        assert perfect.pss is None and inverted.pss is None


@given(st.integers(0, 400).flatmap(lambda n: st.tuples(st.integers(0, n), st.just(n))))
def test_wilson_contains_the_estimate(kn) -> None:
    k, n = kn
    interval = wilson_interval(k, n)
    if n == 0:
        assert interval is None
        return
    low, high = interval
    assert 0.0 <= low <= k / n <= high <= 1.0


@given(st.lists(st.floats(0.0, 1.0, allow_nan=False), min_size=1, max_size=12))
def test_holm_adjusted_values_are_monotone_and_bounded(ps: list[float]) -> None:
    entries = holm_bonferroni(ps)
    for entry in entries:
        assert entry.p_value <= entry.adjusted + 1e-15 <= 1.0 + 1e-15
    ordered = sorted(entries, key=lambda e: e.p_value)
    adjusted = [e.adjusted for e in ordered]
    assert adjusted == sorted(adjusted)
    # rejections form a prefix of the sorted order
    rejects = [e.reject for e in ordered]
    assert rejects == sorted(rejects, reverse=True)


@settings(max_examples=40, deadline=None)
@given(
    st.integers(1, 40),
    st.integers(1, 10),
    st.sampled_from(["moving", "stationary"]),
    st.booleans(),
    st.integers(0, 2**32 - 1),
)
def test_bootstrap_indices_shape_and_range(n_days, length, method, circular, seed) -> None:
    rng = np.random.default_rng(seed)
    idx = block_bootstrap_indices(n_days, length, 7, rng, method=method, circular=circular)
    assert idx.shape == (7, n_days)
    assert idx.min() >= 0 and idx.max() < n_days


@settings(max_examples=25, deadline=None)
@given(st.integers(0, 2**31 - 1), st.integers(1, 4))
def test_delta_is_antisymmetric_in_model_and_reference(seed: int, length: int) -> None:
    rng = np.random.default_rng(seed)
    n_days, per_day = 12, 4
    obs = np.tile([True, False, True, False], n_days)
    days = np.repeat(np.arange(n_days), per_day)
    a = rng.random(obs.size) < 0.5
    b = rng.random(obs.size) < 0.5
    ab = paired_delta_pss(a, b, obs, days, seed=seed, n_boot=200, block_length_days=length)
    ba = paired_delta_pss(b, a, obs, days, seed=seed, n_boot=200, block_length_days=length)
    assert abs(ab.delta + ba.delta) < 1e-12
    assert abs(ab.ci_low + ba.ci_high) < 1e-12 and abs(ab.ci_high + ba.ci_low) < 1e-12


@settings(max_examples=80, deadline=None)
@given(
    st.integers(0, 2**31 - 1),
    st.sampled_from([0.8, 0.9, 0.95]),
    st.sampled_from([40, 200, 333]),
    st.floats(0.3, 0.95),
)
def test_p_below_alpha_exactly_when_the_interval_excludes_zero(
    seed: int, level: float, n_boot: int, skill: float
) -> None:
    rng = np.random.default_rng(seed)
    n_days, per_day = 8, 4
    obs = np.tile([True, False, True, False], n_days)
    days = np.repeat(np.arange(n_days), per_day)
    a = np.where(rng.random(obs.size) < skill, obs, ~obs)
    b = np.where(rng.random(obs.size) < 0.5, obs, ~obs)
    r = paired_delta_pss(a, b, obs, days, seed=seed, n_boot=n_boot, level=level)
    if r.p_value is None:
        assert r.excludes_zero is None
        return
    assert r.excludes_zero == (r.p_value < round(1.0 - level, 9))
