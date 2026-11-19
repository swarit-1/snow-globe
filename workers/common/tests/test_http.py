from __future__ import annotations

import itertools
import json
import multiprocessing
import time
from datetime import timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from lookout_common import http as lh
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
    clock = FakeClock()
    limiter = _limiter(tmp_path, clock)
    limiter.wait()
    clock.now -= 3600
    assert limiter.next_allowed_in() == pytest.approx(5.0)


def test_corrupt_state_assumes_a_request_just_happened(tmp_path: Path):
    clock = FakeClock()
    limiter = _limiter(tmp_path, clock)
    (tmp_path / "ratelimit" / "nps-archive.json").write_text("{not json")
    assert limiter.next_allowed_in() == pytest.approx(5.0)


def test_process_mode_is_shared_in_process_and_auto_picks_it_for_s3(
    monkeypatch: pytest.MonkeyPatch,
):
    clock = FakeClock()
    a = MinIntervalLimiter("lambda-test", 2.0, mode="process", clock=clock, sleep=clock.sleep)
    b = MinIntervalLimiter("lambda-test", 2.0, mode="process", clock=clock, sleep=clock.sleep)
    assert a.wait() == 0.0
    assert b.wait() == pytest.approx(2.0)
    monkeypatch.setenv("LOOKOUT_STORAGE", "s3")
    assert MinIntervalLimiter("lambda-auto", 1.0).mode == "process"


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
