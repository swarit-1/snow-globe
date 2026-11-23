"""Band, polygon and label masks (plates.masks) on synthetic DEM views."""

from __future__ import annotations

import numpy as np
import pytest

from snow_globe_plates.dem import DemView
from snow_globe_plates.masks import BandSpec, band_masks, label_image, polygon_mask


def _view() -> DemView:
    h, w = 60, 80
    rows = np.arange(h)[:, None] * np.ones((1, w))
    sky = rows < 10
    height = np.where(sky, np.nan, 4500.0 - rows * 50.0)  # 4000 m at row 10, 2000 m at row 50
    distance = np.where(sky, np.nan, np.where(rows < 40, 7000.0, 2500.0))
    xs = np.arange(w) + 0.5
    ys = np.arange(h) + 0.5
    return DemView(xs, ys, distance, height, sky, np.zeros((h, w), bool))


BANDS = [
    BandSpec("summit", min_height_m=4000.0),
    BandSpec("upper", min_height_m=3200.0, max_height_m=4000.0),
    BandSpec("near", max_height_m=2400.0, min_distance_m=1500.0, max_distance_m=4000.0),
]


def test_bands_follow_height_and_distance_and_take_a_sky_margin() -> None:
    view = _view()
    masks = band_masks(view, BANDS, occluder=np.zeros((60, 80), bool), skyline_margin_px=3)
    summit = masks["summit"]
    assert summit[10:11].all()  # 4000 m row
    assert summit[7:10].all() and not summit[6].any()  # three sky rows above it
    assert not summit[11].any()
    upper = masks["upper"]
    assert upper[11:27].all() and not upper[27].any()
    assert masks["near"][43:].all() and not masks["near"][:43].any()  # < 2400 m from row 43


def test_occluder_and_overlay_are_removed() -> None:
    view = _view()
    occ = polygon_mask([[(0, 0), (20, 0), (20, 60), (0, 60)]], (80, 60))
    masks = band_masks(view, BANDS, occluder=occ, overlay_box=(0, 55, 80, 60), skyline_margin_px=0)
    assert not masks["summit"][:, :19].any() and masks["summit"][10, 30]
    assert not masks["near"][55:].any() and masks["near"][50, 40]


def _side_by_side() -> DemView:
    """Sky above row 10; summit-high terrain in columns 0-39, upper-high in 40-79."""
    h, w = 60, 80
    rows = np.arange(h)[:, None] * np.ones((1, w))
    cols = np.ones((h, 1)) * np.arange(w)[None, :]
    sky = rows < 10
    height = np.where(sky, np.nan, np.where(cols < 40, 4100.0, 3500.0))
    distance = np.where(sky, np.nan, 7000.0)
    return DemView(np.arange(w) + 0.5, np.arange(h) + 0.5, distance, height, sky, sky & False)


def test_margins_are_shared_out_so_no_pixel_is_in_two_bands() -> None:
    masks = band_masks(_side_by_side(), BANDS, occluder=np.zeros((60, 80), bool))
    assert not (masks["summit"] & masks["upper"]).any()
    assert masks["summit"][9, 39] and masks["upper"][8, 40]  # each to its nearer terrain
    assert masks["summit"][4, 10] and not masks["summit"][3, 10]  # six rows of margin
    lab = label_image(masks)
    for k, mask in enumerate(masks.values(), start=1):
        assert np.array_equal(lab == k, mask)


def test_observed_sky_clips_bands_and_moves_their_margin() -> None:
    view = _view()
    observed = np.zeros((60, 80), bool)
    observed[:10] = True
    observed[:14, 40:] = True  # the real skyline sits 4 rows lower on the right half
    masks = band_masks(
        view, BANDS, occluder=np.zeros((60, 80), bool), skyline_margin_px=3, observed_sky=observed
    )
    summit, upper = masks["summit"], masks["upper"]
    assert summit[10, :40].all() and not summit[:, 43:].any()
    assert observed[:, 40:][summit[:, 40:]].all()  # right of x 40 the summit is margin only
    assert not (summit | upper)[:14, 40:][~observed[:14, 40:]].any()
    assert upper[11:14, 45].all() and upper[14:27, 45].all()  # margin above the real skyline
    assert not upper[10, 45]  # four rows up is beyond a 3 px margin
    assert not (summit & upper).any()


def test_label_image_first_band_wins() -> None:
    a = np.zeros((4, 4), bool)
    a[0] = True
    b = np.zeros((4, 4), bool)
    b[:2] = True
    lab = label_image({"a": a, "b": b})
    assert lab[0].tolist() == [1, 1, 1, 1] and lab[1].tolist() == [2, 2, 2, 2] and lab[2].sum() == 0


def test_validation() -> None:
    with pytest.raises(ValueError):
        BandSpec("x", min_height_m=5.0, max_height_m=5.0)
    with pytest.raises(ValueError):
        BandSpec("bad name")
    with pytest.raises(ValueError, match="repeat"):
        band_masks(_view(), [BANDS[0], BANDS[0]], occluder=np.zeros((60, 80), bool))
    with pytest.raises(ValueError, match="size"):
        band_masks(_view(), BANDS, occluder=np.zeros((5, 5), bool))
    with pytest.raises(ValueError, match="three vertices"):
        polygon_mask([[(0, 0), (1, 1)]], (4, 4))
    assert BandSpec.from_dict(BANDS[2].to_dict()) == BANDS[2]
