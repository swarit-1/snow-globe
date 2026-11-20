"""The committed held-out split and its guards (D-019, D-022, D-024; PRD §13.2, §13.9).

PRD §13.2: "the split is committed before any held-out frame is touched". The split is
``heldout_periods.json`` beside this module. It is committed and versioned: its ``version``
and the file's SHA-256 go into every record that depends on it (:meth:`HeldOutSplit.to_record`).
:func:`committed_split` loads it with strict checks. Per viewpoint it gives:

- **Held-out periods**, as whole local days in the viewpoint's time zone:
  - design P's sealed window, 15 Dec 2026 – 14 Jan 2027, extended by any outage (Rainier,
    and Point Reyes on the same window);
  - design H's season, summer 2026 (1 Jun – 31 Aug 2026): Point Reyes (for K2), Grand
    Canyon, Yosemite, Acadia, the Smokies and Big Bend;
  - none at Grand Teton, which has no design (D-024): excluded from scored metrics,
    development use only.
- **Scoring exclusions:** 15 Jul 2026 at the 7 ARD cameras (D-022).
- **The archive guard** (D-019): archived NPS frames dated on or after 1 Jun 2026 are refused
  for development at every viewpoint. A frame is refused if *any* reading of its date, local
  or UTC, is on or after that day (the lead's reading of 20 Nov 2026, pending the owner's
  D-038): the dates of ``ImageCreateDateTime`` and ``LocalTimeString``, the civil date of its
  UTC time, ``ImageCreateDateTime`` read as local standard time (NPS's summer convention), the
  UTC date of each of those and of the instant itself, and the record's own UTC time, local and
  UTC (:func:`nps_dates`). This is the rule of ``workers/capture/src/lookout_capture/heldout.py``
  (which has read the standard-time date too since D-047), reproduced here (eval does not
  import capture) and kept in parity with it by tests on both sides (:func:`nps_local_dates`,
  :func:`nps_utc_dates`, :func:`archive_guard_reason`).
- **The quarantine** (D-022): anything under ``$LOOKOUT_DATA_DIR/quarantine/`` is refused on
  every path (:func:`key_quarantine_reason`, :func:`path_quarantine_reason`,
  :class:`QuarantineGuard`). The folder is matched in any letter case and by identity
  (device and inode), since this Mac's disk is case-insensitive.

Only the sealed scoring may read held-out frames. It holds a :class:`SealedScoring`, which
names the 40-character commit of the frozen pre-registration (PRD §13.4). Fits never read
held-out frames, sealed or not (§13.9). :mod:`.leakage` and :mod:`.backtest_pair` apply all
of this on every access.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import cache
from importlib import resources
from pathlib import Path, PurePath, PurePosixPath
from types import MappingProxyType
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
from lookout_common.paths import DEFAULT_DATA_DIR, ENV_DATA_DIR, validate_viewpoint_id
from lookout_common.timeutil import isoformat_z, parse_nps_archive_time
from numpy.typing import NDArray

__all__ = [
    "DESIGNS",
    "HELDOUT_PERIODS_RESOURCE",
    "ArchiveGuard",
    "DayRange",
    "FrameLike",
    "HeldOut",
    "HeldOutPeriod",
    "HeldOutSplit",
    "LeakageError",
    "QuarantineGuard",
    "ScoringExclusion",
    "SealedScoring",
    "ViewpointSplit",
    "archive_guard_reason",
    "committed_split",
    "key_quarantine_reason",
    "load_split",
    "nps_dates",
    "nps_local_dates",
    "nps_utc_dates",
    "path_quarantine_reason",
    "split_from_dict",
]

#: The committed split, a package resource beside this module.
HELDOUT_PERIODS_RESOURCE = "heldout_periods.json"
KIND = "lookout.heldout-periods"
SCHEMA_VERSION = 1
#: Evaluation designs (PRD §13.2): H, hindcast; P, sealed prospective.
DESIGNS: tuple[str, ...] = ("P", "H")

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")


class LeakageError(RuntimeError):
    """A baseline, a fit or a pair tried to read a frame it must not see."""


def _aware(moment: datetime, what: str) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware")
    return moment


@dataclass(frozen=True)
class HeldOut:
    """Held-out periods as half-open intervals ``[start, end)`` of aware datetimes."""

    intervals: tuple[tuple[datetime, datetime], ...] = ()

    def __post_init__(self) -> None:
        for start, end in self.intervals:
            _aware(start, "held-out start")
            _aware(end, "held-out end")
            if not start < end:
                raise ValueError("held-out interval must have start < end")

    @classmethod
    def from_local_dates(cls, first_day: date, last_day: date, time_zone: str) -> HeldOut:
        """Whole local days ``first_day`` .. ``last_day`` inclusive, in ``time_zone``."""
        if last_day < first_day:
            raise ValueError("last_day is before first_day")
        zone = ZoneInfo(time_zone)
        start = datetime.combine(first_day, time(0), tzinfo=zone)
        end = datetime.combine(last_day + timedelta(days=1), time(0), tzinfo=zone)
        return cls(((start, end),))

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
    labelled or viewed. TODO(M4): check it against the hash DECISIONS.md records.
    """

    preregistration_commit: str

    def __post_init__(self) -> None:
        if not _COMMIT_RE.fullmatch(self.preregistration_commit):
            raise ValueError("preregistration_commit must be a full 40-character git commit")


