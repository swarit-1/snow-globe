#!/bin/bash
# Keeps the capture loop and the forecast stand-in running, detached from any agent session.
#
# Why: loops started from an agent session are children of that session, so ending the session
# can stop them, and the M0.5 72 h clock with them. This supervisor runs in its own
# session (parent launchd). It never edits a snapshot; it only starts the existing scripts in this
# folder, which resolve ~/lookout-data/cache/runtime/current once at start.
#
# Start it from your own Terminal. The parentheses matter: in an interactive shell a background
# job leads its own process group, so setsid() fails (Perl returns -1, which `or die` misses):
#   mkdir -p ~/lookout-data/cache/runtime/logs && ( nohup /usr/bin/perl -MPOSIX -e \
#     'POSIX::setsid() != -1 or die "setsid: $!\n"; exec @ARGV or die "exec: $!\n"' \
#     /bin/bash ~/lookout-data/cache/runtime/bin/supervise.sh \
#     >> ~/lookout-data/cache/runtime/logs/supervisor.out 2>&1 < /dev/null & )
#   Check: ps -o pid,ppid,tty,stat,command -p "$(cat ~/lookout-data/cache/runtime/supervisor.pid)"
#   shows PPID 1, TTY ?? and an s in STAT.
# Stop: kill "$(cat ~/lookout-data/cache/runtime/supervisor.pid)" stops the supervisor and any loop it
#   started (capture finishes its current camera and saves state). A loop it adopted is left
#   running: it belongs to whoever started it.
# Re-snapshot: flip `current`, touch ~/lookout-data/cache/runtime/graceful-stop.capture, then
#   SIGTERM the capture loop's python and the forecast loop's bash; each restarts from `current`.
#
# Adoption (the default): at start it finds loops already running and starts each replacement only
# after that loop exits, so two NPS fetchers or two stand-ins never run at once. Capture's pid is
# read from the NPS fetcher lock; the forecast loop is found by its script path. Override with
# --adopt-capture PID|auto|none and --adopt-forecast PID|auto|none.
#
# Restart delays (PRD 15.3, CLAUDE.md polite collection):
#   - capture exited 0 (a graceful stop: state saved): 10 s.
#   - capture exited 3 (another NPS fetcher holds the host lock): 60 s, then try again.
#   - capture ended any other way (crash, SIGKILL, or an adopted loop whose exit is unknown):
#     330 s. A process killed between a poll's send and its state save leaves that poll
#     unrecorded; waiting longer than the 300 s poll interval plus the request time means
#     nps.gov can never see the next poll within 5 minutes of it. A graceful stop of an adopted
#     loop can be announced with the graceful-stop.capture flag file (then 10 s).
#   - a job that dies within 600 s of its start doubles its delay, up to 3600 s.
#   - forecast: 10 s, and never while an earlier stand-in's handler is still running.
# Restart windows: while it runs it holds its own `caffeinate -s`, the same AC-only assertion the
# capture loop holds, so the Mac cannot sleep between one loop's exit and the next one's start.
# This Mac's system sleep is 1 minute: on 20 Nov it slept one second after a loop exited (D-079).
# Keep-awake (opt-in): with LOOKOUT_AWAKE_UNTIL_UTC=YYYY-MM-DDTHH:MM:SSZ set, a `caffeinate -ims`
# assertion keeps the system (not the display) awake until then, on battery too. Without it, only
# the capture loop's own `caffeinate -s` applies, which works on AC power only.
set -u
RT="${LOOKOUT_RUNTIME_DIR:-$HOME/lookout-data/cache/runtime}"
# Overridable only so the script can be tested against fake loops in a scratch folder.
DELAY_OK=${SUP_DELAY_OK:-10}; DELAY_LOCKED=${SUP_DELAY_LOCKED:-60}; DELAY_CRASH=${SUP_DELAY_CRASH:-330}
QUICK_S=${SUP_QUICK_S:-600}; MAX_DELAY=${SUP_MAX_DELAY:-3600}; TICK=${SUP_TICK:-5}
HANDLER_PATTERN=${SUP_HANDLER_PATTERN:-'lookout_forecast.lambda_handler import handler'}
BIN="$RT/bin"
DATA="${LOOKOUT_DATA_DIR:-$HOME/lookout-data}"
FETCHER_LOCK="$DATA/state/locks/nps-fetcher.lock"
LOGDIR="$RT/logs"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/supervisor.log"
PIDFILE="$RT/supervisor.pid"
FLAG="$RT/graceful-stop.capture"
UNTIL_UTC="${LOOKOUT_AWAKE_UNTIL_UTC:-}"  # empty: no keep-awake of its own

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$LOG"; }

