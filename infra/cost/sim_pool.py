"""Pool seed runs of infra/cost/sim_capture_s3.py into the one output infra/cost/estimate.py reads (PRD §17).

One simulation run draws its NPS request times with one seed, and its billed waiting moves with that draw: on 21
Nov, six runs of the same code and inputs that differed only in the seed waited 34.90-37.15 s a pass. So the committed
output is several runs pooled, and it keeps every run, whole, under ``replicates``:
- every count and byte total is summed over the runs, ``hours_counted`` and ``passes_counted`` too, so a per-day or
  per-pass figure of the pool is the runs' average;
- every per-pass mean (waiting, pass time, NPS and S3 request time, sleeps by caller) is taken over all the runs'
  passes, from each run's ``fake_seconds_total``; the longest pass and wait are the runs' longest;
- percentiles cannot be pooled from a run's summary, so the pool gives each run's mean, median and 95th percentile
  of the waiting (``fake_wait_seconds``: ``mean_by_run``, ``p50_by_run``, ``p95_by_run``) instead of its own;
- the current objects at the end (``current_bytes_by_prefix``) are the runs' mean, since every run ends at the same
  simulated time.
The runs must agree on everything but the seed: capture version, warm-up, window, viewpoints, S3 latency, and the
request-times file (name, SHA-256, source, pools). It refuses runs without request times (without them every seed
gives the same run) and repeated seeds. Standard library only; infra/tests/test_cost.sh pools the committed output's
replicates again and compares.

    python3 -I infra/cost/sim_pool.py infra/cost/sim_capture_<commit>.json /tmp/sim_seed[1-6].json
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
from typing import Any

#: Fields every run must share, and the request-times fields too.
SAME = ("capture_version", "warmup_hours", "hours_counted", "passes_counted", "viewpoints", "s3_latency_ms")
SAME_TIMES = ("file", "sha256", "source", "pools")
#: Count and byte fields that pool by summing (dicts sum key by key).
SUMMED = (
    "outcomes",
    "nps_requests_total",
    "nps_requests_counted_by_stage",
    "s3_calls_by_operation_and_prefix",
    "noncurrent_bytes_by_prefix",
    "noncurrent_versions_by_prefix",
    "delete_markers",
    "moto_keys_disposed",
    "capture_log_bytes_counted",
)


def _sum(values: list[Any]) -> Any:
    """Sum numbers, or dicts of numbers key by key (keys sorted)."""
    if isinstance(values[0], dict):
        total: collections.Counter[str] = collections.Counter()
        for value in values:
            total.update(value)
        return dict(sorted(total.items()))
    return sum(values)


def pool(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """The pooled output of ``runs`` (see the module docstring); raises ValueError if they cannot be pooled."""
    if len(runs) < 2:
        raise ValueError("pool at least two runs")
    for key in SAME:
        if len({json.dumps(run.get(key), sort_keys=True) for run in runs}) != 1:
            raise ValueError(f"the runs differ in {key}: they must differ only in the seed")
    times = [run.get("request_times") for run in runs]
    if any(t is None for t in times):
        raise ValueError("a run without --request-times: every seed gives the same such run, so there is no spread")
    for key in SAME_TIMES:
        if len({json.dumps(t[key], sort_keys=True) for t in times}) != 1:
            raise ValueError(f"the runs drew from different request times ({key})")
    seeds = [t["seed"] for t in times]
    if len(set(seeds)) != len(seeds):
        raise ValueError(f"a seed is repeated: {seeds}")
    if any(run.get("fake_seconds_total") is None for run in runs):
        raise ValueError("a run without fake_seconds_total (made by an older sim_capture_s3.py): it cannot be pooled")

    first = runs[0]
    passes = sum(run["passes_counted"] for run in runs)
    hours = sum(run["hours_counted"] for run in runs)
    seconds = [run["fake_seconds_total"] for run in runs]
    totals: dict[str, Any] = {k: round(sum(s[k] for s in seconds), 3) for k in ("pass", "wait", "nps_requests")}
    totals["s3_requests"] = round(sum(s["s3_requests"] for s in seconds), 3)
    totals["sleep_by_caller"] = {k: round(v, 3) for k, v in _sum([s["sleep_by_caller"] for s in seconds]).items()}
    summed = {key: _sum([run[key] for run in runs]) for key in SUMMED}
    by_op = _sum([run["s3_calls_by_operation"] for run in runs])
    current = _sum([run["current_bytes_by_prefix"] for run in runs])
    out: dict[str, Any] = {
        "pooled_runs": len(runs),
        "capture_version": first["capture_version"],
        "warmup_hours": first["warmup_hours"],
        "hours_counted": hours,
        "passes_counted": passes,
        "viewpoints": first["viewpoints"],
        "wall_s": round(sum(run["wall_s"] for run in runs), 1),
        "request_times": {
            **{key: times[0][key] for key in SAME_TIMES},
            "seeds": seeds,
            "drawn_counted": _sum([t["drawn_counted"] for t in times]),
        },
        "s3_latency_ms": first["s3_latency_ms"],
        "outcomes": summed["outcomes"],
        "not_due_per_camera_per_day": round(
            summed["outcomes"].get("not_due", 0) / first["viewpoints"] / (hours / 24), 2
        ),
        "nps_requests_total": summed["nps_requests_total"],
        "nps_requests_counted_by_stage": summed["nps_requests_counted_by_stage"],
        "s3_calls_by_operation": dict(sorted(by_op.items(), key=lambda item: (-item[1], item[0]))),
        "s3_calls_by_operation_and_prefix": summed["s3_calls_by_operation_and_prefix"],
        "s3_calls_per_pass": {
            "mean": round(sum(by_op.values()) / passes, 2),
            "min": min(run["s3_calls_per_pass"]["min"] for run in runs),
            "max": max(run["s3_calls_per_pass"]["max"] for run in runs),
        },
        "fake_seconds_total": totals,
        "fake_pass_seconds": {
            "mean": round(totals["pass"] / passes, 2),
            "max": max(run["fake_pass_seconds"]["max"] for run in runs),
        },
        "fake_wait_seconds": {
            "mean": round(totals["wait"] / passes, 2),
            "max": max(run["fake_wait_seconds"]["max"] for run in runs),
            "mean_by_run": [run["fake_wait_seconds"]["mean"] for run in runs],
            "p50_by_run": [run["fake_wait_seconds"]["p50"] for run in runs],
            "p95_by_run": [run["fake_wait_seconds"]["p95"] for run in runs],
        },
        "fake_nps_request_seconds_per_pass": round(totals["nps_requests"] / passes, 2),
        "fake_s3_seconds_per_pass": round(totals["s3_requests"] / passes, 2),
        "fake_sleep_seconds_per_pass_by_caller": {
            k: round(v / passes, 2) for k, v in totals["sleep_by_caller"].items()
        },
        "noncurrent_bytes_by_prefix": summed["noncurrent_bytes_by_prefix"],
        "noncurrent_versions_by_prefix": summed["noncurrent_versions_by_prefix"],
        "current_bytes_by_prefix": {k: round(v / len(runs)) for k, v in current.items()},
        "delete_markers": summed["delete_markers"],
        "moto_keys_disposed": summed["moto_keys_disposed"],
        "capture_log_bytes_counted": summed["capture_log_bytes_counted"],
        "replicates": runs,
    }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path, help="the pooled JSON file to write")
    parser.add_argument("runs", nargs="+", type=Path, help="sim_capture_s3.py outputs, one per seed")
    args = parser.parse_args()
    runs = [json.loads(path.read_text(encoding="utf-8")) for path in args.runs]
    try:
        pooled = pool(runs)
    except ValueError as exc:
        raise SystemExit(f"sim_pool.py: {exc}") from None
    args.out.write_text(json.dumps(pooled, indent=1) + "\n", encoding="utf-8")
    wait = pooled["fake_wait_seconds"]
    print(
        json.dumps(
            {
                "seeds": pooled["request_times"]["seeds"],
                "wait_mean_s": wait["mean"],
                "wait_mean_by_run_s": wait["mean_by_run"],
                "not_due_per_camera_per_day": pooled["not_due_per_camera_per_day"],
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
