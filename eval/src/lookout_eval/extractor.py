"""Extractor v−1: landmark visibility from edge energy (PRD §13.3, §13.4).

PRD §13.3: "A landmark is visible when its edge energy, compared with an
illumination-conditioned clear-day reference, clears a threshold." This is the method of the
FAA/MIT Lincoln Laboratory VEIA (Visibility Estimation through Image Analytics): edges that a
clear day shows fade when cloud, fog or haze lie between the camera and the landmark.

**Energy.** On Rec. 601 luma of the gamma-encoded frame, resampled to a fixed analysis width:

1. a small Gaussian blur (``blur_sigma_px``) suppresses sensor noise and JPEG blocking;
2. Sobel gradient magnitude ``|∇Y|`` (per pixel);
3. divided by the local mean luminance (a ``local_mean_size_px`` box) plus ``epsilon``,
   so a global exposure change, which scales both, cancels (a Weber-contrast measure);
4. the landmark's energy is the mean of that over its box or polygon.

**Reference.** Clear-day frames (a file list chosen by the caller) are binned by Sun
altitude and azimuth; the reference energy is the **median** over the frames in the bin of
the frame being scored. An empty bin falls back to the nearest non-empty bin within
``max_bin_steps``; otherwise there is no reference and no state.

**States.** ``ratio = energy / reference``: ``visible`` if ``ratio >= tau_hi``, ``hidden``
if ``ratio < tau_lo``, ``partial`` in between (PRD §13.4). **Camera in cloud** (§9.3, §13.4)
is a global contrast collapse: the whole frame's energy *and* its RMS contrast both fall
below fractions of the reference's.

**Camera fault** is not weather, and is checked first: a frame that is almost all black
(``dark``), almost all saturated (``saturated``) or one flat value (``blank``) gets no state,
no in-cloud flag and the class ``camera_fault``. Such frames would otherwise look like a
contrast collapse and be scored as "in cloud". The class is unusable for every baseline and
fails the "usable frame" gate (:mod:`.gates`).

The same :func:`extract` serves real frames, rendered frames and baseline frames (§13.3
"One extractor"); only the reference set differs, and it is recorded.

v−1 thresholds are placeholders. TODO(M2): τ and the partial band are fixed on development
data (PRD §13.4), the in-cloud and camera-fault thresholds and bins likewise, and the
extractor is validated against labels before any number is published (§13.8).
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
from lookout_common.images import ImageSource, load_image
from lookout_common.timeutil import isoformat_z, parse_iso_utc
from numpy.typing import NDArray
from PIL import Image, ImageDraw
from scipy import ndimage

from .frames import parse_frame_key, read_sidecar_for_path, sha256_file

__all__ = [
    "CAMERA_FAULT_CLASS",
    "EXTRACTOR_VERSION",
    "ClearDayReference",
    "EdgeStats",
    "ExtractorParams",
    "LandmarkResult",
    "ReferenceEntry",
    "ReferenceFrame",
    "ReferenceLookup",
    "Region",
    "State",
    "camera_fault",
    "classify_ratio",
    "edge_stats",
    "extract",
    "luma",
]

EXTRACTOR_VERSION = "v-1"
#: The P1 class of a frame with a camera fault (not weather; never scored).
CAMERA_FAULT_CLASS = "camera_fault"

type State = Literal["visible", "hidden", "partial"]

_ENERGY_FIELDS = ("analysis_width", "blur_sigma_px", "local_mean_size_px", "epsilon")


@dataclass(frozen=True, slots=True)
class ExtractorParams:
    """Every number the extractor uses. v−1 values are placeholders (TODO(M2))."""

    analysis_width: int = 960
    blur_sigma_px: float = 1.0
    local_mean_size_px: int = 31
    epsilon: float = 0.02
    tau_hi: float = 0.6
    tau_lo: float = 0.35
    in_cloud_energy_ratio_max: float = 0.25
    in_cloud_contrast_ratio_max: float = 0.35
    alt_bin_deg: float = 5.0
    az_bin_deg: float = 20.0
    max_bin_steps: int = 1
    min_region_px: int = 50
    fault_dark_luma: float = 4.0 / 255.0
    fault_bright_luma: float = 251.0 / 255.0
    fault_fraction_min: float = 0.95
    fault_flat_range_max: float = 1.0 / 255.0

    def __post_init__(self) -> None:
        if self.analysis_width < 32:
            raise ValueError("analysis_width must be at least 32 px")
        if not 0.0 < self.tau_lo <= self.tau_hi:
            raise ValueError("need 0 < tau_lo <= tau_hi")
        if self.epsilon <= 0 or self.local_mean_size_px < 3 or self.blur_sigma_px < 0:
            raise ValueError("epsilon > 0, local_mean_size_px >= 3 and blur_sigma_px >= 0")
        if self.alt_bin_deg <= 0 or self.az_bin_deg <= 0 or self.max_bin_steps < 0:
            raise ValueError("bins must be positive and max_bin_steps >= 0")
        if not 0.0 <= self.fault_dark_luma < self.fault_bright_luma <= 1.0:
            raise ValueError("need 0 <= fault_dark_luma < fault_bright_luma <= 1")
        if not 0.5 < self.fault_fraction_min <= 1.0 or self.fault_flat_range_max < 0:
            raise ValueError("fault_fraction_min in (0.5, 1] and fault_flat_range_max >= 0")

    def energy_key(self) -> tuple[Any, ...]:
        """The fields that change energies; a reference and a scored frame must share them."""
        return tuple(getattr(self, name) for name in _ENERGY_FIELDS)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExtractorParams:
        """Inverse of :meth:`to_dict`."""
        return cls(**data)


@dataclass(frozen=True)
class Region:
    """A landmark region: a box ``(x0, y0, x1, y1)`` (x1, y1 exclusive) or a polygon.

    Coordinates are pixels of a frame of ``frame_size`` (width, height); None means the
    scored image's own size. They are scaled to the analysis grid, so a box drawn on a
    3840×2160 archive frame also fits a 1920×1080 stored copy.
    """

    kind: Literal["box", "polygon"]
    points: tuple[tuple[float, float], ...]
    frame_size: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if self.kind == "box":
            if len(self.points) != 2:
                raise ValueError("a box has two corners")
            (x0, y0), (x1, y1) = self.points
            if not (x1 > x0 and y1 > y0):
                raise ValueError("box needs x1 > x0 and y1 > y0")
        elif self.kind == "polygon":
            if len(self.points) < 3:
                raise ValueError("a polygon needs at least three vertices")
        else:
            raise ValueError(f"unknown region kind {self.kind!r}")
        if any(not (math.isfinite(x) and math.isfinite(y)) for x, y in self.points):
            raise ValueError("region coordinates must be finite")

    @classmethod
    def box(
        cls,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        *,
        frame_size: tuple[int, int] | None = None,
    ) -> Region:
        """A box region."""
        return cls("box", ((float(x0), float(y0)), (float(x1), float(y1))), frame_size)

    @classmethod
    def polygon(
        cls, points: Sequence[tuple[float, float]], *, frame_size: tuple[int, int] | None = None
    ) -> Region:
        """A polygon region."""
        return cls("polygon", tuple((float(x), float(y)) for x, y in points), frame_size)

    @classmethod
    def parse(cls, text: str, *, frame_size: tuple[int, int] | None = None) -> Region:
        """Parse ``"x0,y0,x1,y1"`` (a box), ``"x,y;x,y;x,y..."`` (a polygon) or the JSON form."""
        stripped = text.strip()
        if stripped.startswith("{"):
            return cls.from_dict(json.loads(stripped))
        if ";" in stripped:
            pairs = [p.split(",") for p in stripped.split(";") if p.strip()]
            return cls.polygon([(float(x), float(y)) for x, y in pairs], frame_size=frame_size)
        values = [float(v) for v in stripped.split(",")]
        if len(values) != 4:
            raise ValueError("a box is 'x0,y0,x1,y1'")
        return cls.box(*values, frame_size=frame_size)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {
            "kind": self.kind,
            "points": [list(p) for p in self.points],
            "frame_size": None if self.frame_size is None else list(self.frame_size),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Region:
        """Inverse of :meth:`to_dict`."""
        size = data.get("frame_size")
        return cls(
            data["kind"],
            tuple((float(x), float(y)) for x, y in data["points"]),
            None if size is None else (int(size[0]), int(size[1])),
        )

    def mask(
        self, analysis_size: tuple[int, int], image_size: tuple[int, int]
    ) -> NDArray[np.bool_]:
        """Boolean mask of the region on the analysis grid."""
        ref_w, ref_h = self.frame_size or image_size
        aw, ah = analysis_size
        sx, sy = aw / ref_w, ah / ref_h
        for x, y in self.points:
            if not (0.0 <= x <= ref_w and 0.0 <= y <= ref_h):
                raise ValueError(f"region point ({x}, {y}) is outside the {ref_w}x{ref_h} frame")
        if self.kind == "box":
            (x0, y0), (x1, y1) = self.points
            out = np.zeros((ah, aw), dtype=bool)
            c0, c1 = math.floor(x0 * sx), math.ceil(x1 * sx)
            r0, r1 = math.floor(y0 * sy), math.ceil(y1 * sy)
            out[max(0, r0) : min(ah, r1), max(0, c0) : min(aw, c1)] = True
            return out
        canvas = Image.new("L", (aw, ah), 0)
        ImageDraw.Draw(canvas).polygon([(x * sx, y * sy) for x, y in self.points], fill=1)
        return np.asarray(canvas, dtype=np.uint8).astype(bool)


def luma(image: ImageSource, analysis_width: int) -> tuple[NDArray[np.float64], tuple[int, int]]:
    """Rec. 601 luma in [0, 1] at ``analysis_width`` (area-averaged down, bicubic up).

    Returns the luma array and the original ``(width, height)``.
    """
    pil = load_image(image)
    original = pil.size
    if pil.mode not in ("RGB", "L"):
        pil = pil.convert("RGB")
    height = max(1, round(original[1] * analysis_width / original[0]))
    if (analysis_width, height) != original:
        method = Image.Resampling.BOX if analysis_width < original[0] else Image.Resampling.BICUBIC
        pil = pil.resize((analysis_width, height), method)
    array = np.asarray(pil, dtype=np.float64) / 255.0
    if array.ndim == 2:
        return array, original
    y = 0.299 * array[..., 0] + 0.587 * array[..., 1] + 0.114 * array[..., 2]
    return y, original


@dataclass(frozen=True, slots=True)
class EdgeStats:
    """Edge energy of one frame: in the region, over the whole frame, and RMS contrast.

    ``dark_fraction``, ``bright_fraction`` and ``luma_range`` (of the unblurred analysis
    luma) feed the camera-fault check; they are None in stats saved before they existed.
    """

    region_energy: float
    global_energy: float
    rms_contrast: float
    region_rms_contrast: float
    luma_mean: float
    region_px: int
    analysis_size: tuple[int, int]
    image_size: tuple[int, int]
    dark_fraction: float | None = None
    bright_fraction: float | None = None
    luma_range: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        out = asdict(self)
        out["analysis_size"] = list(self.analysis_size)
        out["image_size"] = list(self.image_size)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EdgeStats:
        """Inverse of :meth:`to_dict`."""
        values = dict(data)
        values["analysis_size"] = tuple(values["analysis_size"])
        values["image_size"] = tuple(values["image_size"])
        return cls(**values)


def edge_stats(image: ImageSource, region: Region, params: ExtractorParams) -> EdgeStats:
    """Normalised edge energy of ``region`` in ``image`` (see the module docstring)."""
    y, original = luma(image, params.analysis_width)
    dark_fraction = float(np.count_nonzero(y <= params.fault_dark_luma)) / y.size
    bright_fraction = float(np.count_nonzero(y >= params.fault_bright_luma)) / y.size
    luma_range = float(y.max() - y.min())
    if params.blur_sigma_px > 0:
        y = ndimage.gaussian_filter(y, params.blur_sigma_px, mode="reflect")
    gx = ndimage.sobel(y, axis=1, mode="reflect") / 8.0
    gy = ndimage.sobel(y, axis=0, mode="reflect") / 8.0
    local_mean = ndimage.uniform_filter(y, size=params.local_mean_size_px, mode="reflect")
    normalised = np.hypot(gx, gy) / (local_mean + params.epsilon)
    analysis_size = (y.shape[1], y.shape[0])
    mask = region.mask(analysis_size, original)
    count = int(mask.sum())
    if count < params.min_region_px:
        raise ValueError(
            f"the region covers {count} analysis pixels, fewer than {params.min_region_px}"
        )
    inside = y[mask]
    return EdgeStats(
        region_energy=float(normalised[mask].mean()),
        global_energy=float(normalised.mean()),
        rms_contrast=float(y.std() / (y.mean() + params.epsilon)),
        region_rms_contrast=float(inside.std() / (inside.mean() + params.epsilon)),
        luma_mean=float(y.mean()),
        region_px=count,
        analysis_size=analysis_size,
        image_size=original,
        dark_fraction=dark_fraction,
        bright_fraction=bright_fraction,
        luma_range=luma_range,
    )


def camera_fault(stats: EdgeStats, params: ExtractorParams) -> str | None:
    """``dark``, ``saturated`` or ``blank`` when the frame shows a camera fault, else None.

    A frame is ``dark`` or ``saturated`` when at least ``fault_fraction_min`` of its
    analysis pixels are at or beyond the dark or bright luma limits (an overlay or logo can
    cover the rest), and ``blank`` when its whole luma range is within
    ``fault_flat_range_max`` (a real fog frame still has noise and vignetting). v−1 limits,
    TODO(M2): checked against development frames.
    """
    if stats.dark_fraction is None or stats.bright_fraction is None or stats.luma_range is None:
        return None
    if stats.dark_fraction >= params.fault_fraction_min:
        return "dark"
    if stats.bright_fraction >= params.fault_fraction_min:
        return "saturated"
    if stats.luma_range <= params.fault_flat_range_max:
        return "blank"
    return None


def classify_ratio(ratio: float, *, tau_hi: float, tau_lo: float) -> State:
    """``visible`` if ratio ≥ τ_hi, ``hidden`` if ratio < τ_lo, else ``partial`` (PRD §13.4)."""
    if not math.isfinite(ratio):
        raise ValueError("ratio must be finite")
    if ratio >= tau_hi:
        return "visible"
    if ratio < tau_lo:
        return "hidden"
    return "partial"


@dataclass(frozen=True, slots=True)
class ReferenceFrame:
    """A clear-day reference frame and its image time."""

    path: Path
    image_time: datetime

    @classmethod
    def from_path(cls, path: str | Path, image_time: datetime | None = None) -> ReferenceFrame:
        """Use ``image_time``, else the sidecar's ``image_time_utc``, else the key's stamp."""
        file_path = Path(path).expanduser()
        if image_time is None:
            sidecar = read_sidecar_for_path(file_path)
            if sidecar and sidecar.get("image_time_utc"):
                image_time = parse_iso_utc(sidecar["image_time_utc"])
            else:
                parts = file_path.parts
                key = "/".join(parts[-4:])
                image_time = parse_frame_key(key).image_time
        if image_time.tzinfo is None:
            raise ValueError("reference image_time must be timezone-aware")
        return cls(file_path, image_time)


@dataclass(frozen=True, slots=True)
class ReferenceEntry:
    """One reference frame's Sun position and energies."""

    path: str
    sha256: str
    image_time_utc: str
    sun_alt_deg: float
    sun_az_deg: float
    stats: EdgeStats

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {
            "path": self.path,
            "sha256": self.sha256,
            "image_time_utc": self.image_time_utc,
            "sun_alt_deg": self.sun_alt_deg,
            "sun_az_deg": self.sun_az_deg,
            "stats": self.stats.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ReferenceLookup:
    """The bin used for one scored frame and its median energies."""

    bin: tuple[int, int]
    query_bin: tuple[int, int]
    bin_steps: int
    n: int
    region_energy: float
    global_energy: float
    rms_contrast: float
    sha256s: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        out = asdict(self)
        out["bin"] = list(self.bin)
        out["query_bin"] = list(self.query_bin)
        out["sha256s"] = list(self.sha256s)
        return out


@dataclass(frozen=True)
class ClearDayReference:
    """Illumination-conditioned clear-day reference energies for one region (PRD §13.3)."""

    region: Region
    params: ExtractorParams
    entries: tuple[ReferenceEntry, ...]

    @classmethod
    def build(
        cls,
        frames: Sequence[ReferenceFrame],
        *,
        region: Region,
        sun_of: Callable[[datetime], tuple[float, float]],
        params: ExtractorParams | None = None,
    ) -> ClearDayReference:
        """Measure every reference frame; ``sun_of(time)`` gives the Sun's (alt, az) there."""
        chosen = params or ExtractorParams()
        entries = []
        for frame in frames:
            alt, az = sun_of(frame.image_time)
            entries.append(
                ReferenceEntry(
                    path=str(frame.path),
                    sha256=sha256_file(frame.path),
                    image_time_utc=isoformat_z(frame.image_time),
                    sun_alt_deg=float(alt),
                    sun_az_deg=float(az) % 360.0,
                    stats=edge_stats(frame.path, region, chosen),
                )
            )
        return cls(region, chosen, tuple(entries))

    def _bin(self, alt: float, az: float) -> tuple[int, int]:
        return (
            math.floor(alt / self.params.alt_bin_deg),
            math.floor((az % 360.0) / self.params.az_bin_deg),
        )

    def bins(self) -> dict[tuple[int, int], list[ReferenceEntry]]:
        """Entries grouped by Sun bin."""
        out: dict[tuple[int, int], list[ReferenceEntry]] = {}
        for entry in self.entries:
            out.setdefault(self._bin(entry.sun_alt_deg, entry.sun_az_deg), []).append(entry)
        return out

    def lookup(self, sun_alt_deg: float, sun_az_deg: float) -> ReferenceLookup | None:
        """Median energies of the query's Sun bin, or the nearest non-empty bin within reach."""
        query = self._bin(sun_alt_deg, sun_az_deg)
        n_az = math.ceil(360.0 / self.params.az_bin_deg)
        best: tuple[float, int, int, tuple[int, int]] | None = None
        for key, items in self.bins().items():
            d_alt = abs(key[0] - query[0])
            d_az = abs(key[1] - query[1])
            d_az = min(d_az, n_az - d_az)
            steps = max(d_alt, d_az)
            if steps > self.params.max_bin_steps:
                continue
            rank = (math.hypot(d_alt, d_az), -len(items), steps, key)
            if best is None or rank < best:
                best = rank
        if best is None:
            return None
        key = best[3]
        items = self.bins()[key]
        return ReferenceLookup(
            bin=key,
            query_bin=query,
            bin_steps=best[2],
            n=len(items),
            region_energy=float(np.median([e.stats.region_energy for e in items])),
            global_energy=float(np.median([e.stats.global_energy for e in items])),
            rms_contrast=float(np.median([e.stats.rms_contrast for e in items])),
            sha256s=tuple(e.sha256 for e in items),
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form (cacheable)."""
        return {
            "extractor_version": EXTRACTOR_VERSION,
            "region": self.region.to_dict(),
            "params": self.params.to_dict(),
            "entries": [e.to_dict() for e in self.entries],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClearDayReference:
        """Inverse of :meth:`to_dict`."""
        entries = tuple(
            ReferenceEntry(
                path=e["path"],
                sha256=e["sha256"],
                image_time_utc=e["image_time_utc"],
                sun_alt_deg=float(e["sun_alt_deg"]),
                sun_az_deg=float(e["sun_az_deg"]),
                stats=EdgeStats.from_dict(e["stats"]),
            )
            for e in data["entries"]
        )
        return cls(
            Region.from_dict(data["region"]), ExtractorParams.from_dict(data["params"]), entries
        )


@dataclass(frozen=True)
class LandmarkResult:
    """The extractor's output for one landmark in one frame."""

    state: State | None
    ratio: float | None
    camera_in_cloud: bool | None
    global_energy_ratio: float | None
    contrast_ratio: float | None
    stats: EdgeStats
    sun_alt_deg: float
    sun_az_deg: float
    reference: ReferenceLookup | None
    params: ExtractorParams
    reason: str | None = None
    camera_fault: str | None = None
    extractor_version: str = field(default=EXTRACTOR_VERSION)

    @property
    def p1_class(self) -> str | None:
        """``camera_fault``; else ``in_cloud`` (its own class, PRD §13.4); else the state."""
        if self.camera_fault:
            return CAMERA_FAULT_CLASS
        if self.camera_in_cloud:
            return "in_cloud"
        return self.state

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {
            "extractor_version": self.extractor_version,
            "state": self.state,
            "p1_class": self.p1_class,
            "ratio": self.ratio,
            "camera_in_cloud": self.camera_in_cloud,
            "camera_fault": self.camera_fault,
            "global_energy_ratio": self.global_energy_ratio,
            "contrast_ratio": self.contrast_ratio,
            "sun_alt_deg": self.sun_alt_deg,
            "sun_az_deg": self.sun_az_deg,
            "stats": self.stats.to_dict(),
            "reference": None if self.reference is None else self.reference.to_dict(),
            "params": self.params.to_dict(),
            "reason": self.reason,
        }


def _safe_ratio(value: float, reference: float) -> float | None:
    return None if reference <= 0.0 else value / reference


def extract(
    image: ImageSource,
    region: Region,
    *,
    sun_alt_deg: float,
    sun_az_deg: float,
    reference: ClearDayReference,
    params: ExtractorParams | None = None,
) -> LandmarkResult:
    """Score one landmark in one frame, real or rendered, against the clear-day reference.

    ``params`` defaults to the reference's own. Thresholds may differ from the reference's,
    but the energy settings (analysis width, blur, local-mean window, epsilon) and the region
    must match, or the ratio would compare different measurements.
    """
    chosen = params or reference.params
    if chosen.energy_key() != reference.params.energy_key():
        raise ValueError("energy settings differ from the reference's; rebuild the reference")
    if region.to_dict() != reference.region.to_dict():
        raise ValueError("the region differs from the reference's region")
    stats = edge_stats(image, region, chosen)
    fault = camera_fault(stats, chosen)
    lookup = reference.lookup(sun_alt_deg, sun_az_deg)
    if lookup is None:
        return LandmarkResult(
            state=None,
            ratio=None,
            camera_in_cloud=None,
            global_energy_ratio=None,
            contrast_ratio=None,
            stats=stats,
            sun_alt_deg=sun_alt_deg,
            sun_az_deg=sun_az_deg,
            reference=None,
            params=chosen,
            reason=(
                f"no reference frame within {chosen.max_bin_steps} bin(s) of Sun "
                f"alt {sun_alt_deg:.1f} deg, az {sun_az_deg:.1f} deg"
            ),
            camera_fault=fault,
        )
    ratio = _safe_ratio(stats.region_energy, lookup.region_energy)
    global_ratio = _safe_ratio(stats.global_energy, lookup.global_energy)
    contrast_ratio = _safe_ratio(stats.rms_contrast, lookup.rms_contrast)
    in_cloud = (
        None
        if global_ratio is None or contrast_ratio is None
        else (
            global_ratio < chosen.in_cloud_energy_ratio_max
            and contrast_ratio < chosen.in_cloud_contrast_ratio_max
        )
    )
    state = (
        None if ratio is None else classify_ratio(ratio, tau_hi=chosen.tau_hi, tau_lo=chosen.tau_lo)
    )
    if fault is not None:
        return LandmarkResult(
            state=None,
            ratio=ratio,
            camera_in_cloud=None,
            global_energy_ratio=global_ratio,
            contrast_ratio=contrast_ratio,
            stats=stats,
            sun_alt_deg=sun_alt_deg,
            sun_az_deg=sun_az_deg,
            reference=lookup,
            params=chosen,
            reason=f"camera fault: {fault} frame (not weather; never scored)",
            camera_fault=fault,
        )
    return LandmarkResult(
        state=state,
        ratio=ratio,
        camera_in_cloud=in_cloud,
        global_energy_ratio=global_ratio,
        contrast_ratio=contrast_ratio,
        stats=stats,
        sun_alt_deg=sun_alt_deg,
        sun_az_deg=sun_az_deg,
        reference=lookup,
        params=chosen,
        reason=None if ratio is not None else "reference energy is zero",
    )


def with_thresholds(params: ExtractorParams, *, tau_hi: float, tau_lo: float) -> ExtractorParams:
    """A copy of ``params`` with new thresholds (energies unchanged)."""
    return replace(params, tau_hi=tau_hi, tau_lo=tau_lo)
