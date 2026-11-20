from __future__ import annotations

import itertools
import json
import math
import multiprocessing
import threading
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from lookout_common import http as lh
from lookout_common import timeutil
from lookout_common.http import MinIntervalLimiter
from lookout_common.timeutil import utcnow


# --- polite client ----------------------------------------------------------------------
def _recording_transport(seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "/final"})
        return httpx.Response(200, headers={"Set-Cookie": "session=abc; Path=/"}, text="ok")

    return httpx.MockTransport(handler)


def test_default_user_agent_is_exact():
    assert lh.DEFAULT_USER_AGENT == "Lookout/0.1 (webcam research capture; fetch-on-change)"
    seen: list[httpx.Request] = []
    with lh.polite_client(transport=_recording_transport(seen)) as client:
        client.get("https://www.nps.gov/x")
    assert seen[0].headers["User-Agent"] == lh.DEFAULT_USER_AGENT


def test_user_agent_env_override_and_header_cannot_replace_it(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOOKOUT_USER_AGENT", "Lookout/0.2 (ci)")
    seen: list[httpx.Request] = []
    with lh.polite_client(
        transport=_recording_transport(seen), headers={"user-agent": "Mozilla", "X-Test": "1"}
    ) as client:
        client.get("https://example.org/")
    assert seen[0].headers["User-Agent"] == "Lookout/0.2 (ci)"
    assert seen[0].headers["X-Test"] == "1"
    monkeypatch.setenv("LOOKOUT_USER_AGENT", "   ")
    assert lh.user_agent() == lh.DEFAULT_USER_AGENT


def test_a_per_request_header_cannot_replace_the_ua():
    """Review finding: only constructor headers were filtered before."""
    seen: list[httpx.Request] = []
    with lh.polite_client(transport=_recording_transport(seen)) as client:
        client.get("https://www.nps.gov/x", headers={"User-Agent": "Mozilla/5.0 owner@example"})
        request = client.build_request("GET", "https://www.nps.gov/y")
        request.headers["user-agent"] = "curl/8"
        client.send(request)
    assert [r.headers["User-Agent"] for r in seen] == [lh.DEFAULT_USER_AGENT] * 2


def test_redirect_carries_no_cookie_and_keeps_the_ua():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "www.nps.gov":
            return httpx.Response(
                302,
                headers={"Location": "https://other.example/final", "Set-Cookie": "s=1; Path=/"},
            )
        return httpx.Response(200, text="ok")

    with lh.polite_client(transport=httpx.MockTransport(handler)) as client:
        response = client.get("https://www.nps.gov/start", headers={"User-Agent": "Mozilla"})
    assert response.status_code == 200 and [r.url.host for r in seen] == [
        "www.nps.gov",
        "other.example",
    ]
    assert all(r.headers["User-Agent"] == lh.DEFAULT_USER_AGENT for r in seen)
    assert "cookie" not in {k.lower() for k in seen[1].headers}


def test_redirects_are_capped_at_five():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": f"/r{len(seen)}"})

    with (
        lh.polite_client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(httpx.TooManyRedirects),
    ):
        client.get("https://www.nps.gov/r0")
    assert len(seen) == 6  # the request and five redirects, never a seventh


def test_cookies_are_never_kept():
    seen: list[httpx.Request] = []
    with lh.polite_client(transport=_recording_transport(seen)) as client:
        client.get("https://www.nps.gov/a")
        client.get("https://www.nps.gov/b")
        assert len(client.cookies) == 0
    assert "cookie" not in {k.lower() for k in seen[1].headers}


def test_timeouts_and_redirects():
    seen: list[httpx.Request] = []
    with lh.polite_client(transport=_recording_transport(seen)) as client:
        assert client.timeout.connect == 10.0 and client.timeout.read == 30.0
        response = client.get("https://www.nps.gov/redirect")
    assert response.status_code == 200 and str(response.url).endswith("/final")
    with lh.polite_client(timeout=5.0) as client:
        assert client.timeout.read == 5.0


def test_retry_after_parsing():
    assert lh.retry_after_s(httpx.Response(429, headers={"Retry-After": "120"})) == 120.0
    when = format_datetime(utcnow() + timedelta(seconds=90), usegmt=True)
    got = lh.retry_after_s(httpx.Response(503, headers={"Retry-After": when}))
    assert got is not None and 80 <= got <= 91
    assert lh.retry_after_s(httpx.Response(503, headers={"Retry-After": "soon"})) is None
    assert lh.retry_after_s(httpx.Response(503)) is None


def test_backoff_delay():
    assert lh.backoff_delay(0) == 0.0
    assert [lh.backoff_delay(n, base_s=60) for n in (1, 2, 3)] == [60.0, 120.0, 240.0]
    assert lh.backoff_delay(50, base_s=60, cap_s=3600) == 3600.0


# --- limiter -------------------------------------------------------------------------------
class FakeClock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _limiter(tmp_path: Path, clock: FakeClock, interval: float = 5.0) -> MinIntervalLimiter:
    return MinIntervalLimiter(
        "nps-archive", interval, state_dir=tmp_path, mode="file", clock=clock, sleep=clock.sleep
    )


def test_wait_spaces_requests(tmp_path: Path):
    clock = FakeClock()
    limiter = _limiter(tmp_path, clock)
    assert limiter.wait() == 0.0
    assert limiter.wait() == pytest.approx(5.0)
    clock.now += 60
    assert limiter.wait() == 0.0
    state = json.loads((tmp_path / "ratelimit" / "nps-archive.json").read_text())
    assert state["last_unix"] == clock.now and state["min_interval_s"] == 5.0


def test_state_is_shared_between_instances(tmp_path: Path):
    clock = FakeClock()
    _limiter(tmp_path, clock).wait()
    clock.now += 2
    assert _limiter(tmp_path, clock).next_allowed_in() == pytest.approx(3.0)


def test_hold_spaces_end_to_start_and_records_finish(tmp_path: Path):
    clock = FakeClock()
    limiter = _limiter(tmp_path, clock)
    with limiter.hold() as waited:
        assert waited == 0.0
        clock.now += 20  # a slow request
    assert limiter.next_allowed_in() == pytest.approx(5.0)
    with pytest.raises(RuntimeError, match="boom"), limiter.hold():
        raise RuntimeError("boom")
    assert limiter.next_allowed_in() == pytest.approx(5.0)  # finish recorded despite the error


def test_defer_inside_hold_backs_off_everyone(tmp_path: Path):
    clock = FakeClock()
    limiter = _limiter(tmp_path, clock)
    with limiter.hold():
        limiter.defer(600)  # e.g. a 503: must not deadlock
        assert limiter.next_allowed_in() == 0.0
        with pytest.raises(RuntimeError):
            limiter.wait()
        with pytest.raises(RuntimeError), limiter.hold():
            pass
    other = _limiter(tmp_path, clock)
    assert other.next_allowed_in() == pytest.approx(600.0)
    assert other.wait() == pytest.approx(600.0)


def test_defer_outside_hold(tmp_path: Path):
    clock = FakeClock()
    limiter = _limiter(tmp_path, clock)
    limiter.defer(120)
    assert limiter.next_allowed_in() == pytest.approx(120.0)
    limiter.defer(30)  # a shorter defer never shortens an existing one
    assert limiter.next_allowed_in() == pytest.approx(120.0)


def test_clock_stepping_back_caps_the_wait(tmp_path: Path):
    """Capped at the bound, two intervals (D-077); next_allowed_in() now says what wait()
    takes (review: it said one interval while wait() took two)."""
    clock = FakeClock()
    limiter = _limiter(tmp_path, clock)
    limiter.wait()
    clock.now -= 3600
    assert limiter.next_allowed_in() == pytest.approx(10.0)
    assert limiter.wait() == pytest.approx(10.0)


def test_corrupt_state_assumes_the_longest_back_off_and_keeps_it(tmp_path: Path):
    """Review finding: an unreadable state file used to drop an active back-off."""
    clock = FakeClock()
    limiter = _limiter(tmp_path, clock)
    limiter.defer(3600)
    state = tmp_path / "ratelimit" / "nps-archive.json"
    state.write_text(state.read_text()[:20])  # torn: the back-off is unreadable
    assert limiter.next_allowed_in() == pytest.approx(lh.BACKOFF_CAP_S)
    clock.now += 600
    assert limiter.next_allowed_in() == pytest.approx(lh.BACKOFF_CAP_S)  # still unreadable
    limiter.defer(0)  # any locked call writes the conservative state back ...
    repaired = json.loads(state.read_text())
    assert repaired["not_before_unix"] == pytest.approx(clock.now + lh.BACKOFF_CAP_S)
    clock.now += 1800
    assert limiter.next_allowed_in() == pytest.approx(lh.BACKOFF_CAP_S - 1800)  # ... so it ends
    assert limiter.wait() == pytest.approx(lh.BACKOFF_CAP_S - 1800)


def test_corrupt_state_hold_off_is_configurable(tmp_path: Path):
    clock = FakeClock()
    limiter = MinIntervalLimiter(
        "x",
        5.0,
        state_dir=tmp_path,
        mode="file",
        clock=clock,
        sleep=clock.sleep,
        corrupt_hold_off_s=60.0,
    )
    (tmp_path / "ratelimit" / "x.json").write_text('{"last_unix": "soon"}')
    assert limiter.next_allowed_in() == pytest.approx(60.0)
    (tmp_path / "ratelimit" / "x.json").write_text('{"last_unix": NaN, "not_before_unix": 0}')
    assert limiter.next_allowed_in() == pytest.approx(60.0)
    with pytest.raises(ValueError):
        MinIntervalLimiter("x", 5.0, state_dir=tmp_path, mode="file", corrupt_hold_off_s=-1)


def test_process_mode_is_shared_in_process():
    clock = FakeClock()
    a = MinIntervalLimiter("lambda-test", 2.0, mode="process", clock=clock, sleep=clock.sleep)
    b = MinIntervalLimiter("lambda-test", 2.0, mode="process", clock=clock, sleep=clock.sleep)
    assert a.wait() == 0.0
    assert b.wait() == pytest.approx(2.0)


def test_auto_picks_process_mode_only_on_lambda(monkeypatch: pytest.MonkeyPatch, data_root: Path):
    """Review finding: LOOKOUT_STORAGE=s3 on this laptop used to drop cross-process spacing."""
    monkeypatch.setenv("LOOKOUT_STORAGE", "s3")
    assert MinIntervalLimiter("auto-laptop", 1.0).mode == "file"
    assert (data_root / "state" / "ratelimit").is_dir()
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "lookout-capture")
    assert lh.on_aws_lambda()
    assert MinIntervalLimiter("auto-lambda", 1.0).mode == "process"
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "  ")
    assert not lh.on_aws_lambda()


