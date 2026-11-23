"""``snow-globe-eval p1dev``: PLAN M1.5's dev-only P1 table, end to end (PRD §13.4–§13.7, §13.9).

PLAN M1.5's gate: "A dev-only P1 table, with nothing held out touched". For **every Rainier
daytime frame** (Sun ≥ 3° by ephemeris, PRD §13.4) of the development period:

1. **Guard.** The frame must pass the committed held-out split from metadata, before any of its
   bytes is read (:func:`.leakage.frame_file_verdict`: not quarantined, not in design P's sealed
   window or any held-out period); the run refuses (:class:`.heldout.LeakageError`) any frame
   that does not. Frames the extractor run already left out as not development are counted as
   refused and never opened.
2. **Times.** The valid time is the frame's image time; the issue time is valid − lead (24 h by
   default, PRD §13.4 P1); the lead from availability is reported beside it (D-008).
3. **Forecast.** The latest hindcast run file whose availability for the valid time is at or
   before the issue time (:mod:`.p1dev_forecast`), under the run's ``--availability`` rule:
   ``d054`` by default (run start + the recorded lag model, never before the S3 stamps; D-027,
   D-054), or ``s3-stamps`` (the hindcast's rule). Each record also names the run the other
   rule would choose, and the table lists the frames where they differ. No run, and the frame
   has no model forecast.
4. **Scene and render.** A SceneSpec from that run (:func:`.scene.build_scene`, the backtest
   pair's mapping) with **plate v0.1's camera** (its ``renderer_inputs.camera``: position, lens
   height, heading, pitch, roll and field of view), rendered by the renderer at its current
   version over plate v0.1's base and sky mask through the renderer's batch entry point
   (:mod:`.p1dev_render`; SwiftShader, one runner per batch). Every batch must report the
   rendering code's identity the run started with; a change stops the run (resume re-renders).
5. **Truth and prediction.** The same extractor (:mod:`.p1dev_extract`; v0 by default, a
   parameter) on the real frame (the truth) and on the render (the prediction), with the same
   τ and the same clear-day references.
6. **Fitted assets** (:mod:`.p1dev_assets`). The backtest pair's rule for the plate's input
   frames and the clear-day references (before the issue time, off the valid local day) is
   checked and recorded per frame; a truth frame that is one of the plate's input frames is
   excluded from P1 (``p1.excluded_because``), and the table adds sensitivity blocks.
7. **Baselines** (:mod:`.p1dev_baselines`): persistence (a) and (b) as D-036 implements them,
   only from frames available at the issue time **and labelled as of the issue time** (the
   extractor with clear-day references available by then, :class:`.p1dev_baselines.AsOfLabelStore`;
   each record lists the references behind the label); climatology from development frames
   available by the issue time, labelled the same way; S_rule from the same forecast run.
   S_mos: TODO(M2), fitted at the freeze.
8. **Table** (:mod:`.p1dev_table`): PSS per method and the paired ΔPSS against the stronger
   persistence (chosen on dev-fit weeks, :mod:`.p1dev_weeks`), with day-block bootstrap
   intervals; partial frames excluded and counted; in-cloud frames their own class, and hidden
   in a sensitivity table; frames without a forecast by method, reason and day. Labelled
   provisional while any reason is listed.

**Outputs** (nothing in the repo):

- ``<data dir>/scores/p1dev/<run-id>/``: ``run.json`` (options, inputs with their SHA-256s,
  the code, the renderer's and extractor's identity, every invocation and its exact command),
  ``command.txt``, ``extractor/`` (the extractor's own run over every capture),
  ``scenes/<frame>.json``, ``frames/<frame>.json`` (one record per frame: the frame, valid and
  issue times, the forecast file and its availability, the prediction, the truth and every
  baseline), ``table.json`` and ``table.md``;
- ``<data dir>/renders/p1dev/<run-id>/<frame>/``: ``render.png``, ``render.json`` (the
  renderer's metadata) and ``job.json`` (the scene and renderer identity it was made from), and
  ``batches/`` with each batch's job list and log.

**Resumable.** A run is named by its ``--run-id`` (default: the start time, UTC). Run it again
with the same id after an interruption (the Mac may sleep): the extractor run is reused (only
when the extractor's code is unchanged), a frame whose record is final (``scored`` or
``no-forecast``) and made from the same inputs is skipped, a render whose scene and renderer
are unchanged is reused, and records are written one at a time, atomically. "The same inputs"
(:func:`_keep`) are the frame, the forecast file, the extractor's identity, the rendering
code's identity, and the record's provenance: the digest of the code that makes records
(:data:`RECORD_CODE_MODULES`), the manifest's and plate's SHA-256 and the availability rule.
A frame that had no forecast is tried again, so hindcast files that land later are picked up;
a frame whose scene or render failed is tried again too, and an ``OSError`` (a missing scene
schema, an unreadable frame) stops the run instead of being recorded. Each invocation records
its own code identity and inputs in ``run.json``; the first is never overwritten. The options
that change results are fixed at the run's first start; a resume with other values is refused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from PIL import Image
from snow_globe_common.manifest import Viewpoint, load_manifest
from snow_globe_common.paths import LEGACY_ENV_DATA_DIR, data_dir
from snow_globe_common.storage import LocalStorage
from snow_globe_common.timeutil import isoformat_z, parse_iso_utc, utcnow

from . import __version__
from .backtest_pair import _git_state
from .baselines import DevCase
from .ephemeris import EphemerisBackend, get_ephemeris, moon_position, sun_position
from .extractor_v0_run import DEFAULT_PLATE_VERSION
from .forecast_io import ForecastFileError
from .frames import FrameRecord, scan_frames, sha256_file
from .heldout import LeakageError, committed_split, path_quarantine_reason
from .leakage import frame_file_verdict
from .p1dev_assets import PlateInput, fitted_asset_check, plate_inputs
from .p1dev_baselines import AsOfLabelStore, baselines_for, s_rule_for, stronger_persistence
from .p1dev_extract import EXTRACTORS, P1Extractor, Reading
from .p1dev_forecast import (
    AVAILABILITY_RULES,
    DEFAULT_AVAILABILITY_RULE,
    ForecastChoice,
    HindcastCatalogue,
    hindcast_dir,
    other_rule,
    scene_run,
)
from .p1dev_render import RenderJob, identity_digest, render_batch, renderer_identity
from .p1dev_table import build_table, to_markdown
from .p1dev_weeks import DEV_CHECK, DEV_FIT, dev_week_of_time
from .scene import (
    PlateIdentity,
    SceneAssumptions,
    build_scene,
    find_renderer_dir,
    renderer_version,
    validate_scene,
)

__all__ = [
    "DEFAULT_LEAD_H",
    "DEFAULT_SEED",
    "FINAL_STATUSES",
    "RECORD_KIND",
    "P1DevOptions",
    "add_arguments",
    "model_forecast",
    "plate_viewpoint",
    "run_from_args",
    "run_p1dev",
]

VIEWPOINT = "rainier.paradise-mountain"
DEFAULT_LEAD_H = 24.0
DEFAULT_SEED = 20261123
DEFAULT_N_BOOT = 10_000
DEFAULT_BATCH = 12
RECORD_KIND = "snow-globe.p1dev-frame"
RECORD_SCHEMA_VERSION = 2
RUN_KIND = "snow-globe.p1dev-run"
#: Record statuses a resumed run keeps; ``scene-failed`` and ``render-failed`` are tried again.
FINAL_STATUSES = ("scored", "no-forecast")
#: The P1 class of a render that makes a forecast, and the forecast it makes ("visible"?).
FORECAST_OF_CLASS: dict[str, bool] = {
    "visible": True,
    "partial": False,
    "hidden": False,
    "in_cloud": False,
}
#: Modules that make the per-frame records: their digest is in every record, and a resumed run
#: keeps a record only when it is unchanged (:func:`_keep`).
RECORD_CODE_MODULES = (
    "p1dev.py",
    "p1dev_assets.py",
    "p1dev_baselines.py",
    "p1dev_extract.py",
    "p1dev_forecast.py",
    "p1dev_render.py",
    "p1dev_weeks.py",
    "baselines.py",
    "scene.py",
    "forecast_io.py",
    "frames.py",
    "ephemeris.py",
    "leakage.py",
    "heldout.py",
    "extractor.py",
    "extractor_v0.py",
    "extractor_v0_run.py",
)
#: Modules whose SHA-256 the run records (the records' modules and the table's).
CODE_MODULES = (*RECORD_CODE_MODULES, "p1dev_table.py", "scores.py")

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class P1DevOptions:
    """Everything one run of the table needs (the command line fills it in)."""

    run_id: str | None = None
    viewpoint_id: str = VIEWPOINT
    extractor: str = "v0"
    band: str = "summit"
    plate_dir: Path | None = None
    labels_path: Path | None = None
    hindcast_dir: Path | None = None
    lead_hours: float = DEFAULT_LEAD_H
    n_boot: int = DEFAULT_N_BOOT
    seed: int = DEFAULT_SEED
    block_length_days: int = 1
    persistence_tolerance_min: float = 30.0
    climatology_min_count: int = 10
    batch_size: int = DEFAULT_BATCH
    limit: int | None = None
    renderer_dir: Path | None = None
    backend: str = "swiftshader"
    table_only: bool = False
    #: When a forecast hour was available (:data:`.p1dev_forecast.AVAILABILITY_RULES`).
    availability: str = DEFAULT_AVAILABILITY_RULE

    def __post_init__(self) -> None:
        if self.extractor not in EXTRACTORS:
            raise ValueError(f"extractor must be one of {sorted(EXTRACTORS)}")
        if self.availability not in AVAILABILITY_RULES:
            raise ValueError(f"availability must be one of {sorted(AVAILABILITY_RULES)}")
        if not self.lead_hours > 0:
            raise ValueError("lead_hours must be positive")
        if self.block_length_days < 1:
            raise ValueError("the bootstrap block is at least 1 day (PRD §13.4)")
        if self.batch_size < 1 or self.n_boot < 1:
            raise ValueError("batch_size and n_boot must be positive")

    def result_options(self) -> dict[str, Any]:
        """The options that change results: fixed at a run's first start."""
        out = asdict(self)
        for name in ("run_id", "batch_size", "limit", "renderer_dir", "table_only"):
            out.pop(name)
        return {k: (str(v) if isinstance(v, Path) else v) for k, v in out.items()}


