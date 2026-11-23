"""The extractor of the dev P1 table, run on real frames and renders alike (PRD §13.3, §13.4).

PRD §13.4 P1: the outcome is the summit band's class in the real frame, and the prediction is
"the same extractor, run on the rendered frame"; §13.3: "One extractor is applied to rendered
frames, baseline frames and real frames alike". So one object measures both, with one set of
parameters (τ included), one clear-day reference pool and one reference choice per frame.

The extractor is a parameter of the table (:data:`EXTRACTORS`). **v0** (:class:`V0Extractor`)
is the default; a later version (v0.2, once it passes its §13.8 bar) is added by writing a class
with the same methods (:class:`P1Extractor`) and registering it, then re-running the table.

**v0** is :mod:`.extractor_v0` as :func:`.extractor_v0_run.run` applies it, unchanged:

- :meth:`V0Extractor.prepare` runs :func:`.extractor_v0_run.run` once over every development
  capture of the viewpoint (the PRD §8.6 gates, the clear-day pool from the labels file, the
  validation against the hand labels), into the table run's own folder. Its ``results.jsonl``
  gives every real frame's class (the truth, and the labels persistence and climatology use).
- The pool is rebuilt from those results: the frames marked ``in_reference_pool``, with their
  band energies as written (full precision), and the Sun positions recomputed with the same
  ephemeris and manifest camera at once, checked against the rounded values the run wrote, and
  kept in ``p1dev_state.json`` so a resumed table never recomputes them.
- :meth:`V0Extractor.measure` scores a render with :func:`.extractor_v0.band_energies` and
  :func:`.extractor_v0.classify_frame` against that pool, at the **truth frame's** Sun position
  and local day, excluding the truth frame itself: the render depicts the valid time, which is
  the truth frame's image time, so both get the same references (each record says whether they
  did). No §8.6 gate applies to a render; v0's camera-fault and in-cloud rules do.
- :meth:`V0Extractor.readings_as_of` gives the classes **a baseline may use at an issue
  time** (PRD §13.1: "Baselines see only frames available at issue time"). A frame's class
  depends on its clear-day references, and the run's pool holds frames from every day, the
  valid day (even the valid frame) included. So each frame imaged and available by the issue
  time is classified again from its stored energies, with the pool **cut to the frames imaged
  and available by the issue time** (:func:`readings_as_of_v0`); nothing after the issue time
  can reach a baseline's label. Classes that do not depend on the pool (``night``,
  ``camera_fault``, ``excluded``) are kept. The truth (the outcome) and the render keep the
  whole pool.

The P1 class of a reading is the band's class when the frame is scored (``visible``,
``partial``, ``hidden``), else the frame class (``in_cloud``, ``camera_fault``, ``excluded``
by a gate, ``no_reference``, ``night``).

**Code.** A resumed table reuses the extractor stage only when the extractor's code is the code
that ran it (:func:`.extractor_v0_run.code_identity`); otherwise the truth would come from one
version and the renders' readings from another, and the table refuses (use a new run id).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from snow_globe_common.manifest import load_manifest
from snow_globe_common.timeutil import parse_iso_utc

from .ephemeris import EphemerisBackend, get_ephemeris, sun_position
from .extractor_v0 import (
    EXTRACTOR_V0,
    BandEnergies,
    FrameResult,
    PlateBands,
    ReferenceFrameV0,
    V0Params,
    band_energies,
    classify_frame,
)
from .extractor_v0_run import code_identity as extractor_v0_code_identity
from .extractor_v0_run import run as run_extractor_v0

__all__ = [
    "EXTRACTORS",
    "POOL_INDEPENDENT_CLASSES",
    "P1Extractor",
    "Reading",
    "V0Extractor",
    "p1_class_of",
    "readings_as_of_v0",
]

#: Frame classes that do not depend on the clear-day pool (kept as they are at any issue time).
POOL_INDEPENDENT_CLASSES: tuple[str, ...] = ("night", "camera_fault", "excluded")

#: Rounding of written Sun positions in ``results.jsonl`` (``FrameResult.to_dict``).
_SUN_ROUND_DEG = 0.0005 + 1e-9
STATE_FILE = "p1dev_state.json"


@dataclass(frozen=True)
class Reading:
    """One frame's (or render's) extractor output for the P1 band."""

    extractor: str
    frame_class: str
    p1_class: str | None
    ratio: float | None
    reason: str | None
    references: tuple[str, ...] = ()
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {
            "extractor": self.extractor,
            "frame_class": self.frame_class,
            "p1_class": self.p1_class,
            "ratio": self.ratio,
            "reason": self.reason,
            "references": list(self.references),
            "detail": dict(self.detail),
        }


def p1_class_of(frame_class: str, band_state: str | None) -> str | None:
    """The P1 class: the band's state for a scored frame, else the frame class."""
    return band_state if frame_class == "scored" else frame_class


