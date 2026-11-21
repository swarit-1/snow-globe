"""NPS archive client and backfill: parsing, paging, politeness, resume, size cap, budget."""

from __future__ import annotations

import copy
import itertools
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from snow_globe_common.http import MinIntervalLimiter
from snow_globe_common.manifest import load_manifests
from snow_globe_common.storage import LocalStorage

from snow_globe_capture.archive import (
    MAX_TOTAL_ATTEMPTS,
    ArchiveAsset,
    ArchiveBadBody,
    ArchiveClient,
    ArchiveHTTPError,
    AvailableDays,
    BudgetExhausted,
    RequestBudget,
    api_code,
    archive_asset_key,
    archive_limiter,
    archive_state_key,
    backfill_assets,
    cached_search,
    default_archive_client,
    pick_assets,
    search_body,
    search_cache_key,
)
from snow_globe_capture.heldout import (
    HeldOutOverride,
    asset_dates,
    asset_local_dates,
    asset_utc_dates,
    held_out_reason,
)

from .conftest import FakeClock, FakeNPS, fixture_json, make_jpeg

VIEWPOINTS = load_manifests()
PORE = VIEWPOINTS["point-reyes.beach"]
GRCA = VIEWPOINTS["grand-canyon.yavapai"]


def _assets() -> list[ArchiveAsset]:
    return [
        ArchiveAsset.from_result(r) for r in fixture_json("search_pore_20261117.json")["Results"]
    ]


def _pre_guard_assets() -> list[ArchiveAsset]:
    """The 17 Nov 2026 fixture moved to 17 Nov 2025 (also PST), before the D-019 guard."""
    out = []
    for result in fixture_json("search_pore_20261117.json")["Results"]:
        asset = dict(result["Asset"])
        for name in ("ImageCreateDateTime", "LocalTimeString"):
            asset[name] = asset[name].replace("/2026", "/2025")
        out.append(ArchiveAsset.from_result({"Asset": asset}))
    return out


def _client(fake: FakeNPS, clock: FakeClock, tmp_path: Path, storage=None, budget=None):
    limiter = MinIntervalLimiter(
        "nps-archive",
        5.0,
        state_dir=tmp_path / "state",
        mode="file",
        clock=clock.time,
        sleep=clock.sleep,
    )
    client = httpx.Client(transport=fake.transport())
    return ArchiveClient(
        client=client,
        limiter=limiter,
        storage=storage,
        budget=budget,
        clock=clock.now,
        monotonic=clock.monotonic,
    )


def _paged(results: list[dict], page_size: int) -> list[dict]:
    pages = []
    count = (len(results) + page_size - 1) // page_size
    for n in range(count):
        pages.append(
            {
                "Page": n + 1,
                "PageSize": page_size,
                "PageCount": count,
                "ResultCount": len(results),
                "SearchID": "abc-123",
                "Results": results[n * page_size : (n + 1) * page_size],
            }
        )
    return pages


# --- availability --------------------------------------------------------------------------


def test_available_days_decodes_month_bitmasks():
    days = AvailableDays.from_json("pore", fixture_json("days_pore.json"))
    assert days.min_day == date(2004, 1, 6) and days.max_day == date(2026, 11, 18)
    # 2026-11 mask is 262143 = 2**18 - 1: days 1..18 available.
    assert days.days_in_month(2026, 11)[-1] == date(2026, 11, 18)
    assert days.is_available(date(2026, 11, 18)) and not days.is_available(date(2026, 11, 19))
    assert days.days_in_year(2024) == 360 and days.days_in_year(2026) == 309
    assert not days.is_available(date(2004, 1, 5)) and days.is_available(date(2004, 1, 6))
    # January 2021 mask 16777215 = days 1..24; the 25th is missing, the 24th is nearest.
    assert days.nearest_in_month(date(2021, 1, 28)) == date(2021, 1, 24)
    assert days.nearest_in_month(date(2003, 1, 15)) is None


def test_api_code_uses_the_archive_spelling():
    assert api_code("acad") == "ACAD" and api_code("bibe") == "BIBE" and api_code("pore") == "pore"


def test_search_body_matches_the_page_script():
    body = search_body("pore", date(2021, 1, 5))
    assert body["Operand"]["RightOperand"] == {
        "CompareType": "=",
        "Term": "ImageCreateDate",
        "Attribute": "01/05/2021",
    }
    assert body["PageSize"] == 300 and body["SortTerms"][0]["Ascending"] is True


# --- assets and times ----------------------------------------------------------------------


def test_asset_times_parse_to_utc():
    assets = _assets()
    first = assets[0]
    assert first.image_create_datetime == "11/17/2026 12:00:00 AM"
    assert first.image_time_utc(PORE.time_zone) == datetime(2026, 11, 17, 8, 0, tzinfo=UTC)
    assert assets[-1].image_time_utc(PORE.time_zone) == datetime(2026, 11, 18, 7, 45, tzinfo=UTC)


