"""Statistics for the replay report: percentiles, the Wilson interval and a seeded cluster
bootstrap (PRD §10.6 "a cluster bootstrap by utterance", §7.1 lever 2 and §14 "Wilson upper
bound").

- **Percentiles** are Hyndman and Fan's type 7 (linear between the closest ranks; numpy's
  default), for the point estimate and inside every resample alike.
- **Wilson** is the score interval without continuity correction (Wilson 1927; Newcombe 1998,
  method 3). The report gives the upper end of the two-sided 95 % interval and, beside it, the
  one-sided 95 % bound.
- **The bootstrap** resamples whole utterances with replacement: every trial of a drawn utterance
  comes along, so the interval reflects that repeated plays of one recording are correlated. When
  a group's trials come from two or more harness sessions it resamples sessions too, independently
  of utterances, each trial weighted by the product of its utterance's and its session's counts
  (the pigeonhole bootstrap for crossed factors, Owen 2007, Annals of Applied Statistics 1(2)).
  Intervals are the 2.5 % and 97.5 % percentiles of the resampled statistic. A resample whose
  statistic is undefined (no values, or a zero denominator) is left out and counted. A value that
  is not a finite number is refused (ValueError), because a NaN would not sort and would corrupt
  every percentile after it.

The seed of each group is derived from the run's seed and the group's name (SHA-256), so a group's
interval does not depend on which other groups were computed or in what order.

Standard library only.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import chain
from statistics import NormalDist

#: z for a two-sided 95 % interval (Φ⁻¹(0.975)) and a one-sided 95 % bound (Φ⁻¹(0.95)).
Z_TWO_SIDED_95 = NormalDist().inv_cdf(0.975)
Z_ONE_SIDED_95 = NormalDist().inv_cdf(0.95)

#: The fewest resamples a report may use (the task's floor; PRD §10.6 names no number).
MIN_RESAMPLES = 10_000

#: The percentile interval's ends.
CI_LOW, CI_HIGH = 0.025, 0.975


def quantile(sorted_values: Sequence[float], q: float) -> float:
    """The type-7 `q`-quantile of values already sorted ascending."""
    n = len(sorted_values)
    if n == 0:
        raise ValueError("quantile() of no values")
    if not 0.0 <= q <= 1.0:
        raise ValueError("q is outside [0, 1]")
    h = (n - 1) * q
    lo = math.floor(h)
    hi = min(lo + 1, n - 1)
    return sorted_values[lo] + (h - lo) * (sorted_values[hi] - sorted_values[lo])


def wilson(k: int, n: int, z: float = Z_TWO_SIDED_95) -> tuple[float, float]:
    """The Wilson score interval for `k` successes in `n` trials: the two roots p of
    (k/n - p)² = z² p (1 - p) / n."""
    if n <= 0:
        raise ValueError("wilson() needs n > 0")
    if not 0 <= k <= n:
        raise ValueError("wilson() needs 0 <= k <= n")
    p = k / n
    z2 = z * z
    denominator = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def group_seed(seed: int, name: str) -> int:
    """A 64-bit seed for one group, from the run's seed and the group's name."""
    digest = hashlib.sha256(f"{seed}\x00{name}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


@dataclass(frozen=True)
class Observation:
    """One trial's contribution: the values it adds to each percentile metric (none, one or
    several, such as one per partial event) and the (numerator, denominator) it adds to each ratio
    metric."""

    utterance: str
    session: int | None
    values: Mapping[str, Sequence[float]] = field(default_factory=dict)
    ratios: Mapping[str, tuple[float, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class Estimate:
    """A statistic on the whole group with its bootstrap interval."""

    value: float | None
    low: float | None
    high: float | None
    n: float
    valid_resamples: int


@dataclass(frozen=True)
class BootstrapResult:
    """Every metric's estimate for one group, and how it was resampled."""

    method: str
    resamples: int
    seed: int
    utterances: int
    sessions: int
    quantiles: Mapping[tuple[str, float], Estimate]
    ratios: Mapping[str, Estimate]


def _interval(dist: list[float]) -> tuple[float | None, float | None]:
    if not dist:
        return None, None
    dist.sort()
    return quantile(dist, CI_LOW), quantile(dist, CI_HIGH)


