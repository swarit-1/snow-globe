"""The polite rules hold where they matter: at nps.gov (the lead's decision of 20 Nov 2026).

CLAUDE.md allows a camera's poll at most every 5 minutes and at most 4 image GETs in any
rolling hour. nps.gov sees each request after connection set-up and transit, so a rule kept
send to send can fail there. Capture now counts both rules from the previous request's
*answer*: nps.gov receives a request no later than it answers it, and the next one no earlier
than we send it. The fake nps.gov here (:mod:`tests.receipts`) records when each request
arrives. These tests fail on capture 0.2.1 before answer times, which kept 300.002 s send to
send and counted the image cap on send times.

Also here: the send deadline that bounds an answer a crash kept from the state
(:mod:`lookout_capture.fetch`), on the wall clock and on a monotonic clock that counts system
sleep; the answer time of a failed request, and the bound on one that may still reach nps.gov
after it failed; the total deadline on a body that trickles; redirects, which are never
followed; the ``last_poll_at`` older code reads after a crash (a rollback); and the in-process
guard against wall-clock steps (and the exposure that is left across a restart).
"""

from __future__ import annotations

import itertools
import json
import random
import sys
import time
from datetime import datetime, timedelta
from typing import Any

import httpcore
import httpx
import pytest
from lookout_common.http import polite_client
from lookout_common.manifest import load_manifests
from lookout_common.storage import LocalStorage

from lookout_capture.fetch import (
    FAILED_AFTER_SEND_BOUND_S,
    LIVE_IMAGE_DEADLINE_S,
    LIVE_SEND_DEADLINE_S,
    UNANSWERED_REQUEST_BOUND_S,
    SendDeadlinePassed,
    fetch,
    steady_clock,
)
from lookout_capture.live import MIN_POLL_INTERVAL_S, POLL_MARGIN_S, CaptureRunner
from lookout_capture.state import CameraState, camera_state_key, cap_decision, iso_ms_up

from .conftest import FakeClock, make_jpeg
from .receipts import (
    T0,
    Crash,
    KillableStorage,
    Killer,
    Latency,
    ReceiptNPS,
    run_schedule,
    server_violations,
)

VPS = load_manifests()
RAINIER = VPS["rainier.paradise-mountain"]
PORE = VPS["point-reyes.beach"]


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _state(data_root, vp) -> CameraState:
    return CameraState.from_dict(json.loads((data_root / camera_state_key(vp.id)).read_text()))


def _records(data_root) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for path in sorted((data_root / "capture_log").glob("*.jsonl"))
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def scripted(polls: list[tuple[float, float]], images: list[tuple[float, float]]) -> Latency:
    """Each poll, and each image GET, takes the next (outbound, back) of its list, then 10/50 ms."""
    queues = {"poll": iter(polls), "image": iter(images)}
    return Latency(draw=lambda rng, stage, cold: next(queues[stage], (0.01, 0.05)))


