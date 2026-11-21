#!/bin/bash
# migrate-to-snow-globe.sh: the rename's live switch (D-080), for the lead, right after the M0.5 gate closes.
#
# One command moves the running jobs from the Lookout names to the Snow Globe names (D-079's lessons: one sleep
# assertion across the stop and the start, and the stop and the start in one command):
#   0. Preflight, read-only: AC power; ~/lookout-data a real folder and ~/snow-globe-data absent (not even a
#      symlink), on one disk; the three live scripts the ones this script was written against (SHA-256); the
#      source (HEAD of --repo) the rename commit; the running capture loop (the NPS fetcher lock's holder, running
#      `lookout-capture loop`), the forecast stand-in and its snapshot, and the supervisor if one runs.
#   1. The gate: the read-only data report (`snow-globe-eval data-report --json`, on ~/lookout-data, from
#      --repo's eval/) must show the gate rule's 72 h covered (`clock.met`). It refuses before that, whatever the
#      time, unless --force (the owner's call only).
#   2. A rehearsal, before anything stops: the source's workers/ and viewpoints/ exported to a temporary folder,
#      both venvs built offline (`uv sync --locked --no-dev --offline`), the new code's read-only `status` on the
#      live data, its forecast handler imported, and the rewrite of the three live scripts applied to copies.
#      --dry-run runs the gate and the rehearsal for real (they write only to a temporary folder and uv's cache),
#      prints what a real run would do, and stops.
#   3. `caffeinate -is -w <this pid>` for the whole run: this Mac sleeps one minute after the last assertion
#      drops (D-079); -s holds only on AC power, so -i is added.
#   4. Capture first: waits for the loop's next `pass` record, stops the supervisor if one runs, SIGTERMs the
#      capture loop once and waits for it to exit (it saves its state). The forecast stand-in keeps running, and
#      the move waits until its handler is idle.
#   5. mv ~/lookout-data ~/snow-globe-data, then the symlink ~/lookout-data -> ~/snow-globe-data, so the old
#      snapshots' absolute paths, the running stand-in and any old shell keep working.
#   6. The snapshot of the source with its own venvs at ~/snow-globe-data/cache/runtime/<sha>, its read-only
#      status (saved as <sha>.status.json, as for 3cd2ca7), then the three scripts rewritten for the new names
#      (the old ones kept as *.lookout.bak) and `current` flipped to the snapshot.
#   7. The new capture loop, started about 240 s after the old loop's last pass ended (every camera is then due
#      within its first pass's 60 s window, so none waits an extra interval), as a background job of this
#      script. Checked: it holds the NPS fetcher lock and logs a `snow-globe-capture/` pass.
#   8. The stand-in: SIGTERM to the old stand-in's bash loop, a wait for its handler (two handlers never run at
#      once), then the new stand-in, checked through its first tick. The stand-in moves from forecast 0.1.0
#      (034c46b) to 0.3.0, so read its first run file too (PLAN.md, M0.5).
#   9. Releases its own caffeinate (each capture loop holds its own `caffeinate -s`, as before) and waits on the
#      two new loops, so they stay children of the caller's background command, as the old ones were; --detach
#      exits instead.
#
# Rollback restarts only what this run stopped, and only while no instance of it is alive:
#   - a failure in steps 5-7 stops the new capture loop if one started, puts back the scripts, `current` and the
#     data at ~/lookout-data (removing the half-built snapshot), and starts the old capture loop again. The
#     stand-in was not stopped, so it is left alone. The script can then simply be run again;
#   - if the capture loop does not exit within the wait after its SIGTERM (step 4), nothing else is touched and
#     nothing is started beside it: the script exits and says how to restart capture once that loop is gone;
#   - in step 8, a handler still running after the wait leaves the new stand-in unstarted (capture runs on the
#     new code); a new stand-in whose first tick fails is stopped, and the old stand-in's script is started
#     again from the pre-switch `current` (3cd2ca7, so forecast 0.2.0: what any restart of the stand-in has
#     started since D-079);
#   - a supervisor this run stopped is not restarted by it: start it from your own Terminal, detached (D-076).
#
# usage: infra/ops/migrate-to-snow-globe.sh [--dry-run] [--force] [--repo PATH] [--allow-battery] [--detach]
#                                           [--no-start] [--source-dir DIR]
#   --dry-run        the read-only checks, the gate and the rehearsal; prints the plan; changes nothing live
#   --force          run although the data report does not show the 72 h gate met (the owner's call only)
#   --repo PATH      the repo whose HEAD is the rename commit (default: the git work tree of this script)
#   --allow-battery  run on battery (the loops' own caffeinate -s holds only on AC power)
#   --detach         exit once the new loops are checked, instead of waiting on them
#   --no-start       stop after step 6 and print the start commands (for testing on a scratch HOME)
#   --source-dir DIR take workers/, viewpoints/ and eval/ from DIR instead of HEAD (testing only)
#
# Run it from the lead's session as a background command, after `--dry-run` has passed: from step 9 on it stays
# the new loops' parent until they exit. Exit status: 0 done; 1 refused or failed (the message says what was
# changed, if anything); 2 usage.
set -u
set -o pipefail

