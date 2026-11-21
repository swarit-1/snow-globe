"""A fake www.nps.gov that records when each request *arrives*, and the schedules that drive it.

The polite rules (CLAUDE.md, PRD §15.3) are about what nps.gov sees: a camera's polls at least
300 s apart and at most 4 image GETs in any rolling hour, *at the server*. The client's own
stamps cannot show that, since each request reaches the server some time after it is sent.
:class:`ReceiptNPS` models that time: the capture client stamps its send right before calling
the transport, so the handler is entered at the send instant on the fake clock; the request
then travels a one-way latency before it is received (and recorded), and the answer travels
back. The latency model is pluggable (:class:`Latency`): a base drawn per request, spikes of
several seconds on either leg, and a set-up penalty on a cold connection (the first request
of a client, or one after httpx's 5 s keep-alive has expired), which delays the receipt.

:func:`run_schedule` runs the real capture code under the capture Lambda's schedule (a fresh
runner every 300 s, with optional jitter, ``lambda_handler``'s 45 s wait and 120 s timeout) or
the CLI loop (``CaptureRunner.loop`` with its jitter). It can also kill the process between a
request's arrival and the state save, as a hard kill would (:class:`Killer`), and restart it.
:func:`server_violations` lists every breach of the two rules at the server.

The camera feeds change like the real ones: each ARD feed is rewritten every 300 s and shows
a new quarter-hour image 7 min after it was taken; Rainier has a new image every minute.
Nothing here touches the network.
"""

from __future__ import annotations

import itertools
import json
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from snow_globe_common.http import polite_client
from snow_globe_common.storage import LocalStorage

from snow_globe_capture.live import CaptureOptions, CaptureRunner

from .conftest import FakeClock, FakeNPS, make_jpeg

#: httpx's default keep-alive expiry: a pooled connection idle longer than this is closed.
KEEPALIVE_S = 5.0
T0 = datetime(2026, 11, 21, 0, 0, 0, tzinfo=UTC)


class Crash(BaseException):
    """The process dies: nothing in capture catches it, and nothing after it is saved."""


@dataclass
class Latency:
    """One-way latencies, drawn per request (seconds).

    ``base`` gives each leg; ``spike_p`` puts a spike of ``spike_s`` on one leg, the outbound
    or the return at random; ``cold`` is added to the outbound leg of a request on a cold
    connection (DNS, TCP and TLS before the request is sent). ``draw`` may replace all of it.
    """

    base: tuple[float, float] = (0.02, 0.15)
    spike_p: float = 0.0
    spike_s: tuple[float, float] = (2.0, 8.0)
    cold: tuple[float, float] = (0.03, 0.6)
    draw: Callable[[random.Random, str, bool], tuple[float, float]] | None = None

    def legs(self, rng: random.Random, stage: str, cold: bool) -> tuple[float, float]:
        """(outbound, back) for one request of ``stage`` ("poll" or "image")."""
        if self.draw is not None:
            return self.draw(rng, stage, cold)
        out, back = rng.uniform(*self.base), rng.uniform(*self.base)
        if cold:
            out += rng.uniform(*self.cold)
        if self.spike_p and rng.random() < self.spike_p:
            spike = rng.uniform(*self.spike_s)
            if rng.random() < 0.5:
                out += spike
            else:
                back += spike
        return out, back


@dataclass
class Received:
    """One request as the fake server saw it."""

    method: str
    url: str
    sent: datetime
    received: datetime
    answered: datetime | None  # None: the client was killed before the answer reached it
    viewpoint_id: str
    stage: str  # "poll" (JSON feed or HEAD) or "image"


@dataclass
class Killer:
    """Kills the process at random right after a request has reached the server.

    ``rate`` per request; ``after`` picks where: "received" (the request arrived, its answer
    never came back) or "answered" (the answer arrived, the process died before saving).
    """

    rng: random.Random
    rate: float
    armed: bool = True
    kills: int = 0
    dead: bool = False

    def maybe(self) -> None:
        if self.armed and self.rng.random() < self.rate:
            self.kills += 1
            self.dead = True
            raise Crash


