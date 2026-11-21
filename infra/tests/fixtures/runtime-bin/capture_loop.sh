#!/bin/bash
# Production live capture for all 8 cameras (lookout-capture loop), run from a frozen snapshot of
# committed code with its own venv under ~/lookout-data/cache/runtime/<sha> (outside iCloud, so the
# venv's .pth files are not hidden and no PYTHONPATH is needed; repo venvs are never used).
# The snapshot path is resolved once at start, so flipping `current` never changes a running loop.
# --archive-queue: queued archive backfill runs only in the loop's idle time, inside the same
# host-wide NPS lock (D-023, D-029); the queue starts empty. caffeinate -s keeps the Mac awake only
# while on AC power.
set -u
SNAP=$(readlink "$HOME/lookout-data/cache/runtime/current") || exit 1
cd "$SNAP/workers/capture" || exit 1
unset PYTHONPATH
export LOOKOUT_MANIFEST_DIR=$SNAP/viewpoints/manifests
echo "starting lookout-capture loop at $(date -u +%H:%M:%S) UTC from $SNAP"
exec caffeinate -s "$SNAP/workers/capture/.venv/bin/lookout-capture" loop --interval 300 \
  --archive-queue "$HOME/lookout-data/state/archive/queue.json"
