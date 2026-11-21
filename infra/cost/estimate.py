"""The arithmetic behind infra/README.md's monthly cost estimate (PRD §11.3, §17; D-035), 20 Nov 2026.

Every input says where it comes from: measured, simulated (infra/cost/sim_capture_s3.py; its output for the costed
commit is infra/cost/sim_capture_8246a8b.json), counted from the code, extrapolated, or assumed. Prices are us-east-1
list prices as read on 19 Nov 2026. It prints every figure of the README's cost table, and the monthly totals, as
JSON. Standard library only (Python 3.9 or newer); infra/test.sh runs it on the committed simulation output:

    python3 -I infra/cost/estimate.py infra/cost/sim_capture_8246a8b.json
"""

from __future__ import annotations

import json
import sys

with open(sys.argv[1]) as handle:
    sim = json.load(handle)  # sim_capture_s3.py output: one simulated capture day
per_day = 24 / sim["hours_counted"]
DAYS = 30
GB = 1e9  # decimal, like the MB figures below (AWS bills binary GB, so storage here is up to 7 % high)
LO, HI = 0, 1  # every range below is [low, high]

# ---- prices, us-east-1, read from AWS price lists and pages on 19 Nov 2026 (infra/README.md) --------------------
P_LAMBDA_GBS = 0.0000133334  # Lambda on Arm, per GB-s
P_LAMBDA_REQ = 0.20 / 1e6
P_TMP_GBS = 0.0000000309  # ephemeral storage above 512 MB, per GB-s
FREE_LAMBDA_GBS = 400_000  # a month (with 1 M requests); the AWS page sets no plan condition
FREE_LAMBDA_REQ = 1_000_000
P_STORE = 0.023  # S3 Standard, per GB-month
P_PUT = 0.005 / 1000  # PUT, COPY, POST, LIST
P_GET = 0.0004 / 1000  # GET, HEAD and other requests
P_ECR = 0.10  # per GB-month (the new-customer 500 MB is not counted)
P_LOG_INGEST = 0.50  # CloudWatch Logs, per GB (free: 5 GB a month)
P_ALARM = 0.10  # standard alarm a month (free: 10)
P_SCHEDULER = 1.00 / 1e6  # EventBridge Scheduler invocations (free: 14 M a month)

# ---- runs (infra/terraform: both jobs rate(5 minutes), PRD §11.2) -------------------------------------------------
RUNS = 288 * DAYS  # each job
FC_EXTRACT_CALLS = 24 * DAYS  # calls that extract an HRRR run (one run an hour)
FC_IDLE_CALLS = RUNS - FC_EXTRACT_CALLS

# ---- Lambda compute ---------------------------------------------------------------------------------------------
# Capture, 0.5 GB. Simulated: each pass sleeps on the fake clock while it waits for each camera's 5-minute slot and
# spaces its requests; Lambda bills that time. Assumed on top: the requests themselves and CPU, 5-20 s a pass (the
# laptop's capture loop spent 2.4 s a pass on NPS requests, measured over 41 passes on 20 Nov; a pass also makes
# about 73 S3 requests and makes thumbnails at 512 MB, about 0.29 vCPU).
CAPTURE_GB = 0.5
CAPTURE_IO_CPU_S = (5.0, 20.0)  # assumed
capture_sleep_s = sim["fake_pass_seconds"]["mean"]  # simulated
capture_s = [capture_sleep_s + x for x in CAPTURE_IO_CPU_S]
# Forecast, 2 GB with a 2 GB /tmp. Assumed: 1-5 s for a call that extracts nothing (about 110 S3 requests), 30-150 s
# for one that extracts a run at the deployed profile (all 22 fields and every hour: 453 MB of GRIB for an 18 h run,
# 1,202 MB for a 48 h one, workers/forecast/README.md).
FORECAST_GB = 2.0
FC_IDLE_S = (1.0, 5.0)  # assumed
FC_EXTRACT_S = (30.0, 150.0)  # assumed
fc_seconds = [FC_IDLE_CALLS * FC_IDLE_S[i] + FC_EXTRACT_CALLS * FC_EXTRACT_S[i] for i in (LO, HI)]
capture_gbs = [CAPTURE_GB * s * RUNS for s in capture_s]
forecast_gbs = [FORECAST_GB * s for s in fc_seconds]
lambda_gbs = [capture_gbs[i] + forecast_gbs[i] for i in (LO, HI)]
compute_free = [max(0.0, g - FREE_LAMBDA_GBS) * P_LAMBDA_GBS for g in lambda_gbs]
compute_none = [g * P_LAMBDA_GBS for g in lambda_gbs]
lambda_requests_none = 2 * RUNS * P_LAMBDA_REQ  # 17,280 requests, under the free 1 M
tmp_usd = [1.5 * s * P_TMP_GBS for s in fc_seconds]  # forecast's 1.5 GB above the free 512 MB