# --- small helpers ---------------------------------------------------------------------------


def _write_json(path: Path, doc: Any) -> None:
    """Write JSON atomically (a temporary file beside it, then a rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _digest(items: Mapping[str, Any]) -> str:
    """One SHA-256 over a mapping, in key order (JSON of each value)."""
    text = "".join(
        f"{k} {json.dumps(items[k], sort_keys=True, default=str)}\n" for k in sorted(items)
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _code_identity() -> dict[str, Any]:
    """The code of this invocation: each module's SHA-256, the records' code digest, git."""
    here = Path(__file__).parent
    files = {name: sha256_file(here / name) for name in CODE_MODULES}
    return {
        "snow_globe_eval": __version__,
        "files_sha256": files,
        "record_code_digest": _digest({name: files[name] for name in RECORD_CODE_MODULES}),
        "git": _git_state(),
    }


def _log(message: str) -> None:
    print(f"[p1dev] {message}", file=sys.stderr, flush=True)


def model_forecast(prediction: Reading | None) -> bool | None:
    """The model's yes/no for "visible" from the render's P1 class; None when it makes none."""
    if prediction is None or prediction.p1_class is None:
        return None
    return FORECAST_OF_CLASS.get(prediction.p1_class)


def plate_viewpoint(viewpoint: Viewpoint, camera: Mapping[str, Any]) -> Viewpoint:
    """The viewpoint with the plate's camera: position, ground height and pose (PRD §8.5).

    The scene builder reads the camera's position and ground height from ``camera`` and its
    pose from the manifest's ``pose`` (:func:`.scene.build_scene`); both come from the plate's
    ``renderer_inputs.camera`` here, so the scene's camera is the plate's exactly.
    """
    agl = float(camera["agl_m"])
    cam = replace(
        viewpoint.camera,
        lat=float(camera["lat"]),
        lon=float(camera["lon"]),
        elevation_m=float(camera["elev_m_msl"]) - agl,
    )
    pose = {
        "heading_deg": float(camera["heading_deg"]),
        "pitch_deg": float(camera["pitch_deg"]),
        "roll_deg": float(camera["roll_deg"]),
        "fov_deg": float(camera["hfov_deg"]),
    }
    return replace(viewpoint, camera=cam, raw={**dict(viewpoint.raw), "pose": pose})


@dataclass(frozen=True)
class _Plate:
    folder: Path
    doc: Mapping[str, Any]
    base: Path
    sky_mask: Path | None
    camera: Mapping[str, Any]
    identity: PlateIdentity
    #: Every frame the plate was made from (base, geometry, stars), for the fitted-asset check.
    inputs: tuple[PlateInput, ...] = ()
    json_sha256: str = ""

    def to_record(self) -> dict[str, Any]:
        return {
            "folder": str(self.folder),
            "plate_id": self.doc.get("plate_id"),
            "plate_version": self.doc.get("plate_version"),
            "plate_json_sha256": self.json_sha256,
            "base": {"path": str(self.base), "sha256": self.identity.sha256},
            "sky_mask": None
            if self.sky_mask is None
            else {"path": str(self.sky_mask), "sha256": self.identity.sky_mask_sha256},
            "camera": dict(self.camera),
            "base_frames": [f.get("frame") for f in (self.doc.get("base") or {}).get("frames", [])],
            "input_frames": [
                {
                    "key": item.key,
                    "role": item.role,
                    "image_time_utc": None
                    if item.image_time is None
                    else isoformat_z(item.image_time),
                }
                for item in self.inputs
            ],
        }


def _load_plate(folder: Path, root: Path, viewpoint_id: str) -> _Plate:
    """Plate v0.1's renderer inputs, each file checked against ``plate.json`` (PRD §8.5)."""
    reason = path_quarantine_reason(folder, roots=(root,))
    if reason is not None:
        raise LeakageError(f"plate {folder}: {reason}")
    doc = json.loads((folder / "plate.json").read_text(encoding="utf-8"))
    if doc.get("kind") != "snow-globe.plate" or doc.get("viewpoint_id") != viewpoint_id:
        raise ValueError(f"{folder}/plate.json is not a plate for {viewpoint_id}")
    inputs = doc.get("renderer_inputs") or {}
    camera = inputs.get("camera")
    if not isinstance(camera, Mapping):
        raise ValueError(f"{folder}/plate.json has no renderer_inputs.camera")
    if inputs.get("depth"):
        raise ValueError("this table renders without a depth image; plate.json names one")
    files = doc.get("files") or {}

    def checked(name: str | None) -> tuple[Path, str] | None:
        if not name:
            return None
        path = folder / name
        want = (files.get(name) or {}).get("sha256")
        got = sha256_file(path)
        if got != want:
            raise ValueError(f"{path}: SHA-256 {got} is not plate.json's {want}")
        return path, got

    base = checked(inputs.get("plate"))
    if base is None:
        raise ValueError(f"{folder}/plate.json names no renderer_inputs.plate")
    sky = checked(inputs.get("sky_mask"))
    identity = PlateIdentity(
        id=str(doc["plate_id"]),
        sha256=base[1],
        sky_mask_sha256=None if sky is None else sky[1],
    )
    return _Plate(
        folder,
        doc,
        base[0],
        None if sky is None else sky[0],
        camera,
        identity,
        tuple(plate_inputs(doc)),
        sha256_file(folder / "plate.json"),
    )


