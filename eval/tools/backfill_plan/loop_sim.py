"""Loop uptime needed to spend one day's archive budget, on the real queue runner (PRD §15.3).

Drives capture's real ``QueueRunner.run_idle`` the way ``CaptureRunner.loop`` calls it
(live.py: the next pass is due 300 s + U(0, 30) s after the last one started, and ``idle``
gets ``time_left``), with the real 5 s file limiter, the real persisted ``RequestBudget``,
the real queue file (a copy of the first tranche) and real ``backfill_assets`` storage, all
on a fake clock in a scratch data dir. nps.gov is an ``httpx.MockTransport``: sockets are
disabled (plan.py does that on import), so nothing can leave the machine.

Measured inputs, from the 19 and 20 Nov capture logs (metadata only):
- request times: the 92 successful day searches, and the 85 successful GETs of pre-guard
  assets (their ids come from the sidecars on disk, all dated before 2026-06-01);
- pass durations: the passes of capture 0.2.0 (since 02:39:39 UTC on 20 Nov), and as a
  check the first 73 passes;
- errors: none; independent connect timeouts at 8/215; or the 19 Nov sequence of outcomes
  replayed in order (7 of its 8 failures fall within 10.4 min). A connect timeout costs the
  queue client's 5 s connect timeout, a server disconnect its 10 s read timeout; the runner
  then applies its own deferral (30 s, doubling on consecutive failures, archive.py).

Writes only under OUT (argv[1]); each run's scratch data dir is deleted after the run.
Run from eval/:
    PYTHONPATH=src:../workers/common/src:../workers/capture/src \
        uv run --no-sync python <this file> <OUT> [runs-per-scenario]
"""

from __future__ import annotations

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
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DATA = Path.home() / "lookout-data"
GUARD = date(2026, 6, 1)
V2_SINCE = "2026-11-20T02:39:39"
MAX_WINDOWS = int(os.environ.get("SIM_MAX_WINDOWS", "400"))

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


