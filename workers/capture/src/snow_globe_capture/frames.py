"""Decode, time-stamp, downscale, fingerprint and gate-flag one camera image (PRD §8.6, §8.7).

Shared by live capture and the archive backfill:

- :func:`process_image` decodes the JPEG, reads EXIF, keeps the original bytes when the
  image is at most 1920 px wide and otherwise re-encodes a 1920 px Lanczos copy at JPEG
  quality 90 carrying the original EXIF bytes (CLAUDE.md data layout). It returns the
  digests, sizes, dHash, mean luma, chroma spread and a sharpness score of what is stored,
  and on request a 640 px thumbnail (:func:`make_thumbnail`, PRD §8.7).
- :func:`choose_image_time` picks the image time from the candidates (EXIF, the NPS
  ``imagedate``, HTTP ``Last-Modified``) and reports how far apart they are.
- :func:`sun_position` and :func:`gate_flags` give the capture-time inputs of the PRD §8.6
  quality gates. What each gate has today:

  =============  ==========================================================================
  Fresh          covered by live capture (``duplicate_sha``, ``stale_feed``,
                 ``stale_image_time``, ``same_last_modified_new_bytes``)
  Same view      ``dhash_bits_to_previous`` is recorded; no flag. The gate is plate
                 registration, which waits for plates: TODO(M2)
  Clear lens     ``sharpness_lapvar`` is recorded; no flag. Fog lowers it as much as a wet
                 lens, so the floor needs development frames and masks: TODO(M2)
  Colour mode    ``monochrome`` flag (chroma spread below :data:`MONOCHROME_MAX_CHROMA`)
  Daylight       ``sun`` block and the ``sun_below_3deg`` flag, by ephemeris (never by
                 brightness); scoring recomputes the Sun with ``oculus-core``
  =============  ==========================================================================
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from PIL import ExifTags, Image, ImageFilter, ImageStat, UnidentifiedImageError
from snow_globe_common.images import chroma_spread, dhash64, dhash_hex, exif_summary, luma_stats
from snow_globe_common.timeutil import (
    FLAG_AMBIGUOUS,
    exif_local_to_utc,
    isoformat_z,
    localize,
)

from .sun import solar_elevation_deg

__all__ = [
    "DAYLIGHT_MIN_SUN_DEG",
    "DEFAULT_JPEG_QUALITY",
    "MAX_STORED_WIDTH",
    "MONOCHROME_MAX_CHROMA",
    "SHARPNESS_WIDTH",
    "THUMB_JPEG_QUALITY",
    "THUMB_WIDTH",
    "TIME_CONFLICT_MIN",
    "TINY_FILE_BYTES",
    "TINY_MIN_WIDTH",
    "UPLOAD_LAG_LIMIT_MIN",
    "ImageTimes",
    "ProcessedImage",
    "Thumbnail",
    "choose_image_time",
    "describe_thumbnail",
    "exif_block",
    "gate_flags",
    "laplacian_variance",
    "make_thumbnail",
    "process_image",
    "sha256_hex",
    "sun_position",
    "thumbnail_of_stored",
]

MAX_STORED_WIDTH = 1920
DEFAULT_JPEG_QUALITY = 90
#: A decoded frame at least TINY_MIN_WIDTH px wide but under this many bytes is flagged
#: ``tiny_file`` (an error placeholder is the usual cause). A uniform fog frame is real
#: and compresses well: a 999x666 archive fog frame measured 12,568 bytes (2026-11-19),
#: so the bar sits well below that. Undecodable bodies are flagged ``decode_failed``.
TINY_FILE_BYTES = 8_000
TINY_MIN_WIDTH = 640
#: Two claims of the image time further apart than this are a conflict worth flagging.
TIME_CONFLICT_MIN = 30.0
#: Last-Modified is the upload time; earlier than the image time by more than this is odd.
_UPLOAD_BEFORE_IMAGE_TOLERANCE_MIN = 2.0
#: Last-Modified minus the image time measured 6.50-6.88 min at the 7 ARD cameras and
#: 0.17 min at Rainier (live pass, 19 Nov 2026). A lag over this means a stale, re-saved or
#: mis-stamped file, and is also the widest lag at which ``imagedate`` "agrees" with it.
#: It sits well under 60 min, so a one-hour error can never pass as agreement.
UPLOAD_LAG_LIMIT_MIN = 20.0
#: A camera clock on the wrong daylight-saving rule is off by exactly one hour.
_DST_SHIFT_MIN = 60.0
_DST_SHAPE_TOLERANCE_MIN = 2.0
#: PRD §13.3: a daytime frame has the Sun at least 3° up by ephemeris (apparent altitude).
DAYLIGHT_MIN_SUN_DEG = 3.0
#: ``chroma_spread`` (0-255 scale) below this is a grey, i.e. infrared/monochrome, frame.
#: Calibrated on 19 Nov 2026 (stored JPEGs): the 27 night frames on disk (6 ARD air cams
#: at 10:45 UTC, 21 Rainier frames) read 0.00-0.26; 85 midday archive frames from before
#: the held-out guard (76 thumbnails, 9 hires, Jan 2021 - Jan 2026) read 2.46-91.36; an
#: Acadia twilight frame (Sun -8.9°) read 12.9. Re-check on development fog frames: TODO(M2).
MONOCHROME_MAX_CHROMA = 1.0
#: The sharpness score is measured on a copy at most this wide, so it compares across sizes.
SHARPNESS_WIDTH = 640
#: PRD §8.7: every downloaded frame also gets a thumbnail this wide (staleness checks, Tier 0).
THUMB_WIDTH = 640
#: Thumbnail JPEG quality. ``lookout-capture storage`` (D-080) on the 105 frames on disk on 19 Nov
#: 2026 (06:30-22:23 UTC): a 640 px copy at 85 measured 11.5-81.8 KB, means 34-59 KB per
#: camera, EXIF included (0.4-0.8 KB); at 80 a scratch probe of 29 ARD frames gave 14-22 %
#: less. PRD §8.7 budgets about 52 KB a thumbnail (5 MB for 96 a day).
THUMB_JPEG_QUALITY = 85


def sha256_hex(data: bytes) -> str:
    """Lower-case hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def exif_block(summary: dict[str, Any] | None) -> dict[str, Any]:
    """The EXIF fields kept in sidecars, under their EXIF tag names (None when absent)."""
    s = summary or {}
    return {
        "DateTime": s.get("datetime"),
        "DateTimeOriginal": s.get("datetime_original"),
        "SubSecTimeOriginal": s.get("subsec_original"),
        "OffsetTimeOriginal": s.get("offset_time_original"),
        "Make": s.get("make"),
        "Model": s.get("model"),
        "Artist": s.get("artist"),
        "Copyright": s.get("copyright"),
        "Software": s.get("software"),
        "ImageDescription": s.get("image_description"),
    }


