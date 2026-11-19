"""Persisted per-camera capture state and the rate-cap rules (PRD §15.3; CLAUDE.md polite rules).

One JSON document per camera at ``state/capture/<viewpoint_id>.json``. It is read and
written only while the camera's storage lock is held (``state/locks/capture/<id>.lock``),
so the caps hold across restarts, across the CLI loop and Lambda, and across concurrent
invocations.

What it remembers:

- when the camera was last polled (the JSON feed or a HEAD), so polls stay >= 5 min apart;
- the validators last seen (JSON ``imagedate``/ETag/Last-Modified, image ETag/Last-Modified);
- every image GET in the last two hours, recorded *before* the request goes out, so a
  crash mid-download still counts against the hourly cap;
- recent SHA-256 digests, for dedupe; the last stored image, for "dimensions changed";
- the error counts and the back-off deadline. Feed (JSON/HEAD) and image-GET errors are
  counted separately, so a feed that answers while its image keeps failing still backs
  off exponentially instead of being reset by every good poll.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime, timedelta
from typing import Any

from lookout_common.paths import state_key
from lookout_common.timeutil import isoformat_z, parse_iso_utc

__all__ = [
    "GET_HISTORY_S",
    "HOST_STATE_KEY",
    "RECENT_SHA_LIMIT",
    "STATE_SCHEMA",
    "CameraState",
    "CapDecision",
    "camera_state_key",
    "cap_decision",
    "get_times",
    "parse_time",
]

STATE_SCHEMA = 1
#: GET records older than this are dropped; the cap looks back one hour.
GET_HISTORY_S = 2 * 3600
#: How many recent digests are kept for dedupe (about two days at four frames an hour).
RECENT_SHA_LIMIT = 240
#: Shared back-off for the NPS web server, set on HTTP 429/503 by any camera.
HOST_STATE_KEY = state_key("capture/hosts/www.nps.gov.json")


def camera_state_key(viewpoint_id: str) -> str:
    """Key of one camera's state document: ``state/capture/<viewpoint_id>.json``."""
    return state_key(f"capture/{viewpoint_id}.json")