class KillableStorage(LocalStorage):
    """Local storage that refuses every call once the process is dead (no ``finally`` saves),
    and whose every call can take ``delay_s`` on the fake clock (S3-like request time)."""

    def __init__(
        self,
        root: Any,
        killer: Killer | None,
        *,
        clock: FakeClock | None = None,
        delay_s: float = 0.0,
    ) -> None:
        super().__init__(root)
        self.killer = killer
        self.clock = clock
        self.delay_s = delay_s
        self.calls: dict[str, int] = {}

    def _alive(self, name: str) -> None:
        if self.killer is not None and self.killer.dead:
            raise Crash
        self.calls[name] = self.calls.get(name, 0) + 1
        if self.delay_s > 0 and self.clock is not None:
            self.clock.advance(self.delay_s)

    def read_json(self, *a: Any, **k: Any) -> Any:
        self._alive("read_json")
        return super().read_json(*a, **k)

    def write_json(self, *a: Any, **k: Any) -> Any:
        self._alive("write_json")
        return super().write_json(*a, **k)

    def put_bytes(self, *a: Any, **k: Any) -> Any:
        self._alive("put_bytes")
        return super().put_bytes(*a, **k)

    def get_bytes(self, *a: Any, **k: Any) -> Any:
        self._alive("get_bytes")
        return super().get_bytes(*a, **k)

    def exists(self, *a: Any, **k: Any) -> Any:
        self._alive("exists")
        return super().exists(*a, **k)

    def list_prefix(self, *a: Any, **k: Any) -> Any:
        self._alive("list_prefix")
        return super().list_prefix(*a, **k)

    def lock(self, *a: Any, **k: Any) -> Any:
        self._alive("lock")
        return super().lock(*a, **k)

    def append_jsonl(self, *a: Any, **k: Any) -> Any:
        self._alive("append_jsonl")
        return super().append_jsonl(*a, **k)

    def append_jsonl_many(self, *a: Any, **k: Any) -> Any:
        self._alive("append_jsonl_many")
        return super().append_jsonl_many(*a, **k)


