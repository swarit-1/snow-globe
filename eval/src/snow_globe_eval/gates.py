"""Frame quality gates (PRD §8.6): which captured frames are scored, and why the others are not.

PRD §8.6: "A captured frame is scored only if it passes all of these. Exclusions apply
identically to the model and to every baseline, and their counts are published."

======================  ============================================================  ==========
Gate                    Test here                                                     Status
======================  ============================================================  ==========
``fresh``               NPS image time, HTTP ``Last-Modified`` and SHA-256 all differ  done
                        from the previous capture (sidecar fields)
``same_view``           registration against the plate                                TODO(M2)
``clear_lens``          sharpness above a per-viewpoint floor; reported with and      TODO(M2)
                        without iced-lens exclusions
``colour_mode``         snow-globe-common ``chroma_spread`` above a cut-off (monochrome  cut-off
                        infrared frames are flagged)                                  TODO(M2)
``daylight``            ephemeris Sun altitude, never brightness (:mod:`.daylight`)   done
``usable_frame``        capture's ``decode_failed`` / ``tiny_file`` flags and the     done (v−1)
                        extractor's camera fault (dark, saturated, blank)
======================  ============================================================  ==========

``usable_frame`` is an addition to the PRD's list: a black or saturated frame is a capture
failure, not weather, and must not be scored as "in cloud".

A gate that is not implemented, or has nothing to compare, comes back as *not checked*
(``passed is None``) and is named in :attr:`FrameGates.not_checked`, so a report says which
gates a score did **not** apply. A frame is excluded when a checked gate fails.

The colour-mode cut-off is a required argument with no default: the two local night samples
measure ``chroma_spread`` 0.00 and 0.01 (on 0–255), but no development day frames have been
measured yet (TODO(M2), as snow-globe-common's docstring asks).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
from numpy.typing import NDArray
from snow_globe_common.manifest import Viewpoint
from snow_globe_common.timeutil import parse_iso_utc

from .daylight import P1_MIN_SUN_ALT_DEG, sun_altitude_deg
from .ephemeris import EphemerisBackend

__all__ = [
    "CAPTURE_FAULT_FLAGS",
    "GATES",
    "FrameGates",
    "GateResult",
    "clear_lens_gate",
    "colour_mode_gate",
    "daylight_gate",
    "excluded_mask",
    "exclusion_counts",
    "frame_gates",
    "fresh_gate",
    "same_view_gate",
    "usable_frame_gate",
]

GATES: tuple[str, ...] = (
    "fresh",
    "same_view",
    "clear_lens",
    "colour_mode",
    "daylight",
    "usable_frame",
)
#: snow-globe-capture sidecar flags that make a frame unusable.
CAPTURE_FAULT_FLAGS: tuple[str, ...] = ("decode_failed", "tiny_file")


@dataclass(frozen=True, slots=True)
class GateResult:
    """One gate's verdict: passed, failed, or ``None`` (not checked), with the reason."""

    gate: str
    passed: bool | None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {"gate": self.gate, "passed": self.passed, "reason": self.reason}


@dataclass(frozen=True)
class FrameGates:
    """Every §8.6 gate for one frame."""

    results: tuple[GateResult, ...]

    def __post_init__(self) -> None:
        names = [r.gate for r in self.results]
        if sorted(names) != sorted(GATES):
            raise ValueError(f"FrameGates needs one result per gate {GATES}, got {names}")

    def result(self, gate: str) -> GateResult:
        """The result of one gate."""
        return next(r for r in self.results if r.gate == gate)

    @property
    def excluded_by(self) -> tuple[str, ...]:
        """Gates that were checked and failed."""
        return tuple(r.gate for r in self.results if r.passed is False)

    @property
    def not_checked(self) -> tuple[str, ...]:
        """Gates that were not applied (not implemented, or nothing to compare)."""
        return tuple(r.gate for r in self.results if r.passed is None)

    @property
    def scored(self) -> bool:
        """True unless a checked gate failed (see :attr:`not_checked` for what was not)."""
        return not self.excluded_by

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {
            "scored": self.scored,
            "excluded_by": list(self.excluded_by),
            "not_checked": list(self.not_checked),
            "results": [r.to_dict() for r in self.results],
        }


def _sidecar_time(sidecar: Mapping[str, Any], name: str) -> datetime | None:
    value = sidecar.get(name)
    if not value:
        return None
    try:
        return parse_iso_utc(str(value))
    except ValueError:
        return None


def fresh_gate(current: Mapping[str, Any], previous: Mapping[str, Any] | None) -> GateResult:
    """Fresh (PRD §8.6): image time, HTTP Last-Modified and SHA-256 all differ from before.

    ``current`` and ``previous`` are snow-globe-capture sidecars of the same viewpoint (the
    previous one is the capture just before). A field missing on either side is skipped
    and named; with nothing to compare, the gate is not checked.
    """
    if previous is None:
        return GateResult("fresh", None, "no previous capture to compare with")
    compared: list[str] = []
    same: list[str] = []
    skipped: list[str] = []
    pairs = (
        (
            "image time",
            _sidecar_time(current, "image_time_utc"),
            _sidecar_time(previous, "image_time_utc"),
        ),
        (
            "HTTP Last-Modified",
            current.get("http_last_modified"),
            previous.get("http_last_modified"),
        ),
        ("SHA-256", current.get("sha256"), previous.get("sha256")),
    )
    for name, now, before in pairs:
        if now is None or before is None:
            skipped.append(name)
            continue
        compared.append(name)
        if now == before:
            same.append(name)
    if not compared:
        return GateResult("fresh", None, "no field to compare with the previous capture")
    note = f" (not compared: {', '.join(skipped)})" if skipped else ""
    if same:
        return GateResult("fresh", False, f"same {', '.join(same)} as the previous capture{note}")
    return GateResult("fresh", True, f"compared {', '.join(compared)}{note}")


