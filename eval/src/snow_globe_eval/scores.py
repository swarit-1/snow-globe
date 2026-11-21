"""Verification scores for Snow Globe's pre-registered endpoints (PRD §13.4, §13.5, §13.7).

Binary scores come from a 2×2 contingency table of hits (H), misses (M), false alarms (F)
and correct negatives (C)::

    POD  = H / (H + M)        POFD = F / (F + C)       PSS = POD − POFD
    FAR  = F / (H + F)        CSI  = H / (H + M + F)   bias = (H + F) / (H + M)

**Undefined scores are explicit.** A score whose denominator is zero comes back as ``None``
(JSON ``null``), never as 0 or NaN. With no observed events, or no non-events, PSS says
nothing about skill, and a table must not pretend otherwise. :meth:`Contingency.undefined`
names the reason for each ``None``.

**Masks.** ``excluded`` marks pairs removed by a quality gate (PRD §8.6) and ``partial``
marks truth frames in the extractor's partial band (PRD §13.4: excluded from P1 and
reported). Both are counted, never silently dropped. A pair in both masks counts as
excluded. Exclusions must be identical for the model and every baseline, so the paired
functions take one shared set of masks.

**Statistics** (PRD §13.4, §13.7): a paired ΔPSS with a block bootstrap over days, a
percentile interval and a seeded RNG; Wilson intervals for proportions; Holm–Bonferroni
for the key secondaries. The choice of bootstrap is justified in :func:`paired_delta_pss`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from statistics import NormalDist
from typing import Any, Literal
from zoneinfo import ZoneInfo

import numpy as np
from numpy.typing import ArrayLike, NDArray

__all__ = [
    "BootstrapMethod",
    "Contingency",
    "DeltaPssResult",
    "HolmEntry",
    "PssResult",
    "block_bootstrap_indices",
    "bootstrap_pss",
    "contingency",
    "csi",
    "far",
    "frequency_bias",
    "holm_bonferroni",
    "local_day_ordinals",
    "paired_delta_pss",
    "period_skill_scores",
    "pod",
    "pofd",
    "pss",
    "skill_score",
    "wilson_interval",
]

type BootstrapMethod = Literal["moving", "stationary"]

#: Above this many (replicate x day) cells the bootstrap runs in chunks to bound memory.
_MAX_CELLS_PER_CHUNK = 20_000_000


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


@dataclass(frozen=True, slots=True)
class Contingency:
    """A 2×2 table plus the counts that were masked out (PRD §13.5).

    ``excluded`` and ``partial`` are counts of pairs removed by the masks; they are not part
    of ``n``. Tables add (``a + b``) for pooling, which the PRD allows only beside the
    per-camera numbers (§13.7).
    """

    hits: int
    misses: int
    false_alarms: int
    correct_negatives: int
    excluded: int = 0
    partial: int = 0

    def __post_init__(self) -> None:
        for name in ("hits", "misses", "false_alarms", "correct_negatives", "excluded", "partial"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer, got {value!r}")

    def __add__(self, other: Contingency) -> Contingency:
        if not isinstance(other, Contingency):
            return NotImplemented
        return Contingency(
            self.hits + other.hits,
            self.misses + other.misses,
            self.false_alarms + other.false_alarms,
            self.correct_negatives + other.correct_negatives,
            self.excluded + other.excluded,
            self.partial + other.partial,
        )

    @property
    def n(self) -> int:
        """Scored pairs: H + M + F + C (masked pairs are not included)."""
        return self.hits + self.misses + self.false_alarms + self.correct_negatives

    @property
    def n_events(self) -> int:
        """Observed events, H + M."""
        return self.hits + self.misses

    @property
    def n_non_events(self) -> int:
        """Observed non-events, F + C."""
        return self.false_alarms + self.correct_negatives

    @property
    def base_rate(self) -> float | None:
        """Observed event frequency (H + M) / n; None when nothing was scored."""
        return _ratio(self.n_events, self.n)

    @property
    def pod(self) -> float | None:
        """Probability of detection H / (H + M); None with no observed events."""
        return _ratio(self.hits, self.hits + self.misses)

    @property
    def pofd(self) -> float | None:
        """Probability of false detection F / (F + C); None with no observed non-events."""
        return _ratio(self.false_alarms, self.false_alarms + self.correct_negatives)

    @property
    def pss(self) -> float | None:
        """Peirce skill score POD − POFD; None unless both events and non-events occurred."""
        pod_value, pofd_value = self.pod, self.pofd
        if pod_value is None or pofd_value is None:
            return None
        return pod_value - pofd_value

    @property
    def far(self) -> float | None:
        """False-alarm ratio F / (H + F); None when the event was never forecast."""
        return _ratio(self.false_alarms, self.hits + self.false_alarms)

    @property
    def csi(self) -> float | None:
        """Critical success index H / (H + M + F); None when H + M + F = 0."""
        return _ratio(self.hits, self.hits + self.misses + self.false_alarms)

    @property
    def bias(self) -> float | None:
        """Frequency bias (H + F) / (H + M); None with no observed events."""
        return _ratio(self.hits + self.false_alarms, self.hits + self.misses)

    def undefined(self) -> dict[str, str]:
        """Map each undefined score to the reason it is undefined."""
        reasons: dict[str, str] = {}
        if self.n == 0:
            reasons["base_rate"] = "no scored pairs"
        if self.n_events == 0:
            reasons["pod"] = "no observed events (H + M = 0)"
            reasons["bias"] = "no observed events (H + M = 0)"
        if self.n_non_events == 0:
            reasons["pofd"] = "no observed non-events (F + C = 0)"
        if self.n_events == 0 or self.n_non_events == 0:
            reasons["pss"] = "PSS needs both observed events and non-events"
        if self.hits + self.false_alarms == 0:
            reasons["far"] = "the event was never forecast (H + F = 0)"
        if self.hits + self.misses + self.false_alarms == 0:
            reasons["csi"] = "H + M + F = 0"
        return reasons

    def scores(self) -> dict[str, float | None]:
        """All scores as a dict (None where undefined)."""
        return {
            "pod": self.pod,
            "pofd": self.pofd,
            "pss": self.pss,
            "far": self.far,
            "csi": self.csi,
            "bias": self.bias,
            "base_rate": self.base_rate,
        }

    def to_dict(self) -> dict[str, Any]:
        """Counts, scores and undefined reasons, ready for JSON."""
        out: dict[str, Any] = {key: int(value) for key, value in asdict(self).items()}
        out["n"] = self.n
        out.update(self.scores())
        out["undefined"] = self.undefined()
        return out


def _as_bool_vector(values: ArrayLike, name: str) -> NDArray[np.bool_]:
    """Return a 1-D bool array; accept bools or exact 0/1 integers, refuse anything else."""
    array = np.asarray(values)
    if array.ndim != 1:
        raise ValueError(f"{name} must be one-dimensional, got shape {array.shape}")
    if array.dtype == np.bool_:
        return array
    if array.size == 0:
        return np.zeros(0, dtype=bool)
    if np.issubdtype(array.dtype, np.integer) and np.isin(array, (0, 1)).all():
        return array.astype(bool)
    raise TypeError(
        f"{name} must be boolean (or 0/1 integers); got dtype {array.dtype}. "
        "Missing values belong in the `excluded` mask, not in the data."
    )


def _masks(
    length: int, excluded: ArrayLike | None, partial: ArrayLike | None
) -> tuple[NDArray[np.bool_], NDArray[np.bool_]]:
    ex = np.zeros(length, dtype=bool) if excluded is None else _as_bool_vector(excluded, "excluded")
    pa = np.zeros(length, dtype=bool) if partial is None else _as_bool_vector(partial, "partial")
    if ex.shape != (length,) or pa.shape != (length,):
        raise ValueError("excluded and partial masks must have the same length as the data")
    return ex, pa & ~ex


def contingency(
    forecast: ArrayLike,
    observed: ArrayLike,
    *,
    excluded: ArrayLike | None = None,
    partial: ArrayLike | None = None,
) -> Contingency:
    """Build the 2×2 table from paired boolean forecasts and observations (PRD §13.5).

    ``True`` is the event (for P1: "summit visible"). ``excluded`` and ``partial`` are optional
    boolean masks of the same length; masked pairs are counted in the table's ``excluded``
    and ``partial`` fields and are not scored. Values under a mask are ignored.
    """
    f = _as_bool_vector(forecast, "forecast")
    o = _as_bool_vector(observed, "observed")
    if f.shape != o.shape:
        raise ValueError(f"forecast and observed differ in length: {f.size} vs {o.size}")
    ex, pa = _masks(f.size, excluded, partial)
    scored = ~ex & ~pa
    return Contingency(
        hits=int(np.count_nonzero(f & o & scored)),
        misses=int(np.count_nonzero(~f & o & scored)),
        false_alarms=int(np.count_nonzero(f & ~o & scored)),
        correct_negatives=int(np.count_nonzero(~f & ~o & scored)),
        excluded=int(np.count_nonzero(ex)),
        partial=int(np.count_nonzero(pa)),
    )


def pod(table: Contingency) -> float | None:
    """POD = H / (H + M) (PRD §13.5); None when undefined."""
    return table.pod


def pofd(table: Contingency) -> float | None:
    """POFD = F / (F + C) (PRD §13.5); None when undefined."""
    return table.pofd


def pss(table: Contingency) -> float | None:
    """PSS = POD − POFD (PRD §13.5); None without both events and non-events."""
    return table.pss


def far(table: Contingency) -> float | None:
    """FAR = F / (H + F) (PRD §13.5); None when undefined."""
    return table.far


def csi(table: Contingency) -> float | None:
    """CSI = H / (H + M + F) (PRD §13.5); None when undefined."""
    return table.csi


def frequency_bias(table: Contingency) -> float | None:
    """Frequency bias = (H + F) / (H + M); None with no observed events."""
    return table.bias


def _errors_vector(values: ArrayLike, name: str) -> NDArray[np.float64]:
    array = np.asarray(values, dtype=float)
    if array.ndim != 1:
        raise ValueError(f"{name} must be one-dimensional")
    if not np.isfinite(array).all():
        raise ValueError(f"{name} has non-finite values; mask them instead")
    if (array < 0).any():
        raise ValueError(f"{name} must be non-negative errors (absolute or squared)")
    return array


def skill_score(
    errors: ArrayLike, reference_errors: ArrayLike, *, mask: ArrayLike | None = None
) -> float | None:
    """Skill score SS = 1 − E / E_ref over a whole period (PRD §13.4 K1, §13.5).

    ``E`` and ``E_ref`` are the mean errors over every pair of the period (a ratio of
    totals), never a mean of per-pair ratios, which blow up when a reference error is near
    zero. Errors must be non-negative (absolute or squared). ``mask`` (True = keep) applies
    to both, so the comparison is paired. Returns None when no pair is kept or E_ref = 0.
    """
    e = _errors_vector(errors, "errors")
    r = _errors_vector(reference_errors, "reference_errors")
    if e.shape != r.shape:
        raise ValueError("errors and reference_errors must have the same length")
    if mask is not None:
        keep = _as_bool_vector(mask, "mask")
        if keep.shape != e.shape:
            raise ValueError("mask must have the same length as the errors")
        e, r = e[keep], r[keep]
    if e.size == 0:
        return None
    reference_total = float(r.sum())
    if reference_total == 0.0:
        return None
    return 1.0 - float(e.sum()) / reference_total


def period_skill_scores(
    errors: ArrayLike, reference_errors: ArrayLike, periods: Sequence[Any]
) -> dict[Any, float | None]:
    """One skill score per whole period label (e.g. a season), each from that period's totals."""
    e = _errors_vector(errors, "errors")
    r = _errors_vector(reference_errors, "reference_errors")
    labels = list(periods)
    if not (len(labels) == e.size == r.size):
        raise ValueError("errors, reference_errors and periods must have the same length")
    out: dict[Any, float | None] = {}
    for label in dict.fromkeys(labels):
        keep = np.array([item == label for item in labels], dtype=bool)
        out[label] = skill_score(e, r, mask=keep)
    return out


