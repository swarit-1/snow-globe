"""Small image helpers shared by capture and evaluation (PRD §8.6 frame quality gates).

- :func:`dhash64`: a 64-bit difference hash, for "same frame again?" and "same view?"
  pre-checks. It is a cheap pre-filter, not the registration gate itself.
- :func:`luma_stats`: brightness statistics. PRD §8.6 forbids deciding *daylight* from
  brightness (the ephemeris decides), so these feed exposure and clear-lens checks only.
- :func:`chroma_spread`: mean per-pixel channel spread; near zero means a monochrome
  (infrared) night frame, which PRD §8.6 flags and keeps out of v1 scoring.
- :func:`fit_width` and :func:`jpeg_bytes`: the stored frame is at most 1920 px wide
  (CLAUDE.md data layout).
- :func:`exif_summary`: the EXIF fields capture records (time, offset, make/model,
  NPS authorship).

All helpers accept a ``PIL.Image.Image``, raw bytes, or a path.
"""

from __future__ import annotations

import io
import os
from dataclasses import asdict, dataclass
from typing import Any

from PIL import ExifTags, Image, ImageChops, ImageOps, ImageStat

__all__ = [
    "ImageSource",
    "LumaStats",
    "chroma_spread",
    "dhash64",
    "dhash_hex",
    "exif_summary",
    "fit_width",
    "hamming64",
    "jpeg_bytes",
    "load_image",
    "luma_stats",
]

type ImageSource = Image.Image | bytes | bytearray | str | os.PathLike[str]

_ANALYSIS_MAX_SIDE = 512


def load_image(source: ImageSource) -> Image.Image:
    """Open ``source`` (image, bytes or path) and return a fully loaded Pillow image."""
    if isinstance(source, Image.Image):
        return source
    if isinstance(source, (bytes, bytearray)):
        image = Image.open(io.BytesIO(bytes(source)))
    else:
        image = Image.open(os.fspath(source))
    image.load()
    return image


def _rgb(image: Image.Image) -> Image.Image:
    if image.mode == "RGB":
        return image
    if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
        background = Image.new("RGB", image.size, (0, 0, 0))
        background.paste(image.convert("RGBA"), mask=image.convert("RGBA").getchannel("A"))
        return background
    return image.convert("RGB")


def _analysis_copy(image: Image.Image) -> Image.Image:
    """A small RGB copy for statistics; box-filtered so it averages, not aliases."""
    rgb = _rgb(image)
    longest = max(rgb.size)
    if longest <= _ANALYSIS_MAX_SIDE:
        return rgb
    scale = _ANALYSIS_MAX_SIDE / longest
    size = (max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale)))
    return rgb.resize(size, Image.Resampling.BOX)


def dhash64(source: ImageSource) -> int:
    """Return the 64-bit horizontal difference hash (dHash) of an image.

    The image is converted to 8-bit luma, box-resized to 9 x 8, and each bit records
    whether a pixel is brighter than its right-hand neighbour (row-major, MSB first).
    """
    image = _rgb(load_image(source)).convert("L")
    small = image.resize((9, 8), Image.Resampling.BOX)
    pixels = small.tobytes()
    value = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            value = (value << 1) | (1 if pixels[base + col] > pixels[base + col + 1] else 0)
    return value


def dhash_hex(value: int) -> str:
    """Format a 64-bit hash as 16 lower-case hex characters."""
    if not 0 <= value < 2**64:
        raise ValueError("hash must fit in 64 bits")
    return f"{value:016x}"


def hamming64(a: int, b: int) -> int:
    """Number of differing bits between two 64-bit hashes."""
    return (a ^ b).bit_count()


@dataclass(frozen=True, slots=True)
class LumaStats:
    """Rec. 601 luma statistics on a 0-255 scale, from a <= 512 px analysis copy."""

    mean: float
    std: float
    p01: int
    p50: int
    p99: int
    min: int
    max: int
    dark_frac: float  # share of pixels with luma <= 16
    clipped_frac: float  # share of pixels with luma >= 250
    width: int
    height: int

    def to_dict(self) -> dict[str, Any]:
        """Plain dict for JSON sidecars."""
        return asdict(self)


def _percentile(histogram: list[int], total: int, fraction: float) -> int:
    threshold = fraction * total
    running = 0
    for level, count in enumerate(histogram):
        running += count
        if running >= threshold and count:
            return level
    return len(histogram) - 1


