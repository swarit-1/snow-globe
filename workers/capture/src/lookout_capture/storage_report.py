"""Bytes per frame per camera, and bytes per day under the PRD §8.7 policy against the old one.

A no-network measurement over what is already on disk (``lookout-capture storage``):

- **Measured** per camera, from ``frames/<id>/`` (and ``thumbs/<id>/`` once it exists): the
  stored JPEG bytes per frame (all, Sun up, Sun down: the sidecar's ``sun.daylight``, or the
  NOAA Sun at the image time for the stopgap's frames, which carry no ``sun``), the sidecar
  bytes, and the bytes of a 640 px thumbnail made in memory from each stored frame with the
  same code and settings live capture uses (:func:`lookout_capture.frames.make_thumbnail`).
  Nothing is written.
- **Sidecars under the new policy:** one capture sidecar on disk is extended exactly as the
  new code would (``frame_policy``, the ``thumb`` block; the thumbnail's own sidecar; an
  ``hourly_selection`` block on a promoted frame) and serialised, so their sizes are real.
- **Projected** per camera per day, at the cap of ``max_images_per_hour`` downloads an hour
  (4: the ARD cameras change every 15 min and Rainier faster, so the cap binds), with frame
  and thumbnail sizes weighted by the share of that UTC day's quarter hours with the Sun at
  least 3° up (:data:`lookout_capture.frames.DAYLIGHT_MIN_SUN_DEG`):

  - old: every download kept at <= 1920 px;
  - new, design-P cameras: every frame at <= 1920 px plus every thumbnail;
  - new, the other six: 24 frames at <= 1920 px plus every thumbnail (the staged copies are
    transient: at most about 1.5 hours of frames, deleted once their hour is decided).

Bytes are logical (what S3 bills); ``on_disk_bytes`` adds the local file-system allocation
of the files already stored.
"""

from __future__ import annotations

import io
import json
import statistics
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

from lookout_common.manifest import Viewpoint
from lookout_common.paths import sidecar_key
from lookout_common.storage import LocalStorage
from PIL import Image

from .frames import THUMB_JPEG_QUALITY, THUMB_WIDTH, Thumbnail, make_thumbnail, sun_position
from .retention import (
    EVERY_FRAME_VIEWPOINTS,
    HOURLY_SETTLE_S,
    POLICY_EVERY,
    POLICY_HOURLY,
    frame_policy,
    key_stamp,
    staging_key,
    thumb_key_of_frame,
    thumb_sidecar,
)
from .state import parse_time

__all__ = ["daylight_share", "storage_report"]


def daylight_share(viewpoint: Viewpoint, day: date) -> float:
    """Share of the UTC day's 96 quarter hours with the apparent Sun >= 3° at the camera."""
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    up = 0
    for i in range(96):
        sun = sun_position(
            start + timedelta(minutes=15 * i), viewpoint.camera.lat, viewpoint.camera.lon
        )
        up += bool(sun["daylight"])
    return up / 96.0


def _mean(values: Iterable[float]) -> float | None:
    data = list(values)
    return round(statistics.fmean(data), 1) if data else None


