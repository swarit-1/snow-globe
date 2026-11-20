"""Live capture of the NPS cameras: one polite pass over the manifest cameras (PRD §11.2 "Capture").

Rules (PRD §15.3, CLAUDE.md "Polite collection"), all enforced with state persisted in
storage under a per-camera lock, so they survive restarts and concurrent invocations:

- **Poll** the camera's JSON feed (ARD air cams) or ``HEAD`` its image (Rainier park
  camera) at most once per ``poll_interval_s`` (never under 300 s).
- **Fetch on change only.** ARD: GET the image only if the JSON ``imagedate`` changed
  (a 304 on the conditional JSON GET also means unchanged). Park camera: GET only if
  ``Last-Modified``/``ETag`` changed. The image GET is itself conditional on the last ETag.
- **At most 4 image GETs per camera in any rolling hour** (the manifest's
  ``max_images_per_hour``). Each GET is written to state *before* it is sent. A pacing
  rule keeps GETs at least 0.8 x 3600/N s apart, so a camera that changes at every poll
  (Rainier updates about once a minute) yields frames spread through the hour.
- **Back off** on errors, exponentially (5 min doubling to 1 h), per camera, with separate
  counters for the feed, the image GET and the store; HTTP 429/503 also holds every request
  to www.nps.gov, live and archive alike, honouring ``Retry-After`` (:mod:`.host_backoff`).
- **A download is never fetched twice:** once a 200 JPEG has arrived, its validators and
  ``imagedate`` are recorded as handled even if storing it fails (a denied or failed
  write), so a broken store costs one GET per new image, never repeated GETs of one image.
- **Seed** from frames already stored (the Rainier stopgap's sidecars hold
  ``http_last_modified``, ``etag`` and ``sha256``) so a first pass never re-downloads an
  unchanged image, and fold in frames stored by any other writer before each poll.
- **One fetcher per machine.** The console commands run inside
  :func:`lookout_capture.fetcher_guard.nps_fetcher_lock`; :meth:`CaptureRunner.loop` takes a
  ``before_pass`` check that stops it if a foreign fetcher (the stopgap) appears.

**Storage (PRD §8.7, :mod:`.retention`).** Every downloaded frame gets a 640 px thumbnail
in ``thumbs/``. The two design-P cameras (Rainier, Point Reyes) also keep every frame at
<= 1920 px in ``frames/``; the other six stage it under ``cache/staging/`` and keep only
the frame nearest each UTC hour, promoted to ``frames/`` once the hour's candidates are all
in (every pass decides the ready hours, inside the camera's lock, before it polls). At a
design-P camera the frame is written first; a thumbnail whose write failed or was cut short
is made later from the stored frame (``thumbs_pending``).

Stored files follow the CLAUDE.md layout and carry a JSON sidecar with the PRD §8.6 gate
inputs (:mod:`.frames`); events go to ``capture_log/<UTC date>.jsonl`` (request, stored,
unchanged, duplicate, rate_capped, error, backoff, promoted, staging_dropped, thumb_repaired,
thumb_given_up, plus one ``pass`` record per pass; every poll outcome carries the feed's
validator, for :mod:`.cadence`). With S3 storage a pass's records are written in one
``append_jsonl_many`` call (each S3 append rewrites the day's whole log object), and records a
failed write could not store go to stderr and out with the next pass; locally each record is
appended at once.
"""

from __future__ import annotations

import json
import random
import re
import signal
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from types import FrameType, TracebackType
from typing import Any

import httpx
from lookout_common.http import backoff_delay, polite_client, retry_after_s, user_agent
from lookout_common.images import hamming64
from lookout_common.jsonl import iter_jsonl_bytes
from lookout_common.manifest import Viewpoint
from lookout_common.paths import capture_log_key, frame_key, sidecar_key, thumb_key
from lookout_common.storage import KeyNotFoundError, LocalStorage, LockTimeoutError, Storage
from lookout_common.timeutil import isoformat_z, parse_nps_imagedate, utcnow

from . import CAPTURE_VERSION
from .fetch import Fetched, ResponseTooLarge, fetch
from .frames import (
    THUMB_WIDTH,
    choose_image_time,
    describe_thumbnail,
    gate_flags,
    process_image,
    sha256_hex,
    sun_position,
    thumbnail_of_stored,
)
from .host_backoff import extend_host_hold, host_hold_until
from .retention import (
    EVERY_FRAME_VIEWPOINTS,
    HOURLY_SETTLE_S,
    POLICY_EVERY,
    POLICY_HOURLY,
    STAGING_SCAN_EVERY_S,
    frame_policy,
    promote_ready_hours,
    scan_staged,
    staging_key,
    thumb_sidecar,
)
from .state import (
    GET_HISTORY_S,
    RECENT_SHA_LIMIT,
    CameraState,
    camera_state_key,
    cap_decision,
    get_times,
    parse_time,
)

__all__ = [
    "MIN_POLL_INTERVAL_S",
    "CaptureOptions",
    "CaptureRunner",
    "parse_feed",
    "status_report",
]

#: CLAUDE.md: poll the camera JSON or headers at most every 5 minutes.
MIN_POLL_INTERVAL_S = 300.0
#: Added to the poll spacing when deciding a camera is due, so a send-to-send gap measured
#: from the request log never falls under MIN_POLL_INTERVAL_S. The critic found 8 of 504
#: gaps at 299.06-299.7 s on 19-20 Nov 2026: the poll was stamped before its request's
#: spacing wait. The stamp now moves to the send (``_request(poll_state=...)``) as well.
POLL_MARGIN_S = 1.0
#: An image older than this at fetch time means the feed has stalled.
STALE_IMAGE_AFTER = timedelta(hours=2)
#: Live sidecars. 2 (19 Nov 2026, additive over 1): ``frame_policy`` and ``thumb``; in a
#: thumbnail's sidecar ``variant`` and ``full_frame``; in a promoted frame's ``hourly_selection``.
SIDECAR_SCHEMA = 2
#: Missing thumbnails listed for repair at most; past it the oldest is given up (and logged).
THUMBS_PENDING_LIMIT = 48
#: Thumbnails remade from stored frames per camera per pass, and tries per thumbnail.
THUMB_REPAIRS_PER_PASS = 2
THUMB_REPAIR_ATTEMPTS = 3
#: Batched log records whose write failed, kept for the next pass at most (oldest dropped).
LOG_CARRY_LIMIT = 2000
_IMAGEDATE_RE = re.compile(r'"imagedate"\s*:\s*"([^"]*)"')
_IMAGEFILE_RE = re.compile(r'"imagefilelarge"\s*:\s*"([^"]*)"')


@dataclass
class CaptureOptions:
    """Tunables of a capture run (defaults are the polite production values)."""

    request_gap_s: float = 1.0  # between any two live requests of one process
    lock_timeout_s: float = 0.0  # skip a camera another run is handling
    lock_ttl_s: float = 300.0
    pacing_fraction: float = 0.8
    reconcile: str = "auto"  # "always" | "first-run" | "auto" (always on local disk)
    max_image_bytes: int = 30_000_000
    max_json_bytes: int = 1_000_000
    error_backoff_base_s: float = 300.0
    error_backoff_cap_s: float = 3600.0
    runner: str = "cli"
    #: Cameras that keep every frame at <= 1920 px (PRD §8.7 exception, :mod:`.retention`).
    every_frame: frozenset[str] = EVERY_FRAME_VIEWPOINTS
    hourly_settle_s: float = HOURLY_SETTLE_S
    staging_scan_every_s: float = STAGING_SCAN_EVERY_S
    #: "auto" batches a pass's log records into one write when storage is not local (S3).
    batch_logs: str = "auto"  # "auto" | "always" | "never"


