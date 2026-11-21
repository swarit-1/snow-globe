"""Frame records and the on-disk frame catalogue (CLAUDE.md "Data layout"; PRD §8.7, §13.9).

A :class:`FrameRecord` is metadata only: viewpoint, image time (UTC), storage key, source
and hash. Pixels and extractor labels are reached only through the leakage guard
(:mod:`.leakage`), never from the record itself.

Keys follow the CLAUDE.md layout:

- ``frames/<viewpoint_id>/<YYYY-MM-DD>/<YYYYMMDDTHHMMSSZ>_<sha256[:12]>.jpg`` (live);
- ``archive/<viewpoint_id>/<YYYY-MM-DD>/<YYYYMMDDTHHMMSSZ>_<assetid[:8]>.jpg`` (NPS archive).

snow-globe-capture also stores variants beside a frame, such as
``<stamp>_<assetid[:8]>_thumbmedium.jpg``; :func:`parse_frame_key` reads the variant and
:func:`scan_frames` returns full frames only unless asked for a variant.

The image time comes from the sidecar's ``image_time_utc`` when present, else from the key's
stamp. *Availability*, the moment a forecaster could have read the frame, is the sidecar's
``fetched_at_utc`` for live frames and the image time for archive frames (an archive frame
was on NPS's live feed at its image time; our backfill fetched it later).

An archive record also keeps NPS's own local times from the sidecar
(``image_create_datetime_raw`` and ``local_time_string``), which the archive guard reads
(:mod:`.heldout`, D-019). Nothing here reads a file under the quarantine folder (D-022):
:func:`scan_frames`, :func:`record_from_path` and :func:`read_sidecar_for_path` raise
:class:`~.heldout.LeakageError` first. That holds for a file or sidecar that only *links*
into the quarantine, and for a case variant of the folder's name. :func:`storage_reader` gives
a :class:`~.leakage.FrameStore` a byte reader that checks again at every read.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from snow_globe_common.paths import sidecar_key, validate_viewpoint_id
from snow_globe_common.storage import Storage
from snow_globe_common.timeutil import parse_iso_utc

from .heldout import (
    LeakageError,
    QuarantineGuard,
    key_quarantine_reason,
    path_quarantine_reason,
)

__all__ = [
    "ARCHIVE_SOURCE",
    "FrameKey",
    "FrameRecord",
    "nps_archive_times",
    "parse_frame_key",
    "read_sidecar_for_path",
    "record_from_path",
    "record_from_sidecar",
    "scan_frames",
    "sha256_file",
    "storage_reader",
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


#: Source of a frame backfilled from the NPS image archive (snow-globe-capture's sidecars).
ARCHIVE_SOURCE = "nps-archive"


@dataclass(frozen=True, slots=True)
class FrameRecord:
    """One frame's metadata. ``available_time`` defaults to the image time.

    ``nps_image_create_datetime`` and ``nps_local_time_string`` are an archive frame's
    ``ImageCreateDateTime`` and ``LocalTimeString`` (metadata, for the archive guard).
    """

    viewpoint_id: str
    image_time: datetime
    key: str
    source: str
    sha256: str | None = None
    available_time: datetime | None = None
    nps_image_create_datetime: str | None = None
    nps_local_time_string: str | None = None

    def __post_init__(self) -> None:
        for name in ("image_time", "available_time"):
            value = getattr(self, name)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{name} must be timezone-aware")

    @property
    def available_at(self) -> datetime:
        """When the frame could first have been read: fetch time (live) or image time."""
        return self.available_time or self.image_time

    @property
    def is_archive(self) -> bool:
        """An NPS archive frame: its key is under ``archive/``, its source says so, or it
        carries NPS's archive times (only capture's archive backfill writes them; live
        sidecars never do). Any one is enough, so the archive guard fails closed."""
        return (
            self.source == ARCHIVE_SOURCE
            or self.key.split("/", 1)[0] == "archive"
            or self.nps_image_create_datetime is not None
            or self.nps_local_time_string is not None
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        out = {
            "viewpoint_id": self.viewpoint_id,
            "image_time_utc": self.image_time.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "key": self.key,
            "source": self.source,
            "sha256": self.sha256,
            "available_utc": self.available_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }
        if self.nps_image_create_datetime is not None or self.nps_local_time_string is not None:
            out["nps_image_create_datetime"] = self.nps_image_create_datetime
            out["nps_local_time_string"] = self.nps_local_time_string
        return out


def _optional_text(side: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = side.get(name)
        if value is not None:
            return str(value)
    return None


def nps_archive_times(sidecar: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
    """An archive sidecar's ``ImageCreateDateTime`` and ``LocalTimeString`` (D-019).

    Capture's names (``image_create_datetime_raw``, ``local_time_string``) or NPS's own;
    None for each that is absent.
    """
    side = sidecar or {}
    return (
        _optional_text(side, "image_create_datetime_raw", "ImageCreateDateTime"),
        _optional_text(side, "local_time_string", "LocalTimeString"),
    )


def record_from_sidecar(key: str, sidecar: Mapping[str, Any] | None) -> FrameRecord:
    """Build a record from a frame key and its (optional) sidecar."""
    parts = parse_frame_key(key)
    side = dict(sidecar or {})
    image_time = (
        parse_iso_utc(side["image_time_utc"]) if side.get("image_time_utc") else parts.image_time
    )
    source = str(side.get("source") or (ARCHIVE_SOURCE if parts.kind == "archive" else "live"))
    fetched = side.get("fetched_at_utc")
    if parts.kind == "frames" and fetched:
        available = max(image_time, parse_iso_utc(fetched))
    else:
        available = image_time
    if side.get("viewpoint_id") not in (None, parts.viewpoint_id):
        raise ValueError(f"sidecar of {key} names viewpoint {side['viewpoint_id']!r}")
    sha = side.get("sha256")
    nps_create, nps_local = nps_archive_times(side)
    return FrameRecord(
        viewpoint_id=parts.viewpoint_id,
        image_time=image_time,
        key=key,
        source=source,
        sha256=str(sha) if sha else None,
        available_time=available,
        nps_image_create_datetime=nps_create,
        nps_local_time_string=nps_local,
    )


def _local_paths(storage: Storage) -> tuple[Path, Callable[[str], Path]] | None:
    """A storage's folder and key-to-path map on local disk (``LocalStorage``); None for S3."""
    root = getattr(storage, "root", None)
    path_of = getattr(storage, "path", None)
    if root is None or not callable(path_of):
        return None
    return Path(root), path_of


