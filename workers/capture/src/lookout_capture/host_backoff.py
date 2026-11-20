"""One back-off for www.nps.gov, shared by live capture and the archive (PRD §15.3 "Errors").

CLAUDE.md says to back off on errors. Live capture and the archive backfill both send requests
to www.nps.gov, and since D-023 they can run in one process (``loop --archive-queue``). So an
HTTP 429 or 503 from either one holds every request to the host:

- The hold is one JSON document in the run's storage, :data:`HOST_STATE_KEY`
  (``state/capture/hosts/www.nps.gov.json``): ``{"host", "not_before", "reason", "set_at",
  "source"}``. The CLI loop, the archive work inside it, a later ``archive`` command and the
  Lambda capture job therefore all see the same hold.
- Its length is the larger of the server's ``Retry-After`` and the caller's own back-off.
- It is only ever extended, under the storage lock ``capture/hosts``; a shorter hold never
  replaces a longer one.

Live capture has written and read this document since M0.5 (the frozen runtime reads
``not_before`` and keeps the other fields), so the format only gains fields.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta

from lookout_common.storage import LockTimeoutError, Storage
from lookout_common.timeutil import isoformat_z

from .state import HOST_STATE_KEY, parse_time

__all__ = ["HOST", "HOST_LOCK", "HOST_STATE_KEY", "extend_host_hold", "host_hold_until"]

#: The host every live feed, live image and archive endpoint is on.
HOST = "www.nps.gov"
#: Storage lock taken while the hold document is read and rewritten.
HOST_LOCK = "capture/hosts"


def host_hold_until(storage: Storage, now: datetime) -> datetime | None:
    """The end of the host-wide hold if it is still in force at ``now``, else None.

    An unreadable document raises (``ValueError``), so a caller fails closed.
    """
    doc = storage.read_json(HOST_STATE_KEY, default={}) or {}
    until = parse_time(doc.get("not_before")) if isinstance(doc, Mapping) else None
    return until if until is not None and until > now else None


def extend_host_hold(
    storage: Storage,
    now: datetime,
    seconds: float,
    reason: str,
    *,
    source: str,
    lock_timeout_s: float = 10.0,
) -> datetime | None:
    """Hold every request to www.nps.gov until ``now + seconds``; returns the hold in force.

    A longer hold already recorded is kept. Returns None, writing nothing, if the lock stays
    busy for ``lock_timeout_s`` (the caller's own back-off still applies).
    """
    until = now + timedelta(seconds=max(0.0, float(seconds)))
    try:
        with storage.lock(HOST_LOCK, timeout_s=lock_timeout_s, ttl_s=60.0):
            doc = storage.read_json(HOST_STATE_KEY, default={}) or {}
            if not isinstance(doc, dict):
                doc = {}
            current = parse_time(doc.get("not_before"))
            if current is not None and current >= until:
                return current
            doc.update(
                {
                    "host": HOST,
                    "not_before": isoformat_z(until, "seconds"),
                    "reason": reason,
                    "set_at": isoformat_z(now, "seconds"),
                    "source": source,
                }
            )
            storage.write_json(HOST_STATE_KEY, doc)
            return until
    except LockTimeoutError:
        return None
