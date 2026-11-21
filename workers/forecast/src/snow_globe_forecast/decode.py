"""Decode GRIB2 subsets with ecCodes and read values at the sample cells (PRD §8.3).

A subset file holds the requested messages back to back, in inventory order. Each
message is checked before its values are used:

- its length equals the inventory byte range it came from;
- its product identity (discipline, category, number, first fixed surface, level)
  matches the catalogue entry for the field name;
- its reference time is the run start and its forecast time is the hour asked for;
- its grid is exactly the grid the sample points were mapped on.

Missing values come from the GRIB2 bitmap (HRRR writes "no cloud" as missing for the
cloud base, ceiling and top), never from a sentinel number, and become ``None``.
Values are rounded to 6 significant digits, finer than any HRRR field's packing.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import eccodes
import numpy as np
import numpy.typing as npt

from .fields import FieldSpec
from .grid import GRID_KEYS, LambertConformalGrid
from .idx import MessageRange

__all__ = [
    "DecodeError",
    "DecodedMessage",
    "decode_subset",
    "ecc_version",
    "iter_handles",
    "message_metadata",
    "round_sig",
    "values_and_mask",
]

SIGNIFICANT_DIGITS = 6
_META_KEYS: tuple[str, ...] = (
    "shortName",
    "name",
    "units",
    "typeOfLevel",
    "centre",
    "packingType",
    "md5GridSection",
)
_INT_KEYS: tuple[str, ...] = (
    "discipline",
    "parameterCategory",
    "parameterNumber",
    "typeOfFirstFixedSurface",
    "level",
    "dataDate",
    "dataTime",
    "forecastTime",
    "indicatorOfUnitOfTimeRange",
    "productDefinitionTemplateNumber",
    "generatingProcessIdentifier",
    "bitmapPresent",
    "numberOfMissing",
    "numberOfValues",
    "numberOfDataPoints",
    "totalLength",
    "bitsPerValue",
    "decimalScaleFactor",
    "binaryScaleFactor",
    "resolutionAndComponentFlags",
)
_HOURS_PER_UNIT = {0: 1 / 60, 1: 1.0, 2: 24.0}


class DecodeError(ValueError):
    """A GRIB message failed a check (identity, time, length or grid)."""


def ecc_version() -> str:
    """The ecCodes C library version in use."""
    return str(eccodes.codes_get_api_version())


def round_sig(value: float, digits: int = SIGNIFICANT_DIGITS) -> float:
    """Round to ``digits`` significant digits (0 stays 0)."""
    if value == 0 or not math.isfinite(value):
        return value
    return round(value, digits - 1 - math.floor(math.log10(abs(value))))


@contextmanager
def _handle_scope(gid: Any) -> Iterator[Any]:
    try:
        yield gid
    finally:
        eccodes.codes_release(gid)


def iter_handles(path: Path) -> Iterator[Any]:
    """Yield ecCodes handles for every GRIB message in a file (each released after use)."""
    with path.open("rb") as handle:
        while True:
            gid = eccodes.codes_grib_new_from_file(handle)
            if gid is None:
                return
            with _handle_scope(gid):
                yield gid


def _get(gid: Any, key: str, ktype: type | None = None) -> Any:
    try:
        if ktype is None:
            return eccodes.codes_get(gid, key)
        return eccodes.codes_get(gid, key, ktype=ktype)
    except eccodes.KeyValueNotFoundError:
        return None


def message_metadata(gid: Any) -> dict[str, Any]:
    """The keys Snow Globe records or checks for one message (plus the grid keys)."""
    meta: dict[str, Any] = {key: _get(gid, key) for key in _META_KEYS}
    meta.update({key: _get(gid, key, int) for key in _INT_KEYS})
    meta["grid"] = {key: _get(gid, key) for key in GRID_KEYS}
    flags = meta.get("resolutionAndComponentFlags")
    meta["uvRelativeToGrid"] = None if flags is None else int(bool(flags & 0b1000))
    return meta


def values_and_mask(gid: Any) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.bool_]]:
    """All decoded values and a mask that is True where the bitmap says "missing"."""
    values = np.asarray(eccodes.codes_get_values(gid), dtype=np.float64)
    if _get(gid, "bitmapPresent", int):
        bitmap = np.asarray(eccodes.codes_get_array(gid, "bitmap", int), dtype=np.int8)
        if bitmap.shape != values.shape:
            raise DecodeError("bitmap and values differ in length")
        missing = bitmap == 0
    else:
        missing = np.zeros(values.shape, dtype=bool)
    return values, missing


@dataclass(frozen=True)
class DecodedMessage:
    """One checked message: its metadata and its values at the requested cells."""

    field: str
    meta: Mapping[str, Any]
    values: list[float | None]
    full_missing: int

    @property
    def missing_at_points(self) -> int:
        """How many requested points are missing (bitmap) in this message."""
        return sum(value is None for value in self.values)


def _check_identity(meta: Mapping[str, Any], spec: FieldSpec) -> None:
    got = (
        meta["discipline"],
        meta["parameterCategory"],
        meta["parameterNumber"],
        meta["typeOfFirstFixedSurface"],
        meta["level"],
    )
    want = (spec.discipline, spec.category, spec.number, spec.level_type, spec.level_value)
    if got != want:
        raise DecodeError(
            f"{spec.name}: GRIB identity {got} does not match the catalogue {want} "
            "(discipline, category, number, typeOfFirstFixedSurface, level)"
        )


def _check_time(meta: Mapping[str, Any], name: str, run_start: datetime, fh: int) -> None:
    want_date, want_time = int(run_start.strftime("%Y%m%d")), run_start.hour * 100
    if (meta["dataDate"], meta["dataTime"]) != (want_date, want_time):
        raise DecodeError(
            f"{name}: reference time {meta['dataDate']} {meta['dataTime']:04d} is not the run "
            f"start {want_date} {want_time:04d}"
        )
    unit = _HOURS_PER_UNIT.get(meta["indicatorOfUnitOfTimeRange"])
    if unit is None or abs(meta["forecastTime"] * unit - fh) > 1e-9:
        raise DecodeError(
            f"{name}: forecast time {meta['forecastTime']} (unit "
            f"{meta['indicatorOfUnitOfTimeRange']}) is not f{fh:02d}"
        )


def decode_subset(
    path: Path,
    ranges: Sequence[MessageRange],
    *,
    specs: Mapping[str, FieldSpec],
    grid: LambertConformalGrid,
    run_start: datetime,
    fh: int,
    flat: Sequence[int | None],
    digits: int = SIGNIFICANT_DIGITS,
) -> list[DecodedMessage]:
    """Decode a subset written in ``ranges`` order and pick the values at ``flat`` indices.

    ``flat[k]`` is the value index of point ``k`` (None for a point outside the grid).
    """
    ordered = sorted(ranges, key=lambda item: item.start)
    decoded: list[DecodedMessage] = []
    handles = iter_handles(path)
    try:
        for expected in ordered:
            gid = next(handles, None)
            if gid is None:
                raise DecodeError(f"subset {path.name} ends before {expected.field}")
            meta = message_metadata(gid)
            if meta["totalLength"] != expected.length:
                raise DecodeError(
                    f"{expected.field}: message is {meta['totalLength']} bytes, the inventory "
                    f"range is {expected.length}"
                )
            spec = specs[expected.field]
            _check_identity(meta, spec)
            _check_time(meta, expected.field, run_start, fh)
            grid.check_same(meta["grid"])
            if meta["numberOfDataPoints"] != grid.nx * grid.ny:
                raise DecodeError(
                    f"{expected.field}: {meta['numberOfDataPoints']} points on the grid"
                )
            values, missing = values_and_mask(gid)
            picked: list[float | None] = []
            for index in flat:
                if index is None or missing[index] or not math.isfinite(values[index]):
                    picked.append(None)
                else:
                    picked.append(round_sig(float(values[index]), digits))
            decoded.append(DecodedMessage(expected.field, meta, picked, int(missing.sum())))
        if next(handles, None) is not None:
            raise DecodeError(f"subset {path.name} holds more messages than were requested")
    finally:
        handles.close()
    return decoded
