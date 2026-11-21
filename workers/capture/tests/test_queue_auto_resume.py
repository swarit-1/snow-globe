"""The archive queue's daily resume of failed day searches (D-072, D-078), on the fake clock.

Before 0.3.0 a lead or the owner had to run ``archive queue resume`` every UTC day, or a
tranche whose day search failed three times stalled. Now the loop's runner sets such an entry
back to work itself: at its first idle on a later UTC day than the failure, at most once a UTC
day and 3 times in all, and only when every one of the search's errors was worth another try. A
redirect or another HTTP 4xx, errors counted by code before 0.3.0, any other failure, a
held-out refusal and a size cap still wait for the lead. The budget, the 5.05 s spacing and the
one-at-a-time rule are unchanged: a resume sends nothing, and the entry's requests go through
the same limiter and budget. It is on where the loop turns it on
(``QueueRunner(auto_resume_max=AUTO_RESUME_MAX)``); a runner built without it keeps 0.2.x's
behaviour, which the backfill planner's simulation models.
"""

from __future__ import annotations

import itertools
import json
import os
import time
from collections import Counter
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from snow_globe_common.storage import LocalStorage

import snow_globe_capture.archive_queue as archive_queue
import snow_globe_capture.live as live
from snow_globe_capture.archive import ArchiveClient, ArchiveError, is_retryable
from snow_globe_capture.archive_queue import (
    AUTO_RESUME_MAX,
    STEP_MAX_S,
    ArchiveQueue,
    QueueRunner,
    _retryable_text,
    auto_resumable,
    make_entry,
    queue_summary,
)
from snow_globe_capture.cli import build_parser, main

from .conftest import FakeClock, FakeNPS
from .test_archive import _client
from .test_archive_queue import (
    DAY,
    PORE,
    RAINIER,
    VPS,
    archive_requests,
    queue_with,
    serve_day,
    serve_two_pages,
)
from .test_archive_queue import queue_runner as plain_runner
from .test_live import runner as live_runner
from .test_live import set_rainier

SEARCH = "/api/Search/Execute"
#: The fake UTC day D of these tests, and the days after it.
D = date(2026, 11, 19)


def queue_runner(storage, queue, fake, clock, tmp_path, **kwargs) -> QueueRunner:
    """A queue runner as ``snow-globe-capture loop`` builds it: the daily resume on."""
    kwargs.setdefault("auto_resume_max", AUTO_RESUME_MAX)
    return plain_runner(storage, queue, fake, clock, tmp_path, **kwargs)


def at(day: date, hh: int = 0, mm: int = 0, ss: int = 1) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=UTC)


def plus(day: date, n: int) -> date:
    return day + timedelta(days=n)


class ScriptedSearch:
    """Answers each archive search in turn from ``script``, then with ``then``; nothing else
    changes. Outcomes: ``ok`` (the fake archive's answer), ``timeout`` and ``connect`` (httpx
    transport errors), ``redirect`` (a 301), ``not_json`` (a 200 HTML page), ``slow`` (the right
    answer 25 s late) or an HTTP status such as ``"500"``. Install it before the client is made."""

    def __init__(self, fake: FakeNPS, script: list[str] | None = None, then: str = "ok") -> None:
        self.fake = fake
        self.script = list(script or [])
        self.then = then
        self.original = fake.handler
        self.sent: list[tuple[str, datetime]] = []
        fake.handler = self.handler  # type: ignore[method-assign]

    def handler(self, request: httpx.Request) -> httpx.Response:
        if SEARCH not in str(request.url):
            return self.original(request)
        outcome = self.script.pop(0) if self.script else self.then
        assert self.fake.clock is not None
        self.sent.append((outcome, self.fake.clock.now()))
        if outcome == "ok":
            return self.original(request)
        if outcome == "slow":
            self.fake.clock.advance(25.0)
            return self.original(request)
        self.fake.requests.append(
            {"method": request.method, "url": str(request.url), "t": self.fake.clock.now()}
        )
        if outcome == "timeout":
            raise httpx.ReadTimeout("The read operation timed out", request=request)
        if outcome == "connect":
            raise httpx.ConnectError("[Errno 61] Connection refused", request=request)
        if outcome == "redirect":
            return httpx.Response(301, headers={"Location": "https://www.nps.gov/maintenance.htm"})
        if outcome == "not_json":
            return httpx.Response(200, text="<html>busy</html>")
        return httpx.Response(int(outcome), text="error")


def log_records(data_root, day: date, event: str | None = None) -> list[dict]:
    path = data_root / "capture_log" / f"{day.isoformat()}.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    return [r for r in rows if event is None or r.get("event") == event]


def only_entry(queue: ArchiveQueue) -> dict:
    (entry,) = queue.read()["entries"]
    return entry


def assert_spaced(fake: FakeNPS) -> None:
    sent = [r["t"] for r in archive_requests(fake)]
    assert all((b - a).total_seconds() >= 5.0 for a, b in itertools.pairwise(sent))


# --- the day rule and the cap -------------------------------------------------------------------


