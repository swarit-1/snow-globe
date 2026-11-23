"""Stars found in the camera's night frames and matched to a catalogue (PRD §8.5 steps 3 and 7).

The pose solve (:mod:`.joint`) takes its orientation and field of view from stars, so the
pixel positions of those stars are a main input of the plate. This module finds them, offline
and reproducibly, from the night frames and a star catalogue on local disk, and records every
candidate it looked at: the stars it kept and the ones it rejected, with each one's residual,
so the matching can be audited and the kept set's residuals read against the cut.

**Catalogue.** The Bright Star Catalogue, 5th revised edition (Hoffleit & Warren 1991, CDS
V/50; :func:`read_bsc5`): its J2000 positions at epoch 2000.0, moved to the frame's epoch with
the catalogue's proper motions (``pmRA`` is μα·cos δ), and its V magnitudes. The catalogue file
is read from wherever the owner keeps it (``--catalogue``); nothing of it enters the repo. The
match file this module writes, which does hold catalogue positions, goes to the data dir, and
the build spec cites it by SHA-256.

**Finding stars** (per night frame): Rec. 601 luma, a Gaussian blur of 1 px, minus a
``median_px`` median filter (a high-pass that removes the sky glow); the noise sigma is 1.4826 ×
the median absolute deviation over ``noise_box``, a patch of empty sky.

**Matching** (the passes of ``passes``, each a search radius in pixels and a significance
floor in sigma): every catalogue star at or above ``min_alt_deg`` and no fainter than ``vmax`` is
projected with the current pose; a star inside ``search_box`` and at least ``sky_margin_px``
above the clear geometry frame's skyline takes the brightest high-pass pixel within the
radius, if that peak clears the floor and no brighter star took it first (stars are visited
brightest first). Its position is the intensity-weighted centroid of the positive high-pass
values within 3 px. After each pass the pose (heading, pitch, roll, FOV) is refitted to the
matches, robustly (soft-L1 loss with a 2 px scale), with the camera position held fixed (a
star's direction barely depends on it). After the last pass, matches with a residual under
``keep_px`` are kept and the pose is refitted to them alone; every match of the last pass is
recorded with its residual before that cut, and every star the last pass could not match with
the reason.
"""

from __future__ import annotations

import gzip
import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PIL import Image
from scipy import ndimage, optimize

from .camera import Pose, pixel_rays, project
from .stars import StarObservation, apparent_alt_az

__all__ = [
    "BSC5_EPOCH",
    "MATCH_KIND",
    "Catalogue",
    "MatchSettings",
    "NightFrame",
    "StarMatch",
    "highpass",
    "match_record",
    "match_stars",
    "observations_from_record",
    "radius_from_centre",
    "read_bsc5",
    "settings_from_spec",
    "summary",
]

BSC5_EPOCH = 2000.0
_JULIAN_YEAR_DAYS = 365.25


@dataclass(frozen=True)
class Catalogue:
    """Star positions (J2000 equinox, at ``epoch``), proper motions and V magnitudes."""

    ids: NDArray[np.int64]
    ra_deg: NDArray[np.float64]
    dec_deg: NDArray[np.float64]
    pm_ra_cosdec_arcsec_yr: NDArray[np.float64]
    pm_dec_arcsec_yr: NDArray[np.float64]
    v_mag: NDArray[np.float64]
    epoch: float
    name: str
    sha256: str

    def at(self, moment: datetime) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        """Right ascension and declination (J2000 equinox) moved to ``moment``'s epoch."""
        years = (moment.timestamp() / 86400.0 + 2440587.5 - 2451545.0) / _JULIAN_YEAR_DAYS
        years += 2000.0 - self.epoch
        dec = self.dec_deg + self.pm_dec_arcsec_yr * years / 3600.0
        cos_dec = np.maximum(np.cos(np.radians(self.dec_deg)), 1e-9)
        ra = self.ra_deg + self.pm_ra_cosdec_arcsec_yr * years / 3600.0 / cos_dec
        return ra % 360.0, dec

    def brighter_than(self, vmax: float) -> Catalogue:
        """Only the stars with V ≤ ``vmax``, brightest first."""
        keep = np.nonzero(self.v_mag <= vmax)[0]
        keep = keep[np.argsort(self.v_mag[keep], kind="stable")]
        return Catalogue(
            self.ids[keep],
            self.ra_deg[keep],
            self.dec_deg[keep],
            self.pm_ra_cosdec_arcsec_yr[keep],
            self.pm_dec_arcsec_yr[keep],
            self.v_mag[keep],
            self.epoch,
            self.name,
            self.sha256,
        )


