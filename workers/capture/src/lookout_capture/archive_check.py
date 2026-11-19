"""The M0.5 NPS archive check: depth, cadence, time stamps and camera epochs (PRD §13.2, §13.9).

For each ARD viewpoint it asks the archive, politely (see :mod:`.archive`):

1. ``GetAvailableDays``: first and last day, and available days per year.
2. ``Search`` for sampled dates: one in January and one in July of each year 2021-2026
   (the 15th, or the nearest available day of that month), skipping every date on or
   after the held-out guard (:data:`lookout_capture.heldout.HELD_OUT_FROM`, 1 Jun 2026,
   D-019), so July 2026 is not sampled. From each: frame count, cadence (gaps), day/night
   counts, the ``SolarElevationAngle`` range, and which UTC reading of the local time
   stamps reproduces NPS's own Sun elevation.
3. The ``thumbmedium`` of each sampled date's midday frame (highest Sun), the ``hires`` of
   the newest sampled midday frame, and one ``hires`` per older epoch.
4. dHash distances of every midday thumbnail to the newest one, a change-point search for
   camera moves, the resolution per epoch, and a contact sheet to look at.

Everything downloaded is stored under ``archive/<viewpoint_id>/`` (never twice), searches
are cached under ``cache/``, and the summary JSON goes to
``archive/archive_check_<check_date>.json``. A request budget persisted in ``state/``
caps the whole check (default 220 requests) across separate per-viewpoint commands.

The first run, on 19 Nov 2026, predates the guard and sampled 15 Jul 2026 and
17 Nov 2026 as well: see ``docs/research/nps-archive-check.md``, "Guard breach".
"""

from __future__ import annotations

import io
import itertools
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from lookout_common.images import hamming64
from lookout_common.manifest import Viewpoint
from lookout_common.storage import Storage
from lookout_common.timeutil import isoformat_z, utcnow
from PIL import Image, ImageDraw, ImageFont

from . import CAPTURE_VERSION
from .archive import (
    ArchiveAsset,
    ArchiveClient,
    ArchiveError,
    AvailableDays,
    BudgetExhausted,
    archive_asset_key,
    backfill_assets,
    cached_search,
    pick_assets,
)
from .heldout import HELD_OUT_FROM, held_out_reason
from .sun import solar_elevation_deg

__all__ = [
    "DEFAULT_BUDGET",
    "EXTRA_DATES",
    "HRRRV4_START",
    "PAGING_PROBE_DAY",
    "SampleDate",
    "analyse_day",
    "convention_check",
    "detect_epochs",
    "merge_summary",
    "run_viewpoint_check",
    "sample_dates",
    "summary_key",
]

DEFAULT_BUDGET = 220
#: HRRR version 4 went operational on 2 Dec 2020 (PRD §8.1: "HRRRv4 from Dec 2020").
HRRRV4_START = date(2020, 12, 2)
SAMPLE_YEARS = tuple(range(2021, 2027))
SAMPLE_MONTHS = (1, 7)
TARGET_DAY = 15
#: Extra sampled dates. The 19 Nov 2026 run also sampled 2026-11-17, which is past the
#: held-out guard; nothing is added now.
EXTRA_DATES: tuple[date, ...] = ()
#: The paging probe reads metadata only (Search/Execute), which D-019 allows for any date.
PAGING_PROBE_DAY = date(2026, 11, 17)
#: Frames with NPS Sun elevation below this are left out of the time-stamp test (refraction).
CONVENTION_MIN_SUN_DEG = 5.0
#: Change-point heuristics on dHash64 bit distances; candidates are confirmed by eye.
EPOCH_MIN_CROSS_BITS = 18.0
EPOCH_MIN_MARGIN_BITS = 8.0


def summary_key(check_date: date) -> str:
    """``archive/archive_check_<YYYY-MM-DD>.json``."""
    return f"archive/archive_check_{check_date.isoformat()}.json"


def contact_sheet_key(check_date: date, viewpoint_id: str) -> str:
    """``archive/archive_check/<date>/<viewpoint_id>_contact.jpg``."""
    return f"archive/archive_check/{check_date.isoformat()}/{viewpoint_id}_contact.jpg"


