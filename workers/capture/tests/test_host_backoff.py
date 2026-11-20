"""One back-off for www.nps.gov (PRD §15.3, CLAUDE.md "Back off on errors"): a live 429/503 holds
the queued archive work in the same loop, an archive 429/503 holds live polling, a standalone
archive request waits out a hold, and a hold is only ever extended."""

from __future__ import annotations

from datetime import timedelta

import pytest
from lookout_common.storage import LocalStorage

from lookout_capture.archive_queue import make_entry
from lookout_capture.host_backoff import HOST_STATE_KEY, extend_host_hold, host_hold_until
from lookout_capture.state import parse_time

from .conftest import fixture_json
from .test_archive import _client
from .test_archive_queue import (
    DAY,
    PORE,
    RAINIER,
    archive_requests,
    queue_runner,
    queue_with,
    serve_day,
)
from .test_live import runner as live_runner
from .test_live import set_rainier


def heads(fake) -> list:
    return [r["t"] for r in fake.requests if r["method"] == "HEAD"]


def test_a_live_503_holds_queued_archive_work_until_the_hold_ends(
    data_root, fake_nps, clock, tmp_path
):
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    queue = queue_with(tmp_path, make_entry(PORE, DAY, pick="hourly", min_sun_deg=0.0))
    archive = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    idle_results: list[dict] = []

    def idle(time_left, stopping):
        idle_results.append(archive.run_idle(time_left, stopping))

    passes = {"n": 0}

    def before_pass() -> None:  # the park camera answers 503 once, asking for 30 min
        passes["n"] += 1
        if passes["n"] == 1:
            image = set_rainier(fake_nps, clock.now(), etag='"busy"')
            fake_nps.set(
                RAINIER.truth_camera.image_url,
                image,
                status=503,
                headers={"Retry-After": "1800"},
            )
        elif passes["n"] == 2:
            set_rainier(fake_nps, clock.now(), etag='"ok"')

    live = live_runner(storage, fake_nps, clock)
    started = clock.now()
    live.loop(
        [RAINIER],
        interval_s=300,
        jitter_s=0,
        max_passes=10,
        install_signals=False,
        before_pass=before_pass,
        idle=idle,
    )
    hold = storage.read_json(HOST_STATE_KEY)
    until = parse_time(hold["not_before"])
    assert hold["source"] == "live" and "HTTP 503 on head" in hold["reason"]
    assert until >= started + timedelta(seconds=1799)
    sent = archive_requests(fake_nps)
    # Nothing at all went to the archive while the server's 30-minute hold was in force ...
    assert sent and all(r["t"] >= until for r in sent)
    assert sum(1 for r in idle_results if r.get("host_backoff")) >= 4
    # ... and live polling itself waited too: one HEAD (the 503), then none until the end.
    assert [t for t in heads(fake_nps) if t < until] == [heads(fake_nps)[0]]
    assert len(heads(fake_nps)) >= 2


def test_an_archive_503_holds_live_polling(data_root, fake_nps, clock, tmp_path):
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    queue = queue_with(tmp_path, make_entry(PORE, DAY, pick="midday"))
    archive = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    fake_nps.fail_next["/api/Search/Execute"] = 1  # 503, Retry-After: 120
    assert archive.step() == "error"
    hold = storage.read_json(HOST_STATE_KEY)
    assert hold["source"] == "archive" and hold["reason"] == "HTTP 503 on archive search"
    until = parse_time(hold["not_before"])
    assert until >= clock.now() + timedelta(seconds=119)
    set_rainier(fake_nps, clock.now(), etag='"r1"')
    out = live_runner(storage, fake_nps, clock).run_pass([RAINIER])["outcomes"][RAINIER.id]
    assert out["event"] == "skipped" and out["reason"] == "host_backoff"
    assert heads(fake_nps) == []
    # Queued work also waits for it, and a later step goes out only once it is over.
    assert archive.run_idle(lambda: 10_000.0) == {"host_backoff": 1}
    clock.current = until + timedelta(seconds=1)
    out = live_runner(storage, fake_nps, clock).run_pass([RAINIER])["outcomes"][RAINIER.id]
    assert out["event"] == "stored" and len(heads(fake_nps)) == 1
    assert archive.step() == "searched"
    assert archive_requests(fake_nps)[-1]["t"] >= until


def test_a_standalone_archive_request_waits_out_a_live_hold(data_root, fake_nps, clock, tmp_path):
    storage = LocalStorage(data_root)
    fake_nps.days["pore"] = fixture_json("days_pore.json")
    extend_host_hold(storage, clock.now(), 600, "HTTP 503 on head", source="live")
    start = clock.now()
    archive = _client(fake_nps, clock, tmp_path, storage=storage)
    archive.available_days("pore")
    assert (fake_nps.requests[-1]["t"] - start).total_seconds() >= 599
    # A client told not to share the hold (or with no storage) does not wait for it.
    extend_host_hold(storage, clock.now(), 600, "HTTP 503 on head", source="live")
    free = _client(fake_nps, clock, tmp_path)
    before = clock.now()
    free.available_days("pore")
    assert (fake_nps.requests[-1]["t"] - before).total_seconds() < 6


def test_a_hold_is_only_ever_extended_and_ends(data_root, clock):
    storage = LocalStorage(data_root)
    now = clock.now()
    assert host_hold_until(storage, now) is None
    long = extend_host_hold(storage, now, 900, "HTTP 429 on json", source="live")
    assert long == now + timedelta(seconds=900)
    assert extend_host_hold(storage, now, 60, "HTTP 503 on archive asset", source="archive") == long
    doc = storage.read_json(HOST_STATE_KEY)
    assert doc["reason"] == "HTTP 429 on json" and doc["host"] == "www.nps.gov"
    assert host_hold_until(storage, now + timedelta(seconds=899)) is not None
    assert host_hold_until(storage, now + timedelta(seconds=900)) is None


def test_an_unreadable_hold_stops_queued_work_without_a_request(
    data_root, fake_nps, clock, tmp_path
):
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    storage.put_bytes(HOST_STATE_KEY, b"{not json")
    queue = queue_with(tmp_path, make_entry(PORE, DAY, pick="midday"))
    archive = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    assert archive.run_idle(lambda: 10_000.0) == {"host_backoff": 1}
    assert fake_nps.requests == []
    with pytest.raises(ValueError):
        host_hold_until(storage, clock.now())
