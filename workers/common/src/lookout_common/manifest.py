"""Viewpoint manifests: discovery, schema validation and typed access (PRD §8.2, App. A).

Each viewpoint has ``viewpoints/manifests/<viewpoint_id>.json``, validated against
``viewpoints/schema/viewpoint.schema.json`` (JSON Schema Draft 2020-12) plus a few
cross-field rules a schema cannot express:

- the file name is ``<id>.json`` and ``place_id`` is the part of ``id`` before the dot;
- ``time_zone`` loads as an IANA zone;
- each place has exactly one ``default`` viewpoint;
- no alias is shared by two different places (the parser could not tell them apart);
- each landmark's ``distance_km`` matches the great-circle distance from the camera.

The manifest folder is ``$LOOKOUT_MANIFEST_DIR`` if set, else the first
``viewpoints/manifests`` found walking up from this file (editable installs) or from
the working directory. The schema is ``$LOOKOUT_VIEWPOINT_SCHEMA`` if set, else
``<manifests>/../schema/viewpoint.schema.json``.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from functools import cached_property
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import Draft202012Validator

__all__ = [
    "ENV_MANIFEST_DIR",
    "ENV_SCHEMA",
    "SCHEMA_FILENAME",
    "Camera",
    "Landmark",
    "ManifestError",
    "PoliteCaps",
    "TruthCamera",
    "Viewpoint",
    "great_circle_km",
    "initial_bearing_deg",
    "load_manifest",
    "load_manifests",
    "load_schema",
    "manifest_dir",
    "schema_path",
    "validate_manifest",
]

ENV_MANIFEST_DIR = "LOOKOUT_MANIFEST_DIR"
ENV_SCHEMA = "LOOKOUT_VIEWPOINT_SCHEMA"
SCHEMA_FILENAME = "viewpoint.schema.json"
_EARTH_RADIUS_KM = 6371.0088  # IUGG mean radius
_DISTANCE_TOLERANCE_KM = 0.5
_DISTANCE_TOLERANCE_FRAC = 0.02

Status = Literal["msv", "third", "v1-candidate"]
TruthKind = Literal["ard-aircam", "park-cam"]
Confirmation = Literal["to-request", "requested", "confirmed"]


class ManifestError(ValueError):
    """A manifest failed schema validation or a cross-field rule."""


def great_circle_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in km on a sphere of the IUGG mean Earth radius."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, degrees clockwise from north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(x, y)) % 360.0


def _find_upwards(start: Path) -> Path | None:
    for folder in (start, *start.parents):
        candidate = folder / "viewpoints" / "manifests"
        if candidate.is_dir():
            return candidate
    return None


def manifest_dir() -> Path:
    """Locate the manifest folder (see the module docstring); raise if none is found."""
    env = (os.environ.get(ENV_MANIFEST_DIR) or "").strip()
    if env:
        path = Path(env).expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"{ENV_MANIFEST_DIR}={env} is not a directory")
        return path
    for start in (Path(__file__).resolve().parent, Path.cwd().resolve()):
        found = _find_upwards(start)
        if found is not None:
            return found
    raise FileNotFoundError(f"no viewpoints/manifests folder found; set {ENV_MANIFEST_DIR}")


def schema_path(manifests: Path | None = None) -> Path:
    """Path of the viewpoint JSON schema (``$LOOKOUT_VIEWPOINT_SCHEMA`` or beside the manifests)."""
    env = (os.environ.get(ENV_SCHEMA) or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    base = manifests if manifests is not None else manifest_dir()
    return base.parent / "schema" / SCHEMA_FILENAME


def load_schema(path: Path | None = None) -> dict[str, Any]:
    """Load the schema and check that it is itself a valid Draft 2020-12 schema."""
    schema = json.loads((path or schema_path()).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


def validate_manifest(document: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
    """Raise :class:`ManifestError` listing every schema violation in ``document``."""
    validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    errors = sorted(
        validator.iter_errors(document), key=lambda e: [str(p) for p in e.absolute_path]
    )
    if errors:
        lines = [
            f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in errors
        ]
        raise ManifestError("; ".join(lines))


@dataclass(frozen=True, slots=True)
class Camera:
    """Where the camera is and which way it looks (pose itself is an M2 field)."""

    lat: float
    lon: float
    elevation_m: float
    view_azimuth_deg: float
    position_uncertainty_m: float
    sources: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class PoliteCaps:
    """Collection limits (PRD §15.3): at most N images an hour, polls no faster than N s."""

    max_images_per_hour: int
    poll_interval_s: int
    fetch_on_change: bool


@dataclass(frozen=True, slots=True)
class TruthCamera:
    """The camera feed that supplies truth frames, with its licence facts (PRD §15.1)."""

    operator: str
    kind: TruthKind
    json_url: str | None
    image_url: str
    archive_code: str | None
    archive_since: date | None
    terms_url: str
    terms_checked: date
    ownership_basis: str
    confirmation: Confirmation
    interval_min_nominal: float | None
    fixed_view: bool
    night_mode: str
    polite_caps: PoliteCaps
    publish_cleared: bool
    people_in_frame: bool
    interval_min_measured: float | None = None
    view_label: str | None = None
    image_px: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class Landmark:
    """A surveyed point in view; ``px`` stays None until the M2 pose solve."""

    name: str
    lat: float
    lon: float
    height_m: float
    distance_km: float
    band: str
    px: tuple[float, float] | None


@dataclass(frozen=True)
class Viewpoint:
    """One validated manifest. ``raw`` keeps the full document (M2 fields included)."""

    id: str
    place_id: str
    place: str
    aliases: tuple[str, ...]
    default: bool
    time_zone: str
    status: Status
    camera: Camera
    truth_camera: TruthCamera
    landmarks: tuple[Landmark, ...]
    attribution: str
    path: Path
    raw: Mapping[str, Any] = field(repr=False)

    @cached_property
    def zone(self) -> ZoneInfo:
        """The viewpoint's IANA zone as a ``ZoneInfo``."""
        return ZoneInfo(self.time_zone)

    @property
    def is_msv(self) -> bool:
        """True for the viewpoints the MSV makes claims on (Rainier, Point Reyes)."""
        return self.status == "msv"

    @property
    def has_archive(self) -> bool:
        """True if NPS keeps an image archive for this camera (backfill possible)."""
        return self.truth_camera.archive_code is not None

    def landmark(self, name: str) -> Landmark:
        """Return the landmark called ``name``; raise ``KeyError`` if there is none."""
        for item in self.landmarks:
            if item.name == name:
                return item
        raise KeyError(name)


