"""Simulate the capture Lambda job's S3 requests and billed sleep for the hosting estimate (PRD §17).

infra/README.md, "Monthly cost estimate", uses its output through infra/cost/estimate.py. No AWS, no NPS. It runs
the capture code under REPO_ROOT on a fake clock: a warm-up (``--warmup-hours``, default 1 h: empty state, first-run
reconcile), then HOURS counted hours. Each pass is one Lambda-style call every 300 s with a fresh CaptureRunner and a
120 s deadline, as lambda_handler.handler makes one (wait_for_due_s 45 s, at least 25 s left). The S3 bucket is
moto's, with versioning on, and the feeds are the capture tests' FakeNPS:
- Rainier: a new image every 60 s (measured: the newest image was at most ~126 s old at every poll, on a minute grid);
- the seven ARD cameras: imagedate changes every 15 min (measured median 900 s); the JSON is rewritten every 5 min.

**Request times.** Without ``--request-times`` every NPS request is answered at the instant it is sent. Capture 0.2.1
and later space a camera's polls from the previous *answer* (D-065), so then each camera's slot moves only 2 ms a
pass and the waiting comes out far too low (D-066). With ``--request-times FILE`` (infra/cost/nps_request_times.py's
output: measured durations of the live loop's requests), each request takes a duration drawn from FILE on the fake
clock: for its stage (``json`` feed poll, Rainier's ``head``, ``image``) and connection state (``cold`` for a pass's
first request, after more than 5 s idle (httpx's keep-alive) or after a failed request; a state with fewer than 30
measured requests draws from the stage's whole pool). The draws use ``random.Random(--seed)``, so a run is
repeatable, and one run is one draw: on 21 Nov six runs that differed only in the seed waited 34.90-37.15 s a
pass. The committed output therefore pools runs of several seeds (infra/cost/sim_pool.py).

**S3 and CPU time.** By default S3 answers at once and the capture code's CPU work takes no fake time. Both are real
on Lambda, and under D-065 they do more than add billed time: S3 calls before a poll delay its send, those after its
answer delay the next camera's, so they move the slots and so the waiting and the skips. ``--s3-latency-ms MS`` gives
each S3 request of a pass MS of fake time (a sensitivity input; Lambda's S3 latency is unmeasured). CPU time
(resizing and thumbnails at 512 MB, about 0.29 vCPU) is not simulated at all; estimate.py's assumed 5-20 s a pass
stands for both, as billed time.

For the counted window it reports:
- every S3 call, by operation and key prefix (botocore before-parameter-build events);
- the bytes of every version the window made noncurrent, per prefix: what each overwrite or delete replaced, which
  the bucket's versioning keeps. To keep memory bounded on long runs, the simulation itself deletes old versions and
  delete markers every simulated hour (capture_log/, state/, cache/; everything at the window's start and end),
  found in moto's own store, counts the window's ones as it deletes them, and frees moto's copies of them
  (Sweeper.release_moto_keys, which stops the run if moto no longer keeps them where 5.2.5 does). Capture never
  reads an old version, so it sees no difference;
- the capture_log bytes the window wrote;
- each pass's length on the fake clock, where it slept, and how long its NPS requests (and, with
  ``--s3-latency-ms``, its S3 requests) took. The code's sleeps are the billed time a pass spends waiting: for each
  camera's 5-minute slot (`_run_pass`, `_wait_until`) and between requests (`_space`, request_gap_s). Lambda bills
  that time like any other (`fake_wait_seconds`). estimate.py adds the request and CPU time as an assumption.
``--check`` adds self-checks (infra/tests/test_cost.sh runs a short simulation with it): every pass must last exactly
its sleeps plus its NPS and S3 time; before every sweep the versions read from moto's store must be the ones
ListObjectVersions lists, and after it the versions gone from that listing must be the ones the sweep deleted and
counted, with no old version or delete marker left under the swept prefixes; the run fails at the first mismatch.

Image bytes are synthetic 640 px JPEGs, so frame bytes mean nothing here: frame sizes come from D-028's measured
storage report instead. Every proxy variable points at a closed local port, so nothing can reach a real host. Two
runs of the same code and arguments give the same counts, times and bytes, except a few bytes of lock documents (they
hold the real clock and the process id) and wall_s.

infra/test.sh runs it only briefly, as a test (tests/test_cost.sh, which checks the committed output). Run it with a
venv that has moto, Pillow and httpx (the capture venv, or workers/common's), on a clean export of the commit being
costed (not the working tree), with a throwaway data dir for the HTTP limiter's local state, once per seed; then pool
the runs:

    git archive <commit> workers viewpoints | tar -x -C /tmp/export
    cd workers/capture && for seed in 1 2 3 4 5 6; do SNOW_GLOBE_DATA_DIR="$(mktemp -d)" .venv/bin/python -I \\
      ../../infra/cost/sim_capture_s3.py --request-times ../../infra/cost/nps_request_times_0.2.1.json \\
      --warmup-hours 168 --seed "$seed" /tmp/export 72 "/tmp/sim_seed$seed.json"; done
    python3 -I ../../infra/cost/sim_pool.py ../../infra/cost/sim_capture_<commit>.json /tmp/sim_seed[1-6].json

The commit must use the Snow Globe names (D-080); for a commit from before the rename, such as 8246a8b, run that
commit's own copy of this script (it imports lookout_capture and sets the LOOKOUT_* variables).

Runs (MacBook Air M4):
- 20 Nov 2026, 8246a8b (capture 0.2.0), no request times, 24 h: per day 8,944 PUT, 174 LIST, 7,902 GET, 702 HEAD and
  3,420 DELETE; 35.7 s of waiting a pass (its own copy of this script; infra/cost/sim_capture_8246a8b.json).
- 21 Nov 2026, 8fa6b6a (capture 0.3.0), no request times, 24 h: per day 11,048 PUT, 174 LIST, 10,176 GET, 672 HEAD and
  3,360 DELETE; 14.62 s of waiting a pass, the zero-latency figure D-066 set aside. This script gave the same output
  as its 8246a8b-era version on that commit, but for the lock documents' few bytes and wall_s.
- 21 Nov 2026, 8fa6b6a, the first request-times file (one request after a failure counted warm), 168 h warm-up, 72 h
  counted: 34.27 s of waiting a pass with seed 1, 36.16 and 37.14 s with seeds 2 and 3 (not committed).
- 21 Nov 2026, 8fa6b6a, nps_request_times_0.2.1.json as committed, 168 h warm-up, 72 h counted, seeds 1-6, pooled
  (infra/cost/sim_capture_8fa6b6a.json): per day 11,195 PUT, 174 LIST, 9,929 GET, 705 HEAD and 3,426 DELETE; 35.88 s
  of waiting a pass (the runs: 34.90-37.15 s) and 2.23 s of NPS requests; 3.85 skips a camera a day. Each run took
  301-335 s, three at a time, and at most 586 MB resident.
- The same, seeds 1-3 (sensitivity inputs, not committed): with --s3-latency-ms 20, 33.47 s of waiting a pass
  (30.92-35.08 s) and 1.76 s of S3 time, against 35.64 s (34.90-36.19 s) without; with every measured request time
  x 0.25, 38.01 s (37.18-38.75 s) and 0.75-0.92 skips a camera a day.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import random
import statistics
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("repo_root", help="a clean export of the commit being costed (workers/ and viewpoints/)")
parser.add_argument("hours", type=float, help="simulated hours to count, after the warm-up")
parser.add_argument("out_json", help="where to write the result")
parser.add_argument("--warmup-hours", type=float, default=1.0, help="simulated hours before counting (default 1)")
parser.add_argument("--request-times", help="nps_request_times.py output; without it, requests take no time")
parser.add_argument("--seed", type=int, default=1, help="seed for drawing request times (default 1)")
parser.add_argument(
    "--s3-latency-ms",
    type=float,
    default=0.0,
    help="fake time each S3 request of a pass takes (default 0: S3 answers at once); a sensitivity input",
)
parser.add_argument("--check", action="store_true", help="self-checks for infra/tests/test_cost.sh (slow on long runs)")
args = parser.parse_args()
if not (os.environ.get("SNOW_GLOBE_DATA_DIR") or os.environ.get("LOOKOUT_DATA_DIR")):
    sys.exit("set SNOW_GLOBE_DATA_DIR to a throwaway directory first (the HTTP limiter's local state goes there)")
if args.s3_latency_ms < 0:
    sys.exit("--s3-latency-ms must not be negative")
clone, hours, out_path = args.repo_root, args.hours, args.out_json
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
import httpx  # noqa: E402
from conftest import FakeClock, FakeNPS, fixture_text, make_jpeg  # noqa: E402
from moto import mock_aws  # noqa: E402
from snow_globe_capture import CAPTURE_VERSION  # noqa: E402
from snow_globe_capture.live import CaptureOptions, CaptureRunner  # noqa: E402
from snow_globe_common.http import polite_client  # noqa: E402
from snow_globe_common.manifest import load_manifests  # noqa: E402
from snow_globe_common.storage import S3Storage  # noqa: E402

#: lambda_handler.handler: the function's timeout (infra/terraform/main.tf), the slot wait and the reserve.
LAMBDA_TIMEOUT_S = 120.0
WAIT_FOR_DUE_S = 45.0
MIN_TIME_LEFT_S = 25.0
BUCKET = "snow-globe-sim"
KEEPALIVE_S = 5.0  # httpx's default keep-alive expiry, which polite_client keeps
MIN_POOL = 30  # a (stage, connection state) pool smaller than this draws from the stage's whole pool
SWEEP_EVERY_PASSES = 12  # the simulation's own old-version sweep, once a simulated hour
SWEEP_PREFIXES = ("capture_log/", "state/", "cache/")  # where capture overwrites or deletes
#: --check: how far a pass's length may be from its sleeps plus its request times (the fake clock keeps microseconds,
#: and a positive sleep moves it at least 1 us).
PASS_TIME_TOLERANCE_S = 1e-3

FEED_CODE = {  # fixture JSON body to reuse per camera (only imagedate is replaced)
    "point-reyes.beach": "pore",
    "grand-canyon.yavapai": "grca",
    "grand-teton.science-center": "grte",
    "acadia.mcfarland": "acad",
    "big-bend.panther-junction": "bibe",
    "yosemite.turtleback": "pore",
    "smokies.look-rock": "acad",
}


class CheckFailed(Exception):
    """A --check self-check failed: the simulation's own accounting is wrong, so its output must not be used."""


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


