#!/usr/bin/env bash
# Tests, release build and archive checks for app/speech-macos (PRD §6.5, §7.1, §15.1).
#
# Build output goes to <data dir>/cache/speech-macos (data_dir below: default ~/snow-globe-data), outside the
# iCloud-synced repo; the M1 spike's build.rs finds the archive there. Needs no network after the
# toolchain is installed, no microphone and no permission: the tests never call the real
# recognizer, and nothing here runs snow-globe-transcribe on audio.
#
# Steps: swift test; swift build -c release; sign snow-globe-transcribe ad hoc with the fixed
# identifier dev.snowglobe.transcribe (its Info.plist's bundle identifier, so the tool has one
# stable identity for AssetInventory reservations and TCC); check-symbols (deny list and the
# reviewed import baseline); the checker's mutation self-test; link-check.
#
# usage: scripts/build.sh            # everything
#        scripts/build.sh --no-test  # build and check only
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
DATA_DIR="$(data_dir)" || exit 1
SCRATCH="$DATA_DIR/cache/speech-macos"
KEEP_AWAKE=()
if command -v caffeinate >/dev/null 2>&1; then KEEP_AWAKE=(caffeinate -i); fi

if [ "${1:-}" != "--no-test" ]; then
  ${KEEP_AWAKE[@]+"${KEEP_AWAKE[@]}"} swift test --scratch-path "$SCRATCH"
fi
${KEEP_AWAKE[@]+"${KEEP_AWAKE[@]}"} swift build -c release --scratch-path "$SCRATCH"

CLI="$SCRATCH/release/snow-globe-transcribe"
codesign --sign - --force --identifier dev.snowglobe.transcribe "$CLI" 2>/dev/null
SIGNED="$(codesign -dv "$CLI" 2>&1)"
if ! grep -q '^Identifier=dev.snowglobe.transcribe$' <<<"$SIGNED" || ! grep -q '^Info.plist entries=' <<<"$SIGNED"; then
  echo "build: snow-globe-transcribe is not signed as dev.snowglobe.transcribe with its Info.plist bound:" >&2
  echo "$SIGNED" >&2
  exit 1
fi
echo "signed: $(grep -E '^(Identifier|Info.plist)' <<<"$SIGNED" | tr '\n' ' ')"

scripts/check-symbols.sh "$SCRATCH/release/libsnow_globe_speech.a"
scripts/check-symbols-selftest.sh "$SCRATCH/release/libsnow_globe_speech.a"
scripts/link-check.sh "$SCRATCH/release"
ls -l "$SCRATCH/release/libsnow_globe_speech.a" "$CLI"
