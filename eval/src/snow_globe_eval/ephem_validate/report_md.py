"""``report.md``: the ephemeris validation's numbers as tables (PLAN.md M2 "The ephemeris report").

Everything here is read from ``report.json``; nothing is recomputed or rounded before the
table cell. Small angles are shown in arcseconds (″ = deg × 3600) beside the target in degrees.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["camera_section", "dip_section", "render_markdown"]


def _as(deg: Any, digits: int = 2) -> str:
    """Degrees as arcseconds, or a dash; a value that would round to zero is shown as 1.2e-06″."""
    if not isinstance(deg, (int, float)):
        return "–"
    value = deg * 3600.0
    if value != 0.0 and abs(value) < 0.5 * 10.0**-digits:
        return f"{value:.1e}″"
    return f"{value:.{digits}f}″"


def _deg(deg: Any, digits: int = 4) -> str:
    if not isinstance(deg, (int, float)):
        return "–"
    return f"{deg:.{digits}f}°"


def _num(value: Any, digits: int = 2) -> str:
    if not isinstance(value, (int, float)):
        return "–"
    return f"{value:.{digits}f}"


def _span(block: Mapping[str, Any]) -> str:
    """``min to max`` with signs, four decimals."""
    return f"{block['min']:+.4f} to {block['max']:+.4f}"


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    return [_row(header), _row(["---"] * len(header)), *(_row(r) for r in rows), ""]


def _bands(
    lines: list[str], title: str, block: Mapping[str, Any], key: str, *, digits: int = 2
) -> None:
    rows = []
    for band, s in block.get(key, {}).items():
        if not s.get("n"):
            continue
        rows.append(
            [
                band,
                str(s["n"]),
                _as(s["worst"], digits),
                _as(s["p99"], digits),
                _as(s["median"], digits),
            ]
        )
    if not rows:
        return
    lines.append(f"**{title}**")
    lines.append("")
    lines.extend(_table(["geometric alt band (deg)", "n", "worst", "p99", "median"], rows))


def camera_section(cam: Mapping[str, Any]) -> list[str]:
    """The camera-frame section: parity, the round trip and the per-site, per-body table."""
    lines = ["## The transform into each camera frame (renderer `camera.ts`)", ""]
    if not cam["renderer"]["ran"]:
        return [*lines, f"Not run: {cam['renderer']['why']}", ""]
    par = cam["parity_renderer_vs_independent_px"]
    rt = cam["renderer_round_trip_deg"]
    ld = cam["parity_light_direction_deg"]
    counts = cam.get("parity_directions") or {}
    lines += [
        f"- Renderer vs an independent rotation-matrix implementation: worst "
        f"{par['worst']:.2e} px over {par['n']} directions in front of the camera; light "
        f"direction worst {ld['worst']:.2e}°.",
    ]
    if counts:
        lines.append(
            f"- Directions in front: {counts['in_front']}, behind: {counts['behind']}; the two "
            f"disagree on in front or behind for {counts['front_mismatches']}."
        )
    lines += [
        f"- Renderer round trip pixelRay(projectDirection(d)) vs d: worst {rt['worst']:.2e}°.",
        f"- In frame: {cam['in_frame_rule']}. Pixels at each camera's native size.",
        "",
    ]
    rows = []
    for site, bodies in cam["sites"].items():
        pose = cam["poses"][site]
        frame = f"{pose['width']}×{pose['height']}"
        for body, e in bodies.items():
            if not e.get("n_in_frame"):
                rows.append([site, body, "0", "–", "–", "–", "–", frame])
                continue
            rows.append(
                [
                    site,
                    body,
                    str(e["n_in_frame"]),
                    _num(e["ephemeris_px"]["worst"], 3),
                    _num(e["ephemeris_px"]["p99"], 3),
                    _num(e["scene_geometric_vs_apparent_px"]["worst"], 1),
                    _deg(e["light_direction_vs_apparent_deg"]["worst"], 3),
                    frame,
                ]
            )
    return lines + _table(
        [
            "site",
            "body",
            "in frame",
            "ephemeris px worst",
            "ephemeris px p99",
            "scene geometric vs apparent px worst",
            "light dir vs apparent worst",
            "frame",
        ],
        rows,
    )


def dip_section(dip: Mapping[str, Any]) -> list[str]:
    """Epochs between each camera's horizon dip and the −1° cutoff (outside the convention)."""
    if not dip:
        return []
    rows = []
    for site, e in dip.items():
        cells = [site, _num(e["height_m"], 0), _deg(e["dip_deg"], 3)]
        for body in ("sun", "moon"):
            b = e.get(body) or {}
            cells.append(f"{b.get('n', 0)} ({b.get('n_toward_view', 0)})")
        worst = max((e.get(b) or {}).get("refracted_worst_deg") or 0.0 for b in ("sun", "moon"))
        cells.append(_deg(worst, 3) if worst else "–")
        rows.append(cells)
    return [
        "## Between each camera's horizon dip and the −1° cutoff",
        "",
        "Outside the −1° convention, so outside the verdict; whether the terrain hides the body "
        "is not known until the plates carry DEM horizon maps (TODO(M2)). In brackets: toward "
        "the view (azimuth within hfov/2 of the heading).",
        "",
        *_table(
            [
                "site",
                "height (m)",
                "dip",
                "Sun epochs (toward view)",
                "Moon epochs (toward view)",
                "refracted error worst",
            ],
            rows,
        ),
    ]