def test_a_failed_day_search_is_resumed_from_the_next_utc_day_once_a_day_and_3_times_in_all(
    data_root, fake_nps, clock, tmp_path
):
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    search = ScriptedSearch(fake_nps, then="timeout")  # nps.gov times out every search
    clock.current = at(D, 23, 50, 0)
    queue = queue_with(tmp_path, make_entry(PORE, DAY, pick="midday"))
    eid = only_entry(queue)["id"]
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    assert [run.step() for _ in range(3)] == ["error", "error", "failed"]
    entry = only_entry(queue)
    assert entry["status"] == "failed" and entry["failure_kind"] == "search_retryable"
    assert entry["reason"] == "search failed: ReadTimeout: The read operation timed out"
    assert entry["failed_at"].startswith("2026-11-19T23:5") and auto_resumable(entry)

    # The rest of the UTC day it failed on: never set back, however many idles.
    assert run.run_idle(lambda: 10_000.0) == {}
    clock.current = datetime(2026, 11, 19, 23, 59, 59, 999_000, tzinfo=UTC)
    assert run.run_idle(lambda: 10_000.0) == {}
    assert len(search.sent) == 3 and log_records(data_root, D, "queue_auto_resumed") == []

    for n, attempt in ((1, 1), (2, 2), (3, 3)):
        day = plus(D, n)
        clock.current = max(clock.current, at(day))
        # The first idle of the day sets it back; its search fails three more times.
        assert run.run_idle(lambda: 10_000.0) == {
            "auto_resumed": 1,
            "error": 2,
            "failed": 1,
        }, day
        entry = only_entry(queue)
        assert entry["status"] == "failed" and len(entry["auto_resumes"]) == attempt
        item = entry["auto_resumes"][-1]
        assert item["attempt"] == attempt and item["at"].startswith(f"{day.isoformat()}T00:00:01")
        assert item["reason"] == "search failed: ReadTimeout: The read operation timed out"
        assert item["failed_at"][:10] == plus(day, -1).isoformat()  # it failed the day before
        assert entry["failed_at"][:10] == day.isoformat()  # and again today
        # At most once a UTC day: later idles of the same day leave it alone.
        sent = len(search.sent)
        clock.advance(3600.0)
        assert run.run_idle(lambda: 10_000.0) == {} and len(search.sent) == sent
        (record,) = log_records(data_root, day, "queue_auto_resumed")
        assert record["queue_entry"] == eid and record["attempt"] == attempt
        assert record["reason"] == item["reason"] and record["failed_at"] == item["failed_at"]
        assert record["attempts_max"] == AUTO_RESUME_MAX == 3 and record["status"] == "pending"
        assert record["worker"] == "archive" and record["runner"] == "cli-loop"
        assert record["t"].startswith(f"{day.isoformat()}T00:00:01")
        assert record["capture_version"].startswith("snow-globe-capture/")

    # The 3-attempt cap: on every later day the entry stays failed and nothing is sent.
    for n in (4, 5, 30):
        clock.current = max(clock.current, at(plus(D, n)))
        assert run.run_idle(lambda: 10_000.0) == {}
        assert log_records(data_root, plus(D, n), "queue_auto_resumed") == []
    entry = only_entry(queue)
    assert entry["status"] == "failed" and not auto_resumable(entry)
    assert [i["attempt"] for i in entry["auto_resumes"]] == [1, 2, 3]
    assert len(search.sent) == 3 * (1 + AUTO_RESUME_MAX)  # 12 searches, no asset request
    assert len(archive_requests(fake_nps)) == 12
    assert_spaced(fake_nps)

    # The lead can still resume it by hand; its automatic resumes stay on record.
    search.then = "ok"
    assert queue.resume([eid]) == [eid]
    assert run.run_idle(lambda: 10_000.0) == {"searched": 1, "request": 1, "done": 1}
    entry = only_entry(queue)
    assert entry["status"] == "done" and len(entry["auto_resumes"]) == 3
    assert "failed_at" not in entry and "failure_kind" not in entry and "reason" not in entry


def test_a_clock_stepped_back_across_midnight_never_gives_two_resumes_in_a_utc_day(
    data_root, fake_nps, clock, tmp_path
):
    """Resumed at 00:00:01 on D + 1, the wall clock steps back into D, where the search fails
    again: back on D + 1 the failure is a day old, but D + 1 already had its resume."""
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    search = ScriptedSearch(fake_nps, then="timeout")
    clock.current = at(D, 12, 0, 0)
    queue = queue_with(tmp_path, make_entry(PORE, DAY, pick="midday"))
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    assert run.run_idle(lambda: 10_000.0) == {"error": 2, "failed": 1}
    clock.current = at(plus(D, 1))
    assert run.auto_resume() == [only_entry(queue)["id"]]
    clock.current = at(D, 23, 0, 0)  # the step back
    assert run.run_idle(lambda: 10_000.0) == {"error": 2, "failed": 1}
    assert only_entry(queue)["failed_at"][:10] == D.isoformat()
    clock.current = at(plus(D, 1), 6, 0, 0)
    assert run.run_idle(lambda: 10_000.0) == {} and run.auto_resume() == []
    clock.current = at(plus(D, 2))
    assert run.auto_resume() == [only_entry(queue)["id"]]
    assert [i["attempt"] for i in only_entry(queue)["auto_resumes"]] == [1, 2]
    assert len(search.sent) == 6


def test_a_resumed_entry_finishes_in_the_next_days_budget(data_root, fake_nps, clock, tmp_path):
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    search = ScriptedSearch(fake_nps, ["timeout", "connect", "500"], then="ok")
    clock.current = at(D, 9, 0, 0)
    queue = queue_with(tmp_path, make_entry(PORE, DAY, pick="midday"))
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path, daily_budget=5)
    assert run.run_idle(lambda: 10_000.0) == {"error": 2, "failed": 1}
    assert only_entry(queue)["failure_kind"] == "search_retryable"
    clock.current = at(plus(D, 1), 0, 4, 0)
    assert run.run_idle(lambda: 10_000.0) == {
        "auto_resumed": 1,
        "searched": 1,
        "request": 1,
        "done": 1,
    }
    entry = only_entry(queue)
    assert entry["status"] == "done" and entry["progress"]["outcome_counts"] == {"stored": 1}
    assert [i["attempt"] for i in entry["auto_resumes"]] == [1]
    assert [o for o, _ in search.sent] == ["timeout", "connect", "500", "ok"]
    # Each day's requests went through that day's persisted budget.
    for day, used in ((D, 3), (plus(D, 1), 2)):
        doc = json.loads(
            (data_root / "state" / "archive" / f"budget-archive-queue-{day}.json").read_text()
        )
        assert doc["used"] == used and doc["limit"] == 5
    assert_spaced(fake_nps)


