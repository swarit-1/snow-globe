"""The hindcast's bucket client: every small answer kept, every request polite (PRD §15.3).

:class:`CachingBucket` is a :class:`..bucket.HrrrBucket` (anonymous HTTPS, the project
User-Agent, retries with exponential back-off, ``206`` checks) that also:

- keeps every ListObjectsV2 page and every ``.idx`` inventory it receives under the
  hindcast's ``cache/s3/`` and answers the same request from there afterwards, so the plan
  and the fetch never download one twice (a listing is kept only once its run is more than
  :data:`..discovery.STAMP_TRUST_H` old, when its stamps are final);
- never keeps GRIB bytes (CLAUDE.md "NOAA": GRIB subsets are deleted after extraction);
- before **every attempt** of a network request, the first and each retry after a back-off
  (:meth:`..bucket.HrrrBucket._before_attempt`), waits out the :class:`.quiet.QuietWindow`
  and, with ``cap_bytes``, refuses (:class:`NetworkCapError`) once the hindcast's persisted
  download total has reached the cap; a cache hit is never delayed or refused;
- adds every network request to a ledger persisted in the hindcast's state
  (:data:`LEDGER_KEY`), so the hindcast's total download is known across commands and
  restarts and the 2,000 MB cap can be enforced on it. Each update adds only this client's
  new counts to the totals as stored at that moment, under a file lock
  (:data:`LEDGER_LOCK`), so two clients never drop each other's bytes.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from snow_globe_common.storage import Storage
from snow_globe_common.timeutil import isoformat_z, parse_iso_utc, utcnow

from ..bucket import HrrrBucket, transfer_delta
from ..discovery import STAMP_TRUST_H
from ..extract import BandwidthCapError
from ..hrrr import run_prefix
from .quiet import QuietWindow

__all__ = [
    "CACHE_S3_PREFIX",
    "LEDGER_KEY",
    "LEDGER_LOCK",
    "CachingBucket",
    "NetworkCapError",
    "merge_ledgers",
]

#: Where answers are kept, in the hindcast root.
CACHE_S3_PREFIX = "cache/s3/"
#: The persisted network ledger, in the hindcast root.
LEDGER_KEY = "state/network-ledger.json"
#: Held for each ledger update (a read, an addition and a write), in the hindcast root.
LEDGER_LOCK = "hindcast-ledger"
_RUN_PREFIX_RE = re.compile(r"^hrrr\.(\d{8})/conus/hrrr\.t(\d{2})z\.wrfsfcf$")


class NetworkCapError(BandwidthCapError):
    """The hindcast's persisted download total has reached the cap: no request was sent."""


