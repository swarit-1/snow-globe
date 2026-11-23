"""The ephemeris validation run: fetch (once), compare, report (PLAN.md M2; PRD §8.4).

Stages, each resumable (a stopped run is re-run and skips what is done):

1. **Plan and Horizons**: the planned sites (``<cache>/plan.json``) and their 32 observer
   tables (8 sites × Sun, Moon × airless, refracted), fetched politely once
   (:mod:`.horizons`); oculus-core at every epoch (:mod:`.compare`).
2. **Skyfield**: DE421 once, then the isolated run (:mod:`.skyfield_run`): 1,000 random
   samples, every Horizons epoch at every site (the two references against each other) and
   Skyfield's sunrises and sunsets; its output is cached.
3. **Sunrise and sunset** for every local day of the period against NOAA (:mod:`.sunrise`).
4. **Camera frames** (:mod:`.camera`), on the Horizons epochs.
5. **Report**: ``<data dir>/reports/ephem/<UTC stamp>/`` with ``report.json``, ``report.md``,
   ``beyond_target.jsonl`` (every epoch beyond its target inside the target's domain: every
   geometric case, and the refracted ones whose reference geometric altitude is −1° or
   higher), ``refracted_below_cutoff.jsonl.gz`` (the refracted cases below that convention)
   and ``events.jsonl`` (every sunrise and sunset). A run folder is never overwritten.

With ``offline=True`` nothing is fetched and nothing is installed: every input must be cached.
:func:`run_validation` returns the report; ``snow-globe-eval ephem-validate`` exits 0 when
every target is met and 1 when one is missed (:mod:`.cli`).
"""

from __future__ import annotations

import gzip
import json
import platform
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
from snow_globe_common.manifest import great_circle_km, load_manifests
from snow_globe_common.timeutil import isoformat_z, parse_iso_utc, utcnow

from ..ephemeris import get_ephemeris, refraction_deg
from .camera import CameraPose, camera_validation, pose_for
from .compare import (
    APP_CORE_REFRACTION_OFFSET_DEG,
    REFRACTION_DOMAIN_MIN_DEG,
    BodyComparison,
    Positions,
    beyond_target_rows,
    compare_body,
    count_beyond,
    dip_band,
    oculus_positions,
    refraction_array,
    summarize_comparisons,
)
from .files import atomic_write_bytes, atomic_write_json, atomic_write_text
from .horizons import HorizonsCache, HorizonsTable, current_user_agent, fetch, parse_result
from .plan import (
    PERIOD_START,
    PERIOD_STEP,
    PERIOD_STOP,
    Site,
    load_or_create_plan,
    plan_tables,
)
from .report_md import render_markdown
from .skyfield_run import (
    REFRACTION,
    SAMPLE_COUNT,
    SAMPLE_MAX_HEIGHT_M,
    SAMPLE_SEED,
    SAMPLE_START,
    SAMPLE_STOP,
    SKYFIELD_IERS_TABLE_END,
    SKYFIELD_PINS,
    build_job,
    draw_samples,
    ensure_de421,
    kernel_record,
    skyfield_output,
)
from .stats import (
    ALT_BANDS,
    TARGET_DEG,
    band_label,
    band_labels,
    hour_angle_dec_deg,
    separation_deg,
    summarize,
    summarize_by,
    wrap180,
)
from .sunrise import (
    SUNRISE_SUNSET_DEG,
    attach_skyfield,
    event_rows,
    local_day_bounds,
    summarize_events,
)

__all__ = [
    "D002_WORST_DEG",
    "REPORT_SCHEMA",
    "Period",
    "RunOptions",
    "eop_coverage",
    "refraction_reaches",
    "run_validation",
    "verdict_met",
]

#: Version 2 (from 23 Nov 2026, the review): ``refraction_model_diff`` is the models alone (both
#: at the reference's geometric altitude); version 1's quantity of that name, each model at its
#: own altitude, is ``refraction_diff_own_altitudes``. Adds ``dip_band`` and the EOP segments.
REPORT_SCHEMA = "snow-globe.ephem-validate-report/2"
_BAND_ORDER = tuple(band_label(low, high) for low, high in ALT_BANDS)
#: D-002's worst cases against Horizons (18 Nov 2026: 294 rows, 3 sites, 1950–2050), degrees;
#: PRD §2 and §8.4 and ``app/core/src/solar.rs`` quote them.
D002_WORST_DEG: Mapping[str, float] = {"sun": 0.19 / 60.0, "moon": 0.13 / 60.0}


@dataclass(frozen=True, slots=True)
class RunOptions:
    """Where the cache and the report go, and what may be fetched or run.

    ``sample_count`` is the number of random Skyfield samples; the measured run uses
    :data:`~.skyfield_run.SAMPLE_COUNT` (PRD §8.4's 1,000) and the CLI has no option for it.
    The tests set it small to replay a recorded Skyfield job.
    """

    cache_root: Path
    reports_root: Path
    offline: bool = False
    replan: bool = False
    skyfield: bool = True
    camera: bool = True
    out_dir: Path | None = None
    sample_count: int = SAMPLE_COUNT


def _ms(times: Sequence[datetime]) -> np.ndarray:
    return np.array([t.timestamp() * 1000.0 for t in times], dtype=np.float64)


def _load_tables(
    sites: Sequence[Site], period: Period, options: RunOptions, log: Callable[[str], None]
) -> tuple[dict[tuple[str, str, str], HorizonsTable], list[dict[str, Any]]]:
    cache = HorizonsCache(options.cache_root)
    tables: dict[tuple[str, str, str], HorizonsTable] = {}
    records: list[dict[str, Any]] = []
    for planned in plan_tables(sites, start=period.start, stop=period.stop, step=period.step):
        record = fetch(
            planned.query, cache=cache, label=planned.label, offline=options.offline, log=log
        )
        table = parse_result(record["response"]["result"])
        expected = planned.query.epochs()
        if list(table.times_utc) != expected:
            raise ValueError(
                f"{planned.label}: {len(table)} rows, not the {len(expected)} asked for"
            )
        key = (planned.site.viewpoint_id, planned.query.body, planned.query.apparent)
        tables[key] = table
        records.append(
            {
                "label": planned.label,
                "key": planned.query.key()[:20],
                "file": Path(record["path"]).name,
                "fetched_utc": record.get("fetched_utc"),
                "rows": len(table),
                "target": table.header.get("Target body name"),
                "refraction": table.header.get("Atmos refraction"),
                "eop_file": table.header.get("EOP file"),
                "eop_coverage": table.header.get("EOP coverage"),
                "site_geodetic": table.header.get("Center geodetic"),
            }
        )
    return tables, records


