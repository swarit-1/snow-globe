"""NPS AirWebCams archive: API client and polite, resumable backfill (PRD §11.2, §15.3).

The API (CLAUDE.md "NPS archive API", found in the page script of
``https://www.nps.gov/AirWebCams/pore`` on 2026-11-19; not a documented public API):

- ``GET  /airwebcams/api/GetAvailableDays/<code>?<cachebuster>``: ``MinDay``, ``MaxDay`` and,
  per year and month, a bit mask of days with frames (bit ``d-1`` set = day ``d``).
- ``POST /airwebcams/api/Search/Execute``: a JSON search; up to 300 assets per page.
- ``GET  /airwebcams/GetAsset/<AssetID>/proxy/hires`` (full size) and ``.../thumbmedium.jpg``.

Politeness, enforced here for every archive request from every process on this machine:

- one request at a time, at least 5 s from the end of one to the start of the next
  (:class:`lookout_common.http.MinIntervalLimiter` ``nps-archive``, ``hold()``). The limiter
  is always the cross-process ``flock`` one on local disk (:func:`archive_limiter`), also
  when ``LOOKOUT_STORAGE=s3``, where ``lookout_common`` would otherwise fall back to an
  in-process limiter;
- error back-off shared by every process (``defer``), honouring ``Retry-After``. A 200
  whose body is not a decodable JPEG counts as an error too (:class:`ArchiveBadBody`);
- one back-off for the whole of www.nps.gov, shared with live capture
  (:mod:`lookout_capture.host_backoff`): an HTTP 429/503 here also pauses live polling, and a
  hold that a live 429/503 set is waited out before any archive request;
- an optional request budget persisted in ``state/`` (the archive check uses 220);
- single-threaded; resumable; an asset already stored is never downloaded again; a total
  size cap (``max_bytes``) stops a backfill before the next download; a failing asset is
  retried at most :data:`MAX_ATTEMPTS_PER_RUN` times per run and
  :data:`MAX_TOTAL_ATTEMPTS` times in all;
- the held-out guard (:mod:`.heldout`, D-019): no archived frame dated on or after
  1 Jun 2026 is requested or read without an explicit, logged scoring override.

Only one NPS fetcher may run per machine; the console commands take
:func:`lookout_capture.fetcher_guard.nps_fetcher_lock` around all of this.
"""

from __future__ import annotations

import io
import json
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
from lookout_common import paths
from lookout_common.http import MinIntervalLimiter, backoff_delay, retry_after_s, user_agent
from lookout_common.manifest import Viewpoint
from lookout_common.paths import archive_key, capture_log_key, sidecar_key, state_key
from lookout_common.storage import Storage
from lookout_common.timeutil import isoformat_z, parse_nps_archive_time, utcnow
from PIL import Image, UnidentifiedImageError

from . import CAPTURE_VERSION
from .fetch import Fetched
from .frames import gate_flags, process_image, sun_position
from .heldout import HeldOutOverride, held_out_reason
from .host_backoff import HOST, extend_host_hold, host_hold_until

__all__ = [
    "API_CODE_CASE",
    "API_ROOT",
    "ARCHIVE_LIMITER_NAME",
    "ARCHIVE_MIN_INTERVAL_S",
    "MAX_ATTEMPTS_PER_RUN",
    "MAX_TOTAL_ATTEMPTS",
    "SEARCH_CACHE_MIN_AGE_DAYS",
    "ArchiveAsset",
    "ArchiveBadBody",
    "ArchiveClient",
    "ArchiveError",
    "ArchiveHTTPError",
    "AvailableDays",
    "BackfillResult",
    "BudgetExhausted",
    "Fetched",
    "RequestBudget",
    "RequestStats",
    "SearchResult",
    "api_code",
    "archive_asset_key",
    "archive_limiter",
    "backfill_assets",
    "cached_search",
    "default_archive_client",
    "pick_assets",
    "search_body",
]

API_ROOT = "https://www.nps.gov/airwebcams/"
ARCHIVE_LIMITER_NAME = "nps-archive"
#: CLAUDE.md asks for archive requests at least 5 s apart, end to start. The limiter keeps its
#: interval on the wall clock; the 50 ms more keeps the log's own stamps (``t`` cut to the
#: millisecond, ``elapsed_ms`` rounded to 0.1 ms, and any wall-clock change during a request)
#: at 5 s or more too (D-077). It costs under 1 % of loop time per 1,000 requests (loop_sim).
ARCHIVE_MIN_INTERVAL_S = 5.05
MAX_PAGES = 20
MAX_ASSET_BYTES = 40_000_000
#: A failing asset (transport error, HTTP 429/5xx, or a body that is not a JPEG) is tried at
#: most this many times in one backfill run, with the shared back-off between attempts.
MAX_ATTEMPTS_PER_RUN = 3
#: After this many failed attempts across runs an asset is given up (see ``retry_given_up``).
MAX_TOTAL_ATTEMPTS = 6
#: A day's search is cached (and read from the cache) only once the day is this old, so a
#: day that is still filling is never frozen as "complete".
SEARCH_CACHE_MIN_AGE_DAYS = 2
AssetKind = Literal["hires", "thumbmedium"]
_ASSET_PATHS: dict[str, str] = {
    "hires": "GetAsset/{asset_id}/proxy/hires",
    "thumbmedium": "GetAsset/{asset_id}/thumbmedium.jpg",
}

#: The archive's own spelling of location codes where it is upper case. The manifest schema
#: stores codes in lower case; the feeds' ``URLarchives`` and CLAUDE.md use ``ACAD``/``BIBE``.
API_CODE_CASE: dict[str, str] = {"acad": "ACAD", "bibe": "BIBE"}


