#!/usr/bin/env bash
# Every infra check that needs no AWS credentials, network access to AWS, or apply (CLAUDE.md `make test`):
#   1. terraform fmt -check -recursive
#   2. infra/tests/test_static.sh: no email address in the code, alarm_email required and sensitive, default_tags,
#      depends_on hints, and test files that pin every variable to its default (so 3 is hermetic)
#   3. root module and bootstrap: init -backend=false, validate, and `terraform test` (plan-only, mocked provider)
#   4. infra/tests/test_tf_providers.sh: the offline provider path of step 3, against a fake provider
#   5. shellcheck, when installed
#   6. infra/tests/test_scripts.sh: deploy.sh and build.sh against stub aws/terraform/docker
#   7. infra/tests/test_dockerignore.sh: the Lambda build context, checked with BuildKit (SKIP without Docker)
#   8. infra/tests/test_cost.sh: the hosting estimate's arithmetic (infra/cost/estimate.py) on the committed
#      simulation (seeds pooled), against the figures in infra/README.md; forecast's S3 requests counted by running
#      its code, and a short self-checked capture simulation (each SKIPs without the venv it needs); ruff on
#      infra/cost when ruff is installed
#   9. tflint, when installed (config: infra/terraform/.tflint.hcl)
#  10. infra/tests/test_migrate.sh: infra/ops/migrate-to-snow-globe.sh (the rename's live switch, D-080) against stub
#      loops on a scratch HOME (macOS only; SKIP elsewhere)
#
# Usage: infra/test.sh [--offline | --registry]   (or SNOW_GLOBE_TF_INIT=auto|offline|registry; default auto;
# the deprecated LOOKOUT_TF_INIT is read when it is unset, D-080)
#
# Network: step 3's `terraform init` installs the AWS provider (~760 MB unpacked) into TF_PLUGIN_CACHE_DIR (default
# <data dir>/cache/terraform-plugins, outside the iCloud-synced ~/Documents; the data dir is $SNOW_GLOBE_DATA_DIR, else
# $LOOKOUT_DATA_DIR, else ~/snow-globe-data or, until it is moved, ~/lookout-data). infra/tf_providers.sh decides
# how: from that cache with -plugin-dir and no network once it holds the locked provider for this platform with a
# matching h1: hash, else from registry.terraform.io and releases.hashicorp.com. --offline never contacts the
# registry and fails at once, naming what is missing, if the cache cannot serve; `infra/tf_providers.sh fetch` fills
# it once. Nothing else here uses the network, except `tflint --init` (GitHub), when tflint exists; --offline skips
# it and runs tflint on the rulesets already installed.
set -euo pipefail

INFRA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF="$INFRA/terraform"

while [ $# -gt 0 ]; do
  case "$1" in
    --offline) export SNOW_GLOBE_TF_INIT=offline; shift ;;
    --registry) export SNOW_GLOBE_TF_INIT=registry; shift ;;
    -h | --help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "infra/test.sh: unknown argument: $1 (use --offline or --registry)" >&2; exit 2 ;;
  esac
done

# shellcheck disable=SC1091 # infra/tf_providers.sh is shellchecked on its own below
. "$INFRA/tf_providers.sh"

TF_PLUGIN_CACHE_DIR="$(tfp_cache_dir)" || exit 1 # <data dir>/cache/terraform-plugins unless set (tfp_data_dir)
export TF_PLUGIN_CACHE_DIR
export TF_IN_AUTOMATION=1
export CHECKPOINT_DISABLE=1 # no Terraform version-check call
mkdir -p "$TF_PLUGIN_CACHE_DIR"
LOG="$(mktemp "${TMPDIR:-/tmp}/snow-globe-infra-test.XXXXXX")"
trap 'rm -f "$LOG"' EXIT

step() { printf '\n== %s\n' "$*"; }
tf() { tfp_tf "$@"; } # terraform with a short TMPDIR when needed (infra/tf_providers.sh)

step "terraform fmt -check -recursive"
tf -chdir="$TF" fmt -check -recursive -diff -no-color
echo "ok"

step "static checks"
"$INFRA/tests/test_static.sh"

for dir in "$TF" "$TF/bootstrap"; do
  name="infra/${dir#"$INFRA"/}"
  step "$name: init -backend=false, validate, test"
  : >"$LOG" # a refusal before init writes nothing: show none of the previous module's test log
  tfp_init "$dir" "$LOG" || { cat "$LOG"; exit 1; }
  tf -chdir="$dir" validate -no-color
  if tf -chdir="$dir" test -no-color >"$LOG" 2>&1; then
    grep -E '^Success!' "$LOG"
  else
    cat "$LOG"
    exit 1
  fi
done

step "offline provider path"
"$INFRA/tests/test_tf_providers.sh"

step "shellcheck"
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck -x -s bash "$INFRA/deploy.sh" "$INFRA/test.sh" "$INFRA/tf_providers.sh" "$INFRA/lambda/build.sh" \
    "$INFRA/lambda/smoke.sh" "$INFRA/ops/migrate-to-snow-globe.sh" "$INFRA"/tests/*.sh
  echo "ok"
else
  echo "SKIP shellcheck not installed"
fi

step "deploy.sh and build.sh"
"$INFRA/tests/test_scripts.sh"

step "Lambda build context"
"$INFRA/tests/test_dockerignore.sh"

step "cost estimate"
"$INFRA/tests/test_cost.sh"

step "tflint"
tfp_tflint "$TF"

step "the rename's live switch (infra/ops/migrate-to-snow-globe.sh)"
"$INFRA/tests/test_migrate.sh"

printf '\ninfra/test.sh: all checks passed\n'
