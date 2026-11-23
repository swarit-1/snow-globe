"""Extractor v0.2 (PRD §13.3): illumination, the contrast model, references and frame classes.

Procedural images only (tests/synth_v0_2.py); the plate is written to a temporary folder.
"""

from __future__ import annotations

import io
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from synth_v0_2 import H, W, masks, scene, to_rgb, write_plate

from snow_globe_eval.extractor_v0 import BandEnergies, PlateBands
from snow_globe_eval.extractor_v0_2 import (
    Illumination,
    IlluminationRegions,
    PoolFrame,
    Thresholds,
    V02Params,
    band_reference,
    classify_frame_v02,
    expected_contrast,
    frame_features,
    illumination,
    load_sky_mask,
    normalised_energy,
    select_references,
)
from snow_globe_eval.heldout import LeakageError


def _jpeg(luma: np.ndarray) -> Image.Image:
    buffer = io.BytesIO()
    to_rgb(luma).save(buffer, format="JPEG", quality=95)
    return Image.open(io.BytesIO(buffer.getvalue()))


@pytest.fixture
def plate(tmp_path: Path) -> tuple[PlateBands, np.ndarray]:
    folder = write_plate(tmp_path / "plate")
    return PlateBands.load(folder), load_sky_mask(folder)


def test_expected_contrast_is_the_weber_contrast_of_snow_against_sky() -> None:
    assert expected_contrast(0.5) == pytest.approx(2 * 0.5 / 1.5)
    assert expected_contrast(1.0) == 0.0
    # a sky twice as bright as the snow: a dark dome against a bright sky
    assert expected_contrast(2.0) == pytest.approx(2.0 / 3.0)
    for bad in (-0.1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            expected_contrast(bad)


def test_illumination_reads_the_sky_against_the_brightest_lit_snow() -> None:
    size = (64, 36)
    sky = np.zeros((36, 64), bool)
    sky[:10] = True
    near = np.zeros((36, 64), bool)
    near[20:34] = True
    regions = IlluminationRegions(size=size, sky=sky, near=near)
    rgb = np.full((36, 64, 3), 0.3)
    rgb[:10] = (0.40, 0.45, 0.60)  # a blue sky, luma 0.4325
    rgb[20:34] = 0.2  # dark trees
    rgb[20:22, :] = 0.9  # some lit snow: 128 of 896 near cells, above the 99th percentile
    light = illumination(rgb, regions, V02Params())
    sky_luma = 0.299 * 0.40 + 0.587 * 0.45 + 0.114 * 0.60
    assert light.sky_median_luma == pytest.approx(sky_luma)
    assert light.near_luma_percentile == pytest.approx(0.9)
    assert light.d_index == pytest.approx(sky_luma / 0.9)
    assert light.expected_contrast == pytest.approx(expected_contrast(sky_luma / 0.9))
    assert light.sky_blueness > 0.1
    dark = illumination(np.full((36, 64, 3), 0.01), regions, V02Params())
    assert dark.d_index is None and dark.expected_contrast is None
    with pytest.raises(ValueError):
        illumination(np.zeros((10, 10, 3)), regions, V02Params())


def test_regions_keep_the_near_field_off_the_sky_the_bands_and_the_caption(
    plate: tuple[PlateBands, np.ndarray],
) -> None:
    bands, sky_full = plate
    params = V02Params()
    regions = IlluminationRegions.build(bands, sky_full, (W, H), params)
    m = masks()
    assert not (regions.near & m["sky"]).any()
    assert not (regions.near & (m["summit"] | m["upper"])).any()
    assert not (regions.near & m["overlay"]).any()
    rows = np.nonzero(regions.near.any(axis=1))[0]
    assert rows.min() == round(0.55 * H) and rows.max() == round(0.95 * H) - 1
    assert (regions.sky == (m["sky"] & ~m["overlay"])).all()


def test_load_sky_mask_checks_its_sha256_and_refuses_the_quarantine(tmp_path: Path) -> None:
    folder = write_plate(tmp_path / "plate")
    assert load_sky_mask(folder).shape == (H, W)
    Image.fromarray(np.zeros((H, W), np.uint8)).save(folder / "sky.png")
    with pytest.raises(ValueError, match="SHA-256"):
        load_sky_mask(folder)
    q = write_plate(tmp_path / "quarantine" / "plate")
    with pytest.raises(LeakageError):
        load_sky_mask(q, roots=(tmp_path,))


def _light(d: float | None) -> Illumination:
    c = None if d is None else expected_contrast(d)
    return Illumination(d, 0.5, 0.9, 0.0, c)


def test_normalised_energy_applies_only_to_skyline_bands_and_has_a_floor() -> None:
    params = V02Params()
    assert normalised_energy("summit", 0.03, _light(0.5), params) == pytest.approx(0.03 / (2 / 3))
    assert normalised_energy("upper", 0.03, _light(0.5), params) == 0.03
    assert normalised_energy("summit", 0.03, _light(0.97), params) is None  # C = 0.030 < 0.05
    assert normalised_energy("summit", 0.03, _light(None), params) is None
    ablation = V02Params(contrast_model=False)
    assert normalised_energy("summit", 0.03, _light(0.97), ablation) == 0.03


def _pool_frame(key: str, day: str, d: float, alt: float, value: float = 1.0) -> PoolFrame:
    return PoolFrame(key, day, alt, d, value, value, 0.07, 0.3)


def test_references_are_the_nearest_in_light_then_altitude_on_other_days() -> None:
    params = V02Params()
    pool = [
        _pool_frame("a", "2026-11-20", 0.52, 10.0),  # sunny, same altitude
        _pool_frame("b", "2026-11-20", 0.80, 20.0),  # overcast, 10 deg higher
        _pool_frame("c", "2026-11-21", 0.79, 10.0),  # overcast, same altitude, frame's own day
        _pool_frame("d", "2026-11-22", 0.81, 12.0),  # overcast, held-out day
        _pool_frame("e", "2026-11-19", 0.78, 5.0),  # overcast, 5 deg lower
    ]
    chosen = select_references(
        pool,
        d_index=0.79,
        sun_alt_deg=10.0,
        params=params,
        local_day="2026-11-21",
        exclude_days=("2026-11-22",),
    )
    assert [r.key for _, r in chosen] == ["e", "b", "a"]
    # a D difference of 0.27 (sunny vs overcast) outweighs a 10 deg altitude difference
    assert chosen[2][0] == pytest.approx(np.hypot(0.27 / 0.05, 0.0))
    assert chosen[1][0] == pytest.approx(np.hypot(0.01 / 0.05, 10.0 / 10.0))
    assert (
        select_references(pool, d_index=0.8, sun_alt_deg=20.0, params=params, exclude_keys=("b",))[
            0
        ][1].key
        == "d"
    )
    # ties go to the smaller key
    tied = [_pool_frame("z", "2026-11-19", 0.6, 10.0), _pool_frame("y", "2026-11-19", 0.6, 10.0)]
    assert [
        r.key for _, r in select_references(tied, d_index=0.6, sun_alt_deg=10.0, params=params)
    ] == ["y", "z"]


def _energies(summit: float, upper: float = 0.05, global_energy: float = 0.07) -> BandEnergies:
    return BandEnergies(
        bands={"summit": summit, "upper": upper},
        global_energy=global_energy,
        rms_contrast=0.3,
        dark_fraction=0.0,
        bright_fraction=0.0,
        luma_range=0.9,
        analysis_size=(960, 540),
    )


def test_band_reference_ratio_and_state() -> None:
    params = V02Params()
    pool = [_pool_frame(f"p{i}", "2026-11-20", 0.8, 10.0, v) for i, v in enumerate((1.0, 2.0, 4.0))]
    light = _light(0.8)
    contrast = expected_contrast(0.8)
    e = _energies(summit=1.0 * contrast)  # Ẽ = 1.0; reference = median(1, 2, 4) = 2
    result, used = band_reference(
        "summit",
        e,
        light,
        sun_alt_deg=10.0,
        pool=pool,
        params=params,
        local_day="2026-11-21",
        thresholds=Thresholds(0.6, 0.3),
    )
    assert result.ratio == pytest.approx(0.5) and result.state == "partial"
    assert result.reference == pytest.approx(2.0) and len(used) == 3
    unfitted, _ = band_reference(
        "summit", e, light, sun_alt_deg=10.0, pool=pool, params=params, local_day="2026-11-21"
    )
    assert unfitted.ratio == pytest.approx(0.5) and unfitted.state is None
    assert unfitted.missing == "no_threshold"
    nothing, _ = band_reference(
        "summit", e, light, sun_alt_deg=10.0, pool=pool, params=params, local_day="2026-11-20"
    )
    assert nothing.missing == "no_reference" and nothing.ratio is None
    with pytest.raises(ValueError):
        Thresholds(0.3, 0.6)


def test_frame_classes_come_before_band_states() -> None:
    params = V02Params()
    pools = {"summit": [_pool_frame(f"p{i}", "2026-11-20", 0.6, 10.0, 0.1) for i in range(3)]}
    tau = {"summit": Thresholds(0.6, 0.3), "upper": None}
    common = {
        "sun_az_deg": 180.0,
        "order": ("summit", "upper"),
        "pools": pools,
        "thresholds": tau,
        "params": params,
        "local_day": "2026-11-21",
    }
    light = _light(0.6)
    visible = _energies(summit=0.2 * expected_contrast(0.6))
    night = classify_frame_v02(visible, light, sun_alt_deg=1.0, **common)
    assert night.frame_class == "night" and night.band_class("summit") == "night"
    dark = BandEnergies({"summit": 0.0, "upper": 0.0}, 0.0, 0.0, 1.0, 0.0, 0.0, (960, 540))
    assert classify_frame_v02(dark, light, sun_alt_deg=10.0, **common).camera_fault == "dark"
    gated = classify_frame_v02(
        visible, light, sun_alt_deg=10.0, excluded_because="fresh: same image", **common
    )
    assert gated.frame_class == "excluded" and gated.reason == "fresh: same image"
    fog = BandEnergies({"summit": 0.001, "upper": 0.001}, 0.01, 0.05, 0.0, 0.0, 0.5, (960, 540))
    assert classify_frame_v02(fog, light, sun_alt_deg=10.0, **common).frame_class == "in_cloud"
    scored = classify_frame_v02(visible, light, sun_alt_deg=10.0, **common)
    assert scored.frame_class == "scored"
    assert scored.band_class("summit") == "visible" and scored.obscuration_height == "summit"
    assert scored.band_class("upper") == "no_reference"
    doc = scored.to_dict()
    assert doc["extractor_version"] == "v0.2" and doc["bands"]["summit"]["class"] == "visible"
    json.dumps(doc)


def test_an_undefined_d_leaves_every_band_without_a_reference() -> None:
    """D undefined (a near field too dark): no band can place a reference, skyline or not."""
    params = V02Params()
    pools = {
        band: [_pool_frame(f"{band}{i}", "2026-11-20", 0.6, 10.0) for i in range(3)]
        for band in ("summit", "upper")
    }
    e = _energies(summit=0.05)
    for band in ("summit", "upper"):
        result, used = band_reference(
            band, e, _light(None), sun_alt_deg=10.0, pool=pools[band], params=params, local_day="x"
        )
        assert result.ratio is None and used == [] and "D is undefined" in str(result.missing)
    # C(D) under the floor: only the skyline band loses its reference
    dim = _light(0.97)
    summit, _ = band_reference(
        "summit", e, dim, sun_alt_deg=10.0, pool=pools["summit"], params=params, local_day="x"
    )
    upper, used = band_reference(
        "upper", e, dim, sun_alt_deg=10.0, pool=pools["upper"], params=params, local_day="x"
    )
    assert summit.ratio is None and "under the floor" in str(summit.missing)
    assert upper.ratio is not None and len(used) == 3


def test_the_in_cloud_test_reads_the_first_bands_references_only() -> None:
    """Pre-registration §4: the summit's references, never another band's in their place."""
    params = V02Params()
    order = ("summit", "upper")
    fog = BandEnergies({"summit": 0.001, "upper": 0.001}, 0.01, 0.05, 0.0, 0.0, 0.5, (960, 540))
    pools = {
        band: [_pool_frame(f"{band}{i}", "2026-11-20", 0.97, 10.0) for i in range(3)]
        for band in order
    }
    common = {
        "sun_alt_deg": 10.0,
        "sun_az_deg": 180.0,
        "order": order,
        "pools": pools,
        "thresholds": {"summit": None, "upper": None},
        "params": params,
        "local_day": "2026-11-21",
    }
    # C(0.97) is under the floor: the summit has no reference, so there is no in-cloud test,
    # though the upper band has references
    no_test = classify_frame_v02(fog, _light(0.97), **common)
    assert no_test.frame_class == "scored" and no_test.global_energy_ratio is None
    assert no_test.bands["upper"].ratio is not None
    # with summit references the same frame is in cloud
    pools_ok = {b: [replace(p, d_index=0.6) for p in pools[b]] for b in order}
    flagged = classify_frame_v02(fog, _light(0.6), **{**common, "pools": pools_ok})
    assert flagged.frame_class == "in_cloud"
    assert flagged.global_energy_ratio == pytest.approx(0.01 / 0.07)


def _features(
    plate: tuple[PlateBands, np.ndarray], luma: np.ndarray, params: V02Params, regions: dict
) -> tuple[BandEnergies, Illumination]:
    bands, sky_full = plate
    return frame_features(_jpeg(luma), bands, regions, sky_full, params)


@pytest.mark.parametrize("contrast_model", [True, False])
def test_an_overcast_summit_reads_visible_only_with_the_contrast_model(
    plate: tuple[PlateBands, np.ndarray], contrast_model: bool
) -> None:
    """The mechanism of D-100's failure on a procedural dome, and v0.2's answer to it.

    References come only from sunny frames. Under an overcast the clear dome's skyline is
    faint; the energy ratio alone reads it as hidden, the contrast model as visible. A dome in
    cloud reads low either way.
    """
    params = V02Params(contrast_model=contrast_model)
    regions: dict = {}
    pool = []
    for seed in range(3):
        e, light = _features(plate, scene("sunny", seed=seed), params, regions)
        norm = normalised_energy("summit", e.bands["summit"], light, params)
        assert light.d_index is not None and norm is not None
        pool.append(
            PoolFrame(
                f"s{seed}",
                "2026-11-20",
                10.0,
                light.d_index,
                e.bands["summit"],
                norm,
                e.global_energy,
                e.rms_contrast,
            )
        )

    def ratio(luma: np.ndarray) -> float:
        e, light = _features(plate, luma, params, regions)
        result, _ = band_reference(
            "summit", e, light, sun_alt_deg=10.0, pool=pool, params=params, local_day="2026-11-21"
        )
        assert result.ratio is not None
        return result.ratio

    clear_overcast = ratio(scene("overcast", seed=7))
    clouded = ratio(scene("overcast", cloud=True, seed=8))
    assert clouded < 0.35
    if contrast_model:
        assert clear_overcast > 0.8
    else:
        assert clear_overcast < 0.35
    assert ratio(scene("sunny", seed=9)) == pytest.approx(1.0, abs=0.2)