OLD="$HOME/lookout-data"
NEW="$HOME/snow-globe-data"
OLD_RT="$OLD/cache/runtime"
NEW_RT="$NEW/cache/runtime"
# The live scripts this one was written against (read on 21 Nov 2026, 01:40 UTC; copies in infra/tests/fixtures).
SHA_CAPTURE_LOOP="b1ed35dea98c8b885997b24666f2bf95e015d4e74e35cb6f5d8351c82417269c"
SHA_FORECAST_LOOP="1d9cee12d77dde51c835248c19f6b273a1439f2926d520f8d3a2b6b92034ae5d"
SHA_SUPERVISE="64cb6c4198c1e2562653c37df0f76eb404ed8f62837626b4660a5293a1ba2388"
# Timings. Overridable only so infra/tests/test_migrate.sh can run the script against stub loops.
START_AFTER_PASS_S=${SG_MIGRATE_START_AFTER_PASS_S:-240} # new loop's start after the old loop's last pass
PASS_WAIT_MAX_S=${SG_MIGRATE_PASS_WAIT_MAX_S:-900}       # wait for the old loop's next pass record
STOP_WAIT_S=${SG_MIGRATE_STOP_WAIT_S:-600}               # wait for the capture loop to exit after SIGTERM
HANDLER_WAIT_S=${SG_MIGRATE_HANDLER_WAIT_S:-1200}        # wait for a stand-in handler to finish
FIRST_PASS_WAIT_S=${SG_MIGRATE_FIRST_PASS_WAIT_S:-300}   # wait for the new loop's first pass record
TICK_WAIT_S=${SG_MIGRATE_TICK_WAIT_S:-900}               # wait for the new stand-in's first tick to finish
SUPERVISOR_WAIT_S=150

DRY=0 FORCE=0 ALLOW_BATTERY=0 NO_START=0 DETACH=0 REPO="" SOURCE_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --force) FORCE=1; shift ;;
    --allow-battery) ALLOW_BATTERY=1; shift ;;
    --no-start) NO_START=1; shift ;;
    --detach) DETACH=1; shift ;;
    --repo) REPO="${2:?--repo needs a path}"; shift 2 ;;
    --source-dir) SOURCE_DIR="${2:?--source-dir needs a path}"; shift 2 ;;
    -h | --help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 0 ;;
    *) echo "migrate-to-snow-globe.sh: unknown argument $1 (see --help)" >&2; exit 2 ;;
  esac
done

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
say() { printf '%s %s\n' "$(ts)" "$*"; }
step() { printf '\n%s == %s\n' "$(ts)" "$*"; }
die() {
  printf '%s REFUSED: %s\n' "$(ts)" "$*" >&2
  exit 1
}
py() { /usr/bin/env python3 -I "$@"; }
alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }
command_of() { ps -p "$1" -o command= 2>/dev/null; }
# A process holds the NPS fetcher lock at $1 (a non-blocking flock probe; a missing file is free).
lock_held() {
  [ -e "$1" ] || return 1
  ! py -c 'import fcntl, sys
f = open(sys.argv[1], "a+")
fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)' "$1" 2>/dev/null
}
# Processes of this runtime, under the old or the new path: a capture loop's Python (not the `caffeinate -s`
# beside it, which can outlive it by a moment), and a stand-in's handler.
CAPTURE_RE="[Pp]ython[^ ]* ($OLD_RT|$NEW_RT)/[^ ]*(snow-globe|lookout)-capture loop"
HANDLER_RE="($OLD_RT|$NEW_RT)/.*(snow_globe|lookout)_forecast[.]lambda_handler import handler"
capture_alive() { pgrep -f "$CAPTURE_RE" >/dev/null 2>&1; }
handler_running() { pgrep -f "$HANDLER_RE" >/dev/null 2>&1; }
standin_pids() { # the bash of every forecast_loop.sh (or its .lookout.bak) of this runtime, by either path
  ps -axww -o pid=,command= | awk -v a="/bin/bash $OLD_RT/bin/forecast_loop.sh" -v b="/bin/bash $NEW_RT/bin/forecast_loop.sh" '
    { pid = $1; $1 = ""; sub(/^ /, ""); if (index($0, a) == 1 || index($0, b) == 1) print pid }'
}

# ---- 0. preflight (read-only) ------------------------------------------------------------------------------------
step "0. preflight (read-only)"
if pmset -g batt 2>/dev/null | head -1 | grep -q "AC Power"; then
  say "power: AC"
elif [ "$ALLOW_BATTERY" -eq 1 ]; then
  say "power: battery, allowed by --allow-battery (only this script's caffeinate -i holds the Mac awake)"
elif [ "$DRY" -eq 1 ]; then
  say "power: battery: a real run would be refused without --allow-battery"
else
  die "on battery: the loops' caffeinate -s holds only on AC power (D-079); plug in, or pass --allow-battery"
fi

if [ ! -d "$OLD" ] || [ -L "$OLD" ]; then die "$OLD is not a real folder (already migrated? $(ls -ld "$OLD" 2>&1))"; fi
if [ -e "$NEW" ] || [ -L "$NEW" ]; then
  die "$NEW already exists; the move would land inside it, and renamed code would already read it instead of $OLD (D-080). Tell the owner; delete nothing"
fi
[ "$(stat -f %d "$HOME")" = "$(stat -f %d "$OLD")" ] || die "$OLD is on another disk than $HOME: mv would copy"
for v in SNOW_GLOBE_DATA_DIR LOOKOUT_DATA_DIR; do
  val="$(printenv "$v" || true)"
  if [ -n "$val" ] && [ "$val" != "$OLD" ] && [ "$val" != "$NEW" ]; then
    say "note: this shell sets $v=$val; the rewritten loop scripts clear it and use $NEW"
  fi
done

for pair in "capture_loop.sh:$SHA_CAPTURE_LOOP" "forecast_loop.sh:$SHA_FORECAST_LOOP" "supervise.sh:$SHA_SUPERVISE"; do
  f="${pair%%:*}" want="${pair#*:}"
  got="$(shasum -a 256 "$OLD_RT/bin/$f" 2>/dev/null | awk '{print $1}')"
  [ "$got" = "$want" ] || die "$OLD_RT/bin/$f changed since this script was written (sha256 ${got:-missing}, expected $want); update the rewrite rules below first"
done
say "live scripts: unchanged since 21 Nov 01:40 UTC (sha256 checked)"