def test_auto_mode_uses_the_data_dir_state(data_root: Path):
    limiter = MinIntervalLimiter("nps-live", 0.0)
    assert limiter.mode == "file"
    limiter.wait()
    assert (data_root / "state" / "ratelimit" / "nps-live.json").is_file()


@pytest.mark.parametrize("name", ["", "NPS", "a b", "../x", "x/"])
def test_limiter_name_validation(tmp_path: Path, name: str):
    with pytest.raises(ValueError):
        MinIntervalLimiter(name, 1.0, state_dir=tmp_path)


def _spaced_worker(state_dir: str, out: str, rounds: int) -> None:
    limiter = MinIntervalLimiter("xproc", 0.25, state_dir=state_dir, mode="file")
    for _ in range(rounds):
        with limiter.hold():
            start = time.time()
            time.sleep(0.02)
            end = time.time()
        with open(out, "a") as handle:
            handle.write(f"{start} {end}\n")


def test_hold_serialises_and_spaces_real_processes(tmp_path: Path):
    """Real wall clock, three processes: requests never overlap and are >= 0.25 s apart."""
    out = tmp_path / "times.txt"
    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(target=_spaced_worker, args=(str(tmp_path), str(out), 2)) for _ in range(3)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
        assert proc.exitcode == 0
    spans = sorted(tuple(map(float, line.split())) for line in out.read_text().splitlines())
    assert len(spans) == 6
    gaps = [b_start - a_end for (_, a_end), (b_start, _) in itertools.pairwise(spans)]
    assert min(gaps) >= 0.24, gaps


