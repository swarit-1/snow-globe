#!/usr/bin/env bash
# Tests for the hosting estimate (PRD §11.3, §17; infra/README.md "Monthly cost estimate"). No network.
#
#   1. infra/cost/estimate.py runs on the committed simulation output (cost/sim_capture_8fa6b6a.json), with the
#      standard library only.
#   2. Its arithmetic holds together: capture's pass time is the simulated waiting plus the assumed 5-20 s, every
#      range is low <= high, totals grow month by month, the forecast count is 143 - 2 W per call and 19 GET-tier
#      requests per extraction, and the pooled estimate lies within the range of the runs priced alone.
#   3. The committed simulation drew its NPS request times from the committed measurements
#      (cost/nps_request_times_0.2.1.json, by SHA-256): capture 0.2.1 and later count a camera's slot from the
#      previous answer, so an output made without request times understates the waiting (D-066). Its waiting, NPS
#      time, S3 time and pass time add up.
#   4. It pools at least five runs that differ only in the seed (one run is one draw of the request times), and
#      cost/sim_pool.py pools its replicates into exactly the committed file again.
#   5. cost/nps_request_times.py sorts a synthetic log's requests by stage and connection state.
#   6. Forecast's S3 requests in estimate.py are the ones the forecast code makes: cost/count_forecast_s3.py runs
#      process_pending on this tree with a counting storage (SKIP without workers/forecast's venv).
#   7. sim_capture_s3.py's mechanics: short simulations of this tree with --check (pass time = sleeps + request
#      times; moto's store = ListObjectVersions; the sweeps delete and count exactly the old versions; moto's key
#      list is found and freed), with the measured request times, then with made-up ones (each request must take its
#      pool's value) and 20 ms per S3 request (SKIP without a venv that has moto: workers/capture's, else
#      workers/common's, which make test-infra syncs).
#   8. infra/README.md quotes the figures estimate.py prints, so the table and the arithmetic cannot drift apart; it
#      says the estimate is before tax while the budget counts tax, and it does not state D-035's proposal as decided.
#   9. The cost scripts compile.
#  10. ruff check and ruff format --check on infra/cost (cost/ruff.toml, no cache written), when ruff is installed.
#
# Run: infra/tests/test_cost.sh   (bash 3.2 compatible; exits non-zero on the first failure)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA="$(cd "$HERE/.." && pwd)"
REPO="$(cd "$INFRA/.." && pwd)"
COST="$INFRA/cost"
SIM="$COST/sim_capture_8fa6b6a.json"       # the costed commit's simulation output, seeds pooled
TIMES="$COST/nps_request_times_0.2.1.json" # the measured request times it drew from

PASSED=0
SKIPPED=0
CURRENT=""
fail() {
  printf '  FAIL %s: %s\n' "$CURRENT" "$*"
  exit 1
}
ok() {
  printf '  ok   %s\n' "$CURRENT"
  PASSED=$((PASSED + 1))
}
skip() {
  printf '  SKIP %s (%s)\n' "$CURRENT" "$*"
  SKIPPED=$((SKIPPED + 1))
}

echo "infra/tests/test_cost.sh"
command -v python3 >/dev/null 2>&1 || { echo "  SKIP python3 not installed"; exit 0; }
WORK="$(mktemp -d "${TMPDIR:-/tmp}/snow-globe-cost-test.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

CURRENT="estimate_runs_on_the_committed_simulation"
python3 -I "$COST/estimate.py" "$SIM" >"$WORK/estimate.json" 2>"$WORK/estimate.err" ||
  fail "$(cat "$WORK/estimate.err")"
ok

CURRENT="estimate_arithmetic_holds_together"
python3 -I - "$WORK/estimate.json" "$SIM" >"$WORK/check.out" 2>&1 <<'EOF' || fail "$(cat "$WORK/check.out")"
import json
import sys

est = json.load(open(sys.argv[1]))
sim = json.load(open(sys.argv[2]))
lam, req = est["lambda"], est["s3_requests"]
problems = []


