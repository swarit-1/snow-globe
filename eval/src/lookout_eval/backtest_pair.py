"""One backtest pair, end to end (PRD §16 M0.5: "the pair rendered and scored, labelled v−1").

An archived truth frame and an archived HRRR run become a SceneSpec, a headless render
(``pnpm --dir renderer render``) and two extractor scores, truth and render, with the same
region and clear-day reference. Everything the result depends on is hashed into a pair
record at ``$LOOKOUT_DATA_DIR/scores/pairs/<viewpoint_id>/<name>.json``, with the scene in
``scores/pairs/<viewpoint_id>/<name>/scene.json``. The render, its metadata and the
renderer log go to ``$LOOKOUT_DATA_DIR/renders/pairs/<viewpoint_id>/<name>/``. Nothing goes
in the repo. ``<name>`` is the pair id, plus ``.<tag>`` for a tagged repeat run
(``PairInputs.tag``), which never changes the pair id or the scene.

**The record** names every input with its path, SHA-256, image time and the time it was
available (an archive frame: its image time, when NPS published it; a live frame: when it
was fetched; the forecast: its hours' recorded availability and the rule behind it), the
scene itself, wall times per stage, the extractor's outputs for every landmark box
(:mod:`.landmark_boxes`) and the score (:mod:`.pair_score`): the P1 contingency cell of the
primary box, event "visible", plus the ratio error.

**Registration diagnostic.** The same box is applied to the truth frame, the render (the
relit plate) and the clear-day reference, so the record measures how far the truth frame's
aim differs from the plate's (:mod:`.registration`) and scores the truth again with each box
moved by that shift (``diagnostics``). The PRD §8.6 Same view gate itself stays "not
checked" (TODO(M2)); these numbers only show how much a moved camera could matter.

**Leakage checks** (PRD §13.1, §13.2, §13.9). Any failure raises :class:`LeakageError` (or
:class:`PairError` for inconsistent inputs) before anything is written. Every frame rule is
checked from metadata (the sidecar and the key) before any byte of that frame is read:

- no input lies under the quarantine folder (D-022), whether named in another letter case,
  reached through a link, or found by the folder's identity; under the sealed scoring too.
  That covers every file the pair reads: the frames, the plate, the sky mask and depth image,
  the forecast file, its :mod:`.pair_forecast` summary and the landmark boxes file;
- the held-out periods are the committed split (:mod:`.heldout`, D-019, D-022, D-024) for the
  viewpoint. ``PairInputs.held_out`` can only add periods to it;
- the truth frame is at the valid time (within a tolerance), from the same viewpoint, and not
  held out: not in a held-out period, and not an archived frame past the archive guard
  (dated on or after 1 Jun 2026). Only ``PairInputs.sealed`` (:class:`SealedScoring`, the
  sealed scoring in M4) lifts that. It is also neither a reference frame nor the plate
  (compared by SHA-256);
- the forecast hours the valid time needs were available by the issue time (valid − lead),
  by the file's recorded availability (live ``LastModified`` or modelled);
- every clear-day reference frame and the plate is a fitted asset:
  - taken before the issue time and available by then;
  - not on the valid time's local day;
  - not held out (whether sealed or not);
  - before the fit bound (§13.9), which is the start of the viewpoint's earliest held-out
    period, or of ``PairInputs.design``'s period when the pair is for one design.

  The checks are written into the record;
- with ``--forecast auto``, lookout-forecast picks the newest run whose *modelled*
  availability precedes the issue time and backfills it from NOAA (network).

**Consistency checks** (:class:`PairError`, before anything is written):

- **One view, one aspect ratio.** The plate and every reference frame must have the truth
  frame's aspect ratio (within 1 %), and so must the frame the landmark boxes were drawn on
  (``boxes.drawn_on.size``) and any region given in another frame's pixels. Otherwise a
  normalised box, and the plate the renderer stretches to the truth's size, would cover
  another part of the view: the ARD cameras changed from 3:2 to 16:9 (D-024).
- **The forecast summary** (:mod:`.pair_forecast`), when given, must describe this pair: the
  same forecast file (SHA-256) and run, viewpoint, valid and issue times (its own and its
  choice's), bracketing hours, and the availability the file records for them.

The record names the split (version and SHA-256) and its scoring exclusion, if any: a pair at
Grand Teton, or on 15 Jul 2026 at an ARD camera, is never eligible for scored metrics
(``leakage_checks.scoring_exclusion``, ``extractor.eligible_for_metrics``). No pair counts
toward a metric while the extractor is unvalidated (PRD §13.8): ``counts_toward_metrics`` is
False for every v−1 pair, and ``not_counted_because`` says why.

**Frame quality gates** (§8.6, :mod:`.gates`) are applied to the truth frame and recorded;
a truth frame that fails a gate is not scored (``extractor.agree`` is None).

If ``renderer/scene.schema.json`` does not exist, the pair writes the SceneSpec and stops,
with status ``scene-only``. When Node 24 is present, the renderer's own ``validateScene()``
checks the scene before the browser starts; a rejected scene gets status ``scene-rejected``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import platform
import re
import shlex
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from importlib import metadata
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import lookout_common
from lookout_common.manifest import Viewpoint, load_manifest
from lookout_common.paths import data_dir, enclosing_git_root, sidecar_key
from lookout_common.storage import LocalStorage
from lookout_common.timeutil import isoformat_z, parse_iso_utc, utcnow
from PIL import Image

from . import __version__
from .baselines import s_rule
from .ephemeris import EphemerisBackend, get_ephemeris, moon_position, sun_position
from .extractor import (
    EXTRACTOR_VERSION,
    ClearDayReference,
    ExtractorParams,
    LandmarkResult,
    ReferenceFrame,
    Region,
    classify_ratio,
    edge_stats,
    extract,
)
from .forecast_io import LOW_CLOUD, MID_CLOUD, ForecastRun, summit_ray
from .frames import (
    FrameRecord,
    nps_archive_times,
    parse_frame_key,
    read_sidecar_for_path,
    record_from_path,
    sha256_file,
)
from .gates import frame_gates
from .heldout import DESIGNS, HeldOutSplit, ViewpointSplit, committed_split, path_quarantine_reason
from .landmark_boxes import ViewpointBoxes
from .leakage import HeldOut, LeakageError, SealedScoring
from .pair_score import score_pair
from .registration import estimate_shift
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
    "ASPECT_TOLERANCE",
    "PAIR_LABEL",
    "VALIDATED_EXTRACTORS",
    "PairError",
    "PairInputs",
    "auto_forecast",
    "check_truth_frame",
    "pair_output_name",
    "pair_record_key",
    "pair_render_key",
    "run_pair",
]

PAIR_LABEL = "v-1"
RECORD_KIND = "lookout.backtest-pair"
_TAG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
#: Frames of one view share an aspect ratio to this fraction (as :mod:`.registration` asks).
ASPECT_TOLERANCE = 0.01
#: Extractor versions validated against labels (PRD §13.8). None is yet, so no pair counts
#: toward a metric (TODO(M4): the validation, then its version goes here).
VALIDATED_EXTRACTORS: frozenset[str] = frozenset()
#: A region with this frame size is in normalised coordinates (:mod:`.landmark_boxes`).
_NORMALISED = (1, 1)

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
    #: Extra held-out periods, added to the committed split (they can never remove any).
    held_out: HeldOut | None = None
    #: The design the pair is for; its period sets the fit bound (None: the strictest).
    design: str | None = None
    #: The sealed scoring only (M4): lets the truth frame be a held-out frame.
    sealed: SealedScoring | None = None
    #: Named v−1 landmark boxes (:mod:`.landmark_boxes`). ``region`` must be the primary
    #: box's region; every other box is scored too, and reported beside it.
    boxes: ViewpointBoxes | None = None
    #: A repeat run's label: output names get ``.<tag>``. The pair id and scene do not change.
    tag: str | None = None
    #: :mod:`.pair_forecast`'s JSON summary for the forecast file (provenance of the choice).
    forecast_summary: Path | None = None

    def __post_init__(self) -> None:
        if self.valid_time.tzinfo is None:
            raise ValueError("valid_time must be timezone-aware")
        if not self.lead_hours >= 0:
            raise ValueError("lead_hours must be >= 0")
        if not self.reference_paths:
            raise ValueError("at least one clear-day reference frame is needed")
        if self.design is not None and self.design not in DESIGNS:
            raise ValueError(f"design must be one of {DESIGNS} or None")
        if self.tag is not None and not _TAG_RE.fullmatch(self.tag):
            raise ValueError(
                "tag must be 1-32 characters of a-z, 0-9 and '-', not starting with '-'"
            )
        if self.boxes is not None:
            if self.boxes.viewpoint_id != self.viewpoint_id:
                raise ValueError(
                    f"the boxes are for {self.boxes.viewpoint_id}, not {self.viewpoint_id}"
                )
            if self.region.to_dict() != self.boxes.primary_box.region().to_dict():
                raise ValueError("region must be the primary landmark box's region")

    @property
    def issue_time(self) -> datetime:
        """valid − lead."""
        return self.valid_time - timedelta(hours=self.lead_hours)

    def named_regions(self) -> list[tuple[str, Region]]:
        """The regions to score: the primary first, then the other boxes in file order."""
        if self.boxes is None:
            return [("landmark", self.region)]
        return [(self.boxes.primary, self.region)] + [
            (box.name, box.region()) for box in self.boxes.others
        ]


def pair_output_name(pair_id: str, tag: str | None = None) -> str:
    """The pair id, plus ``.<tag>`` for a tagged repeat run."""
    return pair_id if tag is None else f"{pair_id}.{tag}"


def pair_record_key(viewpoint_id: str, name: str) -> str:
    """Storage key of a pair record (``name`` from :func:`pair_output_name`)."""
    return f"scores/pairs/{viewpoint_id}/{name}.json"


def pair_render_key(viewpoint_id: str, name: str) -> str:
    """Storage prefix of a pair's render, render metadata and renderer log."""
    return f"renders/pairs/{viewpoint_id}/{name}"


