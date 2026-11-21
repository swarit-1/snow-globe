"""The archive check end to end against a fake archive, plus its pieces."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
from snow_globe_common.http import MinIntervalLimiter
from snow_globe_common.manifest import load_manifests
from snow_globe_common.storage import LocalStorage

from snow_globe_capture.archive import ArchiveAsset, ArchiveClient, AvailableDays, RequestBudget
from snow_globe_capture.archive_check import (
    convention_check,
    detect_epochs,
    merge_summary,
    run_viewpoint_check,
    sample_dates,
    summary_key,
)
from snow_globe_capture.heldout import HELD_OUT_FROM
from snow_globe_capture.sun import solar_elevation_deg

from .conftest import FakeClock, FakeNPS, fixture_json, make_jpeg

VIEWPOINTS = load_manifests()
PORE = VIEWPOINTS["point-reyes.beach"]


def _us(d: date) -> str:
    return f"{d.month}/{d.day}/{d.year}"


def _clock12(hour: int, minute: int) -> tuple[str, str]:
    suffix = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    return f"{h12}:{minute:02d}:00 {suffix}", f"{h12}:{minute:02d} {suffix}"


def _day_assets(
    day: date, *, zone_label: str, utc_offset_h: int, lat: float, lon: float, step_min: int = 15
) -> list[dict]:
    """Synthetic Search results for one local day with NPS-like Sun angles."""
    results = []
    for n, minutes in enumerate(range(0, 24 * 60, step_min)):
        hour, minute = divmod(minutes, 60)
        create, label = _clock12(hour, minute)
        local = datetime(day.year, day.month, day.day, hour, minute)
        utc = (local - timedelta(hours=utc_offset_h)).replace(tzinfo=UTC)
        angle = solar_elevation_deg(utc, lat, lon)
        results.append(
            {
                "ItemNumber": n + 1,
                "Asset": {
                    "AssetID": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{day}-{minutes}")),
                    "LocationCode": "pore",
                    "ImageCreateDateTime": f"{_us(day)} {create}",
                    "LocalTimeString": f"{_us(day)} {label} {zone_label}",
                    "SolarElevationAngle": angle,
                    "TimeOfDay": "day" if angle > 0 else "night",
                },
            }
        )
    return results


def test_sample_dates_pick_15th_or_nearest_and_respect_the_guard():
    days = AvailableDays.from_json("pore", fixture_json("days_pore.json"))
    samples = sample_dates(days)
    labels = [s.label for s in samples]
    assert labels[:2] == ["2021-01", "2021-07"] and labels[-1] == "2026-07"
    assert len(samples) == 12  # Jan and Jul 2021-2026; no 2026-11-17 extra any more
    jan21 = samples[0]
    assert jan21.chosen == date(2021, 1, 15)
    assert all(s.chosen is None or days.is_available(s.chosen) for s in samples)
    # D-019: nothing on or after 1 Jun 2026 is chosen; July 2026 is listed as held out.
    assert all(s.chosen is None or s.chosen < HELD_OUT_FROM for s in samples)
    jul26 = samples[-1]
    assert jul26.chosen is None and "held out" in jul26.note
    # An extra date past the guard is refused too, even when the archive has it.
    extra = sample_dates(days, years=(), extra=(date(2026, 11, 17), date(2026, 5, 31)))
    assert [s.chosen for s in extra] == [None, date(2026, 5, 31)]
    # A month that straddles the guard never yields a chosen day after it: only 16 May is
    # available, the target is the 15th, the guard is the 16th.
    straddle = AvailableDays("pore", date(2026, 1, 1), date(2026, 12, 31), {(2026, 5): 1 << 15})
    picked = sample_dates(straddle, years=(2026,), months=(5,), guard=date(2026, 5, 16))
    assert picked[0].chosen is None and "held out" in picked[0].note
    assert sample_dates(straddle, years=(2026,), months=(5,))[0].chosen == date(2026, 5, 16)


def test_convention_check_finds_a_one_hour_label_error():
    lat, lon = PORE.camera.lat, PORE.camera.lon
    honest = [
        ArchiveAsset.from_result(r)
        for r in _day_assets(
            date(2024, 7, 15), zone_label="Pacific Daylight Time", utc_offset_h=-7, lat=lat, lon=lon
        )
    ]
    report = convention_check(honest, PORE)
    assert report["best_offset_min_from_label"] == 0
    assert report["mean_abs_err_deg_label"] < 0.05
    # Labelled "Standard" all year while the clock keeps daylight time: label reads 1 h late.
    fixed_name = [
        ArchiveAsset.from_result(r)
        for r in _day_assets(
            date(2024, 7, 15), zone_label="Pacific Standard Time", utc_offset_h=-7, lat=lat, lon=lon
        )
    ]
    report = convention_check(fixed_name, PORE)
    assert report["best_offset_min_from_label"] == -60
    assert report["mean_abs_err_deg_wall"] < 0.05 < 5 < report["mean_abs_err_deg_label"]


def test_detect_epochs_finds_a_clear_split_and_ignores_noise():
    a, b = 0x0F0F0F0F0F0F0F0F, 0xF0F0F0F0F0F0F0F0
    series = [a, a ^ 1, a ^ 3, b, b ^ 1, b ^ 7]
    result = detect_epochs(series)
    assert result["candidate"]["split_before_index"] == 3
    assert result["candidate"]["last_before_index"] == 2
    noisy = [a, a ^ 1, a ^ 2, a ^ 4, a ^ 8, a ^ 16]
    assert detect_epochs(noisy)["candidate"] is None


def test_viewpoint_check_end_to_end(tmp_path: Path, data_root: Path):
    storage = LocalStorage(data_root)
    clock = FakeClock(datetime(2026, 11, 19, 9, 0, tzinfo=UTC))
    fake = FakeNPS(clock)
    days = fixture_json("days_pore.json")
    fake.days["pore"] = days
    parsed = AvailableDays.from_json("pore", days)
    # The fake archive also serves the two post-guard dates the 19 Nov run sampled; the
    # check must never ask for them.
    wanted = [s.chosen for s in sample_dates(parsed) if s.chosen is not None]
    guarded_ids: set[str] = set()
    for day in [*wanted, date(2026, 7, 15), date(2026, 11, 17)]:
        dst = day.month == 7
        results = _day_assets(
            day,
            zone_label="Pacific Daylight Time" if dst else "Pacific Standard Time",
            utc_offset_h=-7 if dst else -8,
            lat=PORE.camera.lat,
            lon=PORE.camera.lon,
        )
        fake.search_pages[("pore", f"{day:%m/%d/%Y}")] = [
            {
                "Page": 1,
                "PageCount": 1,
                "ResultCount": len(results),
                "SearchID": "s",
                "Results": results,
            }
        ]
        for r in results:
            asset_id = r["Asset"]["AssetID"]
            if day >= HELD_OUT_FROM:
                guarded_ids.add(asset_id)
            pattern = day.month  # "weather" differs a little between samples
            ramp = "up" if day.year < 2023 else "down"  # a "camera move" in 2023
            old = day.year < 2023  # 3:2 camera before 2023, 16:9 after
            fake.assets[f"{asset_id}:thumbmedium"] = make_jpeg(
                99 if old else 100, 66 if old else 56, pattern=pattern, ramp=ramp
            )
            fake.assets[f"{asset_id}:hires"] = make_jpeg(
                999,
                666 if old else 562,
                pattern=pattern,
                ramp=ramp,
            )
    limiter = MinIntervalLimiter(
        "nps-archive",
        5.0,
        state_dir=tmp_path / "rl",
        mode="file",
        clock=clock.time,
        sleep=clock.sleep,
    )
    budget = RequestBudget(storage, "check-test", 60)
    archive = ArchiveClient(
        client=httpx.Client(transport=fake.transport()),
        limiter=limiter,
        storage=storage,
        budget=budget,
        clock=clock.now,
        monotonic=clock.monotonic,
    )
    out = run_viewpoint_check(
        storage=storage,
        archive=archive,
        viewpoint=PORE,
        check_date=date(2026, 11, 19),
        today=date(2026, 11, 19),
    )
    assert out["min_day"] == "2004-01-06" and out["days_per_year"]["2024"] == 360
    rows = out["samples"]
    assert len(rows) == 12 and all(r.get("frames") == 96 for r in rows if r["date"])
    assert rows[-1]["label"] == "2026-07" and rows[-1]["date"] is None
    assert "held out" in rows[-1]["note"]
    assert rows[0]["median_gap_min"] == 15.0 and rows[0]["gap_counts"] == {"15": 95}
    july = rows[1]
    assert july["time_stamp_check"]["best_offset_min_from_label"] == 0
    assert all("thumb" in r["midday"] for r in rows if r["date"])
    cand = out["epochs"]["dhash_change_point"]["candidate"]
    assert cand is not None and cand["first_date_after_split"].startswith("2023")
    # One hires for the current epoch (newest: Jan 2026) and one for the older epoch.
    assert [(d[:4], w, h) for d, w, h in out["hires_resolutions"]] == [
        ("2022", 999, 666),
        ("2026", 999, 562),
    ]
    assert max(d for d, _w, _h in out["hires_resolutions"]) < HELD_OUT_FROM.isoformat()
    assert out["epochs"]["thumb_size_changes_at"] == [
        {"date": "2023-01-16", "from": [99, 66], "to": [100, 56]}
    ]
    # Requests: 1 days + 11 searches + 11 thumbs + 2 hires (newest, and before the change).
    assert archive.stats.by_kind["days"] == 1 and archive.stats.by_kind["search"] == 11
    assert archive.stats.by_kind["thumbmedium"] == 11 and archive.stats.by_kind["hires"] == 2
    assert budget.used() == archive.stats.requests
    # The guard: no post-guard asset was requested, and nothing past it is on disk.
    assert not [r for r in fake.requests if any(i in r["url"] for i in guarded_ids)]
    stored_days = {k.split("/")[2] for k in storage.list_prefix(f"archive/{PORE.id}/")}
    assert stored_days and max(stored_days) < HELD_OUT_FROM.isoformat()
    assert storage.exists(out["contact_sheet_key"])
    gaps = [
        (b["t"] - a["t"]).total_seconds()
        for a, b in zip(fake.requests, fake.requests[1:], strict=False)
    ]
    assert min(gaps) >= 5.0
    # A re-run is free: everything comes from the caches and the stored files.
    before = len(fake.requests)
    run_viewpoint_check(
        storage=storage,
        archive=archive,
        viewpoint=PORE,
        check_date=date(2026, 11, 19),
        today=date(2026, 11, 19),
    )
    assert len(fake.requests) == before
    doc = merge_summary(
        storage, date(2026, 11, 19), viewpoint=out, budget_used=budget.used(), budget_limit=60
    )
    assert storage.read_json(summary_key(date(2026, 11, 19)))["viewpoints"][PORE.id]
    assert doc["request_totals_from_runs"]["requests"] == archive.stats.requests
