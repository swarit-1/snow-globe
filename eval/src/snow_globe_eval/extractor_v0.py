"""Extractor v0: per-band edge ratio against a clear-day reference matched on the Sun (PRD §13.3).

PRD §13.3: "A landmark is visible when its edge energy, compared with an illumination-
conditioned clear-day reference, clears a threshold." v0 does this per **landmark band** of
the viewpoint's plate (``plates/<viewpoint>/<version>/``: summit, upper mountain, mid-slopes,
near ridge at Rainier), where v−1 (:mod:`.extractor`) used one by-eye box.

**Energy** (the same measure as v−1): Rec. 601 luma at ``analysis_width``, a Gaussian blur of
``blur_sigma_px``, Sobel gradient magnitude divided by the local mean luma (a
``local_mean_size_px`` box) plus ``epsilon``; a band's energy is the mean over its mask. A
band mask is the plate's full-resolution PNG, area-averaged onto the analysis grid (a cell is
in the band when at least half of it is). The camera's caption strip is no band's.

**Reference** (illumination-conditioned): the pool is captured frames labelled *clear day* by
viewing (every band visible, direct sunlight; :mod:`.labels`), each with the Sun's geometric
altitude and azimuth (oculus-core, :mod:`.ephemeris`). For a scored frame the reference is the
median band energy of the ``ref_k`` pool frames nearest in Sun position with
``|Δalt| ≤ ref_max_dalt_deg`` and ``|Δaz| ≤ ref_max_daz_deg``, nearest by
``hypot(Δalt / ref_max_dalt_deg, Δaz / ref_max_daz_deg)``. Pool frames from the scored frame's
own local day are never used (``exclude_same_day``), so a frame is never its own reference
and a clear morning cannot vouch for itself; this is the rule under which the validation is
reported. No pool frame within the limits means **no usable reference**, and no class.

**Classes** (PRD §13.4), per band, from ``ratio = energy / reference energy``:

- ``visible`` if ``ratio ≥ tau_hi`` (0.60), ``hidden`` if ``ratio < tau_lo`` (0.35),
  ``partial`` in between. These are v−1's placeholder values, kept unchanged and fixed before
  any v0 ratio was computed; τ and the partial band are fitted on development data in M2
  (TODO(M2), PRD §13.4).
- Frame-level classes come first: ``night`` (Sun below 3°, by ephemeris: PRD §13.4, §8.6),
  ``camera_fault`` (v−1's rule, D-037), ``excluded`` (another PRD §8.6 gate failed, such as
  Fresh or Same view; :mod:`.extractor_v0_run` applies them), ``in_cloud`` (the whole frame's
  energy *and* RMS contrast both below 0.25 and 0.35 of the reference's: v−1's rule),
  ``no_reference``.
- **Obscuration height**: the highest band, in plate order, whose class is ``visible``.

The same :func:`band_energies` serves real and rendered frames (§13.3 "One extractor").
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import numpy as np
from numpy.typing import NDArray
from PIL import Image
from scipy import ndimage
from snow_globe_common.images import ImageSource

from .extractor import classify_ratio, luma
from .heldout import LeakageError, path_quarantine_reason

__all__ = [
    "EXTRACTOR_V0",
    "BandEnergies",
    "BandResult",
    "FrameResult",
    "PlateBands",
    "ReferenceFrameV0",
    "V0Params",
    "band_energies",
    "camera_fault",
    "classify_frame",
    "select_reference",
]

EXTRACTOR_V0 = "v0"
PLATE_KIND = "snow-globe.plate"
type FrameClass = Literal["night", "camera_fault", "excluded", "in_cloud", "no_reference", "scored"]


@dataclass(frozen=True, slots=True)
class V0Params:
    """Every number extractor v0 uses (see the module docstring)."""

    analysis_width: int = 960
    blur_sigma_px: float = 1.0
    local_mean_size_px: int = 31
    epsilon: float = 0.02
    tau_hi: float = 0.6
    tau_lo: float = 0.35
    in_cloud_energy_ratio_max: float = 0.25
    in_cloud_contrast_ratio_max: float = 0.35
    ref_max_dalt_deg: float = 3.0
    ref_max_daz_deg: float = 15.0
    ref_k: int = 3
    exclude_same_day: bool = True
    min_band_cells: int = 50
    min_sun_alt_deg: float = 3.0
    fault_dark_luma: float = 4.0 / 255.0
    fault_bright_luma: float = 251.0 / 255.0
    fault_fraction_min: float = 0.95
    fault_flat_range_max: float = 1.0 / 255.0

    def __post_init__(self) -> None:
        if not 0.0 < self.tau_lo <= self.tau_hi:
            raise ValueError("need 0 < tau_lo <= tau_hi")
        if self.ref_k < 1 or self.ref_max_dalt_deg <= 0 or self.ref_max_daz_deg <= 0:
            raise ValueError("ref_k >= 1 and positive reference limits")
        if self.analysis_width < 32 or self.local_mean_size_px < 3 or self.epsilon <= 0:
            raise ValueError("analysis_width >= 32, local_mean_size_px >= 3, epsilon > 0")

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return asdict(self)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class PlateBands:
    """A plate's landmark bands (full-resolution masks, plate order) and its identity."""

    viewpoint_id: str
    plate_id: str
    plate_json_sha256: str
    frame_size: tuple[int, int]
    order: tuple[str, ...]
    masks: Mapping[str, NDArray[np.bool_]]
    overlay: NDArray[np.bool_]
    source: str
    base_path: Path | None = None
    band_validity: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, plate_dir: str | Path, *, roots: Sequence[Path | None] = ()) -> PlateBands:
        """Read ``plate.json``, its band PNGs and its overlay, checking each file's SHA-256.

        The overlay (the caption strip) decides which pixels count as the frame for the
        whole-frame energy, the fault statistics and the in-cloud flag, so it is checked like a
        band, and so is the base frame (``base.png``) that the Same view gate registers
        against. Bands must not share a pixel: a shared pixel would count in two bands. A
        plate folder under the quarantine folder is refused before it is read (D-022).
        """
        folder = Path(plate_dir).expanduser()
        reason = path_quarantine_reason(folder, roots=roots)
        if reason is not None:
            raise LeakageError(f"plate {folder}: {reason}")
        manifest_path = folder / "plate.json"
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
        if doc.get("kind") != PLATE_KIND:
            raise ValueError(f"{manifest_path}: not a {PLATE_KIND} manifest")
        width, height = (int(v) for v in doc["frame_size"])
        masks: dict[str, NDArray[np.bool_]] = {}
        order = []
        for band in doc["bands"]:
            name, file = band["name"], band["file"]
            path = folder / file
            want = doc["files"][file]["sha256"]
            if _sha256(path) != want:
                raise ValueError(f"{path}: SHA-256 differs from plate.json")
            with Image.open(path) as im:
                mask = np.asarray(im.convert("L")) > 127
            if mask.shape != (height, width):
                raise ValueError(f"{path}: {mask.shape[::-1]} is not the plate's {width}x{height}")
            masks[name] = mask
            order.append(name)
        overlay = np.zeros((height, width), dtype=bool)
        if "overlay.png" in doc.get("files", {}):
            path = folder / "overlay.png"
            if _sha256(path) != doc["files"]["overlay.png"]["sha256"]:
                raise ValueError(f"{path}: SHA-256 differs from plate.json")
            with Image.open(path) as im:
                overlay = np.asarray(im.convert("L")) > 127
            if overlay.shape != (height, width):
                raise ValueError(
                    f"{path}: {overlay.shape[::-1]} is not the plate's {width}x{height}"
                )
        names = list(masks)
        for i, first in enumerate(names):
            for second in names[i + 1 :]:
                shared = int(np.count_nonzero(masks[first] & masks[second]))
                if shared:
                    raise ValueError(f"{folder}: bands {first} and {second} share {shared} pixels")
        base_path = None
        if "base.png" in doc.get("files", {}):
            base_path = folder / "base.png"
            if _sha256(base_path) != doc["files"]["base.png"]["sha256"]:
                raise ValueError(f"{base_path}: SHA-256 differs from plate.json")
        validity = {
            band["name"]: {
                "valid": bool(band.get("valid", True)),
                "not_valid_because": band.get("not_valid_because"),
            }
            for band in doc["bands"]
        }
        return cls(
            viewpoint_id=doc["viewpoint_id"],
            plate_id=doc["plate_id"],
            plate_json_sha256=_sha256(manifest_path),
            frame_size=(width, height),
            order=tuple(order),
            masks=masks,
            overlay=overlay,
            source=str(folder),
            base_path=base_path,
            band_validity=validity,
        )

    def on_grid(self, analysis_size: tuple[int, int]) -> dict[str, NDArray[np.bool_]]:
        """Each band (and ``"_frame"``, everything but the caption) on the analysis grid."""
        aw, ah = analysis_size
        out = {}
        for name, mask in (*self.masks.items(), ("_frame", ~self.overlay)):
            small = Image.fromarray((mask * 255).astype(np.uint8)).resize(
                (aw, ah), Image.Resampling.BOX
            )
            out[name] = np.asarray(small) >= 128
        return out

    def to_record(self) -> dict[str, Any]:
        """Provenance for result records."""
        return {
            "plate_id": self.plate_id,
            "plate_json_sha256": self.plate_json_sha256,
            "source": self.source,
            "bands": list(self.order),
            "band_pixels": {name: int(m.sum()) for name, m in self.masks.items()},
            "band_validity": {name: dict(v) for name, v in self.band_validity.items()},
            "base": None if self.base_path is None else str(self.base_path),
        }


