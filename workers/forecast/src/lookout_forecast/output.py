"""The per-viewpoint, per-run forecast file and its validation (PRD §8.3, §11.1).

One file per viewpoint per run at ``forecast/hrrr/<viewpoint_id>/<YYYYMMDDHH>z.json``,
written with :mod:`lookout_common.storage`. The same file feeds the app (signed URL, PRD
§11.1) and evaluation. Its JSON Schema ships as ``schema/forecast_run.schema.json``;
:func:`validate_document` adds the checks a schema cannot express (array shapes,
cross-references between ``field_names``, ``fields``, ``hours`` and ``values``).

Files are compact JSON (no indentation): a 48 h run is about 21 x 49 x 57 numbers.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator
from lookout_common.timeutil import parse_iso_utc

__all__ = [
    "ARCHIVED_SOURCES",
    "KIND",
    "SCHEMA_VERSION",
    "DocumentError",
    "encode_document",
    "load_schema",
    "validate_document",
]

#: Still 1 in 0.2.x, so readers that accept only 1 (eval's ``forecast_io``) keep reading the
#: files. Live files written by 0.2.x are valid under the 0.1.0 schema; archived ones are
#: not (``availability_model`` gained keys, ``availability_source`` a value). See README.
SCHEMA_VERSION = 1
KIND = "lookout.forecast-run"
#: ``availability_source`` values of archived hours (``lookout_forecast.availability``).
ARCHIVED_SOURCES: tuple[str, ...] = ("modeled", "archived-s3-lastmodified")


class DocumentError(ValueError):
    """A forecast file does not match the schema or its own shape rules."""


@cache
def load_schema() -> dict[str, Any]:
    """The packaged JSON Schema (Draft 2020-12) for forecast files."""
    text = resources.files("lookout_forecast").joinpath("schema/forecast_run.schema.json")
    schema: dict[str, Any] = json.loads(text.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


def validate_document(doc: Mapping[str, Any]) -> None:
    """Raise :class:`DocumentError` unless ``doc`` is a well-formed forecast file."""
    validator = Draft202012Validator(load_schema())
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    if errors:
        lines = [
            f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in errors[:10]
        ]
        raise DocumentError("schema: " + "; ".join(lines))
    names = list(doc["field_names"])
    hours = list(doc["forecast_hours"])
    points = list(doc["points"])
    problems: list[str] = []
    if set(doc["fields"]) != set(names):
        problems.append("fields keys differ from field_names")
    if [hour["fh"] for hour in doc["hours"]] != hours:
        problems.append("hours[].fh differ from forecast_hours")
    if hours != sorted(hours):
        problems.append("forecast_hours are not ascending")
    if set(hours) & set(doc["missing_hours"]):
        problems.append("an hour is both present and missing")
    if len(doc["values"]) != len(names):
        problems.append(f"values has {len(doc['values'])} fields, field_names has {len(names)}")
    for f_index, per_field in enumerate(doc["values"]):
        if len(per_field) != len(hours):
            problems.append(f"values[{f_index}] has {len(per_field)} hours, expected {len(hours)}")
            continue
        for h_index, per_hour in enumerate(per_field):
            if len(per_hour) != len(points):
                problems.append(
                    f"values[{f_index}][{h_index}] has {len(per_hour)} points, "
                    f"expected {len(points)}"
                )
    ids = [point["id"] for point in points]
    if len(ids) != len(set(ids)):
        problems.append("duplicate point ids")
    for point in points:
        cell = (point["i"], point["j"], point["cell_lat"], point["cell_lon"])
        if any(v is None for v in cell) and any(v is not None for v in cell):
            problems.append(
                f"point {point['id']}: i, j, cell_lat, cell_lon must be all set or all null"
            )
    if doc["grid"]["nx"] and points:
        for point in points:
            if point["i"] is not None and not (
                0 <= point["i"] < doc["grid"]["nx"] and 0 <= point["j"] < doc["grid"]["ny"]
            ):
                problems.append(f"point {point['id']} is outside the grid")
    for hour in doc["hours"]:
        ranges = hour["byte_ranges"]
        if sum(item["bytes"] for item in ranges) != hour["bytes_downloaded"]:
            problems.append(f"f{hour['fh']:02d}: byte_ranges do not add up to bytes_downloaded")
        for item in ranges:
            if item["end"] - item["start"] + 1 != item["bytes"]:
                problems.append(f"f{hour['fh']:02d}: {item['field']} range length mismatch")
        archived = hour["availability_source"] in ARCHIVED_SOURCES
        if archived != (hour["availability_model"] is not None):
            problems.append(
                f"f{hour['fh']:02d}: availability_model must be set exactly when the "
                f"availability is archived ({' or '.join(ARCHIVED_SOURCES)})"
            )
        floor = (hour["availability_model"] or {}).get("s3_floor_utc")
        if floor is not None and parse_iso_utc(hour["availability_utc"]) < parse_iso_utc(floor):
            problems.append(
                f"f{hour['fh']:02d}: availability_utc is before the hour's own S3 stamps "
                f"(s3_floor_utc {floor})"
            )
        if hour["availability_source"] == "archived-s3-lastmodified" and floor is None:
            problems.append(f"f{hour['fh']:02d}: archived-s3-lastmodified needs s3_floor_utc")
    if problems:
        raise DocumentError("; ".join(problems[:10]))


def encode_document(doc: Mapping[str, Any]) -> bytes:
    """Compact UTF-8 JSON; NaN or infinity anywhere is an error, never written."""
    return (
        json.dumps(doc, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()
