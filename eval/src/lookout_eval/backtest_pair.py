"""One backtest pair, end to end (PRD §16 M0.5: "the pair rendered and scored, labelled v−1").

An archived truth frame and an archived HRRR run become a SceneSpec, a headless render
(``pnpm --dir renderer render``) and two extractor scores, truth and render, with the same
region and clear-day reference. Everything the result depends on is hashed into a pair
record at ``$LOOKOUT_DATA_DIR/scores/pairs/<viewpoint_id>/<pair_id>.json``. The scene,
render and renderer log sit beside it in ``<pair_id>/``, never in the repo.

**Leakage checks** (PRD §13.1, §13.9). Any failure raises :class:`LeakageError` (or
:class:`PairError` for inconsistent inputs) before anything is written:

- the forecast hours the valid time needs were available by the issue time (valid − lead),
  by the file's recorded availability (live ``LastModified`` or modelled);
- the truth frame is at the valid time (within a tolerance), is not held out, and is
  neither a reference frame nor the plate (compared by SHA-256);
- every clear-day reference frame and the plate is a fitted asset: taken before the issue
  time and available by then, not on the valid time's local day, not held out, and before
  the start of the earliest held-out period (§13.9). The checks are written into the record;
- with ``--forecast auto``, lookout-forecast picks the newest run whose *modelled*
  availability precedes the issue time and backfills it from NOAA (network).

The held-out periods default to design P's sealed window (§13.2) in the viewpoint's time
zone, until the committed split replaces it (TODO(M0.5): the split in DECISIONS.md).

**Frame quality gates** (§8.6, :mod:`.gates`) are applied to the truth frame and recorded;
a truth frame that fails a gate is not scored (``extractor.agree`` is None).

If ``renderer/scene.schema.json`` does not exist, the pair writes the SceneSpec and stops,
with status ``scene-only``. When Node 24 is present, the renderer's own ``validateScene()``
checks the scene before the browser starts; a rejected scene gets status ``scene-rejected``.
"""

from __future__ import annotations

import hashlib
import json
import platform
import shlex
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from importlib import metadata
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import lookout_common
from lookout_common.manifest import Viewpoint, load_manifest
from lookout_common.paths import data_dir, enclosing_git_root, forecast_key, sidecar_key
from lookout_common.storage import LocalStorage
from lookout_common.timeutil import isoformat_z, utcnow
from PIL import Image

from . import __version__
from .baselines import s_rule
from .ephemeris import EphemerisBackend, get_ephemeris, moon_position, sun_position
from .extractor import (
    EXTRACTOR_VERSION,
    ClearDayReference,
    ExtractorParams,
    ReferenceFrame,
    Region,
    extract,
)
from .forecast_io import LOW_CLOUD, MID_CLOUD, ForecastRun, summit_ray
from .frames import parse_frame_key, read_sidecar_for_path, record_from_path, sha256_file
from .gates import frame_gates
from .leakage import HeldOut, LeakageError
from .scene import (
    PlateIdentity,
    RendererValidatorUnavailableError,
    SceneAssumptions,
    SceneSchemaMissingError,
    build_scene,
    find_renderer_dir,
    renderer_validate,
    renderer_version,
    validate_scene,
)

__all__ = [
    "PAIR_LABEL",
    "PairError",
    "PairInputs",
    "auto_forecast",
    "pair_record_key",
    "run_pair",
]

PAIR_LABEL = "v-1"
RECORD_KIND = "lookout.backtest-pair"

Runner = Callable[..., subprocess.CompletedProcess[str]]


class PairError(RuntimeError):
    """The pair's inputs are inconsistent (wrong time, a reused frame, a missing file)."""


