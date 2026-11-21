"""Which live frames are kept at full size, and how the hourly ones are chosen (PRD §8.7).

PRD §8.7 keeps the frame nearest each hour at <= 1920 px plus every frame as a 640 px
thumbnail. The lead's decision for M0.5/M1 (19 Nov 2026), with one exception:

- **Every frame** (``"every"``): ``rainier.paradise-mountain`` and ``point-reyes.beach``,
  the two design-P cameras (D-024), keep every downloaded frame at <= 1920 px in
  ``frames/``, as before.
- **Hourly** (``"hourly-nearest"``): the other six keep at <= 1920 px only the frame
  nearest each UTC hour. Each download is staged at full size under
  ``cache/staging/frames/<id>/<UTC date>/<stamp>_<sha12>.jpg`` (+ ``.json``). Once the hour's
  candidates are all in, the nearest is promoted to ``frames/`` with its sidecar and the
  other staged copies are deleted (capture's own cache files).
- **Thumbnails, every camera:** every downloaded frame also gets a 640 px JPEG with its
  sidecar at ``thumbs/<id>/<UTC date>/<stamp>_<sha12>.jpg``
  (:func:`snow_globe_common.paths.thumb_key`).

The hourly rule, exactly:

- A frame belongs to the UTC hour nearest its image time (the stamp in its key): hour ``H``
  takes image times in ``[H - 30 min, H + 30 min)``.
- Hour ``H`` is decided once ``now >= H + 30 min + HOURLY_SETTLE_S`` (30 min): a camera's
  feed shows only its newest image, and an ARD frame is on the feed about 7 min after its
  image time and polled within 5.5 min, so no candidate can still be on its way.
- The decodable candidate nearest ``H`` wins; ties go to the earlier image time.
- If ``frames/`` already holds a frame in the hour's window (older capture code kept every
  frame; or a crash interrupted a promotion, which is then completed), that frame stays and
  the staged candidates are dropped: ``frames/`` is append-only.

Nothing under ``frames/``, ``archive/`` or ``thumbs/`` is ever deleted; :func:`delete_staged`
refuses any key outside ``cache/staging/``. A crash loses nothing: staged copies persist,
crash orphans are found by listing the staging folder, and every step is idempotent.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from snow_globe_common.paths import sidecar_key, validate_key
from snow_globe_common.storage import KeyNotFoundError, LocalStorage, S3Storage, Storage
from snow_globe_common.timeutil import isoformat_z

from .frames import Thumbnail

__all__ = [
    "EVERY_FRAME_VIEWPOINTS",
    "FILE_FIELDS",
    "HOURLY_SETTLE_S",
    "POLICY_EVERY",
    "POLICY_HOURLY",
    "STAGING_PREFIX",
    "STAGING_SCAN_EVERY_S",
    "Promotion",
    "delete_staged",
    "frame_key_of_staged",
    "frame_policy",
    "frame_sidecar_from_thumb",
    "frames_in_window",
    "hour_slot",
    "key_stamp",
    "promote_ready_hours",
    "scan_staged",
    "staging_key",
    "thumb_key_of_frame",
    "thumb_sidecar",
]

POLICY_EVERY = "every"
POLICY_HOURLY = "hourly-nearest"
#: Lead decision, 19 Nov 2026: the two design-P cameras (D-024) keep every downloaded frame.
EVERY_FRAME_VIEWPOINTS: frozenset[str] = frozenset(
    {"rainier.paradise-mountain", "point-reyes.beach"}
)
STAGING_PREFIX = "cache/staging/"
#: How long after an hour's window closes it is decided (see the module docstring).
HOURLY_SETTLE_S = 1800.0
#: With S3 storage the staging folder is listed this often (locally: every pass); the state's
#: own index covers the rest, so a pass costs no LIST request.
STAGING_SCAN_EVERY_S = 6 * 3600.0
_HALF_HOUR = timedelta(minutes=30)
#: The sidecar fields that describe the file beside it (the rest describe the capture).
FILE_FIELDS: tuple[str, ...] = (
    "stored",
    "stored_width",
    "stored_height",
    "stored_bytes",
    "stored_sha256",
    "jpeg_quality",
)


def frame_policy(viewpoint_id: str, every: Iterable[str] = EVERY_FRAME_VIEWPOINTS) -> str:
    """``"every"`` for the design-P cameras, ``"hourly-nearest"`` for the others (PRD §8.7)."""
    return POLICY_EVERY if viewpoint_id in set(every) else POLICY_HOURLY


def staging_key(frame_key: str) -> str:
    """``frames/<id>/...jpg`` -> ``cache/staging/frames/<id>/...jpg`` (same file name)."""
    validate_key(frame_key)
    if not frame_key.startswith("frames/"):
        raise ValueError(f"not a frame key: {frame_key!r}")
    return STAGING_PREFIX + frame_key


def frame_key_of_staged(key: str) -> str:
    """The ``frames/`` key a staged copy is promoted to."""
    if not key.startswith(STAGING_PREFIX + "frames/"):
        raise ValueError(f"not a staged frame key: {key!r}")
    return key[len(STAGING_PREFIX) :]


def thumb_key_of_frame(frame_key: str) -> str:
    """``frames/<id>/<date>/<name>`` -> ``thumbs/<id>/<date>/<name>`` (PRD §8.7 pairing)."""
    if not frame_key.startswith("frames/"):
        raise ValueError(f"not a frame key: {frame_key!r}")
    return "thumbs/" + frame_key[len("frames/") :]


def key_stamp(key: str) -> datetime | None:
    """The UTC image time in a key's file name (``YYYYMMDDTHHMMSSZ_...``), or None."""
    name = key.rsplit("/", 1)[-1]
    try:
        return datetime.strptime(name[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def hour_slot(moment: datetime) -> datetime:
    """The UTC hour nearest ``moment``: ``H`` for ``[H - 30 min, H + 30 min)``."""
    utc = moment.astimezone(UTC)
    base = utc.replace(minute=0, second=0, microsecond=0)
    return base + timedelta(hours=1) if utc - base >= _HALF_HOUR else base


def thumb_sidecar(
    frame_sidecar: Mapping[str, Any],
    thumb: Thumbnail,
    *,
    frame_key: str,
    policy: str,
    staged_key: str | None = None,
) -> dict[str, Any]:
    """The thumbnail's sidecar: the capture's sidecar, with the file fields of the thumbnail.

    Everything about the capture (validators, EXIF, image time, gate inputs measured on the
    1920 px pixels, Sun) is copied; ``stored*`` and ``jpeg_quality`` describe the thumbnail
    file, and ``full_frame`` the full-size copy (its policy and file fields):

    - ``"every"``: ``full_frame.key`` is the frame in ``frames/``.
    - ``"hourly-nearest"``: the full frame is kept only if it is its hour's nearest, so there
      is no ``key``; ``key_if_promoted`` is where it would be (a promoted frame's sidecar
      carries ``hourly_selection``) and ``staged_key`` where it waits until its hour is decided.

    It holds enough to rebuild the full frame's sidecar (:func:`frame_sidecar_from_thumb`).
    """
    doc = {k: v for k, v in frame_sidecar.items() if k != "thumb"}
    full = {k: frame_sidecar.get(k) for k in FILE_FIELDS}
    doc.update(thumb.to_sidecar())
    doc["variant"] = "thumb"
    if policy == POLICY_HOURLY:
        where: dict[str, Any] = {"key_if_promoted": frame_key, "staged_key": staged_key}
    else:
        where = {"key": frame_key}
    doc["full_frame"] = {**where, "policy": policy, **full}
    return doc


def frame_sidecar_from_thumb(doc: Mapping[str, Any], thumb_jpg_key: str) -> dict[str, Any]:
    """Rebuild a full frame's sidecar from its thumbnail's (a crash between the two writes)."""
    full = doc.get("full_frame") or {}
    out = {k: v for k, v in doc.items() if k not in {"full_frame", "variant"}}
    out["thumb"] = {"key": thumb_jpg_key, **{k: doc.get(k) for k in FILE_FIELDS}}
    out.update({k: full.get(k) for k in FILE_FIELDS})
    out["sidecar_rebuilt_from"] = sidecar_key(thumb_jpg_key)
    return out


# --- deleting capture's own staged copies --------------------------------------------------


def _remove_empty_dirs(folder: os.PathLike[str] | str, stop: os.PathLike[str] | str) -> None:
    """Remove ``folder`` and its parents while empty, never at or above ``stop``."""
    current = os.path.abspath(folder)
    limit = os.path.abspath(stop)
    while current.startswith(limit + os.sep):
        try:
            os.rmdir(current)
        except OSError:
            return
        current = os.path.dirname(current)


def delete_staged(storage: Storage, key: str) -> bool:
    """Delete one staged copy (``cache/staging/...``); refuse every other key. True if deleted.

    CLAUDE.md allows deleting only one's own temp or cache files, and ``snow_globe_common``'s
    ``Storage`` has no delete on purpose. So this works only under :data:`STAGING_PREFIX`:
    with a ``delete_cache(key)`` method if the storage grows one, else a local unlink, else an
    S3 ``DeleteObject`` (the capture role needs ``s3:DeleteObject`` on ``cache/staging/*``).
    """
    validate_key(key)
    if not key.startswith(STAGING_PREFIX):
        raise ValueError(f"refusing to delete {key!r}: only staged copies under cache/staging/")
    method = getattr(storage, "delete_cache", None)
    if callable(method):
        return bool(method(key))
    if isinstance(storage, LocalStorage):
        path = storage.path(key)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        _remove_empty_dirs(path.parent, storage.path(STAGING_PREFIX + "frames"))
        return True
    if isinstance(storage, S3Storage):
        # TODO(M1): use a snow_globe_common delete for cache/ keys once common has one.
        storage._s3.delete_object(Bucket=storage.bucket, Key=storage.prefix + key)
        return True
    raise TypeError(f"cannot delete staged copies from {type(storage).__name__}")


# --- listing ----------------------------------------------------------------------------------


def scan_staged(storage: Storage, viewpoint_id: str) -> list[str]:
    """Every staged ``.jpg`` key of a camera, plus the ``.jpg`` key of any lone ``.json``."""
    stems = set()
    for key in storage.list_prefix(f"{STAGING_PREFIX}frames/{viewpoint_id}/"):
        if key.endswith((".jpg", ".json")):
            stems.add(key.rsplit(".", 1)[0])
    return sorted(f"{stem}.jpg" for stem in stems)


def frames_in_window(
    storage: Storage,
    viewpoint_id: str,
    start: datetime,
    end: datetime,
    cache: dict[str, list[str]] | None = None,
) -> dict[str, str]:
    """``frames/`` JPEGs of a camera whose stamp is in ``[start, end)``: {file name: key}."""
    days = sorted({start.date(), (end - timedelta(microseconds=1)).date()})
    found: dict[str, str] = {}
    for day in days:
        prefix = f"frames/{viewpoint_id}/{day.isoformat()}/"
        if cache is not None and prefix in cache:
            keys = cache[prefix]
        else:
            keys = [k for k in storage.list_prefix(prefix) if k.endswith(".jpg")]
            if cache is not None:
                cache[prefix] = keys
        for key in keys:
            stamp = key_stamp(key)
            if stamp is not None and start <= stamp < end:
                found[key.rsplit("/", 1)[-1]] = key
    return found


# --- the hourly decision ----------------------------------------------------------------------


@dataclass
class Promotion:
    """What one call of :func:`promote_ready_hours` did."""

    events: list[dict[str, Any]] = field(default_factory=list)
    remaining: list[dict[str, Any]] = field(default_factory=list)
    written_sidecars: list[str] = field(default_factory=list)
    last: dict[str, Any] | None = None


@dataclass
class _Candidate:
    entry: Mapping[str, Any]
    key: str
    stamp: datetime
    has_jpg: bool = False
    doc: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        return self.key.rsplit("/", 1)[-1]

    @property
    def decodable(self) -> bool:
        return "decode_failed" not in ((self.doc or {}).get("flags") or [])


def _read_doc(storage: Storage, key: str) -> dict[str, Any] | None:
    try:
        doc = storage.read_json(key)
    except (KeyNotFoundError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _candidate_doc(storage: Storage, entry: Mapping[str, Any], key: str) -> dict[str, Any] | None:
    """The staged sidecar, or one rebuilt from the thumbnail's if a crash lost it."""
    doc = _read_doc(storage, sidecar_key(key))
    if doc is not None:
        return doc
    thumb = entry.get("thumb_key") or thumb_key_of_frame(frame_key_of_staged(key))
    thumb_doc = _read_doc(storage, sidecar_key(thumb))
    if thumb_doc is not None and storage.exists(thumb):
        return frame_sidecar_from_thumb(thumb_doc, thumb)
    return None


def _json_bytes(doc: Mapping[str, Any]) -> bytes:
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode()


def promote_ready_hours(
    storage: Storage,
    viewpoint_id: str,
    entries: Sequence[Mapping[str, Any]],
    now: datetime,
    *,
    settle_s: float = HOURLY_SETTLE_S,
    deleter: Callable[[Storage, str], bool] = delete_staged,
) -> Promotion:
    """Decide every hour whose candidates are all in; keep undecided entries for later.

    For each ready hour (module docstring): promote the nearest decodable staged copy to
    ``frames/`` (JPEG first, then the sidecar with an ``hourly_selection`` block, both
    create-only), or complete a promotion a crash interrupted, or keep the frame already
    there; then delete the hour's staged copies. Returns the log events to write.
    """
    result = Promotion()
    by_slot: dict[datetime, list[_Candidate]] = {}
    seen: set[str] = set()
    for entry in entries:
        key = str(entry.get("key") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        stamp = key_stamp(key) if key.startswith(STAGING_PREFIX + "frames/") else None
        if stamp is None:
            result.events.append({"event": "staging_dropped", "reason": "bad_key", "key": key})
            continue
        by_slot.setdefault(hour_slot(stamp), []).append(_Candidate(entry, key, stamp))
    listing: dict[str, list[str]] = {}
    for slot in sorted(by_slot):
        group = by_slot[slot]
        if now < slot + _HALF_HOUR + timedelta(seconds=settle_s):
            result.remaining.extend(dict(c.entry) for c in group)
            continue
        try:
            _decide(storage, viewpoint_id, slot, group, now, settle_s, listing, result)
        except Exception as exc:  # keep every staged copy; the next pass decides again
            result.remaining.extend(dict(c.entry) for c in group)
            result.events.append(
                {
                    "event": "staging_error",
                    "hour_utc": isoformat_z(slot, "seconds"),
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                }
            )
            continue
        for candidate in group:
            for key in (candidate.key, sidecar_key(candidate.key)):
                try:
                    deleter(storage, key)
                except Exception as exc:  # never let cleanup stop capture; the next scan retries
                    result.events.append(
                        {
                            "event": "staging_error",
                            "hour_utc": isoformat_z(slot, "seconds"),
                            "key": key,
                            "error": f"delete: {type(exc).__name__}: {exc}"[:200],
                        }
                    )
    return result


def _decide(
    storage: Storage,
    viewpoint_id: str,
    slot: datetime,
    group: list[_Candidate],
    now: datetime,
    settle_s: float,
    listing: dict[str, list[str]],
    result: Promotion,
) -> None:
    start, end = slot - _HALF_HOUR, slot + _HALF_HOUR
    for candidate in group:
        candidate.has_jpg = storage.exists(candidate.key)
        candidate.doc = _candidate_doc(storage, candidate.entry, candidate.key)
    valid = [c for c in group if c.has_jpg and c.doc is not None]
    existing = frames_in_window(storage, viewpoint_id, start, end, listing)
    hour = isoformat_z(slot, "seconds")
    selection_base = {
        "policy": POLICY_HOURLY,
        "rule": "frame nearest the UTC hour; decided 30 min after its +-30 min window closed",
        "hour_utc": hour,
        "window_utc": [isoformat_z(start, "seconds"), isoformat_z(end, "seconds")],
        "candidates": len(valid),
        "candidate_names": sorted(c.name for c in valid),
        "settle_s": settle_s,
        "decided_at": isoformat_z(now, "seconds"),
    }
    if existing:
        kept = [c for c in valid if c.name in existing]
        for candidate in kept:  # a promotion a crash cut short: finish it (else a leftover)
            _write_promoted(storage, candidate, slot, selection_base, result, recovered=True)
        dropped = [c.name for c in group if c not in kept]
        if dropped:
            result.events.append(
                {
                    "event": "staging_dropped",
                    "reason": "hour_already_kept",
                    "hour_utc": hour,
                    "kept": sorted(existing),
                    "dropped": dropped,
                }
            )
        return
    if not valid:
        result.events.append(
            {
                "event": "staging_dropped",
                "reason": "no_usable_candidate",
                "hour_utc": hour,
                "dropped": [c.name for c in group],
                "missing_jpg": [c.name for c in group if not c.has_jpg],
                "missing_sidecar": [c.name for c in group if c.doc is None],
            }
        )
        return
    best = min(
        valid,
        key=lambda c: (0 if c.decodable else 1, abs((c.stamp - slot).total_seconds()), c.stamp),
    )
    _write_promoted(storage, best, slot, selection_base, result, recovered=False)
    others = [c.name for c in group if c is not best]
    if others:
        result.events[-1]["dropped"] = others


def _write_promoted(
    storage: Storage,
    candidate: _Candidate,
    slot: datetime,
    selection_base: Mapping[str, Any],
    result: Promotion,
    *,
    recovered: bool,
) -> None:
    target = frame_key_of_staged(candidate.key)
    distance = abs((candidate.stamp - slot).total_seconds())
    selection = {
        **selection_base,
        "distance_s": distance,
        "staged_key": candidate.key,
        **({"recovered": True} if recovered else {}),
    }
    wrote = False
    if not storage.exists(target):
        wrote |= storage.put_bytes(
            target, storage.get_bytes(candidate.key), content_type="image/jpeg", if_absent=True
        )
    doc = {**(candidate.doc or {}), "hourly_selection": selection}
    side = sidecar_key(target)
    wrote |= storage.put_bytes(
        side, _json_bytes(doc), content_type="application/json", if_absent=True
    )
    result.written_sidecars.append(side)
    if recovered and not wrote:
        return  # already complete: the staged copy is a leftover of a cleanup that failed
    thumb = (candidate.doc or {}).get("thumb") or {}
    event = {
        "event": "promoted",
        "key": target,
        "staged_key": candidate.key,
        "thumb_key": thumb.get("key") if isinstance(thumb, Mapping) else None,
        "hour_utc": selection_base["hour_utc"],
        "distance_s": distance,
        "candidates": selection_base["candidates"],
        "recovered": recovered,
    }
    result.events.append(event)
    result.last = {
        "hour_utc": selection_base["hour_utc"],
        "key": target,
        "candidates": selection_base["candidates"],
        "distance_s": distance,
        "at": selection_base["decided_at"],
    }
