"""oculus-core against a reference, epoch by epoch (PRD §8.4 "Checks"; PLAN.md M2).

The ephemeris under test is eval's binding (:func:`snow_globe_eval.ephemeris.get_ephemeris`,
oculus-core, the same core ``app/core`` uses) and eval's refraction
(:func:`snow_globe_eval.ephemeris.refraction_deg`, Sæmundsson at 1010 hPa and 10 °C). For each
epoch it measures:

- ``sep_geo``: the angle between oculus-core's geometric direction (topocentric apparent, no
  refraction) and the reference's airless apparent direction;
- ``sep_refr``: the angle between oculus-core's direction with eval's refraction and the
  reference's refracted direction;
- the altitude and on-sky azimuth parts of each, and for the Moon the illuminated fraction;
- the refraction models alone (``drefraction_model``): eval's refraction at the reference's
  own geometric altitude against the reference's, so one input altitude feeds both. Beside it,
  ``drefraction_own_alt`` puts each model at its own geometric altitude; that one also holds the
  refraction curve's slope times oculus-core's altitude error (about 0.16 × 12″ ≈ 1.9″ just
  above −1°), so it is a diagnostic of the error chain, not of the models.

**app/core's refraction.** ``app/core`` refracts through oculus-core's ``refracted_alt``
(``refraction_from_true`` at the pin, ``core/src/frames.rs``): the same Sæmundsson formula at
1010 hPa and 10 °C without Meeus' +0.0019279 arcmin, so its refracted altitude is 0.116″
below eval's at every altitude from −1° up (:data:`APP_CORE_REFRACTION_OFFSET_DEG`). That is
read from the source, not measured: the binding does not expose oculus-core's refraction.

**Where the refracted target applies: a convention.** eval and ``app/core`` refract a geometric
altitude of −1° and above, and nothing below. Horizons holds its refraction at the −1° value
(0.6466°) for every lower altitude; Skyfield, iterating Bennett's formula from the true
altitude, refracts nothing below a geometric −1° either. The refracted target is judged where
the reference's geometric altitude is −1° or higher (:data:`REFRACTION_DOMAIN_MIN_DEG`), a
convention pending the owner's choice of cutoff (owner question 1), and the lower band is
reported apart, never hidden. The −1° is not where a body leaves the view: a camera above sea
level sees down to its horizon dip (:func:`horizon_dip_deg`; 1.49° at Grand Canyon's 2,150 m),
so a body between −dip and −1° can be in sight where the terrain is lower. :func:`dip_band`
counts those epochs for a line of their own.

TODO(M2): once the plates carry DEM horizon maps (PRD §8.5, PLAN.md M2), judge the refracted
target down to each camera's terrain horizon instead of the −1° convention.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np
from numpy.typing import NDArray
from snow_globe_common.timeutil import isoformat_z

from ..ephemeris import EphemerisBackend, get_ephemeris, refraction_deg
from .stats import (
    ALT_BANDS,
    TARGET_DEG,
    band_label,
    band_labels,
    separation_deg,
    summarize,
    summarize_by,
    wrap180,
)

__all__ = [
    "APP_CORE_REFRACTION_OFFSET_DEG",
    "EARTH_RADIUS_M",
    "REFRACTION_DOMAIN_MIN_DEG",
    "BodyComparison",
    "Positions",
    "beyond_target_rows",
    "compare_body",
    "count_beyond",
    "dip_band",
    "horizon_dip_deg",
    "oculus_positions",
    "refraction_array",
    "summarize_comparisons",
]

#: The refracted target is judged where the reference's geometric altitude is at least this
#: (a convention pending owner question 1; see the module docstring).
REFRACTION_DOMAIN_MIN_DEG = -1.0
#: ``app/core``'s refracted altitude minus eval's, from −1° up: oculus-core's
#: ``refraction_from_true`` leaves out Meeus' +0.0019279 arcmin (read from the pinned source).
APP_CORE_REFRACTION_OFFSET_DEG = -0.0019279 / 60.0
#: Mean Earth radius (IUGG R1), metres, for the horizon dip of an elevated camera.
EARTH_RADIUS_M = 6_371_008.8
_BAND_ORDER = tuple(band_label(low, high) for low, high in ALT_BANDS)


def _utc(ms: float) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


@dataclass(frozen=True)
class Positions:
    """oculus-core at instants ``unix_ms`` for one site (degrees; azimuth from north).

    ``*_ra`` / ``*_dec`` are oculus-core's apparent right ascension and declination (true
    equator and equinox of date), read from the same module the binding wraps, for the
    diagnosis only (NaN when the backend is not oculus-core).
    """

    unix_ms: NDArray[np.float64]
    sun_alt: NDArray[np.float64]
    sun_az: NDArray[np.float64]
    moon_alt: NDArray[np.float64]
    moon_az: NDArray[np.float64]
    moon_illum: NDArray[np.float64]
    sun_ra: NDArray[np.float64]
    sun_dec: NDArray[np.float64]
    moon_ra: NDArray[np.float64]
    moon_dec: NDArray[np.float64]
    backend: str

    def body(self, body: str) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        """Geometric altitude and azimuth of ``body``."""
        if body == "sun":
            return self.sun_alt, self.sun_az
        return self.moon_alt, self.moon_az

    def radec(self, body: str) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        """Apparent RA and Dec of ``body`` (diagnostic)."""
        if body == "sun":
            return self.sun_ra, self.sun_dec
        return self.moon_ra, self.moon_dec


def oculus_positions(
    unix_ms: Sequence[float] | NDArray[np.float64],
    lat_deg: float,
    lon_deg: float,
    height_m: float,
    *,
    ephemeris: EphemerisBackend | None = None,
) -> Positions:
    """Sun and Moon from the binding at every instant (the binding's public methods)."""
    backend = ephemeris or get_ephemeris("oculus")
    module = getattr(backend, "_module", None)
    ms = np.asarray(unix_ms, dtype=np.float64)
    n = ms.size
    out = {k: np.full(n, math.nan) for k in ("sa", "sz", "ma", "mz", "mi", "sr", "sd", "mr", "md")}
    for i, value in enumerate(ms):
        moment = _utc(float(value))
        out["sa"][i], out["sz"][i] = backend.sun_alt_az(moment, lat_deg, lon_deg, height_m)
        out["ma"][i], out["mz"][i], out["mi"][i] = backend.moon_alt_az_illum(
            moment, lat_deg, lon_deg, height_m
        )
        if module is not None:
            r = module.sun_moon(float(value), float(lat_deg), float(lon_deg), float(height_m))
            out["sr"][i], out["sd"][i] = r["sun"]["ra"], r["sun"]["dec"]
            out["mr"][i], out["md"][i] = r["moon"]["ra"], r["moon"]["dec"]
    return Positions(
        unix_ms=ms,
        sun_alt=out["sa"],
        sun_az=out["sz"],
        moon_alt=out["ma"],
        moon_az=out["mz"],
        moon_illum=out["mi"],
        sun_ra=out["sr"] % 360.0,
        sun_dec=out["sd"],
        moon_ra=out["mr"] % 360.0,
        moon_dec=out["md"],
        backend=f"{backend.name} {backend.version}",
    )


def refraction_array(alt_deg: Any) -> NDArray[np.float64]:
    """eval's refraction (Sæmundsson, 1010 hPa, 10 °C; degrees) at each geometric altitude."""
    alt = np.asarray(alt_deg, dtype=np.float64)
    return np.array([refraction_deg(float(a)) for a in alt.ravel()], dtype=np.float64).reshape(
        alt.shape
    )


def _refracted(alt_deg: NDArray[np.float64]) -> NDArray[np.float64]:
    """eval's refracted altitude (Sæmundsson, 1010 hPa, 10 °C) of each geometric altitude."""
    return np.asarray(alt_deg, dtype=np.float64) + refraction_array(alt_deg)


@dataclass(frozen=True)
class BodyComparison:
    """One body at one site against one reference, every epoch (degrees).

    ``ours_refr_at_ref_alt`` is eval's refraction at the *reference's* geometric altitude, for
    the refraction models alone (:attr:`drefraction_model`).
    """

    source: str
    site_id: str
    body: str
    unix_ms: NDArray[np.float64]
    ref_alt: NDArray[np.float64]
    ref_az: NDArray[np.float64]
    ref_alt_refr: NDArray[np.float64]
    ref_az_refr: NDArray[np.float64]
    ours_alt: NDArray[np.float64]
    ours_az: NDArray[np.float64]
    ours_alt_refr: NDArray[np.float64]
    ours_refr_at_ref_alt: NDArray[np.float64]
    radec_sep: NDArray[np.float64]
    illum_diff_pct: NDArray[np.float64]

    @property
    def sep_geo(self) -> NDArray[np.float64]:
        """Angle between the geometric directions."""
        return separation_deg(self.ours_alt, self.ours_az, self.ref_alt, self.ref_az)

    @property
    def sep_refr(self) -> NDArray[np.float64]:
        """Angle between the refracted directions (azimuth is unchanged by refraction)."""
        return separation_deg(self.ours_alt_refr, self.ours_az, self.ref_alt_refr, self.ref_az_refr)

    @property
    def dalt_geo(self) -> NDArray[np.float64]:
        """Geometric altitude, oculus-core minus reference."""
        return self.ours_alt - self.ref_alt

    @property
    def dalt_refr(self) -> NDArray[np.float64]:
        """Refracted altitude, eval minus reference."""
        return self.ours_alt_refr - self.ref_alt_refr

    @property
    def daz_sky(self) -> NDArray[np.float64]:
        """Azimuth difference scaled by cos(altitude): its share of the angle on the sky."""
        return wrap180(self.ours_az - self.ref_az) * np.cos(np.radians(self.ref_alt))

    @property
    def refraction_domain(self) -> NDArray[np.bool_]:
        """Epochs where the refracted target applies (see the module docstring)."""
        return self.ref_alt >= REFRACTION_DOMAIN_MIN_DEG

    @property
    def drefraction_model(self) -> NDArray[np.float64]:
        """eval's refraction minus the reference's, both at the reference's geometric altitude.

        The refraction models alone: one input altitude feeds both, so oculus-core's ephemeris
        error is not in it. It is what PRD §8.4's "apparent altitude with atmospheric
        refraction" adds on top of the ephemeris, model against model.
        """
        return self.ours_refr_at_ref_alt - (self.ref_alt_refr - self.ref_alt)

    @property
    def drefraction_own_alt(self) -> NDArray[np.float64]:
        """eval's refraction at our geometric altitude minus the reference's at its own.

        Not the models alone: it is :attr:`drefraction_model` plus the refraction curve's slope
        times oculus-core's altitude error (|dR/dh| is about 0.16 just above −1°, so a 12″
        altitude error gives about 1.9″), and it jumps by 0.6466° at a cutoff straddle.
        """
        return (self.ours_alt_refr - self.ours_alt) - (self.ref_alt_refr - self.ref_alt)

    @property
    def cutoff_straddle(self) -> NDArray[np.bool_]:
        """Epochs where the two geometric altitudes fall on opposite sides of the −1° cutoff."""
        lim = REFRACTION_DOMAIN_MIN_DEG
        return (self.ref_alt >= lim) != (self.ours_alt >= lim)

    def __len__(self) -> int:
        return int(self.unix_ms.size)


def compare_body(
    *,
    source: str,
    site_id: str,
    body: str,
    unix_ms: NDArray[np.float64],
    ref_alt: NDArray[np.float64],
    ref_az: NDArray[np.float64],
    ref_alt_refr: NDArray[np.float64],
    ref_az_refr: NDArray[np.float64],
    positions: Positions,
    ref_ra: NDArray[np.float64] | None = None,
    ref_dec: NDArray[np.float64] | None = None,
    ref_illum_pct: NDArray[np.float64] | None = None,
) -> BodyComparison:
    """Pair a reference with oculus-core's positions at the same instants."""
    ms = np.asarray(unix_ms, dtype=np.float64)
    if ms.shape != positions.unix_ms.shape or not np.array_equal(ms, positions.unix_ms):
        raise ValueError(f"{source} {site_id} {body}: the reference and oculus epochs differ")
    ours_alt, ours_az = positions.body(body)
    radec = np.full(ms.size, math.nan)
    if ref_ra is not None and ref_dec is not None:
        ra, dec = positions.radec(body)
        radec = separation_deg(dec, ra, ref_dec, ref_ra)
    illum = np.full(ms.size, math.nan)
    if body == "moon" and ref_illum_pct is not None:
        illum = positions.moon_illum * 100.0 - np.asarray(ref_illum_pct, dtype=np.float64)
    ref_alt_deg = np.asarray(ref_alt, dtype=np.float64)
    return BodyComparison(
        source=source,
        site_id=site_id,
        body=body,
        unix_ms=ms,
        ref_alt=ref_alt_deg,
        ref_az=np.asarray(ref_az, dtype=np.float64),
        ref_alt_refr=np.asarray(ref_alt_refr, dtype=np.float64),
        ref_az_refr=np.asarray(ref_az_refr, dtype=np.float64),
        ours_alt=ours_alt,
        ours_az=ours_az,
        ours_alt_refr=_refracted(ours_alt),
        ours_refr_at_ref_alt=refraction_array(ref_alt_deg),
        radec_sep=radec,
        illum_diff_pct=illum,
    )


def horizon_dip_deg(height_m: float) -> float:
    """How far below the horizontal a camera ``height_m`` above sea level sees: the dip.

    ``acos(R / (R + h))`` with the mean Earth radius: the geometric dip of a horizon at sea
    level, with no terrain and no terrestrial refraction. A body between ``-dip`` and the −1°
    refraction cutoff can be in view where the terrain toward it is lower than the camera.
    """
    h = max(0.0, float(height_m))
    return math.degrees(math.acos(EARTH_RADIUS_M / (EARTH_RADIUS_M + h)))


def dip_band(
    items: Sequence[BodyComparison],
    views: Mapping[str, tuple[float, float, float]],
) -> dict[str, Any]:
    """Epochs between each camera's horizon dip and the −1° cutoff, per site and body.

    ``views`` maps a site id to ``(height_m, heading_deg, hfov_deg)``. For each comparison of
    a site in ``views`` it counts the epochs whose reference geometric altitude lies in
    ``[-dip, -1°)``, the ones of those within ``hfov/2`` of the heading in azimuth (toward the
    view), and the refracted error there. They sit outside the −1° convention, so the verdict
    leaves them out; whether the terrain hides them is not known until the plates carry DEM
    horizon maps (the module's TODO(M2)).
    """
    out: dict[str, Any] = {}
    for c in items:
        if c.site_id not in views:
            continue
        height_m, heading_deg, hfov_deg = views[c.site_id]
        dip = horizon_dip_deg(height_m)
        band = (c.ref_alt >= -dip) & (c.ref_alt < REFRACTION_DOMAIN_MIN_DEG)
        toward = band & (np.abs(wrap180(c.ref_az - heading_deg)) <= hfov_deg / 2.0)
        sep = c.sep_refr
        entry: dict[str, Any] = {
            "n": int(band.sum()),
            "n_toward_view": int(toward.sum()),
            "beyond_target": int(np.sum(band & (sep > TARGET_DEG[c.body]))),
            "refracted_worst_deg": float(np.max(sep[band])) if band.any() else None,
        }
        if toward.any():
            entry["toward_view_times_utc"] = [
                isoformat_z(_utc(float(ms))) for ms in c.unix_ms[np.flatnonzero(toward)]
            ]
        site = out.setdefault(
            c.site_id,
            {
                "height_m": height_m,
                "dip_deg": round(dip, 4),
                "heading_deg": heading_deg,
                "hfov_deg": hfov_deg,
            },
        )
        site[c.body] = entry
    return dict(sorted(out.items()))


def _locate(group: Sequence[BodyComparison], index: int) -> tuple[BodyComparison, int]:
    for c in group:
        if index < len(c):
            return c, index
        index -= len(c)
    raise IndexError(index)


def _annotate(summary: dict[str, Any], group: Sequence[BodyComparison]) -> dict[str, Any]:
    """Replace ``worst_index`` (into the concatenated group) by the worst's site and time."""
    if summary.get("n") and "worst_index" in summary:
        c, i = _locate(group, summary.pop("worst_index"))
        summary["worst_site"] = c.site_id
        summary["worst_time_utc"] = isoformat_z(_utc(float(c.unix_ms[i])))
        summary["worst_ref_alt_deg"] = round(float(c.ref_alt[i]), 6)
    return summary


def _concat(items: Sequence[BodyComparison], name: str) -> NDArray[np.float64]:
    return np.concatenate([np.asarray(getattr(c, name)) for c in items]) if items else np.array([])


def _block(group: Sequence[BodyComparison]) -> dict[str, Any]:
    """The summaries of one group of comparisons (one body; one site or all of them)."""
    sep_geo = _concat(group, "sep_geo")
    sep_refr = _concat(group, "sep_refr")
    domain = np.concatenate([c.refraction_domain for c in group])
    straddle = np.concatenate([c.cutoff_straddle for c in group])
    bands = band_labels(_concat(group, "ref_alt"))
    model = np.where(domain, _concat(group, "drefraction_model"), np.nan)
    own_alt = np.where(domain & ~straddle, _concat(group, "drefraction_own_alt"), np.nan)
    block: dict[str, Any] = {
        "n": int(sep_geo.size),
        "geometric": summarize(sep_geo),
        "refracted": summarize(np.where(domain, sep_refr, np.nan)),
        "refracted_outside_domain": summarize(np.where(domain, np.nan, sep_refr)),
        "dalt_geometric": summarize(_concat(group, "dalt_geo")),
        "daz_on_sky_geometric": summarize(_concat(group, "daz_sky")),
        "dalt_refracted": summarize(np.where(domain, _concat(group, "dalt_refr"), np.nan)),
        "radec": summarize(_concat(group, "radec_sep")),
        "geometric_by_band": summarize_by(sep_geo, bands, order=_BAND_ORDER),
        "refracted_by_band": summarize_by(sep_refr, bands, order=_BAND_ORDER),
        # The models alone: both at the reference's geometric altitude (domain).
        "refraction_model_diff": summarize(model),
        "refraction_model_diff_by_band": summarize_by(model, bands, order=_BAND_ORDER),
        # Each at its own geometric altitude (domain, straddles aside): the models plus the
        # refraction slope times oculus-core's altitude error.
        "refraction_diff_own_altitudes": summarize(own_alt),
        "refraction_diff_own_altitudes_by_band": summarize_by(own_alt, bands, order=_BAND_ORDER),
        "cutoff_straddles": int(straddle.sum()),
    }
    if group and group[0].body == "moon":
        block["moon_illum_pct"] = summarize(_concat(group, "illum_diff_pct"))
    for key, value in block.items():
        if key.endswith("_by_band"):
            block[key] = {k: _annotate(v, group) for k, v in value.items() if v.get("n")}
        elif isinstance(value, dict):
            _annotate(value, group)
    return block


def summarize_comparisons(items: Sequence[BodyComparison]) -> dict[str, Any]:
    """Worst, p99 and the rest, per body, per site and per altitude band (degrees).

    ``geometric`` covers every epoch; ``refracted`` covers the refraction domain, and
    ``refracted_outside_domain`` the epochs below it (a convention difference, reported).
    ``radec`` and ``moon_illum_pct`` are diagnostics. Each worst names its site and time.
    """
    out: dict[str, Any] = {}
    for body in sorted({c.body for c in items}):
        group = [c for c in items if c.body == body]
        out[body] = {
            "target_deg": TARGET_DEG[body],
            "all_sites": _block(group),
            "by_site": {c.site_id: _block([c]) for c in group},
        }
    return out


def beyond_target_rows(items: Sequence[BodyComparison]) -> list[dict[str, Any]]:
    """Every epoch whose error exceeds its body's target, geometric or refracted.

    Each row says whether it is inside the domain where the target applies
    (``"domain": "target"``) or below it (``"outside"``, the refraction conventions).
    """
    rows: list[dict[str, Any]] = []
    for c in items:
        target = TARGET_DEG[c.body]
        sep_geo, sep_refr, dom = c.sep_geo, c.sep_refr, c.refraction_domain
        straddle = c.cutoff_straddle
        for quantity, sep in (("geometric", sep_geo), ("refracted", sep_refr)):
            for i in np.flatnonzero(sep > target):
                inside = True if quantity == "geometric" else bool(dom[i])
                if quantity == "geometric":
                    cause = "ephemeris (or UT1): geometric directions differ"
                elif straddle[i]:
                    cause = (
                        "refraction cutoff straddle: the two geometric altitudes lie on opposite "
                        "sides of -1 deg, where eval's refraction drops from 0.6466 deg to 0"
                    )
                elif not inside:
                    cause = (
                        "below the -1 deg cutoff: the refraction conventions differ (outside the "
                        "target's domain, a convention pending owner question 1)"
                    )
                else:
                    cause = "refraction model or ephemeris"
                rows.append(
                    {
                        "cause": cause,
                        "source": c.source,
                        "site_id": c.site_id,
                        "body": c.body,
                        "quantity": quantity,
                        "domain": "target" if inside else "outside",
                        "time_utc": isoformat_z(_utc(float(c.unix_ms[i]))),
                        "ref_alt_deg": round(float(c.ref_alt[i]), 6),
                        "ref_alt_refr_deg": round(float(c.ref_alt_refr[i]), 6),
                        "ref_az_deg": round(float(c.ref_az[i]), 6),
                        "ours_alt_deg": round(float(c.ours_alt[i]), 6),
                        "ours_alt_refr_deg": round(float(c.ours_alt_refr[i]), 6),
                        "ours_az_deg": round(float(c.ours_az[i]), 6),
                        "sep_deg": round(float(sep[i]), 6),
                        "target_deg": target,
                    }
                )
    return rows


def count_beyond(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """How many beyond-target rows per source, body, quantity and domain."""
    counts: dict[str, int] = {}
    for row in rows:
        key = f"{row['source']}/{row['body']}/{row['quantity']}/{row['domain']}"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))
