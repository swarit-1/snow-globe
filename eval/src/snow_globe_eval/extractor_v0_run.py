"""Extractor v0 over every capture of a viewpoint, and its validation (PRD §13.3, §13.8).

:func:`run` scores each development frame of ``frames/<viewpoint>/`` against the plate's bands
(:mod:`.extractor_v0`), with the reference pool drawn from the frames a labels file marks
clear day (:mod:`.labels`), and writes under ``out_dir``:

- ``results.jsonl``: one record per frame (key, SHA-256, image time, Sun, frame class, each
  band's energy, reference energy, ratio and class, the obscuration height, the references,
  the PRD §8.6 gates);
- ``ratio_series.csv``: the same per frame in one line, for plotting the ratio series;
- ``summary.json``: counts by frame class and by band class, frames with no usable reference,
  the gates' exclusion and not-checked counts, the inputs' hashes, the parameters, and the
  SHA-256 of the code that ran (:func:`code_identity`);
- ``validation.json``: the extractor against the hand labels (:func:`validate`).

A run never deletes an earlier one; with ``run_log`` it also appends one line per run (its
folder, parameters, code and input hashes, and headline counts) to that file.

**Reference pool.** Clear-day references are a fitted asset (PRD §13.9), so the pool holds
only frames labelled clear day that the committed split lets fits use (``fit`` in
:func:`.leakage.frame_file_verdict`, which is false at or after the viewpoint's fit bound)
and that pass every checked gate.

**Gates** (PRD §8.6, D-037), on every frame: Fresh (against the previous capture's sidecar),
Daylight (ephemeris), Usable frame (capture's ``decode_failed``/``tiny_file`` flags and the
camera fault), and Same view: the plate's base frame registered onto the frame
(:func:`.registration.estimate_shift`, gradient phase correlation at width
:data:`SAME_VIEW_ANALYSIS_WIDTH` over the window :data:`SAME_VIEW_WINDOW`, the lower frame's
trees and buildings, which show in most weather), passed within
:data:`SAME_VIEW_TOLERANCE_PX` and not checked when the best NCC is under
:data:`SAME_VIEW_MIN_NCC` (too little structure, as in fog or falling snow). These three
values are v0 choices, not fitted: TODO(M2). Clear lens and Colour mode are reported as not
checked (TODO(M2)). A daytime frame that fails a checked gate other than the camera fault is
``excluded`` and never scored or used as a reference.

**Validation** (PRD §13.8, "before any number is published"), per band, on the labelled
daytime frames **outside the reference pool**: a pool frame is labelled clear day, so every
band is visible by construction, and it is scored against the other pool frames; counting it
would score the references against one another. The judged sample therefore leaves out every
pool frame; the pool frames' own scores are reported beside it as a sanity check, and so are
the scores with every pool *day* left out. Per band:

- a 3 × 3 table of label against class among frames the extractor scored, and counts of the
  labelled frames it did not score (no reference, in cloud, camera fault, excluded);
- the binary event *visible* three ways. **Primary** (``label_partial_excluded``): frames
  labelled ``partial`` left out, and the extractor's ``partial`` counted as *not visible*, so a
  clearly visible summit the extractor calls partial is a miss. ``both_partial_excluded``: also
  leaving out frames the extractor calls partial, as P1 drops them (§13.4); this flatters the
  extractor when it calls visible frames partial, so it is reported but not judged.
  ``partial_as_not_visible``: nothing left out, partial is not visible on both sides. Each
  gives hits, misses, false alarms, correct negatives, POD, POFD, PSS, precision and F1;
- the bar, F1 ≥ 0.85 (§13.8), judged on the primary form with the fixed v0 thresholds; with
  thresholds given on the command line instead, the bar is never met (they could have been
  tuned on these very labels);
- the camera-in-cloud flag against its label;
- ratio distributions by label, and F1 over other values of τ_hi with τ_lo kept, as
  *diagnostics for M2's fit*.

``publishable`` is false unless every band meets the bar on about 150 judged frames (§13.8),
the judged sample covers the labelled frames (stratification), the labels record §13.8's
re-label pass and second labeller and ~50 labelled renders, the thresholds are the fixed ones,
and the plate marks every band valid; each failing condition is listed.

No frame is fetched; frames held out by the committed split are skipped and counted
(:func:`.leakage.frame_file_verdict`), and so is anything under the quarantine folder.
"""

