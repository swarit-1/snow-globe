"""v−1 landmark boxes: by-eye stand-ins for landmark projections (PRD §13.3, §16 M0.5).

PRD §13.3 scores landmark visibility inside each landmark's image region. Those regions come
from the pose solve and the plate's landmark-band masks (§8.5), which arrive in M2. For the
M0.5 backtest pair, boxes are drawn by eye on the plate frame and committed in
``landmark_boxes_v1.json``, in normalised image coordinates (0–1, origin top left). They are
**not** in the viewpoint manifest, which keeps surveyed landmarks only (TODO(M2): the pose
solve replaces this file).

A box becomes an extractor :class:`~.extractor.Region` with ``frame_size = (1, 1)``: the
extractor scales region coordinates by the analysis grid over the frame size, so normalised
coordinates fit a frame of any *resolution*, but only of the aspect ratio of the frame they
were drawn on (``drawn_on.size``). A 3:2 box on a 16:9 frame of the same camera covers other
terrain (D-024), so the backtest pair refuses frames of another aspect ratio.

A boxes file under the quarantine folder (D-022) is refused before it is read.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .extractor import Region
from .heldout import LeakageError, path_quarantine_reason

__all__ = [
    "BOXES_KIND",
    "BOXES_V1",
    "LandmarkBox",
    "ViewpointBoxes",
    "load_boxes",
]

BOXES_KIND = "lookout.landmark-boxes"
#: The committed v−1 boxes.
BOXES_V1 = Path(__file__).with_name("landmark_boxes_v1.json")


@dataclass(frozen=True, slots=True)
class LandmarkBox:
    """One named box ``(x0, y0, x1, y1)`` in normalised image coordinates."""

    name: str
    box: tuple[float, float, float, float]
    what: str = ""

    def __post_init__(self) -> None:
        if (
            not self.name
            or not self.name.replace("-", "").isalnum()
            or self.name != self.name.lower()
        ):
            raise ValueError(f"box name {self.name!r} must be lower-case letters, digits and '-'")
        if len(self.box) != 4 or not all(math.isfinite(v) for v in self.box):
            raise ValueError(f"box {self.name}: four finite numbers expected")
        x0, y0, x1, y1 = self.box
        if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
            raise ValueError(f"box {self.name}: need 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1")

    def region(self) -> Region:
        """The extractor region (normalised: ``frame_size`` is ``(1, 1)``)."""
        return Region.box(*self.box, frame_size=(1, 1))

    def shifted(self, dx: float, dy: float) -> LandmarkBox:
        """The box moved by ``(dx, dy)`` (normalised units), clipped to the frame."""
        x0, y0, x1, y1 = self.box
        moved = (
            min(max(x0 + dx, 0.0), 1.0),
            min(max(y0 + dy, 0.0), 1.0),
            min(max(x1 + dx, 0.0), 1.0),
            min(max(y1 + dy, 0.0), 1.0),
        )
        return LandmarkBox(self.name, moved, self.what)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {"name": self.name, "box": list(self.box), "what": self.what}


@dataclass(frozen=True)
class ViewpointBoxes:
    """The boxes of one viewpoint, the primary box's name and where they came from."""

    viewpoint_id: str
    version: str
    primary: str
    boxes: tuple[LandmarkBox, ...]
    drawn_on: dict[str, Any]
    source: str
    source_sha256: str

    def __post_init__(self) -> None:
        names = [b.name for b in self.boxes]
        if not names:
            raise ValueError(f"{self.viewpoint_id}: no boxes")
        if len(set(names)) != len(names):
            raise ValueError(f"{self.viewpoint_id}: box names repeat: {names}")
        if self.primary not in names:
            raise ValueError(f"{self.viewpoint_id}: primary box {self.primary!r} is not defined")

    def box(self, name: str) -> LandmarkBox:
        """A box by name."""
        for item in self.boxes:
            if item.name == name:
                return item
        raise KeyError(f"{self.viewpoint_id} has no box {name!r}")

    @property
    def primary_box(self) -> LandmarkBox:
        """The box the pair's headline score uses."""
        return self.box(self.primary)

    @property
    def others(self) -> tuple[LandmarkBox, ...]:
        """Every box except the primary, in file order."""
        return tuple(b for b in self.boxes if b.name != self.primary)

    def to_record(self) -> dict[str, Any]:
        """Provenance for a pair record: file, hash, version and the boxes."""
        return {
            "source": self.source,
            "sha256": self.source_sha256,
            "version": self.version,
            "primary": self.primary,
            "drawn_on": self.drawn_on,
            "boxes": [b.to_dict() for b in self.boxes],
        }


def load_boxes(
    viewpoint_id: str,
    path: Path | None = None,
    *,
    roots: Iterable[str | os.PathLike[str] | None] = (),
) -> ViewpointBoxes:
    """Read one viewpoint's boxes from ``path`` (default: the committed v−1 file).

    A file under the quarantine folder of any of ``roots``, ``$LOOKOUT_DATA_DIR`` or
    ``~/lookout-data`` raises :class:`~.heldout.LeakageError` before it is read (D-022).
    Raises ``KeyError`` for a viewpoint the file does not cover and ``ValueError`` for a
    malformed file.
    """
    file_path = Path(path).expanduser() if path is not None else BOXES_V1
    quarantined = path_quarantine_reason(file_path, roots=roots)
    if quarantined is not None:
        raise LeakageError(f"landmark boxes {file_path}: {quarantined}")
    data = file_path.read_bytes()
    doc = json.loads(data)
    if doc.get("kind") != BOXES_KIND:
        raise ValueError(f"{file_path}: not a {BOXES_KIND} file")
    entry = (doc.get("viewpoints") or {}).get(viewpoint_id)
    if entry is None:
        raise KeyError(f"{file_path} has no boxes for {viewpoint_id}")
    boxes = tuple(
        LandmarkBox(
            str(b["name"]),
            tuple(float(v) for v in b["box"]),  # type: ignore[arg-type]
            str(b.get("what", "")),
        )
        for b in entry.get("boxes", [])
    )
    return ViewpointBoxes(
        viewpoint_id=viewpoint_id,
        version=str(doc.get("version")),
        primary=str(entry.get("primary")),
        boxes=boxes,
        drawn_on=dict(entry.get("drawn_on") or {}),
        source=str(file_path),
        source_sha256=hashlib.sha256(data).hexdigest(),
    )