# ---- S3 requests ------------------------------------------------------------------------------------------------
ops = sim["s3_calls_by_operation"]
cap_put_tier = round((ops["PutObject"] + ops.get("ListObjectsV2", 0)) * per_day * DAYS)
cap_get_tier = round((ops["GetObject"] + ops.get("HeadObject", 0)) * per_day * DAYS)
cap_requests = cap_put_tier * P_PUT + cap_get_tier * P_GET

# Forecast, counted from the code at 8246a8b (lambda_handler.process_pending, extract.run_covered and _write), with
# the Lambda defaults (12 h lookback, so 13 runs; 8 viewpoints). Every call reads each run's marker (13 GETs), HEADs
# each written run's 8 forecast files, and reads each pending run's failure record: 26 + 7 x W GET-tier requests for
# W written runs. W is 12 with one run pending (110), 11 while the previous run still waits for its last hours (103),
# 13 if the newest run is written within its own hour (117). An extraction adds 11 PUTs (8 forecast files, the
# marker, the log rewrite, the lock) and 2 GETs (the marker re-read, the log read); releasing the lock is a DELETE.
FC_RUNS_IN_LOOKBACK = 13
FC_VIEWPOINTS = 8


def fc_call_get_tier(written: int) -> int:
    return FC_RUNS_IN_LOOKBACK + FC_VIEWPOINTS * written + (FC_RUNS_IN_LOOKBACK - written)


fc_call_get = [fc_call_get_tier(11), fc_call_get_tier(13)]
fc_call_get_steady = fc_call_get_tier(12)
FC_RUN_PUT = 8 + 1 + 1 + 1
FC_RUN_GET = 2
fc_put_tier = FC_EXTRACT_CALLS * FC_RUN_PUT
fc_get_tier = [RUNS * g + FC_EXTRACT_CALLS * FC_RUN_GET for g in fc_call_get]
fc_requests = [fc_put_tier * P_PUT + g * P_GET for g in fc_get_tier]
fc_lookback_heads = RUNS * FC_VIEWPOINTS * 12  # the steady state's HEADs alone
s3_requests = [cap_requests + f for f in fc_requests]

# ---- S3 storage -------------------------------------------------------------------------------------------------
frames_mb_day = 262.0  # measured: D-028's frame, thumbnail and sidecar sizes at the 4-an-hour cap (19 Nov)
light_file_kb = 31.274  # measured: mean of 249 local forecast files (8 fields, f00-f02), 20 Nov


# Extrapolated: the full profile (Terraform sets no SNOW_GLOBE_FORECAST_* variable, so all 22 fields and every hour),
# from the measured layout of a light file: ~5.7-8 B per value, ~0.70 KB + ~0.10 KB per field for each hour, ~0.65 KB
# per field of metadata, ~13.8 KB fixed; 57 points.
def full_file_kb(hours: int, b_per_value: float) -> float:
    values = 22 * hours * 57 * b_per_value / 1000
    return values + hours * (0.70 + 0.10 * 22) + 0.65 * 22 + 13.8


fc_mb_day = [8 * (20 * full_file_kb(19, b) + 4 * full_file_kb(49, b)) / 1000 for b in (5.7, 8.0)]
cap_log_mb_day = sim["capture_log_bytes_counted"] * per_day / 1e6  # simulated day, real records
FC_LOG_MB_DAY = 1.0  # assumed (the stand-in's light profile logged 0.18 MB on 19 Nov while the Mac slept)
cur_mb_day = [frames_mb_day + f + cap_log_mb_day + FC_LOG_MB_DAY for f in fc_mb_day]