@dataclass(frozen=True)
class PairInputs:
    """Everything a backtest pair needs (the CLI fills this in)."""

    viewpoint_id: str
    valid_time: datetime
    lead_hours: float
    truth_path: Path
    forecast: str
    region: Region
    reference_paths: tuple[Path, ...]
    plate_path: Path | None = None
    sky_mask_path: Path | None = None
    depth_path: Path | None = None
    landmark: str | None = None
    truth_tolerance: timedelta = timedelta(minutes=10)
    params: ExtractorParams = field(default_factory=ExtractorParams)
    assumptions: SceneAssumptions = field(default_factory=SceneAssumptions)
    renderer_dir: Path | None = None
    renderer_backend: str = "swiftshader"
    render_timeout_s: float = 900.0
    scene_only: bool = False
    held_out: HeldOut | None = None  # None: design P's sealed window (PRD §13.2)

    def __post_init__(self) -> None:
        if self.valid_time.tzinfo is None:
            raise ValueError("valid_time must be timezone-aware")
        if not self.lead_hours >= 0:
            raise ValueError("lead_hours must be >= 0")
        if not self.reference_paths:
            raise ValueError("at least one clear-day reference frame is needed")

    @property
    def issue_time(self) -> datetime:
        """valid − lead."""
        return self.valid_time - timedelta(hours=self.lead_hours)


def pair_record_key(viewpoint_id: str, pair_id: str) -> str:
    """Storage key of a pair record."""
    return f"scores/pairs/{viewpoint_id}/{pair_id}.json"


def _package_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _git_state() -> dict[str, Any]:
    root = enclosing_git_root(Path(__file__).resolve())
    if root is None:
        return {"commit": None, "dirty": None}
    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None}
    return {"commit": commit, "dirty": bool(dirty)}


def _versions(backend: EphemerisBackend, renderer: str | None) -> dict[str, Any]:
    return {
        "lookout_eval": __version__,
        "lookout_common": getattr(lookout_common, "__version__", None),
        "extractor": EXTRACTOR_VERSION,
        "renderer_version_ts": renderer,
        "ephemeris": f"{backend.name} {backend.version}",
        "python": platform.python_version(),
        "numpy": _package_version("numpy"),
        "scipy": _package_version("scipy"),
        "pillow": _package_version("pillow"),
        "git": _git_state(),
    }


def auto_forecast(
    viewpoint_id: str, issue_time: datetime, valid_time: datetime, *, root: Path
) -> tuple[Path, dict[str, Any]]:
    """Backfill the run whose *modelled* availability precedes the issue time (network).

    Uses lookout-forecast, imported here only: install it with ``uv sync --group forecast``.
    The run is chosen with modelled availability (run start + the p95 live lag per forecast
    hour, PRD §8.3), as for every archived run; the file is written under ``forecast/hrrr/``.
    """
    try:
        from lookout_forecast.availability import LagModel, select_run_for_issue
        from lookout_forecast.bucket import HrrrBucket
        from lookout_forecast.extract import ExtractOptions, Extractor
        from lookout_forecast.hrrr import STORAGE_SOURCE
    except ImportError as exc:
        raise PairError(
            "--forecast auto needs lookout-forecast in this venv: run `uv sync --group forecast`"
        ) from exc
    from lookout_common.manifest import load_manifests

    storage = LocalStorage(root)
    lag_model = LagModel.from_storage(storage)
    choice = select_run_for_issue(issue_time, valid_time, lag_model=lag_model)
    if choice is None:
        raise PairError("no HRRR run covers the valid time with modelled availability by the issue")
    key = forecast_key(STORAGE_SOURCE, viewpoint_id, choice.run_start_utc)
    if not storage.exists(key):
        with HrrrBucket() as bucket:
            Extractor(storage, bucket, load_manifests(), lag_model=lag_model).extract(
                choice.run_start_utc,
                ExtractOptions(viewpoint_ids=(viewpoint_id,), max_fh=choice.fh_hi),
            )
    return storage.path(key), choice.to_dict()


def _truth_time(path: Path) -> datetime:
    try:
        return ReferenceFrame.from_path(path).image_time
    except (ValueError, IndexError) as exc:
        raise PairError(f"cannot tell the image time of the truth frame {path}: {exc}") from exc