def render_markdown(report: Mapping[str, Any]) -> str:
    """The report as Markdown."""
    lines: list[str] = []
    plan = report["plan"]
    lines += [
        "# Ephemeris validation (PRD §8.4, PLAN.md M2)",
        "",
        f"- Created {report['created_utc']}; ephemeris under test: **{report['ephemeris']}** "
        "through eval's binding (the core `app/core` uses).",
        f"- Refraction under test: {report['refraction']}.",
        f"- Targets: Sun ≤ {report['targets_deg']['sun']}°, Moon ≤ {report['targets_deg']['moon']}°"
        " (angle on the sky between our direction and the reference's).",
        f"- Period: {plan['period']['start_utc']} to {plan['period']['stop_utc']}, step "
        f"{plan['period']['step']}; sites planned {plan['created_utc']} from the manifests.",
        f"- Refracted target judged where the reference's geometric altitude is ≥ "
        f"{report['refraction_domain_min_deg']}°: a convention pending owner question 1, not a "
        "visibility rule (below it each model's refraction is a convention; those epochs are "
        "reported apart, and the ones between −1° and each camera's horizon dip on their own "
        "line).",
        "",
        "## Verdict",
        "",
    ]
    rows = []
    for body in ("sun", "moon"):
        v = report["verdict"][body]
        rows.append(
            [
                body,
                _deg(v["target_deg"], 2),
                _as(v.get("horizons_geometric_worst_deg")),
                _as(v.get("skyfield_geometric_worst_deg")),
                "yes" if v["geometric_met"] else f"**no** ({v['geometric_beyond_target']})",
                _as(v.get("horizons_refracted_worst_deg")),
                _as(v.get("horizons_refracted_p99_deg")),
                _as(v.get("skyfield_refracted_worst_deg")),
                "yes"
                if v["refracted_met"]
                else f"**no** ({v['refracted_beyond_target_in_domain']})",
            ]
        )
    lines += _table(
        [
            "body",
            "target",
            "geo worst, Horizons",
            "geo worst, Skyfield",
            "geometric met",
            "refr worst, Horizons",
            "refr p99, Horizons",
            "refr worst, Skyfield",
            "refracted met (cases)",
        ],
        rows,
    )
    findings = report.get("findings") or []
    if findings:
        lines += ["## Findings and bottlenecks", ""]
        lines += [f"- {f}" for f in findings]
        lines.append("")
    ss = report["verdict"]["sunrise_sunset"]
    lines += [
        f"Sunrise and sunset against NOAA: worst |ours − NOAA| = {_num(ss['worst_abs_s'])} s; "
        f"{ss['beyond_60_s']} beyond ±{ss['tolerance_s']:g} s; "
        f"met: {'yes' if ss['met'] else '**no**'}.",
        "",
    ]
    for source in ("horizons", "skyfield"):
        if source == "horizons":
            summary = report["horizons"]["summary"]
            title = "## JPL Horizons, per viewpoint, hourly for 12 months"
        else:
            sky = report.get("skyfield")
            if not sky:
                continue
            summary = sky["samples"]["summary"]
            title = f"## Skyfield (DE421), {sky['samples']['n']} random places and instants"
        lines += [title, ""]
        for body in ("sun", "moon"):
            block = summary[body]
            allb = block["all_sites"]
            lines.append(f"### {body.capitalize()} (target {block['target_deg']}°)")
            lines.append("")
            rows = []
            for site, b in [("all", allb), *sorted(block["by_site"].items())]:
                rows.append(
                    [
                        site,
                        str(b["n"]),
                        _as(b["geometric"].get("worst")),
                        _as(b["geometric"].get("p99")),
                        _as(b["refracted"].get("worst")),
                        _as(b["refracted"].get("p99")),
                        str(b["refracted"].get("n", 0)),
                        _deg(b["refracted_outside_domain"].get("worst")),
                        str(b["geometric"].get("worst_time_utc", "–")),
                    ]
                )
            lines += _table(
                [
                    "site",
                    "n",
                    "geo worst",
                    "geo p99",
                    "refr worst",
                    "refr p99",
                    "refr n (domain)",
                    "refr worst below -1°",
                    "geo worst at",
                ],
                rows,
            )
            _bands(lines, "Geometric error by altitude band (all sites)", allb, "geometric_by_band")
            _bands(lines, "Refracted error by altitude band (all sites)", allb, "refracted_by_band")
            diag = [
                f"altitude part: worst {_as(allb['dalt_geometric'].get('worst'))}, "
                f"mean {_as(allb['dalt_geometric'].get('mean'))}",
                "azimuth part (on the sky): worst "
                f"{_as(allb['daz_on_sky_geometric'].get('worst'))}",
            ]
            if allb["radec"].get("n"):
                diag.append(f"apparent RA/Dec alone: worst {_as(allb['radec'].get('worst'))}")
            model = allb.get("refraction_model_diff", {})
            if model.get("n"):
                diag.append(
                    "refraction models alone (eval's minus the reference's, both at the "
                    f"reference's geometric altitude; domain): worst {_as(model.get('worst'), 4)}, "
                    f"mean {_as(model.get('mean'), 4)}"
                )
            own = allb.get("refraction_diff_own_altitudes", {})
            if own.get("n"):
                diag.append(
                    "refraction each at its own geometric altitude (the models plus the "
                    "refraction slope × our altitude error; domain, straddles aside): worst "
                    f"{_as(own.get('worst'))}"
                )
            diag.append(f"epochs straddling the -1° cutoff: {allb.get('cutoff_straddles', 0)}")
            if "moon_illum_pct" in allb and allb["moon_illum_pct"].get("n"):
                diag.append(
                    "illuminated fraction: worst "
                    f"{_num(allb['moon_illum_pct'].get('worst'), 4)} percentage points"
                )
            lines += ["Diagnostics: " + "; ".join(diag) + ".", ""]
            if source == "skyfield":
                ut1 = sky["samples"].get("geometric_with_skyfield_ut1", {}).get(body, {})
                if ut1.get("n"):
                    lines += [
                        "With UT1 − UTC from Skyfield's table taken out (oculus-core at UTC + "
                        f"(UT1 − UTC); a diagnostic): geometric worst {_as(ut1['worst'])}, "
                        f"p99 {_as(ut1['p99'])}.",
                        "",
                    ]
            _bands(
                lines,
                "Refraction models alone by band (eval's minus the reference's, both at the "
                "reference's geometric altitude; domain)",
                allb,
                "refraction_model_diff_by_band",
                digits=4,
            )
            _bands(
                lines,
                "Refraction each at its own geometric altitude, by band (the models plus the "
                "refraction slope × our altitude error; domain, straddles aside)",
                allb,
                "refraction_diff_own_altitudes_by_band",
            )
    sky = report.get("skyfield")
    if sky:
        agree = sky["reference_agreement_skyfield_vs_horizons"]
        lines += ["## The two references against each other (Skyfield DE421 vs Horizons DE441)", ""]
        rows = [
            [
                body,
                _as(agree[body]["geometric"].get("worst"), 3),
                _as(agree[body]["geometric"].get("p99"), 3),
                _span(agree[body]["hour_angle_s"]),
                _as(agree[body]["declination"].get("worst"), 3),
                _as(agree[body]["skyfield_minus_eval_model"].get("worst"), 2),
                _as(agree[body]["horizons_minus_eval_model"].get("worst"), 4),
                _as(agree[body]["refraction_diff_own_altitudes"].get("worst"), 2),
                str(agree[body]["cutoff_straddles"]),
            ]
            for body in ("sun", "moon")
        ]
        lines += _table(
            [
                "body",
                "geo worst",
                "geo p99",
                "hour angle, Skyfield − Horizons (s)",
                "declination worst",
                "refraction, Skyfield − eval, one altitude (domain) worst",
                "refraction, Horizons − eval, one altitude (domain) worst",
                "refraction, Skyfield − Horizons, own altitudes (straddles aside) worst",
                "cutoff straddles",
            ],
            rows,
        )
        lines += [
            f"Reading: {agree['reading']}. Skyfield's UT1 − UTC over the period: "
            f"{agree['skyfield_dut1_s']['min']:+.4f} to {agree['skyfield_dut1_s']['max']:+.4f} s.",
            "",
        ]
        spreads = {
            body: agree[body].get("hour_angle_minus_skyfield_dut1_spread_s") or {}
            for body in ("sun", "moon")
        }
        if any(spreads.values()):
            parts = [
                f"{body.capitalize()} "
                + ", ".join(f"{k} {v['worst_site_spread']:.4f}" for k, v in spread.items())
                for body, spread in spreads.items()
                if spread
            ]
            lines += [
                "Hour-angle offset minus Skyfield's UT1 − UTC, the largest spread at one site (s): "
                + "; ".join(parts)
                + ". The Sun's is the Earth-rotation gauge (DE421 and DE441 put the Sun within "
                "0.005″ of each other): where it does not move, Horizons holds its last "
                "UT1 − UTC. The Moon's also carries the two ephemerides' Moon positions.",
                "",
            ]
        lines += [
            f"Skyfield {sky['versions'].get('skyfield')}, DE421 SHA-256 {sky['kernel']['sha256']}, "
            f"pins: {', '.join(sky['pins'])}; isolated (python -I): {sky['isolated']}.",
            "",
        ]
        for body in ("sun", "moon"):
            _bands(
                lines,
                f"Refraction models alone by band, {body.capitalize()}: Skyfield (Bennett 1982, "
                "iterated) − eval (Sæmundsson, the formula Horizons uses), both at Skyfield's "
                "geometric altitude (domain)",
                agree[body],
                "skyfield_minus_eval_model_by_band",
            )
    ev = report["sunrise_sunset"]
    lines += ["## Sunrise and sunset (Sun's centre at −0.833°, every local day of the period)", ""]
    rows = []
    for kind in ("sunrise", "sunset"):
        part = ev["by_kind"][kind]
        for field, label in (
            ("ours_minus_noaa_s", "ours − NOAA"),
            ("ours_minus_skyfield_s", "ours − Skyfield"),
            ("noaa_minus_skyfield_s", "NOAA − Skyfield"),
        ):
            s = part[field]
            if not s.get("n"):
                continue
            rows.append(
                [
                    kind,
                    label,
                    str(s["n"]),
                    _num(s["worst"]),
                    _num(s["p99"]),
                    _num(s["mean"]),
                    str(s.get("beyond_60_s", "–")),
                    f"{s.get('worst_site')} {s.get('worst_date')}",
                ]
            )
    lines += _table(
        ["event", "difference", "n", "worst |s|", "p99 |s|", "mean s", "beyond 60 s", "worst at"],
        rows,
    )
    cam = report.get("camera")
    if cam:
        lines += camera_section(cam)
    beyond = report["beyond_target"]
    lines += ["## Cases beyond target", "", "Counts (source/body/quantity/domain):", ""]
    lines += [f"- `{k}`: {v}" for k, v in beyond["counts"].items()] or ["- none"]
    lines += [
        "",
        f"Outside the domain ({beyond.get('outside_domain_file')}): "
        f"{beyond.get('outside_domain_reading')}.",
        "",
        "Inside the target's domain:",
        "",
    ]
    if beyond["in_domain"]:
        rows = [
            [
                r["source"],
                r["site_id"],
                r["body"],
                r["quantity"],
                r["time_utc"],
                _deg(r["ref_alt_deg"]),
                _deg(r["ours_alt_deg"]),
                _deg(r["ref_alt_refr_deg"]),
                _deg(r["ours_alt_refr_deg"]),
                _deg(r["sep_deg"]),
                r.get("cause", ""),
            ]
            for r in beyond["in_domain"]
        ]
        lines += _table(
            [
                "source",
                "site",
                "body",
                "quantity",
                "time",
                "ref geo alt",
                "our geo alt",
                "ref refr alt",
                "our refr alt",
                "error",
                "cause",
            ],
            rows,
        )
    else:
        lines += ["None.", ""]
    lines += dip_section(report.get("dip_band") or {})
    drift = report.get("manifest_drift") or []
    if drift:
        lines += ["## Sites moved in the manifests since the plan", ""]
        for d in drift:
            lines.append(f"- {d}")
        lines.append("")
    lines += [f"Timings: {report['timings_s']}", ""]
    return "\n".join(lines)
