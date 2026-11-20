"""Loop uptime and finish days of the archive queue, on the real queue runner (PRD §15.3).

Drives capture's real ``QueueRunner.run_idle`` the way ``CaptureRunner.loop`` calls it
(live.py: the next pass is due 300 s + U(0, 30) s after the last one started, and ``idle``
gets ``time_left``), with the real 5 s file limiter, the real persisted ``RequestBudget``,
a real queue file (a copy of the one given) and real ``backfill_assets`` storage, all on a
fake clock in a scratch data dir. nps.gov is an ``httpx.MockTransport``: every socket
connect, datagram send and name lookup is disabled (plan.py does that on import), so nothing
can leave the machine.

Two modes:

- ``hours``: each scenario spends one UTC day's budget (1,000 requests) from the head of
  the queue, ``runs`` times; it reports the hours of loop uptime that took.
- ``days``: the queue is worked UTC day after UTC day, with a fixed loop time each day
  (``--hours-per-day``, from 00:00 UTC), until the tranches named in ``--until`` are done;
  it reports each day's requests and, for each tranche the queue holds, the UTC day it
  finished: none of its entries pending, active, failed or stopped (a failed entry has not
  done its work). ``--resume-failed`` starts each UTC day with the lead's ``archive queue
  resume --all``. Tranches come from the planner's plan.json (entry id -> tranche).

Measured inputs, from the capture logs (metadata only):
- request times: the successful searches and hires GETs of the queue's first day (20 Nov
  2026; the GETs only of assets whose sidecars are on disk, all dated before 2026-06-01);
- pass durations: the passes of capture 0.2.0 (since 02:39:39 UTC on 20 Nov), and as a
  check the first 73 passes;
- errors: none; the 20 Nov queue's sequence of outcomes replayed in order (10 of 1,000);
  independent connect timeouts at 8/215; or the 19 Nov archive check's sequence replayed.
  A connect timeout costs the queue client's 5 s connect timeout, a read timeout or a
  server disconnect its 10 s read timeout; the runner then applies its own deferral (30 s,
  doubling on consecutive failures, archive.py).
- partial days: the mock's day keeps each hourly slot's frames with the camera's measured
  share of a complete day (plan.py's completeness), so a day selects as many frames as the
  plan expects.

Writes only under OUT; each run's scratch data dir is deleted after the run.
Run from eval/:
    PYTHONPATH=src:../workers/common/src:../workers/capture/src \
        uv run --no-sync python tools/backfill_plan/loop_sim.py <OUT> --queue <queue file> \
        [--mode hours|days] [--runs N] [--logs DIR] [--plan plan.json] [--until T1b,T3pr] \
        [--tranches T1a,T1b,T3pr] [--resume-failed]
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import random
import shutil
import statistics as st
import sys
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
GUARD = date(2026, 6, 1)
V2_SINCE = "2026-11-20T02:39:39"
QUEUE_DAY = "2026-11-20.jsonl"
CHECK_DAY = "2026-11-19.jsonl"
#: The first UTC day the simulated queue works (the real one resumes then: D-052).
SIM_FIRST_DAY = datetime(2026, 11, 21, tzinfo=UTC)
MAX_WINDOWS = int(os.environ.get("SIM_MAX_WINDOWS", "400"))

_loaded = sys.modules.get("plan")
if _loaded is not None and Path(getattr(_loaded, "__file__", "")).resolve() == HERE / "plan.py":
    plan = _loaded  # already imported (a test): one copy, so `opts is plan.CORE` holds
else:
    _spec = importlib.util.spec_from_file_location("plan", HERE / "plan.py")
    assert _spec is not None and _spec.loader is not None
    plan = importlib.util.module_from_spec(_spec)
    sys.modules["plan"] = plan
    _spec.loader.exec_module(plan)  # disables sockets before anything below can use one

import httpx  # noqa: E402
from lookout_capture.archive import (  # noqa: E402
    ARCHIVE_LIMITER_NAME,
    ARCHIVE_MIN_INTERVAL_S,
    ArchiveClient,
)
from lookout_capture.archive_queue import (  # noqa: E402
    CONNECT_TIMEOUT_S,
    READ_TIMEOUT_S,
    REQUEST_DEADLINE_S,
    STEP_MARGIN_S,
    STEP_MAX_S,
    ArchiveQueue,
    QueueRunner,
)
from lookout_common.http import MinIntervalLimiter  # noqa: E402
from lookout_common.manifest import load_manifests  # noqa: E402
from lookout_common.storage import LocalStorage  # noqa: E402
from PIL import Image  # noqa: E402

DATA = plan.DATA
_OPEN = ("pending", "active")


class FakeClock:
    """One clock for the limiter, the client and the runner: Unix seconds, never sleeps."""

    def __init__(self, start: datetime) -> None:
        self.t = start.timestamp()

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.t, UTC)

    def time(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.t += max(seconds, 1e-6)

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _rows(logs: Path, name: str) -> list[dict[str, Any]]:
    out = []
    for line in (logs / name).read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _outcome(r: Mapping[str, Any]) -> str:
    error = str(r.get("error") or "")
    if not error and (r.get("status") or 0) < 400:
        return "ok"
    if "RemoteProtocolError" in error:
        return "disconnect"
    if "ReadTimeout" in error:
        return "read_timeout"
    return "connect_timeout"


def measured_inputs(logs: Path) -> dict[str, Any]:
    """Request times, pass durations and two outcome sequences (metadata only)."""
    preguard = set()
    for path in (DATA / "archive").glob("*/*/*.json"):
        if date.fromisoformat(path.parent.name) < GUARD:
            preguard.add(json.loads(path.read_text())["asset_id"])

    def requests(name: str) -> list[dict[str, Any]]:
        rows = (r for r in _rows(logs, name) if r.get("event") == "archive_request")
        return sorted(rows, key=lambda r: r["t"])

    queue_day = requests(QUEUE_DAY)
    # The two deliberate paging probes (HTTP 500 on page 2) are not part of the error rate.
    check_day = [
        r for r in requests(CHECK_DAY) if not (r.get("status") == 500 and r.get("kind") == "search")
    ]
    search = [
        r["elapsed_ms"] / 1000 for r in queue_day if r["kind"] == "search" and _outcome(r) == "ok"
    ]
    gets = []
    for r in queue_day:
        url = str(r.get("url"))
        asset = url.split("/GetAsset/", 1)[1].split("/", 1)[0] if "/GetAsset/" in url else ""
        if asset in preguard and _outcome(r) == "ok":
            gets.append(r["elapsed_ms"] / 1000)
    passes = sorted(
        (
            r
            for n in (CHECK_DAY, QUEUE_DAY)
            for r in _rows(logs, n)
            if r.get("event") == "pass" and r.get("worker") == "capture"
        ),
        key=lambda r: r["t"],
    )
    return {
        "search_s": search,
        "get_s": gets,
        "sequence_20nov": [_outcome(r) for r in queue_day],
        "sequence_19nov": [_outcome(r) for r in check_day],
        "pass_s_v2": [float(r["duration_s"]) for r in passes if r["t"] >= V2_SINCE],
        "pass_s_first73": [float(r["duration_s"]) for r in passes[:73]],
    }


def tiny_jpeg() -> bytes:
    """A small real JPEG (the archive client decodes every body)."""
    image = Image.new("RGB", (160, 90), (110, 140, 170))
    image.paste((200, 60, 30), (20, 20, 80, 60))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=80)
    return buffer.getvalue()


#: The zone names NPS writes in LocalTimeString (cached searches of 15 Jul 2025).
ZONE_NAMES = {
    "America/Los_Angeles": ("Pacific Standard Time", "Pacific Daylight Time"),
    "America/Phoenix": ("US Mountain Standard Time", "US Mountain Standard Time"),
    "America/Chicago": ("Central Standard Time", "Central Daylight Time"),
    "America/New_York": ("Eastern Standard Time", "Eastern Daylight Time"),
}


def as_archive_asset(cam: Any, asset: Any) -> Any:
    """A planner frame as the archive lists it: a UUID AssetID and NPS's civil-time label.

    Capture stores an asset only if its id starts with 8 hex characters (an NPS AssetID),
    and parses its time only with a LocalTimeString such as '7/15/2025 11:00 AM Pacific
    Daylight Time': ImageCreateDateTime stays on standard time, LocalTimeString is civil
    time, an hour ahead in summer (lookout_common.timeutil.parse_nps_archive_time).
    """
    local = cam.utc_of(asset).astimezone(cam.tz)
    standard, daylight = ZONE_NAMES[str(cam.tz.key)]
    name = daylight if local.dst() else standard
    hour = local.hour % 12 or 12
    stamp = f"{local.month}/{local.day}/{local.year} {hour}:{local.minute:02d} "
    return replace(
        asset,
        asset_id=str(uuid.uuid5(uuid.NAMESPACE_URL, asset.asset_id)),
        local_time_string=f"{stamp}{'PM' if local.hour >= 12 else 'AM'} {name}",
    )


def partial_day(cam: Any, day: date, share: float, seed: int) -> list[Any]:
    """A day as the archive might hold it: each hourly slot's frames kept with ``share``.

    A frame belongs to the slot pick_assets' hourly rule files it under (its hour up to :30,
    the next hour after), so the hourly pick keeps about ``share`` of a complete day's
    frames, as the plan's completeness says real days do.
    """
    rng = random.Random(f"{seed}-{cam.code}-{day.isoformat()}")
    kept = {slot: rng.random() < share for slot in range(25)}
    out = []
    for asset in cam.synthetic_day(day):
        hh, mm = (int(x) for x in asset.image_create_datetime.split()[1].split(":")[:2])
        hour = hh % 12 + (12 if asset.image_create_datetime.endswith("PM") else 0)
        slot = hour if mm <= 30 else hour + 1
        if kept[slot]:
            out.append(asset)
    return out


class SimNPS:
    """The archive API on a fake clock: each answer takes a measured time; some fail."""

    def __init__(
        self,
        clock: FakeClock,
        rng: random.Random,
        inputs: dict[str, Any],
        cams: dict[str, Any],
        errors: str,
        completeness: Mapping[str, float],
        seed: int,
    ) -> None:
        self.clock = clock
        self.rng = rng
        self.inputs = inputs
        self.by_code = {cam.code.lower(): (vid, cam) for vid, cam in cams.items()}
        self.errors = errors
        self.completeness = completeness
        self.seed = seed
        self.sequence = inputs.get(f"sequence_{errors}", inputs["sequence_20nov"])
        self.offset = rng.randrange(len(self.sequence))
        self.jpeg = tiny_jpeg()
        self.log: list[tuple[float, float, str, str]] = []

    def _outcome(self) -> str:
        if self.errors == "none":
            return "ok"
        if self.errors == "independent":
            return "connect_timeout" if self.rng.random() < 8 / 215 else "ok"
        return self.sequence[(self.offset + len(self.log)) % len(self.sequence)]

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        assert url.startswith("https://www.nps.gov/airwebcams/"), url
        kind = "search" if "/api/Search/Execute" in url else "asset"
        start = self.clock.time()
        outcome = self._outcome()
        if outcome == "connect_timeout":
            self.clock.advance(CONNECT_TIMEOUT_S)
            self.log.append((start, self.clock.time(), kind, outcome))
            raise httpx.ConnectTimeout("simulated TLS handshake timeout", request=request)
        if outcome == "read_timeout":
            self.clock.advance(READ_TIMEOUT_S)
            self.log.append((start, self.clock.time(), kind, outcome))
            raise httpx.ReadTimeout("simulated read timeout", request=request)
        if outcome == "disconnect":
            self.clock.advance(READ_TIMEOUT_S)
            self.log.append((start, self.clock.time(), kind, outcome))
            raise httpx.RemoteProtocolError("simulated server disconnect", request=request)
        pool = self.inputs["search_s"] if kind == "search" else self.inputs["get_s"]
        self.clock.advance(self.rng.choice(pool))
        self.log.append((start, self.clock.time(), kind, outcome))
        if kind == "asset":
            return httpx.Response(200, content=self.jpeg, headers={"Content-Type": "image/jpeg"})
        body = json.loads(request.content)
        vid, cam = self.by_code[body["Operand"]["LeftOperand"]["Attribute"].lower()]
        day = datetime.strptime(body["Operand"]["RightOperand"]["Attribute"], "%m/%d/%Y").date()
        assert day < GUARD, day
        frames = partial_day(cam, day, self.completeness.get(vid, 1.0), self.seed)
        results = [{"Asset": as_archive_asset(cam, a).to_dict()} for a in frames]
        page = {"Results": results, "ResultCount": len(results), "PageCount": 1, "Page": 1}
        return httpx.Response(200, json={**page, "PageSize": 300, "SearchID": "sim"})


def _setup(
    out: Path,
    seed: int,
    errors: str,
    inputs: dict[str, Any],
    cams: dict[str, Any],
    viewpoints: dict[str, Any],
    entries: Sequence[Mapping[str, Any]],
    completeness: Mapping[str, float],
    budget: int,
) -> tuple[Path, FakeClock, SimNPS, ArchiveClient, QueueRunner, ArchiveQueue, random.Random]:
    """A scratch data dir, a copy of the queue, the mock archive and the real runner."""
    rng = random.Random(seed)
    root = out / f"tmp-{errors}-{seed}-{uuid.uuid4().hex[:8]}"
    (root / "data").mkdir(parents=True)
    os.environ["LOOKOUT_DATA_DIR"] = str(root / "data")
    storage = LocalStorage(root / "data")
    queue = ArchiveQueue(root / "queue.json")
    with queue.edit() as doc:
        doc["entries"] = [json.loads(json.dumps(e)) for e in entries]
    clock = FakeClock(SIM_FIRST_DAY + timedelta(seconds=rng.uniform(0, 315)))
    sim = SimNPS(clock, rng, inputs, cams, errors, completeness, seed)
    limiter = MinIntervalLimiter(  # archive_limiter()'s settings, on the fake clock
        ARCHIVE_LIMITER_NAME,
        ARCHIVE_MIN_INTERVAL_S,
        mode="file",
        state_dir=root / "data" / "state",
        clock=clock.time,
        sleep=clock.sleep,
    )
    client = ArchiveClient(
        client=httpx.Client(transport=httpx.MockTransport(sim.handler)),
        limiter=limiter,
        storage=storage,
        clock=clock.now,
        monotonic=clock.time,
        request_deadline_s=REQUEST_DEADLINE_S,
    )
    runner = QueueRunner(
        storage, viewpoints, queue, archive=client, daily_budget=budget, clock=clock.now
    )
    return root, clock, sim, client, runner, queue, rng


def _window(
    clock: FakeClock, rng: random.Random, sim: SimNPS, runner: QueueRunner, pass_pool: list[float]
) -> dict[str, Any]:
    """One live pass and the idle window after it, as CaptureRunner.loop runs them."""
    start = clock.time()
    clock.advance(rng.choice(pass_pool))
    target = start + 300.0 + rng.uniform(0.0, 30.0)
    before = len(sim.log)
    steps = runner.run_idle(lambda target=target: target - clock.time())
    sent = sim.log[before:]
    clock.t = max(clock.t, target)
    return {
        "steps": steps,
        "requests": len(sent),
        "min_left_at_start_s": min((target - s for s, _, _, _ in sent), default=None),
    }


def run_hours(
    out: Path,
    seed: int,
    errors: str,
    pass_pool: list[float],
    inputs: dict[str, Any],
    cams: dict[str, Any],
    viewpoints: dict[str, Any],
    entries: Sequence[Mapping[str, Any]],
    completeness: Mapping[str, float],
    budget: int = 1000,
) -> dict[str, Any]:
    """One UTC day: passes and idle windows until the queue's budget is spent."""
    root, clock, sim, client, runner, queue, rng = _setup(
        out, seed, errors, inputs, cams, viewpoints, entries, completeness, budget
    )
    first = clock.time()
    windows: list[dict[str, Any]] = []
    try:
        while runner.paused_until_day is None and len(windows) < MAX_WINDOWS:
            windows.append(_window(clock, rng, sim, runner, pass_pool))
        done = queue.read()["entries"]
        status = Counter(str(e.get("status")) for e in done)
        outcomes: Counter[str] = Counter()
        internal = 0
        for e in done:
            progress = e.get("progress") or {}
            outcomes.update(progress.get("outcome_counts") or {})
            outcomes.update(Counter((progress.get("outcomes") or {}).values()))
            internal += int(progress.get("internal_errors") or 0)
    finally:
        client.client.close()
        shutil.rmtree(root, ignore_errors=True)
    log = sim.log
    gaps = [b[0] - a[1] for a, b in pairwise(log)]
    full = [w["requests"] for w in windows[:-1]]
    return {
        "seed": seed,
        "errors": errors,
        "requests": len(log),
        "failures": sum(1 for r in log if r[3] != "ok"),
        "hours_to_spend_budget": round((log[-1][1] - first) / 3600, 3),
        "windows": len(windows),
        "entry_status": dict(status),
        "asset_outcomes": dict(outcomes),
        "internal_errors": internal,
        "step_outcomes": dict(sum((Counter(w["steps"]) for w in windows), Counter())),
        "requests_per_full_window_mean": round(st.mean(full), 2) if full else None,
        "min_end_to_start_gap_s": round(min(gaps), 3),
        "min_time_left_at_a_request_start_s": round(
            min(w["min_left_at_start_s"] for w in windows if w["min_left_at_start_s"] is not None),
            3,
        ),
    }


