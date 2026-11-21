#!/usr/bin/env bash
# Tests for infra/ops/migrate-to-snow-globe.sh, the rename's live switch (D-080).
#
# Each case builds a scratch HOME with ~/lookout-data laid out as on the lead's Mac: the three live scripts
# (byte-identical copies in fixtures/runtime-bin/, so the script's SHA-256 check passes), two snapshots (034c46b,
# where the stand-in started, and 3cd2ca7, `current`) whose venvs hold stubs, and the two old loops started from
# those scripts as the lead's session started them. The stubs open no socket: the capture loop takes the NPS
# fetcher lock and logs a `pass` record every second; the stand-in's handler sleeps a second and prints a summary.
# caffeinate, pmset, uv (venv builds and the gate's data report) and sleep are stubs on PATH. Nothing here touches
# the real ~/lookout-data, the real loops or the network; every process it starts is killed at the end.
#
# The script relies on macOS tools (BSD date and stat, lsof), so the cases run on macOS only and SKIP elsewhere.
#
# Run: infra/tests/test_migrate.sh   (bash 3.2 compatible; exits non-zero on the first failure)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
MIG="$REPO/infra/ops/migrate-to-snow-globe.sh"
FIXTURES="$HERE/fixtures/runtime-bin"

if [ "$(uname -s)" != Darwin ]; then
  echo "  SKIP infra/tests/test_migrate.sh: the switch runs on the lead's Mac (BSD date, stat and lsof)"
  exit 0
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/sg-migrate-test.XXXXXX")"
WORK="$(cd "$WORK" && pwd -P)"
trap 'pkill -9 -f "$WORK" 2>/dev/null || true; [ -n "${KEEP_WORK:-}" ] || rm -rf "$WORK"' EXIT

PASSED=0
CURRENT=""
fail() {
  printf '  FAIL %s: %s\n' "$CURRENT" "$*"
  if [ -n "${OUT:-}" ] && [ -f "$OUT" ]; then sed 's/^/    | /' "$OUT" | tail -40; fi
  exit 1
}
ok() {
  printf '  ok   %s\n' "$CURRENT"
  PASSED=$((PASSED + 1))
}
contains() { # contains FILE NEEDLE WHAT
  grep -q -F -- "$2" "$1" || fail "$3: expected to find '$2'"
}

# ---- stubs --------------------------------------------------------------------------------------------------------

STUBS="$WORK/stubs"
FAKEBIN="$WORK/fakebin"
mkdir -p "$STUBS" "$FAKEBIN" "$WORK/tmp"

# The capture CLI under either name: `status --json` prints 8 cameras; `loop` holds the lock and logs passes.
cat >"$STUBS/capture.py" <<'EOF'
#!/usr/bin/env python3
import datetime, fcntl, json, os, signal, sys, time

name = os.path.basename(sys.argv[0])
version = {"lookout-capture": "lookout-capture/0.2.1", "snow-globe-capture": "snow-globe-capture/0.3.0"}[name]
home = os.environ["HOME"]
new, old = os.path.join(home, "snow-globe-data"), os.path.join(home, "lookout-data")
data = (os.environ.get("SNOW_GLOBE_DATA_DIR") or os.environ.get("LOOKOUT_DATA_DIR")
        or (new if os.path.exists(new) else old))
if sys.argv[1] == "status":
    print(json.dumps({"storage": "LocalStorage(%r)" % data, "cameras": [{"viewpoint_id": "vp%d" % i} for i in range(8)]}))
    sys.exit(0)
fd = os.open(os.path.join(data, "state/locks/nps-fetcher.lock"), os.O_RDWR | os.O_CREAT, 0o644)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print(name + ": refused, another fetcher holds the lock", file=sys.stderr)
    sys.exit(3)
os.ftruncate(fd, 0)
os.pwrite(fd, json.dumps({"pid": os.getpid(), "command": name + " " + " ".join(sys.argv[1:])}).encode(), 0)
stop = []
if os.environ.get("STUB_IGNORE_TERM") == "1":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
else:
    signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