def luma_stats(source: ImageSource) -> LumaStats:
    """Return brightness statistics of an image (never used to decide daylight; §8.6)."""
    image = load_image(source)
    luma = _analysis_copy(image).convert("L")
    stat = ImageStat.Stat(luma)
    histogram = luma.histogram()
    total = luma.width * luma.height
    low, high = luma.getextrema()
    return LumaStats(
        mean=float(stat.mean[0]),
        std=float(stat.stddev[0]),
        p01=_percentile(histogram, total, 0.01),
        p50=_percentile(histogram, total, 0.50),
        p99=_percentile(histogram, total, 0.99),
        min=int(low),
        max=int(high),
        dark_frac=sum(histogram[:17]) / total,
        clipped_frac=sum(histogram[250:]) / total,
        width=image.width,
        height=image.height,
    )


def chroma_spread(source: ImageSource) -> float:
    """Mean of ``max(R,G,B) - min(R,G,B)`` per pixel on a 0-255 scale.

    About 0 for a grey (infrared) frame. The cut-off for the colour-mode gate is not
    set here: calibrate it on real day and night frames first.
    """
    red, green, blue = _analysis_copy(load_image(source)).split()
    high = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    low = ImageChops.darker(ImageChops.darker(red, green), blue)
    return float(ImageStat.Stat(ImageChops.subtract(high, low)).mean[0])


def fit_width(source: ImageSource, max_width: int = 1920) -> Image.Image:
    """Return the image scaled down (Lanczos) to at most ``max_width`` px wide.

    The same image object comes back when it is already narrow enough and has no EXIF
    rotation. EXIF orientation is applied first, so width means displayed width.
    """
    image = load_image(source)
    if image.getexif().get(ExifTags.Base.Orientation, 1) not in (None, 1):
        image = ImageOps.exif_transpose(image)
    if image.width <= max_width:
        return image
    height = max(1, round(image.height * max_width / image.width))
    return image.resize((max_width, height), Image.Resampling.LANCZOS)


def jpeg_bytes(image: Image.Image, *, quality: int = 90, exif: bytes | None = None) -> bytes:
    """Encode an image as baseline JPEG bytes (optionally carrying raw EXIF bytes)."""
    buffer = io.BytesIO()
    params: dict[str, Any] = {"format": "JPEG", "quality": quality, "optimize": True}
    if exif:
        params["exif"] = exif
    _rgb(image).save(buffer, **params)
    return buffer.getvalue()


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-16-le") if b"\x00" in value else value.decode("ascii")
        except UnicodeDecodeError:
            return None
    text = str(value).replace("\x00", "").strip()
    return text or None


def _signed16(value: int) -> int:
    return value - 0x10000 if value >= 0x8000 else value


def exif_summary(source: ImageSource) -> dict[str, Any]:
    """Return the EXIF fields capture records, as plain JSON-ready values.

    Keys (each None when absent): ``datetime``, ``datetime_original``,
    ``subsec_original``, ``offset_time_original`` (``"+HH:MM"``, from
    ``OffsetTimeOriginal``, else from the legacy ``TimeZoneOffset`` tag the Axis
    P1387 writes), ``make``, ``model``, ``software``, ``artist``, ``copyright``,
    ``image_description``, plus ``width`` and ``height``.
    """
    image = load_image(source)
    exif = image.getexif()
    sub = exif.get_ifd(ExifTags.IFD.Exif)
    tags = ExifTags.Base

    offset = _clean_text(sub.get(tags.OffsetTimeOriginal)) or _clean_text(sub.get(tags.OffsetTime))
    if offset is None:
        raw = sub.get(tags.TimeZoneOffset)
        hours = raw[0] if isinstance(raw, (tuple, list)) and raw else raw
        if isinstance(hours, int):
            signed = _signed16(hours)
            if -14 <= signed <= 14:
                offset = f"{'-' if signed < 0 else '+'}{abs(signed):02d}:00"

    return {
        "datetime": _clean_text(exif.get(tags.DateTime)),
        "datetime_original": _clean_text(sub.get(tags.DateTimeOriginal)),
        "subsec_original": _clean_text(sub.get(tags.SubsecTimeOriginal)),
        "offset_time_original": offset,
        "make": _clean_text(exif.get(tags.Make)),
        "model": _clean_text(exif.get(tags.Model)),
        "software": _clean_text(exif.get(tags.Software)),
        "artist": _clean_text(exif.get(tags.Artist)),
        "copyright": _clean_text(exif.get(tags.Copyright)),
        "image_description": _clean_text(exif.get(tags.ImageDescription)),
        "width": image.width,
        "height": image.height,
    }
