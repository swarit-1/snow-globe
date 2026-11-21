"""Records that date restarts exactly (0.3.0; additive, older readers ignore the new keys).

- every ``pass`` record carries ``started_at``: the pass's wall-clock start, ISO 8601 Z to the
  millisecond (its ``t`` stays the moment it was written, at the pass's end);
- the loop writes ``loop_started`` first (``capture_version``, ``runner``, ``pid``,
  ``started_at``), and ``loop_stopped`` whenever it ends without being interrupted, with a
  ``reason``: ``signal``, ``requested``, ``max_passes``, ``before_pass`` or ``error``;
- a second signal, a kill or a crash writes no ``loop_stopped``.
"""

from __future__ import annotations

import json
import os
import signal
from datetime import UTC, datetime

import pytest
from snow_globe_common.storage import LocalStorage
from snow_globe_common.timeutil import isoformat_z

import snow_globe_capture.fetcher_guard as guard_module
from snow_globe_capture import CAPTURE_VERSION
from snow_globe_capture.cli import EXIT_FETCHER_BUSY, main

from .test_fetcher_guard import STOPGAP
from .test_live import RAINIER, events, runner, set_rainier

START = datetime(2026, 11, 19, 8, 0, 30, 123_456, tzinfo=UTC)


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _records(data_root, event: str) -> list[dict]:
    return [r for r in events(data_root) if r["event"] == event]


def test_every_pass_record_carries_its_wall_clock_start_to_the_millisecond(
    data_root, clock, fake_nps
):
    clock.current = START
    storage = LocalStorage(data_root)
    set_rainier(fake_nps, clock.now(), etag='"p1"')
    summary = runner(storage, fake_nps, clock).run_pass([RAINIER])
    (record,) = _records(data_root, "pass")
    assert record["started_at"] == "2026-11-19T08:00:30.123Z"  # the clock at the pass's start
    elapsed = (_utc(record["t"]) - _utc(record["started_at"])).total_seconds()
    assert record["duration_s"] >= 1.0  # the HEAD, then the GET a request gap later
    assert abs(elapsed - record["duration_s"]) < 0.02  # t is the pass's end
    assert summary["started_at"] == "2026-11-19T08:00:30Z"  # the summary's own field as before
    # Batched (as on S3): the pass record goes out with its pass, and still carries it.
    clock.advance(400.0)
    started = clock.now()
    runner(storage, fake_nps, clock, batch_logs="always").run_pass([RAINIER])
    second = _records(data_root, "pass")[1]
    assert second["started_at"] == isoformat_z(started, "milliseconds")


def test_the_loop_logs_its_start_and_a_stop_after_max_passes(data_root, clock, fake_nps):
    clock.current = START
    storage = LocalStorage(data_root)
    set_rainier(fake_nps, clock.now(), etag='"l1"')
    run = runner(storage, fake_nps, clock, runner="cli-loop")
    passes = run.loop([RAINIER], interval_s=300, jitter_s=30, max_passes=2, install_signals=False)
    assert passes == 2
    rows = events(data_root)
    first, last = rows[0], rows[-1]
    assert first["event"] == "loop_started" and last["event"] == "loop_stopped"
    assert first["started_at"] == "2026-11-19T08:00:30.123Z"
    assert first["pid"] == os.getpid() and first["runner"] == "cli-loop"
    assert first["capture_version"] == CAPTURE_VERSION
    assert first["worker"] == "capture" and first["viewpoint_id"] is None
    assert first["viewpoints"] == [RAINIER.id]
    assert (first["interval_s"], first["jitter_s"]) == (300, 30)
    assert [r["event"] for r in rows].count("pass") == 2
    assert rows[-2]["event"] == "pass"  # the stop comes after the last pass
    assert last["reason"] == "max_passes" and last["passes"] == 2
    assert last["pid"] == os.getpid() and last["loop_started_at"] == first["started_at"]
    assert "signal" not in last and last["runner"] == "cli-loop"
    head = next(r for r in fake_nps.requests if r["method"] == "HEAD")
    assert _utc(first["t"]) <= head["t"]  # written before anything is sent


def test_a_sigterm_stops_the_loop_with_a_loop_stopped_record(data_root, clock, fake_nps):
    storage = LocalStorage(data_root)
    set_rainier(fake_nps, clock.now(), etag='"s1"')
    run = runner(storage, fake_nps, clock, runner="cli-loop")
    sent_at: list[datetime] = []
    handled: list[bool] = []

    def idle(time_left, stopping):  # the lead's restart: SIGTERM, between two passes
        sent_at.append(clock.now())
        # A real SIGTERM with no handler of the loop's would end the whole test run, not
        # fail this test (review finding, 21 Nov 2026): send it only to the loop's handler.
        handled.append(signal.getsignal(signal.SIGTERM) == run._on_signal)
        if handled[-1]:
            signal.raise_signal(signal.SIGTERM)
        else:
            run.request_stop()

    before = signal.getsignal(signal.SIGTERM)
    passes = run.loop([RAINIER], interval_s=300, jitter_s=0, idle=idle, max_passes=3)
    assert handled == [True], "the loop did not install its SIGTERM handler"
    assert passes == 1
    assert signal.getsignal(signal.SIGTERM) is before  # the handler was put back
    (stop,) = _records(data_root, "loop_stopped")
    assert stop["reason"] == "signal" and stop["signal"] == "SIGTERM" and stop["passes"] == 1
    assert _utc(stop["signal_at"]) == sent_at[0].replace(
        microsecond=sent_at[0].microsecond // 1000 * 1000
    )
    assert _utc(stop["t"]) >= _utc(stop["signal_at"])
    (start,) = _records(data_root, "loop_started")
    assert stop["loop_started_at"] == start["started_at"]