def api_code(code: str) -> str:
    """The location code as the archive API spells it (``acad`` -> ``ACAD``)."""
    return API_CODE_CASE.get(code.lower(), code.lower())


class ArchiveError(RuntimeError):
    """An archive request failed or returned something unexpected."""


class ArchiveHTTPError(ArchiveError):
    """The archive answered with an HTTP error status."""

    def __init__(self, status_code: int, url: str, detail: str = "") -> None:
        super().__init__(f"HTTP {status_code} for {url} {detail}".strip())
        self.status_code = status_code
        self.url = url


class BudgetExhausted(ArchiveError):
    """The persisted request budget is used up; nothing more is sent."""


class ArchiveBadBody(ArchiveError):
    """A 200 answer whose body is not what was asked for (an HTML error page for a JPEG)."""

    def __init__(self, message: str, *, url: str, size: int) -> None:
        super().__init__(message)
        self.url = url
        self.size = size


def jpeg_problem(response: Fetched) -> str | None:
    """Why an asset body is not a usable JPEG (wrong magic bytes, or no decode), or None."""
    body = response.content
    if body[:2] != b"\xff\xd8":
        return (
            f"not a JPEG: {len(body)} bytes of {response.headers.get('Content-Type')!r} "
            f"starting {body[:24]!r}"
        )[:200]
    try:
        with Image.open(io.BytesIO(body)) as image:
            image.load()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        return f"JPEG does not decode ({len(body)} bytes): {type(exc).__name__}: {exc}"[:200]
    return None


# --- availability ----------------------------------------------------------------------


def _us_date(text: str) -> date:
    month, day, year = (int(part) for part in text.strip().split("/"))
    return date(year, month, day)


@dataclass(frozen=True)
class AvailableDays:
    """``GetAvailableDays`` decoded: a bit mask of days per (year, month)."""

    code: str
    min_day: date
    max_day: date
    masks: Mapping[tuple[int, int], int]

    @classmethod
    def from_json(cls, code: str, data: Mapping[str, Any]) -> AvailableDays:
        """Decode ``{"2004": {"1": mask, ...}, ..., "MinDay": "1/6/2004", "MaxDay": ...}``."""
        masks: dict[tuple[int, int], int] = {}
        for year, months in data.items():
            if not str(year).isdigit() or not isinstance(months, Mapping):
                continue
            for month, mask in months.items():
                masks[(int(year), int(month))] = int(mask)
        return cls(code, _us_date(data["MinDay"]), _us_date(data["MaxDay"]), masks)

    def is_available(self, day: date) -> bool:
        """True if the archive says ``day`` has frames (the page's ``checkShowDay``)."""
        mask = self.masks.get((day.year, day.month), 0)
        return bool(mask & (1 << (day.day - 1)))

    def days_in_month(self, year: int, month: int) -> list[date]:
        """Available days of one month, in order."""
        mask = self.masks.get((year, month), 0)
        out = []
        for bit in range(31):
            if mask & (1 << bit):
                try:
                    out.append(date(year, month, bit + 1))
                except ValueError:
                    break
        return out

    def days_in_year(self, year: int) -> int:
        """Number of available days in ``year``."""
        return sum(mask.bit_count() for (y, _m), mask in self.masks.items() if y == year)

    def nearest_in_month(self, target: date) -> date | None:
        """The available day of ``target``'s month closest to it (ties: earlier), or None."""
        days = self.days_in_month(target.year, target.month)
        if not days:
            return None
        return min(days, key=lambda d: (abs((d - target).days), d))


# --- assets --------------------------------------------------------------------------


@dataclass(frozen=True)
class ArchiveAsset:
    """One frame in the archive, as ``Search/Execute`` describes it."""

    asset_id: str
    location_code: str
    image_create_datetime: str
    local_time_string: str
    solar_elevation_deg: float | None
    time_of_day: str | None
    item_number: int | None = None

    @classmethod
    def from_result(cls, result: Mapping[str, Any]) -> ArchiveAsset:
        """Build from one element of ``Results`` (the ``Asset`` object inside it)."""
        asset = result.get("Asset", result)
        angle = asset.get("SolarElevationAngle")
        return cls(
            asset_id=str(asset["AssetID"]),
            location_code=str(asset.get("LocationCode", "")),
            image_create_datetime=str(asset.get("ImageCreateDateTime", "")),
            local_time_string=str(asset.get("LocalTimeString", "")),
            solar_elevation_deg=None if angle is None else float(angle),
            time_of_day=asset.get("TimeOfDay"),
            item_number=result.get("ItemNumber"),
        )

    def image_time_utc(
        self, tz_name: str, convention: Literal["label", "wall"] = "label"
    ) -> datetime:
        """UTC image time via :func:`lookout_common.timeutil.parse_nps_archive_time`."""
        return parse_nps_archive_time(
            self.image_create_datetime, self.local_time_string, tz_name, convention=convention
        )

    def to_dict(self) -> dict[str, Any]:
        """The raw fields, for caches and summaries."""
        return {
            "AssetID": self.asset_id,
            "LocationCode": self.location_code,
            "ImageCreateDateTime": self.image_create_datetime,
            "LocalTimeString": self.local_time_string,
            "SolarElevationAngle": self.solar_elevation_deg,
            "TimeOfDay": self.time_of_day,
        }


@dataclass
class SearchResult:
    """All assets of one search, with the paging the server reported."""

    assets: list[ArchiveAsset]
    result_count: int | None
    page_count: int | None
    pages_fetched: int
    search_id: str | None
    paging_error: str | None = None
    #: Paging stopped because ``may_continue()`` said no (the archive queue's time check).
    cut_short: bool = False