from __future__ import annotations

import csv
import io
import json
import subprocess
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from snow_globe_common.manifest import load_manifest
from snow_globe_common.timeutil import parse_iso_utc

from . import __version__
from .ephemeris import EphemerisBackend, get_ephemeris, sun_position
from .extractor import classify_ratio
from .extractor_v0 import (
    EXTRACTOR_V0,
    BandEnergies,
    BandResult,
    FrameResult,
    PlateBands,
    ReferenceFrameV0,
    V0Params,
    band_energies,
    camera_fault,
    classify_frame,
    local_day,
)
from .frames import read_sidecar_for_path, sha256_file
from .gates import FrameGates, exclusion_counts, frame_gates
from .labels import LabelSet, load_labels
from .leakage import frame_file_verdict
from .registration import Shift, estimate_shift

__all__ = [
    "DEFAULT_PLATE_VERSION",
    "SAME_VIEW_ANALYSIS_WIDTH",
    "SAME_VIEW_MIN_NCC",
    "SAME_VIEW_TOLERANCE_PX",
    "SAME_VIEW_WINDOW",
    "binary_scores",
    "code_identity",
    "run",
    "thresholds_overridden",
    "validate",
    "with_thresholds",
]

F1_BAR = 0.85
PRD_SAMPLE_SIZE_PER_METRIC = 150
#: The plate the command line scores against unless told otherwise.
DEFAULT_PLATE_VERSION = "v0.1"
#: Same view (PRD §8.6) v0 choices, not fitted (TODO(M2)): the window is the lower frame's
#: static trees and buildings (normalised x0, y0, x1, y1), which show in most weather.
SAME_VIEW_WINDOW = (0.0, 0.55, 1.0, 0.95)
SAME_VIEW_ANALYSIS_WIDTH = 960
SAME_VIEW_TOLERANCE_PX = 4.0
SAME_VIEW_MIN_NCC = 0.3
#: Modules whose SHA-256 a run records, so its numbers can be tied to the code that made them.
CODE_MODULES = (
    "extractor.py",
    "extractor_v0.py",
    "extractor_v0_run.py",
    "labels.py",
    "gates.py",
    "registration.py",
    "leakage.py",
    "heldout.py",
)


def binary_scores(pairs: Sequence[tuple[bool, bool]]) -> dict[str, Any]:
    """Hits, misses, false alarms, correct negatives and scores for (observed, predicted)."""
    h = sum(1 for o, p in pairs if o and p)
    m = sum(1 for o, p in pairs if o and not p)
    f = sum(1 for o, p in pairs if not o and p)
    c = sum(1 for o, p in pairs if not o and not p)
    pod = h / (h + m) if h + m else None
    pofd = f / (f + c) if f + c else None
    precision = h / (h + f) if h + f else None
    f1 = 2 * h / (2 * h + m + f) if (2 * h + m + f) else None
    return {
        "n": len(pairs),
        "hits": h,
        "misses": m,
        "false_alarms": f,
        "correct_negatives": c,
        "pod": None if pod is None else round(pod, 4),
        "pofd": None if pofd is None else round(pofd, 4),
        "pss": None if pod is None or pofd is None else round(pod - pofd, 4),
        "precision": None if precision is None else round(precision, 4),
        "f1": None if f1 is None else round(f1, 4),
    }


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def thresholds_overridden(params: V0Params) -> bool:
    """True when τ differs from v0's fixed values (set before any v0 ratio was computed)."""
    fixed = V0Params()
    return params.tau_hi != fixed.tau_hi or params.tau_lo != fixed.tau_lo