def _z_for_level(level: float) -> float:
    if not 0.0 < level < 1.0:
        raise ValueError(f"confidence level must be in (0, 1), got {level}")
    return NormalDist().inv_cdf(0.5 + level / 2.0)


def wilson_interval(successes: int, n: int, *, level: float = 0.95) -> tuple[float, float] | None:
    """Wilson score interval for a binomial proportion ``successes / n``.

    Returns ``(low, high)``, or None when ``n == 0`` (no information). Used for POD, POFD
    and rates beside every count (PRD §13.7).
    """
    if isinstance(successes, bool) or isinstance(n, bool):
        raise TypeError("successes and n must be integers")
    if int(successes) != successes or int(n) != n:
        raise TypeError("successes and n must be integers")
    if n < 0 or successes < 0 or successes > n:
        raise ValueError(f"need 0 <= successes <= n, got {successes} of {n}")
    if n == 0:
        return None
    z = _z_for_level(level)
    p = successes / n
    z2n = z * z / n
    centre = (p + z2n / 2.0) / (1.0 + z2n)
    half = (z / (1.0 + z2n)) * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    low = 0.0 if successes == 0 else max(0.0, centre - half)
    high = 1.0 if successes == n else min(1.0, centre + half)
    return low, high


def local_day_ordinals(times: Iterable[datetime], time_zone: str) -> NDArray[np.int64]:
    """Proleptic-Gregorian ordinals of each time's *local* calendar date in ``time_zone``.

    Day blocks must follow local days: Pacific daytime spans two UTC dates (16:00–00:30 UTC
    in winter), so UTC dates would split one day's frames across two blocks.
    """
    zone = ZoneInfo(time_zone)
    out = []
    for moment in times:
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("times must be timezone-aware")
        out.append(moment.astimezone(zone).date().toordinal())
    return np.asarray(out, dtype=np.int64)