class FrameLike(Protocol):
    """What the rules read from a frame record (:class:`.frames.FrameRecord` has it all)."""

    @property
    def viewpoint_id(self) -> str: ...

    @property
    def image_time(self) -> datetime: ...

    @property
    def key(self) -> str: ...

    @property
    def is_archive(self) -> bool: ...

    @property
    def nps_image_create_datetime(self) -> str | None: ...

    @property
    def nps_local_time_string(self) -> str | None: ...


# --- the archive guard's date rule (the capture rule, reproduced) -----------------------------


def _us_date(text: str) -> date | None:
    head = text.strip().split(" ", 1)[0]
    try:
        return datetime.strptime(head, "%m/%d/%Y").date()
    except ValueError:
        return None


# ImageCreateDateTime as NPS writes it, e.g. "05/31/2026 11:30:00 PM" (lookout-common's form).
_NPS_CLOCK_RE = re.compile(
    r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([ap]m)\s*$",
    re.IGNORECASE,
)
# The head of LocalTimeString, e.g. "5/31/2026 11:30 PM" in "5/31/2026 11:30 PM Pacific ...".
_LABEL_CLOCK_RE = re.compile(
    r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([ap]m)(?:\s|$)",
    re.IGNORECASE,
)


def _wall(match: re.Match[str] | None) -> datetime | None:
    """The naive wall-clock time of a matched NPS clock, or None if it is not a real time."""
    if match is None:
        return None
    month, day, year, hour, minute, second, meridiem = match.groups()
    if not 1 <= int(hour) <= 12:
        return None
    hour24 = int(hour) % 12 + (12 if meridiem.lower() == "pm" else 0)
    try:
        return datetime(int(year), int(month), int(day), hour24, int(minute), int(second or 0))
    except ValueError:
        return None