if [ -n "$SOURCE_DIR" ]; then
  if [ ! -d "$SOURCE_DIR/workers/capture" ] || [ ! -d "$SOURCE_DIR/viewpoints/manifests" ]; then
    die "--source-dir $SOURCE_DIR has no workers/ and viewpoints/"
  fi
  SHA="${SG_MIGRATE_TEST_SHA:-src$(date -u +%H%M)}"
  EVAL_DIR="$SOURCE_DIR/eval"
  say "source: $SOURCE_DIR (testing only), snapshot name $SHA"
else
  REPO="${REPO:-$(git -C "$(dirname "$0")" rev-parse --show-toplevel 2>/dev/null)}"
  if [ -z "$REPO" ] || ! git -C "$REPO" rev-parse --verify -q HEAD >/dev/null; then die "no git repo (pass --repo)"; fi
  SHA="$(git -C "$REPO" rev-parse --short=7 HEAD)"
  git -C "$REPO" cat-file -e "HEAD:workers/capture/src/snow_globe_capture/__init__.py" 2>/dev/null ||
    die "HEAD ($SHA) is not the rename commit: no workers/capture/src/snow_globe_capture"
  cap_version="$(git -C "$REPO" show "HEAD:workers/capture/src/snow_globe_capture/__init__.py" | sed -n 's/^__version__ = "\(.*\)"$/\1/p')"
  case "$cap_version" in 0.3.*) ;; *) die "HEAD's capture is ${cap_version:-unknown}, not 0.3.x" ;; esac
  EVAL_DIR="$REPO/eval"
  say "source: $REPO at $SHA (snow-globe-capture $cap_version)"
fi
[ ! -e "$OLD_RT/$SHA" ] || die "$OLD_RT/$SHA already exists"
UV="$(command -v uv)" || die "uv is not on PATH"

LOCK="$OLD/state/locks/nps-fetcher.lock"
CAP_PID="$(sed -n 's/.*"pid": *\([0-9][0-9]*\).*/\1/p' "$LOCK" 2>/dev/null | head -1)"
if ! alive "$CAP_PID" || ! command_of "$CAP_PID" | grep -q -- 'lookout-capture loop'; then
  die "no running 'lookout-capture loop' holds $LOCK (pid '${CAP_PID:-none}': $(command_of "${CAP_PID:-0}" || echo gone))"
fi
say "capture loop: pid $CAP_PID ($(command_of "$CAP_PID" | cut -c1-140))"
FC_PIDS="$(standin_pids | tr '\n' ' ')"
FC_PIDS="${FC_PIDS% }"
FC_SNAP=""
for p in $FC_PIDS; do
  FC_SNAP="$(lsof -a -p "$p" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)"
done
say "forecast stand-in: pid(s) ${FC_PIDS:-none running}${FC_SNAP:+, cwd $FC_SNAP}"
SUP_PID=""
if [ -f "$OLD_RT/supervisor.pid" ]; then
  cand="$(cat "$OLD_RT/supervisor.pid")"
  if alive "$cand" && command_of "$cand" | grep -q -- "supervise.sh"; then
    # Owner action 1 starts it from its own folder as `/bin/bash supervise.sh`, so the command line may hold
    # only the relative name: accept the full path, or a working folder that is this runtime's bin/ (D-081).
    cand_cwd="$(lsof -a -p "$cand" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)"
    bin_real="$(cd "$OLD_RT/bin" 2>/dev/null && pwd -P)"
    if command_of "$cand" | grep -q -- "$OLD_RT/bin/supervise.sh"; then
      SUP_PID="$cand"
    elif [ -n "$cand_cwd" ] && { [ "$cand_cwd" = "$OLD_RT/bin" ] || [ "$cand_cwd" = "$bin_real" ]; }; then
      SUP_PID="$cand"
    else
      die "supervisor.pid names a running supervise.sh (pid $cand, cwd ${cand_cwd:-unknown}) that is not this runtime's; stop it by hand first: kill $cand"
    fi
  fi
fi
say "supervisor: ${SUP_PID:-not running}"
OLD_CURRENT="$(readlink "$OLD_RT/current")" || die "no $OLD_RT/current"
say "current -> $OLD_CURRENT"

# ---- 1. the gate: the read-only data report must show 72 h covered -----------------------------------------------
step "1. the gate: snow-globe-eval data-report (read-only) on $OLD"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/sg-migrate.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
REPORT="$WORK/data-report.json"
GATE_PY='
import json, sys
try:
    doc = json.load(open(sys.argv[1]))
    clock = doc["clock"]
except (OSError, ValueError, KeyError, TypeError) as exc:
    print("    the data report is unreadable: %r" % (exc,))
    sys.exit(2)
need, got, met = clock.get("required_h"), clock.get("covered_h"), clock.get("met")
print("    %s until %s: %s h covered of %s h elapsed (%s h lost), %s h needed; met %s (at %s); earliest close %s"
      % (doc.get("kind"), clock.get("until_utc"), got, clock.get("elapsed_h"), clock.get("lost_h"), need, met,
         clock.get("met_at_utc"), clock.get("earliest_close_utc")))
numbers = all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (need, got))
ok = (doc.get("kind") in ("snow-globe.data-report", "lookout.data-report") and numbers and need >= 72
      and got >= need and met is True)
sys.exit(0 if ok else 3)
'
gate_rc=0
if (cd "$EVAL_DIR" && /usr/bin/env -u LOOKOUT_DATA_DIR -u VIRTUAL_ENV SNOW_GLOBE_DATA_DIR="$OLD" \
  PYTHONPATH=src:../workers/common/src "$UV" run --no-sync snow-globe-eval data-report --json "$REPORT" >"$WORK/data-report.out" 2>&1); then
  py -c "$GATE_PY" "$REPORT" || gate_rc=$?