def _band_pairs(
    keys: Sequence[str], results: Mapping[str, FrameResult], labels: LabelSet, band: str
) -> dict[str, Any]:
    table = {
        s: {p: 0 for p in ("visible", "partial", "hidden")}
        for s in ("visible", "partial", "hidden")
    }
    unscored: Counter[str] = Counter()
    primary: list[tuple[bool, bool]] = []
    both: list[tuple[bool, bool]] = []
    lenient: list[tuple[bool, bool]] = []
    ratios: dict[str, list[float]] = {"visible": [], "partial": [], "hidden": []}
    for key in keys:
        res = results[key]
        truth = labels.frames[key].bands[band]
        if res.frame_class != "scored":
            unscored[res.frame_class] += 1
            continue
        pred = res.bands[band].state
        ratio = res.bands[band].ratio
        if pred is None or ratio is None:
            unscored["no_ratio"] += 1
            continue
        table[truth][pred] += 1
        ratios[truth].append(ratio)
        lenient.append((truth == "visible", pred == "visible"))
        if truth != "partial":
            primary.append((truth == "visible", pred == "visible"))
            if pred != "partial":
                both.append((truth == "visible", pred == "visible"))
    return {
        "table": table,
        "unscored": dict(unscored),
        "primary": primary,
        "both": both,
        "lenient": lenient,
        "ratios": ratios,
    }