def _zone(tz_name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(tz_name)
    except (ValueError, KeyError):  # ZoneInfoNotFoundError is a KeyError
        return None


def _standard_offset(wall: datetime, zone: ZoneInfo) -> timedelta | None:
    probe = wall.replace(tzinfo=zone)
    offset, daylight = probe.utcoffset(), probe.dst()
    if offset is None:
        return None
    return offset - (daylight or timedelta(0))


def _standard_time_date(image_create_datetime: str, tz_name: str) -> date | None:
    """The civil date of ``ImageCreateDateTime`` read as local *standard* time, or None.

    NPS keeps ``ImageCreateDateTime`` on standard time all year (lookout-common's
    ``parse_nps_archive_time``: every sampled July day of the archive check). So in summer
    civil time is one hour later: "05/31/2026 11:30:00 PM" is 00:30 PDT on 1 Jun. Where the
    zone keeps no daylight time (Arizona), or in winter, this is the stamp's own date.
    """
    wall = _wall(_NPS_CLOCK_RE.match(image_create_datetime or ""))
    zone = _zone(tz_name)
    if wall is None or zone is None:
        return None
    standard = _standard_offset(wall, zone)
    if standard is None:
        return None
    return (wall - standard).replace(tzinfo=UTC).astimezone(zone).date()


def nps_local_dates(image_create_datetime: str, local_time_string: str, tz_name: str) -> list[date]:
    """Every local date an NPS archive asset could be said to fall on (empty if none parses).

    Lookout-capture's ``asset_local_dates``, reproduced (D-019, D-022, D-038, D-047). In summer
    ``ImageCreateDateTime`` stays on local standard time while ``LocalTimeString`` gives civil
    time, so near midnight the two can name different days. Read here:

    - the date of ``ImageCreateDateTime`` and the date of ``LocalTimeString``;
    - the civil date of the UTC time (lookout-common's ``parse_nps_archive_time``) in
      ``tz_name``;
    - the civil date of ``ImageCreateDateTime`` read as local standard time
      (:func:`_standard_time_date`). Capture has read it too since D-047. Without it, a summer
      frame stamped "05/31/2026 11:30:00 PM" whose ``LocalTimeString`` is missing would date
      only to 31 May, though its civil time is 00:30 on 1 Jun.

    The guard also reads UTC dates (:func:`nps_utc_dates`).
    """
    found: list[date] = []
    for text in (image_create_datetime, local_time_string):
        if text:
            parsed = _us_date(text)
            if parsed is not None:
                found.append(parsed)
    try:
        utc = parse_nps_archive_time(image_create_datetime, local_time_string, tz_name)
        found.append(utc.astimezone(ZoneInfo(tz_name)).date())
    except (ValueError, KeyError):
        pass
    standard = _standard_time_date(image_create_datetime, tz_name)
    if standard is not None:
        found.append(standard)
    return sorted(set(found))


def _civil_utc_dates(wall: datetime, zone: ZoneInfo) -> set[date]:
    """UTC dates of a local wall time read as civil time in ``zone``: both sides of a fold
    or a gap, since NPS does not say which one it means."""
    return {wall.replace(tzinfo=zone, fold=fold).astimezone(UTC).date() for fold in (0, 1)}


def _day_utc_dates(day: date, zone: ZoneInfo) -> set[date]:
    """UTC dates of the first and the last instant of a local day: a date read without a
    clock, so the guard takes every UTC date the day touches (it fails closed)."""
    first = datetime.combine(day, time(0), tzinfo=zone)
    last = datetime.combine(day + timedelta(days=1), time(0), tzinfo=zone) - timedelta(
        microseconds=1
    )
    return {first.astimezone(UTC).date(), last.astimezone(UTC).date()}


def nps_utc_dates(image_create_datetime: str, local_time_string: str, tz_name: str) -> list[date]:
    """Every UTC date an NPS archive asset could be said to fall on (the lead, 20 Nov 2026).

    Lookout-capture's ``asset_utc_dates``, reproduced:

    - the UTC date of the asset's instant, under both of lookout-common's conventions for
      ``parse_nps_archive_time`` (``"label"`` and ``"wall"``);
    - each local reading converted in the camera's zone: ``ImageCreateDateTime`` read as civil
      time (both sides of a fold or gap) and as local standard time, and ``LocalTimeString``'s
      clock read as civil time (both sides);
    - a reading that gives a date but no readable clock: every UTC date its local day touches.
    """
    zone = _zone(tz_name)
    if zone is None:
        return []
    found: set[date] = set()
    for convention in ("label", "wall"):
        try:
            instant = parse_nps_archive_time(
                image_create_datetime, local_time_string, tz_name, convention=convention
            )
        except (ValueError, KeyError):
            continue
        found.add(instant.astimezone(UTC).date())
    for text, pattern in (
        (image_create_datetime, _NPS_CLOCK_RE),
        (local_time_string, _LABEL_CLOCK_RE),
    ):
        if not text:
            continue
        wall = _wall(pattern.match(text))
        if wall is not None:
            found |= _civil_utc_dates(wall, zone)
            continue
        day = _us_date(text)
        if day is not None:
            found |= _day_utc_dates(day, zone)
    wall = _wall(_NPS_CLOCK_RE.match(image_create_datetime or ""))
    if wall is not None:
        standard = _standard_offset(wall, zone)
        if standard is not None:
            found.add((wall - standard).date())
    return sorted(found)


def nps_dates(
    image_create_datetime: str,
    local_time_string: str,
    tz_name: str,
    *,
    utc_time: datetime | None = None,
) -> list[date]:
    """Every date, local or UTC, an NPS archive asset could be said to fall on (the guard's set).

    :func:`nps_local_dates` and :func:`nps_utc_dates`, plus, when ``utc_time`` is given (the
    record's own image time: the instant behind its archive key), its civil date in
    ``tz_name`` and its UTC date. Lookout-capture's ``asset_dates`` (its ``instant``).
    """
    found = set(nps_local_dates(image_create_datetime, local_time_string, tz_name))
    found |= set(nps_utc_dates(image_create_datetime, local_time_string, tz_name))
    if utc_time is not None:
        aware = _aware(utc_time, "utc_time")
        zone = _zone(tz_name)
        if zone is not None:
            found.add(aware.astimezone(zone).date())
        found.add(aware.astimezone(UTC).date())
    return sorted(found)


def archive_guard_reason(
    image_create_datetime: str,
    local_time_string: str,
    tz_name: str,
    *,
    guard: date | None = None,
    utc_time: datetime | None = None,
) -> str | None:
    """Why an archived frame may not be read for development, or None if it may (D-019).

    The rule of lookout-capture's ``held_out_reason``: refused when any of its dates, local or
    UTC (:func:`nps_dates`), is on or after ``guard`` (default: the committed split's
    ``archive_guard.from_local_day``, 1 Jun 2026), and when no date can be read at all (the
    guard fails closed). ``utc_time``, when given, is the frame's own UTC image time (eval's
    records carry one); its local and UTC dates count too. NPS strings that are present but
    yield no local date are refused even then: they mean the metadata is broken.
    """
    limit = guard if guard is not None else committed_split().archive_guard.from_day
    local = nps_local_dates(image_create_datetime, local_time_string, tz_name)
    raw_given = bool((image_create_datetime or "").strip() or (local_time_string or "").strip())
    if raw_given and not local:
        return (
            f"archive guard (D-019): no readable date in {image_create_datetime!r} / "
            f"{local_time_string!r}"
        )
    dates = nps_dates(image_create_datetime, local_time_string, tz_name, utc_time=utc_time)
    if not dates:
        return f"archive guard (D-019): no readable date in {image_create_datetime!r}"
    latest = dates[-1]
    if latest >= limit:
        zone = _zone(tz_name)
        civil = {utc_time.astimezone(zone).date()} if utc_time is not None and zone else set()
        which = "local" if latest in {*local, *civil} else "UTC"
        return (
            f"archive guard (D-019): {which} date {latest.isoformat()} is on or after "
            f"{limit.isoformat()}"
        )
    return None


# --- the quarantine ---------------------------------------------------------------------------


def _has_component(path: str | PurePath, name: str) -> bool:
    """True when a ``/``-separated component of ``path`` is ``name`` in any letter case."""
    return f"/{name.casefold()}/" in f"/{str(path).casefold()}/"


def key_quarantine_reason(key: str, *, dir_name: str | None = None) -> str | None:
    """Why a storage key is quarantined, or None (D-022).

    Refused: a key with a ``quarantine`` component in any letter case (``Quarantine/...``
    opens the same folder on a case-insensitive disk, as on this Mac).
    """
    name = dir_name or committed_split().quarantine_dir
    if _has_component(PurePosixPath(key), name):
        return f"under the {name}/ folder (D-022): refused on every path"
    return None


def _data_roots(extra: Iterable[str | os.PathLike[str] | None]) -> list[Path]:
    roots = [Path(r).expanduser() for r in extra if r is not None]
    configured = os.environ.get(ENV_DATA_DIR, "").strip()
    roots.append(Path(configured or DEFAULT_DATA_DIR).expanduser())
    roots.append(Path(DEFAULT_DATA_DIR).expanduser())
    return roots


def _identity(path: str | os.PathLike[str]) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of an existing path, symlinks followed; None if there is none."""
    try:
        info = os.stat(path)
    except (OSError, ValueError):
        return None
    return info.st_dev, info.st_ino


def _ancestors(path: str) -> Iterable[str]:
    """``path`` and each folder above it, up to ``/`` (absolute, normalised paths)."""
    while True:
        yield path
        parent = os.path.dirname(path)
        if parent == path:
            return
        path = parent


class QuarantineGuard:
    """The quarantine folders (D-022), found once, for checking one path or many.

    A path is refused when any of these holds:

    - before or after resolving symlinks, one of its components is the folder's name in
      any letter case;
    - it resolves under ``<root>/quarantine`` for any of ``roots``, ``$LOOKOUT_DATA_DIR``
      and ``~/lookout-data``;
    - it, or one of its existing ancestors, *is* such a folder by identity (device and
      inode).

    Identity catches what spelling cannot. This Mac's APFS is case-insensitive and
    ``Path.resolve()`` keeps the case it is given, so a case variant of a quarantine folder
    that is itself a symlink opens the same files under another name. Checking reads
    metadata only (``stat``); nothing under the folder is opened or listed.

    ``cache_dirs=True`` (for one catalogue scan) remembers each directory's resolution and
    verdict, so many files in one folder cost one ``lstat`` each. A file that is a symlink
    is always resolved on its own. Paths are compared as normalised strings, which is what
    ``Path.resolve()`` (``os.path.realpath``) returns.
    """

    def __init__(
        self,
        roots: Iterable[str | os.PathLike[str] | None] = (),
        *,
        dir_name: str | None = None,
        cache_dirs: bool = False,
    ) -> None:
        self.name = dir_name or committed_split().quarantine_dir
        folders: list[Path] = []
        identities: set[tuple[int, int]] = set()
        for root in _data_roots(roots):
            folder = root / self.name
            resolved = folder.resolve()
            if resolved not in folders:
                folders.append(resolved)
            found = _identity(folder)
            if found is not None:
                identities.add(found)
        self.folders: tuple[Path, ...] = tuple(folders)
        self._prefixes = tuple((str(f), str(f).rstrip("/") + "/") for f in folders)
        self._identities = frozenset(identities)
        self._cache_dirs = cache_dirs
        self._resolved_dirs: dict[str, str] = {}
        self._dirs_inside: dict[str, bool] = {}

    def _resolve(self, absolute: str) -> str:
        if not self._cache_dirs:
            return os.path.realpath(absolute)
        head, tail = os.path.split(absolute)
        if tail in ("", ".", "..") or os.path.islink(absolute):  # a linked file: on its own
            return os.path.realpath(absolute)
        resolved_head = self._resolved_dirs.get(head)
        if resolved_head is None:
            resolved_head = self._resolved_dirs[head] = os.path.realpath(head)
        return os.path.join(resolved_head, tail)

    def _inside_by_identity(self, directory: str) -> bool:
        known = self._dirs_inside.get(directory) if self._cache_dirs else None
        if known is None:
            known = any(_identity(c) in self._identities for c in _ancestors(directory))
            if self._cache_dirs:
                self._dirs_inside[directory] = known
        return known

    def reason(self, path: str | os.PathLike[str]) -> str | None:
        """Why ``path`` is quarantined, or None (see the class docstring)."""
        given = os.path.expanduser(os.fspath(path))
        absolute = given if os.path.isabs(given) else os.path.join(os.getcwd(), given)
        resolved = self._resolve(absolute)
        for form in (absolute, resolved):
            if _has_component(form, self.name):
                return f"{form} is under a {self.name}/ folder (D-022): refused on every path"
        for folder, prefix in self._prefixes:
            if resolved == folder or resolved.startswith(prefix):
                return f"{resolved} is under {folder} (D-022): refused on every path"
        if self._identities and (
            _identity(resolved) in self._identities
            or self._inside_by_identity(os.path.dirname(resolved))
        ):
            return (
                f"{resolved} is inside a {self.name}/ folder, found by device and inode "
                "(D-022): refused on every path"
            )
        return None


def path_quarantine_reason(
    path: str | os.PathLike[str],
    *,
    roots: Iterable[str | os.PathLike[str] | None] = (),
    dir_name: str | None = None,
) -> str | None:
    """Why a file path is quarantined, or None (D-022; :class:`QuarantineGuard`'s rules).

    Refused: a path with a ``quarantine`` component in any letter case, before or after
    resolving symlinks; a path that resolves under ``<root>/quarantine`` for any of
    ``roots``, ``$LOOKOUT_DATA_DIR`` and ``~/lookout-data``; and a path inside such a folder
    by identity (device and inode). Nothing under the folder is opened.
    """
    return QuarantineGuard(roots, dir_name=dir_name).reason(path)


# --- the split --------------------------------------------------------------------------------


@dataclass(frozen=True)
class DayRange:
    """Whole local days ``first`` .. ``last``, inclusive (with a reason for an extension)."""

    first: date
    last: date
    reason: str = ""

    def __post_init__(self) -> None:
        if self.last < self.first:
            raise ValueError(f"day range {self.first} .. {self.last} ends before it starts")

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        out: dict[str, Any] = {
            "first_day": self.first.isoformat(),
            "last_day": self.last.isoformat(),
        }
        if self.reason:
            out["reason"] = self.reason
        return out


@dataclass(frozen=True)
class HeldOutPeriod:
    """One held-out period: its design, its window of local days and any extensions.

    Extensions are contiguous: each starts the day after the previous end and names its
    reason (PRD §13.2: the sealed window is "extended by any outage").
    """

    name: str
    design: str
    window: DayRange
    extensions: tuple[DayRange, ...] = ()
    source: str = ""

    def __post_init__(self) -> None:
        if self.design not in DESIGNS:
            raise ValueError(f"period {self.name}: design must be one of {DESIGNS}")
        previous = self.window.last
        for extension in self.extensions:
            if extension.first != previous + timedelta(days=1):
                raise ValueError(
                    f"period {self.name}: an extension must start the day after "
                    f"{previous.isoformat()}, not on {extension.first.isoformat()}"
                )
            if not extension.reason.strip():
                raise ValueError(f"period {self.name}: an extension needs a reason")
            previous = extension.last

    @property
    def first_day(self) -> date:
        """The first held-out local day."""
        return self.window.first

    @property
    def last_day(self) -> date:
        """The last held-out local day, extensions included."""
        return self.extensions[-1].last if self.extensions else self.window.last

    def contains_day(self, day: date) -> bool:
        """True when local day ``day`` is held out by this period."""
        return self.first_day <= day <= self.last_day

    def held_out(self, time_zone: str) -> HeldOut:
        """This period as an interval of local days in ``time_zone``."""
        return HeldOut.from_local_dates(self.first_day, self.last_day, time_zone)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {
            "name": self.name,
            "design": self.design,
            **self.window.to_dict(),
            "extensions": [e.to_dict() for e in self.extensions],
            "last_day_with_extensions": self.last_day.isoformat(),
            "source": self.source,
        }


@dataclass(frozen=True)
class ViewpointSplit:
    """One viewpoint's part of the split: its designs' periods, and whether it is scored."""

    viewpoint_id: str
    time_zone: str
    periods: tuple[HeldOutPeriod, ...]
    scored: bool
    note: str = ""

    @property
    def designs(self) -> tuple[str, ...]:
        """The designs this viewpoint is evaluated under (empty for Grand Teton)."""
        return tuple(p.design for p in self.periods)

    def period(self, design: str) -> HeldOutPeriod:
        """The held-out period of ``design``; :class:`LeakageError` if there is none."""
        for candidate in self.periods:
            if candidate.design == design:
                return candidate
        raise LeakageError(
            f"{self.viewpoint_id} has no design {design} in the committed split "
            f"(designs: {', '.join(self.designs) or 'none'})"
        )

    def local_day(self, moment: datetime) -> date:
        """The civil date of ``moment`` in this viewpoint's zone."""
        return _aware(moment, "moment").astimezone(ZoneInfo(self.time_zone)).date()

    def period_at(self, moment: datetime) -> HeldOutPeriod | None:
        """The held-out period ``moment`` falls in, if any."""
        day = self.local_day(moment)
        return next((p for p in self.periods if p.contains_day(day)), None)

    def held_out(self, design: str | None = None) -> HeldOut:
        """Every held-out period as intervals, or only ``design``'s."""
        chosen = self.periods if design is None else (self.period(design),)
        out = HeldOut()
        for period in chosen:
            out = out | period.held_out(self.time_zone)
        return out

    def fit_bound(self, design: str | None = None) -> datetime | None:
        """Fits see only frames imaged before this moment (PRD §13.9).

        Without a design it is the start of the earliest held-out period, the strictest bound.
        With one, it is the start of that design's period, so design P at Point Reyes may fit
        on development frames from after the summer-2026 season. Either way a fit never reads
        a frame inside any held-out period.
        """
        if design is None:
            return self.held_out().earliest_start
        return self.period(design).held_out(self.time_zone).earliest_start


@dataclass(frozen=True)
class ScoringExclusion:
    """A local day excluded from scoring at some viewpoints (D-022: 15 Jul 2026)."""

    day: date
    viewpoints: frozenset[str]
    reason: str
    source: str


@dataclass(frozen=True)
class ArchiveGuard:
    """D-019's guard: archived frames dated on or after ``from_day`` wait for the sealed scoring."""

    from_day: date
    source: str = ""

    def reason(
        self,
        image_create_datetime: str,
        local_time_string: str,
        tz_name: str,
        *,
        utc_time: datetime | None = None,
    ) -> str | None:
        """:func:`archive_guard_reason` with this guard's date."""
        return archive_guard_reason(
            image_create_datetime,
            local_time_string,
            tz_name,
            guard=self.from_day,
            utc_time=utc_time,
        )


@dataclass(frozen=True)
class HeldOutSplit:
    """The committed held-out split (see the module docstring), with its rules."""

    version: int
    sha256: str
    source: str
    decided: date
    freeze: date
    decisions: tuple[str, ...]
    periods: Mapping[str, HeldOutPeriod]
    viewpoints: Mapping[str, ViewpointSplit]
    scoring_exclusions: tuple[ScoringExclusion, ...]
    archive_guard: ArchiveGuard
    quarantine_dir: str

    def viewpoint(self, viewpoint_id: str) -> ViewpointSplit:
        """A viewpoint's split; :class:`LeakageError` for a viewpoint the split does not name.

        Fails closed: PRD §13.2 commits the split before any of a viewpoint's frames is touched.
        """
        try:
            return self.viewpoints[viewpoint_id]
        except KeyError:
            raise LeakageError(
                f"viewpoint {viewpoint_id!r} has no committed held-out split "
                f"({self.source} v{self.version}); PRD §13.2 commits the split before any "
                "of its frames is touched"
            ) from None

    def held_out(self, viewpoint_id: str, design: str | None = None) -> HeldOut:
        """A viewpoint's held-out periods (all of them, or one design's) as intervals."""
        return self.viewpoint(viewpoint_id).held_out(design)

    def period_reason(self, viewpoint_id: str, moment: datetime) -> str | None:
        """Why ``moment`` is held out at a viewpoint (its period), or None."""
        split = self.viewpoint(viewpoint_id)
        period = split.period_at(moment)
        if period is None:
            return None
        return (
            f"in the held-out period {period.name} (design {period.design}: "
            f"{period.first_day.isoformat()} to {period.last_day.isoformat()}, local days in "
            f"{split.time_zone}; {period.source})"
        )

    def archive_reason(self, record: FrameLike) -> str | None:
        """Why an archived frame is past the archive guard, or None (always None when live)."""
        if not record.is_archive:
            return None
        split = self.viewpoint(record.viewpoint_id)
        return self.archive_guard.reason(
            record.nps_image_create_datetime or "",
            record.nps_local_time_string or "",
            split.time_zone,
            utc_time=record.image_time,
        )

    def held_out_reason(self, record: FrameLike, *, extra: HeldOut | None = None) -> str | None:
        """Why only the sealed scoring may read this frame, or None if development may.

        A frame is held out when it is in one of its viewpoint's held-out periods, in an
        ``extra`` period the caller adds, or archived and past the archive guard.
        """
        reason = self.period_reason(record.viewpoint_id, record.image_time)
        if reason is not None:
            return reason
        if extra is not None and extra.contains(record.image_time):
            return "in an extra held-out period given by the caller"
        return self.archive_reason(record)

    def fit_bound(
        self, viewpoint_id: str, design: str | None = None, *, extra: HeldOut | None = None
    ) -> datetime | None:
        """The fit bound (:meth:`ViewpointSplit.fit_bound`), tightened by ``extra`` periods."""
        bounds = [
            bound
            for bound in (
                self.viewpoint(viewpoint_id).fit_bound(design),
                None if extra is None else extra.earliest_start,
            )
            if bound is not None
        ]
        return min(bounds, default=None)

    def scoring_exclusion_reason(self, viewpoint_id: str, moment: datetime) -> str | None:
        """Why a pair at ``moment`` must not count in scored metrics, or None.

        A viewpoint the split marks as not scored (Grand Teton, D-024) is always excluded;
        otherwise a listed local day is (15 Jul 2026 at the 7 ARD cameras, D-022).
        """
        split = self.viewpoint(viewpoint_id)
        if not split.scored:
            return f"{viewpoint_id} is excluded from scored metrics: {split.note}"
        day = split.local_day(moment)
        for exclusion in self.scoring_exclusions:
            if exclusion.day == day and viewpoint_id in exclusion.viewpoints:
                return (
                    f"{day.isoformat()} at {viewpoint_id} is excluded from scoring "
                    f"({exclusion.source}): {exclusion.reason}"
                )
        return None

    def scoring_excluded_mask(
        self, viewpoint_id: str, moments: Sequence[datetime]
    ) -> NDArray[np.bool_]:
        """The split's ``excluded`` mask for :func:`.scores.contingency` (OR it with the gates')."""
        return np.array(
            [self.scoring_exclusion_reason(viewpoint_id, m) is not None for m in moments],
            dtype=bool,
        )

    def to_record(self) -> dict[str, Any]:
        """What a record names to say which split it used."""
        return {
            "kind": KIND,
            "version": self.version,
            "sha256": self.sha256,
            "source": self.source,
            "decisions": list(self.decisions),
        }

    def summary(self) -> dict[str, Any]:
        """The split in full, with every held-out period also as UTC instants (for the CLI)."""
        viewpoints = {}
        for vp_id, split in self.viewpoints.items():
            bounds: dict[str, str | None] = {}
            for design in (None, *split.designs):
                bound = split.fit_bound(design)
                label = "strictest" if design is None else f"design {design}"
                bounds[label] = None if bound is None else isoformat_z(bound)
            viewpoints[vp_id] = {
                "time_zone": split.time_zone,
                "designs": {p.design: p.name for p in split.periods},
                "scored": split.scored,
                "held_out_utc": [
                    [isoformat_z(start), isoformat_z(end)]
                    for start, end in split.held_out().intervals
                ],
                "fit_bound_utc": bounds,
                "note": split.note,
            }
        return {
            **self.to_record(),
            "decided": self.decided.isoformat(),
            "freeze": self.freeze.isoformat(),
            "periods": {name: p.to_dict() for name, p in self.periods.items()},
            "viewpoints": viewpoints,
            "scoring_exclusions": [
                {
                    "local_day": e.day.isoformat(),
                    "viewpoints": sorted(e.viewpoints),
                    "reason": e.reason,
                    "source": e.source,
                }
                for e in self.scoring_exclusions
            ],
            "archive_guard": {
                "from_local_day": self.archive_guard.from_day.isoformat(),
                "viewpoints": "all",
                "source": self.archive_guard.source,
            },
            "quarantine_dir": self.quarantine_dir,
        }


# --- loading ----------------------------------------------------------------------------------


_ISO_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class _Fields:
    """Strict reading of one JSON object: required and optional keys, nothing else."""

    def __init__(
        self,
        data: Any,
        where: str,
        required: frozenset[str] | set[str],
        optional: frozenset[str] | set[str] = frozenset(),
    ) -> None:
        if not isinstance(data, Mapping):
            raise ValueError(f"{where} must be an object")
        missing = set(required) - data.keys()
        unknown = data.keys() - set(required) - set(optional)
        if missing:
            raise ValueError(f"{where} is missing {sorted(missing)}")
        if unknown:
            raise ValueError(f"{where} has unknown keys {sorted(unknown)}")
        self.data = data
        self.where = where

    def text(self, name: str, *, empty: bool = False) -> str:
        value = self.data.get(name, "")
        if not isinstance(value, str) or (not empty and not value.strip()):
            raise ValueError(f"{self.where}.{name} must be a non-empty string")
        return value

    def day(self, name: str) -> date:
        value = self.data.get(name)
        if isinstance(value, str) and _ISO_DAY_RE.fullmatch(value):
            try:
                return date.fromisoformat(value)
            except ValueError:
                pass
        raise ValueError(f"{self.where}.{name} must be a YYYY-MM-DD date, got {value!r}")


def _day_range(data: Any, where: str, *, with_reason: bool) -> DayRange:
    keys = {"first_day", "last_day"} | ({"reason"} if with_reason else set())
    fields = _Fields(data, where, keys)
    return DayRange(
        fields.day("first_day"),
        fields.day("last_day"),
        fields.text("reason") if with_reason else "",
    )


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r} in the held-out split")
        out[key] = value
    return out


def split_from_dict(data: Mapping[str, Any], *, sha256: str, source: str) -> HeldOutSplit:
    """Check a held-out split document strictly and build it (``ValueError`` on any problem)."""
    top = _Fields(
        data,
        source,
        {
            "kind",
            "schema_version",
            "version",
            "decided",
            "freeze",
            "decisions",
            "periods",
            "viewpoints",
            "scoring_exclusions",
            "archive_guard",
            "quarantine",
        },
        {"change_rule", "day_basis"},
    )
    if data["kind"] != KIND:
        raise ValueError(f"{source}: kind must be {KIND!r}")
    if data["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"{source}: schema_version must be {SCHEMA_VERSION}")
    version = data["version"]
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError(f"{source}: version must be a positive integer")
    decisions = data["decisions"]
    if not isinstance(decisions, list) or not all(
        isinstance(d, str) and re.fullmatch(r"D-\d{3}", d) for d in decisions
    ):
        raise ValueError(f"{source}: decisions must be a list like ['D-019']")

    if not isinstance(data["periods"], Mapping) or not data["periods"]:
        raise ValueError(f"{source}.periods must be a non-empty object")
    periods: dict[str, HeldOutPeriod] = {}
    for name, body in data["periods"].items():
        where = f"{source}.periods.{name}"
        fields = _Fields(
            body,
            where,
            {"design", "first_day", "last_day", "extensions", "source"},
            {"extension_rule"},
        )
        extensions = body["extensions"]
        if not isinstance(extensions, list):
            raise ValueError(f"{where}.extensions must be a list")
        periods[name] = HeldOutPeriod(
            name=name,
            design=fields.text("design"),
            window=DayRange(fields.day("first_day"), fields.day("last_day")),
            extensions=tuple(
                _day_range(e, f"{where}.extensions[{i}]", with_reason=True)
                for i, e in enumerate(extensions)
            ),
            source=fields.text("source"),
        )

    if not isinstance(data["viewpoints"], Mapping) or not data["viewpoints"]:
        raise ValueError(f"{source}.viewpoints must be a non-empty object")
    viewpoints: dict[str, ViewpointSplit] = {}
    for vp_id, body in data["viewpoints"].items():
        where = f"{source}.viewpoints.{vp_id}"
        validate_viewpoint_id(vp_id)
        fields = _Fields(body, where, {"time_zone", "designs", "scored"}, {"note"})
        zone = fields.text("time_zone")
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"{where}.time_zone {zone!r} is not an IANA zone") from None
        designs = body["designs"]
        if not isinstance(designs, Mapping):
            raise ValueError(f"{where}.designs must be an object")
        chosen: list[HeldOutPeriod] = []
        for design, period_name in designs.items():
            if design not in DESIGNS:
                raise ValueError(f"{where}.designs: unknown design {design!r}")
            period = periods.get(period_name)
            if period is None:
                raise ValueError(f"{where}.designs.{design} names no period {period_name!r}")
            if period.design != design:
                raise ValueError(
                    f"{where}.designs.{design}: {period_name} is a design-{period.design} period"
                )
            chosen.append(period)
        scored = body["scored"]
        if not isinstance(scored, bool):
            raise ValueError(f"{where}.scored must be true or false")
        if scored != bool(chosen):
            raise ValueError(f"{where}: a viewpoint is scored exactly when it has a design")
        viewpoints[vp_id] = ViewpointSplit(
            viewpoint_id=vp_id,
            time_zone=zone,
            periods=tuple(chosen),
            scored=scored,
            note=fields.text("note", empty=True),
        )

    exclusions_raw = data["scoring_exclusions"]
    if not isinstance(exclusions_raw, list):
        raise ValueError(f"{source}.scoring_exclusions must be a list")
    exclusions = []
    for i, body in enumerate(exclusions_raw):
        where = f"{source}.scoring_exclusions[{i}]"
        fields = _Fields(body, where, {"local_day", "viewpoints", "reason", "source"})
        names = body["viewpoints"]
        if not isinstance(names, list) or not names or not set(names) <= viewpoints.keys():
            raise ValueError(f"{where}.viewpoints must list viewpoints of this split")
        exclusions.append(
            ScoringExclusion(
                day=fields.day("local_day"),
                viewpoints=frozenset(names),
                reason=fields.text("reason"),
                source=fields.text("source"),
            )
        )

    guard_fields = _Fields(
        data["archive_guard"],
        f"{source}.archive_guard",
        {"from_local_day", "viewpoints", "source"},
        {"date_rule"},
    )
    if data["archive_guard"]["viewpoints"] != "all":
        raise ValueError(f"{source}.archive_guard.viewpoints must be 'all' (D-019)")
    quarantine_fields = _Fields(
        data["quarantine"], f"{source}.quarantine", {"dir", "source"}, {"rule"}
    )
    quarantine_dir = quarantine_fields.text("dir")
    if "/" in quarantine_dir or quarantine_dir in (".", ".."):
        raise ValueError(f"{source}.quarantine.dir must be one folder name")

    guard = ArchiveGuard(guard_fields.day("from_local_day"), guard_fields.text("source"))
    return HeldOutSplit(
        version=version,
        sha256=sha256,
        source=source,
        decided=top.day("decided"),
        freeze=top.day("freeze"),
        decisions=tuple(decisions),
        periods=MappingProxyType(periods),
        viewpoints=MappingProxyType(viewpoints),
        scoring_exclusions=tuple(exclusions),
        archive_guard=guard,
        quarantine_dir=quarantine_dir,
    )


def _parse(raw: bytes, source: str) -> HeldOutSplit:
    data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    return split_from_dict(data, sha256=hashlib.sha256(raw).hexdigest(), source=source)


def load_split(path: str | os.PathLike[str]) -> HeldOutSplit:
    """Load a split document from a file (tests and drafts; production uses the committed one)."""
    return _parse(Path(path).read_bytes(), str(path))


@cache
def committed_split() -> HeldOutSplit:
    """The committed split, ``lookout_eval/heldout_periods.json`` (loaded once)."""
    raw = resources.files("lookout_eval").joinpath(HELDOUT_PERIODS_RESOURCE).read_bytes()
    return _parse(raw, f"lookout_eval/{HELDOUT_PERIODS_RESOURCE}")