@dataclass(frozen=True)
class BandEnergies:
    """One frame's normalised edge energy per band, over the whole frame, and its fault stats."""

    bands: dict[str, float]
    global_energy: float
    rms_contrast: float
    dark_fraction: float
    bright_fraction: float
    luma_range: float
    analysis_size: tuple[int, int]

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        out = asdict(self)
        out["analysis_size"] = list(self.analysis_size)
        return out


def band_energies(image: ImageSource, plate: PlateBands, params: V0Params) -> BandEnergies:
    """Normalised Sobel energy per band (v−1's measure; see the module docstring)."""
    y, original = luma(image, params.analysis_width)
    if original != plate.frame_size:
        raise ValueError(f"frame is {original}, the plate is {plate.frame_size}")
    grids = plate.on_grid((y.shape[1], y.shape[0]))
    frame = grids.pop("_frame")
    dark = float(np.count_nonzero(y[frame] <= params.fault_dark_luma)) / max(1, int(frame.sum()))
    bright = float(np.count_nonzero(y[frame] >= params.fault_bright_luma)) / max(
        1, int(frame.sum())
    )
    luma_range = float(y[frame].max() - y[frame].min())
    if params.blur_sigma_px > 0:
        y = ndimage.gaussian_filter(y, params.blur_sigma_px, mode="reflect")
    gx = ndimage.sobel(y, axis=1, mode="reflect") / 8.0
    gy = ndimage.sobel(y, axis=0, mode="reflect") / 8.0
    local_mean = ndimage.uniform_filter(y, size=params.local_mean_size_px, mode="reflect")
    normalised = np.hypot(gx, gy) / (local_mean + params.epsilon)
    bands = {}
    for name, cells in grids.items():
        if int(cells.sum()) < params.min_band_cells:
            raise ValueError(f"band {name} covers {int(cells.sum())} analysis cells")
        bands[name] = float(normalised[cells].mean())
    inside = y[frame]
    return BandEnergies(
        bands=bands,
        global_energy=float(normalised[frame].mean()),
        rms_contrast=float(inside.std() / (inside.mean() + params.epsilon)),
        dark_fraction=dark,
        bright_fraction=bright,
        luma_range=luma_range,
        analysis_size=(y.shape[1], y.shape[0]),
    )