def _field(line: str, start: int, end: int) -> str:
    return line[start - 1 : end].strip()


def read_bsc5(path: str | Path) -> Catalogue:
    """Read CDS V/50's ``catalog`` (plain or gzip), per its ReadMe's byte layout.

    Entries without a J2000 position or a V magnitude (novae and other non-stellar objects
    kept for numbering) are skipped. A missing proper motion is taken as zero.
    """
    file_path = Path(path)
    data = file_path.read_bytes()
    text = (gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data).decode("latin-1")
    ids, ra, dec, pmra, pmde, vmag = [], [], [], [], [], []
    for line in text.splitlines():
        if len(line) < 107 or not _field(line, 76, 77) or not _field(line, 103, 107):
            continue
        hours = int(_field(line, 76, 77)) + int(_field(line, 78, 79)) / 60.0
        hours += float(_field(line, 80, 83)) / 3600.0
        sign = -1.0 if _field(line, 84, 84) == "-" else 1.0
        degrees = int(_field(line, 85, 86)) + int(_field(line, 87, 88)) / 60.0
        degrees += int(_field(line, 89, 90)) / 3600.0
        ids.append(int(_field(line, 1, 4)))
        ra.append(hours * 15.0)
        dec.append(sign * degrees)
        vmag.append(float(_field(line, 103, 107)))
        pmra.append(float(_field(line, 149, 154) or 0.0) if len(line) >= 154 else 0.0)
        pmde.append(float(_field(line, 155, 160) or 0.0) if len(line) >= 160 else 0.0)
    if not ids:
        raise ValueError(f"{file_path}: no catalogue entries read (not CDS V/50's catalog?)")
    return Catalogue(
        np.array(ids, dtype=np.int64),
        np.array(ra),
        np.array(dec),
        np.array(pmra),
        np.array(pmde),
        np.array(vmag),
        BSC5_EPOCH,
        "Bright Star Catalogue, 5th Revised Ed. (Hoffleit & Warren 1991), CDS V/50",
        hashlib.sha256(data).hexdigest(),
    )


def highpass(luma: NDArray[np.float64], median_px: int = 15) -> NDArray[np.float64]:
    """Luma blurred by 1 px minus its ``median_px`` median: point sources stand out."""
    smooth = ndimage.gaussian_filter(np.asarray(luma, dtype=np.float64), 1.0)
    return smooth - ndimage.median_filter(smooth, size=median_px)


def _noise_sigma(hp: NDArray[np.float64], box: tuple[int, int, int, int]) -> float:
    x0, y0, x1, y1 = box
    patch = hp[y0:y1, x0:x1]
    return float(1.4826 * np.median(np.abs(patch - np.median(patch))))


def _centroid(hp: NDArray[np.float64], px: int, py: int, r: int = 3) -> tuple[float, float]:
    y0, y1 = max(0, py - r), min(hp.shape[0], py + r + 1)
    x0, x1 = max(0, px - r), min(hp.shape[1], px + r + 1)
    w = np.clip(hp[y0:y1, x0:x1], 0.0, None)
    total = float(w.sum())
    if total <= 0.0:
        return px + 0.5, py + 0.5
    yy, xx = np.mgrid[y0:y1, x0:x1]
    return float((w * xx).sum() / total) + 0.5, float((w * yy).sum() / total) + 0.5


@dataclass(frozen=True)
class NightFrame:
    """A night frame's identity, time and high-pass image."""

    key: str
    sha256: str
    image_time: datetime
    hp: NDArray[np.float64]
    sigma: float

    @classmethod
    def from_luma(
        cls,
        key: str,
        sha256: str,
        image_time: datetime,
        luma: NDArray[np.float64],
        *,
        noise_box: tuple[int, int, int, int],
        median_px: int = 15,
    ) -> NightFrame:
        """High-pass a frame's luma (0–255) and measure its noise in ``noise_box``."""
        hp = highpass(luma, median_px)
        return cls(key, sha256, image_time, hp, _noise_sigma(hp, noise_box))

    @classmethod
    def from_file(
        cls,
        key: str,
        path: Path,
        sha256: str,
        image_time: datetime,
        *,
        noise_box: tuple[int, int, int, int],
        median_px: int = 15,
    ) -> NightFrame:
        """Read a frame from disk (Rec. 601 luma, 0–255)."""
        with Image.open(path) as im:
            luma = np.asarray(im.convert("L"), dtype=np.float64)
        return cls.from_luma(
            key, sha256, image_time, luma, noise_box=noise_box, median_px=median_px
        )


