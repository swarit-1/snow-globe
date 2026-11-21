"""The archive backfill queue, worked in the live loop's idle time (PRD §11.2, §15.3; D-023).

D-023: archive backfill must never run as a second NPS fetcher beside live capture. So
``snow-globe-capture loop --archive-queue <path>`` does queued archive work itself, between its
live passes, in the same process and so inside the same host-wide NPS-fetcher lock:

- **One request at a time, >= 5 s apart:** every archive request goes through the
  ``nps-archive`` limiter (:func:`snow_globe_capture.archive.archive_limiter`), and a step sends
  one request. (The one exception: a day's search whose results span several pages sends
  each further page only if it, too, fits before the next pass. A day holds at most 96
  frames, under the 300-a-page size, so it does not happen in practice.)
- **Never delaying a due live poll:** a request starts only if the limiter's wait plus the
  longest a request may take (:data:`STEP_MAX_S`: a :data:`REQUEST_DEADLINE_S` deadline on
  the body plus the client's 10 s read timeout) plus :data:`STEP_MARGIN_S` fit before the
  next pass is due.
- **One back-off for www.nps.gov** (:mod:`.host_backoff`): no step starts while a hold set by
  a live or an archive HTTP 429/503 is in force, and an archive 429/503 holds live polling.
- **The existing rules:** the held-out guard (:mod:`.heldout`: a queued day on or after
  1 Jun 2026 is refused when added and when worked, and every asset is checked again by
  :func:`~snow_globe_capture.archive.backfill_assets`); a persisted request budget per UTC day
  (:data:`DEFAULT_DAILY_BUDGET`); the per-viewpoint size cap (``max_gb``) and a total cap over
  every viewpoint's backfill (:data:`DEFAULT_TOTAL_CAP_GB`, PRD §15.3 "a total size cap");
  resumable state (``state/archive/<id>.json`` lists what is stored, so nothing is downloaded
  twice, and the queue file records each entry's position).

A queue entry is one day to backfill at one viewpoint, with options: which assets
(``hires`` and/or ``thumbmedium``), which frames (``all``, ``hourly`` or ``midday``) and a
minimum NPS Sun elevation (``--daytime-only`` is 0°). Working it is that day's one
``Search`` (or the cached one), then one asset GET per step. A finished entry keeps only
counts (the per-asset record is ``state/archive/<id>.json``), so the file stays small.

The queue is a JSON file (default ``$SNOW_GLOBE_DATA_DIR/state/archive/queue.json``), edited
under an ``flock`` on ``<file>.lock`` and replaced atomically, so
``snow-globe-capture archive queue add|list|remove|resume`` can edit it while the loop runs,
without contacting nps.gov.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
from snow_globe_common import paths
from snow_globe_common.manifest import Viewpoint
from snow_globe_common.paths import capture_log_key
from snow_globe_common.storage import Storage
from snow_globe_common.timeutil import isoformat_z, utcnow

from . import CAPTURE_VERSION
from .archive import (
    ArchiveAsset,
    ArchiveClient,
    ArchiveError,
    BudgetExhausted,
    RequestBudget,
    archive_limiter,
    archive_state_key,
    backfill_assets,
    cached_search,
    pick_assets,
)
from .heldout import HELD_OUT_FROM
from .host_backoff import host_hold_until

__all__ = [
    "CONNECT_TIMEOUT_S",
    "DEFAULT_DAILY_BUDGET",
    "DEFAULT_TOTAL_CAP_GB",
    "KINDS",
    "PICKS",
    "QUEUE_SCHEMA",
    "READ_TIMEOUT_S",
    "REQUEST_DEADLINE_S",
    "STEP_MARGIN_S",
    "STEP_MAX_S",
    "ArchiveQueue",
    "QueueRunner",
    "default_queue_path",
    "entry_id",
    "make_entry",
    "queue_summary",
]

QUEUE_SCHEMA = 1
KINDS = ("hires", "thumbmedium")
PICKS = ("all", "hourly", "midday")
#: Requests the queue may send per UTC day (a persisted :class:`RequestBudget`).
DEFAULT_DAILY_BUDGET = 1000
#: The queue stops once every viewpoint's archive backfill together has downloaded this much
#: (``bytes_downloaded`` summed over ``state/archive/<id>.json``; PRD §15.3 "a total size cap").
DEFAULT_TOTAL_CAP_GB = 5.0
#: A body still arriving after this long is abandoned as an error (then backed off).
REQUEST_DEADLINE_S = 20.0
#: The queue client's read timeout (between two chunks) and connect timeout.
READ_TIMEOUT_S = 10.0
CONNECT_TIMEOUT_S = 5.0
#: The longest one request can take once it has started: the body deadline is checked as
#: each chunk arrives, and the next chunk may take up to the read timeout.
STEP_MAX_S = REQUEST_DEADLINE_S + READ_TIMEOUT_S
#: Kept free before a due pass on top of STEP_MAX_S.
STEP_MARGIN_S = 5.0
#: An entry whose search, or whose steps, fail this many times is marked failed.
MAX_ENTRY_ERRORS = 3
#: Failed downloads are retried by sweeping the entry again, at most this many sweeps.
MAX_SWEEPS = 3
_OPEN = ("pending", "active")
#: No further work without ``archive queue resume`` ("stopped": a size cap was reached).
_FINISHED = ("done", "refused", "failed", "stopped")
#: Work an entry's own steps can no longer change: its progress is compacted to counts.
_SETTLED = ("done", "refused", "failed")


def default_queue_path() -> Path:
    """``$SNOW_GLOBE_DATA_DIR/state/archive/queue.json`` (the folder is not created here)."""
    return paths.data_dir() / "state" / "archive" / "queue.json"


def entry_id(
    viewpoint_id: str, day: str, kinds: Sequence[str], pick: str, min_sun_deg: float | None
) -> str:
    """A readable, deterministic id, so the same work is never queued twice."""
    sun = "any-sun" if min_sun_deg is None else f"sun>={min_sun_deg:g}"
    return f"{viewpoint_id}/{day}/{'+'.join(kinds)}/{pick}/{sun}"


def make_entry(
    viewpoint: Viewpoint,
    day: date,
    *,
    kinds: Sequence[str] = ("hires",),
    pick: str = "hourly",
    min_sun_deg: float | None = None,
    max_gb: float = 1.0,
    convention: str = "label",
    now: datetime | None = None,
) -> dict[str, Any]:
    """A new queue entry; raise ``ValueError`` for a day past the held-out guard or bad options."""
    if viewpoint.truth_camera.archive_code is None:
        raise ValueError(f"{viewpoint.id} has no NPS archive (CLAUDE.md viewpoint table)")
    if day >= HELD_OUT_FROM:
        raise ValueError(
            f"refused: {day.isoformat()} is on or after {HELD_OUT_FROM.isoformat()}, the D-019 "
            "held-out guard; the queue never downloads held-out frames (scoring uses "
            "'archive day --held-out-override')"
        )
    bad = [k for k in kinds if k not in KINDS]
    if bad or not kinds:
        raise ValueError(f"kinds must be some of {KINDS}, got {list(kinds)}")
    if pick not in PICKS:
        raise ValueError(f"pick must be one of {PICKS}, got {pick!r}")
    if max_gb <= 0:
        raise ValueError("max_gb must be positive")
    ordered = [k for k in KINDS if k in kinds]
    return {
        "id": entry_id(viewpoint.id, day.isoformat(), ordered, pick, min_sun_deg),
        "viewpoint_id": viewpoint.id,
        "date": day.isoformat(),
        "kinds": ordered,
        "pick": pick,
        "min_sun_deg": min_sun_deg,
        "max_gb": max_gb,
        "convention": convention,
        "added_at": isoformat_z(now or utcnow(), "seconds"),
        "status": "pending",
        "progress": {},
    }


# --- the queue file ------------------------------------------------------------------------


class ArchiveQueue:
    """The queue file: read without a lock (it is replaced atomically), edited under ``flock``."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser()
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def __repr__(self) -> str:
        return f"ArchiveQueue({str(self.path)!r})"

    def read(self) -> dict[str, Any]:
        """The queue document (an empty queue if the file does not exist yet)."""
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema": QUEUE_SCHEMA, "entries": []}
        if not isinstance(doc, dict) or not isinstance(doc.get("entries"), list):
            raise ValueError(f"{self.path} is not an archive queue file")
        return doc

    @contextmanager
    def edit(self) -> Iterator[dict[str, Any]]:
        """Read-modify-write the queue under its lock; the change is written on exit."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            doc = self.read()
            yield doc
            doc["schema"] = QUEUE_SCHEMA
            doc["updated_at"] = isoformat_z(utcnow(), "seconds")
            self._write(doc)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _write(self, doc: Mapping[str, Any]) -> None:
        data = (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode()
        fd, tmp = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def add(self, entries: Sequence[Mapping[str, Any]]) -> tuple[list[str], list[str]]:
        """Append new entries; returns (added ids, ids already queued)."""
        added: list[str] = []
        present: list[str] = []
        with self.edit() as doc:
            ids = {e.get("id") for e in doc["entries"]}
            for entry in entries:
                if entry["id"] in ids:
                    present.append(entry["id"])
                    continue
                doc["entries"].append(dict(entry))
                ids.add(entry["id"])
                added.append(entry["id"])
        return added, present

    def resume(
        self, ids: Sequence[str] = (), *, every: bool = False, max_gb: float | None = None
    ) -> list[str]:
        """Set stopped or failed entries back to work (all of them with ``every``); returns ids.

        ``max_gb`` raises the entry's per-viewpoint size cap, which a "stopped" entry needs
        (its cap was reached). An entry whose progress was compacted starts again from its
        search; ``state/archive/<id>.json`` still keeps what it stored from being fetched again.
        """
        if max_gb is not None and max_gb <= 0:
            raise ValueError("max_gb must be positive")
        wanted = set(ids)
        resumed: list[str] = []
        with self.edit() as doc:
            for entry in doc["entries"]:
                if entry.get("status") not in ("stopped", "failed"):
                    continue
                if not (every or entry.get("id") in wanted):
                    continue
                progress = entry.get("progress") or {}
                for name in ("stopped", "search_errors", "internal_errors", "search_cut_short"):
                    progress.pop(name, None)
                if not isinstance(progress.get("selected"), list):
                    progress = {"requests": progress.get("requests", 0)}
                entry["progress"] = progress
                entry["status"] = "active" if "selected" in progress else "pending"
                entry.pop("reason", None)
                if max_gb is not None:
                    entry["max_gb"] = float(max_gb)
                resumed.append(str(entry.get("id")))
        return resumed

    def remove(
        self, ids: Sequence[str] = (), *, finished: bool = False, everything: bool = False
    ) -> list[str]:
        """Remove entries by id, or every finished one (done, refused, failed or stopped), or all.

        Returns the removed ids. Removing an entry only stops future work on it: what it
        stored stays, and ``state/archive/<id>.json`` still keeps those assets from being
        fetched again.
        """
        wanted = set(ids)
        removed: list[str] = []
        with self.edit() as doc:
            kept = []
            for entry in doc["entries"]:
                if (
                    everything
                    or entry.get("id") in wanted
                    or (finished and entry.get("status") in _FINISHED)
                ):
                    removed.append(str(entry.get("id")))
                else:
                    kept.append(entry)
            doc["entries"] = kept
        return removed


def _counts(progress: Mapping[str, Any]) -> dict[str, int]:
    """Outcome counts of an entry: from its per-asset outcomes, or kept as counts once settled."""
    if isinstance(progress.get("outcome_counts"), Mapping):
        return {str(k): int(v) for k, v in progress["outcome_counts"].items()}
    outcomes = progress.get("outcomes") or {}
    return dict(Counter(outcomes.values()))


def _compact(progress: dict[str, Any]) -> None:
    """Replace a settled entry's asset list and per-asset outcomes by their counts."""
    selected = progress.pop("selected", None)
    outcomes = progress.pop("outcomes", None)
    if isinstance(selected, list):
        progress["selected_count"] = len(selected)
    if isinstance(outcomes, Mapping):
        progress["outcome_counts"] = dict(Counter(str(v) for v in outcomes.values()))