def test_pick_assets_midday_hourly_and_sun_filter():
    assets = _assets()
    midday = pick_assets(assets, pick="midday")
    assert len(midday) == 1
    assert midday[0].solar_elevation_deg == max(a.solar_elevation_deg for a in assets)
    daytime = pick_assets(assets, min_sun_deg=0.0)
    assert daytime and all(a.solar_elevation_deg >= 0 for a in daytime)
    hourly = pick_assets(assets, pick="hourly")
    assert 20 <= len(hourly) <= 25
    assert all(
        a.image_create_datetime.split()[1].endswith((":00:00", ":15:00", ":45:00")) for a in hourly
    )


def test_hourly_pick_leaves_a_frame_after_2330_to_the_next_day():
    """Backfill plan review (20 Nov 2026): a frame after 23:30 is nearest the *next* day's
    00:00, so it is none of this day's hours. It used to be filed under this day's 00:00 and,
    with that day's own 00:00 and 00:15 frames missing, stood in for them 23 h away."""

    def at(clock: str, i: int) -> ArchiveAsset:
        return ArchiveAsset(
            asset_id=f"asset-{i}",
            location_code="pore",
            image_create_datetime=f"05/30/2026 {clock}",
            local_time_string="",
            solar_elevation_deg=None,
            time_of_day=None,
        )

    clocks = ["12:45:00 AM", "1:00:00 AM", "10:45:00 PM", "11:00:00 PM", "11:30:00 PM"]
    day = [at(c, i) for i, c in enumerate([*clocks, "11:45:00 PM"])]
    picked = [a.image_create_datetime.split(" ", 1)[1] for a in pick_assets(day, pick="hourly")]
    assert picked == ["1:00:00 AM", "11:00:00 PM"]
    # 23:30 itself is still hour 23's (30 min either way: the earlier hour, as before).
    late = [at("11:30:00 PM", 0), at("11:45:00 PM", 1)]
    assert [a.asset_id for a in pick_assets(late, pick="hourly")] == ["asset-0"]


def test_archive_keys_differ_for_thumbs_and_hires():
    when = datetime(2026, 11, 17, 20, 0, tzinfo=UTC)
    hires = archive_asset_key(PORE.id, when, "41af76bd-a3c7-4c0e-9888-750c6c041ff7", "hires")
    thumb = archive_asset_key(PORE.id, when, "41af76bd-a3c7-4c0e-9888-750c6c041ff7", "thumbmedium")
    assert hires == "archive/point-reyes.beach/2026-11-17/20261117T200000Z_41af76bd.jpg"
    assert thumb == "archive/point-reyes.beach/2026-11-17/20261117T200000Z_41af76bd_thumbmedium.jpg"


# --- paging and politeness ----------------------------------------------------------------


def test_search_follows_paging(fake_nps: FakeNPS, clock: FakeClock, tmp_path: Path):
    results = fixture_json("search_pore_20261117.json")["Results"]
    fake_nps.search_pages[("pore", "11/17/2026")] = _paged(results, 50)
    archive = _client(fake_nps, clock, tmp_path)
    found, _pages = archive.search_day("pore", date(2026, 11, 17), page_size=50)
    assert len(found.assets) == 88 and found.pages_fetched == 2 and found.page_count == 2
    assert len({a.asset_id for a in found.assets}) == 88
    assert all(r["url"].endswith("/api/Search/Execute") for r in fake_nps.requests)
    assert archive.stats.requests == 2 and found.paging_error is None


def test_failed_page_keeps_page_one_and_is_not_cached(fake_nps, clock, tmp_path, data_root):
    storage = LocalStorage(data_root)
    results = fixture_json("search_pore_20261117.json")["Results"]
    fake_nps.search_pages[("pore", "11/17/2026")] = _paged(results, 50)
    original = fake_nps.handler

    def page_two_fails(request):
        if request.method == "POST" and b'"Page": 2' in request.content.replace(
            b'"Page":2', b'"Page": 2'
        ):
            fake_nps.requests.append({"method": "POST", "url": str(request.url), "t": clock.now()})
            return httpx.Response(500, text="<!doctype html>")
        return original(request)

    fake_nps.handler = page_two_fails  # type: ignore[method-assign]
    archive = _client(fake_nps, clock, tmp_path, storage=storage)
    found, _ = archive.search_day("pore", date(2026, 11, 17), page_size=50)
    assert len(found.assets) == 50 and "page 2" in (found.paging_error or "")
    assets, meta = cached_search(
        storage, archive, "pore", date(2026, 11, 17), today=date(2026, 11, 19), page_size=50
    )
    assert meta["complete"] is False and len(assets) == 50
    assert not storage.exists("cache/nps-archive/search/pore/2026-11-17.json")


def test_search_stops_if_server_ignores_the_page(fake_nps, clock, tmp_path):
    results = fixture_json("search_pore_20261117.json")["Results"]
    pages = _paged(results, 50)
    fake_nps.search_pages[("pore", "11/17/2026")] = [pages[0], pages[0]]  # page 2 repeats page 1
    archive = _client(fake_nps, clock, tmp_path)
    found, _ = archive.search_day("pore", date(2026, 11, 17), page_size=50)
    assert len(found.assets) == 50 and found.pages_fetched == 2