def _auto_worker(data_dir: str, out: str) -> None:
    import os

    os.environ["LOOKOUT_DATA_DIR"] = data_dir
    os.environ["LOOKOUT_STORAGE"] = "s3"  # S3 storage on this laptop, not on Lambda
    os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
    limiter = MinIntervalLimiter("probe-auto", 0.3)  # the default mode="auto"
    for _ in range(2):
        with limiter.hold():
            start = time.time()
            time.sleep(0.01)
            end = time.time()
        with open(out, "a") as handle:
            handle.write(f"{start} {end} {limiter.mode}\n")


def test_auto_mode_with_s3_storage_still_spaces_processes_on_one_machine(tmp_path: Path):
    """Review finding: two processes started requests microseconds apart under auto + S3."""
    out = tmp_path / "times.txt"
    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(target=_auto_worker, args=(str(tmp_path / "data"), str(out))) for _ in range(2)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
        assert proc.exitcode == 0
    rows = sorted(line.split() for line in out.read_text().splitlines())
    assert {mode for *_, mode in rows} == {"file"} and len(rows) == 4
    spans = sorted((float(start), float(end)) for start, end, _ in rows)
    gaps = [b_start - a_end for (_, a_end), (b_start, _) in itertools.pairwise(spans)]
    assert min(gaps) >= 0.29, gaps