def _selected_count(progress: Mapping[str, Any]) -> int | None:
    selected = progress.get("selected")
    if isinstance(selected, list):
        return len(selected)
    count = progress.get("selected_count")
    return int(count) if isinstance(count, int) else None


def queue_summary(queue: ArchiveQueue) -> dict[str, Any]:
    """Progress of every entry and the runner's last heartbeat (for ``status``; no network)."""
    try:
        doc = queue.read()
    except (OSError, ValueError) as exc:
        return {"path": str(queue.path), "error": f"{type(exc).__name__}: {exc}"[:200]}
    rows = []
    for entry in doc["entries"]:
        progress = entry.get("progress") or {}
        selected = _selected_count(progress)
        pairs = None if selected is None else selected * len(entry.get("kinds") or [])
        rows.append(
            {
                "id": entry.get("id"),
                "status": entry.get("status"),
                "pairs": pairs,
                "next": progress.get("next"),
                "sweep": progress.get("sweep"),
                "outcomes": _counts(progress),
                "bytes": progress.get("bytes", 0),
                "requests": progress.get("requests", 0),
                "stopped": progress.get("stopped"),
                "reason": entry.get("reason"),
            }
        )
    by_status = Counter(str(r["status"]) for r in rows)
    return {
        "path": str(queue.path),
        "entries": len(rows),
        "by_status": dict(sorted(by_status.items())),
        "runner": doc.get("runner"),
        "rows": rows,
    }