def _frame_info(path: Path, data_root: Path) -> tuple[datetime, datetime, str | None]:
    """Image time, availability and viewpoint of a frame file (sidecar, else the key)."""
    try:
        record = record_from_path(path, data_root=data_root)
    except (ValueError, IndexError):
        record = None
    if record is not None:
        return record.image_time, record.available_at, record.viewpoint_id
    try:
        frame = ReferenceFrame.from_path(path)
    except (ValueError, IndexError) as exc:
        raise PairError(
            f"cannot tell when {path} was taken (no sidecar image_time_utc and not a frame key)"
        ) from exc
    sidecar = read_sidecar_for_path(path) or {}
    return frame.image_time, frame.image_time, sidecar.get("viewpoint_id")


def _check_viewpoint(role: str, path: Path, found: str | None, expected: str) -> None:
    if found is not None and found != expected:
        raise PairError(f"{role} {path} is from viewpoint {found}, not {expected}")


def _check_fitted_asset(
    role: str,
    path: Path,
    *,
    viewpoint_id: str,
    issue: datetime,
    valid: datetime,
    held_out: HeldOut,
    time_zone: str,
    data_root: Path,
) -> dict[str, Any]:
    """Refuse a reference frame or plate that a forecast issued at ``issue`` could not use.

    PRD §13.9: fitted assets (plates, clear-day references) come only from development
    frames that precede the held-out season; and nothing may postdate the issue time
    (§13.1). Same-local-day frames are refused too: they share the valid day's weather.
    """
    image_time, available, found = _frame_info(path, data_root)
    _check_viewpoint(role, path, found, viewpoint_id)
    zone = ZoneInfo(time_zone)
    problems = []
    if image_time >= issue:
        problems.append(
            f"taken at {isoformat_z(image_time)}, not before the issue time {isoformat_z(issue)}"
        )
    elif available > issue:
        problems.append(f"available only at {isoformat_z(available)}, after the issue time")
    valid_day = valid.astimezone(zone).date()
    if image_time.astimezone(zone).date() == valid_day:
        problems.append(f"taken on the valid time's local day ({valid_day})")
    bound = held_out.earliest_start
    if held_out.contains(image_time):
        problems.append("held out")
    elif bound is not None and image_time >= bound:
        problems.append(
            f"not before the held-out period that starts {isoformat_z(bound)} (PRD §13.9)"
        )
    if problems:
        raise LeakageError(f"{role} {path}: " + "; ".join(problems))
    return {
        "role": role,
        "path": str(path),
        "image_time_utc": isoformat_z(image_time),
        "available_utc": isoformat_z(available),
        "passed": True,
    }


def _previous_sidecar(
    storage: LocalStorage, truth_path: Path, data_root: Path
) -> dict[str, Any] | None:
    """The sidecar of the capture just before the truth frame (same day or the day before).

    Feeds the Fresh gate; None when the truth frame is outside the data dir or nothing
    earlier is stored within a day.
    """
    try:
        key = truth_path.relative_to(data_root.resolve()).as_posix()
        truth = parse_frame_key(key)
    except ValueError:
        return None
    best: tuple[datetime, str] | None = None
    for day in (truth.image_time - timedelta(days=1), truth.image_time):
        prefix = f"{truth.kind}/{truth.viewpoint_id}/{day:%Y-%m-%d}/"
        for candidate in storage.list_prefix(prefix):
            try:
                parts = parse_frame_key(candidate)
            except ValueError:
                continue
            earlier = parts.variant is None and parts.image_time < truth.image_time
            if earlier and (best is None or parts.image_time > best[0]):
                best = (parts.image_time, candidate)
    if best is None:
        return None
    return storage.read_json(sidecar_key(best[1]), default=None)


def _renderer_validation(scene: dict[str, Any], renderer_dir: Path) -> dict[str, Any]:
    """The renderer's own validateScene() on the scene, or why it could not run."""
    try:
        (issues,) = renderer_validate([scene], renderer_dir)
    except (RendererValidatorUnavailableError, RuntimeError) as exc:
        return {"ok": None, "issues": [], "reason": f"{type(exc).__name__}: {exc}"[:500]}
    return {"ok": not issues, "issues": issues, "reason": None}


