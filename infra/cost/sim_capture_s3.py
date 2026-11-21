"""Simulate the capture Lambda job's S3 requests and billed sleep for the hosting estimate (PRD §17).

infra/README.md, "Monthly cost estimate", uses its output through infra/cost/estimate.py. No AWS, no NPS. It runs
the capture code under REPO_ROOT for HOURS hours of a fake clock. Each pass is one Lambda-style call every 300 s
with a fresh CaptureRunner and a 120 s deadline, as lambda_handler.handler makes one (wait_for_due_s 45 s, at
least 25 s left). The S3 bucket is moto's, with versioning on, and the feeds are the capture tests' FakeNPS:
- Rainier: a new image every 60 s (measured: the newest image was at most ~126 s old at every poll, on a minute grid);
- the seven ARD cameras: imagedate changes every 15 min (measured median 900 s); the JSON is rewritten every 5 min.

The first simulated hour (empty state, first-run reconcile) is not counted. For the counted window it reports:
- every S3 call, by operation and key prefix (botocore before-parameter-build events);
- the bytes of every version the window made noncurrent, per prefix: what each overwrite or delete replaced, which
  the bucket's versioning keeps (versions already noncurrent when the window starts are left out);
- the capture_log bytes the window wrote;
- each pass's length on the fake clock, and where it slept. The fake clock moves only when the code sleeps, so this
  is the billed time a pass spends waiting: for each camera's 5-minute slot (`_run_pass`, `_wait_until`) and between
  requests (`_space`, request_gap_s). Lambda bills that time like any other. Network and CPU time are not simulated;
  estimate.py adds them as an assumption.
Image bytes are synthetic 640 px JPEGs, so frame bytes mean nothing here: frame sizes come from D-028's measured
storage report instead. Every proxy variable points at a closed local port, so nothing can reach a real host. Two
runs of the same code give the same counts, times and bytes, except a few bytes of lock documents (they hold the real
clock and the process id) and wall_s.

infra/test.sh does not run it (tests/test_cost.sh checks its committed output). Run it with the capture venv
(moto, Pillow, httpx), on a clean export of the commit being costed (not the working tree), with a throwaway data
dir for the HTTP limiter's local state:

    git archive <commit> workers viewpoints | tar -x -C /tmp/export
    cd workers/capture && SNOW_GLOBE_DATA_DIR="$(mktemp -d)" .venv/bin/python -I ../../infra/cost/sim_capture_s3.py \\
      /tmp/export 24 /tmp/sim_24h.json

The commit must use the Snow Globe names (D-080); for a commit from before the rename, such as 8246a8b below, run
that commit's own copy of this script (it imports lookout_capture and sets the LOOKOUT_* variables).

20 Nov 2026, commit 8246a8b (capture 0.2.0), 24 h: 288 passes in about 21 s; per day 8,944 PUT, 174 LIST, 7,902 GET,
702 HEAD and 3,420 DELETE; a pass lasts 35.7 s on the fake clock on average (infra/cost/sim_capture_8246a8b.json).
"""

from __future__ import annotations

import collections
import json
import os
import statistics
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

if len(sys.argv) != 4 or not (os.environ.get("SNOW_GLOBE_DATA_DIR") or os.environ.get("LOOKOUT_DATA_DIR")):
    sys.exit("usage: SNOW_GLOBE_DATA_DIR=<temp dir> python -I sim_capture_s3.py REPO_ROOT HOURS OUT_JSON")
clone, hours, out_path = sys.argv[1], float(sys.argv[2]), sys.argv[3]
sys.path[:0] = [
    f"{clone}/workers/capture/src",
    f"{clone}/workers/common/src",
    f"{clone}/workers/capture/tests",
]
os.environ.update(
    {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
        "SNOW_GLOBE_MANIFEST_DIR": f"{clone}/viewpoints/manifests",
        "SNOW_GLOBE_USER_AGENT": "SnowGlobe/0.1 (webcam research capture; fetch-on-change)",
        # Proxies at a closed port: nothing here may reach a real host.
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "",
    }
)

import boto3  # noqa: E402
from conftest import FakeClock, FakeNPS, fixture_text, make_jpeg  # noqa: E402
from moto import mock_aws  # noqa: E402
from snow_globe_capture import CAPTURE_VERSION  # noqa: E402
from snow_globe_capture.live import CaptureOptions, CaptureRunner  # noqa: E402
from snow_globe_common.http import polite_client  # noqa: E402
from snow_globe_common.manifest import load_manifests  # noqa: E402
from snow_globe_common.storage import S3Storage  # noqa: E402

