#!/bin/bash
# Wraps the release m1-sck in a minimal app bundle so macOS attributes Screen Recording to the
# helper itself rather than to the terminal (PLAN.md M1 1a check 11; PRD §18.1: "a harness-only
# signed identity"). The bundle goes to <data dir>/cache/spike-m1/, never into the repo. The data dir
# is the workers' (D-080): $SNOW_GLOBE_DATA_DIR, else $LOOKOUT_DATA_DIR, else ~/snow-globe-data if it
# exists, else ~/lookout-data if it exists, else ~/snow-globe-data.
#
#   scripts/bundle-sck-helper.sh                 # ad-hoc signature (identity changes on rebuild)
#   scripts/bundle-sck-helper.sh "Apple Development: <name> (<team>)"   # a stable identity
#
# Then run it through LaunchServices (no terminal, so pass the answer later with m1-report note):
#   open -W -n --env SNOW_GLOBE_DATA_DIR="$SNOW_GLOBE_DATA_DIR" \
#     --stdout "$SNOW_GLOBE_DATA_DIR/cache/spike-m1/consent.out" \
#     "$SNOW_GLOBE_DATA_DIR/cache/spike-m1/M1SckProbe.app" --args consent --run-id <id> --scenario repeat
#
# This script only copies and signs local files. It does not run the helper.
set -euo pipefail

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
TARGET_DIR="${CARGO_TARGET_DIR:-$DATA_DIR/cache/cargo-target}"
IDENTITY="${1:--}"
BIN="$TARGET_DIR/release/m1-sck"
OUT_DIR="$DATA_DIR/cache/spike-m1"
APP="$OUT_DIR/M1SckProbe.app"

if [ ! -x "$BIN" ]; then
  echo "no $BIN; build it first: cargo build --release -p m1-spike-sck" >&2
  exit 1
fi
case "$APP" in
  "$(cd "$(dirname "$0")/../../.." && pwd)"/*) echo "refusing to write inside the repository" >&2; exit 1 ;;
esac

mkdir -p "$OUT_DIR"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp "$BIN" "$APP/Contents/MacOS/m1-sck"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>dev.snowglobe.spike-m1.sck</string>
  <key>CFBundleName</key><string>M1SckProbe</string>
  <key>CFBundleExecutable</key><string>m1-sck</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>26.0</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

codesign --force --sign "$IDENTITY" --identifier dev.snowglobe.spike-m1.sck "$APP"
echo "bundled: $APP"
# What TCC keys on, without the certificate's subject or team id (they name a person; m1-sck
# logs the same fields).
codesign -dvvv "$APP" 2>&1 | grep -E '^(Identifier=|CDHash=|Signature[= ])' || true
