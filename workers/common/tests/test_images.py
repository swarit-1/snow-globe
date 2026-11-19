from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from lookout_common import images as im

from .conftest import SAMPLES


def gradient(width: int = 64, height: int = 48, *, rising: bool = True) -> Image.Image:
    image = Image.new("L", (width, height))
    for x in range(width):
        level = round(255 * x / (width - 1))
        ImageDraw.Draw(image).line([(x, 0), (x, height)], fill=level if rising else 255 - level)
    return image.convert("RGB")


def scene(seed: int) -> Image.Image:
    image = Image.new("RGB", (320, 180), (90, 120, 160))
    draw = ImageDraw.Draw(image)
    draw.polygon(
        [(0, 180), (60 + seed, 70), (160, 140), (250 - seed, 40), (320, 180)], fill=(60, 70, 50)
    )
    draw.ellipse((200 + seed, 20, 240 + seed, 60), fill=(250, 240, 200))
    return image


def test_dhash_of_gradients():
    assert im.dhash64(gradient(rising=False)) == 2**64 - 1  # every pixel brighter than its right
    assert im.dhash64(gradient(rising=True)) == 0
    assert im.dhash_hex(0) == "0" * 16 and im.dhash_hex(2**64 - 1) == "f" * 16
    with pytest.raises(ValueError):
        im.dhash_hex(2**64)


def test_dhash_is_stable_under_recompression_and_separates_scenes():
    original = scene(0)
    buffer = io.BytesIO()
    original.save(buffer, format="JPEG", quality=60)
    recompressed = buffer.getvalue()
    assert im.hamming64(im.dhash64(original), im.dhash64(recompressed)) <= 4
    assert im.hamming64(im.dhash64(original), im.dhash64(scene(40))) >= 8
    assert im.dhash64(recompressed) == im.dhash64(io.BytesIO(recompressed).getvalue())


def test_luma_stats_on_flat_and_split_images():
    flat = im.luma_stats(Image.new("RGB", (100, 50), (128, 128, 128)))
    assert (flat.mean, flat.std, flat.p01, flat.p50, flat.p99) == (128.0, 0.0, 128, 128, 128)
    assert (flat.width, flat.height) == (100, 50)
    split = Image.new("L", (100, 100), 0)
    split.paste(255, (50, 0, 100, 100))
    stats = im.luma_stats(split)
    assert stats.mean == pytest.approx(127.5)
    assert stats.dark_frac == pytest.approx(0.5) and stats.clipped_frac == pytest.approx(0.5)
    assert (stats.min, stats.max, stats.p01, stats.p99) == (0, 255, 0, 255)
    assert stats.to_dict()["mean"] == pytest.approx(127.5)


def test_luma_stats_downsamples_large_frames_but_reports_full_size():
    big = Image.new("RGB", (3840, 2160), (10, 20, 30))
    stats = im.luma_stats(big)
    assert (stats.width, stats.height) == (3840, 2160)
    assert stats.mean == pytest.approx(0.299 * 10 + 0.587 * 20 + 0.114 * 30, abs=1)


def test_chroma_spread_detects_monochrome():
    assert im.chroma_spread(Image.new("RGB", (64, 64), (77, 77, 77))) == 0.0
    assert im.chroma_spread(Image.new("RGB", (64, 64), (255, 0, 0))) == 255.0
    assert im.chroma_spread(Image.new("L", (64, 64), 200)) == 0.0
    assert im.chroma_spread(scene(0)) > 5


def test_fit_width_and_jpeg_bytes():
    frame = Image.new("RGB", (3840, 2160), (1, 2, 3))
    small = im.fit_width(frame, 1920)
    assert small.size == (1920, 1080)
    narrow = Image.new("RGB", (1920, 1080))
    assert im.fit_width(narrow, 1920) is narrow
    data = im.jpeg_bytes(small, quality=85)
    assert data[:2] == b"\xff\xd8"
    assert im.load_image(data).size == (1920, 1080)


def test_fit_width_applies_exif_orientation():
    portrait_on_disk = Image.new("RGB", (400, 200))
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees clockwise to display
    buffer = io.BytesIO()
    portrait_on_disk.save(buffer, format="JPEG", exif=exif.tobytes())
    shown = im.fit_width(buffer.getvalue(), 1000)
    assert shown.size == (200, 400)


def _jpeg_with_exif() -> bytes:
    exif = Image.Exif()
    exif[0x0110] = "P1387"  # Model
    exif[0x010F] = "AXIS"  # Make
    exif[0x0132] = "2026:11:18 20:21:03"  # DateTime
    exif[0x013B] = "National Park Service"  # Artist
    sub = exif.get_ifd(0x8769)
    sub[0x9003] = "2026:11:18 20:21:03"  # DateTimeOriginal
    sub[0x9291] = "194"  # SubSecTimeOriginal
    sub[0x882A] = 65528  # TimeZoneOffset as the Axis camera stores it (-8 as unsigned)
    buffer = io.BytesIO()
    Image.new("RGB", (32, 16)).save(buffer, format="JPEG", exif=exif.tobytes())
    return buffer.getvalue()


def test_exif_summary_reads_axis_style_fields():
    info = im.exif_summary(_jpeg_with_exif())
    assert info["make"] == "AXIS" and info["model"] == "P1387"
    assert info["datetime_original"] == "2026:11:18 20:21:03"
    assert info["subsec_original"] == "194"
    assert info["offset_time_original"] == "-08:00"
    assert info["artist"] == "National Park Service" and info["copyright"] is None
    assert (info["width"], info["height"]) == (32, 16)


def test_exif_summary_without_exif():
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buffer, format="PNG")
    info = im.exif_summary(buffer.getvalue())
    assert info["datetime"] is None and info["offset_time_original"] is None


def test_load_image_from_path(tmp_path: Path):
    path = tmp_path / "x.png"
    Image.new("RGB", (5, 7)).save(path)
    assert im.load_image(path).size == (5, 7)
    assert im.load_image(str(path)).size == (5, 7)


# --- recorded fixtures (present on the dev machine only; skipped in CI) ---------------------
@pytest.mark.skipif(not (SAMPLES / "mountain.jpg").exists(), reason="local sample frames absent")
def test_recorded_rainier_frame():
    info = im.exif_summary(SAMPLES / "mountain.jpg")
    assert (info["make"], info["model"], info["offset_time_original"]) == (
        "AXIS",
        "P1387",
        "-08:00",
    )
    assert info["artist"] is None and info["copyright"] is None
    assert (info["width"], info["height"]) == (1920, 1080)
    assert im.chroma_spread(SAMPLES / "mountain.jpg") < 0.5  # monochrome night frame


@pytest.mark.skipif(not (SAMPLES / "porelarge.jpg").exists(), reason="local sample frames absent")
def test_recorded_point_reyes_frame():
    info = im.exif_summary(SAMPLES / "porelarge.jpg")
    assert info["artist"] == "National Park Service" == info["copyright"]
    assert info["software"] == "ARS_Digicam"
    assert (info["width"], info["height"]) == (3840, 2160)