@dataclass(frozen=True)
class MatchSettings:
    """Every number the matcher uses (module docstring)."""

    vmax: float = 4.6
    min_alt_deg: float = 2.0
    passes: tuple[tuple[int, float], ...] = ((28, 10.0), (14, 8.0), (8, 7.0), (5, 6.0), (4, 6.0))
    keep_px: float = 4.0
    search_box: tuple[int, int, int, int] = (480, 0, 1900, 1060)
    sky_margin_px: int = 8
    noise_box: tuple[int, int, int, int] = (600, 30, 1800, 250)
    median_px: int = 15
    loss_scale_px: float = 2.0

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {
            "vmax": self.vmax,
            "min_alt_deg": self.min_alt_deg,
            "passes_radius_px_and_min_sigma": [list(p) for p in self.passes],
            "keep_px": self.keep_px,
            "search_box": list(self.search_box),
            "sky_margin_px": self.sky_margin_px,
            "noise_box": list(self.noise_box),
            "median_px": self.median_px,
            "loss": f"soft_l1, scale {self.loss_scale_px} px",
        }


@dataclass
class StarMatch:
    """The matcher's result: the pose, the kept stars, and every candidate it judged."""

    pose: Pose
    kept: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    unmatched: list[dict[str, Any]]
    passes: list[dict[str, Any]]
    residuals_before_cut_px: list[float]
    residuals_after_cut_px: list[float]
    angles_after_cut_deg: list[float] = field(default_factory=list)

    def observations(self) -> list[StarObservation]:
        """The kept stars as joint-solve inputs."""
        return [
            StarObservation(
                ra_deg=k["ra_deg"],
                dec_deg=k["dec_deg"],
                v_mag=k["v_mag"],
                image_time=datetime.fromisoformat(k["image_time_utc"].replace("Z", "+00:00")),
                px=(k["px"][0], k["px"][1]),
                frame_key=k["frame_key"],
            )
            for k in self.kept
        ]