def check(cond, msg):
    if not cond:
        problems.append(msg)


wait = sim.get("fake_wait_seconds", sim["fake_pass_seconds"])["mean"]  # an older output: all of a pass is waiting
check(lam["capture_sleep_s_simulated"] == wait, "capture's billed waiting must be the simulation's mean waiting")
check(lam["capture_pass_s"] == [round(wait + 5, 1), round(wait + 20, 1)], "capture pass = waiting + 5-20 s assumed")
check(lam["capture_gbs"] == [round(0.5 * (wait + s) * 8640) for s in (5, 20)], "capture GB-s = 0.5 GB x pass x 8,640")
for name, pair in [
    ("lambda total GB-s", lam["total_gbs"]),
    ("compute with the free tier", lam["compute_usd_free_tier"]),
    ("compute without it", lam["compute_usd_no_free_tier"]),
    ("S3 requests", req["usd"]),
    *[(f"total, month {m}", v) for m, v in est["total_usd_free_tiers_by_month"].items()],
]:
    check(pair[0] <= pair[1], f"{name}: low {pair[0]} > high {pair[1]}")
months = est["total_usd_free_tiers_by_month"]
for i in (0, 1):
    series = [months[str(m)][i] for m in range(1, 7)]
    check(series == sorted(series), f"totals must grow month by month: {series}")
fc = req["forecast"]
check(fc["per_call_get_tier"] == [117, 121], "forecast per call: 143 - 2 W for W = 13 and 11")
check(fc["per_call_get_tier_one_run_pending"] == 119, "forecast per call with one run pending (W = 12): 119")
check((fc["per_extraction_get_tier"], fc["per_extraction_put_tier"]) == (19, 11), "forecast per extraction: 19 and 11")
check(fc["get_tier"] == [8640 * g + 720 * 19 for g in (117, 121)], "forecast GET tier = calls x per call + extractions")
check(fc["put_tier"] == 720 * 11, "forecast PUT tier = 720 extractions x 11")
ops, month = sim["s3_calls_by_operation"], 30 * 24 / sim["hours_counted"]
check(req["capture"]["put_tier"] == round((ops["PutObject"] + ops["ListObjectsV2"]) * month), "capture PUT tier a month")
check(req["capture"]["get_tier"] == round((ops["GetObject"] + ops["HeadObject"]) * month), "capture GET tier a month")
spread = est.get("seed_spread")
check(spread is not None, "the committed output pools several runs, so the estimate must price each one too")
if spread is not None:
    check(len(spread["capture_wait_s_by_run"]) == spread["runs"] == len(sim["replicates"]), "one estimate per run")
    lo, hi = spread["capture_wait_s"]
    check(lo <= wait <= hi, f"the pooled waiting {wait} s must lie within the runs' {lo}-{hi} s")
    for m in ("1", "2", "3"):
        for i, end in ((0, "low"), (1, "high")):
            a, b = spread["total_usd_free_tiers_by_month"][m][end]
            check(a - 0.01 <= months[m][i] <= b + 0.01, f"month {m} {end}: {months[m][i]} outside the runs' {a}-{b}")
if problems:
    print("\n".join(problems))
    sys.exit(1)
EOF
ok

CURRENT="simulation_drew_the_committed_request_times"
python3 -I - "$SIM" "$TIMES" >"$WORK/times.out" 2>&1 <<'PY' || fail "$(cat "$WORK/times.out")"
import hashlib
import json
import os
import sys

sim = json.load(open(sys.argv[1]))
raw = open(sys.argv[2], "rb").read()
times, used = json.loads(raw), sim.get("request_times")
problems = []


def check(cond, msg):
    if not cond:
        problems.append(msg)


