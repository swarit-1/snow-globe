"""Time helpers: UTC now, ISO formatting, and parsers for NPS and EXIF timestamps.

Implements the time handling that capture and backfill need (PRD §8.6 "Fresh" gate,
§15.3, CLAUDE.md "NPS archive API"). All public functions return timezone-aware UTC
datetimes and refuse naive input. Local wall times are converted with the IANA zone
of the viewpoint; daylight-saving gaps and folds are handled explicitly, using the
PRD §7.4 rule: a nonexistent time moves forward, an ambiguous one takes the earlier
offset, and both are flagged.

NPS labels times with a zone *name* ("PST", "Pacific Standard Time"). The live feed's
abbreviation fixes the UTC offset exactly. The archive's long name is checked against
the viewpoint's zone and read as an honest civil-time label; the M0.5 archive check
confirmed that, summer included (see :func:`parse_nps_archive_time`).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

__all__ = [
    "FLAG_AMBIGUOUS",
    "FLAG_NONEXISTENT",
    "FLAG_OFFSET_FROM_EXIF",
    "NPS_ABBREVIATION_OFFSETS_H",
    "AmbiguousTimeError",
    "NonexistentTimeError",
    "exif_local_to_utc",
    "isoformat_z",
    "localize",
    "parse_iso_utc",
    "parse_nps_archive_time",
    "parse_nps_imagedate",
    "to_utc",
    "utcnow",
]

#: Flag: the wall time occurs twice (DST fold); the earlier instant was chosen.
FLAG_AMBIGUOUS = "ambiguous-earlier-offset"
#: Flag: the wall time does not exist (DST gap); it was moved forward across the gap.
FLAG_NONEXISTENT = "nonexistent-moved-forward"
#: Flag: an explicit EXIF offset was used instead of the zone rules.
FLAG_OFFSET_FROM_EXIF = "offset-from-exif"

#: UTC offsets (hours) for the zone abbreviations the NPS live feeds use.
NPS_ABBREVIATION_OFFSETS_H: dict[str, int] = {
    "PST": -8,
    "PDT": -7,
    "MST": -7,
    "MDT": -6,
    "CST": -6,
    "CDT": -5,
    "EST": -5,
    "EDT": -4,
}

# Long zone names that can appear in the archive's LocalTimeString, mapped to
# (standard offset in hours, is_daylight, IANA zones the name is consistent with).
_US_REGIONS: dict[str, tuple[int, frozenset[str]]] = {
    "Pacific": (-8, frozenset({"America/Los_Angeles"})),
    "Mountain": (-7, frozenset({"America/Denver", "America/Boise", "America/Phoenix"})),
    "US Mountain": (-7, frozenset({"America/Phoenix"})),  # Windows ID for Arizona
    "Central": (-6, frozenset({"America/Chicago"})),
    "Eastern": (-5, frozenset({"America/New_York", "America/Detroit"})),
}
_NO_DST_ZONES = frozenset({"America/Phoenix"})


class AmbiguousTimeError(ValueError):
    """A local wall time occurs twice and nothing says which occurrence is meant."""


class NonexistentTimeError(ValueError):
    """A local wall time falls in a DST gap and the caller asked not to shift it."""


def utcnow() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(UTC)


def to_utc(moment: datetime) -> datetime:
    """Convert an aware datetime to UTC; raise ``ValueError`` on a naive one."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"naive datetime {moment.isoformat()} has no zone; refusing to guess")
    return moment.astimezone(UTC)


def isoformat_z(moment: datetime, timespec: str = "auto") -> str:
    """Format an aware datetime as RFC 3339 UTC with a ``Z`` suffix.

    ``timespec`` is passed to :meth:`datetime.isoformat` (``"seconds"``,
    ``"milliseconds"``, ...). Example: ``2026-11-19T06:40:14Z``.
    """
    return to_utc(moment).isoformat(timespec=timespec).replace("+00:00", "Z")


def parse_iso_utc(text: str) -> datetime:
    """Parse an ISO 8601 / RFC 3339 timestamp with an offset or ``Z``; return UTC."""
    moment = datetime.fromisoformat(text.strip())
    return to_utc(moment)