def parse_feed(text: str) -> dict[str, Any]:
    """Parse an ARD JSON feed; fall back to a regex for ``imagedate`` if the JSON is malformed."""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    out: dict[str, Any] = {"_parsed_by": "regex"}
    for name, pattern in (("imagedate", _IMAGEDATE_RE), ("imagefilelarge", _IMAGEFILE_RE)):
        match = pattern.search(text)
        if match:
            out[name] = match.group(1)
    return out


def _http_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else None


def _is_local(storage: Storage) -> bool:
    return isinstance(storage, LocalStorage)


def _json_bytes(doc: Mapping[str, Any]) -> bytes:
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode()


def _validators(context: Mapping[str, Any]) -> dict[str, Any]:
    """The poll's feed validator for a log record: JSON ``Last-Modified`` (ARD) or HEAD's.

    Logged in every poll outcome so ``lookout-capture cadence`` sees each poll's value.
    """
    if "json_url" in context:
        return {"json_last_modified": context.get("json_last_modified")}
    if "head_last_modified" in context:
        return {"head_last_modified": context.get("head_last_modified")}
    return {}


class CaptureRunner:
    """Runs capture passes against one storage backend with one HTTP client."""

    def __init__(
        self,
        storage: Storage,
        client: httpx.Client,
        *,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        options: CaptureOptions | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.storage = storage
        self.client = client
        self.clock = clock
        self.sleep = sleep
        self.monotonic = monotonic
        self.options = options or CaptureOptions()
        self.rng = rng or random.Random()
        self._last_request: float | None = None
        self._stop = False
        self._signals = 0
        #: Records of the pass in progress, when logs are batched (S3); None otherwise.
        self._log_buffer: list[tuple[str, dict[str, Any]]] | None = None
        #: Batched records whose write failed, sent first with the next pass's batch.
        self._log_carry: list[tuple[str, dict[str, Any]]] = []
        #: Log writes that failed (the log never stops capture), and the last error.
        self.log_errors = 0
        self.last_log_error: str | None = None
        #: Unwritten records dropped from the carry (they were printed to stderr).
        self.log_records_dropped = 0

    @classmethod
    def default(
        cls, storage: Storage, *, runner: str = "cli", transport: httpx.BaseTransport | None = None
    ) -> CaptureRunner:
        """A runner on :func:`lookout_common.http.polite_client` (project UA, no cookies)."""
        return cls(
            storage, polite_client(transport=transport), options=CaptureOptions(runner=runner)
        )

    def __enter__(self) -> CaptureRunner:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.client.close()

    # -- logging --------------------------------------------------------------------
    def _log(self, viewpoint_id: str | None, event: str, **fields: Any) -> dict[str, Any]:
        """Write one ``capture_log`` record now, or queue it for the pass's single write."""
        now = self.clock()
        record = {
            "t": isoformat_z(now, "milliseconds"),
            "worker": "capture",
            "viewpoint_id": viewpoint_id,
            "event": event,
            **fields,
            "runner": self.options.runner,
            "capture_version": CAPTURE_VERSION,
        }
        key = capture_log_key(now)
        if self._log_buffer is not None:
            self._log_buffer.append((key, record))
            return record
        try:
            self.storage.append_jsonl(key, record)
        except Exception as exc:  # the log must never stop capture; keep going
            record["log_error"] = repr(exc)[:200]
            self.log_errors += 1
            self.last_log_error = record["log_error"]
        return record

    def _batch_logs(self) -> bool:
        mode = self.options.batch_logs
        return mode == "always" or (mode == "auto" and not _is_local(self.storage))

    def _flush_logs(self, *, keep_batching: bool = False) -> int:
        """Write the queued records, one ``append_jsonl_many`` per log file; returns writes.

        Each S3 append is a GET and a PUT of the day's whole log (lookout_common.storage), so
        one write per pass instead of one per record is most of the logging cost on Lambda.
        Records whose write fails are never just dropped: they are printed to stderr as JSON
        lines (CloudWatch keeps a Lambda's), and kept, up to :data:`LOG_CARRY_LIMIT`, to go
        out first with the next pass of this runner (the CLI loop). ``keep_batching`` flushes
        mid-pass and goes on batching.
        """
        buffer = self._log_buffer or []
        self._log_buffer = [] if keep_batching else None
        groups: dict[str, list[dict[str, Any]]] = {}
        for key, record in buffer:
            groups.setdefault(key, []).append(record)
        writes = 0
        for key, records in groups.items():
            try:
                self.storage.append_jsonl_many(key, records)
                writes += 1
            except Exception as exc:  # the log must never stop capture
                self.log_errors += 1
                self.last_log_error = repr(exc)[:200]
                self._keep_unwritten(key, records)
        return writes

    def _keep_unwritten(self, key: str, records: list[dict[str, Any]]) -> None:
        for record in records:
            with suppress(Exception):  # an unprintable record must not stop capture either
                print(json.dumps({"capture_log_unwritten": key, **record}), file=sys.stderr)
        self._log_carry.extend((key, record) for record in records)
        overflow = len(self._log_carry) - LOG_CARRY_LIMIT
        if overflow > 0:
            del self._log_carry[:overflow]
            self.log_records_dropped += overflow

    # -- HTTP ---------------------------------------------------------------------------
    def _space(self) -> None:
        if self._last_request is not None and self.options.request_gap_s > 0:
            wait = self._last_request + self.options.request_gap_s - self.monotonic()
            if wait > 0:
                self.sleep(wait)
        self._last_request = self.monotonic()

    def _request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None,
        max_bytes: int,
        *,
        viewpoint_id: str,
        stage: str,
        poll_state: CameraState | None = None,
    ) -> Fetched:
        """Send one live request, spaced from the last, and log it as a ``request`` event.

        Every nps.gov request of a pass is in ``capture_log`` (method, stage, status, bytes,
        time), so the request count of a measured run can be checked line by line.
        ``poll_state`` (the poll's first request: the JSON GET or the HEAD) gets its
        ``last_poll_at`` stamped after the spacing wait, at the send, so the 5-minute poll
        spacing (CLAUDE.md, PRD §15.3) is measured from when the request really went out.
        """
        self._space()
        if poll_state is not None:
            poll_state.last_poll_at = isoformat_z(self.clock(), "milliseconds")
        started = self.monotonic()
        status: int | None = None
        size = 0
        error: str | None = None
        try:
            response = fetch(self.client, method, url, headers=headers, max_bytes=max_bytes)
            status, size = response.status_code, len(response.content)
            return response
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:200]
            raise
        finally:
            self._last_request = self.monotonic()
            self._log(
                viewpoint_id,
                "request",
                stage=stage,
                method=method,
                url=url,
                status=status,
                bytes=size,
                elapsed_ms=round((self._last_request - started) * 1000.0, 1),
                **({"error": error} if error else {}),
            )

    # -- state ------------------------------------------------------------------------
    def _load_state(self, viewpoint_id: str) -> CameraState:
        doc = self.storage.read_json(camera_state_key(viewpoint_id), default=None)
        if isinstance(doc, Mapping):
            return CameraState.from_dict(doc)
        return CameraState(viewpoint_id=viewpoint_id)

    def _save_state(self, state: CameraState) -> None:
        self.storage.write_json(camera_state_key(state.viewpoint_id), state.to_dict())

    def _host_hold(self, now: datetime) -> datetime | None:
        """The www.nps.gov hold in force: any live or archive 429/503 sets it (``host_backoff``)."""
        return host_hold_until(self.storage, now)

    def _set_host_hold(self, seconds: float, reason: str) -> None:
        extend_host_hold(self.storage, self.clock(), seconds, reason, source="live")

    # -- reconciliation with frames stored by anyone ----------------------------------
    def reconcile(
        self, viewpoint: Viewpoint, state: CameraState, now: datetime, *, full: bool
    ) -> int:
        """Fold sidecars not yet known into the state: GET times, digests, last validators.

        ``full`` (first run) reads the newest RECENT_SHA_LIMIT sidecars of the camera;
        otherwise today's and yesterday's folders are listed and only unseen sidecars read.
        Both ``frames/`` and ``thumbs/`` are read: an hourly camera has a thumbnail for every
        download but a full frame for one an hour. This is how the stopgap's frames seed the
        rate cap and the change check. A frame whose sidecar names a thumbnail that does not
        exist (a crash between the two writes) is listed in ``thumbs_pending`` for repair.
        """
        roots = ("frames", "thumbs")
        if full:
            prefixes = [f"{root}/{viewpoint.id}/" for root in roots]
        else:
            days = {now.date(), (now - timedelta(days=1)).date()}
            prefixes = [
                f"{root}/{viewpoint.id}/{d.isoformat()}/" for root in roots for d in sorted(days)
            ]
        found = {k for p in prefixes for k in self.storage.list_prefix(p) if k.endswith(".json")}
        # Oldest first by file name (the image time), so frames/ and thumbs/ interleave.
        keys = sorted(found, key=lambda k: (k.rsplit("/", 1)[-1], k))
        if full:
            keys = keys[-RECENT_SHA_LIMIT:]
        known = set(state.known_sidecars)
        fresh = [k for k in keys if k not in known]
        folded = 0
        for key in fresh:
            try:
                doc = self.storage.read_json(key)
            except (KeyNotFoundError, ValueError):
                continue
            if not isinstance(doc, Mapping) or doc.get("viewpoint_id") not in (None, viewpoint.id):
                continue
            self._fold_sidecar(state, key, doc, now)
            folded += 1
            thumb = doc.get("thumb")
            if key.startswith("frames/") and isinstance(thumb, Mapping) and thumb.get("key"):
                tkey = str(thumb["key"])  # its sidecar is written last, so it says "complete"
                if not self.storage.exists(sidecar_key(tkey)):
                    self._queue_thumb(state, key[: -len(".json")] + ".jpg", tkey, now)
        state.known_sidecars.extend(fresh)
        if full:
            recent = [t for t in get_times(state.gets) if (now - t).total_seconds() < 3600]
            state.seeded = {
                "at": isoformat_z(now, "seconds"),
                "sidecars_read": folded,
                "latest_key": (state.last_image or {}).get("key"),
                "gets_last_hour": len(recent),
            }
        return folded

    def _fold_sidecar(
        self, state: CameraState, key: str, doc: Mapping[str, Any], now: datetime
    ) -> None:
        jpg = key[: -len(".json")] + ".jpg"
        sha = doc.get("sha256")
        if isinstance(sha, str) and sha:
            state.remember_sha(sha, jpg)
        fetched = parse_time(doc.get("fetched_at_utc"))
        if (
            fetched is not None
            and (now - fetched).total_seconds() < GET_HISTORY_S
            and not state.has_get_near(fetched)
        ):
            state.add_get(fetched, status="ok", source=f"sidecar:{doc.get('capture_version')}")
        last = state.last_image or {}
        last_fetched = parse_time(last.get("fetched_at_utc"))
        if fetched is not None and (last_fetched is None or fetched > last_fetched):
            state.last_image = {
                "key": jpg,
                "sha256": sha,
                "etag": doc.get("etag"),
                "last_modified": doc.get("http_last_modified"),
                "content_length": doc.get("content_length"),
                "width": doc.get("original_width"),
                "height": doc.get("original_height"),
                "image_time_utc": doc.get("image_time_utc"),
                "fetched_at_utc": isoformat_z(fetched, "milliseconds"),
                "json_imagedate_raw": doc.get("json_imagedate_raw"),
                "dhash64": doc.get("dhash64"),
                "capture_version": doc.get("capture_version"),
            }
            state.settled_etag = doc.get("etag")
            state.settled_last_modified = doc.get("http_last_modified")
            if doc.get("json_imagedate_raw"):
                state.handled_imagedate_raw = doc["json_imagedate_raw"]

    # -- one camera -----------------------------------------------------------------------
    def poll_interval_s(self, viewpoint: Viewpoint) -> float:
        """The camera's poll spacing: the manifest value, never under 300 s."""
        return max(MIN_POLL_INTERVAL_S, float(viewpoint.truth_camera.polite_caps.poll_interval_s))

    def frame_policy(self, viewpoint: Viewpoint) -> str:
        """``"every"`` or ``"hourly-nearest"`` for this camera (PRD §8.7, :mod:`.retention`)."""
        return frame_policy(viewpoint.id, self.options.every_frame)

    def capture_camera(self, viewpoint: Viewpoint) -> dict[str, Any]:
        """Poll one camera if its slot is due, fetch its image if changed and allowed, store it.

        An hourly-retention camera first has its ready hours decided, and any camera has its
        missing thumbnails made (no network), also when it is not due or the host is in
        back-off. Once a poll has started the state is saved however it ends, so what it
        learnt (poll time, validators, a reserved or completed GET) is never lost.
        """
        now = self.clock()
        hold = self._host_hold(now)
        hourly = self.frame_policy(viewpoint) == POLICY_HOURLY
        if hold is not None and not hourly:
            return {"event": "skipped", "reason": "host_backoff", "until": isoformat_z(hold)}
        try:
            with self.storage.lock(
                f"capture/{viewpoint.id}",
                timeout_s=self.options.lock_timeout_s,
                ttl_s=self.options.lock_ttl_s,
            ):
                state = self._load_state(viewpoint.id)
                first_run = state.created_at is None
                if first_run:
                    state.created_at = isoformat_z(now, "seconds")
                mode = self.options.reconcile
                if first_run or mode == "always" or (mode == "auto" and _is_local(self.storage)):
                    self.reconcile(viewpoint, state, now, full=first_run)
                state.prune(now)
                if hourly:
                    self._promote(viewpoint, state, now, scan=first_run)
                if state.thumbs_pending:
                    self._repair_thumbs(viewpoint, state)
                if hold is not None:
                    self._save_state(state)
                    return {
                        "event": "skipped",
                        "reason": "host_backoff",
                        "until": isoformat_z(hold),
                    }
                due = state.poll_due_at(self.poll_interval_s(viewpoint) + POLL_MARGIN_S)
                if due is not None and due > now:
                    self._save_state(state)
                    return {"event": "not_due", "due_at": isoformat_z(due, "seconds")}
                try:
                    if viewpoint.truth_camera.kind == "ard-aircam":
                        outcome = self._poll_ard(viewpoint, state)
                    else:
                        outcome = self._poll_park(viewpoint, state)
                finally:
                    state.prune(self.clock())
                    self._save_state(state)
                return outcome
        except LockTimeoutError:
            return {"event": "skipped", "reason": "locked"}

    def _promote(
        self, viewpoint: Viewpoint, state: CameraState, now: datetime, *, scan: bool
    ) -> None:
        """Decide the camera's ready hours (PRD §8.7): promote the nearest frame, drop the rest.

        The staging folder is listed on the first run, on every pass with local storage, and
        every ``staging_scan_every_s`` with S3, so a staged copy that a crash left out of the
        state is still decided. A failure is logged and retried next pass; it never stops the
        poll.
        """
        try:
            scanned_at = parse_time(state.staging_scan_at)
            if (
                scan
                or _is_local(self.storage)
                or scanned_at is None
                or (now - scanned_at).total_seconds() >= self.options.staging_scan_every_s
            ):
                known = {str(e.get("key")) for e in state.staged}
                for key in scan_staged(self.storage, viewpoint.id):
                    if key not in known:
                        state.staged.append({"key": key, "source": "scan"})
                state.staging_scan_at = isoformat_z(now, "seconds")
            if not state.staged:
                return
            result = promote_ready_hours(
                self.storage,
                viewpoint.id,
                state.staged,
                now,
                settle_s=self.options.hourly_settle_s,
            )
        except Exception as exc:
            state.bump("staging_error")
            self._log(viewpoint.id, "staging_error", error=f"{type(exc).__name__}: {exc}"[:200])
            return
        state.staged = result.remaining
        for side in result.written_sidecars:
            if side not in state.known_sidecars:
                state.known_sidecars.append(side)
        if result.last is not None:
            state.last_promotion = result.last
        for event in result.events:
            fields = dict(event)
            name = str(fields.pop("event"))
            state.bump(name)
            self._log(viewpoint.id, name, **fields)

    def _start_poll(self, state: CameraState) -> datetime:
        now = self.clock()
        state.last_poll_at = isoformat_z(now, "milliseconds")
        state.polls += 1
        return now

    def _ok(self, state: CameraState) -> None:
        """The feed (JSON or HEAD) answered: reset its error count, lift the poll back-off."""
        state.failures = 0
        state.not_before = None

    def _image_ok(self, state: CameraState) -> None:
        """The image GET answered with a JPEG or a 304: reset the image error count."""
        state.image_failures = 0
        state.not_before = None

    def _error(
        self,
        viewpoint: Viewpoint,
        state: CameraState,
        stage: str,
        *,
        status: int | None = None,
        error: str | None = None,
        response: Fetched | None = None,
    ) -> dict[str, Any]:
        """Count a failure of ``stage`` and back off: 5 min doubling to 1 h, per stage.

        The image stage has its own counter, which a good feed poll does not reset, so a
        feed that answers while the image keeps failing still backs off 5, 10, 20, 40, 60 min.
        """
        now = self.clock()
        if stage == "image":
            state.image_failures += 1
            failures = state.image_failures
        else:
            state.failures += 1
            failures = state.failures
        delay = backoff_delay(
            failures,
            base_s=self.options.error_backoff_base_s,
            cap_s=self.options.error_backoff_cap_s,
        )
        asked = retry_after_s(response) if response is not None else None  # type: ignore[arg-type]
        if asked:
            delay = max(delay, min(asked, 6 * 3600.0))
        until = now + timedelta(seconds=delay)
        state.not_before = isoformat_z(until, "seconds")
        state.last_error = {
            "t": isoformat_z(now, "seconds"),
            "stage": stage,
            "status": status,
            "error": error,
        }
        state.bump("error")
        self._log(viewpoint.id, "error", stage=stage, status=status, error=error, failures=failures)
        self._log(
            viewpoint.id,
            "backoff",
            seconds=delay,
            until=isoformat_z(until, "seconds"),
            failures=failures,
            stage=stage,
        )
        if status in (429, 503):
            self._set_host_hold(max(delay, asked or 0.0), f"HTTP {status} on {stage}")
        return {
            "event": "error",
            "stage": stage,
            "status": status,
            "error": error,
            "backoff_s": delay,
        }

    def _store_failed(
        self,
        viewpoint: Viewpoint,
        state: CameraState,
        exc: Exception,
        *,
        sha: str,
        etag: str | None,
        last_modified: str | None,
        raw_imagedate: str | None,
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        """A 200 JPEG arrived but could not be stored: settle it anyway, then back off.

        The download's validators (and, at an ARD camera, its ``imagedate``) are recorded as
        handled, exactly as for a stored frame, so the next polls see "unchanged" and the same
        bytes are never fetched again (CLAUDE.md: download only when changed). The frame is
        lost; the error is logged with its digest. Polling then backs off 5 min doubling to
        1 h on ``store_failures`` (reset by the next stored download), so a store that keeps
        failing costs one GET per new image at most.
        """
        now = self.clock()
        error = f"{type(exc).__name__}: {exc}"[:200]
        if viewpoint.truth_camera.kind == "ard-aircam":
            state.handled_imagedate_raw = raw_imagedate
        state.settled_etag, state.settled_last_modified = etag, last_modified
        state.store_failures += 1
        delay = backoff_delay(
            state.store_failures,
            base_s=self.options.error_backoff_base_s,
            cap_s=self.options.error_backoff_cap_s,
        )
        until = now + timedelta(seconds=delay)
        state.not_before = isoformat_z(until, "seconds")
        state.last_error = {
            "t": isoformat_z(now, "seconds"),
            "stage": "store",
            "status": 200,
            "error": error,
            "sha256": sha[:12],
        }
        state.bump("error")
        state.bump("store_failed")
        self._log(
            viewpoint.id,
            "error",
            stage="store",
            status=200,
            error=error,
            failures=state.store_failures,
            sha256=sha[:12],
            imagedate_raw=raw_imagedate,
            http_last_modified=last_modified,
            **_validators(context),
        )
        self._log(
            viewpoint.id,
            "backoff",
            seconds=delay,
            until=isoformat_z(until, "seconds"),
            failures=state.store_failures,
            stage="store",
        )
        return {
            "event": "error",
            "stage": "store",
            "status": 200,
            "error": error,
            "backoff_s": delay,
        }

    def _poll_ard(self, viewpoint: Viewpoint, state: CameraState) -> dict[str, Any]:
        tc = viewpoint.truth_camera
        assert tc.json_url is not None
        self._start_poll(state)
        headers: dict[str, str] = {}
        if state.json_etag:
            headers["If-None-Match"] = state.json_etag
        if state.json_last_modified:
            headers["If-Modified-Since"] = state.json_last_modified
        try:
            response = self._request(
                "GET",
                tc.json_url,
                headers,
                self.options.max_json_bytes,
                viewpoint_id=viewpoint.id,
                stage="json",
                poll_state=state,
            )
        except httpx.HTTPError as exc:
            return self._error(viewpoint, state, "json", error=f"{type(exc).__name__}: {exc}"[:200])
        if response.status_code == 304:
            self._ok(state)
            pending = state.imagedate_raw
            if pending and pending != state.handled_imagedate_raw:
                # The feed has not changed since the last poll, but that poll's new image
                # was never fetched (rate cap or error): fetch it now.
                return self._ard_changed(viewpoint, state, {"imagedate": pending})
            state.bump("unchanged")
            self._log(
                viewpoint.id,
                "unchanged",
                reason="json_not_modified",
                imagedate_raw=state.imagedate_raw,
                # A 304 confirms the validators sent: the feed's Last-Modified is unchanged.
                json_last_modified=response.headers.get("Last-Modified")
                or state.json_last_modified,
            )
            return {"event": "unchanged", "reason": "json_not_modified"}
        if response.status_code != 200:
            return self._error(
                viewpoint, state, "json", status=response.status_code, response=response
            )
        feed = parse_feed(response.text)
        raw = feed.get("imagedate")
        if not isinstance(raw, str) or not raw.strip():
            return self._error(viewpoint, state, "json", status=200, error="no imagedate in feed")
        self._ok(state)
        state.json_etag = response.headers.get("ETag")
        state.json_last_modified = response.headers.get("Last-Modified")
        state.imagedate_raw = raw
        if raw == state.handled_imagedate_raw:
            state.bump("unchanged")
            self._log(
                viewpoint.id,
                "unchanged",
                reason="imagedate_same",
                imagedate_raw=raw,
                json_last_modified=state.json_last_modified,
            )
            return {"event": "unchanged", "reason": "imagedate_same", "imagedate_raw": raw}
        return self._ard_changed(viewpoint, state, feed)

    def _ard_changed(
        self, viewpoint: Viewpoint, state: CameraState, feed: Mapping[str, Any]
    ) -> dict[str, Any]:
        """The feed shows an imagedate not yet handled: fetch the image (cap permitting)."""
        tc = viewpoint.truth_camera
        raw = str(feed["imagedate"])
        flags: list[str] = []
        try:
            imagedate_utc: datetime | None = parse_nps_imagedate(raw)
        except ValueError:
            imagedate_utc = None
            flags.append("imagedate_unparsed")
        expected_file = tc.image_url.rsplit("/", 1)[-1]
        if feed.get("imagefilelarge") and feed["imagefilelarge"] != expected_file:
            flags.append("image_file_mismatch")
        if feed.get("_parsed_by") == "regex":
            flags.append("feed_json_malformed")
        context = {
            "json_url": tc.json_url,
            "json_imagedate_raw": raw,
            "json_imagedate_utc": isoformat_z(imagedate_utc) if imagedate_utc else None,
            "json_last_modified": state.json_last_modified,
            "json_etag": state.json_etag,
            "json_imagefilelarge": feed.get("imagefilelarge"),
        }
        return self._fetch_image(
            viewpoint, state, context=context, imagedate_utc=imagedate_utc, flags=flags
        )

    def _poll_park(self, viewpoint: Viewpoint, state: CameraState) -> dict[str, Any]:
        tc = viewpoint.truth_camera
        self._start_poll(state)
        try:
            response = self._request(
                "HEAD",
                tc.image_url,
                None,
                0,
                viewpoint_id=viewpoint.id,
                stage="head",
                poll_state=state,
            )
        except httpx.HTTPError as exc:
            return self._error(viewpoint, state, "head", error=f"{type(exc).__name__}: {exc}"[:200])
        if response.status_code != 200:
            return self._error(
                viewpoint, state, "head", status=response.status_code, response=response
            )
        self._ok(state)
        etag = response.headers.get("ETag")
        last_modified = response.headers.get("Last-Modified")
        state.head_etag, state.head_last_modified = etag, last_modified
        last = state.last_image or {}
        seen_etag = state.settled_etag or last.get("etag")
        seen_lm = state.settled_last_modified or last.get("last_modified")
        if etag and seen_etag:
            same = etag == seen_etag
        else:
            same = bool(last_modified) and last_modified == seen_lm
        if same:
            state.bump("unchanged")
            self._log(
                viewpoint.id,
                "unchanged",
                reason="validators_same",
                http_last_modified=last_modified,
                etag=etag,
            )
            return {
                "event": "unchanged",
                "reason": "validators_same",
                "http_last_modified": last_modified,
            }
        context = {
            "head_last_modified": last_modified,
            "head_etag": etag,
            "head_content_length": response.headers.get("Content-Length"),
        }
        return self._fetch_image(viewpoint, state, context=context, imagedate_utc=None, flags=[])

    # -- the image GET ----------------------------------------------------------------------
    def _fetch_image(
        self,
        viewpoint: Viewpoint,
        state: CameraState,
        *,
        context: dict[str, Any],
        imagedate_utc: datetime | None,
        flags: list[str],
    ) -> dict[str, Any]:
        tc = viewpoint.truth_camera
        caps = tc.polite_caps
        now = self.clock()
        spacing = self.options.pacing_fraction * 3600.0 / max(1, caps.max_images_per_hour)
        decision = cap_decision(
            state.gets, now, max_per_hour=caps.max_images_per_hour, min_spacing_s=spacing
        )
        is_ard = tc.kind == "ard-aircam"
        if not decision.allowed:
            state.bump("rate_capped")
            next_at = (
                isoformat_z(decision.next_allowed_at, "seconds")
                if decision.next_allowed_at
                else None
            )
            self._log(
                viewpoint.id,
                "rate_capped",
                reason=decision.reason,
                gets_last_hour=decision.gets_last_hour,
                next_allowed_at=next_at,
                imagedate_raw=context.get("json_imagedate_raw"),
                http_last_modified=context.get("head_last_modified"),
                **_validators(context),
            )
            return {
                "event": "rate_capped",
                "reason": decision.reason,
                "gets_last_hour": decision.gets_last_hour,
                "next_allowed_at": next_at,
            }

        record = state.add_get(now, status="reserved")
        self._save_state(state)  # the GET counts against the cap even if we crash mid-download
        last = state.last_image or {}
        settled = state.settled_etag or last.get("etag")
        headers = {"If-None-Match": settled} if settled else None
        try:
            response = self._request(
                "GET",
                tc.image_url,
                headers,
                self.options.max_image_bytes,
                viewpoint_id=viewpoint.id,
                stage="image",
            )
        except (httpx.HTTPError, ResponseTooLarge) as exc:
            record["status"] = "error"
            return self._error(
                viewpoint, state, "image", error=f"{type(exc).__name__}: {exc}"[:200]
            )
        fetched_at = self.clock()
        raw_imagedate = context.get("json_imagedate_raw")
        if response.status_code == 304:
            record["status"] = "not-modified"
            self._image_ok(state)
            state.bump("duplicate")
            if is_ard:
                state.handled_imagedate_raw = raw_imagedate
                flags = [*flags, "stale_feed"]
            state.settled_etag = (
                response.headers.get("ETag") or context.get("head_etag") or state.settled_etag
            )
            state.settled_last_modified = (
                response.headers.get("Last-Modified")
                or context.get("head_last_modified")
                or state.settled_last_modified
            )
            self._log(
                viewpoint.id,
                "duplicate",
                reason="not_modified",
                flags=flags,
                imagedate_raw=raw_imagedate,
                **_validators(context),
            )
            return {"event": "duplicate", "reason": "not_modified", "flags": flags}
        if response.status_code != 200:
            record["status"] = "error"
            return self._error(
                viewpoint, state, "image", status=response.status_code, response=response
            )
        body = response.content
        declared = response.headers.get("Content-Length")
        encoding = (response.headers.get("Content-Encoding") or "identity").lower()
        # Content-Length counts the encoded bytes; only an identity body can be compared.
        if (
            encoding == "identity"
            and declared
            and declared.isdigit()
            and int(declared) != len(body)
        ):
            record["status"] = "error"
            return self._error(
                viewpoint,
                state,
                "image",
                status=200,
                error=f"truncated: {len(body)} of {declared} bytes",
            )
        if body[:2] != b"\xff\xd8":
            record["status"] = "error"
            return self._error(
                viewpoint,
                state,
                "image",
                status=200,
                error=f"not a JPEG ({response.headers.get('Content-Type')})",
            )
        record["status"] = "ok"
        self._image_ok(state)
        sha = sha256_hex(body)
        etag = response.headers.get("ETag")
        last_modified = response.headers.get("Last-Modified")
        if sha in state.recent_shas or sha == last.get("sha256"):
            dup_flags = [*flags, "duplicate_sha"]
            if is_ard and raw_imagedate != last.get("json_imagedate_raw"):
                dup_flags.append("stale_feed")
            if is_ard:
                state.handled_imagedate_raw = raw_imagedate
            state.settled_etag, state.settled_last_modified = etag, last_modified
            state.bump("duplicate")
            self._log(
                viewpoint.id,
                "duplicate",
                reason="same_sha256",
                sha256=sha[:12],
                matches_key=state.recent_shas.get(sha) or last.get("key"),
                flags=dup_flags,
                imagedate_raw=raw_imagedate,
                http_last_modified=last_modified,
                **_validators(context),
            )
            return {"event": "duplicate", "reason": "same_sha256", "flags": dup_flags}
        try:
            return self._store(
                viewpoint,
                state,
                body=body,
                sha=sha,
                response=response,
                fetched_at=fetched_at,
                context=context,
                imagedate_utc=imagedate_utc,
                flags=flags,
                gets_last_hour=decision.gets_last_hour + 1,
            )
        except Exception as exc:  # a denied write, a full disk: never fetch these bytes again
            return self._store_failed(
                viewpoint,
                state,
                exc,
                sha=sha,
                etag=etag,
                last_modified=last_modified,
                raw_imagedate=raw_imagedate,
                context=context,
            )

    def _store(
        self,
        viewpoint: Viewpoint,
        state: CameraState,
        *,
        body: bytes,
        sha: str,
        response: Fetched,
        fetched_at: datetime,
        context: dict[str, Any],
        imagedate_utc: datetime | None,
        flags: list[str],
        gets_last_hour: int,
    ) -> dict[str, Any]:
        """Store one new download (PRD §8.7, :mod:`.retention`) and record it in the state.

        - Every camera: a 640 px thumbnail and its sidecar in ``thumbs/`` (none if the image
          does not decode).
        - ``"every"`` cameras: the <= 1920 px frame and sidecar in ``frames/``, as before.
        - ``"hourly-nearest"`` cameras: the <= 1920 px frame and sidecar staged under
          ``cache/staging/frames/``, listed in ``state.staged`` until its hour is decided.

        Writes are create-only and ordered so that a crash between two of them leaves
        nothing that cannot be completed: at an hourly camera the thumbnail's sidecar (which
        holds everything needed to rebuild the frame's) is written before the staged frame;
        at an "every" camera the frame comes first and a thumbnail that could not be written
        is listed in ``state.thumbs_pending``, to be made from the stored frame. The outcome's
        ``key`` is the download's durable file: the frame (``"every"``), else the thumbnail.
        A write that fails otherwise raises; :meth:`_store_failed` then settles the download.
        """
        tc = viewpoint.truth_camera
        last = state.last_image or {}
        policy = self.frame_policy(viewpoint)
        processed = process_image(body, thumb_width=THUMB_WIDTH)
        exif = processed.exif
        last_modified = response.headers.get("Last-Modified")
        times = choose_image_time(
            tz_name=viewpoint.time_zone,
            fetched_at=fetched_at,
            exif_datetime=exif.get("DateTimeOriginal") or exif.get("DateTime"),
            exif_offset=exif.get("OffsetTimeOriginal"),
            exif_subsec=exif.get("SubSecTimeOriginal"),
            imagedate_utc=imagedate_utc,
            last_modified_utc=_http_time(last_modified),
        )
        sun = sun_position(times.image_time_utc, viewpoint.camera.lat, viewpoint.camera.lon)
        all_flags = [*flags, *processed.flags, *times.flags, *gate_flags(processed, sun)]
        dhash_to_previous: int | None = None
        if processed.dhash64 and last.get("dhash64"):
            try:
                dhash_to_previous = hamming64(
                    int(processed.dhash64, 16), int(str(last["dhash64"]), 16)
                )
            except ValueError:
                dhash_to_previous = None
        if (
            processed.original_width
            and last.get("width")
            and (processed.original_width, processed.original_height)
            != (last.get("width"), last.get("height"))
        ):
            all_flags.append("dimensions_changed")
        if fetched_at - times.image_time_utc > STALE_IMAGE_AFTER:
            all_flags.append("stale_image_time")
        if last_modified and last_modified == last.get("last_modified"):
            all_flags.append("same_last_modified_new_bytes")
        fkey = frame_key(viewpoint.id, times.image_time_utc, sha)
        thumb = processed.thumb
        tkey = thumb_key(viewpoint.id, times.image_time_utc, sha) if thumb else None
        skey = staging_key(fkey) if policy == POLICY_HOURLY else None
        sidecar: dict[str, Any] = {
            "viewpoint_id": viewpoint.id,
            "source": "live",
            "source_url": tc.image_url,
            "fetched_at_utc": isoformat_z(fetched_at, "milliseconds"),
            "http_status": response.status_code,
            "http_last_modified": last_modified,
            "etag": response.headers.get("ETag"),
            "content_length": len(body),
            "content_type": response.headers.get("Content-Type"),
            **context,
            "exif": exif,
            **times.to_sidecar(),
            "original_width": processed.original_width,
            "original_height": processed.original_height,
            "original_bytes": processed.original_bytes,
            "sha256": sha,
            "stored": processed.stored_mode,
            "stored_width": processed.stored_width,
            "stored_height": processed.stored_height,
            "stored_bytes": len(processed.stored),
            "stored_sha256": processed.stored_sha256,
            "jpeg_quality": processed.jpeg_quality,
            "dhash64": processed.dhash64,
            "dhash_bits_to_previous": dhash_to_previous,
            "luma_mean": processed.luma_mean,
            "chroma_spread": processed.chroma_spread,
            "sharpness_lapvar": processed.sharpness_lapvar,
            "sun": sun,
            "decode_error": processed.decode_error,
            "flags": sorted(set(all_flags)),
            "previous_key": last.get("key"),
            "gets_last_hour": gets_last_hour,
            "frame_policy": policy,
            "thumb": {"key": tkey, **thumb.to_sidecar()} if thumb and tkey else None,
            "user_agent": user_agent(),
            "capture_version": CAPTURE_VERSION,
            "sidecar_schema": SIDECAR_SCHEMA,
        }
        frame_json = _json_bytes(sidecar)
        thumb_json = (
            _json_bytes(
                thumb_sidecar(sidecar, thumb, frame_key=fkey, policy=policy, staged_key=skey)
            )
            if thumb and tkey
            else None
        )

        def put(key: str, data: bytes, content_type: str) -> bool:
            return self.storage.put_bytes(key, data, content_type=content_type, if_absent=True)

        def put_thumb() -> bool:
            if not (thumb and tkey and thumb_json):
                return False
            wrote_thumb = put(tkey, thumb.data, "image/jpeg")
            put(sidecar_key(tkey), thumb_json, "application/json")
            return wrote_thumb

        thumb_error: str | None = None
        if policy == POLICY_EVERY:
            wrote = put(fkey, processed.stored, "image/jpeg")
            put(sidecar_key(fkey), frame_json, "application/json")
            try:
                put_thumb()
            except Exception as exc:  # the frame is stored; make its thumbnail later
                thumb_error = f"{type(exc).__name__}: {exc}"[:200]
                if tkey:
                    self._queue_thumb(state, fkey, tkey, fetched_at)
            durable = fkey
            written_sidecars = [sidecar_key(fkey)]
        else:
            assert skey is not None
            if thumb_json is not None:
                wrote = put_thumb()
                wrote = put(skey, processed.stored, "image/jpeg") or wrote
                put(sidecar_key(skey), frame_json, "application/json")
            else:  # undecodable, so no thumbnail: the sidecar goes first
                put(sidecar_key(skey), frame_json, "application/json")
                wrote = put(skey, processed.stored, "image/jpeg")
            if not any(e.get("key") == skey for e in state.staged):
                state.staged.append(
                    {
                        "key": skey,
                        "thumb_key": tkey,
                        "sha256": sha,
                        "image_time_utc": sidecar["image_time_utc"],
                        "at": isoformat_z(fetched_at, "seconds"),
                    }
                )
            durable = tkey or skey
            written_sidecars = []
        if tkey and thumb_error is None:
            written_sidecars.append(sidecar_key(tkey))
        state.store_failures = 0
        state.remember_sha(sha, durable)
        for side_key in written_sidecars:
            if side_key not in state.known_sidecars:
                state.known_sidecars.append(side_key)
        state.last_image = {
            "key": durable,
            "frame_key": fkey,
            "thumb_key": tkey,
            "frame_policy": policy,
            "sha256": sha,
            "etag": sidecar["etag"],
            "last_modified": last_modified,
            "content_length": len(body),
            "width": processed.original_width,
            "height": processed.original_height,
            "image_time_utc": sidecar["image_time_utc"],
            "fetched_at_utc": sidecar["fetched_at_utc"],
            "json_imagedate_raw": context.get("json_imagedate_raw"),
            "dhash64": processed.dhash64,
            "capture_version": CAPTURE_VERSION,
        }
        if tc.kind == "ard-aircam":
            state.handled_imagedate_raw = context.get("json_imagedate_raw")
        state.settled_etag, state.settled_last_modified = sidecar["etag"], last_modified
        event = "stored" if wrote else "duplicate"
        state.bump(event)
        summary = {
            "key": durable,
            "frame_policy": policy,
            "frame_key": fkey,
            "thumb_key": tkey,
            **({"staged_key": skey} if skey else {}),
            "original_bytes": processed.original_bytes,
            "stored_bytes": len(processed.stored),
            "thumb_bytes": len(thumb.data) if thumb else None,
            "width": processed.original_width,
            "height": processed.original_height,
            "stored_width": processed.stored_width,
            "image_time_utc": sidecar["image_time_utc"],
            "image_time_source": times.source,
            "disagreement_min": times.disagreement_min,
            "flags": sidecar["flags"],
            "gets_last_hour": gets_last_hour,
            **_validators(context),
            **({"thumb_error": thumb_error} if thumb_error else {}),
        }
        self._log(viewpoint.id, event, **summary, **({} if wrote else {"reason": "key_exists"}))
        return {"event": event, **summary}

    # -- thumbnails a write failure or a crash left out (PRD §8.7) --------------------------
    def _queue_thumb(
        self, state: CameraState, frame_key: str, thumb_key: str, when: datetime
    ) -> None:
        """List a stored frame whose thumbnail is missing; oldest dropped past the limit."""
        if any(p.get("frame_key") == frame_key for p in state.thumbs_pending):
            return
        state.thumbs_pending.append(
            {
                "frame_key": frame_key,
                "thumb_key": thumb_key,
                "attempts": 0,
                "at": isoformat_z(when, "seconds"),
            }
        )
        while len(state.thumbs_pending) > THUMBS_PENDING_LIMIT:
            dropped = state.thumbs_pending.pop(0)
            state.bump("thumb_given_up")
            self._log(state.viewpoint_id, "thumb_given_up", reason="queue_full", **dropped)

    def _repair_thumbs(self, viewpoint: Viewpoint, state: CameraState) -> None:
        """Make missing thumbnails from stored frames, a few per pass (no network).

        Each pending frame is read back, thumbnailed exactly as live capture does (640 px,
        Lanczos, q85, original EXIF), and written create-only with a sidecar built from the
        frame's (``thumb_made_from: "stored_frame"``); a thumbnail JPEG that exists without its
        sidecar keeps its bytes and gets the sidecar. The frame's own sidecar still describes
        the thumbnail that could not be written. A frame that is gone, or that fails
        :data:`THUMB_REPAIR_ATTEMPTS` times, is dropped and logged (``thumb_given_up``); every
        repair is logged (``thumb_repaired``).
        """
        policy = self.frame_policy(viewpoint)
        kept: list[dict[str, Any]] = []
        budget = THUMB_REPAIRS_PER_PASS
        for entry in state.thumbs_pending:
            fkey, tkey = str(entry.get("frame_key")), str(entry.get("thumb_key"))
            if budget <= 0:
                kept.append(entry)
                continue
            budget -= 1
            try:
                if self.storage.exists(sidecar_key(tkey)):
                    continue
                if not self.storage.exists(fkey):
                    state.bump("thumb_given_up")
                    self._log(viewpoint.id, "thumb_given_up", reason="frame_missing", **entry)
                    continue
                doc = self.storage.read_json(sidecar_key(fkey))
                if self.storage.exists(tkey):  # keep the thumbnail live capture made
                    thumb = describe_thumbnail(self.storage.get_bytes(tkey))
                    made_from = "live_capture"
                else:
                    thumb = thumbnail_of_stored(self.storage.get_bytes(fkey))
                    made_from = "stored_frame"
                tdoc = thumb_sidecar(
                    {**doc, "thumb_made_from": made_from},
                    thumb,
                    frame_key=fkey,
                    policy=doc.get("frame_policy") or policy,
                )
                tdoc["full_frame"]["key"] = fkey  # it is stored: we just read it
                self.storage.put_bytes(tkey, thumb.data, content_type="image/jpeg", if_absent=True)
                self.storage.put_bytes(
                    sidecar_key(tkey),
                    _json_bytes(tdoc),
                    content_type="application/json",
                    if_absent=True,
                )
            except Exception as exc:
                entry = {**entry, "attempts": int(entry.get("attempts", 0)) + 1}
                entry["last_error"] = f"{type(exc).__name__}: {exc}"[:200]
                if entry["attempts"] >= THUMB_REPAIR_ATTEMPTS:
                    state.bump("thumb_given_up")
                    self._log(viewpoint.id, "thumb_given_up", reason="attempts", **entry)
                else:
                    kept.append(entry)
                continue
            if sidecar_key(tkey) not in state.known_sidecars:
                state.known_sidecars.append(sidecar_key(tkey))
            state.bump("thumb_repaired")
            self._log(viewpoint.id, "thumb_repaired", frame_key=fkey, thumb_key=tkey)
        state.thumbs_pending = kept

    # -- passes -----------------------------------------------------------------------------
    def run_pass(
        self,
        viewpoints: Sequence[Viewpoint],
        *,
        wait_for_due_s: float = 0.0,
        time_left: Callable[[], float] | None = None,
        min_time_left_s: float = 25.0,
    ) -> dict[str, Any]:
        """Visit every camera once, earliest-due first, waiting up to ``wait_for_due_s`` for a slot.

        ``time_left`` (Lambda's remaining time) stops the pass before the deadline. When logs
        are batched (S3), every record of the pass, the ``pass`` record included, goes out in
        one ``append_jsonl_many`` per log file when the pass ends, also if it fails; records an
        earlier pass could not write go first. Once less than ``2 x min_time_left_s`` is left,
        the records so far are written before each further camera, so a hard timeout loses
        at most one camera's records.
        """
        if not self._batch_logs() or self._log_buffer is not None:
            return self._run_pass(viewpoints, wait_for_due_s, time_left, min_time_left_s)
        self._log_buffer, self._log_carry = list(self._log_carry), []
        try:
            return self._run_pass(viewpoints, wait_for_due_s, time_left, min_time_left_s)
        finally:
            self._flush_logs()

    def _run_pass(
        self,
        viewpoints: Sequence[Viewpoint],
        wait_for_due_s: float,
        time_left: Callable[[], float] | None,
        min_time_left_s: float,
    ) -> dict[str, Any]:
        started = self.clock()
        t0 = self.monotonic()
        order: list[tuple[datetime, Viewpoint]] = []
        for vp in viewpoints:
            try:
                state = self._load_state(vp.id)
                due = state.poll_due_at(self.poll_interval_s(vp) + POLL_MARGIN_S) or started
            except Exception:  # an unreadable state file: let the camera's own try report it
                due = started
            order.append((due, vp))
        order.sort(key=lambda item: (item[0], item[1].id))
        outcomes: dict[str, dict[str, Any]] = {}
        for due, vp in order:
            if self._stop:
                outcomes[vp.id] = {"event": "skipped", "reason": "stopping"}
                continue
            if time_left is not None and time_left() < min_time_left_s:
                outcomes[vp.id] = {"event": "skipped", "reason": "time_left"}
                continue
            if self._log_buffer and time_left is not None and time_left() < 2 * min_time_left_s:
                self._flush_logs(keep_batching=True)  # close to the deadline: write what we have
            wait = (due - self.clock()).total_seconds()
            if 0 < wait <= wait_for_due_s:
                self.sleep(wait + 0.05)
            try:
                outcomes[vp.id] = self.capture_camera(vp)
            except Exception as exc:  # one broken camera must not stop the others
                outcomes[vp.id] = {
                    "event": "error",
                    "stage": "internal",
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                }
                self._log(vp.id, "error", stage="internal", error=outcomes[vp.id]["error"])
        counts = Counter(o["event"] for o in outcomes.values())
        summary = {
            "started_at": isoformat_z(started, "seconds"),
            "duration_s": round(self.monotonic() - t0, 2),
            "runner": self.options.runner,
            "cameras": len(order),
            "counts": dict(sorted(counts.items())),
            "errors": counts.get("error", 0),
            "outcomes": outcomes,
        }
        self._log(
            None,
            "pass",
            cameras=len(order),
            counts=summary["counts"],
            duration_s=summary["duration_s"],
            outcomes={k: v.get("event") for k, v in outcomes.items()},
        )
        return summary

    def request_stop(self) -> None:
        """Ask a running loop to stop after the camera it is on."""
        self._stop = True

    def _on_signal(self, signum: int, frame: FrameType | None) -> None:
        self._signals += 1
        self._stop = True
        if self._signals > 1:
            raise KeyboardInterrupt

    def loop(
        self,
        viewpoints: Sequence[Viewpoint],
        *,
        interval_s: float = 300.0,
        jitter_s: float = 30.0,
        max_passes: int | None = None,
        install_signals: bool = True,
        before_pass: Callable[[], None] | None = None,
        idle: Callable[[Callable[[], float], Callable[[], bool]], Any] | None = None,
    ) -> int:
        """Run passes every ``interval_s`` plus 0..``jitter_s`` s until Ctrl-C or SIGTERM.

        The first Ctrl-C finishes the current camera, writes state and exits; a second
        one interrupts at once. ``before_pass`` runs before every pass; whatever it raises
        (the CLI passes the foreign-fetcher check) is logged and ends the loop. Returns the
        number of passes run.

        ``idle(time_left, stopping)`` runs between passes, in this process and so inside the
        host-wide NPS-fetcher lock (the CLI passes the archive queue, D-023). ``time_left()``
        is the seconds until the next pass is due; ``idle`` must return before then, which
        is how queued archive work never delays a live poll. An exception it raises is
        logged (``idle_error``) and never stops live capture.
        """
        previous: dict[int, Any] = {}
        if install_signals:
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, self._on_signal)
        passes = 0
        try:
            while not self._stop:
                start = self.monotonic()
                if before_pass is not None:
                    try:
                        before_pass()
                    except Exception as exc:
                        self._log(None, "loop_stopped", error=f"{type(exc).__name__}: {exc}"[:300])
                        raise
                self.run_pass(viewpoints, wait_for_due_s=60.0)
                passes += 1
                if max_passes is not None and passes >= max_passes:
                    break
                target = start + interval_s + self.rng.uniform(0.0, max(0.0, jitter_s))
                if idle is not None and not self._stop:
                    try:
                        idle(lambda t=target: t - self.monotonic(), lambda: self._stop)
                    except Exception as exc:  # archive work must never stop live capture
                        self._log(None, "idle_error", error=f"{type(exc).__name__}: {exc}"[:300])
                while not self._stop:
                    remaining = target - self.monotonic()
                    if remaining <= 0.001:  # never spin on a sub-millisecond remainder
                        break
                    self.sleep(min(1.0, remaining))
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        return passes


