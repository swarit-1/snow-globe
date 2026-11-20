#!/bin/bash
# Wraps the release m1-sck in a minimal app bundle so macOS attributes Screen Recording to the
# helper itself rather than to the terminal (PLAN.md M1 1a check 11; PRD §18.1: "a harness-only
# signed identity"). The bundle goes to $LOOKOUT_DATA_DIR/cache/spike-m1/, never into the repo.
#
#   scripts/bundle-sck-helper.sh                 # ad-hoc signature (identity changes on rebuild)
#   scripts/bundle-sck-helper.sh "Apple Development: <name> (<team>)"   # a stable identity
#
# Then run it through LaunchServices (no terminal, so pass the answer later with m1-report note):
#   open -W -n --env LOOKOUT_DATA_DIR="$LOOKOUT_DATA_DIR" \
#     --stdout "$LOOKOUT_DATA_DIR/cache/spike-m1/consent.out" \
#     "$LOOKOUT_DATA_DIR/cache/spike-m1/M1SckProbe.app" --args consent --run-id <id> --scenario repeat
#
# This script only copies and signs local files. It does not run the helper.
set -euo pipefail

TARGET_DIR="${CARGO_TARGET_DIR:-$HOME/lookout-data/cache/cargo-target}"
DATA_DIR="${LOOKOUT_DATA_DIR:-$HOME/lookout-data}"
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
  <key>CFBundleIdentifier</key><string>dev.lookout.spike-m1.sck</string>
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

codesign --force --sign "$IDENTITY" --identifier dev.lookout.spike-m1.sck "$APP"
echo "bundled: $APP"
# What TCC keys on, without the certificate's subject or team id (they name a person; m1-sck
# logs the same fields).
codesign -dvvv "$APP" 2>&1 | grep -E '^(Identifier=|CDHash=|Signature[= ])' || true
