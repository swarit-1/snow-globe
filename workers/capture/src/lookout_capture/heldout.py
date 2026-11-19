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

from dataclasses import dataclass
from datetime import date, datetime
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


def asset_local_dates(
    image_create_datetime: str, local_time_string: str, tz_name: str
) -> list[date]:
    """Every local date an archive asset could be said to fall on (empty if none parses).

    In summer ``ImageCreateDateTime`` stays on local standard time while ``LocalTimeString``
    gives civil time, so near midnight the two can name different days. Both are read, plus
    the civil date of the parsed UTC time in the manifest zone; the guard uses the latest.
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