def parse_time(value: Any) -> datetime | None:
    """Parse a stored ISO 8601 time (``Z`` or an offset) to aware UTC; None if absent or bad."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else None
    try:
        return parse_iso_utc(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class CameraState:
    """Everything capture remembers about one camera between passes."""

    viewpoint_id: str
    schema: int = STATE_SCHEMA
    created_at: str | None = None
    seeded: dict[str, Any] | None = None
    last_poll_at: str | None = None
    polls: int = 0
    # Live JSON feed (ARD air cams).
    json_etag: str | None = None
    json_last_modified: str | None = None
    imagedate_raw: str | None = None
    #: The imagedate whose image was fetched or deliberately settled (stored/duplicate).
    handled_imagedate_raw: str | None = None
    # Last validators seen by a HEAD (park camera).
    head_etag: str | None = None
    head_last_modified: str | None = None
    #: Validators of the newest image already settled (stored, or found to be a duplicate):
    #: a HEAD showing these means "unchanged".
    settled_etag: str | None = None
    settled_last_modified: str | None = None
    #: The newest image we hold: key, sha256, etag, last_modified, width, height, times.
    last_image: dict[str, Any] | None = None
    #: Image GETs: [{"t": iso, "status": "reserved|ok|not-modified|error", "source": ...}].
    gets: list[dict[str, Any]] = field(default_factory=list)
    #: sha256 -> key, oldest first, at most RECENT_SHA_LIMIT entries.
    recent_shas: dict[str, str] = field(default_factory=dict)
    #: Sidecar keys already folded in by reconciliation (today and yesterday only).
    known_sidecars: list[str] = field(default_factory=list)
    #: Consecutive failed polls of the feed (JSON GET or HEAD).
    failures: int = 0
    #: Consecutive failed image GETs; reset only by a good image (200 JPEG or 304).
    image_failures: int = 0
    not_before: str | None = None
    last_error: dict[str, Any] | None = None
    counters: dict[str, int] = field(default_factory=dict)

    # -- serialisation --------------------------------------------------------------
    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CameraState:
        """Build from a stored document, ignoring keys this version does not know."""
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in data.items() if k in known}
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        """Plain dict for :meth:`Storage.write_json`."""
        return asdict(self)

    # -- helpers ----------------------------------------------------------------------
    def bump(self, counter: str, n: int = 1) -> None:
        """Add ``n`` to a named counter (stored, unchanged, duplicate, ...)."""
        self.counters[counter] = int(self.counters.get(counter, 0)) + n

    def remember_sha(self, sha256: str, key: str) -> None:
        """Record a digest for dedupe, keeping only the newest RECENT_SHA_LIMIT."""
        self.recent_shas.pop(sha256, None)
        self.recent_shas[sha256] = key
        while len(self.recent_shas) > RECENT_SHA_LIMIT:
            self.recent_shas.pop(next(iter(self.recent_shas)))

    def add_get(self, when: datetime, *, status: str, source: str = "self") -> dict[str, Any]:
        """Append an image-GET record (the caller persists the state)."""
        record = {"t": isoformat_z(when, "milliseconds"), "status": status, "source": source}
        self.gets.append(record)
        return record

    def has_get_near(self, when: datetime, tolerance_s: float = 2.0) -> bool:
        """True if a GET is already recorded within ``tolerance_s`` of ``when``."""
        return any(abs((t - when).total_seconds()) <= tolerance_s for t in get_times(self.gets))

    def prune(self, now: datetime) -> None:
        """Drop GET records older than GET_HISTORY_S and sidecar keys older than yesterday."""
        horizon = now - timedelta(seconds=GET_HISTORY_S)
        kept = []
        for record in self.gets:
            when = parse_time(record.get("t"))
            if when is not None and when >= horizon:
                kept.append(record)
        self.gets = kept
        oldest_day = (now - timedelta(days=1)).date().isoformat()
        self.known_sidecars = [k for k in self.known_sidecars if _key_day(k) >= oldest_day]

    def poll_due_at(self, poll_interval_s: float) -> datetime | None:
        """Earliest time the next poll may start (poll spacing and back-off), or None if now."""
        candidates = []
        last = parse_time(self.last_poll_at)
        if last is not None:
            candidates.append(last + timedelta(seconds=poll_interval_s))
        hold = parse_time(self.not_before)
        if hold is not None:
            candidates.append(hold)
        return max(candidates) if candidates else None


def _key_day(key: str) -> str:
    parts = key.split("/")
    return parts[2] if len(parts) > 3 else ""


def get_times(gets: Iterable[Mapping[str, Any]]) -> list[datetime]:
    """Times of the recorded GETs (any status), oldest first."""
    times = [parse_time(record.get("t")) for record in gets]
    return sorted(t for t in times if t is not None)


@dataclass(frozen=True)
class CapDecision:
    """Whether an image GET may go out now, and if not, why and from when."""

    allowed: bool
    reason: str | None  # None, "hourly_cap" or "spacing"
    gets_last_hour: int
    next_allowed_at: datetime | None


def cap_decision(
    gets: Iterable[Mapping[str, Any]],
    now: datetime,
    *,
    max_per_hour: int,
    min_spacing_s: float,
    window_s: float = 3600.0,
) -> CapDecision:
    """Apply the hard cap (``max_per_hour`` GETs in any rolling ``window_s``) and the pacing rule.

    A GET at time ``t`` counts while ``now - t < window_s``. Pacing keeps consecutive
    GETs ``min_spacing_s`` apart, so a camera that changes every 5 minutes (Rainier)
    yields frames spread through the hour instead of four in a row and then nothing.
    Pacing is stricter than the cap, never looser.
    """
    times = get_times(gets)
    recent = [t for t in times if (now - t).total_seconds() < window_s]
    candidates: list[tuple[datetime, str]] = []
    if len(recent) >= max_per_hour:
        # The oldest GET that must age out before one more fits under the cap.
        oldest_blocking = recent[len(recent) - max_per_hour]
        candidates.append((oldest_blocking + timedelta(seconds=window_s), "hourly_cap"))
    if times and min_spacing_s > 0:
        spaced = times[-1] + timedelta(seconds=min_spacing_s)
        if spaced > now:
            candidates.append((spaced, "spacing"))
    if not candidates:
        return CapDecision(True, None, len(recent), None)
    when, reason = max(candidates, key=lambda item: item[0])
    # Report the binding reason: the hourly cap outranks pacing when both apply.
    if any(r == "hourly_cap" for _, r in candidates):
        reason = "hourly_cap"
    return CapDecision(False, reason, len(recent), when)