def test_a_day_boundary_inside_the_live_loop(data_root, fake_nps, clock, tmp_path):
    """The real loop, across UTC midnight: the search fails three times in the first idle on
    D, is left alone by D's later idles, and goes back to work at the first idle after 00:00 UTC
    on D + 1, where it finishes; the live polls keep their schedule throughout."""
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    ScriptedSearch(fake_nps, ["timeout"] * 3, then="ok")
    clock.current = at(D, 23, 40, 0)
    queue = queue_with(tmp_path, make_entry(PORE, DAY, pick="midday"))
    archive_runner = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    set_rainier(fake_nps, clock.now(), etag='"m1"')
    live = live_runner(storage, fake_nps, clock)
    assert (
        live.loop(
            [RAINIER],
            interval_s=300,
            jitter_s=30,
            max_passes=12,
            install_signals=False,
            idle=archive_runner.run_idle,
        )
        == 12
    )
    heads = [r["t"] for r in fake_nps.requests if r["method"] == "HEAD"]
    assert len(heads) == 12 and heads[0].date() == D and heads[-1].date() == plus(D, 1)
    assert all(300 <= (b - a).total_seconds() <= 332 for a, b in itertools.pairwise(heads))
    sent = archive_requests(fake_nps)
    for r in sent:  # no archive request within STEP_MAX_S of the next live poll
        later = [h for h in heads if h > r["t"]]
        assert not later or (min(later) - r["t"]).total_seconds() >= STEP_MAX_S
    assert_spaced(fake_nps)
    searches = [r["t"] for r in sent if SEARCH in r["url"]]
    assert [t.date() for t in searches] == [D, D, D, plus(D, 1)]
    first_after_midnight = min(h for h in heads if h.date() == plus(D, 1))
    (record,) = log_records(data_root, plus(D, 1), "queue_auto_resumed")
    resumed = datetime.fromisoformat(record["t"].replace("Z", "+00:00"))
    # In the first idle of the new day: after its first pass, before the next one.
    assert first_after_midnight < resumed < min(h for h in heads if h > first_after_midnight)
    assert record["attempt"] == 1 and record["failed_at"][:10] == D.isoformat()
    entry = only_entry(queue)
    assert entry["status"] == "done" and len(entry["auto_resumes"]) == 1


# --- what is never resumed by itself ------------------------------------------------------------


@pytest.mark.parametrize(
    ("script", "start"),
    [
        (["redirect"] * 3, "search failed: HTTP 301 for "),
        (["404"] * 3, "search failed: HTTP 404 for "),
        (["403"] * 3, "search failed: HTTP 403 for "),
        (["timeout", "timeout", "redirect"], "search failed: HTTP 301 for "),
        (["redirect", "timeout", "timeout"], "search failed: ReadTimeout: "),  # any one of them
        (["400", "500", "500"], "search failed: HTTP 500 for "),
    ],
)
def test_a_search_failed_with_a_redirect_or_another_4xx_is_never_resumed_by_itself(
    script, start, data_root, fake_nps, clock, tmp_path
):
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    search = ScriptedSearch(fake_nps, script, then="ok")
    queue = queue_with(tmp_path, make_entry(PORE, DAY, pick="midday"))
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    assert [run.step() for _ in range(3)] == ["error", "error", "failed"]
    entry = only_entry(queue)
    assert entry["reason"].startswith(start) and entry["failure_kind"] == "search_not_retryable"
    assert not auto_resumable(entry)
    for n in (1, 2, 3):
        clock.current = at(plus(D, n))
        assert run.run_idle(lambda: 10_000.0) == {} and run.auto_resume() == []
        assert log_records(data_root, plus(D, n), "queue_auto_resumed") == []
    assert len(search.sent) == 3 and only_entry(queue) == entry  # untouched
    assert queue_summary(queue)["rows"][0]["auto_resumable"] is False


def test_other_failures_refusals_and_size_caps_are_never_resumed_by_itself(
    data_root, fake_nps, clock, tmp_path
):
    storage = LocalStorage(data_root)
    serve_two_pages(fake_nps)  # 17 Nov: a search over two pages
    queue = ArchiveQueue(tmp_path / "q.json")
    broken = make_entry(PORE, date(2025, 11, 18))
    broken["progress"] = {"selected": [{"no": "AssetID"}], "next": 0, "sweep": 1, "outcomes": {}}
    broken["status"] = "active"
    held_out = make_entry(PORE, date(2025, 11, 19))
    held_out.update(date="2026-07-15", id="hand-edited")
    unknown = make_entry(PORE, date(2025, 11, 20))
    unknown.update(viewpoint_id="nowhere.camera", id="unknown-viewpoint")
    cut_short = make_entry(PORE, DAY, pick="midday")
    capped = make_entry(PORE, DAY, pick="hourly", max_gb=1e-9)
    with queue.edit() as doc:
        doc["entries"] = [broken, held_out, unknown, cut_short]
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    steps = [run.step(fits=lambda: False) for _ in range(8)]
    assert steps == ["error", "error", "failed", "refused", "failed"] + ["search_cut_short"] * 2 + [
        "failed"
    ]
    queue.add([capped])
    while (outcome := run.step()) != "stopped":  # the size cap, after its first frame
        assert outcome in ("searched", "request")
    entries = {e["id"]: e for e in queue.read()["entries"]}
    reasons = {e["id"]: (e["status"], e.get("reason", "")) for e in entries.values()}
    assert reasons[broken["id"]][1].startswith("internal error: KeyError")
    assert reasons["hand-edited"] == ("refused", "held-out guard (D-019): 2026-07-15")
    assert reasons["unknown-viewpoint"] == ("failed", "unknown viewpoint or no NPS archive")
    assert "more pages than fit" in reasons[cut_short["id"]][1]
    assert reasons[capped["id"]][0] == "stopped" and "size cap" in reasons[capped["id"]][1]
    assert not any(auto_resumable(e) for e in entries.values())
    before = queue.read()["entries"]
    requests = len(fake_nps.requests)
    for n in (1, 2, 5):
        clock.current = at(plus(D, n))
        assert run.auto_resume() == [] and "auto_resumed" not in run.run_idle(lambda: 10_000.0)
    assert queue.read()["entries"] == before and len(fake_nps.requests) == requests