def search_body(
    code: str, day: date, *, page_size: int = 300, page: int = 1, search_id: str | None = None
) -> dict[str, Any]:
    """The JSON body the archive page sends for "all frames of one day", oldest first."""
    return {
        "SearchID": search_id,
        "Save": False,
        "Operand": {
            "LeftOperand": {"MatchType": "Exact", "Term": "LocationCode", "Attribute": code},
            "RightOperand": {
                "CompareType": "=",
                "Term": "ImageCreateDate",
                "Attribute": f"{day.month:02d}/{day.day:02d}/{day.year:04d}",
            },
            "Operator": "AND",
        },
        "QueryID": None,
        "ActionFilter": "Search",
        "CacheResults": False,
        "RoleFilter": None,
        "SubmitterFilter": None,
        "StatusFilter": "Active",
        "SortTerms": [{"Term": "CreateDate", "Ascending": True}],
        "PageSize": page_size,
        "ResultTerms": [
            "ImageCreateDate",
            "CustomTextFields",
            "CustomNumberFields",
            "AdditionalMetadata",
        ],
        "CurrentPage": page,
        "Page": page,
        "PageCount": 0,
    }


# --- request accounting -------------------------------------------------------------


@dataclass
class RequestStats:
    """Counts of requests and bytes sent by one :class:`ArchiveClient`."""

    requests: int = 0
    bytes: int = 0
    errors: int = 0
    waited_s: float = 0.0
    by_kind: Counter[str] = field(default_factory=Counter)
    bytes_by_kind: Counter[str] = field(default_factory=Counter)

    def to_dict(self) -> dict[str, Any]:
        """Plain dict for summaries."""
        return {
            "requests": self.requests,
            "bytes": self.bytes,
            "errors": self.errors,
            "waited_s": round(self.waited_s, 1),
            "by_kind": dict(self.by_kind),
            "bytes_by_kind": dict(self.bytes_by_kind),
        }


class RequestBudget:
    """A request allowance shared by every process, kept at ``state/archive/budget-<name>.json``.

    :meth:`consume` is called before a request is sent, so failed requests count too.
    """

    def __init__(self, storage: Storage, name: str, limit: int) -> None:
        self.storage = storage
        self.name = name
        self.limit = int(limit)
        self.key = state_key(f"archive/budget-{name}.json")

    def used(self) -> int:
        """Requests already counted against this budget."""
        return int(self.storage.read_json(self.key, default={}).get("used", 0))

    def consume(self, what: str = "") -> int:
        """Count one request; raise :class:`BudgetExhausted` (sending nothing) if none is left."""
        with self.storage.lock(f"archive/budget-{self.name}", timeout_s=30.0):
            doc = self.storage.read_json(self.key, default={})
            used = int(doc.get("used", 0))
            if used >= self.limit:
                raise BudgetExhausted(f"budget {self.name!r} used {used} of {self.limit}")
            doc.update(
                {
                    "name": self.name,
                    "limit": self.limit,
                    "used": used + 1,
                    "last": what,
                    "updated_at": isoformat_z(utcnow(), "milliseconds"),
                }
            )
            self.storage.write_json(self.key, doc)
            return used + 1