@dataclass(frozen=True)
class Thumbnail:
    """A JPEG thumbnail and its size (PRD §8.7)."""

    data: bytes
    width: int
    height: int
    sha256: str
    jpeg_quality: int
    #: The width it was made for; ``width`` is smaller only for a narrower original.
    target_width: int = THUMB_WIDTH

    def to_sidecar(self) -> dict[str, Any]:
        """The file fields a sidecar records for the thumbnail."""
        return {
            "stored": f"thumb-{self.target_width}",
            "stored_width": self.width,
            "stored_height": self.height,
            "stored_bytes": len(self.data),
            "stored_sha256": self.sha256,
            "jpeg_quality": self.jpeg_quality,
        }


def make_thumbnail(
    image: Image.Image,
    *,
    width: int = THUMB_WIDTH,
    quality: int = THUMB_JPEG_QUALITY,
    exif: bytes = b"",
) -> Thumbnail:
    """A ``width`` px wide Lanczos copy of ``image`` as JPEG ``quality`` (PRD §8.7 thumbnails).

    The aspect ratio is kept; an image already at most ``width`` wide is re-encoded at its
    own size, never enlarged. The original EXIF bytes (0.4-0.8 KB at the NPS cameras) are
    carried over, as in the 1920 px copies, so a thumbnail keeps its camera time and owner.
    """
    rgb = image.convert("RGB")
    if rgb.width > width:
        height = max(1, round(rgb.height * width / rgb.width))
        rgb = rgb.resize((width, height), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    params: dict[str, Any] = {"format": "JPEG", "quality": quality, "optimize": True}
    if exif:
        params["exif"] = exif
    rgb.save(buffer, **params)
    data = buffer.getvalue()
    return Thumbnail(data, rgb.width, rgb.height, sha256_hex(data), quality, target_width=width)


def thumbnail_of_stored(
    data: bytes, *, width: int = THUMB_WIDTH, quality: int = THUMB_JPEG_QUALITY
) -> Thumbnail:
    """A thumbnail made from a frame already stored, for one whose write failed (PRD §8.7).

    The stored JPEG (at most 1920 px) is decoded and thumbnailed as :func:`process_image`
    does, its EXIF kept. Live capture makes thumbnails from the pixels before the 1920 px
    copy is encoded, so a remade one can differ from it by a few bytes. Raises
    ``ValueError`` if the frame does not decode.
    """
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            exif_raw = image.info.get("exif") or b""
            return make_thumbnail(image, width=width, quality=quality, exif=exif_raw)
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValueError(f"stored frame does not decode: {type(exc).__name__}: {exc}") from exc


def describe_thumbnail(data: bytes, *, quality: int = THUMB_JPEG_QUALITY) -> Thumbnail:
    """A :class:`Thumbnail` for thumbnail bytes already written (their sidecar was not).

    The bytes are kept as they are; only the size is read. ``quality`` is the one live
    capture uses. Raises ``ValueError`` if they do not decode.
    """
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValueError(f"thumbnail does not decode: {type(exc).__name__}: {exc}") from exc
    return Thumbnail(data, width, height, sha256_hex(data), quality)


@dataclass
class ProcessedImage:
    """What :func:`process_image` learned about one image, and the bytes to store."""

    original_bytes: int
    sha256: str
    decoded: bool
    stored: bytes
    stored_sha256: str
    stored_mode: str  # "original" | "downscaled-1920" | "undecodable-original"
    original_width: int | None = None
    original_height: int | None = None
    stored_width: int | None = None
    stored_height: int | None = None
    jpeg_quality: int | None = None
    exif: dict[str, Any] = field(default_factory=lambda: exif_block(None))
    exif_raw_bytes: int = 0
    dhash64: str | None = None
    luma_mean: float | None = None
    chroma_spread: float | None = None
    sharpness_lapvar: float | None = None
    decode_error: str | None = None
    flags: list[str] = field(default_factory=list)
    #: The 640 px thumbnail, when :func:`process_image` was asked for one and could decode.
    thumb: Thumbnail | None = None


_LAPLACIAN_POS = ImageFilter.Kernel((3, 3), (0, 1, 0, 1, -4, 1, 0, 1, 0), scale=4)
_LAPLACIAN_NEG = ImageFilter.Kernel((3, 3), (0, -1, 0, -1, 4, -1, 0, -1, 0), scale=4)


def laplacian_variance(image: Image.Image, *, width: int = SHARPNESS_WIDTH) -> float:
    """Variance of the 4-neighbour Laplacian of the luma: the Clear-lens score (PRD §8.6).

    Measured on a box-filtered grey copy at most ``width`` px wide, inside a 1 px border.
    Pillow clips filter output to 0-255, so the response is taken in two halves (positive
    and negative), each divided by 4 to fit; the variance is exact up to that rounding.
    Higher is sharper. Night noise raises it and fog lowers it, so it is recorded, not
    gated: the floor is set per viewpoint on development frames, TODO(M2).
    """
    grey = image.convert("L")
    if grey.width > width:
        height = max(1, round(grey.height * width / grey.width))
        grey = grey.resize((width, height), Image.Resampling.BOX)
    if grey.width < 3 or grey.height < 3:
        return 0.0
    inner = (1, 1, grey.width - 1, grey.height - 1)
    pos = ImageStat.Stat(grey.filter(_LAPLACIAN_POS).crop(inner))
    neg = ImageStat.Stat(grey.filter(_LAPLACIAN_NEG).crop(inner))
    count = pos.count[0]
    mean = (pos.sum[0] - neg.sum[0]) / count
    mean_square = (pos.sum2[0] + neg.sum2[0]) / count
    return 16.0 * max(0.0, mean_square - mean * mean)


def sun_position(moment: datetime, lat: float, lon: float) -> dict[str, Any]:
    """The Sun at ``moment`` for a camera: geometric and apparent elevation, in degrees.

    NOAA solar-calculator equations (:mod:`.sun`, about 0.01°), recorded at capture so the
    Daylight gate never falls back on frame brightness (PRD §8.6). Scoring recomputes the
    Sun with the product ephemeris (``oculus-core``, PRD §8.4).
    """
    geometric = solar_elevation_deg(moment, lat, lon)
    apparent = solar_elevation_deg(moment, lat, lon, refracted=True)
    return {
        "elevation_deg": round(geometric, 3),
        "apparent_elevation_deg": round(apparent, 3),
        "daylight": apparent >= DAYLIGHT_MIN_SUN_DEG,
        "daylight_min_deg": DAYLIGHT_MIN_SUN_DEG,
        "model": "NOAA solar calculator (snow_globe_capture.sun)",
    }


def gate_flags(processed: ProcessedImage, sun: dict[str, Any] | None) -> list[str]:
    """Quality-gate flags known at capture time (PRD §8.6; see the module docstring)."""
    flags: list[str] = []
    if processed.chroma_spread is not None and processed.chroma_spread < MONOCHROME_MAX_CHROMA:
        flags.append("monochrome")
    if sun is not None and not sun.get("daylight", True):
        flags.append("sun_below_3deg")
    return flags


def process_image(
    body: bytes,
    *,
    max_width: int = MAX_STORED_WIDTH,
    quality: int = DEFAULT_JPEG_QUALITY,
    keep_original: bool = False,
    thumb_width: int | None = None,
    thumb_quality: int = THUMB_JPEG_QUALITY,
) -> ProcessedImage:
    """Decode a JPEG and prepare the bytes to store (PRD §8.7: at most 1920 px wide).

    - At most ``max_width`` wide (or ``keep_original``): the downloaded bytes are kept as-is.
    - Wider: Lanczos to ``max_width`` (aspect kept), JPEG ``quality``, original EXIF bytes kept.
    - Undecodable: the bytes are kept as-is and the ``decode_failed`` flag is set, so the
      quality gates can exclude the frame and count the exclusion. No thumbnail is made.
    - ``thumb_width`` (live capture passes :data:`THUMB_WIDTH`): also a thumbnail, made from
      the stored pixels (the 1920 px copy before its JPEG encoding, or the original).

    dHash, mean luma, chroma spread and the sharpness score are measured on the stored pixels.
    """
    sha = sha256_hex(body)
    flags: list[str] = []
    try:
        image = Image.open(io.BytesIO(body))
        image.load()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        flags.append("decode_failed")
        return ProcessedImage(
            original_bytes=len(body),
            sha256=sha,
            decoded=False,
            stored=body,
            stored_sha256=sha,
            stored_mode="undecodable-original",
            decode_error=f"{type(exc).__name__}: {exc}"[:200],
            flags=flags,
        )
    width, height = image.size
    if width >= TINY_MIN_WIDTH and len(body) < TINY_FILE_BYTES:
        flags.append("tiny_file")
    summary = exif_summary(image)
    exif_raw = image.info.get("exif") or b""
    orientation = image.getexif().get(ExifTags.Base.Orientation, 1)
    if orientation not in (None, 1):
        flags.append("exif_orientation")  # pixels are stored as recorded, EXIF unchanged
    if image.format != "JPEG":
        flags.append(f"format_{(image.format or 'unknown').lower()}")

    if keep_original or width <= max_width:
        stored, stored_mode, stored_image, used_quality = body, "original", image, None
    else:
        new_height = max(1, round(height * max_width / width))
        resized = image.convert("RGB").resize((max_width, new_height), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        params: dict[str, Any] = {"format": "JPEG", "quality": quality, "optimize": True}
        if exif_raw:
            params["exif"] = exif_raw
        resized.save(buffer, **params)
        stored = buffer.getvalue()
        stored_mode, stored_image, used_quality = f"downscaled-{max_width}", resized, quality

    stats = luma_stats(stored_image)
    thumb = (
        make_thumbnail(stored_image, width=thumb_width, quality=thumb_quality, exif=exif_raw)
        if thumb_width
        else None
    )
    return ProcessedImage(
        original_bytes=len(body),
        sha256=sha,
        decoded=True,
        stored=stored,
        stored_sha256=sha256_hex(stored),
        stored_mode=stored_mode,
        original_width=width,
        original_height=height,
        stored_width=stored_image.width,
        stored_height=stored_image.height,
        jpeg_quality=used_quality,
        exif=exif_block(summary),
        exif_raw_bytes=len(exif_raw),
        dhash64=dhash_hex(dhash64(stored_image)),
        luma_mean=round(stats.mean, 3),
        chroma_spread=round(chroma_spread(stored_image), 4),
        sharpness_lapvar=round(laplacian_variance(stored_image), 2),
        flags=flags,
        thumb=thumb,
    )


# --- image time ----------------------------------------------------------------------


@dataclass
class ImageTimes:
    """The chosen image time, where it came from, and how the candidates compare."""

    image_time_utc: datetime
    source: str
    candidates: dict[str, str | None]
    disagreement_min: dict[str, float | None]
    flags: list[str]

    def to_sidecar(self) -> dict[str, Any]:
        """Fields for a sidecar: image_time_utc, image_time_source, candidates, disagreements."""
        return {
            "image_time_utc": isoformat_z(self.image_time_utc),
            "image_time_source": self.source,
            "image_time_candidates_utc": self.candidates,
            "image_time_disagreement_min": self.disagreement_min,
            "image_time_flags": self.flags,
        }


def _minutes(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return round((a - b).total_seconds() / 60.0, 3)


def _exif_candidates(
    exif_datetime: str | None, offset: str | None, subsec: str | None, tz_name: str
) -> tuple[list[datetime], list[str]]:
    """UTC readings of an EXIF stamp: one normally, two in a DST fold (earlier first)."""
    if not exif_datetime:
        return [], []
    try:
        first, flags = exif_local_to_utc(exif_datetime, tz_name, offset=offset, subsec=subsec)
    except ValueError as exc:
        return [], [f"exif_time_unparsed: {exc}"[:120]]
    readings = [first]
    if FLAG_AMBIGUOUS in flags:
        cleaned = exif_datetime.replace("\x00", "").strip().replace("-", ":").replace("T", " ")
        try:
            naive = datetime.strptime(cleaned[:19], "%Y:%m:%d %H:%M:%S")
            if subsec:
                digits = "".join(ch for ch in subsec if ch.isdigit())
                if digits:
                    naive = naive.replace(microsecond=int((digits + "000000")[:6]))
            later, _ = localize(naive, tz_name, on_ambiguous="later")
            readings.append(later)
        except ValueError:
            pass
    return readings, sorted(flags)


def choose_image_time(
    *,
    tz_name: str,
    fetched_at: datetime,
    exif_datetime: str | None = None,
    exif_offset: str | None = None,
    exif_subsec: str | None = None,
    imagedate_utc: datetime | None = None,
    last_modified_utc: datetime | None = None,
) -> ImageTimes:
    """Pick the image time (PRD §8.6 "Fresh") and measure the candidates against each other.

    Order of preference:

    1. EXIF ``DateTimeOriginal`` (or ``DateTime``): it travels inside the image bytes.
       With an EXIF offset it is exact; otherwise it is local wall time in the manifest
       zone, with DST handled by :func:`snow_globe_common.timeutil.exif_local_to_utc`. In a
       DST fold the occurrence nearest the NPS ``imagedate`` (else ``Last-Modified``) wins.
    2. The NPS JSON ``imagedate`` (ARD air cams): its zone abbreviation fixes the offset.
    3. HTTP ``Last-Modified``: the upload time, seconds to minutes after capture.
    4. The fetch time, as a last resort (flagged).

    EXIF is overruled, and flagged ``exif_time_overruled``, only for the signature of a
    camera clock on the wrong DST rule: EXIF is within 2 min of exactly one hour from
    ``imagedate``, *and* ``imagedate`` agrees with ``Last-Modified`` (the upload follows it
    by -2 to +:data:`UPLOAD_LAG_LIMIT_MIN` min; 6.5-6.9 min measured). Any other
    disagreement over :data:`TIME_CONFLICT_MIN` keeps EXIF and is flagged ``time_conflict``,
    so a stale image re-served under a new ``imagedate`` keeps its old time (and is then
    flagged stale by the caller). A ``Last-Modified`` more than
    :data:`UPLOAD_LAG_LIMIT_MIN` after the chosen time is flagged
    ``last_modified_long_after_image_time``, which also catches a one-hour error shared by
    EXIF and ``imagedate``.
    """
    flags: list[str] = []
    exif_readings, exif_flags = _exif_candidates(exif_datetime, exif_offset, exif_subsec, tz_name)
    flags.extend(f"exif:{f}" for f in exif_flags)
    reference = imagedate_utc or last_modified_utc
    exif_utc: datetime | None = None
    if exif_readings:
        exif_utc = exif_readings[0]
        if len(exif_readings) > 1 and reference is not None:
            exif_utc = min(exif_readings, key=lambda t: abs((t - reference).total_seconds()))
            flags.append(
                "exif_fold_resolved_by_imagedate"
                if imagedate_utc is not None
                else "exif_fold_resolved_by_last_modified"
            )

    candidates: dict[str, datetime | None] = {
        "exif": exif_utc,
        "json_imagedate": imagedate_utc,
        "http_last_modified": last_modified_utc,
    }
    disagreement = {
        "exif_minus_json_imagedate": _minutes(exif_utc, imagedate_utc),
        "http_last_modified_minus_exif": _minutes(last_modified_utc, exif_utc),
        "http_last_modified_minus_json_imagedate": _minutes(last_modified_utc, imagedate_utc),
    }
    claims = [t for t in (exif_utc, imagedate_utc) if t is not None]
    disagreement["max_abs_between_image_time_claims"] = (
        round(abs((claims[0] - claims[1]).total_seconds()) / 60.0, 3) if len(claims) == 2 else None
    )

    chosen: datetime
    source: str
    if exif_utc is not None:
        chosen = exif_utc
        source = "exif_offset" if exif_offset else "exif_local"
        gap = disagreement["exif_minus_json_imagedate"]
        if gap is not None and abs(gap) > TIME_CONFLICT_MIN:
            lm_gap = disagreement["http_last_modified_minus_json_imagedate"]
            dst_shaped = abs(abs(gap) - _DST_SHIFT_MIN) <= _DST_SHAPE_TOLERANCE_MIN
            imagedate_agrees = (
                lm_gap is not None
                and -_UPLOAD_BEFORE_IMAGE_TOLERANCE_MIN <= lm_gap <= UPLOAD_LAG_LIMIT_MIN
            )
            if dst_shaped and imagedate_agrees:
                chosen, source = imagedate_utc, "json_imagedate"  # type: ignore[assignment]
                flags.append("exif_time_overruled")
            else:
                flags.append("time_conflict")
    elif imagedate_utc is not None:
        chosen, source = imagedate_utc, "json_imagedate"
    elif last_modified_utc is not None:
        chosen, source = last_modified_utc, "http_last_modified"
    else:
        chosen, source = fetched_at, "fetched_at"
        flags.append("image_time_from_fetch")

    lm_lag = _minutes(last_modified_utc, chosen)
    if lm_lag is not None:
        if lm_lag < -_UPLOAD_BEFORE_IMAGE_TOLERANCE_MIN:
            flags.append("last_modified_before_image_time")
        elif lm_lag > UPLOAD_LAG_LIMIT_MIN:
            flags.append("last_modified_long_after_image_time")
    if chosen > fetched_at + timedelta(minutes=_UPLOAD_BEFORE_IMAGE_TOLERANCE_MIN):
        flags.append("image_time_in_future")
    return ImageTimes(
        image_time_utc=chosen,
        source=source,
        candidates={k: (isoformat_z(v) if v is not None else None) for k, v in candidates.items()},
        disagreement_min=disagreement,
        flags=flags,
    )