@pytest.mark.parametrize(
    "script",
    [
        ["timeout"] * 3,
        ["connect"] * 3,
        ["500"] * 3,
        ["503"] * 3,
        ["429"] * 3,
        ["not_json"] * 3,
        ["slow"] * 3,
        ["timeout", "502", "not_json"],
    ],
)
def test_every_error_worth_another_try_is_resumed_by_itself(
    script, data_root, fake_nps, clock, tmp_path
):
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    ScriptedSearch(fake_nps, script, then="ok")
    queue = queue_with(tmp_path, make_entry(PORE, DAY, pick="midday"))
    archive = _client(fake_nps, clock, tmp_path, storage=storage)
    archive.request_deadline_s = 20.0  # as the queue's own client: "slow" is past it
    run = QueueRunner(
        storage, VPS, queue, archive=archive, clock=clock.now, auto_resume_max=AUTO_RESUME_MAX
    )
    assert [run.step() for _ in range(3)] == ["error", "error", "failed"]
    entry = only_entry(queue)
    assert entry["failure_kind"] == "search_retryable" and auto_resumable(entry), entry["reason"]
    assert run.auto_resume() == []  # not on the day it failed
    clock.current = max(clock.current, at(plus(clock.current.date(), 1)))
    assert run.auto_resume() == [entry["id"]]
    resumed = only_entry(queue)
    assert resumed["status"] == "pending" and resumed["progress"] == {"requests": 3}
    assert run.auto_resume() == []  # once a UTC day


# --- entries written before 0.3.0 ---------------------------------------------------------------


def legacy_failed(day: date, reason: str, last_step_at: str | None) -> dict:
    """A failed entry as capture 0.2.0 and 0.2.1 leave it (``_search``, ``step``, ``_compact``):
    no ``failure_kind``, no ``failed_at``; the failing step's time in ``last_step_at``."""
    entry = make_entry(PORE, day, now=datetime(2026, 11, 20, 1, 0, tzinfo=UTC))
    entry.update(status="failed", reason=reason)
    progress = {"search_errors": 3, "last_error": reason.split(": ", 1)[-1], "requests": 3}
    if last_step_at is not None:
        progress["last_step_at"] = last_step_at
    entry["progress"] = progress
    return entry


URL = "https://www.nps.gov/airwebcams/api/Search/Execute"


@pytest.mark.parametrize(
    ("reason", "last_step_at", "resumed"),
    [
        # D-071: Big Bend's three search ReadTimeouts at 11:30:05 UTC on 20 Nov
        ("search failed: ReadTimeout: The read operation timed out", "2026-11-20T11:30:05Z", True),
        ("search failed: ConnectTimeout: timed out", "2026-11-20T11:30:05Z", True),
        ("search failed: RemoteProtocolError: Server disconnected", "2026-11-20T23:59:59Z", True),
        (f"search failed: HTTP 503 for {URL} busy", "2026-11-20T11:30:05Z", True),
        (f"search failed: HTTP 429 for {URL}", "2026-11-20T11:30:05Z", True),
        ("search failed: Search bibe 2025-06-14: not JSON", "2026-11-20T11:30:05Z", True),
        (f"search failed: ArchiveError: {URL} took longer than 20 s", "2026-11-20T11:30:05Z", True),
        (
            f"search failed: HTTP 301 for {URL} redirect to https://x.example/ not followed",
            "2026-11-20T11:30:05Z",
            False,
        ),
        (f"search failed: HTTP 404 for {URL}", "2026-11-20T11:30:05Z", False),
        ("search failed: something new", "2026-11-20T11:30:05Z", False),  # fail closed
        ("search failed: ReadTimeout: The read operation timed out", None, False),  # no date
        ("search failed: ReadTimeout: x", "not a time", False),
        ("internal error: KeyError: 'AssetID'", "2026-11-20T11:30:05Z", False),
        # An internal error whose text is a transport error's is still not a day search's.
        ("internal error: ConnectError: [Errno 61] Connection refused", "2026-11-20T11:30Z", False),
        (
            "the day's search has more pages than fit between live passes; run 'archive day' "
            "with the loop stopped",
            "2026-11-20T11:30:05Z",
            False,
        ),
    ],
)
def test_an_entry_failed_by_0_2_x_is_read_from_its_reason_and_dated_by_its_last_step(
    reason, last_step_at, resumed, data_root, fake_nps, tmp_path
):
    storage = LocalStorage(data_root)
    entry = legacy_failed(date(2025, 6, 14), reason, last_step_at)
    queue = ArchiveQueue(tmp_path / "q.json")
    with queue.edit() as doc:
        doc["entries"].append(entry)
    clock = FakeClock(datetime(2026, 11, 20, 23, 59, 59, tzinfo=UTC))
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    assert run.auto_resume() == []  # 20 Nov: the day it failed
    clock.current = datetime(2026, 11, 21, 0, 0, 0, tzinfo=UTC)
    assert run.auto_resume() == ([entry["id"]] if resumed else [])
    after = only_entry(queue)
    if resumed:
        assert after["status"] == "pending" and after["progress"] == {"requests": 3}
        assert after["auto_resumes"] == [
            {
                "attempt": 1,
                "at": "2026-11-21T00:00:00.000Z",
                "reason": reason,
                "failed_at": f"{last_step_at[:19]}.000Z",
            }
        ]
        (record,) = log_records(data_root, date(2026, 11, 21), "queue_auto_resumed")
        assert record["queue_entry"] == entry["id"] and record["reason"] == reason
    else:
        assert after == entry and log_records(data_root, date(2026, 11, 21)) == []
    assert fake_nps.requests == []  # a resume sends nothing