# --- status ----------------------------------------------------------------------------------


def status_report(
    storage: Storage,
    viewpoints: Iterable[Viewpoint],
    *,
    now: datetime,
    every_frame: Iterable[str] = EVERY_FRAME_VIEWPOINTS,
) -> dict[str, Any]:
    """Persisted capture state per camera plus today's event counts (no network).

    Each camera row also gives its PRD §8.7 frame policy, how many full-size frames are
    staged awaiting their hour's decision, and the last decision. ``frame_policy`` is what
    this code would do; ``frame_policy_recorded`` is what the writer of the newest frame did
    (from the state; code older than PRD §8.7 storage kept every frame), which differs while
    an older snapshot is still the one running.
    """
    every = set(every_frame)
    cameras = []
    for vp in viewpoints:
        doc = storage.read_json(camera_state_key(vp.id), default=None)
        if not isinstance(doc, Mapping):
            cameras.append({"viewpoint_id": vp.id, "state": "none"})
            continue
        state = CameraState.from_dict(doc)
        last = state.last_image or {}
        image_time = parse_time(last.get("image_time_utc"))
        due = state.poll_due_at(
            max(MIN_POLL_INTERVAL_S, float(vp.truth_camera.polite_caps.poll_interval_s))
        )
        recent = [t for t in get_times(state.gets) if (now - t).total_seconds() < 3600]
        cameras.append(
            {
                "viewpoint_id": vp.id,
                "last_poll_at": state.last_poll_at[:19] if state.last_poll_at else None,
                "last_image_key": last.get("key"),
                "last_image_time_utc": last.get("image_time_utc"),
                "last_image_age_min": round((now - image_time).total_seconds() / 60.0, 1)
                if image_time
                else None,
                "gets_last_hour": len(recent),
                "failures": state.failures,
                "image_failures": state.image_failures,
                "not_before": state.not_before,
                "next_poll_at": isoformat_z(due, "seconds") if due and due > now else None,
                "counters": state.counters,
                "seeded": state.seeded,
                "last_error": state.last_error,
                "frame_policy": frame_policy(vp.id, every),
                "frame_policy_recorded": last.get("frame_policy")
                or (POLICY_EVERY if last else None),
                "last_capture_version": last.get("capture_version"),
                "staged": len(state.staged),
                "last_promotion": state.last_promotion,
                "store_failures": state.store_failures,
                "thumbs_pending": len(state.thumbs_pending),
            }
        )
    events: Counter[str] = Counter()
    try:
        data = storage.get_bytes(capture_log_key(now))
        for record in iter_jsonl_bytes(data):
            worker = record.get("worker") or ("stopgap" if "vp" in record else "?")
            events[f"{worker}:{record.get('event')}"] += 1
    except KeyNotFoundError:
        pass
    return {
        "now": isoformat_z(now, "seconds"),
        "storage": repr(storage),
        "cameras": cameras,
        "events_today": dict(sorted(events.items())),
    }
