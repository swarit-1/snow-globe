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
- **A daily resume of failed day searches** (D-072, D-078): an entry whose day search failed
  :data:`MAX_ENTRY_ERRORS` times, each time with an error worth another try
  (:func:`~snow_globe_capture.archive.is_retryable`: a timeout, a connection error, HTTP
  429/5xx, a body too slow or too large, a body that is not JSON), is set back to work by the
  loop's runner itself, at its first idle on a later UTC day than the failure, at most once a
  UTC day and :data:`AUTO_RESUME_MAX` times in all (:meth:`QueueRunner.auto_resume`, logged as
  ``queue_auto_resumed``). That is the lead's daily ``archive queue resume``, which the
  backfill plan's finish days assume. It sends nothing itself: the entry then waits its turn
  under the budget, the spacing and the one-at-a-time rule above. Every other failure (a
  redirect or another HTTP 4xx among the search's errors, errors counted by code before
  0.3.0, a search cut short, an internal error, a bad entry), a held-out refusal and a size
  cap still wait for ``archive queue resume``. It is on only where asked for:
  ``QueueRunner(auto_resume_max=AUTO_RESUME_MAX)``, as ``snow-globe-capture loop`` builds it;
  the default 0 keeps a runner built by other code (the backfill planner's simulation) to
  the 0.2.x behaviour its own accounting models.

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
import re
import tempfile
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, date, datetime
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
    is_retryable,
    pick_assets,
)
from .heldout import HELD_OUT_FROM
from .host_backoff import host_hold_until
from .state import parse_time