ALL_OUTCOMES = (
    *("timeout", "connect", "slow", "not_json", "redirect"),
    *("301", "302", "400", "403", "404", "410", "429", "500", "502", "503", "504"),
)


@pytest.mark.parametrize("outcome", ALL_OUTCOMES)
def test_the_reason_reading_agrees_with_is_retryable_on_real_errors(
    outcome, fake_nps, clock, tmp_path
):
    """``_retryable_text`` reads the text of an error; ``is_retryable`` the error itself. On
    every error ``ArchiveClient`` raises for a search, they agree."""
    serve_day(fake_nps)
    ScriptedSearch(fake_nps, [outcome])
    archive: ArchiveClient = _client(fake_nps, clock, tmp_path)
    archive.request_deadline_s = 20.0
    with pytest.raises(ArchiveError) as caught:
        archive.search_day("pore", DAY)
    exc = caught.value
    assert _retryable_text(str(exc)[:160]) is is_retryable(exc)
    worth = outcome in ("timeout", "connect", "slow", "not_json") or outcome[0] == "5"
    assert is_retryable(exc) is (worth or outcome == "429"), str(exc)


# --- the listing, and entries edited by hand ----------------------------------------------------


def test_the_queue_listing_says_what_the_daily_resume_will_do(
    data_root, fake_nps, clock, tmp_path, capsys
):
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    ScriptedSearch(fake_nps, then="timeout")
    queue = ArchiveQueue(data_root / "state" / "archive" / "queue.json")
    queue.add([make_entry(PORE, DAY, pick="midday")])
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    for _ in range(3):
        run.step()
    row = queue_summary(queue)["rows"][0]
    assert row["auto_resumable"] is True and row["auto_resumes"] == 0
    assert main(["archive", "queue", "list"]) == 0
    note = "[auto-resume 1/3 at the loop's first idle on a later UTC day]"
    assert note in capsys.readouterr().out
    for n in (1, 2, 3):
        clock.current = max(clock.current, at(plus(D, n)))
        run.run_idle(lambda: 10_000.0)
    row = queue_summary(queue)["rows"][0]
    assert row["status"] == "failed" and row["auto_resumes"] == 3 and not row["auto_resumable"]
    assert main(["archive", "queue", "list"]) == 0
    assert "[auto-resumed 3/3]" in capsys.readouterr().out
    assert main(["archive", "queue", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)["rows"][0]
    assert listed["auto_resumes"] == 3 and listed["auto_resumable"] is False


TIMED_OUT = "search failed: ReadTimeout: The read operation timed out"


@pytest.mark.parametrize("status", ["refused", "stopped", "done", "pending", "active", None])
def test_only_a_failed_entry_is_ever_resumed_by_itself(
    status, data_root, fake_nps, clock, tmp_path
):
    """Even with the reason and kind of a failed day search (an edit by hand), an entry that
    is not ``failed`` is never touched: a refusal or a size cap waits for the lead."""
    storage = LocalStorage(data_root)
    entry = legacy_failed(date(2025, 6, 14), TIMED_OUT, "2026-11-18T10:00:00Z")
    entry.update(status=status, failure_kind="search_retryable")
    queue = ArchiveQueue(tmp_path / "q.json")
    with queue.edit() as doc:
        doc["entries"].append(entry)
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    assert not auto_resumable(entry) and run.auto_resume() == []
    assert only_entry(queue) == entry


def test_hand_edits_never_wedge_the_queue_and_never_resume(data_root, fake_nps, clock, tmp_path):
    storage = LocalStorage(data_root)
    queue = ArchiveQueue(tmp_path / "q.json")
    odd = legacy_failed(date(2025, 6, 14), TIMED_OUT, "2026-11-18T10:00:00Z")
    odd["auto_resumes"] = "three times"  # not a list: no further automatic resume
    stamp = legacy_failed(date(2025, 6, 15), TIMED_OUT, "2026-11-18T10:00:00Z")
    stamp["auto_resumes"] = [{"attempt": 1, "at": "garbled"}]  # an unreadable stamp: fail closed
    good = legacy_failed(date(2025, 6, 16), TIMED_OUT, "2026-11-18T10:00:00Z")
    with queue.edit() as doc:
        doc["entries"] = [odd, stamp, "not an entry", good]
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    assert run.auto_resume() == [good["id"]]
    entries = queue.read()["entries"]
    assert entries[0] == odd and entries[1] == stamp and entries[2] == "not an entry"
    assert entries[3]["status"] == "pending"


def test_a_resume_that_fails_is_logged_once_a_day_and_the_queue_works_on(
    data_root, fake_nps, clock, tmp_path, monkeypatch
):
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    queue = queue_with(tmp_path, make_entry(PORE, DAY, pick="midday"))
    stuck = legacy_failed(date(2025, 6, 14), TIMED_OUT, "2026-11-18T10:00:00Z")
    with queue.edit() as doc:
        doc["entries"].insert(0, stuck)
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path)

    def broken(entry, **kwargs):
        raise RuntimeError("a bug in the resume")

    monkeypatch.setattr(archive_queue, "_reopen", broken)
    assert run.run_idle(lambda: 10_000.0) == {"searched": 1, "request": 1, "done": 1}
    entries = queue.read()["entries"]
    assert entries[0] == stuck and entries[1]["status"] == "done"  # nothing half-written
    (error,) = log_records(data_root, D, "queue_auto_resume_error")
    assert error["reason"] == "RuntimeError: a bug in the resume" and error["worker"] == "archive"
    assert run.run_idle(lambda: 10_000.0) == {}  # the same day: not logged again
    assert len(log_records(data_root, D, "queue_auto_resume_error")) == 1
    clock.current = at(plus(D, 1))
    run.run_idle(lambda: 10_000.0)
    assert len(log_records(data_root, plus(D, 1), "queue_auto_resume_error")) == 1