def test_requests_are_at_least_5_s_apart_across_clients(fake_nps, clock, tmp_path):
    fake_nps.days["pore"] = fixture_json("days_pore.json")
    one = _client(fake_nps, clock, tmp_path)
    two = _client(fake_nps, clock, tmp_path)  # a second "process" sharing the limiter file
    for archive in (one, two, one, two, one):
        archive.available_days("pore")
    times = [r["t"] for r in fake_nps.requests]
    gaps = [(b - a).total_seconds() for a, b in itertools.pairwise(times)]
    assert len(times) == 5 and min(gaps) >= 5.0
    assert one.stats.waited_s >= 10.0


def test_archive_records_name_their_runner_as_live_records_do(fake_nps, clock, tmp_path, data_root):
    """Lead's request (20 Nov 2026): ``archive_request`` records (worker "archive") carry the
    ``runner`` field live ``request`` records have: ``cli-loop`` for the queue inside the loop,
    ``cli-archive`` and ``cli-archive-check`` for the standalone commands, ``cli`` by default."""
    from snow_globe_capture.archive_queue import _queue_client

    storage = LocalStorage(data_root)
    fake_nps.days["pore"] = fixture_json("days_pore.json")
    archive = _client(fake_nps, clock, tmp_path, storage=storage)
    assert archive.runner == "cli"
    archive.runner = "cli-archive"
    archive.available_days("pore")
    lines = [
        json.loads(line)
        for path in (data_root / "capture_log").glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    (record,) = [r for r in lines if r["event"] == "archive_request"]
    assert record["worker"] == "archive" and record["runner"] == "cli-archive"
    assert _queue_client(storage).runner == "cli-loop"
    assert default_archive_client(storage, runner="cli-archive-check").runner == (
        "cli-archive-check"
    )


def test_503_defers_every_client_and_counts_the_error(fake_nps, clock, tmp_path):
    fake_nps.days["pore"] = fixture_json("days_pore.json")
    fake_nps.fail_next["GetAvailableDays"] = 1
    archive = _client(fake_nps, clock, tmp_path)
    with pytest.raises(ArchiveHTTPError) as caught:
        archive.available_days("pore")
    assert caught.value.status_code == 503
    assert archive.limiter.next_allowed_in() >= 119.0  # Retry-After: 120
    assert archive.stats.errors == 1
    before = clock.now()
    archive.available_days("pore")
    assert (fake_nps.requests[-1]["t"] - before).total_seconds() >= 119.0


def test_budget_is_persisted_and_stops_requests(fake_nps, clock, tmp_path, data_root):
    storage = LocalStorage(data_root)
    fake_nps.days["pore"] = fixture_json("days_pore.json")
    budget = RequestBudget(storage, "test", 2)
    archive = _client(fake_nps, clock, tmp_path, storage=storage, budget=budget)
    archive.available_days("pore")
    archive.available_days("pore")
    again = RequestBudget(storage, "test", 2)  # a restart reads the same count
    archive.budget = again
    with pytest.raises(BudgetExhausted):
        archive.available_days("pore")
    assert fake_nps.count("GET", "GetAvailableDays") == 2 and again.used() == 2


# --- backfill: resume, never twice, size cap, dry run ----------------------------------------


def _serve_assets(fake: FakeNPS, assets: list[ArchiveAsset], *, hires_width: int = 3840) -> None:
    for i, asset in enumerate(assets):
        fake.assets[f"{asset.asset_id}:thumbmedium"] = make_jpeg(320, 180, pattern=i)
        fake.assets[f"{asset.asset_id}:hires"] = make_jpeg(
            hires_width,
            hires_width * 9 // 16,
            pattern=i,
            exif_datetime="2026:11:17 12:00:00",
            artist="National Park Service",
            software="ARS_Digicam",
        )


def test_backfill_never_downloads_twice_and_resumes(fake_nps, clock, tmp_path, data_root):
    storage = LocalStorage(data_root)
    assets = pick_assets(_pre_guard_assets(), min_sun_deg=20.0)
    _serve_assets(fake_nps, assets)
    archive = _client(fake_nps, clock, tmp_path, storage=storage)
    first = backfill_assets(storage=storage, archive=archive, viewpoint=PORE, assets=assets[:3])
    assert first.downloaded == 3 and fake_nps.count("GET", "GetAsset") == 3
    # A second run over a longer list fetches only the new ones.
    second = backfill_assets(storage=storage, archive=archive, viewpoint=PORE, assets=assets[:5])
    assert second.downloaded == 2 and second.skipped_existing == 3
    assert fake_nps.count("GET", "GetAsset") == 5
    # State lost: the stored files alone still prevent a re-download.
    storage.path("state/archive/point-reyes.beach.json").unlink()
    third = backfill_assets(storage=storage, archive=archive, viewpoint=PORE, assets=assets[:5])
    assert third.downloaded == 0 and fake_nps.count("GET", "GetAsset") == 5
    key = first.stored[0]["key"]
    side = storage.read_json(key[:-4] + ".json")
    assert side["asset_id"] == assets[0].asset_id and side["source"] == "nps-archive"
    assert side["image_create_datetime_raw"] == assets[0].image_create_datetime
    assert side["solar_elevation_deg"] == assets[0].solar_elevation_deg
    assert side["image_time_utc"].endswith("Z") and len(side["dhash64"]) == 16
    assert side["original_width"] == 320 and side["stored"] == "original"


def test_backfill_hires_is_downscaled_and_capped(fake_nps, clock, tmp_path, data_root):
    storage = LocalStorage(data_root)
    assets = pick_assets(_pre_guard_assets(), min_sun_deg=20.0)
    _serve_assets(fake_nps, assets)
    archive = _client(fake_nps, clock, tmp_path, storage=storage)
    result = backfill_assets(
        storage=storage,
        archive=archive,
        viewpoint=PORE,
        assets=assets,
        kinds=("hires",),
        max_bytes=1,
    )
    assert result.downloaded == 1 and result.stopped and "size cap" in result.stopped
    assert fake_nps.count("GET", "proxy/hires") == 1
    side = storage.read_json(result.stored[0]["key"][:-4] + ".json")
    assert (side["original_width"], side["stored_width"], side["stored_height"]) == (
        3840,
        1920,
        1080,
    )
    assert side["stored"] == "downscaled-1920" and side["exif"]["Artist"] == "National Park Service"
    # The cap is cumulative across runs: a new run downloads nothing more.
    again = backfill_assets(
        storage=storage,
        archive=archive,
        viewpoint=PORE,
        assets=assets,
        kinds=("hires",),
        max_bytes=1,
    )
    assert again.downloaded == 0 and fake_nps.count("GET", "proxy/hires") == 1


def test_dry_run_sends_no_asset_requests(fake_nps, clock, tmp_path, data_root):
    storage = LocalStorage(data_root)
    assets = pick_assets(_pre_guard_assets(), min_sun_deg=0.0)
    archive = _client(fake_nps, clock, tmp_path, storage=storage)
    result = backfill_assets(
        storage=storage, archive=archive, viewpoint=PORE, assets=assets, dry_run=True
    )
    assert result.downloaded == 0 and len(result.plan) == len(assets)
    assert fake_nps.requests == []


def test_search_cache_avoids_repeat_requests(fake_nps, clock, tmp_path, data_root):
    storage = LocalStorage(data_root)
    fake_nps.search_pages[("pore", "11/17/2026")] = [fixture_json("search_pore_20261117.json")]
    archive = _client(fake_nps, clock, tmp_path, storage=storage)
    a, meta = cached_search(storage, archive, "pore", date(2026, 11, 17), today=date(2026, 11, 19))
    b, meta2 = cached_search(storage, archive, "pore", date(2026, 11, 17), today=date(2026, 11, 19))
    assert len(a) == len(b) == 88 and meta["from_cache"] is False and meta2["from_cache"] is True
    assert fake_nps.count("POST") == 1
    # Yesterday is not cached (frames may still arrive): not read, and not written either.
    fake_nps.search_pages[("pore", "11/18/2026")] = [
        copy.deepcopy(fake_nps.search_pages[("pore", "11/17/2026")][0])
    ]
    cached_search(storage, archive, "pore", date(2026, 11, 18), today=date(2026, 11, 19))
    cached_search(storage, archive, "pore", date(2026, 11, 18), today=date(2026, 11, 19))
    assert fake_nps.count("POST") == 3
    assert not storage.exists(search_cache_key("pore", date(2026, 11, 18)))
    assert meta["cached_on"] == "2026-11-19"


def test_grand_canyon_times_use_arizona_rules():
    asset = ArchiveAsset.from_result(
        {
            "Asset": {
                "AssetID": "0123abcd-0000-0000-0000-000000000000",
                "LocationCode": "grca",
                "ImageCreateDateTime": "7/15/2024 12:00:00 PM",
                "LocalTimeString": "7/15/2024 12:00 PM US Mountain Standard Time",
                "SolarElevationAngle": 75.0,
                "TimeOfDay": "day",
            }
        }
    )
    assert asset.image_time_utc(GRCA.time_zone) == datetime(2024, 7, 15, 19, 0, tzinfo=UTC)
    assert asset.image_time_utc(GRCA.time_zone, "wall") == datetime(2024, 7, 15, 19, 0, tzinfo=UTC)


# --- the search cache never freezes a day that is still filling (review finding) -------------


def test_search_of_a_recent_day_is_never_cached_and_a_stale_entry_is_refetched(
    fake_nps, clock, tmp_path, data_root
):
    storage = LocalStorage(data_root)
    results = fixture_json("search_pore_20261117.json")["Results"]
    day = date(2026, 11, 19)
    # On 19 Nov the day is still filling: 40 frames so far.
    fake_nps.search_pages[("pore", "11/19/2026")] = [
        {"Page": 1, "PageCount": 1, "ResultCount": 40, "SearchID": "a", "Results": results[:40]}
    ]
    archive = _client(fake_nps, clock, tmp_path, storage=storage)
    found, meta = cached_search(storage, archive, "pore", day, today=day)
    assert len(found) == 40 and meta["complete"] and not meta["from_cache"]
    assert not storage.exists(search_cache_key("pore", day))
    # An entry the old code wrote on the day itself must not be served two days later.
    storage.write_json(
        search_cache_key("pore", day),
        {
            "meta": {"day": "2026-11-19", "fetched_at": "2026-11-19T12:00:00Z", "complete": True},
            "assets": [ArchiveAsset.from_result(r).to_dict() for r in results[:40]],
        },
    )
    fake_nps.search_pages[("pore", "11/19/2026")] = [
        {"Page": 1, "PageCount": 1, "ResultCount": 88, "SearchID": "b", "Results": results}
    ]
    later = date(2026, 11, 21)
    found, meta = cached_search(storage, archive, "pore", day, today=later)
    assert len(found) == 88 and meta["from_cache"] is False and fake_nps.count("POST") == 2
    # Now it is written (the day is closed), and the next call is served from the cache.
    assert storage.read_json(search_cache_key("pore", day))["meta"]["cached_on"] == "2026-11-21"
    again, meta = cached_search(storage, archive, "pore", day, today=later)
    assert len(again) == 88 and meta["from_cache"] is True and fake_nps.count("POST") == 2


# --- a 200 that is not a JPEG is an error, retried a bounded number of times -----------------


def test_html_body_is_not_stored_and_is_retried_with_backoff(fake_nps, clock, tmp_path, data_root):
    storage = LocalStorage(data_root)
    asset = pick_assets(_pre_guard_assets(), pick="midday")[0]
    fake_nps.assets[f"{asset.asset_id}:thumbmedium"] = b"<!doctype html><h1>Server Error</h1>"
    archive = _client(fake_nps, clock, tmp_path, storage=storage)
    first = backfill_assets(storage=storage, archive=archive, viewpoint=PORE, assets=[asset])
    assert first.downloaded == 0 and first.failed == 1 and first.attempts == 3
    assert fake_nps.count("GET", "GetAsset") == 3 and not storage.list_prefix("archive/")
    times = [r["t"] for r in fake_nps.requests]
    gaps = [(b - a).total_seconds() for a, b in itertools.pairwise(times)]
    assert gaps[0] >= 30.0 and gaps[1] >= 60.0  # the shared back-off doubles
    state = storage.read_json(archive_state_key(PORE.id))
    assert asset.asset_id not in state.get("completed", {})
    assert state["failed"][asset.asset_id]["thumbmedium"]["attempts"] == 3
    assert state["bytes_downloaded"] == 3 * 36  # bad bodies count toward the size cap
    logged = [
        e
        for e in (data_root / "capture_log").glob("*.jsonl")
        for e in e.read_text().splitlines()
        if '"archive_request"' in e
    ]
    assert len(logged) == 3 and all("bad body: not a JPEG" in line for line in logged)
    # The server recovers: the next run stores the real frame and clears the failure.
    fake_nps.assets[f"{asset.asset_id}:thumbmedium"] = make_jpeg(100, 56)
    second = backfill_assets(storage=storage, archive=archive, viewpoint=PORE, assets=[asset])
    assert second.downloaded == 1 and second.failed == 0 and fake_nps.count("GET", "GetAsset") == 4
    state = storage.read_json(archive_state_key(PORE.id))
    assert asset.asset_id in state["completed"] and asset.asset_id not in state["failed"]


def test_truncated_jpeg_is_a_bad_body_and_gives_up_after_the_total_cap(
    fake_nps, clock, tmp_path, data_root
):
    storage = LocalStorage(data_root)
    asset = pick_assets(_pre_guard_assets(), pick="midday")[0]
    good = make_jpeg(320, 180, pattern=5)
    fake_nps.assets[f"{asset.asset_id}:thumbmedium"] = good[: len(good) // 2]
    archive = _client(fake_nps, clock, tmp_path, storage=storage)
    with pytest.raises(ArchiveBadBody, match="does not decode"):
        archive.get_asset(asset.asset_id, "thumbmedium")  # one request outside any backfill
    runs = [
        backfill_assets(storage=storage, archive=archive, viewpoint=PORE, assets=[asset])
        for _ in range(3)
    ]
    assert [r.attempts for r in runs] == [3, 3, 0]  # 3 + 3 = MAX_TOTAL_ATTEMPTS, then none
    assert runs[-1].given_up == 1 and runs[-1].failed == 0
    assert fake_nps.count("GET", "GetAsset") == 1 + MAX_TOTAL_ATTEMPTS
    assert not storage.list_prefix("archive/")
    # The total cap is exact: with 4 failures on record a run makes at most 2 more.
    state = storage.read_json(archive_state_key(PORE.id))
    state["failed"][asset.asset_id]["thumbmedium"]["attempts"] = 4
    storage.write_json(archive_state_key(PORE.id), state)
    once_more = backfill_assets(storage=storage, archive=archive, viewpoint=PORE, assets=[asset])
    assert once_more.attempts == 2 and once_more.failed == 1
    retried = backfill_assets(
        storage=storage, archive=archive, viewpoint=PORE, assets=[asset], retry_given_up=True
    )
    assert retried.attempts == 3 and retried.downloaded == 0


def test_a_404_is_not_retried(fake_nps, clock, tmp_path, data_root):
    storage = LocalStorage(data_root)
    asset = pick_assets(_pre_guard_assets(), pick="midday")[0]  # the fake has no bytes: 404
    archive = _client(fake_nps, clock, tmp_path, storage=storage)
    result = backfill_assets(storage=storage, archive=archive, viewpoint=PORE, assets=[asset])
    assert result.failed == 1 and result.attempts == 1 and fake_nps.count("GET", "GetAsset") == 1


def test_an_archive_redirect_is_never_followed_or_retried(fake_nps, clock, tmp_path, data_root):
    """Review finding (20 Nov 2026): the production archive clients followed redirects, so one
    logged request could be several at www.nps.gov, outside the 5 s limiter, the budget and
    the log. They now follow none: a 302 is one request, logged with where it pointed, and
    (like a 404) not retried. Without the fix the 302 below is followed and the asset fetched
    from its new address: two requests at nps.gov for one logged."""
    from snow_globe_capture.archive_queue import _queue_client

    storage = LocalStorage(data_root)
    asset = pick_assets(_pre_guard_assets(), pick="midday")[0]
    fake_nps.assets[f"{asset.asset_id}:thumbmedium"] = make_jpeg()  # what backfill asks for

    def handler(request: httpx.Request) -> httpx.Response:
        if "/GetAsset/" in str(request.url) and "moved" not in str(request.url):
            return httpx.Response(302, headers={"Location": f"{request.url}?moved=1"})
        return fake_nps.handler(request)

    archive = default_archive_client(storage, transport=httpx.MockTransport(handler))
    archive.limiter = _client(fake_nps, clock, tmp_path).limiter  # the fake clock's 5 s limiter
    result = backfill_assets(storage=storage, archive=archive, viewpoint=PORE, assets=[asset])
    assert result.failed == 1 and result.attempts == 1 and result.downloaded == 0
    assert fake_nps.count("GET", "GetAsset") == 0  # the redirect's target was never asked for
    logged = [
        json.loads(line)
        for path in (data_root / "capture_log").glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    (request,) = [r for r in logged if r["event"] == "archive_request"]
    assert request["status"] == 302 and "moved=1 not followed" in request["error"]
    assert _queue_client(storage).client.follow_redirects is False


# --- the held-out guard (D-019) ----------------------------------------------------------------


def _asset_on(create: str, local: str, asset_id: str = "7e57a55e-0000-0000-0000-000000000001"):
    return ArchiveAsset.from_result(
        {
            "Asset": {
                "AssetID": asset_id,
                "LocationCode": "pore",
                "ImageCreateDateTime": create,
                "LocalTimeString": local,
                "SolarElevationAngle": 70.0,
                "TimeOfDay": "day",
            }
        }
    )


def test_held_out_reason_reads_every_local_date_and_fails_closed():
    tz = PORE.time_zone
    assert (
        held_out_reason("01/15/2026 12:00:00 PM", "1/15/2026 12:00 PM Pacific Standard Time", tz)
        is None
    )
    assert (
        held_out_reason("07/15/2025 01:15:00 PM", "7/15/2025 2:15 PM Pacific Daylight Time", tz)
        is None
    )
    summer = held_out_reason(
        "07/15/2026 01:15:00 PM", "7/15/2026 2:15 PM Pacific Daylight Time", tz
    )
    assert summer is not None and "2026-07-15" in summer
    # Standard-time stamp on 31 May, civil time already 1 Jun: refused.
    edge = held_out_reason("05/31/2026 11:30:00 PM", "6/1/2026 12:30 AM Pacific Daylight Time", tz)
    assert edge is not None and "2026-06-01" in edge
    assert held_out_reason("garbage", "", tz) is not None  # no readable date: refused
    with pytest.raises(ValueError, match="written reason"):
        HeldOutOverride("scoring")


def test_summer_edge_without_local_time_string_is_refused():
    """Critic finding (20 Nov): ImageCreateDateTime stays on standard time in summer, so with
    no LocalTimeString "05/31/2026 11:30:00 PM" (00:30 PDT on 1 Jun) used to pass. Same
    cases as snow-globe-eval's test of nps_local_dates (D-038, D-047). The local readings still
    turn at local midnight; the guard itself now turns earlier, at UTC midnight (next test)."""
    la = PORE.time_zone
    assert la == "America/Los_Angeles"
    edge = held_out_reason("05/31/2026 11:30:00 PM", "", la)
    assert edge is not None and "2026-06-01" in edge
    assert date(2026, 6, 1) in asset_local_dates("05/31/2026 11:30:00 PM", "", la)
    # 23:00 standard is 00:00 PDT on 1 Jun; one second earlier is still 31 May locally.
    assert asset_local_dates("05/31/2026 10:59:59 PM", "", la) == [date(2026, 5, 31)]
    assert date(2026, 6, 1) in asset_local_dates("05/31/2026 11:00:00 PM", "", la)
    assert held_out_reason("05/31/2026 12:00:00 PM", "", la) is None  # noon, not midnight
    # Arizona keeps no daylight time: 23:30 on 31 May is 31 May locally (06:30 UTC on 1 Jun).
    assert asset_local_dates("05/31/2026 11:30:00 PM", "", "America/Phoenix") == [date(2026, 5, 31)]
    # Eastern zone, same edge.
    assert held_out_reason("05/31/2026 11:30:00 PM", "", "America/New_York") is not None
    # Winter: the standard-time reading is the stamp's own date.
    assert asset_local_dates("11/30/2025 11:30:00 PM", "", la) == [date(2025, 11, 30)]


#: The last ImageCreateDateTime of 31 May 2026 (local standard time, NPS's convention) still
#: before UTC midnight, per zone: standard offsets -8, -7 (Arizona: no daylight time), -7, -6, -5.
UTC_EDGE = {
    "America/Los_Angeles": ("03:59:59 PM", "04:00:00 PM", "Pacific"),
    "America/Phoenix": ("04:59:59 PM", "05:00:00 PM", "US Mountain"),
    "America/Denver": ("04:59:59 PM", "05:00:00 PM", "Mountain"),
    "America/Chicago": ("05:59:59 PM", "06:00:00 PM", "Central"),
    "America/New_York": ("06:59:59 PM", "07:00:00 PM", "Eastern"),
}


@pytest.mark.parametrize("zone", sorted(UTC_EDGE))
def test_the_guard_also_reads_utc_so_31_may_local_evening_is_held_out(zone):
    """The lead's reading of 20 Nov 2026 (pending the owner's D-038): an asset is held out if
    any reading of its date, local or UTC, is on or after 1 Jun 2026. A 31 May local-evening
    frame is already 1 Jun in UTC, so the guard now turns at UTC midnight: one second earlier
    is allowed, UTC midnight is refused, with or without a LocalTimeString."""
    last_ok, first_out, region = UTC_EDGE[zone]
    daylight = zone != "America/Phoenix"
    kind = "Daylight" if daylight else "Standard"

    def label(stamp: str) -> str:  # NPS's civil-time label: an hour ahead in daylight time
        wall = datetime.strptime(f"05/31/2026 {stamp}", "%m/%d/%Y %I:%M:%S %p")
        civil = wall + timedelta(hours=1 if daylight else 0)
        clock = f"{civil.hour % 12 or 12}:{civil:%M} {'PM' if civil.hour >= 12 else 'AM'}"
        return f"{civil.month}/{civil.day}/{civil.year} {clock} {region} {kind} Time"

    for with_label in (True, False):
        ok = (f"05/31/2026 {last_ok}", label(last_ok) if with_label else "")
        out = (f"05/31/2026 {first_out}", label(first_out) if with_label else "")
        assert asset_local_dates(*out, zone) == [date(2026, 5, 31)]  # local readings: 31 May
        assert held_out_reason(*ok, zone) is None, (zone, ok)
        reason = held_out_reason(*out, zone)
        assert reason is not None and "UTC date 2026-06-01" in reason, (zone, out, reason)
        assert asset_utc_dates(*out, zone)[-1] == date(2026, 6, 1)


def test_every_utc_reading_counts_and_a_date_without_a_clock_fails_closed():
    """The UTC readings (``asset_utc_dates``): the instant under both of snow-globe-common's
    conventions, each local clock converted in the camera's zone (both sides of a DST fold), and,
    for a reading with a date but no readable clock, every UTC date its local day touches."""
    la = PORE.time_zone
    # LocalTimeString alone decides: its clock read in the zone is 06:30 UTC on 1 Jun.
    assert asset_utc_dates("", "5/31/2026 11:30 PM Pacific Daylight Time", la) == [date(2026, 6, 1)]
    # A label with no clock: 31 May local runs to 06:59:59 UTC on 1 Jun, so it is refused.
    assert asset_utc_dates("", "5/31/2026", la) == [date(2026, 5, 31), date(2026, 6, 1)]
    assert held_out_reason("", "5/31/2026", la) is not None
    # A broken clock in ImageCreateDateTime: its date still counts, its whole day in UTC too.
    assert date(2026, 6, 1) in asset_utc_dates("05/31/2026 25:61", "", la)
    # The fall-back fold (1 Nov 2026, 01:30 happens twice): both sides are read.
    assert asset_utc_dates("11/01/2026 01:30:00 AM", "", la) == [date(2026, 11, 1)]
    # The instant behind an archive key is read too, local and UTC (backfill passes it).
    instant = datetime(2026, 6, 1, 0, 30, tzinfo=UTC)  # 17:30 PDT on 31 May
    assert held_out_reason("01/15/2026 12:00:00 PM", "", la, instant=instant) is not None
    assert asset_dates("", "", la, instant=instant) == [date(2026, 5, 31), date(2026, 6, 1)]
    with pytest.raises(ValueError, match="aware"):
        asset_dates("", "", la, instant=datetime(2026, 6, 1))
    # Winter and early summer days are untouched.
    assert held_out_reason("01/15/2026 11:30:00 PM", "", la) is None
    assert held_out_reason("05/30/2026 11:45:00 PM", "", la) is None


def test_backfill_refuses_held_out_frames_unless_overridden(fake_nps, clock, tmp_path, data_root):
    storage = LocalStorage(data_root)
    summer = _asset_on("07/15/2026 01:15:00 PM", "7/15/2026 2:15 PM Pacific Daylight Time")
    fake_nps.assets[f"{summer.asset_id}:thumbmedium"] = make_jpeg(100, 56)
    fake_nps.assets[f"{summer.asset_id}:hires"] = make_jpeg(999, 562)
    archive = _client(fake_nps, clock, tmp_path, storage=storage)
    refused = backfill_assets(
        storage=storage,
        archive=archive,
        viewpoint=PORE,
        assets=[summer],
        kinds=("thumbmedium", "hires"),
    )
    assert refused.refused_held_out == 2 and refused.downloaded == 0 and refused.planned == 0
    assert fake_nps.requests == [] and not storage.list_prefix("archive/")
    assert all("held-out guard" in p["refused"] for p in refused.plan)
    dry = backfill_assets(
        storage=storage, archive=archive, viewpoint=PORE, assets=[summer], dry_run=True
    )
    assert dry.refused_held_out == 1 and fake_nps.requests == []
    # Scoring, with a written reason: released, logged, and noted in the sidecar.
    reason = "design-H scoring after pre-registration commit abc1234 (test)"
    released = backfill_assets(
        storage=storage,
        archive=archive,
        viewpoint=PORE,
        assets=[summer],
        held_out_override=HeldOutOverride(reason),
    )
    assert released.downloaded == 1 and released.released_held_out == 1
    side = storage.read_json(released.stored[0]["key"][:-4] + ".json")
    assert side["held_out_override"] == reason
    log = (data_root / "capture_log").glob("*.jsonl")
    events = [line for path in log for line in path.read_text().splitlines()]
    assert any('"held_out_override"' in line and reason in line for line in events)


def test_archive_sidecars_carry_the_gate_inputs(fake_nps, clock, tmp_path, data_root):
    storage = LocalStorage(data_root)
    assets = pick_assets(_pre_guard_assets(), pick="midday")
    _serve_assets(fake_nps, assets)
    archive = _client(fake_nps, clock, tmp_path, storage=storage)
    result = backfill_assets(storage=storage, archive=archive, viewpoint=PORE, assets=assets)
    side = storage.read_json(result.stored[0]["key"][:-4] + ".json")
    assert side["sun"]["daylight"] is True and side["sun"]["elevation_deg"] > 20
    assert abs(side["sun"]["elevation_deg"] - assets[0].solar_elevation_deg) < 0.2
    assert side["sharpness_lapvar"] > 0 and "monochrome" not in side["flags"]


# --- the archive limiter is cross-process even with SNOW_GLOBE_STORAGE=s3 (review finding) -------


def test_archive_limiter_is_file_based_under_s3_storage(monkeypatch, data_root):
    monkeypatch.setenv("SNOW_GLOBE_STORAGE", "s3")
    # Since common's fix, "auto" is file-based everywhere except on Lambda.
    assert MinIntervalLimiter("nps-archive", 5.0).mode == "file"
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "snow-globe-capture")
    assert MinIntervalLimiter("nps-archive", 5.0).mode == "process"
    limiter = archive_limiter()  # the archive's limiter stays file-based even there
    assert limiter.mode == "file" and limiter.min_interval_s == 5.05
    assert default_archive_client(LocalStorage(data_root)).limiter.mode == "file"
    assert (data_root / "state" / "ratelimit").is_dir()


_HOLDER = """
import json, sys, time
from snow_globe_capture.archive import archive_limiter
from snow_globe_common.http import MinIntervalLimiter
start_at, interval, which = float(sys.argv[1]), float(sys.argv[2]), sys.argv[3]
if which == "archive":
    limiter = archive_limiter(interval)
else:  # an in-process limiter, as common's mode="auto" gave under S3 before both fixes
    limiter = MinIntervalLimiter("nps-archive", interval, mode="process")
while time.time() < start_at:
    time.sleep(0.005)
spans = []
for _ in range(2):
    with limiter.hold():
        began = time.time()
        time.sleep(0.05)
        spans.append((began, time.time()))
print(json.dumps({"mode": limiter.mode, "spans": spans}))
"""


def _two_processes_holding(tmp_path, data_root, which: str, interval: float):
    import json
    import os
    import subprocess
    import sys
    import time

    from .conftest import subprocess_prelude

    script = tmp_path / "holder.py"
    script.write_text(subprocess_prelude() + _HOLDER)
    env = {**os.environ, "SNOW_GLOBE_STORAGE": "s3", "SNOW_GLOBE_DATA_DIR": str(data_root)}
    start_at = time.time() + 1.5
    procs = [
        subprocess.Popen(
            [sys.executable, "-I", str(script), str(start_at), str(interval), which],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for _ in range(2)
    ]
    modes, spans = set(), []
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err
        doc = json.loads(out)
        modes.add(doc["mode"])
        spans.extend(tuple(s) for s in doc["spans"])
    spans.sort()
    gaps = [begin - end for (_, end), (begin, _) in itertools.pairwise(spans)]
    return modes, gaps


def test_archive_requests_are_spaced_across_real_processes_under_s3(tmp_path, data_root):
    interval = 0.4
    modes, gaps = _two_processes_holding(tmp_path, data_root, "archive", interval)
    assert modes == {"file"} and len(gaps) == 3
    assert min(gaps) >= interval - 0.02  # one at a time, >= interval end to start
    # Negative control: the limiter the archive used before the fix overlaps under S3.
    modes, gaps = _two_processes_holding(tmp_path, data_root, "auto", interval)
    assert modes == {"process"} and min(gaps) < interval - 0.02
