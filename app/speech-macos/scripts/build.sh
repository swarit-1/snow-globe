#!/usr/bin/env bash
# Tests, release build and archive checks for app/speech-macos (PRD §6.5, §7.1, §15.1).
#
# Build output goes to $LOOKOUT_DATA_DIR/cache/speech-macos (default ~/lookout-data), outside the
# iCloud-synced repo; the M1 spike's build.rs finds the archive there. Needs no network after the
# toolchain is installed, no microphone and no permission: the tests never call the real
# recognizer, and nothing here runs lookout-transcribe on audio.
#
# Steps: swift test; swift build -c release; sign lookout-transcribe ad hoc with the fixed
# identifier dev.lookout.transcribe (its Info.plist's bundle identifier, so the tool has one
# stable identity for AssetInventory reservations and TCC); check-symbols (deny list and the
# reviewed import baseline); the checker's mutation self-test; link-check.
#
# usage: scripts/build.sh            # everything
#        scripts/build.sh --no-test  # build and check only
set -euo pipefail

cd "$(dirname "$0")/.."
SCRATCH="${LOOKOUT_DATA_DIR:-$HOME/lookout-data}/cache/speech-macos"
KEEP_AWAKE=()
if command -v caffeinate >/dev/null 2>&1; then KEEP_AWAKE=(caffeinate -i); fi

if [ "${1:-}" != "--no-test" ]; then
  ${KEEP_AWAKE[@]+"${KEEP_AWAKE[@]}"} swift test --scratch-path "$SCRATCH"
fi
${KEEP_AWAKE[@]+"${KEEP_AWAKE[@]}"} swift build -c release --scratch-path "$SCRATCH"

CLI="$SCRATCH/release/lookout-transcribe"
codesign --sign - --force --identifier dev.lookout.transcribe "$CLI" 2>/dev/null
SIGNED="$(codesign -dv "$CLI" 2>&1)"
if ! grep -q '^Identifier=dev.lookout.transcribe$' <<<"$SIGNED" || ! grep -q '^Info.plist entries=' <<<"$SIGNED"; then
  echo "build: lookout-transcribe is not signed as dev.lookout.transcribe with its Info.plist bound:" >&2
  echo "$SIGNED" >&2
  exit 1
fi
echo "signed: $(grep -E '^(Identifier|Info.plist)' <<<"$SIGNED" | tr '\n' ' ')"

scripts/check-symbols.sh "$SCRATCH/release/liblookout_speech.a"
scripts/check-symbols-selftest.sh "$SCRATCH/release/liblookout_speech.a"
scripts/link-check.sh "$SCRATCH/release"
ls -l "$SCRATCH/release/liblookout_speech.a" "$CLI"