def test_auto_resume_can_be_turned_off_for_a_runner(data_root, fake_nps, clock, tmp_path):
    storage = LocalStorage(data_root)
    queue = ArchiveQueue(tmp_path / "q.json")
    entry = legacy_failed(date(2025, 6, 14), TIMED_OUT, "2026-11-18T10:00Z")
    with queue.edit() as doc:
        doc["entries"].append(entry)
    off = queue_runner(storage, queue, fake_nps, clock, tmp_path, auto_resume_max=0)
    assert off.auto_resume() == [] and off.run_idle(lambda: 10_000.0) == {}
    assert only_entry(queue) == entry
    on = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    assert on.auto_resume() == [entry["id"]]


def test_the_resume_is_off_unless_asked_for_and_the_loop_asks_for_it(
    data_root, fake_nps, clock, tmp_path, monkeypatch
):
    """Review finding (21 Nov 2026): the resume was on in every ``QueueRunner`` by default, so
    the backfill planner's simulation, which builds one to model the loop, silently changed
    model: its days mode without ``--resume-failed`` saw failed entries reopened the next day
    while its own accounting still took them as settled for good. A runner is now built with
    it off (0.2.x's behaviour) unless asked, and ``snow-globe-capture loop`` asks for it."""
    storage = LocalStorage(data_root)
    queue = ArchiveQueue(tmp_path / "q.json")
    entry = legacy_failed(date(2025, 6, 14), TIMED_OUT, "2026-11-18T10:00Z")
    with queue.edit() as doc:
        doc["entries"].append(entry)
    plain = plain_runner(storage, queue, fake_nps, clock, tmp_path)  # QueueRunner's defaults
    assert plain.auto_resume_max == 0
    for n in (1, 2, 3):
        clock.current = at(plus(D, n))
        assert plain.run_idle(lambda: 0.0) == {} and plain.auto_resume() == []
    assert only_entry(queue) == entry and fake_nps.requests == []

    built: list[QueueRunner] = []

    def fake_loop(self, viewpoints, **kwargs):  # the loop the console script starts
        built.append(kwargs["idle"].__self__)
        return 0

    monkeypatch.setattr(live.CaptureRunner, "loop", fake_loop)
    path = data_root / "state" / "archive" / "queue.json"
    assert main(["loop", "--archive-queue", str(path), "--only", PORE.id]) == 0
    (loop_runner,) = built
    assert isinstance(loop_runner, QueueRunner) and loop_runner.auto_resume_max == AUTO_RESUME_MAX


# --- review findings of 21 Nov 2026 -------------------------------------------------------------


@pytest.mark.parametrize("counted_before", [1, 2])
def test_search_errors_counted_before_0_3_0_are_never_taken_as_worth_another_try(
    counted_before, data_root, fake_nps, clock, tmp_path
):
    """Review finding (21 Nov 2026): an entry part-way through its search errors at the switch
    (counted by 0.2.1, which kept no judgement of them; here two 404s) was marked
    ``search_retryable`` on the strength of the errors 0.3.0 itself saw, and set back to work
    by itself the next day. It is now ``search_unclassified``: it waits for the lead."""
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    search = ScriptedSearch(fake_nps, ["timeout"] * (3 - counted_before), then="ok")
    entry = make_entry(PORE, DAY, pick="midday")
    entry["progress"] = {  # as 0.2.1 leaves an entry between two of its search errors
        "search_errors": counted_before,
        "last_error": f"HTTP 404 for {URL}",
        "requests": counted_before,
        "last_step_at": "2026-11-19T00:00:00Z",
    }
    queue = queue_with(tmp_path, entry)
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    steps = [run.step() for _ in range(3 - counted_before)]
    assert steps == ["error"] * (2 - counted_before) + ["failed"]
    failed = only_entry(queue)
    assert failed["reason"] == TIMED_OUT and failed["failure_kind"] == "search_unclassified"
    assert not auto_resumable(failed)
    assert queue_summary(queue)["rows"][0]["auto_resumable"] is False
    for n in (1, 2, 4):
        clock.current = at(plus(D, n))
        assert run.run_idle(lambda: 10_000.0) == {} and run.auto_resume() == []
    assert only_entry(queue) == failed and len(search.sent) == 3 - counted_before
    # The lead's resume starts a new episode, which 0.3.0 judges in full: three timeouts now
    # make it search_retryable, and the loop resumes it by itself the next day.
    search.script = ["timeout"] * 3
    assert queue.resume([failed["id"]]) == [failed["id"]]
    assert only_entry(queue)["progress"] == {"requests": 3}  # its 0.2.1 and 0.3.0 searches
    assert [run.step() for _ in range(3)] == ["error", "error", "failed"]
    again = only_entry(queue)
    assert again["failure_kind"] == "search_retryable" and auto_resumable(again)
    assert again["progress"]["search_errors_retryable"] == 3
    clock.current = at(plus(clock.current.date(), 1))
    assert run.auto_resume() == [again["id"]]


def test_a_redirect_after_a_manual_resume_is_judged_afresh(data_root, fake_nps, clock, tmp_path):
    """A manual resume clears the judgement of the episode before it, either way."""
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    ScriptedSearch(fake_nps, ["404"] * 3 + ["timeout"] * 3 + ["timeout", "redirect", "timeout"])
    queue = queue_with(tmp_path, make_entry(PORE, DAY, pick="midday"))
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    kinds = []
    for _ in range(3):
        assert [run.step() for _ in range(3)] == ["error", "error", "failed"]
        kinds.append(only_entry(queue)["failure_kind"])
        queue.resume([only_entry(queue)["id"]])
    assert kinds == ["search_not_retryable", "search_retryable", "search_not_retryable"]