else
  sed 's/^/    | /' "$WORK/data-report.out" | tail -20
  gate_rc=2
fi
if [ "$gate_rc" -eq 0 ]; then
  say "gate: met; the 72 h clock has closed"
elif [ "$FORCE" -eq 1 ]; then
  say "gate: NOT shown met (status $gate_rc), but --force was given"
elif [ "$DRY" -eq 1 ]; then
  say "gate: not shown met (status $gate_rc): a real run now would be refused without --force"
else
  die "the data report does not show the M0.5 gate's 72 h covered (status $gate_rc; see above); nothing was changed. Rerun after the close it gives"
fi

# The new supervise.sh, capture_loop.sh and forecast_loop.sh: exact replacements, each of which must match.
REWRITE_PY="$WORK/rewrite.py"
cat >"$REWRITE_PY" <<'PYEOF'
import sys
from pathlib import Path

CLEAR = (
    "unset PYTHONPATH\n"
    "# The rename (D-080): clear the caller's SNOW_GLOBE_* variables and their old-named twins, then set the\n"
    "# data dir and the manifests explicitly, so nothing inherited can redirect this loop.\n"
    "for v in $(compgen -e | grep -E '^(LOOKOUT|SNOW_GLOBE)_'); do unset \"$v\"; done\n"
    "export SNOW_GLOBE_DATA_DIR=\"$HOME/snow-globe-data\"\n"
    "export SNOW_GLOBE_MANIFEST_DIR=$SNAP/viewpoints/manifests\n"
)
RULES = {
    "capture_loop.sh": [
        ("(lookout-capture loop)", "(snow-globe-capture loop)", 1),
        ("~/lookout-data/cache/runtime/<sha>", "~/snow-globe-data/cache/runtime/<sha>", 1),
        ('SNAP=$(readlink "$HOME/lookout-data/cache/runtime/current")',
         'SNAP=$(readlink "$HOME/snow-globe-data/cache/runtime/current")', 1),
        ("unset PYTHONPATH\nexport LOOKOUT_MANIFEST_DIR=$SNAP/viewpoints/manifests\n", CLEAR, 1),
        ('echo "starting lookout-capture loop at', 'echo "starting snow-globe-capture loop at', 1),
        ('"$SNAP/workers/capture/.venv/bin/lookout-capture" loop',
         '"$SNAP/workers/capture/.venv/bin/snow-globe-capture" loop', 1),
        ('--archive-queue "$HOME/lookout-data/state/archive/queue.json"',
         '--archive-queue "$HOME/snow-globe-data/state/archive/queue.json"', 1),
    ],
    "forecast_loop.sh": [
        ('SNAP=$(readlink "$HOME/lookout-data/cache/runtime/current")',
         'SNAP=$(readlink "$HOME/snow-globe-data/cache/runtime/current")', 1),
        ("unset PYTHONPATH\nexport LOOKOUT_MANIFEST_DIR=$SNAP/viewpoints/manifests\n", CLEAR, 1),
        ("export LOOKOUT_FORECAST_FIELDS=", "export SNOW_GLOBE_FORECAST_FIELDS=", 1),
        ("export LOOKOUT_FORECAST_MAX_FH=2 LOOKOUT_FORECAST_MAX_MB=40",
         "export SNOW_GLOBE_FORECAST_MAX_FH=2 SNOW_GLOBE_FORECAST_MAX_MB=40", 1),
        ("from lookout_forecast.lambda_handler import handler",
         "from snow_globe_forecast.lambda_handler import handler", 1),
    ],
    "supervise.sh": [
        ("# Keeps the capture loop and the forecast stand-in running, detached from any agent session.\n",
         "# Keeps the capture loop and the forecast stand-in running, detached from any agent session.\n"
         "# Renamed by the switch of D-080: Snow Globe paths and variables (the old names still read as a\n"
         "# fallback); it adopts a capture loop and waits for a stand-in handler under either name.\n", 1),
        ("~/lookout-data/cache/runtime", "~/snow-globe-data/cache/runtime", 7),
        ("LOOKOUT_AWAKE_UNTIL_UTC=YYYY", "SNOW_GLOBE_AWAKE_UNTIL_UTC=YYYY", 1),
        ('RT="${LOOKOUT_RUNTIME_DIR:-$HOME/lookout-data/cache/runtime}"',
         'RT="${SNOW_GLOBE_RUNTIME_DIR:-${LOOKOUT_RUNTIME_DIR:-$HOME/snow-globe-data/cache/runtime}}"', 1),
        ("HANDLER_PATTERN=${SUP_HANDLER_PATTERN:-'lookout_forecast.lambda_handler import handler'}",
         "HANDLER_PATTERN=${SUP_HANDLER_PATTERN:-'(snow_globe|lookout)_forecast.lambda_handler import handler'}", 1),
        ('DATA="${LOOKOUT_DATA_DIR:-$HOME/lookout-data}"',
         'DATA="${SNOW_GLOBE_DATA_DIR:-${LOOKOUT_DATA_DIR:-$HOME/snow-globe-data}}"', 1),
        ('UNTIL_UTC="${LOOKOUT_AWAKE_UNTIL_UTC:-}"',
         'UNTIL_UTC="${SNOW_GLOBE_AWAKE_UNTIL_UTC:-${LOOKOUT_AWAKE_UNTIL_UTC:-}}"', 1),
        ('grep -q -- "$2"; }', 'grep -E -q -- "$2"; }', 1),
        ("alive_as \"$pid\" 'lookout-capture loop'", "alive_as \"$pid\" '(snow-globe|lookout)-capture loop'", 1),
        ("alive_as \"$ADOPT_CAPTURE\" 'lookout-capture loop'",
         "alive_as \"$ADOPT_CAPTURE\" '(snow-globe|lookout)-capture loop'", 1),
    ],
}
# What may still say lookout afterwards: the deprecated variables read as a fallback, and the old names matched.
ALLOWED = ("LOOKOUT_RUNTIME_DIR", "LOOKOUT_DATA_DIR", "LOOKOUT_AWAKE_UNTIL_UTC", "(LOOKOUT|SNOW_GLOBE)_",
           "|lookout)", "lookout|")

