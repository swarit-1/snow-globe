"""Count the S3 requests of the forecast Lambda's calls, for the hosting estimate (PRD §11.2, §17).

infra/cost/estimate.py counts forecast's S3 requests from the code: so many per call for W written runs in the
lookback, so many per extraction. This script checks that count by running the code. It calls
snow_globe_forecast.lambda_handler.process_pending under REPO_ROOT, as the Lambda does (12 h lookback, so 13 runs),
against the forecast tests' fake HRRR bucket (tests/fakebucket.py: synthetic GRIB2 on a 40 x 40 piece of the HRRR
grid around Rainier) with 8 viewpoints: Rainier's manifest under 8 ids, since the requests depend on how many
viewpoints there are, not where they are. Its storage is a LocalStorage behind a wrapper that counts every call the
forecast code makes, and each is priced as the S3 requests snow_globe_common.storage.S3Storage makes for it:

    read_json, get_bytes: 1 GET   exists: 1 HEAD   put_bytes, write_json: 1 PUT   append_jsonl(_many): 1 GET + 1 PUT
    lock: 1 PUT + 1 DELETE (when uncontended)   list_prefix: 1 LIST a page   delete: 1 DELETE

A storage call not in that table stops the run, so a new kind of request cannot go uncounted. Requests to NOAA's
bucket are not counted (they are not billed to this account). The calls, on a fake clock (``now``):
1. a backlog call extracts the 12 complete runs before the newest; the newest is not on S3 yet;
2. an idle call 5 min later: W = 12, one run pending;
3. the newest run appears, and a call 8 min after that extracts it: that call minus call 2 is one extraction;
4. an idle call 5 min later, in the same hour: W = 13;
5. a second setup whose previous run lacks its last hour (it waits until it is 4 h old): an idle call, W = 11.
It prints JSON: GET-tier (GET + HEAD) and PUT-tier (PUT + LIST) requests per idle call by W, and per extraction.
infra/tests/test_cost.sh runs it on the working tree, when workers/forecast's venv exists, and fails if estimate.py
counts otherwise. Needs that venv (ecCodes, numpy, httpx); no network, no AWS:

    cd workers/forecast && .venv/bin/python -I ../../infra/cost/count_forecast_s3.py ../..

Run 21 Nov 2026 on an export of 8fa6b6a (forecast 0.2.0): per idle call 117, 119 and 121 GET-tier requests for W = 13,
12 and 11, and per extraction 19 GET-tier, 11 PUT-tier and 1 DELETE; on an export of 8246a8b (under its old package
names) 117, 110 and 103, and 2, 11 and 1, as counted from that code for D-060.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

#: The S3 requests snow_globe_common.storage.S3Storage makes for each Storage call.
S3_REQUESTS: dict[str, dict[str, int]] = {
    "read_json": {"GET": 1},
    "get_bytes": {"GET": 1},
    "exists": {"HEAD": 1},
    "put_bytes": {"PUT": 1},
    "write_json": {"PUT": 1},
    "append_jsonl": {"GET": 1, "PUT": 1},
    "append_jsonl_many": {"GET": 1, "PUT": 1},
    "lock": {"PUT": 1, "DELETE": 1},
    "list_prefix": {"LIST": 1},
    "delete": {"DELETE": 1},
}
VIEWPOINTS = 8
LOOKBACK_H = 12  # the Lambda's default (SNOW_GLOBE_FORECAST_LOOKBACK_H)
NEWEST = datetime(2026, 11, 19, 20, tzinfo=UTC)  # the newest run of the lookback


class CountingStorage:
    """Forwards to a Storage and counts the calls made on it (calls inside the Storage are not counted)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: collections.Counter[str] = collections.Counter()

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def counted(*args: Any, **kwargs: Any) -> Any:
            self.calls[name] += 1
            return attr(*args, **kwargs)

        return counted

    def take(self) -> dict[str, int]:
        """The S3 requests of the calls since the last take, by operation."""
        requests: collections.Counter[str] = collections.Counter()
        for name, n in self.calls.items():
            if name not in S3_REQUESTS:
                raise SystemExit(f"count_forecast_s3.py: the forecast code calls Storage.{name}, which has no S3 price")
            for op, k in S3_REQUESTS[name].items():
                requests[op] += n * k
        self.calls.clear()
        return dict(sorted(requests.items()))


