#!/usr/bin/env bash
# Every infra check that needs no AWS credentials, network access to AWS, or apply (CLAUDE.md `make test`):
#   1. terraform fmt -check -recursive
#   2. infra/tests/test_static.sh: no email address in the code, alarm_email required and sensitive, default_tags,
#      depends_on hints, and test files that pin every variable to its default (so 3 is hermetic)
#   3. root module and bootstrap: init -backend=false, validate, and `terraform test` (plan-only, mocked provider)
#   4. shellcheck, when installed
#   5. infra/tests/test_scripts.sh: deploy.sh and build.sh against stub aws/terraform/docker
#   6. infra/tests/test_dockerignore.sh: the Lambda build context, checked with BuildKit (SKIP without Docker)
#   7. tflint, when installed (config: infra/terraform/.tflint.hcl)
#
# `terraform init` downloads the AWS provider (~735 MB) from the Terraform registry the first time. It is cached in
# TF_PLUGIN_CACHE_DIR (default $LOOKOUT_DATA_DIR/cache/terraform-plugins), outside the iCloud-synced ~/Documents.
set -euo pipefail

INFRA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF="$INFRA/terraform"
export TF_PLUGIN_CACHE_DIR="${TF_PLUGIN_CACHE_DIR:-${LOOKOUT_DATA_DIR:-$HOME/lookout-data}/cache/terraform-plugins}"
export TF_IN_AUTOMATION=1
export CHECKPOINT_DISABLE=1 # no Terraform version-check call
mkdir -p "$TF_PLUGIN_CACHE_DIR"
LOG="$(mktemp "${TMPDIR:-/tmp}/lookout-infra-test.XXXXXX")"
trap 'rm -f "$LOG"' EXIT

step() { printf '\n== %s\n' "$*"; }

# Terraform reaches its provider over a Unix socket created in TMPDIR, and socket paths are capped at 104 bytes
# on macOS (108 on Linux). With a longer TMPDIR every provider call fails with "Failed to read any lines from
# plugin's stdout", so Terraform alone gets a short temp dir in that case.
tf() {
  if [ "${#TMPDIR}" -gt 60 ]; then
    TMPDIR=/tmp terraform "$@"
  else
    terraform "$@"
  fi
}
TMPDIR="${TMPDIR:-/tmp}"

step "terraform fmt -check -recursive"
tf -chdir="$TF" fmt -check -recursive -diff -no-color
echo "ok"

step "static checks"
"$INFRA/tests/test_static.sh"

for dir in "$TF" "$TF/bootstrap"; do
  name="infra/${dir#"$INFRA"/}"
  step "$name: init -backend=false, validate, test"
  tf -chdir="$dir" init -backend=false -input=false -no-color >"$LOG" 2>&1 || { cat "$LOG"; exit 1; }
  tf -chdir="$dir" validate -no-color
  if tf -chdir="$dir" test -no-color >"$LOG" 2>&1; then
    grep -E '^Success!' "$LOG"
  else
    cat "$LOG"
    exit 1
  fi
done

step "shellcheck"
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck -x -s bash "$INFRA/deploy.sh" "$INFRA/test.sh" "$INFRA/lambda/build.sh" "$INFRA"/tests/*.sh
  echo "ok"
else
  echo "SKIP shellcheck not installed"
fi

step "deploy.sh and build.sh"
"$INFRA/tests/test_scripts.sh"

step "Lambda build context"
"$INFRA/tests/test_dockerignore.sh"

step "tflint"
if command -v tflint >/dev/null 2>&1; then
  (cd "$TF" && tflint --init --config "$TF/.tflint.hcl" >/dev/null && tflint --recursive --config "$TF/.tflint.hcl")
  echo "ok"
else
  echo "SKIP tflint not installed"
fi

printf '\ninfra/test.sh: all checks passed\n'