@dataclass(frozen=True)
class SampleDate:
    """A wanted date, the available day chosen for it, and why."""

    label: str
    target: date
    chosen: date | None
    note: str


def _guard_note(guard: date) -> str:
    return (
        f"held out: on or after {guard.isoformat()}, the D-019 guard; archived frames are "
        "viewed only before it until scoring"
    )


def sample_dates(
    days: AvailableDays,
    *,
    years: Iterable[int] = SAMPLE_YEARS,
    months: Iterable[int] = SAMPLE_MONTHS,
    target_day: int = TARGET_DAY,
    extra: Iterable[date] = EXTRA_DATES,
    guard: date = HELD_OUT_FROM,
) -> list[SampleDate]:
    """The sampled dates: the target day of each (year, month), or the nearest available one.

    A date on or after ``guard`` (the D-019 held-out guard) is listed with ``chosen=None``
    and a note, so the check neither searches nor downloads it.
    """
    out: list[SampleDate] = []
    for year in years:
        for month in months:
            target = date(year, month, target_day)
            label = f"{year}-{month:02d}"
            if target >= guard or date(year, month, 1) >= guard:
                out.append(SampleDate(label, target, None, _guard_note(guard)))
                continue
            if target > days.max_day:
                out.append(SampleDate(label, target, None, "after MaxDay"))
                continue
            chosen = days.nearest_in_month(target)
            if chosen is not None and chosen >= guard:
                out.append(SampleDate(label, target, None, _guard_note(guard)))
                continue
            note = (
                "target day available"
                if chosen == target
                else (
                    "no day available in this month"
                    if chosen is None
                    else f"nearest available day ({(chosen - target).days:+d} d)"
                )
            )
            out.append(SampleDate(label, target, chosen, note))
    for when in extra:
        chosen = when if days.is_available(when) else days.nearest_in_month(when)
        if when >= guard or (chosen is not None and chosen >= guard):
            out.append(SampleDate(when.isoformat(), when, None, _guard_note(guard)))
            continue
        note = "target day available" if chosen == when else "nearest available day"
        out.append(SampleDate(when.isoformat(), when, chosen, note))
    return out


def _gap_minutes(times: Sequence[datetime]) -> list[float]:
    return [round((b - a).total_seconds() / 60.0, 2) for a, b in itertools.pairwise(times)]


def convention_check(
    assets: Sequence[ArchiveAsset],
    viewpoint: Viewpoint,
    *,
    min_sun_deg: float = CONVENTION_MIN_SUN_DEG,
) -> dict[str, Any]:
    """Which UTC reading of the archive's local times reproduces NPS's ``SolarElevationAngle``?

    For every frame with NPS Sun elevation >= ``min_sun_deg`` it computes the Sun elevation
    (NOAA equations, :mod:`.sun`, geometric) at the ``label`` and ``wall`` readings of
    :func:`lookout_common.timeutil.parse_nps_archive_time`, and the constant clock offset
    (-120..+120 min, 1 min steps) from the label reading that best fits NPS's values.
    """
    lat, lon = viewpoint.camera.lat, viewpoint.camera.lon
    rows = []
    for asset in assets:
        if asset.solar_elevation_deg is None or asset.solar_elevation_deg < min_sun_deg:
            continue
        try:
            label = asset.image_time_utc(viewpoint.time_zone, "label")
        except ValueError:
            continue
        try:
            wall: datetime | None = asset.image_time_utc(viewpoint.time_zone, "wall")
        except ValueError:
            wall = None
        rows.append((asset, label, wall))
    if not rows:
        return {"frames": 0}
    nps = [a.solar_elevation_deg for a, _, _ in rows]

    def mean_abs(times: Sequence[datetime | None]) -> float | None:
        diffs = [
            abs(solar_elevation_deg(t, lat, lon) - n)
            for t, n in zip(times, nps, strict=True)
            if t is not None
        ]
        return round(statistics.fmean(diffs), 3) if diffs else None

    label_times = [label for _, label, _ in rows]
    best_offset, best_err = 0, float("inf")
    for offset in range(-120, 121):
        delta = timedelta(minutes=offset)
        err = statistics.fmean(
            abs(solar_elevation_deg(t + delta, lat, lon) - n)
            for t, n in zip(label_times, nps, strict=True)
        )
        if err < best_err - 1e-9:
            best_offset, best_err = offset, err
    label_err = mean_abs(label_times)
    wall_err = mean_abs([w for _, _, w in rows])
    max_label = max(
        abs(solar_elevation_deg(t, lat, lon) - n) for t, n in zip(label_times, nps, strict=True)
    )
    return {
        "frames": len(rows),
        "zone_names": sorted({a.local_time_string.split(" ", 3)[-1] for a, _, _ in rows}),
        "mean_abs_err_deg_label": label_err,
        "max_abs_err_deg_label": round(max_label, 3),
        "mean_abs_err_deg_wall": wall_err,
        "label_and_wall_differ": any(w is not None and w != lbl for _, lbl, w in rows),
        "best_offset_min_from_label": best_offset,
        "best_offset_mean_abs_err_deg": round(best_err, 3),
    }