src, dst = Path(sys.argv[1]), Path(sys.argv[2])
text = src.read_text()
for old, new, count in RULES[src.name]:
    found = text.count(old)
    if found != count:
        sys.exit(f"{src.name}: {old!r} occurs {found} times, expected {count}")
    text = text.replace(old, new)
for line in text.splitlines():
    if "lookout" in line.lower() and not any(a in line for a in ALLOWED):
        sys.exit(f"{src.name}: a line still names lookout: {line!r}")
dst.write_text(text)
PYEOF

# ---- 2. rehearsal: build and read the live state with the new code, before anything stops ------------------------
step "2. rehearsal: export, offline venvs, a read-only status and the script rewrite, in $WORK"
build_snapshot() { # $1: target folder (must not exist): workers/ and viewpoints/ with both venvs, offline
  local dir="$1" p
  mkdir -p "$dir" || return 1
  if [ -n "$SOURCE_DIR" ]; then
    (cd "$SOURCE_DIR" && tar -cf - --exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude .ruff_cache \
      workers viewpoints) | tar -xf - -C "$dir" || return 1
  else
    git -C "$REPO" archive --format=tar HEAD workers viewpoints | tar -xf - -C "$dir" || return 1
  fi
  for p in capture forecast; do
    printf '    + uv sync --locked --no-dev --offline in %s\n' "$dir/workers/$p"
    /usr/bin/env -u PYTHONPATH -u VIRTUAL_ENV -u UV_PROJECT_ENVIRONMENT UV_OFFLINE=1 \
      "$UV" --directory "$dir/workers/$p" sync --locked --no-dev --offline || return 1
  done
}
STATUS_PY='
import json, sys
doc = json.load(open(sys.argv[1]))
cameras = len(doc.get("cameras", []))
print("    status: %d cameras, storage %s" % (cameras, doc.get("storage")))
sys.exit(0 if cameras == 8 else 1)
'
status_check() { # $1 snapshot folder, $2 data dir, $3 output file: the new code's read-only status and import check
  (cd "$1/workers/capture" && /usr/bin/env -u PYTHONPATH -u LOOKOUT_DATA_DIR -u LOOKOUT_MANIFEST_DIR \
    SNOW_GLOBE_DATA_DIR="$2" SNOW_GLOBE_MANIFEST_DIR="$1/viewpoints/manifests" .venv/bin/snow-globe-capture status --json >"$3") || return 1
  py -c "$STATUS_PY" "$3" || return 1
  /usr/bin/env -u PYTHONPATH "$1/workers/forecast/.venv/bin/python" -c \
    'import snow_globe_forecast.lambda_handler as h; assert callable(h.handler)' || return 1
  printf '    the forecast handler imports\n'
}
build_snapshot "$WORK/snapshot/$SHA" || die "the rehearsal's build failed (nothing was stopped); see above"
status_check "$WORK/snapshot/$SHA" "$OLD" "$WORK/status.json" || die "the rehearsal's status failed (nothing was stopped); see above"
for f in capture_loop.sh forecast_loop.sh supervise.sh; do
  py "$REWRITE_PY" "$OLD_RT/bin/$f" "$WORK/$f" || die "the rewrite of $f does not apply (nothing was stopped)"
  bash -n "$WORK/$f" || die "the rewritten $f does not parse (nothing was stopped)"
  if [ "$DRY" -eq 1 ]; then
    diff -u "$OLD_RT/bin/$f" "$WORK/$f" | sed 's/^/    /'
  fi
done
say "rehearsal passed"

