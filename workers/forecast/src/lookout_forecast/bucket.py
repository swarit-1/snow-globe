"""Anonymous HTTPS access to the NOAA HRRR bucket (CLAUDE.md "NOAA"; PRD §8.3, §15.3).

Everything goes through :func:`lookout_common.http.polite_client`, so requests carry the
project User-Agent, bounded timeouts and no cookies. Three kinds of request exist:

- ``listing``: S3 ListObjectsV2 (``GET /?list-type=2&prefix=...``), parsed from XML;
- ``idx``: a whole ``.idx`` inventory (about 10 KB);
- ``grib``: a byte range of a GRIB2 file. The response must be ``206`` with the exact
  ``Content-Range`` asked for; a ``200`` (the server ignoring ``Range``) is closed
  unread, so a full 140 MB file is never downloaded by accident.

Transient failures (timeouts, connection errors, HTTP 429 and 5xx) are retried with
exponential back-off. Every byte received is counted in a :class:`TransferLedger`.
"""

from __future__ import annotations

import re
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from lookout_common.http import polite_client, retry_after_s
from lookout_common.timeutil import parse_iso_utc

from .hrrr import BASE_URL

__all__ = [
    "BucketError",
    "HrrrBucket",
    "ListPage",
    "ObjectNotFoundError",
    "RangeNotHonouredError",
    "S3Object",
    "TransferLedger",
    "parse_list_page",
    "transfer_delta",
]

Kind = Literal["listing", "idx", "grib"]
_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class BucketError(RuntimeError):
    """The bucket answered with something unusable (after retries)."""


class ObjectNotFoundError(BucketError):
    """The key does not exist (HTTP 404, or 403 for anonymous S3 reads)."""


class RangeNotHonouredError(BucketError):
    """A ranged GET came back as anything but ``206`` with the requested range."""


@dataclass(frozen=True, slots=True)
class S3Object:
    """One key from a ListObjectsV2 page."""

    key: str
    last_modified: datetime
    size: int
    etag: str


@dataclass(frozen=True, slots=True)
class ListPage:
    """One ListObjectsV2 response."""

    objects: tuple[S3Object, ...]
    is_truncated: bool
    next_token: str | None


