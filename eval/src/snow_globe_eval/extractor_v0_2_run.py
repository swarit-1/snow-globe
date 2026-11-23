"""``snow-globe-eval extractor-v0.2``: extractor v0.2's leave-one-day-out check (PRD §13.8).

The run, under ``<data dir>/scores/extractor-v0.2/<viewpoint>/<run id>/``:

1. **Refuses to start without the pre-registration** (``preregistration.md``, by default
   ``<data dir>/scores/extractor-v0.2/preregistration.md``), whose SHA-256 and modification
   time it records: the method and the scoring rule are fixed before any score. **Refuses a
   finished run's folder** (one holding ``run.json``, written last): a scored run is never
   written over, so a re-run, to check a run or after any change, takes a new run id.
2. **Features** of every labelled frame (``features.jsonl``, appended frame by frame, so a run
   stopped by sleep resumes where it stopped): the committed split's guard first
   (:func:`.leakage.frame_file_verdict`, metadata only), then the Sun (oculus-core at the
   manifest camera), v0's band energies and v0.2's illumination from one decode
   (:func:`.extractor_v0_2.frame_features`), and the PRD §8.6 gates as extractor v0's run
   applies them (Fresh against the previous development capture, Daylight, Usable frame,
   Same view against the plate's base with v0's window, tolerance and NCC floor). A cached
   line is reused only when its stamp (:func:`feature_inputs`: the parameters, the code, the
   plate and sky mask, the manifest camera and time zone, the ephemeris and the split) and
   its frame's inputs (the frame's SHA-256, hashed again, its sidecar, the previous
   capture's sidecar and the fit verdict) are unchanged. A computed frame is used as its
   cache line reads back, so a resumed run and an uninterrupted one use the same numbers.
3. **Leave-one-day-out** for every method of :data:`.extractor_v0_2_lodo.METHODS`
   (``folds.json``, ``predictions.jsonl``), scored against the merged labels
   (:mod:`.extractor_v0_2_scores`; ``scores.json``), the paired bootstrap of v0.2 against each
   other method on the summit, each method's in-cloud flags against the in-cloud labels, and
   v0.2's in-sample fit on every day for use from now on.
4. ``report.md``, the tables in short, and ``run.json``, the provenance: code SHA-256s and git
   state, inputs' SHA-256s, the feature stamp, parameters, the camera and the ephemeris.

Labels v0.2 judge summit, upper and mid. The near band is computed but not judged: the plate
marks it not valid and the labellers did not label it.

``--merge-labels DIR --labels OUT`` instead merges a labels folder into a new merged labels
file (:func:`.extractor_v0_2_labels.build_merged_file`; never an existing file, so never the
committed one) and prints the agreement.

No frame is fetched; nothing under the quarantine folder is read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import snow_globe_common.images as common_images
import snow_globe_common.manifest as common_manifest
import snow_globe_common.timeutil as common_timeutil
from numpy.typing import NDArray
from snow_globe_common.manifest import Viewpoint, load_manifest
from snow_globe_common.timeutil import parse_iso_utc

from . import __version__
from .ephemeris import EphemerisBackend, get_ephemeris, sun_position
from .extractor_v0 import PlateBands, camera_fault, local_day
from .extractor_v0_2 import (
    IlluminationRegions,
    V02Params,
    frame_features,
    load_sky_mask,
)
from .extractor_v0_2_labels import (
    MergedLabels,
    build_merged_file,
    load_merged_labels,
)
from .extractor_v0_2_lodo import METHODS, FrameFeatures, deployed_fit, run_lodo
from .extractor_v0_2_scores import camera_in_cloud, paired_bootstrap_delta_f1, score_method
from .extractor_v0_run import (
    SAME_VIEW_ANALYSIS_WIDTH,
    SAME_VIEW_MIN_NCC,
    SAME_VIEW_TOLERANCE_PX,
    SAME_VIEW_WINDOW,
)
from .frames import read_sidecar_for_path, sha256_file
from .gates import frame_gates
from .heldout import LeakageError, committed_split, path_quarantine_reason
from .labels import LABELS_RAINIER, load_labels
from .leakage import frame_file_verdict
from .registration import estimate_shift

__all__ = [
    "CODE_MODULES",
    "FEATURE_MODULES",
    "code_identity",
    "extract_features",
    "feature_inputs",
    "register",
    "run",
    "stratification",
]

F1_BAR = 0.85
#: Descriptive only, for the stratification note: the pre-registration's §3 found D 0.50-0.69
#: under direct sun and 0.71-0.88 under overcast, fog and snow. It never classes a frame.
OVERCAST_D_MIN = 0.70
#: The file a finished run writes last; a folder holding it is never written over.
FINISHED_MARKER = "run.json"
#: The eval modules a cached feature line is computed by (their SHA-256s enter its stamp).
FEATURE_MODULES = (
    "daylight.py",
    "ephemeris.py",
    "extractor.py",
    "extractor_v0.py",
    "extractor_v0_run.py",
    "extractor_v0_2.py",
    "extractor_v0_2_lodo.py",
    "extractor_v0_2_run.py",
    "frames.py",
    "gates.py",
    "heldout.py",
    "leakage.py",
    "registration.py",
)
#: Modules whose SHA-256 a run records (the feature modules and the rest of the check).
CODE_MODULES = tuple(
    sorted(
        {
            *FEATURE_MODULES,
            "extractor_v0_2_labels.py",
            "extractor_v0_2_scores.py",
            "labels.py",
            "scores.py",
        }
    )
)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_sha256(doc: Mapping[str, Any] | None) -> str | None:
    """SHA-256 of a JSON object in canonical form (sorted keys); None for none or empty."""
    if not doc:
        return None
    return _sha256_text(json.dumps(doc, sort_keys=True, separators=(",", ":")))


def code_identity(modules: Sequence[str] = CODE_MODULES) -> dict[str, Any]:
    """SHA-256 of the modules and, where git answers, the commit and uncommitted files."""
    here = Path(__file__).parent
    files = {name: sha256_file(here / name) for name in modules}
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
            ["git", "-C", str(here), "status", "--porcelain", "--", *modules],
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


def feature_inputs(
    *,
    params: V02Params,
    plate: PlateBands,
    sky_full: NDArray[np.bool_],
    viewpoint: Viewpoint,
    ephemeris: EphemerisBackend,
) -> dict[str, Any]:
    """Every run-wide input a cached feature line depends on; its stamp is their SHA-256.

    The parameters; the code that computes a line (eval's :data:`FEATURE_MODULES` and the
    snow-globe-common modules they decode and parse with); the plate, by ``plate.json``'s
    SHA-256 (:meth:`.extractor_v0.PlateBands.load` and :func:`.extractor_v0_2.load_sky_mask`
    check every mask and the base frame against it), and the sky mask passed in; the
    manifest camera and time zone (the Sun, the Daylight gate, the local day); the ephemeris;
    and the committed split (whether a frame is development and may enter a fit, PRD §13.9).
    Per-frame inputs are checked line by line (:func:`extract_features`).
    """
    here = Path(__file__).parent
    code = {name: sha256_file(here / name) for name in FEATURE_MODULES}
    for module in (common_images, common_manifest, common_timeutil):
        path = Path(str(module.__file__))
        code[f"snow_globe_common/{path.name}"] = sha256_file(path)
    packed = np.packbits(np.ascontiguousarray(sky_full, dtype=bool))
    split = committed_split()
    cam = viewpoint.camera
    return {
        "params": params.to_dict(),
        "code_sha256": code,
        "plate_json_sha256": plate.plate_json_sha256,
        "sky_mask": {
            "shape": list(sky_full.shape),
            "packed_sha256": hashlib.sha256(packed.tobytes()).hexdigest(),
        },
        "camera": {"lat": cam.lat, "lon": cam.lon, "elevation_m": cam.elevation_m},
        "time_zone": viewpoint.time_zone,
        "ephemeris": {"backend": ephemeris.name, "version": ephemeris.version},
        "split": {"version": split.version, "sha256": split.sha256},
    }


def _read_cache(path: Path) -> dict[str, tuple[dict[str, Any], str]]:
    """The cache's lines by frame key (the last line of a key wins), parsed and as written."""
    out: dict[str, tuple[dict[str, Any], str]] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            doc = json.loads(line)
            key = str(doc["feature"]["key"])
        except (ValueError, KeyError, TypeError):
            continue  # a line cut short by a stop: that frame is computed again
        out[key] = (doc, line)
    return out