if [ "$DRY" -eq 1 ]; then
  step "the rest, as a real run would do it (nothing below is done)"
  cat <<EOF
    - caffeinate -is -w <pid>; wait (up to $PASS_WAIT_MAX_S s) for the next pass record of capture pid $CAP_PID
    - ${SUP_PID:+kill -TERM $SUP_PID (the supervisor), then }kill -TERM $CAP_PID once; wait up to $STOP_WAIT_S s for it to exit
    - wait until no stand-in handler runs; mv $OLD $NEW; ln -s $NEW $OLD
    - build $NEW_RT/$SHA (own venvs, offline), its status to $NEW_RT/$SHA.status.json
    - the three scripts as diffed above (old ones kept as *.lookout.bak); current -> $NEW_RT/$SHA
    - at the old pass end + $START_AFTER_PASS_S s: nohup $NEW_RT/bin/capture_loop.sh &; check the lock and a pass
    - kill -TERM ${FC_PIDS:-<no stand-in running>} (the stand-in's bash); wait for its handler; nohup $NEW_RT/bin/forecast_loop.sh &
EOF
  say "dry run complete: nothing live was changed"
  exit 0
fi

# ---- 3. one sleep assertion for the whole run --------------------------------------------------------------------
step "3. caffeinate -is -w $$ (until this script exits)"
caffeinate -is -w $$ &
CAFF_PID=$!
say "caffeinate pid $CAFF_PID"

# ---- what this run changed, and the way back ---------------------------------------------------------------------
CAP_STOPPED=0 FC_STOPPED=0 SUP_STOPPED=0 MOVED=0 BUILT=0 SCRIPTS_SWAPPED=0 FLIPPED=0 NEW_CAP="" NEW_FC=""
rt_now() { if [ "$MOVED" -eq 1 ]; then printf '%s\n' "$NEW_RT"; else printf '%s\n' "$OLD_RT"; fi; }
start_capture() { # $1 runtime dir: its bin/capture_loop.sh as a background job of this script
  mkdir -p "$1/logs"
  nohup "$1/bin/capture_loop.sh" >>"$1/logs/capture_loop.out" 2>&1 </dev/null &
  NEW_CAP=$!
  say "started $1/bin/capture_loop.sh (pid $NEW_CAP)"
}
start_standin() { # $1 script to run, $2 log file
  nohup "$1" >>"$2" 2>&1 </dev/null &
  NEW_FC=$!
  say "started $1 (pid $NEW_FC)"
}
supervisor_note() {
  if [ "$SUP_STOPPED" -eq 1 ]; then
    say "the supervisor this run stopped is not restarted: start it from your own Terminal, detached, as its header says (D-076)"
  fi
}
# A failure in steps 4-7: everything back as it was, and the old capture loop again if this run stopped it.
rollback() {
  local why="$1" rt f p
  printf '%s FAILED: %s\n' "$(ts)" "$why" >&2
  say "rolling back"
  if alive "$NEW_CAP"; then
    kill -TERM "$NEW_CAP" 2>/dev/null
    for _ in $(seq 1 "$STOP_WAIT_S"); do
      alive "$NEW_CAP" || break
      sleep 1
    done
  fi
  rt="$(rt_now)"
  if [ "$SCRIPTS_SWAPPED" -eq 1 ]; then
    for f in capture_loop.sh forecast_loop.sh supervise.sh; do mv -f "$rt/bin/$f.lookout.bak" "$rt/bin/$f"; done
    SCRIPTS_SWAPPED=0
  fi
  rm -f "$rt"/bin/*.sh.new
  if [ "$FLIPPED" -eq 1 ]; then
    ln -sfn "$OLD_CURRENT" "$rt/current"
    FLIPPED=0
  fi
  if alive "$NEW_CAP"; then
    say "the new capture loop $NEW_CAP did not stop: the data stays at $NEW behind the symlink $OLD, which both names reach; current -> $(readlink "$rt/current"). Nothing else is started beside it"
    supervisor_note
    exit 1
  fi
  if [ "$MOVED" -eq 1 ]; then
    if [ "$BUILT" -eq 1 ] && [ -d "$NEW_RT/$SHA" ]; then rm -rf "${NEW_RT:?}/${SHA:?}" "$NEW_RT/$SHA.status.json"; fi
    if [ -L "$OLD" ]; then rm "$OLD"; fi
    if [ ! -e "$OLD" ] && mv "$NEW" "$OLD"; then
      MOVED=0
      say "the data is back at $OLD; nothing of this run is left there"
    else
      ln -s "$NEW" "$OLD" 2>/dev/null
      say "the data could not be moved back: it stays at $NEW behind the symlink $OLD, which both names reach"
    fi
  fi
  rt="$(rt_now)"
  if [ "$CAP_STOPPED" -eq 1 ]; then
    if lock_held "$OLD/state/locks/nps-fetcher.lock" || capture_alive; then
      say "the old capture loop is NOT restarted: another capture loop or NPS fetcher is alive ($(pgrep -fl "$CAPTURE_RE" | head -3 | tr '\n' ';'))"
    else
      start_capture "$rt"
    fi
  fi
  if [ "$FC_STOPPED" -eq 1 ]; then # the supervisor took it down with it
    for p in $(seq 1 "$HANDLER_WAIT_S"); do
      handler_running || break
      sleep 1
    done
    if handler_running || [ -n "$(standin_pids)" ]; then
      say "the stand-in is NOT restarted: a stand-in or its handler is still alive"
    else
      start_standin "$rt/bin/forecast_loop.sh" "$rt/logs/forecast_loop.out"
    fi
  fi
  supervisor_note
  say "rolled back: current -> $(readlink "$rt/current"). Fix the cause, then run this script again"
  exit 1
}

# ---- 4. capture first: wait for a pass to end, then stop the supervisor and the capture loop ---------------------
step "4. wait for the capture loop's pass to end, then stop it gracefully (the stand-in keeps running)"
PASS_PY='
import json, sys, time
from datetime import datetime, timedelta, timezone
log_dir, since, wait, prefix = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
def parse(t):
    return datetime.strptime(t.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z" if "." in t else "%Y-%m-%dT%H:%M:%S%z").timestamp()
deadline = time.time() + wait
while True:
    now = datetime.now(timezone.utc)
    best = None
    for day in sorted({now.date(), (now - timedelta(hours=1)).date()}):
        try:
            with open("%s/%s.jsonl" % (log_dir, day.isoformat()), "rb") as f:
                f.seek(0, 2); start = max(0, f.tell() - 262144); f.seek(start)
                lines = f.read().splitlines()[1 if start else 0:]
        except FileNotFoundError:
            continue
        for raw in lines:
            try:
                r = json.loads(raw)
            except ValueError:
                continue
            if (r.get("event") == "pass" and r.get("worker") == "capture" and r.get("runner") == "cli-loop"
                    and str(r.get("capture_version", "")).startswith(prefix)):
                end = parse(r["t"])
                if end >= since and (best is None or end > best[0]):
                    best = (end, end - float(r.get("duration_s") or 0), r)
    if best or time.time() > deadline:
        break
    time.sleep(0.5)
if not best:
    sys.exit(1)
print("%.3f %.3f %s %s" % (best[0], best[1], best[2].get("capture_version"), best[2]["t"]))
'
say "waiting up to $PASS_WAIT_MAX_S s for the next pass record of pid $CAP_PID ..."
pass_line="$(py -c "$PASS_PY" "$OLD/capture_log" "$(date -u +%s)" "$PASS_WAIT_MAX_S" "lookout-capture/")" ||
  die "no pass ended within $PASS_WAIT_MAX_S s (is the loop alive and the Mac awake?); nothing was stopped"
read -r PASS_END PASS_START PASS_VERSION PASS_T <<<"$pass_line"
say "pass ended at $PASS_T ($PASS_VERSION; it started at $(date -u -r "${PASS_START%.*}" +%H:%M:%S))"
if [ -n "$SUP_PID" ]; then
  say "stopping the supervisor $SUP_PID first, or it would restart the old loops from current"
  kill -TERM "$SUP_PID"
  for _ in $(seq 1 "$SUPERVISOR_WAIT_S"); do
    alive "$SUP_PID" || break
    sleep 1
  done
  SUP_STOPPED=1
  for p in $FC_PIDS; do
    if ! alive "$p"; then FC_STOPPED=1; fi
  done
  if ! alive "$CAP_PID"; then CAP_STOPPED=1; fi
  if alive "$SUP_PID"; then rollback "the supervisor $SUP_PID is still running $SUPERVISOR_WAIT_S s after its SIGTERM"; fi
  say "supervisor stopped"
fi
if alive "$CAP_PID"; then
  kill -TERM "$CAP_PID"
  say "SIGTERM to the capture loop $CAP_PID"
fi
for i in $(seq 1 "$STOP_WAIT_S"); do
  alive "$CAP_PID" || break
  if [ $((i % 60)) -eq 0 ]; then say "the capture loop $CAP_PID is still finishing (${i} s)"; fi
  sleep 1
done
if alive "$CAP_PID"; then
  die "the capture loop $CAP_PID got its SIGTERM at the pass end and is still running after $STOP_WAIT_S s. Nothing else was stopped or changed, and nothing was started beside it. Once it has exited, capture is down: start it again with  nohup $OLD_RT/bin/capture_loop.sh >> $OLD_RT/logs/capture_loop.out 2>&1 < /dev/null &  or run this script again"
fi
CAP_STOPPED=1
say "capture loop $CAP_PID exited"
if lock_held "$LOCK"; then rollback "another process still holds $LOCK"; fi
for i in $(seq 1 "$HANDLER_WAIT_S"); do
  handler_running || break
  if [ $((i % 60)) -eq 1 ]; then say "a stand-in handler is writing; the move waits for it to finish"; fi
  sleep 1
done
if handler_running; then rollback "a stand-in handler was still running $HANDLER_WAIT_S s later, so the data was not moved"; fi

# ---- 5. move the data, leave a symlink ---------------------------------------------------------------------------
step "5. mv $OLD $NEW; ln -s $NEW $OLD"
mv "$OLD" "$NEW" || rollback "mv failed"
MOVED=1
ln -s "$NEW" "$OLD" || rollback "the symlink $OLD was not made"

# ---- 6. the new snapshot, its read-only status, the scripts and `current` -----------------------------------------
step "6. snapshot $SHA at $NEW_RT/$SHA (own venvs), its status, the scripts, then current -> it"
BUILT=1
build_snapshot "$NEW_RT/$SHA" || rollback "the snapshot build failed"
status_check "$NEW_RT/$SHA" "$NEW" "$NEW_RT/$SHA.status.json" || rollback "the new snapshot's status failed"
for f in capture_loop.sh forecast_loop.sh supervise.sh; do
  py "$REWRITE_PY" "$NEW_RT/bin/$f" "$NEW_RT/bin/$f.new" || rollback "the rewrite of $f failed"
  chmod 755 "$NEW_RT/bin/$f.new"
  bash -n "$NEW_RT/bin/$f.new" || rollback "the rewritten $f does not parse"
done
for f in capture_loop.sh forecast_loop.sh supervise.sh; do cp -p "$NEW_RT/bin/$f" "$NEW_RT/bin/$f.lookout.bak"; done
SCRIPTS_SWAPPED=1
for f in capture_loop.sh forecast_loop.sh supervise.sh; do mv -f "$NEW_RT/bin/$f.new" "$NEW_RT/bin/$f"; done
say "scripts rewritten (old ones in $NEW_RT/bin/*.lookout.bak)"
ln -sfn "$NEW_RT/$SHA" "$NEW_RT/current" || rollback "current was not flipped"
FLIPPED=1
say "current -> $(readlink "$NEW_RT/current")"

if [ "$NO_START" -eq 1 ]; then
  step "--no-start: start them yourself"
  printf '    at %s UTC (the old pass end + %s s) or later:\n' "$(date -u -r "$((${PASS_END%.*} + START_AFTER_PASS_S))" +%H:%M:%S)" "$START_AFTER_PASS_S"
  printf '      nohup %s >> %s 2>&1 < /dev/null &\n' "$NEW_RT/bin/capture_loop.sh" "$NEW_RT/logs/capture_loop.out"
  printf '    then stop the stand-in (kill -TERM %s), wait for its handler, and:\n' "${FC_PIDS:-<none>}"
  printf '      nohup %s >> %s 2>&1 < /dev/null &\n' "$NEW_RT/bin/forecast_loop.sh" "$NEW_RT/logs/forecast_loop.out"
  exit 0
fi

# ---- 7. the new capture loop, timed after the old loop's last pass, and checked -----------------------------------
step "7. start the new capture loop (a background job of this script)"
start_at=$((${PASS_END%.*} + START_AFTER_PASS_S))
wait_s=$((start_at - $(date -u +%s)))
if [ "$wait_s" -gt 0 ]; then
  say "waiting $wait_s s, so the new loop's first pass finds every camera due within its 60 s window"
  sleep "$wait_s"
fi
start_capture "$NEW_RT"
NEW_LOCK="$NEW/state/locks/nps-fetcher.lock"
cap_ok=0 pid=""
for _ in $(seq 1 60); do
  sleep 1
  alive "$NEW_CAP" || break
  pid="$(sed -n 's/.*"pid": *\([0-9][0-9]*\).*/\1/p' "$NEW_LOCK" 2>/dev/null | head -1)"
  if alive "$pid" && command_of "$pid" | grep -q -- 'snow-globe-capture loop'; then
    cap_ok=1
    break
  fi
done
[ "$cap_ok" -eq 1 ] || rollback "the new capture loop did not take $NEW_LOCK within 60 s (see $NEW_RT/logs/capture_loop.out)"
say "the new capture loop (pid $pid) holds the NPS fetcher lock"
if first="$(py -c "$PASS_PY" "$NEW/capture_log" "$(date -u +%s)" "$FIRST_PASS_WAIT_S" "snow-globe-capture/")"; then
  say "first pass of the new loop: $(printf '%s' "$first" | awk '{print $3 " at " $4}')"
elif alive "$NEW_CAP"; then
  say "WARNING: no snow-globe-capture pass logged within $FIRST_PASS_WAIT_S s; the loop (pid $pid) is still running: check $NEW_RT/logs/capture_loop.out"
else
  rollback "the new capture loop exited before its first pass (see $NEW_RT/logs/capture_loop.out)"
fi

# ---- 8. the stand-in: stop the old one, wait for its handler, start and check the new one -------------------------
step "8. the forecast stand-in"
standin_failed=""
for p in $FC_PIDS; do
  if alive "$p"; then
    kill -TERM "$p"
    FC_STOPPED=1
    say "SIGTERM to the old stand-in's bash $p"
  fi
done
for _ in $(seq 1 30); do
  [ -n "$(standin_pids)" ] || break
  sleep 1
done
for i in $(seq 1 "$HANDLER_WAIT_S"); do
  handler_running || break
  if [ $((i % 60)) -eq 1 ]; then say "the old stand-in's handler is still writing; waiting for it to finish"; fi
  sleep 1
done
if handler_running; then
  standin_failed="a stand-in handler is still running after $HANDLER_WAIT_S s, so no new stand-in was started (two never run at once). Once it ends: nohup $NEW_RT/bin/forecast_loop.sh >> $NEW_RT/logs/forecast_loop.out 2>&1 < /dev/null &"
elif [ -n "$(standin_pids)" ]; then
  standin_failed="a stand-in loop is still alive ($(standin_pids | tr '\n' ' ')), so no new one was started"
else
  log="$NEW_RT/logs/forecast_loop.out"
  mkdir -p "$NEW_RT/logs"
  touch "$log"
  from=$(($(wc -c <"$log") + 1))
  start_standin "$NEW_RT/bin/forecast_loop.sh" "$log"
  tick="unknown"
  for _ in $(seq 1 "$TICK_WAIT_S"); do
    sleep 1
    out="$(tail -c +"$from" "$log")"
    if printf '%s\n' "$out" | grep -q -E 'Traceback|Error'; then tick="failed"; break; fi
    if printf '%s\n' "$out" | grep -q '^{"extracted"'; then tick="ok"; break; fi
    if ! alive "$NEW_FC"; then tick="failed"; break; fi
  done
  case "$tick" in
    ok) say "the new stand-in's first tick finished: $(tail -c +"$from" "$log" | grep '^{"extracted"' | tail -1 | cut -c1-200)" ;;
    unknown) say "WARNING: the new stand-in's first tick has not finished within $TICK_WAIT_S s; it is still running: check $log" ;;
    failed)
      say "the new stand-in's first tick failed:"
      tail -c +"$from" "$log" | tail -8 | sed 's/^/    | /'
      kill -TERM "$NEW_FC" 2>/dev/null
      for _ in $(seq 1 "$HANDLER_WAIT_S"); do
        if ! handler_running && [ -z "$(standin_pids)" ]; then break; fi
        sleep 1
      done
      if handler_running || [ -n "$(standin_pids)" ]; then
        standin_failed="the new stand-in failed its first tick and did not stop cleanly; no stand-in was started again"
      else
        # The old script reads `current` once at its start: it names the pre-switch snapshot for that moment only.
        ln -sfn "$OLD_CURRENT" "$NEW_RT/current"
        from=$(($(wc -c <"$log") + 1))
        start_standin "$NEW_RT/bin/forecast_loop.sh.lookout.bak" "$log"
        for _ in $(seq 1 30); do
          sleep 1
          if tail -c +"$from" "$log" | grep -q "^tick .* code "; then break; fi
        done
        ln -sfn "$NEW_RT/$SHA" "$NEW_RT/current"
        standin_failed="the new stand-in failed its first tick; the old stand-in's script runs again (pid $NEW_FC, from $OLD_CURRENT: $(tail -c +"$from" "$log" | grep '^tick' | head -1)); current -> $(readlink "$NEW_RT/current") again"
      fi
      ;;
  esac
fi

# ---- 9. done: release the assertion and stay the loops' parent ----------------------------------------------------
step "9. done"
say "data at $NEW (symlink $OLD), current -> $(readlink "$NEW_RT/current"), capture loop pid $NEW_CAP${NEW_FC:+, stand-in pid $NEW_FC}"
supervisor_note
if [ -n "$standin_failed" ]; then say "STAND-IN: $standin_failed"; fi
if kill "$CAFF_PID" 2>/dev/null; then
  wait "$CAFF_PID" 2>/dev/null
  say "released this script's caffeinate (the capture loop holds its own caffeinate -s)"
fi
if [ -n "$standin_failed" ]; then
  say "exiting with status 1 for the stand-in; the capture loop keeps running"
  exit 1
fi
if [ "$DETACH" -eq 1 ]; then
  say "--detach: exiting; the loops keep running"
  exit 0
fi
say "waiting on the loops (capture $NEW_CAP, stand-in $NEW_FC); this command ends when both have exited"
wait "$NEW_CAP"
cap_rc=$?
wait "$NEW_FC"
fc_rc=$?
say "the capture loop exited with status $cap_rc and the stand-in with $fc_rc"
