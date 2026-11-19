"""SceneSpec construction for the headless renderer (PRD §9.2, §9.5, §13.2 "Every pair renders").

The contract is ``renderer/scene.schema.json`` (Draft 2020-12) and ``validateScene()`` in
``renderer/src/scene.ts`` (renderer 0.2.0 and later). Sun and Moon are supplied by the caller
for now (geometric altitude, azimuth clockwise from north), from oculus-core.

- :func:`validate_scene` checks the JSON schema plus the cross-field rules eval can check
  without the renderer's code (seed, run initialisation and lead, plate hashes).
- :func:`renderer_validate` runs the renderer's own ``validateScene()`` under Node (no
  browser), which also checks the Sun against the renderer's solar formula. The default test
  suite runs it whenever Node 24 is present, so a change in ``scene.ts`` that the schema does
  not yet show still fails here.

**v−1 mapping from HRRR to the renderer's layers** (crude by design; every assumption is
written into ``meta.assumptions``):

- Layer boundaries are HRRR's cloud-cover pressure levels at standard-atmosphere heights:
  low below 642 hPa (3,686 m), mid 642–350 hPa (3,686–8,117 m), high above.
- Low base = HRRR cloud base at the camera cell when it lies in the low band, else 500 m
  above the model terrain; mid base = the cloud base when it lies in the mid band, else
  3,686 m; high base = 8,117 m (or a higher cloud base). Tops are the band top, capped by
  HRRR cloud top when that is lower; thickness is at least 100 m (high: 1,500 m nominal).
- Each layer's cover profile is HRRR's cover at the camera cell and along the view ray every
  3 km to 45 km (16 knots, the schema's maximum).
- Ceiling at the main landmark's point (else the camera's); visibility, precipitation and
  wind at the camera's point.
- Clear-air haze 0.03 km⁻¹ (a ~130 km visual range): TODO(M2), fitted per viewpoint (§9.3).
- Pose: the manifest's ``pose`` if present, else heading = view azimuth, pitch 0, roll 0,
  horizontal FOV 60°: TODO(M2), the pose solve (§8.5).

TODO(M1.5): replace the band mapping with per-layer bases from the model levels.
"""

from __future__ import annotations

import functools
import hashlib
import itertools
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from lookout_common.manifest import Viewpoint
from lookout_common.timeutil import isoformat_z, parse_iso_utc

from .ephemeris import MoonPosition, SunPosition
from .forecast_io import (
    CEILING,
    CLOUD_BASE,
    CLOUD_TOP,
    HIGH_CLOUD,
    LOW_CLOUD,
    MID_CLOUD,
    PRECIP_RATE,
    VISIBILITY,
    ForecastRun,
    ray_points_every,
)
from .frames import sha256_file

__all__ = [
    "LOW_TOP_M",
    "MAX_LEAD_H",
    "MID_TOP_M",
    "NODE_MIN_MAJOR",
    "PlateIdentity",
    "RendererValidatorUnavailableError",
    "SceneAssumptions",
    "SceneSchemaMissingError",
    "build_scene",
    "derive_seed",
    "find_renderer_dir",
    "node_executable",
    "renderer_validate",
    "renderer_version",
    "validate_scene",
]

#: Standard-atmosphere heights of HRRR's 642 hPa and 350 hPa cloud-layer boundaries.
LOW_TOP_M = 3686.0
MID_TOP_M = 8117.0
MAX_KNOTS = 16
#: The renderer accepts valid times 0 to 384 h after the run's initialisation.
MAX_LEAD_H = 384
ENV_RENDERER_DIR = "LOOKOUT_RENDERER_DIR"
ENV_NODE = "LOOKOUT_NODE"
#: Node runs the renderer's TypeScript directly (type stripping, unflagged from 23.6);
#: CLAUDE.md pins Node 24.
NODE_MIN_MAJOR = 24
_VALIDATOR_SCRIPT = Path(__file__).with_name("renderer_validate.mjs")
_ASSET_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class SceneSchemaMissingError(FileNotFoundError):
    """renderer/scene.schema.json does not exist yet: write the SceneSpec and stop."""


class RendererValidatorUnavailableError(RuntimeError):
    """Node 24, or the renderer's ``src/scene.ts``, is not available to validate a scene."""