def _deferring_holder(state_dir: str, ready: str, out: str) -> None:
    limiter = MinIntervalLimiter("probe-hold", 0.05, state_dir=state_dir, mode="file")
    limiter.defer(1.5)
    Path(ready).write_text(str(time.time()))
    with limiter.hold():
        Path(out).write_text(str(time.time()))


def test_back_off_sleep_does_not_hold_the_lock_across_processes(tmp_path: Path):
    """Review finding: hold() slept under the flock, so other processes' calls blocked."""
    ready, out = tmp_path / "ready.txt", tmp_path / "started.txt"
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_deferring_holder, args=(str(tmp_path), str(ready), str(out)))
    proc.start()
    deadline = time.time() + 30
    while not ready.exists() and time.time() < deadline:
        time.sleep(0.01)
    time.sleep(0.3)  # the holder is now waiting out its 1.5 s back-off inside hold()
    limiter = MinIntervalLimiter("probe-hold", 0.05, state_dir=tmp_path, mode="file")
    began = time.time()
    remaining = limiter.next_allowed_in()
    limiter.defer(0.0)  # takes the lock: must not wait for the holder's back-off
    blocked = time.time() - began
    proc.join(30)
    assert proc.exitcode == 0
    assert blocked < 0.5, blocked
    assert 0.3 < remaining < 1.3, remaining
    started, deferred_at = float(out.read_text()), float(ready.read_text())
    assert started - deferred_at >= 1.45  # the holder still honoured the back-off


# --- both clocks after every sleep (D-077) --------------------------------------------------
#: 20 Nov 2026: an archive request ended at 09:56:47.412 UTC on the log's stamps; the next one
#: waited 4.987 s and started at 09:56:52.406, 4.9944 s later (D-077).
D077_END = datetime(2026, 11, 20, 9, 56, 47, 412000, tzinfo=UTC).timestamp()
D077_WAITED = 4.987


class Machine:
    """One machine's clocks: the wall clock (``time``) and real elapsed time (``monotonic``).

    ``sleep(s)`` lets ``s`` of real time pass, as ``time.sleep`` does on a monotonic clock,
    while the wall clock advances by ``s * wall_rate`` (a slew) plus the next queued
    ``steps`` entry (a step during that sleep). ``advance`` moves both clocks together.
    """

    def __init__(self, wall: float) -> None:
        self.wall = wall
        self.real = 50_000.0
        self.wall_rate = 1.0
        self.steps: list[float] = []
        self.slept: list[float] = []

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.real

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        if seconds > 0:
            self.real += seconds
            self.wall += seconds * self.wall_rate + (self.steps.pop(0) if self.steps else 0.0)

    def advance(self, seconds: float) -> None:
        self.real += seconds
        self.wall += seconds

    def stamp(self) -> tuple[float, float]:
        return self.wall, self.real


def _on(machine: Machine, tmp_path: Path, **kwargs: float) -> MinIntervalLimiter:
    """An ``nps-archive`` file-mode limiter on ``machine``: one "process" per instance."""
    return MinIntervalLimiter(
        "nps-archive",
        5.0,
        state_dir=tmp_path,
        mode="file",
        clock=machine.time,
        sleep=machine.sleep,
        monotonic=machine.monotonic,
        **kwargs,
    )


def _d077_first_request(tmp_path: Path) -> tuple[Machine, MinIntervalLimiter, tuple]:
    """A request that ends at D077_END; the next turn reads the state 13 ms later."""
    machine = Machine(D077_END - 0.2656)
    limiter = _on(machine, tmp_path)
    with limiter.hold():
        machine.advance(0.2656)  # the request runs
    ended = machine.stamp()
    machine.advance(5.0 - D077_WAITED)  # the queue's own work before its next request
    return machine, limiter, ended


@pytest.mark.parametrize("loss", ["slew", "step back"])
def test_d077_a_wall_clock_that_loses_6_ms_in_the_sleep_cannot_start_a_request_early(
    tmp_path: Path, loss: str
):
    """HEAD let the request start as soon as its sleep ended: 4.994 s on the wall clock."""
    machine, limiter, ended = _d077_first_request(tmp_path)
    if loss == "slew":
        machine.wall_rate = 1.0 - 0.006 / D077_WAITED
    else:
        machine.steps = [-0.006]
    with limiter.hold() as waited:
        started = machine.stamp()
    assert started[0] - ended[0] >= 5.0, started[0] - ended[0]  # the wall clock: the log
    assert started[1] - ended[1] >= 5.0  # real time
    assert waited == pytest.approx(D077_WAITED + 0.006, abs=1e-4)
    assert machine.slept[0] == pytest.approx(D077_WAITED)  # the same first sleep as before