def _parse_date(value: str | None) -> date | None:
    return None if value is None else date.fromisoformat(value)


def _to_viewpoint(doc: Mapping[str, Any], path: Path) -> Viewpoint:
    cam = doc["camera"]
    truth = doc["truth_camera"]
    caps = truth["polite_caps"]
    return Viewpoint(
        id=doc["id"],
        place_id=doc["place_id"],
        place=doc["place"],
        aliases=tuple(doc["aliases"]),
        default=bool(doc["default"]),
        time_zone=doc["time_zone"],
        status=doc["status"],
        camera=Camera(
            lat=float(cam["lat"]),
            lon=float(cam["lon"]),
            elevation_m=float(cam["elevation_m"]),
            view_azimuth_deg=float(cam["view_azimuth_deg"]),
            position_uncertainty_m=float(cam["position_uncertainty_m"]),
            sources=tuple(cam["sources"]),
        ),
        truth_camera=TruthCamera(
            operator=truth["operator"],
            kind=truth["kind"],
            json_url=truth["json_url"],
            image_url=truth["image_url"],
            archive_code=truth["archive_code"],
            archive_since=_parse_date(truth["archive_since"]),
            terms_url=truth["terms_url"],
            terms_checked=date.fromisoformat(truth["terms_checked"]),
            ownership_basis=truth["ownership_basis"],
            confirmation=truth["confirmation"],
            interval_min_nominal=truth["interval_min_nominal"],
            fixed_view=bool(truth["fixed_view"]),
            night_mode=truth["night_mode"],
            polite_caps=PoliteCaps(
                max_images_per_hour=int(caps["max_images_per_hour"]),
                poll_interval_s=int(caps["poll_interval_s"]),
                fetch_on_change=bool(caps["fetch_on_change"]),
            ),
            publish_cleared=bool(truth["publish_cleared"]),
            people_in_frame=bool(truth["people_in_frame"]),
            interval_min_measured=truth.get("interval_min_measured"),
            view_label=truth.get("view_label"),
            image_px=None if truth.get("image_px") is None else tuple(truth["image_px"]),
        ),
        landmarks=tuple(
            Landmark(
                name=item["name"],
                lat=float(item["lat"]),
                lon=float(item["lon"]),
                height_m=float(item["height_m"]),
                distance_km=float(item["distance_km"]),
                band=item["band"],
                px=None if item.get("px") is None else (float(item["px"][0]), float(item["px"][1])),
            )
            for item in doc["landmarks"]
        ),
        attribution=doc["attribution"],
        path=path,
        raw=doc,
    )