def _storage_quarantine_reason(storage: Storage) -> str | None:
    root = getattr(storage, "root", None)
    if root is not None:
        return path_quarantine_reason(root)
    prefix = getattr(storage, "prefix", None)
    if prefix:
        return key_quarantine_reason(str(prefix))
    return None


def _listed_quarantine_reason(
    key: str, local: tuple[QuarantineGuard, Callable[[str], Path]] | None
) -> str | None:
    """Why a listed frame key may not be read (D-022): its name, or, on local disk, where its
    file or its sidecar resolves (a link into the quarantine, a case variant)."""
    reason = key_quarantine_reason(key)
    if reason is None and local is not None:
        guard, path_of = local
        file_path = os.fspath(path_of(key))
        reason = guard.reason(file_path)
        if reason is None and key.endswith(".jpg"):  # the sidecar, as sidecar_key names it
            reason = guard.reason(file_path.removesuffix(".jpg") + ".json")
    return reason


def scan_frames(
    storage: Storage,
    viewpoint_id: str,
    *,
    kinds: Iterable[str] = _KINDS,
    variant: str | None = None,
) -> list[FrameRecord]:
    """Every stored frame of a viewpoint (live and archive), sorted by image time.

    Only full frames by default; ``variant="thumbmedium"`` lists those thumbnails instead.
    Records are metadata: the leakage guard (:mod:`.leakage`) decides which may be read.
    A storage rooted in the quarantine folder is refused (D-022). So is any listed frame
    whose key names the folder, or, on local disk, whose file or sidecar resolves into it
    (for example a symlink from ``frames/`` into ``quarantine/``). The scan raises
    :class:`~.heldout.LeakageError` before that sidecar is read.
    """
    validate_viewpoint_id(viewpoint_id)
    quarantined = _storage_quarantine_reason(storage)
    if quarantined is not None:
        raise LeakageError(f"scan_frames: the storage {storage!r} is quarantined: {quarantined}")
    paths = _local_paths(storage)
    local = None if paths is None else (QuarantineGuard((paths[0],), cache_dirs=True), paths[1])
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
            reason = _listed_quarantine_reason(key, local)
            if reason is not None:
                raise LeakageError(f"scan_frames: {key}: {reason}")
            sidecar = (
                storage.read_json(sidecar_key(key), default=None) if key.endswith(".jpg") else None
            )
            out.append(record_from_sidecar(key, sidecar))
    return sorted(out, key=lambda r: (r.image_time, r.key))