class RequestTimes:
    """Measured request durations (seconds) to draw from, by stage and connection state."""

    def __init__(self, path: str, seed: int) -> None:
        raw = Path(path).read_bytes()
        doc = json.loads(raw)
        self.name = Path(path).name
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self.source = doc.get("source", {})
        self.pools: dict[tuple[str, str], list[float]] = {}
        self.pooled_from: dict[str, str] = {}
        for stage, by_state in doc["elapsed_ms"].items():
            both = sorted(by_state["cold"] + by_state["warm"])
            for state in ("cold", "warm"):
                own = by_state[state]
                chosen = own if len(own) >= MIN_POOL else both
                self.pools[(stage, state)] = [ms / 1000 for ms in chosen]
                self.pooled_from[f"{stage} {state}"] = f"{stage} {state}" if chosen is own else f"{stage} (all)"
        self.rng = random.Random(seed)

    def draw(self, stage: str, cold: bool) -> float:
        return self.rng.choice(self.pools[(stage, "cold" if cold else "warm")])


def stage_of(request: httpx.Request) -> str:
    if request.method == "HEAD":
        return "head"
    return "json" if request.url.path.endswith("json.txt") else "image"


def store() -> list[tuple[str, list[Any]]]:
    """Every key in moto's bucket with its versions, oldest first (moto's store keeps the current one last).

    Read straight from moto: its ListObjectVersions deep-copies every version in the bucket on every call, whatever
    the prefix, so on a long run listing versions grew without bound (21 Nov). The capture code never calls it.
    ``--check`` compares this with ListObjectVersions before every sweep.
    """
    from moto.s3.models import s3_backends

    for by_partition in s3_backends.values():
        for backend in by_partition.values():
            if BUCKET in backend.buckets:
                return [(name, list(versions)) for name, versions in backend.buckets[BUCKET].keys.iterlists()]
    raise LookupError(BUCKET)