def test_one_due_entry_that_cannot_be_set_back_never_blocks_the_others(
    data_root, fake_nps, clock, tmp_path
):
    """Review finding (21 Nov 2026): a due entry whose ``progress`` was not a record (a hand
    edit) made the resume raise inside the locked edit, so the whole edit was dropped: no
    entry was ever set back by itself again, and only ``queue_auto_resume_error`` was logged,
    once a day. Such an entry is now never due (fail closed), and the others go on."""
    storage = LocalStorage(data_root)
    queue = ArchiveQueue(tmp_path / "q.json")
    odd = legacy_failed(date(2025, 6, 14), TIMED_OUT, None)
    odd.update(
        failure_kind="search_retryable", failed_at="2026-11-18T10:00:00.000Z", progress=["x"]
    )
    good = legacy_failed(date(2025, 6, 16), TIMED_OUT, "2026-11-18T10:00:00Z")
    with queue.edit() as doc:
        doc["entries"] = [odd, good]
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    assert run.run_idle(lambda: 0.0) == {"auto_resumed": 1}  # no time left for a step
    first, second = queue.read()["entries"]
    assert first == odd  # untouched
    assert second["status"] == "pending" and [i["attempt"] for i in second["auto_resumes"]] == [1]
    assert log_records(data_root, D, "queue_auto_resume_error") == []
    assert [r["queue_entry"] for r in log_records(data_root, D, "queue_auto_resumed")] == [
        good["id"]
    ]
    assert not auto_resumable(odd) and auto_resumable(good)
    assert [r["auto_resumable"] for r in queue_summary(queue)["rows"]] == [False, False]


def test_an_entry_whose_set_back_fails_stays_as_it_was_and_the_others_go_on(
    data_root, fake_nps, clock, tmp_path, monkeypatch
):
    """Whatever makes one entry's resume fail, that entry is left exactly as it was, logged
    once a UTC day with its id, and every other due entry is still set back."""
    storage = LocalStorage(data_root)
    queue = ArchiveQueue(tmp_path / "q.json")
    stuck = legacy_failed(date(2025, 6, 14), TIMED_OUT, "2026-11-18T10:00:00Z")
    good = legacy_failed(date(2025, 6, 15), TIMED_OUT, "2026-11-18T10:00:00Z")
    with queue.edit() as doc:
        doc["entries"] = [stuck, good]
    real = archive_queue._reopen

    def picky(entry, **kwargs):
        if entry.get("date") == stuck["date"]:
            entry["status"] = "half-done"  # a change it must not leave behind
            raise RuntimeError("cannot set this one back")
        return real(entry, **kwargs)

    monkeypatch.setattr(archive_queue, "_reopen", picky)
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    assert run.auto_resume() == [good["id"]]
    first, second = queue.read()["entries"]
    assert first == stuck and second["status"] == "pending"
    (error,) = log_records(data_root, D, "queue_auto_resume_error")
    assert error["queue_entry"] == stuck["id"] and error["worker"] == "archive"
    assert error["reason"] == "RuntimeError: cannot set this one back"
    assert run.auto_resume() == [] and queue.read()["entries"][0] == stuck  # still due, still fails
    assert len(log_records(data_root, D, "queue_auto_resume_error")) == 1  # once a UTC day


def runner_block(capture_version: bool, auto_resume_max: int | None) -> dict:
    """A queue's ``runner`` heartbeat: as 0.2.1 writes it, plus what 0.3.0 adds."""
    block = {
        "pid": 4242,
        "updated_at": "2026-11-21T01:52:22Z",
        "last_entry": "x",
        "last_requests": 1,
        "requests_this_process": 1000,
        "budget": {"name": "archive-queue-2026-11-21", "used": 1000, "limit": 1000},
    }
    if capture_version:
        block["capture_version"] = "snow-globe-capture/0.3.0"
    if auto_resume_max is not None:
        block["auto_resume_max"] = auto_resume_max
    return block


