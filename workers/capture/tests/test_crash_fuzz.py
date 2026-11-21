"""Crash injection.

**PRD §8.7 storage at an hourly camera:** the process dies before or after any write or delete
of a frame, thumbnail, staged copy or camera state, and the staged index in the state is
sometimes lost. Whatever the crashes, nothing under ``frames/`` or ``thumbs/`` is ever lost or
overwritten, no hour keeps two frames, every decided hour keeps the frame nearest it of those
staged when it was decided, and staging ends holding only the hours not yet decided. After the
crashes the feed stalls for two hours on an image the crashes kept from being fetched (a stalled
feed): it arrives after its hour was decided, and must not replace the kept frame.

**The polite rules (PRD §15.3):** the process dies between a request's arrival at nps.gov and
the state save, and is restarted; nps.gov still never sees two polls of a camera under 300 s
apart or five of its image GETs within an hour."""

from __future__ import annotations

import hashlib
import json
import random
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import pytest
from snow_globe_common.storage import LocalStorage

from snow_globe_capture.retention import hour_slot, key_stamp
from snow_globe_capture.state import camera_state_key

from .conftest import FakeClock
from .receipts import Killer, Latency, run_schedule, server_violations
from .test_live import RAINIER, runner
from .test_live_retention import GRCA, ArdFeed, jpgs


class Crash(BaseException):
    """The process dies: nothing in capture catches it."""


class CrashyStorage(LocalStorage):
    """Local storage that dies at random just before or just after a data or state write."""

    RISKY = ("frames/", "thumbs/", "cache/staging/", "state/capture/")

    def __init__(self, root, rng: random.Random, rate: float, clock) -> None:
        super().__init__(root)
        self.rng, self.rate, self.armed, self.clock = rng, rate, True, clock
        self.crashes = 0
        #: When each staged JPEG's write first completed: the candidates an hour chooses from.
        self.staged_ok: dict[str, datetime] = {}
        #: When each frames/ JPEG was written: the moment its hour was decided.
        self.decided_at: dict[str, datetime] = {}

    def _maybe_crash(self) -> None:
        if self.armed and self.rng.random() < self.rate:
            self.crashes += 1
            raise Crash

    def put_bytes(self, key, data, *, content_type=None, if_absent=False):
        risky = key.startswith(self.RISKY)
        if risky:
            self._maybe_crash()
        wrote = super().put_bytes(key, data, content_type=content_type, if_absent=if_absent)
        if key.endswith(".jpg"):
            if key.startswith("cache/staging/frames/"):
                self.staged_ok.setdefault(key, self.clock())
            elif key.startswith("frames/"):
                self.decided_at.setdefault(key, self.clock())
        if risky:
            self._maybe_crash()
        return wrote

    def delete_cache(self, key: str) -> bool:  # what retention.delete_staged calls first
        self._maybe_crash()
        try:
            self.path(key).unlink()
        except FileNotFoundError:
            return False
        self._maybe_crash()
        return True


def snapshot(storage: LocalStorage) -> dict[str, str]:
    out = {}
    for root in ("frames/", "thumbs/"):
        for key in storage.list_prefix(root):
            out[key] = hashlib.sha256(storage.get_bytes(key)).hexdigest()
    return out