def current_bytes(prefix: str = "") -> dict[str, int]:
    """The size of every current object under ``prefix``, by key."""
    from moto.s3.models import FakeDeleteMarker

    return {
        name: versions[-1].size
        for name, versions in store()
        if versions and name.startswith(prefix) and not isinstance(versions[-1], FakeDeleteMarker)
    }


#: One version or delete marker: (key, version id, size or None for a marker).
Version = tuple[str, str, int | None]


def store_versions() -> set[Version]:
    """Every version and delete marker in moto's store (--check)."""
    from moto.s3.models import FakeDeleteMarker

    return {
        (name, v.version_id, None if isinstance(v, FakeDeleteMarker) else v.size)
        for name, versions in store()
        for v in versions
    }


def api_versions(client: Any) -> set[Version]:
    """Every version and delete marker that ListObjectVersions lists (--check)."""
    found: set[Version] = set()
    for page in client.get_paginator("list_object_versions").paginate(Bucket=BUCKET):
        found.update((v["Key"], v["VersionId"], v["Size"]) for v in page.get("Versions", []))
        found.update((m["Key"], m["VersionId"], None) for m in page.get("DeleteMarkers", []))
    return found


class Sweeper:
    """The simulation's own old-version sweep: it deletes old versions and delete markers, and counts them."""

    def __init__(self, client: Any, *, check: bool = False) -> None:
        self.client = client
        self.check = check
        self.bytes: collections.Counter[str] = collections.Counter()
        self.versions: collections.Counter[str] = collections.Counter()
        self.markers = 0
        self.keys_disposed = 0
        self.checked_sweeps = 0

    def sweep(self, prefixes: tuple[str, ...], *, count: bool) -> None:
        """Delete every noncurrent version and delete marker under ``prefixes``; with ``count``, add them up first.

        A key whose newest version is a delete marker loses all its versions at once, so it stays absent; a key
        with a current object keeps it. Capture reads only current objects, so it cannot tell. The versions are
        found in moto's store (:func:`store`) and deleted through the S3 API.
        """
        from moto.s3.models import FakeDeleteMarker

        before = api_versions(self.client) if self.check else set()
        if self.check and before != store_versions():
            raise CheckFailed(f"moto's store and ListObjectVersions disagree on {len(before ^ store_versions())} items")
        doomed: list[dict[str, str]] = []
        nbytes: collections.Counter[str] = collections.Counter()
        nversions: collections.Counter[str] = collections.Counter()
        nmarkers = 0
        for name, versions in store():
            if not name.startswith(prefixes):
                continue
            for v in versions:
                marker = isinstance(v, FakeDeleteMarker)
                if v is versions[-1] and not marker:
                    continue  # the current object stays
                doomed.append({"Key": name, "VersionId": v.version_id})
                if marker:
                    nmarkers += 1
                else:
                    nbytes[prefix_of(name)] += v.size
                    nversions[prefix_of(name)] += 1
        for i in range(0, len(doomed), 1000):
            self.client.delete_objects(Bucket=BUCKET, Delete={"Objects": doomed[i : i + 1000], "Quiet": True})
        if self.check:  # what is counted below is what the check compares with the listing
            self._check_swept(prefixes, before, sum(nbytes.values()), sum(nversions.values()), nmarkers)
        if count:
            self.bytes.update(nbytes)
            self.versions.update(nversions)
            self.markers += nmarkers
        self.keys_disposed += self.release_moto_keys()

    def _check_swept(self, prefixes: tuple[str, ...], before: set[Version], nbytes: int, nver: int, nmark: int) -> None:
        """--check: what left the listing is what the sweep counted, and nothing old is left under ``prefixes``."""
        after = api_versions(self.client)
        gone = before - after
        listed = (
            sum(size for _, _, size in gone if size is not None),
            sum(1 for _, _, size in gone if size is not None),
            sum(1 for _, _, size in gone if size is None),
        )
        if after - before or listed != (nbytes, nver, nmark):
            raise CheckFailed(
                f"the sweep counted {(nbytes, nver, nmark)} (bytes, versions, markers); the listing lost "
                f"{listed} and gained {len(after - before)}"
            )
        per_key = collections.Counter(key for key, _, _ in after if key.startswith(prefixes))
        left = [key for key, n in per_key.items() if n > 1]
        markers = [key for key, _, size in after if size is None and key.startswith(prefixes)]
        if left or markers:
            raise CheckFailed(f"the sweep left old versions of {len(left)} keys and {len(markers)} delete markers")
        self.checked_sweeps += 1

    @staticmethod
    def release_moto_keys() -> int:
        """Free the S3 objects moto keeps after no bucket holds them; return how many keys it disposed of.

        Every moto model class records every instance it makes (``instances_tracked`` in moto 5.2, for its own reset
        and dashboard) until the mock ends: every key version capture overwrote, every copy moto made to answer a
        listing, and each one's ACL. On 21 Nov a 10-day run grew past 20 GB that way, deleted versions or not.
        Between passes nothing else uses them: keys that no bucket holds are disposed of, and the S3 models' lists
        are emptied (live objects stay referenced by their owners). If moto no longer keeps FakeKey's list where
        5.2.5 does, this raises instead of letting the memory grow unnoticed.
        """
        from moto.core.model_instances import model_data
        from moto.s3.models import FakeKey, s3_backends

        models = model_data.get("s3", {})
        tracked_keys = getattr(FakeKey, "instances_tracked", None)
        if models.get(FakeKey.__name__) is not FakeKey or not isinstance(tracked_keys, list):
            raise LookupError(
                "moto no longer lists FakeKey instances in model_data['s3'][...].instances_tracked (moto 5.2.5 does): "
                "find where it keeps them before a long run, or its memory grows without bound"
            )
        held: set[int] = set()
        for by_partition in s3_backends.values():
            for backend in by_partition.values():
                for bucket in backend.buckets.values():
                    for _, versions in bucket.keys.iterlists():
                        held.update(id(k) for k in versions)
        disposed = 0
        for model in models.values():
            tracked = getattr(model, "instances_tracked", None)
            if not isinstance(tracked, list):
                continue
            if model is FakeKey:
                for key in tracked:
                    if id(key) not in held:
                        key.dispose()
                        disposed += 1
            tracked.clear()
        return disposed