def _wall_kind(naive: datetime, zone: ZoneInfo) -> Literal["normal", "fold", "gap"]:
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    if first.utcoffset() == second.utcoffset():
        return "normal"
    round_trip = first.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    return "fold" if round_trip == naive else "gap"


def localize(
    naive: datetime,
    tz_name: str,
    *,
    on_ambiguous: Literal["earlier", "later", "raise"] = "earlier",
    on_nonexistent: Literal["forward", "raise"] = "forward",
) -> tuple[datetime, frozenset[str]]:
    """Attach an IANA zone to a naive wall time and return ``(utc, flags)``.

    Default policy follows PRD §7.4: an ambiguous time (DST fold) takes the earlier
    offset, i.e. the first occurrence, flagged :data:`FLAG_AMBIGUOUS`; a nonexistent
    time (DST gap) moves forward by the gap, flagged :data:`FLAG_NONEXISTENT`.
    """
    if naive.tzinfo is not None:
        raise ValueError("localize() takes a naive wall time")
    zone = ZoneInfo(tz_name)
    kind = _wall_kind(naive, zone)
    flags: set[str] = set()
    if kind == "fold":
        if on_ambiguous == "raise":
            raise AmbiguousTimeError(f"{naive.isoformat()} occurs twice in {tz_name}")
        fold = 0 if on_ambiguous == "earlier" else 1
        aware = naive.replace(tzinfo=zone, fold=fold)
        flags.add(FLAG_AMBIGUOUS)
    elif kind == "gap":
        if on_nonexistent == "raise":
            raise NonexistentTimeError(f"{naive.isoformat()} does not exist in {tz_name}")
        # fold=0 inside a gap uses the offset from before the transition, which maps
        # the wall time forward by the size of the gap (02:30 -> 03:30 in spring).
        aware = naive.replace(tzinfo=zone, fold=0)
        flags.add(FLAG_NONEXISTENT)
    else:
        aware = naive.replace(tzinfo=zone)
    return aware.astimezone(UTC), frozenset(flags)


_IMAGEDATE_RE = re.compile(
    r"^\s*(?:updated\s+)?(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*"
    r"([ap]\.?m\.?)\s+([a-z]{3,4})\s*$",
    re.IGNORECASE,
)


def _twelve_to_24(hour: int, meridiem: str) -> int:
    if not 1 <= hour <= 12:
        raise ValueError(f"12-hour clock value {hour} out of range")
    pm = meridiem.lower().replace(".", "") == "pm"
    return (hour % 12) + (12 if pm else 0)


def parse_nps_imagedate(text: str) -> datetime:
    """Parse an NPS live-feed stamp such as ``'Updated 11/18/2026 10:00 PM PST'`` to UTC.

    The abbreviation fixes the offset (PST/PDT/MST/MDT/CST/CDT/EST/EDT); any other
    abbreviation raises ``ValueError``. The ``Updated`` prefix is optional.
    """
    match = _IMAGEDATE_RE.match(text or "")
    if not match:
        raise ValueError(f"unrecognised NPS imagedate {text!r}")
    month, day, year, hour, minute, second, meridiem, abbr = match.groups()
    abbr = abbr.upper()
    if abbr not in NPS_ABBREVIATION_OFFSETS_H:
        raise ValueError(f"unknown time zone abbreviation {abbr!r} in {text!r}")
    offset = timezone(timedelta(hours=NPS_ABBREVIATION_OFFSETS_H[abbr]))
    local = datetime(
        int(year),
        int(month),
        int(day),
        _twelve_to_24(int(hour), meridiem),
        int(minute),
        int(second or 0),
        tzinfo=offset,
    )
    return local.astimezone(UTC)


_ARCHIVE_DT_RE = re.compile(
    r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([ap]m)\s*$",
    re.IGNORECASE,
)
_LOCAL_STRING_RE = re.compile(
    r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([ap]m)\s+(.+?)\s*$",
    re.IGNORECASE,
)
_ZONE_NAME_RE = re.compile(r"^(?P<region>.+?)\s+(?P<kind>standard|daylight)\s+time$", re.IGNORECASE)