def analyse_day(assets: Sequence[ArchiveAsset], viewpoint: Viewpoint) -> dict[str, Any]:
    """Frame count, cadence, day/night split, Sun range and time-stamp check for one day."""
    times = []
    unparsed = 0
    for asset in assets:
        try:
            times.append(asset.image_time_utc(viewpoint.time_zone, "label"))
        except ValueError:
            unparsed += 1
    times.sort()
    gaps = _gap_minutes(times)
    gap_hist = Counter(
        "15" if abs(g - 15) < 1 else ("30" if abs(g - 30) < 1 else ("<15" if g < 15 else ">30"))
        for g in gaps
    )
    angles = [a.solar_elevation_deg for a in assets if a.solar_elevation_deg is not None]
    return {
        "frames": len(assets),
        "unparsed_times": unparsed,
        "first_local": assets[0].local_time_string if assets else None,
        "last_local": assets[-1].local_time_string if assets else None,
        "median_gap_min": statistics.median(gaps) if gaps else None,
        "gap_counts": dict(sorted(gap_hist.items())),
        "max_gap_min": max(gaps) if gaps else None,
        "time_of_day": dict(sorted(Counter(a.time_of_day or "none" for a in assets).items())),
        "frames_sun_above_0": sum(1 for a in angles if a > 0),
        "solar_elevation_min_deg": round(min(angles), 2) if angles else None,
        "solar_elevation_max_deg": round(max(angles), 2) if angles else None,
        "time_stamp_check": convention_check(assets, viewpoint),
    }


def detect_epochs(hashes: Sequence[int | None]) -> dict[str, Any]:
    """Find at most one change point in a time-ordered series of dHash64 values.

    For every split with at least two thumbnails on each side it compares the mean
    distance across the split with the mean distance within the two sides. A split is a
    *candidate* epoch boundary when the cross mean is >= EPOCH_MIN_CROSS_BITS and exceeds
    the within mean by >= EPOCH_MIN_MARGIN_BITS. Weather (snow, fog, overcast) also moves
    dHash, so every candidate is checked by eye on the contact sheet.
    """
    indexed = [(i, h) for i, h in enumerate(hashes) if h is not None]
    if len(indexed) < 4:
        return {"candidate": None, "reason": "fewer than 4 thumbnails"}

    def mean_pairs(values: Sequence[int]) -> float:
        pairs = [hamming64(a, b) for i, a in enumerate(values) for b in values[i + 1 :]]
        return statistics.fmean(pairs) if pairs else 0.0

    best: dict[str, Any] | None = None
    for k in range(2, len(indexed) - 1):
        left = [h for _, h in indexed[:k]]
        right = [h for _, h in indexed[k:]]
        within_pairs = [hamming64(a, b) for i, a in enumerate(left) for b in left[i + 1 :]]
        within_pairs += [hamming64(a, b) for i, a in enumerate(right) for b in right[i + 1 :]]
        within = statistics.fmean(within_pairs)
        cross = statistics.fmean(hamming64(a, b) for a in left for b in right)
        margin = cross - within
        if best is None or margin > best["margin_bits"]:
            best = {
                "split_before_index": indexed[k][0],
                "last_before_index": indexed[k - 1][0],
                "cross_mean_bits": round(cross, 2),
                "within_mean_bits": round(within, 2),
                "margin_bits": round(margin, 2),
            }
    assert best is not None
    is_candidate = (
        best["cross_mean_bits"] >= EPOCH_MIN_CROSS_BITS
        and best["margin_bits"] >= EPOCH_MIN_MARGIN_BITS
    )
    return {
        "candidate": best if is_candidate else None,
        "best_split": best,
        "all_pairs_mean_bits": round(mean_pairs([h for _, h in indexed]), 2),
    }


