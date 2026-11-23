"""Dev-fit and dev-check weeks, alternating (PRD §13.9 "Feature admission").

PRD §13.9: "Development data is split into dev-fit and dev-check weeks, alternating. A renderer
feature is admitted only if it improves dev-check P1 PSS by ≥ 0.02". The unit eval already uses
for this split is the **ISO week** (Monday to Sunday): the archive backfill planner samples whole
ISO weeks and counts them by ISO-week parity (``eval/tools/backfill_plan/plan.py``,
``t1a_parity_by_month``; docs/research/archive-backfill-plan.md §4, "ISO weeks; parity
alternates"). This module applies that parity:

- a frame's week is the ISO week of its **valid time's local calendar day** in the viewpoint's
  zone (the day blocks of the bootstrap are local days too, :func:`.scores.local_day_ordinals`);
- **odd ISO weeks are dev-fit, even ISO weeks are dev-check**, so the split alternates week by
  week and starts with dev-fit at design P's first development week (ISO 2026-W47, 16–22 Nov,
  which holds the go-ahead of 19 Nov). Which parity fits is this module's choice, recorded for
  the lead (the planner's text leaves it open).

An ISO year with 53 weeks puts two odd weeks together (week 53, then week 1); 2026 is such a
year, but its week 53 (28 Dec 2026 – 3 Jan 2027) lies inside design P's sealed window, which
never reaches development.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

__all__ = ["DEV_CHECK", "DEV_FIT", "DevRole", "dev_role", "dev_week", "dev_week_of_time"]

DEV_FIT = "dev-fit"
DEV_CHECK = "dev-check"
type DevRole = Literal["dev-fit", "dev-check"]


def dev_role(day: date) -> DevRole:
    """``dev-fit`` for a day in an odd ISO week, ``dev-check`` for an even one."""
    if isinstance(day, datetime):
        raise TypeError("pass the local calendar date, not a datetime (see dev_week_of_time)")
    return DEV_FIT if day.isocalendar().week % 2 == 1 else DEV_CHECK


def dev_week(day: date) -> dict[str, object]:
    """The ISO year and week of a local day, and its role, as a JSON-ready dict."""
    iso = day.isocalendar()
    return {"iso_year": iso.year, "iso_week": iso.week, "role": dev_role(day)}


def dev_week_of_time(moment: datetime, time_zone: str) -> dict[str, object]:
    """:func:`dev_week` of a time's local calendar day in ``time_zone`` (an aware time only)."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("moment must be timezone-aware")
    local = moment.astimezone(ZoneInfo(time_zone)).date()
    return {"local_day": local.isoformat(), **dev_week(local)}