def _stats(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    a = np.asarray(values, dtype=np.float64)
    return {
        "n": int(a.size),
        "median": round(float(np.median(a)), 3),
        "p90": round(float(np.percentile(a, 90)), 3),
        "max": round(float(a.max()), 3),
    }


def match_stars(
    frames: Sequence[NightFrame],
    catalogue: Catalogue,
    camera_lat_lon: tuple[float, float],
    initial: Pose,
    skyline_rows: NDArray[np.float64],
    settings: MatchSettings | None = None,
) -> StarMatch:
    """Find and match stars in ``frames`` (module docstring); ``skyline_rows`` per column."""
    cfg = settings or MatchSettings()
    bright = catalogue.brighter_than(cfg.vmax)
    lat, lon = camera_lat_lon
    per_frame = []
    for frame in frames:
        ra, dec = bright.at(frame.image_time)
        az, alt = apparent_alt_az(ra, dec, frame.image_time, lat, lon)
        a, h = np.radians(az), np.radians(alt)
        dirs = np.stack([np.sin(a) * np.cos(h), np.cos(a) * np.cos(h), np.sin(h)], axis=-1)
        per_frame.append((frame, ra, dec, alt, dirs))
    x_lo, y_lo, x_hi, y_hi = cfg.search_box

    def run_pass(
        pose: Pose, radius: int, min_sigma: float
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        matches: list[dict[str, Any]] = []
        missed: list[dict[str, Any]] = []
        for frame, ra, dec, alt, dirs in per_frame:
            uv, depth = project(dirs, pose)
            used: set[tuple[int, int]] = set()
            for i in range(len(bright.ids)):
                x, y = float(uv[i, 0]), float(uv[i, 1])
                if depth[i] <= 0 or alt[i] < cfg.min_alt_deg:
                    continue
                if not (x_lo + radius < x < x_hi - radius and y_lo + radius + 6 < y < y_hi):
                    continue
                column = min(max(int(x), 0), len(skyline_rows) - 1)
                if y > skyline_rows[column] - cfg.sky_margin_px:
                    continue
                star = {
                    "frame_key": frame.key,
                    "catalogue_id": int(bright.ids[i]),
                    "v_mag": float(bright.v_mag[i]),
                    "ra_deg": round(float(ra[i]), 6),
                    "dec_deg": round(float(dec[i]), 6),
                    "predicted_px": [round(x, 2), round(y, 2)],
                    "dir": dirs[i],
                    "image_time_utc": frame.image_time.isoformat().replace("+00:00", "Z"),
                }
                xi, yi = int(x), int(y)
                win = frame.hp[yi - radius : yi + radius + 1, xi - radius : xi + radius + 1]
                k = np.unravel_index(int(np.argmax(win)), win.shape)
                px, py = xi - radius + int(k[1]), yi - radius + int(k[0])
                significance = float(win[k]) / frame.sigma if frame.sigma > 0 else 0.0
                star["peak_sigma"] = round(significance, 1)
                if significance < min_sigma:
                    missed.append({**star, "why": f"brightest peak {significance:.1f} sigma"})
                    continue
                if (px, py) in used:
                    missed.append({**star, "why": "peak already taken by a brighter star"})
                    continue
                used.add((px, py))
                cx, cy = _centroid(frame.hp, px, py)
                matches.append({**star, "px": [round(cx, 2), round(cy, 2)]})
        return matches, missed

    def fit(matches: list[dict[str, Any]], start: Pose) -> tuple[Pose, NDArray[np.float64]]:
        dirs = np.array([m["dir"] for m in matches])
        px = np.array([m["px"] for m in matches], dtype=np.float64)

        def residuals(v: NDArray[np.float64]) -> NDArray[np.float64]:
            uv, _ = project(dirs, start.with_angles(*v))
            return (uv - px).ravel()

        x0 = [start.heading_deg, start.pitch_deg, start.roll_deg, start.hfov_deg]
        sol = optimize.least_squares(residuals, x0, loss="soft_l1", f_scale=cfg.loss_scale_px)
        pose = start.with_angles(float(sol.x[0]) % 360.0, *(float(v) for v in sol.x[1:]))
        uv, _ = project(dirs, pose)
        return pose, np.hypot(*(uv - px).T)

    pose = initial
    history = []
    matches: list[dict[str, Any]] = []
    missed: list[dict[str, Any]] = []
    residual = np.zeros(0)
    for radius, min_sigma in cfg.passes:
        matches, missed = run_pass(pose, radius, min_sigma)
        if len(matches) < 4:
            raise ValueError(f"pass R={radius}: only {len(matches)} stars matched")
        pose, residual = fit(matches, pose)
        history.append(
            {
                "radius_px": radius,
                "min_sigma": min_sigma,
                "matched": len(matches),
                "pose": [round(v, 4) for v in _angles(pose)],
                "residual_px": _stats(residual.tolist()),
            }
        )
    keep = residual < cfg.keep_px
    if int(keep.sum()) < 4:
        raise ValueError(f"only {int(keep.sum())} stars within {cfg.keep_px} px")
    kept = [m for m, k in zip(matches, keep, strict=True) if k]
    rejected = [
        {**m, "residual_px": round(float(r), 2), "why": f"residual {r:.2f} px >= {cfg.keep_px}"}
        for m, r, k in zip(matches, residual, keep, strict=True)
        if not k
    ]
    before = residual.tolist()
    pose, after = fit(kept, pose)
    dirs = np.array([m["dir"] for m in kept])
    px = np.array([m["px"] for m in kept], dtype=np.float64)
    rays = pixel_rays(px[:, 0], px[:, 1], pose)
    angles = np.degrees(np.arccos(np.clip(np.sum(rays * dirs, axis=-1), -1.0, 1.0)))
    for m, r in zip(kept, after, strict=True):
        m["residual_px"] = round(float(r), 2)

    def clean(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{k: v for k, v in item.items() if k != "dir"} for item in items]

    return StarMatch(
        pose=pose,
        kept=clean(kept),
        rejected=clean(rejected),
        unmatched=clean(missed),
        passes=history,
        residuals_before_cut_px=[round(float(v), 3) for v in before],
        residuals_after_cut_px=[round(float(v), 3) for v in after],
        angles_after_cut_deg=[round(float(v), 4) for v in angles],
    )


def _angles(pose: Pose) -> tuple[float, float, float, float]:
    return (pose.heading_deg, pose.pitch_deg, pose.roll_deg, pose.hfov_deg)


def summary(match: StarMatch) -> dict[str, Any]:
    """Counts and residual distributions for the match file and the plate manifest."""
    return {
        "kept": len(match.kept),
        "rejected_after_last_pass": len(match.rejected),
        "unmatched_in_last_pass": len(match.unmatched),
        "residual_px_before_cut": _stats(match.residuals_before_cut_px),
        "residual_px_after_cut": _stats(match.residuals_after_cut_px),
        "angle_deg_after_cut": _stats(match.angles_after_cut_deg),
        "pose_star_only": dict(
            zip(
                ("heading_deg", "pitch_deg", "roll_deg", "hfov_deg"),
                (round(v, 4) for v in _angles(match.pose)),
                strict=True,
            )
        ),
        "x_range": _span([k["px"][0] for k in match.kept]),
        "y_range": _span([k["px"][1] for k in match.kept]),
        "frames": sorted({k["frame_key"] for k in match.kept}),
    }


def _span(values: Sequence[float]) -> list[float] | None:
    return None if not values else [round(min(values), 1), round(max(values), 1)]


def radius_from_centre(px: Sequence[float], pose: Pose) -> float:
    """Distance of a pixel from the frame centre (for reporting where stars reach)."""
    return math.hypot(px[0] - pose.width / 2.0, px[1] - pose.height / 2.0)


MATCH_KIND = "snow-globe.plate-star-match"


def settings_from_spec(stars_spec: dict[str, Any]) -> MatchSettings:
    """The spec's ``stars.match_settings`` (defaults for any field left out)."""
    raw = dict(stars_spec.get("match_settings", {}))
    fields: dict[str, Any] = {}
    for name in ("vmax", "min_alt_deg", "keep_px", "loss_scale_px"):
        if name in raw:
            fields[name] = float(raw[name])
    for name in ("sky_margin_px", "median_px"):
        if name in raw:
            fields[name] = int(raw[name])
    for name in ("search_box", "noise_box"):
        if name in raw:
            a, b, c, d = (int(v) for v in raw[name])
            fields[name] = (a, b, c, d)
    if "passes" in raw:
        fields["passes"] = tuple((int(r), float(m)) for r, m in raw["passes"])
    return MatchSettings(**fields)


def match_record(
    match: StarMatch,
    *,
    viewpoint_id: str,
    catalogue: Catalogue,
    catalogue_path: str,
    frames: Sequence[NightFrame],
    camera_lat_lon: tuple[float, float],
    initial: Pose,
    settings: MatchSettings,
    skyline_from: dict[str, Any],
    builder: str,
    made_at_utc: str,
) -> dict[str, Any]:
    """The match file's content (it holds catalogue positions, so it lives in the data dir)."""
    return {
        "kind": MATCH_KIND,
        "viewpoint_id": viewpoint_id,
        "made_at_utc": made_at_utc,
        "builder": builder,
        "catalogue": {
            "name": catalogue.name,
            "file": catalogue_path,
            "sha256": catalogue.sha256,
            "epoch": catalogue.epoch,
            "proper_motion": "applied to each frame's epoch (pmRA is mu_alpha cos delta)",
            "where": "data dir only: catalogue positions are not committed to the repo",
        },
        "camera_lat_lon": list(camera_lat_lon),
        "initial_pose": initial.to_dict(),
        "settings": settings.to_dict(),
        "skyline_from": skyline_from,
        "frames": [
            {
                "key": f.key,
                "sha256": f.sha256,
                "image_time_utc": f.image_time.isoformat().replace("+00:00", "Z"),
                "noise_sigma": round(f.sigma, 3),
            }
            for f in frames
        ],
        "passes": match.passes,
        "summary": summary(match),
        "residual_px_before_cut": match.residuals_before_cut_px,
        "kept": match.kept,
        "rejected": match.rejected,
        "unmatched": match.unmatched,
    }


def observations_from_record(record: dict[str, Any]) -> list[StarObservation]:
    """The kept stars of a match file as joint-solve inputs."""
    if record.get("kind") != MATCH_KIND:
        raise ValueError(f"not a {MATCH_KIND} file")
    return [
        StarObservation(
            ra_deg=float(k["ra_deg"]),
            dec_deg=float(k["dec_deg"]),
            v_mag=float(k["v_mag"]),
            image_time=datetime.fromisoformat(str(k["image_time_utc"]).replace("Z", "+00:00")),
            px=(float(k["px"][0]), float(k["px"][1])),
            frame_key=str(k["frame_key"]),
        )
        for k in record["kept"]
    ]
