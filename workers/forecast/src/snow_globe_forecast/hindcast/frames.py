"""The frames P1's hindcast pairs with forecasts: every captured frame of a viewpoint (PRD §13.4).

P1 scores daytime frames, "Sun >= 3 deg by ephemeris". The frames are the live captures under
``frames/<viewpoint_id>/`` (every frame is kept at Rainier and Point Reyes, D-028) and
``thumbs/<viewpoint_id>/`` (a thumbnail of every download since 20 Nov 2026). Only their JSON
sidecars are read (metadata; no image is opened). A frame found in both trees is one frame:
both sidecars carry the original's SHA-256.

Held-out data (CLAUDE.md; D-019, D-022): a frame dated in design P's sealed window
(15 Dec 2026 to 14 Jan 2027, by its UTC or its local date) is never used and only counted.
Archived frames are not read at all (Rainier has no NPS archive).
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from snow_globe_common.manifest import Viewpoint
from snow_globe_common.storage import KeyNotFoundError, Storage
from snow_globe_common.timeutil import isoformat_z, parse_iso_utc

from .sun import solar_altitude_deg

__all__ = [
    "SEALED_FIRST_DAY",
    "SEALED_LAST_DAY",
    "TREES",
    "CapturedFrame",
    "FrameSet",
    "collect_frames",
    "frames_from_times",
    "in_sealed_window",
]

#: Design P's sealed window (PRD §13.2, §13.9): never used for development.
SEALED_FIRST_DAY = date(2026, 12, 15)
SEALED_LAST_DAY = date(2027, 1, 14)
#: The live capture trees read (never ``archive/``).
TREES: tuple[str, ...] = ("frames", "thumbs")


def in_sealed_window(moment: datetime, zone: ZoneInfo) -> bool:
    """True when ``moment``'s UTC date or its local date falls in the sealed window."""
    days = (moment.date(), moment.astimezone(zone).date())
    return any(SEALED_FIRST_DAY <= day <= SEALED_LAST_DAY for day in days)


@dataclass(frozen=True, slots=True)
class CapturedFrame:
    """One captured frame: its key, image time, the Sun at the camera, and where it was found."""

    key: str
    image_time_utc: datetime
    sha256: str | None
    trees: tuple[str, ...]
    sun_alt_deg: float
    #: The geometric elevation capture recorded in the sidecar (None for the 19 Nov stopgap).
    sidecar_sun_deg: float | None = None


@dataclass(frozen=True)
class FrameSet:
    """Every frame found for one viewpoint, once each, sorted by image time."""

    viewpoint_id: str
    camera: tuple[float, float]
    frames: tuple[CapturedFrame, ...]
    sidecars_read: Mapping[str, int] = field(default_factory=dict)
    skipped: Mapping[str, int] = field(default_factory=dict)

    def daytime(self, min_sun_alt_deg: float) -> list[CapturedFrame]:
        """The frames with the Sun at or above ``min_sun_alt_deg`` (geometric)."""
        return [f for f in self.frames if f.sun_alt_deg >= min_sun_alt_deg]

    def summary(self, min_sun_alt_deg: float) -> dict[str, Any]:
        """JSON-ready counts: frames by tree and day, daytime frames, the sidecar Sun check."""
        day = self.daytime(min_sun_alt_deg)
        by_day: Counter[str] = Counter(f.image_time_utc.strftime("%Y-%m-%d") for f in day)
        both = [f for f in self.frames if f.sidecar_sun_deg is not None]
        diffs = [abs(f.sun_alt_deg - float(f.sidecar_sun_deg or 0.0)) for f in both]
        flips = [
            f.key
            for f in both
            if (f.sun_alt_deg >= min_sun_alt_deg)
            != (float(f.sidecar_sun_deg or 0.0) >= min_sun_alt_deg)
        ]
        near = [f for f in self.frames if abs(f.sun_alt_deg - min_sun_alt_deg) < 0.05]
        return {
            "camera_lat_lon": list(self.camera),
            "sidecars_read": dict(self.sidecars_read),
            "skipped": dict(self.skipped),
            "frames": len(self.frames),
            "in_both_trees": sum(1 for f in self.frames if len(f.trees) > 1),
            "only_in": {tree: sum(1 for f in self.frames if f.trees == (tree,)) for tree in TREES},
            "first_utc": isoformat_z(self.frames[0].image_time_utc) if self.frames else None,
            "last_utc": isoformat_z(self.frames[-1].image_time_utc) if self.frames else None,
            "min_sun_alt_deg": min_sun_alt_deg,
            "daytime": len(day),
            "night": len(self.frames) - len(day),
            "daytime_by_utc_day": dict(sorted(by_day.items())),
            "within_0.05_deg_of_threshold": len(near),
            "sun_check": {
                "what": (
                    "this module's NOAA altitude at the manifest camera against the geometric "
                    "elevation capture recorded in the sidecar (computed at the capture "
                    "snapshot's camera position)"
                ),
                "frames_with_sidecar_sun": len(both),
                "max_abs_diff_deg": round(max(diffs), 4) if diffs else None,
                "median_abs_diff_deg": round(median(diffs), 4) if diffs else None,
                "daytime_disagreements": flips,
            },
        }