def validate(
    results: Mapping[str, FrameResult],
    labels: LabelSet,
    order: Sequence[str],
    params: V0Params,
    *,
    pool_keys: Sequence[str] = (),
    local_days: Mapping[str, str] | None = None,
    band_validity: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compare extractor classes with the labels (see the module docstring).

    ``pool_keys`` are the reference pool's frames, left out of the judged sample;
    ``local_days`` (frame key to local day) gives the pool-days-left-out diagnostic;
    ``band_validity`` is the plate's per-band verdict (``PlateBands.band_validity``).
    """
    overridden = thresholds_overridden(params)
    pool = set(pool_keys)
    out: dict[str, Any] = {
        "labels": labels.to_record(),
        "thresholds": {"tau_hi": params.tau_hi, "tau_lo": params.tau_lo},
        "thresholds_overridden": overridden,
        "f1_bar": F1_BAR,
        "prd_sample_size_per_metric": PRD_SAMPLE_SIZE_PER_METRIC,
        "judged_sample": (
            "labelled frames outside the clear-day reference pool: pool frames are labelled "
            "with every band visible and are scored against one another, so they are reported "
            "only as a sanity check (pool_frames_only)"
        ),
        "bands": {},
    }
    labelled_all = [k for k in labels.frames if k in results]
    labelled = [k for k in labelled_all if k not in pool]
    pool_labelled = [k for k in labelled_all if k in pool]
    pool_days = {local_days[k] for k in pool_labelled} if local_days is not None else set()
    off_pool_days = (
        [k for k in labelled if local_days[k] not in pool_days] if local_days is not None else []
    )
    out["labelled_frames_in_run"] = len(labelled_all)
    out["reference_pool_frames_left_out"] = len(pool_labelled)
    out["judged_frames_scored_or_not"] = len(labelled)
    out["labelled_frames_missing_from_run"] = sorted(set(labels.frames) - set(results))
    validity = dict(band_validity or {})
    for band in order:
        judged = _band_pairs(labelled, results, labels, band)
        primary = binary_scores(judged["primary"])
        sweep = {}
        for tau_hi in (0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
            if tau_hi < params.tau_lo:
                continue
            pairs = []
            for key in labelled:
                res = results[key]
                ratio = res.bands[band].ratio if res.frame_class == "scored" else None
                truth = labels.frames[key].bands[band]
                if ratio is None:
                    continue
                pred = classify_ratio(ratio, tau_hi=tau_hi, tau_lo=params.tau_lo)
                if truth != "partial":
                    pairs.append((truth == "visible", pred == "visible"))
            sweep[f"{tau_hi:.1f}"] = binary_scores(pairs)["f1"]
        pool_only = _band_pairs(pool_labelled, results, labels, band)
        band_ok = validity.get(band, {"valid": True})
        item: dict[str, Any] = {
            "label_by_class": judged["table"],
            "labelled_not_scored": judged["unscored"],
            "label_partial_excluded": primary,
            "both_partial_excluded": binary_scores(judged["both"]),
            "partial_as_not_visible": binary_scores(judged["lenient"]),
            "meets_f1_bar": (
                not overridden and primary["f1"] is not None and primary["f1"] >= F1_BAR
            ),
            "judged_on": "label_partial_excluded, reference pool left out",
            "plate_band_valid": bool(band_ok.get("valid", True)),
            "plate_band_not_valid_because": band_ok.get("not_valid_because"),
            "pool_frames_only_sanity_check": binary_scores(pool_only["primary"]),
            "diagnostic_ratio_by_label": {
                s: {
                    "n": len(v),
                    "median": None if not v else round(_median(v) or 0.0, 4),
                    "min": None if not v else round(min(v), 4),
                    "max": None if not v else round(max(v), 4),
                }
                for s, v in judged["ratios"].items()
            },
            "diagnostic_f1_by_tau_hi_not_for_publication": sweep,
        }
        if local_days is not None:
            item["diagnostic_pool_days_left_out"] = binary_scores(
                _band_pairs(off_pool_days, results, labels, band)["primary"]
            )
        out["bands"][band] = item
    scored = [k for k in labelled if results[k].frame_class == "scored"]
    out["scored_sample"] = {
        "judged_frames": len(labelled),
        "scored": len(scored),
        "by_local_day": dict(Counter(labels.frames[k].image_time_utc[:10] for k in scored)),
        "by_sky": dict(Counter(labels.frames[k].sky for k in scored)),
        "unscored_by_summit_label": dict(
            Counter(labels.frames[k].bands[order[0]] for k in labelled if k not in scored)
        ),
        "note": "by_local_day counts UTC dates of image time",
    }
    reasons = []
    for band, item in out["bands"].items():
        n = item["label_partial_excluded"]["n"]
        if n < PRD_SAMPLE_SIZE_PER_METRIC:
            reasons.append(
                f"{band}: {n} judged frames, fewer than §13.8's ~{PRD_SAMPLE_SIZE_PER_METRIC} "
                "per metric"
            )
        f1 = item["label_partial_excluded"]["f1"]
        if f1 is None or f1 < F1_BAR:
            reasons.append(f"{band}: F1 {f1} is below {F1_BAR}")
        if not item["plate_band_valid"]:
            reasons.append(
                f"{band}: the plate marks this band not valid "
                f"({item['plate_band_not_valid_because']})"
            )
    if len(scored) < len(labelled):
        reasons.append(
            f"{len(labelled) - len(scored)} of {len(labelled)} judged frames were not scored "
            "(mostly no clear-day reference near their Sun position on another day), so the "
            "judged sample is not stratified by time of day and weather as §13.8 asks"
        )
    protocol = labels.protocol
    if not protocol.get("relabel_pass"):
        reasons.append("no re-label pass of 50 frames a week later (§13.8)")
    if not protocol.get("second_labeller"):
        reasons.append("one labeller; §13.8 asks for a second labeller if one is available")
    if not protocol.get("labelled_renders"):
        reasons.append("no labelled renders (§13.8 asks for ~50)")
    if overridden:
        reasons.append(
            f"thresholds overridden (tau_hi {params.tau_hi}, tau_lo {params.tau_lo}) instead of "
            "v0's fixed 0.6 and 0.35: they may have been chosen on these labels"
        )
    out["publishable"] = not reasons
    out["not_publishable_because"] = reasons
    cloud: Counter[tuple[bool, bool]] = Counter()
    for key in labelled:
        res = results[key]
        if res.frame_class in ("night", "camera_fault", "excluded", "no_reference"):
            continue
        cloud[(labels.frames[key].camera_in_cloud, res.frame_class == "in_cloud")] += 1
    out["camera_in_cloud"] = {
        "labelled_and_flagged": cloud[(True, True)],
        "labelled_not_flagged": cloud[(True, False)],
        "flagged_not_labelled": cloud[(False, True)],
        "neither": cloud[(False, False)],
    }
    return out


def code_identity() -> dict[str, Any]:
    """SHA-256 of the extractor's modules and, where git answers, the commit and dirty files."""
    here = Path(__file__).parent
    files = {name: sha256_file(here / name) for name in CODE_MODULES}
    record: dict[str, Any] = {"package_version": __version__, "files_sha256": files}
    try:
        head = subprocess.run(
            ["git", "-C", str(here), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(here), "status", "--porcelain", "--", *CODE_MODULES],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        record["git"] = None
    else:
        record["git"] = {
            "head": head,
            "uncommitted": sorted(line[3:] for line in status if line.strip()),
        }
    return record


def _frames(root: Path, viewpoint_id: str) -> list[Path]:
    return sorted((root / "frames" / viewpoint_id).glob("*/*.jpg"))


def _excluded(
    e: BandEnergies, order: Sequence[str], alt: float, az: float, why: str
) -> FrameResult:
    blank = {n: BandResult(e.bands[n], None, None, None) for n in order}
    return FrameResult("excluded", why, alt, az, blank, None, None, None)


def _same_view_shift(base: Path | None, frame: Path) -> Shift | None:
    if base is None:
        return None
    return estimate_shift(
        base,
        frame,
        window=SAME_VIEW_WINDOW,
        analysis_width=SAME_VIEW_ANALYSIS_WIDTH,
        scales=(1.0,),
    )


def run(
    *,
    data_root: Path,
    viewpoint_id: str,
    plate_dir: Path,
    labels_path: Path | None,
    out_dir: Path,
    params: V0Params | None = None,
    ephemeris: EphemerisBackend | None = None,
    run_log: Path | None = None,
) -> dict[str, Any]:
    """Score every development frame and validate against the labels (module docstring)."""
    chosen = params or V0Params()
    backend = ephemeris or get_ephemeris()
    viewpoint = load_manifest(viewpoint_id)
    plate = PlateBands.load(plate_dir, roots=(data_root,))
    if plate.viewpoint_id != viewpoint_id:
        raise ValueError(f"plate {plate.plate_id} is not for {viewpoint_id}")
    labels = load_labels(labels_path, roots=(data_root,))
    if labels.viewpoint_id != viewpoint_id:
        raise ValueError(f"labels are for {labels.viewpoint_id}, not {viewpoint_id}")
    cam = viewpoint.camera
    energies: dict[str, BandEnergies] = {}
    meta: dict[str, dict[str, Any]] = {}
    skipped: Counter[str] = Counter()
    previous: dict[str, Any] | None = None
    for path in _frames(data_root, viewpoint_id):
        verdict = frame_file_verdict(path, data_root=data_root)
        if not verdict["development"]:
            skipped["not development: " + "; ".join(verdict["reasons"])] += 1
            continue
        side = read_sidecar_for_path(path, roots=(data_root,)) or {}
        key = f"frames/{viewpoint_id}/{path.parent.name}/{path.name}"
        image_time = (
            parse_iso_utc(side["image_time_utc"])
            if side.get("image_time_utc")
            else parse_iso_utc(verdict["image_time_utc"])
        )
        sun = sun_position(image_time, cam.lat, cam.lon, cam.elevation_m, ephemeris=backend)
        sha = sha256_file(path)
        if key in labels.frames and labels.frames[key].sha256 not in (sha, side.get("sha256")):
            raise ValueError(f"{key}: the labelled SHA-256 is not the file's")
        try:
            energies[key] = band_energies(path, plate, chosen)
        except (OSError, ValueError) as exc:
            skipped[f"unreadable: {type(exc).__name__}"] += 1
            previous = side or previous
            continue
        daytime = sun.altitude_deg >= chosen.min_sun_alt_deg
        shift = _same_view_shift(plate.base_path, path) if daytime else None
        gates = frame_gates(
            viewpoint=viewpoint,
            image_time=image_time,
            sidecar=side or None,
            previous_sidecar=previous,
            camera_fault=camera_fault(energies[key], chosen),
            min_sun_alt_deg=chosen.min_sun_alt_deg,
            ephemeris=backend,
            view_shift=shift,
            same_view_tolerance_px=SAME_VIEW_TOLERANCE_PX,
            same_view_min_ncc=SAME_VIEW_MIN_NCC,
        )
        previous = side or previous
        meta[key] = {
            "sha256": sha,
            "image_time": image_time,
            "sun_alt": sun.altitude_deg,
            "sun_az": sun.azimuth_deg,
            "local_day": local_day(image_time, viewpoint.time_zone),
            "fit": bool(verdict["fit"]),
            "fit_reasons": list(verdict["reasons"]),
            "gates": gates,
            "shift": shift,
        }

    def gate_exclusion(key: str) -> str | None:
        gates: FrameGates = meta[key]["gates"]
        if meta[key]["sun_alt"] < chosen.min_sun_alt_deg:
            return None  # night: classify_frame names it
        if camera_fault(energies[key], chosen) is not None:
            return None  # camera fault: classify_frame names it (D-037)
        failed = [f"{r.gate}: {r.reason}" for r in gates.results if r.passed is False]
        return "; ".join(failed) or None

    excluded = {key: why for key in energies if (why := gate_exclusion(key)) is not None}
    clear_day = [k for k in labels.clear_day_keys if k in energies]
    refused_for_fits = sorted(k for k in clear_day if not meta[k]["fit"])
    pool = [
        ReferenceFrameV0(
            k,
            meta[k]["sha256"],
            meta[k]["image_time"],
            meta[k]["sun_alt"],
            meta[k]["sun_az"],
            meta[k]["local_day"],
            energies[k],
        )
        for k in clear_day
        if meta[k]["fit"]
        and k not in excluded
        and meta[k]["sun_alt"] >= chosen.min_sun_alt_deg
        and camera_fault(energies[k], chosen) is None
    ]
    results: dict[str, FrameResult] = {}
    for key, e in energies.items():
        m = meta[key]
        if key in excluded:
            results[key] = _excluded(e, plate.order, m["sun_alt"], m["sun_az"], excluded[key])
            continue
        results[key] = classify_frame(
            e,
            sun_alt_deg=m["sun_alt"],
            sun_az_deg=m["sun_az"],
            order=plate.order,
            pool=pool,
            params=chosen,
            local_day=m["local_day"],
            exclude_keys=(key,),
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    pool_keys = {r.key for r in pool}
    lines = []
    for key in sorted(results):
        shift = meta[key]["shift"]
        rec = {
            "key": key,
            "sha256": meta[key]["sha256"],
            "image_time_utc": meta[key]["image_time"]
            .astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "local_day": meta[key]["local_day"],
            "label": None if key not in labels.frames else labels.frames[key].bands,
            "in_reference_pool": key in pool_keys,
            **results[key].to_dict(),
            "energies": energies[key].to_dict(),
            "gates": meta[key]["gates"].to_dict(),
            "same_view_shift": None if shift is None else shift.to_dict(),
        }
        lines.append(json.dumps(rec))
    (out_dir / "results.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "image_time_utc",
            "sun_alt_deg",
            "frame_class",
            "in_reference_pool",
            *(f"{b}_ratio" for b in plate.order),
            *(f"{b}_class" for b in plate.order),
            "obscuration_height",
        ]
    )
    for key in sorted(results, key=lambda k: meta[k]["image_time"]):
        r = results[key]
        writer.writerow(
            [
                meta[key]["image_time"].astimezone(UTC).isoformat().replace("+00:00", "Z"),
                round(r.sun_alt_deg, 2),
                r.frame_class,
                int(key in pool_keys),
                *(
                    "" if r.bands[b].ratio is None else round(r.bands[b].ratio, 4)
                    for b in plate.order
                ),
                *("" if r.band_class(b) is None else r.band_class(b) for b in plate.order),
                r.obscuration_height or "",
            ]
        )
    (out_dir / "ratio_series.csv").write_text(buffer.getvalue(), encoding="utf-8")
    frame_classes = Counter(r.frame_class for r in results.values())
    band_classes = {
        b: dict(Counter(r.bands[b].state for r in results.values() if r.frame_class == "scored"))
        for b in plate.order
    }
    daytime = [r for r in results.values() if r.frame_class != "night"]
    daytime_gates = [meta[k]["gates"] for k, r in results.items() if r.frame_class != "night"]
    overridden = thresholds_overridden(chosen)
    summary: dict[str, Any] = {
        "kind": "snow-globe.extractor-v0-run",
        "extractor_version": EXTRACTOR_V0,
        "viewpoint_id": viewpoint_id,
        "run_at_utc": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "out_dir": str(out_dir),
        "code": code_identity(),
        "plate": plate.to_record(),
        "labels": labels.to_record(),
        "params": chosen.to_dict(),
        "thresholds_overridden": overridden,
        "same_view": {
            "window": list(SAME_VIEW_WINDOW),
            "analysis_width": SAME_VIEW_ANALYSIS_WIDTH,
            "tolerance_px": SAME_VIEW_TOLERANCE_PX,
            "min_ncc": SAME_VIEW_MIN_NCC,
            "against": plate.to_record()["base"],
            "note": "v0 choices, not fitted (TODO(M2)); daytime frames only",
        },
        "ephemeris": {"backend": backend.name, "version": backend.version},
        "frames_scored_or_classed": len(results),
        "frames_skipped": dict(skipped),
        "frame_classes": dict(frame_classes),
        "daytime_frames": len(daytime),
        "daytime_without_usable_reference": frame_classes.get("no_reference", 0),
        "gates_all_frames": exclusion_counts(meta[k]["gates"] for k in results),
        "gates_daytime_frames": exclusion_counts(daytime_gates),
        "clear_day_labelled": len(clear_day),
        "clear_day_refused_for_fits": refused_for_fits,
        "reference_pool": [
            {
                "key": r.key,
                "sun_alt_deg": round(r.sun_alt_deg, 2),
                "sun_az_deg": round(r.sun_az_deg, 2),
                "local_day": r.local_day,
            }
            for r in pool
        ],
        "band_classes_of_scored_frames": band_classes,
        "obscuration_height_of_scored_frames": dict(
            Counter(
                r.obscuration_height or "none"
                for r in results.values()
                if r.frame_class == "scored"
            )
        ),
        "counts_toward_metrics": False,
    }
    validation = validate(
        results,
        labels,
        plate.order,
        chosen,
        pool_keys=sorted(pool_keys),
        local_days={k: meta[k]["local_day"] for k in results},
        band_validity=plate.band_validity,
    )
    tau_note = (
        f"tau_hi {chosen.tau_hi} and tau_lo {chosen.tau_lo} were set on the command line, "
        "not v0's fixed values"
        if overridden
        else f"tau_hi {chosen.tau_hi} and tau_lo {chosen.tau_lo} are v-1's placeholders, "
        "kept fixed and not fitted on development data (TODO(M2), PRD §13.4)"
    )
    summary["not_counted_because"] = [
        *(f"validation: {r}" for r in validation["not_publishable_because"]),
        tau_note,
    ]
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (out_dir / "validation.json").write_text(
        json.dumps(validation, indent=2) + "\n", encoding="utf-8"
    )
    if run_log is not None:
        entry = {
            "run_at_utc": summary["run_at_utc"],
            "out_dir": str(out_dir),
            "code": summary["code"],
            "plate_json_sha256": plate.plate_json_sha256,
            "labels_sha256": labels.sha256,
            "params": summary["params"],
            "thresholds_overridden": overridden,
            "frame_classes": summary["frame_classes"],
            "judged_f1": {
                b: v["label_partial_excluded"]["f1"] for b, v in validation["bands"].items()
            },
            "judged_n": {
                b: v["label_partial_excluded"]["n"] for b, v in validation["bands"].items()
            },
        }
        run_log.parent.mkdir(parents=True, exist_ok=True)
        with run_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    return {"summary": summary, "validation": validation}


def with_thresholds(params: V0Params, *, tau_hi: float | None, tau_lo: float | None) -> V0Params:
    """``params`` with any given thresholds replaced."""
    return replace(
        params,
        tau_hi=params.tau_hi if tau_hi is None else tau_hi,
        tau_lo=params.tau_lo if tau_lo is None else tau_lo,
    )
