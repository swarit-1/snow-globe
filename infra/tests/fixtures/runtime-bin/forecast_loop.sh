#!/bin/bash
# Local stand-in for the forecast Lambda until AWS is live (D-014), run from a frozen snapshot with
# its own venv (resolved once at start). Every 5 minutes it calls the production handler with a
# light profile: 8 fields (the 7 of 19 Nov plus PRES:cloud base, D-026), f00-f02, at most 40 MB per
# run. The worker deletes GRIB subsets after extraction.
set -u
SNAP=$(readlink "$HOME/lookout-data/cache/runtime/current") || exit 1
cd "$SNAP/workers/forecast" || exit 1
unset PYTHONPATH
export LOOKOUT_MANIFEST_DIR=$SNAP/viewpoints/manifests
export LOOKOUT_FORECAST_FIELDS='VIS:surface,HGT:cloud ceiling,HGT:cloud base,PRES:cloud base,LCDC:low cloud layer,MCDC:middle cloud layer,HCDC:high cloud layer,HGT:surface'
export LOOKOUT_FORECAST_MAX_FH=2 LOOKOUT_FORECAST_MAX_MB=40
PY=$SNAP/workers/forecast/.venv/bin/python
while true; do
  echo "tick $(date -u +%FT%TZ) code $SNAP"
  "$PY" -c 'import json; from lookout_forecast.lambda_handler import handler; r=handler({}, None); print(json.dumps({"extracted": r.get("extracted"), "runs": [x for x in r.get("runs", []) if x.get("status") not in ("written",)]}, default=str)[:900])' 2>&1 | tail -4
  sleep 300
done