def _check_scene_camera(scene: Mapping[str, Any], camera: Mapping[str, Any]) -> None:
    got = scene["camera"]
    for name in ("lat", "lon", "elev_m_msl", "heading_deg", "pitch_deg", "roll_deg", "hfov_deg"):
        if abs(float(got[name]) - float(camera[name])) > 1e-6:
            raise ValueError(
                f"the scene's camera {name} {got[name]} is not the plate's {camera[name]}"
            )


# --- the run ---------------------------------------------------------------------------------


@dataclass
class _Context:
    options: P1DevOptions
    root: Path
    run_id: str
    out: Path
    renders: Path
    viewpoint: Viewpoint
    scene_viewpoint: Viewpoint
    plate: _Plate
    extractor: P1Extractor
    truth: Mapping[str, Reading]
    catalogue: HindcastCatalogue
    renderer_dir: Path
    renderer: Mapping[str, Any]
    backend: EphemerisBackend
    runner: Runner
    store: AsOfLabelStore
    #: Every classified capture's metadata record (image and availability times), by key.
    frames: Mapping[str, FrameRecord]
    #: Each classified capture's availability time, by key (the as-of labels' input).
    available: Mapping[str, datetime]
    #: What every record is made from besides the frame and forecast (see :func:`_keep`).
    provenance: Mapping[str, Any]


@dataclass
class _Prepared:
    key: str
    stem: str
    meta: Mapping[str, Any]
    valid: datetime
    issue: datetime
    verdict: Mapping[str, Any]
    choice: ForecastChoice
    other_choice: ForecastChoice
    baselines: dict[str, Any]
    label_references: dict[str, Any]
    scene_path: Path | None = None
    scene_sha256: str | None = None
    scene_error: str | None = None
    render_job: RenderJob | None = None
    render: dict[str, Any] | None = None
    timings: dict[str, float] | None = None


def _record_path(ctx: _Context, stem: str) -> Path:
    return ctx.out / "frames" / f"{stem}.json"


def _render_dir(ctx: _Context, stem: str) -> Path:
    return ctx.renders / stem


def _reusable_render(ctx: _Context, stem: str, scene_sha: str) -> dict[str, Any] | None:
    folder = _render_dir(ctx, stem)
    job = _read_json(folder / "job.json")
    png = folder / "render.png"
    if not isinstance(job, dict) or not png.is_file():
        return None
    if job.get("scene_sha256") != scene_sha:
        return None
    if job.get("renderer_digest") != identity_digest(ctx.renderer):
        return None
    if job.get("png_sha256") != sha256_file(png):
        return None
    return {**job, "reused": True}


def _extractor_digest(identity: Mapping[str, Any]) -> str:
    """The digest of what the extractor's readings depend on (version, params, pool, code)."""
    keys = ("extractor", "band", "params", "plate", "labels", "code", "reference_pool")
    return _digest({k: identity.get(k) for k in keys})


def _keep(ctx: _Context, record: Any, frame_sha: str, choice: ForecastChoice) -> bool:
    """True when a stored record is final and made from the same inputs (resume).

    The same frame, forecast file and provenance (record code digest, manifest and plate
    SHA-256, extractor digest, availability rule), and for a scored record the same rendering
    code.
    """
    if not isinstance(record, dict) or record.get("status") not in FINAL_STATUSES:
        return False
    if record.get("schema_version") != RECORD_SCHEMA_VERSION:
        return False
    if record.get("frame", {}).get("sha256") != frame_sha:
        return False
    chosen = None if choice.file is None else choice.file.sha256
    if (record.get("forecast") or {}).get("sha256") != chosen:
        return False
    if record.get("extractor") != ctx.extractor.identity().get("extractor"):
        return False
    if record.get("provenance") != dict(ctx.provenance):
        return False
    if record.get("status") == "scored":
        digest = (record.get("render") or {}).get("renderer_digest")
        if digest != identity_digest(ctx.renderer):
            return False
    return True