def bootstrap(
    observations: Sequence[Observation],
    quantile_metrics: Mapping[str, Sequence[float]],
    ratio_metrics: Sequence[str],
    resamples: int,
    seed: int,
    by_session: bool = True,
) -> BootstrapResult:
    """Point estimates and percentile intervals for every metric of one group (module doc).

    `quantile_metrics` maps a metric to the quantiles wanted of it (for example
    `{"release_to_final_ms": (0.5, 0.95)}`); `ratio_metrics` names metrics estimated as
    Σ numerator / Σ denominator. Sessions are resampled when `by_session` is set and the trials
    come from two or more sessions; a trial with no session then counts as its own session None.
    """
    if resamples < 1:
        raise ValueError("resamples must be at least 1")
    utterances = sorted({o.utterance for o in observations})
    sessions = sorted({o.session for o in observations}, key=lambda s: (s is None, s or 0))
    two_way = by_session and len(sessions) >= 2
    u_index = {u: i for i, u in enumerate(utterances)}
    s_index = {s: i for i, s in enumerate(sessions)} if two_way else {}
    n_u, n_s = len(utterances), (len(sessions) if two_way else 1)

    # cells[metric][session][utterance] -> list of values, or [num, den]
    q_cells = {m: [[[] for _ in range(n_u)] for _ in range(n_s)] for m in quantile_metrics}
    r_num = {m: [[0.0] * n_u for _ in range(n_s)] for m in ratio_metrics}
    r_den = {m: [[0.0] * n_u for _ in range(n_s)] for m in ratio_metrics}
    for o in observations:
        ui = u_index[o.utterance]
        si = s_index[o.session] if two_way else 0
        for m in quantile_metrics:
            values = o.values.get(m, ())
            if not all(math.isfinite(v) for v in values):
                # NaN would not sort, and every percentile after it would be silently wrong.
                raise ValueError(f"{m}: a value that is not a finite number ({o.utterance})")
            q_cells[m][si][ui].extend(values)
        for m in ratio_metrics:
            num, den = o.ratios.get(m, (0.0, 0.0))
            if not (math.isfinite(num) and math.isfinite(den)):
                raise ValueError(f"{m}: a ratio term that is not a finite number ({o.utterance})")
            r_num[m][si][ui] += num
            r_den[m][si][ui] += den

    quantile_out: dict[tuple[str, float], Estimate] = {}
    ratio_out: dict[str, Estimate] = {}
    all_u, all_s = list(range(n_u)), list(range(n_s))

    def pooled(m: str, du: list[int], ds: list[int]) -> list[float]:
        rows = q_cells[m]
        return list(
            chain.from_iterable(chain.from_iterable(map(rows[s].__getitem__, du)) for s in ds)
        )

    def ratio(m: str, du: list[int], ds: list[int]) -> tuple[float, float]:
        nums, dens = r_num[m], r_den[m]
        num = sum(sum(map(nums[s].__getitem__, du)) for s in ds)
        den = sum(sum(map(dens[s].__getitem__, du)) for s in ds)
        return num, den

    q_dist: dict[tuple[str, float], list[float]] = {
        (m, q): [] for m, qs in quantile_metrics.items() for q in qs
    }
    r_dist: dict[str, list[float]] = {m: [] for m in ratio_metrics}
    if n_u:
        rng = random.Random(seed)
        for _ in range(resamples):
            du = rng.choices(all_u, k=n_u)
            ds = rng.choices(all_s, k=n_s) if two_way else all_s
            for m, qs in quantile_metrics.items():
                values = pooled(m, du, ds)
                if values:
                    values.sort()
                    for q in qs:
                        q_dist[(m, q)].append(quantile(values, q))
            for m in ratio_metrics:
                num, den = ratio(m, du, ds)
                if den > 0:
                    r_dist[m].append(num / den)

    for m, qs in quantile_metrics.items():
        values = sorted(pooled(m, all_u, all_s))
        for q in qs:
            low, high = _interval(q_dist[(m, q)])
            quantile_out[(m, q)] = Estimate(
                value=quantile(values, q) if values else None,
                low=low,
                high=high,
                n=len(values),
                valid_resamples=len(q_dist[(m, q)]),
            )
    for m in ratio_metrics:
        num, den = ratio(m, all_u, all_s)
        low, high = _interval(r_dist[m])
        ratio_out[m] = Estimate(
            value=num / den if den > 0 else None,
            low=low,
            high=high,
            n=den,
            valid_resamples=len(r_dist[m]),
        )
    return BootstrapResult(
        method="utterance x session (pigeonhole)" if two_way else "utterance",
        resamples=resamples,
        seed=seed,
        utterances=n_u,
        sessions=len(sessions),
        quantiles=quantile_out,
        ratios=ratio_out,
    )