def tiers(requests: dict[str, int]) -> dict[str, int]:
    return {
        "get_tier": requests.get("GET", 0) + requests.get("HEAD", 0),
        "put_tier": requests.get("PUT", 0) + requests.get("LIST", 0),
        "delete": requests.get("DELETE", 0),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo_root", type=Path, help="the tree whose forecast code to run (workers/, viewpoints/)")
    args = parser.parse_args()
    root = args.repo_root.resolve()
    sys.path[:0] = [
        str(root / "workers/forecast/src"),
        str(root / "workers/common/src"),
        str(root / "workers/forecast"),
    ]
    with tempfile.TemporaryDirectory(prefix="snow-globe-forecast-count.") as tmp:
        print(json.dumps(count(root, Path(tmp)), indent=1))


def count(root: Path, work: Path) -> dict[str, Any]:
    """Run the calls (module docstring) with ``work`` as the data dir; return what they requested."""
    os.environ.update(
        {
            "SNOW_GLOBE_DATA_DIR": str(work),  # the extractor's GRIB cache; never the real data dir
            "SNOW_GLOBE_MANIFEST_DIR": str(root / "viewpoints/manifests"),
            "HTTPS_PROXY": "http://127.0.0.1:9",  # nothing here may reach a real host
            "HTTP_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
        }
    )
    from snow_globe_common.manifest import load_manifests
    from snow_globe_common.storage import LocalStorage
    from snow_globe_forecast import lambda_handler
    from snow_globe_forecast.bucket import HrrrBucket
    from snow_globe_forecast.extract import ExtractOptions
    from snow_globe_forecast.fields import resolve_fields
    from snow_globe_forecast.grid import HRRR_CONUS
    from tests.fakebucket import FakeBucket, sub_grid

    fields = ["VIS:surface", "HGT:surface"]
    options = ExtractOptions(fields=tuple(resolve_fields(["VIS", "TERRAIN"])), max_fh=2)  # hours 0-2
    ci, cj = HRRR_CONUS.nearest_ij(46.78455, -121.74065)
    grid = sub_grid(ci - 20, cj - 20, 40, 40)
    rainier = load_manifests()["rainier.paradise-mountain"]
    viewpoints = {f"rainier.copy-{k}": replace(rainier, id=f"rainier.copy-{k}") for k in range(VIEWPOINTS)}

    def setup(previous_hours: int) -> tuple[FakeBucket, CountingStorage, Any]:
        fake = FakeBucket(grid=grid)
        for back in range(2, LOOKBACK_H + 1):
            fake.add_run(NEWEST - timedelta(hours=back), fields=fields, hours=range(3))
        fake.add_run(NEWEST - timedelta(hours=1), fields=fields, hours=range(previous_hours))
        storage = CountingStorage(LocalStorage(Path(tempfile.mkdtemp(dir=work))))
        bucket = HrrrBucket(transport=fake.transport(), sleep=lambda _s: None)
        return fake, storage, bucket

    def call(storage: CountingStorage, bucket: Any, minutes: int) -> dict[str, Any]:
        summary = lambda_handler.process_pending(
            storage,
            bucket,
            viewpoints,
            now=NEWEST + timedelta(minutes=minutes),
            options=options,
            grid=grid,
            lookback_h=LOOKBACK_H,
            max_runs=LOOKBACK_H + 1,
        )
        statuses = collections.Counter(row["status"].split(":")[0].split(" (")[0] for row in summary["runs"])
        return {"extracted": summary["extracted"], "statuses": dict(sorted(statuses.items())), **tiers(storage.take())}

    fake, storage, bucket = setup(previous_hours=3)
    with bucket:
        backlog = call(storage, bucket, 40)
        idle_12 = call(storage, bucket, 45)
        fake.add_run(NEWEST, fields=fields, hours=range(3))  # its last hour is on S3 from minute 52
        extraction = call(storage, bucket, 53)
        idle_13 = call(storage, bucket, 58)
    fake, storage, bucket = setup(previous_hours=2)
    with bucket:
        call(storage, bucket, 40)
        idle_11 = call(storage, bucket, 45)
    calls = {
        "backlog": backlog,
        "idle_w12": idle_12,
        "extraction": extraction,
        "idle_w13": idle_13,
        "idle_w11": idle_11,
    }
    want = {"idle_w12": (0, 12), "extraction": (1, 12), "idle_w13": (0, 13), "idle_w11": (0, 11)}
    for name, (extracted, written) in want.items():
        got = calls[name]
        if got["extracted"] != extracted or got["statuses"].get("written", 0) != written:
            raise SystemExit(f"count_forecast_s3.py: call {name} did not go as planned: {got}")
    by_written = {"11": idle_11, "12": idle_12, "13": idle_13}
    return {
        "viewpoints": VIEWPOINTS,
        "runs_in_lookback": LOOKBACK_H + 1,
        "idle_call_get_tier_by_written_runs": {w: c["get_tier"] for w, c in by_written.items()},
        "idle_call_put_tier_by_written_runs": {w: c["put_tier"] for w, c in by_written.items()},
        "per_extraction": {k: extraction[k] - idle_12[k] for k in ("get_tier", "put_tier", "delete")},
        "calls": calls,
    }


if __name__ == "__main__":
    main()