def _horizons_comparisons(
    sites: Sequence[Site], tables: Mapping[tuple[str, str, str], HorizonsTable]
) -> tuple[list[BodyComparison], dict[str, Positions]]:
    backend = get_ephemeris("oculus")
    items: list[BodyComparison] = []
    positions: dict[str, Positions] = {}
    for site in sites:
        sid = site.viewpoint_id
        ms = _ms(tables[(sid, "sun", "airless")].times_utc)
        pos = oculus_positions(ms, site.lat_deg, site.lon_deg, site.height_m, ephemeris=backend)
        positions[sid] = pos
        for body in ("sun", "moon"):
            air, refr = tables[(sid, body, "airless")], tables[(sid, body, "refracted")]
            if air.times_utc != refr.times_utc or not np.array_equal(_ms(air.times_utc), ms):
                raise ValueError(f"{sid} {body}: the airless and refracted tables differ in epochs")
            items.append(
                compare_body(
                    source="horizons",
                    site_id=sid,
                    body=body,
                    unix_ms=ms,
                    ref_alt=air.values["el_deg"],
                    ref_az=air.values["az_deg"],
                    ref_alt_refr=refr.values["el_deg"],
                    ref_az_refr=refr.values["az_deg"],
                    positions=pos,
                    ref_ra=air.values.get("ra_deg"),
                    ref_dec=air.values.get("dec_deg"),
                    ref_illum_pct=air.values.get("illum_pct"),
                )
            )
    return items, positions


def _sample_positions(
    samples: Sequence[Mapping[str, Any]], shift_ms: Sequence[float] | None = None
) -> Positions:
    """oculus-core at every random sample, each at its own place.

    ``shift_ms`` (diagnostic only) moves each instant, for example by Skyfield's UT1 − UTC so
    that oculus-core's UT1 = UTC simplification is taken out; ``unix_ms`` stays the sample's.
    """
    backend = get_ephemeris("oculus")
    shifts = [0.0] * len(samples) if shift_ms is None else list(shift_ms)
    parts = [
        oculus_positions(
            [s["unix_ms"] + d], s["lat_deg"], s["lon_deg"], s["height_m"], ephemeris=backend
        )
        for s, d in zip(samples, shifts, strict=True)
    ]

    def cat(name: str) -> np.ndarray:
        return np.concatenate([getattr(p, name) for p in parts])

    return Positions(
        unix_ms=np.array([float(s["unix_ms"]) for s in samples], dtype=np.float64),
        sun_alt=cat("sun_alt"),
        sun_az=cat("sun_az"),
        moon_alt=cat("moon_alt"),
        moon_az=cat("moon_az"),
        moon_illum=cat("moon_illum"),
        sun_ra=cat("sun_ra"),
        sun_dec=cat("sun_dec"),
        moon_ra=cat("moon_ra"),
        moon_dec=cat("moon_dec"),
        backend=parts[0].backend if parts else "",
    )


def _skyfield_job(
    kernel: Path,
    sites: Sequence[Site],
    tables: Mapping[tuple[str, str, str], HorizonsTable],
    period: Period,
    sample_count: int = SAMPLE_COUNT,
) -> dict[str, Any]:
    """The Skyfield job: the random samples, every Horizons epoch per site, the event search."""
    record = kernel_record(kernel)
    site_jobs = [
        {
            "id": s.viewpoint_id,
            "lat_deg": s.lat_deg,
            "lon_deg": s.lon_deg,
            "height_m": s.height_m,
            "unix_ms": _ms(tables[(s.viewpoint_id, "sun", "airless")].times_utc).tolist(),
        }
        for s in sites
    ]
    first, last = period.days()
    events = []
    for s in sites:
        zone = ZoneInfo(s.time_zone)
        start, _ = local_day_bounds(zone, first - timedelta(days=1))
        _, stop = local_day_bounds(zone, last + timedelta(days=1))
        events.append(
            {
                "id": s.viewpoint_id,
                "lat_deg": s.lat_deg,
                "lon_deg": s.lon_deg,
                "height_m": s.height_m,
                "start_unix_ms": start.timestamp() * 1000.0,
                "stop_unix_ms": stop.timestamp() * 1000.0,
            }
        )
    return build_job(
        kernel=kernel,
        kernel_sha256=record["sha256"],
        samples=draw_samples(sample_count),
        sites=site_jobs,
        events={"horizon_deg": SUNRISE_SUNSET_DEG, "sites": events},
    )


@dataclass(frozen=True, slots=True)
class Period:
    """The plan's period: ``start`` to ``stop`` inclusive every ``step``."""

    start: datetime
    stop: datetime
    step: str

    @classmethod
    def from_plan(cls, doc: Mapping[str, Any]) -> Period:
        """The period a plan document names (the module's 12 months if it names none)."""
        period = doc.get("period") or {}
        if not period:
            return cls(PERIOD_START, PERIOD_STOP, PERIOD_STEP)
        return cls(
            parse_iso_utc(period["start_utc"]), parse_iso_utc(period["stop_utc"]), period["step"]
        )

    def days(self) -> tuple[date, date]:
        """The first and last calendar dates (UTC) of the period."""
        return self.start.date(), self.stop.date()