def _label_references(
    ctx: _Context, issue: datetime, baselines: Mapping[str, Any]
) -> dict[str, Any]:
    """The clear-day references behind each persistence frame's label, checked (PRD §13.1).

    Every reference must have been imaged and available by the issue time; one that was not
    raises :class:`LeakageError` (the as-of labels make that impossible; this checks it).
    """
    readings = ctx.extractor.readings_as_of(issue, ctx.available)
    out: dict[str, Any] = {}
    for name in ("persistence_a", "persistence_b"):
        frames = getattr(baselines.get(name), "frames", ()) or ()
        if not frames:
            continue
        reading = readings.get(frames[0])
        refs = list(reading.references) if reading is not None else []
        for ref in refs:
            record = ctx.frames.get(ref)
            if record is None or record.image_time > issue or record.available_at > issue:
                raise LeakageError(
                    f"{name}'s frame {frames[0]} is labelled against {ref}, which was not "
                    f"available at the issue time {isoformat_z(issue)} (PRD §13.1)"
                )
        out[name] = {
            "frame": frames[0],
            "label_as_of_issue": None if reading is None else reading.p1_class,
            "references": refs,
            "latest_reference_utc": None
            if not refs
            else isoformat_z(max(ctx.frames[r].image_time for r in refs)),
        }
    return out


def _prepare(ctx: _Context, key: str) -> _Prepared:
    """Guard, times, forecast choice, baselines and scene for one frame (no render yet)."""
    t0 = time.perf_counter()
    path = ctx.root / key
    verdict = frame_file_verdict(path, data_root=ctx.root)
    if not verdict["development"]:
        raise LeakageError(
            f"{key} may not be used for development ({'; '.join(verdict['reasons'])}): the run "
            "refuses it (PRD §13.2, D-019, D-022)"
        )
    meta = ctx.extractor.frame_meta(key)
    valid = parse_iso_utc(meta["image_time_utc"])
    issue = valid - timedelta(hours=ctx.options.lead_hours)
    choice = ctx.catalogue.choose(issue, valid)
    other = ctx.catalogue.choose(issue, valid, rule=other_rule(ctx.catalogue.rule))
    t1 = time.perf_counter()
    s_rule = None
    camera_height = float(ctx.plate.camera["elev_m_msl"])
    if choice.file is not None and choice.available is not None:
        s_rule = s_rule_for(
            run=choice.file.run,
            viewpoint=ctx.scene_viewpoint,
            landmark=ctx.scene_viewpoint.landmarks[0],
            issue_time=issue,
            valid_time=valid,
            available=choice.available,
            camera_height_m_msl=camera_height,
        )
    baselines = baselines_for(
        ctx.store,
        viewpoint_id=ctx.viewpoint.id,
        time_zone=ctx.viewpoint.time_zone,
        issue_time=issue,
        valid_time=valid,
        label=ctx.options.band,
        tolerance=timedelta(minutes=ctx.options.persistence_tolerance_min),
        climatology_min_count=ctx.options.climatology_min_count,
        s_rule_prediction=s_rule,
    )
    label_references = _label_references(ctx, issue, baselines)
    t2 = time.perf_counter()
    prepared = _Prepared(
        key=key,
        stem=Path(key).stem,
        meta=meta,
        valid=valid,
        issue=issue,
        verdict=verdict,
        choice=choice,
        other_choice=other,
        baselines=baselines,
        label_references=label_references,
    )
    if choice.file is not None:
        _scene(ctx, prepared)
    t3 = time.perf_counter()
    prepared.timings = {
        "guard_and_forecast_choice_s": round(t1 - t0, 4),
        "baselines_s": round(t2 - t1, 4),
        "scene_s": round(t3 - t2, 4),
    }
    return prepared


def _scene(ctx: _Context, p: _Prepared) -> None:
    """Build, check and write the frame's SceneSpec, and queue its render unless reusable.

    A ``ForecastFileError`` or ``ValueError`` (the run lacks a field, the scene fails the
    schema) is recorded as ``scene-failed``, which a resume tries again. An ``OSError`` (the
    truth frame unreadable, ``renderer/scene.schema.json`` missing) stops the run: it is a
    fault of the machine or of a concurrent edit, not of this frame.
    """
    choice = p.choice
    assert choice.file is not None and choice.available is not None
    vp = ctx.scene_viewpoint
    cam = vp.camera
    with Image.open(ctx.root / p.key) as image:
        width, height = image.size
    try:
        run, absent = scene_run(choice.file.run)
        scene = build_scene(
            viewpoint=vp,
            valid_time=p.valid,
            run=run,
            sun=sun_position(p.valid, cam.lat, cam.lon, cam.elevation_m, ephemeris=ctx.backend),
            moon=moon_position(p.valid, cam.lat, cam.lon, cam.elevation_m, ephemeris=ctx.backend),
            width=width,
            height=height,
            renderer_version=str(ctx.renderer["renderer_version"]),
            plate=ctx.plate.identity,
            assumptions=SceneAssumptions(agl_m=float(ctx.plate.camera["agl_m"])),
            meta={
                "issued_by": "snow-globe-eval p1dev (PLAN M1.5 dev-only P1 table)",
                "pose_source": f"{ctx.plate.doc.get('plate_id')} renderer_inputs.camera",
                "p1dev_run_id": ctx.run_id,
                "truth_frame": p.key,
                "issue_time_utc": isoformat_z(p.issue),
                "forecast_hours": [choice.fh_lo, choice.fh_hi],
                "forecast_weight_hi": choice.weight_hi,
                "forecast_available_utc": isoformat_z(choice.available),
                "forecast_availability_rule": choice.rule,
                "forecast_availability_source": choice.availability_source,
                "forecast_fields_absent_read_as_missing": list(absent),
            },
        )
        _check_scene_camera(scene, ctx.plate.camera)
        validate_scene(scene, ctx.renderer_dir)
    except (ForecastFileError, ValueError) as exc:
        p.scene_error = f"{type(exc).__name__}: {exc}"[:1000]
        return
    path = ctx.out / "scenes" / f"{p.stem}.json"
    _write_json(path, scene)
    p.scene_path = path
    p.scene_sha256 = sha256_file(path)
    reusable = _reusable_render(ctx, p.stem, p.scene_sha256)
    if reusable is not None:
        p.render = reusable
        return
    folder = _render_dir(ctx, p.stem)
    p.render_job = RenderJob(
        id=p.stem,
        scene=path,
        plate=ctx.plate.base,
        sky_mask=ctx.plate.sky_mask,
        depth=None,
        out=folder / "render.png",
        meta=folder / "render.json",
    )