# --- working the queue ------------------------------------------------------------------


def _queue_client(storage: Storage, runner: str = "cli-loop") -> ArchiveClient:
    """The production archive client of the queue: polite UA, short timeouts, a body deadline,
    and no redirect followed (a 3xx is an error: :meth:`.archive.ArchiveClient._send`)."""
    from snow_globe_common.http import polite_client

    return ArchiveClient(
        client=polite_client(
            timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
            follow_redirects=False,
        ),
        limiter=archive_limiter(),
        storage=storage,
        request_deadline_s=REQUEST_DEADLINE_S,
        runner=runner,
    )


class QueueRunner:
    """Works the queue one archive request at a time, in the gaps between live passes."""

    def __init__(
        self,
        storage: Storage,
        viewpoints: Mapping[str, Viewpoint],
        queue: ArchiveQueue,
        *,
        archive: ArchiveClient | None = None,
        daily_budget: int = DEFAULT_DAILY_BUDGET,
        total_cap_gb: float | None = DEFAULT_TOTAL_CAP_GB,
        clock: Callable[[], datetime] = utcnow,
        step_max_s: float = STEP_MAX_S,
        margin_s: float = STEP_MARGIN_S,
        runner: str = "cli-loop",
    ) -> None:
        self.storage = storage
        self.viewpoints = viewpoints
        self.queue = queue
        self._archive = archive
        self.daily_budget = int(daily_budget)
        #: None: no queue-wide cap (the per-viewpoint ``max_gb`` still applies).
        self.total_cap_bytes = None if total_cap_gb is None else int(float(total_cap_gb) * 1e9)
        self.clock = clock
        self.step_max_s = step_max_s
        self.margin_s = margin_s
        self.paused_until_day: date | None = None
        #: Bytes the last step downloaded (bad bodies included), for the total cap.
        self.last_step_bytes = 0
        self._cap_logged = False
        #: Written into the queue's records and its archive client's, as live capture's is.
        self.runner = runner

    @property
    def archive(self) -> ArchiveClient:
        """The archive client, made on first use (a queue with no work never makes one)."""
        if self._archive is None:
            self._archive = _queue_client(self.storage, self.runner)
        return self._archive

    def close(self) -> None:
        """Close the HTTP client, if one was made."""
        if self._archive is not None:
            self._archive.client.close()

    # -- the loop's idle hook -----------------------------------------------------------
    def run_idle(
        self, time_left: Callable[[], float], stopping: Callable[[], bool] = lambda: False
    ) -> dict[str, Any]:
        """Do queue steps while each fits before the next live pass (the loop's ``idle``).

        No step starts while a www.nps.gov hold is in force (``"host_backoff"``: a live or an
        archive 429/503, :mod:`.host_backoff`), while today's budget is spent, or once every
        viewpoint's backfill together has reached the total cap (``"total_cap"``).
        """
        steps: Counter[str] = Counter()

        def fits() -> bool:
            wait = self.archive.limiter.next_allowed_in()
            return wait + self.step_max_s + self.margin_s <= time_left()

        total: int | None = None
        while not stopping():
            today = self.clock().date()
            if self.paused_until_day is not None and today < self.paused_until_day:
                break
            if not self._has_work():
                break
            if self._host_held():
                steps["host_backoff"] += 1
                break
            if self.total_cap_bytes is not None:
                total = self.total_bytes() if total is None else total + self.last_step_bytes
                if total >= self.total_cap_bytes:
                    steps["total_cap"] += 1
                    if not self._cap_logged:  # once per process: the queue waits for the lead
                        self._cap_logged = True
                        self._log(
                            {"reason": f"total cap: {total} >= {self.total_cap_bytes} bytes"},
                            "queue_total_cap",
                        )
                    break
            if not fits():
                break
            outcome = self.step(fits=fits)
            steps[outcome] += 1
            if outcome in ("idle", "paused", "search_cut_short"):
                break
        return dict(steps)

    def _has_work(self) -> bool:
        try:
            doc = self.queue.read()
        except (OSError, ValueError):
            return False
        return any(e.get("status") in _OPEN for e in doc["entries"])

    def _host_held(self) -> bool:
        """True while the shared www.nps.gov hold is in force (an unreadable one counts)."""
        try:
            return host_hold_until(self.storage, self.clock()) is not None
        except Exception:  # fail closed: no archive request without a readable hold state
            return True

    def total_bytes(self) -> int:
        """Bytes every viewpoint's archive backfill has downloaded (``state/archive/<id>.json``)."""
        total = 0
        for viewpoint_id, viewpoint in sorted(self.viewpoints.items()):
            if viewpoint.truth_camera.archive_code is None:
                continue
            doc = self.storage.read_json(archive_state_key(viewpoint_id), default={}) or {}
            total += int(doc.get("bytes_downloaded", 0)) if isinstance(doc, Mapping) else 0
        return total

    # -- one step ---------------------------------------------------------------------
    def step(self, *, fits: Callable[[], bool] | None = None) -> str:
        """Advance the first open entry by one archive request; returns what happened.

        ``"idle"`` (nothing open), ``"searched"``, ``"request"`` (one asset GET), ``"sweep"``,
        ``"done"``, ``"stopped"`` (size cap), ``"paused"`` (today's budget is used up),
        ``"refused"``, ``"failed"``, ``"error"`` or ``"search_cut_short"``.

        ``fits()`` (from :meth:`run_idle`) says whether one more request still fits before
        the next live pass; it is asked before each further page of a paged search.
        """
        self.last_step_bytes = 0
        doc = self.queue.read()
        entry = next((e for e in doc["entries"] if e.get("status") in _OPEN), None)
        if entry is None:
            return "idle"
        work = json.loads(json.dumps(entry))  # a private copy, merged back under the lock
        before = self.archive.stats.requests
        bytes_before = int((work.get("progress") or {}).get("bytes", 0))
        self.archive.budget = (
            RequestBudget(
                self.storage, f"archive-queue-{self.clock().date().isoformat()}", self.daily_budget
            )
            if self.daily_budget > 0
            else None
        )
        try:
            outcome = self._advance(work, fits)
        except Exception as exc:  # a broken entry (say, edited by hand) must not wedge the queue
            progress = work.setdefault("progress", {})
            progress["internal_errors"] = int(progress.get("internal_errors", 0)) + 1
            progress["last_error"] = f"{type(exc).__name__}: {exc}"[:200]
            outcome = "error"
            if progress["internal_errors"] >= MAX_ENTRY_ERRORS:
                work.update(status="failed", reason=f"internal error: {progress['last_error']}")
                outcome = "failed"
        finally:
            sent = self.archive.stats.requests - before
            progress = work.setdefault("progress", {})
            progress["requests"] = int(progress.get("requests", 0)) + sent
            progress["last_step_at"] = isoformat_z(self.clock(), "seconds")
            self.last_step_bytes = max(0, int(progress.get("bytes", 0)) - bytes_before)
            if work.get("status") in _SETTLED:
                _compact(progress)
            self._commit(work, sent)
        if outcome == "paused":
            self.paused_until_day = date.fromordinal(self.clock().date().toordinal() + 1)
        if outcome in ("done", "stopped", "refused", "failed"):
            self._log(work, f"queue_{outcome}")
        return outcome

    def _commit(self, work: Mapping[str, Any], sent: int) -> None:
        with self.queue.edit() as doc:
            for index, entry in enumerate(doc["entries"]):
                if entry.get("id") == work.get("id"):
                    doc["entries"][index] = dict(work)
                    break  # (an entry removed meanwhile stays removed)
            budget = self.archive.budget
            doc["runner"] = {
                "pid": os.getpid(),
                "updated_at": isoformat_z(self.clock(), "seconds"),
                "last_entry": work.get("id"),
                "last_requests": sent,
                "requests_this_process": self.archive.stats.requests,
                "budget": None
                if budget is None
                else {"name": budget.name, "used": budget.used(), "limit": budget.limit},
            }

    def _log(self, entry: Mapping[str, Any], event: str) -> None:
        with suppress(Exception):  # the log never stops the loop
            self.storage.append_jsonl(
                capture_log_key(self.clock()),
                {
                    "worker": "archive",
                    "viewpoint_id": entry.get("viewpoint_id"),
                    "event": event,
                    "queue_entry": entry.get("id"),
                    "status": entry.get("status"),
                    "outcomes": _counts(entry.get("progress") or {}),
                    "reason": entry.get("reason"),
                    "runner": self.runner,
                    "capture_version": CAPTURE_VERSION,
                },
            )

    def _advance(self, entry: dict[str, Any], fits: Callable[[], bool] | None = None) -> str:
        viewpoint = self.viewpoints.get(str(entry.get("viewpoint_id")))
        if viewpoint is None or viewpoint.truth_camera.archive_code is None:
            entry.update(status="failed", reason="unknown viewpoint or no NPS archive")
            return "failed"
        try:
            day = date.fromisoformat(str(entry.get("date")))
        except ValueError:
            entry.update(status="failed", reason=f"bad date {entry.get('date')!r}")
            return "failed"
        if day >= HELD_OUT_FROM:  # the queue file may have been edited by hand
            entry.update(status="refused", reason=f"held-out guard (D-019): {day.isoformat()}")
            return "refused"
        progress = entry.setdefault("progress", {})
        if not isinstance(progress.get("selected"), list):
            outcome = self._search(entry, viewpoint, day, fits)
            if outcome != "cached":
                return outcome
        return self._download(entry, viewpoint)

    def _search(
        self,
        entry: dict[str, Any],
        viewpoint: Viewpoint,
        day: date,
        fits: Callable[[], bool] | None = None,
    ) -> str:
        progress = entry["progress"]
        code = viewpoint.truth_camera.archive_code
        assert code is not None
        before = self.archive.stats.requests
        try:
            assets, meta = cached_search(
                self.storage, self.archive, code, day, today=self.clock().date(), may_continue=fits
            )
        except BudgetExhausted:
            return "paused"
        except ArchiveError as exc:
            progress["search_errors"] = int(progress.get("search_errors", 0)) + 1
            progress["last_error"] = str(exc)[:200]
            if progress["search_errors"] >= MAX_ENTRY_ERRORS:
                entry.update(status="failed", reason=f"search failed: {str(exc)[:160]}")
                return "failed"
            return "error"
        if meta.get("cut_short"):
            # A paged search stopped so as not to reach the next live pass: a partial list
            # would silently drop the rest of the day, so search again in a later gap.
            progress["search_cut_short"] = int(progress.get("search_cut_short", 0)) + 1
            progress["last_error"] = str(meta.get("paging_error"))[:200]
            if progress["search_cut_short"] >= MAX_ENTRY_ERRORS:
                entry.update(
                    status="failed",
                    reason="the day's search has more pages than fit between live passes; "
                    "run 'archive day' with the loop stopped",
                )
                return "failed"
            return "search_cut_short"
        chosen = pick_assets(
            assets, pick=entry.get("pick", "all"), min_sun_deg=entry.get("min_sun_deg")
        )
        progress.update(
            {
                "search": {
                    k: meta.get(k)
                    for k in (
                        "from_cache",
                        "result_count",
                        "complete",
                        "paging_error",
                        "fetched_at",
                    )
                },
                "assets_found": len(assets),
                "selected": [a.to_dict() for a in chosen],
                "next": 0,
                "sweep": 1,
                "outcomes": {},
            }
        )
        entry["status"] = "active"
        return "searched" if self.archive.stats.requests > before else "cached"

    def _download(self, entry: dict[str, Any], viewpoint: Viewpoint) -> str:
        progress = entry["progress"]
        kinds = list(entry.get("kinds") or ["hires"])
        assets = [ArchiveAsset.from_result({"Asset": a}) for a in progress["selected"]]
        pairs = [(asset, kind) for asset in assets for kind in kinds]
        outcomes: dict[str, str] = progress.setdefault("outcomes", {})
        index = int(progress.get("next", 0))
        max_bytes = int(float(entry.get("max_gb", 1.0)) * 1e9)
        while index < len(pairs):
            asset, kind = pairs[index]
            label = f"{asset.asset_id}:{kind}"
            if outcomes.get(label, "failed") != "failed":  # settled in an earlier sweep
                index += 1
                progress["next"] = index
                continue
            before = self.archive.stats.requests
            result = backfill_assets(
                storage=self.storage,
                archive=self.archive,
                viewpoint=viewpoint,
                assets=[asset],
                kinds=(kind,),  # type: ignore[arg-type]
                max_bytes=max_bytes,
                convention=entry.get("convention", "label"),  # type: ignore[arg-type]
                max_attempts=1,
            )
            sent = self.archive.stats.requests - before
            progress["bytes"] = int(progress.get("bytes", 0)) + result.bytes_downloaded
            if result.stopped and result.stopped.startswith("budget"):
                progress["next"] = index  # this pair again tomorrow
                return "paused"
            if result.stopped:
                progress.update(next=index, stopped=result.stopped)
                entry.update(status="stopped", reason=result.stopped)
                return "stopped"
            outcomes[label] = (
                "stored"
                if result.downloaded
                else "existing"
                if result.skipped_existing
                else "refused_held_out"
                if result.refused_held_out
                else "given_up"
                if result.given_up
                else "failed"
            )
            index += 1
            progress["next"] = index
            if sent:
                return "request"
        retry = [label for label, outcome in outcomes.items() if outcome == "failed"]
        if retry and int(progress.get("sweep", 1)) < MAX_SWEEPS:
            progress.update(next=0, sweep=int(progress.get("sweep", 1)) + 1)
            return "sweep"
        entry["status"] = "done"
        return "done"
