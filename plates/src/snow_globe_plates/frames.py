"""Captured frames as plate inputs, behind the committed held-out split (PRD §8.5 step 1, §13.9).

A plate is a fitted asset (PRD §13.9: "every fitted asset comes only from development frames
that precede the held-out season: plates, haze profiles, clear-day references, …"), so every
input frame must be one the committed split lets a **fit** use. The rule is not reproduced
here: :func:`frame_input` asks snow-globe-eval's
:func:`~snow_globe_eval.leakage.frame_file_verdict`, the one implementation of the split
(``heldout_periods_v2.json``), and requires both of its answers:

- ``development``: not in any held-out period of the viewpoint (design P's sealed window,
  15 Dec 2026 – 14 Jan 2027 extended by any outage the split records; design H's summer 2026
  season where the viewpoint has one), not an archived frame dated 1 Jun 2026 or later by any
  reading of its date (D-019, D-047, D-070), and not under the quarantine folder (D-022, in any
  letter case, through links, and by folder identity);
- ``fit``: imaged before the viewpoint's fit bound, the start of its earliest held-out period
  (2026-12-15T08:00Z at Rainier; 2026-06-01T07:00Z at Point Reyes, whose design-H season
  comes first).

The verdict reads only the key and the sidecar, never pixels. On top of it, a frame whose
sidecar time and key stamp differ by more than :data:`MAX_KEY_TIME_SKEW_S` is refused, so a
wrong sidecar time cannot move a frame across a bound. The key layout and sidecars are
snow-globe-capture's (CLAUDE.md "Data layout").
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from snow_globe_eval.heldout import path_quarantine_reason
from snow_globe_eval.leakage import frame_file_verdict

__all__ = [
    "MAX_KEY_TIME_SKEW_S",
    "FrameInput",
    "HeldOutFrameError",
    "frame_input",
    "sha256_file",
]

#: A key's stamp is the image time truncated to whole seconds.
MAX_KEY_TIME_SKEW_S = 2.0
_NAME = re.compile(r"^(?P<stamp>\d{8}T\d{6}Z)_[0-9a-f]{8,64}\.(?:jpg|jpeg|png)$")


class HeldOutFrameError(RuntimeError):
    """A frame the plate may not use (held out, after the fit bound, quarantined, unplaceable)."""


def sha256_file(path: Path) -> str:
    """Hex SHA-256 of a file's bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class FrameInput:
    """A development frame a fit may use: its key, file, sidecar, image time and verdict."""

    key: str
    path: Path
    sidecar: dict[str, Any]
    image_time: datetime
    verdict: dict[str, Any]

    @property
    def recorded_sha256(self) -> str | None:
        """The SHA-256 capture recorded for the stored file."""
        return self.sidecar.get("stored_sha256") or self.sidecar.get("sha256")


def frame_input(root: Path, key: str, time_zone: str | None = None) -> FrameInput:
    """Place a frame key under ``root`` and check that a fit may use it (module docstring).

    Raises :class:`HeldOutFrameError` for a quarantined, held-out, post-fit-bound, unplaceable
    or malformed frame, and ``FileNotFoundError`` when the file is missing. ``time_zone`` is
    accepted for older callers and ignored: the split knows each viewpoint's zone.
    """
    del time_zone
    parts = key.split("/")
    if len(parts) != 4 or parts[0] not in ("frames", "archive"):
        raise HeldOutFrameError(f"{key}: not a frames/ or archive/ key")
    match = _NAME.match(parts[3])
    if not match:
        raise HeldOutFrameError(f"{key}: file name is not <YYYYMMDDTHHMMSSZ>_<hex>.jpg")
    path = root / key
    quarantined = path_quarantine_reason(path, roots=(root,))
    if quarantined is not None:
        raise HeldOutFrameError(f"{key}: {quarantined} (D-022)")
    if not path.is_file():
        raise FileNotFoundError(path)
    verdict = frame_file_verdict(path, data_root=root)
    if not verdict["development"]:
        raise HeldOutFrameError(f"{key}: not a development frame: {'; '.join(verdict['reasons'])}")
    if not verdict["fit"]:
        raise HeldOutFrameError(f"{key}: not for fits: {'; '.join(verdict['reasons'])}")
    sidecar = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    image_time = datetime.fromisoformat(str(verdict["image_time_utc"]).replace("Z", "+00:00"))
    stamp = datetime.strptime(match["stamp"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    skew = abs((image_time - stamp).total_seconds())
    if skew > MAX_KEY_TIME_SKEW_S:
        raise HeldOutFrameError(f"{key}: sidecar image time is {skew:.0f} s from the key's stamp")
    return FrameInput(key=key, path=path, sidecar=sidecar, image_time=image_time, verdict=verdict)