@dataclass(frozen=True)
class ReferenceFrameV0:
    """A clear-day pool frame: its identity, Sun position, local day and energies."""

    key: str
    sha256: str
    image_time: datetime
    sun_alt_deg: float
    sun_az_deg: float
    local_day: str
    energies: BandEnergies


def _daz(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def select_reference(
    pool: Sequence[ReferenceFrameV0],
    sun_alt_deg: float,
    sun_az_deg: float,
    params: V0Params,
    *,
    local_day: str | None = None,
    exclude_keys: Sequence[str] = (),
) -> list[ReferenceFrameV0]:
    """The ``ref_k`` nearest pool frames within the Sun limits (see the module docstring)."""
    ranked = []
    for ref in pool:
        if ref.key in exclude_keys:
            continue
        if params.exclude_same_day and local_day is not None and ref.local_day == local_day:
            continue
        dalt = abs(ref.sun_alt_deg - sun_alt_deg)
        daz = _daz(ref.sun_az_deg, sun_az_deg)
        if dalt > params.ref_max_dalt_deg or daz > params.ref_max_daz_deg:
            continue
        ranked.append(
            (math.hypot(dalt / params.ref_max_dalt_deg, daz / params.ref_max_daz_deg), ref.key, ref)
        )
    ranked.sort(key=lambda r: (r[0], r[1]))
    return [r[2] for r in ranked[: params.ref_k]]


def camera_fault(e: BandEnergies, params: V0Params) -> str | None:
    """D-037's camera fault ("dark", "saturated" or "blank"), or None."""
    if e.dark_fraction >= params.fault_fraction_min:
        return "dark"
    if e.bright_fraction >= params.fault_fraction_min:
        return "saturated"
    if e.luma_range <= params.fault_flat_range_max:
        return "blank"
    return None


@dataclass(frozen=True)
class BandResult:
    """One band's ratio and class."""

    energy: float
    reference_energy: float | None
    ratio: float | None
    state: str | None


@dataclass(frozen=True)
class FrameResult:
    """Extractor v0's output for one frame."""

    frame_class: FrameClass
    reason: str | None
    sun_alt_deg: float
    sun_az_deg: float
    bands: dict[str, BandResult]
    obscuration_height: str | None
    global_energy_ratio: float | None
    contrast_ratio: float | None
    references: list[dict[str, Any]] = field(default_factory=list)
    camera_fault: str | None = None

    def band_class(self, name: str) -> str | None:
        """A band's P1-style class: the frame class unless scored, else the band's state."""
        if self.frame_class != "scored":
            return self.frame_class
        return self.bands[name].state

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {
            "extractor_version": EXTRACTOR_V0,
            "frame_class": self.frame_class,
            "reason": self.reason,
            "camera_fault": self.camera_fault,
            "sun_alt_deg": round(self.sun_alt_deg, 3),
            "sun_az_deg": round(self.sun_az_deg, 3),
            "bands": {
                name: {
                    "energy": round(b.energy, 6),
                    "reference_energy": None
                    if b.reference_energy is None
                    else round(b.reference_energy, 6),
                    "ratio": None if b.ratio is None else round(b.ratio, 4),
                    "class": self.band_class(name),
                }
                for name, b in self.bands.items()
            },
            "obscuration_height": self.obscuration_height,
            "global_energy_ratio": None
            if self.global_energy_ratio is None
            else round(self.global_energy_ratio, 4),
            "contrast_ratio": None
            if self.contrast_ratio is None
            else round(self.contrast_ratio, 4),
            "references": self.references,
        }


def classify_frame(
    energies: BandEnergies,
    *,
    sun_alt_deg: float,
    sun_az_deg: float,
    order: Sequence[str],
    pool: Sequence[ReferenceFrameV0],
    params: V0Params,
    local_day: str | None,
    exclude_keys: Sequence[str] = (),
) -> FrameResult:
    """Classes for one frame from its energies (see the module docstring)."""
    blank = {n: BandResult(energies.bands[n], None, None, None) for n in order}
    if sun_alt_deg < params.min_sun_alt_deg:
        return FrameResult(
            "night",
            f"Sun {sun_alt_deg:.2f} deg is below {params.min_sun_alt_deg} deg",
            sun_alt_deg,
            sun_az_deg,
            blank,
            None,
            None,
            None,
        )
    fault = camera_fault(energies, params)
    if fault is not None:
        return FrameResult(
            "camera_fault",
            f"camera fault: {fault} frame",
            sun_alt_deg,
            sun_az_deg,
            blank,
            None,
            None,
            None,
            camera_fault=fault,
        )
    refs = select_reference(
        pool, sun_alt_deg, sun_az_deg, params, local_day=local_day, exclude_keys=exclude_keys
    )
    if not refs:
        return FrameResult(
            "no_reference",
            f"no clear-day frame within {params.ref_max_dalt_deg} deg of Sun altitude and "
            f"{params.ref_max_daz_deg} deg of azimuth"
            + (" on another day" if params.exclude_same_day else ""),
            sun_alt_deg,
            sun_az_deg,
            blank,
            None,
            None,
            None,
        )
    bands = {}
    for name in order:
        ref_e = float(np.median([r.energies.bands[name] for r in refs]))
        ratio = energies.bands[name] / ref_e if ref_e > 0 else None
        state = (
            None
            if ratio is None
            else classify_ratio(ratio, tau_hi=params.tau_hi, tau_lo=params.tau_lo)
        )
        bands[name] = BandResult(energies.bands[name], ref_e, ratio, state)
    g_ref = float(np.median([r.energies.global_energy for r in refs]))
    c_ref = float(np.median([r.energies.rms_contrast for r in refs]))
    g_ratio = energies.global_energy / g_ref if g_ref > 0 else None
    c_ratio = energies.rms_contrast / c_ref if c_ref > 0 else None
    references = [
        {
            "key": r.key,
            "sha256": r.sha256,
            "sun_alt_deg": round(r.sun_alt_deg, 3),
            "sun_az_deg": round(r.sun_az_deg, 3),
        }
        for r in refs
    ]
    in_cloud = (
        g_ratio is not None
        and c_ratio is not None
        and g_ratio < params.in_cloud_energy_ratio_max
        and c_ratio < params.in_cloud_contrast_ratio_max
    )
    if in_cloud:
        return FrameResult(
            "in_cloud",
            "whole-frame energy and contrast collapsed (camera in cloud)",
            sun_alt_deg,
            sun_az_deg,
            bands,
            None,
            g_ratio,
            c_ratio,
            references,
        )
    height = next((n for n in order if bands[n].state == "visible"), None)
    return FrameResult(
        "scored", None, sun_alt_deg, sun_az_deg, bands, height, g_ratio, c_ratio, references
    )


def local_day(moment: datetime, time_zone: str) -> str:
    """The local calendar day of a time in a zone (ISO)."""
    return moment.astimezone(ZoneInfo(time_zone)).date().isoformat()
