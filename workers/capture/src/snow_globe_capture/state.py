"""Persisted per-camera capture state and the rate-cap rules (PRD §15.3; CLAUDE.md polite rules).

One JSON document per camera at ``state/capture/<viewpoint_id>.json``. It is read and
written only while the camera's storage lock is held (``state/locks/capture/<id>.lock``),
so the caps hold across restarts, across the CLI loop and Lambda, and across concurrent
invocations.

**Both rules count answer times** (the lead's decision of 20 Nov 2026, amending D-023 and
D-048). nps.gov receives a request no later than it answers it, and the next one no earlier
than we send it, so a rule measured from our *answer* to our next *send* holds at nps.gov
whatever the network does; one measured send to send does not. What it remembers:

- the last poll (the JSON feed or a HEAD): its send stamp (``last_poll_at``) and its answer
  time (``last_answer_at``), the one clock reading taken right after the response was read in
  full or the request failed. The next poll is due 300 s + ``POLL_MARGIN_S`` after the answer.
  A request that failed after it may have left this machine, before any response head came
  back, counts as answered :data:`~snow_globe_capture.fetch.FAILED_AFTER_SEND_BOUND_S` after the
  failure (``last_answer_bound: "failed_after_send"``). A poll is reserved and saved before
  its send (``poll_reserved_at``), so a crash between the send and the save still counts it:
  found unanswered, it counts as answered
  :data:`~snow_globe_capture.fetch.UNANSWERED_REQUEST_BOUND_S` after the reservation. While it
  is reserved, ``last_poll_at`` holds that bound too, for code before 0.2.1 (a rollback),
  which schedules from ``last_poll_at`` alone;
- the validators last seen (JSON ``imagedate``/ETag/Last-Modified, image ETag/Last-Modified);
- every image GET in the last two hours, reserved and saved *before* the request goes out,
  then given its send (``t``) and its answer time (``answered_at``). The rolling-hour cap and
  the pacing count answer times (:func:`get_answer_bound`, :func:`cap_decision`);
- recent SHA-256 digests, for dedupe; the last stored image, for "dimensions changed";
- the error counts and the back-off deadline. Feed (JSON/HEAD) and image-GET errors are
  counted separately, so a feed that answers while its image keeps failing still backs
  off exponentially instead of being reset by every good poll;
- for a camera that keeps only the frame nearest each UTC hour (PRD §8.7,
  :mod:`.retention`), the full-size frames staged under ``cache/staging/`` and not yet
  decided, and when that folder was last listed;
- downloads that could not be stored (``store_failures``), and stored frames whose
  thumbnail is still to be made (``thumbs_pending``).

Fields added later have defaults, and unknown fields are ignored, so state written by an
older or newer version still loads (the running loop is a frozen snapshot of older code).
State that older code wrote has no answer times; :meth:`CameraState.settle_unanswered`
bounds them from above before anything is sent (``workers/capture/README.md``, "Upgrade and
crash bounds").
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime, timedelta
from typing import Any

from snow_globe_common.paths import state_key
from snow_globe_common.timeutil import isoformat_z, parse_iso_utc

from .fetch import UNANSWERED_REQUEST_BOUND_S

__all__ = [
    "BOUND_FAILED_AFTER_SEND",
    "BOUND_LEGACY_RECORD",
    "BOUND_LEGACY_STATE",
    "BOUND_UNANSWERED_RESERVATION",
    "GET_HISTORY_S",
    "HOST_STATE_KEY",
    "RECENT_SHA_LIMIT",
    "STAMP_RESOLUTION_S",
    "STATE_SCHEMA",
    "UNANSWERED_REQUEST_BOUND_S",
    "UNCONFIRMED_SEND_SLACK_S",
    "Adjust",
    "CameraState",
    "CapDecision",
    "answer_times",
    "camera_state_key",
    "cap_decision",
    "get_answer_bound",
    "get_times",
    "iso_ms_up",
    "parse_time",
]

#: Unchanged by the answer times: every field they add is optional (code that does not know
#: them ignores them, and this code bounds their absence).
STATE_SCHEMA = 1
#: GET records whose answer is older than this are dropped; the cap looks back one hour.
GET_HISTORY_S = 2 * 3600
#: How many recent digests are kept for dedupe (about two days at four frames an hour).
RECENT_SHA_LIMIT = 240
#: Shared back-off for the NPS web server, set on HTTP 429/503 by any camera.
HOST_STATE_KEY = state_key("capture/hosts/www.nps.gov.json")
#: Code before 0.2.1 stamped an image GET at its reservation and saved the state; the request
#: went out after that write and the 1 s request spacing. Its records therefore count from
#: ``t`` + this + :data:`UNANSWERED_REQUEST_BOUND_S` (they hold no answer time).
UNCONFIRMED_SEND_SLACK_S = 2.0
#: Stamps are written to the millisecond. One read from a sidecar (``fetched_at_utc``, taken
#: right after the response was read in full) was truncated, so it is moved up by this.
STAMP_RESOLUTION_S = 0.001

#: Why an answer time is an upper bound rather than a recorded answer (``last_answer_bound``
#: in the state, ``answer_bound`` in a GET record and in the request log).
#: A request failed after it may have left, and no response head came back (``live``).
BOUND_FAILED_AFTER_SEND = "failed_after_send"
#: A poll or GET was reserved and saved, and no answer was saved: a crash between the two.
BOUND_UNANSWERED_RESERVATION = "unanswered_reservation"
#: The state came from code that recorded no answer time (0.2.0, or 0.2.1 before answer times).
BOUND_LEGACY_STATE = "legacy_state"
#: A GET record of code before 0.2.1 (its ``t`` is the reservation).
BOUND_LEGACY_RECORD = "legacy_record"

#: Maps a stored answer stamp and its parsed time to the time a rule should use. The runner
#: passes one that also reads the stamp on the monotonic clock (``live``'s in-process guard
#: against wall-clock steps); without one the stored time is used.
Adjust = Callable[[str, datetime], datetime]


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


def iso_ms_up(when: datetime) -> str:
    """``when`` as ISO 8601 UTC to the millisecond, rounded *up*.

    Used for every stamp that bounds when nps.gov received a request from above (an answer
    time, a reservation), so the stamp is never earlier than the clock reading behind it. Send
    stamps, which bound a receipt from below, are truncated (``isoformat_z``).
    """
    spare = when.microsecond % 1000
    if spare:
        when += timedelta(microseconds=1000 - spare)
    return isoformat_z(when, "milliseconds")


def _plus(when: datetime, seconds: float) -> datetime:
    return when + timedelta(seconds=seconds)


@dataclass
class CameraState:
    """Everything capture remembers about one camera between passes."""

    viewpoint_id: str
    schema: int = STATE_SCHEMA
    created_at: str | None = None
    seeded: dict[str, Any] | None = None
    #: The last poll's send stamp (truncated to the millisecond), taken right before the request.
    #: While a poll is reserved and not yet sent it is the reservation +
    #: UNANSWERED_REQUEST_BOUND_S (rounded up): code before 0.2.1 reads only this field (and
    #: polls 301 s after it), so a rollback after a crash in that window still waits 5 minutes
    #: from the latest moment the reserved poll can have reached nps.gov.
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
    #: Image GETs: [{"t", "reserved_at", "answered_at", "status", "source"}]. ``reserved_at``
    #: is when it was reserved and saved, ``t`` its send once it went out (the reservation
    #: until then), ``answered_at`` its answer time; ``status`` reserved|ok|not-modified|error.
    #: ``answer_bound`` (when present) says ``answered_at`` is an upper bound, and why
    #: (``failed_after_send``, ``unanswered_reservation``, ``legacy_record``).
    #: Records folded in from sidecars have ``source: "sidecar:<version>"`` and the fetch time.
    gets: list[dict[str, Any]] = field(default_factory=list)
    #: sha256 -> key, oldest first, at most RECENT_SHA_LIMIT entries.
    recent_shas: dict[str, str] = field(default_factory=dict)
    #: Sidecar keys already folded in by reconciliation (today and yesterday only).
    known_sidecars: list[str] = field(default_factory=list)
    #: Consecutive failed polls of the feed (JSON GET or HEAD).
    failures: int = 0
    #: Consecutive failed image GETs; reset only by a good image (200 JPEG or 304).
    image_failures: int = 0
    #: Consecutive downloads that arrived but could not be stored (a denied or failed write);
    #: reset by a stored download. Each one is still marked handled, so it is never fetched
    #: again, and polling backs off 5 min doubling to 1 h.
    store_failures: int = 0
    not_before: str | None = None
    last_error: dict[str, Any] | None = None
    counters: dict[str, int] = field(default_factory=dict)
    #: Hourly-retention cameras: full-size frames staged and not yet decided
    #: ([{"key": "cache/staging/frames/...jpg", "thumb_key": ..., "sha256": ..., "at": iso}]).
    staged: list[dict[str, Any]] = field(default_factory=list)
    #: When ``cache/staging/frames/<id>/`` was last listed (orphans of a crash are found then).
    staging_scan_at: str | None = None
    #: The last hourly decision: {"hour_utc", "key", "candidates", "at"}.
    last_promotion: dict[str, Any] | None = None
    #: Stored frames whose thumbnail is missing (a failed or interrupted write), to be made
    #: from the stored frame: [{"frame_key", "thumb_key", "attempts", "at"}].
    thumbs_pending: list[dict[str, Any]] = field(default_factory=list)
    #: When nps.gov answered the last poll, or its request failed: one clock reading taken right
    #: after the response was read in full (or the failure), rounded up to the millisecond. The
    #: next poll is due 300 s + POLL_MARGIN_S after it (``live.poll_spacing_s``). Added in 0.2.1.
    last_answer_at: str | None = None
    #: None when ``last_answer_at`` is a recorded answer. Otherwise it is an upper bound and this
    #: says why: ``"failed_after_send"`` (the poll failed after it may have left, before any
    #: answer came back: the failure + FAILED_AFTER_SEND_BOUND_S, set by ``live``),
    #: ``"unanswered_reservation"`` (a poll was reserved and no answer was saved: the process
    #: died between the send and the save) or ``"legacy_state"`` (state from code that recorded
    #: no answer time). :meth:`settle_unanswered` sets the last two.
    last_answer_bound: str | None = None
    #: A poll reserved and saved right before its send (rounded up to the millisecond). Cleared
    #: when its answer is recorded, or when the poll is given up before the send. Found set when
    #: the state is loaded, it is a poll whose answer was never saved.
    poll_reserved_at: str | None = None

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

    def add_get(
        self, when: datetime, *, status: str, source: str = "self", reserved: bool = False
    ) -> dict[str, Any]:
        """Append an image-GET record (the caller persists the state).

        ``reserved`` marks a record live capture saves right before its send: it gets
        ``reserved_at`` (rounded up), so a crash before its answer is recorded still bounds it.
        """
        record = {"t": isoformat_z(when, "milliseconds"), "status": status, "source": source}
        if reserved:
            record["reserved_at"] = iso_ms_up(when)
        self.gets.append(record)
        return record

    def has_get_near(self, when: datetime, tolerance_s: float = 2.0) -> bool:
        """True if a GET is already recorded (reserved, sent or answered) within ``tolerance_s``."""
        for record in self.gets:
            for name in ("t", "reserved_at", "answered_at"):
                stamp = parse_time(record.get(name))
                if stamp is not None and abs((stamp - when).total_seconds()) <= tolerance_s:
                    return True
        return False

    def prune(self, now: datetime) -> None:
        """Drop GET records answered over GET_HISTORY_S ago, and sidecar keys before yesterday."""
        horizon = now - timedelta(seconds=GET_HISTORY_S)
        kept = []
        for record in self.gets:
            bound = get_answer_bound(record)
            if bound is not None and bound >= horizon:
                kept.append(record)
        self.gets = kept
        oldest_day = (now - timedelta(days=1)).date().isoformat()
        self.known_sidecars = [k for k in self.known_sidecars if _key_day(k) >= oldest_day]

    # -- the poll rule ----------------------------------------------------------------
    def settle_unanswered(
        self, now: datetime, *, bound_s: float = UNANSWERED_REQUEST_BOUND_S
    ) -> list[dict[str, Any]]:
        """Replace what an earlier writer left unanswered by upper bounds; return what changed.

        Called under the camera's lock right after the state is loaded, before anything is
        sent. Whoever wrote the state no longer holds the lock, so its requests are over: a
        crashed process is dead (a local ``flock`` dies with it; an S3 lock outlives the 120 s
        Lambda by its 300 s TTL), and a live one saves its answers before it lets go.

        - **A poll reserved and never answered** (a crash between the send and the save): its
          head left this machine within ``bound_s`` of the reservation (the send deadline plus
          the write timeout, :mod:`.fetch`), so it counts as answered then.
        - **State from code that recorded no answer** (0.2.0, or 0.2.1 before answer times):
          the send stamp + ``bound_s``, and no earlier than ``now``. That writer saved its
          state after its poll ended and before this run read it, so ``now`` bounds the answer
          even for a poll it sent and then crashed before saving (0.2.0 had no reservation);
          the send stamp + ``bound_s`` is the bound read from the code (a request reaches the
          socket within it).
        - **Image GETs with no answer time** of this camera's own (``source: "self"``): reserved
          ones count as answered ``bound_s`` after the later of the reservation and the send;
          records of code before 0.2.1 (``t`` is the reservation) ``UNCONFIRMED_SEND_SLACK_S``
          later still (:func:`get_answer_bound`). They are not moved to ``now``: that would
          hold a camera's images back for up to an hour after an upgrade.
        """
        changes: list[dict[str, Any]] = []
        reserved = parse_time(self.poll_reserved_at)
        if reserved is not None:
            bound = _plus(reserved, bound_s)
            recorded = parse_time(self.last_answer_at)
            if recorded is None or bound > recorded:
                self.last_answer_at = iso_ms_up(bound)
                self.last_answer_bound = BOUND_UNANSWERED_RESERVATION
            changes.append(
                {
                    "what": "poll",
                    "kind": BOUND_UNANSWERED_RESERVATION,
                    "reserved_at": self.poll_reserved_at,
                    "answer_bound_at": self.last_answer_at,
                }
            )
            self.poll_reserved_at = None
        elif self.last_answer_at is None and self.last_poll_at:
            sent = parse_time(self.last_poll_at)
            if sent is not None:
                self.last_answer_at = iso_ms_up(max(_plus(sent, bound_s), now))
                self.last_answer_bound = BOUND_LEGACY_STATE
                changes.append(
                    {
                        "what": "poll",
                        "kind": BOUND_LEGACY_STATE,
                        "sent_at": self.last_poll_at,
                        "answer_bound_at": self.last_answer_at,
                    }
                )
        for record in self.gets:
            if record.get("answered_at") or str(record.get("source") or "self") != "self":
                continue
            bound = get_answer_bound(record, bound_s=bound_s)
            if bound is None:
                continue
            kind = BOUND_UNANSWERED_RESERVATION if "reserved_at" in record else BOUND_LEGACY_RECORD
            record["answered_at"] = iso_ms_up(bound)
            record["answer_bound"] = kind
            # The GET's own stamps go under their own names: a change is logged as a record
            # whose ``t`` is the moment it is written (``live._log``).
            change = {"what": "image_get", "kind": kind, "get_t": record.get("t")}
            if "reserved_at" in record:
                change["get_reserved_at"] = record["reserved_at"]
            change["answer_bound_at"] = record["answered_at"]
            changes.append(change)
        return changes

    def poll_basis(
        self,
        *,
        bound_s: float = UNANSWERED_REQUEST_BOUND_S,
        now: datetime | None = None,
        include_reservation: bool = True,
        adjust: Adjust | None = None,
    ) -> datetime | None:
        """The latest moment nps.gov can have received this camera's last poll, or None.

        The latest that applies of: the answer time (``last_answer_at``, through ``adjust``);
        a reservation not yet answered + ``bound_s`` (``include_reservation``; the run that
        made it leaves it out while it waits for its own slot); and, for state with a send stamp
        but no answer time (code before answer times), the send stamp + ``bound_s``, and no
        earlier than ``now`` when given (what :meth:`settle_unanswered` would set now).
        """
        candidates: list[datetime] = []
        answered = parse_time(self.last_answer_at)
        if answered is not None:
            stamp = str(self.last_answer_at)
            candidates.append(adjust(stamp, answered) if adjust is not None else answered)
        reserved = parse_time(self.poll_reserved_at) if include_reservation else None
        if reserved is not None:
            candidates.append(_plus(reserved, bound_s))
        sent = parse_time(self.last_poll_at)
        if answered is None and sent is not None:
            candidates.append(_plus(sent, bound_s))
            if now is not None:
                candidates.append(now)
        return max(candidates) if candidates else None

    def poll_due_at(
        self,
        spacing_s: float,
        *,
        bound_s: float = UNANSWERED_REQUEST_BOUND_S,
        now: datetime | None = None,
        include_reservation: bool = True,
        adjust: Adjust | None = None,
    ) -> datetime | None:
        """Earliest time the next poll may be sent (poll spacing and back-off), or None if now.

        ``spacing_s`` after :meth:`poll_basis` (the scheduler passes 300 s + POLL_MARGIN_S),
        and never before the back-off deadline ``not_before``.
        """
        candidates = []
        basis = self.poll_basis(
            bound_s=bound_s, now=now, include_reservation=include_reservation, adjust=adjust
        )
        if basis is not None:
            candidates.append(_plus(basis, spacing_s))
        hold = parse_time(self.not_before)
        if hold is not None:
            candidates.append(hold)
        return max(candidates) if candidates else None


_DAY_SEGMENT = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _key_day(key: str) -> str:
    """The ``YYYY-MM-DD`` folder of a frame, thumbnail or staging key ("" if it has none)."""
    for segment in key.split("/")[:-1]:
        if _DAY_SEGMENT.match(segment):
            return segment
    return ""


def get_times(gets: Iterable[Mapping[str, Any]]) -> list[datetime]:
    """``t`` of the recorded GETs (any status), oldest first: when each was sent or reserved."""
    times = [parse_time(record.get("t")) for record in gets]
    return sorted(t for t in times if t is not None)


def get_answer_bound(
    record: Mapping[str, Any], *, bound_s: float = UNANSWERED_REQUEST_BOUND_S
) -> datetime | None:
    """The latest moment nps.gov can have received one image GET: what the cap and pacing count.

    - ``answered_at`` when recorded (also a bound :meth:`CameraState.settle_unanswered` set);
    - a sidecar's fetch time (``source: "sidecar:..."``), read right after the response was
      read in full, plus :data:`STAMP_RESOLUTION_S` for its truncation;
    - a reserved record with no answer: ``bound_s`` after the later of ``reserved_at`` and
      ``t`` (the send, if it went out);
    - a record of code before 0.2.1 (``t`` is the reservation, no ``reserved_at``):
      ``t`` + :data:`UNCONFIRMED_SEND_SLACK_S` + ``bound_s``.
    """
    answered = parse_time(record.get("answered_at"))
    if answered is not None:
        return answered
    when = parse_time(record.get("t"))
    if when is None:
        return None
    if str(record.get("source") or "self") != "self":
        return _plus(when, STAMP_RESOLUTION_S)
    reserved = parse_time(record.get("reserved_at"))
    if reserved is not None:
        return _plus(max(when, reserved), bound_s)
    return _plus(when, UNCONFIRMED_SEND_SLACK_S + bound_s)


def answer_times(
    gets: Iterable[Mapping[str, Any]],
    *,
    bound_s: float = UNANSWERED_REQUEST_BOUND_S,
    adjust: Adjust | None = None,
) -> list[datetime]:
    """Each GET's answer time or bound (:func:`get_answer_bound`), through ``adjust``, sorted."""
    out = []
    for record in gets:
        bound = get_answer_bound(record, bound_s=bound_s)
        if bound is None:
            continue
        stamp = record.get("answered_at")
        if adjust is not None and isinstance(stamp, str) and stamp:
            bound = adjust(stamp, bound)
        out.append(bound)
    return sorted(out)


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
    bound_s: float = UNANSWERED_REQUEST_BOUND_S,
    adjust: Adjust | None = None,
) -> CapDecision:
    """Apply the hard cap (``max_per_hour`` GETs in any rolling ``window_s``) and the pacing rule.

    Both count answer times (:func:`answer_times`). A GET may be sent only while fewer than
    ``max_per_hour`` of the camera's GETs have an answer time in the last ``window_s``; one
    answered (or bounded) after ``now`` counts too. Since a camera's GETs run one at a time,
    the next one is sent no earlier than ``window_s`` after the answer of the one
    ``max_per_hour`` back, and nps.gov received that one no later than it answered: so
    nps.gov never sees ``max_per_hour + 1`` GETs of a camera within ``window_s``, however long
    each took to arrive. Pacing keeps each GET ``min_spacing_s`` after the last one's answer,
    so a camera that changes at every 5-minute poll (Rainier updates about once a minute:
    ``snow-globe-capture cadence``) yields frames spread through the hour instead of four in a
    row and then nothing. Pacing is stricter than the cap, never looser.
    """
    answers = answer_times(gets, bound_s=bound_s, adjust=adjust)
    recent = [a for a in answers if (now - a).total_seconds() < window_s]
    candidates: list[tuple[datetime, str]] = []
    if len(recent) >= max_per_hour:
        # The answer that must age out before one more GET fits under the cap.
        oldest_blocking = recent[len(recent) - max_per_hour]
        candidates.append((_plus(oldest_blocking, window_s), "hourly_cap"))
    if answers and min_spacing_s > 0:
        spaced = _plus(answers[-1], min_spacing_s)
        if spaced > now:
            candidates.append((spaced, "spacing"))
    if not candidates:
        return CapDecision(True, None, len(recent), None)
    when, reason = max(candidates, key=lambda item: item[0])
    # Report the binding reason: the hourly cap outranks pacing when both apply.
    if any(r == "hourly_cap" for _, r in candidates):
        reason = "hourly_cap"
    return CapDecision(False, reason, len(recent), when)
