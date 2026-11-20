"""Which HRRR runs exist, which hours they have, and when each hour appeared (PRD §8.3, §11.2).

A run is **complete** when every forecast hour (f00-f18, or f00-f48 at 00/06/12/18Z) has
both its GRIB2 file and its ``.idx``. Hours do not arrive in order: on 2026-11-19 the 07Z
run had f09 on S3 before f08, and the 05Z run had f01 seven minutes before f00.

An hour is usable only once both objects exist (the worker needs the inventory to make
range requests), so its availability is the later of the two ``LastModified`` stamps.
S3 gives a multipart upload the time the upload *started*; using the later ``.idx`` stamp
(written a few seconds after the GRIB2) keeps the estimate from running early.

**Old stamps are not always publication times** (PRD §13.9). Most archived runs keep them
(14 of the 18 listed on 20 Nov 2026, from 2021-03 to 2026-03), but some objects were rewritten
long after the run: the ``.idx`` files of 2020120312 and 2021011500 carry 17 Feb 2021 stamps
while their GRIB2 files keep stamps 54-115 min after the run start, and both objects of
2015071512 and 2018071512 carry 2020-2021 stamps. :func:`publication_floor` therefore counts
a stamp only within :data:`STAMP_TRUST_H` of the run start.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from lookout_common.timeutil import utcnow

from .bucket import HrrrBucket, S3Object
from .hrrr import (
    as_run_start,
    day_prefix,
    floor_hour,
    format_run_id,
    object_url,
    parse_object_key,
    run_hours,
    run_prefix,
)

__all__ = [
    "IDX_AFTER_GRIB_MARGIN_S",
    "STAMP_TRUST_H",
    "HourFiles",
    "RunListing",
    "find_latest_complete",
    "group_runs",
    "list_day",
    "list_run",
    "publication_floor",
]

#: A ``LastModified`` stamp later than this after its run's start says when the object was
#: copied or rewritten, not when the hour was published. Listed on 20 Nov 2026: published hours
#: carried stamps 46.6-131.2 min after the run start (4,688 hours of 195 runs of 12-20 Nov
#: 2026, and 784 hours of 16 archived runs of Dec 2020 - Mar 2026, whose largest was 116.5
#: min), while rewritten objects were 33.6 days to 5.6 years late. 24 h is also the
#: extractor's live window: a stamp the live worker would have used.
STAMP_TRUST_H = 24.0
#: When only an hour's GRIB2 stamp can be trusted (its ``.idx`` was rewritten later), the hour
#: counts as usable this long after the GRIB2 stamp: in 5,374 hours of 209 runs listed with
#: both stamps trusted, the ``.idx`` came 3-188 s after its GRIB2 (median 4 s, 99th percentile
#: 9 s; the 188 s was 2022011506 f31), never before it.
IDX_AFTER_GRIB_MARGIN_S = 300.0


def publication_floor(
    run_start: datetime, grib_utc: datetime | None, idx_utc: datetime | None
) -> tuple[datetime | None, tuple[str, ...]]:
    """The earliest an hour can have been usable, from its own S3 stamps (PRD §8.3, §13.9).

    Returns ``(floor, ignored)``. A stamp counts only within :data:`STAMP_TRUST_H` of the run
    start; ``ignored`` names the ones that do not (``"grib"``, ``"idx"``). With both stamps
    trusted the floor is the later one, which is what a live run uses as its availability;
    with only the ``.idx`` stamp, that stamp (an inventory is written after its GRIB2); with
    only the GRIB2 stamp, that stamp plus :data:`IDX_AFTER_GRIB_MARGIN_S`; with neither,
    None.
    """
    start = as_run_start(run_start)
    limit = start + timedelta(hours=STAMP_TRUST_H)
    stamps = {"grib": grib_utc, "idx": idx_utc}
    ignored = tuple(name for name, stamp in stamps.items() if stamp is not None and stamp > limit)
    grib = grib_utc if grib_utc is not None and "grib" not in ignored else None
    idx = idx_utc if idx_utc is not None and "idx" not in ignored else None
    if grib is not None and idx is not None:
        return max(grib, idx), ignored
    if idx is not None:
        return idx, ignored
    if grib is not None:
        return grib + timedelta(seconds=IDX_AFTER_GRIB_MARGIN_S), ignored
    return None, ignored


@dataclass(frozen=True, slots=True)
class HourFiles:
    """The GRIB2 file and inventory of one forecast hour, as listed."""

    fh: int
    grib: S3Object | None = None
    idx: S3Object | None = None

    @property
    def ready(self) -> bool:
        """True once both the GRIB2 file and its ``.idx`` exist."""
        return self.grib is not None and self.idx is not None

    @property
    def availability_utc(self) -> datetime | None:
        """The later of the two ``LastModified`` stamps (None until both exist)."""
        if self.grib is None or self.idx is None:
            return None
        return max(self.grib.last_modified, self.idx.last_modified)

    @property
    def grib_url(self) -> str | None:
        """HTTPS URL of the GRIB2 file."""
        return object_url(self.grib.key) if self.grib is not None else None

    def stamps_trusted(self, run_start: datetime) -> bool:
        """True when both stamps exist and fall within :data:`STAMP_TRUST_H` of the run start."""
        if self.grib is None or self.idx is None:
            return False
        return not publication_floor(run_start, self.grib.last_modified, self.idx.last_modified)[1]


@dataclass(frozen=True)
class RunListing:
    """One run's ``wrfsfc`` objects at the moment they were listed."""

    run_start: datetime
    hours: Mapping[int, HourFiles]
    listed_utc: datetime
    expected: tuple[int, ...] = field(default=())

    def __post_init__(self) -> None:
        as_run_start(self.run_start)
        if not self.expected:
            object.__setattr__(self, "expected", tuple(run_hours(self.run_start)))

    @property
    def run_id(self) -> str:
        """``YYYYMMDDHH``."""
        return format_run_id(self.run_start)

    @property
    def ready_hours(self) -> list[int]:
        """Expected hours whose GRIB2 and ``.idx`` both exist."""
        return [fh for fh in self.expected if fh in self.hours and self.hours[fh].ready]

    @property
    def missing_hours(self) -> list[int]:
        """Expected hours not (yet) usable."""
        ready = set(self.ready_hours)
        return [fh for fh in self.expected if fh not in ready]

    @property
    def complete(self) -> bool:
        """True when every expected hour is usable."""
        return not self.missing_hours

    @property
    def exists(self) -> bool:
        """True if any object of the run is listed."""
        return bool(self.hours)

    def hour(self, fh: int) -> HourFiles:
        """The listing of one hour (raises ``KeyError`` if that hour is not listed)."""
        return self.hours[fh]