class ArchiveClient:
    """The archive API with every request spaced, counted and logged (see the module docstring)."""

    def __init__(
        self,
        *,
        client: httpx.Client,
        limiter: MinIntervalLimiter,
        storage: Storage | None = None,
        budget: RequestBudget | None = None,
        api_root: str = API_ROOT,
        clock: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        log_requests: bool = True,
        request_deadline_s: float | None = None,
        share_host_backoff: bool = True,
        runner: str = "cli",
    ) -> None:
        self.client = client
        self.limiter = limiter
        self.storage = storage
        self.budget = budget
        self.api_root = api_root if api_root.endswith("/") else api_root + "/"
        self.clock = clock
        self.monotonic = monotonic
        self.log_requests = log_requests and storage is not None
        #: A body still arriving after this many seconds is abandoned as an error (the
        #: archive queue bounds each step this way so it never runs into a live pass).
        self.request_deadline_s = request_deadline_s
        #: Honour and extend the www.nps.gov hold that live capture also uses
        #: (:mod:`lookout_capture.host_backoff`); needs ``storage``.
        self.share_host_backoff = share_host_backoff and storage is not None
        self.stats = RequestStats()
        self.failures = 0
        #: Which process sent the requests, written into every record as live capture's
        #: ``runner`` is: ``cli-loop`` (the queue inside the loop), ``cli-archive``,
        #: ``cli-archive-check`` or ``cli``.
        self.runner = runner

    # -- the shared www.nps.gov hold --------------------------------------------------
    def _shares_hold(self, url: str) -> bool:
        return self.share_host_backoff and httpx.URL(url).host == HOST

    def _wait_for_host_hold(self, url: str) -> None:
        """Defer the archive limiter to the end of a host hold (a live 429/503's, say)."""
        if not self._shares_hold(url):
            return
        assert self.storage is not None
        now = self.clock()
        until = host_hold_until(self.storage, now)
        if until is not None:
            wait = (until - now).total_seconds()
            print(
                f"lookout-capture: www.nps.gov is held until {isoformat_z(until, 'seconds')} "
                f"(an HTTP 429/503); waiting {wait:.0f} s before {url}",
                file=sys.stderr,
            )
            self.limiter.defer(wait)

    def _extend_host_hold(self, url: str, seconds: float, reason: str) -> None:
        """An archive 429/503 holds live capture too (PRD §15.3: back off on errors).

        If the hold cannot be written, the archive's own back-off (``defer``) still applies
        and the HTTP error is still the one raised.
        """
        if self._shares_hold(url):
            assert self.storage is not None
            with suppress(Exception):
                extend_host_hold(self.storage, self.clock(), seconds, reason, source="archive")

    # -- transport ------------------------------------------------------------------
    def _send(
        self,
        method: str,
        url: str,
        *,
        kind: str,
        json_body: Any = None,
        max_bytes: int = MAX_ASSET_BYTES,
        validate: Callable[[Fetched], str | None] | None = None,
    ) -> Fetched:
        """Send one request under the budget, the limiter and the log.

        ``validate`` checks a 2xx body; a problem it names is an error like an HTTP 5xx: it
        is logged, counted, deferred for every process and raised as :class:`ArchiveBadBody`.
        A 3xx is never followed (the production clients are built with
        ``follow_redirects=False``: each hop would be one more www.nps.gov request outside
        the limiter, the budget and the log); it is logged with its ``Location`` and raised as
        :class:`ArchiveHTTPError`, which is not retried.

        A www.nps.gov hold in force (:mod:`lookout_capture.host_backoff`, set by a live or an
        archive 429/503) is waited out first, through the limiter; an HTTP 429/503 here
        extends that hold, so live capture backs off too.
        """
        self._wait_for_host_hold(url)
        if self.budget is not None:
            self.budget.consume(f"{kind} {url}")
        started_wall = self.clock()
        with self.limiter.hold() as waited:
            self.stats.waited_s += waited
            t0 = self.monotonic()
            status: int | None = None
            size = 0
            error: str | None = None
            bad_body = False
            redirect: str | None = None
            try:
                with self.client.stream(method, url, json=json_body) as response:
                    status = response.status_code
                    chunks = []
                    deadline = self.request_deadline_s
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise ArchiveError(f"{url} is larger than {max_bytes} bytes")
                        if deadline is not None and self.monotonic() - t0 > deadline:
                            raise ArchiveError(f"{url} took longer than {deadline:g} s")
                        chunks.append(chunk)
                    result = Fetched(response.status_code, response.headers, b"".join(chunks), url)
                if status is not None and 300 <= status < 400:
                    redirect = str(result.headers.get("Location"))
                    error = f"HTTP {status}: redirect to {redirect} not followed"[:200]
                elif validate is not None and status is not None and status < 400:
                    problem = validate(result)
                    if problem is not None:
                        error, bad_body = f"bad body: {problem}"[:200], True
            except (httpx.TransportError, ArchiveError) as exc:
                error = f"{type(exc).__name__}: {exc}"[:200]
                self.failures += 1
                self.limiter.defer(backoff_delay(self.failures, base_s=30.0, cap_s=1800.0))
                raise ArchiveError(error) from exc
            finally:
                elapsed_ms = round((self.monotonic() - t0) * 1000.0, 1)
                self.stats.requests += 1
                self.stats.by_kind[kind] += 1
                self.stats.bytes += size
                self.stats.bytes_by_kind[kind] += size
                if error is not None or (status is not None and status >= 400):
                    self.stats.errors += 1
                self._log(kind, method, url, status, size, waited, elapsed_ms, error, started_wall)
            if redirect is not None and status is not None:
                self.failures += 1
                raise ArchiveHTTPError(status, url, f"redirect to {redirect} not followed")
            if status is not None and status >= 400:
                self.failures += 1
                wait = retry_after_s(result) if status in (429, 503) else None
                if status in (429, 503) or status >= 500:
                    delay = max(
                        wait or 0.0, backoff_delay(self.failures, base_s=30.0, cap_s=1800.0)
                    )
                    self.limiter.defer(delay)
                    if status in (429, 503):
                        self._extend_host_hold(url, delay, f"HTTP {status} on archive {kind}")
                raise ArchiveHTTPError(status, url, result.text[:120] if kind != "asset" else "")
            if bad_body:
                self.failures += 1
                self.limiter.defer(backoff_delay(self.failures, base_s=30.0, cap_s=1800.0))
                raise ArchiveBadBody(error or "bad body", url=url, size=size)
            self.failures = 0
            return result

    def _log(
        self,
        kind: str,
        method: str,
        url: str,
        status: int | None,
        size: int,
        waited: float,
        elapsed_ms: float,
        error: str | None,
        started: datetime,
    ) -> None:
        if not self.log_requests or self.storage is None:
            return
        record = {
            "worker": "archive",
            "event": "archive_request",
            "kind": kind,
            "method": method,
            "url": url,
            "status": status,
            "bytes": size,
            "waited_s": round(waited, 3),
            "elapsed_ms": elapsed_ms,
            "runner": self.runner,
            "capture_version": CAPTURE_VERSION,
        }
        if error:
            record["error"] = error
        self.storage.append_jsonl(capture_log_key(started), record)

    # -- endpoints ------------------------------------------------------------------
    def available_days(self, code: str) -> tuple[AvailableDays, dict[str, Any]]:
        """``GetAvailableDays`` for ``code``; returns the decoded days and the raw JSON."""
        stamp = self.clock().astimezone(UTC).strftime("%Y%m%d%H00")
        url = f"{self.api_root}api/GetAvailableDays/{api_code(code)}?{stamp}"
        response = self._send("GET", url, kind="days")
        try:
            raw = response.json()
        except ValueError as exc:
            raise ArchiveError(f"GetAvailableDays {code}: not JSON") from exc
        return AvailableDays.from_json(code, raw), raw

    def search_day(
        self,
        code: str,
        day: date,
        *,
        page_size: int = 300,
        may_continue: Callable[[], bool] | None = None,
    ) -> tuple[SearchResult, list[dict[str, Any]]]:
        """All assets of one local day, following the server's paging (``Page``/``SearchID``).

        ``may_continue()`` is asked before each page after the first; when it says no,
        paging stops and the result is marked ``cut_short`` (and so incomplete).
        """
        wire_code = api_code(code)
        url = f"{self.api_root}api/Search/Execute"
        body = search_body(wire_code, day, page_size=page_size)
        response = self._send("POST", url, kind="search", json_body=body)
        page = self._search_json(response, code, day)
        pages = [page]
        assets = [ArchiveAsset.from_result(r) for r in page.get("Results") or []]
        page_count = page.get("PageCount")
        search_id = page.get("SearchID")
        number = 1
        paging_error: str | None = None
        cut_short = False
        while isinstance(page_count, int) and number < min(page_count, MAX_PAGES):
            if may_continue is not None and not may_continue():
                cut_short = True
                paging_error = f"stopped before page {number + 1} of {page_count}: no time left"
                break
            number += 1
            # POST /api/Search/Execute/<SearchID> answered HTTP 500 on 2026-11-19, so later
            # pages go to the same URL with Page/CurrentPage set and the SearchID in the body.
            next_body = search_body(
                wire_code, day, page_size=page_size, page=number, search_id=search_id
            )
            try:
                response = self._send("POST", url, kind="search", json_body=next_body)
                page = self._search_json(response, code, day)
            except BudgetExhausted:
                raise
            except ArchiveError as exc:
                paging_error = f"page {number}: {exc}"[:200]
                break
            pages.append(page)
            new = [ArchiveAsset.from_result(r) for r in page.get("Results") or []]
            seen = {a.asset_id for a in assets}
            if not new or all(a.asset_id in seen for a in new):
                paging_error = f"page {number} repeated earlier results"
                break  # the server ignored the page number; stop rather than loop
            assets.extend(a for a in new if a.asset_id not in seen)
        result = SearchResult(
            assets=assets,
            result_count=pages[0].get("ResultCount"),
            page_count=page_count if isinstance(page_count, int) else None,
            pages_fetched=len(pages),
            search_id=search_id,
            paging_error=paging_error,
            cut_short=cut_short,
        )
        return result, pages

    @staticmethod
    def _search_json(response: Fetched, code: str, day: date) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise ArchiveError(f"Search {code} {day}: not JSON") from exc
        if not isinstance(data, dict):
            raise ArchiveError(f"Search {code} {day}: unexpected {type(data).__name__}")
        return data

    def get_asset(self, asset_id: str, kind: AssetKind) -> Fetched:
        """Download one asset (``hires`` or ``thumbmedium``); a non-JPEG raises ArchiveBadBody."""
        url = self.asset_url(asset_id, kind)
        return self._send("GET", url, kind=kind, validate=jpeg_problem)

    def asset_url(self, asset_id: str, kind: AssetKind) -> str:
        """URL of one asset."""
        return self.api_root + _ASSET_PATHS[kind].format(asset_id=asset_id)

    def get_page(self, url: str) -> Fetched:
        """GET any other nps.gov page under the same spacing, budget and logging."""
        return self._send("GET", url, kind="page", max_bytes=5_000_000)