class P1Extractor(Protocol):
    """What the table needs from an extractor version (see the module docstring)."""

    version: str
    band: str

    def prepare(self) -> None:
        """Classify every development capture of the viewpoint (resumable)."""

    def truth_readings(self) -> Mapping[str, Reading]:
        """Every classified real frame's reading, by frame key."""

    def frame_meta(self, key: str) -> Mapping[str, Any]:
        """A classified frame's SHA-256, image time, local day and Sun position."""

    def measure(self, image: Path, *, truth_key: str) -> Reading:
        """A render's reading, against the truth frame's references."""

    def readings_as_of(
        self, moment: datetime, available: Mapping[str, datetime]
    ) -> Mapping[str, Reading]:
        """Readings a baseline may use at ``moment``: frames and references available by then."""

    def reference_times(self) -> Mapping[str, datetime]:
        """Every clear-day reference frame's image time, by key."""

    def identity(self) -> dict[str, Any]:
        """Version, parameters, plate, labels, pool and code: what the readings depend on."""

    def validation(self) -> dict[str, Any]:
        """The PRD §13.8 validation: ``publishable`` and ``not_publishable_because``."""

    def provisional_notes(self) -> list[str]:
        """Why the extractor's numbers are provisional, besides its validation (its τ, ...)."""


def _energies(data: Mapping[str, Any]) -> BandEnergies:
    return BandEnergies(
        bands={str(k): float(v) for k, v in data["bands"].items()},
        global_energy=float(data["global_energy"]),
        rms_contrast=float(data["rms_contrast"]),
        dark_fraction=float(data["dark_fraction"]),
        bright_fraction=float(data["bright_fraction"]),
        luma_range=float(data["luma_range"]),
        analysis_size=(int(data["analysis_size"][0]), int(data["analysis_size"][1])),
    )


def _reading_from_result(result: FrameResult, band: str) -> Reading:
    data = result.to_dict()
    entry = data["bands"][band]
    return Reading(
        extractor=EXTRACTOR_V0,
        frame_class=result.frame_class,
        p1_class=p1_class_of(result.frame_class, result.bands[band].state),
        ratio=entry["ratio"],
        reason=result.reason,
        references=tuple(r["key"] for r in result.references),
        detail={
            "energy": entry["energy"],
            "reference_energy": entry["reference_energy"],
            "camera_fault": result.camera_fault,
            "global_energy_ratio": data["global_energy_ratio"],
            "contrast_ratio": data["contrast_ratio"],
            "obscuration_height": result.obscuration_height,
            "bands": {name: b["class"] for name, b in data["bands"].items()},
        },
    )