class _Stages:
    """Wall time per stage of a pair, in seconds (``time.perf_counter``)."""

    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.seconds: dict[str, float] = {}

    @contextlib.contextmanager
    def __call__(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - start
            self.seconds[name] = round(self.seconds.get(name, 0.0) + elapsed, 4)

    def to_dict(self) -> dict[str, float]:
        """Every stage so far, plus ``total`` since the pair started."""
        return {**self.seconds, "total": round(time.perf_counter() - self.started, 4)}


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
    """Backfill the run whose availability precedes the issue time (network).

    Uses lookout-forecast, imported here only: install it with ``uv sync --group forecast``,
    or run :mod:`.pair_forecast` in lookout-forecast's own environment and pass the file. The
    run is chosen by :func:`.pair_forecast.choose_run`: lookout-forecast's
    ``select_run_for_issue`` with its ``LagModel`` and ``listed_availability``, as for every
    archived run (PRD §8.3): modelled, but never before the run's own S3 stamps (D-054). Only
    the scene's fields and the two bracketing hours are extracted, under
    :data:`.pair_forecast.DEFAULT_MAX_MB` of GRIB (CLAUDE.md "NOAA").
    """
    from .pair_forecast import PairForecastError, fetch_pair_forecast

    try:
        import lookout_forecast  # noqa: F401
    except ImportError as exc:
        raise PairError(
            "--forecast auto needs lookout-forecast in this venv: run `uv sync --group forecast`"
        ) from exc
    storage = LocalStorage(root)
    lead_h = (valid_time - issue_time).total_seconds() / 3600.0
    try:
        summary = fetch_pair_forecast(viewpoint_id, valid_time, lead_h, storage=storage)
    except PairForecastError as exc:
        raise PairError(str(exc)) from exc
    return storage.path(summary["file"]["key"]), summary


def _frame_record(path: Path, data_root: Path, viewpoint_id: str) -> tuple[FrameRecord, str | None]:
    """A frame file's record from metadata only (sidecar and key), and the viewpoint it names.

    No byte of the frame is read. A quarantined file is refused first (D-022). A file outside
    ``frames/`` and ``archive/`` is dated by its sidecar's ``image_time_utc`` or its name, and
    counts as archived when its sidecar says so or carries NPS's archive times.
    """
    quarantined = path_quarantine_reason(path, roots=(data_root,))
    if quarantined is not None:
        raise LeakageError(f"{path}: {quarantined}")
    try:
        record = record_from_path(path, data_root=data_root, hash_missing=False)
    except (ValueError, IndexError):
        record = None
    if record is not None:
        return record, record.viewpoint_id
    try:
        # The sidecar first, checked against the pair's data root too (LeakageError, D-022).
        sidecar = read_sidecar_for_path(path, roots=(data_root,)) or {}
        frame = ReferenceFrame.from_path(path)
    except (ValueError, IndexError) as exc:
        raise PairError(
            f"cannot tell when {path} was taken (no sidecar image_time_utc and not a frame key)"
        ) from exc
    found = sidecar.get("viewpoint_id")
    raw_create, raw_local = nps_archive_times(sidecar)
    record = FrameRecord(
        viewpoint_id=str(found or viewpoint_id),
        image_time=frame.image_time,
        key=str(path),
        source=str(sidecar.get("source") or "unknown"),
        available_time=frame.image_time,
        nps_image_create_datetime=raw_create,
        nps_local_time_string=raw_local,
    )
    return record, found


def _check_viewpoint(role: str, path: Path, found: str | None, expected: str) -> None:
    if found is not None and found != expected:
        raise PairError(f"{role} {path} is from viewpoint {found}, not {expected}")


def _viewpoint_split(
    split: HeldOutSplit, viewpoint: Viewpoint, design: str | None
) -> ViewpointSplit:
    """The viewpoint's committed split, checked against its manifest and the pair's design."""
    vp_split = split.viewpoint(viewpoint.id)
    if vp_split.time_zone != viewpoint.time_zone:
        raise PairError(
            f"{viewpoint.id}: the manifest's time zone {viewpoint.time_zone} differs from the "
            f"committed held-out split's {vp_split.time_zone}"
        )
    if design is not None:
        vp_split.period(design)  # LeakageError when the viewpoint has no such design
    return vp_split


def check_truth_frame(
    path: Path,
    *,
    viewpoint_id: str,
    data_root: Path | None = None,
    held_out: HeldOut | None = None,
    sealed: SealedScoring | None = None,
) -> tuple[FrameRecord, str | None]:
    """The truth frame's leakage checks, from metadata only, before any of its bytes is read.

    Refused with :class:`LeakageError`: a quarantined file (D-022), and, unless ``sealed``, a
    held-out frame under the committed split plus ``held_out``. That covers a held-out
    period and an archived frame past the archive guard. A frame from another viewpoint, or
    a missing file, raises :class:`PairError`. Returns the record and, under the sealed
    scoring, why the frame is held out (else None).
    """
    root = data_root if data_root is not None else data_dir()
    file_path = Path(path).expanduser().resolve()
    quarantined = path_quarantine_reason(file_path, roots=(root,))
    if quarantined is not None:
        raise LeakageError(f"truth frame {file_path}: {quarantined}")
    if not file_path.is_file():
        raise PairError(f"truth frame {file_path} does not exist")
    record, found = _frame_record(file_path, root, viewpoint_id)
    _check_viewpoint("truth frame", file_path, found, viewpoint_id)
    reason = committed_split().held_out_reason(record, extra=held_out)
    if reason is not None and sealed is None:
        raise LeakageError(
            f"the truth frame {file_path} ({isoformat_z(record.image_time)}) is held out: "
            f"{reason}; held-out frames are read only by the sealed scoring "
            "(PairInputs.sealed = SealedScoring(<pre-registration commit>), M4)"
        )
    return record, reason


def _check_fitted_asset(
    role: str,
    path: Path,
    *,
    viewpoint_id: str,
    issue: datetime,
    valid: datetime,
    split: HeldOutSplit,
    extra: HeldOut,
    fit_bound: datetime | None,
    time_zone: str,
    data_root: Path,
) -> tuple[FrameRecord, dict[str, Any]]:
    """Refuse a reference frame or plate that a forecast issued at ``issue`` could not use.

    PRD §13.9: fitted assets (plates, clear-day references) come only from development
    frames that precede the held-out season; and nothing may postdate the issue time
    (§13.1). Same-local-day frames are refused too: they share the valid day's weather.
    Held-out frames (a period, or an archived frame past the guard) are refused even under
    the sealed scoring. Everything is checked from metadata, before any byte is read.
    """
    record, found = _frame_record(path, data_root, viewpoint_id)
    _check_viewpoint(role, path, found, viewpoint_id)
    image_time, available = record.image_time, record.available_at
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
    held = split.held_out_reason(record, extra=extra)
    if held is not None:
        problems.append(f"held out ({held})")
    elif fit_bound is not None and image_time >= fit_bound:
        problems.append(
            f"not before the held-out period that starts {isoformat_z(fit_bound)} (PRD §13.9)"
        )
    if problems:
        raise LeakageError(f"{role} {path}: " + "; ".join(problems))
    return record, {
        "role": role,
        "path": str(path),
        "image_time_utc": isoformat_z(image_time),
        "available_utc": isoformat_z(available),
        "archive": record.is_archive,
        "passed": True,
    }


def _refuse_if_quarantined(role: str, path: Path | None, data_root: Path) -> None:
    """Raise :class:`LeakageError` if ``path`` lies under the quarantine folder (D-022)."""
    if path is None:
        return
    reason = path_quarantine_reason(Path(path).expanduser(), roots=(data_root,))
    if reason is not None:
        raise LeakageError(f"{role} {path}: {reason}")


def _refuse_quarantined_inputs(inputs: PairInputs, data_root: Path) -> None:
    """No input of the pair may come from the quarantine folder (D-022).

    Every file the pair reads is checked before any of them is opened: the frames, the
    plate, the masks, the forecast file, its summary and the landmark boxes file. The boxes
    are already loaded (:func:`.landmark_boxes.load_boxes` checks its file before reading
    it); refusing their source here keeps a hand-built :class:`.ViewpointBoxes` out too.
    """
    paths: list[tuple[str, Path | None]] = [
        ("truth frame", inputs.truth_path),
        *(("reference frame", p) for p in inputs.reference_paths),
        ("plate", inputs.plate_path),
        ("sky mask", inputs.sky_mask_path),
        ("depth image", inputs.depth_path),
        ("forecast summary", inputs.forecast_summary),
        ("landmark boxes", None if inputs.boxes is None else Path(inputs.boxes.source)),
    ]
    if inputs.forecast != "auto":
        paths.append(("forecast", Path(inputs.forecast)))
    for role, path in paths:
        _refuse_if_quarantined(role, path, data_root)


def _held_out_source(split: HeldOutSplit, extra: HeldOut | None) -> str:
    source = f"committed split {split.source} v{split.version} (sha256 {split.sha256[:12]})"
    if extra is not None and extra.intervals:
        source += ", plus extra periods from PairInputs.held_out"
    return source


def _previous_sidecar(
    storage: LocalStorage, truth_path: Path, data_root: Path
) -> dict[str, Any] | None:
    """The sidecar of the capture just before the truth frame (same day or the day before).

    Feeds the Fresh gate; None when the truth frame is outside the data dir or nothing
    earlier is stored within a day. A sidecar that resolves into the quarantine is refused
    before it is read (D-022).
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
    previous = sidecar_key(best[1])
    quarantined = path_quarantine_reason(storage.path(previous), roots=(data_root,))
    if quarantined is not None:
        raise LeakageError(f"the previous capture's sidecar {previous}: {quarantined}")
    return storage.read_json(previous, default=None)


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


def _frame_times(record: FrameRecord, path: Path, data_root: Path) -> dict[str, Any]:
    """A frame input's times from metadata: image time, availability and the rule behind it.

    An archive frame was on NPS's live feed at its image time, so that is when a forecaster
    could have read it; Lookout's own copy was fetched later (``fetched_at_utc``). A live
    frame is available when Lookout fetched it.
    """
    sidecar = read_sidecar_for_path(path, roots=(data_root,)) or {}
    if record.is_archive:
        rule = "archive frame: its NPS image time (it was on the live feed then)"
    elif record.available_time is not None:
        rule = "live frame: fetched_at_utc, when Lookout could first read it"
    else:
        rule = "no fetch time recorded: the image time"
    return {
        "image_time_utc": isoformat_z(record.image_time),
        "available_utc": isoformat_z(record.available_at),
        "available_rule": rule,
        "fetched_at_utc": sidecar.get("fetched_at_utc"),
        "frame_source": record.source,
        "archive": record.is_archive,
    }


_FORECAST_HOUR_FIELDS = (
    "fh",
    "valid_time_utc",
    "availability_utc",
    "availability_source",
    "availability_model",
    "lag_s",
    "grib_last_modified_utc",
    "fetched_utc",
    "bytes_downloaded",
    "url",
)


def _forecast_detail(run: ForecastRun, fh_lo: int, fh_hi: int) -> dict[str, Any]:
    """The bracketing hours' availability (and the rule behind it) and the file's fetch."""
    doc = run.doc
    return {
        "hours": [
            {name: run.hour(fh).get(name) for name in _FORECAST_HOUR_FIELDS}
            for fh in sorted({fh_lo, fh_hi})
        ],
        "live": doc.get("live"),
        "run_start_utc": doc.get("run_start_utc"),
        "forecast_hours_in_file": doc.get("forecast_hours"),
        "field_names": doc.get("field_names"),
        "fetch_started_utc": doc.get("fetch_started_utc"),
        "fetch_finished_utc": doc.get("fetch_finished_utc"),
        "software": doc.get("software"),
    }


def _summary_time(value: Any) -> datetime | None:
    """A summary's timestamp as UTC, or None when it is missing or unreadable."""
    try:
        return parse_iso_utc(str(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _summary_mismatches(
    summary: dict[str, Any],
    *,
    viewpoint_id: str,
    issue: datetime,
    valid: datetime,
    fh_lo: int,
    fh_hi: int,
    available: datetime,
) -> list[str]:
    """How a :mod:`.pair_forecast` summary differs from the pair it is given to (empty: none).

    The summary's ``choice`` is embedded in the record as the provenance of the pair's run,
    so it must be the choice for this pair: the same viewpoint, valid and issue times, hours
    and availability (the later of the bracketing hours') that the forecast file records.
    """
    choice = summary.get("choice") or {}
    problems = []
    if summary.get("viewpoint_id") != viewpoint_id:
        problems.append(f"viewpoint_id {summary.get('viewpoint_id')!r}, not {viewpoint_id}")
    for label, value, want in (
        ("valid_time_utc", summary.get("valid_time_utc"), valid),
        ("issue_time_utc", summary.get("issue_time_utc"), issue),
        ("choice.valid_time_utc", choice.get("valid_time_utc"), valid),
        ("choice.issue_time_utc", choice.get("issue_time_utc"), issue),
        ("choice.availability_utc", choice.get("availability_utc"), available),
    ):
        if _summary_time(value) != want:
            problems.append(f"{label} {value!r}, not {isoformat_z(want)}")
    hours = sorted({fh_lo, fh_hi})
    try:
        listed = sorted(int(fh) for fh in summary.get("hours") or [])
    except (TypeError, ValueError):
        listed = None
    if listed != hours:
        problems.append(f"hours {summary.get('hours')!r}, not {hours}")
    if (choice.get("fh_lo"), choice.get("fh_hi")) != (fh_lo, fh_hi):
        problems.append(
            f"choice hours f{choice.get('fh_lo')}/f{choice.get('fh_hi')}, not f{fh_lo}/f{fh_hi}"
        )
    return problems


def _forecast_summary(
    path: Path | None,
    run: ForecastRun,
    *,
    viewpoint_id: str,
    issue: datetime,
    valid: datetime,
    fh_lo: int,
    fh_hi: int,
    available: datetime,
) -> dict[str, Any] | None:
    """:mod:`.pair_forecast`'s summary for this forecast file, checked against the pair.

    It must name the same file (SHA-256) and run, and describe this pair
    (:func:`_summary_mismatches`); otherwise :class:`PairError`.
    """
    if path is None:
        return None
    file_path = Path(path).expanduser().resolve()
    data = file_path.read_bytes()
    summary = json.loads(data)
    if not isinstance(summary, dict) or summary.get("kind") != "lookout.pair-forecast":
        raise PairError(f"{file_path} is not a lookout.pair-forecast summary")
    named = (summary.get("file") or {}).get("sha256")
    named_run = (summary.get("choice") or {}).get("run")
    if named != run.sha256 or named_run != run.run_id:
        raise PairError(
            f"the forecast summary {file_path} names run {named_run} with file sha256 {named}, "
            f"not this forecast file ({run.run_id}, {run.sha256})"
        )
    problems = _summary_mismatches(
        summary,
        viewpoint_id=viewpoint_id,
        issue=issue,
        valid=valid,
        fh_lo=fh_lo,
        fh_hi=fh_hi,
        available=available,
    )
    if problems:
        raise PairError(
            f"the forecast summary {file_path} is not for this pair: " + "; ".join(problems)
        )
    result = summary.get("result") or {}
    return {
        "path": str(file_path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "choice": summary.get("choice"),
        "fields": summary.get("fields"),
        "max_mb": summary.get("max_mb"),
        "planned_grib_bytes": (summary.get("plan") or {}).get("total_bytes"),
        "transfer": result.get("transfer"),
        "extract_seconds_total": result.get("seconds_total"),
        "reused_existing_file": summary.get("reused_existing_file"),
        "code": summary.get("code"),
        "timings_s": summary.get("timings_s"),
    }


def _normalised_bbox(
    regions: Sequence[tuple[str, Region]], image_size: tuple[int, int]
) -> tuple[float, float, float, float]:
    """The normalised bounding box of every region (the registration window)."""
    xs: list[float] = []
    ys: list[float] = []
    for _, region in regions:
        width, height = region.frame_size or image_size
        for x, y in region.points:
            xs.append(x / width)
            ys.append(y / height)
    return (max(0.0, min(xs)), max(0.0, min(ys)), min(1.0, max(xs)), min(1.0, max(ys)))


def _translated(
    region: Region, dx_norm: float, dy_norm: float, image_size: tuple[int, int]
) -> Region | None:
    """``region`` moved by a normalised shift and clipped to the frame (None if it leaves)."""
    width, height = region.frame_size or image_size
    points = [
        (min(max(x + dx_norm * width, 0.0), width), min(max(y + dy_norm * height, 0.0), height))
        for x, y in region.points
    ]
    try:
        if region.kind == "box":
            (x0, y0), (x1, y1) = points
            return Region.box(x0, y0, x1, y1, frame_size=region.frame_size)
        return Region.polygon(points, frame_size=region.frame_size)
    except ValueError:
        return None


def _aligned_truth(
    truth_path: Path,
    region: Region,
    reference: ClearDayReference,
    truth_sun: tuple[float, float],
    shift: dict[str, Any],
    image_size: tuple[int, int],
    params: ExtractorParams,
) -> dict[str, Any]:
    """The truth frame re-scored with the box moved by the measured shift (a diagnostic)."""
    moved = _translated(region, shift["dx_norm"], shift["dy_norm"], image_size)
    if moved is None:
        return {"region": None, "ratio": None, "state": None, "reason": "box leaves the frame"}
    lookup = reference.lookup(*truth_sun)
    try:
        stats = edge_stats(truth_path, moved, params)
    except ValueError as exc:
        return {"region": moved.to_dict(), "ratio": None, "state": None, "reason": str(exc)}
    ratio = (
        None
        if lookup is None or lookup.region_energy <= 0.0
        else stats.region_energy / lookup.region_energy
    )
    return {
        "region": moved.to_dict(),
        "ratio": ratio,
        "state": (
            None
            if ratio is None
            else classify_ratio(ratio, tau_hi=params.tau_hi, tau_lo=params.tau_lo)
        ),
        "region_energy": stats.region_energy,
        "reason": None if ratio is not None else "no reference in the truth's Sun bin",
    }


def _box_entry(
    region: Region,
    reference: ClearDayReference,
    truth: LandmarkResult,
    render: LandmarkResult,
    truth_scored: bool,
) -> dict[str, Any]:
    """One box's region, clear-day reference and extractor outputs, and whether classes agree."""
    agree = (
        None
        if not truth_scored or truth.p1_class is None or render.p1_class is None
        else truth.p1_class == render.p1_class
    )
    return {
        "region": region.to_dict(),
        "reference": {
            "n": len(reference.entries),
            "entries": [e.to_dict() for e in reference.entries],
        },
        "truth": truth.to_dict(),
        "render": render.to_dict(),
        "agree": agree,
    }


def _image_size(path: Path) -> tuple[int, int]:
    """A frame's (width, height), from its header."""
    with Image.open(path) as image:
        return image.size


def _aspect_problems(
    truth_size: tuple[int, int],
    plate: tuple[Path, tuple[int, int]],
    references: Sequence[tuple[Path, tuple[int, int]]],
    regions: Sequence[tuple[str, Region]],
    boxes: ViewpointBoxes | None,
) -> list[str]:
    """Everything whose aspect ratio differs from the truth frame's by more than 1 %.

    One view needs one aspect ratio: the renderer draws at the truth's size and stretches the
    plate to it, and the clear-day references are measured inside the same (normalised) box.
    The boxes' ``drawn_on.size`` and any region given in another frame's pixels count too; a
    normalised region (frame size 1×1) has no aspect of its own.
    """
    want = truth_size[0] / truth_size[1]

    def differs(size: Sequence[int]) -> bool:
        width, height = size
        return abs(width / height - want) > ASPECT_TOLERANCE * want

    def shown(size: Sequence[int]) -> str:
        return "x".join(str(v) for v in size)

    problems = []
    if differs(plate[1]):
        problems.append(f"the plate {plate[0]} is {shown(plate[1])}")
    for path, size in references:
        if differs(size):
            problems.append(f"the reference frame {path} is {shown(size)}")
    raw = None if boxes is None else boxes.drawn_on.get("size")
    if raw is not None:
        drawn = _frame_size(raw)
        if drawn is None:
            problems.append(f"the landmark boxes' drawn_on.size {raw!r} is not a frame size")
        elif differs(drawn):
            problems.append(f"the landmark boxes were drawn on a {shown(drawn)} frame")
    for name, region in regions:
        size = region.frame_size
        if size is None or tuple(size) == _NORMALISED:
            continue
        if min(size) <= 0:
            problems.append(f"region {name} has the frame size {shown(size)}")
        elif differs(size):
            problems.append(f"region {name} is in the pixels of a {shown(size)} frame")
    return problems


def _frame_size(value: Any) -> tuple[int, int] | None:
    """``[width, height]`` as two positive integers, or None when it is not that."""
    try:
        width, height = (int(v) for v in value)
    except (TypeError, ValueError):
        return None
    return (width, height) if width > 0 and height > 0 else None


def _not_counted_because(
    scored: bool, excluded_by: Sequence[str], exclusion: str | None
) -> list[str]:
    """Why a pair does not count toward a metric (empty: it counts)."""
    reasons = []
    if not scored:
        reasons.append(
            "the truth frame failed a quality gate (PRD §8.6): " + ", ".join(excluded_by)
        )
    if exclusion is not None:
        reasons.append(f"scoring exclusion: {exclusion}")
    if EXTRACTOR_VERSION not in VALIDATED_EXTRACTORS:
        reasons.append(f"extractor {EXTRACTOR_VERSION} is not validated against labels (PRD §13.8)")
    if PAIR_LABEL == "v-1":
        reasons.append("a v-1 pair is a pipeline test (PRD §16 M0.5), not a scored pair")
    return reasons


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
    inputs raise :class:`PairError` (frames of another aspect ratio, a forecast summary for
    another pair, a reused frame), before anything is written; a forecast, reference, plate
    or truth frame that breaks the leakage rules, or any input under the quarantine, raises
    :class:`LeakageError` (see the module docstring), before any byte of a frame is read.
    """
    stages = _Stages()
    data_root = root if root is not None else data_dir()
    storage = LocalStorage(data_root)
    backend = ephemeris or get_ephemeris()
    viewpoint = load_manifest(inputs.viewpoint_id)
    cam = viewpoint.camera
    issue, valid = inputs.issue_time, inputs.valid_time
    regions = inputs.named_regions()

    with stages("leakage_checks"):
        split = committed_split()
        vp_split = _viewpoint_split(split, viewpoint, inputs.design)
        extra = inputs.held_out if inputs.held_out is not None else HeldOut()
        held_out = vp_split.held_out() | extra
        fit_bound = split.fit_bound(viewpoint.id, inputs.design, extra=extra)
        _refuse_quarantined_inputs(inputs, data_root)

        # -- truth: the held-out rules first, from metadata -----------------------------
        truth_path = inputs.truth_path.expanduser().resolve()
        truth_record, truth_held = check_truth_frame(
            truth_path,
            viewpoint_id=viewpoint.id,
            data_root=data_root,
            held_out=extra,
            sealed=inputs.sealed,
        )
        truth_time = truth_record.image_time
        if abs(truth_time - valid) > inputs.truth_tolerance:
            raise PairError(
                f"truth frame time {isoformat_z(truth_time)} is more than "
                f"{inputs.truth_tolerance} from the valid time {isoformat_z(valid)}"
            )
    notes: list[str] = []
    if "archive" not in truth_path.parts:
        notes.append("truth frame is not under archive/ (expected for a hindcast pair)")

    # -- forecast --------------------------------------------------------------------------
    with stages("forecast"):
        auto_choice = None
        if inputs.forecast == "auto":
            forecast_path, auto_choice = auto_forecast(
                inputs.viewpoint_id, issue, valid, root=data_root
            )
            _refuse_if_quarantined("forecast", forecast_path, data_root)
        else:
            forecast_path = Path(inputs.forecast).expanduser().resolve()
        run = ForecastRun.load(forecast_path)
        if run.viewpoint_id != viewpoint.id:
            raise PairError(f"forecast file is for {run.viewpoint_id}, not {viewpoint.id}")
        available, source = run.availability(valid)
        if available > issue:
            raise LeakageError(
                f"forecast run {run.run_id} was available at {isoformat_z(available)} "
                f"({source}), after the issue time {isoformat_z(issue)}"
            )
        fh_lo, fh_hi, weight = run.bracket(valid)
        summary = _forecast_summary(
            inputs.forecast_summary,
            run,
            viewpoint_id=viewpoint.id,
            issue=issue,
            valid=valid,
            fh_lo=fh_lo,
            fh_hi=fh_hi,
            available=available,
        )

    # -- references and plate: the fitted-asset rules, from metadata -------------------------
    def sun_of(moment: datetime) -> tuple[float, float]:
        return backend.sun_alt_az(moment, cam.lat, cam.lon, cam.elevation_m)

    asset_rules: dict[str, Any] = {
        "viewpoint_id": viewpoint.id,
        "issue": issue,
        "valid": valid,
        "split": split,
        "extra": extra,
        "fit_bound": fit_bound,
        "time_zone": viewpoint.time_zone,
        "data_root": data_root,
    }
    with stages("leakage_checks"):
        references: list[ReferenceFrame] = []
        reference_records: list[FrameRecord] = []
        reference_checks: list[dict[str, Any]] = []
        for given in inputs.reference_paths:
            path = Path(given).expanduser().resolve()
            record, check = _check_fitted_asset("reference frame", path, **asset_rules)
            references.append(ReferenceFrame(path, record.image_time))
            reference_records.append(record)
            reference_checks.append(check)
        sun = sun_position(valid, cam.lat, cam.lon, cam.elevation_m, ephemeris=backend)
        moon = moon_position(valid, cam.lat, cam.lon, cam.elevation_m, ephemeris=backend)
        plate_path, plate_source = _choose_plate(
            inputs.plate_path, references, sun_of, (sun.altitude_deg, sun.azimuth_deg)
        )
        plate_path = plate_path.expanduser().resolve()
        plate_record, plate_check = _check_fitted_asset("plate", plate_path, **asset_rules)
        # The Fresh gate's previous capture (metadata, quarantine-checked) before any write.
        previous_sidecar = _previous_sidecar(storage, truth_path, data_root)

    # -- only now are frame bytes read: hashes, identity, size -------------------------------
    with stages("hash_inputs"):
        truth_sha = sha256_file(truth_path)
        reference_shas = [sha256_file(r.path) for r in references]
        if truth_sha in reference_shas:
            raise PairError("the truth frame is among the reference frames")
        plate_sha = sha256_file(plate_path)
        if plate_sha == truth_sha:
            raise PairError("the plate is the truth frame")
        truth_size = _image_size(truth_path)
        plate_size = _image_size(plate_path)
        reference_sizes = [_image_size(r.path) for r in references]
        mismatched = _aspect_problems(
            truth_size,
            (plate_path, plate_size),
            [(r.path, size) for r, size in zip(references, reference_sizes, strict=True)],
            regions,
            inputs.boxes,
        )
        if mismatched:
            raise PairError(
                f"the truth frame is {truth_size[0]}x{truth_size[1]}, but "
                + "; ".join(mismatched)
                + ": one view needs one aspect ratio, within "
                + f"{ASPECT_TOLERANCE:.0%} (D-024: the 3:2 and 16:9 epochs are different views)"
            )
        plate = PlateIdentity.from_files(
            viewpoint.id,
            plate_path,
            sky_mask=None if inputs.sky_mask_path is None else inputs.sky_mask_path.expanduser(),
            depth=None if inputs.depth_path is None else inputs.depth_path.expanduser(),
        )
    scoring_exclusion = split.scoring_exclusion_reason(
        viewpoint.id, valid
    ) or split.scoring_exclusion_reason(viewpoint.id, truth_time)

    # -- identity --------------------------------------------------------------------------
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
    if inputs.boxes is not None:
        # Only when boxes are named, so pairs without them keep their ids.
        digest_input["boxes"] = [[name, region.to_dict()] for name, region in regions]
    digest = hashlib.sha256(json.dumps(digest_input, sort_keys=True).encode()).hexdigest()
    pair_id = f"{valid:%Y%m%dT%H%M%SZ}_L{inputs.lead_hours:g}h_{digest[:10]}"
    name = pair_output_name(pair_id, inputs.tag)
    out_dir = data_root / "scores" / "pairs" / viewpoint.id / name
    render_dir = storage.path(pair_render_key(viewpoint.id, name))
    out_dir.mkdir(parents=True, exist_ok=True)

    # -- scene -----------------------------------------------------------------------------
    with stages("scene_build"):
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

    truth_times = _frame_times(truth_record, truth_path, data_root)
    record: dict[str, Any] = {
        "kind": RECORD_KIND,
        "schema_version": 1,
        "label": PAIR_LABEL,
        "pair_id": pair_id,
        "tag": inputs.tag,
        "output_name": name,
        "created_utc": isoformat_z(now(), "seconds"),
        "status": None,
        "viewpoint_id": viewpoint.id,
        "valid_time_utc": isoformat_z(valid),
        "issue_time_utc": isoformat_z(issue),
        "lead_nominal_h": inputs.lead_hours,
        "lead_from_availability_h": round((valid - available).total_seconds() / 3600.0, 6),
        "lead_from_run_start_h": round((valid - run.run_start).total_seconds() / 3600.0, 6),
        "outputs": {
            "record": str(storage.path(pair_record_key(viewpoint.id, name))),
            "scene": str(scene_path),
            "render_dir": str(render_dir),
        },
        "inputs": {
            "truth": {
                "path": str(truth_path),
                "sha256": truth_sha,
                **truth_times,
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
                **_forecast_detail(run, fh_lo, fh_hi),
                "auto_choice": auto_choice,
                "summary": summary,
            },
            "references": [
                {
                    "path": str(r.path),
                    "sha256": s,
                    **_frame_times(rec, r.path, data_root),
                    "size": list(size),
                }
                for r, s, rec, size in zip(
                    references, reference_shas, reference_records, reference_sizes, strict=True
                )
            ],
            "plate": {
                "path": str(plate_path),
                "id": plate.id,
                "sha256": plate_sha,
                "source": plate_source,
                **_frame_times(plate_record, plate_path, data_root),
                "size": list(plate_size),
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
            "boxes": None if inputs.boxes is None else inputs.boxes.to_record(),
        },
        "leakage_checks": {
            "result": "passed",
            "split": split.to_record(),
            "design": inputs.design,
            "held_out": held_out.to_dict(),
            "held_out_source": _held_out_source(split, inputs.held_out),
            "fit_bound_utc": None if fit_bound is None else isoformat_z(fit_bound),
            "sealed_scoring": (
                None if inputs.sealed is None else inputs.sealed.preregistration_commit
            ),
            "truth_held_out": truth_held is not None,
            "truth_held_out_reason": truth_held,
            "truth_archive": truth_record.is_archive,
            "quarantine": "no input is under the quarantine folder (D-022)",
            "scoring_exclusion": scoring_exclusion,
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
            "document": scene,
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
        "score": None,
        "diagnostics": None,
        "s_rule": None,
        "timings_s": None,
        "notes": notes,
    }

    # -- S_rule, for context (no frames needed) -------------------------------------------
    landmark = _landmark(viewpoint, inputs.landmark)
    if landmark is not None:
        with stages("s_rule"):
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
        with stages("scene_schema_check"):
            validate_scene(scene, renderer_dir)
    except SceneSchemaMissingError:
        record["status"] = "scene-only"
        notes.append("renderer/scene.schema.json does not exist: SceneSpec written, render skipped")
        return _write(storage, record, stages)
    with stages("scene_renderer_check"):
        validation = _renderer_validation(scene, renderer_dir)
    record["scene"]["renderer_validation"] = validation
    if validation["ok"] is False:
        record["status"] = "scene-rejected"
        notes.append("the renderer's validateScene() rejected the scene; render skipped")
        return _write(storage, record, stages)
    if inputs.scene_only:
        record["status"] = "scene-only"
        notes.append("scene only, as requested")
        return _write(storage, record, stages)

    render_dir.mkdir(parents=True, exist_ok=True)
    render_png = render_dir / "render.png"
    with stages("render"):
        render = _render(
            runner,
            renderer_dir,
            scene_path,
            plate_path,
            render_png,
            render_dir / "render.json",
            inputs,
        )
    record["render"] = render
    if render["png"] is None:
        record["status"] = "render-failed"
        return _write(storage, record, stages)

    # -- extractor on truth and render, for every box ------------------------------------
    truth_sun = sun_of(truth_time)
    with stages("clear_day_reference"):
        reference_of = {
            name: ClearDayReference.build(
                references, region=region, sun_of=sun_of, params=inputs.params
            )
            for name, region in regions
        }
    with stages("extract_truth"):
        truth_of = {
            name: extract(
                truth_path,
                region,
                sun_alt_deg=truth_sun[0],
                sun_az_deg=truth_sun[1],
                reference=reference_of[name],
            )
            for name, region in regions
        }
    with stages("extract_render"):
        render_of = {
            name: extract(
                render_png,
                region,
                sun_alt_deg=sun.altitude_deg,
                sun_az_deg=sun.azimuth_deg,
                reference=reference_of[name],
            )
            for name, region in regions
        }
    primary = regions[0][0]
    truth_result, render_result = truth_of[primary], render_of[primary]

    with stages("gates"):
        gates = frame_gates(
            viewpoint=viewpoint,
            image_time=truth_time,
            sidecar=read_sidecar_for_path(truth_path, roots=(data_root,)),
            previous_sidecar=previous_sidecar,
            camera_fault=truth_result.camera_fault,
            ephemeris=backend,
        )
    record["gates"] = {"truth": gates.to_dict()}
    for name in render_of:
        if render_of[name].camera_fault:
            notes.append(
                f"box {name}: the render is a camera-fault frame ({render_of[name].camera_fault})"
            )
    boxes = {
        name: _box_entry(region, reference_of[name], truth_of[name], render_of[name], gates.scored)
        for name, region in regions
    }
    not_counted = _not_counted_because(gates.scored, gates.excluded_by, scoring_exclusion)
    record["extractor"] = {
        "version": EXTRACTOR_VERSION,
        "params": inputs.params.to_dict(),
        "primary_box": primary,
        "reference": boxes[primary]["reference"],
        "truth": truth_result.to_dict(),
        "render": render_result.to_dict(),
        "scored": gates.scored,
        "excluded_by": list(gates.excluded_by),
        # The split and the quality gates allow the pair to count ...
        "eligible_for_metrics": gates.scored and scoring_exclusion is None,
        # ... and it does only with a validated extractor (PRD §13.8): never in v-1.
        "counts_toward_metrics": not not_counted,
        "not_counted_because": not_counted,
        "agree": boxes[primary]["agree"],
        "boxes": boxes,
    }
    with stages("score"):
        per_box = {
            name: score_pair(truth_of[name], render_of[name], truth_scored=gates.scored)
            for name, _ in regions
        }
    record["score"] = {
        "label": PAIR_LABEL,
        "primary_box": primary,
        **per_box[primary],
        "eligible_for_metrics": record["extractor"]["eligible_for_metrics"],
        "counts_toward_metrics": record["extractor"]["counts_toward_metrics"],
        "boxes": per_box,
    }

    # -- diagnostics: how far the truth frame's aim differs from the plate's ----------------
    diagnostics: dict[str, Any] = {"registration": None, "aligned_truth": {}, "note": None}
    with stages("registration"):
        try:
            shift = estimate_shift(
                plate_path,
                truth_path,
                window=_normalised_bbox(regions, truth_size),
                analysis_width=inputs.params.analysis_width,
            ).to_dict()
        except ValueError as exc:
            shift = None
            diagnostics["note"] = f"registration not measured: {exc}"
    if shift is not None:
        diagnostics["registration"] = shift
        diagnostics["note"] = (
            "The PRD §8.6 Same view gate is not applied in v-1 (TODO(M2)). The truth frame is "
            "re-scored with each box moved by the measured shift, to show how much the aim "
            "difference matters; the score above uses the unmoved boxes."
        )
        with stages("aligned_truth"):
            diagnostics["aligned_truth"] = {
                name: _aligned_truth(
                    truth_path,
                    region,
                    reference_of[name],
                    truth_sun,
                    shift,
                    truth_size,
                    inputs.params,
                )
                for name, region in regions
            }
    record["diagnostics"] = diagnostics
    record["status"] = "scored"
    return _write(storage, record, stages)


def _write(storage: LocalStorage, record: dict[str, Any], stages: _Stages) -> dict[str, Any]:
    key = pair_record_key(record["viewpoint_id"], record["output_name"])
    record["record_path"] = str(storage.path(key))
    record["timings_s"] = stages.to_dict()
    storage.write_json(key, record)
    return record
