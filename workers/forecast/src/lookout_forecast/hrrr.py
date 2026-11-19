"""HRRR on AWS: bucket, object names and run arithmetic (PRD §8.1, §8.3).

See docs/research/forecast.md §2.

NOAA publishes every HRRR CONUS cycle to the open-data bucket ``noaa-hrrr-bdp-pds``
(us-east-1), read here over anonymous HTTPS. The surface file for run ``HH`` of day
``YYYYMMDD`` and forecast hour ``FF`` is::

    hrrr.YYYYMMDD/conus/hrrr.tHHz.wrfsfcfFF.grib2        (+ ``.idx`` inventory)

Runs start every hour. The 00/06/12/18Z ("synoptic") runs go to 48 h; the others to 18 h.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

__all__ = [
    "BASE_URL",
    "BUCKET",
    "KEY_RE",
    "MODEL",
    "PRODUCT",
    "SOURCE",
    "STORAGE_SOURCE",
    "SYNOPTIC_HOURS",
    "ObjectName",
    "as_run_start",
    "day_prefix",
    "floor_hour",
    "format_run_id",
    "idx_key",
    "max_fh",
    "object_key",
    "object_url",
    "parse_object_key",
    "parse_run_id",
    "run_hours",
    "run_prefix",
    "valid_time",
]

#: S3 bucket and the anonymous HTTPS endpoint CLAUDE.md prescribes for it.
BUCKET = "noaa-hrrr-bdp-pds"
BASE_URL = f"https://{BUCKET}.s3.amazonaws.com"
#: Values written into every forecast file (PRD §8.3 "source and model").
SOURCE = BUCKET
MODEL = "hrrr-conus"
PRODUCT = "wrfsfc"
#: The ``<source>`` in ``forecast/<source>/<viewpoint>/<run>z.json`` (CLAUDE.md data layout).
STORAGE_SOURCE = "hrrr"
#: Cycles that run to 48 h; every other cycle stops at 18 h.
SYNOPTIC_HOURS = frozenset({0, 6, 12, 18})

#: ``hrrr.20261119/conus/hrrr.t06z.wrfsfcf07.grib2`` or the same with ``.idx``.
KEY_RE = re.compile(
    r"^hrrr\.(?P<day>\d{8})/conus/hrrr\.t(?P<hour>\d{2})z\.wrfsfcf(?P<fh>\d{2})\.grib2(?P<idx>\.idx)?$"
)
_RUN_ID_RE = re.compile(r"^\d{10}$")


def floor_hour(moment: datetime) -> datetime:
    """Return ``moment`` in UTC, truncated to the hour."""
    if moment.tzinfo is None:
        raise ValueError("expected a timezone-aware datetime")
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def as_run_start(moment: datetime) -> datetime:
    """Validate that ``moment`` is an aware datetime on the hour and return it in UTC."""
    if moment.tzinfo is None:
        raise ValueError("run start must be timezone-aware")
    utc = moment.astimezone(UTC)
    if (utc.minute, utc.second, utc.microsecond) != (0, 0, 0):
        raise ValueError(f"run start {utc.isoformat()} is not on the hour")
    return utc


def parse_run_id(text: str) -> datetime:
    """Parse ``YYYYMMDDHH`` (an optional trailing ``z`` is accepted) into a UTC run start."""
    raw = text.strip().lower().removesuffix("z")
    if not _RUN_ID_RE.match(raw):
        raise ValueError(f"run id {text!r} must look like YYYYMMDDHH, e.g. 2026111906")
    return datetime.strptime(raw, "%Y%m%d%H").replace(tzinfo=UTC)


def format_run_id(run_start: datetime) -> str:
    """Format a run start as ``YYYYMMDDHH`` (the form used in keys and logs)."""
    return as_run_start(run_start).strftime("%Y%m%d%H")


def max_fh(run_start: datetime) -> int:
    """Last forecast hour HRRR produces for this cycle: 48 at 00/06/12/18Z, else 18."""
    return 48 if as_run_start(run_start).hour in SYNOPTIC_HOURS else 18


def run_hours(run_start: datetime, limit: int | None = None) -> list[int]:
    """Forecast hours of a run, ``0..max_fh``, optionally capped at ``limit``."""
    last = max_fh(run_start)
    if limit is not None:
        if limit < 0:
            raise ValueError("forecast-hour limit must be >= 0")
        last = min(last, limit)
    return list(range(last + 1))


def valid_time(run_start: datetime, fh: int) -> datetime:
    """Valid time of forecast hour ``fh`` of the run."""
    return as_run_start(run_start) + timedelta(hours=fh)


def day_prefix(day: date | datetime) -> str:
    """S3 prefix of one UTC day's CONUS files: ``hrrr.YYYYMMDD/conus/``."""
    if isinstance(day, datetime):
        day = day.astimezone(UTC).date()
    return f"hrrr.{day:%Y%m%d}/conus/"


def run_prefix(run_start: datetime) -> str:
    """S3 prefix that lists exactly one run's ``wrfsfc`` files and inventories."""
    utc = as_run_start(run_start)
    return f"{day_prefix(utc)}hrrr.t{utc:%H}z.wrfsfcf"


def object_key(run_start: datetime, fh: int) -> str:
    """Key of the ``wrfsfc`` GRIB2 file for one forecast hour."""
    if not 0 <= fh <= 99:
        raise ValueError(f"forecast hour {fh} out of range")
    return f"{run_prefix(run_start)}{fh:02d}.grib2"


def idx_key(run_start: datetime, fh: int) -> str:
    """Key of the ``.idx`` inventory that sits beside a GRIB2 file."""
    return object_key(run_start, fh) + ".idx"


def object_url(key: str) -> str:
    """Anonymous HTTPS URL of a bucket key."""
    return f"{BASE_URL}/{key}"


@dataclass(frozen=True, slots=True)
class ObjectName:
    """A parsed ``wrfsfc`` key: which run, which hour, GRIB2 or inventory."""

    run_start: datetime
    fh: int
    is_idx: bool


def parse_object_key(key: str) -> ObjectName | None:
    """Parse a ``wrfsfc`` key (GRIB2 or ``.idx``); return None for any other product."""
    match = KEY_RE.match(key)
    if match is None:
        return None
    run_start = datetime.strptime(match["day"] + match["hour"], "%Y%m%d%H").replace(tzinfo=UTC)
    return ObjectName(run_start=run_start, fh=int(match["fh"]), is_idx=match["idx"] is not None)