while not stop:
    now = datetime.datetime.now(datetime.timezone.utc)
    rec = {"t": now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000), "worker": "capture",
           "event": "pass", "runner": "cli-loop", "duration_s": 0.2, "capture_version": version}
    with open(os.path.join(data, "capture_log", now.strftime("%Y-%m-%d") + ".jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")
    for _ in range(10):
        if stop:
            break
        time.sleep(0.1)
os.ftruncate(fd, 0)
print(name + ": stopped by SIGTERM")
EOF

# A snapshot venv's python: the stand-in's tick sleeps a second and prints a summary (or fails, in a venv built
# with FAKE_FORECAST_FAIL=1); the rehearsal's import check passes.
cat >"$STUBS/python.sh" <<'EOF'
#!/bin/bash
case "$*" in
  *"import snow_globe_forecast.lambda_handler as h"*) exit 0 ;;
  *"lambda_handler import handler; r=handler"*)
    sleep 1
    if [ -f "$(dirname "$0")/FAIL" ]; then
      printf 'Traceback (most recent call last):\nModuleNotFoundError: No module named snow_globe_forecast\n' >&2
      exit 1
    fi
    echo '{"extracted": 0, "runs": []}' ;;
  *) exit 0 ;;
esac
EOF

cat >"$FAKEBIN/caffeinate" <<'EOF'
#!/bin/bash
# Stub: `caffeinate [-flags] [-w pid] [-t s] [utility...]` runs the utility in place, or waits for the pid.
w=""
while [ $# -gt 0 ]; do
  case "$1" in
    -w) w="$2"; shift 2 ;;
    -t) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
if [ $# -gt 0 ]; then exec "$@"; fi
while [ -n "$w" ] && kill -0 "$w" 2>/dev/null; do /bin/sleep 0.2; done
EOF
printf '#!/bin/bash\necho "Now drawing from '\''AC Power'\''"\n' >"$FAKEBIN/pmset"
# sleep, tagged with this run's folder, so the stand-in's `sleep 300` is killed at the end like everything else.
printf '#!/bin/bash\nexec -a "sleep %s" /bin/sleep "$@"\n' "$WORK" >"$FAKEBIN/sleep"
cat >"$FAKEBIN/uv" <<EOF
#!/bin/bash
# Stub uv: "--directory DIR sync ..." makes DIR/.venv with stub executables (failing for DIR under
# \$FAKE_UV_FAIL_UNDER); "run --no-sync snow-globe-eval data-report --json FILE" writes a gate report.
if [ "\$1" = --directory ]; then
  dir="\$2"
  if [ -n "\${FAKE_UV_FAIL_UNDER:-}" ]; then case "\$dir" in "\$FAKE_UV_FAIL_UNDER"*) echo "fake uv: failing for \$dir" >&2; exit 1 ;; esac; fi
  mkdir -p "\$dir/.venv/bin"
  case "\$dir" in
    */workers/capture) cp "$STUBS/capture.py" "\$dir/.venv/bin/snow-globe-capture"; chmod 755 "\$dir/.venv/bin/snow-globe-capture" ;;
    */workers/forecast)
      cp "$STUBS/python.sh" "\$dir/.venv/bin/python"; chmod 755 "\$dir/.venv/bin/python"
      if [ "\${FAKE_FORECAST_FAIL:-0}" = 1 ]; then : >"\$dir/.venv/bin/FAIL"; fi ;;
  esac
  exit 0
fi
if [ "\$1" = run ]; then
  out="" prev=""
  for a in "\$@"; do
    if [ "\$prev" = --json ]; then out="\$a"; fi
    prev="\$a"
  done
  printf '{"kind": "snow-globe.data-report", "clock": {"required_h": 72.0, "covered_h": %s, "elapsed_h": 82.8, "lost_h": 10.8, "met": %s, "met_at_utc": null, "until_utc": "now", "earliest_close_utc": "2026-11-22T23:12:06.983Z"}}\n' "\${FAKE_COVERED_H:-72.5}" "\${FAKE_MET:-true}" >"\$out"
  echo "fake uv: data report written to \$out"
  exit 0
