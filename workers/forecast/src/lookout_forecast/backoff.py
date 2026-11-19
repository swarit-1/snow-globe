"""Run-level back-off for the scheduled worker (CLAUDE.md "Back off on errors"; PRD §11.2).

When a run fails, the Lambda records it in ``state/forecast/hrrr/<YYYYMMDDHH>z.failure.json``
and does not re-list, re-plan or re-download that run on every tick:

- a **cap** refusal (the planned download is over ``LOOKOUT_FORECAST_MAX_MB``) is not
  retried until the settings change (fields, hours, cap or viewpoints), because the same
  plan would be refused again;
- any other **error** is retried after 10, 20, 40, ... minutes, at most 4 h apart.

Changed settings always allow a retry. A successful extraction marks the record
resolved (storage has no delete; CLAUDE.md). The record sits beside, not inside, the
run's state marker, so recording a failure can never overwrite a marker that another
extraction wrote.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from lookout_common.paths import state_key
from lookout_common.storage import Storage
from lookout_common.timeutil import isoformat_z, parse_iso_utc

from .hrrr import STORAGE_SOURCE, format_run_id

__all__ = [
    "BACKOFF_BASE_S",
    "BACKOFF_MAX_S",
    "FailureKind",
    "RunFailure",
    "failure_key",
    "next_delay_s",
    "read_failure",
    "record_failure",
    "resolve_failure",
    "settings_fingerprint",
]

FailureKind = Literal["cap", "error"]

#: First retry delay after an error, then doubled per further failure.
BACKOFF_BASE_S = 600.0
#: Longest delay between two attempts at one run.
BACKOFF_MAX_S = 4 * 3600.0


def failure_key(run_start: datetime) -> str:
    """``state/forecast/hrrr/<YYYYMMDDHH>z.failure.json``."""
    return state_key(f"forecast/{STORAGE_SOURCE}/{format_run_id(run_start)}z.failure.json")


def settings_fingerprint(
    *,
    field_names: Sequence[str],
    max_fh: int | None,
    max_bytes: int | None,
    viewpoint_ids: Sequence[str],
) -> str:
    """A stable string for the settings a failure applies to."""
    return json.dumps(
        {
            "fields": sorted(field_names),
            "max_fh": max_fh,
            "max_bytes": max_bytes,
            "viewpoints": sorted(viewpoint_ids),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def next_delay_s(attempts: int) -> float:
    """Wait after the ``attempts``-th consecutive error: 10 min, doubling, at most 4 h."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    return min(BACKOFF_BASE_S * 2.0 ** (attempts - 1), BACKOFF_MAX_S)


@dataclass(frozen=True)
class RunFailure:
    """The last failure of one run, as stored."""

    run_id: str
    kind: FailureKind
    error: str
    attempts: int
    first_failed_utc: datetime
    last_failed_utc: datetime
    #: None for cap refusals: they wait for the settings to change.
    next_try_utc: datetime | None
    settings: str

    def skip_reason(self, now: datetime, settings: str) -> str | None:
        """Why the run should not be tried at ``now`` with ``settings``; None to try it."""
        if settings != self.settings:
            return None
        if self.kind == "cap":
            return (
                f"refused by the cap with these settings ({self.error}); retried once the "
                "fields, hours, cap or viewpoints change"
            )
        if self.next_try_utc is not None and now < self.next_try_utc:
            return (
                f"{self.attempts} failure(s), next try at {isoformat_z(self.next_try_utc)}; "
                f"last: {self.error}"
            )
        return None

    def to_json(self) -> dict[str, Any]:
        """The stored record."""
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "error": self.error,
            "attempts": self.attempts,
            "first_failed_utc": isoformat_z(self.first_failed_utc, "seconds"),
            "last_failed_utc": isoformat_z(self.last_failed_utc, "seconds"),
            "next_try_utc": (
                isoformat_z(self.next_try_utc, "seconds") if self.next_try_utc else None
            ),
            "settings": self.settings,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> RunFailure:
        """Parse a stored record (raises ``KeyError``/``ValueError`` if malformed)."""
        kind = data["kind"]
        if kind not in ("cap", "error"):
            raise ValueError(f"unknown failure kind {kind!r}")
        return cls(
            run_id=str(data["run_id"]),
            kind=kind,
            error=str(data["error"]),
            attempts=int(data["attempts"]),
            first_failed_utc=parse_iso_utc(data["first_failed_utc"]),
            last_failed_utc=parse_iso_utc(data["last_failed_utc"]),
            next_try_utc=parse_iso_utc(data["next_try_utc"]) if data["next_try_utc"] else None,
            settings=str(data["settings"]),
        )


def read_failure(storage: Storage, run_start: datetime) -> RunFailure | None:
    """The run's unresolved failure, or None (no record, resolved, or unreadable)."""
    data = storage.read_json(failure_key(run_start), default=None)
    if not isinstance(data, Mapping) or data.get("resolved_utc"):
        return None
    try:
        return RunFailure.from_json(data)
    except (KeyError, TypeError, ValueError):
        return None  # an unreadable record must not block the run forever


def record_failure(
    storage: Storage,
    run_start: datetime,
    *,
    kind: FailureKind,
    error: str,
    settings: str,
    now: datetime,
) -> RunFailure:
    """Store one more failure of the run and return the new record.

    Attempts count consecutive failures under the same settings; new settings start over.
    """
    previous = read_failure(storage, run_start)
    if previous is not None and previous.settings == settings:
        attempts, first = previous.attempts + 1, previous.first_failed_utc
    else:
        attempts, first = 1, now
    failure = RunFailure(
        run_id=format_run_id(run_start),
        kind=kind,
        error=error[:500],
        attempts=attempts,
        first_failed_utc=first,
        last_failed_utc=now,
        next_try_utc=None if kind == "cap" else now + timedelta(seconds=next_delay_s(attempts)),
        settings=settings,
    )
    storage.write_json(failure_key(run_start), failure.to_json())
    return failure


def resolve_failure(storage: Storage, run_start: datetime, *, now: datetime) -> bool:
    """Mark an unresolved failure record resolved (kept as history); True if there was one."""
    previous = read_failure(storage, run_start)
    if previous is None:
        return False
    storage.write_json(
        failure_key(run_start),
        {"resolved_utc": isoformat_z(now, "seconds"), "last_failure": previous.to_json()},
    )
    return True
