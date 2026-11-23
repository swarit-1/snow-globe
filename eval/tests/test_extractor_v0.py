"""Extractor v0 (PRD §13.3): plate bands, band energies, reference choice and frame classes.

Procedural images only (tests/synth_images.py); the plate is written to a temporary folder.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from synth_images import PEAK_BOX, fogged, in_cloud, landscape, to_rgb

from snow_globe_eval.extractor import ExtractorParams, Region, edge_stats
from snow_globe_eval.extractor_v0 import (
    PlateBands,
    ReferenceFrameV0,
    V0Params,
    band_energies,
    classify_frame,
    select_reference,
)
from snow_globe_eval.heldout import LeakageError

W, H = 640, 360


def write_plate(
    folder: Path,
    bands: dict[str, np.ndarray],
    *,
    viewpoint: str = "test.view",
    base: Image.Image | None = None,
    invalid: dict[str, str] | None = None,
) -> Path:
    """A minimal plate folder: plate.json, one PNG per band, an overlay strip, maybe a base."""
    folder.mkdir(parents=True, exist_ok=True)
    files = {}
    order = []
    for name, mask in bands.items():
        path = folder / f"band_{name}.png"
        Image.fromarray((mask * 255).astype(np.uint8)).save(path)
        files[path.name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        entry: dict[str, object] = {"name": name, "file": path.name}
        if invalid and name in invalid:
            entry.update(valid=False, not_valid_because=invalid[name])
        order.append(entry)
    overlay = np.zeros((H, W), bool)
    overlay[H - 6 :] = True
    Image.fromarray((overlay * 255).astype(np.uint8)).save(folder / "overlay.png")
    files["overlay.png"] = {
        "sha256": hashlib.sha256((folder / "overlay.png").read_bytes()).hexdigest()
    }
    if base is not None:
        base.save(folder / "base.png")
        files["base.png"] = {
            "sha256": hashlib.sha256((folder / "base.png").read_bytes()).hexdigest()
        }
    doc = {
        "kind": "snow-globe.plate",
        "viewpoint_id": viewpoint,
        "plate_id": f"{viewpoint}/plate-v0",
        "frame_size": [W, H],
        "files": files,
        "bands": order,
    }
    (folder / "plate.json").write_text(json.dumps(doc))
    return folder


def peak_and_ground() -> dict[str, np.ndarray]:
    peak = np.zeros((H, W), bool)
    x0, y0, x1, y1 = PEAK_BOX
    peak[y0:y1, x0:x1] = True
    ground = np.zeros((H, W), bool)
    ground[280:340, 50:600] = True
    return {"peak": peak, "ground": ground}


@pytest.fixture
def plate(tmp_path: Path) -> PlateBands:
    return PlateBands.load(write_plate(tmp_path / "plate", peak_and_ground()))


def ref(
    key: str, image: Image.Image, plate: PlateBands, alt: float, az: float, day: str
) -> ReferenceFrameV0:
    t = datetime(2026, 11, 20, 18, tzinfo=UTC)
    return ReferenceFrameV0(key, "0" * 64, t, alt, az, day, band_energies(image, plate, V0Params()))


def test_plate_bands_load_and_check_hashes(tmp_path: Path) -> None:
    folder = write_plate(tmp_path / "plate", peak_and_ground())
    plate = PlateBands.load(folder)
    assert plate.order == ("peak", "ground") and plate.frame_size == (W, H)
    assert plate.to_record()["band_pixels"]["peak"] == 140 * 150
    grid = plate.on_grid((320, 180))
    assert grid["peak"].sum() == pytest.approx(70 * 75, rel=0.05) and not grid["_frame"][-1].any()
    (folder / "band_peak.png").write_bytes((folder / "band_ground.png").read_bytes())
    with pytest.raises(ValueError, match="SHA-256"):
        PlateBands.load(folder)


def test_plate_overlay_base_and_overlaps_are_checked(tmp_path: Path) -> None:
    folder = write_plate(tmp_path / "plate", peak_and_ground(), base=to_rgb(landscape(1)))
    plate = PlateBands.load(folder)
    assert plate.base_path == folder / "base.png" and plate.band_validity["peak"]["valid"]
    full = Image.fromarray(np.full((H, W), 255, np.uint8))
    full.save(folder / "overlay.png")  # an overlay that would hide the whole frame
    with pytest.raises(ValueError, match=r"overlay\.png: SHA-256"):
        PlateBands.load(folder)
    folder = write_plate(tmp_path / "plate2", peak_and_ground(), base=to_rgb(landscape(1)))
    to_rgb(landscape(2)).save(folder / "base.png")
    with pytest.raises(ValueError, match=r"base\.png: SHA-256"):
        PlateBands.load(folder)
    bands = peak_and_ground()
    bands["ground"][150:200, 300:320] = True  # 1,000 pixels inside the peak box too
    with pytest.raises(ValueError, match="share 1000 pixels"):
        PlateBands.load(write_plate(tmp_path / "plate3", bands))
    marked = write_plate(tmp_path / "plate4", peak_and_ground(), invalid={"ground": "off"})
    validity = PlateBands.load(marked).band_validity
    assert validity["ground"] == {"valid": False, "not_valid_because": "off"}


def test_plate_under_quarantine_is_refused(tmp_path: Path) -> None:
    folder = write_plate(tmp_path / "quarantine" / "plate", peak_and_ground())
    with pytest.raises(LeakageError):
        PlateBands.load(folder, roots=(tmp_path,))


def test_band_energy_is_v_minus_1_energy_over_the_same_region(plate: PlateBands) -> None:
    image = to_rgb(landscape(9))
    e = band_energies(image, plate, V0Params())
    v1 = edge_stats(image, Region.box(*PEAK_BOX), ExtractorParams())
    assert e.bands["peak"] == pytest.approx(v1.region_energy, rel=0.02)
    assert e.analysis_size == (960, 540)
    with pytest.raises(ValueError, match="plate"):
        band_energies(image.resize((320, 180)), plate, V0Params())


def test_reference_choice_limits_nearest_and_same_day(plate: PlateBands) -> None:
    img = to_rgb(landscape(1))
    pool = [
        ref("a", img, plate, 20.0, 160.0, "2026-11-20"),
        ref("b", img, plate, 21.0, 165.0, "2026-11-21"),
        ref("c", img, plate, 22.5, 170.0, "2026-11-21"),
        ref("d", img, plate, 10.0, 140.0, "2026-11-21"),
    ]
    p = V0Params(ref_k=2)
    picked = select_reference(pool, 21.0, 164.0, p, local_day="2026-11-22")
    # distances hypot(dalt / 3, daz / 15): b 0.07, a 0.43, c 0.64; d is out of range
    assert [r.key for r in picked] == ["b", "a"]
    assert [r.key for r in select_reference(pool, 21.0, 164.0, p, local_day="2026-11-21")] == ["a"]
    left = select_reference(pool, 21.0, 164.0, p, local_day="2026-11-22", exclude_keys=("b",))
    assert [r.key for r in left] == ["a", "c"]
    assert select_reference(pool, 40.0, 164.0, p, local_day=None) == []
    assert select_reference(pool, 21.0, 200.0, p, local_day=None) == []


def _classify(image: Image.Image, plate: PlateBands, pool, alt: float = 21.0):
    return classify_frame(
        band_energies(image, plate, V0Params()),
        sun_alt_deg=alt,
        sun_az_deg=165.0,
        order=plate.order,
        pool=pool,
        params=V0Params(),
        local_day="2026-11-23",
    )


def test_frame_classes(plate: PlateBands) -> None:
    pool = [
        ref(f"r{s}", to_rgb(landscape(s)), plate, 21.0 + 0.2 * s, 165.0, "2026-11-20")
        for s in (1, 2, 3)
    ]
    clear = _classify(to_rgb(landscape(9)), plate, pool)
    assert clear.frame_class == "scored" and clear.bands["peak"].state == "visible"
    assert 0.85 < clear.bands["peak"].ratio < 1.15 and clear.obscuration_height == "peak"
    assert len(clear.references) == 3 and clear.to_dict()["bands"]["peak"]["class"] == "visible"
    capped = _classify(
        to_rgb(np.where(np.arange(H)[:, None] < 210, fogged(landscape(9), 0.85), landscape(9))),
        plate,
        pool,
    )
    assert capped.bands["peak"].state == "hidden" and capped.bands["ground"].state == "visible"
    assert capped.obscuration_height == "ground"
    cloud = _classify(to_rgb(in_cloud(3)), plate, pool)
    assert cloud.frame_class == "in_cloud" and cloud.band_class("peak") == "in_cloud"
    assert _classify(to_rgb(landscape(9)), plate, pool, alt=2.0).frame_class == "night"
    black = Image.new("RGB", (W, H), (0, 0, 0))
    assert _classify(black, plate, pool).frame_class == "camera_fault"
    assert _classify(to_rgb(landscape(9)), plate, []).frame_class == "no_reference"


def test_params_validation() -> None:
    with pytest.raises(ValueError):
        V0Params(tau_hi=0.3, tau_lo=0.5)
    with pytest.raises(ValueError):
        V0Params(ref_k=0)
    assert V0Params().to_dict()["tau_hi"] == 0.6
