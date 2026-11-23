"""Tests for stats.py: known answers for percentiles, the Wilson interval and the cluster
bootstrap. Every input is made up; nothing here is a measurement.

    python3 -I -B -m unittest discover -s bench/replay -t bench/replay -p 'test_*.py'
"""

from __future__ import annotations

import math
import unittest
from collections import Counter
from itertools import product

import stats


def obs(utterance: str, value: float, session: int | None = None) -> stats.Observation:
    """A trial adding `value` to the percentile metric `v` and to the mean `m`."""
    return stats.Observation(utterance, session, {"v": [value]}, {"m": (value, 1.0)})


class Quantiles(unittest.TestCase):
    """Type 7, as numpy's default."""

    def test_known_values(self) -> None:
        self.assertEqual(stats.quantile([1, 2, 3, 4], 0.5), 2.5)
        self.assertAlmostEqual(stats.quantile([1, 2, 3, 4], 0.95), 3.85)
        self.assertAlmostEqual(stats.quantile(list(range(1, 11)), 0.95), 9.55)
        self.assertEqual(stats.quantile([7], 0.95), 7)
        self.assertEqual(stats.quantile([1, 5, 9], 0.0), 1)
        self.assertEqual(stats.quantile([1, 5, 9], 1.0), 9)
        with self.assertRaises(ValueError):
            stats.quantile([], 0.5)
        with self.assertRaises(ValueError):
            stats.quantile([1], 1.5)


class Wilson(unittest.TestCase):
    """The score interval without continuity correction."""

    def test_newcombe_1998_table(self) -> None:
        # Newcombe, Statistics in Medicine 17 (1998) 857-872, Table I, method 3 (Wilson score).
        for (k, n), (lo, hi) in {
            (81, 263): (0.2553, 0.3662),
            (15, 148): (0.0624, 0.1605),
            (0, 20): (0.0000, 0.1611),
            (1, 29): (0.0061, 0.1718),
            (29, 29): (0.8830, 1.0000),
        }.items():
            got = stats.wilson(k, n)
            self.assertAlmostEqual(got[0], lo, places=4, msg=(k, n))
            self.assertAlmostEqual(got[1], hi, places=4, msg=(k, n))

    def test_closed_forms(self) -> None:
        z = stats.Z_TWO_SIDED_95
        for n in (1, 10, 100, 200):
            self.assertAlmostEqual(stats.wilson(0, n)[1], z * z / (n + z * z))
            self.assertEqual(stats.wilson(0, n)[0], 0.0)
            self.assertAlmostEqual(stats.wilson(n, n)[0], n / (n + z * z))
            lo, hi = stats.wilson(n, 2 * n)
            self.assertAlmostEqual(lo + hi, 1.0)  # symmetric about 1/2

    def test_the_ends_are_the_roots_of_the_score_equation(self) -> None:
        for k, n, z in ((3, 17, stats.Z_TWO_SIDED_95), (40, 50, stats.Z_ONE_SIDED_95)):
            for p in stats.wilson(k, n, z):
                self.assertAlmostEqual((k / n - p) ** 2, z * z * p * (1 - p) / n, places=12)

    def test_z_values(self) -> None:
        self.assertAlmostEqual(stats.Z_TWO_SIDED_95, 1.959963984540054, places=12)
        self.assertAlmostEqual(stats.Z_ONE_SIDED_95, 1.6448536269514722, places=12)
        with self.assertRaises(ValueError):
            stats.wilson(1, 0)
        with self.assertRaises(ValueError):
            stats.wilson(3, 2)


