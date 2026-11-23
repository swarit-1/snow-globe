"""Hand labels of development frames, made by viewing (PRD §13.8 "Labels").

PRD §13.8 asks for labelled real frames, stratified by time of day and weather, before any
extractor number is published. A labels file records, per frame:

- its key and SHA-256 (so a label can never drift onto another image);
- per landmark band (summit, upper, mid, near at Rainier): ``visible``, ``partial`` or
  ``hidden``, under the protocol written in the file;
- ``camera_in_cloud`` (the camera itself in fog: near trees faded), ``sky``, ``light``, and
  ``clear_day_reference`` (every band visible in direct sunlight: the pool that extractor v0
  draws its illumination-conditioned references from, :mod:`.extractor_v0`).

**Leakage** (PRD §13.1: "held-out frames are absent from the labelling store"): a file that
labels any frame the committed split holds out is refused whole, and so is a file under the
quarantine folder (D-022). Each label is placed twice, by its ``image_time_utc`` and by the
time stamped in its key, and the split is asked about both; a label whose two times differ by
more than :data:`MAX_KEY_TIME_SKEW_S` is refused too, so a copy-paste error in one field
cannot carry a held-out frame past the guard.

Two Rainier files sit beside this module, both for the 105 daytime captures of 19-22 Nov 2026
(Sun ≥ 3°):

- ``labels_rainier_v0.json``, the first pass: every band labelled by the builder of extractor
  v0 on 23 Nov 2026 by viewing crops of each frame, before any v0 ratio was computed, with the
  bands defined by landmarks (the plate did not exist yet);
- ``labels_rainier_v0_1.json`` (the default, :data:`LABELS_RAINIER`): the same summit labels,
  and upper, mid and near labelled again inside the plate v0.1 band outlines, the regions the
  extractor scores. That re-label was made after a v0 run and its reviews existed (the file's
  protocol says so, and keeps each frame's first-pass labels).

One labeller at a time, no re-label a week later, no second labeller, no labelled renders yet:
TODO(M2), PRD §13.8. A file records those in its protocol (``relabel_pass``,
``second_labeller``, ``labelled_renders``), which :mod:`.extractor_v0_run` reads.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from snow_globe_common.timeutil import parse_iso_utc

from .frames import FrameRecord, parse_frame_key
from .heldout import LeakageError, committed_split, path_quarantine_reason

__all__ = [
    "LABELS_KIND",
    "LABELS_RAINIER",
    "LABELS_RAINIER_V0",
    "MAX_KEY_TIME_SKEW_S",
    "STATES",
    "FrameLabel",
    "LabelSet",
    "load_labels",
]

LABELS_KIND = "snow-globe.frame-labels"
LABELS_RAINIER_V0 = Path(__file__).with_name("labels_rainier_v0.json")
#: The labels the extractor is validated on by default: summit from the first pass, upper, mid
#: and near re-labelled inside the plate v0.1 band outlines.
LABELS_RAINIER = Path(__file__).with_name("labels_rainier_v0_1.json")
STATES = ("visible", "partial", "hidden")
#: The key's stamp is the image time truncated to whole seconds (CLAUDE.md "Data layout").
MAX_KEY_TIME_SKEW_S = 2.0


@dataclass(frozen=True)
class FrameLabel:
    """One frame's labels."""

    key: str
    sha256: str
    image_time_utc: str
    bands: dict[str, str]
    camera_in_cloud: bool
    sky: str
    light: str | None
    clear_day_reference: bool
    note: str = ""
    first_pass: dict[str, str] | None = None


@dataclass(frozen=True)
class LabelSet:
    """A labels file: protocol, bands, frames by key, and the file's own SHA-256."""

    viewpoint_id: str
    version: str
    protocol: dict[str, Any]
    bands: tuple[str, ...]
    frames: dict[str, FrameLabel]
    source: str
    sha256: str

    @property
    def clear_day_keys(self) -> list[str]:
        """Keys labelled clear day (the reference pool)."""
        return sorted(k for k, f in self.frames.items() if f.clear_day_reference)

    def to_record(self) -> dict[str, Any]:
        """Provenance for result records."""
        return {
            "source": self.source,
            "sha256": self.sha256,
            "version": self.version,
            "frames": len(self.frames),
            "labeller": self.protocol.get("labeller"),
        }


def load_labels(
    path: str | Path | None = None, *, roots: Iterable[str | Path | None] = ()
) -> LabelSet:
    """Read a labels file (default: Rainier v0.1), refusing held-out and quarantined frames."""
    file_path = Path(path).expanduser() if path is not None else LABELS_RAINIER
    reason = path_quarantine_reason(file_path, roots=roots)
    if reason is not None:
        raise LeakageError(f"labels {file_path}: {reason}")
    data = file_path.read_bytes()
    doc = json.loads(data)
    if doc.get("kind") != LABELS_KIND:
        raise ValueError(f"{file_path}: not a {LABELS_KIND} file")
    bands = tuple(doc["bands"])
    split = committed_split()
    frames: dict[str, FrameLabel] = {}
    for item in doc["frames"]:
        key = item["key"]
        parsed = parse_frame_key(key)
        if parsed.viewpoint_id != doc["viewpoint_id"]:
            raise ValueError(f"{key}: not a frame of {doc['viewpoint_id']}")
        if key in frames:
            raise ValueError(f"{key} is labelled twice")
        labelled_time = parse_iso_utc(item["image_time_utc"])
        source = "nps-archive" if parsed.kind == "archive" else "live"
        for moment in (labelled_time, parsed.image_time):
            record = FrameRecord(
                viewpoint_id=parsed.viewpoint_id, image_time=moment, key=key, source=source
            )
            held = split.held_out_reason(record)
            if held is not None:
                raise LeakageError(f"{file_path}: labels a held-out frame {key} ({held})")
        skew = abs((labelled_time - parsed.image_time).total_seconds())
        if skew > MAX_KEY_TIME_SKEW_S:
            raise ValueError(
                f"{key}: image_time_utc {item['image_time_utc']} is {skew:.0f} s from the "
                "time in the key"
            )
        states = item["bands"]
        if set(states) != set(bands) or any(s not in STATES for s in states.values()):
            raise ValueError(f"{key}: bands must be {bands} with states {STATES}")
        frames[key] = FrameLabel(
            key=key,
            sha256=item["sha256"],
            image_time_utc=item["image_time_utc"],
            bands=dict(states),
            camera_in_cloud=bool(item["camera_in_cloud"]),
            sky=str(item["sky"]),
            light=item.get("light"),
            clear_day_reference=bool(item["clear_day_reference"]),
            note=str(item.get("note", "")),
            first_pass=dict(item["first_pass"]) if item.get("first_pass") else None,
        )
    return LabelSet(
        viewpoint_id=doc["viewpoint_id"],
        version=str(doc["version"]),
        protocol=dict(doc["protocol"]),
        bands=bands,
        frames=frames,
        source=str(file_path),
        sha256=hashlib.sha256(data).hexdigest(),
    )