def _rows(name: str) -> list[dict[str, Any]]:
    out = []
    for line in (DATA / "capture_log" / name).read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def measured_inputs() -> dict[str, Any]:
    """Request times, pass durations and the 19 Nov outcome sequence (metadata only)."""
    preguard = set()
    for path in (DATA / "archive").glob("*/*/*.json"):
        if date.fromisoformat(path.parent.name) < GUARD:
            preguard.add(json.loads(path.read_text())["asset_id"])
    reqs = sorted(
        (r for r in _rows("2026-11-19.jsonl") if r.get("event") == "archive_request"),
        key=lambda r: r["t"],
    )
    # The two deliberate paging probes (HTTP 500 on page 2) are not part of the error rate.
    counted = [r for r in reqs if not (r.get("status") == 500 and r.get("kind") == "search")]

    def failed(r: dict[str, Any]) -> bool:
        return bool(r.get("error")) or (r.get("status") or 0) >= 400

    search = [r["elapsed_ms"] / 1000 for r in counted if r["kind"] == "search" and not failed(r)]
    gets = []
    for r in counted:
        url = str(r.get("url"))
        asset = url.split("/GetAsset/", 1)[1].split("/", 1)[0] if "/GetAsset/" in url else ""
        if asset in preguard and not failed(r):
            gets.append(r["elapsed_ms"] / 1000)
    sequence = []
    for r in counted:
        if not failed(r):
            sequence.append("ok")
        elif "RemoteProtocolError" in str(r.get("error")):
            sequence.append("disconnect")
        else:
            sequence.append("connect_timeout")
    passes = sorted(
        (
            r
            for n in ("2026-11-19.jsonl", "2026-11-20.jsonl")
            for r in _rows(n)
            if r.get("event") == "pass" and r.get("worker") == "capture"
        ),
        key=lambda r: r["t"],
    )
    return {
        "search_s": search,
        "get_s": gets,
        "sequence": sequence,
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


class SimNPS:
    """The archive API on a fake clock: each answer takes a measured time; some fail."""

    def __init__(
        self,
        clock: FakeClock,
        rng: random.Random,
        inputs: dict[str, Any],
        cams: dict[str, Any],
        errors: str,
    ) -> None:
        self.clock = clock
        self.rng = rng
        self.inputs = inputs
        self.by_code = {cam.code.lower(): cam for cam in cams.values()}
        self.errors = errors
        self.offset = rng.randrange(len(inputs["sequence"]))
        self.jpeg = tiny_jpeg()
        self.log: list[tuple[float, float, str, str]] = []

    def _outcome(self) -> str:
        if self.errors == "none":
            return "ok"
        if self.errors == "independent":
            return "connect_timeout" if self.rng.random() < 8 / 215 else "ok"
        seq = self.inputs["sequence"]
        return seq[(self.offset + len(self.log)) % len(seq)]

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
        cam = self.by_code[body["Operand"]["LeftOperand"]["Attribute"].lower()]
        day = datetime.strptime(body["Operand"]["RightOperand"]["Attribute"], "%m/%d/%Y").date()
        results = [{"Asset": as_archive_asset(cam, a).to_dict()} for a in cam.synthetic_day(day)]
        page = {"Results": results, "ResultCount": len(results), "PageCount": 1, "Page": 1}
        return httpx.Response(200, json={**page, "PageSize": 300, "SearchID": "sim"})


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


def run_once(
    out: Path,
    seed: int,
    errors: str,
    pass_pool: list[float],
    inputs: dict[str, Any],
    cams: dict[str, Any],
    viewpoints: dict[str, Any],
    budget: int,
) -> dict[str, Any]:
    """One UTC day: passes and idle windows until the queue's budget is spent."""
    rng = random.Random(seed)
    root = out / f"tmp-{errors}-{seed}"
    shutil.rmtree(root, ignore_errors=True)
    (root / "data").mkdir(parents=True)
    os.environ["LOOKOUT_DATA_DIR"] = str(root / "data")
    storage = LocalStorage(root / "data")
    queue = ArchiveQueue(root / "queue.json")
    source = json.loads((HERE.parent / "queue.json").read_text())
    with queue.edit() as doc:
        doc["entries"] = source["entries"]
    clock = FakeClock(datetime(2026, 11, 21, tzinfo=UTC) + timedelta(seconds=rng.uniform(0, 315)))
    sim = SimNPS(clock, rng, inputs, cams, errors)
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
    first = clock.time()
    windows: list[dict[str, Any]] = []
    try:
        while runner.paused_until_day is None and len(windows) < MAX_WINDOWS:
            start = clock.time()
            clock.advance(rng.choice(pass_pool))
            target = start + 300.0 + rng.uniform(0.0, 30.0)
            before = len(sim.log)
            steps = runner.run_idle(lambda target=target: target - clock.time())
            sent = sim.log[before:]
            windows.append(
                {
                    "steps": steps,
                    "requests": len(sent),
                    "min_left_at_start_s": min((target - s for s, _, _, _ in sent), default=None),
                }
            )
            clock.t = max(clock.t, target)
        entries = queue.read()["entries"]
        status = Counter(str(e.get("status")) for e in entries)
        outcomes: Counter[str] = Counter()
        internal = 0
        for e in entries:
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


def main(out_dir: str, runs: int) -> None:
    """Every scenario ``runs`` times; writes loop-sim.json."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    assert not str(out.resolve()).startswith(str(DATA.resolve()))
    inputs = measured_inputs()
    cams = plan.load_cameras()
    viewpoints = load_manifests()
    results: dict[str, Any] = {
        "inputs": {
            "search_n": len(inputs["search_s"]),
            "search_mean_s": round(st.mean(inputs["search_s"]), 3),
            "get_n": len(inputs["get_s"]),
            "get_mean_s": round(st.mean(inputs["get_s"]), 3),
            "sequence_n": len(inputs["sequence"]),
            "sequence_failures": sum(1 for s in inputs["sequence"] if s != "ok"),
            "pass_v2_n": len(inputs["pass_s_v2"]),
            "pass_v2_median_s": st.median(inputs["pass_s_v2"]),
            "pass_first73_median_s": st.median(inputs["pass_s_first73"]),
            "step_max_s": STEP_MAX_S,
            "step_margin_s": STEP_MARGIN_S,
        },
        "scenarios": {},
    }
    for name, errors, pool in (
        ("no errors, capture 0.2.0 passes", "none", inputs["pass_s_v2"]),
        ("no errors, first 73 passes", "none", inputs["pass_s_first73"]),
        ("independent connect timeouts at 8/215", "independent", inputs["pass_s_v2"]),
        ("19 Nov outcome sequence replayed", "replay", inputs["pass_s_v2"]),
    ):
        rows = [
            run_once(out, seed, errors, pool, inputs, cams, viewpoints, 1000)
            for seed in range(runs)
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
            "asset_outcomes": dict(sum((Counter(r["asset_outcomes"]) for r in rows), Counter())),
            "rows": rows,
        }
        print(
            name, json.dumps({k: v for k, v in results["scenarios"][name].items() if k != "rows"})
        )
    (out / "loop-sim.json").write_text(json.dumps(results, indent=1) + "\n")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 10)