def _parse_us_clock(match: re.Match[str]) -> datetime:
    month, day, year, hour, minute, second, meridiem = match.groups()[:7]
    return datetime(
        int(year),
        int(month),
        int(day),
        _twelve_to_24(int(hour), meridiem),
        int(minute),
        int(second or 0),
    )


def _zone_name_info(name: str) -> tuple[str, int, bool, frozenset[str]]:
    zone_match = _ZONE_NAME_RE.match(name.strip())
    if not zone_match:
        raise ValueError(f"unknown zone name {name!r} in LocalTimeString")
    region_key = " ".join(zone_match["region"].split()).title().replace("Us ", "US ")
    if region_key not in _US_REGIONS:
        raise ValueError(f"unknown zone name {name!r} in LocalTimeString")
    std_offset, zones = _US_REGIONS[region_key]
    return region_key, std_offset, zone_match["kind"].lower() == "daylight", zones


def parse_nps_archive_time(
    image_create_datetime: str,
    local_time_string: str,
    tz_name: str,
    *,
    convention: Literal["label", "wall"] = "label",
) -> datetime:
    """Convert an NPS archive asset's local time to aware UTC.

    Inputs come from one ``/airwebcams/api/Search/Execute`` result, for example
    ``ImageCreateDateTime = "11/17/2026 12:00:00 AM"`` and
    ``LocalTimeString = "11/17/2026 12:00 AM Pacific Standard Time"``, plus the
    viewpoint's IANA zone (Grand Canyon is ``America/Phoenix``: Arizona keeps MST).

    Checks: both strings must give the same wall time to the minute, and the zone
    name must belong to ``tz_name``'s region (``ValueError`` otherwise). The one
    accepted exception is the summer case below: a "Daylight" ``LocalTimeString``
    exactly 1 h ahead of ``ImageCreateDateTime``, which is then read from the label.

    ``convention`` picks how the wall time maps to UTC:

    - ``"label"`` (default, and the one the evidence supports): read
      ``LocalTimeString`` as an honest civil-time label. "Standard" means the zone's
      standard offset and "Daylight" means standard + 1 h. It cannot be ambiguous.
    - ``"wall"``: treat the wall time as civil time in ``tz_name`` with DST rules. This
      would be right if the name were a fixed Windows zone ID (e.g. "Pacific Standard
      Time" all year) while the clock followed DST; the archive check found no such
      case. A fold is resolved by a "Daylight" label; otherwise
      :class:`AmbiguousTimeError` is raised. A gap raises
      :class:`NonexistentTimeError`, since a camera cannot record a time that did not
      exist.

    Evidence (M0.5 archive check, ``docs/research/nps-archive-check.md``; D-024): on
    all 90 sampled days, winter and summer, times parsed here match NPS's own
    ``SolarElevationAngle``. The mean absolute difference per day is 0.015-0.065°
    and the best-fitting clock offset is 0 min on every day; a 1 h error would show
    as up to ~12°. In summer, ``ImageCreateDateTime`` stays on local standard time
    while ``LocalTimeString`` gives civil time labelled "Daylight", one hour ahead
    (``07/15/2021 01:30:00 PM`` against ``7/15/2021 2:30 PM Pacific Daylight Time``);
    both name the same instant, which this function returns.
    """
    create_match = _ARCHIVE_DT_RE.match(image_create_datetime or "")
    if not create_match:
        raise ValueError(f"unrecognised ImageCreateDateTime {image_create_datetime!r}")
    label_match = _LOCAL_STRING_RE.match(local_time_string or "")
    if not label_match:
        raise ValueError(f"unrecognised LocalTimeString {local_time_string!r}")
    wall = _parse_us_clock(create_match)
    label_wall = _parse_us_clock(label_match)
    region, std_offset, is_daylight, zones = _zone_name_info(label_match.group(8))
    if wall.replace(second=0) != label_wall.replace(second=0):
        # Summer frames keep ImageCreateDateTime in local *standard* time while
        # LocalTimeString gives civil time labelled Daylight, one hour ahead. Seen for
        # every July sample of `pore` in the M0.5 archive check (2026-11-19), e.g.
        # "07/15/2021 01:30:00 PM" vs "7/15/2021 2:30 PM Pacific Daylight Time".
        # Use the civil time with its label; anything else is still an error.
        if is_daylight and (wall + timedelta(hours=1)).replace(second=0) == label_wall.replace(
            second=0
        ):
            wall = label_wall.replace(second=wall.second)
        else:
            raise ValueError(
                f"ImageCreateDateTime {image_create_datetime!r} and LocalTimeString "
                f"{local_time_string!r} disagree"
            )
    if tz_name not in zones:
        raise ValueError(f"zone name {region!r} does not match viewpoint zone {tz_name!r}")
    if is_daylight and tz_name in _NO_DST_ZONES:
        raise ValueError(f"{tz_name} keeps standard time all year; got {local_time_string!r}")

    zone = ZoneInfo(tz_name)
    if convention == "label":
        if is_daylight and not any(wall.replace(tzinfo=zone, fold=f).dst() for f in (0, 1)):
            raise ValueError(
                f"{local_time_string!r} says daylight time but {tz_name} is not in DST"
            )
        offset = timezone(timedelta(hours=std_offset + (1 if is_daylight else 0)))
        return wall.replace(tzinfo=offset).astimezone(UTC)
    if convention != "wall":
        raise ValueError(f"unknown convention {convention!r}")

    kind = _wall_kind(wall, zone)
    if kind == "gap":
        raise NonexistentTimeError(f"{wall.isoformat()} does not exist in {tz_name}")
    if kind == "fold":
        if not is_daylight:
            raise AmbiguousTimeError(
                f"{wall.isoformat()} occurs twice in {tz_name} and the label does not say which"
            )
        return wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
    return wall.replace(tzinfo=zone).astimezone(UTC)