def parse_list_page(xml_bytes: bytes) -> ListPage:
    """Parse a ListObjectsV2 XML body; raise :class:`BucketError` on an S3 ``<Error>``."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise BucketError(f"listing is not XML: {exc}") from exc
    if root.tag in {"Error", f"{_S3_NS}Error"}:
        code = root.findtext("Code") or root.findtext(f"{_S3_NS}Code") or "?"
        message = root.findtext("Message") or root.findtext(f"{_S3_NS}Message") or ""
        raise BucketError(f"S3 error {code}: {message}")
    if root.tag != f"{_S3_NS}ListBucketResult":
        raise BucketError(f"unexpected listing root element {root.tag!r}")
    objects: list[S3Object] = []
    for item in root.iter(f"{_S3_NS}Contents"):
        key = item.findtext(f"{_S3_NS}Key")
        modified = item.findtext(f"{_S3_NS}LastModified")
        size = item.findtext(f"{_S3_NS}Size")
        if key is None or modified is None or size is None:
            raise BucketError("listing entry without Key, LastModified or Size")
        etag = (item.findtext(f"{_S3_NS}ETag") or "").strip('"')
        objects.append(S3Object(key, parse_iso_utc(modified), int(size), etag))
    truncated = (root.findtext(f"{_S3_NS}IsTruncated") or "false").strip().lower() == "true"
    token = root.findtext(f"{_S3_NS}NextContinuationToken")
    if truncated and not token:
        raise BucketError("truncated listing without a NextContinuationToken")
    return ListPage(tuple(objects), truncated, token if truncated else None)


@dataclass
class TransferLedger:
    """Thread-safe count of requests, retries, bytes and seconds by request kind."""

    requests: dict[str, int] = field(default_factory=dict)
    bytes: dict[str, int] = field(default_factory=dict)
    seconds: dict[str, float] = field(default_factory=dict)
    retries: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, kind: str, nbytes: int, seconds: float) -> None:
        """Record one finished request."""
        with self._lock:
            self.requests[kind] = self.requests.get(kind, 0) + 1
            self.bytes[kind] = self.bytes.get(kind, 0) + nbytes
            self.seconds[kind] = self.seconds.get(kind, 0.0) + seconds

    def add_retry(self) -> None:
        """Record one retried attempt."""
        with self._lock:
            self.retries += 1

    @property
    def total_bytes(self) -> int:
        """All bytes received, every kind."""
        with self._lock:
            return sum(self.bytes.values())

    def snapshot(self) -> dict[str, Any]:
        """A JSON-ready copy of the counters (everything since the ledger was created)."""
        with self._lock:
            return {
                "requests": dict(self.requests),
                "bytes": dict(self.bytes),
                "seconds": {k: round(v, 3) for k, v in self.seconds.items()},
                "retries": self.retries,
                "total_bytes": sum(self.bytes.values()),
            }

    def since(self, before: Mapping[str, Any]) -> dict[str, Any]:
        """What was counted after ``before`` (an earlier :meth:`snapshot`), same shape.

        A bucket client is shared by every run a process handles, so a run reports this
        difference, never the client's lifetime totals.
        """
        return transfer_delta(before, self.snapshot())


def transfer_delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """``after - before`` for two :meth:`TransferLedger.snapshot` results; zero kinds dropped."""

    def diff(key: str, digits: int | None = None) -> dict[str, Any]:
        old = before.get(key, {})
        out: dict[str, Any] = {}
        for kind, value in after.get(key, {}).items():
            delta = value - old.get(kind, 0)
            if digits is not None:
                delta = round(delta, digits)
            if delta:
                out[kind] = delta
        return out

    byte_counts = diff("bytes")
    return {
        "requests": diff("requests"),
        "bytes": byte_counts,
        "seconds": diff("seconds", 3),
        "retries": int(after.get("retries", 0)) - int(before.get("retries", 0)),
        "total_bytes": sum(byte_counts.values()),
    }


class HrrrBucket:
    """Polite, counted, retrying reads of ``noaa-hrrr-bdp-pds`` over anonymous HTTPS.

    Each thread gets its own ``httpx.Client`` (all from ``polite_client``). Worker threads
    come and go with each thread pool, so a new client first closes the clients of
    threads that have ended; the open clients never outnumber the live threads that
    used the bucket. ``transport`` exists for tests (``httpx.MockTransport``). Use as a
    context manager, or call :meth:`close`.

    ``listing_sink``, if set, is called with ``(prefix, body)`` for every ListObjectsV2
    page received, before it is parsed (``--save-listings`` uses it to record fixtures).
    """

    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: httpx.Timeout | float | None = None,
        max_attempts: int = 4,
        backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        ledger: TransferLedger | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.base_url = base_url.rstrip("/")
        self._transport = transport
        self._timeout = timeout
        self.max_attempts = max_attempts
        self.backoff_s = backoff_s
        self.max_backoff_s = max_backoff_s
        self._sleep = sleep
        self.ledger = ledger or TransferLedger()
        self._local = threading.local()
        self._clients: list[tuple[threading.Thread, httpx.Client]] = []
        self._clients_lock = threading.Lock()
        self.listing_sink: Callable[[str, bytes], None] | None = None

    def __enter__(self) -> HrrrBucket:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close every per-thread client."""
        with self._clients_lock:
            clients, self._clients = self._clients, []
        for _, client in clients:
            client.close()

    @property
    def open_clients(self) -> int:
        """How many per-thread clients are open (for tests and diagnostics)."""
        with self._clients_lock:
            return len(self._clients)

    def _client(self) -> httpx.Client:
        client = getattr(self._local, "client", None)
        if client is None:
            client = polite_client(timeout=self._timeout, transport=self._transport)
            self._local.client = client
            with self._clients_lock:
                ended = [c for thread, c in self._clients if not thread.is_alive()]
                self._clients = [(t, c) for t, c in self._clients if t.is_alive()]
                self._clients.append((threading.current_thread(), client))
            for old in ended:
                old.close()  # its thread is gone, so nothing else can be using it
        return client

    def url(self, key: str) -> str:
        """Full URL of a bucket key."""
        return f"{self.base_url}/{key}"

    # -- retry core ----------------------------------------------------------------
    def _delay(self, attempt: int, response: httpx.Response | None) -> float:
        hinted = retry_after_s(response) if response is not None else None
        delay = self.backoff_s * 2 ** (attempt - 1)
        if hinted is not None:
            delay = max(delay, hinted)
        return min(delay, self.max_backoff_s)

    def _get(
        self,
        url: str,
        kind: Kind,
        *,
        headers: dict[str, str] | None = None,
        check: Callable[[httpx.Response], None] | None = None,
    ) -> bytes:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            started = time.monotonic()
            response: httpx.Response | None = None
            try:
                with self._client().stream("GET", url, headers=headers) as response:
                    if response.status_code in {403, 404}:
                        raise ObjectNotFoundError(f"{response.status_code} for {url}")
                    if response.status_code in _RETRY_STATUS:
                        raise _Retryable(f"HTTP {response.status_code} for {url}")
                    if check is not None:
                        check(response)  # may raise before the body is read
                    elif response.status_code != 200:
                        raise BucketError(f"HTTP {response.status_code} for {url}")
                    body = response.read()
            except (_Retryable, httpx.TransportError) as exc:
                last_error = exc
                if attempt == self.max_attempts:
                    break
                self.ledger.add_retry()
                self._sleep(self._delay(attempt, response))
                continue
            self.ledger.add(kind, len(body), time.monotonic() - started)
            return body
        raise BucketError(f"giving up on {url} after {self.max_attempts} attempts: {last_error}")

    # -- the three request kinds ---------------------------------------------------------
    def list_page(
        self, prefix: str, *, continuation_token: str | None = None, max_keys: int = 1000
    ) -> ListPage:
        """One ListObjectsV2 page for ``prefix``."""
        params: dict[str, str] = {"list-type": "2", "prefix": prefix, "max-keys": str(max_keys)}
        if continuation_token:
            params["continuation-token"] = continuation_token
        body = self._get(f"{self.base_url}/?{urlencode(params)}", "listing")
        if self.listing_sink is not None:
            self.listing_sink(prefix, body)
        return parse_list_page(body)

    def list_objects(self, prefix: str, *, max_pages: int = 100) -> list[S3Object]:
        """Every key under ``prefix`` (follows continuation tokens)."""
        objects: list[S3Object] = []
        token: str | None = None
        for _ in range(max_pages):
            page = self.list_page(prefix, continuation_token=token)
            objects.extend(page.objects)
            if not page.is_truncated:
                return objects
            token = page.next_token
        raise BucketError(f"listing {prefix!r} exceeded {max_pages} pages")

    def get_text(self, key: str) -> str:
        """A whole small object as text (used for ``.idx`` inventories)."""
        return self._get(self.url(key), "idx").decode("utf-8")

    def get_range(self, key: str, start: int, end: int) -> bytes:
        """Bytes ``start..end`` (inclusive) of an object, verified against Content-Range."""
        if start < 0 or end < start:
            raise ValueError(f"bad byte range {start}-{end}")

        def check(response: httpx.Response) -> None:
            if response.status_code != 206:
                raise RangeNotHonouredError(
                    f"expected 206 for bytes={start}-{end} of {key}, got "
                    f"{response.status_code} (body not read)"
                )
            match = _CONTENT_RANGE_RE.match(response.headers.get("content-range", ""))
            if match is None or (int(match[1]), int(match[2])) != (start, end):
                raise RangeNotHonouredError(
                    f"Content-Range {response.headers.get('content-range')!r} does not match "
                    f"bytes={start}-{end} of {key}"
                )

        body = self._get(
            self.url(key), "grib", headers={"Range": f"bytes={start}-{end}"}, check=check
        )
        if len(body) != end - start + 1:
            raise RangeNotHonouredError(f"got {len(body)} bytes for bytes={start}-{end} of {key}")
        return body


class _Retryable(Exception):
    """Internal: a response worth retrying."""