#: Entry statuses that still need ``archive queue resume`` before the work is done.
_NEEDS_RESUME = ("failed", "stopped")


def tranche_progress(
    entries: Sequence[Mapping[str, Any]], labels: Mapping[str, str]
) -> dict[str, dict[str, int]]:
    """Per tranche of the queue's own entries (by ``labels``): open, needs-resume and others.

    ``open`` counts pending and active entries, ``needs_resume`` failed and stopped ones
    (their work is not done until someone resumes them), ``settled`` the rest (done or
    refused). Only tranches that hold an entry appear, so a tranche left out of the queue
    is never reported.
    """
    out: dict[str, dict[str, int]] = {}
    for e in entries:
        row = out.setdefault(
            labels.get(str(e["id"]), "other"), {"open": 0, "needs_resume": 0, "settled": 0}
        )
        status = str(e.get("status"))
        if status in _OPEN:
            row["open"] += 1
        elif status in _NEEDS_RESUME:
            row["needs_resume"] += 1
        else:
            row["settled"] += 1
    return out


def note_finished(
    progress: Mapping[str, Mapping[str, int]],
    day: str,
    finished: dict[str, str],
    settled: dict[str, str],
) -> None:
    """Record ``day`` for each tranche that finished, or settled with failures, on it.

    A tranche is finished when none of its entries is open or needs a resume: a failed entry
    has not done its work. It is settled (``settled``) on the first day none is open, even if
    some failed; for a tranche that finished, that is its finish day.
    """
    for tranche, row in progress.items():
        if row["open"] == 0:
            settled.setdefault(tranche, day)
            if row["needs_resume"] == 0:
                finished.setdefault(tranche, day)


