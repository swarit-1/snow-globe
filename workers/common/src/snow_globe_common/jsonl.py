"""Append-only JSON Lines logs (CLAUDE.md data layout: ``capture_log/``, ``forecast_log/``).

One event per line, compact JSON, UTF-8, ``\\n``-terminated. Every record carries a
UTC timestamp ``t`` (RFC 3339 with ``Z``, milliseconds), added here when the caller
did not set one. Gate evidence for M0.5 (PRD §16: ">= 72 h of worker and capture
logs") is read straight from these files, so writes are whole-line and locked, and one
damaged line never makes the rest of a log unreadable:

- A write cut short (a crash, power loss, a full disk) leaves a fragment with no
  newline. The next writer sees that the file does not end in ``\\n`` and starts its
  records on a new line, so the fragment never swallows them.
- A reader skips any line that is not a JSON object, wherever it is, and reports it
  through ``bad_lines``. ``strict=True`` raises instead, for audits.

Non-finite floats (NaN, ±Infinity) are written as ``null``, so every line is valid
RFC 8259 JSON for jq, JavaScript and serde as well as for Python.
"""

from __future__ import annotations

import fcntl
import json
import logging
import math
import os
from collections.abc import Iterable, Iterator, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any, NamedTuple

from .timeutil import isoformat_z, utcnow

__all__ = [
    "REASON_INVALID_JSON",
    "REASON_NOT_OBJECT",
    "REASON_UNTERMINATED",
    "BadLine",
    "append_jsonl",
    "append_jsonl_many",
    "dumps_json",
    "encode_line",
    "iter_jsonl_bytes",
    "json_default",
    "make_record",
    "read_jsonl",
]

log = logging.getLogger(__name__)

#: A line that is not valid JSON (or not UTF-8), with a newline after it.
REASON_INVALID_JSON = "invalid-json"
#: A line that is valid JSON but not an object, so it is not a record.
REASON_NOT_OBJECT = "not-an-object"
#: The bytes after the last newline do not parse: a write cut short, or still in progress.
REASON_UNTERMINATED = "unterminated-last-line"

_EXCERPT_BYTES = 120


class BadLine(NamedTuple):
    """One line a non-strict read skipped: 1-based line number, reason, first bytes.

    Kept so that log audits (PRD §16 gate evidence) can count damage instead of losing it.
    """

    line: int
    reason: str
    excerpt: bytes


def json_default(value: Any) -> Any:
    """``json.dumps`` hook: aware datetimes -> RFC 3339 ``Z``, dates -> ISO, paths -> str."""
    if isinstance(value, datetime):
        return isoformat_z(value)  # raises on naive datetimes, which carry no zone
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


def _finite(value: Any) -> Any:
    """Copy ``value``, replacing every non-finite float in dicts, lists and tuples by None."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(item) for item in value]
    return value


def dumps_json(obj: Any, *, indent: int | None = None) -> str:
    """Serialise ``obj`` for a Snow Globe file (PRD §8.7): valid RFC 8259 JSON, UTF-8 kept.

    Compact (``,`` and ``:`` with no spaces) when ``indent`` is None, else indented.
    Types beyond JSON go through :func:`json_default`. NaN and ±Infinity become
    ``null``, because strict parsers (jq, JavaScript, serde_json) reject them.
    """
    separators = (",", ":") if indent is None else (",", ": ")

    def dump(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=separators,
            indent=indent,
            default=json_default,
            allow_nan=False,
        )

    try:
        return dump(obj)
    except ValueError:
        # Most likely a non-finite float. Any other ValueError (a naive datetime, say)
        # is raised again by the second attempt.
        return dump(_finite(obj))


def make_record(record: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Return a copy of ``record`` with ``t`` first, set to ``now`` (default: UTC now) if absent."""
    out: dict[str, Any] = dict(record)
    stamp = out.pop("t", None)
    if stamp is None:
        stamp = isoformat_z(now or utcnow(), "milliseconds")
    elif isinstance(stamp, datetime):
        stamp = isoformat_z(stamp, "milliseconds")
    return {"t": stamp, **out}


