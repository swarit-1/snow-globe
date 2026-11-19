"""Extractor v−1 (PRD §13.3) on procedural images and on the local sample frames."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from conftest import sample
from PIL import Image, ImageEnhance, ImageFilter
from synth_images import (
    PEAK_BOX,
    blurred,
    encoded,
    fogged,
    fogged_far,
    in_cloud,
    landscape,
    to_rgb,
)

from lookout_eval.extractor import (
    ClearDayReference,
    ExtractorParams,
    ReferenceFrame,
    Region,
    classify_ratio,
    edge_stats,
    extract,
)

T0 = datetime(2026, 11, 20, 20, 0, tzinfo=UTC)
SUN = (22.0, 185.0)  # alt bin 4, az bin 9 with 5 deg x 20 deg bins
REGION = Region.box(*PEAK_BOX)


def _fixed_sun(_: datetime) -> tuple[float, float]:
    return SUN


@pytest.fixture
def reference(tmp_path: Path) -> ClearDayReference:
    frames = []
    for seed in range(1, 6):
        path = tmp_path / f"ref{seed}.png"
        to_rgb(landscape(seed)).save(path)
        frames.append(ReferenceFrame(path, T0 + timedelta(days=seed)))
    return ClearDayReference.build(frames, region=REGION, sun_of=_fixed_sun)


def _score(image, reference, sun=SUN):
    return extract(image, REGION, sun_alt_deg=sun[0], sun_az_deg=sun[1], reference=reference)


def test_sharp_frame_is_visible(reference) -> None:
    r = _score(to_rgb(landscape(9)), reference)
    assert 0.9 <= r.ratio <= 1.1
    assert r.state == "visible" and r.camera_in_cloud is False and r.p1_class == "visible"
    assert r.reference.n == 5 and r.reference.bin_steps == 0


def test_blur_lowers_the_ratio_monotonically(reference) -> None:
    base = landscape(9)
    ratios = [
        _score(to_rgb(blurred(base, s) if s else base), reference).ratio for s in (0, 1, 2, 3, 4, 6)
    ]
    assert ratios == sorted(ratios, reverse=True)
    assert _score(to_rgb(blurred(base, 1)), reference).state == "visible"
    assert _score(to_rgb(blurred(base, 4)), reference).state == "hidden"


def test_fog_overlay_lowers_the_ratio_monotonically(reference) -> None:
    base = landscape(9)
    ratios = [_score(to_rgb(fogged(base, a)), reference).ratio for a in (0.0, 0.2, 0.4, 0.6, 0.8)]
    assert ratios == sorted(ratios, reverse=True)
    assert _score(to_rgb(fogged(base, 0.4)), reference).state == "partial"
    # fog over the distant peak only: summit hidden, the camera itself is not in cloud
    distant = _score(to_rgb(fogged_far(base, 0.7)), reference)
    assert distant.state == "hidden" and distant.camera_in_cloud is False
    # the same veil over the whole frame, foreground included, is a global contrast collapse
    everywhere = _score(to_rgb(fogged(base, 0.7)), reference)
    assert everywhere.state == "hidden" and everywhere.camera_in_cloud is True


def test_exposure_change_barely_moves_the_ratio(reference) -> None:
    base = landscape(9)
    sharp = _score(to_rgb(base), reference).ratio
    for gain in (0.4, 0.6, 0.8):
        ratio = _score(to_rgb(base * gain), reference).ratio
        assert 0.9 <= ratio / sharp <= 1.1, gain


def test_camera_in_cloud_is_a_global_contrast_collapse(reference) -> None:
    r = _score(to_rgb(in_cloud(3)), reference)
    assert r.camera_in_cloud is True and r.p1_class == "in_cloud"
    assert r.global_energy_ratio < 0.1 and r.contrast_ratio < 0.1


@pytest.mark.parametrize(
    ("value", "fault"), [(0, "dark"), (255, "saturated"), (128, "blank"), (2, "dark")]
)
def test_camera_faults_are_not_weather(reference, value: int, fault: str) -> None:
    """Review finding: black and white frames used to come out as 'hidden, camera in cloud'."""
    frame = Image.new("RGB", (640, 360), (value,) * 3)
    r = _score(frame, reference)
    assert r.camera_fault == fault and r.p1_class == "camera_fault"
    assert r.state is None and r.camera_in_cloud is None and "camera fault" in r.reason
    assert r.to_dict()["camera_fault"] == fault


def test_a_black_frame_with_an_overlay_is_still_a_fault(reference) -> None:
    frame = Image.new("RGB", (640, 360), (0, 0, 0))
    frame.paste((255, 255, 255), (0, 0, 640, 12))  # a timestamp banner, 3 % of the frame
    assert _score(frame, reference).camera_fault == "dark"


def test_fog_and_in_cloud_frames_are_not_camera_faults(reference) -> None:
    assert _score(to_rgb(in_cloud(3)), reference).camera_fault is None
    assert _score(to_rgb(fogged(landscape(9), 0.8)), reference).camera_fault is None
    assert _score(to_rgb(landscape(9) * 0.3), reference).camera_fault is None  # dim, not black


def test_rendered_png_and_camera_jpeg_score_alike(reference) -> None:
    image = to_rgb(landscape(9))
    png = _score(encoded(image, "PNG"), reference).ratio
    jpeg = _score(encoded(image, "JPEG", quality=90), reference).ratio
    assert abs(jpeg / png - 1.0) < 0.05


def test_region_coordinates_scale_with_frame_size() -> None:
    params = ExtractorParams()
    image = to_rgb(landscape(9))
    region = Region.box(*PEAK_BOX, frame_size=(640, 360))
    small = edge_stats(image, region, params).region_energy
    large = edge_stats(image.resize((1920, 1080), Image.Resampling.LANCZOS), region, params)
    assert abs(large.region_energy / small - 1.0) < 0.03
    assert large.image_size == (1920, 1080) and large.analysis_size == (960, 540)


def test_polygon_rectangle_equals_box() -> None:
    params = ExtractorParams()
    x0, y0, x1, y1 = PEAK_BOX
    image = to_rgb(landscape(9))
    box = edge_stats(image, Region.box(x0, y0, x1, y1), params)
    poly = edge_stats(image, Region.polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)]), params)
    assert abs(poly.region_energy / box.region_energy - 1.0) < 0.02
    assert abs(poly.region_px - box.region_px) / box.region_px < 0.03


def test_region_parsing_and_validation() -> None:
    assert Region.parse("1,2,30,40") == Region.box(1, 2, 30, 40)
    poly = Region.parse("0,0;10,0;10,10")
    assert poly.kind == "polygon" and len(poly.points) == 3
    assert Region.parse(
        '{"kind": "box", "points": [[1, 2], [30, 40]], "frame_size": [640, 360]}'
    ).frame_size == (640, 360)
    with pytest.raises(ValueError):
        Region.box(10, 10, 5, 20)
    with pytest.raises(ValueError):
        Region.parse("1,2,3")
    params = ExtractorParams()
    with pytest.raises(ValueError, match="outside"):
        edge_stats(to_rgb(landscape(1)), Region.box(600, 300, 700, 400), params)
    with pytest.raises(ValueError, match="fewer than"):
        edge_stats(to_rgb(landscape(1)), Region.box(10, 10, 12, 12), params)


def test_reference_bins_and_fallback(reference) -> None:
    exact = reference.lookup(21.0, 181.0)
    assert exact is not None and exact.bin == exact.query_bin and exact.bin_steps == 0
    near = reference.lookup(26.0, 205.0)  # one bin up in altitude and azimuth
    assert near is not None and near.bin_steps == 1
    assert reference.lookup(40.0, 185.0) is None
    far = _score(to_rgb(landscape(9)), reference, sun=(40.0, 185.0))
    assert far.state is None and far.ratio is None and "no reference" in far.reason


def test_reference_round_trip_and_consistency_checks(reference) -> None:
    again = ClearDayReference.from_dict(reference.to_dict())
    assert again.lookup(*SUN) == reference.lookup(*SUN)
    with pytest.raises(ValueError, match="energy settings"):
        extract(
            to_rgb(landscape(9)),
            REGION,
            sun_alt_deg=SUN[0],
            sun_az_deg=SUN[1],
            reference=reference,
            params=ExtractorParams(analysis_width=480),
        )
    with pytest.raises(ValueError, match="region differs"):
        extract(
            to_rgb(landscape(9)),
            Region.box(0, 0, 100, 100),
            sun_alt_deg=SUN[0],
            sun_az_deg=SUN[1],
            reference=reference,
        )
    loose = ExtractorParams(tau_hi=0.2, tau_lo=0.1)
    assert (
        extract(
            to_rgb(blurred(landscape(9), 4)),
            REGION,
            sun_alt_deg=SUN[0],
            sun_az_deg=SUN[1],
            reference=reference,
            params=loose,
        ).state
        == "partial"
    )


def test_classify_ratio_boundaries() -> None:
    assert classify_ratio(0.6, tau_hi=0.6, tau_lo=0.35) == "visible"
    assert classify_ratio(0.5999, tau_hi=0.6, tau_lo=0.35) == "partial"
    assert classify_ratio(0.35, tau_hi=0.6, tau_lo=0.35) == "partial"
    assert classify_ratio(0.3499, tau_hi=0.6, tau_lo=0.35) == "hidden"
    with pytest.raises(ValueError):
        ExtractorParams(tau_hi=0.3, tau_lo=0.5)


def test_reference_frame_time_from_key(tmp_path: Path) -> None:
    folder = tmp_path / "archive" / "point-reyes.beach" / "2026-11-17"
    folder.mkdir(parents=True)
    path = folder / "20261117T200000Z_50c46ac9.jpg"
    to_rgb(landscape(1)).save(path)
    assert ReferenceFrame.from_path(path).image_time == datetime(2026, 11, 17, 20, tzinfo=UTC)


@pytest.mark.parametrize(
    ("name", "box"),
    [("mountain.jpg", (760, 300, 1160, 600)), ("porelarge.jpg", (1520, 600, 2320, 1200))],
)
def test_sample_frames(tmp_path: Path, name: str, box: tuple[int, int, int, int]) -> None:
    """Real NPS frames (night of 18 Nov): deterministic, exposure-tolerant, and blur or fog hides.

    Measured 2026-11-19: brightness x0.7 moved the ratio by 2.1 % (Rainier) and 5.6 % (the
    dark Point Reyes frame); 8 px blur gave 0.45 and 0.12; a 0.6 fog veil gave 0.16 and 0.08.
    """
    path = sample(name)
    with Image.open(path) as im:
        im.load()
        frame = im.copy()
    region = Region.box(*box, frame_size=frame.size)
    reference = ClearDayReference.build(
        [ReferenceFrame(path, T0)], region=region, sun_of=_fixed_sun
    )

    def score(image):
        return extract(image, region, sun_alt_deg=SUN[0], sun_az_deg=SUN[1], reference=reference)

    itself = score(path)
    assert itself.ratio == pytest.approx(1.0) and itself.state == "visible"
    assert itself.camera_fault is None  # dark night frames, but not camera faults
    assert score(path).stats == itself.stats  # deterministic
    darker = score(ImageEnhance.Brightness(frame).enhance(0.7)).ratio
    assert 0.9 <= darker <= 1.1
    blur = score(frame.filter(ImageFilter.GaussianBlur(8 * frame.size[0] / 1920))).ratio
    assert blur < 0.6
    rgb = np.asarray(frame.convert("RGB"), dtype=float) / 255.0
    veil = Image.fromarray(np.clip((rgb * 0.4 + 0.9 * 0.6) * 255 + 0.5, 0, 255).astype(np.uint8))
    assert score(veil).state == "hidden"