def _skyfield_comparisons(
    job: Mapping[str, Any], result: Mapping[str, Any]
) -> tuple[list[BodyComparison], dict[str, Any]]:
    samples = job["samples"]
    rows = {r["id"]: r for r in result["samples"]}
    if sorted(rows) != [s["id"] for s in samples]:
        raise ValueError("the Skyfield output does not cover every sample")
    pos = _sample_positions(samples)
    items = []
    for body in ("sun", "moon"):
        ref = [rows[s["id"]][body] for s in samples]
        illum = (
            np.array([r["illuminated_fraction"] * 100.0 for r in ref]) if body == "moon" else None
        )
        items.append(
            compare_body(
                source="skyfield",
                site_id="random-1000",
                body=body,
                unix_ms=pos.unix_ms,
                ref_alt=np.array([r["alt_deg"] for r in ref]),
                ref_az=np.array([r["az_deg"] for r in ref]),
                ref_alt_refr=np.array([r["alt_refr_deg"] for r in ref]),
                ref_az_refr=np.array([r["az_refr_deg"] for r in ref]),
                positions=pos,
                ref_illum_pct=illum,
            )
        )
    dut1 = np.array([rows[s["id"]]["dut1_s"] for s in samples])
    shifted = _sample_positions(samples, (dut1 * 1000.0).tolist())
    ut1_corrected: dict[str, Any] = {}
    for c in items:
        alt, az = shifted.body(c.body)
        sep = separation_deg(alt, az, c.ref_alt, c.ref_az)
        ut1_corrected[c.body] = _strip(summarize(sep))
    lat = np.array([s["lat_deg"] for s in samples])
    years = np.array([datetime.fromtimestamp(s["unix_ms"] / 1000.0, tz=UTC).year for s in samples])
    meta = {
        "n": len(samples),
        "canonical": len(samples) == SAMPLE_COUNT,
        "seed": SAMPLE_SEED,
        "places": "latitude asin(U(-1,1)), longitude U(-180,180), height U(0, 4500 m)",
        "instants": f"uniform in [{isoformat_z(SAMPLE_START)}, {isoformat_z(SAMPLE_STOP)}) to 1 ms",
        "lat_range_deg": [float(lat.min()), float(lat.max())],
        "year_range": [int(years.min()), int(years.max())],
        "max_height_m": SAMPLE_MAX_HEIGHT_M,
        "skyfield_dut1_s": {"min": float(dut1.min()), "max": float(dut1.max())},
        "geometric_with_skyfield_ut1": ut1_corrected,
        "note": (
            "oculus-core takes UT1 = UTC; Skyfield's built-in table gives UT1 - UTC (the IERS "
            f"values and predictions shipped with Skyfield, to {SKYFIELD_IERS_TABLE_END}, "
            "extrapolated after), and |UT1 - UTC| <= 0.9 s moves a direction by at most "
            "13.5 arcsec. geometric_with_skyfield_ut1 evaluates oculus-core at UTC + "
            "(UT1 - UTC), which takes that simplification out (a diagnostic, not the product's "
            "number; approximate wherever Skyfield's value is a prediction or extrapolated)."
        ),
    }
    return items, meta


_EOP_RE = re.compile(
    r"TO\s+(\d{4})-([A-Z]{3})-(\d{2})\.\s*PREDICTS->\s*(\d{4})-([A-Z]{3})-(\d{2})", re.IGNORECASE
)
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
#: Below this spread (s) of the hour-angle offset minus Skyfield's UT1 − UTC at one site, the
#: stretch after Horizons' EOP predictions reads as a held value (5 ms is 0.075″ of rotation).
_HELD_SPREAD_S = 0.005
_MON = {m: i for i, m in enumerate(_MONTHS, 1)}


def eop_coverage(text: str | None) -> tuple[date, date] | None:
    """The last data-based day and the last predicted day of a Horizons ``EOP coverage`` line.

    ``"DATA-BASED 1962-JAN-20 TO 2026-NOV-20. PREDICTS-> 2027-FEB-15"`` gives
    ``(2026-11-20, 2027-02-15)``; None when the line is missing or reads otherwise.
    """
    match = _EOP_RE.search(text or "")
    if match is None:
        return None
    y1, m1, d1, y2, m2, d2 = match.groups()
    return (
        date(int(y1), _MON[m1.upper()], int(d1)),
        date(int(y2), _MON[m2.upper()], int(d2)),
    )


def _eop_segments(
    times: Sequence[datetime], coverage: tuple[date, date] | None
) -> dict[str, np.ndarray]:
    """Masks of the epochs on Horizons' EOP data, on its predictions, and after both."""
    if coverage is None:
        return {}
    data_end, predicts_end = coverage
    day = np.array([t.date().toordinal() for t in times])
    return {
        "eop_data": day <= data_end.toordinal(),
        "eop_predicted": (day > data_end.toordinal()) & (day <= predicts_end.toordinal()),
        "after_eop": day > predicts_end.toordinal(),
    }