fi
echo "fake uv: unexpected \$*" >&2
exit 2
EOF
chmod 755 "$STUBS"/* "$FAKEBIN"/*

# The source of the new snapshot (--source-dir): only what the script exports and what the stubs need.
SRC="$WORK/src"
mkdir -p "$SRC/workers/capture/src/snow_globe_capture" "$SRC/workers/forecast" "$SRC/viewpoints/manifests" "$SRC/eval"
echo '__version__ = "0.3.0"' >"$SRC/workers/capture/src/snow_globe_capture/__init__.py"
cp "$REPO"/viewpoints/manifests/*.json "$SRC/viewpoints/manifests/"

# ---- one scratch HOME per case, with the old loops running ----------------------------------------------------

H="" OLD="" RT="" OLD_CAP="" OLD_FC="" OUT=""
RUNENV=() # the case's HOME, the stubs first on PATH, and no inherited data-dir variable; `env` execs the command,
# so a background job's $! is the loop's own pid
set_runenv() {
  RUNENV=(env -u SNOW_GLOBE_DATA_DIR -u LOOKOUT_DATA_DIR -u LOOKOUT_MANIFEST_DIR -u SNOW_GLOBE_MANIFEST_DIR
    HOME="$H" PATH="$FAKEBIN:$PATH" TMPDIR="$WORK/tmp")
}
lock_pid() { sed -n 's/.*"pid": *\([0-9][0-9]*\).*/\1/p' "$1/state/locks/nps-fetcher.lock" 2>/dev/null | head -1; }
count() { pgrep -f "$1" 2>/dev/null | wc -l | tr -d ' '; }
new_case() { # new_case NAME [STUB_IGNORE_TERM]
  CURRENT="$1"
  H="$WORK/$1/home"
  OLD="$H/lookout-data"
  RT="$OLD/cache/runtime"
  OUT="$WORK/$1/migrate.out"
  set_runenv
  mkdir -p "$OLD"/capture_log "$OLD"/forecast_log "$OLD"/state/locks "$OLD"/state/archive "$RT/bin" "$RT/logs"
  cp "$FIXTURES"/capture_loop.sh "$FIXTURES"/forecast_loop.sh "$FIXTURES"/supervise.sh "$RT/bin/"
  chmod 755 "$RT"/bin/*.sh
  local snap
  for snap in 034c46b 3cd2ca7; do
    mkdir -p "$RT/$snap/workers/capture/.venv/bin" "$RT/$snap/workers/forecast/.venv/bin" "$RT/$snap/viewpoints/manifests"
    cp "$STUBS/capture.py" "$RT/$snap/workers/capture/.venv/bin/lookout-capture"
    cp "$STUBS/python.sh" "$RT/$snap/workers/forecast/.venv/bin/python"
    chmod 755 "$RT/$snap/workers/capture/.venv/bin/lookout-capture" "$RT/$snap/workers/forecast/.venv/bin/python"
  done
  echo '{"schema": 1, "entries": []}' >"$OLD/state/archive/queue.json"
  # As on the Mac: the stand-in started from 034c46b; `current` moved to 3cd2ca7 before capture restarted.
  ln -s "$RT/034c46b" "$RT/current"
  "${RUNENV[@]}" nohup "$RT/bin/forecast_loop.sh" >>"$RT/logs/forecast_loop.out" 2>&1 </dev/null &
  OLD_FC=$!
  disown "$OLD_FC" # no job notice when the case kills it
  local _
  for _ in $(seq 1 100); do
    if grep -q '^{"extracted"' "$RT/logs/forecast_loop.out" 2>/dev/null; then break; fi
    /bin/sleep 0.1
  done
  grep -q '^{"extracted"' "$RT/logs/forecast_loop.out" || fail "the old stand-in did not tick"
  ln -sfn "$RT/3cd2ca7" "$RT/current"
  "${RUNENV[@]}" STUB_IGNORE_TERM="${2:-0}" nohup "$RT/bin/capture_loop.sh" >>"$RT/logs/capture_loop.out" 2>&1 </dev/null &
  OLD_CAP=$!
  disown "$OLD_CAP"
  for _ in $(seq 1 100); do
    if [ "$(lock_pid "$OLD")" = "$OLD_CAP" ]; then break; fi
    /bin/sleep 0.1
  done
  [ "$(lock_pid "$OLD")" = "$OLD_CAP" ] || fail "the old capture loop did not take the lock"
}
migrate() { # migrate ARGS...: the script under test, with short waits; its status in RC, its output in $OUT
  RC=0
  "${RUNENV[@]}" SG_MIGRATE_TEST_SHA=new1234 SG_MIGRATE_START_AFTER_PASS_S=2 SG_MIGRATE_PASS_WAIT_MAX_S=20 \
    SG_MIGRATE_STOP_WAIT_S=4 SG_MIGRATE_HANDLER_WAIT_S=10 SG_MIGRATE_FIRST_PASS_WAIT_S=20 SG_MIGRATE_TICK_WAIT_S=20 \
    "$@" >"$OUT" 2>&1 || RC=$?
}
unchanged() { # nothing live moved: the data, `current`, the scripts and both old loops are as they were
  if [ ! -d "$OLD" ] || [ -L "$OLD" ]; then fail "$OLD is not the real folder any more"; fi
  if [ -e "$H/snow-globe-data" ] && [ "${1:-}" != keep-new ]; then fail "$H/snow-globe-data exists"; fi
  [ "$(readlink "$RT/current")" = "$RT/3cd2ca7" ] || fail "current -> $(readlink "$RT/current")"
  local f
  for f in capture_loop.sh forecast_loop.sh supervise.sh; do
    cmp -s "$FIXTURES/$f" "$RT/bin/$f" || fail "$f changed"
  done
  kill -0 "$OLD_CAP" 2>/dev/null || fail "the old capture loop $OLD_CAP is gone"
  [ "$(lock_pid "$OLD")" = "$OLD_CAP" ] || fail "the lock holder changed: $(lock_pid "$OLD")"
  kill -0 "$OLD_FC" 2>/dev/null || fail "the old stand-in $OLD_FC is gone"
  [ "$(count "$H/.*capture loop")" = 1 ] || fail "capture processes: $(pgrep -fl "$H/.*capture loop")"
  [ "$(count "/bin/bash $H/.*forecast_loop.sh")" = 1 ] || fail "stand-ins: $(pgrep -fl "forecast_loop.sh" | grep "$H")"
}
end_case() { pkill -9 -f "$WORK/$CURRENT/" 2>/dev/null || true; }

# ---- cases --------------------------------------------------------------------------------------------------------

new_case gate_not_met_refuses_and_changes_nothing
migrate FAKE_MET=false FAKE_COVERED_H=30.906 bash "$MIG" --source-dir "$SRC" --detach
[ "$RC" -eq 1 ] || fail "exit $RC"
contains "$OUT" "does not show the M0.5 gate's 72 h covered" "the refusal"
contains "$OUT" "30.906 h covered" "the report's figures"
unchanged
ok
end_case

new_case dry_run_rehearses_for_real_and_changes_nothing
migrate FAKE_MET=false bash "$MIG" --dry-run --source-dir "$SRC"
[ "$RC" -eq 0 ] || fail "exit $RC"
contains "$OUT" "a real run now would be refused without --force" "the gate note"
contains "$OUT" "status: 8 cameras" "the rehearsal's status ran"
# shellcheck disable=SC2016 # the literal line of the rewritten script
contains "$OUT" '+exec caffeinate -s "$SNAP/workers/capture/.venv/bin/snow-globe-capture" loop' "the capture_loop.sh diff"
contains "$OUT" "+  \"\$PY\" -c 'import json; from snow_globe_forecast.lambda_handler import handler;" "the forecast_loop.sh diff"
contains "$OUT" "dry run complete: nothing live was changed" "the end"
unchanged
ok
end_case

new_case an_existing_snow_globe_data_is_refused
mkdir "$H/snow-globe-data"
migrate bash "$MIG" --source-dir "$SRC" --detach
[ "$RC" -eq 1 ] || fail "exit $RC"
contains "$OUT" "snow-globe-data already exists" "the refusal"
unchanged keep-new
if [ ! -d "$H/snow-globe-data" ] || [ -n "$(ls -A "$H/snow-globe-data")" ]; then fail "the stray folder was touched"; fi
ok
end_case

new_case full_run_moves_the_data_and_restarts_both_loops_on_the_new_names
migrate bash "$MIG" --source-dir "$SRC" --detach
[ "$RC" -eq 0 ] || fail "exit $RC"
NEW="$H/snow-globe-data"
if [ ! -L "$OLD" ] || [ "$(readlink "$OLD")" != "$NEW" ] || [ ! -d "$NEW" ] || [ -L "$NEW" ]; then
  fail "the move: $(ls -la "$H")"
fi
NRT="$NEW/cache/runtime"
[ "$(readlink "$NRT/current")" = "$NRT/new1234" ] || fail "current -> $(readlink "$NRT/current")"
[ -f "$NRT/new1234.status.json" ] || fail "no status of the new snapshot"
for f in capture_loop.sh forecast_loop.sh supervise.sh; do
  cmp -s "$FIXTURES/$f" "$NRT/bin/$f.lookout.bak" || fail "$f.lookout.bak is not the old script"
done
grep -q 'snow-globe-capture" loop' "$NRT/bin/capture_loop.sh" || fail "capture_loop.sh not rewritten"
grep -q 'from snow_globe_forecast.lambda_handler import handler' "$NRT/bin/forecast_loop.sh" || fail "forecast_loop.sh not rewritten"
new_cap="$(lock_pid "$NEW")"
if [ -z "$new_cap" ] || [ "$new_cap" = "$OLD_CAP" ]; then fail "lock holder $new_cap"; fi
ps -p "$new_cap" -o command= | grep -q "$NRT/new1234/workers/capture/.venv/bin/snow-globe-capture loop" || fail "the lock holder runs $(ps -p "$new_cap" -o command=)"
grep -q '"capture_version": "snow-globe-capture/0.3.0"' "$NEW"/capture_log/*.jsonl || fail "no pass of the new loop"
if kill -0 "$OLD_CAP" 2>/dev/null; then fail "the old capture loop still runs"; fi
if kill -0 "$OLD_FC" 2>/dev/null; then fail "the old stand-in still runs"; fi
[ "$(count "$H/.*capture loop")" = 1 ] || fail "capture processes: $(pgrep -fl "$H/.*capture loop")"
[ "$(count "/bin/bash $NRT/bin/forecast_loop.sh")" = 1 ] || fail "new stand-ins: $(pgrep -fl forecast_loop.sh)"
grep -q "^tick .* code $NRT/new1234" "$NRT/logs/forecast_loop.out" || fail "the new stand-in did not tick from the new snapshot"
contains "$OUT" "the new stand-in's first tick finished" "the stand-in check"
contains "$OUT" "the new capture loop (pid $new_cap) holds the NPS fetcher lock" "the capture check"
# The new loops' scripts clear inherited variables and name the new data dir.
# shellcheck disable=SC2016 # the literal line of the rewritten script
grep -q 'export SNOW_GLOBE_DATA_DIR="$HOME/snow-globe-data"' "$NRT/bin/capture_loop.sh" || fail "no explicit data dir"
ok
end_case

new_case a_failed_build_rolls_back_and_restarts_only_capture
migrate FAKE_UV_FAIL_UNDER="$H/snow-globe-data/" bash "$MIG" --source-dir "$SRC" --detach
[ "$RC" -eq 1 ] || fail "exit $RC"
contains "$OUT" "FAILED: the snapshot build failed" "the failure"
contains "$OUT" "the data is back at $OLD" "the data restored"
if [ ! -d "$OLD" ] || [ -L "$OLD" ] || [ -e "$H/snow-globe-data" ]; then fail "the data: $(ls -la "$H")"; fi
[ "$(readlink "$RT/current")" = "$RT/3cd2ca7" ] || fail "current -> $(readlink "$RT/current")"
for f in capture_loop.sh forecast_loop.sh supervise.sh; do cmp -s "$FIXTURES/$f" "$RT/bin/$f" || fail "$f not restored"; done
[ ! -e "$RT/new1234" ] || fail "the half-built snapshot is left"
if kill -0 "$OLD_CAP" 2>/dev/null; then fail "the stopped loop still runs"; fi
restarted=""
for _ in $(seq 1 50); do
  restarted="$(lock_pid "$OLD")"
  if [ -n "$restarted" ] && [ "$restarted" != "$OLD_CAP" ]; then break; fi
  /bin/sleep 0.1
done
if [ -z "$restarted" ] || [ "$restarted" = "$OLD_CAP" ]; then fail "no new holder of the lock: '$restarted'"; fi
ps -p "$restarted" -o command= | grep -q "$RT/3cd2ca7/workers/capture/.venv/bin/lookout-capture loop" || fail "the restarted loop runs $(ps -p "$restarted" -o command=)"
kill -0 "$OLD_FC" 2>/dev/null || fail "the stand-in, which was never stopped, is gone"
[ "$(count "$H/.*capture loop")" = 1 ] || fail "capture processes: $(pgrep -fl "$H/.*capture loop")"
[ "$(count "/bin/bash $H/.*forecast_loop.sh")" = 1 ] || fail "stand-ins: $(pgrep -fl forecast_loop.sh)"
ok
end_case

new_case a_capture_loop_that_ignores_sigterm_is_left_alone 1
migrate bash "$MIG" --source-dir "$SRC" --detach
[ "$RC" -eq 1 ] || fail "exit $RC"
contains "$OUT" "is still running after 4 s. Nothing else was stopped or changed" "the refusal"
unchanged
ok
end_case

new_case a_failing_new_stand_in_gives_the_old_one_back
migrate FAKE_FORECAST_FAIL=1 bash "$MIG" --source-dir "$SRC" --detach
[ "$RC" -eq 1 ] || fail "exit $RC"
NEW="$H/snow-globe-data"
NRT="$NEW/cache/runtime"
contains "$OUT" "the new stand-in's first tick failed" "the failure"
contains "$OUT" "the old stand-in's script runs again" "the way back"
[ "$(readlink "$NRT/current")" = "$NRT/new1234" ] || fail "current -> $(readlink "$NRT/current")"
new_cap="$(lock_pid "$NEW")"
ps -p "$new_cap" -o command= | grep -q "snow-globe-capture loop" || fail "capture is not on the new code"
[ "$(count "/bin/bash $NRT/bin/forecast_loop.sh.lookout.bak")" = 1 ] || fail "the old stand-in script is not running"
[ "$(count "/bin/bash $NRT/bin/forecast_loop.sh\$")" = 0 ] || fail "the failed new stand-in still runs"
grep -q "^tick .* code $OLD/cache/runtime/3cd2ca7" "$NRT/logs/forecast_loop.out" || fail "the old stand-in did not start from 3cd2ca7"
ok
end_case

new_case a_supervisor_started_the_owners_way_is_found_and_stopped_first
# Owner action 1: `cd` into the runtime's bin/ and start `/bin/bash supervise.sh` detached, so its command line holds
# only the relative name (D-081). The switch must still find it through supervisor.pid and stop it before the loops.
(cd "$RT/bin" && "${RUNENV[@]}" nohup /bin/bash supervise.sh >>"$RT/logs/supervisor.out" 2>&1 </dev/null &)
for _ in $(seq 1 100); do
  if [ -f "$RT/supervisor.pid" ] && kill -0 "$(cat "$RT/supervisor.pid")" 2>/dev/null; then break; fi
  /bin/sleep 0.1
done
SUP="$(cat "$RT/supervisor.pid")"
kill -0 "$SUP" 2>/dev/null || fail "the stub supervisor did not start"
ps -p "$SUP" -o command= | grep -q -- "$RT/bin/supervise.sh" && fail "the supervisor's command line holds the full path; the case would not test the relative start"
migrate FAKE_MET=false bash "$MIG" --dry-run --source-dir "$SRC"
[ "$RC" -eq 0 ] || fail "dry run exit $RC"
contains "$OUT" "supervisor: $SUP" "the dry run finds the relatively started supervisor"
migrate bash "$MIG" --source-dir "$SRC" --detach
[ "$RC" -eq 0 ] || fail "exit $RC"
contains "$OUT" "stopping the supervisor $SUP first" "the supervisor stopped before the loops"
if kill -0 "$SUP" 2>/dev/null; then fail "the supervisor $SUP still runs"; fi
NRT="$H/snow-globe-data/cache/runtime"
[ "$(count "$H/.*capture loop")" = 1 ] || fail "capture processes: $(pgrep -fl "$H/.*capture loop")"
[ "$(count "/bin/bash $NRT/bin/forecast_loop.sh")" = 1 ] || fail "new stand-ins: $(pgrep -fl forecast_loop.sh)"
ok
end_case

echo "infra/tests/test_migrate.sh: $PASSED passed, 0 failed"