def archive_limiter(
    min_interval_s: float = ARCHIVE_MIN_INTERVAL_S, *, state_dir: str | Path | None = None
) -> MinIntervalLimiter:
    """The ``nps-archive`` limiter: always the cross-process ``flock`` one on local disk.

    ``lookout_common``'s ``mode="auto"`` falls back to an in-process limiter when
    ``LOOKOUT_STORAGE=s3``; two archive commands on this laptop with S3 storage would then
    hit nps.gov together. The archive rule is about this machine, so the limiter's state
    lives in the local ``state/ratelimit/`` folder whatever the storage backend. (An archive
    run on Lambda would be coordinated only within its container; none is scheduled.)
    ``min_interval_s`` exists for tests; production code uses the 5.05 s default
    (:data:`ARCHIVE_MIN_INTERVAL_S`: the rule's 5 s plus 50 ms for the log's stamps, D-077).
    """
    return MinIntervalLimiter(
        ARCHIVE_LIMITER_NAME,
        min_interval_s,
        mode="file",
        state_dir=state_dir if state_dir is not None else paths.state_dir(),
    )


def default_archive_client(
    storage: Storage,
    *,
    budget: RequestBudget | None = None,
    transport: httpx.BaseTransport | None = None,
    runner: str = "cli",
) -> ArchiveClient:
    """An :class:`ArchiveClient` on :func:`polite_client` with the shared 5.05 s limiter.

    Redirects are not followed (:meth:`ArchiveClient._send`).
    """
    from lookout_common.http import polite_client

    return ArchiveClient(
        client=polite_client(transport=transport, follow_redirects=False),
        limiter=archive_limiter(),
        storage=storage,
        budget=budget,
        runner=runner,
    )


# --- caching of search results ----------------------------------------------------------


def search_cache_key(code: str, day: date) -> str:
    """Deletable cache of one day's search: ``cache/nps-archive/search/<code>/<date>.json``."""
    return f"cache/nps-archive/search/{code.lower()}/{day.isoformat()}.json"


def _old_enough(day: date, on: date) -> bool:
    return day <= on - timedelta(days=SEARCH_CACHE_MIN_AGE_DAYS)


def _usable_cache(doc: Any, day: date) -> bool:
    """A cached search counts only if it was complete and written once the day had closed.

    Entries written before 2026-11-20 carry no ``cached_on``; their ``fetched_at`` date
    stands in. A day searched while it was still filling is refetched, not trusted.
    """
    if not isinstance(doc, Mapping):
        return False
    meta = doc.get("meta") or {}
    stamp = str(meta.get("cached_on") or str(meta.get("fetched_at") or "")[:10])
    try:
        written = date.fromisoformat(stamp)
    except ValueError:
        return False
    if not _old_enough(day, written):
        return False
    complete = meta.get("complete")
    if complete is None:
        count = meta.get("result_count")
        complete = count is None or len(doc.get("assets") or []) >= int(count)
    return bool(complete)


