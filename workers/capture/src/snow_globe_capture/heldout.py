"""The design-H held-out guard for archived NPS frames (D-019; PRD §13.2, §13.9).

D-019 fixed the hindcast split before any archived frame was to be viewed:

- held out, at every ARD camera: summer 2026, 1 Jun - 31 Aug 2026 (local dates);
- until scoring, archived frames (full size *or* thumbnail) are viewed only from before
  1 Jun 2026; metadata (``GetAvailableDays``, ``Search/Execute``) may be read for any date.

This module is that rule in code. Everything that downloads or reads archived frames
(:func:`snow_globe_capture.archive.backfill_assets`, the archive check, the CLI) asks
:func:`held_out_reason` first and refuses a frame dated on or after :data:`HELD_OUT_FROM`
unless it holds a :class:`HeldOutOverride`. An override carries a written reason and is
logged with every frame it releases; only the scoring step may use one (PRD §13.2: the
split is committed before any held-out frame is touched).

**Which date** (the lead's reading of 20 Nov 2026, pending the owner's D-038): an asset is
held out if *any* reading of its date, local or UTC, is on or after 1 Jun 2026
(:func:`asset_dates`). So a frame of 31 May local evening, already 1 Jun in UTC, is refused.
snow-globe-eval's ``heldout.nps_dates`` reads exactly the same set; the two are kept in parity
by tests on both sides (eval's fuzz loads this file).

On 19 Nov 2026 the M0.5 archive check broke this rule before the guard existed. The record
is in ``docs/research/nps-archive-check.md``, section "Guard breach".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from snow_globe_common.timeutil import parse_nps_archive_time

__all__ = [
    "HELD_OUT_FROM",
    "HeldOutOverride",
    "asset_dates",
    "asset_local_dates",
    "asset_utc_dates",
    "held_out_reason",
]

#: D-019: archived frames dated on or after this date (local or UTC) are not viewed before
#: scoring.
HELD_OUT_FROM = date(2026, 6, 1)
_MIN_REASON_CHARS = 12


@dataclass(frozen=True)
class HeldOutOverride:
    """Permission to touch archived frames past the guard, for scoring only.

    ``reason`` is written into the capture log next to every frame the override releases,
    so a reviewer can tell scoring access from a leak.
    """

    reason: str

    def __post_init__(self) -> None:
        if len(self.reason.strip()) < _MIN_REASON_CHARS:
            raise ValueError(
                f"a held-out override needs a written reason (at least {_MIN_REASON_CHARS} "
                "characters), e.g. 'design-H scoring after the pre-registration commit <sha>'"
            )


def _us_date(text: str) -> date | None:
    head = text.strip().split(" ", 1)[0]
    try:
        return datetime.strptime(head, "%m/%d/%Y").date()
    except ValueError:
        return None


# ImageCreateDateTime as NPS writes it, e.g. "05/31/2026 11:30:00 PM" (snow-globe-common's form).
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

    NPS keeps ``ImageCreateDateTime`` on standard time all year (snow-globe-common's
    ``parse_nps_archive_time``), so in summer civil time is one hour later:
    "05/31/2026 11:30:00 PM" is 00:30 PDT on 1 Jun. Where the zone keeps no daylight time
    (Arizona), or in winter, this is the stamp's own date. The same reading as snow-globe-eval's
    held-out guard (D-038, D-047).
    """
    wall = _wall(_NPS_CLOCK_RE.match(image_create_datetime or ""))
    zone = _zone(tz_name)
    if wall is None or zone is None:
        return None
    standard = _standard_offset(wall, zone)
    if standard is None:
        return None
    return (wall - standard).replace(tzinfo=UTC).astimezone(zone).date()


def asset_local_dates(
    image_create_datetime: str, local_time_string: str, tz_name: str
) -> list[date]:
    """Every local date an archive asset could be said to fall on (empty if none parses).

    In summer ``ImageCreateDateTime`` stays on local standard time while ``LocalTimeString``
    gives civil time, so near midnight the two can name different days. Read here (D-019,
    D-022, D-038, D-047):

    - the date of ``ImageCreateDateTime`` and the date of ``LocalTimeString``;
    - the civil date of the parsed UTC time in the manifest zone;
    - the civil date of ``ImageCreateDateTime`` read as local standard time, so a summer
      frame stamped "05/31/2026 11:30:00 PM" with no readable ``LocalTimeString`` still
      dates to 1 Jun (00:30 PDT).

    This is the same set snow-globe-eval's ``nps_local_dates`` reads.
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


def asset_utc_dates(image_create_datetime: str, local_time_string: str, tz_name: str) -> list[date]:
    """Every UTC date an archive asset could be said to fall on (the lead's reading, 20 Nov 2026).

    - the UTC date of the asset's instant, under both of snow-globe-common's conventions for
      ``parse_nps_archive_time`` (``"label"``, the default, and ``"wall"``): the instant behind
      its archive key, whichever convention the backfill was asked to use;
    - each local reading converted in the camera's zone: ``ImageCreateDateTime`` read as civil
      time (both sides of a fold or gap) and as local standard time, and ``LocalTimeString``'s
      clock read as civil time (both sides);
    - a reading that gives a date but no readable clock: every UTC date its local day touches.

    Same as snow-globe-eval's ``nps_utc_dates``.
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


def asset_dates(
    image_create_datetime: str,
    local_time_string: str,
    tz_name: str,
    *,
    instant: datetime | None = None,
) -> list[date]:
    """Every date, local or UTC, an archive asset could be said to fall on (the guard's set).

    :func:`asset_local_dates` and :func:`asset_utc_dates`, plus, when ``instant`` is given (the
    aware time behind the asset's archive key), its civil date in ``tz_name`` and its UTC date.
    Same as snow-globe-eval's ``nps_dates``.
    """
    found = set(asset_local_dates(image_create_datetime, local_time_string, tz_name))
    found |= set(asset_utc_dates(image_create_datetime, local_time_string, tz_name))
    if instant is not None:
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("instant must be timezone-aware")
        zone = _zone(tz_name)
        if zone is not None:
            found.add(instant.astimezone(zone).date())
        found.add(instant.astimezone(UTC).date())
    return sorted(found)


def held_out_reason(
    image_create_datetime: str,
    local_time_string: str,
    tz_name: str,
    *,
    guard: date = HELD_OUT_FROM,
    instant: datetime | None = None,
) -> str | None:
    """Why an archived frame may not be viewed yet, or None if it may.

    A frame is refused when any of its dates, local or UTC (:func:`asset_dates`), is on or
    after ``guard``, and also when no date can be read at all (the guard fails closed). NPS
    strings that are given but yield no local date are refused even with an ``instant``: they
    mean the metadata is broken. The rule of snow-globe-eval's ``archive_guard_reason``.
    """
    local = asset_local_dates(image_create_datetime, local_time_string, tz_name)
    raw_given = bool((image_create_datetime or "").strip() or (local_time_string or "").strip())
    if raw_given and not local:
        return f"held-out guard (D-019): no readable date in {image_create_datetime!r}"
    dates = asset_dates(image_create_datetime, local_time_string, tz_name, instant=instant)
    if not dates:
        return f"held-out guard (D-019): no readable date in {image_create_datetime!r}"
    latest = dates[-1]
    if latest >= guard:
        zone = _zone(tz_name)
        civil = {instant.astimezone(zone).date()} if instant is not None and zone else set()
        which = "local" if latest in {*local, *civil} else "UTC"
        return (
            f"held-out guard (D-019): {which} date {latest.isoformat()} is on or after "
            f"{guard.isoformat()}; archived frames are viewed only before it until scoring"
        )
    return None