def _day_ordinals(days: ArrayLike | Sequence[Any]) -> NDArray[np.int64]:
    """Normalise day labels (ints, ``date`` objects or datetime64[D]) to int ordinals.

    A datetime64 array must have the unit ``D``: finer units hold times, whose local day
    is not their UTC day, and coarser units (months, years) are not days. Both raise
    ``TypeError``, as Python datetimes do.
    """
    if isinstance(days, np.ndarray) and np.issubdtype(days.dtype, np.datetime64):
        unit, _ = np.datetime_data(days.dtype)
        if unit != "D":
            raise TypeError(
                f"datetime64 day labels must have the unit 'D', got {unit!r}; for times, "
                "use local_day_ordinals(times, time_zone)"
            )
        if np.isnat(days).any():
            raise ValueError("day labels contain NaT; mask those pairs instead")
        return days.astype(np.int64)
    values = list(days)  # type: ignore[arg-type]
    out = np.empty(len(values), dtype=np.int64)
    for i, value in enumerate(values):
        if isinstance(value, datetime):
            raise TypeError(
                "day labels must be dates or integers, not datetimes; "
                "use local_day_ordinals(times, time_zone)"
            )
        if isinstance(value, date):
            out[i] = value.toordinal()
        elif isinstance(value, (int, np.integer)) and not isinstance(value, bool):
            out[i] = int(value)
        else:
            raise TypeError(f"unsupported day label {value!r}")
    return out