def _check_one(doc: Mapping[str, Any], path: Path) -> None:
    problems: list[str] = []
    if path.stem != doc["id"]:
        problems.append(f"file name {path.name} does not match id {doc['id']!r}")
    if doc["id"].split(".", 1)[0] != doc["place_id"]:
        problems.append(f"place_id {doc['place_id']!r} is not the prefix of id {doc['id']!r}")
    try:
        ZoneInfo(doc["time_zone"])
    except (ZoneInfoNotFoundError, ValueError):
        problems.append(f"time_zone {doc['time_zone']!r} is not an IANA zone")
    cam = doc["camera"]
    for item in doc["landmarks"]:
        computed = great_circle_km(cam["lat"], cam["lon"], item["lat"], item["lon"])
        allowed = max(_DISTANCE_TOLERANCE_KM, _DISTANCE_TOLERANCE_FRAC * computed)
        if abs(computed - item["distance_km"]) > allowed:
            problems.append(
                f"landmark {item['name']!r}: distance_km {item['distance_km']} but the "
                f"camera-to-landmark great-circle distance is {computed:.2f} km"
            )
    if problems:
        raise ManifestError(f"{path.name}: " + "; ".join(problems))


def _check_set(viewpoints: Mapping[str, Viewpoint]) -> None:
    problems: list[str] = []
    defaults: dict[str, list[str]] = {}
    alias_owner: dict[str, str] = {}
    for vp in viewpoints.values():
        defaults.setdefault(vp.place_id, [])
        if vp.default:
            defaults[vp.place_id].append(vp.id)
        for alias in vp.aliases:
            owner = alias_owner.setdefault(alias, vp.place_id)
            if owner != vp.place_id:
                problems.append(f"alias {alias!r} is used by both {owner!r} and {vp.place_id!r}")
    for place_id, ids in sorted(defaults.items()):
        if len(ids) != 1:
            problems.append(f"place {place_id!r} needs exactly one default viewpoint, has {ids}")
    if problems:
        raise ManifestError("; ".join(problems))


def load_manifests(
    directory: str | os.PathLike[str] | None = None, *, schema: Mapping[str, Any] | None = None
) -> dict[str, Viewpoint]:
    """Load, validate and cross-check every ``*.json`` manifest; return them keyed by id.

    Raises :class:`ManifestError` on the first file that fails, naming the file.
    """
    folder = Path(directory).resolve() if directory is not None else manifest_dir()
    if not folder.is_dir():
        raise FileNotFoundError(f"manifest folder {folder} does not exist")
    active_schema = schema if schema is not None else load_schema(schema_path(folder))
    viewpoints: dict[str, Viewpoint] = {}
    for path in sorted(folder.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ManifestError(f"{path.name}: invalid JSON: {exc}") from exc
        try:
            validate_manifest(document, active_schema)
        except ManifestError as exc:
            raise ManifestError(f"{path.name}: {exc}") from None
        _check_one(document, path)
        viewpoint = _to_viewpoint(document, path)
        if viewpoint.id in viewpoints:
            raise ManifestError(f"{path.name}: duplicate id {viewpoint.id!r}")
        viewpoints[viewpoint.id] = viewpoint
    _check_set(viewpoints)
    return viewpoints


def load_manifest(viewpoint_id: str, directory: str | os.PathLike[str] | None = None) -> Viewpoint:
    """Load every manifest (so set-level rules hold) and return the one with ``viewpoint_id``."""
    viewpoints = load_manifests(directory)
    try:
        return viewpoints[viewpoint_id]
    except KeyError:
        raise KeyError(f"no manifest for viewpoint {viewpoint_id!r}") from None


def main(argv: list[str] | None = None) -> int:
    """CLI for CI (``uv run lookout-viewpoints-check [DIR]``): validate every manifest."""
    import sys

    args = sys.argv[1:] if argv is None else argv
    try:
        viewpoints = load_manifests(args[0] if args else None)
    except (ManifestError, FileNotFoundError) as exc:
        print(f"viewpoint manifests: FAILED: {exc}", file=sys.stderr)
        return 1
    for vp in viewpoints.values():
        print(f"ok  {vp.id:<28} {vp.status:<13} {vp.time_zone:<20} {len(vp.aliases)} aliases")
    print(f"viewpoint manifests: {len(viewpoints)} valid")
    return 0
