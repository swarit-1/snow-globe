"""The design-H held-out guard for archived NPS frames (D-019; PRD §13.2, §13.9).

D-019 fixed the hindcast split before any archived frame was to be viewed:

- held out, at every ARD camera: summer 2026, 1 Jun - 31 Aug 2026 (local dates);
- until scoring, archived frames (full size *or* thumbnail) are viewed only from before
  1 Jun 2026; metadata (``GetAvailableDays``, ``Search/Execute``) may be read for any date.

This module is that rule in code. Everything that downloads or reads archived frames
(:func:`lookout_capture.archive.backfill_assets`, the archive check, the CLI) asks
:func:`held_out_reason` first and refuses a frame dated on or after :data:`HELD_OUT_FROM`
unless it holds a :class:`HeldOutOverride`. An override carries a written reason and is
logged with every frame it releases; only the scoring step may use one (PRD §13.2: the
split is committed before any held-out frame is touched).

On 19 Nov 2026 the M0.5 archive check broke this rule before the guard existed. The record
is in ``docs/research/nps-archive-check.md``, section "Guard breach".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from lookout_common.timeutil import parse_nps_archive_time

__all__ = [
    "HELD_OUT_FROM",
    "HeldOutOverride",
    "asset_local_dates",
    "held_out_reason",
]

#: D-019: archived frames dated on or after this local date are not viewed before scoring.
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


# ImageCreateDateTime as NPS writes it, e.g. "05/31/2026 11:30:00 PM" (lookout-common's form).
_NPS_CLOCK_RE = re.compile(
    r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([ap]m)\s*$",
    re.IGNORECASE,
)


def _standard_time_date(image_create_datetime: str, tz_name: str) -> date | None:
    """The civil date of ``ImageCreateDateTime`` read as local *standard* time, or None.

    NPS keeps ``ImageCreateDateTime`` on standard time all year (lookout-common's
    ``parse_nps_archive_time``), so in summer civil time is one hour later:
    "05/31/2026 11:30:00 PM" is 00:30 PDT on 1 Jun. Where the zone keeps no daylight time
    (Arizona), or in winter, this is the stamp's own date. The same reading as lookout-eval's
    held-out guard (D-038), so capture's guard is never looser than eval's.
    """
    match = _NPS_CLOCK_RE.match(image_create_datetime or "")
    if match is None:
        return None
    month, day, year, hour, minute, second, meridiem = match.groups()
    if not 1 <= int(hour) <= 12:
        return None
    hour24 = int(hour) % 12 + (12 if meridiem.lower() == "pm" else 0)
    try:
        wall = datetime(int(year), int(month), int(day), hour24, int(minute), int(second or 0))
        zone = ZoneInfo(tz_name)
    except (ValueError, KeyError):  # ZoneInfoNotFoundError is a KeyError
        return None
    probe = wall.replace(tzinfo=zone)
    offset, daylight = probe.utcoffset(), probe.dst()
    if offset is None:
        return None
    standard = offset - (daylight or timedelta(0))
    return (wall - standard).replace(tzinfo=UTC).astimezone(zone).date()


def asset_local_dates(
    image_create_datetime: str, local_time_string: str, tz_name: str
) -> list[date]:
    """Every local date an archive asset could be said to fall on (empty if none parses).

    In summer ``ImageCreateDateTime`` stays on local standard time while ``LocalTimeString``
    gives civil time, so near midnight the two can name different days. Read here (D-019,
    D-022, D-038):

    - the date of ``ImageCreateDateTime`` and the date of ``LocalTimeString``;
    - the civil date of the parsed UTC time in the manifest zone;
    - the civil date of ``ImageCreateDateTime`` read as local standard time, so a summer
      frame stamped "05/31/2026 11:30:00 PM" with no readable ``LocalTimeString`` still
      dates to 1 Jun (00:30 PDT).

    The guard uses the latest. This is the same set lookout-eval's ``nps_local_dates`` reads.
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


def held_out_reason(
    image_create_datetime: str,
    local_time_string: str,
    tz_name: str,
    *,
    guard: date = HELD_OUT_FROM,
) -> str | None:
    """Why an archived frame may not be viewed yet, or None if it may.

    A frame is refused when any of its local dates (:func:`asset_local_dates`) is on or
    after ``guard``, and also when no date can be read at all (the guard fails closed).
    """
    dates = asset_local_dates(image_create_datetime, local_time_string, tz_name)
    if not dates:
        return f"held-out guard (D-019): no readable date in {image_create_datetime!r}"
    latest = dates[-1]
    if latest >= guard:
        return (
            f"held-out guard (D-019): local date {latest.isoformat()} is on or after "
            f"{guard.isoformat()}; archived frames are viewed only before it until scoring"
        )
    return None