# Previous versions the lifecycle rules keep (the standing amount once steady): what a simulated day made
# noncurrent, kept 1-2 days (logs, cache/) or 30-31 days (state documents, locks); a rule's days count to the next
# midnight UTC. The staged frames' bytes are D-028's every-frame sizes at the six hourly cameras (the simulation's
# images are synthetic). Forecast's own previous versions (its log and markers) are under 0.1 GB and left out.
nc_mb_day = {k: v * per_day / 1e6 for k, v in sim["noncurrent_bytes_by_prefix"].items()}
STAGED_MB_DAY = 55.2 + 48.9 + 55.5 + 46.8 + 38.6 + 59.7  # measured (D-028, "old": every frame)
noncurrent_gb = [
    (
        nc_mb_day["capture_log/"] * d1
        + nc_mb_day["state/capture/"] * d30
        + STAGED_MB_DAY * d1
        + nc_mb_day.get("state/locks/", 0.0) * d30
    )
    / 1000
    for d1, d30 in ((1, 30), (2, 31))
]


# A month's storage: current objects as at the month's midpoint (they grow linearly), and previous versions ramping
# up to their standing amount over the first 30 days.
def current_cost(m: int, i: int) -> float:
    return cur_mb_day[i] * (m - 0.5) * DAYS / 1000 * P_STORE


def noncurrent_cost(m: int, i: int) -> float:
    return noncurrent_gb[i] * min(1.0, (m - 0.5) * DAYS / 30) * P_STORE


storage = {m: [current_cost(m, i) + noncurrent_cost(m, i) for i in (LO, HI)] for m in range(1, 7)}
day90_gb = [d * 90 / 1000 for d in cur_mb_day]
day90_rate = [(day90_gb[i] + noncurrent_gb[i]) * P_STORE for i in (LO, HI)]

# ---- ECR ----------------------------------------------------------------------------------------------------------
image_bytes = 293_843_493 + 5_717 + 2_270  # measured: layers + config + manifest, lookout-lambda:8246a8bd570b
base_bytes = 200_297_165  # measured: the pinned AWS base's layers, identical in every build of this pin
ecr_gb = [(base_bytes + 5 * (image_bytes - base_bytes)) / GB, 5 * image_bytes / GB]  # base stored once, or 5 times
ecr = [g * P_ECR for g in ecr_gb]

# ---- the rest: zero with the free tiers -----------------------------------------------------------------------------
LOG_KB_PER_RUN = (1.0, 5.0)  # assumed
log_gb = [2 * RUNS * kb / 1e6 for kb in LOG_KB_PER_RUN]
logs_none = [g * P_LOG_INGEST for g in log_gb]
alarms_none = 4 * P_ALARM
scheduler_none = 2 * RUNS * P_SCHEDULER

# ---- totals -------------------------------------------------------------------------------------------------------
CAP_USD = 3.0  # PRD §17's hosting hard cap, monthly (D-035)


def total(m: int, i: int, free: bool) -> float:
    base = s3_requests[i] + storage[m][i] + ecr[i] + tmp_usd[i]
    if free:
        return base + compute_free[i]
    return base + compute_none[i] + lambda_requests_none + logs_none[i] + alarms_none + scheduler_none


totals_free = {m: [total(m, i, True) for i in (LO, HI)] for m in storage}
totals_none = {m: [total(m, i, False) for i in (LO, HI)] for m in storage}
first_over_cap = [next((m for m in storage if totals_free[m][i] > CAP_USD), None) for i in (LO, HI)]


def cap_day(m: int, i: int) -> float | None:
    """The day of month m on which spend passes the cap, with the free tiers, or None if it does not.

    Everything but Lambda compute accrues evenly over the month (storage, which grows, a little faster at the end);
    compute accrues only once the month's free GB-s are used up, at the even rate of the month's GB-s.
    """
    other = totals_free[m][i] - compute_free[i]
    gbs_day = lambda_gbs[i] / DAYS
    free_until = min(float(DAYS), FREE_LAMBDA_GBS / gbs_day)
    if other / DAYS * free_until >= CAP_USD:
        day = CAP_USD / (other / DAYS)
    else:
        day = (CAP_USD + gbs_day * P_LAMBDA_GBS * free_until) / (other / DAYS + gbs_day * P_LAMBDA_GBS)
    return round(day, 1) if day <= DAYS else None


