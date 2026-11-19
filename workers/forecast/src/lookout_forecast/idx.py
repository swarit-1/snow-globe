"""GRIB2 ``.idx`` inventories: parse them and turn field names into byte ranges (PRD §8.3).

An HRRR ``.idx`` file has one line per GRIB message, in file order::

    121:89734548:d=2026111906:HGT:cloud base:6 hour fcst:

that is message number, byte offset, reference date, variable, level and the forecast
description. A message ends one byte before the next message starts; the last message
ends at the end of the file (its size comes from the bucket listing).

Fields are named ``VAR:LEVEL`` exactly as in the inventory (``"HGT:cloud base"``).
Every field Lookout reads is instantaneous, so a name that matches several lines is
resolved to the one whose forecast description is ``anl`` or ``N hour fcst``; anything
still ambiguous is an error rather than a guess.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

__all__ = [
    "INSTANTANEOUS_RE",
    "IdxEntry",
    "IdxError",
    "MessageRange",
    "RangeGroup",
    "group_contiguous",
    "message_ranges",
    "parse_idx",
    "select_entry",
]

#: Forecast descriptions of instantaneous fields: ``anl`` at f00, ``N hour fcst`` after.
INSTANTANEOUS_RE = re.compile(r"^(anl|\d+ hour fcst|\d+ min fcst)$")


class IdxError(ValueError):
    """An inventory is malformed, or a field is missing or ambiguous in it."""


@dataclass(frozen=True, slots=True)
class IdxEntry:
    """One inventory line."""

    message: int
    offset: int
    date: str
    variable: str
    level: str
    forecast: str
    line: str

    @property
    def name(self) -> str:
        """The field name used everywhere in Lookout: ``VAR:LEVEL``."""
        return f"{self.variable}:{self.level}"


@dataclass(frozen=True, slots=True)
class MessageRange:
    """The bytes of one GRIB message: ``start`` and ``end`` are inclusive offsets."""

    field: str
    message: int
    start: int
    end: int
    idx_line: str

    @property
    def length(self) -> int:
        """Number of bytes in the message."""
        return self.end - self.start + 1


@dataclass(frozen=True, slots=True)
class RangeGroup:
    """Adjacent messages fetched with one ``Range`` request (S3 serves one range per GET)."""

    start: int
    end: int
    messages: tuple[MessageRange, ...]

    @property
    def length(self) -> int:
        """Number of bytes the request returns."""
        return self.end - self.start + 1

    @property
    def header(self) -> str:
        """The HTTP ``Range`` header value for this group."""
        return f"bytes={self.start}-{self.end}"


def parse_idx(text: str) -> list[IdxEntry]:
    """Parse inventory text into entries, checking that offsets strictly increase."""
    entries: list[IdxEntry] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        parts = line.split(":")
        if len(parts) < 6:
            raise IdxError(f"idx line {number} has {len(parts)} fields: {line!r}")
        try:
            message = int(parts[0])
            offset = int(parts[1])
        except ValueError as exc:
            raise IdxError(
                f"idx line {number} has a bad message number or offset: {line!r}"
            ) from exc
        if entries and offset <= entries[-1].offset:
            raise IdxError(f"idx offsets do not increase at line {number}: {line!r}")
        entries.append(
            IdxEntry(
                message=message,
                offset=offset,
                date=parts[2],
                variable=parts[3],
                level=parts[4],
                forecast=parts[5],
                line=line,
            )
        )
    if not entries:
        raise IdxError("empty idx file")
    if entries[0].offset != 0:
        raise IdxError(f"first idx message starts at byte {entries[0].offset}, expected 0")
    return entries


def select_entry(entries: Sequence[IdxEntry], name: str) -> IdxEntry:
    """Return the single instantaneous entry called ``name`` (``VAR:LEVEL``).

    Raises :class:`IdxError` when the field is absent, or when more than one
    instantaneous message carries the name.
    """
    matches = [entry for entry in entries if entry.name == name]
    if not matches:
        raise IdxError(f"field {name!r} is not in the inventory")
    if len(matches) > 1:
        matches = [entry for entry in matches if INSTANTANEOUS_RE.match(entry.forecast)]
    if len(matches) != 1:
        lines = "; ".join(entry.line for entry in matches) or "none instantaneous"
        raise IdxError(f"field {name!r} is ambiguous in the inventory: {lines}")
    return matches[0]


def message_ranges(
    entries: Sequence[IdxEntry], names: Iterable[str], *, file_size: int | None
) -> list[MessageRange]:
    """Byte ranges of the named fields, in file order.

    ``file_size`` (from the bucket listing) closes the last message of the file; without
    it, asking for the last message raises :class:`IdxError`.
    """
    position = {id(entry): index for index, entry in enumerate(entries)}
    ranges: list[MessageRange] = []
    for name in dict.fromkeys(names):  # de-duplicate, keep order
        entry = select_entry(entries, name)
        index = position[id(entry)]
        if index + 1 < len(entries):
            end = entries[index + 1].offset - 1
        elif file_size is not None:
            end = file_size - 1
        else:
            raise IdxError(f"{name!r} is the last message and the file size is unknown")
        if end < entry.offset:
            raise IdxError(f"{name!r} has an empty byte range")
        ranges.append(MessageRange(name, entry.message, entry.offset, end, entry.line))
    return sorted(ranges, key=lambda item: item.start)


def group_contiguous(ranges: Iterable[MessageRange]) -> list[RangeGroup]:
    """Merge messages that sit back to back in the file into single requests.

    Only exact neighbours merge (no gap bytes are downloaded), so the bytes fetched are
    exactly the sum of the message lengths.
    """
    groups: list[RangeGroup] = []
    current: list[MessageRange] = []
    for item in sorted(ranges, key=lambda r: r.start):
        if current:
            if item.start <= current[-1].end:
                raise IdxError(f"overlapping message ranges at byte {item.start}")
            if item.start != current[-1].end + 1:
                groups.append(RangeGroup(current[0].start, current[-1].end, tuple(current)))
                current = []
        current.append(item)
    if current:
        groups.append(RangeGroup(current[0].start, current[-1].end, tuple(current)))
    return groups
