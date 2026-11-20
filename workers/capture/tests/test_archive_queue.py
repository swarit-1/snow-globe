"""The archive queue and the combined live + archive loop (D-023): one request per step, >= 5 s
apart, never into a due live poll, held-out guard, daily budget, size cap, resume, and the
no-network queue commands."""

from __future__ import annotations

import itertools
import json
import threading
import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from lookout_common.manifest import load_manifests
from lookout_common.storage import LocalStorage

from lookout_capture.archive import api_code
from lookout_capture.archive_queue import (
    STEP_MARGIN_S,
    STEP_MAX_S,
    ArchiveQueue,
    QueueRunner,
    entry_id,
    make_entry,
    queue_summary,
)
from lookout_capture.cli import build_parser, main
from lookout_capture.heldout import HELD_OUT_FROM

from .conftest import FakeNPS, fixture_json, make_jpeg
from .test_archive import _client
from .test_live import runner as live_runner
from .test_live import set_rainier

VPS = load_manifests()
PORE = VPS["point-reyes.beach"]
RAINIER = VPS["rainier.paradise-mountain"]
DAY = date(2025, 11, 17)


def serve_day(fake: FakeNPS, *, width: int = 999) -> list[dict]:
    """The 17 Nov 2026 search fixture moved to 17 Nov 2025 (before the guard), with assets."""
    doc = fixture_json("search_pore_20261117.json")
    results = []
    for i, result in enumerate(doc["Results"]):
        asset = dict(result["Asset"])
        for name in ("ImageCreateDateTime", "LocalTimeString"):
            asset[name] = asset[name].replace("/2026", "/2025")
        results.append({**result, "Asset": asset})
        fake.assets[f"{asset['AssetID']}:hires"] = make_jpeg(width, width * 2 // 3, pattern=i)
        fake.assets[f"{asset['AssetID']}:thumbmedium"] = make_jpeg(320, 213, pattern=i)
    fake.search_pages[("pore", "11/17/2025")] = [{**doc, "Results": results}]
    return results


def queue_with(tmp_path, *entries: dict) -> ArchiveQueue:
    queue = ArchiveQueue(tmp_path / "queue" / "queue.json")
    queue.add(list(entries))
    return queue


def queue_runner(storage, queue, fake, clock, tmp_path, **kwargs) -> QueueRunner:
    archive = _client(fake, clock, tmp_path, storage=storage)
    return QueueRunner(storage, VPS, queue, archive=archive, clock=clock.now, **kwargs)


def archive_requests(fake: FakeNPS) -> list[dict]:
    return [r for r in fake.requests if "/airwebcams/" in r["url"]]


# --- entries and the queue file -------------------------------------------------------------


def test_entries_refuse_held_out_days_and_cameras_without_an_archive():
    entry = make_entry(PORE, DAY, kinds=["thumbmedium", "hires"], pick="hourly", min_sun_deg=0.0)
    assert entry["id"] == "point-reyes.beach/2025-11-17/hires+thumbmedium/hourly/sun>=0"
    assert entry["kinds"] == ["hires", "thumbmedium"] and entry["status"] == "pending"
    assert entry_id(PORE.id, "2025-11-17", ["hires"], "all", None).endswith("/all/any-sun")
    for late in (date(2026, 6, 1), date(2026, 7, 15), date(2026, 11, 17)):
        with pytest.raises(ValueError, match="held-out guard"):
            make_entry(PORE, late)
    make_entry(PORE, date(2026, 5, 31))  # the last day before the guard is allowed
    with pytest.raises(ValueError, match="no NPS archive"):
        make_entry(RAINIER, DAY)
    with pytest.raises(ValueError):
        make_entry(PORE, DAY, kinds=["huge"])
    with pytest.raises(ValueError):
        make_entry(PORE, DAY, pick="noon")


def test_queue_file_add_remove_and_concurrent_edits(tmp_path):
    queue = ArchiveQueue(tmp_path / "q.json")
    assert queue.read() == {"schema": 1, "entries": []}
    a, b = make_entry(PORE, DAY), make_entry(PORE, date(2025, 11, 18))
    assert queue.add([a, b, a]) == ([a["id"], b["id"]], [a["id"]])
    assert queue.add([a]) == ([], [a["id"]])

    def add(day: int) -> None:
        queue.add([make_entry(PORE, date(2025, 10, day))])

    threads = [threading.Thread(target=add, args=(d,)) for d in range(1, 9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(queue.read()["entries"]) == 10
    with queue.edit() as doc:
        doc["entries"][0]["status"] = "done"
    assert queue.remove(finished=True) == [a["id"]]
    assert queue.remove([b["id"]]) == [b["id"]]
    assert len(queue.remove(everything=True)) == 8 and queue.read()["entries"] == []
    assert json.loads((tmp_path / "q.json").read_text())["schema"] == 1


# --- working the queue ---------------------------------------------------------------------


def test_one_request_per_step_at_least_5_s_apart_until_done(data_root, fake_nps, clock, tmp_path):
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    queue = queue_with(tmp_path, make_entry(PORE, DAY, pick="hourly", min_sun_deg=0.0))
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    outcomes = []
    for _ in range(40):
        before = len(fake_nps.requests)
        outcome = run.step()
        outcomes.append(outcome)
        assert len(fake_nps.requests) - before <= 1  # never more than one request a step
        if outcome == "done":
            break
    assert outcomes[0] == "searched" and outcomes[-1] == "done"
    sent = archive_requests(fake_nps)
    gaps = [(b["t"] - a["t"]).total_seconds() for a, b in itertools.pairwise(sent)]
    assert min(gaps) >= 5.0
    entry = queue.read()["entries"][0]
    progress = entry["progress"]
    # A finished entry keeps counts only; state/archive/<id>.json holds the per-asset record.
    assert "selected" not in progress and "outcomes" not in progress
    selected = progress["selected_count"]
    assert 8 <= selected <= 12  # hourly, Sun up, in mid-November
    assert len([r for r in sent if "proxy/hires" in r["url"]]) == selected
    assert entry["status"] == "done" and progress["outcome_counts"] == {"stored": selected}
    assert progress["requests"] == len(sent)
    # (afternoon PST frames fall in the next UTC date folder)
    stored = [k for k in storage.list_prefix("archive/point-reyes.beach/") if k.endswith(".jpg")]
    assert len(stored) == selected
    assert all(storage.read_json(k[:-4] + ".json")["solar_elevation_deg"] >= 0 for k in stored)
    assert run.step() == "idle" and len(archive_requests(fake_nps)) == len(sent)
    runner_doc = queue.read()["runner"]
    assert runner_doc["budget"]["name"] == "archive-queue-2026-11-19"
    assert runner_doc["budget"]["used"] == len(sent)


def test_a_restart_resumes_without_fetching_anything_twice(data_root, fake_nps, clock, tmp_path):
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    queue = queue_with(tmp_path, make_entry(PORE, DAY, pick="hourly", min_sun_deg=0.0))
    first = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    for _ in range(4):
        first.step()
    first.close()
    second = queue_runner(storage, queue, fake_nps, clock, tmp_path)  # a new process
    while second.step() != "done":
        pass
    gets = [r["url"] for r in archive_requests(fake_nps) if r["method"] == "GET"]
    assert len(gets) == len(set(gets))  # every asset fetched once
    assert len([r for r in archive_requests(fake_nps) if r["method"] == "POST"]) == 1
    # Removed and queued again: the search comes from the cache (the day closed long ago)
    # and every frame is already stored, so nothing at all is sent.
    entry = queue.read()["entries"][0]
    assert queue.remove([entry["id"]]) == [entry["id"]]
    queue.add([make_entry(PORE, DAY, pick="hourly", min_sun_deg=0.0)])
    before = len(fake_nps.requests)
    assert second.step() == "done" and len(fake_nps.requests) == before
    counts = queue.read()["entries"][0]["progress"]["outcome_counts"]
    assert set(counts) == {"existing"}


def test_held_out_or_hand_edited_entries_are_refused_without_a_request(
    data_root, fake_nps, clock, tmp_path
):
    storage = LocalStorage(data_root)
    queue = ArchiveQueue(tmp_path / "q.json")
    with queue.edit() as doc:  # someone edits the file by hand past the guard
        entry = make_entry(PORE, DAY)
        entry.update(date="2026-07-15", id="hand-edited")
        doc["entries"].append(entry)
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    assert run.step() == "refused" and fake_nps.requests == []
    assert queue.read()["entries"][0]["status"] == "refused"
    assert "held-out guard" in queue.read()["entries"][0]["reason"]


def test_a_31_may_2026_entry_downloads_only_the_assets_before_the_summer_edge(
    data_root, fake_nps, clock, tmp_path
):
    """The queue is the only backfill path (D-029), and 31 May 2026 is the last day it may
    take (D-019, D-038). Each asset is dated by its LocalTimeString, its ImageCreateDateTime,
    that time read as local standard time, and the UTC date of each of those and of the
    instant behind its key (the lead's reading of 20 Nov 2026), and refused if any of them is
    1 June or later. At Point Reyes 31 May turns into 1 June UTC at 17:00 PDT, so the
    local-evening asset stamped 10:45 PM, 31 May in both its strings is refused, without a
    request, and only the 3:45 PM asset is fetched.
    """
    storage = LocalStorage(data_root)
    stamps = [
        ("05/31/2026 03:45:00 PM", "5/31/2026 4:45 PM Pacific Daylight Time"),  # 23:45 UTC
        ("05/31/2026 04:00:00 PM", "5/31/2026 5:00 PM Pacific Daylight Time"),  # 00:00 UTC
        ("05/31/2026 10:45:00 PM", "5/31/2026 11:45 PM Pacific Daylight Time"),  # 06:45 UTC
        ("05/31/2026 11:15:00 PM", None),  # standard time: 12:15 AM PDT on 1 June
        ("05/31/2026 11:30:00 PM", "6/1/2026 12:30 AM Pacific Daylight Time"),
        ("05/31/2026 11:45:00 PM", "garbage"),  # standard time: 12:45 AM PDT on 1 June
    ]
    results = []
    for i, (created, local) in enumerate(stamps):
        asset_id = f"{i + 1:08d}-aaaa-4bbb-8ccc-0000000000{i + 1:02d}"
        asset = {
            "LocationCode": "pore",
            "TimeOfDay": "night",
            "SolarElevationAngle": -20.0,
            "AssetID": asset_id,
            "ImageCreateDateTime": created,
            **({"LocalTimeString": local} if local is not None else {}),
        }
        results.append({"ItemNumber": i + 1, "Asset": asset})
        fake_nps.assets[f"{asset_id}:hires"] = make_jpeg(600, 400, pattern=i)
    fake_nps.search_pages[("pore", "05/31/2026")] = [
        {"Page": 1, "PageSize": 300, "ResultCount": 6, "PageCount": 1, "Results": results}
    ]
    queue = queue_with(tmp_path, make_entry(PORE, date(2026, 5, 31), pick="all"))
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    steps = []
    for _ in range(10):
        steps.append(run.step())
        if steps[-1] == "done":
            break
    assert steps[-1] == "done", steps
    gets = [r["url"] for r in archive_requests(fake_nps) if "/GetAsset/" in r["url"]]
    assert gets == [
        f"https://www.nps.gov/airwebcams/GetAsset/{results[0]['Asset']['AssetID']}/proxy/hires"
    ]
    entry = queue.read()["entries"][0]
    assert entry["progress"]["outcome_counts"] == {"stored": 1, "refused_held_out": 5}


#: The zone names NPS writes in LocalTimeString (cached searches of 15 Jul 2025).
ZONE_NAMES = {
    "America/Los_Angeles": ("Pacific Standard Time", "Pacific Daylight Time"),
    "America/Phoenix": ("US Mountain Standard Time", "US Mountain Standard Time"),
    "America/New_York": ("Eastern Standard Time", "Eastern Daylight Time"),
}


def serve_whole_day(fake: FakeNPS, viewpoint_id: str, day: date) -> list[str]:
    """All 96 frames of ``day`` as the archive lists them (ImageCreateDateTime on standard
    time, LocalTimeString on civil time); returns their asset ids in order."""
    vp = VPS[viewpoint_id]
    tz = ZoneInfo(vp.time_zone)
    probe = datetime(day.year, 1, 15, 12, tzinfo=tz)
    standard = (probe.utcoffset() or timedelta(0)) - (probe.dst() or timedelta(0))
    code = vp.truth_camera.archive_code
    results, ids = [], []
    for i in range(96):
        wall = datetime(day.year, day.month, day.day) + timedelta(minutes=15 * i)
        civil = (wall - standard).replace(tzinfo=UTC).astimezone(tz)
        name = ZONE_NAMES[vp.time_zone][1 if civil.dst() else 0]
        label = (
            f"{civil.month}/{civil.day}/{civil.year} {civil.hour % 12 or 12}:{civil.minute:02d} "
            f"{'PM' if civil.hour >= 12 else 'AM'} {name}"
        )
        asset_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{code}/{wall:%Y%m%dT%H%M}"))
        ids.append(asset_id)
        results.append(
            {
                "ItemNumber": i + 1,
                "Asset": {
                    "AssetID": asset_id,
                    "LocationCode": code,
                    "ImageCreateDateTime": wall.strftime("%m/%d/%Y %I:%M:%S %p"),
                    "LocalTimeString": label,
                    "SolarElevationAngle": 10.0,  # 'pick all' with no Sun limit ignores it
                    "TimeOfDay": "day",
                },
            }
        )
        fake.assets[f"{asset_id}:hires"] = make_jpeg(999, 562, pattern=i)
    page = {"Results": results, "ResultCount": 96, "PageCount": 1, "Page": 1, "PageSize": 300}
    fake.search_pages[(api_code(code), day.strftime("%m/%d/%Y"))] = [page]
    return ids


def test_31_may_at_the_guard_end_to_end_through_the_queue(data_root, fake_nps, clock, tmp_path):
    """Backfill plan review (20 Nov 2026), whole days at three cameras, with the UTC reading
    (the lead, 20 Nov 2026, pending the owner's D-038): every frame from UTC midnight on is
    refused without a request. That is 16:00 standard (17:00 PDT) at Point Reyes, 32 of 96
    frames; 19:00 EST (20:00 EDT) at Acadia, 20; 17:00 MST at Grand Canyon (Arizona keeps
    standard time), 28. Every stored frame is on 31 May in its camera's civil time and in UTC
    (D-019, D-022, D-038)."""
    storage = LocalStorage(data_root)
    day = HELD_OUT_FROM - timedelta(days=1)
    expected = {"point-reyes.beach": 64, "acadia.mcfarland": 76, "grand-canyon.yavapai": 68}
    ids = {vid: serve_whole_day(fake_nps, vid, day) for vid in expected}
    queue = ArchiveQueue(tmp_path / "q.json")
    queue.add([make_entry(VPS[vid], day, pick="all", min_sun_deg=None) for vid in expected])
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    for _ in range(400):
        if run.step() == "idle":
            break
    entries = {e["viewpoint_id"]: e for e in queue.read()["entries"]}
    gets = [r["url"] for r in archive_requests(fake_nps) if "/GetAsset/" in r["url"]]
    for vid, stored in expected.items():
        entry = entries[vid]
        counts = entry["progress"]["outcome_counts"]
        assert entry["status"] == "done"
        assert counts == {"stored": stored, "refused_held_out": 96 - stored}
        fetched = [u for u in gets if any(a in u for a in ids[vid])]
        assert len(fetched) == stored  # a refused frame never costs a request
        refused = ids[vid][stored:]  # the evening: every frame from UTC midnight on
        assert not any(a in u for u in gets for a in refused)
    sidecars = [json.loads(p.read_text()) for p in (data_root / "archive").glob("*/*/*.json")]
    assert len(sidecars) == sum(expected.values())
    for side in sidecars:
        utc = datetime.fromisoformat(side["image_time_utc"].replace("Z", "+00:00"))
        assert utc.astimezone(ZoneInfo(VPS[side["viewpoint_id"]].time_zone)).date() == day
        assert utc.date() == day


@pytest.mark.parametrize(
    "spelling", ["2026-06-01", "2026-07-15", "9999-12-31", "20260601", "2026-W23-1"]
)
def test_every_iso_spelling_of_a_held_out_day_is_refused(spelling, data_root, tmp_path):
    """Backfill plan review (20 Nov 2026): the guard holds for every ISO 8601 spelling Python
    reads, at make_entry and at ``archive queue add``, and a refused add writes no file."""
    day = date.fromisoformat(spelling)
    assert day >= HELD_OUT_FROM
    with pytest.raises(ValueError, match="held-out guard"):
        make_entry(VPS["point-reyes.beach"], day)
    queue_file = tmp_path / "q.json"
    with pytest.raises(SystemExit, match="held-out guard"):
        main(["archive", "queue", "add", "point-reyes.beach", spelling, "--queue", str(queue_file)])
    assert not queue_file.exists()


def test_the_daily_budget_pauses_the_queue_until_the_next_utc_day(
    data_root, fake_nps, clock, tmp_path
):
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    queue = queue_with(tmp_path, make_entry(PORE, DAY, pick="hourly", min_sun_deg=0.0))
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path, daily_budget=3)
    assert [run.step() for _ in range(3)] == ["searched", "request", "request"]
    assert run.step() == "paused" and len(archive_requests(fake_nps)) == 3
    assert run.run_idle(lambda: 10_000.0) == {}  # nothing until tomorrow
    clock.current = datetime(2026, 11, 20, 0, 0, 5, tzinfo=UTC)
    # A fresh budget: 39 s before the next pass leaves room for one step (35 s), then the
    # limiter's 5 s wait makes the second one too long.
    deadline = clock.monotonic() + 39.0
    assert run.run_idle(lambda: deadline - clock.monotonic()) == {"request": 1}
    assert len(archive_requests(fake_nps)) == 4


def test_the_size_cap_stops_the_entry(data_root, fake_nps, clock, tmp_path):
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    queue = queue_with(tmp_path, make_entry(PORE, DAY, pick="hourly", max_gb=1e-9))
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    assert run.step() == "searched" and run.step() == "request"
    assert run.step() == "stopped" and len(archive_requests(fake_nps)) == 2
    entry = queue.read()["entries"][0]
    assert entry["status"] == "stopped" and "size cap" in entry["reason"]


def test_failed_downloads_are_swept_again(data_root, fake_nps, clock, tmp_path):
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    queue = queue_with(tmp_path, make_entry(PORE, DAY, pick="midday"))
    fake_nps.fail_next["/proxy/hires"] = 1  # a 503 with Retry-After: 120
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    assert [run.step() for _ in range(4)] == ["searched", "request", "sweep", "request"]
    assert run.step() == "done"
    entry = queue.read()["entries"][0]
    assert entry["progress"]["sweep"] == 2 and entry["progress"]["outcome_counts"] == {"stored": 1}
    hires = [r["t"] for r in archive_requests(fake_nps) if "proxy/hires" in r["url"]]
    assert (hires[1] - hires[0]).total_seconds() >= 120  # the 503's Retry-After was honoured


def test_idle_work_never_starts_a_step_that_could_reach_the_next_pass(
    data_root, fake_nps, clock, tmp_path
):
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    queue = queue_with(tmp_path, make_entry(PORE, DAY, pick="all", min_sun_deg=0.0))
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    assert run.run_idle(lambda: STEP_MAX_S + STEP_MARGIN_S - 0.1) == {}
    assert fake_nps.requests == []
    deadline = clock.monotonic() + 120.0
    done = run.run_idle(lambda: deadline - clock.monotonic())
    sent = archive_requests(fake_nps)
    assert done and sum(done.values()) == len(sent) >= 2
    assert all(deadline - r["t"].timestamp() >= STEP_MAX_S for r in sent)
    stopping = run.run_idle(lambda: 10_000.0, lambda: True)
    assert stopping == {}


# --- the combined loop ------------------------------------------------------------------------


def test_combined_loop_does_archive_work_only_between_live_passes(
    data_root, fake_nps, clock, tmp_path
):
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    queue = queue_with(tmp_path, make_entry(PORE, DAY, pick="hourly", min_sun_deg=0.0))
    archive_runner = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    set_rainier(fake_nps, clock.now(), etag='"c1"')
    live = live_runner(storage, fake_nps, clock)
    passes = live.loop(
        [RAINIER],
        interval_s=300,
        jitter_s=30,
        max_passes=3,
        install_signals=False,
        idle=archive_runner.run_idle,
    )
    assert passes == 3
    heads = [r["t"] for r in fake_nps.requests if r["method"] == "HEAD"]
    assert len(heads) == 3
    # The live polls kept their schedule: the archive work did not delay them.
    assert all(300 <= (b - a).total_seconds() <= 332 for a, b in itertools.pairwise(heads))
    sent = archive_requests(fake_nps)
    assert sent and all(heads[0] < r["t"] < heads[-1] for r in sent)
    for r in sent:  # none in the last STEP_MAX_S before a pass
        nxt = min(h for h in heads if h > r["t"])
        assert (nxt - r["t"]).total_seconds() >= STEP_MAX_S
    gaps = [(b["t"] - a["t"]).total_seconds() for a, b in itertools.pairwise(sent)]
    assert min(gaps) >= 5.0
    progress = queue.read()["entries"][0]["progress"]
    assert progress["requests"] == len(sent) and progress["next"] >= 1


def test_an_idle_error_is_logged_and_never_stops_live_capture(data_root, fake_nps, clock):
    storage = LocalStorage(data_root)
    set_rainier(fake_nps, clock.now(), etag='"e1"')
    live = live_runner(storage, fake_nps, clock)

    def broken(time_left, stopping):
        raise RuntimeError("queue file is corrupt")

    assert live.loop([RAINIER], max_passes=2, install_signals=False, idle=broken) == 2
    lines = (data_root / "capture_log" / "2026-11-19.jsonl").read_text().splitlines()
    assert any(json.loads(line)["event"] == "idle_error" for line in lines)


# --- the console commands --------------------------------------------------------------------


@pytest.fixture
def no_network(monkeypatch):
    import lookout_common.http as http

    import lookout_capture.archive as archive

    def refuse(*args, **kwargs):
        raise AssertionError("a queue command built an HTTP client")

    monkeypatch.setattr(archive, "default_archive_client", refuse)
    monkeypatch.setattr(http, "polite_client", refuse)


def test_queue_commands_never_touch_the_network_or_the_fetcher_lock(data_root, capsys, no_network):
    from lookout_capture.fetcher_guard import nps_fetcher_lock

    with nps_fetcher_lock("the running loop", processes=lambda: []):  # held, as by the loop
        assert (
            main(
                [
                    "archive",
                    "queue",
                    "add",
                    "point-reyes.beach",
                    "2025-11-17",
                    "2025-11-18",
                    "--daytime-only",
                ]
            )
            == 0
        )
        added = json.loads(capsys.readouterr().out)
        assert len(added["added"]) == 2 and added["queue"].endswith("state/archive/queue.json")
        assert main(["archive", "queue", "list", "--json"]) == 0
        listed = json.loads(capsys.readouterr().out)
        assert listed["entries"] == 2 and listed["by_status"] == {"pending": 2}
        assert main(["status", "--json"]) == 0
        status = json.loads(capsys.readouterr().out)
        assert status["archive_queue"]["entries"] == 2
        assert main(["archive", "queue", "remove", added["added"][0]]) == 0
        assert json.loads(capsys.readouterr().out)["removed"] == [added["added"][0]]
        with pytest.raises(SystemExit, match="held-out guard"):
            main(["archive", "queue", "add", "point-reyes.beach", "2026-07-15"])
        with pytest.raises(SystemExit, match="no NPS archive"):
            main(["archive", "queue", "add", "rainier.paradise-mountain", "2025-11-17"])
        with pytest.raises(SystemExit, match="give ids"):
            main(["archive", "queue", "remove"])
    assert len(ArchiveQueue(data_root / "state" / "archive" / "queue.json").read()["entries"]) == 1


def test_loop_and_status_parse_the_queue_options():
    args = build_parser().parse_args(
        ["loop", "--archive-queue", "/tmp/q.json", "--archive-budget", "50"]
    )
    assert args.archive_queue == "/tmp/q.json" and args.archive_budget == 50
    assert args.archive_total_gb is None
    assert build_parser().parse_args(["loop", "--archive-total-gb", "2.5"]).archive_total_gb == 2.5
    assert args.nps_fetcher is True
    assert build_parser().parse_args(["loop"]).archive_queue is None
    status = build_parser().parse_args(["status", "--archive-queue", "/tmp/q.json"])
    assert status.archive_queue == "/tmp/q.json"
    add = build_parser().parse_args(["archive", "queue", "add", "point-reyes.beach", "2025-01-15"])
    assert add.pick == "hourly" and not getattr(add, "nps_fetcher", False)


def test_queue_summary_reports_progress(data_root, fake_nps, clock, tmp_path):
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    queue = queue_with(tmp_path, make_entry(PORE, DAY, pick="midday"))
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    run.step()
    run.step()
    summary = queue_summary(queue)
    row = summary["rows"][0]
    assert summary["by_status"] == {"active": 1} and row["pairs"] == 1
    assert row["outcomes"] == {"stored": 1} and row["requests"] == 2 and row["bytes"] > 0
    assert summary["runner"]["last_entry"] == row["id"]
    clock.advance(timedelta(days=1).total_seconds())
    assert run.step() == "done"


def test_a_broken_entry_fails_after_three_steps_and_the_queue_moves_on(
    data_root, fake_nps, clock, tmp_path
):
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    queue = ArchiveQueue(tmp_path / "q.json")
    broken = make_entry(PORE, date(2025, 11, 18))
    broken["progress"] = {"selected": [{"no": "AssetID"}], "next": 0, "sweep": 1, "outcomes": {}}
    broken["status"] = "active"
    queue.add([broken, make_entry(PORE, DAY, pick="midday")])
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    assert [run.step() for _ in range(3)] == ["error", "error", "failed"]
    assert fake_nps.requests == []
    entry = queue.read()["entries"][0]
    assert entry["status"] == "failed" and "KeyError" in entry["reason"]
    assert run.step() == "searched"  # the next entry is worked


def test_queue_add_takes_a_range_of_days_and_refuses_one_crossing_the_guard(data_root, capsys):
    assert (
        main(
            [
                "archive",
                "queue",
                "add",
                "grand-canyon.yavapai",
                "2025-07-01",
                "--through",
                "2025-07-31",
                "--daytime-only",
            ]
        )
        == 0
    )
    added = json.loads(capsys.readouterr().out)["added"]
    assert len(added) == 31 and added[0].startswith("grand-canyon.yavapai/2025-07-01/hires/hourly")
    with pytest.raises(SystemExit, match="held-out guard"):
        main(
            [
                "archive",
                "queue",
                "add",
                "grand-canyon.yavapai",
                "2026-05-20",
                "--through",
                "2026-06-10",
            ]
        )
    with pytest.raises(SystemExit, match="exactly one start date"):
        main(
            [
                "archive",
                "queue",
                "add",
                "grand-canyon.yavapai",
                "2025-07-01",
                "2025-07-02",
                "--through",
                "2025-07-05",
            ]
        )
    with pytest.raises(SystemExit, match="days after the start"):
        main(
            [
                "archive",
                "queue",
                "add",
                "grand-canyon.yavapai",
                "2025-07-01",
                "--through",
                "2025-06-01",
            ]
        )
    assert len(ArchiveQueue(data_root / "state" / "archive" / "queue.json").read()["entries"]) == 31


def test_a_queue_file_inside_the_repo_is_refused(data_root, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)
    with pytest.raises(SystemExit, match="refused queue file"):
        main(["archive", "queue", "list", "--queue", "queue.json"])
    with pytest.raises(SystemExit, match="refused queue file"):
        main(["status", "--archive-queue", str(repo / "q.json")])
    assert not (repo / "queue.json").exists()


# --- paging, the total cap, compaction, resume, the production client ------------------------


def serve_two_pages(fake: FakeNPS) -> None:
    """The day's 88 results over two pages of 50 (a day never needs this; the guard must hold)."""
    from .test_archive import _paged

    results = serve_day(fake)
    fake.search_pages[("pore", "11/17/2025")] = _paged(results, 50)


def test_a_paged_search_sends_a_further_page_only_if_it_fits(data_root, fake_nps, clock, tmp_path):
    storage = LocalStorage(data_root)
    serve_two_pages(fake_nps)
    queue = queue_with(tmp_path, make_entry(PORE, DAY, pick="midday"))
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    assert run.step(fits=lambda: False) == "search_cut_short"
    assert len(archive_requests(fake_nps)) == 1  # page 1 only: page 2 would not have fitted
    entry = queue.read()["entries"][0]
    assert entry["status"] == "pending" and "selected" not in entry["progress"]
    assert entry["progress"]["search_cut_short"] == 1
    # In the loop: 40 s before the next pass fits page 1 (35 s) but not page 2 (5 s more).
    deadline = clock.monotonic() + 40.0
    assert run.run_idle(lambda: deadline - clock.monotonic()) == {"search_cut_short": 1}
    assert len(archive_requests(fake_nps)) == 2
    # A full gap takes both pages in one step, and the complete search is then cached.
    assert run.run_idle(lambda: 10_000.0)["searched"] == 1
    posts = [r for r in archive_requests(fake_nps) if r["method"] == "POST"]
    assert len(posts) == 4
    assert storage.exists("cache/nps-archive/search/pore/2025-11-17.json")
    # Three cut-short searches in a row give the entry up, with the reason.
    queue.add([make_entry(PORE, date(2025, 11, 18), pick="midday")])
    fake_nps.search_pages[("pore", "11/18/2025")] = fake_nps.search_pages[("pore", "11/17/2025")]
    queue.remove([entry["id"]])
    assert [run.step(fits=lambda: False) for _ in range(3)] == [
        "search_cut_short",
        "search_cut_short",
        "failed",
    ]
    failed = queue.read()["entries"][0]
    assert failed["status"] == "failed" and "more pages than fit" in failed["reason"]


def test_finished_entries_keep_counts_only(data_root, fake_nps, clock, tmp_path):
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    queue = queue_with(tmp_path, make_entry(PORE, DAY, pick="hourly", min_sun_deg=0.0))
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    while run.step() != "done":
        pass
    entry = queue.read()["entries"][0]
    assert len(json.dumps(entry)) < 1000  # was ~5 KB with the asset list and outcomes
    row = queue_summary(queue)["rows"][0]
    assert row["status"] == "done" and row["pairs"] == entry["progress"]["selected_count"]
    assert row["outcomes"] == {"stored": row["pairs"]}


def test_a_stopped_entry_counts_as_finished_and_can_be_resumed_with_a_larger_cap(
    data_root, fake_nps, clock, tmp_path, capsys
):
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    queue = ArchiveQueue(data_root / "state" / "archive" / "queue.json")
    queue.add([make_entry(PORE, DAY, pick="hourly", max_gb=1e-9)])
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    assert [run.step() for _ in range(3)] == ["searched", "request", "stopped"]
    with pytest.raises(SystemExit, match="give ids or --all"):
        main(["archive", "queue", "resume"])
    assert main(["archive", "queue", "resume", "--all", "--max-gb", "1"]) == 0
    resumed = json.loads(capsys.readouterr().out)["resumed"]
    entry = queue.read()["entries"][0]
    assert resumed == [entry["id"]] and entry["status"] == "active" and entry["max_gb"] == 1.0
    assert "stopped" not in entry["progress"] and entry["progress"]["next"] == 1
    before = len(archive_requests(fake_nps))
    while (outcome := run.step()) != "done":
        assert outcome == "request"
    gets = [r["url"] for r in archive_requests(fake_nps) if r["method"] == "GET"]
    assert len(gets) == len(set(gets)) and len(archive_requests(fake_nps)) > before
    # "remove --finished" takes stopped entries too.
    queue.add([make_entry(PORE, date(2025, 11, 18), pick="midday")])
    with queue.edit() as doc:
        doc["entries"][-1]["status"] = "stopped"
    assert len(queue.remove(finished=True)) == 2 and queue.read()["entries"] == []


def test_the_total_cap_stops_queued_work_over_all_viewpoints(data_root, fake_nps, clock, tmp_path):
    from lookout_capture.archive import archive_state_key

    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    for vp, size in (("grand-canyon.yavapai", 3_000_000_000), ("acadia.mcfarland", 2_000_000_000)):
        storage.write_json(archive_state_key(vp), {"viewpoint_id": vp, "bytes_downloaded": size})
    queue = queue_with(tmp_path, make_entry(PORE, DAY, pick="midday"))
    capped = queue_runner(storage, queue, fake_nps, clock, tmp_path, total_cap_gb=5.0)
    assert capped.total_bytes() == 5_000_000_000
    assert capped.run_idle(lambda: 10_000.0) == {"total_cap": 1} and fake_nps.requests == []
    assert capped.run_idle(lambda: 10_000.0) == {"total_cap": 1}
    lines = (data_root / "capture_log" / "2026-11-19.jsonl").read_text().splitlines()
    assert [json.loads(line)["event"] for line in lines] == ["queue_total_cap"]  # logged once
    wider = queue_runner(storage, queue, fake_nps, clock, tmp_path, total_cap_gb=None)
    assert wider.run_idle(lambda: 10_000.0)["done"] == 1


def test_the_production_queue_client_is_polite_and_made_only_when_there_is_work(data_root):
    from lookout_common.http import user_agent

    from lookout_capture.archive_queue import (
        CONNECT_TIMEOUT_S,
        READ_TIMEOUT_S,
        REQUEST_DEADLINE_S,
        _queue_client,
    )

    storage = LocalStorage(data_root)
    client = _queue_client(storage)
    try:
        timeout = client.client.timeout
        assert (timeout.connect, timeout.read) == (CONNECT_TIMEOUT_S, READ_TIMEOUT_S) == (5.0, 10.0)
        assert client.request_deadline_s == REQUEST_DEADLINE_S == 20.0
        assert STEP_MAX_S == REQUEST_DEADLINE_S + READ_TIMEOUT_S
        assert client.limiter.min_interval_s == 5.0 and client.limiter.mode == "file"
        assert client.client.headers["User-Agent"] == user_agent()
        assert user_agent().startswith("Lookout/0.1")
        assert client.share_host_backoff and client.storage is storage
    finally:
        client.client.close()
    idle = QueueRunner(storage, VPS, ArchiveQueue(data_root / "q.json"))
    assert idle.run_idle(lambda: 10_000.0) == {} and idle._archive is None
    idle.close()


def test_slow_archive_requests_never_delay_a_live_poll(data_root, fake_nps, clock, tmp_path):
    """28 s archive answers (just under the 30 s a request may take) between 5-min passes."""
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    original = fake_nps.handler

    def slow(request):
        response = original(request)
        if "/airwebcams/" in str(request.url):
            clock.advance(28.0)
        return response

    fake_nps.handler = slow  # type: ignore[method-assign]
    queue = queue_with(tmp_path, make_entry(PORE, DAY, pick="all", min_sun_deg=0.0))
    archive_runner = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    set_rainier(fake_nps, clock.now(), etag='"s1"')
    live = live_runner(storage, fake_nps, clock)
    live.loop(
        [RAINIER],
        interval_s=300,
        jitter_s=30,
        max_passes=4,
        install_signals=False,
        idle=archive_runner.run_idle,
    )
    heads = [r["t"] for r in fake_nps.requests if r["method"] == "HEAD"]
    assert len(heads) == 4
    assert all(300 <= (b - a).total_seconds() <= 332 for a, b in itertools.pairwise(heads))
    sent = archive_requests(fake_nps)
    assert len(sent) >= 9  # the gaps were still used
    for r in sent:
        later = [h for h in heads if h > r["t"]]
        if later:  # every slow answer was over before the next pass began
            assert (min(later) - r["t"]).total_seconds() >= 28.0 + STEP_MARGIN_S