@pytest.mark.parametrize(
    ("runner", "resumes", "note"),
    [
        (None, 0, "[needs 'archive queue resume': the queue's last runner (0.2.x, or none yet)"),
        (runner_block(False, None), 0, "[needs 'archive queue resume': the queue's last runner"),
        (runner_block(True, 3), 0, "[auto-resume 1/3 at the loop's first idle on a later UTC day]"),
        (runner_block(True, 3), 2, "[auto-resume 3/3 at the loop's first idle on a later UTC day]"),
        (runner_block(True, 3), 3, "[auto-resumed 3/3]"),
        (runner_block(True, 1), 1, "[auto-resumed 1/1]"),
        (runner_block(True, 0), 0, None),
    ],
)
def test_the_listing_promises_a_resume_only_when_the_queues_runner_makes_it(
    runner, resumes, note, data_root, capsys
):
    """Review finding (21 Nov 2026): the listing promised '[auto-resume 1/3 ...]' whichever
    loop worked the queue, also the 0.2.1 loop that runs until the switch and never resumes
    anything by itself. It now reads the queue's ``runner`` heartbeat, where 0.3.0 records its
    ``auto_resume_max``; with none recorded it says to resume the entry by hand."""
    queue = ArchiveQueue(data_root / "state" / "archive" / "queue.json")
    entry = legacy_failed(date(2025, 6, 14), TIMED_OUT, "2026-11-20T11:30:05Z")
    entry["auto_resumes"] = [
        {"attempt": n + 1, "at": f"2026-11-1{n}T00:00:01.000Z", "reason": TIMED_OUT}
        for n in range(resumes)
    ]
    with queue.edit() as doc:
        doc["entries"] = [entry]
        if runner is not None:
            doc["runner"] = runner
    assert main(["archive", "queue", "list"]) == 0
    out = capsys.readouterr().out
    if note is None:
        assert "auto-resume" not in out and "needs 'archive queue resume'" not in out
    else:
        assert note in out, out
    assert main(["archive", "queue", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    cap = None if runner is None else runner.get("auto_resume_max")
    assert listed["auto_resume_max"] == cap and listed["rows"][0]["auto_resumes"] == resumes


def test_the_queue_heartbeat_records_the_code_and_its_resume_cap(
    data_root, fake_nps, clock, tmp_path
):
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    queue = queue_with(tmp_path, make_entry(PORE, DAY, pick="midday"))
    queue_runner(storage, queue, fake_nps, clock, tmp_path).step()
    block = queue.read()["runner"]
    assert block["capture_version"].startswith("snow-globe-capture/")
    assert block["auto_resume_max"] == AUTO_RESUME_MAX == queue_summary(queue)["auto_resume_max"]
    plain_runner(storage, queue, fake_nps, clock, tmp_path).step()
    assert queue.read()["runner"]["auto_resume_max"] == 0
    assert queue_summary(queue)["auto_resume_max"] == 0


def test_the_resume_help_names_every_error_worth_another_try(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["archive", "queue", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    for kind in (
        "timeouts",
        "connection errors",
        "HTTP 429/5xx",
        "a body too slow or too large",
        "a body that is not JSON",
    ):
        assert kind in text, kind


def _set_zone(name: str | None) -> None:
    if name is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = name
    time.tzset()


@pytest.mark.parametrize(
    ("zone", "fails_at"),
    [
        ("America/Chicago", (1, 0, 0)),  # 01:00 UTC on D is 19:00 on D - 1 there
        ("Asia/Tokyo", (20, 0, 0)),  # 20:00 UTC on D is 05:00 on D + 1 there
    ],
)
def test_the_day_rule_is_the_utc_day_whatever_the_machine_zone(
    zone, fails_at, data_root, fake_nps, clock, tmp_path
):
    """Review finding (21 Nov 2026): the UTC-day rule was pinned only through this Mac's zone,
    so a regression that read either day in local time could pass CI (TZ=UTC). This queue
    fails most between 00:00 and 02:00 UTC, when its budget is spent."""
    saved = os.environ.get("TZ")
    _set_zone(zone)
    try:
        storage = LocalStorage(data_root)
        serve_day(fake_nps)
        ScriptedSearch(fake_nps, ["timeout"] * 3, then="ok")
        clock.current = at(D, *fails_at)
        queue = queue_with(tmp_path, make_entry(PORE, DAY, pick="midday"))
        run = queue_runner(storage, queue, fake_nps, clock, tmp_path)
        assert [run.step() for _ in range(3)] == ["error", "error", "failed"]
        assert only_entry(queue)["failed_at"][:10] == D.isoformat()
        for when in (clock.current + timedelta(minutes=5), at(D, 23, 59, 59)):
            clock.current = max(clock.current, when)
            assert run.auto_resume() == []  # never on the UTC day it failed
        clock.current = at(plus(D, 1), 0, 0, 1)
        assert run.auto_resume() == [only_entry(queue)["id"]]
    finally:
        _set_zone(saved)


def test_a_run_of_refusals_backs_the_queue_off_instead_of_spending_its_budget(
    data_root, fake_nps, clock, tmp_path
):
    """Review finding (21 Nov 2026; there since 0.2.0): against an HTTP 403 on every search,
    the queue sent a search each 5 s until its daily budget (1,000) was spent, because a 3xx
    or a 4xx other than 429 deferred nothing. Now every error answer backs off (30 s doubling
    to 30 min), so a day of the loop's idles sends a few dozen, never into a live pass."""
    storage = LocalStorage(data_root)
    search = ScriptedSearch(fake_nps, then="403")
    days = [date(2025, 10, 1) + timedelta(days=i) for i in range(40)]
    queue = queue_with(tmp_path, *[make_entry(PORE, d, pick="midday") for d in days])
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path)
    start = clock.now()

    def until(end: datetime):
        return lambda: (end - clock.now()).total_seconds()

    while clock.now() < start + timedelta(days=1):  # the loop: a pass each 300 s, idles between
        clock.advance(30.0)  # the pass
        idle_end = clock.now() + timedelta(seconds=270)
        run.run_idle(until(idle_end))
        assert clock.now() <= idle_end - timedelta(seconds=5)  # never into the next pass
        clock.current = max(clock.current, idle_end)
    sent = [t for _, t in search.sent if t < start + timedelta(days=1)]
    gaps = [(b - a).total_seconds() for a, b in itertools.pairwise(sent)]
    assert 7 <= len(sent) <= 55, len(sent)  # 1 + 6 doublings + one per 30 min at most
    assert min(gaps) >= 30.0 and max(gaps) <= 1800.0 + 300.0
    budget = json.loads(
        (data_root / "state" / "archive" / f"budget-archive-queue-{D}.json").read_text()
    )
    assert budget["used"] == len([t for t in sent if t.date() == D]) < 1000
    statuses = Counter(e["status"] for e in queue.read()["entries"])
    assert statuses["failed"] == len(sent) // 3 and statuses["pending"] + statuses["active"] > 0
    assert all(
        e.get("failure_kind") == "search_not_retryable"
        for e in queue.read()["entries"]
        if e["status"] == "failed"
    )


def test_a_runner_cap_below_3_is_the_cap(data_root, fake_nps, clock, tmp_path):
    storage = LocalStorage(data_root)
    serve_day(fake_nps)
    ScriptedSearch(fake_nps, then="timeout")
    queue = queue_with(tmp_path, make_entry(PORE, DAY, pick="midday"))
    run = queue_runner(storage, queue, fake_nps, clock, tmp_path, auto_resume_max=1)
    assert [run.step() for _ in range(3)] == ["error", "error", "failed"]
    resumed = []
    for n in (1, 2, 3, 4):
        clock.current = max(clock.current, at(plus(D, n)))
        resumed.append(run.run_idle(lambda: 10_000.0).get("auto_resumed", 0))
    assert resumed == [1, 0, 0, 0]
    entry = only_entry(queue)
    assert entry["status"] == "failed" and len(entry["auto_resumes"]) == 1
    assert not auto_resumable(entry, 1) and auto_resumable(entry, 3)