def merge_ledgers(base: Mapping[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    """``base + extra`` for two :meth:`..bucket.TransferLedger.snapshot`-shaped dicts."""

    def add(key: str, digits: int | None = None) -> dict[str, Any]:
        out: dict[str, Any] = dict(base.get(key, {}))
        for kind, value in extra.get(key, {}).items():
            total = out.get(kind, 0) + value
            out[kind] = round(total, digits) if digits is not None else total
        return out

    byte_counts = add("bytes")
    return {
        "requests": add("requests"),
        "bytes": byte_counts,
        "seconds": add("seconds", 3),
        "retries": int(base.get("retries", 0)) + int(extra.get("retries", 0)),
        "total_bytes": sum(byte_counts.values()),
    }


class CachingBucket(HrrrBucket):
    """An :class:`..bucket.HrrrBucket` that keeps listings and inventories (see module doc).

    ``cache`` is the hindcast root storage. ``quiet`` is waited out before each network
    attempt (None: no window, for tests). ``cap_bytes``, if given, refuses every network
    attempt once the persisted total has reached it. ``ledger_key`` None keeps no persisted
    ledger. The in-memory :attr:`ledger` counts this client's network requests only; cache
    hits are counted in :attr:`cache_hits`.
    """

    def __init__(
        self,
        cache: Storage,
        *,
        quiet: QuietWindow | None = None,
        cap_bytes: int | None = None,
        ledger_key: str | None = LEDGER_KEY,
        clock: Callable[[], datetime] = utcnow,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.cache = cache
        self.quiet = quiet
        self.cap_bytes = cap_bytes
        self.clock = clock
        self.ledger_key = ledger_key
        self.cache_hits: Counter[str] = Counter()
        self._persist_lock = threading.Lock()
        # This client's counts already added to the stored totals (none yet).
        self._persisted: dict[str, Any] = self.ledger.snapshot()

    # -- cache keys ---------------------------------------------------------------------
    def _listing_key(self, prefix: str, token: str | None, max_keys: str) -> str | None:
        match = _RUN_PREFIX_RE.match(prefix)
        if match is None:
            return None  # only one run's listing is kept (a day listing is never used here)
        run_start = datetime.strptime(match[1] + match[2], "%Y%m%d%H").replace(tzinfo=UTC)
        if self.clock() - run_start < timedelta(hours=STAMP_TRUST_H):
            return None  # its stamps can still change: never kept
        name = prefix.replace("/", "_")
        suffix = f".{hashlib.sha256(token.encode()).hexdigest()[:16]}" if token else ""
        return f"{CACHE_S3_PREFIX}list/{name}.max{max_keys}{suffix}.xml"

    def _cache_key(self, url: str, kind: str) -> str | None:
        parts = urlsplit(url)
        if kind == "idx":
            return f"{CACHE_S3_PREFIX}idx/{parts.path.lstrip('/')}"
        if kind == "listing":
            query = parse_qs(parts.query)
            prefix = query.get("prefix", [""])[0]
            token = query.get("continuation-token", [None])[0]
            return self._listing_key(prefix, token, query.get("max-keys", ["1000"])[0])
        return None  # GRIB ranges are never kept

    def listing_fetched_utc(self, run_start: datetime) -> datetime | None:
        """When the kept first listing page of a run was received (None if not kept)."""
        key = self._listing_key(run_prefix(run_start), None, "1000")
        if key is None:
            return None
        meta = self.cache.read_json(f"{key}.json", default=None)
        if not isinstance(meta, Mapping) or not meta.get("fetched_utc"):
            return None
        return parse_iso_utc(str(meta["fetched_utc"]))

    # -- the ledger -------------------------------------------------------------------
    def _read_stored(self) -> dict[str, Any]:
        if self.ledger_key is None:
            return {}
        stored = self.cache.read_json(self.ledger_key, default=None)
        return dict(stored) if isinstance(stored, Mapping) else {}

    def stored_ledger(self) -> dict[str, Any]:
        """Every network request the hindcast has made: the persisted totals, read now (so
        other clients' requests count), plus this client's not persisted yet."""
        with self._persist_lock:
            pending = transfer_delta(self._persisted, self.ledger.snapshot())
            return merge_ledgers(self._read_stored(), pending)

    def _persist(self) -> None:
        """Add this client's new counts to the stored totals (read again under the lock)."""
        if self.ledger_key is None:
            return
        with self._persist_lock:
            snapshot = self.ledger.snapshot()
            delta = transfer_delta(self._persisted, snapshot)
            if not (delta["requests"] or delta["bytes"] or delta["retries"]):
                return  # nothing new (a refused request sends nothing)
            self._add_to_stored(delta)
            self._persisted = snapshot

    def _add_to_stored(self, delta: Mapping[str, Any]) -> None:
        assert self.ledger_key is not None
        with self.cache.lock(LEDGER_LOCK, timeout_s=60.0, ttl_s=120.0):
            totals = merge_ledgers(self._read_stored(), delta)
            totals["updated_utc"] = isoformat_z(self.clock(), "seconds")
            self.cache.write_json(self.ledger_key, totals)

    # -- the request choke point -----------------------------------------------------------
    def _before_attempt(self, attempt: int) -> None:
        """Before every network attempt (retries too): the quiet window, then the cap."""
        if self.quiet is not None:
            self.quiet.wait()
        if self.cap_bytes is not None:
            spent = int(self.stored_ledger()["total_bytes"])
            if spent >= self.cap_bytes:
                raise NetworkCapError(
                    f"the hindcast has downloaded {spent / 1e6:.1f} MB, the cap of "
                    f"{self.cap_bytes / 1e6:.1f} MB: no further request is sent",
                    planned_bytes=spent,
                    cap_bytes=self.cap_bytes,
                )

    def _get(
        self,
        url: str,
        kind: Any,
        *,
        headers: dict[str, str] | None = None,
        check: Callable[[httpx.Response], None] | None = None,
    ) -> bytes:
        key = self._cache_key(url, kind)
        if key is not None and self.cache.exists(key):
            self.cache_hits[kind] += 1
            return self.cache.get_bytes(key)
        try:
            body = super()._get(url, kind, headers=headers, check=check)
        finally:
            self._persist()  # failed attempts and retries count too
        if key is not None:
            self.cache.put_bytes(key, body)
            self.cache.write_json(
                f"{key}.json",
                {
                    "url": url,
                    "fetched_utc": isoformat_z(self.clock(), "milliseconds"),
                    "bytes": len(body),
                },
            )
        return body