__all__ = [
    "AUTO_RESUME_MAX",
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
    "auto_resumable",
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
#: An entry whose day search failed is set back to work by the loop's runner at most this many
#: times in all, at most once a UTC day and never on the UTC day it failed (D-072, D-078).
AUTO_RESUME_MAX = 3
#: How the reason of an entry failed by its day search starts (the same text since 0.2.0).
SEARCH_FAILED = "search failed: "
#: ``failure_kind`` of an entry failed by its day search (written from 0.3.0 on): 0.3.0 judged
#: every one of its search errors worth another try (:func:`.archive.is_retryable`); or at least
#: one was not (a redirect or another HTTP 4xx); or some were counted by code before 0.3.0,
#: which kept no judgement of them (an entry part-way through its errors at the switch).
#: Only the first is ever resumed by itself.
FAILURE_SEARCH_RETRYABLE = "search_retryable"
FAILURE_SEARCH_NOT_RETRYABLE = "search_not_retryable"
FAILURE_SEARCH_UNCLASSIFIED = "search_unclassified"


def _transport_error_names() -> frozenset[str]:
    """httpx's transport errors by class name: how ``ArchiveClient._send`` words them."""
    names: set[str] = set()
    todo: list[type] = [httpx.TransportError]
    while todo:
        cls = todo.pop()
        names.add(cls.__name__)
        todo.extend(cls.__subclasses__())
    return frozenset(names)


_TRANSPORT_ERRORS = _transport_error_names()
#: ``str()`` of an :class:`~.archive.ArchiveHTTPError`: ``HTTP <status> for <url> ...``.
_HTTP_ERROR_RE = re.compile(r"HTTP (\d{3}) for ")
#: A body too slow or too large, as ``_send`` words it (``ArchiveError: <url> took longer ...``).
_BODY_LIMIT_RE = re.compile(r"ArchiveError: \S+ (took longer than|is larger than) ")
#: A search answer that is not a JSON object (``Search <code> <day>: not JSON``).
_SEARCH_BODY_RE = re.compile(r"Search \S+ \S+: (not JSON|unexpected \w+)$")


def _retryable_text(error: str) -> bool:
    """:func:`.archive.is_retryable` read back from an error's text (``str(exc)``).

    For an entry failed before 0.3.0, which kept only its reason: ``search failed: `` and the
    text of the *last* of its three errors, cut to 160 characters (the same wording since
    0.2.0). The two errors before it left no trace on the entry, so such an entry is judged by
    its last error alone. True only for the shapes :func:`.archive.is_retryable` calls worth
    another try: a transport error (``ReadTimeout: ...``), a body too slow or too large, HTTP
    429 or 5xx, a search answer that is not JSON. A redirect, another HTTP 4xx and any text not
    recognised give False (fail closed: the entry then waits for ``archive queue resume``).
    """
    text = error.strip()
    match = _HTTP_ERROR_RE.match(text)
    if match is not None:
        status = int(match.group(1))
        return status == 429 or status >= 500
    name, sep, _rest = text.partition(":")
    if sep and name in _TRANSPORT_ERRORS:
        return True
    return bool(_BODY_LIMIT_RE.match(text) or _SEARCH_BODY_RE.match(text))


def _auto_resume_history(entry: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    """The entry's automatic resumes so far, oldest first (``auto_resumes``), or None when the
    field is not a list of records (edited by hand: no further automatic resume)."""
    history = entry.get("auto_resumes", [])
    if not isinstance(history, list) or not all(isinstance(h, Mapping) for h in history):
        return None
    return history


def auto_resumable(entry: Any, limit: int = AUTO_RESUME_MAX) -> bool:
    """True for a failed entry the queue runner may set back to work itself (D-072, D-078).

    Its day search failed :data:`MAX_ENTRY_ERRORS` times, each time with an error worth another
    try (``failure_kind: "search_retryable"``; an entry failed before 0.3.0 has no
    ``failure_kind`` and is judged by the last error its reason keeps,
    :func:`_retryable_text`), and it has been set back fewer than ``limit`` times. Never true
    for another failure, nor for a refused or stopped entry, nor for an entry whose
    ``progress`` is not a record (edited by hand: fail closed). *When* it is set back is
    :meth:`QueueRunner.auto_resume`'s rule.
    """
    if not isinstance(entry, Mapping) or entry.get("status") != "failed":
        return False
    progress = entry.get("progress")
    if progress is not None and not isinstance(progress, Mapping):
        return False
    reason = entry.get("reason")
    if not isinstance(reason, str) or not reason.startswith(SEARCH_FAILED):
        return False
    kind = entry.get("failure_kind")
    if kind is None:
        if not _retryable_text(reason[len(SEARCH_FAILED) :]):
            return False
    elif kind != FAILURE_SEARCH_RETRYABLE:
        return False
    history = _auto_resume_history(entry)
    return history is not None and len(history) < limit


def _search_failure_kind(progress: Mapping[str, Any]) -> str:
    """``failure_kind`` of an entry whose day search has just failed for the last time.

    ``search_retryable`` only when 0.3.0 judged *every* one of its ``search_errors`` worth
    another try (``search_errors_retryable`` equals it); ``search_not_retryable`` when one or
    more was not (a redirect or another HTTP 4xx); else ``search_unclassified``: some of its
    errors were counted by code before 0.3.0, which kept no judgement of them (fail closed).
    """
    errors = int(progress.get("search_errors", 0))
    if int(progress.get("search_errors_not_retryable", 0)) > 0:
        return FAILURE_SEARCH_NOT_RETRYABLE
    if int(progress.get("search_errors_retryable", 0)) == errors:
        return FAILURE_SEARCH_RETRYABLE
    return FAILURE_SEARCH_UNCLASSIFIED


def _failed_at(entry: Mapping[str, Any]) -> datetime | None:
    """When the entry failed: ``failed_at`` (written from 0.3.0 on), else its last step's time,
    which is the failing step's (a failed entry takes no further step); None if neither reads."""
    progress = entry.get("progress")
    last_step = progress.get("last_step_at") if isinstance(progress, Mapping) else None
    for value in (entry.get("failed_at"), last_step):
        when = parse_time(value)
        if when is not None:
            return when
    return None


def _auto_resume_due(entry: Any, today: date, limit: int) -> bool:
    """:func:`auto_resumable`, and ``today`` (UTC) is a later UTC day than the failure and than
    each automatic resume before it. An unreadable time counts as today (fail closed)."""
    if not auto_resumable(entry, limit):
        return False
    failed = _failed_at(entry)
    if failed is None or failed.date() >= today:
        return False
    for item in _auto_resume_history(entry) or []:
        at = parse_time(item.get("at"))
        if at is None or at.date() >= today:
            return False
    return True


def _reopen(entry: dict[str, Any], *, max_gb: float | None = None) -> None:
    """Set a stopped or failed entry back to work, as ``archive queue resume`` does.

    Its error counts and its failure are cleared; an entry whose progress was compacted (or
    that never got past its search) starts again from its search, and
    ``state/archive/<id>.json`` still keeps what it stored from being fetched again. Its
    automatic resumes (``auto_resumes``) stay, so the limit counts them all. ``max_gb`` raises
    the per-viewpoint size cap, which a "stopped" entry needs.
    """
    progress = entry.get("progress") or {}
    for name in (
        "stopped",
        "search_errors",
        "search_errors_retryable",
        "search_errors_not_retryable",
        "internal_errors",
        "search_cut_short",
    ):
        progress.pop(name, None)
    if not isinstance(progress.get("selected"), list):
        progress = {"requests": progress.get("requests", 0)}
    entry["progress"] = progress
    entry["status"] = "active" if "selected" in progress else "pending"
    for name in ("reason", "failed_at", "failure_kind"):
        entry.pop(name, None)
    if max_gb is not None:
        entry["max_gb"] = float(max_gb)


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
        search; ``state/archive/<id>.json`` still keeps what it stored from being fetched again
        (:func:`_reopen`). The loop resumes an entry whose day search failed by itself, once a
        UTC day (:meth:`QueueRunner.auto_resume`); this is the lead's way for every other one.
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
                _reopen(entry, max_gb=max_gb)
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


def _runner_auto_resume_max(runner: Any) -> int | None:
    """The ``auto_resume_max`` the queue's last runner recorded (0.3.0 on), else None: a runner
    from before 0.3.0 (or none yet), which never sets anything back to work by itself."""
    value = runner.get("auto_resume_max") if isinstance(runner, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(0, value)


def queue_summary(queue: ArchiveQueue) -> dict[str, Any]:
    """Progress of every entry and the runner's last heartbeat (for ``status``; no network).

    ``auto_resume_max`` is the cap the queue's last runner recorded in its heartbeat (0 when
    its automatic resume is off), or None when that runner predates 0.3.0 (or none has
    stepped yet): such a loop sets nothing back to work by itself. Each row also gives
    ``auto_resumes``, how many times a loop has set the entry back to work itself, and
    ``auto_resumable``: a failed entry that qualifies for the automatic resume
    (:func:`auto_resumable`) under that cap, or under :data:`AUTO_RESUME_MAX` when the cap is
    None. The loop sets it back to work at its first idle on a later UTC day than the failure
    (:meth:`QueueRunner.auto_resume`) only when ``auto_resume_max`` is not None.
    """
    try:
        doc = queue.read()
    except (OSError, ValueError) as exc:
        return {"path": str(queue.path), "error": f"{type(exc).__name__}: {exc}"[:200]}
    cap = _runner_auto_resume_max(doc.get("runner"))
    rows = []
    for entry in doc["entries"]:
        progress = entry.get("progress") or {}
        if not isinstance(progress, Mapping):  # edited by hand: listed with no progress
            progress = {}
        selected = _selected_count(progress)
        pairs = None if selected is None else selected * len(entry.get("kinds") or [])
        history = _auto_resume_history(entry)
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
                "auto_resumes": None if history is None else len(history),
                "auto_resumable": auto_resumable(entry, AUTO_RESUME_MAX if cap is None else cap),
            }
        )
    by_status = Counter(str(r["status"]) for r in rows)
    return {
        "path": str(queue.path),
        "entries": len(rows),
        "by_status": dict(sorted(by_status.items())),
        "runner": doc.get("runner"),
        "auto_resume_max": cap,
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
    """Works the queue one archive request at a time, in the gaps between live passes.

    ``auto_resume_max`` turns on the daily resume of failed day searches (:meth:`auto_resume`):
    the most automatic resumes an entry gets in all. ``snow-globe-capture loop`` passes
    :data:`AUTO_RESUME_MAX`; the default 0 leaves every failed entry for ``archive queue
    resume``, as 0.2.x did, so code that builds a runner to model the loop (the backfill
    planner's ``loop_sim.py``) keeps that model until it asks for this one.
    """

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
        auto_resume_max: int = 0,
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
        #: Automatic resumes allowed per entry in all (:meth:`auto_resume`); 0 (the default)
        #: turns them off. Recorded in the queue's ``runner`` heartbeat, for the listing.
        self.auto_resume_max = max(0, int(auto_resume_max))
        #: The UTC day an automatic resume last failed to run (logged once a day).
        self._auto_resume_error_day: date | None = None

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

        First, entries whose day search failed on an earlier UTC day are set back to work
        (:meth:`auto_resume`; ``"auto_resumed"`` counts them; it sends nothing). No step starts
        while a www.nps.gov hold is in force (``"host_backoff"``: a live or an archive 429/503,
        :mod:`.host_backoff`), while today's budget is spent, or once every viewpoint's backfill
        together has reached the total cap (``"total_cap"``).
        """
        steps: Counter[str] = Counter()
        if not stopping():
            resumed = self._auto_resume_logged()
            if resumed:
                steps["auto_resumed"] += len(resumed)

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

    # -- the daily resume (D-072, D-078) --------------------------------------------------
    def auto_resume(self) -> list[str]:
        """Set entries whose day search failed back to work, once a UTC day; returns their ids.

        The lead's daily ``archive queue resume``, done by the loop: an entry is set back to
        work when :func:`auto_resumable` (its day search failed :data:`MAX_ENTRY_ERRORS` times,
        each time with an error worth another try, and it has been set back fewer than
        ``auto_resume_max`` times in all) and today (UTC) is a later UTC day than its failure
        and than its last automatic resume. So an entry that failed on day D goes back to work
        at the first idle of D + 1 or later, the budget of a new day, as the backfill plan's
        daily resume assumed; it is set back at most once a UTC day. A refused (held-out) or
        stopped (size cap) entry, and every other failure, is left for ``archive queue
        resume``.

        Nothing is sent: the queue file is edited under its lock (:func:`_reopen`, as the
        manual resume does) and the entry waits its turn under the same budget, spacing and
        one-at-a-time rule as any other. Each resume is appended to the entry's
        ``auto_resumes`` (``attempt``, ``at``, the ``reason`` it failed and ``failed_at``) and
        logged as ``queue_auto_resumed`` with the attempt number and the reason it failed.

        Each entry is set back on a copy, so one that cannot be (a hand edit this code did not
        foresee) stays exactly as it was, is logged as ``queue_auto_resume_error`` (at most
        once a UTC day) and never keeps the other entries from being set back.
        """
        if self.auto_resume_max <= 0:
            return []
        now = self.clock()
        today = now.astimezone(UTC).date()
        try:
            current = self.queue.read()
        except (OSError, ValueError):
            return []
        if not any(self._resume_due(e, today) for e in current["entries"]):
            return []  # the usual case: no lock taken, nothing written
        done: list[tuple[dict[str, Any], dict[str, Any]]] = []
        broken: list[tuple[Any, str]] = []
        with self.queue.edit() as doc:
            entries = doc["entries"]
            for index, entry in enumerate(entries):
                if not self._resume_due(entry, today):  # checked again under the lock
                    continue
                try:
                    work, item = self._set_back(entry, now)
                except Exception as exc:  # this entry stays as it was; the others go on
                    broken.append((entry.get("id"), f"{type(exc).__name__}: {exc}"[:200]))
                    continue
                entries[index] = work
                done.append((work, item))
        for entry, item in done:
            self._log(
                entry,
                "queue_auto_resumed",
                reason=item["reason"],
                attempt=item["attempt"],
                attempts_max=self.auto_resume_max,
                failed_at=item["failed_at"],
            )
        for queue_entry, error in broken:
            self._log_resume_error(error, queue_entry)
        return [str(entry.get("id")) for entry, _item in done]

    @staticmethod
    def _set_back(entry: Mapping[str, Any], now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
        """A copy of ``entry`` set back to work by the daily resume, and its ``auto_resumes``
        record; ``entry`` itself is left untouched."""
        work = json.loads(json.dumps(entry))
        failed = _failed_at(work)
        history = list(_auto_resume_history(work) or [])
        item = {
            "attempt": len(history) + 1,
            "at": isoformat_z(now, "milliseconds"),
            "reason": work.get("reason"),
            "failed_at": None if failed is None else isoformat_z(failed, "milliseconds"),
        }
        _reopen(work)
        work["auto_resumes"] = [*history, item]
        return work, item

    def _resume_due(self, entry: Any, today: date) -> bool:
        try:
            return _auto_resume_due(entry, today, self.auto_resume_max)
        except Exception:  # a hand-edited entry must not wedge the queue: never resume it
            return False

    def _log_resume_error(self, error: str, queue_entry: Any = None) -> None:
        """Log ``queue_auto_resume_error`` at most once a UTC day (``queue_entry``: the entry
        that could not be set back, or None when the resume as a whole failed)."""
        today = self.clock().astimezone(UTC).date()
        if self._auto_resume_error_day == today:
            return
        self._auto_resume_error_day = today
        self._log({"id": queue_entry}, "queue_auto_resume_error", reason=error)

    def _auto_resume_logged(self) -> list[str]:
        """:meth:`auto_resume`, whose own failure never stops the queue's work (logged once a
        UTC day as ``queue_auto_resume_error``)."""
        try:
            return self.auto_resume()
        except Exception as exc:
            self._log_resume_error(f"{type(exc).__name__}: {exc}"[:200])
            return []

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
            if work.get("status") == "failed":  # an open entry failed in this step
                work["failed_at"] = isoformat_z(self.clock(), "milliseconds")
            if work.get("status") in _SETTLED:
                _compact(progress)
            self._commit(work, sent)
        if outcome == "paused":
            self.paused_until_day = date.fromordinal(self.clock().date().toordinal() + 1)
        if outcome in ("done", "stopped", "refused", "failed"):
            self._log(work, f"queue_{outcome}")
        return outcome

    def _commit(self, work: Mapping[str, Any], sent: int) -> None:
        """Merge the stepped entry back under the lock and write the runner's heartbeat.

        The heartbeat (``runner``) also says, from 0.3.0 on, which code works the queue
        (``capture_version``) and whether it sets failed day searches back to work itself
        (``auto_resume_max``), so the listing never promises a resume the loop will not do.
        """
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
                "capture_version": CAPTURE_VERSION,
                "auto_resume_max": self.auto_resume_max,
            }

    def _log(self, entry: Mapping[str, Any], event: str, **extra: Any) -> None:
        """One ``capture_log`` record about a queue entry; ``extra`` adds fields (or gives
        ``reason``). ``t`` is the runner's clock when the record is made, as live capture's."""
        with suppress(Exception):  # the log never stops the loop
            now = self.clock()
            self.storage.append_jsonl(
                capture_log_key(now),
                {
                    "t": isoformat_z(now, "milliseconds"),
                    "worker": "archive",
                    "viewpoint_id": entry.get("viewpoint_id"),
                    "event": event,
                    "queue_entry": entry.get("id"),
                    "status": entry.get("status"),
                    "outcomes": _counts(entry.get("progress") or {}),
                    "reason": entry.get("reason"),
                    **extra,
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
            # Each error is judged as it comes (0.3.0 on). Errors counted before 0.3.0 are in
            # search_errors but in neither of these, so they are never taken as worth a try.
            judged = (
                "search_errors_retryable" if is_retryable(exc) else "search_errors_not_retryable"
            )
            progress[judged] = int(progress.get(judged, 0)) + 1
            if progress["search_errors"] >= MAX_ENTRY_ERRORS:
                entry.update(
                    status="failed",
                    reason=f"{SEARCH_FAILED}{str(exc)[:160]}",
                    failure_kind=_search_failure_kind(progress),
                )
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