def readings_as_of_v0(
    records: Mapping[str, Mapping[str, Any]],
    energies: Mapping[str, BandEnergies],
    sun: Mapping[str, tuple[float, float]],
    pool: Sequence[ReferenceFrameV0],
    *,
    moment: datetime,
    available: Mapping[str, datetime],
    params: V0Params,
    order: Sequence[str],
    band: str,
    stored: Mapping[str, Reading],
) -> dict[str, Reading]:
    """Extractor v0's readings as a baseline issued at ``moment`` may use them (PRD §13.1).

    Every frame of ``records`` (the run's ``results.jsonl`` by key) imaged and available by
    ``moment`` is classified again from its stored ``energies`` and full-precision ``sun``, with
    ``pool`` cut to the clear-day frames imaged and available by ``moment``; v0's own rules
    still apply (no pool frame from the frame's own local day, never the frame itself). A frame
    or pool frame with no time in ``available`` counts as never available (fail closed). The
    classes that do not depend on the pool (:data:`POOL_INDEPENDENT_CLASSES`) are the run's own
    (``stored``). Frames after ``moment`` get no reading.
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("moment must be timezone-aware")

    def by_then(key: str, image_time: datetime) -> bool:
        when = available.get(key)
        return when is not None and image_time <= moment and when <= moment

    cut = [ref for ref in pool if by_then(ref.key, ref.image_time)]
    out: dict[str, Reading] = {}
    for key, rec in records.items():
        if not by_then(key, parse_iso_utc(rec["image_time_utc"])):
            continue
        if rec["frame_class"] in POOL_INDEPENDENT_CLASSES:
            out[key] = stored[key]
            continue
        alt, az = sun[key]
        result = classify_frame(
            energies[key],
            sun_alt_deg=alt,
            sun_az_deg=az,
            order=order,
            pool=cut,
            params=params,
            local_day=rec["local_day"],
            exclude_keys=(key,),
        )
        out[key] = _reading_from_result(result, band)
    return out


class V0Extractor:
    """Extractor v0 for the dev P1 table (see the module docstring)."""

    version = EXTRACTOR_V0

    def __init__(
        self,
        *,
        data_root: Path,
        viewpoint_id: str,
        plate_dir: Path,
        work_dir: Path,
        labels_path: Path | None = None,
        params: V0Params | None = None,
        ephemeris: EphemerisBackend | None = None,
        band: str = "summit",
        require_same_code: bool = True,
    ) -> None:
        self.data_root = data_root
        self.viewpoint_id = viewpoint_id
        self.plate_dir = plate_dir
        self.work_dir = work_dir
        self.labels_path = labels_path
        self.params = params or V0Params()
        self.backend = ephemeris or get_ephemeris()
        self.band = band
        #: Refuse a stored extractor stage made by other extractor code (a table-only rebuild
        #: passes False: it reads the stored readings and classifies nothing).
        self.require_same_code = require_same_code
        self._plate: PlateBands | None = None
        self._records: dict[str, dict[str, Any]] = {}
        self._frame_energies: dict[str, BandEnergies] = {}
        self._sun: dict[str, tuple[float, float]] = {}
        self._pool: list[ReferenceFrameV0] = []
        self._summary: dict[str, Any] = {}
        self._validation: dict[str, Any] = {}
        self._state: dict[str, Any] = {}
        self._stored: dict[str, Reading] = {}
        self._as_of: dict[datetime, dict[str, Reading]] = {}
        self._as_of_available: Mapping[str, datetime] | None = None
        self._code_now: dict[str, Any] = {}

    # -- preparation -----------------------------------------------------------------------
    def _done(self) -> bool:
        return all(
            (self.work_dir / name).is_file()
            for name in ("results.jsonl", "summary.json", "validation.json", STATE_FILE)
        )

    def prepare(self) -> None:
        """Run extractor v0 over every development capture, once; a resumed table reloads it."""
        self._plate = PlateBands.load(self.plate_dir, roots=(self.data_root,))
        if self.band not in self._plate.order:
            raise ValueError(f"the plate has no band {self.band!r} (bands {self._plate.order})")
        if not self._done():
            if self.work_dir.exists() and any(self.work_dir.iterdir()):
                raise RuntimeError(
                    f"{self.work_dir} holds an unfinished extractor run; remove it to start again"
                )
            run_extractor_v0(
                data_root=self.data_root,
                viewpoint_id=self.viewpoint_id,
                plate_dir=self.plate_dir,
                labels_path=self.labels_path,
                out_dir=self.work_dir,
                params=self.params,
                ephemeris=self.backend,
            )
            self._write_state()
        self._load()

    def _results(self) -> dict[str, dict[str, Any]]:
        out = {}
        for line in (self.work_dir / "results.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                out[rec["key"]] = rec
        return out

    def _write_state(self) -> None:
        """Full-precision Sun positions of every classified frame, checked against the run's."""
        camera = load_manifest(self.viewpoint_id).camera
        sun: dict[str, list[float]] = {}
        for key, rec in self._results().items():
            moment = parse_iso_utc(rec["image_time_utc"])
            pos = sun_position(
                moment, camera.lat, camera.lon, camera.elevation_m, ephemeris=self.backend
            )
            for value, written in (
                (pos.altitude_deg, rec["sun_alt_deg"]),
                (pos.azimuth_deg, rec["sun_az_deg"]),
            ):
                if abs(round(value, 3) - float(written)) > _SUN_ROUND_DEG:
                    raise RuntimeError(
                        f"{key}: the Sun recomputed for the extractor's results ({value:.4f} deg) "
                        f"is not the run's ({written}); did the manifest or the ephemeris change "
                        "during the run? Remove the extractor folder and start again"
                    )
            sun[key] = [pos.altitude_deg, pos.azimuth_deg]
        state = {
            "kind": "snow-globe.p1dev-extractor-state",
            "extractor": self.version,
            "manifest_camera": {
                "lat": camera.lat,
                "lon": camera.lon,
                "elevation_m": camera.elevation_m,
            },
            "ephemeris": {"backend": self.backend.name, "version": self.backend.version},
            "sun_deg": sun,
        }
        (self.work_dir / STATE_FILE).write_text(
            json.dumps(state, indent=1) + "\n", encoding="utf-8"
        )

    def _load(self) -> None:
        self._records = self._results()
        self._state = json.loads((self.work_dir / STATE_FILE).read_text(encoding="utf-8"))
        self._sun = {k: (float(v[0]), float(v[1])) for k, v in self._state["sun_deg"].items()}
        missing = sorted(set(self._records) - set(self._sun))
        if missing:
            raise RuntimeError(f"{STATE_FILE} lacks the Sun of {len(missing)} frames")
        self._summary = json.loads((self.work_dir / "summary.json").read_text(encoding="utf-8"))
        self._validation = json.loads(
            (self.work_dir / "validation.json").read_text(encoding="utf-8")
        )
        if self._summary.get("params") != self.params.to_dict():
            raise RuntimeError(
                f"{self.work_dir} was made with other extractor parameters; use a new run id"
            )
        self._code_now = extractor_v0_code_identity()
        ran = (self._summary.get("code") or {}).get("files_sha256")
        if self.require_same_code and ran != self._code_now.get("files_sha256"):
            raise RuntimeError(
                f"{self.work_dir} was made by other extractor code than this ({ran} against "
                f"{self._code_now.get('files_sha256')}): the truth and the renders' readings "
                "would come from two versions; use a new run id (a --table-only rebuild is "
                "still allowed)"
            )
        self._frame_energies = {
            key: _energies(rec["energies"]) for key, rec in self._records.items()
        }
        self._stored = {}
        self._as_of = {}
        self._as_of_available = None
        self._pool = [
            ReferenceFrameV0(
                key,
                rec["sha256"],
                parse_iso_utc(rec["image_time_utc"]),
                self._sun[key][0],
                self._sun[key][1],
                rec["local_day"],
                self._frame_energies[key],
            )
            for key, rec in sorted(self._records.items())
            if rec.get("in_reference_pool")
        ]

    # -- readings --------------------------------------------------------------------------
    def truth_readings(self) -> dict[str, Reading]:
        """Every classified real frame's reading (night frames included, as ``night``)."""
        if self._stored:
            return dict(self._stored)
        out = {}
        for key, rec in self._records.items():
            entry = rec["bands"][self.band]
            state = entry["class"] if rec["frame_class"] == "scored" else None
            out[key] = Reading(
                extractor=self.version,
                frame_class=rec["frame_class"],
                p1_class=p1_class_of(rec["frame_class"], state),
                ratio=entry["ratio"],
                reason=rec["reason"],
                references=tuple(r["key"] for r in rec.get("references") or []),
                detail={
                    "energy": entry["energy"],
                    "reference_energy": entry["reference_energy"],
                    "camera_fault": rec.get("camera_fault"),
                    "global_energy_ratio": rec.get("global_energy_ratio"),
                    "contrast_ratio": rec.get("contrast_ratio"),
                    "obscuration_height": rec.get("obscuration_height"),
                    "bands": {name: b["class"] for name, b in rec["bands"].items()},
                    "in_reference_pool": bool(rec.get("in_reference_pool")),
                    "gates": rec.get("gates"),
                },
            )
        self._stored = out
        return dict(out)

    def readings_as_of(
        self, moment: datetime, available: Mapping[str, datetime]
    ) -> dict[str, Reading]:
        """The readings a baseline issued at ``moment`` may use (:func:`readings_as_of_v0`).

        ``available`` maps each frame key to the time it was available (its fetch, for a live
        frame). Made once per moment and kept (a new ``available`` mapping starts afresh).
        """
        if self._plate is None:
            raise RuntimeError("prepare() first")
        if available is not self._as_of_available:
            self._as_of = {}
            self._as_of_available = available
        if moment not in self._as_of:
            self._as_of[moment] = readings_as_of_v0(
                self._records,
                self._frame_energies,
                self._sun,
                self._pool,
                moment=moment,
                available=available,
                params=self.params,
                order=self._plate.order,
                band=self.band,
                stored=self._stored or self.truth_readings(),
            )
        return dict(self._as_of[moment])

    def reference_times(self) -> dict[str, datetime]:
        """Every clear-day pool frame's image time, by key."""
        return {ref.key: ref.image_time for ref in self._pool}

    def frame_meta(self, key: str) -> dict[str, Any]:
        """A classified frame's SHA-256, image time, local day and the Sun the extractor used."""
        rec = self._records[key]
        alt, az = self._sun[key]
        return {
            "sha256": rec["sha256"],
            "image_time_utc": rec["image_time_utc"],
            "local_day": rec["local_day"],
            "sun_alt_deg": alt,
            "sun_az_deg": az,
        }

    def measure(self, image: Path, *, truth_key: str) -> Reading:
        """A render's reading against the pool, at the truth frame's Sun position and local day."""
        if self._plate is None:
            raise RuntimeError("prepare() first")
        rec = self._records[truth_key]
        alt, az = self._sun[truth_key]
        energies = band_energies(image, self._plate, self.params)
        result = classify_frame(
            energies,
            sun_alt_deg=alt,
            sun_az_deg=az,
            order=self._plate.order,
            pool=self._pool,
            params=self.params,
            local_day=rec["local_day"],
            exclude_keys=(truth_key,),
        )
        return _reading_from_result(result, self.band)

    # -- provenance ------------------------------------------------------------------------
    def identity(self) -> dict[str, Any]:
        """What every reading depends on (see :class:`P1Extractor`)."""
        return {
            "extractor": self.version,
            "band": self.band,
            "params": self.params.to_dict(),
            "run_dir": str(self.work_dir),
            "plate": self._summary.get("plate"),
            "labels": self._summary.get("labels"),
            "code": self._summary.get("code"),
            "code_now_differs": (self._summary.get("code") or {}).get("files_sha256")
            != self._code_now.get("files_sha256"),
            "run_at_utc": self._summary.get("run_at_utc"),
            "ephemeris": self._state.get("ephemeris"),
            "manifest_camera": self._state.get("manifest_camera"),
            "reference_pool": [r.key for r in self._pool],
            "frames_classified": len(self._records),
            "frames_skipped_by_the_run": self._summary.get("frames_skipped"),
            "frame_classes": self._summary.get("frame_classes"),
        }

    def validation(self) -> dict[str, Any]:
        """The run's PRD §13.8 validation, in short."""
        band = (self._validation.get("bands") or {}).get(self.band) or {}
        judged = band.get("label_partial_excluded") or {}
        return {
            "publishable": bool(self._validation.get("publishable")),
            "not_publishable_because": list(self._validation.get("not_publishable_because") or []),
            "band": self.band,
            "band_f1": judged.get("f1"),
            "band_judged_n": judged.get("n"),
            "f1_bar": self._validation.get("f1_bar"),
            "meets_f1_bar": band.get("meets_f1_bar"),
        }

    def provisional_notes(self) -> list[str]:
        """The extractor stage's own not-counted notes besides its validation (τ's status).

        From the run's ``summary.json`` (``not_counted_because``), so the τ statement follows
        the parameters that ran: v0's fixed placeholders, or values set on the command line.
        """
        notes = [
            str(note)
            for note in self._summary.get("not_counted_because") or []
            if not str(note).startswith("validation: ")
        ]
        if not notes:
            notes.append(
                f"extractor {self.version}: tau_hi {self.params.tau_hi} and tau_lo "
                f"{self.params.tau_lo}; the run states no fit of them"
            )
        return notes


#: Extractor versions the table can run (``--extractor``): each a factory taking the keyword
#: arguments of :class:`V0Extractor` and returning a :class:`P1Extractor`.
EXTRACTORS: dict[str, Callable[..., P1Extractor]] = {EXTRACTOR_V0: V0Extractor}
