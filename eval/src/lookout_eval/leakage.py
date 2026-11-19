"""The leakage guard: the only door to frames for baselines and fitted assets (PRD §13.1, §13.9).

Rules enforced here, by raising :class:`LeakageError`:

1. **Issue time.** A baseline issued at time *t* sees only frames *available* at *t*:
   ``image_time <= t`` **and** ``available_at <= t`` (PRD §13.1: "Baselines see only frames
   available at issue time"). Every access path applies the same check: listings,
   :meth:`IssueTimeView.nearest`, ``get()``, ``label()`` and ``read_bytes()``. There is no
   exception, persistence (b) included (see :mod:`.baselines`).
2. **Held-out frames.**

   - The labelling store holds no labels for held-out frames (PRD §13.1: "held-out frames
     are absent from the labelling store"). :class:`FrameStore` refuses them unless it is
     built for the sealed scoring (:class:`SealedScoring`, M4), which names the commit of
     the frozen pre-registration (§13.4).
   - A fit never sees a held-out frame, nor any frame at or after the start of the earliest
     held-out period (§13.9: "Every fitted asset comes only from development frames that
     precede the held-out season"): plates, haze profiles, clear-day references, τ, S_mos,
     climatology and analog weights.
   - Issue-time views refuse held-out frames too, unless built with ``allow_held_out=True``,
     which only a sealed-scoring store allows.
3. **Fits up to a time.** A fit view built with ``until=t`` also refuses frames imaged or
   made available after *t*, so a climatology evaluated at an issue time is fitted only on
   frames that were available then.

:class:`FrameStore` holds the records, their labels (extractor outputs) and a byte reader.
Views hand out records that pass the rules; ``label()``, ``read_bytes()`` and ``get()`` check
again, so a record obtained some other way (another view, a constructed key) still cannot be
read. With ``record_accesses=True`` every access is logged, so a test can assert that no read
broke the rules.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .frames import FrameRecord

__all__ = [
    "SEALED_WINDOW_FIRST_DAY",
    "SEALED_WINDOW_LAST_DAY",
    "Access",
    "FitView",
    "FrameStore",
    "HeldOut",
    "IssueTimeView",
    "LeakageError",
    "SealedScoring",
]

#: Design P's sealed window, as local calendar days (PRD §13.2: 15 Dec 2026 – 14 Jan 2027,
#: "extended by any outage"). The committed split replaces it once DECISIONS.md records one.
SEALED_WINDOW_FIRST_DAY = date(2026, 12, 15)
SEALED_WINDOW_LAST_DAY = date(2027, 1, 14)

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")


class LeakageError(RuntimeError):
    """A baseline or a fit tried to read a frame it must not see."""


def _aware(moment: datetime, what: str) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware")
    return moment


@dataclass(frozen=True)
class HeldOut:
    """Held-out periods as half-open UTC intervals ``[start, end)``."""

    intervals: tuple[tuple[datetime, datetime], ...] = ()

    def __post_init__(self) -> None:
        for start, end in self.intervals:
            _aware(start, "held-out start")
            _aware(end, "held-out end")
            if not start < end:
                raise ValueError("held-out interval must have start < end")

    @classmethod
    def from_local_dates(cls, first_day: date, last_day: date, time_zone: str) -> HeldOut:
        """Whole local days ``first_day`` .. ``last_day`` inclusive, e.g. the sealed window."""
        if last_day < first_day:
            raise ValueError("last_day is before first_day")
        zone = ZoneInfo(time_zone)
        start = datetime.combine(first_day, time(0), tzinfo=zone)
        end = datetime.combine(last_day + timedelta(days=1), time(0), tzinfo=zone)
        return cls(((start, end),))

    @classmethod
    def sealed_window(cls, time_zone: str) -> HeldOut:
        """Design P's sealed window (PRD §13.2) in a viewpoint's time zone."""
        return cls.from_local_dates(SEALED_WINDOW_FIRST_DAY, SEALED_WINDOW_LAST_DAY, time_zone)

    def __or__(self, other: HeldOut) -> HeldOut:
        if not isinstance(other, HeldOut):
            return NotImplemented
        return HeldOut(self.intervals + other.intervals)

    @property
    def earliest_start(self) -> datetime | None:
        """Start of the first held-out period; fits see only frames before it (PRD §13.9)."""
        return min((start for start, _ in self.intervals), default=None)

    def contains(self, moment: datetime) -> bool:
        """True if ``moment`` falls in a held-out interval."""
        return any(start <= moment < end for start, end in self.intervals)

    def to_dict(self) -> list[list[str]]:
        """JSON-ready list of ``[start, end)`` pairs."""
        return [[s.isoformat(), e.isoformat()] for s, e in self.intervals]


@dataclass(frozen=True)
class SealedScoring:
    """Permission to label and read held-out frames: the final sealed scoring only (M4).

    ``preregistration_commit`` is the full git commit of the frozen PRD §13.4
    pre-registration, which DECISIONS.md records before any held-out frame is scored,
    labelled or viewed.
    """

    preregistration_commit: str

    def __post_init__(self) -> None:
        if not _COMMIT_RE.fullmatch(self.preregistration_commit):
            raise ValueError("preregistration_commit must be a full 40-character git commit")


@dataclass(frozen=True, slots=True)
class Access:
    """One logged access. ``issue_time`` is the view's issue time (or a fit's ``until``)."""

    key: str
    viewpoint_id: str
    image_time: datetime
    available_at: datetime
    purpose: str
    what: str  # "record", "label" or "bytes"
    issue_time: datetime | None
    held_out: bool

    @property
    def post_issue(self) -> bool:
        """The frame was imaged or made available after the issue time."""
        return self.issue_time is not None and (
            self.image_time > self.issue_time or self.available_at > self.issue_time
        )


class FrameStore:
    """Frame records, their labels and pixels, behind the leakage rules.

    ``labels`` maps a frame key to a dict of named labels (for example
    ``{"summit": "visible", "weather_class": "clear"}``); every key must be a record of the
    store, and held-out frames may carry labels only in a store built with ``sealed``.
    ``reader`` returns a frame's bytes.
    """

    def __init__(
        self,
        records: Iterable[FrameRecord],
        *,
        held_out: HeldOut | None = None,
        labels: Mapping[str, Mapping[str, Any]] | None = None,
        reader: Callable[[FrameRecord], bytes] | None = None,
        record_accesses: bool = False,
        sealed: SealedScoring | None = None,
    ) -> None:
        self.held_out = held_out or HeldOut()
        self.sealed = sealed
        self._by_vp: dict[str, list[FrameRecord]] = {}
        self._by_key: dict[str, FrameRecord] = {}
        for record in records:
            if record.key in self._by_key:
                raise ValueError(f"duplicate frame key {record.key}")
            self._by_key[record.key] = record
            self._by_vp.setdefault(record.viewpoint_id, []).append(record)
        self._times: dict[str, list[datetime]] = {}
        for vp, items in self._by_vp.items():
            items.sort(key=lambda r: (r.image_time, r.key))
            self._times[vp] = [r.image_time for r in items]
        self._labels: dict[str, dict[str, Any]] = {}
        for key, value in (labels or {}).items():
            record = self._by_key.get(key)
            if record is None:
                raise ValueError(f"labels for {key!r}, which is not a frame of this store")
            if sealed is None and self.held_out.contains(record.image_time):
                raise LeakageError(
                    f"held-out frame {key} has labels; held-out frames are absent from the "
                    "labelling store until the sealed scoring (PRD §13.1; SealedScoring)"
                )
            self._labels[key] = dict(value)
        self._reader = reader
        self.record_accesses = record_accesses
        self._log: list[Access] = []

    @property
    def accesses(self) -> tuple[Access, ...]:
        """Logged accesses (empty unless ``record_accesses``)."""
        return tuple(self._log)

    @property
    def fit_bound(self) -> datetime | None:
        """Fits see only frames imaged before this moment (the earliest held-out start)."""
        return self.held_out.earliest_start

    def clear_log(self) -> None:
        """Forget logged accesses."""
        self._log.clear()

    def viewpoints(self) -> tuple[str, ...]:
        """Viewpoints with at least one frame."""
        return tuple(sorted(self._by_vp))

    def as_of(
        self, issue_time: datetime, *, purpose: str, allow_held_out: bool = False
    ) -> IssueTimeView:
        """A view for a prediction issued at ``issue_time`` (rule 1, and rule 2 by default)."""
        if allow_held_out and self.sealed is None:
            raise LeakageError(
                "allow_held_out needs a FrameStore built for the sealed scoring (SealedScoring)"
            )
        return IssueTimeView(self, _aware(issue_time, "issue_time"), purpose, allow_held_out)

    def for_fit(self, *, purpose: str, until: datetime | None = None) -> FitView:
        """A view for fitting an asset: development frames before the held-out periods.

        With ``until``, only frames imaged and available by then.
        """
        return FitView(self, purpose, None if until is None else _aware(until, "until"))

    # -- internals used by the views -------------------------------------------------------
    def _records(self, viewpoint_id: str, upto: datetime | None) -> list[FrameRecord]:
        items = self._by_vp.get(viewpoint_id, [])
        if upto is None:
            return list(items)
        index = bisect.bisect_right(self._times[viewpoint_id], upto)
        return items[:index]

    def _record(self, key: str) -> FrameRecord:
        try:
            return self._by_key[key]
        except KeyError:
            raise KeyError(f"no frame {key!r} in the store") from None

    def _label(self, record: FrameRecord, name: str) -> Any:
        return self._labels.get(record.key, {}).get(name)

    def _bytes(self, record: FrameRecord) -> bytes:
        if self._reader is None:
            raise RuntimeError("this FrameStore has no byte reader")
        return self._reader(record)

    def _log_access(
        self, record: FrameRecord, purpose: str, what: str, issue_time: datetime | None
    ) -> None:
        if self.record_accesses:
            self._log.append(
                Access(
                    key=record.key,
                    viewpoint_id=record.viewpoint_id,
                    image_time=record.image_time,
                    available_at=record.available_at,
                    purpose=purpose,
                    what=what,
                    issue_time=issue_time,
                    held_out=self.held_out.contains(record.image_time),
                )
            )


def _matches(label: Any, values: Collection[Any] | None) -> bool:
    return label is not None if values is None else label in values


class IssueTimeView:
    """What a baseline may see at its issue time (see the module docstring)."""

    def __init__(
        self, store: FrameStore, issue_time: datetime, purpose: str, allow_held_out: bool
    ) -> None:
        self._store = store
        self.issue_time = issue_time
        self.purpose = purpose
        self.allow_held_out = allow_held_out

    def _refusal(self, record: FrameRecord) -> str | None:
        if record.image_time > self.issue_time:
            return (
                f"frame {record.key} (image time {record.image_time.isoformat()}) is after the "
                f"issue time {self.issue_time.isoformat()} [{self.purpose}]"
            )
        if record.available_at > self.issue_time:
            return (
                f"frame {record.key} was not available until {record.available_at.isoformat()}, "
                f"after the issue time {self.issue_time.isoformat()} [{self.purpose}]"
            )
        if not self.allow_held_out and self._store.held_out.contains(record.image_time):
            return f"frame {record.key} is held out [{self.purpose}]"
        return None

    def _check(self, record: FrameRecord) -> FrameRecord:
        reason = self._refusal(record)
        if reason:
            raise LeakageError(reason)
        return record

    def records(
        self,
        viewpoint_id: str,
        *,
        label: str | None = None,
        values: Collection[Any] | None = None,
    ) -> list[FrameRecord]:
        """Frames available at the issue time, oldest first.

        With ``label``, only frames whose label is in ``values`` (or not None) are returned.
        """
        out = []
        for record in self._store._records(viewpoint_id, self.issue_time):
            if self._refusal(record) is not None:
                continue
            if label is not None and not _matches(self._store._label(record, label), values):
                continue
            self._store._log_access(record, self.purpose, "record", self.issue_time)
            out.append(record)
        return out

    def latest(
        self,
        viewpoint_id: str,
        *,
        label: str | None = None,
        values: Collection[Any] | None = None,
    ) -> FrameRecord | None:
        """The newest frame available at the issue time (optionally with a usable label)."""
        for record in reversed(self._store._records(viewpoint_id, self.issue_time)):
            if self._refusal(record) is not None:
                continue
            if label is not None and not _matches(self._store._label(record, label), values):
                continue
            self._store._log_access(record, self.purpose, "record", self.issue_time)
            return record
        return None

    def nearest(
        self,
        viewpoint_id: str,
        target: datetime,
        *,
        tolerance: timedelta,
        label: str | None = None,
        values: Collection[Any] | None = None,
    ) -> FrameRecord | None:
        """The frame nearest ``target`` within ``tolerance``, among frames available now.

        The search window ``target ± tolerance`` is cut at the issue time, and a frame
        imaged before the issue time but made available after it is skipped. A ``target``
        after the issue time asks for the future and raises :class:`LeakageError`.
        """
        _aware(target, "target")
        if tolerance < timedelta(0):
            raise ValueError("tolerance must not be negative")
        if target > self.issue_time:
            raise LeakageError(
                f"nearest({target.isoformat()}) asks for a moment after the issue time "
                f"{self.issue_time.isoformat()} [{self.purpose}]"
            )
        window_end = min(target + tolerance, self.issue_time)
        best: FrameRecord | None = None
        best_gap: timedelta | None = None
        for record in self._store._records(viewpoint_id, window_end):
            gap = abs(record.image_time - target)
            if gap > tolerance or self._refusal(record) is not None:
                continue
            if label is not None and not _matches(self._store._label(record, label), values):
                continue
            if best_gap is None or gap < best_gap:
                best, best_gap = record, gap
        if best is not None:
            self._store._log_access(best, self.purpose, "record", self.issue_time)
        return best

    def get(self, key: str) -> FrameRecord:
        """Look a frame up by key; raises :class:`LeakageError` if this view may not see it."""
        record = self._check(self._store._record(key))
        self._store._log_access(record, self.purpose, "record", self.issue_time)
        return record

    def label(self, record: FrameRecord, name: str) -> Any:
        """A frame's label, after the leakage check."""
        stored = self._check(self._store._record(record.key))
        self._store._log_access(stored, self.purpose, "label", self.issue_time)
        return self._store._label(stored, name)

    def read_bytes(self, record: FrameRecord) -> bytes:
        """A frame's bytes, after the leakage check."""
        stored = self._check(self._store._record(record.key))
        self._store._log_access(stored, self.purpose, "bytes", self.issue_time)
        return self._store._bytes(stored)


