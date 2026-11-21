#!/usr/bin/env bash
# Links a small C program against libsnow_globe_speech.a with the flags a non-Swift host needs (the
# Tauri shell, the M1 spike), then runs it. The program calls only sg_speech_version and argument
# checks that return before any session exists, so it never reaches the recognizer, needs no
# permission and touches no audio (PRD §6.5: Swift in-process static library, C ABI).
#
# usage: scripts/link-check.sh [dir containing libsnow_globe_speech.a]
set -euo pipefail

cd "$(dirname "$0")/.."
# The data dir, resolved as the workers resolve it (D-080): $SNOW_GLOBE_DATA_DIR, else the deprecated
# $LOOKOUT_DATA_DIR (blank counts as unset; surrounding whitespace is dropped), else ~/snow-globe-data if it
# exists, else ~/lookout-data if it exists (the folder's name before the rename), else ~/snow-globe-data.
# Symlinks are followed and nothing is created. While both folders exist and are different folders (something
# made ~/snow-globe-data before the rename's live switch), it prints why on stderr and returns 1.
data_dir() {
  local v new="$HOME/snow-globe-data" old="$HOME/lookout-data"
  for v in "${SNOW_GLOBE_DATA_DIR:-}" "${LOOKOUT_DATA_DIR:-}"; do
    v="${v#"${v%%[![:space:]]*}"}"
    v="${v%"${v##*[![:space:]]}"}"
    if [ -n "$v" ]; then
      printf '%s\n' "$v"
      return 0
    fi
  done
  if [ -e "$new" ] && [ -e "$old" ] && ! [ "$new" -ef "$old" ]; then
    printf '%s\n' "both $new and $old exist and are different folders, so the data dir is ambiguous (D-080):" \
      "until the rename's live switch the data is in $old, and after it $old is a symlink to $new." \
      "Set SNOW_GLOBE_DATA_DIR to the folder you mean, and tell the lead which one is stray." >&2
    return 1
  elif [ -e "$new" ] || [ ! -e "$old" ]; then
    printf '%s\n' "$new"
  else
    printf '%s\n' "$old"
  fi
}
if [ -n "${1:-}" ]; then
  LIBDIR="$1"
else
  DATA_DIR="$(data_dir)" || exit 1
  LIBDIR="$DATA_DIR/cache/speech-macos/release"
fi
SDK="$(xcrun --sdk macosx --show-sdk-path)"
OUT="$(mktemp -d "${TMPDIR:-/tmp}/sg-link.XXXXXX")"
trap 'rm -rf "$OUT"' EXIT

cat >"$OUT/host.c" <<'EOF'
#include <stdio.h>
#include <string.h>
#include "snow_globe_speech.h"

static void cb(void *ctx, sg_event_kind kind, const char *text, double a, double b, int32_t e) {
  (void)ctx; (void)kind; (void)text; (void)a; (void)b; (void)e;
}

int main(void) {
  int bad = 0;
  const char *v = sg_speech_version();
  printf("version: %s\n", v);
  if (v == NULL || strncmp(v, "snow-globe-speech ", 18) != 0) bad = 1;
  /* Each of these is rejected before a session (and so the recognizer) exists. */
  if (sg_speech_start(NULL, 48000.0, cb, NULL) != NULL) bad = 1;
  if (sg_speech_start("en-US", 1.0, cb, NULL) != NULL) bad = 1;
  if (sg_speech_start("en-US", 48000.0, NULL, NULL) != NULL) bad = 1;
  if (sg_speech_push_f32(NULL, NULL, 1) != SG_SPEECH_ERR_ARGUMENT) bad = 1;
  if (sg_speech_finish(NULL) != SG_SPEECH_ERR_ARGUMENT) bad = 1;
  if (sg_speech_set_silence_pad_ms(-1) != SG_SPEECH_ERR_ARGUMENT) bad = 1;
  if (sg_speech_set_model_retention(7) != SG_SPEECH_ERR_ARGUMENT) bad = 1;
  if (sg_speech_status(NULL) != SG_SPEECH_ERR_ARGUMENT) bad = 1;
  sg_speech_cancel(NULL);
  sg_speech_free(NULL);
  printf("%s\n", bad ? "link-check: FAILED" : "link-check: passed");
  return bad;
}
EOF

# The minimal link line: the archive, the SDK's Swift runtime stubs, the runtime's install
# location as an rpath, and the frameworks the library's code calls. The Swift objects' autolink
# entries pull in the rest (libswiftCore, libswift_Concurrency, ...).
xcrun clang -std=c11 -Wall -Wextra -Werror -I include -c "$OUT/host.c" -o "$OUT/host.o"
xcrun clang "$OUT/host.o" -o "$OUT/host" \
  -L "$LIBDIR" -lsnow_globe_speech \
  -L "$SDK/usr/lib/swift" -Wl,-rpath,/usr/lib/swift \
  -framework Foundation -framework AVFoundation -framework CoreMedia -framework Speech
echo "linked: $(stat -f %z "$OUT/host") bytes"
"$OUT/host"