_EXIF_RE = re.compile(r"^\s*(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})\s*$")
_EXIF_OFFSET_RE = re.compile(r"^\s*([+-])(\d{2}):(\d{2})\s*$")


def exif_local_to_utc(
    exif_datetime_str: str,
    tz_name: str,
    *,
    offset: str | None = None,
    subsec: str | None = None,
) -> tuple[datetime, frozenset[str]]:
    """Convert an EXIF ``DateTime``/``DateTimeOriginal`` local stamp to ``(utc, flags)``.

    ``exif_datetime_str`` looks like ``"2026:11:18 20:21:03"`` (trailing NULs and
    spaces are ignored). If ``offset`` (EXIF ``OffsetTimeOriginal``, e.g. ``"-08:00"``)
    is given it is used and flagged :data:`FLAG_OFFSET_FROM_EXIF`. Otherwise the wall
    time is placed in ``tz_name`` with the PRD §7.4 policy: a fold takes the earlier
    offset (:data:`FLAG_AMBIGUOUS`), a gap moves forward (:data:`FLAG_NONEXISTENT`).
    ``subsec`` (EXIF ``SubSecTimeOriginal``, e.g. ``"194"``) adds fractional seconds.
    """
    cleaned = (exif_datetime_str or "").replace("\x00", "").strip()
    match = _EXIF_RE.match(cleaned)
    if not match:
        raise ValueError(f"unrecognised EXIF datetime {exif_datetime_str!r}")
    year, month, day, hour, minute, second = (int(part) for part in match.groups())
    if year == 0:
        raise ValueError(f"EXIF datetime {exif_datetime_str!r} is unset (camera clock not set)")
    naive = datetime(year, month, day, hour, minute, second)
    if subsec:
        digits = "".join(ch for ch in subsec.replace("\x00", "").strip() if ch.isdigit())
        if digits:
            naive = naive.replace(microsecond=int((digits + "000000")[:6]))
    if offset:
        offset_match = _EXIF_OFFSET_RE.match(offset.replace("\x00", ""))
        if not offset_match:
            raise ValueError(f"unrecognised EXIF offset {offset!r}")
        sign, hours, minutes = offset_match.groups()
        delta = timedelta(hours=int(hours), minutes=int(minutes))
        tz = timezone(delta if sign == "+" else -delta)
        return naive.replace(tzinfo=tz).astimezone(UTC), frozenset({FLAG_OFFSET_FROM_EXIF})
    return localize(naive, tz_name)
