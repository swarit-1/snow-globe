#!/usr/bin/env bash
# Smoke-test the Snow Globe Lambda image locally (PRD §11.3, D-014). It builds nothing, pushes nothing, touches no AWS
# account, and never invokes the capture handler: that handler polls nps.gov, and only one NPS fetcher may run.
#
# Usage:
#   infra/lambda/smoke.sh IMAGE            the offline probe, then the forecast handler under the Lambda Runtime
#                                          Interface Emulator, invoked twice (reads NOAA's public HRRR bucket)
#   infra/lambda/smoke.sh --offline IMAGE  the offline probe only
#
# 1. Offline probe: the image's default command must not be the capture handler. Then, with no network, a
#    read-only root file system and a non-root user, infra/lambda/smoke_probe.py resolves both handlers with the
#    Lambda runtime's own loader, checks that the pinned packages and ecCodes' native library load from /var/task,
#    and builds the capture handler's manifests, storage and HTTP client without using them. Nothing is invoked.
# 2. Forecast invoke: the emulator bundled in the AWS base image (/usr/local/bin/aws-lambda-rie) runs
#    snow_globe_forecast.lambda_handler.handler, with SNOW_GLOBE_STORAGE=local into a new temporary directory and a tiny
#    profile (VIS:surface, forecast hour 0, at most 10 MB a run, 1 h lookback). The container gets the deployed
#    function's settings where Docker can copy them: 2048 MB and no swap, 1.16 vCPU (Lambda gives one vCPU per
#    1,769 MB), a 2 GB /tmp, a read-only root and a non-root user. It is invoked twice. The second call must find
#    the first call's runs already written, and no GRIB file may be left behind. The container and the temporary
#    directory are removed on exit.
#
# The emulator's own REPORT line shows "Init Duration: 0.04 ms" and a "Max Memory Used" equal to the memory size.
# Both are emulator artefacts, so this script reports the runtime's INIT REPORT, each INVOKE RTDONE duration, the
# container's cgroup memory.peak and its network bytes instead. Results go to stdout as key=value lines.
#
# Exit codes: 0 ok, 1 a check failed, 2 usage error, 4 Docker, curl or jq unavailable.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROBE="$HERE/smoke_probe.py"
DOCKER="${DOCKER:-docker}"
HANDLER="snow_globe_forecast.lambda_handler.handler"

log() { printf 'smoke.sh: %s\n' "$*" >&2; }
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}" >&2; }
status=0
fail() { log "FAIL $*"; status=1; }

offline=0
image=""
while [ $# -gt 0 ]; do
  case "$1" in
    --offline) offline=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) log "unknown option: $1"; usage; exit 2 ;;
    *)
      [ -z "$image" ] || { log "give one image"; exit 2; }
      image="$1"; shift ;;
  esac
done
[ -n "$image" ] || { usage; exit 2; }

for tool in "$DOCKER" curl jq; do
  command -v "$tool" >/dev/null 2>&1 || { log "$tool not found"; exit 4; }
done
"$DOCKER" info >/dev/null 2>&1 || { log "the Docker daemon is not running (start Docker Desktop)"; exit 4; }
"$DOCKER" image inspect "$image" >/dev/null 2>&1 || { log "no local image '$image' (infra/lambda/build.sh builds one)"; exit 2; }

# Non-root, as on Lambda. The invoking user's ids keep the temporary files removable on any host.
uid="$(id -u)"
gid="$(id -g)"
if [ "$uid" = 0 ]; then uid=993; gid=990; fi

# ---- 1. offline probe -------------------------------------------------------------------------------------------
# A local `docker run` without a command runs the default one, which must never be the NPS-polling capture handler.
default_cmd="$("$DOCKER" image inspect --format '{{json .Config.Cmd}}' "$image")"
printf 'default_cmd=%s\n' "$default_cmd"
case "$default_cmd" in
  *snow_globe_capture*) fail "the image's default command is the capture handler, so a local run would poll nps.gov" ;;
esac

log "1/2 offline probe of $image: no network, read-only root, user $uid:$gid"
if probe="$("$DOCKER" run --rm --network none --read-only --tmpfs /tmp:rw,size=64m --user "$uid:$gid" \
      -v "$PROBE:/probe/smoke_probe.py:ro" \
      -e SNOW_GLOBE_STORAGE=s3 -e SNOW_GLOBE_S3_BUCKET=snow-globe-smoke-no-such-bucket -e AWS_REGION=us-east-1 \
      --entrypoint /var/lang/bin/python3.13 "$image" /probe/smoke_probe.py)"; then
  log "offline probe passed"
else
  fail "offline probe: $(printf '%s' "$probe" | jq -c '.failures' 2>/dev/null || echo 'no output')"
fi
printf '%s\n' "$probe" >&2
printf 'probe_failures=%s\n' "$(printf '%s' "$probe" | jq -c '.failures' 2>/dev/null || echo '"no output"')"
printf 'packages=%s\n' "$(printf '%s' "$probe" | jq -r '.dists_task_root | join(",")' 2>/dev/null || true)"

if [ "$offline" -eq 1 ]; then
  [ "$status" -eq 0 ] && log "ok (offline only)"
  exit "$status"
fi

