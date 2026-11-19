"""Frame records and the on-disk frame catalogue (CLAUDE.md "Data layout"; PRD §8.7, §13.9).

A :class:`FrameRecord` is metadata only: viewpoint, image time (UTC), storage key, source
and hash. Pixels and extractor labels are reached only through the leakage guard
(:mod:`.leakage`), never from the record itself.

Keys follow the CLAUDE.md layout:

- ``frames/<viewpoint_id>/<YYYY-MM-DD>/<YYYYMMDDTHHMMSSZ>_<sha256[:12]>.jpg`` (live);
- ``archive/<viewpoint_id>/<YYYY-MM-DD>/<YYYYMMDDTHHMMSSZ>_<assetid[:8]>.jpg`` (NPS archive).

lookout-capture also stores variants beside a frame, such as
``<stamp>_<assetid[:8]>_thumbmedium.jpg``; :func:`parse_frame_key` reads the variant and
:func:`scan_frames` returns full frames only unless asked for a variant.

The image time comes from the sidecar's ``image_time_utc`` when present, else from the key's
stamp. *Availability*, the moment a forecaster could have read the frame, is the sidecar's
``fetched_at_utc`` for live frames and the image time for archive frames (an archive frame
was on NPS's live feed at its image time; our backfill fetched it later).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lookout_common.paths import sidecar_key, validate_viewpoint_id
from lookout_common.storage import Storage
from lookout_common.timeutil import parse_iso_utc

__all__ = [
    "FrameKey",
    "FrameRecord",
    "parse_frame_key",
    "read_sidecar_for_path",
    "record_from_path",
    "record_from_sidecar",
    "scan_frames",
    "sha256_file",
]

_NAME_RE = re.compile(
    r"^(?P<stamp>\d{8}T\d{6}Z)_(?P<tag>[0-9a-f]{8,64})(?:_(?P<variant>[a-z][a-z0-9]*))?"
    r"\.(?:jpg|jpeg|png)$"
)
_KINDS = ("frames", "archive")


@dataclass(frozen=True, slots=True)
class FrameKey:
    """The parts of a frame key."""

    kind: str
    viewpoint_id: str
    image_time: datetime
    tag: str
    variant: str | None = None


def parse_frame_key(key: str) -> FrameKey:
    """Split ``frames/...`` or ``archive/...`` into its parts; raise ``ValueError`` otherwise."""
    parts = key.split("/")
    if len(parts) != 4 or parts[0] not in _KINDS:
        raise ValueError(f"not a frame key: {key!r}")
    kind, viewpoint_id, day, name = parts
    validate_viewpoint_id(viewpoint_id)
    match = _NAME_RE.match(name)
    if not match:
        raise ValueError(
            f"frame file name {name!r} does not match <YYYYMMDDTHHMMSSZ>_<hex>[_<variant>].jpg"
        )
    image_time = datetime.strptime(match["stamp"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    if image_time.strftime("%Y-%m-%d") != day:
        raise ValueError(f"date folder {day} does not match the stamp in {name}")
    return FrameKey(kind, viewpoint_id, image_time, match["tag"], match["variant"])


@dataclass(frozen=True, slots=True)
class FrameRecord:
    """One frame's metadata. ``available_time`` defaults to the image time."""

    viewpoint_id: str
    image_time: datetime
    key: str
    source: str
    sha256: str | None = None
    available_time: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("image_time", "available_time"):
            value = getattr(self, name)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{name} must be timezone-aware")

    @property
    def available_at(self) -> datetime:
        """When the frame could first have been read: fetch time (live) or image time."""
        return self.available_time or self.image_time

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {
            "viewpoint_id": self.viewpoint_id,
            "image_time_utc": self.image_time.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "key": self.key,
            "source": self.source,
            "sha256": self.sha256,
            "available_utc": self.available_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }


def record_from_sidecar(key: str, sidecar: Mapping[str, Any] | None) -> FrameRecord:
    """Build a record from a frame key and its (optional) sidecar."""
    parts = parse_frame_key(key)
    side = dict(sidecar or {})
    image_time = (
        parse_iso_utc(side["image_time_utc"]) if side.get("image_time_utc") else parts.image_time
    )
    source = str(side.get("source") or ("nps-archive" if parts.kind == "archive" else "live"))
    fetched = side.get("fetched_at_utc")
    if parts.kind == "frames" and fetched:
        available = max(image_time, parse_iso_utc(fetched))
    else:
        available = image_time
    if side.get("viewpoint_id") not in (None, parts.viewpoint_id):
        raise ValueError(f"sidecar of {key} names viewpoint {side['viewpoint_id']!r}")
    sha = side.get("sha256")
    return FrameRecord(
        viewpoint_id=parts.viewpoint_id,
        image_time=image_time,
        key=key,
        source=source,
        sha256=str(sha) if sha else None,
        available_time=available,
    )


def scan_frames(
    storage: Storage,
    viewpoint_id: str,
    *,
    kinds: Iterable[str] = _KINDS,
    variant: str | None = None,
) -> list[FrameRecord]:
    """Every stored frame of a viewpoint (live and archive), sorted by image time.

    Only full frames by default; ``variant="thumbmedium"`` lists those thumbnails instead.
    """
    validate_viewpoint_id(viewpoint_id)
    out: list[FrameRecord] = []
    for kind in kinds:
        if kind not in _KINDS:
            raise ValueError(f"unknown frame kind {kind!r}")
        for key in storage.list_prefix(f"{kind}/{viewpoint_id}/"):
            if not key.endswith((".jpg", ".jpeg", ".png")):
                continue
            try:
                parts = parse_frame_key(key)
            except ValueError:
                continue  # not a frame (e.g. a report) or a name outside the layout
            if parts.variant != variant:
                continue
            sidecar = (
                storage.read_json(sidecar_key(key), default=None) if key.endswith(".jpg") else None
            )
            out.append(record_from_sidecar(key, sidecar))
    return sorted(out, key=lambda r: (r.image_time, r.key))


def sha256_file(path: str | Path) -> str:
    """Hex SHA-256 of a file's bytes."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_sidecar_for_path(path: str | Path) -> dict[str, Any] | None:
    """The ``.json`` sidecar beside a frame file, if there is one."""
    import json

    candidate = Path(path).with_suffix(".json")
    if not candidate.exists():
        return None
    return json.loads(candidate.read_text(encoding="utf-8"))


def record_from_path(path: str | Path, *, data_root: Path | None = None) -> FrameRecord:
    """A record for a frame file on disk, keyed relative to the data dir when it lies inside."""
    file_path = Path(path).expanduser().resolve()
    key: str | None = None
    if data_root is not None:
        try:
            key = file_path.relative_to(data_root.resolve()).as_posix()
        except ValueError:
            key = None
    if key is None:
        parts = file_path.parts
        for kind in _KINDS:
            if kind in parts:
                index = len(parts) - 1 - parts[::-1].index(kind)
                key = "/".join(parts[index:])
                break
    if key is None:
        raise ValueError(f"{file_path} is not under frames/ or archive/")
    record = record_from_sidecar(key, read_sidecar_for_path(file_path))
    if record.sha256 is None:
        record = FrameRecord(
            record.viewpoint_id,
            record.image_time,
            record.key,
            record.source,
            sha256_file(file_path),
            record.available_time,
        )
    return record