def encode_line(record: Mapping[str, Any]) -> bytes:
    """Encode one record as a single compact JSON line (always exactly one ``\\n``)."""
    return (dumps_json(record) + "\n").encode("utf-8")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def append_jsonl_many(
    path: str | os.PathLike[str],
    records: Iterable[Mapping[str, Any]],
    *,
    fsync: bool = False,
) -> list[dict[str, Any]]:
    """Append records to a local ``.jsonl`` file under an exclusive ``flock``.

    Creates parent directories. All lines go out in one locked ``O_APPEND`` write, so
    concurrent writers on one machine never interleave. If the file does not end in a
    newline (an earlier write was cut short), the payload starts with one: the
    fragment stays a line of its own, which readers skip, and the new records stay
    readable. Returns the records as written.
    """
    rows = [make_record(record) for record in records]
    if not rows:
        return rows
    payload = b"".join(encode_line(row) for row in rows)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":
                payload = b"\n" + payload  # never glue a record onto a torn line
            _write_all(fd, payload)
            if fsync:
                os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    return rows


def append_jsonl(
    path: str | os.PathLike[str], record: Mapping[str, Any], *, fsync: bool = False
) -> dict[str, Any]:
    """Append one record (adding UTC ``t`` if absent) to a local ``.jsonl`` file."""
    return append_jsonl_many(path, [record], fsync=fsync)[0]


def iter_jsonl_bytes(
    data: bytes, *, strict: bool = False, bad_lines: list[BadLine] | None = None
) -> Iterator[dict[str, Any]]:
    """Yield the records (JSON objects) in JSONL bytes, in order; blank lines are ignored.

    Non-strict (the default): a line that is not valid UTF-8 JSON, or not an object,
    is skipped wherever it is, and appended to ``bad_lines`` when a list is given.
    Such a line is normally a write cut short by a crash or a full disk. Skipped lines
    are also logged as one warning per call, except an unterminated last line on its
    own, which may be a write still in progress in another process.

    ``strict=True`` raises on the first bad line instead (``json.JSONDecodeError`` or
    ``UnicodeDecodeError`` for a line that does not parse, ``ValueError`` for one that
    is not an object). Use it for audits.
    """
    lines = data.split(b"\n")
    last_index = len(lines) - 1  # the bytes after the final newline (b"" if terminated)
    skipped: list[BadLine] = []

    def skip(index: int, reason: str, raw: bytes) -> None:
        bad = BadLine(index + 1, reason, raw[:_EXCERPT_BYTES])
        skipped.append(bad)
        if bad_lines is not None:
            bad_lines.append(bad)  # at once, so a consumer that stops early still sees it

    for index, raw in enumerate(lines):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            if strict:
                raise
            skip(index, REASON_UNTERMINATED if index == last_index else REASON_INVALID_JSON, raw)
            continue
        if not isinstance(value, dict):
            if strict:
                raise ValueError(f"JSONL line {index + 1} is not an object")
            skip(index, REASON_NOT_OBJECT, raw)
            continue
        yield value
    damaged = [bad for bad in skipped if bad.reason != REASON_UNTERMINATED]
    if damaged:
        log.warning(
            "skipped %d damaged JSONL line(s); first at line %d (%s)",
            len(skipped),
            damaged[0].line,
            damaged[0].reason,
        )


def read_jsonl(
    path: str | os.PathLike[str],
    *,
    strict: bool = False,
    bad_lines: list[BadLine] | None = None,
) -> list[dict[str, Any]]:
    """Read every record from a local ``.jsonl`` file (see :func:`iter_jsonl_bytes`)."""
    return list(iter_jsonl_bytes(Path(path).read_bytes(), strict=strict, bad_lines=bad_lines))