def main() -> None:
    start = datetime(2026, 11, 20, 0, 2, 0, tzinfo=UTC)
    clock = FakeClock(start)
    fake = FakeNPS(clock)
    vps = load_manifests()
    times = RequestTimes(args.request_times, args.seed) if args.request_times else None
    s3_latency_s = args.s3_latency_ms / 1000

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
    wait_seconds: list[float] = []
    nps_seconds: list[float] = []
    s3_seconds: list[float] = []
    slept_by: collections.Counter[str] = collections.Counter()
    nps_by_stage: collections.Counter[str] = collections.Counter()
    drawn: collections.Counter[str] = collections.Counter()
    counting = {"on": False}
    in_pass = {"on": False}
    pass_state = {"slept": 0.0, "nps_s": 0.0, "s3_s": 0.0}

    def sleep(seconds: float) -> None:
        # The capture code's sleeps, by the function that sleeps (frame 1 is the caller of this function).
        if seconds > 0:
            pass_state["slept"] += seconds
            if counting["on"]:
                slept_by[sys._getframe(1).f_code.co_name] += seconds
        clock.sleep(seconds)

    def transport() -> httpx.MockTransport:
        """One pass's connection to the fake nps.gov: each request takes a measured time (with --request-times)."""
        last_answer: list[datetime | None] = [None]

        def handler(request: httpx.Request) -> httpx.Response:
            stage = stage_of(request)
            sent = clock.now()
            response = fake.handler(request)
            if counting["on"]:
                nps_by_stage[stage] += 1
            if times is not None:
                cold = last_answer[0] is None or (sent - last_answer[0]).total_seconds() > KEEPALIVE_S
                seconds = times.draw(stage, cold)
                clock.advance(seconds)
                last_answer[0] = clock.now()
                pass_state["nps_s"] += seconds
                if counting["on"]:
                    drawn[f"{stage} {'cold' if cold else 'warm'}"] += 1
            return response

        return httpx.MockTransport(handler)

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        client.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})

        def on_call(model: Any, params: dict[str, Any], **_: Any) -> None:
            if counting["on"]:
                key = params.get("Key") or params.get("Prefix") or ""
                calls[(model.name, prefix_of(key))] += 1
            if in_pass["on"] and s3_latency_s > 0:  # the capture code's own calls; never the sweeps'
                clock.advance(s3_latency_s)
                pass_state["s3_s"] += s3_latency_s

        client.meta.events.register("before-parameter-build.s3", on_call)
        storage = S3Storage(BUCKET, client=client)
        sweeper = Sweeper(client, check=args.check)
        warmup_passes = round(args.warmup_hours * 12)
        n_passes = round(hours * 12)
        outcomes: collections.Counter[str] = collections.Counter()
        t0 = time.monotonic()
        log_bytes_before = 0
        for i in range(warmup_passes + n_passes):
            set_feeds(clock.now())
            counting["on"] = False  # the sweeps are the simulation's own calls
            if i == warmup_passes:  # the counted window starts with no old version anywhere
                sweeper.sweep(("",), count=False)
                log_bytes_before = sum(current_bytes("capture_log/").values())
            elif i % SWEEP_EVERY_PASSES == 0:
                sweeper.sweep(SWEEP_PREFIXES, count=i > warmup_passes)
            counting["on"] = i >= warmup_passes
            pass_state.update(slept=0.0, nps_s=0.0, s3_s=0.0)
            before = sum(calls.values())
            began = clock.monotonic()
            in_pass["on"] = True
            runner = CaptureRunner(
                storage,
                polite_client(transport=transport()),
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
            in_pass["on"] = False
            length = clock.monotonic() - began
            accounted = pass_state["slept"] + pass_state["nps_s"] + pass_state["s3_s"]
            if args.check and abs(length - accounted) > PASS_TIME_TOLERANCE_S:
                raise CheckFailed(f"pass {i} lasted {length:.6f} s; its sleeps and request times, {accounted:.6f} s")
            if counting["on"]:
                per_pass.append(sum(calls.values()) - before)
                pass_seconds.append(length)
                wait_seconds.append(pass_state["slept"])
                nps_seconds.append(pass_state["nps_s"])
                s3_seconds.append(pass_state["s3_s"])
                for out in summary["outcomes"].values():
                    outcomes[out.get("event", "?")] += 1
            # Next scheduled tick, 300 s after this one started (rate(5 minutes)).
            tick = start + timedelta(seconds=300 * (i + 1))
            if clock.now() < tick:
                clock.current = tick
        wall = time.monotonic() - t0
        counting["on"] = False  # the inventory below is the simulation's own, not the capture job's

        # The window's last old versions, everywhere; then what is current, by prefix.
        sweeper.sweep(("",), count=True)
        current: collections.Counter[str] = collections.Counter()
        for name, size in current_bytes().items():
            current[prefix_of(name)] += size
        log_sizes = current_bytes("capture_log/")
        log_bytes_counted = sum(log_sizes.values()) - log_bytes_before

    by_op: collections.Counter[str] = collections.Counter()
    for (op, _), n in calls.items():
        by_op[op] += n
    days = hours / 24
    result = {
        "capture_version": CAPTURE_VERSION,
        "warmup_hours": args.warmup_hours,
        "hours_counted": hours,
        "passes_counted": n_passes,
        "viewpoints": len(vps),
        "wall_s": round(wall, 1),
        "request_times": (
            None
            if times is None
            else {
                "file": times.name,
                "sha256": times.sha256,
                "source": times.source,
                "seed": args.seed,
                "pools": times.pooled_from,
                "drawn_counted": dict(sorted(drawn.items())),
            }
        ),
        "s3_latency_ms": args.s3_latency_ms,
        "checked": ({"passes": warmup_passes + n_passes, "sweeps": sweeper.checked_sweeps} if args.check else None),
        "outcomes": dict(outcomes),
        "not_due_per_camera_per_day": round(outcomes.get("not_due", 0) / len(vps) / days, 2),
        "nps_requests_total": len(fake.requests),
        "nps_requests_counted_by_stage": dict(sorted(nps_by_stage.items())),
        "s3_calls_by_operation": dict(by_op.most_common()),
        "s3_calls_by_operation_and_prefix": {f"{op} {p}": n for (op, p), n in sorted(calls.items())},
        "s3_calls_per_pass": {
            "mean": round(sum(per_pass) / len(per_pass), 2),
            "min": min(per_pass),
            "max": max(per_pass),
        },
        # The counted window's totals, unrounded but for the millisecond, so that runs can be pooled exactly.
        "fake_seconds_total": {
            "pass": round(sum(pass_seconds), 3),
            "wait": round(sum(wait_seconds), 3),
            "nps_requests": round(sum(nps_seconds), 3),
            "s3_requests": round(sum(s3_seconds), 3),
            "sleep_by_caller": {k: round(v, 3) for k, v in sorted(slept_by.items())},
        },
        "fake_pass_seconds": summary_of(pass_seconds),
        "fake_wait_seconds": summary_of(wait_seconds),
        "fake_nps_request_seconds_per_pass": round(sum(nps_seconds) / n_passes, 2),
        "fake_s3_seconds_per_pass": round(sum(s3_seconds) / n_passes, 2),
        "fake_sleep_seconds_per_pass_by_caller": {k: round(v / n_passes, 2) for k, v in sorted(slept_by.items())},
        "noncurrent_bytes_by_prefix": dict(sorted(sweeper.bytes.items())),
        "noncurrent_versions_by_prefix": dict(sorted(sweeper.versions.items())),
        "current_bytes_by_prefix": dict(sorted(current.items())),
        "delete_markers": sweeper.markers,
        "moto_keys_disposed": sweeper.keys_disposed,
        "capture_log_bytes_counted": log_bytes_counted,
        "capture_log_sizes": dict(sorted(log_sizes.items())),
    }
    with open(out_path, "w") as handle:
        json.dump(result, handle, indent=1)
    shown = (
        "passes_counted",
        "wall_s",
        "outcomes",
        "not_due_per_camera_per_day",
        "s3_calls_by_operation",
        "s3_calls_per_pass",
        "fake_pass_seconds",
        "fake_wait_seconds",
        "fake_nps_request_seconds_per_pass",
        "fake_s3_seconds_per_pass",
    )
    print(json.dumps({k: result[k] for k in shown}, indent=1))


try:
    main()
except CheckFailed as exc:
    sys.exit(f"sim_capture_s3.py --check failed: {exc}")