#: lambda_handler.handler at 8246a8b: the function's timeout (infra/terraform/main.tf), the slot wait and the reserve.
LAMBDA_TIMEOUT_S = 120.0
WAIT_FOR_DUE_S = 45.0
MIN_TIME_LEFT_S = 25.0
BUCKET = "snow-globe-sim"

FEED_CODE = {  # fixture JSON body to reuse per camera (only imagedate is replaced)
    "point-reyes.beach": "pore",
    "grand-canyon.yavapai": "grca",
    "grand-teton.science-center": "grte",
    "acadia.mcfarland": "acad",
    "big-bend.panther-junction": "bibe",
    "yosemite.turtleback": "pore",
    "smokies.look-rock": "acad",
}


def prefix_of(key: str) -> str:
    for p in ("state/locks/", "state/capture/hosts/", "cache/staging/"):
        if key.startswith(p):
            return p
    parts = key.split("/")
    if parts[0] == "state" and len(parts) > 2:
        return "state/" + parts[1] + "/"
    return parts[0] + "/"


def summary_of(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "mean": round(statistics.fmean(ordered), 2),
        "p50": round(statistics.median(ordered), 2),
        "p95": round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], 2),
        "max": round(ordered[-1], 2),
    }


def versions(client: Any) -> tuple[list[dict], list[dict]]:
    """Every object version and delete marker in the bucket."""
    found: list[dict] = []
    markers: list[dict] = []
    for page in client.get_paginator("list_object_versions").paginate(Bucket=BUCKET):
        found.extend(page.get("Versions", []))
        markers.extend(page.get("DeleteMarkers", []))
    return found, markers


def log_bytes(client: Any) -> int:
    listed = client.list_objects_v2(Bucket=BUCKET, Prefix="capture_log/").get("Contents", [])
    return sum(item["Size"] for item in listed)


