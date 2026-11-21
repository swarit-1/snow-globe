"""Data-directory resolution and storage-key builders (CLAUDE.md "Data layout"; PRD §8.7).

Every artefact Snow Globe collects lives outside the repo, in the data dir (:func:`data_dir`):
``$SNOW_GLOBE_DATA_DIR``, else the deprecated ``$LOOKOUT_DATA_DIR``, else ``~/snow-globe-data``
when it exists, else ``~/lookout-data`` when it exists (the folder's name before the rename,
D-080), else ``~/snow-globe-data``. The key builders here return POSIX *relative* keys
such as ``frames/<viewpoint_id>/<YYYY-MM-DD>/<YYYYMMDDTHHMMSSZ>_<sha256[:12]>.jpg``.
The same key is used on local disk (relative to the data dir) and in S3 (relative
to the bucket prefix), so a worker never needs to know where it is running.

Dates in keys are UTC dates of the UTC timestamp in the same key.

The data-dir guard (D-017) compares folders by identity (device and inode), not only by
spelling, so ``~/documents`` on a case-insensitive disk and the firmlinked
``/System/Volumes/Data/Users/...`` path are both recognised as ``~/Documents``.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

from .env import env_get, legacy_name

__all__ = [
    "DEFAULT_DATA_DIR",
    "ENV_ALLOW_SYNCED",
    "ENV_DATA_DIR",
    "LEGACY_DATA_DIR",
    "LEGACY_ENV_DATA_DIR",
    "SYNCED_ROOTS",
    "VIEWPOINT_ID_RE",
    "AmbiguousDataDirError",
    "UnsafeDataDirError",
    "archive_key",
    "cache_dir",
    "capture_log_key",
    "check_data_dir",
    "compact_utc",
    "data_dir",
    "default_data_dir",
    "enclosing_git_root",
    "forecast_key",
    "forecast_log_key",
    "frame_key",
    "local_path",
    "sidecar_key",
    "state_dir",
    "state_key",
    "thumb_key",
    "validate_key",
    "validate_viewpoint_id",
]

ENV_DATA_DIR = "SNOW_GLOBE_DATA_DIR"
#: Still read when ``SNOW_GLOBE_DATA_DIR`` is unset (:mod:`.env`, D-080).
LEGACY_ENV_DATA_DIR = legacy_name(ENV_DATA_DIR)
ENV_ALLOW_SYNCED = "SNOW_GLOBE_ALLOW_SYNCED_DATA"
DEFAULT_DATA_DIR = "~/snow-globe-data"
#: The default before the rename (D-080): used while it exists and the new folder does not.
LEGACY_DATA_DIR = "~/lookout-data"

#: Folders that a sync client mirrors to the cloud on this kind of machine. ``~/Documents``
#: and ``~/Desktop`` carry the iCloud ``com.apple.file-provider-domain-id`` xattr here.
SYNCED_ROOTS: tuple[str, ...] = (
    "~/Documents",
    "~/Desktop",
    "~/Library/Mobile Documents",
    "~/Library/CloudStorage",
)

#: ``<place>.<view>``: lower-case ASCII words joined by hyphens, one dot (CLAUDE.md IDs).
VIEWPOINT_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\.[a-z0-9]+(?:-[a-z0-9]+)*$")
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_HEX_RE = re.compile(r"^[0-9a-f]+$")


class UnsafeDataDirError(RuntimeError):
    """Raised when the data dir would put collected data in git or in a synced folder."""


class AmbiguousDataDirError(UnsafeDataDirError):
    """Raised when no variable names the data dir and both default folders exist apart (D-080).

    Before the rename's live switch the data is in ``~/lookout-data`` and ``~/snow-globe-data``
    does not exist; the switch moves the folder and leaves ``~/lookout-data`` as a symlink to
    ``~/snow-globe-data``. Two different folders mean something created ``~/snow-globe-data``
    early (a build cache, a copied command). Silently preferring it would give this process a
    second NPS fetcher lock, empty rate-limit state and empty logs beside the running loop, so
    nothing is read or written until a variable names the folder meant.
    """


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def enclosing_git_root(path: Path) -> Path | None:
    """Return the nearest ancestor of ``path`` (inclusive) holding a ``.git`` entry, if any."""
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _identity(path: Path) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of an existing path (symlinks followed), else None."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_dev, info.st_ino


def _is_within_by_identity(path: Path, root: Path) -> bool:
    """True if ``root`` exists and ``path`` or one of its existing ancestors *is* ``root``.

    Folder identity, not spelling: on a case-insensitive APFS disk ``~/documents`` is
    ``~/Documents``, and ``/System/Volumes/Data/Users/<me>/Documents`` is the same
    folder through a firmlink. ``Path.resolve()`` keeps both spellings as typed.
    """
    target = _identity(root)
    if target is None:
        return False
    return any(_identity(candidate) == target for candidate in (path, *path.parents))


def check_data_dir(path: Path, *, home: Path | None = None) -> Path:
    """Refuse a data dir inside a git work tree or a cloud-synced folder; return it resolved.

    Implements D-017 and CLAUDE.md "Data":

    - Inside any git work tree (this repo included): always refused. CLAUDE.md forbids
      frames, audio, plates, renders, traces or GRIB files inside the repo.
    - Inside ``~/Documents``, ``~/Desktop`` or another synced root: refused unless
      ``SNOW_GLOBE_ALLOW_SYNCED_DATA=1`` (iCloud would upload gigabytes of frames). A
      path counts as inside when it is spelled under the root, or when it or an
      existing ancestor is the same folder as the root (case variants, firmlinks).
    """
    resolved = path.expanduser().resolve()
    git_root = enclosing_git_root(resolved)
    if git_root is not None:
        raise UnsafeDataDirError(
            f"data dir {resolved} is inside the git work tree {git_root}; "
            f"set {ENV_DATA_DIR} to a folder outside any repo (default {DEFAULT_DATA_DIR})"
        )
    home_dir = (home or Path.home()).resolve()
    if not _truthy(env_get(ENV_ALLOW_SYNCED)):
        for root in SYNCED_ROOTS:
            synced = (home_dir / root.removeprefix("~/")).resolve()
            if _is_within(resolved, synced) or _is_within_by_identity(resolved, synced):
                raise UnsafeDataDirError(
                    f"data dir {resolved} is inside {synced}, which is cloud-synced; "
                    f"use {DEFAULT_DATA_DIR} or set {ENV_ALLOW_SYNCED}=1 to override"
                )
    return resolved


def default_data_dir(home: Path | None = None) -> Path:
    """The data dir when no variable names one: ``~/snow-globe-data`` or ``~/lookout-data``.

    ``~/snow-globe-data`` when it exists, else ``~/lookout-data`` when it exists, else
    ``~/snow-globe-data`` (D-080). So data collected before the rename stays the default
    until it is moved, and after the move (which leaves ``~/lookout-data`` as a symlink to
    the new folder) the new name wins. Symlinks are followed; nothing is created.

    Raises :class:`AmbiguousDataDirError` when both exist and are different folders (by
    device and inode, so either one being a symlink to the other is fine).
    """
    base = home if home is not None else Path.home()
    new = base / DEFAULT_DATA_DIR.removeprefix("~/")
    old = base / LEGACY_DATA_DIR.removeprefix("~/")
    if new.exists():
        if old.exists() and _identity(new) != _identity(old):
            raise AmbiguousDataDirError(
                f"both {new} and {old} exist and are different folders, so the data dir is "
                f"ambiguous (D-080): until the rename's live switch the data is in {old}, and "
                f"after it {old} is a symlink to {new}. Something created {new} early; nothing "
                f"was read or written. Set {ENV_DATA_DIR} to the folder you mean, and tell the "
                "lead which folder is stray (never delete anything under a data dir yourself)"
            )
        return new
    if old.exists():
        return old
    return new


def data_dir() -> Path:
    """Return the checked data dir (D-017, D-080).

    In order: ``$SNOW_GLOBE_DATA_DIR``; else the deprecated ``$LOOKOUT_DATA_DIR``; else
    ``~/snow-globe-data`` if it exists; else ``~/lookout-data`` if it exists; else
    ``~/snow-globe-data`` (:func:`default_data_dir`, which refuses while both folders exist
    apart). A blank variable counts as unset. The directory is not created here; writers
    create what they need.
    """
    raw = (env_get(ENV_DATA_DIR) or "").strip()
    return check_data_dir(Path(raw) if raw else default_data_dir())


def state_dir() -> Path:
    """Return ``<data dir>/state`` (created), for persisted rate-limit and dedupe state."""
    path = data_dir() / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    """Return ``<data dir>/cache`` (created): deletable caches such as GRIB subsets."""
    path = data_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def validate_key(key: str) -> str:
    """Return ``key`` if it is a safe POSIX relative key, else raise ``ValueError``.

    Safe means: non-empty, relative, ``/``-separated, no empty segments and no segment
    that starts with ``.`` (so no ``.``/``..``, and no hidden names: local listings skip
    dot-files such as temp files and ``.DS_Store``, and S3 must list the same keys),
    no backslashes, no NUL, and no trailing slash.
    """
    if not isinstance(key, str) or not key:
        raise ValueError("storage key must be a non-empty string")
    if "\\" in key or "\x00" in key:
        raise ValueError(f"storage key {key!r} contains a backslash or NUL")
    if key.startswith("/") or key.endswith("/"):
        raise ValueError(f"storage key {key!r} must be relative and must not end with '/'")
    for segment in key.split("/"):
        if segment == "":
            raise ValueError(f"storage key {key!r} has an empty segment")
        if segment.startswith("."):
            raise ValueError(f"storage key {key!r} has a segment starting with '.'")
    return key


def local_path(key: str, root: Path | None = None) -> Path:
    """Map a storage key to an absolute path under ``root`` (default: the data dir)."""
    base = root if root is not None else data_dir()
    return base.joinpath(*PurePosixPath(validate_key(key)).parts)


def validate_viewpoint_id(viewpoint_id: str) -> str:
    """Return ``viewpoint_id`` if it matches ``<place>.<view>`` (CLAUDE.md), else raise."""
    if not isinstance(viewpoint_id, str) or not VIEWPOINT_ID_RE.match(viewpoint_id):
        raise ValueError(
            f"invalid viewpoint_id {viewpoint_id!r}; expected e.g. 'point-reyes.beach'"
        )
    return viewpoint_id


def _as_utc(moment: datetime, what: str) -> datetime:
    if not isinstance(moment, datetime):
        raise TypeError(f"{what} must be a datetime, got {type(moment).__name__}")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware (got naive {moment.isoformat()})")
    return moment.astimezone(UTC)


def compact_utc(moment: datetime) -> str:
    """Format an aware datetime as ``YYYYMMDDTHHMMSSZ`` in UTC (sub-seconds dropped)."""
    return _as_utc(moment, "timestamp").strftime("%Y%m%dT%H%M%SZ")


def _utc_date(value: date | datetime, what: str) -> date:
    if isinstance(value, datetime):
        return _as_utc(value, what).date()
    if isinstance(value, date):
        return value
    raise TypeError(f"{what} must be a date or an aware datetime")


def _hashed_image_key(folder: str, viewpoint_id: str, image_time_utc: datetime, sha256: str) -> str:
    validate_viewpoint_id(viewpoint_id)
    digest = sha256.lower() if isinstance(sha256, str) else ""
    if len(digest) < 12 or not _HEX_RE.match(digest):
        raise ValueError("sha256 must be a hex digest of at least 12 characters")
    utc = _as_utc(image_time_utc, "image_time_utc")
    return f"{folder}/{viewpoint_id}/{utc:%Y-%m-%d}/{compact_utc(utc)}_{digest[:12]}.jpg"


def frame_key(viewpoint_id: str, image_time_utc: datetime, sha256: str) -> str:
    """Key of a live frame: ``frames/<vp>/<YYYY-MM-DD>/<YYYYMMDDTHHMMSSZ>_<sha256[:12]>.jpg``.

    ``sha256`` is the lower-case hex digest of the bytes as downloaded (so dedupe works
    before any resize). The date folder is the UTC date of the image time.
    """
    return _hashed_image_key("frames", viewpoint_id, image_time_utc, sha256)


def thumb_key(viewpoint_id: str, image_time_utc: datetime, sha256: str) -> str:
    """Key of a 640 px thumbnail (PRD §8.7 staleness checks and Tier 0).

    ``thumbs/<vp>/<YYYY-MM-DD>/<YYYYMMDDTHHMMSSZ>_<sha256[:12]>.jpg``, UTC image time and
    UTC date folder. Pass the same ``sha256`` as :func:`frame_key`, the digest of the
    bytes as downloaded, so a thumbnail and its full frame share a file name.
    """
    return _hashed_image_key("thumbs", viewpoint_id, image_time_utc, sha256)


def archive_key(viewpoint_id: str, image_time_utc: datetime, asset_id: str) -> str:
    """Key of an NPS archive frame.

    ``archive/<vp>/<YYYY-MM-DD>/<YYYYMMDDTHHMMSSZ>_<assetid[:8]>.jpg``, UTC image time.
    """
    validate_viewpoint_id(viewpoint_id)
    asset = asset_id.lower() if isinstance(asset_id, str) else ""
    head = asset[:8]
    if len(head) < 8 or not re.match(r"^[0-9a-f]{8}$", head):
        raise ValueError(f"asset_id {asset_id!r} must start with 8 hex characters (NPS AssetID)")
    utc = _as_utc(image_time_utc, "image_time_utc")
    return f"archive/{viewpoint_id}/{utc:%Y-%m-%d}/{compact_utc(utc)}_{head}.jpg"


def sidecar_key(key: str) -> str:
    """Return the ``.json`` sidecar key that sits beside a ``.jpg`` frame key."""
    validate_key(key)
    if not key.endswith(".jpg"):
        raise ValueError(f"sidecars belong to .jpg frame keys, got {key!r}")
    return key[: -len(".jpg")] + ".json"


def capture_log_key(day: date | datetime) -> str:
    """Key of the append-only capture log for a UTC day: ``capture_log/<YYYY-MM-DD>.jsonl``."""
    return f"capture_log/{_utc_date(day, 'day'):%Y-%m-%d}.jsonl"


def forecast_key(source: str, viewpoint_id: str, run_start_utc: datetime) -> str:
    """Key of one forecast run for one viewpoint: ``forecast/<source>/<vp>/<YYYYMMDDHH>z.json``.

    ``run_start_utc`` must be on the hour (model cycles are), so two runs never share a key.
    """
    if not isinstance(source, str) or not _SLUG_RE.match(source):
        raise ValueError(f"forecast source {source!r} must be a lower-case slug such as 'hrrr'")
    validate_viewpoint_id(viewpoint_id)
    utc = _as_utc(run_start_utc, "run_start_utc")
    if (utc.minute, utc.second, utc.microsecond) != (0, 0, 0):
        raise ValueError(f"run_start_utc {utc.isoformat()} is not on the hour")
    return f"forecast/{source}/{viewpoint_id}/{utc:%Y%m%d%H}z.json"


def forecast_log_key(day: date | datetime) -> str:
    """Key of the forecast-worker log for a UTC day: ``forecast_log/<YYYY-MM-DD>.jsonl``."""
    return f"forecast_log/{_utc_date(day, 'day'):%Y-%m-%d}.jsonl"


def state_key(name: str) -> str:
    """Key under ``state/``. ``name`` may contain ``/`` (e.g. ``capture/pore.json``)."""
    if not isinstance(name, str) or not name:
        raise ValueError("state name must be a non-empty string")
    for segment in name.split("/"):
        if not _SEGMENT_RE.match(segment) or segment in {".", ".."}:
            raise ValueError(f"state name {name!r} has an unsafe segment {segment!r}")
    return validate_key(f"state/{name}")