cap_days = {m: [cap_day(m, i) for i in (LO, HI)] for m in storage}


def r(values: list[float], digits: int = 2) -> list[float]:
    """Round each value once; dollar figures go to cents straight from the unrounded value (no double rounding)."""
    return [round(v, digits) for v in values]


out = {
    "capture_version": sim["capture_version"],
    "lambda": {
        "capture_sleep_s_simulated": capture_sleep_s,
        "capture_sleep_by_caller_s": sim.get("fake_sleep_seconds_per_pass_by_caller"),
        "capture_pass_s": r(capture_s, 1),
        "capture_gbs": r(capture_gbs, 0),
        "forecast_gbs": r(forecast_gbs, 0),
        "total_gbs": r(lambda_gbs, 0),
        "over_free_gbs": r([max(0.0, g - FREE_LAMBDA_GBS) for g in lambda_gbs], 0),
        "compute_usd_free_tier": r(compute_free),
        "compute_usd_no_free_tier": r(compute_none),
        "requests_usd_no_free_tier": round(lambda_requests_none, 4),
        "tmp_usd": r(tmp_usd, 4),
    },
    "s3_requests": {
        "capture": {"put_tier": cap_put_tier, "get_tier": cap_get_tier, "usd": round(cap_requests, 2)},
        "forecast": {
            "per_call_get_tier": fc_call_get,
            "per_call_get_tier_one_run_pending": fc_call_get_steady,
            "put_tier": fc_put_tier,
            "get_tier": fc_get_tier,
            "lookback_heads_steady": fc_lookback_heads,
            "lookback_heads_usd": round(fc_lookback_heads * P_GET, 2),
            "usd": r(fc_requests),
        },
        "usd": r(s3_requests),
    },
    "s3_storage": {
        "forecast_file_kb": {
            "18h": [round(full_file_kb(19, b), 1) for b in (5.7, 8.0)],
            "48h": [round(full_file_kb(49, b), 1) for b in (5.7, 8.0)],
        },
        "forecast_mb_day": r(fc_mb_day, 1),
        "light_forecast_mb_day": round(8 * 24 * light_file_kb / 1000, 1),
        "capture_log_mb_day": round(cap_log_mb_day, 2),
        "current_mb_day": r(cur_mb_day, 1),
        "current_avg_gb_by_month": {
            m: r([cur_mb_day[i] * (m - 0.5) * DAYS / 1000 for i in (LO, HI)], 1) for m in (1, 2, 3)
        },
        "day90_current_gb": r(day90_gb, 1),
        "current_usd_by_month": {m: r([current_cost(m, i) for i in (LO, HI)]) for m in range(1, 7)},
        "noncurrent_mb_day": {k: round(v, 1) for k, v in nc_mb_day.items()},
        "noncurrent_standing_gb": r(noncurrent_gb),
        "noncurrent_usd_by_month": {m: r([noncurrent_cost(m, i) for i in (LO, HI)]) for m in (1, 2)},
        "usd_by_month": {m: r(v) for m, v in storage.items()},
        "day90_rate_usd": r(day90_rate),
        "frames_only_day90_usd": round(23.6 * P_STORE, 2),
        "frames_only_month_avg_usd": [round(frames_mb_day * ((m - 0.5) * DAYS) / 1000 * P_STORE, 2) for m in (1, 2, 3)],
        "growth_gb_per_month": r([d * DAYS / 1000 for d in cur_mb_day], 1),
    },
    "ecr": {"gb": r(ecr_gb, 3), "usd": r(ecr)},
    "no_free_tier_extras": {
        "logs_gb": r(log_gb, 3),
        "logs_usd": r(logs_none),
        "alarms_usd": alarms_none,
        "scheduler_usd": round(scheduler_none, 2),
    },
    "total_usd_free_tiers_by_month": {m: r(v) for m, v in totals_free.items()},
    "total_usd_no_free_tier_by_month": {m: r(v) for m, v in totals_none.items()},
    "first_month_over_cap_free_tiers": first_over_cap,
    "free_gbs_used_up_on_day": r([min(float(DAYS), FREE_LAMBDA_GBS / (g / DAYS)) for g in lambda_gbs], 1),
    "cap_day_by_month_free_tiers": cap_days,
}
print(json.dumps(out, indent=1))
