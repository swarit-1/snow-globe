"""Append-only JSON Lines logs (CLAUDE.md data layout: ``capture_log/``, ``forecast_log/``).

One event per line, compact JSON, UTF-8, ``\\n``-terminated. Every record carries a
UTC timestamp ``t`` (RFC 3339 with ``Z``, milliseconds), added here when the caller
did not set one. Gate evidence for M0.5 (PRD §16: ">= 72 h of worker and capture
logs") is read straight from these files, so writes are whole-line and locked.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterable, Iterator, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .timeutil import isoformat_z, utcnow

__all__ = [
    "append_jsonl",
    "append_jsonl_many",
    "encode_line",
    "iter_jsonl_bytes",
    "json_default",
    "make_record",
    "read_jsonl",
]


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
    text = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=json_default)
    return (text + "\n").encode("utf-8")


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
    concurrent writers on one machine never interleave. Returns the records as written.
    """
    rows = [make_record(record) for record in records]
    if not rows:
        return rows
    payload = b"".join(encode_line(row) for row in rows)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
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


def iter_jsonl_bytes(data: bytes, *, strict: bool = False) -> Iterator[dict[str, Any]]:
    """Yield records from JSONL bytes.

    A final line with no trailing newline that fails to parse is a write cut short by
    a crash; it is skipped unless ``strict``. A bad line anywhere else raises.
    """
    lines = data.split(b"\n")
    last_index = len(lines) - 1
    for index, raw in enumerate(lines):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            if index == last_index and not strict:
                return
            raise
        if not isinstance(value, dict):
            raise ValueError(f"JSONL line {index + 1} is not an object")
        yield value


def read_jsonl(path: str | os.PathLike[str], *, strict: bool = False) -> list[dict[str, Any]]:
    """Read every record from a local ``.jsonl`` file (see :func:`iter_jsonl_bytes`)."""
    return list(iter_jsonl_bytes(Path(path).read_bytes(), strict=strict))
