#!/usr/bin/env bash
# Links a small C program against liblookout_speech.a with the flags a non-Swift host needs (the
# Tauri shell, the M1 spike), then runs it. The program calls only lk_speech_version and argument
# checks that return before any session exists, so it never reaches the recognizer, needs no
# permission and touches no audio (PRD §6.5: Swift in-process static library, C ABI).
#
# usage: scripts/link-check.sh [dir containing liblookout_speech.a]
set -euo pipefail

cd "$(dirname "$0")/.."
LIBDIR="${1:-${LOOKOUT_DATA_DIR:-$HOME/lookout-data}/cache/speech-macos/release}"
SDK="$(xcrun --sdk macosx --show-sdk-path)"
OUT="$(mktemp -d "${TMPDIR:-/tmp}/lk-link.XXXXXX")"
trap 'rm -rf "$OUT"' EXIT

cat >"$OUT/host.c" <<'EOF'
#include <stdio.h>
#include <string.h>
#include "lookout_speech.h"

static void cb(void *ctx, lk_event_kind kind, const char *text, double a, double b, int32_t e) {
  (void)ctx; (void)kind; (void)text; (void)a; (void)b; (void)e;
}

int main(void) {
  int bad = 0;
  const char *v = lk_speech_version();
  printf("version: %s\n", v);
  if (v == NULL || strncmp(v, "lookout-speech ", 15) != 0) bad = 1;
  /* Each of these is rejected before a session (and so the recognizer) exists. */
  if (lk_speech_start(NULL, 48000.0, cb, NULL) != NULL) bad = 1;
  if (lk_speech_start("en-US", 1.0, cb, NULL) != NULL) bad = 1;
  if (lk_speech_start("en-US", 48000.0, NULL, NULL) != NULL) bad = 1;
  if (lk_speech_push_f32(NULL, NULL, 1) != LK_SPEECH_ERR_ARGUMENT) bad = 1;
  if (lk_speech_finish(NULL) != LK_SPEECH_ERR_ARGUMENT) bad = 1;
  if (lk_speech_set_silence_pad_ms(-1) != LK_SPEECH_ERR_ARGUMENT) bad = 1;
  if (lk_speech_set_model_retention(7) != LK_SPEECH_ERR_ARGUMENT) bad = 1;
  if (lk_speech_status(NULL) != LK_SPEECH_ERR_ARGUMENT) bad = 1;
  lk_speech_cancel(NULL);
  lk_speech_free(NULL);
  printf("%s\n", bad ? "link-check: FAILED" : "link-check: passed");
  return bad;
}
EOF

# The minimal link line: the archive, the SDK's Swift runtime stubs, the runtime's install
# location as an rpath, and the frameworks the library's code calls. The Swift objects' autolink
# entries pull in the rest (libswiftCore, libswift_Concurrency, ...).
xcrun clang -std=c11 -Wall -Wextra -Werror -I include -c "$OUT/host.c" -o "$OUT/host.o"
xcrun clang "$OUT/host.o" -o "$OUT/host" \
  -L "$LIBDIR" -llookout_speech \
  -L "$SDK/usr/lib/swift" -Wl,-rpath,/usr/lib/swift \
  -framework Foundation -framework AVFoundation -framework CoreMedia -framework Speech
echo "linked: $(stat -f %z "$OUT/host") bytes"
"$OUT/host"