def run_days(
    out: Path,
    seed: int,
    errors: str,
    pass_pool: list[float],
    inputs: dict[str, Any],
    cams: dict[str, Any],
    viewpoints: dict[str, Any],
    entries: Sequence[Mapping[str, Any]],
    completeness: Mapping[str, float],
    labels: Mapping[str, str],
    until: Sequence[str],
    hours_per_day: float,
    max_days: int,
    budget: int = 1000,
    resume_failed: bool = False,
) -> dict[str, Any]:
    """UTC day after UTC day, ``hours_per_day`` of loop time from 00:00 UTC each day.

    With ``resume_failed``, each UTC day starts with ``ArchiveQueue.resume(every=True)``,
    the lead's daily ``archive queue resume --all`` (a failed entry otherwise waits for
    someone). Stops when the tranches in ``until`` are finished (with ``resume_failed``) or
    settled (without: a failed entry would never finish), or after ``max_days``. Returns each
    day's requests, failures and resumed entries, whether it spent the budget, and per
    tranche of the queue the UTC day it finished (:func:`note_finished`), the day it settled,
    and the entries still needing a resume at the end.
    """
    root, clock, sim, client, runner, queue, rng = _setup(
        out, seed, errors, inputs, cams, viewpoints, entries, completeness, budget
    )
    days: list[dict[str, Any]] = []
    finished: dict[str, str] = {}
    settled: dict[str, str] = {}
    try:
        for index in range(max_days):
            day0 = SIM_FIRST_DAY + timedelta(days=index)
            clock.t = max(clock.t, day0.timestamp() + rng.uniform(0, 315))
            resumed = queue.resume(every=True) if resume_failed else []
            stop_at = day0.timestamp() + hours_per_day * 3600
            before = len(sim.log)
            windows = 0
            while clock.time() < stop_at and windows < MAX_WINDOWS:
                if runner.paused_until_day is not None and clock.now().date() < (
                    runner.paused_until_day
                ):
                    break
                result = _window(clock, rng, sim, runner, pass_pool)
                windows += 1
                if not result["requests"] and not any(
                    e["status"] in _OPEN for e in queue.read()["entries"]
                ):
                    break  # the queue has nothing open
            sent = sim.log[before:]
            progress = tranche_progress(queue.read()["entries"], labels)
            note_finished(progress, day0.date().isoformat(), finished, settled)
            days.append(
                {
                    "day": day0.date().isoformat(),
                    "requests": len(sent),
                    "failures": sum(1 for r in sent if r[3] != "ok"),
                    "resumed_at_start": len(resumed),
                    "budget_spent": len(sent) >= budget,
                    "hours_first_to_last": round((sent[-1][1] - sent[0][0]) / 3600, 3)
                    if sent
                    else 0.0,
                    "open_by_tranche": {t: r["open"] for t, r in progress.items() if r["open"]},
                    "needs_resume_by_tranche": {
                        t: r["needs_resume"] for t, r in progress.items() if r["needs_resume"]
                    },
                }
            )
            done = finished if resume_failed else settled
            if all(t in done for t in until):
                break
        final = queue.read()["entries"]
        status = Counter(str(e.get("status")) for e in final)
        needs_resume = {
            str(e["id"]): labels.get(str(e["id"]), "other")
            for e in final
            if e.get("status") in _NEEDS_RESUME
        }
    finally:
        client.client.close()
        shutil.rmtree(root, ignore_errors=True)
    log = sim.log
    gaps = [b[0] - a[1] for a, b in pairwise(log)]
    return {
        "seed": seed,
        "errors": errors,
        "hours_per_day": hours_per_day,
        "resume_failed_daily": resume_failed,
        "days": days,
        "finished_utc_day": finished,
        "settled_utc_day": settled,
        "needs_resume_at_end": needs_resume,
        "entry_status": dict(status),
        "requests": len(log),
        "failures": sum(1 for r in log if r[3] != "ok"),
        "max_requests_in_a_utc_day": max(d["requests"] for d in days),
        "min_end_to_start_gap_s": round(min(gaps), 3) if gaps else None,
    }