check(used is not None, "the committed simulation ran without request times: its waiting is the zero-latency figure")
if used is not None:
    check(used["file"] == os.path.basename(sys.argv[2]), f"it drew from {used['file']}, not the committed file")
    check(used["sha256"] == hashlib.sha256(raw).hexdigest(), "the request-times file changed after the simulation ran")
    check(used["source"] == times["source"], "the simulation's record of the measurements must match the file")
    drawn, sent = sum(used["drawn_counted"].values()), sum(sim["nps_requests_counted_by_stage"].values())
    check(drawn == sent, f"every counted NPS request draws a time: {drawn} drawn, {sent} sent")
if used is None or "fake_wait_seconds" not in sim:
    print("\n".join(problems or ["the committed simulation has no fake_wait_seconds"]))
    sys.exit(1)
wait, whole = sim["fake_wait_seconds"]["mean"], sim["fake_pass_seconds"]["mean"]
spent = sim["fake_nps_request_seconds_per_pass"] + sim.get("fake_s3_seconds_per_pass", 0.0)
check(abs(wait + spent - whole) <= 0.02, "a pass is its waiting plus its NPS and S3 time")
check(abs(sum(sim["fake_sleep_seconds_per_pass_by_caller"].values()) - wait) <= 0.02, "the waiting is the sleeps")
check(sim["warmup_hours"] >= 24, "the counted window starts after the first day's catch-up")
check(sim.get("s3_latency_ms", 0.0) == 0.0, "the committed runs give S3 no time (a latency run is a sensitivity input)")
counts = {stage: {s: len(v) for s, v in by.items()} for stage, by in times["elapsed_ms"].items()}
check(counts == times["counts"], "the request-times file's counts must match its lists")
check(sum(n for by in counts.values() for n in by.values()) == times["source"]["answered"], "answered = listed")
if problems:
    print("\n".join(problems))
    sys.exit(1)
PY
ok

CURRENT="simulation_pools_five_seeds_or_more"
python3 -I - "$SIM" "$WORK/replicates" >"$WORK/pool.out" 2>&1 <<'PY' || fail "$(cat "$WORK/pool.out")"
import json
import os
import sys

sim = json.load(open(sys.argv[1]))
reps = sim.get("replicates") or []
problems = []
seeds = [r["request_times"]["seed"] for r in reps if r.get("request_times")]
if len(reps) < 5 or len(set(seeds)) != len(reps):
    problems.append(f"the committed output must pool at least 5 runs with distinct seeds: seeds {seeds}")
if sim.get("request_times", {}).get("seeds") != seeds:
    problems.append("the pool's seeds must be its replicates' seeds")
for key in ("capture_version", "warmup_hours", "s3_latency_ms"):
    if any(r.get(key) != sim.get(key) for r in reps):
        problems.append(f"every run must share the pool's {key}")
if problems:
    print("\n".join(problems))
    sys.exit(1)
os.makedirs(sys.argv[2])
for i, rep in enumerate(reps):
    with open(os.path.join(sys.argv[2], f"run{i:02d}.json"), "w") as handle:
        json.dump(rep, handle)
PY
python3 -I "$COST/sim_pool.py" "$WORK/pooled.json" "$WORK"/replicates/run*.json >"$WORK/pool.out" 2>&1 ||
  fail "$(cat "$WORK/pool.out")"
python3 -I -c 'import json, sys; sys.exit(json.load(open(sys.argv[1])) != json.load(open(sys.argv[2])))' \
  "$WORK/pooled.json" "$SIM" || fail "cost/sim_pool.py pools the replicates into something other than the committed file"
ok