class ReceiptNPS:
    """The fake www.nps.gov: changing feeds, per-request latency, and a log of receipts."""

    def __init__(
        self,
        vps: Sequence[Any],
        clock: FakeClock,
        *,
        latency: Latency | None = None,
        seed: int = 7,
        killer: Killer | None = None,
        kill_after: str = "received",
        true_now: Callable[[], datetime] | None = None,
    ) -> None:
        self.fake = FakeNPS(clock)
        self.clock = clock
        #: The server's clock: true time, which a client's wall clock may be stepped away from.
        self.true_now = true_now or clock.now
        self.vps = list(vps)
        self.rng = random.Random(seed)
        self.latency = latency or Latency()
        self.t0 = self.true_now()
        self.phase = {vp.id: self.rng.uniform(0, 300) for vp in self.vps}
        self.received: list[Received] = []
        self.killer = killer
        self.kill_after = kill_after
        self._bodies: dict[tuple[str, int], bytes] = {}
        self._urls: dict[str, tuple[Any, str]] = {}
        for vp in self.vps:
            tc = vp.truth_camera
            if tc.json_url:
                self._urls[tc.json_url] = (vp, "poll")
            self._urls[tc.image_url] = (vp, "image" if tc.kind == "ard-aircam" else "?")

    # -- the feeds -------------------------------------------------------------------------
    def _body(self, vp_id: str, index: int, color: tuple[int, int, int]) -> bytes:
        key = (vp_id, index)
        if key not in self._bodies:
            self._bodies[key] = make_jpeg(160, 90, pattern=index % 997, color=color)
            if len(self._bodies) > 64:
                self._bodies.pop(next(iter(self._bodies)))
        return self._bodies[key]

    def _refresh(self, vp: Any) -> None:
        tc = vp.truth_camera
        t = (self.true_now() - self.t0).total_seconds()
        if tc.kind == "ard-aircam":
            i = int((t - 420) // 900)
            shot = self.t0 + timedelta(seconds=900 * i)
            local = shot.astimezone(ZoneInfo(vp.time_zone))
            imagedate = "Updated " + local.strftime("%m/%d/%Y %I:%M %p %Z")
            j = int((t - self.phase[vp.id]) // 300)
            json_lm = self.t0 + timedelta(seconds=self.phase[vp.id] + 300 * j)
            feed = {"imagedate": imagedate, "imagefilelarge": tc.image_url.rsplit("/", 1)[-1]}
            self.fake.set(
                tc.json_url,
                json.dumps(feed).encode(),
                content_type="text/plain",
                last_modified=json_lm.replace(microsecond=0),
                etag=f'"j{j}"',
            )
            body = self._body(vp.id, i, (i % 251, 60, 90))
            self.fake.set(
                tc.image_url, body, last_modified=shot + timedelta(seconds=410), etag=f'"i{i}"'
            )
        else:
            i = int((t - 14) // 60)
            body = self._body(vp.id, i, (40, i % 251, 70))
            moment = self.t0 + timedelta(seconds=60 * i + 14)
            self.fake.set(tc.image_url, body, last_modified=moment, etag=f'"r{i}"')

    # -- the transport ---------------------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        """A transport for one client: its own connection, cold until used, cold again idle."""
        last: list[datetime | None] = [None]

        def handler(request: httpx.Request) -> httpx.Response:
            sent = self.true_now()
            cold = last[0] is None or (sent - last[0]).total_seconds() > KEEPALIVE_S
            url = str(request.url).split("?", 1)[0]
            vp, stage = self._urls.get(url, (None, "?"))
            if stage == "?":
                stage = "poll" if request.method == "HEAD" else "image"
            out, back = self.latency.legs(self.rng, stage, cold)
            self.clock.advance(out)
            received = self.true_now()
            if vp is not None:
                self._refresh(vp)
            response = self.fake.handler(request)
            if self.killer is not None and self.kill_after == "received":
                # Killed while the answer is on its way back: it never arrives.
                self._record(request, url, vp, stage, sent, received, None)
                self.killer.maybe()
                self.received[-1].answered = None
            self.clock.advance(back)
            last[0] = sent + timedelta(seconds=out + back)
            if self.killer is not None and self.kill_after == "received":
                self.received[-1].answered = self.true_now()
            else:
                self._record(request, url, vp, stage, sent, received, self.true_now())
            if self.killer is not None and self.kill_after == "answered":
                self.killer.maybe()  # killed after the answer arrived, before anything is saved
            return response

        return httpx.MockTransport(handler)

    def _record(
        self,
        request: httpx.Request,
        url: str,
        vp: Any,
        stage: str,
        sent: datetime,
        received: datetime,
        answered: datetime | None,
    ) -> None:
        self.received.append(
            Received(
                request.method,
                url,
                sent,
                received,
                answered,
                vp.id if vp is not None else "?",
                stage,
            )
        )

    # -- what the server saw ---------------------------------------------------------------
    def receipts(self, viewpoint_id: str, stage: str) -> list[datetime]:
        """Receipt times of one camera's polls ("poll") or image GETs ("image"), in order."""
        return sorted(
            r.received for r in self.received if r.viewpoint_id == viewpoint_id and r.stage == stage
        )


def server_violations(nps: ReceiptNPS, vps: Sequence[Any]) -> list[tuple[str, str, float]]:
    """Every breach of the polite rules as nps.gov saw them: (camera, rule, seconds).

    - "poll": two consecutive polls of a camera received less than 300 s apart;
    - "image": five consecutive image GETs of a camera received within less than 3600 s.
    """
    out: list[tuple[str, str, float]] = []
    for vp in vps:
        polls = nps.receipts(vp.id, "poll")
        for a, b in itertools.pairwise(polls):
            gap = (b - a).total_seconds()
            if gap < 300.0:
                out.append((vp.id, "poll", gap))
        gets = nps.receipts(vp.id, "image")
        for i in range(len(gets) - 4):
            span = (gets[i + 4] - gets[i]).total_seconds()
            if span < 3600.0:
                out.append((vp.id, "image", span))
    return out


@dataclass
class ScheduleRun:
    """What one fake-clock run did."""

    nps: ReceiptNPS
    storage: KillableStorage | None = None
    summaries: list[dict[str, Any]] = field(default_factory=list)
    starts: list[datetime] = field(default_factory=list)
    restarts: int = 0

    def not_due(self) -> dict[str, int]:
        """Passes (after the first) in which each camera was not due: skipped invocations."""
        counts: dict[str, int] = {}
        for summary in self.summaries[1:]:
            for vp_id, outcome in summary["outcomes"].items():
                if outcome.get("event") == "not_due":
                    counts[vp_id] = counts.get(vp_id, 0) + 1
        return counts


def run_schedule(
    data_root: Any,
    vps: Sequence[Any],
    *,
    hours: float,
    schedule: str,
    jitter_s: float = 0.0,
    latency: Latency | None = None,
    seed: int = 7,
    killer: Killer | None = None,
    kill_after: str = "received",
    restart_s: tuple[float, float] = (0.5, 10.0),
    start: datetime = T0,
    storage_delay_s: float = 0.0,
    idle: Callable[[Callable[[], float], Callable[[], bool]], Any] | None = None,
    sleep: Callable[[FakeClock, float], None] | None = None,
    options: dict[str, Any] | None = None,
) -> ScheduleRun:
    """Run the capture code on a fake clock under ``schedule`` for ``hours``.

    - ``"lambda"``: a fresh runner every 300 s (+ 0..``jitter_s``), ``run_pass(wait 45 s)``
      with Lambda's 120 s timeout and 25 s reserve, as ``lambda_handler.handler``.
    - ``"loop"``: one runner, ``loop(interval 300, jitter_s)``, as ``snow-globe-capture loop``.

    With a ``killer``, a kill ends the process (no state is saved after it); a supervisor
    restarts it ``restart_s`` later with a fresh runner (the loop) or the next invocation runs
    (Lambda). ``storage_delay_s`` makes every storage call take that long; ``idle`` is the
    loop's idle hook (the archive queue's place); ``sleep(clock, seconds)`` replaces the
    runners' sleep (to account for it). The Lambda schedule runs with the options capture
    takes on S3, where the Lambda runs (``reconcile="first-run"``, ``batch_logs="always"``);
    the loop with the local defaults. ``options`` overrides either.
    """
    clock = FakeClock(start)
    rng = random.Random(seed + 1)
    nps = ReceiptNPS(vps, clock, latency=latency, seed=seed, killer=killer, kill_after=kill_after)
    storage = KillableStorage(data_root, killer, clock=clock, delay_s=storage_delay_s)
    run = ScheduleRun(nps, storage=storage)
    end = start + timedelta(hours=hours)

    def do_sleep(seconds: float) -> None:
        if sleep is not None:
            sleep(clock, seconds)
        else:
            clock.sleep(seconds)

    s3_like = {"reconcile": "first-run", "batch_logs": "always"} if schedule == "lambda" else {}
    chosen = {**s3_like, **(options or {})}

    def make(name: str) -> CaptureRunner:
        return CaptureRunner(
            storage,
            polite_client(transport=nps.transport()),
            clock=clock.now,
            sleep=do_sleep,
            monotonic=clock.monotonic,
            options=CaptureOptions(runner=name, **chosen),
            rng=random.Random(rng.random()),
        )

    def revive() -> None:
        if killer is not None and killer.dead:
            killer.dead = False
            run.restarts += 1

    if schedule == "lambda":
        k = 0
        while True:
            tick = start + timedelta(seconds=300 * k + rng.uniform(0.0, jitter_s))
            if tick >= end:
                break
            clock.current = max(clock.current, tick)
            invoked = clock.monotonic()
            run.starts.append(clock.now())
            try:
                with make("lambda") as runner:
                    run.summaries.append(
                        runner.run_pass(
                            vps,
                            wait_for_due_s=45.0,
                            time_left=lambda inv=invoked: 120.0 - (clock.monotonic() - inv),
                            min_time_left_s=25.0,
                        )
                    )
            except Crash:
                revive()
            k += 1
        return run
    if schedule != "loop":
        raise ValueError(schedule)
    while clock.now() < end:
        runner = make("cli-loop")
        original = runner.run_pass

        def recorded(viewpoints: Any, _original: Any = original, _runner: Any = runner, **kw: Any):
            run.starts.append(clock.now())
            summary = _original(viewpoints, **kw)
            run.summaries.append(summary)
            if clock.now() >= end:
                _runner.request_stop()
            return summary

        runner.run_pass = recorded  # type: ignore[method-assign]
        try:
            runner.loop(vps, interval_s=300.0, jitter_s=jitter_s, install_signals=False, idle=idle)
            break
        except Crash:
            revive()
            clock.advance(rng.uniform(*restart_s))  # the supervisor restarts the loop
    return run