ADOPT_CAPTURE="auto"
ADOPT_FORECAST="auto"
while [ $# -gt 0 ]; do
  case "$1" in
    --adopt-capture) ADOPT_CAPTURE="$2"; shift 2 ;;
    --adopt-forecast) ADOPT_FORECAST="$2"; shift 2 ;;
    *) echo "supervise.sh: unknown argument $1" >&2; exit 2 ;;
  esac
done

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "supervise.sh: already running as pid $(cat "$PIDFILE")" >&2
  exit 1
fi
echo $$ > "$PIDFILE"

alive() { [ -n "$1" ] && kill -0 "$1" 2>/dev/null; }
# An adopted pid counts only while it still runs the expected command (guards against pid reuse).
alive_as() { alive "$1" && ps -p "$1" -o command= 2>/dev/null | grep -q -- "$2"; }
handler_running() { pgrep -f "$HANDLER_PATTERN" > /dev/null 2>&1; }

# The capture loop holding the NPS fetcher lock, if it is alive and is a capture loop.
detect_capture() {
  local pid
  pid=$(sed -n 's/.*"pid": *\([0-9][0-9]*\).*/\1/p' "$FETCHER_LOCK" 2>/dev/null | head -1)
  if [ -n "$pid" ] && alive_as "$pid" 'lookout-capture loop'; then echo "$pid"; fi
}
# The oldest running forecast_loop.sh from this runtime folder.
detect_forecast() {
  local pid
  for pid in $(pgrep -f "$BIN/forecast_loop.sh" 2>/dev/null); do
    if [ "$pid" != "$$" ] && alive_as "$pid" 'forecast_loop.sh'; then echo "$pid"; return; fi
  done
}
case "$ADOPT_CAPTURE" in
  auto) ADOPT_CAPTURE=$(detect_capture) ;;
  none) ADOPT_CAPTURE="" ;;
esac
case "$ADOPT_FORECAST" in
  auto) ADOPT_FORECAST=$(detect_forecast) ;;
  none) ADOPT_FORECAST="" ;;
esac

CAP_PID=""; CAP_NEXT=0; CAP_STARTED=0; CAP_BACKOFF=0
FC_PID="";  FC_NEXT=0;  FC_STARTED=0;  FC_BACKOFF=0
AWAKE_PID=""
STOP=0
trap 'STOP=1' TERM INT HUP

# Seconds until UNTIL_UTC, or 0 if it has passed or cannot be read.
awake_left() {
  [ -n "$UNTIL_UTC" ] || { echo 0; return; }
  local until_s
  until_s=$(date -j -u -f '%Y-%m-%dT%H:%M:%SZ' "$UNTIL_UTC" +%s 2>/dev/null) || { echo 0; return; }
  local left=$((until_s - $(date +%s)))
  [ "$left" -gt 0 ] && echo "$left" || echo 0
}

# Delay after a job ends: the base delay, doubled per quick death (run < 600 s), capped at 3600 s.
next_delay() { # $1 base, $2 started_at, $3 current backoff count -> prints "delay backoff"
  local base=$1 started=$2 backoff=$3 now
  now=$(date +%s)
  if [ $((now - started)) -lt "$QUICK_S" ] && [ "$started" -gt 0 ]; then backoff=$((backoff + 1)); else backoff=0; fi
  local d=$base i=0
  while [ $i -lt $backoff ] && [ $d -lt "$MAX_DELAY" ]; do d=$((d * 2)); i=$((i + 1)); done
  [ $d -gt "$MAX_DELAY" ] && d=$MAX_DELAY
  echo "$d $backoff"
}