CURRENT="request_times_tool_sorts_by_stage_and_connection"
# Seven requests of the loop at version 0.2.1 before 00:05, under a made-up package name (the tool matches the
# version): the first is cold; the image 1 s after it is warm; the HEAD 6 s after the image's answer is cold; a 30 s
# failure is left out, and the poll 0.65 s after its end is cold (httpcore closes a connection whose response failed);
# a poll exactly 5.000 s after the previous answer is warm, an image 5.001 s after its poll's answer cold (httpcore's
# keep-alive expires once the clock is past answer + 5 s). Another runner, another version, an archive request, a
# request after --until and a torn last line are not read.
cat >"$WORK/log.jsonl" <<'JSONL'
{"event":"request","runner":"cli-loop","capture_version":"pkg/0.2.1","stage":"json","sent_at":"2026-11-21T00:00:00.000Z","answered_at":"2026-11-21T00:00:00.100Z","status":200,"elapsed_ms":100.0}
{"event":"request","runner":"cli-loop","capture_version":"pkg/0.2.1","stage":"image","sent_at":"2026-11-21T00:00:01.100Z","answered_at":"2026-11-21T00:00:01.300Z","status":200,"elapsed_ms":200.0}
{"event":"request","runner":"cli-loop","capture_version":"pkg/0.2.1","stage":"head","sent_at":"2026-11-21T00:00:07.300Z","answered_at":"2026-11-21T00:00:07.350Z","status":304,"elapsed_ms":50.0}
{"event":"request","runner":"cli-loop","capture_version":"pkg/0.2.1","stage":"json","sent_at":"2026-11-21T00:00:08.350Z","answered_at":"2026-11-21T00:00:38.350Z","status":null,"elapsed_ms":30000.0}
{"event":"request","runner":"cli-loop","capture_version":"pkg/0.2.1","stage":"json","sent_at":"2026-11-21T00:00:39.000Z","answered_at":"2026-11-21T00:00:39.070Z","status":200,"elapsed_ms":70.0}
{"event":"request","runner":"cli-loop","capture_version":"pkg/0.2.1","stage":"json","sent_at":"2026-11-21T00:00:44.070Z","answered_at":"2026-11-21T00:00:44.110Z","status":200,"elapsed_ms":40.0}
{"event":"request","runner":"cli-loop","capture_version":"pkg/0.2.1","stage":"image","sent_at":"2026-11-21T00:00:49.111Z","answered_at":"2026-11-21T00:00:49.411Z","status":200,"elapsed_ms":300.0}
{"event":"request","runner":"lambda","capture_version":"pkg/0.2.1","stage":"json","sent_at":"2026-11-21T00:00:40.000Z","answered_at":"2026-11-21T00:00:40.010Z","status":200,"elapsed_ms":10.0}
{"event":"request","runner":"cli-loop","capture_version":"pkg/0.2.0","stage":"json","sent_at":"2026-11-21T00:00:41.000Z","answered_at":"2026-11-21T00:00:41.010Z","status":200,"elapsed_ms":10.0}
{"event":"archive_request","runner":"cli-loop","capture_version":"pkg/0.2.1","sent_at":"2026-11-21T00:00:42.000Z"}
{"event":"request","runner":"cli-loop","capture_version":"pkg/0.2.1","stage":"json","sent_at":"2026-11-21T00:10:00.000Z","answered_at":"2026-11-21T00:10:00.010Z","status":200,"elapsed_ms":10.0}
{"event":"request","runner":"cli-
JSONL
python3 -I "$COST/nps_request_times.py" --version 0.2.1 --until 2026-11-21T00:05:00Z --out "$WORK/times.json" \
  "$WORK/log.jsonl" >"$WORK/tool.out" 2>&1 || fail "$(cat "$WORK/tool.out")"
python3 -I - "$WORK/times.json" >"$WORK/tool.check" 2>&1 <<'PY' || fail "$(cat "$WORK/tool.check")"
import json
import sys

doc = json.load(open(sys.argv[1]))
want = {
    "json": {"cold": [70.0, 100.0], "warm": [40.0]},
    "head": {"cold": [50.0], "warm": []},
    "image": {"cold": [300.0], "warm": [200.0]},
}
problems = []
if doc["elapsed_ms"] != want:
    problems.append(f"pools {doc['elapsed_ms']} != {want}")
got = (doc["source"]["requests"], doc["source"]["answered"], doc["source"]["left_out_unanswered"])
if got != (7, 6, 1):
    problems.append(f"requests, answered, left out: {got} != (7, 6, 1)")
if doc["counts"] != {"json": {"cold": 2, "warm": 1}, "head": {"cold": 1, "warm": 0}, "image": {"cold": 1, "warm": 1}}:
    problems.append(f"counts {doc['counts']}")
if problems:
    print("\n".join(problems))
    sys.exit(1)
PY
ok

CURRENT="forecast_requests_are_the_codes"
FORECAST_PY="$REPO/workers/forecast/.venv/bin/python"
if [ -x "$FORECAST_PY" ] && "$FORECAST_PY" -I -c 'import eccodes, numpy, httpx' >/dev/null 2>&1; then
  # -B: no bytecode written into the (iCloud-synced) tree.
  "$FORECAST_PY" -I -B "$COST/count_forecast_s3.py" "$REPO" >"$WORK/forecast.json" 2>"$WORK/forecast.err" ||
    fail "$(tail -5 "$WORK/forecast.err")"
  python3 -I - "$WORK/forecast.json" "$WORK/estimate.json" >"$WORK/forecast.out" 2>&1 <<'PY' || fail "$(cat "$WORK/forecast.out")"
import json
import sys

ran = json.load(open(sys.argv[1]))
fc = json.load(open(sys.argv[2]))["s3_requests"]["forecast"]
counted = {"11": fc["per_call_get_tier"][1], "12": fc["per_call_get_tier_one_run_pending"], "13": fc["per_call_get_tier"][0]}
problems = []
if ran["idle_call_get_tier_by_written_runs"] != counted:
    problems.append(f"per idle call by W: the code makes {ran['idle_call_get_tier_by_written_runs']}, estimate.py counts {counted}")
if any(ran["idle_call_put_tier_by_written_runs"].values()):
    problems.append(f"an idle call writes: {ran['idle_call_put_tier_by_written_runs']}")
one = ran["per_extraction"]
if (one["get_tier"], one["put_tier"]) != (fc["per_extraction_get_tier"], fc["per_extraction_put_tier"]):
    problems.append(f"per extraction: the code makes {one}, estimate.py counts {fc['per_extraction_get_tier']} and {fc['per_extraction_put_tier']}")
if problems:
    print("forecast's S3 requests changed: re-count them in infra/cost/estimate.py and re-price (infra/README.md)")
    print("\n".join(problems))
    sys.exit(1)
PY
  ok
else
  skip "workers/forecast/.venv with ecCodes not found"
fi

CURRENT="simulation_mechanics_short_run"
SIM_PY=""
for venv in "$REPO/workers/capture/.venv" "$REPO/workers/common/.venv"; do
  if [ -x "$venv/bin/python" ] && "$venv/bin/python" -I -c 'import boto3, httpx, moto, PIL, pytest' >/dev/null 2>&1; then
    SIM_PY="$venv/bin/python"
    break
  fi
done
if [ -n "$SIM_PY" ]; then
  # Run 1: the measured request times, 2 h warm-up then 2 h counted, every self-check on. Run 2: 1 h + 1 h with 20 ms
  # an S3 request and made-up request times, one value per stage and connection state (30 of each, so no pool is
  # merged), so that the NPS time must be exactly the draws' values. The data dir is the test's own (the HTTP
  # limiter's local state); -B writes no bytecode into the tree.
  python3 -I -c '
import json, sys
ms = {"json": (500.0, 100.0), "head": (400.0, 200.0), "image": (600.0, 300.0)}
doc = {"source": {"made_up": True}, "elapsed_ms": {k: {"cold": [c] * 30, "warm": [w] * 30} for k, (c, w) in ms.items()}}
json.dump(doc, open(sys.argv[1], "w"))' "$WORK/fixed_times.json"
  SNOW_GLOBE_DATA_DIR="$WORK/sim-data" "$SIM_PY" -I -B "$COST/sim_capture_s3.py" --request-times "$TIMES" \
    --warmup-hours 2 --check "$REPO" 2 "$WORK/sim_check.json" >"$WORK/sim.out" 2>&1 || fail "$(tail -5 "$WORK/sim.out")"
  SNOW_GLOBE_DATA_DIR="$WORK/sim-data-s3" "$SIM_PY" -I -B "$COST/sim_capture_s3.py" --request-times \
    "$WORK/fixed_times.json" --warmup-hours 1 --check --s3-latency-ms 20 "$REPO" 1 "$WORK/sim_s3.json" \
    >"$WORK/sim.out" 2>&1 || fail "$(tail -5 "$WORK/sim.out")"
  python3 -I - "$WORK/sim_check.json" "$WORK/sim_s3.json" >"$WORK/sim.check" 2>&1 <<'PY' || fail "$(cat "$WORK/sim.check")"
import json
import sys

problems = []
fixed = json.load(open(sys.argv[2]))
value_s = {"json": (0.5, 0.1), "head": (0.4, 0.2), "image": (0.6, 0.3)}
drawn = fixed["request_times"]["drawn_counted"]
expect = sum(n * value_s[k.split()[0]][0 if k.endswith("cold") else 1] for k, n in drawn.items())
if abs(fixed["fake_seconds_total"]["nps_requests"] - expect) > 0.01:
    problems.append(f"each NPS request must take its pool's time: {fixed['fake_seconds_total']['nps_requests']} s, not {expect:.3f}")
cold = sum(n for k, n in drawn.items() if k.endswith("cold"))
if cold < fixed["passes_counted"] or cold == sum(drawn.values()):
    problems.append(f"each pass's first request is cold, and later ones within 5 s warm: {drawn}")
for path, hours, latency_ms in ((sys.argv[1], 4, 0.0), (sys.argv[2], 2, 20.0)):
    sim = json.load(open(path))
    name = f"{hours} h run, {latency_ms:g} ms S3"
    total = sim["fake_seconds_total"]
    calls = sum(sim["s3_calls_by_operation"].values())
    if sim["checked"] != {"passes": hours * 12, "sweeps": hours + 1}:
        problems.append(f"{name}: every pass and sweep is checked: {sim['checked']}")
    if sum(sim["request_times"]["drawn_counted"].values()) != sum(sim["nps_requests_counted_by_stage"].values()):
        problems.append(f"{name}: every counted NPS request draws a measured time")
    if abs(total["wait"] + total["nps_requests"] + total["s3_requests"] - total["pass"]) > 0.01:
        problems.append(f"{name}: the passes are their waiting plus their request times: {total}")
    if abs(sum(total["sleep_by_caller"].values()) - total["wait"]) > 0.01:
        problems.append(f"{name}: the waiting is the code's sleeps: {total}")
    if abs(total["s3_requests"] - latency_ms / 1000 * calls) > 0.01:
        problems.append(f"{name}: each of the {calls} counted S3 requests takes {latency_ms:g} ms: {total['s3_requests']} s")
    if sim["moto_keys_disposed"] <= 0:
        problems.append(f"{name}: the sweeps must free moto's copies of old versions")
    for prefix in ("capture_log/", "state/capture/", "state/locks/", "cache/staging/"):
        if sim["noncurrent_versions_by_prefix"].get(prefix, 0) <= 0:
            problems.append(f"{name}: no old version of {prefix} counted: {sim['noncurrent_versions_by_prefix']}")
    if sim["passes_counted"] != hours / 2 * 12 or sim["outcomes"].get("stored", 0) <= 0:
        problems.append(f"{name}: {sim['passes_counted']} passes counted, outcomes {sim['outcomes']}")
if problems:
    print("\n".join(problems))
    sys.exit(1)
PY
  ok
else
  skip "no venv with moto, boto3, httpx, Pillow and pytest: run 'uv sync' in workers/common"
fi

CURRENT="readme_quotes_the_estimate"
python3 -I - "$WORK/estimate.json" "$INFRA/README.md" "$SIM" >"$WORK/readme.out" 2>&1 <<'EOF' || fail "$(cat "$WORK/readme.out")"
import json
import sys

est = json.load(open(sys.argv[1]))
text = open(sys.argv[2], encoding="utf-8").read()
sim = json.load(open(sys.argv[3]))
section = text[text.index("## Monthly cost estimate") : text.index("\n## Variables")]
budget = text[text.index("**What the budget counts") : text.index("### Spend limits")]
lam, req, sto = est["lambda"], est["s3_requests"], est["s3_storage"]
free, none = est["total_usd_free_tiers_by_month"], est["total_usd_no_free_tier_by_month"]
spread = est["seed_spread"]


def usd(pair):
    lo, hi = (f"{v:.2f}" for v in pair)
    return f"${lo}" if lo == hi else f"${lo}–{hi}"


def n(value):
    return f"{value:,.0f}"


def s(pair, digits=2):
    lo, hi = (f"{v:.{digits}f}" for v in pair)
    return lo if lo == hi else f"{lo}–{hi}"


want = [
    f"for the code at commit `8fa6b6a` ({est['capture_version'].replace('snow-globe-capture/', 'capture ').replace('lookout-capture/', 'capture ')})",
    f"Capture 0.5 GB × {lam['capture_pass_s'][0]}–{lam['capture_pass_s'][1]} s × 8,640",
    f"({lam['capture_sleep_s_simulated']} s of simulated waiting plus 5–20 s assumed)",
    f"{n(lam['capture_gbs'][0])}–{n(lam['capture_gbs'][1])} GB-s",
    f"{n(lam['forecast_gbs'][0])}–{n(lam['forecast_gbs'][1])} GB-s",
    f"In all {n(lam['total_gbs'][0])}–{n(lam['total_gbs'][1])} GB-s",
    f"**{usd([0, lam['compute_usd_free_tier'][1]]).replace('$0.00', '$0')}** with the free tier",
    f"the top of the range is {n(lam['over_free_gbs'][1])} GB-s over it",
    f"Without it, {usd(lam['compute_usd_no_free_tier'])}.",
    f"GET and HEAD, ${req['capture']['usd']:.2f}.",
    f"{n(req['forecast']['get_tier'][0])}–{n(req['forecast']['get_tier'][1])} GET and HEAD, {usd(req['forecast']['usd'])};",
    f"lookback HEADs alone are {n(req['forecast']['lookback_heads_steady'])} (${req['forecast']['lookback_heads_usd']:.2f})",
    f"That is {req['forecast']['per_call_get_tier_one_run_pending']} with one run pending (W = 12), "
    f"{req['forecast']['per_call_get_tier'][1]} while the previous run still waits for its last hours, and "
    f"{req['forecast']['per_call_get_tier'][0]} if the newest run is complete within its own hour.",
    f"and {req['forecast']['per_extraction_get_tier']} GET-tier requests",
    f"{n(req['forecast']['added_since_8246a8b_steady'])} more GET-tier requests a month than 8246a8b's code at the "
    f"steady state, ${req['forecast']['added_since_8246a8b_usd']:.2f}",
    f"| **{usd(req['usd'])}** |",
    f"**{usd(sto['current_usd_by_month']['1'])}** (month 1), **{usd(sto['current_usd_by_month']['2'])}** (month 2), "
    f"**{usd(sto['current_usd_by_month']['3'])}** (month 3)",
    f"**{usd(sto['noncurrent_usd_by_month']['1'])}** (month 1), **{usd(sto['noncurrent_usd_by_month']['2'])}** after",
    f"| **{usd(est['ecr']['usd'])}** |",
    f"With the free tiers: **{usd(free['1'])}** in month 1, **{usd(free['2'])}** in month 2, "
    f"**{usd(free['3'])}** in month 3",
    f"With none: {usd(none['1'])} in month 1.",
    f"the storage rate on day 90 is {usd(sto['day90_rate_usd'])}",
    f"The top, ${free['1'][1]:.2f}, is over it in month 1",
    f"spend passes $3 on about day {round(est['cap_day_by_month_free_tiers']['1'][1])}",
    # The runs priced alone (estimate.py's seed_spread).
    f"Priced from each of the {spread['runs']} runs alone, month 1 is {usd(spread['total_usd_free_tiers_by_month']['1']['low'])} "
    f"at the bottom and {usd(spread['total_usd_free_tiers_by_month']['1']['high'])} at the top",
    f"(the runs: {s(spread['capture_wait_s'])} s)",
]
first = est["first_month_over_cap_free_tiers"]
want.append(f"The bottom of the range passes $3 in month {first[0]}, the top from month {first[1]}.")
# The simulation's own figures that the section quotes.
runs = sim["pooled_runs"]
days = sim["hours_counted"] / 24
ops = {k: n(v / days) for k, v in sim["s3_calls_by_operation"].items()}
wait, per_pass = sim["fake_wait_seconds"], sim["s3_calls_per_pass"]
seeds = sim["request_times"]["seeds"]
want += [
    f"Per day: {ops['PutObject']} PUT, {ops['ListObjectsV2']} LIST, {ops['GetObject']} GET, {ops['HeadObject']} HEAD and "
    f"{ops['DeleteObject']} DELETE",
    f"One pass makes {per_pass['min']}–{per_pass['max']} S3 requests, {round(per_pass['mean'])} on average.",
    f"A pass waits {wait['mean']} s on the simulation's clock on average over the {runs} runs (each run's mean "
    f"{s([min(wait['mean_by_run']), max(wait['mean_by_run'])])} s, median {s([min(wait['p50_by_run']), max(wait['p50_by_run'])], 1)} s, "
    f"95th percentile {s([min(wait['p95_by_run']), max(wait['p95_by_run'])], 1)} s; longest {wait['max']:.1f} s)",
    f"Its NPS requests take {sim['fake_nps_request_seconds_per_pass']} s more",
    f"({sim['not_due_per_camera_per_day']} `not_due` per camera per day",
    f"Stores reached {round(sim['outcomes']['stored'] / days)} a day of the 768 the cap allows.",
    f"from the {sim['request_times']['source']['answered']:,} answered requests",
    f"**{runs} runs, seeds {seeds[0]}–{seeds[-1]}, pooled.**",
    f"Each run warms up for {sim['warmup_hours'] / 24:.0f} simulated days and counts the next {days / runs:.0f}",
]
missing = [w for w in want if w not in section]
# The budget counts tax (the Terraform test pins AWS's default) and the estimate is before tax: both must say so.
for where, phrase in ((section, "before tax"), (budget, "counts tax")):
    if phrase not in where:
        missing.append(phrase)
# D-035 is a proposal for the owner (DECISIONS.md): the README must not state it as decided.
wrong = [p for p in ("D-035 turns the hard stop off", "where D-035 turns") if p in text]
if missing or wrong:
    print("infra/README.md does not quote these figures of infra/cost/estimate.py (re-run it and update the section):")
    print("\n".join(f"  {w}" for w in missing))
    print("\n".join(f"  states a pending decision as made: {w!r}" for w in wrong))
    sys.exit(1)
EOF
ok

CURRENT="cost_scripts_compile"
for script in estimate.py sim_capture_s3.py sim_pool.py nps_request_times.py count_forecast_s3.py; do
  python3 -I -c 'import ast, sys; ast.parse(open(sys.argv[1]).read(), sys.argv[1])' "$COST/$script" ||
    fail "infra/cost/$script does not parse"
done
ok

CURRENT="ruff_on_cost_scripts"
if command -v ruff >/dev/null 2>&1; then
  out="$(ruff check --no-cache --config "$COST/ruff.toml" "$COST" 2>&1 &&
    ruff format --check --no-cache --config "$COST/ruff.toml" "$COST" 2>&1)" || fail "$out"
  [ ! -e "$COST/.ruff_cache" ] || fail "ruff left a cache in infra/cost (iCloud-synced)"
  ok
else
  skip "ruff not installed"
fi

echo "infra/tests/test_cost.sh: $PASSED passed, 0 failed, $SKIPPED skipped"