SCENARIOS = (
    ("no errors, capture 0.2.0 passes", "none", "pass_s_v2"),
    ("no errors, first 73 passes", "none", "pass_s_first73"),
    ("20 Nov queue outcome sequence replayed", "20nov", "pass_s_v2"),
    ("independent connect timeouts at 8/215", "independent", "pass_s_v2"),
    ("19 Nov outcome sequence replayed", "19nov", "pass_s_v2"),
)


def main(argv: Sequence[str] | None = None) -> None:
    """Run the chosen mode; writes loop-sim-<mode>[-<name>].json under OUT."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--queue", type=Path, required=True, help="queue file to copy (scratch)")
    parser.add_argument("--plan", type=Path, help="plan.json (tranche of every entry id)")
    parser.add_argument("--logs", type=Path, default=DATA / "capture_log")
    parser.add_argument("--mode", choices=("hours", "days"), default="hours")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--scenario", action="append", help="errors: none, 20nov, ... (days)")
    parser.add_argument("--hours-per-day", type=float, default=2.2)
    parser.add_argument("--max-days", type=int, default=12)
    parser.add_argument("--until", default="T1b,T3pr")
    parser.add_argument("--name", default="")
    parser.add_argument(
        "--tranches",
        default="",
        help="keep only the open entries of these tranches (needs --plan); settled entries "
        "never change what the runner does, so this only makes the queue file smaller",
    )
    parser.add_argument(
        "--resume-failed",
        action="store_true",
        help="days mode: resume every failed or stopped entry at the start of each UTC day "
        "(the lead's daily 'archive queue resume --all')",
    )
    args = parser.parse_args(argv)
    out = args.out
    plan.refuse_under_data(out, "output folder")  # explicit: -O strips asserts
    plan.refuse_under_data(args.queue, "queue file (pass a copy)")
    out.mkdir(parents=True, exist_ok=True)
    inputs = measured_inputs(args.logs)
    cams = plan.load_cameras()
    viewpoints = load_manifests()
    completeness = plan.completeness_of(plan.picks_on_cached_days(cams))
    entries = json.loads(args.queue.read_text())["entries"]
    labels = (
        {row["id"]: row["tranche"] for row in json.loads(args.plan.read_text())}
        if args.plan
        else {}
    )
    keep = [t for t in args.tranches.split(",") if t]
    if keep:
        if not labels:
            raise SystemExit("--tranches needs --plan")
        entries = [
            e for e in entries if e.get("status") in _OPEN and labels.get(str(e["id"])) in keep
        ]
    results: dict[str, Any] = {
        "queue": args.queue.name,
        "kept_tranches": keep or "all",
        "entries": len(entries),
        "open_entries": sum(1 for e in entries if e.get("status") in _OPEN),
        "completeness": completeness,
        "inputs": {
            "search_n": len(inputs["search_s"]),
            "search_mean_s": round(st.mean(inputs["search_s"]), 3),
            "get_n": len(inputs["get_s"]),
            "get_mean_s": round(st.mean(inputs["get_s"]), 3),
            "sequence_20nov_n": len(inputs["sequence_20nov"]),
            "sequence_20nov_failures": sum(1 for s in inputs["sequence_20nov"] if s != "ok"),
            "sequence_19nov_n": len(inputs["sequence_19nov"]),
            "sequence_19nov_failures": sum(1 for s in inputs["sequence_19nov"] if s != "ok"),
            "pass_v2_n": len(inputs["pass_s_v2"]),
            "pass_v2_median_s": st.median(inputs["pass_s_v2"]),
            "pass_first73_median_s": st.median(inputs["pass_s_first73"]),
            "step_max_s": STEP_MAX_S,
            "step_margin_s": STEP_MARGIN_S,
        },
        "scenarios": {},
    }
    if args.mode == "hours":
        for name, errors, pool in SCENARIOS:
            rows = [
                run_hours(
                    out, seed, errors, inputs[pool], inputs, cams, viewpoints, entries, completeness
                )
                for seed in range(args.runs)
            ]
            hours = [r["hours_to_spend_budget"] for r in rows]
            results["scenarios"][name] = {
                "runs": len(rows),
                "hours_mean": round(st.mean(hours), 3),
                "hours_min": min(hours),
                "hours_max": max(hours),
                "failures_mean": round(st.mean(r["failures"] for r in rows), 1),
                "requests_per_full_window_mean": round(
                    st.mean(r["requests_per_full_window_mean"] for r in rows), 2
                ),
                "min_end_to_start_gap_s": min(r["min_end_to_start_gap_s"] for r in rows),
                "min_time_left_at_a_request_start_s": min(
                    r["min_time_left_at_a_request_start_s"] for r in rows
                ),
                "all_spent_exactly_the_budget": all(r["requests"] == 1000 for r in rows),
                "internal_errors": sum(r["internal_errors"] for r in rows),
                "failed_entries": sum(r["entry_status"].get("failed", 0) for r in rows),
                "asset_outcomes": dict(
                    sum((Counter(r["asset_outcomes"]) for r in rows), Counter())
                ),
                "rows": rows,
            }
            summary = {k: v for k, v in results["scenarios"][name].items() if k != "rows"}
            print(name, json.dumps(summary), flush=True)
    else:
        if args.plan is None:
            raise SystemExit("--plan is needed for the tranche of each entry")
        until = [t for t in args.until.split(",") if t]
        missing = [t for t in until if t not in tranche_progress(entries, labels)]
        if missing:
            raise SystemExit(f"--until names tranches the queue does not hold: {missing}")
        for errors in args.scenario or ["20nov"]:
            rows = [
                run_days(
                    out,
                    seed,
                    errors,
                    inputs["pass_s_v2"],
                    inputs,
                    cams,
                    viewpoints,
                    entries,
                    completeness,
                    labels,
                    until,
                    args.hours_per_day,
                    args.max_days,
                    resume_failed=args.resume_failed,
                )
                for seed in range(args.runs)
            ]
            results["scenarios"][errors] = {
                "runs": len(rows),
                "hours_per_day": args.hours_per_day,
                "resume_failed_daily": args.resume_failed,
                "finished_utc_day": {
                    t: dict(
                        Counter(r["finished_utc_day"].get(t, "not by the last day") for r in rows)
                    )
                    for t in until
                },
                "settled_utc_day": {
                    t: dict(
                        Counter(r["settled_utc_day"].get(t, "not by the last day") for r in rows)
                    )
                    for t in until
                },
                "runs_with_entries_needing_resume_at_end": sum(
                    1 for r in rows if r["needs_resume_at_end"]
                ),
                "entries_needing_resume_at_end": dict(
                    Counter(t for r in rows for t in r["needs_resume_at_end"].values())
                ),
                "entries_resumed": sum(d["resumed_at_start"] for r in rows for d in r["days"]),
                "max_requests_in_a_utc_day": max(r["max_requests_in_a_utc_day"] for r in rows),
                "days_short_of_the_budget": sum(
                    1 for r in rows for d in r["days"][:-1] if not d["budget_spent"]
                ),
                "min_end_to_start_gap_s": min(
                    r["min_end_to_start_gap_s"] for r in rows if r["min_end_to_start_gap_s"]
                ),
                "rows": rows,
            }
            summary = {k: v for k, v in results["scenarios"][errors].items() if k != "rows"}
            print(errors, json.dumps(summary), flush=True)
    suffix = f"-{args.name}" if args.name else ""
    (out / f"loop-sim-{args.mode}{suffix}.json").write_text(json.dumps(results, indent=1) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