def _isoformat_ms(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def extract_features(
    *,
    data_root: Path,
    viewpoint: Viewpoint,
    plate: PlateBands,
    sky_full: Any,
    labels: MergedLabels,
    params: V02Params,
    cache_path: Path,
    ephemeris: EphemerisBackend,
) -> tuple[dict[str, FrameFeatures], dict[str, Any]]:
    """Features of every labelled frame (module docstring, step 2), resumable.

    Returns the features and a record of the feature phase: frames computed and reused, the
    stamp and its inputs, lines superseded, and the SHA-256 of ``features.jsonl``, which at
    the end holds exactly the lines used, one per labelled frame in key order.
    """
    inputs = feature_inputs(
        params=params, plate=plate, sky_full=sky_full, viewpoint=viewpoint, ephemeris=ephemeris
    )
    stamp = _sha256_text(json.dumps(inputs, sort_keys=True))
    cached = _read_cache(cache_path)
    regions: dict[tuple[int, int], IlluminationRegions] = {}
    frames = sorted((data_root / "frames" / viewpoint.id).glob("*/*.jpg"))
    out: dict[str, FrameFeatures] = {}
    used: dict[str, str] = {}
    counts: dict[str, Any] = {"computed": 0, "reused": 0, "frames_listed": len(frames)}
    previous: dict[str, Any] | None = None
    cam = viewpoint.camera
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if cache_path.is_file():
        with cache_path.open("rb+") as handle:  # a line cut short by a stop: end it first
            handle.seek(0, os.SEEK_END)
            if handle.tell():
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    handle.write(b"\n")
    for path in frames:
        key = f"frames/{viewpoint.id}/{path.parent.name}/{path.name}"
        verdict = frame_file_verdict(path, data_root=data_root)
        if not verdict["development"]:
            if key in labels.frames:
                raise LeakageError(f"{key} is labelled but not development: {verdict['reasons']}")
            continue
        side = read_sidecar_for_path(path, roots=(data_root,)) or {}
        prior, previous = previous, (side or previous)
        if key not in labels.frames:
            continue
        label = labels.frames[key]
        sha = sha256_file(path)
        if sha != label.sha256:
            raise ValueError(f"{key}: the labelled SHA-256 is not the file's")
        frame_inputs = {
            "frame_sha256": sha,
            "sidecar_sha256": _canonical_sha256(side),
            "previous_sidecar_sha256": _canonical_sha256(prior),
            "fit_allowed": bool(verdict["fit"]),
        }
        hit = cached.get(key)
        if (
            hit is not None
            and hit[0].get("stamp") == stamp
            and hit[0].get("inputs") == frame_inputs
        ):
            out[key] = FrameFeatures.from_dict(hit[0]["feature"])
            used[key] = hit[1]
            counts["reused"] += 1
            continue
        image_time = parse_iso_utc(side.get("image_time_utc") or verdict["image_time_utc"])
        sun = sun_position(image_time, cam.lat, cam.lon, cam.elevation_m, ephemeris=ephemeris)
        energies, light = frame_features(path, plate, regions, sky_full, params)
        daytime = sun.altitude_deg >= params.min_sun_alt_deg
        shift = None
        if daytime and plate.base_path is not None:
            shift = estimate_shift(
                plate.base_path,
                path,
                window=SAME_VIEW_WINDOW,
                analysis_width=SAME_VIEW_ANALYSIS_WIDTH,
                scales=(1.0,),
            )
        fault = camera_fault(energies, params.energy_params())
        gates = frame_gates(
            viewpoint=viewpoint,
            image_time=image_time,
            sidecar=side or None,
            previous_sidecar=prior,
            camera_fault=fault,
            min_sun_alt_deg=params.min_sun_alt_deg,
            ephemeris=ephemeris,
            view_shift=shift,
            same_view_tolerance_px=SAME_VIEW_TOLERANCE_PX,
            same_view_min_ncc=SAME_VIEW_MIN_NCC,
        )
        failure = None
        if daytime and fault is None:
            failed = [f"{r.gate}: {r.reason}" for r in gates.results if r.passed is False]
            failure = "; ".join(failed) or None
        feat = FrameFeatures(
            key=key,
            sha256=sha,
            image_time_utc=_isoformat_ms(image_time),
            local_day=local_day(image_time, viewpoint.time_zone),
            sun_alt_deg=sun.altitude_deg,
            sun_az_deg=sun.azimuth_deg,
            energies=energies,
            light=light,
            gate_failure=failure,
            fit_allowed=bool(verdict["fit"]),
            gates=gates.to_dict(),
            same_view_shift=None if shift is None else shift.to_dict(),
        )
        line = json.dumps({"stamp": stamp, "inputs": frame_inputs, "feature": feat.to_dict()})
        with cache_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        # used as the line reads back (rounded light values), exactly as a resumed run reads it
        out[key] = FrameFeatures.from_dict(json.loads(line)["feature"])
        used[key] = line
        counts["computed"] += 1
    missing = sorted(set(labels.frames) - set(out))
    if missing:
        raise FileNotFoundError(f"labelled frames not found as development frames: {missing[:5]}")
    text = "".join(used[k] + "\n" for k in sorted(used))
    on_disk = cache_path.read_text(encoding="utf-8") if cache_path.is_file() else ""
    counts["superseded_lines"] = max(0, len(on_disk.splitlines()) - len(used))
    if on_disk != text:  # stale or cut-short lines from an earlier, unfinished attempt
        _write(cache_path, text)
    counts["jsonl_sha256"] = _sha256_text(text)
    counts["stamp"] = {"sha256": stamp, "inputs": inputs}
    return out, counts


def _write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def stratification(
    features: Mapping[str, FrameFeatures], labels: MergedLabels, band: str
) -> dict[str, Any]:
    """Where a band's visible labels fall, by local day and light (PRD §13.8's stratification).

    PRD §13.8 asks for labels "stratified by time of day and weather". This says how far the
    labelled sample is from that: its local days, the visible labels per day, and those in
    overcast light (D at or above :data:`OVERCAST_D_MIN`, a descriptive cut that classes
    nothing) with their span of image times.
    """
    keys = sorted(k for k in labels.frames if k in features)
    visible = [features[k] for k in keys if labels.frames[k].bands.get(band) == "visible"]
    overcast = [
        f for f in visible if f.light.d_index is not None and f.light.d_index >= OVERCAST_D_MIN
    ]
    times = sorted(f.image_time_utc for f in overcast)
    return {
        "band": band,
        "local_days": sorted({features[k].local_day for k in keys}),
        "labelled_frames": len(keys),
        "visible": len(visible),
        "visible_by_local_day": dict(sorted(Counter(f.local_day for f in visible).items())),
        "overcast_d_min": OVERCAST_D_MIN,
        "visible_in_overcast_light": len(overcast),
        "visible_in_overcast_light_by_local_day": dict(
            sorted(Counter(f.local_day for f in overcast).items())
        ),
        "visible_in_overcast_light_span_utc": [times[0], times[-1]] if times else None,
    }


def _stratification_reason(strat: Mapping[str, Any]) -> str:
    days = strat["local_days"]
    by_day = strat["visible_by_local_day"]
    top_two = sum(sorted(by_day.values(), reverse=True)[:2])
    over = strat["visible_in_overcast_light_by_local_day"]
    span = strat["visible_in_overcast_light_span_utc"]
    where = ", ".join(f"{d} {n}" for d, n in over.items()) or "none"
    when = f", {span[0]} to {span[1]}" if span else ""
    return (
        f"not stratified by time of day and weather (§13.8): {strat['labelled_frames']} "
        f"captures of {len(days)} local days ({days[0]} to {days[-1]}); the {strat['visible']} "
        f"visible {strat['band']} labels fall on {len(by_day)} days "
        f"({', '.join(f'{d} {n}' for d, n in by_day.items())}), {top_two} of them on two; "
        f"{strat['visible_in_overcast_light']} are in overcast light "
        f"(D ≥ {strat['overcast_d_min']}: {where}{when})"
    )


def _fmt(value: Any) -> str:
    return "–" if value is None else (f"{value:.3f}" if isinstance(value, float) else str(value))


def _report(doc: Mapping[str, Any]) -> str:
    """The run's tables in short (Markdown)."""
    lines = [
        f"# Extractor v0.2, leave-one-day-out: {doc['viewpoint_id']}",
        "",
        f"Run `{doc['run_id']}` at {doc['finished_at_utc']}. Pre-registration SHA-256 "
        f"`{doc['preregistration']['sha256'][:16]}…`, "
        f"written {doc['preregistration']['mtime_utc']}.",
        f"Labels `{doc['labels']['sha256'][:16]}…` ({doc['labels']['frames']} frames); "
        f"days {', '.join(doc['days'])}.",
        "",
        "## Pooled primary F1 per band (bar 0.85)",
        "",
        "| Method | Band | n | H | M | F | C | F1 | precision | recall | 95 % day-block interval |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, item in doc["methods"].items():
        for band, b in item["scores"]["bands"].items():
            p = b["primary"]["pooled"]
            bs = b["bootstrap_primary"]
            lines.append(
                f"| {name} | {band} | {p['n']} | {p['hits']} | {p['misses']} | "
                f"{p['false_alarms']} | {p['correct_negatives']} | {_fmt(p['f1'])} | "
                f"{_fmt(p['precision'])} | {_fmt(p['recall'])} | "
                f"{_fmt(bs['interval'][0])}–{_fmt(bs['interval'][1])} |"
            )
    lines += ["", "## Per day (primary form, summit)", ""]
    lines.append("| Method | Day | n | H | M | F | C | F1 | precision | recall |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for name, item in doc["methods"].items():
        per_day = item["scores"]["bands"]["summit"]["primary"]["per_day"]
        for day, p in per_day.items():
            lines.append(
                f"| {name} | {day} | {p['n']} | {p['hits']} | {p['misses']} | "
                f"{p['false_alarms']} | {p['correct_negatives']} | {_fmt(p['f1'])} | "
                f"{_fmt(p['precision'])} | {_fmt(p['recall'])} |"
            )
    lines += ["", "## Summit F1 under the other forms", ""]
    forms = list(next(iter(doc["methods"].values()))["scores"]["bands"]["summit"])
    forms = [f for f in forms if f not in ("bootstrap_primary", "confusion")]
    lines.append("| Method | " + " | ".join(forms) + " |")
    lines.append("|---|" + "---|" * len(forms))
    for name, item in doc["methods"].items():
        s = item["scores"]["bands"]["summit"]
        lines.append(f"| {name} | " + " | ".join(_fmt(s[f]["pooled"]["f1"]) for f in forms) + " |")
    band0 = next(iter(doc["methods"].values()))["camera_in_cloud"]["band"]
    lines += [
        "",
        f"## Camera in cloud: frame class in_cloud against the merged {band0} label in_cloud",
        "",
        "PRD §13.4 keeps frames from inside cloud out of P1's primary. Tested: the frame had "
        "the in-cloud test's references (not night, camera fault, excluded or without one).",
        "",
        "| Method | labelled in_cloud | tested | flagged | flagged, not labelled "
        "| labellers split, one says in_cloud |",
        "|---|---|---|---|---|---|",
    ]
    for name, item in doc["methods"].items():
        c = item["camera_in_cloud"]
        tested = c["labelled_and_flagged"] + c["labelled_not_flagged"]
        lines.append(
            f"| {name} | {c['labelled_in_cloud']} | {tested} | {c['labelled_and_flagged']} | "
            f"{c['flagged_not_labelled']} | {c['labellers_split_one_in_cloud']} |"
        )
    lines += ["", "## Paired bootstrap, summit, primary: F1(v0.2) − F1(other)", ""]
    for other, item in doc["comparisons"].items():
        lines.append(
            f"- vs {other}: ΔF1 {_fmt(item['delta_f1'])}, 95 % interval "
            f"{_fmt(item['interval'][0])}–{_fmt(item['interval'][1])}"
        )
    lines += ["", "## Bar", "", doc["bar"]["statement"], ""]
    return "\n".join(lines) + "\n"


def run(
    *,
    data_root: Path,
    viewpoint_id: str,
    plate_dir: Path,
    labels_path: Path | None,
    v0_labels_path: Path | None,
    out_dir: Path,
    preregistration: Path,
    run_id: str,
    n_boot: int = 10_000,
    seed: int = 20261123,
    ephemeris: EphemerisBackend | None = None,
) -> dict[str, Any]:
    """The whole check (module docstring). Returns what ``run.json`` holds.

    ``ValueError`` when ``out_dir`` holds a finished run (its ``run.json``): runs are never
    written over. A folder with features but no ``run.json`` is an unfinished run, resumed.
    """
    started = time.monotonic()
    finished = out_dir / FINISHED_MARKER
    if finished.exists():
        raise ValueError(
            f"{out_dir} holds a finished run ({FINISHED_MARKER}); runs are never written over: "
            "use a new run id"
        )
    prereg = preregistration.expanduser()
    reason = path_quarantine_reason(prereg, roots=(data_root,))
    if reason is not None:
        raise LeakageError(f"pre-registration {prereg}: {reason}")
    if not prereg.is_file():
        raise FileNotFoundError(f"no pre-registration at {prereg}: write it before scoring")
    prereg_record = {
        "path": str(prereg),
        "sha256": sha256_file(prereg),
        "mtime_utc": datetime.fromtimestamp(prereg.stat().st_mtime, UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
    backend = ephemeris or get_ephemeris()
    viewpoint = load_manifest(viewpoint_id)
    plate = PlateBands.load(plate_dir, roots=(data_root,))
    if plate.viewpoint_id != viewpoint_id:
        raise ValueError(f"plate {plate.plate_id} is not for {viewpoint_id}")
    sky_full = load_sky_mask(plate_dir, roots=(data_root,))
    labels = load_merged_labels(labels_path, roots=(data_root,))
    if labels.viewpoint_id != viewpoint_id:
        raise ValueError(f"labels are for {labels.viewpoint_id}, not {viewpoint_id}")
    v0_labels = load_labels(v0_labels_path or LABELS_RAINIER, roots=(data_root,))
    params = V02Params()
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    features, feature_counts = extract_features(
        data_root=data_root,
        viewpoint=viewpoint,
        plate=plate,
        sky_full=sky_full,
        labels=labels,
        params=params,
        cache_path=out_dir / "features.jsonl",
        ephemeris=backend,
    )
    feature_s = time.monotonic() - t0
    order = plate.order
    judged = labels.bands
    methods: dict[str, Any] = {}
    all_predictions: dict[str, dict[str, Any]] = {}
    t1 = time.monotonic()
    for method in METHODS:
        fold_fits, predictions = run_lodo(
            method,
            features,
            labels,
            order,
            judged=judged,
            clear_day_keys=v0_labels.clear_day_keys,
        )
        scores = score_method(predictions, labels, judged, n_boot=n_boot, seed=seed)
        methods[method.name] = {
            "family": method.family,
            "fit_tau": method.fit_tau,
            "params": (method.v02 or method.v0).to_dict(),  # type: ignore[union-attr]
            "folds": [f.to_dict() for f in fold_fits],
            "camera_in_cloud": camera_in_cloud(predictions, labels, judged[0]),
            "scores": scores,
        }
        for key, pred in predictions.items():
            entry = all_predictions.setdefault(key, {})
            entry[method.name] = {
                "classes": pred.classes,
                "ratios": {b: None if r is None else round(r, 4) for b, r in pred.ratios.items()},
                "record": pred.record,
            }
    lodo_s = time.monotonic() - t1
    days = methods["v0.2"]["scores"]["days"]
    comparisons = {}
    for other in ("v0", "v0-fitted-tau", "v0.2-A"):
        comparisons[other] = paired_bootstrap_delta_f1(
            methods["v0.2"]["scores"]["bands"]["summit"]["primary"]["tables"],
            methods[other]["scores"]["bands"]["summit"]["primary"]["tables"],
            days=days,
            n_boot=n_boot,
            seed=seed,
        )
    deployed = deployed_fit(METHODS[0], features, labels, order, judged=judged)
    summit = methods["v0.2"]["scores"]["bands"]["summit"]
    f1 = summit["primary"]["pooled"]["f1"]
    met = f1 is not None and f1 >= F1_BAR
    interval = summit["bootstrap_primary"]["interval"]
    statement = (
        f"Pooled leave-one-day-out summit F1 (primary form) of v0.2 is {f1}, "
        f"{'at or above' if met else 'below'} PRD §13.8's {F1_BAR}; 95 % day-block interval "
        f"{interval[0]}–{interval[1]} over {len(days)} days."
    )
    not_publishable = []
    for band, item in summit_and_others(methods["v0.2"]["scores"]["bands"]):
        n = item["primary"]["pooled"]["n"]
        band_f1 = item["primary"]["pooled"]["f1"]
        if band_f1 is None or band_f1 < F1_BAR:
            not_publishable.append(f"{band}: LODO F1 {band_f1} is below {F1_BAR}")
        if n < 150:
            not_publishable.append(f"{band}: {n} judged frames, fewer than §13.8's ~150")
    strat = stratification(features, labels, judged[0])
    not_publishable += [
        _stratification_reason(strat),
        "no re-label pass of 50 frames a week later (§13.8)",
        "no labelled renders (§13.8 asks for ~50)",
        f"{len(days)} days: the day-block interval rests on {len(days)} blocks",
    ]
    cloud = methods["v0.2"]["camera_in_cloud"]
    if cloud["labelled_in_cloud"] and cloud["labelled_and_flagged"] < cloud["labelled_in_cloud"]:
        not_publishable.append(
            f"in-cloud class: v0.2 flags {cloud['labelled_and_flagged']} of the "
            f"{cloud['labelled_in_cloud']} frames whose {judged[0]} both labellers call in_cloud "
            "and scores the rest (PRD §13.4 keeps in-cloud frames out of P1's primary)"
        )
    doc: dict[str, Any] = {
        "kind": "snow-globe.extractor-v0.2-lodo",
        "viewpoint_id": viewpoint_id,
        "run_id": run_id,
        "out_dir": str(out_dir),
        "finished_at_utc": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "preregistration": prereg_record,
        "code": code_identity(),
        "plate": plate.to_record(),
        "labels": labels.to_record(),
        "v0_labels": v0_labels.to_record(),
        "camera": {
            "source": "the viewpoint manifest",
            "lat": viewpoint.camera.lat,
            "lon": viewpoint.camera.lon,
            "elevation_m": viewpoint.camera.elevation_m,
        },
        "ephemeris": {"backend": backend.name, "version": backend.version},
        "params": params.to_dict(),
        "days": days,
        "features": feature_counts,
        "stratification": strat,
        "gates_failed": sorted(k for k, f in features.items() if f.gate_failure is not None),
        "wall_s": {
            "features": round(feature_s, 2),
            "lodo_and_scores": round(lodo_s, 2),
            "total": round(time.monotonic() - started, 2),
        },
        "methods": methods,
        "comparisons": comparisons,
        "deployed_fit": deployed,
        "bar": {"band": "summit", "f1": f1, "bar": F1_BAR, "met": met, "statement": statement},
        "publishable": False,
        "not_publishable_because": not_publishable,
    }
    lines = []
    for key in sorted(all_predictions):
        feat = features[key]
        lab = labels.frames[key]
        lines.append(
            json.dumps(
                {
                    "key": key,
                    "sheet": lab.sheet,
                    "local_day": feat.local_day,
                    "image_time_utc": feat.image_time_utc,
                    "sun_alt_deg": round(feat.sun_alt_deg, 3),
                    "light": feat.light.to_dict(),
                    "label": lab.bands,
                    "labellers": {w: {b: lab.labellers[w][b] for b in judged} for w in ("a", "b")},
                    "methods": all_predictions[key],
                }
            )
        )
    _write(out_dir / "predictions.jsonl", "\n".join(lines) + "\n")
    _write(
        out_dir / "folds.json",
        json.dumps({m: v["folds"] for m, v in methods.items()}, indent=1) + "\n",
    )
    _write(
        out_dir / "scores.json",
        json.dumps(
            {
                "methods": {
                    m: {**v["scores"], "camera_in_cloud": v["camera_in_cloud"]}
                    for m, v in methods.items()
                },
                "comparisons": comparisons,
                "bar": doc["bar"],
            },
            indent=1,
        )
        + "\n",
    )
    _write(out_dir / "report.md", _report(doc))
    slim = {k: v for k, v in doc.items() if k != "methods"}
    slim["methods"] = {
        m: {k: v for k, v in item.items() if k != "scores"} for m, item in methods.items()
    }
    _write(out_dir / FINISHED_MARKER, json.dumps(slim, indent=1) + "\n")  # last: the run is done
    return doc


def summit_and_others(bands: Mapping[str, Any]) -> list[tuple[str, Any]]:
    """Bands in their order (summit first, as P1's band)."""
    return list(bands.items())


def register(sub: Any) -> None:
    """Add ``extractor-v0.2`` to the ``snow-globe-eval`` subcommands."""
    parser = sub.add_parser(
        "extractor-v0.2",
        help="extractor v0.2's leave-one-day-out check on labels v0.2 (pre-registered), with v0",
    )
    parser.add_argument("--viewpoint", default="rainier.paradise-mountain")
    parser.add_argument(
        "--plate", type=Path, help="plate folder (default: <data dir>/plates/<viewpoint>/v0.1)"
    )
    parser.add_argument(
        "--labels",
        type=Path,
        help="merged labels to score against (default: the committed "
        "snow_globe_eval/labels_rainier_v0_2.json); with --merge-labels, the new file to write "
        "(required there, and never an existing file)",
    )
    parser.add_argument(
        "--v0-labels",
        type=Path,
        help="labels v0 draws its clear-day pool from (default: labels_rainier_v0_1.json)",
    )
    parser.add_argument(
        "--preregistration",
        type=Path,
        help="default: <data dir>/scores/extractor-v0.2/preregistration.md; required to score",
    )
    parser.add_argument(
        "--run-id",
        help="run folder name (default: a UTC stamp); reuse only to resume an unfinished run, "
        "since a finished run (its run.json) is never written over",
    )
    parser.add_argument("--n-boot", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=20261123)
    parser.add_argument(
        "--merge-labels",
        type=Path,
        metavar="DIR",
        help="instead: merge DIR/labels-a.json and DIR/labels-b.json through DIR/sample.json "
        "into --labels OUT (a new file) and print the agreement",
    )
    parser.set_defaults(handler=run_from_args)


def run_from_args(args: argparse.Namespace) -> int:
    """The command line's entry point."""
    from snow_globe_common.paths import data_dir

    root = data_dir()
    if args.merge_labels is not None:
        if args.labels is None:
            raise ValueError(
                "--merge-labels needs --labels OUT, the new merged file to write; the committed "
                "labels are never rebuilt in place"
            )
        report = build_merged_file(
            args.merge_labels, args.labels, data_root=root, viewpoint_id=args.viewpoint
        )
        print(json.dumps(report, indent=2))
        return 0
    base = root / "scores" / "extractor-v0.2"
    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = base / args.viewpoint / run_id
    doc = run(
        data_root=root,
        viewpoint_id=args.viewpoint,
        plate_dir=args.plate or root / "plates" / args.viewpoint / "v0.1",
        labels_path=args.labels,
        v0_labels_path=args.v0_labels,
        out_dir=out,
        preregistration=args.preregistration or base / "preregistration.md",
        run_id=run_id,
        n_boot=args.n_boot,
        seed=args.seed,
    )
    print(_report(doc), end="")
    return 0
