#!/usr/bin/env bash
# Tests for the hosting estimate (PRD §11.3, §17; infra/README.md "Monthly cost estimate"). No network.
#
#   1. infra/cost/estimate.py runs on the committed simulation output (cost/sim_capture_8246a8b.json), with the
#      standard library only.
#   2. Its arithmetic holds together: capture's pass time is the simulated waiting plus the assumed 5-20 s, every
#      range is low <= high, totals grow month by month, the forecast count is 26 + 7 W per call.
#   3. infra/README.md quotes the figures it prints, so the table and the arithmetic cannot drift apart.
#   4. sim_capture_s3.py compiles. Running it needs the capture venv and about 20 s, so it is not run here.
#   5. ruff check and ruff format --check on infra/cost (cost/ruff.toml, no cache written), when ruff is installed.
#
# Run: infra/tests/test_cost.sh   (bash 3.2 compatible; exits non-zero on the first failure)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA="$(cd "$HERE/.." && pwd)"
COST="$INFRA/cost"

PASSED=0
CURRENT=""
fail() {
  printf '  FAIL %s: %s\n' "$CURRENT" "$*"
  exit 1
}
ok() {
  printf '  ok   %s\n' "$CURRENT"
  PASSED=$((PASSED + 1))
}

echo "infra/tests/test_cost.sh"
command -v python3 >/dev/null 2>&1 || { echo "  SKIP python3 not installed"; exit 0; }
WORK="$(mktemp -d "${TMPDIR:-/tmp}/lookout-cost-test.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

CURRENT="estimate_runs_on_the_committed_simulation"
python3 -I "$COST/estimate.py" "$COST/sim_capture_8246a8b.json" >"$WORK/estimate.json" 2>"$WORK/estimate.err" ||
  fail "$(cat "$WORK/estimate.err")"
ok

CURRENT="estimate_arithmetic_holds_together"
python3 -I - "$WORK/estimate.json" "$COST/sim_capture_8246a8b.json" >"$WORK/check.out" 2>&1 <<'EOF' || fail "$(cat "$WORK/check.out")"
import json
import sys

est = json.load(open(sys.argv[1]))
sim = json.load(open(sys.argv[2]))
lam, req = est["lambda"], est["s3_requests"]
problems = []


def check(cond, msg):
    if not cond:
        problems.append(msg)


wait = sim["fake_pass_seconds"]["mean"]
check(lam["capture_sleep_s_simulated"] == wait, "capture's billed waiting must be the simulation's mean pass time")
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
check(req["forecast"]["per_call_get_tier"] == [103, 117], "forecast per call: 26 + 7 W for W = 11 and 13")
check(req["forecast"]["per_call_get_tier_one_run_pending"] == 110, "forecast per call with one run pending: 110")
ops = sim["s3_calls_by_operation"]
check(req["capture"]["put_tier"] == (ops["PutObject"] + ops["ListObjectsV2"]) * 30, "capture PUT tier = sim x 30")
check(req["capture"]["get_tier"] == (ops["GetObject"] + ops["HeadObject"]) * 30, "capture GET tier = sim x 30")
if problems:
    print("\n".join(problems))
    sys.exit(1)
EOF
ok

CURRENT="readme_quotes_the_estimate"
python3 -I - "$WORK/estimate.json" "$INFRA/README.md" >"$WORK/readme.out" 2>&1 <<'EOF' || fail "$(cat "$WORK/readme.out")"
import json
import sys

est = json.load(open(sys.argv[1]))
text = open(sys.argv[2], encoding="utf-8").read()
section = text[text.index("## Monthly cost estimate") : text.index("\n## Variables")]
lam, req, sto = est["lambda"], est["s3_requests"], est["s3_storage"]
free, none = est["total_usd_free_tiers_by_month"], est["total_usd_no_free_tier_by_month"]


def usd(pair):
    lo, hi = (f"{v:.2f}" for v in pair)
    return f"${lo}" if lo == hi else f"${lo}–{hi}"


def n(value):
    return f"{value:,.0f}"


want = [
    f"for the code at commit `8246a8b` ({est['capture_version'].replace('lookout-capture/', 'capture ')})",
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
]
first = est["first_month_over_cap_free_tiers"]
want.append(f"The bottom of the range passes $3 in month {first[0]}, the top from month {first[1]}.")
missing = [w for w in want if w not in section]
if missing:
    print("infra/README.md does not quote these figures of infra/cost/estimate.py (re-run it and update the section):")
    print("\n".join(f"  {w}" for w in missing))
    sys.exit(1)
EOF
ok

CURRENT="simulation_compiles"
python3 -I -c 'import ast, sys; ast.parse(open(sys.argv[1]).read(), sys.argv[1])' "$COST/sim_capture_s3.py" ||
  fail "infra/cost/sim_capture_s3.py does not parse"
ok

CURRENT="ruff_on_cost_scripts"
if command -v ruff >/dev/null 2>&1; then
  out="$(ruff check --no-cache --config "$COST/ruff.toml" "$COST" 2>&1 &&
    ruff format --check --no-cache --config "$COST/ruff.toml" "$COST" 2>&1)" || fail "$out"
  [ ! -e "$COST/.ruff_cache" ] || fail "ruff left a cache in infra/cost (iCloud-synced)"
  ok
else
  echo "  SKIP $CURRENT (ruff not installed)"
fi

echo "infra/tests/test_cost.sh: $PASSED passed, 0 failed"