def test_d077_wait_spaces_starts_on_the_wall_clock_too(tmp_path: Path):
    machine = Machine(D077_END)
    limiter = _on(machine, tmp_path)
    assert limiter.wait() == 0.0
    first = machine.stamp()
    machine.advance(0.2656)
    machine.steps = [-0.006]
    limiter.wait()
    assert machine.wall - first[0] >= 5.0, machine.wall - first[0]
    assert machine.real - first[1] >= 5.0


@pytest.mark.parametrize("step", [-0.5, -3.0, -4.99])
def test_a_step_back_of_up_to_one_interval_in_the_sleep_is_waited_out(tmp_path: Path, step):
    machine, limiter, ended = _d077_first_request(tmp_path)
    machine.steps = [step]
    with limiter.hold() as waited:
        started = machine.stamp()
    assert started[0] - ended[0] >= 5.0 and started[1] - ended[1] >= 5.0
    assert waited == pytest.approx(D077_WAITED - step) and waited <= 2 * 5.0


@pytest.mark.parametrize("step", [-5.5, -60.0, -3600.0])
def test_a_step_back_of_more_than_one_interval_waits_at_most_one_interval_more(
    tmp_path: Path, step
):
    """The bound: rule 1 gives way one interval after the wait the state asked for."""
    machine, limiter, ended = _d077_first_request(tmp_path)
    began = machine.real
    machine.steps = [step]
    with limiter.hold() as waited:
        started_real = machine.real
    assert started_real - began == pytest.approx(D077_WAITED + 5.0)
    assert waited == pytest.approx(D077_WAITED + 5.0) and waited <= 2 * 5.0
    assert started_real - ended[1] >= 5.0  # real time is never short


def test_a_wall_clock_stepped_back_between_requests_waits_two_intervals_at_most(
    tmp_path: Path,
):
    machine, limiter, _ = _d077_first_request(tmp_path)
    machine.wall -= 3600.0  # the stored time is now an hour in the future
    assert limiter.next_allowed_in() == pytest.approx(10.0)  # the wait hold() takes: the bound
    began = machine.real
    with limiter.hold() as waited:
        started_real = machine.real
        machine.advance(0.3)  # the request runs
    assert waited == pytest.approx(10.0) and started_real - began == pytest.approx(10.0)
    # A small step back between requests is still honoured on the wall clock.
    ended_wall = machine.wall
    machine.advance(0.013)
    machine.wall -= 2.0
    with limiter.hold() as waited:
        started_wall = machine.wall
    assert started_wall - ended_wall >= 5.0 and waited <= 2 * 5.0


def test_a_frozen_wall_clock_still_lets_the_request_start_within_the_bound(tmp_path: Path):
    machine, limiter, _ = _d077_first_request(tmp_path)
    machine.wall_rate = 0.0  # the wall clock stops
    began = machine.real
    with limiter.hold() as waited:
        started_real = machine.real
    assert started_real - began == pytest.approx(D077_WAITED + 5.0) and waited <= 2 * 5.0


def test_a_wall_clock_step_forward_never_shortens_the_real_gap(tmp_path: Path):
    """HEAD started 2 s (real) after the previous request ended."""
    machine, limiter, ended = _d077_first_request(tmp_path)
    machine.wall += 3.0  # stepped forward between requests
    with limiter.hold() as waited:
        started = machine.stamp()
    assert started[1] - ended[1] >= 5.0, started[1] - ended[1]
    assert waited == pytest.approx(D077_WAITED)


def test_a_step_forward_during_the_sleep_never_shortens_the_real_gap(tmp_path: Path):
    """An invariant HEAD kept too: the first sleep is the whole wait asked for. Times are
    binary-exact, so float rounding cannot decide it (review: with D-077's times it failed on
    HEAD only by 11 ns of rounding). Rule 3's own guard is the early-sleep test further down."""
    machine = Machine(1_800_000_000.0)
    limiter = _on(machine, tmp_path)
    with limiter.hold():
        machine.advance(0.25)
    ended = machine.stamp()
    machine.advance(0.0625)
    machine.steps = [3.0]
    with limiter.hold() as waited:
        started = machine.stamp()
    assert started[1] - ended[1] == 5.0 and waited == 4.9375


def test_wait_keeps_real_start_spacing_when_the_wall_clock_jumps_forward(tmp_path: Path):
    machine = Machine(D077_END)
    limiter = _on(machine, tmp_path)
    limiter.wait()
    first_real = machine.real
    machine.wall += 4.0
    assert limiter.wait() == pytest.approx(5.0)
    assert machine.real - first_real >= 5.0