def _choose_plate(
    explicit: Path | None,
    references: Sequence[ReferenceFrame],
    sun_of: Callable[[datetime], tuple[float, float]],
    valid_sun: tuple[float, float],
) -> tuple[Path, str]:
    if explicit is not None:
        return explicit, "explicit"

    def gap(frame: ReferenceFrame) -> float:
        alt, az = sun_of(frame.image_time)
        d_az = (az - valid_sun[1] + 180.0) % 360.0 - 180.0
        return (alt - valid_sun[0]) ** 2 + (0.25 * d_az) ** 2

    best = min(references, key=gap)
    return best.path, "the reference frame nearest the valid time's Sun position"


def _landmark(viewpoint: Viewpoint, name: str | None):
    if name is not None:
        return viewpoint.landmark(name)
    return viewpoint.landmarks[0] if viewpoint.landmarks else None


def _render(
    runner: Runner,
    renderer_dir: Path,
    scene_path: Path,
    plate: Path,
    out_png: Path,
    out_meta: Path,
    inputs: PairInputs,
) -> dict[str, Any]:
    command = [
        "pnpm",
        "--dir",
        str(renderer_dir),
        "render",
        "--scene",
        str(scene_path),
        "--plate",
        str(plate),
        "--out",
        str(out_png),
        "--meta",
        str(out_meta),
        "--backend",
        inputs.renderer_backend,
    ]
    if inputs.sky_mask_path is not None:
        command += ["--sky-mask", str(inputs.sky_mask_path)]
    if inputs.depth_path is not None:
        command += ["--depth", str(inputs.depth_path)]
    started = time.monotonic()
    try:
        done = runner(command, capture_output=True, text=True, timeout=inputs.render_timeout_s)
        returncode, stdout, stderr = done.returncode, done.stdout or "", done.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        returncode, stdout, stderr = -1, "", f"{type(exc).__name__}: {exc}"
    elapsed = time.monotonic() - started
    log = out_png.with_name("renderer.log")
    log.write_text(
        f"$ {shlex.join(command)}\n# exit {returncode} after {elapsed:.2f} s\n"
        f"--- stdout\n{stdout}\n--- stderr\n{stderr}\n",
        encoding="utf-8",
    )
    meta = None
    if returncode == 0 and out_meta.is_file():
        meta = json.loads(out_meta.read_text(encoding="utf-8"))
    return {
        "command": command,
        "returncode": returncode,
        "wall_s": round(elapsed, 3),
        "log": str(log),
        "stderr_tail": stderr[-2000:],
        "meta": meta,
        "png": (
            {"path": str(out_png), "sha256": sha256_file(out_png)}
            if returncode == 0 and out_png.is_file()
            else None
        ),
    }


