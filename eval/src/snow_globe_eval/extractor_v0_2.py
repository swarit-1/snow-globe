"""Extractor v0.2: the band edge ratio against an illumination-conditioned reference (PRD §13.3).

PRD §13.3: "A landmark is visible when its edge energy, compared with an illumination-
conditioned clear-day reference, clears a threshold." Extractor v0 (:mod:`.extractor_v0`)
conditioned its reference on the Sun's position alone (within 3° of altitude and 15° of
azimuth) and drew it from frames labelled clear day in direct sunlight. It missed PRD §13.8's
bar on the summit (D-100) for two reasons: it read bright-overcast summits as partial or hidden,
because its references showed a sunlit skyline against a blue sky; and 58 % of the daytime
frames had no reference within its Sun limits.

v0.2 keeps v0's **energy** (:func:`.extractor_v0.band_energies`: the Sobel gradient of Rec. 601
luma over the local mean luma, averaged over each plate band, at ``analysis_width``) and changes
what it is compared with.

**The physics.** The summit band is mostly the dome's skyline. At a step between the sky's luma
Y_s and the dome's Y_d, that energy is proportional to the Weber contrast
2·|Y_d − Y_s| / (Y_d + Y_s), since it is a gradient over the local mean. In clear air the
contrast is set by the light: under direct sun the lit snow is far brighter than the sky behind
it; under an overcast the sky lights the snow and is nearly as bright as it, so a perfectly
clear dome shows a faint skyline. A clear-air reference must match the frame's light, and the
Sun's position alone does not say whether the Sun reaches the snow.

**Illumination descriptor** (from the frame itself, never from a label): D = the median luma of
the sky (the plate's ``sky.png``) over the 99th-percentile luma of the camera's near field (the
lower frame, ``near_window``, outside the sky, every band and the caption strip). It says how
bright the sky is against the brightest snow lit by the same light: about 0.5 under a clear sky
with the Sun on the snow, about 0.7-0.9 under overcast, fog or falling snow. It does not depend
on whether the mountain shows.

**Expected clear-air contrast** of a skyline band: C(D) = 2·|1 − D| / (1 + D), the Weber
contrast between the brightest lit snow and the sky. For a skyline band (``skyline_bands``, the
summit) the energy is divided by C(D), giving an illumination-normalised energy Ẽ = E / C(D):
edge energy per unit of the contrast clear air would show in this light. The upper and mid
bands are not normalised: their edges are the terrain's own rock and snow, whose contrast the
albedo sets, not the sky. With ``contrast_model=False`` (the ablation v0.2-A) no band is. A frame
whose C(D) is below ``min_expected_contrast`` has no reference for its skyline bands: clear air
would show no skyline in that light. A frame whose D is undefined (its near field darker than
``min_near_luma``) has no reference for **any** band, since the reference distance below needs
D, and it cannot serve in any band's pool. (The pre-registration of run 20261123-lodo1, §4,
says only the summit loses its reference then; that run never met an undefined D, so the code
was left as it is and the wording is to be fixed in the next pre-registration.)

**Reference**: for each band, the median Ẽ of the ``ref_k`` pool frames nearest in
(D, Sun altitude), by hypot(ΔD / ``scale_d``, Δalt / ``scale_alt_deg``), from local days other
than the frame's own (``exclude_same_day``) and other than any day the caller excludes (a held-
back fold). There is no hard limit, so a frame has a reference whenever the pool has a frame;
the distance to each reference is recorded. Sun azimuth is not used: within direct light the
dome's skyline energy changes little with the Sun's position, and v0's azimuth limit was what
left most frames without a reference. A band's **pool** is the frames whose label for that band
is visible (labels v0.2: both labellers agree) and that pass the PRD §8.6 gates; it is a fitted
asset (PRD §13.9), so only development frames the split lets fits use may enter it.

**Classes** (PRD §13.4), per band, from ratio = Ẽ / reference: ``visible`` if ratio ≥ τ_hi,
``hidden`` if ratio < τ_lo, ``partial`` between. τ_hi and τ_lo are fitted on development labels
(:mod:`.extractor_v0_2_lodo`), never fixed here. Frame classes come first, as in v0: ``night``
(Sun below 3° by ephemeris), ``camera_fault`` (D-037), ``excluded`` (another §8.6 gate failed),
``in_cloud`` (v−1's rule: the whole frame's energy and RMS contrast both below 0.25 and 0.35 of
their medians over the references of the first band in plate order, the summit at Rainier, as
the pre-registration's §4 fixes; a frame whose first band has no reference gets no in-cloud
test). **Obscuration height**: the highest band, in plate order, whose class is visible.

TODO(M2): the in-cloud thresholds are v−1's, set against sunny clear-day references. Against
v0.2's references, matched in D, a camera in fog reads 0.30-0.48 of its references' whole-frame
energy, so run 20261123-lodo1 flagged 1 of the 7 frames both labellers call in cloud and classed
the other 6 hidden. Fit or redesign the rule on labels before P1 (PRD §13.4) relies on it.

The same functions serve real and rendered frames (§13.3 "One extractor").
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray
from PIL import Image
from snow_globe_common.images import ImageSource, load_image

from .extractor import classify_ratio
from .extractor_v0 import BandEnergies, PlateBands, V0Params, band_energies, camera_fault
from .heldout import LeakageError, path_quarantine_reason

__all__ = [
    "EXTRACTOR_V0_2",
    "BandResultV02",
    "FrameResultV02",
    "Illumination",
    "IlluminationRegions",
    "PoolFrame",
    "Thresholds",
    "V02Params",
    "band_reference",
    "classify_frame_v02",
    "expected_contrast",
    "frame_features",
    "illumination",
    "load_sky_mask",
    "normalised_energy",
    "select_references",
]

EXTRACTOR_V0_2 = "v0.2"
type FrameClassV02 = Literal["night", "camera_fault", "excluded", "in_cloud", "scored"]


@dataclass(frozen=True, slots=True)
class V02Params:
    """Every number extractor v0.2 uses that is not fitted (see the module docstring).

    The energy and frame-class values are v0's. τ is not here: it is fitted
    (:class:`Thresholds`).
    """

    analysis_width: int = 960
    blur_sigma_px: float = 1.0
    local_mean_size_px: int = 31
    epsilon: float = 0.02
    min_band_cells: int = 50
    min_sun_alt_deg: float = 3.0
    in_cloud_energy_ratio_max: float = 0.25
    in_cloud_contrast_ratio_max: float = 0.35
    fault_dark_luma: float = 4.0 / 255.0
    fault_bright_luma: float = 251.0 / 255.0
    fault_fraction_min: float = 0.95
    fault_flat_range_max: float = 1.0 / 255.0
    near_window: tuple[float, float, float, float] = (0.0, 0.55, 1.0, 0.95)
    near_percentile: float = 99.0
    min_near_luma: float = 0.05
    skyline_bands: tuple[str, ...] = ("summit",)
    contrast_model: bool = True
    min_expected_contrast: float = 0.05
    ref_k: int = 3
    scale_d: float = 0.05
    scale_alt_deg: float = 10.0
    exclude_same_day: bool = True

    def __post_init__(self) -> None:
        if self.ref_k < 1 or self.scale_d <= 0 or self.scale_alt_deg <= 0:
            raise ValueError("ref_k >= 1 and positive distance scales")
        x0, y0, x1, y1 = self.near_window
        if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
            raise ValueError("near_window must be x0 < x1 and y0 < y1 within [0, 1]")
        if not 0.0 < self.near_percentile <= 100.0 or self.min_expected_contrast <= 0:
            raise ValueError("near_percentile in (0, 100] and a positive contrast floor")

    def energy_params(self) -> V0Params:
        """v0's parameters for :func:`.extractor_v0.band_energies` and the camera fault."""
        return V0Params(
            analysis_width=self.analysis_width,
            blur_sigma_px=self.blur_sigma_px,
            local_mean_size_px=self.local_mean_size_px,
            epsilon=self.epsilon,
            min_band_cells=self.min_band_cells,
            min_sun_alt_deg=self.min_sun_alt_deg,
            in_cloud_energy_ratio_max=self.in_cloud_energy_ratio_max,
            in_cloud_contrast_ratio_max=self.in_cloud_contrast_ratio_max,
            fault_dark_luma=self.fault_dark_luma,
            fault_bright_luma=self.fault_bright_luma,
            fault_fraction_min=self.fault_fraction_min,
            fault_flat_range_max=self.fault_flat_range_max,
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        out = asdict(self)
        out["near_window"] = list(self.near_window)
        out["skyline_bands"] = list(self.skyline_bands)
        return out


def load_sky_mask(plate_dir: str | Path, *, roots: Sequence[Path | None] = ()) -> NDArray[np.bool_]:
    """The plate's sky mask (``sky.png``, full resolution), checked against ``plate.json``.

    A plate folder under the quarantine folder is refused before it is read (D-022).
    """
    folder = Path(plate_dir).expanduser()
    reason = path_quarantine_reason(folder, roots=roots)
    if reason is not None:
        raise LeakageError(f"plate {folder}: {reason}")
    doc = json.loads((folder / "plate.json").read_text(encoding="utf-8"))
    entry = doc.get("files", {}).get("sky.png")
    if entry is None:
        raise ValueError(f"{folder}: plate.json lists no sky.png")
    path = folder / "sky.png"
    if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
        raise ValueError(f"{path}: SHA-256 differs from plate.json")
    with Image.open(path) as im:
        mask = np.asarray(im.convert("L")) > 127
    width, height = (int(v) for v in doc["frame_size"])
    if mask.shape != (height, width):
        raise ValueError(f"{path}: {mask.shape[::-1]} is not the plate's {width}x{height}")
    return mask


def _on_grid(mask: NDArray[np.bool_], size: tuple[int, int]) -> NDArray[np.bool_]:
    """A full-resolution mask area-averaged onto the analysis grid (in when at least half)."""
    small = Image.fromarray((mask * 255).astype(np.uint8)).resize(size, Image.Resampling.BOX)
    return np.asarray(small) >= 128


@dataclass(frozen=True)
class IlluminationRegions:
    """The sky and near-field cells of one plate on one analysis grid."""

    size: tuple[int, int]
    sky: NDArray[np.bool_]
    near: NDArray[np.bool_]

    @classmethod
    def build(
        cls,
        plate: PlateBands,
        sky_full: NDArray[np.bool_],
        size: tuple[int, int],
        params: V02Params,
    ) -> IlluminationRegions:
        """Sky cells, and near-field cells: ``near_window`` outside sky, bands and caption."""
        width, height = size
        if sky_full.shape != (plate.frame_size[1], plate.frame_size[0]):
            raise ValueError("the sky mask is not the plate's size")
        grids = plate.on_grid(size)
        frame = grids.pop("_frame")
        sky = _on_grid(sky_full, size) & frame
        near = np.zeros((height, width), dtype=bool)
        x0, y0, x1, y1 = params.near_window
        near[round(y0 * height) : round(y1 * height), round(x0 * width) : round(x1 * width)] = True
        near &= frame & ~sky
        for cells in grids.values():
            near &= ~cells
        if int(sky.sum()) < params.min_band_cells or int(near.sum()) < params.min_band_cells:
            raise ValueError("the sky or the near field covers too few analysis cells")
        return cls(size=size, sky=sky, near=near)


def expected_contrast(d_index: float) -> float:
    """C(D) = 2·|1 − D| / (1 + D): the Weber contrast of lit snow against the sky (D ≥ 0)."""
    if not math.isfinite(d_index) or d_index < 0:
        raise ValueError("D must be finite and non-negative")
    return 2.0 * abs(1.0 - d_index) / (1.0 + d_index)


@dataclass(frozen=True)
class Illumination:
    """A frame's light: D, its two parts, the sky's blueness (a diagnostic) and C(D)."""

    d_index: float | None
    sky_median_luma: float
    near_luma_percentile: float
    sky_blueness: float
    expected_contrast: float | None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {
            "d_index": None if self.d_index is None else round(self.d_index, 6),
            "sky_median_luma": round(self.sky_median_luma, 6),
            "near_luma_percentile": round(self.near_luma_percentile, 6),
            "sky_blueness": round(self.sky_blueness, 6),
            "expected_contrast": None
            if self.expected_contrast is None
            else round(self.expected_contrast, 6),
        }

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> Illumination:
        """Inverse of :meth:`to_dict` (rounded values)."""
        return cls(
            d_index=doc["d_index"],
            sky_median_luma=float(doc["sky_median_luma"]),
            near_luma_percentile=float(doc["near_luma_percentile"]),
            sky_blueness=float(doc["sky_blueness"]),
            expected_contrast=doc["expected_contrast"],
        )


def illumination(
    rgb: NDArray[np.float64], regions: IlluminationRegions, params: V02Params
) -> Illumination:
    """D and C(D) from an RGB array in [0, 1] on the analysis grid (module docstring).

    D is None when the near field's percentile luma is under ``min_near_luma``: too dark to
    say how lit snow looks. Such a frame then has no reference for any band (the reference
    distance needs D) and serves in no pool.
    """
    if rgb.shape[:2] != (regions.size[1], regions.size[0]) or rgb.ndim != 3:
        raise ValueError("the RGB array is not on the regions' grid")
    y = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
    sky_median = float(np.median(y[regions.sky]))
    near_level = float(np.percentile(y[regions.near], params.near_percentile))
    r, g, b = (float(np.median(rgb[..., i][regions.sky])) for i in range(3))
    blueness = (b - r) / max(1e-6, r + g + b)
    if near_level < params.min_near_luma:
        return Illumination(None, sky_median, near_level, blueness, None)
    d_index = sky_median / near_level
    return Illumination(d_index, sky_median, near_level, blueness, expected_contrast(d_index))


def frame_features(
    image: ImageSource,
    plate: PlateBands,
    regions_by_size: dict[tuple[int, int], IlluminationRegions],
    sky_full: NDArray[np.bool_],
    params: V02Params,
) -> tuple[BandEnergies, Illumination]:
    """v0's band energies and v0.2's illumination from one decode of the frame."""
    pil = load_image(image)
    if pil.mode != "RGB":
        pil = pil.convert("RGB")
    energies = band_energies(pil, plate, params.energy_params())
    size = energies.analysis_size
    if size not in regions_by_size:
        regions_by_size[size] = IlluminationRegions.build(plate, sky_full, size, params)
    small = pil if pil.size == size else pil.resize(size, Image.Resampling.BOX)
    rgb = np.asarray(small, dtype=np.float64) / 255.0
    return energies, illumination(rgb, regions_by_size[size], params)


def normalised_energy(
    band: str, energy: float, light: Illumination, params: V02Params
) -> float | None:
    """Ẽ: E / C(D) for a skyline band under the contrast model, else E; None when undefined."""
    if not params.contrast_model or band not in params.skyline_bands:
        return energy
    contrast = light.expected_contrast
    if contrast is None or contrast < params.min_expected_contrast:
        return None
    return energy / contrast


@dataclass(frozen=True)
class PoolFrame:
    """One reference-pool frame for one band: identity, day, light and energies."""

    key: str
    local_day: str
    sun_alt_deg: float
    d_index: float
    energy: float
    normalised: float
    global_energy: float
    rms_contrast: float


def select_references(
    pool: Sequence[PoolFrame],
    *,
    d_index: float,
    sun_alt_deg: float,
    params: V02Params,
    local_day: str | None = None,
    exclude_days: Sequence[str] = (),
    exclude_keys: Sequence[str] = (),
) -> list[tuple[float, PoolFrame]]:
    """The ``ref_k`` nearest pool frames in (D, Sun altitude), with their distances.

    Never a frame of ``local_day`` (with ``exclude_same_day``), of ``exclude_days`` or of
    ``exclude_keys``; ties go to the smaller key, so the choice is deterministic.
    """
    blocked_days = set(exclude_days)
    if params.exclude_same_day and local_day is not None:
        blocked_days.add(local_day)
    blocked_keys = set(exclude_keys)
    ranked = []
    for ref in pool:
        if ref.local_day in blocked_days or ref.key in blocked_keys:
            continue
        distance = math.hypot(
            (ref.d_index - d_index) / params.scale_d,
            (ref.sun_alt_deg - sun_alt_deg) / params.scale_alt_deg,
        )
        ranked.append((distance, ref.key, ref))
    ranked.sort(key=lambda r: (r[0], r[1]))
    return [(d, ref) for d, _, ref in ranked[: params.ref_k]]


@dataclass(frozen=True)
class Thresholds:
    """A band's fitted τ (visible ≥ tau_hi > partial ≥ tau_lo > hidden)."""

    tau_hi: float
    tau_lo: float

    def __post_init__(self) -> None:
        if not (0.0 < self.tau_lo <= self.tau_hi and math.isfinite(self.tau_hi)):
            raise ValueError("need 0 < tau_lo <= tau_hi, finite")


@dataclass(frozen=True)
class BandResultV02:
    """One band: energy, Ẽ, the reference, the ratio, the state, and why a state is missing."""

    energy: float
    normalised_energy: float | None
    reference: float | None
    ratio: float | None
    state: str | None
    references: tuple[tuple[str, float], ...] = ()
    missing: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {
            "energy": round(self.energy, 6),
            "normalised_energy": None
            if self.normalised_energy is None
            else round(self.normalised_energy, 6),
            "reference": None if self.reference is None else round(self.reference, 6),
            "ratio": None if self.ratio is None else round(self.ratio, 4),
            "state": self.state,
            "references": [{"key": k, "distance": round(d, 4)} for k, d in self.references],
            "missing": self.missing,
        }


def band_reference(
    band: str,
    energies: BandEnergies,
    light: Illumination,
    *,
    sun_alt_deg: float,
    pool: Sequence[PoolFrame],
    params: V02Params,
    local_day: str | None,
    exclude_days: Sequence[str] = (),
    exclude_keys: Sequence[str] = (),
    thresholds: Thresholds | None = None,
) -> tuple[BandResultV02, list[PoolFrame]]:
    """One band's ratio (and state when ``thresholds`` are given) and the references used.

    With D undefined no band has a reference, skyline or not: the distance to a reference
    needs D (module docstring).
    """
    energy = energies.bands[band]
    if light.d_index is None:
        why = "D is undefined (the near field is too dark), so no reference can be chosen"
        return BandResultV02(energy, None, None, None, None, missing=why), []
    norm = normalised_energy(band, energy, light, params)
    if norm is None:
        why = "no expected contrast: C(D) is under the floor"
        return BandResultV02(energy, None, None, None, None, missing=why), []
    chosen = select_references(
        pool,
        d_index=light.d_index,
        sun_alt_deg=sun_alt_deg,
        params=params,
        local_day=local_day,
        exclude_days=exclude_days,
        exclude_keys=exclude_keys,
    )
    if not chosen:
        return BandResultV02(energy, norm, None, None, None, missing="no_reference"), []
    reference = float(np.median([ref.normalised for _, ref in chosen]))
    ratio = norm / reference if reference > 0 else None
    refs = tuple((ref.key, d) for d, ref in chosen)
    if ratio is None:
        return BandResultV02(energy, norm, reference, None, None, refs, "zero reference"), []
    if thresholds is None:
        return BandResultV02(energy, norm, reference, ratio, None, refs, "no_threshold"), [
            ref for _, ref in chosen
        ]
    state = classify_ratio(ratio, tau_hi=thresholds.tau_hi, tau_lo=thresholds.tau_lo)
    return BandResultV02(energy, norm, reference, ratio, state, refs), [ref for _, ref in chosen]


@dataclass(frozen=True)
class FrameResultV02:
    """Extractor v0.2's output for one frame."""

    frame_class: FrameClassV02
    reason: str | None
    sun_alt_deg: float
    sun_az_deg: float
    light: Illumination
    bands: dict[str, BandResultV02]
    obscuration_height: str | None
    global_energy_ratio: float | None = None
    contrast_ratio: float | None = None
    camera_fault: str | None = None

    def band_class(self, name: str) -> str | None:
        """The frame class unless scored; else the band's state, or why it has none."""
        if self.frame_class != "scored":
            return self.frame_class
        band = self.bands[name]
        return band.state if band.state is not None else band.missing

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {
            "extractor_version": EXTRACTOR_V0_2,
            "frame_class": self.frame_class,
            "reason": self.reason,
            "camera_fault": self.camera_fault,
            "sun_alt_deg": round(self.sun_alt_deg, 3),
            "sun_az_deg": round(self.sun_az_deg, 3),
            "light": self.light.to_dict(),
            "bands": {
                name: {**b.to_dict(), "class": self.band_class(name)}
                for name, b in self.bands.items()
            },
            "obscuration_height": self.obscuration_height,
            "global_energy_ratio": None
            if self.global_energy_ratio is None
            else round(self.global_energy_ratio, 4),
            "contrast_ratio": None
            if self.contrast_ratio is None
            else round(self.contrast_ratio, 4),
        }


def classify_frame_v02(
    energies: BandEnergies,
    light: Illumination,
    *,
    sun_alt_deg: float,
    sun_az_deg: float,
    order: Sequence[str],
    pools: Mapping[str, Sequence[PoolFrame]],
    thresholds: Mapping[str, Thresholds | None],
    params: V02Params,
    local_day: str | None,
    exclude_days: Sequence[str] = (),
    exclude_keys: Sequence[str] = (),
    excluded_because: str | None = None,
) -> FrameResultV02:
    """Classes for one frame (module docstring). A band with no pool or no τ gets no state.

    ``excluded_because`` is a failed §8.6 gate other than Daylight and the camera fault,
    which this function checks itself.
    """
    blank = {
        n: BandResultV02(energies.bands[n], None, None, None, None, missing="not scored")
        for n in order
    }
    if sun_alt_deg < params.min_sun_alt_deg:
        why = f"Sun {sun_alt_deg:.2f} deg is below {params.min_sun_alt_deg} deg"
        return FrameResultV02("night", why, sun_alt_deg, sun_az_deg, light, blank, None)
    fault = camera_fault(energies, params.energy_params())
    if fault is not None:
        return FrameResultV02(
            "camera_fault",
            f"camera fault: {fault} frame",
            sun_alt_deg,
            sun_az_deg,
            light,
            blank,
            None,
            camera_fault=fault,
        )
    if excluded_because is not None:
        return FrameResultV02(
            "excluded", excluded_because, sun_alt_deg, sun_az_deg, light, blank, None
        )
    bands: dict[str, BandResultV02] = {}
    first_refs: list[PoolFrame] = []
    for name in order:
        result, used = band_reference(
            name,
            energies,
            light,
            sun_alt_deg=sun_alt_deg,
            pool=pools.get(name, ()),
            params=params,
            local_day=local_day,
            exclude_days=exclude_days,
            exclude_keys=exclude_keys,
            thresholds=thresholds.get(name),
        )
        bands[name] = result
        if name == order[0]:
            # the in-cloud test reads the first band's references only (pre-registration §4)
            first_refs = used
    g_ratio = c_ratio = None
    if first_refs:
        g_ref = float(np.median([r.global_energy for r in first_refs]))
        c_ref = float(np.median([r.rms_contrast for r in first_refs]))
        g_ratio = energies.global_energy / g_ref if g_ref > 0 else None
        c_ratio = energies.rms_contrast / c_ref if c_ref > 0 else None
    if (
        g_ratio is not None
        and c_ratio is not None
        and g_ratio < params.in_cloud_energy_ratio_max
        and c_ratio < params.in_cloud_contrast_ratio_max
    ):
        return FrameResultV02(
            "in_cloud",
            "whole-frame energy and contrast collapsed (camera in cloud)",
            sun_alt_deg,
            sun_az_deg,
            light,
            bands,
            None,
            g_ratio,
            c_ratio,
        )
    height = next((n for n in order if bands[n].state == "visible"), None)
    return FrameResultV02(
        "scored", None, sun_alt_deg, sun_az_deg, light, bands, height, g_ratio, c_ratio
    )