def block_bootstrap_indices(
    n_days: int,
    block_length: int,
    n_boot: int,
    rng: np.random.Generator,
    *,
    method: BootstrapMethod = "moving",
    circular: bool = True,
) -> NDArray[np.int64]:
    """Return an ``(n_boot, n_days)`` array of resampled day indices.

    - ``method="moving"``: blocks of exactly ``block_length`` consecutive days. With
      ``circular=True`` (the default) the series wraps, so every day has the same chance of
      selection (Politis & Romano 1992); without it, starts are drawn from
      ``[0, n_days − L]`` (Künsch 1989) and the first and last days are under-sampled.
    - ``method="stationary"``: block lengths are geometric with mean ``block_length`` and
      the series always wraps (Politis & Romano 1994).
    """
    if isinstance(block_length, bool) or not isinstance(block_length, (int, np.integer)):
        raise TypeError("block_length must be an integer number of days")
    if block_length < 1:
        raise ValueError("block_length must be at least 1 day (PRD §13.4)")
    if n_days < 1:
        raise ValueError("need at least one day")
    if n_boot < 1:
        raise ValueError("n_boot must be positive")
    length = int(block_length)
    if method == "moving":
        if circular:
            n_blocks = math.ceil(n_days / length)
            starts = rng.integers(0, n_days, size=(n_boot, n_blocks))
            offsets = np.arange(length)
            idx = (starts[:, :, None] + offsets[None, None, :]) % n_days
        else:
            length = min(length, n_days)
            n_blocks = math.ceil(n_days / length)
            starts = rng.integers(0, n_days - length + 1, size=(n_boot, n_blocks))
            idx = starts[:, :, None] + np.arange(length)[None, None, :]
        return idx.reshape(n_boot, -1)[:, :n_days].astype(np.int64)
    if method == "stationary":
        new_block = rng.random((n_boot, n_days)) < (1.0 / length)
        new_block[:, 0] = True
        random_starts = rng.integers(0, n_days, size=(n_boot, n_days))
        position = np.arange(n_days)
        last_start = np.maximum.accumulate(np.where(new_block, position, 0), axis=1)
        start_day = np.take_along_axis(random_starts, last_start, axis=1)
        return ((start_day + (position - last_start)) % n_days).astype(np.int64)
    raise ValueError(f"unknown bootstrap method {method!r}")