log "supervisor $$ started; current -> $(readlink "$RT/current"); adopt capture=${ADOPT_CAPTURE:-none} forecast=${ADOPT_FORECAST:-none}; awake until ${UNTIL_UTC:-never (opt-in)}"
caffeinate -s -w $$ &
HOLD_PID=$!
log "caffeinate -s while the supervisor runs (pid $HOLD_PID): no sleep between restarts on AC"
left=$(awake_left)
if [ "$left" -gt 0 ]; then
  caffeinate -ims -t "$left" -w $$ &
  AWAKE_PID=$!
  log "caffeinate -ims for ${left} s (pid $AWAKE_PID)"
fi

while [ $STOP -eq 0 ]; do
  now=$(date +%s)

  # --- capture -------------------------------------------------------------------------------
  if [ -n "$ADOPT_CAPTURE" ]; then
    if ! alive_as "$ADOPT_CAPTURE" 'lookout-capture loop'; then
      if [ -f "$FLAG" ]; then d=$DELAY_OK; rm -f "$FLAG"; why="graceful stop announced"; else d=$DELAY_CRASH; why="exit unknown"; fi
      log "adopted capture loop $ADOPT_CAPTURE has exited ($why); starting a new one in ${d} s"
      ADOPT_CAPTURE=""; CAP_NEXT=$((now + d)); CAP_STARTED=0
    fi
  elif [ -n "$CAP_PID" ]; then
    if ! alive "$CAP_PID"; then
      wait "$CAP_PID"; rc=$?
      if [ -f "$FLAG" ]; then rm -f "$FLAG"; fi
      case $rc in
        0) base=$DELAY_OK ;;
        3) base=$DELAY_LOCKED ;;
        *) base=$DELAY_CRASH ;;
      esac
      set -- $(next_delay $base $CAP_STARTED $CAP_BACKOFF); d=$1; CAP_BACKOFF=$2
      log "capture loop $CAP_PID exited with status $rc; starting a new one in ${d} s"
      CAP_PID=""; CAP_NEXT=$((now + d))
    fi
  elif [ "$now" -ge "$CAP_NEXT" ]; then
    nohup "$BIN/capture_loop.sh" >> "$LOGDIR/capture_loop.out" 2>&1 < /dev/null &
    CAP_PID=$!; CAP_STARTED=$now
    log "capture loop started as pid $CAP_PID from $(readlink "$RT/current")"
  fi

  # --- forecast stand-in ---------------------------------------------------------------------
  if [ -n "$ADOPT_FORECAST" ]; then
    if ! alive_as "$ADOPT_FORECAST" 'forecast_loop.sh'; then
      log "adopted forecast loop $ADOPT_FORECAST has exited; starting a new one in ${DELAY_OK} s"
      ADOPT_FORECAST=""; FC_NEXT=$((now + DELAY_OK)); FC_STARTED=0
    fi
  elif [ -n "$FC_PID" ]; then
    if ! alive "$FC_PID"; then
      wait "$FC_PID"; rc=$?
      set -- $(next_delay $DELAY_OK $FC_STARTED $FC_BACKOFF); d=$1; FC_BACKOFF=$2
      log "forecast loop $FC_PID exited with status $rc; starting a new one in ${d} s"
      FC_PID=""; FC_NEXT=$((now + d))
    fi
  elif [ "$now" -ge "$FC_NEXT" ]; then
    if handler_running; then
      FC_NEXT=$((now + 15))  # an earlier stand-in's handler is still finishing its run
    else
      nohup "$BIN/forecast_loop.sh" >> "$LOGDIR/forecast_loop.out" 2>&1 < /dev/null &
      FC_PID=$!; FC_STARTED=$now
      log "forecast loop started as pid $FC_PID from $(readlink "$RT/current")"
    fi
  fi

  sleep "$TICK" &
  wait $! 2>/dev/null
done

log "supervisor $$ stopping: SIGTERM to capture ${CAP_PID:-none} and forecast ${FC_PID:-none}"
[ -n "$CAP_PID" ] && kill -TERM "$CAP_PID" 2>/dev/null
[ -n "$FC_PID" ] && kill -TERM "$FC_PID" 2>/dev/null
for _ in $(seq 1 120); do
  alive "$CAP_PID" || break
  sleep 1
done
alive "$CAP_PID" && log "capture loop $CAP_PID still running after 120 s; left to finish"
[ -n "$AWAKE_PID" ] && kill "$AWAKE_PID" 2>/dev/null
kill "$HOLD_PID" 2>/dev/null
rm -f "$PIDFILE"
log "supervisor $$ stopped"