def same_view_gate() -> GateResult:
    """Same view (PRD §8.6): registration against the plate. Not implemented yet."""
    return GateResult("same_view", None, "TODO(M2): registration against the plate (§8.5)")


def clear_lens_gate() -> GateResult:
    """Clear lens (PRD §8.6): sharpness above a floor. Not implemented yet."""
    return GateResult(
        "clear_lens",
        None,
        "TODO(M2): sharpness floor fitted per viewpoint on development frames; reported "
        "with and without iced-lens exclusions",
    )


def colour_mode_gate(chroma_spread: float | None, *, max_chroma_spread: float | None) -> GateResult:
    """Colour mode (PRD §8.6): a monochrome (infrared) frame is flagged and not scored in v1.

    ``chroma_spread`` is snow-globe-common's :func:`~snow_globe_common.images.chroma_spread`
    (mean per-pixel channel spread, 0–255; capture stores it in the sidecar). The frame is
    monochrome when it is at or below ``max_chroma_spread``. Without a cut-off, or without
    the measurement, the gate is not checked.
    """
    if max_chroma_spread is None:
        return GateResult(
            "colour_mode",
            None,
            "TODO(M2): cut-off not calibrated on development day and night frames",
        )
    if chroma_spread is None:
        return GateResult("colour_mode", None, "no chroma_spread measured for this frame")
    if chroma_spread <= max_chroma_spread:
        return GateResult(
            "colour_mode",
            False,
            f"monochrome (infrared) frame: chroma_spread {chroma_spread:.3g} <= "
            f"{max_chroma_spread:.3g}",
        )
    return GateResult("colour_mode", True, f"chroma_spread {chroma_spread:.3g}")


def daylight_gate(
    viewpoint: Viewpoint,
    image_time: datetime,
    *,
    min_sun_alt_deg: float = P1_MIN_SUN_ALT_DEG,
    refracted: bool = False,
    ephemeris: EphemerisBackend | None = None,
) -> GateResult:
    """Daylight (PRD §8.6): the ephemeris Sun altitude at the camera, never brightness."""
    cam = viewpoint.camera
    altitude = sun_altitude_deg(
        image_time, cam.lat, cam.lon, cam.elevation_m, refracted=refracted, ephemeris=ephemeris
    )
    kind = "refracted" if refracted else "geometric"
    reason = f"Sun {altitude:.2f} deg ({kind}); threshold {min_sun_alt_deg:g} deg"
    return GateResult("daylight", altitude >= min_sun_alt_deg, reason)


def usable_frame_gate(
    *, capture_flags: Collection[str] = (), camera_fault: str | None = None
) -> GateResult:
    """Usable frame: no capture failure flag and no camera fault from the extractor."""
    problems = [f"capture flag {flag}" for flag in CAPTURE_FAULT_FLAGS if flag in capture_flags]
    if camera_fault:
        problems.append(f"camera fault ({camera_fault})")
    if problems:
        return GateResult("usable_frame", False, "; ".join(problems))
    return GateResult("usable_frame", True, None)


def frame_gates(
    *,
    viewpoint: Viewpoint,
    image_time: datetime,
    sidecar: Mapping[str, Any] | None = None,
    previous_sidecar: Mapping[str, Any] | None = None,
    camera_fault: str | None = None,
    max_chroma_spread: float | None = None,
    min_sun_alt_deg: float = P1_MIN_SUN_ALT_DEG,
    refracted: bool = False,
    ephemeris: EphemerisBackend | None = None,
) -> FrameGates:
    """Apply every §8.6 gate to one frame (see the module docstring for what each checks)."""
    side = dict(sidecar or {})
    chroma = side.get("chroma_spread")
    return FrameGates(
        (
            fresh_gate(side, previous_sidecar) if side else GateResult("fresh", None, "no sidecar"),
            same_view_gate(),
            clear_lens_gate(),
            colour_mode_gate(
                None if chroma is None else float(chroma), max_chroma_spread=max_chroma_spread
            ),
            daylight_gate(
                viewpoint,
                image_time,
                min_sun_alt_deg=min_sun_alt_deg,
                refracted=refracted,
                ephemeris=ephemeris,
            ),
            usable_frame_gate(capture_flags=side.get("flags") or (), camera_fault=camera_fault),
        )
    )


def excluded_mask(gates: Sequence[FrameGates]) -> NDArray[np.bool_]:
    """The ``excluded`` mask for :func:`.scores.contingency`: True where a gate failed."""
    return np.array([not g.scored for g in gates], dtype=bool)


def exclusion_counts(gates: Iterable[FrameGates]) -> dict[str, Any]:
    """Counts to publish (PRD §8.6): frames, exclusions per gate, and gates not checked.

    A frame that fails several gates counts once under each, and once in ``excluded``.
    """
    items = list(gates)
    return {
        "frames": len(items),
        "excluded": sum(not g.scored for g in items),
        "excluded_by": {name: sum(name in g.excluded_by for g in items) for name in GATES},
        "not_checked": {name: sum(name in g.not_checked for g in items) for name in GATES},
    }