def test_request_stop_and_a_signal_before_the_first_pass(data_root, clock, fake_nps):
    storage = LocalStorage(data_root)
    set_rainier(fake_nps, clock.now(), etag='"r1"')
    run = runner(storage, fake_nps, clock)

    def idle(time_left, stopping):
        run.request_stop()

    assert run.loop([RAINIER], interval_s=300, idle=idle, install_signals=False) == 1
    (stop,) = _records(data_root, "loop_stopped")
    assert stop["reason"] == "requested" and stop["passes"] == 1 and "signal" not in stop
    # Stopped before it began (a signal right after start): both records, no pass.
    early = runner(storage, fake_nps, clock)
    early._on_signal(signal.SIGINT, None)
    assert early.loop([RAINIER], install_signals=False) == 0
    stops = _records(data_root, "loop_stopped")
    assert len(stops) == 2 and stops[1]["reason"] == "signal" and stops[1]["passes"] == 0
    assert stops[1]["signal"] == "SIGINT"
    assert len(_records(data_root, "loop_started")) == 2 and len(_records(data_root, "pass")) == 1


def test_a_second_signal_or_a_crash_writes_no_loop_stopped(data_root, clock, fake_nps, monkeypatch):
    storage = LocalStorage(data_root)
    set_rainier(fake_nps, clock.now(), etag='"k1"')
    run = runner(storage, fake_nps, clock)

    def twice(time_left, stopping):
        run._on_signal(signal.SIGTERM, None)
        run._on_signal(signal.SIGTERM, None)  # the second interrupts at once

    with pytest.raises(KeyboardInterrupt):
        run.loop([RAINIER], interval_s=300, idle=twice, install_signals=False)
    assert _records(data_root, "loop_stopped") == []
    assert len(_records(data_root, "loop_started")) == 1

    class Killed(BaseException):
        pass

    crashing = runner(storage, fake_nps, clock)

    def die(*args, **kwargs):
        raise Killed

    monkeypatch.setattr(crashing, "run_pass", die)
    with pytest.raises(Killed):
        crashing.loop([RAINIER], install_signals=False)
    assert _records(data_root, "loop_stopped") == []
    assert len(_records(data_root, "loop_started")) == 2


def test_an_error_out_of_a_pass_is_logged_as_the_stop_and_raised(
    data_root, clock, fake_nps, monkeypatch
):
    storage = LocalStorage(data_root)
    run = runner(storage, fake_nps, clock)

    def broken(*args, **kwargs):
        raise RuntimeError("state folder vanished")

    monkeypatch.setattr(run, "run_pass", broken)
    with pytest.raises(RuntimeError, match="vanished"):
        run.loop([RAINIER], install_signals=False)
    (stop,) = _records(data_root, "loop_stopped")
    assert stop["reason"] == "error" and stop["passes"] == 0
    assert stop["error"] == "RuntimeError: state folder vanished"


def test_the_loop_stopped_by_a_foreign_fetcher_writes_one_loop_stopped(
    data_root, monkeypatch, capsys
):
    """Through the console script, as fetcher_guard's test runs it: the stopgap appears after
    the first pass; the one ``loop_stopped`` names the reason and carries the error."""
    import snow_globe_capture.live as live

    def fake_pass(self, viewpoints, **kwargs):
        return {"counts": {}, "errors": 0, "outcomes": {}}

    monkeypatch.setattr(live.CaptureRunner, "run_pass", fake_pass)
    monkeypatch.setattr(live.time, "sleep", lambda s: None)
    seen = iter([[], [], [STOPGAP]])  # lock check, first pass check, then the stopgap appears
    monkeypatch.setattr(guard_module, "list_processes", lambda: next(seen))
    code = main(["loop", "--interval", "0", "--jitter", "0", "--only", "point-reyes.beach"])
    assert code == EXIT_FETCHER_BUSY
    rows = [
        json.loads(line)
        for path in (data_root / "capture_log").glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    starts = [r for r in rows if r["event"] == "loop_started"]
    (stop,) = [r for r in rows if r["event"] == "loop_stopped"]
    assert len(starts) == 1 and starts[0]["runner"] == "cli-loop"
    assert starts[0]["viewpoints"] == ["point-reyes.beach"]
    assert stop["reason"] == "before_pass" and stop["passes"] == 1
    assert stop["error"].startswith("FetcherBusy: another NPS fetcher is active")
    assert f"process {STOPGAP.pid}" in stop["error"] and stop["pid"] == os.getpid()
    assert stop["loop_started_at"] == starts[0]["started_at"]


def test_batched_logs_still_write_the_loop_records_at_once(data_root, clock, fake_nps):
    storage = LocalStorage(data_root)
    set_rainier(fake_nps, clock.now(), etag='"b1"')
    run = runner(storage, fake_nps, clock, batch_logs="always")
    assert run.loop([RAINIER], max_passes=1, install_signals=False) == 1
    kinds = [r["event"] for r in events(data_root)]
    assert kinds[0] == "loop_started" and kinds[-1] == "loop_stopped"
    assert kinds[-2] == "pass" and run._log_buffer is None