def group_runs(objects: Iterable[S3Object], *, listed_utc: datetime) -> dict[datetime, RunListing]:
    """Group listed objects into runs, ignoring every product other than ``wrfsfc``."""
    grouped: dict[datetime, dict[int, HourFiles]] = {}
    for obj in objects:
        name = parse_object_key(obj.key)
        if name is None:
            continue
        hours = grouped.setdefault(name.run_start, {})
        current = hours.get(name.fh, HourFiles(name.fh))
        if name.is_idx:
            current = HourFiles(name.fh, current.grib, obj)
        else:
            current = HourFiles(name.fh, obj, current.idx)
        hours[name.fh] = current
    return {
        run: RunListing(run, dict(sorted(hours.items())), listed_utc)
        for run, hours in sorted(grouped.items())
    }


def list_run(bucket: HrrrBucket, run_start: datetime, *, now: datetime | None = None) -> RunListing:
    """List one run with the narrow prefix ``hrrr.YYYYMMDD/conus/hrrr.tHHz.wrfsfcf`` (1 request)."""
    run_start = as_run_start(run_start)
    listed = now or utcnow()
    runs = group_runs(bucket.list_objects(run_prefix(run_start)), listed_utc=listed)
    return runs.get(run_start) or RunListing(run_start, {}, listed)


def list_day(
    bucket: HrrrBucket, day: date, *, now: datetime | None = None
) -> dict[datetime, RunListing]:
    """List every run of a UTC day with the prefix ``hrrr.YYYYMMDD/conus/`` (several pages).

    The day prefix holds every product (``wrfprs``, ``wrfnat``, ``wrfsubh`` too), so this
    costs several 1,000-key pages; the worker's hot path uses :func:`list_run` instead.
    """
    listed = now or utcnow()
    return group_runs(bucket.list_objects(day_prefix(day)), listed_utc=listed)


def find_latest_complete(
    bucket: HrrrBucket,
    *,
    now: datetime | None = None,
    lookback_h: int = 8,
) -> RunListing | None:
    """The newest complete run whose start is within ``lookback_h`` hours of ``now``."""
    moment = now or utcnow()
    newest = floor_hour(moment)
    for back in range(lookback_h + 1):
        listing = list_run(bucket, newest - timedelta(hours=back), now=moment)
        if listing.complete:
            return listing
    return None
