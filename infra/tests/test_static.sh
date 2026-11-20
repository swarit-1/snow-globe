#!/usr/bin/env bash
# Static checks on infra/ that `terraform test` cannot make (a mocked provider never sees the provider block, a
# variable's default, or depends_on). No network, no credentials, no Terraform run.
#
#   1. Privacy (CLAUDE.md; the repo is public): no email address anywhere in the Terraform code, the infra scripts
#      (shell, Python, Dockerfiles) or the cost files; in the tests and the README, only addresses at the reserved
#      example.com and example.invalid domains; and variable "alarm_email" stays required (no default), non-nullable
#      and sensitive.
#   2. Tags: both provider blocks keep default_tags, and the root one uses local.tags (project=lookout).
#   3. Ordering: the depends_on hints that avoid first-deploy races are still there.
#   4. Hermetic tests: every test file pins every variable of its module, and each pin equals the variable's
#      default, so `terraform test` checks the shipped defaults whatever TF_VAR_* or *.auto.tfvars say.
#
# Run: infra/tests/test_static.sh [INFRA_DIR]   (bash 3.2 compatible; exits non-zero on the first failure)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA="${1:-$(cd "$HERE/.." && pwd)}"
TF="$INFRA/terraform"

PASSED=0
CURRENT=""
fail() {
  printf '  FAIL %s: %s\n' "$CURRENT" "$*"
  exit 1
}
ok() {
  printf '  ok   %s\n' "$CURRENT"
  PASSED=$((PASSED + 1))
}

# block_of FILE HEADER: the lines of the top-level block that starts with the line HEADER (up to its closing "}").
block_of() {
  awk -v header="$2" '
    $0 == header { inside = 1 }
    inside { print }
    inside && /^}/ { exit }
  ' "$1"
}

# defaults_of FILE: "name<TAB>default" for each variable block; "<none>" when it has no default.
defaults_of() {
  awk '
    /^variable "[^"]+" \{/ {
      match($0, /"[^"]+"/); name = substr($0, RSTART + 1, RLENGTH - 2); def = "<none>"; inside = 1; next
    }
    inside && /^  default[[:space:]]*=/ { sub(/^  default[[:space:]]*=[[:space:]]*/, ""); def = $0; next }
    inside && /^}/ { printf "%s\t%s\n", name, def; inside = 0 }
  ' "$1"
}

# pins_of FILE: "name<TAB>value" from a test file's top-level `variables { ... }` block.
pins_of() {
  awk '
    /^variables \{/ { inside = 1; next }
    inside && /^}/ { exit }
    inside && /^  [a-z_]+[[:space:]]*=/ {
      line = $0; sub(/^  /, "", line)
      name = line; sub(/[[:space:]]*=.*/, "", name)
      value = line; sub(/^[a-z_]+[[:space:]]*=[[:space:]]*/, "", value)
      printf "%s\t%s\n", name, value
    }
  ' "$1"
}

echo "infra/tests/test_static.sh"

# ---- 1. privacy ---------------------------------------------------------------------------------------------------

CURRENT="no_email_address_in_infra_code"
EMAIL_RE='[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}'
files="$(find "$TF" "$INFRA/lambda" "$INFRA/cost" "$INFRA/deploy.sh" "$INFRA/test.sh" "$INFRA/tf_providers.sh" \
  \( -name .terraform -o -name tests -o -name __pycache__ -o -name .ruff_cache \) -prune -o \
  -type f \( -name '*.tf' -o -name '*.hcl' -o -name '*.sh' -o -name '*.py' -o -name '*.json' -o -name '*.toml' \
  -o -name 'Dockerfile*' \) ! -name '.terraform.lock.hcl' -print)"
[ -n "$files" ] || fail "found no files to scan under $INFRA"
# shellcheck disable=SC2086 # one path per line, none with spaces
if hits="$(grep -n -E "$EMAIL_RE" $files)"; then
  fail "email-like text in infra code (put addresses in TF_VAR_alarm_email only):
$hits"
fi
ok

# Tests need an address to pass as alarm_email, and the README shows where yours goes: those use only the reserved
# example.com and example.invalid domains (RFC 2606), which reach nobody.
CURRENT="tests_and_readme_use_only_example_addresses"
files="$(find "$INFRA/tests" "$TF/tests" "$TF/bootstrap/tests" "$INFRA/README.md" -type f -print)"
[ -n "$files" ] || fail "found no test files or README under $INFRA"
# shellcheck disable=SC2086 # one path per line, none with spaces
if hits="$(grep -n -o -E "$EMAIL_RE" $files | grep -v -E '@example\.(com|invalid)$')"; then
  fail "an address outside example.com and example.invalid in the tests or the README:
$hits"
fi
ok