def main() -> None:
    start = datetime(2026, 11, 20, 0, 2, 0, tzinfo=UTC)
    clock = FakeClock(start)
    fake = FakeNPS(clock)
    vps = load_manifests()

    def floor_to(t: datetime, step_s: int) -> datetime:
        return datetime.fromtimestamp(int(t.timestamp()) // step_s * step_s, UTC)

    def set_feeds(now: datetime) -> None:
        # What the feeds show at `now`: the newest image already uploaded (no Last-Modified in the future).
        rainier = vps["rainier.paradise-mountain"]
        minute = floor_to(now - timedelta(seconds=14), 60)
        local = minute.astimezone(ZoneInfo(rainier.time_zone))
        fake.set(
            rainier.truth_camera.image_url,
            make_jpeg(
                640,
                360,
                pattern=int(minute.timestamp() // 60) % 997,
                exif_datetime=local.strftime("%Y:%m:%d %H:%M:%S"),
                make="AXIS",
                model="P1387",
            ),
            last_modified=minute + timedelta(seconds=14),
            etag=f'"r{int(minute.timestamp())}"',
        )
        quarter = floor_to(now - timedelta(minutes=6, seconds=40), 900)
        five = floor_to(now - timedelta(seconds=40), 300)
        for vp_id, code in FEED_CODE.items():
            vp = vps[vp_id]
            local = quarter.astimezone(ZoneInfo(vp.time_zone))
            imagedate = "Updated " + local.strftime("%m/%d/%Y %I:%M %p ").lstrip("0") + local.tzname()
            body = fixture_text(f"{code}json.txt")
            original = json.loads(body)["imagedate"]
            fake.set(
                vp.truth_camera.json_url,
                body.replace(original, imagedate).encode(),
                content_type="text/plain",
                last_modified=five + timedelta(seconds=40),
                etag=f'"j{vp_id}{int(five.timestamp())}"',
            )
            fake.set(
                vp.truth_camera.image_url,
                make_jpeg(
                    640,
                    360,
                    pattern=int(quarter.timestamp() // 900) % 991,
                    exif_datetime=local.strftime("%Y:%m:%d %H:%M:%S"),
                ),
                last_modified=quarter + timedelta(minutes=6, seconds=40),
                etag=f'"i{vp_id}{int(quarter.timestamp())}"',
            )

    calls: collections.Counter[tuple[str, str]] = collections.Counter()
    per_pass: list[int] = []
    pass_seconds: list[float] = []
    slept_by: collections.Counter[str] = collections.Counter()
    counting = {"on": False}

    def sleep(seconds: float) -> None:
        # The capture code's sleeps, by the function that sleeps (frame 1 is the caller of this function).
        if counting["on"] and seconds > 0:
            slept_by[sys._getframe(1).f_code.co_name] += seconds
        clock.sleep(seconds)

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        client.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})

        def on_call(model: Any, params: dict[str, Any], **_: Any) -> None:
            if counting["on"]:
                key = params.get("Key") or params.get("Prefix") or ""
                calls[(model.name, prefix_of(key))] += 1

        client.meta.events.register("before-parameter-build.s3", on_call)
        storage = S3Storage(BUCKET, client=client)
        warmup_passes = 12  # the first hour: first-run reconcile, empty state; not counted
        n_passes = int(hours * 12)
        outcomes: collections.Counter[str] = collections.Counter()
        t0 = time.monotonic()
        noncurrent_before: set[str] = set()
        markers_before: set[str] = set()
        log_bytes_before = 0
        for i in range(warmup_passes + n_passes):
            set_feeds(clock.now())
            if i == warmup_passes:  # the counted window starts: note what versioning already holds
                found, markers = versions(client)
                noncurrent_before = {v["VersionId"] for v in found if not v["IsLatest"]}
                markers_before = {m["VersionId"] for m in markers}
                log_bytes_before = log_bytes(client)
            counting["on"] = i >= warmup_passes
            before = sum(calls.values())
            began = clock.monotonic()
            runner = CaptureRunner(
                storage,
                polite_client(transport=fake.transport()),
                clock=clock.now,
                sleep=sleep,
                monotonic=clock.monotonic,
                options=CaptureOptions(runner="lambda"),
            )
            with runner:
                summary = runner.run_pass(
                    list(vps.values()),
                    wait_for_due_s=WAIT_FOR_DUE_S,
                    time_left=lambda began=began: LAMBDA_TIMEOUT_S - (clock.monotonic() - began),
                    min_time_left_s=MIN_TIME_LEFT_S,
                )
            if counting["on"]:
                per_pass.append(sum(calls.values()) - before)
                pass_seconds.append(clock.monotonic() - began)
                for out in summary["outcomes"].values():
                    outcomes[out.get("event", "?")] += 1
            # Next scheduled tick, 300 s after this one started (rate(5 minutes)).
            tick = start + timedelta(seconds=300 * (i + 1))
            if clock.now() < tick:
                clock.current = tick
        wall = time.monotonic() - t0
        counting["on"] = False  # the inventory below is the simulation's own, not the capture job's

        # What the counted window made noncurrent, by prefix, and current bytes by prefix.
        noncurrent: collections.Counter[str] = collections.Counter()
        noncurrent_n: collections.Counter[str] = collections.Counter()
        current: collections.Counter[str] = collections.Counter()
        found, markers = versions(client)
        for v in found:
            p = prefix_of(v["Key"])
            if v["IsLatest"]:
                current[p] += v["Size"]
            elif v["VersionId"] not in noncurrent_before:
                noncurrent[p] += v["Size"]
                noncurrent_n[p] += 1
        new_markers = sum(1 for m in markers if m["VersionId"] not in markers_before)
        log_sizes = {
            k["Key"]: k["Size"]
            for k in client.list_objects_v2(Bucket=BUCKET, Prefix="capture_log/").get("Contents", [])
        }
        log_bytes_counted = sum(log_sizes.values()) - log_bytes_before

    by_op: collections.Counter[str] = collections.Counter()
    for (op, _), n in calls.items():
        by_op[op] += n
    result = {
        "capture_version": CAPTURE_VERSION,
        "hours_counted": hours,
        "passes_counted": n_passes,
        "wall_s": round(wall, 1),
        "outcomes": dict(outcomes),
        "nps_requests_total": len(fake.requests),
        "s3_calls_by_operation": dict(by_op.most_common()),
        "s3_calls_by_operation_and_prefix": {f"{op} {p}": n for (op, p), n in sorted(calls.items())},
        "s3_calls_per_pass": {
            "mean": round(sum(per_pass) / len(per_pass), 2),
            "min": min(per_pass),
            "max": max(per_pass),
        },
        "fake_pass_seconds": summary_of(pass_seconds),
        "fake_sleep_seconds_per_pass_by_caller": {k: round(v / n_passes, 2) for k, v in sorted(slept_by.items())},
        "noncurrent_bytes_by_prefix": dict(sorted(noncurrent.items())),
        "noncurrent_versions_by_prefix": dict(sorted(noncurrent_n.items())),
        "current_bytes_by_prefix": dict(sorted(current.items())),
        "delete_markers": new_markers,
        "capture_log_bytes_counted": log_bytes_counted,
        "capture_log_sizes": log_sizes,
    }
    with open(out_path, "w") as handle:
        json.dump(result, handle, indent=1)
    shown = ("passes_counted", "wall_s", "outcomes", "s3_calls_by_operation", "s3_calls_per_pass", "fake_pass_seconds")
    print(json.dumps({k: result[k] for k in shown}, indent=1))


main()
