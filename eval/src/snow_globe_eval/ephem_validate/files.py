"""Atomic file writes and hashes for the ephemeris validation's cache and reports.

Every cache entry and report file is written to a temporary file beside its target and
renamed into place, so a run stopped part-way (the Mac sleeping, a kill) leaves either the
old file or the new one, never a torn one; a restart then skips what is already there.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = ["atomic_write_bytes", "atomic_write_json", "atomic_write_text", "sha256_file"]


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (mode 0644), creating the folder."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, text: str) -> None:
    """Write UTF-8 ``text`` to ``path`` atomically."""
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(
    path: Path, payload: Mapping[str, Any] | list[Any], *, indent: int | None = None
) -> None:
    """Write ``payload`` as JSON to ``path`` atomically (NaN is written as null)."""
    text = json.dumps(_nan_to_none(payload), indent=indent, allow_nan=False)
    atomic_write_text(path, text + ("\n" if indent is not None else ""))


def _nan_to_none(value: Any) -> Any:
    if isinstance(value, float):
        return None if value != value or value in (float("inf"), float("-inf")) else value
    if isinstance(value, Mapping):
        return {k: _nan_to_none(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_nan_to_none(v) for v in value]
    return value


def sha256_file(path: Path) -> str:
    """SHA-256 (hex) of a file's bytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