# ---- 2. forecast handler under the emulator ---------------------------------------------------------------------
work="$(mktemp -d "${TMPDIR:-/tmp}/snow-globe-lambda-smoke.XXXXXX")"
data="$work/data"
mkdir -p "$data"
name="snow-globe-lambda-smoke-$$"
# Invoked only by the EXIT trap below. shellcheck 0.9 (CI's ubuntu-24.04) calls its body unreachable (SC2317),
# 0.10 and later call the function unused (SC2329); both are disabled for this function alone.
# shellcheck disable=SC2317,SC2329
cleanup() {
  "$DOCKER" rm -f "$name" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

log "2/2 $HANDLER under the Lambda Runtime Interface Emulator; data in a temporary directory"
"$DOCKER" run -d --name "$name" \
  --read-only --tmpfs /tmp:rw,exec,size=2048m,mode=1777 \
  --memory 2048m --memory-swap 2048m --cpus 1.16 \
  --user "$uid:$gid" \
  -p 127.0.0.1::8080 \
  -v "$data:/mnt/snow-globe-data" \
  -e AWS_LAMBDA_FUNCTION_NAME=snow-globe-forecast \
  -e AWS_LAMBDA_FUNCTION_MEMORY_SIZE=2048 \
  -e AWS_LAMBDA_FUNCTION_TIMEOUT=600 \
  -e SNOW_GLOBE_STORAGE=local \
  -e SNOW_GLOBE_DATA_DIR=/mnt/snow-globe-data \
  -e SNOW_GLOBE_FORECAST_FIELDS=VIS:surface \
  -e SNOW_GLOBE_FORECAST_MAX_FH=0 \
  -e SNOW_GLOBE_FORECAST_MAX_MB=10 \
  -e SNOW_GLOBE_FORECAST_LOOKBACK_H=1 \
  "$image" "$HANDLER" >/dev/null

port="$("$DOCKER" port "$name" 8080/tcp | sed -n 's/^127\.0\.0\.1:\([0-9][0-9]*\)$/\1/p' | head -n 1)"
[ -n "$port" ] || { log "FAIL the emulator's port was not published"; exit 1; }
for _ in $(seq 1 50); do
  if curl -s -o /dev/null "http://127.0.0.1:$port/"; then break; fi
  sleep 0.2
done

files_report() {  # label
  local count bytes
  count="$(find "$data" -type f | wc -l | tr -d ' ')"
  bytes="$(find "$data" -type f -exec cat {} + 2>/dev/null | wc -c | tr -d ' ')"
  printf '%s_files=%s\n%s_file_bytes=%s\n' "$1" "$count" "$1" "$bytes"
  (cd "$data" && find . -type f | LC_ALL=C sort | sed "s|^\./|  $1: |") >&2
  if [ -n "$(find "$data" \( -name '*.grib2' -o -name '*.grib' -o -name '*.idx' \) -print)" ]; then
    fail "GRIB left under the data directory after $1"
  fi
}

answered=0
for n in 1 2; do
  body="$(printf '{"job":"forecast","scheduled_time":"%s","execution_id":"local-smoke-%s"}' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$n")"
  code="$(curl -sS --max-time 660 -X POST "http://127.0.0.1:$port/2015-03-31/functions/function/invocations" \
    -H 'content-type: application/json' -d "$body" -o "$work/response$n.json" -w '%{http_code}')" || code="curl failed"
  # A function error comes back as HTTP 200 with errorType; anything but a run summary is a failure.
  if [ "$code" != 200 ] ||
    ! jq -e 'type == "object" and has("runs") and (has("errorType") | not)' "$work/response$n.json" >/dev/null 2>&1; then
    fail "invoke $n: HTTP $code, $(head -c 400 "$work/response$n.json" 2>/dev/null || true)"
    continue
  fi
  answered=$((answered + 1))
  jq -c '{extracted, runs: [.runs[] | {run, status, bytes}], transfer}' "$work/response$n.json" | sed "s/^/invoke${n}_summary=/"
  printf 'invoke%s_bytes=%s\n' "$n" "$(jq '.transfer.total_bytes' "$work/response$n.json")"
  printf 'invoke%s_net_io=%s\n' "$n" "$("$DOCKER" stats --no-stream --format '{{.NetIO}}' "$name")"
  files_report "invoke$n"
done

if [ "$answered" -eq 2 ]; then
  if ! jq -e '.extracted >= 1' "$work/response1.json" >/dev/null; then
    fail "invoke 1 extracted no run, so the decode path was not exercised (NOAA late? try again in a few minutes)"
  fi
  # Every run the first call extracted must be 'written' (skipped) in the second.
  if ! jq -e -n --slurpfile a "$work/response1.json" --slurpfile b "$work/response2.json" '
      ([$a[0].runs[] | select(.status | startswith("extracted")) | .run]
        - [$b[0].runs[] | select(.status == "written") | .run]) == []' >/dev/null; then
    fail "invoke 2 did not find invoke 1's runs written (idempotency)"
  fi
fi

peak="$("$DOCKER" exec "$name" cat /sys/fs/cgroup/memory.peak 2>/dev/null || true)"
"$DOCKER" logs "$name" >"$work/container.log" 2>&1 || true
grep -E 'INIT REPORT|INVOKE RTDONE|^REPORT|forecast worker:|\[ERROR\]' "$work/container.log" >&2 || true
printf 'init_ms=%s\n' "$(sed -n 's/.*INIT REPORT(durationMs: \([0-9.]*\)).*/\1/p' "$work/container.log" | head -n 1)"
sed -n 's/.*INVOKE RTDONE(status: \([a-z]*\).*duration: \([0-9.]*\)ms).*/\1 \2/p' "$work/container.log" |
  awk '{ printf "invoke%d_status=%s\ninvoke%d_ms=%s\n", NR, $1, NR, $2 }'
if [ -n "$peak" ]; then
  awk -v b="$peak" 'BEGIN { printf "memory_peak_mib=%.1f\n", b / 1048576 }'
fi
if grep -q '\[ERROR\]' "$work/container.log"; then
  fail "the handler logged an error"
fi

if [ "$status" -eq 0 ]; then log "ok"; fi
exit "$status"
