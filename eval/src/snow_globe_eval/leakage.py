"""The leakage guard: the only door to frames for baselines and fitted assets (PRD §13.1, §13.9).

Rules enforced here, by raising :class:`LeakageError`:

1. **Issue time.** A baseline issued at time *t* sees only frames *available* at *t*:
   ``image_time <= t`` **and** ``available_at <= t`` (PRD §13.1: "Baselines see only frames
   available at issue time"). Every access path applies the same check: listings,
   :meth:`IssueTimeView.nearest`, ``get()``, ``label()`` and ``read_bytes()``. There is no
   exception, persistence (b) included (see :mod:`.baselines`).
2. **Held-out frames**, from the committed split (:mod:`.heldout`, D-019, D-022, D-024). A
   frame is held out when it falls in one of its viewpoint's held-out periods (design P's
   sealed window, design H's summer 2026), or in an extra period the caller adds (extras can
   only add), or when it is an archived NPS frame past the archive guard (dated on or after
   1 Jun 2026, at every viewpoint).

   - The labelling store holds no labels for held-out frames (PRD §13.1: "held-out frames
     are absent from the labelling store"). :class:`FrameStore` refuses them unless it is
     built for the sealed scoring (:class:`SealedScoring`, M4), which names the commit of
     the frozen pre-registration (§13.4).
   - Issue-time views refuse held-out frames too, unless built with ``allow_held_out=True``,
     which only a sealed-scoring store allows.
   - A fit never sees a held-out frame, sealed or not. Nor does it see any frame at or after
     its *fit bound* (§13.9: "Every fitted asset comes only from development frames that
     precede the held-out season"). That bound is the start of the viewpoint's earliest
     held-out period, or, for a fit made for one design (``design="P"`` or ``"H"``), the
     start of that design's period. The rule covers plates, haze profiles, clear-day
     references, τ, S_mos, climatology and analog weights.
3. **Fits up to a time.** A fit view built with ``until=t`` also refuses frames imaged or
   made available after *t*, so a climatology evaluated at an issue time is fitted only on
   frames that were available then.
4. **Quarantine and unknown viewpoints.** A store refuses, when it is built, any record
   under the quarantine folder (D-022; the name in any letter case) and any viewpoint the
   committed split does not name. That is the fail-closed reading of PRD §13.2, which
   commits the split before any frame is touched. For frames on disk, use
   :func:`.frames.storage_reader` as the store's reader: it checks where each file resolves
   at every read.
5. **One split.** A store always uses the committed split (:func:`.heldout.committed_split`);
   it takes no other. Only :class:`SealedScoring` opens held-out frames, and only to
   issue-time views and labels, never to fits.

:class:`FrameStore` holds the records, their labels (extractor outputs) and a byte reader.
Views hand out records that pass the rules; ``label()``, ``read_bytes()`` and ``get()`` check
again, so a record obtained some other way (another view, a constructed key) still cannot be
read. With ``record_accesses=True`` every access is logged, so a test can assert that no read
broke the rules (the default suite does, in its audit tests).

The scoring exclusions (15 Jul 2026 at the 7 ARD cameras; Grand Teton not scored) do not
block reading. Scores apply them as a mask: :meth:`.heldout.HeldOutSplit.scoring_excluded_mask`.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from snow_globe_common.timeutil import isoformat_z

from .frames import FrameRecord, record_from_path
from .heldout import (
    DESIGNS,
    HeldOut,
    HeldOutSplit,
    LeakageError,
    SealedScoring,
    committed_split,
    key_quarantine_reason,
    path_quarantine_reason,
)

__all__ = [
    "Access",
    "FitView",
    "FrameStore",
    "HeldOut",
    "HeldOutSplit",
    "IssueTimeView",
    "LeakageError",
    "SealedScoring",
    "committed_split",
    "frame_file_verdict",
]


def _aware(moment: datetime, what: str) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware")
    return moment


def frame_file_verdict(
    path: str | Path,
    *,
    data_root: Path | None = None,
    design: str | None = None,
) -> dict[str, Any]:
    """What the committed split lets a frame file be used for, from metadata only.

    Reads the frame's sidecar and key, never its pixels, and nothing under the quarantine
    folder. ``development`` is True when baselines and labels may use the frame before the
    sealed scoring. ``fit`` is True when a fitted asset may also use it (before the fit
    bound of ``design``, or the strictest bound). The check fails closed: a file that does
    not exist, or cannot be placed (not under ``frames/`` or ``archive/``, or an unknown
    viewpoint), is refused.
    """
    chosen = committed_split()
    _check_design(design)
    file_path = Path(path).expanduser().resolve()
    verdict: dict[str, Any] = {
        "path": str(file_path),
        "viewpoint_id": None,
        "image_time_utc": None,
        "archive": None,
        "development": False,
        "fit": False,
        "reasons": [],
        "scoring_exclusion": None,
    }
    quarantined = path_quarantine_reason(file_path, roots=(data_root,))
    if quarantined is not None:
        verdict["reasons"].append(quarantined)
        return verdict
    if not file_path.is_file():
        verdict["reasons"].append("does not exist (or is not a file)")
        return verdict
    try:
        record = record_from_path(file_path, data_root=data_root, hash_missing=False)
        vp_split = chosen.viewpoint(record.viewpoint_id)
    except (ValueError, LeakageError) as exc:
        verdict["reasons"].append(f"cannot be placed: {exc}")
        return verdict
    verdict.update(
        viewpoint_id=record.viewpoint_id,
        image_time_utc=isoformat_z(record.image_time),
        archive=record.is_archive,
        scoring_exclusion=chosen.scoring_exclusion_reason(record.viewpoint_id, record.image_time),
    )
    held = chosen.held_out_reason(record)
    if held is not None:
        verdict["reasons"].append(f"held out: {held}")
        return verdict
    verdict["development"] = True
    try:
        bound = vp_split.fit_bound(design)
    except LeakageError as exc:
        verdict["reasons"].append(f"not for fits: {exc}")
        return verdict
    if bound is not None and record.image_time >= bound:
        verdict["reasons"].append(
            f"not for fits: at or after the fit bound {isoformat_z(bound)} (PRD §13.9)"
        )
    else:
        verdict["fit"] = True
    return verdict


def _check_design(design: str | None) -> str | None:
    if design is not None and design not in DESIGNS:
        raise ValueError(f"design must be one of {DESIGNS} or None, got {design!r}")
    return design


@dataclass(frozen=True, slots=True)
class Access:
    """One logged access. ``issue_time`` is the view's issue time (or a fit's ``until``).

    ``held_out`` is True when only the sealed scoring may read the frame, and
    ``held_out_reason`` says why (a held-out period, an extra period or the archive guard).
    """

    key: str
    viewpoint_id: str
    image_time: datetime
    available_at: datetime
    purpose: str
    what: str  # "record", "label" or "bytes"
    issue_time: datetime | None
    held_out: bool
    held_out_reason: str | None = None

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
    ``reader`` returns a frame's bytes; for frames on disk use
    :func:`.frames.storage_reader`, which refuses the quarantine at every read.

    The split is always the committed one (:func:`.heldout.committed_split`, read-only as
    :attr:`split`); there is no parameter for another. ``held_out`` adds extra periods at
    every viewpoint, on top of the split. It can only add.
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
        self._split = committed_split()
        self.extra_held_out = held_out if held_out is not None else HeldOut()
        self.sealed = sealed
        self._by_vp: dict[str, list[FrameRecord]] = {}
        self._by_key: dict[str, FrameRecord] = {}
        self._held_reasons: dict[str, str | None] = {}
        for record in records:
            if record.key in self._by_key:
                raise ValueError(f"duplicate frame key {record.key}")
            quarantined = key_quarantine_reason(record.key, dir_name=self.split.quarantine_dir)
            if quarantined is not None:
                raise LeakageError(f"frame {record.key} is {quarantined}")
            self.split.viewpoint(record.viewpoint_id)  # an unknown viewpoint fails closed
            self._by_key[record.key] = record
            self._by_vp.setdefault(record.viewpoint_id, []).append(record)
            self._held_reasons[record.key] = self.split.held_out_reason(
                record, extra=self.extra_held_out
            )
        self._times: dict[str, list[datetime]] = {}
        for vp, items in self._by_vp.items():
            items.sort(key=lambda r: (r.image_time, r.key))
            self._times[vp] = [r.image_time for r in items]
        self._labels: dict[str, dict[str, Any]] = {}
        for key, value in (labels or {}).items():
            record = self._by_key.get(key)
            if record is None:
                raise ValueError(f"labels for {key!r}, which is not a frame of this store")
            reason = self._held_reasons[key]
            if sealed is None and reason is not None:
                raise LeakageError(
                    f"held-out frame {key} has labels ({reason}); held-out frames are absent "
                    "from the labelling store until the sealed scoring (PRD §13.1; SealedScoring)"
                )
            self._labels[key] = dict(value)
        self._reader = reader
        self.record_accesses = record_accesses
        self._log: list[Access] = []
        self._bounds: dict[tuple[str, str | None], datetime | None] = {}

    @property
    def split(self) -> HeldOutSplit:
        """The committed held-out split this store applies (read-only)."""
        return self._split

    @property
    def accesses(self) -> tuple[Access, ...]:
        """Logged accesses (empty unless ``record_accesses``)."""
        return tuple(self._log)

    def clear_log(self) -> None:
        """Forget logged accesses."""
        self._log.clear()

    def viewpoints(self) -> tuple[str, ...]:
        """Viewpoints with at least one frame."""
        return tuple(sorted(self._by_vp))

    def held_out_reason(self, record: FrameRecord) -> str | None:
        """Why only the sealed scoring may read ``record``, or None (rule 2)."""
        if self._by_key.get(record.key) is record:
            return self._held_reasons[record.key]
        return self.split.held_out_reason(record, extra=self.extra_held_out)

    def held_out_for(self, viewpoint_id: str) -> HeldOut:
        """A viewpoint's held-out periods: the split's, plus any extra ones."""
        return self.split.held_out(viewpoint_id) | self.extra_held_out

    def is_held_out(self, viewpoint_id: str, moment: datetime) -> bool:
        """True if ``moment`` is in a held-out period at ``viewpoint_id`` (split or extra)."""
        return self.split.period_reason(
            viewpoint_id, moment
        ) is not None or self.extra_held_out.contains(moment)

    def fit_bound(self, viewpoint_id: str, design: str | None = None) -> datetime | None:
        """Fits at ``viewpoint_id`` see only frames imaged before this moment (rule 2)."""
        key = (viewpoint_id, _check_design(design))
        if key not in self._bounds:
            self._bounds[key] = self.split.fit_bound(
                viewpoint_id, design, extra=self.extra_held_out
            )
        return self._bounds[key]

    def as_of(
        self, issue_time: datetime, *, purpose: str, allow_held_out: bool = False
    ) -> IssueTimeView:
        """A view for a prediction issued at ``issue_time`` (rule 1, and rule 2 by default)."""
        if allow_held_out and self.sealed is None:
            raise LeakageError(
                "allow_held_out needs a FrameStore built for the sealed scoring (SealedScoring)"
            )
        return IssueTimeView(self, _aware(issue_time, "issue_time"), purpose, allow_held_out)

    def for_fit(
        self, *, purpose: str, until: datetime | None = None, design: str | None = None
    ) -> FitView:
        """A view for fitting an asset: development frames before the fit bound.

        With ``until``, only frames imaged and available by then. With ``design``, the fit
        is for that design only and its bound is that design's period (rule 2).
        """
        return FitView(
            self, purpose, None if until is None else _aware(until, "until"), _check_design(design)
        )

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
            reason = self.held_out_reason(record)
            self._log.append(
                Access(
                    key=record.key,
                    viewpoint_id=record.viewpoint_id,
                    image_time=record.image_time,
                    available_at=record.available_at,
                    purpose=purpose,
                    what=what,
                    issue_time=issue_time,
                    held_out=reason is not None,
                    held_out_reason=reason,
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
        if not self.allow_held_out:
            reason = self._store.held_out_reason(record)
            if reason is not None:
                return f"frame {record.key} is held out: {reason} [{self.purpose}]"
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
    """What a fitted asset may see (rules 2 and 3 of the module docstring).

    No :class:`SealedScoring` opens this view to held-out frames: fits come from
    development frames only (PRD §13.9).

    TODO(M2): PRD §13.9 also keeps development data within one camera epoch. D-024 gives
    each design-H camera a usable span (for example Point Reyes from Jul 2023, after its
    2021–22 3:2 epoch; Grand Canyon from Jul 2024). The split holds these only as notes, so
    this view does not filter by them yet, and a fit can mix epochs. That is not leakage, but
    a fitted asset could be built on the wrong view. The fix is a per-viewpoint
    ``development_from`` in a new version of the split (the one after
    :data:`.heldout.CURRENT_SPLIT_VERSION`: version 2 is taken by a text-only correction; with a
    DECISIONS.md entry), applied here and in the pair's fitted-asset check.
    """

    def __init__(
        self, store: FrameStore, purpose: str, until: datetime | None, design: str | None = None
    ) -> None:
        self._store = store
        self.purpose = purpose
        self.until = until
        self.design = _check_design(design)

    def _refusal(self, record: FrameRecord) -> str | None:
        reason = self._store.held_out_reason(record)
        if reason is not None:
            return f"fit '{self.purpose}' may not read held-out frame {record.key}: {reason}"
        try:
            bound = self._store.fit_bound(record.viewpoint_id, self.design)
        except LeakageError as exc:  # the viewpoint has no such design
            return f"fit '{self.purpose}' (design {self.design}): {exc}"
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
        """Development frames the fit may use, oldest first, optionally with a usable label.

        A fit for a design the viewpoint is not evaluated under raises :class:`LeakageError`.
        """
        if self.design is not None:
            self._store.split.viewpoint(viewpoint_id).period(self.design)
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