CURRENT="alarm_email_is_required_and_sensitive"
block="$(block_of "$TF/variables.tf" 'variable "alarm_email" {')"
[ -n "$block" ] || fail "variable \"alarm_email\" not found in terraform/variables.tf"
if printf '%s\n' "$block" | grep -Eq '^  default[[:space:]]*='; then
  fail "alarm_email must have no default: an address in the code would be published"
fi
printf '%s\n' "$block" | grep -Eq '^  sensitive[[:space:]]*=[[:space:]]*true$' || fail "alarm_email must keep sensitive = true"
printf '%s\n' "$block" | grep -Eq '^  nullable[[:space:]]*=[[:space:]]*false$' || fail "alarm_email must keep nullable = false"
ok

# ---- 2. tags ------------------------------------------------------------------------------------------------------

CURRENT="providers_tag_every_resource"
block="$(block_of "$TF/versions.tf" 'provider "aws" {')"
printf '%s\n' "$block" | grep -Eq '^  default_tags \{$' || fail "terraform/versions.tf: provider \"aws\" lost its default_tags block"
printf '%s\n' "$block" | grep -Eq '^    tags = local\.tags$' || fail "terraform/versions.tf: default_tags must be local.tags (project=lookout)"
block="$(block_of "$TF/bootstrap/versions.tf" 'provider "aws" {')"
printf '%s\n' "$block" | grep -Eq '^  default_tags \{$' || fail "terraform/bootstrap/versions.tf: provider \"aws\" lost its default_tags block"
printf '%s\n' "$block" | grep -Eq '^      project += var\.project$' || fail "terraform/bootstrap/versions.tf: default_tags must set project"
ok

# ---- 3. ordering --------------------------------------------------------------------------------------------------

CURRENT="depends_on_hints_are_kept"
# file|block header|dependency that must appear in the block's depends_on
for spec in \
  'lambda.tf|resource "aws_lambda_function" "job" {|aws_iam_role_policy.job' \
  'scheduler.tf|resource "aws_scheduler_schedule" "job" {|aws_iam_role_policy.scheduler' \
  'storage.tf|resource "aws_s3_bucket_policy" "data" {|aws_s3_bucket_public_access_block.data' \
  'storage.tf|resource "aws_s3_bucket_lifecycle_configuration" "data" {|aws_s3_bucket_versioning.data' \
  'monitoring.tf|resource "aws_budgets_budget_action" "hard_stop" {|aws_iam_role_policy.budget_action' \
  'bootstrap/main.tf|resource "aws_s3_bucket_policy" "tfstate" {|aws_s3_bucket_public_access_block.tfstate' \
  'bootstrap/main.tf|resource "aws_s3_bucket_lifecycle_configuration" "tfstate" {|aws_s3_bucket_versioning.tfstate'; do
  file="${spec%%|*}"
  rest="${spec#*|}"
  header="${rest%|*}"
  dep="${rest##*|}"
  block="$(block_of "$TF/$file" "$header")"
  [ -n "$block" ] || fail "$file: block '$header' not found"
  printf '%s\n' "$block" | grep -Eq "^  depends_on = \[.*${dep}[],]" || fail "$file: '$header' must keep depends_on = [$dep]"
done
ok

# ---- 4. hermetic tests --------------------------------------------------------------------------------------------

CURRENT="test_files_pin_every_variable_to_its_default"
for module in "$TF" "$TF/bootstrap"; do
  defaults="$(defaults_of "$module/variables.tf")"
  [ -n "$defaults" ] || fail "no variables found in ${module#"$INFRA"/}/variables.tf"
  tests_found=0
  for test_file in "$module"/tests/*.tftest.hcl; do
    [ -f "$test_file" ] || continue
    tests_found=1
    rel="${test_file#"$INFRA"/}"
    pins="$(pins_of "$test_file")"
    while IFS="$(printf '\t')" read -r name def; do
      pin="$(printf '%s\n' "$pins" | awk -F '\t' -v n="$name" '$1 == n { print $2; found = 1 } END { exit !found }')" ||
        fail "$rel: variable \"$name\" is not pinned in the file-level variables block"
      if [ "$def" != "<none>" ] && [ "$def" != "null" ] && [ "$pin" != "$def" ]; then
        fail "$rel: $name = $pin, but its default is $def (pin the default, or change both on purpose)"
      fi
    done <<EOF
$defaults
EOF
    while IFS="$(printf '\t')" read -r name _; do
      [ -n "$name" ] || continue
      printf '%s\n' "$defaults" | awk -F '\t' -v n="$name" '$1 == n { found = 1 } END { exit !found }' ||
        fail "$rel: pins \"$name\", which ${module#"$INFRA"/}/variables.tf does not declare"
    done <<EOF
$pins
EOF
  done
  [ "$tests_found" -eq 1 ] || fail "no test files in ${module#"$INFRA"/}/tests"
done
ok

echo "infra/tests/test_static.sh: $PASSED passed, 0 failed"