def _pss_from_counts(counts: NDArray[np.int64]) -> NDArray[np.float64]:
    """Vectorised PSS from an (..., 4) array of H, M, F, C; NaN where undefined."""
    h, m, f, c = (counts[..., i].astype(float) for i in range(4))
    events, non_events = h + m, f + c
    with np.errstate(invalid="ignore", divide="ignore"):
        value = h / events - f / non_events
    value[(events == 0) | (non_events == 0)] = np.nan
    return value


def _per_day_counts(
    forecast: NDArray[np.bool_], observed: NDArray[np.bool_], day_index: NDArray[np.int64], n: int
) -> NDArray[np.int64]:
    cells = np.stack(
        [forecast & observed, ~forecast & observed, forecast & ~observed, ~forecast & ~observed],
        axis=1,
    ).astype(np.int64)
    out = np.zeros((n, 4), dtype=np.int64)
    np.add.at(out, day_index, cells)
    return out


def _resampled_counts(
    per_day: Sequence[NDArray[np.int64]],
    n_days: int,
    *,
    block_length: int,
    n_boot: int,
    seed: int,
    method: BootstrapMethod,
    circular: bool,
) -> list[NDArray[np.int64]]:
    """Resample days once and return the summed tables for each forecast (paired)."""
    rng = np.random.default_rng(seed)
    chunk = max(1, min(n_boot, _MAX_CELLS_PER_CHUNK // max(1, n_days)))
    results: list[list[NDArray[np.int64]]] = [[] for _ in per_day]
    done = 0
    while done < n_boot:
        size = min(chunk, n_boot - done)
        idx = block_bootstrap_indices(
            n_days, block_length, size, rng, method=method, circular=circular
        )
        flat = (idx + n_days * np.arange(size)[:, None]).ravel()
        multiplicity = np.bincount(flat, minlength=size * n_days).reshape(size, n_days)
        for slot, table in enumerate(per_day):
            results[slot].append(multiplicity @ table)
        done += size
    return [np.concatenate(parts, axis=0) for parts in results]


def _tail_count(n: int, level: float) -> int:
    """k = ceil(n · α / 2) with α = 1 − level, rounded so that 0.95 means α = 0.05 exactly."""
    return max(1, math.ceil(round(n * (1.0 - level) / 2.0, 9)))


def _percentile_interval(
    values: NDArray[np.float64], level: float
) -> tuple[float | None, float | None]:
    """Percentile interval from order statistics: the k-th smallest and k-th largest value.

    With k = ceil(B · α / 2) for B values, the lower end is above 0 exactly when fewer than
    B · α / 2 values are ≤ 0, and the upper end is below 0 exactly when fewer than B · α / 2
    values are ≥ 0. That is what makes the interval and :func:`_two_sided_p` agree exactly
    (no interpolation between order statistics).
    """
    if values.size == 0:
        return None, None
    ordered = np.sort(values)
    k = min(_tail_count(ordered.size, level), ordered.size)
    return float(ordered[k - 1]), float(ordered[ordered.size - k])


def _two_sided_p(values: NDArray[np.float64]) -> float | None:
    """p-value by inverting the percentile interval: 2 · min(P*(Δ ≤ 0), P*(Δ ≥ 0)).

    Resamples with Δ = 0 count in both tails, as they do in the interval.
    """
    if values.size == 0:
        return None
    below, above = int(np.count_nonzero(values <= 0.0)), int(np.count_nonzero(values >= 0.0))
    return float(min(1.0, 2.0 * min(below, above) / values.size))


@dataclass(frozen=True)
class _Prepared:
    observed: NDArray[np.bool_]
    forecasts: tuple[NDArray[np.bool_], ...]
    day_index: NDArray[np.int64]
    n_days: int
    first_day: int | None
    excluded: NDArray[np.bool_]
    partial: NDArray[np.bool_]


def _prepare(
    forecasts: Sequence[ArrayLike],
    observed: ArrayLike,
    days: ArrayLike | Sequence[Any],
    excluded: ArrayLike | None,
    partial: ArrayLike | None,
) -> _Prepared:
    o = _as_bool_vector(observed, "observed")
    fs = tuple(_as_bool_vector(f, f"forecast {i}") for i, f in enumerate(forecasts))
    for f in fs:
        if f.shape != o.shape:
            raise ValueError("every forecast must have the same length as observed")
    day_values = _day_ordinals(days)
    if day_values.shape != o.shape:
        raise ValueError("days must have the same length as observed")
    ex, pa = _masks(o.size, excluded, partial)
    scored = ~ex & ~pa
    if not scored.any():
        return _Prepared(o[:0], tuple(f[:0] for f in fs), day_values[:0], 0, None, ex, pa)
    kept_days = day_values[scored]
    first = int(kept_days.min())
    n_days = int(kept_days.max()) - first + 1
    return _Prepared(
        observed=o[scored],
        forecasts=tuple(f[scored] for f in fs),
        day_index=kept_days - first,
        n_days=n_days,
        first_day=first,
        excluded=ex,
        partial=pa,
    )


@dataclass(frozen=True)
class PssResult:
    """PSS of one forecast with a day-block bootstrap interval."""

    pss: float | None
    ci_low: float | None
    ci_high: float | None
    level: float
    table: Contingency
    n_boot: int
    n_undefined: int
    method: str
    circular: bool
    block_length_days: int
    seed: int
    n_pairs: int
    n_days_spanned: int
    n_days_with_pairs: int
    flags: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict (the table is expanded)."""
        out = asdict(self)
        out["table"] = self.table.to_dict()
        out["flags"] = list(self.flags)
        return out


@dataclass(frozen=True)
class DeltaPssResult:
    """Paired ΔPSS = PSS(model) − PSS(reference) with a day-block bootstrap (PRD §13.4)."""

    delta: float | None
    pss_model: float | None
    pss_reference: float | None
    ci_low: float | None
    ci_high: float | None
    p_value: float | None
    level: float
    table_model: Contingency
    table_reference: Contingency
    n_boot: int
    n_undefined: int
    method: str
    circular: bool
    block_length_days: int
    seed: int
    n_pairs: int
    n_days_spanned: int
    n_days_with_pairs: int
    flags: tuple[str, ...] = field(default=())

    @property
    def excludes_zero(self) -> bool | None:
        """True when the interval excludes 0 (the P1 claim rule); None without an interval."""
        if self.ci_low is None or self.ci_high is None:
            return None
        return self.ci_low > 0.0 or self.ci_high < 0.0

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict (tables expanded)."""
        out = asdict(self)
        out["table_model"] = self.table_model.to_dict()
        out["table_reference"] = self.table_reference.to_dict()
        out["excludes_zero"] = self.excludes_zero
        out["flags"] = list(self.flags)
        return out


def _common_flags(n_days: int, block_length: int, n_undefined: int, n_boot: int) -> list[str]:
    flags = []
    if n_days and block_length >= n_days:
        flags.append("block-length-covers-series: the interval has no between-block variance")
    if n_undefined:
        flags.append(f"{n_undefined} of {n_boot} resamples had an undefined PSS and were dropped")
    return flags


def bootstrap_pss(
    forecast: ArrayLike,
    observed: ArrayLike,
    days: ArrayLike | Sequence[Any],
    *,
    seed: int,
    excluded: ArrayLike | None = None,
    partial: ArrayLike | None = None,
    block_length_days: int = 1,
    n_boot: int = 10_000,
    method: BootstrapMethod = "moving",
    circular: bool = True,
    level: float = 0.95,
) -> PssResult:
    """PSS of one forecast with a percentile interval from a day-block bootstrap.

    Same resampling as :func:`paired_delta_pss`; used for "P1 with its confidence interval"
    beside the paired test (PRD §13.10).
    """
    _z_for_level(level)
    prep = _prepare([forecast], observed, days, excluded, partial)
    table = contingency(forecast, observed, excluded=prep.excluded, partial=prep.partial)
    n_with = int(np.unique(prep.day_index).size)
    if prep.n_days == 0:
        return PssResult(
            None,
            None,
            None,
            level,
            table,
            n_boot,
            0,
            method,
            circular,
            int(block_length_days),
            seed,
            0,
            0,
            0,
            ("no scored pairs",),
        )
    per_day = _per_day_counts(prep.forecasts[0], prep.observed, prep.day_index, prep.n_days)
    (boot,) = _resampled_counts(
        [per_day],
        prep.n_days,
        block_length=block_length_days,
        n_boot=n_boot,
        seed=seed,
        method=method,
        circular=circular,
    )
    values = _pss_from_counts(boot)
    defined = values[~np.isnan(values)]
    n_undefined = int(values.size - defined.size)
    low, high = _percentile_interval(defined, level)
    flags = _common_flags(prep.n_days, int(block_length_days), n_undefined, n_boot)
    return PssResult(
        pss=table.pss,
        ci_low=low,
        ci_high=high,
        level=level,
        table=table,
        n_boot=n_boot,
        n_undefined=n_undefined,
        method=method,
        circular=circular,
        block_length_days=int(block_length_days),
        seed=seed,
        n_pairs=table.n,
        n_days_spanned=prep.n_days,
        n_days_with_pairs=n_with,
        flags=tuple(flags),
    )


def paired_delta_pss(
    model: ArrayLike,
    reference: ArrayLike,
    observed: ArrayLike,
    days: ArrayLike | Sequence[Any],
    *,
    seed: int,
    excluded: ArrayLike | None = None,
    partial: ArrayLike | None = None,
    block_length_days: int = 1,
    n_boot: int = 10_000,
    method: BootstrapMethod = "moving",
    circular: bool = True,
    level: float = 0.95,
) -> DeltaPssResult:
    """Paired ΔPSS (model − reference) with a percentile interval from a day-block bootstrap.

    PRD §13.4: "the paired ΔPSS against persistence, with a 95 % confidence interval from a
    block bootstrap. The block length is set from development-period autocorrelation, and is
    at least 1 day." The claim is made if the interval excludes 0.

    **Resampling unit.** The unit is the local calendar day (``days`` holds one label per
    pair: an int ordinal, a ``date`` or datetime64[D]; see :func:`local_day_ordinals`).
    Blocks are runs of *calendar* days from the first to the last scored day, so an outage
    day is an empty day, not a skipped one, and a block of L days always spans L days of
    weather. Model and reference are resampled with the same indices (paired), and both
    see the same masks.

    **Why the moving-block bootstrap, wrapped, is the default.**

    1. A fixed block length is a single integer, so it can be frozen in the pre-registration
       exactly as §13.4 asks ("set from development-period autocorrelation").
    2. At the same mean block length, the stationary bootstrap's variance estimator has a
       larger asymptotic mean-squared error than the moving or circular block bootstrap,
       because its random block lengths add variance (Lahiri 1999, *Ann. Statist.* 27:386).
       Its advantage, less sensitivity to the block length, matters less once L is fixed in
       advance.
    3. Wrapping (the circular block bootstrap, Politis & Romano 1992) gives every day the
       same selection probability. The plain moving-block bootstrap under-samples the first
       and last L − 1 days, a visible bias with only ~31 sealed days.

    ``method="stationary"`` is kept for a sensitivity analysis only.

    **Interval and p-value.** The interval is the percentile interval of the defined
    resamples, taken from order statistics: with B defined resamples and k = ceil(B·α/2),
    the k-th smallest and the k-th largest ΔPSS (α = 1 − ``level``). A resample in which
    either PSS is undefined (no events or no non-events) is dropped and counted in
    ``n_undefined``; a flag is raised when any were dropped. The p-value inverts the
    interval, ``2 · min(P*(Δ ≤ 0), P*(Δ ≥ 0))`` over the same resamples, so "p < α" holds
    exactly when the 1 − α interval excludes 0 (a property test checks it). At p = α
    exactly the interval does not exclude 0, while :func:`holm_bonferroni` rejects at
    adjusted p ≤ α; the P1 claim uses the interval. The p-value feeds
    :func:`holm_bonferroni`.

    ``seed`` is required: the same inputs and seed always give the same interval.
    """
    _z_for_level(level)
    prep = _prepare([model, reference], observed, days, excluded, partial)
    table_model = contingency(model, observed, excluded=prep.excluded, partial=prep.partial)
    table_ref = contingency(reference, observed, excluded=prep.excluded, partial=prep.partial)
    pss_m, pss_r = table_model.pss, table_ref.pss
    delta = None if pss_m is None or pss_r is None else pss_m - pss_r
    if prep.n_days == 0:
        return DeltaPssResult(
            None,
            pss_m,
            pss_r,
            None,
            None,
            None,
            level,
            table_model,
            table_ref,
            n_boot,
            0,
            method,
            circular,
            int(block_length_days),
            seed,
            0,
            0,
            0,
            ("no scored pairs",),
        )
    per_model = _per_day_counts(prep.forecasts[0], prep.observed, prep.day_index, prep.n_days)
    per_ref = _per_day_counts(prep.forecasts[1], prep.observed, prep.day_index, prep.n_days)
    boot_model, boot_ref = _resampled_counts(
        [per_model, per_ref],
        prep.n_days,
        block_length=block_length_days,
        n_boot=n_boot,
        seed=seed,
        method=method,
        circular=circular,
    )
    deltas = _pss_from_counts(boot_model) - _pss_from_counts(boot_ref)
    defined = deltas[~np.isnan(deltas)]
    n_undefined = int(deltas.size - defined.size)
    low, high = _percentile_interval(defined, level)
    flags = _common_flags(prep.n_days, int(block_length_days), n_undefined, n_boot)
    if delta is None:
        flags.append("full-sample PSS undefined for the model or the reference")
    return DeltaPssResult(
        delta=delta,
        pss_model=pss_m,
        pss_reference=pss_r,
        ci_low=low,
        ci_high=high,
        p_value=_two_sided_p(defined),
        level=level,
        table_model=table_model,
        table_reference=table_ref,
        n_boot=n_boot,
        n_undefined=n_undefined,
        method=method,
        circular=circular,
        block_length_days=int(block_length_days),
        seed=seed,
        n_pairs=table_model.n,
        n_days_spanned=prep.n_days,
        n_days_with_pairs=int(np.unique(prep.day_index).size),
        flags=tuple(flags),
    )


@dataclass(frozen=True, slots=True)
class HolmEntry:
    """One hypothesis after Holm's step-down correction."""

    name: str
    p_value: float
    adjusted: float
    reject: bool


def holm_bonferroni(
    p_values: Mapping[str, float] | Sequence[float], *, alpha: float = 0.05
) -> list[HolmEntry]:
    """Holm–Bonferroni step-down correction (PRD §13.4 key secondaries, §13.10).

    Sort the m p-values ascending; the adjusted value of the i-th smallest is
    ``max_{j ≤ i} min(1, (m − j + 1) · p_(j))``, and a hypothesis is rejected when its
    adjusted value is ≤ ``alpha`` (equivalent to stopping at the first
    ``p_(i) > alpha / (m − i + 1)``). Entries come back in the input order; sequences are
    named by their index. Ties keep input order.
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    if isinstance(p_values, Mapping):
        items = [(str(name), float(value)) for name, value in p_values.items()]
    else:
        items = [(str(i), float(value)) for i, value in enumerate(p_values)]
    for name, value in items:
        if math.isnan(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"p-value for {name!r} must be in [0, 1], got {value}")
    m = len(items)
    order = sorted(range(m), key=lambda i: (items[i][1], i))
    adjusted = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * items[i][1]))
        adjusted[i] = running
    return [
        HolmEntry(name=name, p_value=value, adjusted=adjusted[i], reject=adjusted[i] <= alpha)
        for i, (name, value) in enumerate(items)
    ]