class Bootstrap(unittest.TestCase):
    """Known answers for the cluster bootstrap."""

    def test_the_floor_is_ten_thousand(self) -> None:
        self.assertEqual(stats.MIN_RESAMPLES, 10_000)

    def test_identical_clusters_give_a_point_interval(self) -> None:
        r = stats.bootstrap(
            [obs(f"u{i}", 5.0) for i in range(8)], {"v": (0.5, 0.95)}, ["m"], 500, 1
        )
        for e in (r.quantiles[("v", 0.5)], r.quantiles[("v", 0.95)], r.ratios["m"]):
            self.assertEqual((e.value, e.low, e.high, e.valid_resamples), (5.0, 5.0, 5.0, 500))

    def test_two_clusters_have_three_resampled_means(self) -> None:
        # Resampling {0, 1} gives a mean of 0, 1/2 or 1 with probabilities 1/4, 1/2, 1/4.
        r = 10_000
        dist = Counter(self._means([obs("a", 0.0), obs("b", 1.0)], r, seed=7))
        self.assertEqual(set(dist), {0.0, 0.5, 1.0})
        for value, p in ((0.0, 0.25), (0.5, 0.5), (1.0, 0.25)):
            sd = math.sqrt(r * p * (1 - p))
            self.assertLess(abs(dist[value] - r * p), 4 * sd, value)
        result = stats.bootstrap([obs("a", 0.0), obs("b", 1.0)], {}, ["m"], r, 7)
        self.assertEqual((result.ratios["m"].low, result.ratios["m"].high), (0.0, 1.0))
        self.assertEqual(result.ratios["m"].value, 0.5)

    def test_whole_utterances_are_resampled(self) -> None:
        # 100 trials of one utterance at 0 and one trial of another at 1: a resample holds both
        # utterances, one twice or the other twice, so the mean is 1/101, 0 or 1, nothing else.
        trials = [obs("many", 0.0) for _ in range(100)] + [obs("one", 1.0)]
        dist = Counter(self._means(trials, 2_000, seed=3))
        self.assertEqual(set(dist), {0.0, 1 / 101, 1.0})

    def test_the_bootstrap_variance_of_a_mean(self) -> None:
        # One trial per utterance: the bootstrap variance of the mean is the plug-in variance
        # over n, Σ(x - x̄)² / n².
        xs = [float(x) for x in (3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5, 8, 9, 7, 9)]
        n, mean = len(xs), sum(xs) / len(xs)
        want = sum((x - mean) ** 2 for x in xs) / n / n
        means = self._means([obs(f"u{i}", x) for i, x in enumerate(xs)], 20_000, seed=11)
        m = sum(means) / len(means)
        got = sum((v - m) ** 2 for v in means) / (len(means) - 1)
        self.assertLess(abs(got / want - 1), 0.05)

    def test_the_pigeonhole_distribution_for_two_by_two(self) -> None:
        # Utterances u0, u1 crossed with sessions s0, s1, one trial per cell, values 0..3. The
        # exact bootstrap distribution of the mean, enumerated over both factors' resamples.
        values = {("u0", 0): 0.0, ("u0", 1): 1.0, ("u1", 0): 2.0, ("u1", 1): 3.0}
        trials = [obs(u, v, s) for (u, s), v in values.items()]
        exact: Counter[float] = Counter()
        for du in product((0, 1), repeat=2):
            for ds in product((0, 1), repeat=2):
                wu, ws = Counter(du), Counter(ds)
                num = sum(wu[ui] * ws[s] * values[(f"u{ui}", s)] for ui in (0, 1) for s in (0, 1))
                den = sum(wu[ui] * ws[s] for ui in (0, 1) for s in (0, 1))
                exact[num / den] += 1 / 16
        self.assertEqual(
            dict(exact),
            {
                0.0: 1 / 16,
                0.5: 2 / 16,
                1.0: 3 / 16,
                1.5: 4 / 16,
                2.0: 3 / 16,
                2.5: 2 / 16,
                3.0: 1 / 16,
            },
        )
        r = 16_000
        dist = Counter(self._means(trials, r, seed=5))
        self.assertEqual(set(dist), set(exact))
        for value, p in exact.items():
            self.assertLess(abs(dist[value] - r * p), 4 * math.sqrt(r * p * (1 - p)), value)
        result = stats.bootstrap(trials, {}, ["m"], 200, 5)
        self.assertEqual((result.method, result.sessions), ("utterance x session (pigeonhole)", 2))
        one_way = stats.bootstrap(trials, {}, ["m"], 200, 5, by_session=False)
        self.assertEqual(one_way.method, "utterance")

    def test_one_session_is_the_one_way_bootstrap(self) -> None:
        trials = [obs(f"u{i}", float(i), session=4) for i in range(6)]
        a = stats.bootstrap(trials, {"v": (0.5,)}, ["m"], 300, 9)
        b = stats.bootstrap(
            [obs(f"u{i}", float(i)) for i in range(6)], {"v": (0.5,)}, ["m"], 300, 9
        )
        self.assertEqual(a.method, "utterance")
        self.assertEqual(a.ratios, b.ratios)
        self.assertEqual(a.quantiles, b.quantiles)

    def test_seeded_and_order_free(self) -> None:
        trials = [obs(f"u{i}", float(i % 5), session=i % 3) for i in range(30)]
        a = stats.bootstrap(trials, {"v": (0.5, 0.95)}, ["m"], 400, 42)
        b = stats.bootstrap(list(reversed(trials)), {"v": (0.5, 0.95)}, ["m"], 400, 42)
        self.assertEqual(a, b)  # trials are indexed by sorted utterance and session
        c = stats.bootstrap(trials, {"v": (0.5, 0.95)}, ["m"], 400, 43)
        self.assertNotEqual(a.ratios["m"], c.ratios["m"])
        self.assertEqual(stats.group_seed(1, "idle"), stats.group_seed(1, "idle"))
        self.assertNotEqual(stats.group_seed(1, "idle"), stats.group_seed(1, "warm"))
        self.assertNotEqual(stats.group_seed(1, "idle"), stats.group_seed(2, "idle"))

    def test_undefined_resamples_are_left_out_and_counted(self) -> None:
        # Only one of four utterances has a value: a resample that misses it has none.
        trials = [stats.Observation("a", None, {"v": [1.0]}, {"m": (1.0, 1.0)})]
        trials += [stats.Observation(u, None, {}, {}) for u in "bcd"]
        r = stats.bootstrap(trials, {"v": (0.5,)}, ["m"], 4_000, 2)
        e = r.quantiles[("v", 0.5)]
        # P(no "a" in 4 draws) = (3/4)^4 ≈ 0.316.
        self.assertLess(abs(e.valid_resamples / 4_000 - (1 - 0.75**4)), 0.03)
        self.assertEqual(e.valid_resamples, r.ratios["m"].valid_resamples)
        self.assertEqual((e.value, e.n), (1.0, 1))

    def test_no_trials(self) -> None:
        r = stats.bootstrap([], {"v": (0.5,)}, ["m"], 10, 1)
        self.assertEqual(r.quantiles[("v", 0.5)].value, None)
        self.assertEqual(r.ratios["m"].valid_resamples, 0)

    def test_a_number_that_is_not_finite_is_refused(self) -> None:
        # A NaN does not sort: it would silently move every percentile of its metric.
        for bad in (math.nan, math.inf, -math.inf):
            values = [stats.Observation("a", None, {"v": [1.0, bad]}, {})]
            with self.assertRaises(ValueError):
                stats.bootstrap(values, {"v": (0.5,)}, [], 10, 1)
            ratio = [stats.Observation("a", None, {}, {"m": (bad, 1.0)})]
            with self.assertRaises(ValueError):
                stats.bootstrap(ratio, {}, ["m"], 10, 1)

    @staticmethod
    def _means(trials: list[stats.Observation], resamples: int, seed: int) -> list[float]:
        """Every resample's mean, by running the bootstrap one resample at a time with the
        seeded generator's successive states (the same draws `bootstrap` makes)."""
        import random

        rng = random.Random(seed)
        utterances = sorted({t.utterance for t in trials})
        sessions = sorted({t.session for t in trials}, key=lambda s: (s is None, s or 0))
        two_way = len(sessions) >= 2
        cells: dict[tuple[int, int], float] = {}
        for t in trials:
            key = (utterances.index(t.utterance), sessions.index(t.session) if two_way else 0)
            cells[key] = cells.get(key, 0.0) + t.ratios["m"][0]
        counts: Counter[tuple[int, int]] = Counter(
            (utterances.index(t.utterance), sessions.index(t.session) if two_way else 0)
            for t in trials
        )
        out = []
        for _ in range(resamples):
            du = rng.choices(range(len(utterances)), k=len(utterances))
            ds = rng.choices(range(len(sessions)), k=len(sessions)) if two_way else [0]
            num = sum(cells.get((u, s), 0.0) for s in ds for u in du)
            den = sum(counts.get((u, s), 0) for s in ds for u in du)
            out.append(num / den)
        return out


class BootstrapMatchesItsOwnDraws(unittest.TestCase):
    """The helper above draws as `bootstrap` does, so its distributions are `bootstrap`'s."""

    def test_same_interval(self) -> None:
        trials = [obs(f"u{i}", float(i * i % 7), session=i % 2) for i in range(12)]
        means = sorted(Bootstrap._means(trials, 1_000, seed=8))
        r = stats.bootstrap(trials, {}, ["m"], 1_000, 8)
        self.assertAlmostEqual(r.ratios["m"].low, stats.quantile(means, stats.CI_LOW))
        self.assertAlmostEqual(r.ratios["m"].high, stats.quantile(means, stats.CI_HIGH))


if __name__ == "__main__":
    unittest.main()