def cached_search(
    storage: Storage,
    archive: ArchiveClient,
    code: str,
    day: date,
    *,
    today: date,
    page_size: int = 300,
    may_continue: Callable[[], bool] | None = None,
) -> tuple[list[ArchiveAsset], dict[str, Any]]:
    """One day's assets (metadata only), from the cache once the day is closed.

    A search is read from, and written to, ``cache/`` only when ``day`` is at least
    :data:`SEARCH_CACHE_MIN_AGE_DAYS` before ``today`` (UTC) and the search was complete.
    Today's and yesterday's searches are never cached: frames may still be arriving.
    ``may_continue`` goes to :meth:`ArchiveClient.search_day`; a search it cut short is
    marked ``cut_short`` and incomplete, and is never cached.
    """
    key = search_cache_key(code, day)
    if _old_enough(day, today):
        cached = storage.read_json(key, default=None)
        if _usable_cache(cached, day):
            assets = [ArchiveAsset.from_result({"Asset": a}) for a in cached.get("assets", [])]
            return assets, {**cached.get("meta", {}), "from_cache": True}
    result, _pages = archive.search_day(code, day, page_size=page_size, may_continue=may_continue)
    meta = {
        "code": code,
        "day": day.isoformat(),
        "fetched_at": isoformat_z(utcnow(), "seconds"),
        "cached_on": today.isoformat(),
        "result_count": result.result_count,
        "page_count": result.page_count,
        "pages_fetched": result.pages_fetched,
        "page_size": page_size,
        "search_id": result.search_id,
        "paging_error": result.paging_error,
        "cut_short": result.cut_short,
        "complete": not result.cut_short
        and (result.result_count is None or len(result.assets) >= result.result_count),
        "from_cache": False,
    }
    if meta["complete"] and _old_enough(day, today):
        storage.write_json(key, {"meta": meta, "assets": [a.to_dict() for a in result.assets]})
    return result.assets, meta


# --- backfill -------------------------------------------------------------------------


def archive_asset_key(
    viewpoint_id: str, image_time_utc: datetime, asset_id: str, kind: AssetKind
) -> str:
    """Storage key: the CLAUDE.md archive key for ``hires``; ``..._thumbmedium.jpg`` for thumbs."""
    key = archive_key(viewpoint_id, image_time_utc, asset_id)
    if kind == "hires":
        return key
    return key[: -len(".jpg")] + f"_{kind}.jpg"


def archive_state_key(viewpoint_id: str) -> str:
    """Per-viewpoint backfill state: ``state/archive/<viewpoint_id>.json``."""
    return state_key(f"archive/{viewpoint_id}.json")


def pick_assets(
    assets: Sequence[ArchiveAsset],
    *,
    pick: Literal["all", "midday", "hourly"] = "all",
    min_sun_deg: float | None = None,
) -> list[ArchiveAsset]:
    """Filter one day's assets.

    - ``min_sun_deg``: keep frames whose NPS ``SolarElevationAngle`` is at least this
      (daylight is decided by the ephemeris, never by brightness: PRD §8.6).
    - ``midday``: the single frame with the highest Sun.
    - ``hourly``: the frame nearest each full hour of local time (first on ties), for the
      hours of this day. A frame after 23:30 is nearest the *next* day's 00:00, which that
      day's own entry picks from its own frames, so it is left out here; it used to be filed
      under this day's 00:00 and could stand in for a missing midnight frame 23 hours away.
    """
    chosen = [
        a
        for a in assets
        if min_sun_deg is None
        or (a.solar_elevation_deg is not None and a.solar_elevation_deg >= min_sun_deg)
    ]
    if pick == "midday":
        with_sun = [a for a in chosen if a.solar_elevation_deg is not None]
        return [max(with_sun, key=lambda a: a.solar_elevation_deg)] if with_sun else []  # type: ignore[arg-type, return-value]
    if pick == "hourly":
        best: dict[str, tuple[int, ArchiveAsset]] = {}
        for asset in chosen:
            parts = asset.image_create_datetime.split()
            if len(parts) < 3:
                continue
            hh, mm = parts[1].split(":")[:2]
            hour = (int(hh) % 12) + (12 if parts[2].upper() == "PM" else 0)
            minutes = int(mm)
            if minutes <= 30:
                slot_hour, dist = hour, minutes
            elif hour < 23:
                slot_hour, dist = hour + 1, 60 - minutes
            else:  # nearest the next day's 00:00: not one of this day's hours
                continue
            slot = f"{parts[0]} {slot_hour:02d}"
            if slot not in best or dist < best[slot][0]:
                best[slot] = (dist, asset)
        return sorted((a for _, a in best.values()), key=lambda a: assets.index(a))
    return list(chosen)