@pytest.mark.parametrize("seed", range(10))
def test_crashes_anywhere_never_lose_a_frame_or_keep_an_hour_twice(data_root, fake_nps, seed):
    rng = random.Random(seed)
    clock = FakeClock(datetime(2026, 11, 19, 12, 1, 30, tzinfo=UTC))
    storage = CrashyStorage(data_root, rng, rate=0.06 if seed % 2 else 0.15, clock=clock.now)
    plain = LocalStorage(data_root)  # the test's own edits never crash
    fake_nps.clock = clock
    feed = ArdFeed(fake_nps, GRCA, "grca")
    seen: dict[str, str] = {}

    def one_pass() -> None:
        with suppress(Crash):
            runner(storage, fake_nps, clock).run_pass([GRCA])  # a fresh process every pass
        now = snapshot(storage)
        for key, digest in seen.items():  # append-only: nothing lost, nothing rewritten
            assert now.get(key) == digest, key
        seen.update(now)

    for _ in range(36):  # three hours of 5-minute passes
        feed.update(clock.now())
        one_pass()
        if rng.random() < 0.1 and plain.exists(camera_state_key(GRCA.id)):
            doc = plain.read_json(camera_state_key(GRCA.id))
            doc["staged"] = []  # the staged index is lost (the folder is still listed)
            plain.write_json(camera_state_key(GRCA.id), doc)
        clock.advance(300)
    storage.armed = False
    clock.advance(2 * 3600)  # the feed stalls: its last image is still shown two hours on
    for _ in range(2):  # crash-free passes, long after: every hour so far is ready
        one_pass()
        clock.advance(300)
    frames = jpgs(storage, "frames/")
    hours = [hour_slot(key_stamp(k)) for k in frames]
    assert len(hours) == len(set(hours))
    assert all(storage.exists(k[:-4] + ".json") for k in frames)  # promotions completed
    staged_hours = {hour_slot(key_stamp(k)) for k in storage.staged_ok}
    decided = {h for h in staged_hours if clock.now() >= h + timedelta(hours=1)}
    assert {hour_slot(key_stamp(k)) for k in frames} == decided
    for key in frames:  # the nearest candidate staged by the decision won (ties: the earlier)
        hour = hour_slot(key_stamp(key))
        eligible = [
            key_stamp(k)
            for k, at in storage.staged_ok.items()
            if hour_slot(key_stamp(k)) == hour and at <= storage.decided_at[key]
        ]
        assert key_stamp(key) == min(eligible, key=lambda t: (abs(t - hour), t))
    leftovers = jpgs(storage, "cache/staging/")
    assert all(clock.now() < hour_slot(key_stamp(k)) + timedelta(hours=1) for k in leftovers)
    assert storage.crashes > 0


# --- kills between a request's send and the state save (the lead's decision, 20 Nov 2026) ------


@pytest.mark.parametrize("seed", range(8))
def test_kills_between_the_send_and_the_save_never_break_the_rules_at_the_server(data_root, seed):
    """A poll is reserved and saved before its send, as an image GET is. Here the process is
    killed at random right after a request reached nps.gov, while its answer is on its way
    back (odd seeds) or right after it arrived (even seeds), so nothing of that request is
    saved but its reservation; a supervisor restarts the loop 0.5-10 s later. nps.gov still
    sees >= 300 s between a camera's polls and >= 3600 s across 5 of its GETs: the restarted
    loop counts each unanswered reservation as answered UNANSWERED_REQUEST_BOUND_S after it
    (logged as ``answer_bound``). Without the poll reservation (0.2.1 before answer times) the
    restarted loop polled the camera again within seconds of the killed poll."""
    rng = random.Random(1000 + seed)
    killer = Killer(rng, rate=0.05)
    vps = [RAINIER, GRCA]
    run = run_schedule(
        data_root,
        vps,
        hours=8,
        schedule="loop",
        jitter_s=30.0,
        latency=Latency(spike_p=0.02),
        seed=seed,
        killer=killer,
        kill_after="received" if seed % 2 else "answered",
    )
    assert killer.kills >= 3 and run.restarts == killer.kills
    assert server_violations(run.nps, vps) == []
    lines = [
        json.loads(line)
        for path in (data_root / "capture_log").glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    bounds = [r for r in lines if r["event"] == "answer_bound"]
    assert bounds and {b["kind"] for b in bounds} <= {"unanswered_reservation"}
    for vp in vps:  # each camera still polled at about its 5-minute rate
        assert len(run.nps.receipts(vp.id, "poll")) >= 8 * 12 * 0.6, vp.id


@pytest.mark.parametrize("seed", range(4))
def test_kills_on_the_lambda_schedule_never_break_the_rules_at_the_server(data_root, seed):
    """The same kills on the capture Lambda's schedule: a killed invocation is followed by the
    next one, 300 s later, which bounds what the killed one left unanswered."""
    killer = Killer(random.Random(2000 + seed), rate=0.05)
    vps = [RAINIER, GRCA]
    run = run_schedule(
        data_root,
        vps,
        hours=8,
        schedule="lambda",
        latency=Latency(spike_p=0.02),
        seed=seed,
        killer=killer,
        kill_after="received" if seed % 2 else "answered",
    )
    assert killer.kills >= 3
    assert server_violations(run.nps, vps) == []