def storage_reader(storage: Storage) -> Callable[[FrameRecord], bytes]:
    """A :class:`~.leakage.FrameStore` byte reader for frames kept in ``storage`` (D-022).

    Every read checks the quarantine again, at the moment of the read: the key by name, and,
    on local disk, the file where it resolves. So a link into ``quarantine/`` made after the
    catalogue was scanned is still refused. Use it as ``FrameStore(..., reader=
    storage_reader(storage))``.
    """
    paths = _local_paths(storage)

    def read(record: FrameRecord) -> bytes:
        reason = key_quarantine_reason(record.key)
        if reason is None and paths is not None:
            root, path_of = paths
            reason = path_quarantine_reason(path_of(record.key), roots=(root,))
        if reason is not None:
            raise LeakageError(f"frame {record.key}: {reason}")
        return storage.get_bytes(record.key)

    return read


def sha256_file(path: str | Path) -> str:
    """Hex SHA-256 of a file's bytes."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _refuse_quarantined(path: Path, roots: Iterable[Path | None] = ()) -> None:
    reason = path_quarantine_reason(path, roots=roots)
    if reason is not None:
        raise LeakageError(f"{path}: {reason}")


def read_sidecar_for_path(
    path: str | Path, *, roots: Iterable[Path | None] = ()
) -> dict[str, Any] | None:
    """The ``.json`` sidecar beside a frame file, if there is one (never in quarantine).

    The sidecar is checked on its own, so one that links into the quarantine is refused even
    when its frame is not. ``roots`` adds data folders to the ones always guarded
    (``$SNOW_GLOBE_DATA_DIR``, ``$LOOKOUT_DATA_DIR``, ``~/snow-globe-data``, ``~/lookout-data``;
    D-022, D-080).
    """
    candidate = Path(path).with_suffix(".json")
    _refuse_quarantined(candidate, roots)
    if not candidate.exists():
        return None
    return json.loads(candidate.read_text(encoding="utf-8"))


def record_from_path(
    path: str | Path, *, data_root: Path | None = None, hash_missing: bool = True
) -> FrameRecord:
    """A record for a frame file on disk, keyed relative to the data dir when it lies inside.

    A quarantined file is refused before anything is read (D-022). With ``hash_missing``
    (the default), a file whose sidecar has no SHA-256 is hashed; pass False to read
    metadata only, as the leakage checks do before a frame may be read.
    """
    file_path = Path(path).expanduser().resolve()
    _refuse_quarantined(file_path, (data_root,))
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
    record = record_from_sidecar(key, read_sidecar_for_path(file_path, roots=(data_root,)))
    if record.sha256 is None and hash_missing:
        record = replace(record, sha256=sha256_file(file_path))
    return record