@dataclass
class BackfillResult:
    """What one backfill call did."""

    viewpoint_id: str
    planned: int = 0
    downloaded: int = 0
    skipped_existing: int = 0
    failed: int = 0
    given_up: int = 0
    refused_held_out: int = 0
    released_held_out: int = 0
    attempts: int = 0
    bytes_downloaded: int = 0
    stopped: str | None = None
    stored: list[dict[str, Any]] = field(default_factory=list)
    plan: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Plain dict for the CLI and summaries."""
        return {
            "viewpoint_id": self.viewpoint_id,
            "planned": self.planned,
            "downloaded": self.downloaded,
            "skipped_existing": self.skipped_existing,
            "failed": self.failed,
            "given_up": self.given_up,
            "refused_held_out": self.refused_held_out,
            "released_held_out": self.released_held_out,
            "attempts": self.attempts,
            "bytes_downloaded": self.bytes_downloaded,
            "stopped": self.stopped,
            "stored": self.stored,
        }


def _retryable(exc: ArchiveError) -> bool:
    """Transport errors, HTTP 429/5xx and bad bodies are worth another try; a 404 or a
    redirect (3xx, never followed) is not."""
    if isinstance(exc, ArchiveHTTPError):
        return exc.status_code == 429 or exc.status_code >= 500
    return True


def backfill_assets(
    *,
    storage: Storage,
    archive: ArchiveClient,
    viewpoint: Viewpoint,
    assets: Iterable[ArchiveAsset],
    kinds: Sequence[AssetKind] = ("thumbmedium",),
    dry_run: bool = False,
    max_bytes: int | None = None,
    convention: Literal["label", "wall"] = "label",
    keep_original: bool = False,
    lock_timeout_s: float = 0.0,
    held_out_override: HeldOutOverride | None = None,
    max_attempts: int = MAX_ATTEMPTS_PER_RUN,
    retry_given_up: bool = False,
) -> BackfillResult:
    """Download the given assets once each, store them with sidecars, and record progress.

    - **Held-out guard (D-019):** an asset dated on or after
      :data:`lookout_capture.heldout.HELD_OUT_FROM` is refused before any request or read,
      unless ``held_out_override`` is given; each asset an override releases is logged
      (``held_out_override``) with its reason and noted in the sidecar.
    - **Never twice:** an asset whose key already exists, or that the state lists as done,
      is skipped without a request.
    - **Size cap:** ``max_bytes`` caps the bytes this viewpoint's backfill has downloaded in
      total (all runs, from the state, bad bodies included); the check runs before each
      request, so the cap is overshot by at most one file.
    - **Errors:** a body that is not a decodable JPEG is not stored and not marked done. It,
      a transport error or an HTTP 429/5xx is retried up to ``max_attempts`` times in this
      run, each after the client's shared back-off; failures are kept in the state, and an
      asset that has failed :data:`MAX_TOTAL_ATTEMPTS` times is given up unless
      ``retry_given_up``.
    - ``dry_run`` sends nothing. Hires frames wider than 1920 px are stored downscaled like
      live frames (PRD §15.2), unless ``keep_original``.
    """
    result = BackfillResult(viewpoint.id)
    with storage.lock(f"archive/{viewpoint.id}", timeout_s=lock_timeout_s, ttl_s=6 * 3600.0):
        state_doc = storage.read_json(archive_state_key(viewpoint.id), default={})
        completed: dict[str, dict[str, str]] = state_doc.get("completed", {})
        failures: dict[str, dict[str, dict[str, Any]]] = state_doc.get("failed", {})
        total_bytes = int(state_doc.get("bytes_downloaded", 0))

        def save_state() -> None:
            state_doc.update(
                {
                    "viewpoint_id": viewpoint.id,
                    "completed": completed,
                    "failed": failures,
                    "bytes_downloaded": total_bytes,
                    "updated_at": isoformat_z(utcnow(), "seconds"),
                }
            )
            storage.write_json(archive_state_key(viewpoint.id), state_doc)

        for asset in assets:
            # The instant behind the asset's archive key, when it parses: its local and UTC
            # dates are two more of the guard's readings (the guard reads both conventions too).
            time_error: str | None = None
            try:
                key_time: datetime | None = asset.image_time_utc(viewpoint.time_zone, convention)
            except ValueError as exc:
                key_time, time_error = None, f"time: {exc}"[:160]
            guard_reason = held_out_reason(
                asset.image_create_datetime,
                asset.local_time_string,
                viewpoint.time_zone,
                instant=key_time,
            )
            for kind in kinds:
                if guard_reason is not None and held_out_override is None:
                    result.refused_held_out += 1
                    result.plan.append(
                        {"asset_id": asset.asset_id, "kind": kind, "refused": guard_reason}
                    )
                    continue
                if key_time is None:
                    result.failed += 1
                    result.plan.append(
                        {"asset_id": asset.asset_id, "kind": kind, "error": time_error}
                    )
                    continue
                image_time = key_time
                key = archive_asset_key(viewpoint.id, image_time, asset.asset_id, kind)
                result.planned += 1
                done_key = completed.get(asset.asset_id, {}).get(kind)
                if done_key or storage.exists(key):
                    result.skipped_existing += 1
                    continue
                past = failures.get(asset.asset_id, {}).get(kind) or {}
                tries_left = max(1, max_attempts)
                if not retry_given_up:
                    tries_left = min(tries_left, MAX_TOTAL_ATTEMPTS - int(past.get("attempts", 0)))
                if tries_left <= 0:
                    result.given_up += 1
                    result.plan.append({"asset_id": asset.asset_id, "kind": kind, "given_up": past})
                    continue
                if dry_run:
                    result.plan.append(
                        {
                            "asset_id": asset.asset_id,
                            "kind": kind,
                            "key": key,
                            "local": asset.image_create_datetime,
                            **({"held_out_override": True} if guard_reason else {}),
                        }
                    )
                    continue
                if guard_reason is not None and held_out_override is not None:
                    result.released_held_out += 1
                    _log_archive_event(
                        storage,
                        viewpoint.id,
                        "held_out_override",
                        asset,
                        kind,
                        {"guard": guard_reason, "reason": held_out_override.reason},
                        runner=archive.runner,
                    )
                response: Fetched | None = None
                last_error: ArchiveError | None = None
                for _attempt in range(tries_left):
                    if max_bytes is not None and total_bytes >= max_bytes:
                        result.stopped = f"size cap reached: {total_bytes} >= {max_bytes} bytes"
                        break
                    result.attempts += 1
                    try:
                        response = archive.get_asset(asset.asset_id, kind)
                        break
                    except BudgetExhausted as exc:
                        result.stopped = str(exc)
                        break
                    except ArchiveError as exc:
                        last_error = exc
                        if isinstance(exc, ArchiveBadBody):
                            total_bytes += exc.size
                            result.bytes_downloaded += exc.size
                        entry = failures.setdefault(asset.asset_id, {}).setdefault(kind, {})
                        entry.update(
                            {
                                "attempts": int(entry.get("attempts", 0)) + 1,
                                "last_error": str(exc)[:200],
                                "at": isoformat_z(utcnow(), "seconds"),
                            }
                        )
                        if not _retryable(exc):
                            break
                if response is None:
                    if result.stopped and last_error is None:
                        break  # budget or size cap before any attempt of this asset
                    result.failed += 1
                    _log_archive_event(
                        storage,
                        viewpoint.id,
                        "error",
                        asset,
                        kind,
                        {"error": str(last_error)[:200] if last_error else result.stopped},
                        runner=archive.runner,
                    )
                    save_state()
                    if result.stopped:
                        break
                    continue
                body = response.content
                total_bytes += len(body)
                result.bytes_downloaded += len(body)
                record = _store_asset(
                    storage,
                    archive,
                    viewpoint,
                    asset,
                    kind,
                    key,
                    image_time,
                    response,
                    convention,
                    keep_original,
                    held_out_override.reason if guard_reason and held_out_override else None,
                )
                completed.setdefault(asset.asset_id, {})[kind] = key
                if asset.asset_id in failures:
                    failures[asset.asset_id].pop(kind, None)
                    if not failures[asset.asset_id]:
                        failures.pop(asset.asset_id)
                save_state()
                result.downloaded += 1
                result.stored.append(record)
            if result.stopped:
                break
    return result


def _log_archive_event(
    storage: Storage,
    viewpoint_id: str,
    event: str,
    asset: ArchiveAsset,
    kind: str,
    extra: Mapping[str, Any],
    runner: str | None = None,
) -> None:
    storage.append_jsonl(
        capture_log_key(utcnow()),
        {
            "worker": "archive",
            "viewpoint_id": viewpoint_id,
            "event": event,
            "asset_id": asset.asset_id,
            "kind": kind,
            **extra,
            **({"runner": runner} if runner else {}),
            "capture_version": CAPTURE_VERSION,
        },
    )


def _store_asset(
    storage: Storage,
    archive: ArchiveClient,
    viewpoint: Viewpoint,
    asset: ArchiveAsset,
    kind: AssetKind,
    key: str,
    image_time: datetime,
    response: Fetched,
    convention: str,
    keep_original: bool,
    override_reason: str | None = None,
) -> dict[str, Any]:
    body = response.content
    processed = process_image(body, keep_original=keep_original or kind != "hires")
    fetched_at = utcnow()
    sun = sun_position(image_time, viewpoint.camera.lat, viewpoint.camera.lon)
    flags = sorted({*processed.flags, *gate_flags(processed, sun)})
    sidecar = {
        "viewpoint_id": viewpoint.id,
        "source": "nps-archive",
        "kind": kind,
        "source_url": archive.asset_url(asset.asset_id, kind),
        "fetched_at_utc": isoformat_z(fetched_at, "milliseconds"),
        "asset_id": asset.asset_id,
        "location_code": asset.location_code,
        "image_create_datetime_raw": asset.image_create_datetime,
        "local_time_string": asset.local_time_string,
        "image_time_utc": isoformat_z(image_time),
        "image_time_source": f"nps_archive_{convention}",
        "time_convention": convention,
        "solar_elevation_deg": asset.solar_elevation_deg,
        "time_of_day": asset.time_of_day,
        "http_last_modified": response.headers.get("Last-Modified"),
        "etag": response.headers.get("ETag"),
        "content_type": response.headers.get("Content-Type"),
        "content_length": len(body),
        "original_width": processed.original_width,
        "original_height": processed.original_height,
        "original_bytes": processed.original_bytes,
        "sha256": processed.sha256,
        "stored": processed.stored_mode,
        "stored_width": processed.stored_width,
        "stored_height": processed.stored_height,
        "stored_bytes": len(processed.stored),
        "stored_sha256": processed.stored_sha256,
        "jpeg_quality": processed.jpeg_quality,
        "dhash64": processed.dhash64,
        "luma_mean": processed.luma_mean,
        "chroma_spread": processed.chroma_spread,
        "sharpness_lapvar": processed.sharpness_lapvar,
        "sun": sun,
        "exif": processed.exif,
        "flags": flags,
        **({"held_out_override": override_reason} if override_reason else {}),
        "user_agent": user_agent(),
        "capture_version": CAPTURE_VERSION,
        "sidecar_schema": 1,
    }
    wrote = storage.put_bytes(key, processed.stored, content_type="image/jpeg", if_absent=True)
    storage.put_bytes(
        sidecar_key(key),
        (json.dumps(sidecar, indent=2, ensure_ascii=False) + "\n").encode(),
        content_type="application/json",
        if_absent=True,
    )
    _log_archive_event(
        storage,
        viewpoint.id,
        "stored" if wrote else "duplicate",
        asset,
        kind,
        {
            "key": key,
            "bytes": len(body),
            "stored_bytes": len(processed.stored),
            "width": processed.original_width,
            "height": processed.original_height,
            "flags": flags,
        },
        runner=archive.runner,
    )
    return {
        "key": key,
        **{
            k: sidecar[k]
            for k in (
                "asset_id",
                "kind",
                "image_time_utc",
                "local_time_string",
                "solar_elevation_deg",
                "time_of_day",
                "original_width",
                "original_height",
                "original_bytes",
                "stored_bytes",
                "sha256",
                "dhash64",
                "luma_mean",
                "chroma_spread",
                "flags",
                "exif",
            )
        },
    }