def _reference_agreement(
    sites: Sequence[Site],
    tables: Mapping[tuple[str, str, str], HorizonsTable],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Skyfield (DE421) against Horizons (DE441) at every Horizons epoch: how good the truth is.

    Per body: the geometric and refracted angles; the difference split into hour angle
    (seconds of time; Earth rotation) and declination; and the hour-angle offset minus
    Skyfield's UT1 − UTC within each stretch of Horizons' EOP coverage (data, predictions,
    after both), whose spread per site shows where Horizons holds its last value. Refraction:
    ``skyfield_minus_eval_model`` is Skyfield's refraction against eval's at Skyfield's own
    geometric altitude, and ``horizons_minus_eval_model`` Horizons' against eval's at Horizons'
    (the models alone, both ≈ "Skyfield's model minus Horizons'", since Horizons' is eval's
    formula); ``refraction_diff_own_altitudes`` is Skyfield's minus Horizons', each at its own
    altitude (straddles aside), which also holds their 4″ difference in altitude times the
    refraction slope.
    """
    by_site = {r["id"]: r for r in result["sites"]}
    out: dict[str, Any] = {}
    lim = REFRACTION_DOMAIN_MIN_DEG
    coverage = None
    for s in sites:
        coverage = eop_coverage(
            tables[(s.viewpoint_id, "sun", "airless")].header.get("EOP coverage")
        )
        if coverage is not None:
            break
    for body in ("sun", "moon"):
        geo, refr, alts, dha, ddec, own, sky_model, hz_model, straddle = ([] for _ in range(9))
        spreads: dict[str, list[float]] = {}
        for s in sites:
            sky = by_site[s.viewpoint_id][body]
            air = tables[(s.viewpoint_id, body, "airless")]
            ref = tables[(s.viewpoint_id, body, "refracted")]
            s_alt, s_az = np.asarray(sky["alt_deg"]), np.asarray(sky["az_deg"])
            s_refr = np.asarray(sky["alt_refr_deg"])
            h_alt, h_az = air.values["el_deg"], air.values["az_deg"]
            geo.append(separation_deg(s_alt, s_az, h_alt, h_az))
            refr.append(
                separation_deg(
                    s_refr, sky["az_refr_deg"], ref.values["el_deg"], ref.values["az_deg"]
                )
            )
            alts.append(h_alt)
            ha_s, dec_s = hour_angle_dec_deg(s_alt, s_az, s.lat_deg)
            ha_h, dec_h = hour_angle_dec_deg(h_alt, h_az, s.lat_deg)
            site_dha = wrap180(ha_s - ha_h) * 240.0  # seconds of time
            dha.append(site_dha)
            ddec.append(dec_s - dec_h)
            own.append((s_refr - s_alt) - (ref.values["el_deg"] - h_alt))
            sky_model.append((s_refr - s_alt) - refraction_array(s_alt))
            hz_model.append((ref.values["el_deg"] - h_alt) - refraction_array(h_alt))
            straddle.append((h_alt >= lim) != (s_alt >= lim))
            held = site_dha - np.asarray(by_site[s.viewpoint_id]["dut1_s"])
            for name, mask in _eop_segments(air.times_utc, coverage).items():
                if mask.any():
                    spreads.setdefault(name, []).append(float(np.ptp(held[mask])))
        g, r, a = np.concatenate(geo), np.concatenate(refr), np.concatenate(alts)
        dom = a >= lim
        hour = np.concatenate(dha)
        cut = np.concatenate(straddle)
        sky_m = np.where(dom, np.concatenate(sky_model), np.nan)
        own_m = np.where(dom & ~cut, np.concatenate(own), np.nan)
        bands = band_labels(a)

        def by_band(values: np.ndarray, labels: np.ndarray = bands) -> dict[str, Any]:
            return {
                k: _strip(v)
                for k, v in summarize_by(values, labels, order=_BAND_ORDER).items()
                if v.get("n")
            }

        out[body] = {
            "geometric": _strip(summarize(g)),
            "hour_angle_s": {
                "mean": float(hour.mean()),
                "min": float(hour.min()),
                "max": float(hour.max()),
            },
            "hour_angle_minus_skyfield_dut1_spread_s": {
                name: {"worst_site_spread": max(v), "sites": len(v)} for name, v in spreads.items()
            },
            "declination": _strip(summarize(np.concatenate(ddec))),
            "refracted_domain": _strip(summarize(np.where(dom, r, np.nan))),
            "refracted_by_band": {
                k: _strip(v) for k, v in summarize_by(r, bands, order=_BAND_ORDER).items()
            },
            "skyfield_minus_eval_model": _strip(summarize(sky_m)),
            "skyfield_minus_eval_model_by_band": by_band(sky_m),
            "horizons_minus_eval_model": _strip(
                summarize(np.where(dom, np.concatenate(hz_model), np.nan))
            ),
            "refraction_diff_own_altitudes": _strip(summarize(own_m)),
            "refraction_diff_own_altitudes_by_band": by_band(own_m),
            "cutoff_straddles": int(cut.sum()),
        }
    dut1 = np.concatenate([np.asarray(by_site[s.viewpoint_id]["dut1_s"]) for s in sites])
    out["skyfield_dut1_s"] = {"min": float(dut1.min()), "max": float(dut1.max())}
    out["horizons_eop_coverage"] = (
        None
        if coverage is None
        else {"data_to": coverage[0].isoformat(), "predictions_to": coverage[1].isoformat()}
    )
    out["skyfield_ut1_table_to"] = SKYFIELD_IERS_TABLE_END.isoformat()
    if coverage is None:
        eop = "its EOP (no coverage line read)"
    else:
        eop = f"its measured EOP to {coverage[0]}, its predictions to {coverage[1]}"
        # The Sun's offset is the Earth-rotation gauge: DE421 and DE441 put the Sun within
        # 0.005" of each other, the Moon only within a few hundredths of an arcsecond (its
        # own position difference moves the Moon's offset by about 0.01 s).
        sun_after = out["sun"]["hour_angle_minus_skyfield_dut1_spread_s"].get("after_eop")
        if sun_after is not None:
            spread = sun_after["worst_site_spread"]
            if spread < _HELD_SPREAD_S:
                eop += (
                    " and its last value held after that (the Sun's hour-angle offset minus "
                    f"Skyfield's UT1 - UTC moves by at most {spread:.4f} s at a site there)"
                )
            else:
                eop += (
                    " and, after that, not a held value (the Sun's hour-angle offset minus "
                    f"Skyfield's UT1 - UTC moves by {spread:.4f} s at a site there)"
                )
    dec_worst = {b: (out[b]["declination"].get("worst") or 0.0) * 3600.0 for b in ("sun", "moon")}
    out["reading"] = (
        "Skyfield minus Horizons is an hour-angle offset, the same for the Sun and the Moon, "
        f"with the declinations within {dec_worst['sun']:.3f} arcsec (Sun) and "
        f"{dec_worst['moon']:.3f} arcsec (Moon): the two differ in Earth rotation, not in the "
        "ephemerides. "
        "Skyfield's UT1 - UTC is the table shipped with Skyfield (IERS values and predictions, "
        f"to {SKYFIELD_IERS_TABLE_END}, extrapolated after); Horizons' is {eop}; Horizons also "
        "applies polar motion"
    )
    return out


def _strip(summary: dict[str, Any]) -> dict[str, Any]:
    summary.pop("worst_index", None)
    return summary


def _manifest_drift(sites: Sequence[Site]) -> list[dict[str, Any]]:
    """Planned sites against the manifests now (a later correction is reported, not mixed in)."""
    try:
        manifests = load_manifests()
    except (OSError, ValueError, RuntimeError) as exc:
        return [{"error": f"manifests not loadable: {exc}"}]
    out = []
    for s in sites:
        vp = manifests.get(s.viewpoint_id)
        if vp is None:
            out.append({"viewpoint_id": s.viewpoint_id, "current": None})
            continue
        moved_m = great_circle_km(s.lat_deg, s.lon_deg, vp.camera.lat, vp.camera.lon) * 1000.0
        dh = vp.camera.elevation_m - s.height_m
        if moved_m > 0.5 or abs(dh) > 0.5:
            out.append(
                {
                    "viewpoint_id": s.viewpoint_id,
                    "planned": s.to_dict(),
                    "current": {
                        "lat_deg": vp.camera.lat,
                        "lon_deg": vp.camera.lon,
                        "height_m": vp.camera.elevation_m,
                    },
                    "moved_m": round(moved_m, 1),
                    "height_change_m": round(dh, 1),
                    "effect": (
                        "a km of site change moves the Sun < 0.001 arcsec and the Moon "
                        "< 0.6 arcsec (parallax), far below both targets"
                    ),
                }
            )
    return out


def _poses(sites: Sequence[Site]) -> dict[str, CameraPose]:
    """Each planned site's pose as the renderer gets it (a site with no manifest is left out)."""
    try:
        manifests = load_manifests()
    except (OSError, ValueError, RuntimeError):
        return {}
    return {
        s.viewpoint_id: pose_for(manifests[s.viewpoint_id])
        for s in sites
        if s.viewpoint_id in manifests
    }


def _views(
    sites: Sequence[Site], poses: Mapping[str, CameraPose]
) -> dict[str, tuple[float, float, float]]:
    """``(height_m, heading_deg, hfov_deg)`` per site for :func:`~.compare.dip_band`.

    The heading and field of view are the renderer's pose; a site with no manifest falls back
    to its planned view azimuth and the scene builder's nominal field of view.
    """
    from ..scene import SceneAssumptions

    nominal_hfov = SceneAssumptions().hfov_deg
    out: dict[str, tuple[float, float, float]] = {}
    for s in sites:
        pose = poses.get(s.viewpoint_id)
        heading = pose.heading_deg if pose else s.view_azimuth_deg
        out[s.viewpoint_id] = (s.height_m, heading, pose.hfov_deg if pose else nominal_hfov)
    return out


def _outside_counts(
    summary: Mapping[str, Any], beyond: Mapping[str, int], body: str
) -> dict[str, int]:
    """Horizons epochs below the −1° convention: how many, beyond target, and within it."""
    n = int(summary[body]["all_sites"]["refracted_outside_domain"].get("n", 0))
    over = int(beyond.get(f"horizons/{body}/refracted/outside", 0))
    return {"n": n, "beyond_target": over, "agree": n - over}


def _outside_reading(counts: Mapping[str, Mapping[str, int]]) -> str:
    """The outside-domain line of the report, from the counts."""
    sun, moon = counts.get("sun", {}), counts.get("moon", {})
    return (
        "below a geometric -1 deg eval refracts nothing and Horizons holds its -1 deg value "
        f"(0.6466 deg): {sun.get('beyond_target', 0)} Sun and {moon.get('beyond_target', 0)} "
        "Moon epochs differ by about 0.647 deg, listed in full in the .gz file; "
        f"{sun.get('agree', 0)} Sun and {moon.get('agree', 0)} Moon epochs are reverse "
        "straddles (our geometric altitude at or above -1 deg), where both refract and agree. "
        "The -1 deg is a convention pending owner question 1, not a visibility rule (dip_band)"
    )


def _verdict(
    horizons: Mapping[str, Any],
    skyfield: Mapping[str, Any] | None,
    events: Mapping[str, Any],
    beyond: Mapping[str, int],
) -> dict[str, Any]:
    """Targets met or not, from the measured worsts (no thresholds other than the PRD's).

    Per body: ``geometric_met`` (oculus-core's direction, every epoch of both references) and
    ``refracted_met`` (eval's refracted direction, the refraction domain), each with the count
    of cases beyond the target that decides it.
    """
    out: dict[str, Any] = {}
    for body, target in TARGET_DEG.items():
        lines: dict[str, Any] = {"target_deg": target}
        for source, summary in (("horizons", horizons), ("skyfield", skyfield)):
            if not summary or body not in summary:
                continue
            block = summary[body]["all_sites"]
            lines[f"{source}_geometric_worst_deg"] = block["geometric"]["worst"]
            lines[f"{source}_geometric_p99_deg"] = block["geometric"]["p99"]
            lines[f"{source}_refracted_worst_deg"] = block["refracted"]["worst"]
            lines[f"{source}_refracted_p99_deg"] = block["refracted"]["p99"]
            lines[f"{source}_cutoff_straddles"] = block["cutoff_straddles"]

        def count(quantity: str, body: str = body) -> int:
            return sum(
                v for k, v in beyond.items() if k.split("/")[1:] == [body, quantity, "target"]
            )

        lines["geometric_beyond_target"] = count("geometric")
        lines["refracted_beyond_target_in_domain"] = count("refracted")
        lines["geometric_met"] = lines["geometric_beyond_target"] == 0
        lines["refracted_met"] = lines["refracted_beyond_target_in_domain"] == 0
        lines["met"] = lines["geometric_met"] and lines["refracted_met"]
        out[body] = lines
    worst_event = events["all"]["ours_minus_noaa_s"]
    out["sunrise_sunset"] = {
        "tolerance_s": events["tolerance_s"],
        "worst_abs_s": worst_event.get("worst"),
        "beyond_60_s": worst_event.get("beyond_60_s"),
        "met": worst_event.get("beyond_60_s") == 0
        and events["missing_ours"] == 0
        and events["missing_noaa"] == 0,
    }
    return out


def verdict_met(verdict: Mapping[str, Any]) -> bool:
    """True when every target of a :func:`_verdict` is met (Sun, Moon, sunrise and sunset)."""
    return all(bool((verdict.get(k) or {}).get("met")) for k in ("sun", "moon", "sunrise_sunset"))


def refraction_reaches(target_deg: float) -> float:
    """The geometric altitude above which eval's refraction is below ``target_deg`` (bisection)."""
    lo, hi = 0.0, 89.0
    if refraction_deg(lo) < target_deg:
        return lo
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if refraction_deg(mid) > target_deg:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _arcsec(deg: Any, digits: int = 1) -> str:
    """Degrees in arcseconds for the findings; a value that would round to zero as 1.2e-06."""
    if not isinstance(deg, (int, float)):
        return "n/a"
    value = deg * 3600.0
    if value != 0.0 and abs(value) < 0.5 * 10.0**-digits:
        return f"{value:.1e} arcsec"
    return f"{value:.{digits}f} arcsec"


def _arcmin(deg: Any) -> str:
    return f"{deg * 60.0:.3f}'" if isinstance(deg, (int, float)) else "n/a"


def _worst(block: Mapping[str, Any] | None, key: str) -> float | None:
    value = ((block or {}).get(key) or {}).get("worst")
    return float(value) if isinstance(value, (int, float)) else None


def _worst_band(by_band: Mapping[str, Any] | None) -> str | None:
    """The band label whose worst is largest, or None."""
    bands = [(v["worst"], k) for k, v in (by_band or {}).items() if v.get("n")]
    return max(bands)[1] if bands else None


def _d002_finding(report: Mapping[str, Any]) -> str:
    """D-002's worst cases against this run's (PRD §2, §8.4 quote D-002)."""
    hz = report["horizons"]["summary"]
    samples = ((report.get("skyfield") or {}).get("samples") or {}).get("summary") or {}
    now = {b: hz[b]["all_sites"]["geometric"].get("worst") for b in ("sun", "moon")}
    sky = {b: _worst((samples.get(b) or {}).get("all_sites"), "geometric") for b in now}
    beyond = [b for b in now if (now[b] or 0.0) > D002_WORST_DEG[b]]
    verb = "are superseded" if beyond else "still hold"
    line = (
        f"D-002's worst cases against Horizons ({_arcmin(D002_WORST_DEG['sun'])} Sun, "
        f"{_arcmin(D002_WORST_DEG['moon'])} Moon; 294 rows, 3 sites, 1950-2050), which PRD §2 "
        f"and §8.4 and app/core's solar.rs quote, {verb}: this run measures "
        f"{_arcmin(now['sun'])} (Sun) and {_arcmin(now['moon'])} (Moon) against Horizons"
    )
    if sky["sun"] is not None:
        line += f", and {_arcmin(sky['sun'])} and {_arcmin(sky['moon'])} against Skyfield"
    targets = report["targets_deg"]
    return line + f"; the targets are {_arcmin(targets['sun'])} and {_arcmin(targets['moon'])}."


def _refraction_finding(hz: Mapping[str, Any], samples: Mapping[str, Any]) -> str:
    """The refraction models alone, and what the each-at-its-own-altitude number really is."""
    bodies = ("sun", "moon")
    model = max(_worst(hz[b]["all_sites"], "refraction_model_diff") or 0.0 for b in bodies)
    own = max(_worst(hz[b]["all_sites"], "refraction_diff_own_altitudes") or 0.0 for b in bodies)
    line = (
        "Refraction: at one input altitude (the reference's geometric altitude) eval's "
        f"Saemundsson and Horizons' refraction differ by at most {_arcsec(model, 4)} over the "
        "domain: the same formula. Each at its own geometric altitude they differ by up to "
        f"{_arcsec(own, 2)} (straddles aside), which is the refraction curve's slope times "
        "oculus-core's altitude error, not the models."
    )
    if samples:
        sky = [(samples[b]["all_sites"], b) for b in bodies if b in samples]
        sky_model = max(_worst(block, "refraction_model_diff") or 0.0 for block, _ in sky)
        sun_block = samples.get("sun", {}).get("all_sites", {})
        band = _worst_band(sun_block.get("refraction_model_diff_by_band"))
        line += (
            f" Skyfield's Bennett formula, iterated, differs from eval's by up to "
            f"{_arcsec(sky_model)} at one altitude on the random samples"
            + (f" (the Sun's worst in the {band} deg band)" if band else "")
            + "."
        )
    return line + (
        " app/core refracts with oculus-core's refraction_from_true: eval's formula without "
        "Meeus' +0.0019279 arcmin, so its refracted altitudes sit "
        f"{_arcsec(-APP_CORE_REFRACTION_OFFSET_DEG, 3)} below eval's (read from the pinned "
        "source; the binding does not expose it, so it is not measured here)."
    )


def _dip_finding(dip: Mapping[str, Any]) -> str | None:
    """The epochs between each camera's horizon dip and the −1° cutoff, in words."""
    totals = {"sun": 0, "moon": 0}
    toward: list[str] = []
    worst = 0.0
    for site, entry in dip.items():
        for body in totals:
            e = entry.get(body) or {}
            totals[body] += e.get("n", 0)
            worst = max(worst, e.get("refracted_worst_deg") or 0.0)
            if e.get("n_toward_view"):
                toward.append(f"{site} {body} {e['n_toward_view']}")
    if not any(totals.values()):
        return None
    return (
        "Between each camera's horizon dip and the -1 deg cutoff (outside the convention, so "
        f"outside the verdict): {totals['sun']} Sun and {totals['moon']} Moon epochs, toward "
        f"the view (within hfov/2 of the heading) {', '.join(toward) or 'none'}. eval and "
        "app/core refract nothing there while Horizons applies its held 0.6466 deg, an error of "
        f"up to {worst:.3f} deg, beyond both targets; whether the terrain hides the body there "
        "is not known until the plates carry DEM horizon maps (TODO(M2))."
    )


def _camera_finding(cam: Mapping[str, Any], sun_target_deg: float) -> str:
    """The camera stage in words: parity, the ephemeris through it, the SceneSpec gap."""
    worst_band: dict[str, float] = {}
    ephem_px = 0.0
    per_body: dict[str, tuple[float, str, float, str]] = {}
    for site, bodies in cam["sites"].items():
        for body, entry in bodies.items():
            ephem_px = max(ephem_px, (entry.get("ephemeris_px") or {}).get("worst", 0.0))
            for band, v in (entry.get("scene_geometric_vs_apparent_px_by_band") or {}).items():
                worst_band[band] = max(worst_band.get(band, 0.0), v["worst"])
            gap = (entry.get("scene_geometric_vs_apparent_px") or {}).get("worst")
            light = (entry.get("light_direction_vs_apparent_deg") or {}).get("worst")
            if gap is None or light is None:
                continue
            px, px_site, deg, deg_site = per_body.get(body, (0.0, "", 0.0, ""))
            if gap > px:
                px, px_site = gap, site
            if light > deg:
                deg, deg_site = light, site
            per_body[body] = (px, px_site, deg, deg_site)
    bands = ", ".join(f"{k} deg: {v:.1f} px" for k, v in worst_band.items())
    bodies_text = "; ".join(
        f"{body.capitalize()} up to {px:.1f} px ({px_site}) and {deg:.3f} deg ({deg_site})"
        for body, (px, px_site, deg, deg_site) in sorted(per_body.items(), reverse=True)
    )
    mismatches = cam.get("parity_directions", {}).get("front_mismatches")
    return (
        "Camera frames: the renderer's camera.ts matches an independent implementation to "
        f"{cam['parity_renderer_vs_independent_px']['worst']:.1e} px"
        + (
            f" ({mismatches} directions where the two disagree on in front or behind)"
            if mismatches is not None
            else ""
        )
        + f", and the ephemeris error through it is at most {ephem_px:.2f} px. But scene.sun and "
        "scene.moon carry the geometric altitude, so the disk the renderer draws, and its light "
        "direction, sit below where the body appears by the refraction: "
        f"{bodies_text or 'no body in frame'}; worst by band, both bodies, {bands or 'none'} "
        "(native frame size). eval's refraction exceeds the Sun target below "
        f"{refraction_reaches(sun_target_deg):.1f} deg of geometric altitude."
    )


def _findings(report: Mapping[str, Any]) -> list[str]:
    """The bottlenecks and causes, in words, from the measured numbers of ``report``."""
    out: list[str] = []
    hz = report["horizons"]["summary"]
    sky = report.get("skyfield") or {}
    samples = (sky.get("samples") or {}).get("summary") or {}
    ut1 = (sky.get("samples") or {}).get("geometric_with_skyfield_ut1") or {}
    for body, model in (
        ("sun", "JPL mean Keplerian elements (Standish 1992)"),
        ("moon", "truncated ELP-2000/82 (Meeus ch. 47)"),
    ):
        h = hz[body]["all_sites"]
        line = (
            f"{body.capitalize()}: oculus-core's geometric error is at most "
            f"{_arcsec(h['geometric']['worst'])} against Horizons over the 12 months "
            f"(apparent RA/Dec alone {_arcsec(h['radec'].get('worst'))}, so it is the {model}, "
            "not Earth rotation)"
        )
        if body in samples:
            sample_worst = samples[body]["all_sites"]["geometric"]["worst"]
            line += (
                f"; over 1998-2030 against Skyfield {_arcsec(sample_worst)}"
                f", {_arcsec((ut1.get(body) or {}).get('worst'))} once UT1 - UTC is taken out "
                "(oculus-core takes UT1 = UTC)"
            )
        line += f". Target {report['targets_deg'][body]} deg."
        out.append(line)
    out.append(_d002_finding(report))
    out.append(_refraction_finding(hz, samples))
    inside = report["beyond_target"]["in_domain"]
    if inside:
        cases = "; ".join(
            f"{r['source']} {r['site_id']} {r['body']} {r['quantity']} {r['time_utc']}: "
            f"{r['sep_deg']:.4f} deg ({r['cause']})"
            for r in inside
        )
        out.append(f"Cases beyond target inside the domain: {cases}.")
    outside = report["beyond_target"].get("outside_domain_horizons") or {}
    if any(v["n"] for v in outside.values()):
        parts = "; ".join(
            f"{body}: {v['n']} epochs, {v['beyond_target']} beyond target and {v['agree']} "
            "within it (reverse straddles: our geometric altitude at or above -1 deg, so both "
            "refract)"
            for body, v in outside.items()
        )
        out.append(
            "Horizons epochs below the -1 deg convention (outside the verdict, listed in "
            f"{report['beyond_target']['outside_domain_file']}): {parts}."
        )
    dip_line = _dip_finding(report.get("dip_band") or {})
    if dip_line:
        out.append(dip_line)
    cam = report.get("camera") or {}
    if (cam.get("renderer") or {}).get("ran"):
        out.append(_camera_finding(cam, report["targets_deg"]["sun"]))
    ev = report["sunrise_sunset"]["all"]
    out.append(
        "Sunrise and sunset: ours minus NOAA at most "
        f"{(ev['ours_minus_noaa_s'] or {}).get('worst', float('nan')):.2f} s; ours minus Skyfield "
        f"{(ev['ours_minus_skyfield_s'] or {}).get('worst', float('nan')):.2f} s; NOAA minus "
        f"Skyfield {(ev['noaa_minus_skyfield_s'] or {}).get('worst', float('nan')):.2f} s "
        "(NOAA's own simplifications are the larger part)."
    )
    return out


def run_validation(options: RunOptions, *, log: Callable[[str], None] = print) -> dict[str, Any]:
    """Run every stage and write the report; returns the report (see the module docstring)."""
    started = time.monotonic()
    timings: dict[str, float] = {}
    sites, plan_doc = load_or_create_plan(options.cache_root, replan=options.replan)
    period = Period.from_plan(plan_doc)
    tables, table_records = _load_tables(sites, period, options, log)
    timings["horizons_load_s"] = time.monotonic() - started
    t = time.monotonic()
    items, _ = _horizons_comparisons(sites, tables)
    horizons_summary = summarize_comparisons(items)
    rows = beyond_target_rows(items)
    timings["horizons_compare_s"] = time.monotonic() - t

    skyfield_block: dict[str, Any] | None = None
    skyfield_summary: dict[str, Any] | None = None
    sky_events: list[dict[str, Any]] = []
    if options.skyfield:
        t = time.monotonic()
        kernel = ensure_de421(options.cache_root, offline=options.offline, log=log)
        job = _skyfield_job(kernel, sites, tables, period, options.sample_count)
        result = skyfield_output(options.cache_root, job, offline=options.offline, log=log)
        sky_items, sample_meta = _skyfield_comparisons(job, result)
        skyfield_summary = summarize_comparisons(sky_items)
        rows += beyond_target_rows(sky_items)
        sky_events = list(result.get("events", []))
        skyfield_block = {
            "versions": result.get("versions"),
            "python": result.get("python"),
            "isolated": result.get("isolated"),
            "pins": list(SKYFIELD_PINS),
            "kernel": {**kernel_record(kernel), "file": kernel.name},
            "timescale": (
                "Skyfield built-in (builtin=True): shipped IERS UT1-UTC, delta T, leap seconds"
            ),
            "refraction": dict(REFRACTION),
            "output_file": Path(result["path"]).name,
            "samples": {**sample_meta, "summary": skyfield_summary},
            "reference_agreement_skyfield_vs_horizons": _reference_agreement(sites, tables, result),
        }
        timings["skyfield_s"] = time.monotonic() - t

    t = time.monotonic()
    first, last = period.days()
    events = event_rows(sites, first, last)
    if sky_events:
        attach_skyfield(events, sky_events)
    events_summary = summarize_events(events)
    timings["sunrise_s"] = time.monotonic() - t

    poses = _poses(sites)
    dip = dip_band(items, _views(sites, poses))
    camera_block: dict[str, Any] | None = None
    if options.camera:
        t = time.monotonic()
        camera_block = camera_validation(poses, items)
        timings["camera_s"] = time.monotonic() - t

    beyond = count_beyond(rows)
    outside_domain = {
        body: _outside_counts(horizons_summary, beyond, body) for body in ("sun", "moon")
    }
    backend = get_ephemeris("oculus")
    report = {
        "schema": REPORT_SCHEMA,
        "created_utc": isoformat_z(utcnow()),
        "ephemeris": f"{backend.name} {backend.version}",
        "refraction": (
            "eval saemundsson: 1010 hPa, 10 C, applied for geometric altitude >= -1 deg; "
            "app/core's (oculus-core refraction_from_true) is the same formula without Meeus' "
            "+0.0019279 arcmin, so 0.116 arcsec lower (from the pinned source, not measured)"
        ),
        "app_core_refraction_offset_deg": APP_CORE_REFRACTION_OFFSET_DEG,
        "targets_deg": dict(TARGET_DEG),
        "refraction_domain_min_deg": REFRACTION_DOMAIN_MIN_DEG,
        "refraction_domain_rule": (
            "a convention pending owner question 1, not a visibility rule: a camera above sea "
            "level sees down to its horizon dip (dip_band)"
        ),
        "python": platform.python_version(),
        "user_agent": current_user_agent(),
        "plan": plan_doc,
        "manifest_drift": _manifest_drift(sites),
        "horizons": {"tables": table_records, "summary": horizons_summary},
        "skyfield": skyfield_block,
        "sunrise_sunset": events_summary,
        "camera": camera_block,
        "dip_band": dip,
        "beyond_target": {
            "counts": beyond,
            "in_domain": [r for r in rows if r["domain"] == "target"],
            "file": "beyond_target.jsonl",
            "outside_domain_file": "refracted_below_cutoff.jsonl.gz",
            "outside_domain_horizons": outside_domain,
            "outside_domain_reading": _outside_reading(outside_domain),
        },
        "verdict": _verdict(horizons_summary, skyfield_summary, events_summary, beyond),
        "timings_s": {k: round(v, 3) for k, v in timings.items()},
    }
    out_dir = options.out_dir or options.reports_root / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    if out_dir.exists() and any(out_dir.iterdir()):
        raise ValueError(f"{out_dir} exists; runs are never overwritten")
    inside = [r for r in rows if r["domain"] == "target"]
    outside = [r for r in rows if r["domain"] != "target"]
    atomic_write_text(
        out_dir / "beyond_target.jsonl", "".join(json.dumps(r) + "\n" for r in inside)
    )
    atomic_write_bytes(
        out_dir / "refracted_below_cutoff.jsonl.gz",
        gzip.compress("".join(json.dumps(r) + "\n" for r in outside).encode("utf-8"), mtime=0),
    )
    atomic_write_text(
        out_dir / "events.jsonl",
        "".join(
            json.dumps({k: v for k, v in r.items() if not k.endswith("_ms")}) + "\n" for r in events
        ),
    )
    report["findings"] = _findings(report)
    report["timings_s"]["total_s"] = round(time.monotonic() - started, 3)
    atomic_write_json(out_dir / "report.json", report, indent=2)
    atomic_write_text(out_dir / "report.md", render_markdown(report))
    report["out_dir"] = str(out_dir)
    log(f"report: {out_dir}")
    return report