def run_pair(
    inputs: PairInputs,
    *,
    ephemeris: EphemerisBackend | None = None,
    runner: Runner = subprocess.run,
    root: Path | None = None,
    now: Callable[[], datetime] = utcnow,
) -> dict[str, Any]:
    """Run one backtest pair and write its record; returns the record.

    ``status`` is ``scored``, ``scene-only`` (no scene schema yet, or asked for),
    ``scene-rejected`` (by the renderer's validateScene) or ``render-failed``. Inconsistent
    inputs raise :class:`PairError`; a forecast, reference, plate or truth frame that breaks
    the leakage rules raises :class:`LeakageError` (see the module docstring).
    """
    data_root = root if root is not None else data_dir()
    storage = LocalStorage(data_root)
    backend = ephemeris or get_ephemeris()
    viewpoint = load_manifest(inputs.viewpoint_id)
    cam = viewpoint.camera
    issue, valid = inputs.issue_time, inputs.valid_time
    held_out = (
        inputs.held_out
        if inputs.held_out is not None
        else HeldOut.sealed_window(viewpoint.time_zone)
    )

    # -- truth -------------------------------------------------------------------------
    truth_path = inputs.truth_path.expanduser().resolve()
    if not truth_path.is_file():
        raise PairError(f"truth frame {truth_path} does not exist")
    truth_sha = sha256_file(truth_path)
    truth_time = _truth_time(truth_path)
    _check_viewpoint("truth frame", truth_path, _frame_info(truth_path, data_root)[2], viewpoint.id)
    if abs(truth_time - valid) > inputs.truth_tolerance:
        raise PairError(
            f"truth frame time {isoformat_z(truth_time)} is more than {inputs.truth_tolerance} "
            f"from the valid time {isoformat_z(valid)}"
        )
    if held_out.contains(truth_time):
        raise LeakageError(
            f"the truth frame {truth_path} ({isoformat_z(truth_time)}) is held out; held-out "
            "frames are scored only in the sealed scoring (TODO(M4))"
        )
    with Image.open(truth_path) as image:
        truth_size = image.size
    notes: list[str] = []
    if "archive" not in truth_path.parts:
        notes.append("truth frame is not under archive/ (expected for a hindcast pair)")

    # -- forecast ------------------------------------------------------------------------
    auto_choice = None
    if inputs.forecast == "auto":
        forecast_path, auto_choice = auto_forecast(
            inputs.viewpoint_id, issue, valid, root=data_root
        )
    else:
        forecast_path = Path(inputs.forecast).expanduser().resolve()
    run = ForecastRun.load(forecast_path)
    if run.viewpoint_id != viewpoint.id:
        raise PairError(f"forecast file is for {run.viewpoint_id}, not {viewpoint.id}")
    available, source = run.availability(valid)
    if available > issue:
        raise LeakageError(
            f"forecast run {run.run_id} was available at {isoformat_z(available)} ({source}), "
            f"after the issue time {isoformat_z(issue)}"
        )
    fh_lo, fh_hi, weight = run.bracket(valid)

    # -- references and plate ------------------------------------------------------------
    def sun_of(moment: datetime) -> tuple[float, float]:
        return backend.sun_alt_az(moment, cam.lat, cam.lon, cam.elevation_m)

    references = [
        ReferenceFrame.from_path(Path(p).expanduser().resolve()) for p in inputs.reference_paths
    ]
    reference_shas = [sha256_file(r.path) for r in references]
    if truth_sha in reference_shas:
        raise PairError("the truth frame is among the reference frames")
    sun = sun_position(valid, cam.lat, cam.lon, cam.elevation_m, ephemeris=backend)
    moon = moon_position(valid, cam.lat, cam.lon, cam.elevation_m, ephemeris=backend)
    plate_path, plate_source = _choose_plate(
        inputs.plate_path, references, sun_of, (sun.altitude_deg, sun.azimuth_deg)
    )
    plate_path = plate_path.expanduser().resolve()
    plate_sha = sha256_file(plate_path)
    if plate_sha == truth_sha:
        raise PairError("the plate is the truth frame")
    asset_rules = {
        "viewpoint_id": viewpoint.id,
        "issue": issue,
        "valid": valid,
        "held_out": held_out,
        "time_zone": viewpoint.time_zone,
        "data_root": data_root,
    }
    reference_checks = [
        _check_fitted_asset("reference frame", r.path, **asset_rules) for r in references
    ]
    plate_check = _check_fitted_asset("plate", plate_path, **asset_rules)
    plate = PlateIdentity.from_files(
        viewpoint.id,
        plate_path,
        sky_mask=None if inputs.sky_mask_path is None else inputs.sky_mask_path.expanduser(),
        depth=None if inputs.depth_path is None else inputs.depth_path.expanduser(),
    )

    # -- identity ------------------------------------------------------------------------
    digest_input = {
        "viewpoint": viewpoint.id,
        "valid": isoformat_z(valid),
        "lead_h": inputs.lead_hours,
        "truth": truth_sha,
        "forecast": run.sha256,
        "references": sorted(reference_shas),
        "plate": plate_sha,
        "sky_mask": plate.sky_mask_sha256,
        "depth": plate.depth_sha256,
        "region": inputs.region.to_dict(),
        "params": inputs.params.to_dict(),
        "assumptions": inputs.assumptions.to_dict(),
        "eval": __version__,
    }
    digest = hashlib.sha256(json.dumps(digest_input, sort_keys=True).encode()).hexdigest()
    pair_id = f"{valid:%Y%m%dT%H%M%SZ}_L{inputs.lead_hours:g}h_{digest[:10]}"
    out_dir = data_root / "scores" / "pairs" / viewpoint.id / pair_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # -- scene ---------------------------------------------------------------------------
    renderer_dir = (inputs.renderer_dir or find_renderer_dir()).resolve()
    version = renderer_version(renderer_dir)
    scene = build_scene(
        viewpoint=viewpoint,
        valid_time=valid,
        run=run,
        sun=sun,
        moon=moon,
        width=truth_size[0],
        height=truth_size[1],
        renderer_version=version,
        plate=plate,
        assumptions=inputs.assumptions,
        meta={
            "pair_id": pair_id,
            "issue_time_utc": isoformat_z(issue),
            "forecast_hours": [fh_lo, fh_hi],
            "forecast_weight_hi": weight,
            "forecast_available_utc": isoformat_z(available),
            "forecast_availability_source": source,
        },
    )
    scene_path = out_dir / "scene.json"
    scene_path.write_text(json.dumps(scene, indent=2) + "\n", encoding="utf-8")

    record: dict[str, Any] = {
        "kind": RECORD_KIND,
        "schema_version": 1,
        "label": PAIR_LABEL,
        "pair_id": pair_id,
        "created_utc": isoformat_z(now(), "seconds"),
        "status": None,
        "viewpoint_id": viewpoint.id,
        "valid_time_utc": isoformat_z(valid),
        "issue_time_utc": isoformat_z(issue),
        "lead_nominal_h": inputs.lead_hours,
        "lead_from_availability_h": round((valid - available).total_seconds() / 3600.0, 6),
        "lead_from_run_start_h": round((valid - run.run_start).total_seconds() / 3600.0, 6),
        "inputs": {
            "truth": {
                "path": str(truth_path),
                "sha256": truth_sha,
                "image_time_utc": isoformat_z(truth_time),
                "size": list(truth_size),
            },
            "forecast": {
                "mode": "auto" if inputs.forecast == "auto" else "path",
                "path": str(forecast_path),
                "sha256": run.sha256,
                "run": run.run_id,
                "fh_lo": fh_lo,
                "fh_hi": fh_hi,
                "weight_hi": weight,
                "available_utc": isoformat_z(available),
                "availability_source": source,
                "auto_choice": auto_choice,
            },
            "references": [
                {"path": str(r.path), "sha256": s, "image_time_utc": isoformat_z(r.image_time)}
                for r, s in zip(references, reference_shas, strict=True)
            ],
            "plate": {
                "path": str(plate_path),
                "id": plate.id,
                "sha256": plate_sha,
                "source": plate_source,
            },
            "sky_mask": (
                None
                if inputs.sky_mask_path is None
                else {"path": str(inputs.sky_mask_path), "sha256": plate.sky_mask_sha256}
            ),
            "depth": (
                None
                if inputs.depth_path is None
                else {"path": str(inputs.depth_path), "sha256": plate.depth_sha256}
            ),
            "region": inputs.region.to_dict(),
        },
        "leakage_checks": {
            "result": "passed",
            "held_out": held_out.to_dict(),
            "held_out_source": (
                "PairInputs.held_out"
                if inputs.held_out is not None
                else "design P sealed window (PRD §13.2), until the committed split"
            ),
            "truth_held_out": False,
            "forecast_available_by_issue": True,
            "references": reference_checks,
            "plate": plate_check,
            "not_checked": ["sky mask and depth provenance: TODO(M2), the plate builder"],
        },
        "versions": _versions(backend, version),
        "scene": {
            "path": str(scene_path),
            "sha256": sha256_file(scene_path),
            "seed": scene["seed"],
            "run_init_utc": scene["run_init_utc"],
            "flags": scene["meta"]["flags"],
            "renderer_validation": None,
        },
        "gates": None,
        "ephemeris": {
            "sun_alt_deg": sun.altitude_deg,
            "sun_apparent_alt_deg": sun.apparent_altitude_deg,
            "sun_az_deg": sun.azimuth_deg,
            "moon_alt_deg": moon.altitude_deg,
            "moon_az_deg": moon.azimuth_deg,
            "moon_illuminated_fraction": moon.illuminated_fraction,
        },
        "render": None,
        "extractor": None,
        "s_rule": None,
        "notes": notes,
    }

    # -- S_rule, for context (no frames needed) -------------------------------------------
    landmark = _landmark(viewpoint, inputs.landmark)
    if landmark is not None:
        samples, ray_detail = summit_ray(
            run, viewpoint, landmark, valid, camera_height_m_msl=scene["camera"]["elev_m_msl"]
        )
        landmark_point = run.landmark_point(landmark)["id"]
        prediction = s_rule(
            viewpoint_id=viewpoint.id,
            issue_time=issue,
            valid_time=valid,
            forecast_available_time=available,
            ray_samples=samples,
            summit_height_m_msl=landmark.height_m,
            low_cover_pct=run.reading(LOW_CLOUD, landmark_point, valid).value,
            mid_cover_pct=run.reading(MID_CLOUD, landmark_point, valid).value,
        )
        record["s_rule"] = {**prediction.to_dict(), "ray": ray_detail}
    else:
        notes.append("the manifest has no landmark yet, so S_rule is not computed (TODO(M2))")

    # -- schema check, the renderer's own validator, then render ---------------------------
    try:
        validate_scene(scene, renderer_dir)
    except SceneSchemaMissingError:
        record["status"] = "scene-only"
        notes.append("renderer/scene.schema.json does not exist: SceneSpec written, render skipped")
        return _write(storage, record)
    validation = _renderer_validation(scene, renderer_dir)
    record["scene"]["renderer_validation"] = validation
    if validation["ok"] is False:
        record["status"] = "scene-rejected"
        notes.append("the renderer's validateScene() rejected the scene; render skipped")
        return _write(storage, record)
    if inputs.scene_only:
        record["status"] = "scene-only"
        notes.append("scene only, as requested")
        return _write(storage, record)

    render_png = out_dir / "render.png"
    render = _render(
        runner, renderer_dir, scene_path, plate_path, render_png, out_dir / "render.json", inputs
    )
    record["render"] = render
    if render["png"] is None:
        record["status"] = "render-failed"
        return _write(storage, record)

    # -- extractor on truth and render ---------------------------------------------------
    reference = ClearDayReference.build(
        references, region=inputs.region, sun_of=sun_of, params=inputs.params
    )
    truth_sun = sun_of(truth_time)
    truth_result = extract(
        truth_path,
        inputs.region,
        sun_alt_deg=truth_sun[0],
        sun_az_deg=truth_sun[1],
        reference=reference,
    )
    render_result = extract(
        render_png,
        inputs.region,
        sun_alt_deg=sun.altitude_deg,
        sun_az_deg=sun.azimuth_deg,
        reference=reference,
    )
    gates = frame_gates(
        viewpoint=viewpoint,
        image_time=truth_time,
        sidecar=read_sidecar_for_path(truth_path),
        previous_sidecar=_previous_sidecar(storage, truth_path, data_root),
        camera_fault=truth_result.camera_fault,
        ephemeris=backend,
    )
    record["gates"] = {"truth": gates.to_dict()}
    if render_result.camera_fault:
        notes.append(f"the render is a camera-fault frame ({render_result.camera_fault})")
    agree = (
        None
        if not gates.scored or truth_result.p1_class is None or render_result.p1_class is None
        else truth_result.p1_class == render_result.p1_class
    )
    record["extractor"] = {
        "version": EXTRACTOR_VERSION,
        "params": inputs.params.to_dict(),
        "reference": {
            "n": len(reference.entries),
            "entries": [e.to_dict() for e in reference.entries],
        },
        "truth": truth_result.to_dict(),
        "render": render_result.to_dict(),
        "scored": gates.scored,
        "excluded_by": list(gates.excluded_by),
        "agree": agree,
    }
    record["status"] = "scored"
    return _write(storage, record)


def _write(storage: LocalStorage, record: dict[str, Any]) -> dict[str, Any]:
    record["record_path"] = str(
        storage.path(pair_record_key(record["viewpoint_id"], record["pair_id"]))
    )
    storage.write_json(pair_record_key(record["viewpoint_id"], record["pair_id"]), record)
    return record