@dataclass(frozen=True, slots=True)
class PlateIdentity:
    """The plate set a scene is issued for (``scene.plate``): asset id and file hashes.

    The renderer checks each file it is given against these hashes, so the scene, and its
    ``scene_hash``, name the exact plate, sky mask and depth image (PRD §9.5, §10.3).
    """

    id: str
    sha256: str
    sky_mask_sha256: str | None = None
    depth_sha256: str | None = None

    def __post_init__(self) -> None:
        if len(self.id) > 200 or not _ASSET_ID_RE.fullmatch(self.id):
            raise ValueError(f"plate id {self.id!r} is not a renderer asset id")
        for name in ("sha256", "sky_mask_sha256", "depth_sha256"):
            value = getattr(self, name)
            if value is not None and not _SHA256_RE.fullmatch(value):
                raise ValueError(f"plate {name} must be 64 lower-case hex characters")

    @classmethod
    def from_files(
        cls,
        viewpoint_id: str,
        plate: Path,
        *,
        sky_mask: Path | None = None,
        depth: Path | None = None,
        asset_id: str | None = None,
    ) -> PlateIdentity:
        """Hash the files; the id defaults to ``<viewpoint_id>/plate-<sha256[:12]>``."""
        digest = sha256_file(plate)
        return cls(
            id=asset_id or f"{viewpoint_id}/plate-{digest[:12]}",
            sha256=digest,
            sky_mask_sha256=None if sky_mask is None else sha256_file(sky_mask),
            depth_sha256=None if depth is None else sha256_file(depth),
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SceneAssumptions:
    """v−1 choices that a solved pose and fitted assets replace in M2."""

    hfov_deg: float = 60.0
    pitch_deg: float = 0.0
    roll_deg: float = 0.0
    heading_deg: float | None = None  # None: the manifest's view azimuth
    agl_m: float = 10.0
    haze_beta_per_km: float = 0.03
    low_base_default_agl_m: float = 500.0
    high_thickness_m: float = 1500.0
    min_thickness_m: float = 100.0
    knot_step_km: float = 3.0

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return asdict(self)


def derive_seed(viewpoint_id: str, run_init_utc: str) -> str:
    """The renderer's ``deriveSeed``: 16 hex chars of SHA-256 of ``lookout-seed-v1|vp|run``."""
    return hashlib.sha256(f"lookout-seed-v1|{viewpoint_id}|{run_init_utc}".encode()).hexdigest()[
        :16
    ]


def find_renderer_dir(start: Path | None = None) -> Path:
    """``$LOOKOUT_RENDERER_DIR``, else the ``renderer/`` folder found walking up from here."""
    env = (os.environ.get(ENV_RENDERER_DIR) or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    for folder in (start or Path(__file__).resolve()).parents:
        candidate = folder / "renderer"
        if (candidate / "package.json").is_file():
            return candidate
    raise FileNotFoundError(f"no renderer/ folder found; set {ENV_RENDERER_DIR}")


def renderer_version(renderer_dir: Path) -> str:
    """``RENDERER_VERSION`` from ``renderer/src/version.ts``."""
    text = (renderer_dir / "src" / "version.ts").read_text(encoding="utf-8")
    match = re.search(r"RENDERER_VERSION\s*=\s*['\"]([^'\"]+)['\"]", text)
    if not match:
        raise ValueError("RENDERER_VERSION not found in renderer/src/version.ts")
    return match.group(1)


def _cross_field_issues(scene: Mapping[str, Any]) -> list[str]:
    """The renderer's cross-field rules that need no solar formula (see ``validateScene``)."""
    issues: list[str] = []
    try:
        init = parse_iso_utc(str(scene["run_init_utc"]))
        valid = parse_iso_utc(str(scene["valid_time_utc"]))
    except (KeyError, ValueError) as exc:
        return [f"run_init_utc / valid_time_utc unreadable: {exc}"]
    lead = valid - init
    if not timedelta(0) <= lead <= timedelta(hours=MAX_LEAD_H):
        issues.append(f"valid_time_utc must be 0 to {MAX_LEAD_H} h after run_init_utc")
    expected = derive_seed(str(scene.get("viewpoint_id")), str(scene["run_init_utc"]))
    if scene.get("seed") != expected:
        issues.append(f"seed must be deriveSeed(viewpoint_id, run_init_utc) = {expected}")
    for layer in scene.get("forecast", {}).get("layers", []):
        distances = [k["distance_km"] for k in layer.get("cover_profile", [])]
        if any(b <= a for a, b in itertools.pairwise(distances)):
            issues.append(f"layer {layer.get('name')}: cover_profile distances must increase")
    return issues


def validate_scene(scene: Mapping[str, Any], renderer_dir: Path) -> None:
    """Validate against ``renderer/scene.schema.json`` and eval's cross-field checks.

    Raises :class:`SceneSchemaMissingError` without a schema and ``ValueError`` on any
    failure. The cross-field rules checked here: the seed is ``deriveSeed(viewpoint_id,
    run_init_utc)``, the valid time lies 0–384 h after ``run_init_utc``, and cover-profile
    distances increase (mask and depth hashes are in the schema). The renderer's Sun check
    needs :func:`renderer_validate`.
    """
    path = renderer_dir / "scene.schema.json"
    if not path.is_file():
        raise SceneSchemaMissingError(str(path))
    schema = json.loads(path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(scene), key=lambda e: [str(p) for p in e.absolute_path])
    if errors:
        lines = [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in errors]
        raise ValueError("SceneSpec fails renderer/scene.schema.json: " + "; ".join(lines[:10]))
    issues = _cross_field_issues(scene)
    if issues:
        raise ValueError("SceneSpec fails the renderer's cross-field rules: " + "; ".join(issues))


def node_executable() -> str | None:
    """``$LOOKOUT_NODE``, else ``node`` on the PATH, else None."""
    return (os.environ.get(ENV_NODE) or "").strip() or shutil.which("node")


@functools.lru_cache(maxsize=8)
def _node_major(node: str) -> int:
    try:
        done = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=30, check=True
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RendererValidatorUnavailableError(f"cannot run {node} --version: {exc}") from exc
    match = re.match(r"v(\d+)\.", done.stdout.strip())
    if not match:
        raise RendererValidatorUnavailableError(f"unexpected node version {done.stdout!r}")
    return int(match.group(1))


def renderer_validate(
    scenes: Sequence[Mapping[str, Any]],
    renderer_dir: Path,
    *,
    allow_synthetic_sun: bool = False,
    node: str | None = None,
    timeout_s: float = 120.0,
) -> list[list[str]]:
    """Run the renderer's own ``validateScene()`` under Node; one issue list per scene.

    An empty list means the renderer accepts the scene. Needs Node >= 24 (it runs
    ``renderer/src/scene.ts`` by type stripping; no browser and no ``node_modules``) and
    raises :class:`RendererValidatorUnavailableError` without it. ``allow_synthetic_sun``
    skips the renderer's Sun check, for scenes built with a test ephemeris only.
    """
    executable = node or node_executable()
    if executable is None:
        raise RendererValidatorUnavailableError(f"node not found (set {ENV_NODE})")
    major = _node_major(executable)
    if major < NODE_MIN_MAJOR:
        raise RendererValidatorUnavailableError(f"node {major} found; need {NODE_MIN_MAJOR}+")
    if not (renderer_dir / "src" / "scene.ts").is_file():
        raise RendererValidatorUnavailableError(f"{renderer_dir}/src/scene.ts not found")
    with tempfile.TemporaryDirectory(prefix="lookout-eval-scenes-") as folder:
        files = []
        for i, scene in enumerate(scenes):
            path = Path(folder) / f"scene-{i}.json"
            path.write_text(json.dumps(scene), encoding="utf-8")
            files.append(str(path))
        command = [executable, str(_VALIDATOR_SCRIPT), str(renderer_dir)]
        if allow_synthetic_sun:
            command.append("--allow-synthetic-sun")
        try:
            done = subprocess.run(
                [*command, *files], capture_output=True, text=True, timeout=timeout_s, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RendererValidatorUnavailableError(f"{shlex.join(command)}: {exc}") from exc
    if done.returncode != 0:
        raise RuntimeError(
            f"renderer validator exited {done.returncode}: {done.stderr.strip()[-2000:]}"
        )
    results = json.loads(done.stdout)
    if len(results) != len(scenes):
        raise RuntimeError(
            f"renderer validator returned {len(results)} results for {len(scenes)} scenes"
        )
    return [list(item["issues"]) for item in results]


def _pose(viewpoint: Viewpoint, assumptions: SceneAssumptions) -> tuple[dict[str, float], str]:
    pose = viewpoint.raw.get("pose")
    if isinstance(pose, Mapping) and pose.get("heading_deg") is not None:
        return (
            {
                "heading_deg": float(pose["heading_deg"]),
                "pitch_deg": float(pose.get("pitch_deg") or 0.0),
                "roll_deg": float(pose.get("roll_deg") or 0.0),
                "hfov_deg": float(pose.get("fov_deg") or assumptions.hfov_deg),
            },
            "manifest pose",
        )
    heading = (
        assumptions.heading_deg
        if assumptions.heading_deg is not None
        else viewpoint.camera.view_azimuth_deg
    )
    return (
        {
            "heading_deg": float(heading),
            "pitch_deg": assumptions.pitch_deg,
            "roll_deg": assumptions.roll_deg,
            "hfov_deg": assumptions.hfov_deg,
        },
        "nominal: no solved pose in the manifest (TODO(M2))",
    )


def _layers(
    run: ForecastRun, valid_time: datetime, assumptions: SceneAssumptions, flags: list[str]
) -> list[dict[str, Any]]:
    camera_id = run.points_of_kind("camera")[0]["id"]
    terrain = run.point(camera_id).get("model_terrain_m")
    base = run.height_msl(CLOUD_BASE, camera_id, valid_time).value
    top = run.height_msl(CLOUD_TOP, camera_id, valid_time).value
    ground = float(terrain) if terrain is not None else 0.0
    if terrain is None:
        flags.append("camera cell has no model terrain; low base measured from 0 m")
    # name: (default base, band of cloud-base heights this layer accepts, cover field)
    bands = {
        "low": (ground + assumptions.low_base_default_agl_m, (-float("inf"), LOW_TOP_M), LOW_CLOUD),
        "mid": (LOW_TOP_M, (LOW_TOP_M, MID_TOP_M), MID_CLOUD),
        "high": (MID_TOP_M, (MID_TOP_M, float("inf")), HIGH_CLOUD),
    }
    knots_points = ray_points_every(run, assumptions.knot_step_km, MAX_KNOTS)
    layers = []
    for name, (default_base, (lower, upper), field_name) in bands.items():
        layer_base = base if base is not None and lower <= base < upper else default_base
        if name == "high":
            layer_top = layer_base + assumptions.high_thickness_m
        else:
            layer_top = upper
            if top is not None and layer_base < top < upper:
                layer_top = top
        thickness = max(assumptions.min_thickness_m, layer_top - layer_base)
        knots = []
        for point in knots_points:
            reading = run.reading(field_name, point["id"], valid_time)
            if reading.value is None:
                continue
            cover = min(1.0, max(0.0, reading.value / 100.0))
            knots.append({"distance_km": round(float(point["distance_km"]), 6), "cover_0_1": cover})
        if not knots:
            flags.append(f"{name} layer: no cover values; layer omitted")
            continue
        layers.append(
            {
                "name": name,
                "base_m_msl": round(float(layer_base), 3),
                "thickness_m": round(float(thickness), 3),
                "cover_profile": knots,
            }
        )
    return layers


def build_scene(
    *,
    viewpoint: Viewpoint,
    valid_time: datetime,
    run: ForecastRun,
    sun: SunPosition,
    moon: MoonPosition,
    width: int,
    height: int,
    renderer_version: str,
    plate: PlateIdentity,
    assumptions: SceneAssumptions | None = None,
    meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The SceneSpec for one (viewpoint, run, valid time), as a JSON-ready dict.

    ``run_init_utc`` is the run's initialisation in the canonical ``YYYY-MM-DDTHH:MM:SSZ``
    form, and the seed is ``deriveSeed`` of exactly that string. ``plate`` names the plate
    set; ``has_sky_mask`` / ``has_depth`` follow from its hashes.
    """
    chosen = assumptions or SceneAssumptions()
    run_init_utc = isoformat_z(run.run_start, "seconds")
    lead = valid_time - run.run_start
    if not timedelta(0) <= lead <= timedelta(hours=MAX_LEAD_H):
        raise ValueError(
            f"valid time {isoformat_z(valid_time)} is not 0 to {MAX_LEAD_H} h after the run's "
            f"initialisation {run_init_utc}"
        )
    flags: list[str] = []
    pose, pose_source = _pose(viewpoint, chosen)
    camera_id = run.points_of_kind("camera")[0]["id"]
    ceiling_point = camera_id
    if viewpoint.landmarks:
        ceiling_point = run.landmark_point(viewpoint.landmarks[0])["id"]
    ceiling = run.height_msl(CEILING, ceiling_point, valid_time)
    if ceiling.missing not in (None, "null"):
        raise ValueError(f"ceiling unavailable at {ceiling_point} ({ceiling.missing})")
    visibility = run.reading(VISIBILITY, camera_id, valid_time)
    vis_value = visibility.value
    if vis_value is not None and vis_value <= 0.0:
        flags.append(f"visibility {vis_value} m raised to 1 m (the schema needs > 0)")
        vis_value = 1.0
    precip = run.reading(PRECIP_RATE, camera_id, valid_time)
    if precip.value is None:
        flags.append("precipitation rate unavailable; 0 used")
    east, north = run.earth_relative_wind(camera_id, valid_time)
    if east.value is None or north.value is None:
        flags.append("10 m wind unavailable; 0 used")
    layers = _layers(run, valid_time, chosen, flags)
    for reading in (ceiling, visibility, precip, east, north):
        flags.extend(reading.flags)
    plate_spec: dict[str, Any] = {
        "id": plate.id,
        "sha256": plate.sha256,
        "has_sky_mask": plate.sky_mask_sha256 is not None,
        "has_depth": plate.depth_sha256 is not None,
    }
    if plate.sky_mask_sha256 is not None:
        plate_spec["sky_mask_sha256"] = plate.sky_mask_sha256
    if plate.depth_sha256 is not None:
        plate_spec["depth_sha256"] = plate.depth_sha256
    elif viewpoint.landmarks:
        plate_spec["nominal_depth_m"] = round(viewpoint.landmarks[0].distance_km * 1000.0, 1)
    scene_meta = {
        "issued_by": "lookout-eval backtest-pair (v-1)",
        "forecast_run": run.run_id,
        "forecast_file_sha256": run.sha256,
        "pose_source": pose_source,
        "assumptions": chosen.to_dict(),
        "flags": sorted(set(flags)),
        **dict(meta or {}),
    }
    return {
        "viewpoint_id": viewpoint.id,
        "valid_time_utc": isoformat_z(valid_time.astimezone(UTC), "seconds"),
        "run_init_utc": run_init_utc,
        "renderer_version": renderer_version,
        "seed": derive_seed(viewpoint.id, run_init_utc),
        "camera": {
            "width": int(width),
            "height": int(height),
            "lat": viewpoint.camera.lat,
            "lon": viewpoint.camera.lon,
            "elev_m_msl": viewpoint.camera.elevation_m + chosen.agl_m,
            **pose,
            "agl_m": chosen.agl_m,
        },
        "sun": {"alt_deg": sun.altitude_deg, "az_deg": sun.azimuth_deg},
        "moon": {
            "alt_deg": moon.altitude_deg,
            "az_deg": moon.azimuth_deg,
            "illuminated_fraction": min(1.0, max(0.0, moon.illuminated_fraction)),
        },
        "forecast": {
            "layers": layers,
            "ceiling_m_msl": None if ceiling.value is None else round(ceiling.value, 3),
            "visibility_m": None if vis_value is None else round(vis_value, 3),
            "precip_rate_mm_h": round(max(0.0, (precip.value or 0.0) * 3600.0), 6),
            "wind_u_ms": round(east.value or 0.0, 4),
            "wind_v_ms": round(north.value or 0.0, 4),
        },
        "haze": {"beta_per_km": chosen.haze_beta_per_km},
        "plate": plate_spec,
        "meta": scene_meta,
    }