def _sidecar_sun(side: Mapping[str, Any]) -> float | None:
    sun = side.get("sun")
    if isinstance(sun, Mapping) and isinstance(sun.get("elevation_deg"), int | float):
        return float(sun["elevation_deg"])
    return None


def collect_frames(
    storage: Storage, viewpoint: Viewpoint, *, trees: Iterable[str] = TREES
) -> FrameSet:
    """Every captured frame of ``viewpoint`` under ``frames/`` and ``thumbs/`` (sidecars only).

    The Sun is computed at the manifest's camera (:func:`.sun.solar_altitude_deg`). A frame
    in both trees is counted once (by the original's SHA-256, else by image time); its key is
    the ``frames/`` image when there is one. Frames in the sealed window, sidecars without an
    image time and unreadable sidecars are skipped and counted.
    """
    cam = viewpoint.camera
    found: dict[str, dict[str, Any]] = {}
    read: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    for tree in trees:
        for key in storage.list_prefix(f"{tree}/{viewpoint.id}/"):
            if not key.endswith(".json"):
                continue
            try:
                side = storage.read_json(key)
            except (KeyNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
                skipped["unreadable sidecar"] += 1
                continue
            read[tree] += 1
            if not isinstance(side, Mapping) or not side.get("image_time_utc"):
                skipped["no image_time_utc"] += 1
                continue
            moment = parse_iso_utc(str(side["image_time_utc"]))
            if in_sealed_window(moment, viewpoint.zone):
                skipped["sealed window 2026-12-15..2027-01-14 (D-019, D-022)"] += 1
                continue
            sha = side.get("sha256") if isinstance(side.get("sha256"), str) else None
            identity = sha or isoformat_z(moment, "milliseconds")
            image_key = key.removesuffix(".json") + ".jpg"
            entry = found.setdefault(
                identity,
                {"time": moment, "sha": sha, "trees": set(), "keys": {}, "sun": None},
            )
            entry["trees"].add(tree)
            entry["keys"].setdefault(tree, image_key)
            if entry["sun"] is None:
                entry["sun"] = _sidecar_sun(side)
    frames = sorted(
        (
            CapturedFrame(
                key=entry["keys"].get("frames") or next(iter(entry["keys"].values())),
                image_time_utc=entry["time"],
                sha256=entry["sha"],
                trees=tuple(t for t in TREES if t in entry["trees"]),
                sun_alt_deg=round(solar_altitude_deg(entry["time"], cam.lat, cam.lon), 4),
                sidecar_sun_deg=entry["sun"],
            )
            for entry in found.values()
        ),
        key=lambda f: (f.image_time_utc, f.key),
    )
    return FrameSet(
        viewpoint_id=viewpoint.id,
        camera=(cam.lat, cam.lon),
        frames=tuple(frames),
        sidecars_read=dict(read),
        skipped=dict(skipped),
    )


def frames_from_times(
    viewpoint: Viewpoint, times: Sequence[datetime], *, prefix: str = "frames"
) -> FrameSet:
    """A :class:`FrameSet` of given image times (tests and what-if plans; no storage read)."""
    cam = viewpoint.camera
    frames = tuple(
        CapturedFrame(
            key=f"{prefix}/{viewpoint.id}/{t:%Y-%m-%d}/{t:%Y%m%dT%H%M%SZ}.jpg",
            image_time_utc=t,
            sha256=None,
            trees=(prefix,),
            sun_alt_deg=round(solar_altitude_deg(t, cam.lat, cam.lon), 4),
        )
        for t in sorted(times)
        if not in_sealed_window(t, viewpoint.zone)
    )
    return FrameSet(viewpoint.id, (cam.lat, cam.lon), frames)