def test_two_processes_share_only_the_wall_clock_state(tmp_path: Path):
    """Two file-mode instances, as two processes: every pair is >= 5 s on the wall clock,
    with D-077's 6 ms lost in each sleep; each instance also keeps its own real gap."""
    machine = Machine(D077_END)
    a, b = _on(machine, tmp_path), _on(machine, tmp_path)
    spans: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for limiter in (a, b, a, b, a):
        machine.steps = [-0.006]
        with limiter.hold():
            start = machine.stamp()
            machine.advance(0.2656)
        spans.append((start, machine.stamp()))
        machine.advance(0.013)
    gaps = [b_start[0] - a_end[0] for (_, a_end), (b_start, _) in itertools.pairwise(spans)]
    assert min(gaps) >= 5.0, gaps
    # a's own requests (0, 2, 4) are spaced in real time; b's turn between them counts too.
    a_gaps = [spans[i + 2][0][1] - spans[i][1][1] for i in (0, 2)]
    assert min(a_gaps) >= 5.0
    # Across processes the wall clock is the only shared reference: after a forward step,
    # b waits only the wall clock's remainder, while a's own real gap still holds.
    machine.wall += 4.0
    with b.hold() as waited:
        pass
    assert waited == pytest.approx(5.0 - 0.013 - 4.0)


def test_corrupt_state_hold_off_is_waited_out_on_the_wall_clock(tmp_path: Path):
    machine = Machine(D077_END)
    limiter = _on(machine, tmp_path, corrupt_hold_off_s=60.0)
    state = tmp_path / "ratelimit" / "nps-archive.json"
    state.write_text('{"last_unix": 1')  # torn: the conservative hold-off applies
    read_at = machine.wall
    machine.steps = [-0.05]  # the wall clock loses 50 ms during the hold-off
    with limiter.hold() as waited:
        started = machine.wall
        repaired = json.loads(state.read_text())
    assert repaired["not_before_unix"] == pytest.approx(read_at + 60.0)  # written back
    assert started >= read_at + 60.0, started - read_at
    assert waited == pytest.approx(60.05)
    state.write_text("not json")  # and the bound holds there too
    machine.steps = [-3600.0]
    began = machine.real
    assert limiter.wait() == pytest.approx(65.0) and machine.real - began == pytest.approx(65.0)


def test_a_back_off_is_waited_out_on_the_wall_clock_and_bounded(tmp_path: Path):
    machine = Machine(D077_END)
    limiter = _on(machine, tmp_path)
    limiter.defer(60.0)
    until = machine.wall + 60.0
    machine.steps = [-0.25]
    assert limiter.wait() == pytest.approx(60.25) and machine.wall >= until
    limiter.defer(30.0)  # never shortened: a shorter defer leaves the longer one
    limiter.defer(10.0)
    assert limiter.next_allowed_in() == pytest.approx(30.0)
    machine.steps = [-3600.0]
    began = machine.real
    assert limiter.wait() == pytest.approx(35.0) and machine.real - began == pytest.approx(35.0)


def test_the_monotonic_clock_defaults(tmp_path: Path):
    """Real clocks: the steady clock, one record per process and limiter. Fakes: ``clock``."""
    real = MinIntervalLimiter("probe-clock", 0.0, state_dir=tmp_path, mode="file")
    again = MinIntervalLimiter("probe-clock", 0.0, state_dir=tmp_path, mode="file")
    other = MinIntervalLimiter("probe-clock", 0.0, state_dir=tmp_path / "x", mode="file")
    assert real._monotonic is timeutil.steady_clock
    with real.hold():
        pass
    assert again._own_last_real() == real._own_last_real() is not None
    assert other._own_last_real() is None
    clock = FakeClock()
    fake = MinIntervalLimiter("probe-clock", 0.0, mode="process", clock=clock, sleep=clock.sleep)
    assert fake._monotonic is clock