def _json_size(doc: Mapping[str, Any]) -> int:
    return len((json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode())


def _weighted(
    day_mean: float | None, night_mean: float | None, all_mean: float | None, share: float
) -> float | None:
    if day_mean is not None and night_mean is not None:
        return share * day_mean + (1.0 - share) * night_mean
    return all_mean


def _new_sidecar_sizes(
    doc: Mapping[str, Any], key: str, thumb_bytes: int, policy: str
) -> dict[str, int]:
    """Serialised sizes of the sidecars the new code writes, built from one real sidecar.

    ``key`` is that sidecar's frame key; the thumbnail and staging keys follow from it, and
    the thumbnail block carries a real-length digest, so every field has its real length.
    """
    name = key.rsplit("/", 1)[-1]
    thumb = Thumbnail(b"\0" * thumb_bytes, THUMB_WIDTH, 360, "0" * 64, THUMB_JPEG_QUALITY)
    frame_doc = {
        **doc,
        "frame_policy": policy,
        "thumb": {"key": thumb_key_of_frame(key), **thumb.to_sidecar()},
    }
    thumb_doc = thumb_sidecar(
        frame_doc,
        thumb,
        frame_key=key,
        policy=policy,
        staged_key=staging_key(key) if policy == POLICY_HOURLY else None,
    )
    promoted = {
        **frame_doc,
        "hourly_selection": {
            "policy": POLICY_HOURLY,
            "rule": "frame nearest the UTC hour; decided 30 min after its +-30 min window closed",
            "hour_utc": "2026-11-19T13:00:00Z",
            "window_utc": ["2026-11-19T12:30:00Z", "2026-11-19T13:30:00Z"],
            "candidates": 4,
            "candidate_names": [name] * 4,
            "settle_s": HOURLY_SETTLE_S,
            "decided_at": "2026-11-19T14:00:12Z",
            "distance_s": 0.0,
            "staged_key": staging_key(key),
        },
    }
    return {
        "frame_sidecar": _json_size(frame_doc),
        "thumb_sidecar": _json_size(thumb_doc),
        "promoted_sidecar": _json_size(promoted),
    }


def storage_report(
    storage: LocalStorage,
    viewpoints: Iterable[Viewpoint],
    *,
    day: date,
    every_frame: Iterable[str] = EVERY_FRAME_VIEWPOINTS,
) -> dict[str, Any]:
    """Measure the stored frames of every camera and project bytes per day (module docstring)."""
    every = set(every_frame)
    rows = []
    totals = {"old": 0.0, "new": 0.0}
    stamps: list[datetime] = []
    for vp in viewpoints:
        policy = frame_policy(vp.id, every)
        frames = []
        sample: tuple[str, dict[str, Any]] | None = None
        on_disk = 0
        for key in storage.list_prefix(f"frames/{vp.id}/"):
            if not key.endswith(".jpg"):
                continue
            stamp = key_stamp(key)
            if stamp is not None:
                stamps.append(stamp)
            path = storage.path(key)
            side_path = storage.path(sidecar_key(key))
            try:
                doc = json.loads(side_path.read_text())
            except (OSError, ValueError):
                doc = {}
            data = path.read_bytes()
            on_disk += path.stat().st_blocks * 512 + (
                side_path.stat().st_blocks * 512 if side_path.exists() else 0
            )
            sun = doc.get("sun") or {}
            if "daylight" in sun:
                daylight = bool(sun["daylight"])
            else:
                when = parse_time(doc.get("image_time_utc"))
                daylight = (
                    bool(sun_position(when, vp.camera.lat, vp.camera.lon)["daylight"])
                    if when
                    else None
                )
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                thumb = make_thumbnail(image, exif=image.info.get("exif") or b"")
            frames.append(
                {
                    "bytes": len(data),
                    "sidecar_bytes": side_path.stat().st_size if side_path.exists() else 0,
                    "daylight": daylight,
                    "thumb_bytes": len(thumb.data),
                    "capture_version": doc.get("capture_version"),
                }
            )
            if str(doc.get("capture_version", "")).startswith("lookout-capture/"):
                sample = (key, doc)
        existing_thumbs = [
            storage.path(k).stat().st_size
            for k in storage.list_prefix(f"thumbs/{vp.id}/")
            if k.endswith(".jpg")
        ]
        share = daylight_share(vp, day)
        frame_all = _mean(f["bytes"] for f in frames)
        frame_day = _mean(f["bytes"] for f in frames if f["daylight"] is True)
        frame_night = _mean(f["bytes"] for f in frames if f["daylight"] is False)
        thumb_all = _mean(f["thumb_bytes"] for f in frames)
        thumb_day = _mean(f["thumb_bytes"] for f in frames if f["daylight"] is True)
        thumb_night = _mean(f["thumb_bytes"] for f in frames if f["daylight"] is False)
        frame_w = _weighted(frame_day, frame_night, frame_all, share)
        thumb_w = _weighted(thumb_day, thumb_night, thumb_all, share)
        capture_sides = [
            f["sidecar_bytes"]
            for f in frames
            if str(f["capture_version"]).startswith("lookout-capture/")
        ]
        side_old = _mean(capture_sides)
        new_sides = (
            _new_sidecar_sizes(sample[1], sample[0], int(thumb_w or 0), policy) if sample else None
        )
        downloads = 24 * int(vp.truth_camera.polite_caps.max_images_per_hour)
        projection: dict[str, Any] = {
            "downloads_per_day": downloads,
            "daylight_share": round(share, 3),
        }
        if frame_w is not None and thumb_w is not None and side_old is not None and new_sides:
            old = downloads * (frame_w + side_old)
            thumbs = downloads * (thumb_w + new_sides["thumb_sidecar"])
            if policy == POLICY_EVERY:
                full = downloads * (frame_w + new_sides["frame_sidecar"])
            else:
                full = 24 * (frame_w + new_sides["promoted_sidecar"])
            new = full + thumbs
            projection.update(
                {
                    "frame_bytes_weighted": round(frame_w),
                    "thumb_bytes_weighted": round(thumb_w),
                    "old_bytes_per_day": round(old),
                    "new_bytes_per_day": round(new),
                    "new_full_frames_bytes_per_day": round(full),
                    "new_thumbs_bytes_per_day": round(thumbs),
                    "new_over_old": round(new / old, 3) if old else None,
                }
            )
            totals["old"] += old
            totals["new"] += new
        rows.append(
            {
                "viewpoint_id": vp.id,
                "frame_policy": policy,
                "frames_on_disk": len(frames),
                "frames_sun_up": sum(1 for f in frames if f["daylight"] is True),
                "frames_sun_down": sum(1 for f in frames if f["daylight"] is False),
                "frame_bytes": {
                    "mean": frame_all,
                    "sun_up": frame_day,
                    "sun_down": frame_night,
                    "min": min((f["bytes"] for f in frames), default=None),
                    "max": max((f["bytes"] for f in frames), default=None),
                },
                "thumb_bytes_made_in_memory": {
                    "mean": thumb_all,
                    "sun_up": thumb_day,
                    "sun_down": thumb_night,
                    "min": min((f["thumb_bytes"] for f in frames), default=None),
                    "max": max((f["thumb_bytes"] for f in frames), default=None),
                },
                "thumbs_on_disk": {"n": len(existing_thumbs), "mean": _mean(existing_thumbs)},
                "sidecar_bytes": {"capture_mean": side_old, "new": new_sides},
                "on_disk_bytes": on_disk,
                "projection": projection,
            }
        )
    return {
        "day": day.isoformat(),
        "frames_span_utc": [
            min(stamps).isoformat() if stamps else None,
            max(stamps).isoformat() if stamps else None,
        ],
        "thumb_width": THUMB_WIDTH,
        "thumb_jpeg_quality": THUMB_JPEG_QUALITY,
        "cameras": rows,
        "totals_per_day": {
            "old_bytes": round(totals["old"]),
            "new_bytes": round(totals["new"]),
            "new_over_old": round(totals["new"] / totals["old"], 3) if totals["old"] else None,
            "old_bytes_90_days": round(totals["old"] * 90),
            "new_bytes_90_days": round(totals["new"] * 90),
        },
        "notes": [
            "thumbnails are made from the stored <= 1920 px frames; live capture makes them "
            "from the same pixels before the 1920 px JPEG encoding, so sizes differ slightly",
            "the Sun-up and Sun-down means are weighted by the daylight share of 'day'",
        ],
    }