def _contact_sheet(tiles: Sequence[tuple[str, bytes | None]], *, width: int = 320) -> bytes:
    """A labelled grid of thumbnails (JPEG bytes) for checking epochs by eye."""
    cols = 4
    rows = max(1, (len(tiles) + cols - 1) // cols)
    tile_h = round(width * 9 / 16)
    label_h = 18
    sheet = Image.new("RGB", (cols * width, rows * (tile_h + label_h)), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for index, (label, data) in enumerate(tiles):
        x = (index % cols) * width
        y = (index // cols) * (tile_h + label_h)
        if data:
            try:
                thumb = Image.open(io.BytesIO(data)).convert("RGB")
                scale = min(width / thumb.width, tile_h / thumb.height)
                size = (max(1, round(thumb.width * scale)), max(1, round(thumb.height * scale)))
                thumb = thumb.resize(size, Image.Resampling.LANCZOS)
                sheet.paste(thumb, (x, y + label_h))
            except OSError:
                pass
        draw.text((x + 4, y + 3), label, fill=(240, 240, 240), font=font)
    buffer = io.BytesIO()
    sheet.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def _midday(assets: Sequence[ArchiveAsset]) -> ArchiveAsset | None:
    picked = pick_assets(assets, pick="midday")
    return picked[0] if picked else None


def _load_sidecar(storage: Storage, key: str) -> dict[str, Any] | None:
    side = key[: -len(".jpg")] + ".json"
    return storage.read_json(side, default=None)


def _download(
    storage: Storage,
    archive: ArchiveClient,
    viewpoint: Viewpoint,
    asset: ArchiveAsset,
    kind: str,
    max_bytes: int | None,
) -> dict[str, Any] | None:
    """Store one asset (no request if already stored); return its sidecar or None.

    A frame behind the held-out guard is neither requested nor read, even if a file for
    it is already on disk (the 19 Nov 2026 breach left some).
    """
    if held_out_reason(asset.image_create_datetime, asset.local_time_string, viewpoint.time_zone):
        return None
    try:
        asset.image_time_utc(viewpoint.time_zone)
    except ValueError:
        return None  # an unparseable time stamp: reported by analyse_day, not downloaded
    key = archive_asset_key(
        viewpoint.id, asset.image_time_utc(viewpoint.time_zone), asset.asset_id, kind
    )  # type: ignore[arg-type]
    if not storage.exists(key):
        result = backfill_assets(
            storage=storage,
            archive=archive,
            viewpoint=viewpoint,
            assets=[asset],
            kinds=(kind,),
            max_bytes=max_bytes,
        )  # type: ignore[arg-type]
        if result.stopped and "budget" in result.stopped:
            raise BudgetExhausted(result.stopped)
    if not storage.exists(key):
        return None
    side = _load_sidecar(storage, key) or {}
    return {"key": key, **side}


def run_viewpoint_check(
    *,
    storage: Storage,
    archive: ArchiveClient,
    viewpoint: Viewpoint,
    check_date: date,
    today: date | None = None,
    paging_probe: bool = False,
    max_bytes: int | None = 400_000_000,
) -> dict[str, Any]:
    """Run the whole check for one ARD viewpoint and return its summary (see the module doc)."""
    code = viewpoint.truth_camera.archive_code
    if code is None:
        raise ValueError(f"{viewpoint.id} has no NPS archive")
    today = today or utcnow().date()
    started = utcnow()
    out: dict[str, Any] = {
        "viewpoint_id": viewpoint.id,
        "archive_code": code,
        "time_zone": viewpoint.time_zone,
        "started_at": isoformat_z(started, "seconds"),
        "capture_version": CAPTURE_VERSION,
        "notes": [],
    }

    # 1. availability ------------------------------------------------------------------
    days_cache = f"cache/nps-archive/days/{code}-{check_date:%Y%m%d}.json"
    raw_days = storage.read_json(days_cache, default=None)
    if raw_days is None:
        days, raw_days = archive.available_days(code)
        storage.write_json(days_cache, raw_days)
    else:
        days = AvailableDays.from_json(code, raw_days)
    out["min_day"] = days.min_day.isoformat()
    out["max_day"] = days.max_day.isoformat()
    out["days_per_year"] = {str(y): days.days_in_year(y) for y in range(2020, 2027)}
    out["days_since_hrrrv4"] = sum(
        1 for (y, m) in days.masks for d in days.days_in_month(y, m) if d >= HRRRV4_START
    )
    out["calendar_days_since_hrrrv4"] = (days.max_day - HRRRV4_START).days + 1

    # 2. sampled dates -------------------------------------------------------------------
    samples = sample_dates(days)
    sample_rows: list[dict[str, Any]] = []
    middays: list[tuple[SampleDate, ArchiveAsset | None]] = []
    for sample in samples:
        row: dict[str, Any] = {
            "label": sample.label,
            "target": sample.target.isoformat(),
            "date": sample.chosen.isoformat() if sample.chosen else None,
            "note": sample.note,
        }
        if sample.chosen is None:
            sample_rows.append(row)
            middays.append((sample, None))
            continue
        try:
            assets, meta = cached_search(storage, archive, code, sample.chosen, today=today)
        except BudgetExhausted:
            out["notes"].append(f"budget exhausted before searching {sample.chosen}")
            sample_rows.append(row)
            middays.append((sample, None))
            break
        except ArchiveError as exc:
            row["error"] = str(exc)[:200]
            sample_rows.append(row)
            middays.append((sample, None))
            continue
        row["search"] = {
            k: meta.get(k)
            for k in (
                "result_count",
                "page_count",
                "pages_fetched",
                "page_size",
                "from_cache",
                "paging_error",
                "complete",
            )
        }
        row.update(analyse_day(assets, viewpoint))
        midday = _midday(assets)
        if midday is not None:
            row["midday"] = {
                "asset_id": midday.asset_id,
                "local_time_string": midday.local_time_string,
                "solar_elevation_deg": midday.solar_elevation_deg,
            }
        sample_rows.append(row)
        middays.append((sample, midday))

    # 3. midday thumbnails, hires of the newest and oldest sample -------------------------
    budget_hit = False
    for (_sample, midday), row in zip(middays, sample_rows, strict=True):
        if midday is None or budget_hit:
            continue
        try:
            side = _download(storage, archive, viewpoint, midday, "thumbmedium", max_bytes)
        except BudgetExhausted:
            budget_hit = True
            out["notes"].append("budget exhausted during thumbnails")
            continue
        if side:
            row["midday"]["thumb"] = {
                k: side.get(k)
                for k in (
                    "key",
                    "original_width",
                    "original_height",
                    "original_bytes",
                    "dhash64",
                    "luma_mean",
                    "chroma_spread",
                )
            }
    with_midday = [(i, m) for i, (_s, m) in enumerate(middays) if m is not None]
    hires_targets: list[int] = []
    if with_midday:
        # The newest sampled midday: the resolution of the current epoch. Older epochs get
        # theirs below, one hires each, once the thumbnails show where they change.
        newest = max(with_midday, key=lambda im: middays[im[0]][0].chosen)  # type: ignore[arg-type,return-value]
        hires_targets = [newest[0]]
    for index in hires_targets:
        if budget_hit:
            break
        _fetch_hires(
            storage, archive, viewpoint, middays[index][1], sample_rows[index], max_bytes, out
        )
        budget_hit = any("budget" in n for n in out["notes"])

    # 4. view comparison and epochs -------------------------------------------------------
    order = sorted(
        (i for i, (s, m) in enumerate(middays) if m is not None and s.chosen is not None),
        key=lambda i: middays[i][0].chosen,  # type: ignore[arg-type,return-value]
    )
    hashes = [_thumb_hash(sample_rows[i]) for i in order]
    newest_hash = hashes[-1] if hashes else None
    for i, h in zip(order, hashes, strict=True):
        if h is not None and newest_hash is not None:
            sample_rows[i]["midday"]["dhash_bits_to_newest"] = hamming64(h, newest_hash)
    for prev, cur in itertools.pairwise(order):
        a, b = _thumb_hash(sample_rows[prev]), _thumb_hash(sample_rows[cur])
        if a is not None and b is not None:
            sample_rows[cur]["midday"]["dhash_bits_to_previous_sample"] = hamming64(a, b)
    epochs = detect_epochs(hashes)
    size_changes = []
    previous_size: tuple[int, int] | None = None
    for i in order:
        thumb = sample_rows[i]["midday"].get("thumb") or {}
        size = (thumb.get("original_width"), thumb.get("original_height"))
        if None in size:
            continue
        if previous_size is not None and size != previous_size:
            size_changes.append(
                {"date": sample_rows[i]["date"], "from": list(previous_size), "to": list(size)}
            )
        previous_size = size  # type: ignore[assignment]
    for split in (epochs.get("best_split"), epochs.get("candidate")):
        if split is not None:
            split["first_date_after_split"] = sample_rows[order[split["split_before_index"]]][
                "date"
            ]
            split["last_date_before_split"] = sample_rows[order[split["last_before_index"]]]["date"]
    # One hires per older epoch: the last sample before each thumbnail size change. A dHash
    # candidate alone is not an epoch (weather moves dHash too); it is reported for review.
    by_date = {sample_rows[i]["date"]: i for i in order}
    for change in size_changes:
        position = order.index(by_date[change["date"]])
        if position == 0 or budget_hit:
            continue
        before = order[position - 1]
        _fetch_hires(
            storage, archive, viewpoint, middays[before][1], sample_rows[before], max_bytes, out
        )
        budget_hit = any("budget" in n for n in out["notes"])
    out["epochs"] = {"dhash_change_point": epochs, "thumb_size_changes_at": size_changes}
    out["hires_resolutions"] = sorted(
        {
            (
                row["date"],
                row["midday"]["hires"]["original_width"],
                row["midday"]["hires"]["original_height"],
            )
            for row in sample_rows
            if row.get("midday", {}).get("hires")
        }
    )
    out["samples"] = sample_rows

    # 5. paging probe: metadata of one recent day, 50 per page (two requests) ---------------
    if paging_probe and not budget_hit:
        probe_day = PAGING_PROBE_DAY
        try:
            probe, _pages = archive.search_day(code, probe_day, page_size=50)
            out["paging_probe"] = {
                "day": probe_day.isoformat(),
                "page_size": 50,
                "result_count": probe.result_count,
                "page_count": probe.page_count,
                "pages_fetched": probe.pages_fetched,
                "assets_received": len(probe.assets),
                "distinct_assets": len({a.asset_id for a in probe.assets}),
                "paging_error": probe.paging_error,
            }
        except (ArchiveError, BudgetExhausted) as exc:
            out["paging_probe"] = {"day": probe_day.isoformat(), "error": str(exc)[:200]}

    # 6. contact sheet --------------------------------------------------------------------
    tiles: list[tuple[str, bytes | None]] = []
    for i in order:
        thumb = (sample_rows[i]["midday"].get("thumb") or {}).get("key")
        bits = sample_rows[i]["midday"].get("dhash_bits_to_newest")
        label = (
            f"{sample_rows[i]['date']}  d={bits}" if bits is not None else sample_rows[i]["date"]
        )
        tiles.append((label, storage.get_bytes(thumb) if thumb and storage.exists(thumb) else None))
    if tiles:
        key = contact_sheet_key(check_date, viewpoint.id)
        storage.put_bytes(key, _contact_sheet(tiles), content_type="image/jpeg")
        out["contact_sheet_key"] = key
    out["finished_at"] = isoformat_z(utcnow(), "seconds")
    out["requests_this_run"] = archive.stats.to_dict()
    return out


def _thumb_hash(row: Mapping[str, Any]) -> int | None:
    value = ((row.get("midday") or {}).get("thumb") or {}).get("dhash64")
    return int(value, 16) if value else None


def _fetch_hires(
    storage: Storage,
    archive: ArchiveClient,
    viewpoint: Viewpoint,
    asset: ArchiveAsset | None,
    row: dict[str, Any],
    max_bytes: int | None,
    out: dict[str, Any],
) -> None:
    if asset is None or row.get("midday", {}).get("hires"):
        return
    try:
        side = _download(storage, archive, viewpoint, asset, "hires", max_bytes)
    except BudgetExhausted:
        out["notes"].append("budget exhausted during hires downloads")
        return
    if not side:
        return
    exif = side.get("exif") or {}
    entry = {
        k: side.get(k)
        for k in ("key", "original_width", "original_height", "original_bytes", "dhash64", "sha256")
    }
    entry["exif"] = {
        k: exif.get(k)
        for k in ("DateTime", "DateTimeOriginal", "Software", "Artist", "Make", "Model")
    }
    exif_time = exif.get("DateTimeOriginal") or exif.get("DateTime")
    if exif_time:
        try:
            parsed = datetime.strptime(exif_time.strip()[:19], "%Y:%m:%d %H:%M:%S")
            created = datetime.strptime(asset.image_create_datetime, "%m/%d/%Y %I:%M:%S %p")
            entry["exif_minus_image_create_min"] = round(
                (parsed - created).total_seconds() / 60.0, 2
            )
        except ValueError:
            pass
    row["midday"]["hires"] = entry


def merge_summary(
    storage: Storage,
    check_date: date,
    *,
    viewpoint: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
    run: Mapping[str, Any] | None = None,
    budget_used: int | None = None,
    budget_limit: int | None = None,
) -> dict[str, Any]:
    """Fold one viewpoint's result (and/or extra sections) into the summary JSON, under a lock."""
    key = summary_key(check_date)
    with storage.lock(f"archive-check-{check_date.isoformat()}", timeout_s=60.0):
        doc = storage.read_json(key, default=None) or {
            "check_date": check_date.isoformat(),
            "method": {
                "sampled_dates": "15th of January and July 2021-2026 (nearest available day of "
                f"the month if missing), only before the D-019 guard {HELD_OUT_FROM.isoformat()}",
                "midday": "frame with the highest NPS SolarElevationAngle on the date",
                "downloads": "thumbmedium of each midday frame; hires of the newest sampled "
                "midday and of the last sample before each thumbnail-size change",
                "spacing": ">= 5 s end-to-start between nps.gov archive requests, one at a time",
                "time_stamp_check": "NOAA solar elevation at the label/wall UTC readings vs NPS "
                "SolarElevationAngle, frames with Sun >= 5 deg",
                "epochs": "dHash64 change point on midday thumbnails, confirmed by eye",
            },
            "viewpoints": {},
            "runs": [],
        }
        if viewpoint is not None:
            doc["viewpoints"][viewpoint["viewpoint_id"]] = dict(viewpoint)
            doc["runs"].append(
                {
                    "viewpoint_id": viewpoint["viewpoint_id"],
                    "finished_at": viewpoint.get("finished_at"),
                    "requests": viewpoint.get("requests_this_run"),
                }
            )
        if run is not None:
            doc["runs"].append(dict(run))
        if extra:
            doc.update(extra)
        totals = Counter()
        for run in doc["runs"]:
            stats = run.get("requests") or {}
            totals["requests"] += int(stats.get("requests", 0))
            totals["bytes"] += int(stats.get("bytes", 0))
            totals["errors"] += int(stats.get("errors", 0))
        doc["request_totals_from_runs"] = dict(totals)
        if budget_used is not None:
            doc["budget"] = {"limit": budget_limit, "used": budget_used}
        doc["updated_at"] = isoformat_z(datetime.now(UTC), "seconds")
        storage.write_json(key, doc)
        return doc