def _runner(storage, nps: ReceiptNPS, clock) -> CaptureRunner:
    return CaptureRunner(
        storage,
        polite_client(transport=nps.transport()),
        clock=clock.now,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


# --- one request that arrives late ---------------------------------------------------------------


@pytest.mark.parametrize("vp", [RAINIER, PORE], ids=["head", "json"])
def test_a_poll_that_arrives_late_is_followed_300_s_after_it_arrived(data_root, vp):
    """The first poll takes 5 s to reach nps.gov (a stalled connection), the next arrives at
    once. Polled again as early as the scheduler allows, they are >= 300 s apart at nps.gov:
    the next poll waits for the first one's answer + 300.002 s. Kept send to send, the second
    went out 300.002 s after the first's send and reached nps.gov 295 s after the first."""
    clock = FakeClock(T0)
    nps = ReceiptNPS([vp], clock, latency=scripted([(5.0, 0.05)], []))
    storage = LocalStorage(data_root)
    _runner(storage, nps, clock).run_pass([vp])
    clock.current = T0 + timedelta(seconds=290)
    _runner(storage, nps, clock).run_pass([vp], wait_for_due_s=60.0)  # waits for the slot
    first, second = nps.receipts(vp.id, "poll")
    gap = (second - first).total_seconds()
    assert 300.0 <= gap < 300.2, gap
    assert server_violations(nps, [vp]) == []


def test_five_image_gets_span_an_hour_at_the_server_when_the_first_arrives_late(data_root):
    """An ARD feed shows a new image every 15 minutes, so its GETs come 900 s apart and the
    5th is due an hour after the 1st. The 1st takes 5 s to reach nps.gov, and the poll before
    the 5th answers 1.5 s late, so the 5th is decided just after the 1st's send + 3600 s.
    Counted on answers, the 5th waits for the 1st's answer + 3600 s, so nps.gov never sees 5
    GETs within an hour; counted on sends, the 5th went out and reached nps.gov about 3596.5 s
    after the 1st arrived."""
    polls = [(0.01, 0.05)] * 12 + [(0.01, 1.5)]  # the 13th poll (01:00) answers 1.5 s late
    run = run_schedule(
        data_root,
        [PORE],
        hours=1.5,
        schedule="lambda",
        latency=scripted(polls, [(5.0, 0.05)]),
    )
    gets = run.nps.receipts(PORE.id, "image")
    assert len(gets) >= 5
    assert (gets[4] - gets[0]).total_seconds() >= 3600.0, gets
    assert server_violations(run.nps, [PORE]) == []
    capped = [
        r
        for r in _records(data_root)
        if r["event"] == "rate_capped" and r["reason"] == "hourly_cap"
    ]
    assert capped  # the 5th was held back, not sent


# --- the request log and the state: one answer reading -----------------------------------------


def test_every_request_record_carries_its_answer_and_a_failure_is_answered_when_it_failed(
    data_root,
):
    """``answered_at`` is one clock reading right after the response was read in full, or
    right after the request failed; the state holds the very same string, and the next poll is
    due 300.002 s after it (or later, after the error back-off)."""
    clock = FakeClock(T0)
    storage = LocalStorage(data_root)

    def refuse(request: httpx.Request) -> httpx.Response:
        clock.advance(3.0)  # three seconds, then the connection is reset
        raise httpx.ConnectError("reset by peer", request=request)

    run = CaptureRunner(
        storage,
        httpx.Client(transport=httpx.MockTransport(refuse)),
        clock=clock.now,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    out = run.run_pass([PORE])["outcomes"][PORE.id]
    assert out["event"] == "error" and out["stage"] == "json"
    (record,) = [r for r in _records(data_root) if r["event"] == "request"]
    assert record["sent_at"] == "2026-11-21T00:00:00.000Z"
    assert record["answered_at"] == "2026-11-21T00:00:03.000Z" and record["status"] is None
    state = _state(data_root, PORE)
    assert (state.last_poll_at, state.last_answer_at) == (record["sent_at"], record["answered_at"])
    assert state.poll_reserved_at is None and state.last_answer_bound is None
    due = state.poll_due_at(MIN_POLL_INTERVAL_S + POLL_MARGIN_S)
    assert due >= _utc(record["answered_at"]) + timedelta(seconds=300.002)


def test_a_reservation_that_cannot_be_sent_in_time_is_abandoned_unsent(data_root):
    """The send deadline: a poll whose request is not on its way LIVE_SEND_DEADLINE_S after its
    reservation is abandoned before anything is sent (here the reservation's own state write
    took 31 s), logged as a failed request and backed off. That deadline is what lets a crash
    between the send and the save be bounded (UNANSWERED_REQUEST_BOUND_S)."""
    clock = FakeClock(T0)
    nps = ReceiptNPS([PORE], clock)

    class SlowStateWrites(LocalStorage):
        def write_json(self, key: str, *a: Any, **k: Any) -> Any:
            if key.startswith("state/capture/"):
                clock.advance(LIVE_SEND_DEADLINE_S + 1.0)
            return super().write_json(key, *a, **k)

    storage = SlowStateWrites(data_root)
    out = _runner(storage, nps, clock).run_pass([PORE])["outcomes"][PORE.id]
    assert out["event"] == "error" and "SendDeadlinePassed" in out["error"]
    assert nps.received == []  # nothing reached nps.gov
    (record,) = [r for r in _records(data_root) if r["event"] == "request"]
    assert "SendDeadlinePassed" in record["error"]
    assert UNANSWERED_REQUEST_BOUND_S == LIVE_SEND_DEADLINE_S + 30.0


class _Stream(httpcore.MockStream):
    """httpcore's mock stream, recording what is written to the "socket"."""

    def __init__(self, buffer: list[bytes]) -> None:
        super().__init__(buffer)
        self.written: list[bytes] = []

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.written.append(buffer)


class _SlowConnect(httpcore.MockBackend):
    """A network backend whose connect (DNS, TCP and TLS) takes ``connect_s`` of a fake
    monotonic clock; the real httpx and httpcore code runs on top of it, trace events and all."""

    def __init__(self, mono: list[float], connect_s: float) -> None:
        super().__init__([b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"])
        self.mono, self.connect_s = mono, connect_s
        self.streams: list[_Stream] = []

    def connect_tcp(self, *args: Any, **kwargs: Any) -> httpcore.NetworkStream:
        self.mono[0] += self.connect_s
        stream = _Stream(list(self._buffer))
        self.streams.append(stream)
        return stream


def _client_on(backend: httpcore.MockBackend) -> httpx.Client:
    transport = httpx.HTTPTransport()
    transport._pool = httpcore.ConnectionPool(network_backend=backend)
    return httpx.Client(transport=transport)


def test_the_send_deadline_is_checked_right_before_the_request_head_is_written():
    """Through the installed httpx and httpcore: a connection that took 31 s to set up (DNS,
    TCP, TLS, none of which httpx bounds as a whole) gets nothing written, and the request
    fails with SendDeadlinePassed; within the deadline the request goes out as usual."""
    mono = [100.0]
    slow = _SlowConnect(mono, connect_s=31.0)
    with _client_on(slow) as client, pytest.raises(SendDeadlinePassed):
        fetch(client, "GET", "https://www.nps.gov/x", send_by=130.0, monotonic=lambda: mono[0])
    assert len(slow.streams) == 1 and slow.streams[0].written == []
    mono[0] = 100.0
    quick = _SlowConnect(mono, connect_s=1.0)
    with _client_on(quick) as client:
        got = fetch(
            client, "GET", "https://www.nps.gov/x", send_by=130.0, monotonic=lambda: mono[0]
        )
    assert got.status_code == 200 and got.content == b"ok"
    assert b"GET /x HTTP/1.1" in b"".join(quick.streams[0].written)
    with _client_on(_SlowConnect([0.0], 0.0)) as client, pytest.raises(SendDeadlinePassed):
        fetch(client, "GET", "https://www.nps.gov/x", send_by=99.0, monotonic=lambda: 100.0)


# --- wall-clock steps -----------------------------------------------------------------------------


class SteppingClock:
    """A wall clock that can be stepped (as NTP does) over a monotonic clock that cannot."""

    def __init__(self, start: datetime) -> None:
        self.start = start
        self.mono_s = 0.0
        self.offset = timedelta(0)

    def true_now(self) -> datetime:
        return self.start + timedelta(seconds=self.mono_s)

    def now(self) -> datetime:
        return self.true_now() + self.offset

    def monotonic(self) -> float:
        return self.mono_s

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.mono_s += max(seconds, 1e-6)

    def advance(self, seconds: float) -> None:
        self.mono_s += seconds

    def step(self, seconds: float) -> None:
        self.offset += timedelta(seconds=seconds)


def _stepping_runner(storage, nps: ReceiptNPS, clock: SteppingClock) -> CaptureRunner:
    return CaptureRunner(
        storage,
        polite_client(transport=nps.transport()),
        clock=clock.now,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


@pytest.mark.parametrize("step_s", [30.0, -30.0], ids=["forward", "backward"])
def test_a_wall_clock_step_within_a_process_never_brings_a_poll_early(data_root, step_s):
    """Within a process each answer is also read on the monotonic clock and the rule uses the
    later reading: a forward step (the wall clock jumps 30 s ahead) still leaves 300 s of
    monotonic time; a backward step only delays the poll by the step. The forward case fails
    without the guard; the backward case is a characterisation (code without the guard
    passes it too) that pins the delay to the step and no more."""
    clock = SteppingClock(T0)
    nps = ReceiptNPS([RAINIER], clock, true_now=clock.true_now)
    storage = LocalStorage(data_root)
    run = _stepping_runner(storage, nps, clock)
    run.run_pass([RAINIER])
    clock.advance(100.0)
    clock.step(step_s)
    clock.advance(170.0)  # by the wall clock the slot is 300 s after the answer
    for _ in range(4):  # the same process, passes 60 s apart, each waiting up to 60 s
        run.run_pass([RAINIER], wait_for_due_s=60.0)
        clock.advance(60.0)
    first, second = nps.receipts(RAINIER.id, "poll")[:2]
    gap = (second - first).total_seconds()
    assert gap >= 300.0, gap
    assert gap < 301.5 + max(0.0, -step_s), gap  # a backward step delays by the step, no more


def test_across_a_restart_a_forward_step_shortens_that_one_gap_by_the_step(data_root):
    """The exposure that is left (README, "Wall-clock steps"): a new process has only the
    wall-clock stamps, so a forward step between the last answer and the restart brings that
    camera's next poll early by the step. Measured here, not hidden: 30 s ahead gives 270 s.
    A characterisation, not a guard: code before answer times gives the same 270 s."""
    clock = SteppingClock(T0)
    nps = ReceiptNPS([RAINIER], clock, true_now=clock.true_now)
    storage = LocalStorage(data_root)
    _stepping_runner(storage, nps, clock).run_pass([RAINIER])
    clock.advance(100.0)
    clock.step(30.0)
    clock.advance(170.0)
    _stepping_runner(storage, nps, clock).run_pass([RAINIER], wait_for_due_s=60.0)  # restarted
    first, second = nps.receipts(RAINIER.id, "poll")[:2]
    gap = (second - first).total_seconds()
    heads = [r for r in _records(data_root) if r["event"] == "request" and r["stage"] == "head"]
    by_wall = (_utc(heads[1]["sent_at"]) - _utc(heads[0]["answered_at"])).total_seconds()
    assert by_wall >= MIN_POLL_INTERVAL_S + POLL_MARGIN_S  # the rule held on the stamps
    assert by_wall - 30.0 - 1e-3 <= gap < MIN_POLL_INTERVAL_S, (gap, by_wall)  # early by 30 s


def test_the_image_cap_reads_this_process_answers_on_the_monotonic_clock_too(data_root):
    """Four GETs answered in this process, then the wall clock jumps 120 s ahead: by the wall
    clock the 1st answer is an hour old 120 s early, but the cap still counts 3600 s of
    monotonic time."""
    clock = SteppingClock(T0)
    storage = LocalStorage(data_root)
    unused = httpx.MockTransport(lambda request: httpx.Response(500))
    run = CaptureRunner(
        storage, polite_client(transport=unused), clock=clock.now, monotonic=clock.monotonic
    )
    state = CameraState(viewpoint_id=RAINIER.id)
    for i in range(4):  # answered 0.2 s after each reservation, 900 s apart
        record = state.add_get(clock.now(), status="reserved", reserved=True)
        clock.advance(0.2)
        record["answered_at"] = iso_ms_up(clock.now())
        run._steady[(RAINIER.id, record["answered_at"])] = clock.monotonic()
        clock.advance(899.8 if i < 3 else 0.0)
    first = _utc(state.gets[0]["answered_at"])
    clock.advance(3600.0 - 2700.0 - 121.0)  # 121 s short of an hour after the 1st answer
    clock.step(125.0)
    assert clock.now() >= first + timedelta(seconds=3600)  # the wall clock says "an hour"
    plain = cap_decision(state.gets, clock.now(), max_per_hour=4, min_spacing_s=0)
    guarded = cap_decision(
        state.gets, clock.now(), max_per_hour=4, min_spacing_s=0, adjust=run._adjuster(RAINIER.id)
    )
    assert plain.allowed  # the exposure the guard closes
    assert not guarded.allowed and guarded.reason == "hourly_cap"
    assert guarded.next_allowed_at - clock.now() > timedelta(seconds=120)


# --- the whole schedule, with spikes, on the request log alone -----------------------------------


def test_the_log_alone_shows_every_gap_at_the_server_is_at_least_its_provable_bound(data_root):
    """For every pair of consecutive polls, nps.gov's real gap is at least ``sent_at`` of the
    later minus ``answered_at`` of the earlier, the bound a reader can take from the log; and
    that bound is >= 300.002 s. Twelve hours of the exact Lambda schedule with 2 % spikes."""
    vps = [RAINIER, PORE]
    run = run_schedule(data_root, vps, hours=12, schedule="lambda", latency=Latency(spike_p=0.02))
    requests = [r for r in _records(data_root) if r["event"] == "request"]
    for vp in vps:
        stage = "json" if vp.truth_camera.kind == "ard-aircam" else "head"
        polls = [r for r in requests if r["viewpoint_id"] == vp.id and r["stage"] == stage]
        server = run.nps.receipts(vp.id, "poll")
        assert len(polls) == len(server)
        for (a, b), (ra, rb) in zip(
            itertools.pairwise(polls), itertools.pairwise(server), strict=True
        ):
            provable = (_utc(b["sent_at"]) - _utc(a["answered_at"])).total_seconds()
            assert provable >= MIN_POLL_INTERVAL_S + POLL_MARGIN_S - 1e-9
            assert (rb - ra).total_seconds() >= provable - 1e-3
    assert server_violations(run.nps, vps) == []
    assert any(r.received - r.sent > timedelta(seconds=2) for r in run.nps.received)  # spikes


# --- a failure nps.gov may still receive after it ------------------------------------------------


def _late_receipt_runner(storage, clock, received: list[datetime], *, late_s: float):
    """A JSON poll whose first request gets no answer: the client waits its 30 s read timeout
    and gives up, and nps.gov (a front end, a backlog) hands the request on ``late_s`` after
    that. Later polls are answered at once with a 304."""

    def handler(request: httpx.Request) -> httpx.Response:
        if not received:
            clock.advance(30.0)
            received.append(clock.now() + timedelta(seconds=late_s))
            raise httpx.ReadTimeout("timed out", request=request)
        received.append(clock.now())
        return httpx.Response(304)

    return CaptureRunner(
        storage,
        polite_client(transport=httpx.MockTransport(handler)),
        clock=clock.now,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


def test_a_poll_that_got_no_answer_counts_as_answered_300_s_after_it_failed(data_root):
    """Review finding (20 Nov 2026): a request that failed after it may have left (a read
    timeout, a reset, Ctrl-C) was answered "when it failed", but nothing shows nps.gov had it
    by then: here a front end hands it to nps.gov 250 s after the client gave up. It now counts
    as answered FAILED_AFTER_SEND_BOUND_S (300 s) after the failure, so the next poll reaches
    nps.gov >= 300 s after that late receipt. Counted from the failure (and its 5-minute
    back-off, which falls at the same moment), the next poll came 50 s after it."""
    clock = FakeClock(T0)
    storage = LocalStorage(data_root)
    received: list[datetime] = []
    out = _late_receipt_runner(storage, clock, received, late_s=250.0).run_pass([PORE])
    assert out["outcomes"][PORE.id]["event"] == "error"
    state = _state(data_root, PORE)
    failed_at = T0 + timedelta(seconds=30)
    assert state.last_answer_bound == "failed_after_send"
    assert _utc(state.last_answer_at) == failed_at + timedelta(seconds=FAILED_AFTER_SEND_BOUND_S)
    (record,) = [r for r in _records(data_root) if r["event"] == "request"]
    assert record["answered_at"] == "2026-11-21T00:00:30.000Z"  # the reading itself
    assert record["answer_bound"] == "failed_after_send"
    assert record["answer_bound_at"] == state.last_answer_at
    for _ in range(14):  # a loop's passes, 60 s apart, each waiting up to 60 s for a slot
        clock.advance(60.0)
        _late_receipt_runner(storage, clock, received, late_s=250.0).run_pass(
            [PORE], wait_for_due_s=60.0
        )
    late, after = received[0], received[1]
    assert (after - late).total_seconds() >= MIN_POLL_INTERVAL_S, (late, after)
    assert after >= failed_at + timedelta(seconds=FAILED_AFTER_SEND_BOUND_S + 300.002)
    assert _state(data_root, PORE).last_answer_bound is None  # the answered poll clears it


class _Script(httpcore.MockStream):
    """httpcore's mock stream: the given reads, then a read timeout (nothing more arrives)."""

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        if not self._buffer:
            raise httpcore.ReadTimeout("timed out")
        return self._buffer.pop(0)


class _ScriptBackend(httpcore.MockBackend):
    def connect_tcp(self, *args: Any, **kwargs: Any) -> httpcore.NetworkStream:
        return _Script(list(self._buffer))


@pytest.mark.parametrize(
    ("reads", "bounded"),
    [
        ([], True),  # no response head: nps.gov may still get it
        ([b"HTTP/1.1 200 OK\r\nContent-Length: 40\r\n\r\n{"], False),  # it answered
    ],
    ids=["no_answer", "answer_then_body_timeout"],
)
def test_through_httpx_only_a_failure_before_any_answer_is_bounded(data_root, reads, bounded):
    """Through the installed httpx and httpcore: a read timeout before the response head
    counts as answered 300 s later; one in the middle of the body does not (the head proves
    nps.gov had the request), and neither does a request that could not connect."""
    clock = FakeClock(T0)
    storage = LocalStorage(data_root)
    run = CaptureRunner(
        storage,
        _client_on(_ScriptBackend(reads)),
        clock=clock.now,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    out = run.run_pass([PORE])["outcomes"][PORE.id]
    assert out["event"] == "error" and "ReadTimeout" in out["error"]
    state = _state(data_root, PORE)
    (record,) = [r for r in _records(data_root) if r["event"] == "request"]
    if bounded:
        assert state.last_answer_bound == "failed_after_send"
        assert _utc(state.last_answer_at) - _utc(record["answered_at"]) == timedelta(seconds=300)
    else:
        assert state.last_answer_bound is None and "answer_bound" not in record
        assert state.last_answer_at == record["answered_at"]


def test_an_image_get_that_got_no_answer_is_counted_by_the_cap_300_s_later(data_root):
    """The same bound for an image GET: its record's answer (what the cap and the pacing
    count) is the failure + 300 s, marked ``answer_bound: failed_after_send``."""
    clock = FakeClock(T0)
    storage = LocalStorage(data_root)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"ETag": '"new"', "Last-Modified": "x"})
        clock.advance(30.0)
        raise httpx.ReadTimeout("timed out", request=request)

    run = CaptureRunner(
        storage,
        polite_client(transport=httpx.MockTransport(handler)),
        clock=clock.now,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    out = run.run_pass([RAINIER])["outcomes"][RAINIER.id]
    assert out["event"] == "error" and out["stage"] == "image"
    (get,) = _state(data_root, RAINIER).gets
    image = [r for r in _records(data_root) if r["event"] == "request" and r["stage"] == "image"]
    assert get["answer_bound"] == "failed_after_send" and get["status"] == "error"
    assert _utc(get["answered_at"]) == _utc(image[0]["answered_at"]) + timedelta(seconds=300)


# --- redirects ------------------------------------------------------------------------------------


def test_a_redirect_is_never_followed_so_nps_gov_sees_one_request_per_logged_request(data_root):
    """Review finding (20 Nov 2026): the polite client followed up to 5 redirects silently, so
    one logged poll could be two to six requests at nps.gov, none of them seen by the log, the
    state or the caps. The live client now follows none: a 301 is an error like any other
    (logged with its ``Location``, backed off), and nps.gov gets exactly what the log shows."""
    clock = FakeClock(T0)
    storage = LocalStorage(data_root)
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append((request.method, url))
        moved = url.replace("/webcams/", "/webcams/v2/").replace("/webcams-mora/", "/v2/")
        return httpx.Response(301, headers={"Location": moved})

    with CaptureRunner.default(storage, transport=httpx.MockTransport(handler)) as run:
        run.clock, run.sleep, run.monotonic = clock.now, clock.sleep, clock.monotonic
        outcomes = run.run_pass([RAINIER, PORE])["outcomes"]
    assert {vp: o["status"] for vp, o in outcomes.items()} == {RAINIER.id: 301, PORE.id: 301}
    requests = [r for r in _records(data_root) if r["event"] == "request"]
    assert len(seen) == len(requests) == 2
    assert sorted(r["url"] for r in requests) == sorted(url for _, url in seen)
    errors = [r for r in _records(data_root) if r["event"] == "error"]
    assert sorted(e["location"] for e in errors) == sorted(r["location"] for r in requests)
    assert all("/v2/" in e["location"] for e in errors)
    assert _state(data_root, PORE).last_error["location"].endswith("/v2/json/porejson.txt")


# --- the poll's reservation and older code (a rollback) -------------------------------------------


def test_after_a_crash_between_send_and_save_older_code_still_waits_5_minutes(data_root):
    """Review finding (20 Nov 2026): code before 0.2.1 (the 0.2.0 snapshot a rollback would
    start) knows no reservation and polls 301 s after ``last_poll_at``. Killed between a
    poll's send and its save, 0.2.1 left the previous poll's ``last_poll_at``, so that code
    polled again at once (1 s later at nps.gov, in the review's run of the real 0.2.0 code).
    While a poll is reserved, ``last_poll_at`` now holds the reservation + 60 s, the latest
    moment the reserved poll can reach nps.gov; 0.2.0's own rule then gives >= 300 s."""
    clock = FakeClock(T0)
    killer = Killer(random.Random(1), rate=1.0, armed=False)
    nps = ReceiptNPS([PORE], clock, killer=killer, kill_after="received")
    storage = KillableStorage(data_root, killer)
    _runner(storage, nps, clock).run_pass([PORE])
    clock.current = T0 + timedelta(seconds=301)
    killer.armed = True
    with pytest.raises(Crash):  # killed after nps.gov received the poll, before the save
        _runner(storage, nps, clock).run_pass([PORE])
    crashed = nps.receipts(PORE.id, "poll")[-1]
    state = _state(data_root, PORE)
    older_code_next_send = _utc(state.last_poll_at) + timedelta(seconds=301.0)  # 0.2.0's rule
    assert (older_code_next_send - crashed).total_seconds() >= MIN_POLL_INTERVAL_S
    reserved = _utc(state.poll_reserved_at)
    assert _utc(state.last_poll_at) == reserved + timedelta(seconds=UNANSWERED_REQUEST_BOUND_S)
    # This code reads the reservation itself: answered at the same bound.
    killer.dead, killer.armed = False, False
    clock.advance(5.0)
    out = _runner(storage, nps, clock).run_pass([PORE])["outcomes"][PORE.id]
    assert out["event"] == "not_due"
    settled = _state(data_root, PORE)
    assert settled.last_answer_bound == "unanswered_reservation"
    assert _utc(settled.last_answer_at) == reserved + timedelta(seconds=60)


def test_a_poll_given_up_before_its_send_puts_its_last_poll_at_back(data_root):
    """A camera waited for reserves its poll shortly before the slot; if the wait is then
    given up (here the wall clock stalls), nothing was sent: the reservation is dropped and
    ``last_poll_at`` is the previous poll's send again."""
    storage = LocalStorage(data_root)
    stalled = T0 + timedelta(seconds=290)
    previous = CameraState(
        viewpoint_id=PORE.id,
        created_at="2026-11-21T00:00:00Z",
        last_poll_at="2026-11-21T00:00:00.000Z",
        last_answer_at="2026-11-21T00:00:00.100Z",
    )
    storage.write_json(camera_state_key(PORE.id), previous.to_dict())
    mono = [0.0]
    run = CaptureRunner(
        storage,
        polite_client(transport=httpx.MockTransport(lambda request: httpx.Response(500))),
        clock=lambda: stalled,
        sleep=lambda seconds: mono.__setitem__(0, mono[0] + max(seconds, 1e-6)),
        monotonic=lambda: mono[0],
    )
    out = run.run_pass([PORE], wait_for_due_s=30.0)["outcomes"][PORE.id]
    assert out["event"] == "not_due"
    state = _state(data_root, PORE)
    assert state.poll_reserved_at is None
    assert state.last_poll_at == "2026-11-21T00:00:00.000Z"


# --- the send deadline across a system sleep ------------------------------------------------------


def test_a_system_sleep_between_the_reservation_and_the_send_abandons_the_request(data_root):
    """Review finding (20 Nov 2026): the send deadline was read on ``time.monotonic()``,
    ``mach_absolute_time()`` on macOS, which stops while the Mac sleeps, while the 60 s bound
    it justifies is applied in wall time. Here the Mac sleeps an hour during the reservation's
    state write (the wall clock moves on, that clock does not): the request must not go out an
    hour after its reservation. It is abandoned unsent (the deadline is checked on the wall
    clock as well), and nothing reaches nps.gov."""
    clock = FakeClock(T0)
    uptime = [0.0]  # mach_absolute_time: stands still while the Mac sleeps
    nps = ReceiptNPS([PORE], clock)
    armed = [True]

    class SleepsDuringTheReservation(LocalStorage):
        def write_json(self, key: str, *a: Any, **k: Any) -> Any:
            if key.startswith("state/capture/") and armed[0]:
                armed[0] = False
                clock.advance(3600.0)
            return super().write_json(key, *a, **k)

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        uptime[0] += seconds

    run = CaptureRunner(
        SleepsDuringTheReservation(data_root),
        polite_client(transport=nps.transport()),
        clock=clock.now,
        sleep=sleep,
        monotonic=lambda: uptime[0],
    )
    out = run.run_pass([PORE])["outcomes"][PORE.id]
    assert out["event"] == "error" and "SendDeadlinePassed" in out["error"]
    assert nps.received == []


def test_live_capture_reads_a_monotonic_clock_that_counts_system_sleep(data_root):
    """The in-process clock (send deadlines, the guard against wall-clock steps, request
    spacing) counts the time the machine sleeps: CLOCK_MONOTONIC_RAW on macOS (Python's
    ``time.monotonic()`` there is ``mach_absolute_time()``, which stops during sleep),
    CLOCK_BOOTTIME on Linux."""
    run = CaptureRunner(LocalStorage(data_root), httpx.Client())
    with run:
        assert run.monotonic is steady_clock
    if sys.platform == "darwin":
        assert time.get_clock_info("monotonic").implementation == "mach_absolute_time()"
        assert abs(steady_clock() - time.clock_gettime(time.CLOCK_MONOTONIC_RAW)) < 1.0
    elif hasattr(time, "CLOCK_BOOTTIME"):
        assert abs(steady_clock() - time.clock_gettime(time.CLOCK_BOOTTIME)) < 1.0


# --- the total deadline ---------------------------------------------------------------------------


def test_an_image_body_that_trickles_is_abandoned_at_its_total_deadline(data_root):
    """Review finding (20 Nov 2026): httpx's 30 s read timeout is per read, so a body arriving
    one chunk every 25 s kept a camera's visit, and an S3 camera lock (TTL 300 s), going for
    as long as the server liked. An image GET now gives up LIVE_IMAGE_DEADLINE_S (60 s) after
    it started, at the first chunk past it; the response head had come, so its answer is the
    moment it was abandoned (no extra bound)."""
    clock = FakeClock(T0)
    storage = LocalStorage(data_root)
    body = make_jpeg()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"ETag": '"new"', "Last-Modified": "x"})

        def trickle():
            for i in range(0, len(body), 200):
                clock.advance(25.0)
                yield body[i : i + 200]

        return httpx.Response(200, content=trickle(), headers={"ETag": '"new"'})

    run = CaptureRunner(
        storage,
        polite_client(transport=httpx.MockTransport(handler)),
        clock=clock.now,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    summary = run.run_pass([RAINIER])
    out = summary["outcomes"][RAINIER.id]
    assert out["event"] == "error" and "RequestDeadlinePassed" in out["error"]
    image = [r for r in _records(data_root) if r["event"] == "request" and r["stage"] == "image"]
    (record,) = image
    took = (_utc(record["answered_at"]) - _utc(record["sent_at"])).total_seconds()
    assert LIVE_IMAGE_DEADLINE_S < took <= LIVE_IMAGE_DEADLINE_S + 25.0 + 0.002, took
    assert "answer_bound" not in record  # the head had arrived: nps.gov had the request
    assert summary["duration_s"] < 120.0  # the visit, lock held, ends well inside 300 s


# --- the Lambda execution environment -------------------------------------------------------------


def test_on_lambda_each_request_names_its_execution_environment(data_root, monkeypatch):
    """Every Lambda invocation reads answer stamps another invocation may have written on
    another host's clock, with only the 2 ms margin between them. Each request record names the
    execution environment's log stream, so pairs that cross environments can be checked once
    the Lambda runs; outside Lambda the field is absent."""
    clock = FakeClock(T0)
    nps = ReceiptNPS([PORE], clock)
    monkeypatch.setenv("AWS_LAMBDA_LOG_STREAM_NAME", "2026/11/21/[$LATEST]0123456789abcdef")
    _runner(LocalStorage(data_root), nps, clock).run_pass([PORE])
    monkeypatch.delenv("AWS_LAMBDA_LOG_STREAM_NAME")
    clock.advance(301.0)
    _runner(LocalStorage(data_root), nps, clock).run_pass([PORE])
    polls = [r for r in _records(data_root) if r["event"] == "request" and r["stage"] == "json"]
    assert polls[0]["lambda_log_stream"] == "2026/11/21/[$LATEST]0123456789abcdef"
    assert "lambda_log_stream" not in polls[1]