def test_threads_on_real_clocks_are_one_at_a_time_and_spaced_on_both_clocks(tmp_path: Path):
    limiter = MinIntervalLimiter("probe-threads", 0.12, state_dir=tmp_path, mode="file")
    spans: list[tuple[float, float, float, float]] = []
    guard = threading.Lock()

    def work() -> None:
        for _ in range(3):
            with limiter.hold():
                start = (time.time(), timeutil.steady_clock())
                time.sleep(0.01)
                end = (time.time(), timeutil.steady_clock())
            with guard:
                spans.append((start[1], end[1], start[0], end[0]))

    threads = [threading.Thread(target=work) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    spans.sort()
    assert len(spans) == 9
    real_gaps = [b[0] - a[1] for a, b in itertools.pairwise(spans)]
    wall_gaps = [b[2] - a[3] for a, b in itertools.pairwise(spans)]
    assert min(real_gaps) >= 0.12 and min(wall_gaps) >= 0.12, (real_gaps, wall_gaps)


def test_d077_in_process_mode_too():
    machine = Machine(D077_END)
    a, b = (
        MinIntervalLimiter(
            "probe-process-d077",
            5.0,
            mode="process",
            clock=machine.time,
            sleep=machine.sleep,
            monotonic=machine.monotonic,
        )
        for _ in range(2)
    )
    with a.hold():
        machine.advance(0.2656)
    ended = machine.wall
    machine.advance(0.013)
    machine.steps = [-0.006]
    with b.hold():
        started = machine.wall
    assert started - ended >= 5.0, started - ended


# --- W5 review fixes ------------------------------------------------------------------------
class OddSleepMachine(Machine):
    """A machine whose ``sleep(s)`` lets ``s * scale + late`` pass (at least 1 ms), with the
    wall clock's slew and queued steps applied as in :class:`Machine`."""

    def __init__(self, wall: float, *, scale: float = 1.0, late: float = 0.0) -> None:
        super().__init__(wall)
        self.scale, self.late = scale, late

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        if seconds > 0:
            passed = max(seconds * self.scale + self.late, 0.001)
            self.real += passed
            self.wall += passed * self.wall_rate + (self.steps.pop(0) if self.steps else 0.0)


class _Runaway(Exception):
    """Stops a fake sleep that would otherwise spin for ever (not a RuntimeError)."""


@pytest.mark.parametrize(
    ("step", "expected"),
    [(0.0, 4.987), (4.99, 4.987), (60.0, 4.987), (-2.0, 6.987), (-3600.0, 10.0)],
    ids=["none", "+4.99", "+60", "-2", "-3600"],
)
def test_next_allowed_in_is_the_wait_hold_takes_after_a_wall_clock_step(
    tmp_path: Path, step: float, expected: float
):
    """Review: after a step it said 0 s (forward) or 5 s (back) while hold() waited 4.987,
    6.987 or 10 s, so the queue's fits() could start a step that ran into the live pass."""
    machine, limiter, _ = _d077_first_request(tmp_path)
    machine.wall += step
    said = limiter.next_allowed_in()
    began = machine.real
    with limiter.hold() as waited:
        took = machine.real - began
    assert said == pytest.approx(expected) and took == pytest.approx(expected)
    assert waited == pytest.approx(took)


@pytest.mark.parametrize(("scale", "late"), [(1.0, 0.25), (0.5, 0.0)], ids=["late", "early"])
def test_waited_is_the_real_time_that_passed(tmp_path: Path, scale: float, late: float):
    """Review: waited summed the sleeps asked for (4.987 s for a 5.237 s wait, or about
    9.97 s for a 4.99 s one when sleep returns early); the archive logs it as waited_s."""
    machine = OddSleepMachine(D077_END, scale=scale, late=late)
    limiter = _on(machine, tmp_path)
    with limiter.hold():
        machine.advance(0.2656)
    machine.advance(0.013)
    began = machine.real
    with limiter.hold() as waited:
        took = machine.real - began
    assert took >= D077_WAITED - 1e-6 and waited == pytest.approx(took)
    assert len(machine.slept) >= 1


def test_a_no_op_sleep_on_the_real_clock_reports_the_real_wait():
    """Review: with the real clock and a sleep that returns at once, waited was the sum of
    thousands of sleeps asked for (491 s for a 0.05 s wait)."""
    asked: list[float] = []
    limiter = MinIntervalLimiter("probe-no-op-sleep", 0.05, mode="process", sleep=asked.append)
    limiter.wait()
    waited = limiter.wait()
    assert 0.05 - 1e-3 <= waited < 1.0 and asked, (waited, len(asked))


def test_real_time_is_measured_unless_both_clock_and_sleep_are_fakes(tmp_path: Path):
    """Review: one injected part with one real part ran rules 2 and 3 on the wrong clock."""
    clock = FakeClock()
    interruptible = MinIntervalLimiter(  # the real clock, a sleep a shutdown can cut short
        "probe-mixed", 0.0, state_dir=tmp_path, mode="file", sleep=threading.Event().wait
    )
    frozen_fake = MinIntervalLimiter("probe-mixed", 0.0, mode="process", clock=clock)
    machine = MinIntervalLimiter("probe-mixed", 0.0, mode="process", clock=clock, sleep=clock.sleep)
    assert interruptible._monotonic is timeutil.steady_clock
    assert interruptible._own_key is not None  # shared with this process's other instances
    assert frozen_fake._monotonic is timeutil.steady_clock
    assert machine._monotonic is clock


def test_a_frozen_fake_clock_with_the_real_sleep_starts_within_the_bound():
    """Review: a fake clock that never moves, with time.sleep, hung hold() for ever."""
    frozen = FakeClock()
    limiter = MinIntervalLimiter("probe-frozen-clock", 0.05, mode="process", clock=frozen)
    done: list[float] = []

    def run() -> None:
        with limiter.hold():
            pass
        with limiter.hold() as waited:
            done.append(waited)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(10)
    assert not worker.is_alive(), "hold() never returned"
    assert 0.1 - 1e-3 <= done[0] < 1.0, done  # rule 1 never holds; the bound: 2 x 0.05 s


def test_a_fake_sleep_that_never_lets_time_pass_raises_instead_of_hanging():
    clock = FakeClock()
    asked: list[float] = []

    def stuck(seconds: float) -> None:  # records the sleep, lets no time pass
        asked.append(seconds)
        if len(asked) > 5000:
            raise _Runaway

    limiter = MinIntervalLimiter("probe-stuck-sleep", 5.0, mode="process", clock=clock, sleep=stuck)
    limiter.wait()
    with pytest.raises(RuntimeError, match="did not move"):
        limiter.wait()
    assert len(asked) == lh._STALL_LIMIT == 1000


def test_defer_never_records_a_non_finite_or_unbounded_back_off(tmp_path: Path):
    """Review: defer(nan) recorded no back-off at all, and defer(inf) wrote Infinity, which
    was read back as a torn file and so cut to the corrupt-state hold-off."""
    clock = FakeClock()
    limiter = _on_fake(tmp_path, clock, corrupt_hold_off_s=60.0)
    state = tmp_path / "ratelimit" / "nps-archive.json"
    limiter.defer(math.nan)  # unknown: the conservative side, as an unreadable file
    assert limiter.next_allowed_in() == pytest.approx(60.0)
    limiter.defer(math.inf)
    recorded = json.loads(state.read_text())["not_before_unix"]
    assert math.isfinite(recorded) and recorded == pytest.approx(clock.now + 30 * 86_400)
    assert limiter.next_allowed_in() == pytest.approx(30 * 86_400)
    limiter.defer(1e12)  # never shortened, never longer than 30 days
    assert json.loads(state.read_text())["not_before_unix"] == recorded


def test_a_long_back_off_is_slept_an_hour_at_a_time(tmp_path: Path):
    """Review: a back-off over about 9.2e9 s made time.sleep raise OverflowError."""
    clock = FakeClock()
    limiter = _limiter(tmp_path, clock)
    limiter.defer(3 * 86_400.0)
    assert limiter.wait() == pytest.approx(3 * 86_400.0)
    assert max(clock.slept) == 3600.0 and len(clock.slept) == 72
    asked: list[float] = []

    def first_only(seconds: float) -> None:
        asked.append(seconds)
        raise _Runaway

    hand_edited = MinIntervalLimiter(
        "nps-archive", 5.0, state_dir=tmp_path, mode="file", clock=clock, sleep=first_only
    )
    state = tmp_path / "ratelimit" / "nps-archive.json"
    state.write_text(json.dumps({"last_unix": clock.now, "not_before_unix": clock.now + 1e12}))
    with pytest.raises(_Runaway):
        hand_edited.wait()
    assert asked == [3600.0]


def test_rule_3_keeps_the_wait_when_sleep_returns_early_and_the_wall_clock_jumps(
    tmp_path: Path,
):
    """Rule 3 binds only when sleep returns early (review: no test caught its removal). The
    state is another process's, so rule 2 has none of this process's requests to go on."""
    machine = OddSleepMachine(D077_END, scale=0.5)
    writer, reader = _on(machine, tmp_path), _on(machine, tmp_path)
    with writer.hold():
        machine.advance(0.2656)
    machine.advance(0.013)
    machine.steps = [3.0]  # forward, during the first (early-returning) sleep
    began = machine.real
    with reader.hold() as waited:
        took = machine.real - began
    assert took >= D077_WAITED - 1e-6, took
    assert waited == pytest.approx(took)


def _on_fake(tmp_path: Path, clock: FakeClock, **kwargs: float) -> MinIntervalLimiter:
    return MinIntervalLimiter(
        "nps-archive",
        5.0,
        state_dir=tmp_path,
        mode="file",
        clock=clock,
        sleep=clock.sleep,
        **kwargs,
    )