def _finish(
    ctx: _Context,
    p: _Prepared,
    results: Mapping[str, Mapping[str, Any]],
    batch: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The frame's record, after its render (if any) and the extractor on it."""
    truth = ctx.truth[p.key]
    prediction: Reading | None = None
    status = "no-forecast"
    render_info: dict[str, Any] | None = p.render
    t0 = time.perf_counter()
    if p.choice.file is not None:
        status = "scene-failed" if p.scene_error else "render-failed"
    if p.render_job is not None:
        result = results.get(p.render_job.id) or {}
        png = p.render_job.out
        start = (batch or {}).get("start") or {}
        if result.get("ok") and png.is_file() and sha256_file(png) == result.get("sha256"):
            render_info = {
                "scene_sha256": p.scene_sha256,
                # the digest the batch itself reported (checked equal to the run's)
                "renderer_digest": identity_digest(start) or identity_digest(ctx.renderer),
                "png": str(png),
                "png_sha256": result["sha256"],
                "meta": result.get("meta"),
                "scene_hash": result.get("scene_hash"),
                "render_ms": result.get("render_ms"),
                "total_ms": result.get("total_ms"),
                "batch_log": (batch or {}).get("log"),
                "created_utc": isoformat_z(utcnow(), "seconds"),
            }
            _write_json(_render_dir(ctx, p.stem) / "job.json", render_info)
            render_info = {**render_info, "reused": False}
        else:
            render_info = {"error": result.get("error") or "the render did not come back"}
    if render_info is not None and render_info.get("png"):
        prediction = ctx.extractor.measure(Path(render_info["png"]), truth_key=p.key)
        status = "scored"
    t1 = time.perf_counter()
    forecasts = {
        "model": model_forecast(prediction),
        **{name: pred.value for name, pred in p.baselines.items()},
    }
    choice_record = p.choice.to_record()
    week = dev_week_of_time(p.valid, ctx.viewpoint.time_zone)
    references = set(truth.references) | set(() if prediction is None else prediction.references)
    assets = fitted_asset_check(
        truth_key=p.key,
        issue=p.issue,
        valid=p.valid,
        time_zone=ctx.viewpoint.time_zone,
        viewpoint_id=ctx.viewpoint.id,
        plate=ctx.plate.inputs,
        references=references,
        records=ctx.frames,
        split=committed_split(),
    )
    excluded = None
    if assets["truth_is_plate_input"] is not None:
        excluded = (
            f"the truth frame is one of the plate's input frames ({assets['truth_is_plate_input']}"
            "): the render is made from it (backtest_pair refuses a plate that is the truth frame)"
        )
    other = p.other_choice.summary()
    return {
        "kind": RECORD_KIND,
        "schema_version": RECORD_SCHEMA_VERSION,
        "run_id": ctx.run_id,
        "status": status,
        "extractor": ctx.extractor.identity().get("extractor"),
        "provenance": dict(ctx.provenance),
        "created_utc": isoformat_z(utcnow(), "seconds"),
        "frame": {
            "key": p.key,
            "path": str(ctx.root / p.key),
            "sha256": p.meta["sha256"],
            "image_time_utc": p.meta["image_time_utc"],
            "local_day": week["local_day"],
            "dev_week": {k: week[k] for k in ("iso_year", "iso_week", "role")},
            "sun_alt_deg": p.meta["sun_alt_deg"],
            "sun_az_deg": p.meta["sun_az_deg"],
        },
        "heldout_guard": {
            "development": p.verdict["development"],
            "fit": p.verdict["fit"],
            "reasons": list(p.verdict["reasons"]),
            "scoring_exclusion": p.verdict["scoring_exclusion"],
        },
        "valid_time_utc": isoformat_z(p.valid),
        "issue_time_utc": isoformat_z(p.issue),
        "lead_nominal_h": ctx.options.lead_hours,
        "availability_rule": p.choice.rule,
        "forecast": choice_record,
        "forecast_passed_over": [list(item) for item in p.choice.passed_over]
        if choice_record is None
        else None,
        "forecast_other_rule": {**other, "same_run": other["run"] == p.choice.run_id},
        "scene": None
        if p.choice.file is None
        else {
            "path": None if p.scene_path is None else str(p.scene_path),
            "sha256": p.scene_sha256,
            "error": p.scene_error,
        },
        "render": render_info,
        "truth": truth.to_record(),
        "prediction": None if prediction is None else prediction.to_record(),
        "same_references": None
        if prediction is None
        else sorted(prediction.references) == sorted(truth.references),
        "fitted_assets": assets,
        "baselines": {
            name: {
                **pred.to_dict(),
                **({"label": p.label_references[name]} if name in p.label_references else {}),
            }
            for name, pred in p.baselines.items()
        },
        "p1": {
            "event": "summit band visible",
            "truth_class": truth.p1_class,
            "excluded_because": excluded,
            "forecasts": forecasts,
        },
        "timings_s": {**(p.timings or {}), "extract_render_s": round(t1 - t0, 4)},
    }


def _check_batch_identity(ctx: _Context, batch: Mapping[str, Any]) -> None:
    """Stop the run if a batch was rendered by other code than the run identified at its start.

    The batch script prints the identity of the code it renders with; another package may
    change ``renderer/src`` while a run goes on. A batch with another digest is never recorded
    (no ``job.json``, no frame record): a resume identifies the renderer again and renders
    those frames again.
    """
    start = batch.get("start")
    if not isinstance(start, Mapping):
        return  # the script printed nothing (its results come back failed and are retried)
    got, want = identity_digest(start), identity_digest(ctx.renderer)
    if got != want:
        raise RuntimeError(
            f"the renderer changed during the run: the batch logged in {batch.get('log')} "
            f"rendered with digest {got}, the run started with {want}; nothing of that batch is "
            "recorded. Run again with the same --run-id to render those frames with the new code."
        )


def _env_record() -> dict[str, str]:
    names = (
        "SNOW_GLOBE_DATA_DIR",
        LEGACY_ENV_DATA_DIR,
        "SNOW_GLOBE_EPHEMERIS",
        "SNOW_GLOBE_NODE",
        "SNOW_GLOBE_RENDERER_DIR",
        "SNOW_GLOBE_MANIFEST_DIR",
        "PYTHONPATH",
    )
    return {n: os.environ[n] for n in names if os.environ.get(n)}


def _runnable(command: Sequence[str]) -> list[str]:
    """The command as it can be run again: ``python -m`` for a module run, else as given."""
    argv = list(command)
    if argv and argv[0].endswith(("snow_globe_eval/cli.py", "snow_globe_eval/__main__.py")):
        return [sys.executable, "-m", "snow_globe_eval.cli", *argv[1:]]
    return argv


def _command_line(command: Sequence[str]) -> str:
    """``cd <cwd> && <env> <command>``: the exact command, runnable as written."""
    env = " ".join(f"{k}={shlex.quote(v)}" for k, v in _env_record().items())
    run = shlex.join(_runnable(command))
    return f"cd {shlex.quote(str(Path.cwd()))} && " + (env + " " if env else "") + run


def _start_run(
    options: P1DevOptions, out: Path, run_id: str, command: Sequence[str], now: datetime
) -> dict[str, Any]:
    """Create ``run.json``, or check a resumed run's options against it."""
    path = out / "run.json"
    existing = _read_json(path)
    invocation = {
        "started_utc": isoformat_z(now, "seconds"),
        "command": list(command),
        "command_line": _command_line(command),
        "cwd": str(Path.cwd()),
        "env": _env_record(),
        "batch_size": options.batch_size,
        "limit": options.limit,
        "table_only": options.table_only,
        "code": _code_identity(),
    }
    if isinstance(existing, dict):
        if existing.get("options") != options.result_options():
            raise ValueError(
                f"run {run_id} was started with other options ({existing.get('options')}); "
                "use a new --run-id"
            )
        existing.setdefault("invocations", []).append(invocation)
        run = existing
    else:
        if out.exists() and any(out.iterdir()):
            raise ValueError(f"{out} exists without a run.json; use a new --run-id")
        run = {
            "kind": RUN_KIND,
            "schema_version": 1,
            "run_id": run_id,
            "created_utc": isoformat_z(now, "seconds"),
            "options": options.result_options(),
            "invocations": [invocation],
        }
    _write_json(path, run)
    with (out / "command.txt").open("a", encoding="utf-8") as handle:
        handle.write(f"# {invocation['started_utc']}  (cwd {invocation['cwd']})\n")
        handle.write(invocation["command_line"] + "\n")
    return run


def _daytime_keys(ctx: _Context) -> list[str]:
    keys = [k for k, r in ctx.truth.items() if r.frame_class != "night"]
    keys.sort(key=lambda k: (parse_iso_utc(ctx.extractor.frame_meta(k)["image_time_utc"]), k))
    if ctx.options.limit is not None:
        keys = keys[: ctx.options.limit]
    return keys


def _records(ctx: _Context, keys: Sequence[str]) -> list[dict[str, Any]]:
    out = []
    for key in keys:
        record = _read_json(_record_path(ctx, Path(key).stem))
        if isinstance(record, dict) and record.get("kind") == RECORD_KIND:
            out.append(record)
    return out


def _table(ctx: _Context, run: dict[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    records = _records(ctx, keys)
    cases = [
        DevCase(
            ctx.viewpoint.id,
            parse_iso_utc(r["issue_time_utc"]),
            parse_iso_utc(r["valid_time_utc"]),
            {"visible": True, "hidden": False}.get(str(r["p1"]["truth_class"])),
        )
        for r in records
        if r["frame"]["dev_week"]["role"] == DEV_FIT
    ]
    choice, choice_record = stronger_persistence(
        ctx.store,
        cases,
        label=ctx.options.band,
        tolerance=timedelta(minutes=ctx.options.persistence_tolerance_min),
    )
    validation = ctx.extractor.validation()
    provisional = [
        f"extractor {ctx.options.extractor}: "
        + (
            "meets its PRD §13.8 bar"
            if validation["publishable"]
            else f"below its PRD §13.8 bar ({validation['band']} F1 {validation['band_f1']} on "
            f"{validation['band_judged_n']} judged frames; bar {validation['f1_bar']})"
        ),
        *(f"validation: {r}" for r in validation["not_publishable_because"]),
        *ctx.extractor.provisional_notes(),
        f"the bootstrap block of {ctx.options.block_length_days} day(s) is not yet set from "
        "development autocorrelation (TODO(M2); PRD §13.4: at least 1 day)",
        f"{len({r['frame']['local_day'] for r in records})} local days only: the intervals are "
        "wide and the effective n is the days",
    ]
    rule = ctx.catalogue.rule
    if rule != "d054":
        provisional.append(
            f"forecast availability by the {rule!r} rule, not D-027/D-054's lag model with the "
            "S3 floor: a sensitivity until the lead rules"
        )
    else:
        provisional.append(
            "forecast availability by D-054's rule with the lag model each hour records (the "
            "live log's model when the hindcast file was written), not one recomputed from "
            "forecast_log/ at selection as D-027 asks; the lead rules which availability P1 uses"
        )
    assets_failing = sum(
        not (r.get("fitted_assets") or {}).get("passes", False)
        for r in records
        if r["p1"].get("excluded_because") is None
    )
    if assets_failing:
        provisional.append(
            f"{assets_failing} of {len(records)} frames use a fitted asset the backtest pair would "
            "refuse (plate v0.1's input frames or a clear-day reference taken at or after the "
            "issue time, or on the valid local day): the primary uses them pending the lead's "
            "ruling; the 'fitted assets before the issue time' block restricts to those that pass"
        )
    dev_check_assets = sum(
        r["frame"]["dev_week"]["role"] == DEV_CHECK
        and bool((r.get("fitted_assets") or {}).get("uses_dev_check_week_asset"))
        for r in records
    )
    if dev_check_assets:
        provisional.append(
            f"{dev_check_assets} dev-check frames are measured or rendered with an asset from a "
            "dev-check week; PRD §13.9 fits on dev-fit weeks (TODO(M2): dev-fit assets only)"
        )
    roles = Counter(r["frame"]["dev_week"]["role"] for r in records)
    if not roles.get(DEV_CHECK):
        provisional.append(
            "no dev-check frames yet (odd ISO weeks are dev-fit): the stronger persistence was "
            "chosen on the very frames it is compared on, and the dev-check rows are empty"
        )
    if not roles.get(DEV_FIT):
        provisional.append("no dev-fit frames: the stronger persistence defaults to (a) by rule")
    extractor = ctx.extractor.identity()
    params = extractor.get("params") or {}
    statuses = Counter(r["status"] for r in records)
    skipped = extractor.get("frames_skipped_by_the_run") or {}
    refused = {k: v for k, v in skipped.items() if str(k).startswith("not development")}
    unreadable = {k: v for k, v in skipped.items() if not str(k).startswith("not development")}
    split = committed_split()
    header = {
        "run_id": ctx.run_id,
        "created_utc": isoformat_z(utcnow(), "seconds"),
        "viewpoint_id": ctx.viewpoint.id,
        "lead_nominal_h": ctx.options.lead_hours,
        "availability": {"rule": rule, "description": AVAILABILITY_RULES[rule]},
        "extractor": {
            "extractor": extractor.get("extractor"),
            "band": ctx.options.band,
            "tau": {"tau_hi": params.get("tau_hi"), "tau_lo": params.get("tau_lo")},
            "reference_pool": len(extractor.get("reference_pool") or []),
            "validation": validation,
        },
        "renderer": {
            "renderer_version": ctx.renderer.get("renderer_version"),
            "digest": identity_digest(ctx.renderer),
            "backend": ctx.options.backend,
        },
        "plate": {"plate_id": ctx.plate.doc.get("plate_id"), "camera": dict(ctx.plate.camera)},
        "split": split.to_record(),
        "refused_by_the_guard": sum(int(v) for v in refused.values()),
        "refused_by_the_guard_reasons": refused,
        "unreadable_frames": sum(int(v) for v in unreadable.values()),
        "unreadable_frames_reasons": unreadable,
        "baseline_labels": "as of each issue time: the extractor with clear-day references "
        "imaged and available by the issue time (p1dev_baselines.AsOfLabelStore, PRD §13.1)",
        "dev_split_rule": "ISO-week parity of the valid time's local day: odd weeks dev-fit, "
        "even weeks dev-check (p1dev_weeks)",
        "records_by_status": dict(sorted(statuses.items())),
        "frames_without_record": len(keys) - len(records),
        "commands": [
            {
                "started_utc": inv.get("started_utc"),
                "table_only": bool(inv.get("table_only")),
                "command_line": inv.get("command_line"),
            }
            for inv in run.get("invocations", [])
        ],
        "run_dir": str(ctx.out),
    }
    table = build_table(
        records,
        stronger=choice.chosen,
        persistence_choice=choice_record,
        provisional_because=provisional,
        seed=ctx.options.seed,
        n_boot=ctx.options.n_boot,
        block_length_days=ctx.options.block_length_days,
        header=header,
    )
    _write_json(ctx.out / "table.json", table)
    (ctx.out / "table.md").write_text(to_markdown(table), encoding="utf-8")
    return table


def run_p1dev(
    options: P1DevOptions,
    *,
    command: Sequence[str] = (),
    ephemeris: EphemerisBackend | None = None,
    runner: Runner = subprocess.run,
    root: Path | None = None,
    now: Callable[[], datetime] = utcnow,
) -> dict[str, Any]:
    """Run (or resume) the table; returns a short summary (see the module docstring)."""
    started = now()
    data_root = (root if root is not None else data_dir()).resolve()
    run_id = options.run_id or started.strftime("%Y%m%dT%H%M%SZ")
    if not run_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError("run id: letters, digits, '-' and '_' only")
    out = data_root / "scores" / "p1dev" / run_id
    renders = data_root / "renders" / "p1dev" / run_id
    out.mkdir(parents=True, exist_ok=True)
    run = _start_run(options, out, run_id, command or sys.argv, started)
    backend = ephemeris or get_ephemeris()
    viewpoint = load_manifest(options.viewpoint_id)
    vp_split = committed_split().viewpoint(viewpoint.id)
    if vp_split.time_zone != viewpoint.time_zone:
        raise ValueError(
            f"{viewpoint.id}: the manifest's zone {viewpoint.time_zone} is not the split's "
            f"{vp_split.time_zone}"
        )
    plate_dir = (
        options.plate_dir or data_root / "plates" / viewpoint.id / DEFAULT_PLATE_VERSION
    ).expanduser()
    plate = _load_plate(plate_dir.resolve(), data_root, viewpoint.id)
    extractor = EXTRACTORS[options.extractor](
        data_root=data_root,
        viewpoint_id=viewpoint.id,
        plate_dir=plate.folder,
        work_dir=out / "extractor",
        labels_path=options.labels_path,
        ephemeris=backend,
        band=options.band,
        require_same_code=not options.table_only,
    )
    _log(f"run {run_id}: extractor {options.extractor} over every development capture")
    extractor.prepare()
    truth = extractor.truth_readings()
    storage = LocalStorage(data_root)
    scanned = {r.key: r for r in scan_frames(storage, viewpoint.id, kinds=("frames",))}
    records = [r for r in scanned.values() if r.key in truth]
    available = {r.key: r.available_at for r in records}

    def labels_at(moment: datetime) -> dict[str, str | None]:
        return {k: r.p1_class for k, r in extractor.readings_as_of(moment, available).items()}

    store = AsOfLabelStore(records, labels_at, label=options.band)
    catalogue = HindcastCatalogue.scan(
        (options.hindcast_dir or hindcast_dir(data_root, viewpoint.id)).expanduser(),
        viewpoint_id=viewpoint.id,
        roots=(data_root,),
        rule=options.availability,
    )
    renderer_dir = (options.renderer_dir or find_renderer_dir()).resolve()
    if options.table_only and isinstance(run.get("renderer"), dict):
        identity = run["renderer"]  # the table renders nothing: the run's recorded identity
    else:
        identity = renderer_identity(renderer_dir, runner=runner)
        if identity.get("renderer_version") != renderer_version(renderer_dir):
            raise RuntimeError("the batch script reports another renderer version than version.ts")
    invocation = run["invocations"][-1]
    provenance = {
        "record_code_digest": invocation["code"]["record_code_digest"],
        "manifest_sha256": sha256_file(viewpoint.path),
        "plate_json_sha256": plate.json_sha256,
        "extractor_digest": _extractor_digest(extractor.identity()),
        "availability_rule": options.availability,
        "lead_hours": options.lead_hours,
    }
    ctx = _Context(
        options=options,
        root=data_root,
        run_id=run_id,
        out=out,
        renders=renders,
        viewpoint=viewpoint,
        scene_viewpoint=plate_viewpoint(viewpoint, plate.camera),
        plate=plate,
        extractor=extractor,
        truth=truth,
        catalogue=catalogue,
        renderer_dir=renderer_dir,
        renderer=identity,
        backend=backend,
        runner=runner,
        store=store,
        frames=scanned,
        available=available,
        provenance=provenance,
    )
    keys = _daytime_keys(ctx)
    invocation["inputs"] = {
        "manifest": {
            "path": str(viewpoint.path),
            "sha256": provenance["manifest_sha256"],
            "camera": asdict(viewpoint.camera) | {"sources": None},
        },
        "plate": plate.to_record(),
        "hindcast": catalogue.to_record(),
        "split": committed_split().to_record(),
    }
    invocation["provenance"] = provenance
    invocation["renderer"] = identity
    invocation["extractor"] = extractor.identity()
    if not options.table_only:
        run["renderer"] = identity  # the identity a later --table-only rebuild reports
    run["extractor"] = extractor.identity()
    run.setdefault("code", invocation["code"])  # the first invocation's; never overwritten
    _write_json(out / "run.json", run)
    _log(f"{len(keys)} daytime frames; {len(catalogue.files)} hindcast files in {catalogue.folder}")

    done = kept = 0
    if not options.table_only:
        pending: list[str] = []
        for key in keys:
            meta = extractor.frame_meta(key)
            valid = parse_iso_utc(meta["image_time_utc"])
            choice = catalogue.choose(valid - timedelta(hours=options.lead_hours), valid)
            if _keep(ctx, _read_json(_record_path(ctx, Path(key).stem)), meta["sha256"], choice):
                kept += 1
            else:
                pending.append(key)
        _log(f"{kept} frames already done; {len(pending)} to do")
        for start in range(0, len(pending), options.batch_size):
            chunk = [_prepare(ctx, key) for key in pending[start : start + options.batch_size]]
            jobs = [p.render_job for p in chunk if p.render_job is not None]
            batch = render_batch(
                jobs,
                renderer_dir=renderer_dir,
                log_path=renders / "batches" / f"batch-{utcnow():%Y%m%dT%H%M%SZ}-{start:05d}.log",
                backend=options.backend,
                runner=runner,
            )
            _check_batch_identity(ctx, batch)
            for p in chunk:
                record = _finish(ctx, p, batch["results"], batch)
                _write_json(_record_path(ctx, p.stem), record)
                done += 1
            _log(
                f"{kept + done}/{len(keys)} frames: batch of {len(chunk)} with {len(jobs)} renders "
                f"({batch.get('wall_s', 0)} s)"
            )
    table = _table(ctx, run, keys)
    run = _read_json(out / "run.json") or run
    run["invocations"][-1]["finished_utc"] = isoformat_z(utcnow(), "seconds")
    run["invocations"][-1]["frames_done"] = done
    run["invocations"][-1]["frames_kept"] = kept
    run["progress"] = {
        "daytime_frames": len(keys),
        "records_by_status": table["records_by_status"],
        "frames_without_record": table["frames_without_record"],
    }
    _write_json(out / "run.json", run)
    primary = table["primary"]
    stronger = table["persistence_choice"]["stronger"]
    headline = next((row for row in primary["delta"]["all"] if row["reference"] == stronger), None)
    return {
        "run_id": run_id,
        "out": str(out),
        "table": str(out / "table.md"),
        "availability_rule": options.availability,
        "daytime_frames": len(keys),
        "with_forecast_run": table["frames"]["all"]["with_forecast_run"],
        "excluded_truth_is_plate_input": table["frames"]["all"]["excluded_truth_is_plate_input"],
        "records_by_status": table["records_by_status"],
        "stronger_persistence": table["persistence_choice"]["stronger"],
        "delta_vs_stronger_persistence_all": None
        if headline is None
        else {
            k: headline[k] for k in ("delta", "ci_low", "ci_high", "n_pairs", "n_days_with_pairs")
        },
        "provisional": table["provisional"],
    }


# --- command line ----------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """The ``snow-globe-eval p1dev`` options."""
    parser.add_argument(
        "--run-id", help="name of the run; give an earlier one to resume it (default: UTC now)"
    )
    parser.add_argument("--viewpoint", default=VIEWPOINT, help=f"viewpoint (default {VIEWPOINT})")
    parser.add_argument(
        "--extractor", default="v0", choices=sorted(EXTRACTORS), help="extractor version (v0)"
    )
    parser.add_argument("--band", default="summit", help="the P1 band of the plate (summit)")
    parser.add_argument(
        "--plate",
        type=Path,
        help=f"plate folder (default <data dir>/plates/<viewpoint>/{DEFAULT_PLATE_VERSION})",
    )
    parser.add_argument(
        "--labels", type=Path, help="labels file for the clear-day pool (default: the committed)"
    )
    parser.add_argument(
        "--hindcast-dir",
        type=Path,
        help="hindcast run files (default <data dir>/forecast/hindcast/hrrr/<viewpoint>)",
    )
    parser.add_argument(
        "--lead",
        type=float,
        default=DEFAULT_LEAD_H,
        help="nominal lead, h (24); issue = valid - lead",
    )
    parser.add_argument(
        "--availability",
        choices=sorted(AVAILABILITY_RULES),
        default=DEFAULT_AVAILABILITY_RULE,
        help="when a forecast hour was available: d054 (default; run start + the recorded lag "
        "model, never before the S3 stamps, D-027/D-054) or s3-stamps (the hindcast's rule)",
    )
    parser.add_argument("--n-boot", type=int, default=DEFAULT_N_BOOT, help="bootstrap resamples")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="bootstrap seed")
    parser.add_argument(
        "--block-days", type=int, default=1, help="bootstrap block length in days (>= 1)"
    )
    parser.add_argument(
        "--batch", type=int, default=DEFAULT_BATCH, help="frames per render batch (progress unit)"
    )
    parser.add_argument("--limit", type=int, help="only the first N daytime frames (a smoke run)")
    parser.add_argument("--renderer-dir", type=Path, help="renderer/ (default: found in the repo)")
    parser.add_argument("--backend", choices=["swiftshader", "gpu"], default="swiftshader")
    parser.add_argument(
        "--table-only",
        action="store_true",
        help="rebuild table.json and table.md from the run's records; render nothing",
    )


def options_from_args(args: argparse.Namespace) -> P1DevOptions:
    """:class:`P1DevOptions` from parsed arguments."""
    return P1DevOptions(
        run_id=args.run_id,
        viewpoint_id=args.viewpoint,
        extractor=args.extractor,
        band=args.band,
        plate_dir=args.plate,
        labels_path=args.labels,
        hindcast_dir=args.hindcast_dir,
        lead_hours=args.lead,
        n_boot=args.n_boot,
        seed=args.seed,
        block_length_days=args.block_days,
        batch_size=args.batch,
        limit=args.limit,
        renderer_dir=args.renderer_dir,
        backend=args.backend,
        table_only=args.table_only,
        availability=args.availability,
    )


def run_from_args(args: argparse.Namespace) -> int:
    """``snow-globe-eval p1dev``: run or resume the table and print its summary."""
    summary = run_p1dev(options_from_args(args), command=sys.argv)
    print(json.dumps(summary, indent=2))
    return 0