class FitView:
    """What a fitted asset may see (rule 2 and rule 3 of the module docstring)."""

    def __init__(self, store: FrameStore, purpose: str, until: datetime | None) -> None:
        self._store = store
        self.purpose = purpose
        self.until = until

    def _refusal(self, record: FrameRecord) -> str | None:
        if self._store.held_out.contains(record.image_time):
            return f"fit '{self.purpose}' may not read held-out frame {record.key}"
        bound = self._store.fit_bound
        if bound is not None and record.image_time >= bound:
            return (
                f"fit '{self.purpose}' may read only development frames from before the "
                f"held-out period that starts {bound.isoformat()}; {record.key} is later "
                "(PRD §13.9)"
            )
        if self.until is not None and record.image_time > self.until:
            return (
                f"fit '{self.purpose}' is limited to frames up to {self.until.isoformat()}; "
                f"{record.key} is later"
            )
        if self.until is not None and record.available_at > self.until:
            return (
                f"fit '{self.purpose}' is limited to frames available by "
                f"{self.until.isoformat()}; {record.key} was available at "
                f"{record.available_at.isoformat()}"
            )
        return None

    def _check(self, record: FrameRecord) -> FrameRecord:
        reason = self._refusal(record)
        if reason:
            raise LeakageError(reason)
        return record

    def records(
        self,
        viewpoint_id: str,
        *,
        label: str | None = None,
        values: Collection[Any] | None = None,
    ) -> list[FrameRecord]:
        """Development frames the fit may use, oldest first, optionally with a usable label."""
        out = []
        for record in self._store._records(viewpoint_id, self.until):
            if self._refusal(record) is not None:
                continue
            if label is not None and not _matches(self._store._label(record, label), values):
                continue
            self._store._log_access(record, self.purpose, "record", self.until)
            out.append(record)
        return out

    def get(self, key: str) -> FrameRecord:
        """Look a frame up by key; raises :class:`LeakageError` for a frame the fit may not use."""
        record = self._check(self._store._record(key))
        self._store._log_access(record, self.purpose, "record", self.until)
        return record

    def label(self, record: FrameRecord, name: str) -> Any:
        """A frame's label, after the leakage check."""
        stored = self._check(self._store._record(record.key))
        self._store._log_access(stored, self.purpose, "label", self.until)
        return self._store._label(stored, name)

    def read_bytes(self, record: FrameRecord) -> bytes:
        """A frame's bytes, after the leakage check."""
        stored = self._check(self._store._record(record.key))
        self._store._log_access(stored, self.purpose, "bytes", self.until)
        return self._store._bytes(stored)
