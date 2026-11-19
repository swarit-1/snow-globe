#!/usr/bin/env bash
# Tests for infra/deploy.sh and infra/lambda/build.sh.
#
# Every external command (aws, terraform, docker) is a stub on PATH that logs its arguments and prints canned
# answers, so these tests need no network, no AWS account and no Docker daemon. Each case runs the real scripts
# inside a throwaway copy of the repo layout (a fresh git repo, so image tags are deterministic).
#
# Run: infra/tests/test_scripts.sh   (bash 3.2 compatible; exits non-zero on the first failure)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/lookout-infra-tests.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

PASSED=0
FAILED=0
CURRENT=""

fail() {
  printf '  FAIL %s: %s\n' "$CURRENT" "$*"
  FAILED=$((FAILED + 1))
  exit 1
}
ok() {
  printf '  ok   %s\n' "$CURRENT"
  PASSED=$((PASSED + 1))
}
contains() { # contains "haystack" "needle" "what"
  case "$1" in *"$2"*) ;; *) fail "$3: expected to find '$2'" ;; esac
}
lacks() { # lacks "haystack" "needle" "what"
  case "$1" in *"$2"*) fail "$3: did not expect '$2'" ;; *) ;; esac
}

# ---- fixtures -----------------------------------------------------------------------------------------------------

make_stubs() { # make_stubs DIR
  local bin="$1"
  mkdir -p "$bin"
  cat >"$bin/aws" <<'EOF'
#!/usr/bin/env bash
echo "aws $*" >>"$STUB_LOG"
case "$*" in
  "sts get-caller-identity"*)
    if [ "${STUB_AWS_NO_CREDS:-0}" = 1 ]; then echo "Unable to locate credentials." >&2; exit 253; fi
    printf '123456789012\tarn:aws:iam::123456789012:user/stub\n' ;;
  "configure get region"*)
    if [ -n "${STUB_PROFILE_REGION:-}" ]; then echo "$STUB_PROFILE_REGION"; else exit 1; fi ;;
  "lambda get-account-settings"*) echo "${STUB_LAMBDA_LIMIT:-1000}" ;;
  "s3api head-bucket"*)
    if [ "${STUB_BUCKET_EXISTS:-0}" = 1 ]; then exit 0; fi
    echo "An error occurred (404) when calling the HeadBucket operation: Not Found" >&2; exit 254 ;;
  "ecr describe-repositories"*) echo "123456789012.dkr.ecr.us-east-1.amazonaws.com/lookout/lambda" ;;
  "ecr get-login-password"*) echo "stub-password" ;;
  "ecr describe-images"*)
    if [ "${STUB_IMAGE_EXISTS:-0}" = 1 ]; then echo "2026-11-19T00:00:00+00:00"; exit 0; fi
    echo "An error occurred (ImageNotFoundException) when calling the DescribeImages operation: The image requested does not exist" >&2
    exit 254 ;;
  *) ;;
esac
EOF
  cat >"$bin/terraform" <<'EOF'
#!/usr/bin/env bash
echo "terraform $*" >>"$STUB_LOG"
case "$*" in
  *"output -raw data_bucket"*) echo "lookout-data-123456789012" ;;
  *) ;;
esac
EOF
  cat >"$bin/docker" <<'EOF'
#!/usr/bin/env bash
echo "docker $*" >>"$STUB_LOG"
case "$1" in
  info) [ "${STUB_DOCKER_DOWN:-0}" = 1 ] && exit 1 ;;
  login) cat >/dev/null ;;
esac
exit 0
EOF
  chmod +x "$bin/aws" "$bin/terraform" "$bin/docker"
}

# A copy of the repo layout that deploy.sh and build.sh rely on, as its own git repo.
# Variants: "" (no workers), with-workers (everything the image needs), no-handlers (workers without the two
# Lambda handler modules).
make_repo() { # make_repo DIR [with-workers|no-handlers]
  local dir="$1" variant="${2:-}"
  mkdir -p "$dir/infra/lambda" "$dir/infra/terraform/bootstrap" "$dir/docs"
  cp "$REPO/infra/deploy.sh" "$dir/infra/deploy.sh"
  cp "$REPO/infra/lambda/build.sh" "$REPO/infra/lambda/Dockerfile" "$REPO/infra/lambda/Dockerfile.dockerignore" "$dir/infra/lambda/"
  echo "# docs" >"$dir/docs/notes.md"
  if [ -n "$variant" ]; then
    local w
    for w in common capture forecast; do
      mkdir -p "$dir/workers/$w/src/lookout_$w"
      printf '[project]\nname = "lookout-%s"\n' "$w" >"$dir/workers/$w/pyproject.toml"
      : >"$dir/workers/$w/src/lookout_$w/__init__.py"
    done
    echo "version = 1" >"$dir/workers/capture/uv.lock"
    echo "version = 1" >"$dir/workers/forecast/uv.lock"
    if [ "$variant" = "with-workers" ]; then
      for w in capture forecast; do
        printf 'def handler(event, context):\n    return {}\n' >"$dir/workers/$w/src/lookout_$w/lambda_handler.py"
      done
    fi
    mkdir -p "$dir/viewpoints/manifests" "$dir/viewpoints/schema"
    echo '{"id": "rainier.paradise-mountain"}' >"$dir/viewpoints/manifests/rainier.paradise-mountain.json"
    echo '{}' >"$dir/viewpoints/schema/viewpoint.schema.json"
  fi
  git -C "$dir" init -q
  git -C "$dir" add -A
  git -C "$dir" -c user.name=lookout-test -c user.email=test@example.invalid -c commit.gpgsign=false commit -q -m fixture
}

# new_case NAME [with-workers|no-handlers]: fresh repo, stubs and log; sets CASE_DIR, STUB_LOG, PATH_WITH_STUBS
new_case() {
  CURRENT="$1"
  CASE_DIR="$WORK/$1"
  mkdir -p "$CASE_DIR"
  make_stubs "$CASE_DIR/bin"
  make_repo "$CASE_DIR/repo" "${2:-}"
  STUB_LOG="$CASE_DIR/calls.log"
  : >"$STUB_LOG"
  export STUB_LOG
  PATH_WITH_STUBS="$CASE_DIR/bin:$PATH"
}

# deploy [args...]: run the copied deploy.sh with stubs and none of the caller's region settings; sets OUT and
# CODE. Stdin is passed through. DEPLOY_ENV (space-separated NAME=value words) adds variables for one case.
deploy() {
  set +e
  # shellcheck disable=SC2086 # DEPLOY_ENV is a list of NAME=value words
  OUT="$(cd "$CASE_DIR/repo" && env -u AWS_REGION -u AWS_DEFAULT_REGION -u TF_VAR_region -u AWS_PROFILE \
    -u LOOKOUT_TF_STATE_BUCKET PATH="$PATH_WITH_STUBS" ${DEPLOY_ENV:-} infra/deploy.sh "$@" 2>&1)"
  CODE=$?
  set -e
}
build() {
  set +e
  OUT="$(cd "$CASE_DIR/repo" && env PATH="$PATH_WITH_STUBS" infra/lambda/build.sh "$@" 2>&1)"
  CODE=$?
  set -e
}
calls() { cat "$STUB_LOG"; }
sha12() { git -C "$CASE_DIR/repo" rev-parse --short=12 HEAD; }

echo "infra/tests/test_scripts.sh"

# ---- deploy.sh: refusals ------------------------------------------------------------------------------------------

new_case refuses_without_alarm_email with-workers
unset TF_VAR_alarm_email
deploy --dry-run </dev/null
[ "$CODE" -eq 2 ] || fail "exit code $CODE, want 2"
contains "$OUT" "TF_VAR_alarm_email is not set" "message"
contains "$OUT" "nothing was changed" "message"
[ ! -s "$STUB_LOG" ] || fail "no external command may run before the local checks pass; ran: $(calls)"
ok

new_case refuses_a_malformed_alarm_email with-workers
TF_VAR_alarm_email="not an address" deploy --dry-run </dev/null
[ "$CODE" -eq 2 ] || fail "exit code $CODE, want 2"
contains "$OUT" "does not look like an email address" "message"
[ ! -s "$STUB_LOG" ] || fail "no external command may run; ran: $(calls)"
ok

new_case refuses_without_aws_credentials with-workers
STUB_AWS_NO_CREDS=1 TF_VAR_alarm_email=alerts@example.com deploy --dry-run </dev/null
[ "$CODE" -eq 2 ] || fail "exit code $CODE, want 2"
contains "$OUT" "no working AWS credentials" "message"
log="$(calls)"
contains "$log" "aws sts get-caller-identity" "identity check"
lacks "$log" "terraform" "no terraform after a failed credential check"
lacks "$log" "lambda get-account-settings" "stops at the first AWS failure"
ok

new_case refuses_a_low_lambda_concurrency_quota with-workers
STUB_LAMBDA_LIMIT=10 TF_VAR_alarm_email=alerts@example.com deploy --dry-run </dev/null
[ "$CODE" -eq 2 ] || fail "exit code $CODE, want 2"
contains "$OUT" "concurrency limit in us-east-1 is 10" "message"
contains "$OUT" "Service Quotas" "remedy"
lacks "$(calls)" "terraform" "no terraform"
ok

new_case refuses_when_the_image_cannot_be_built
TF_VAR_alarm_email=alerts@example.com deploy --dry-run </dev/null
[ "$CODE" -eq 2 ] || fail "exit code $CODE, want 2"
contains "$OUT" "cannot build yet" "build.sh explains what is missing"
lacks "$(calls)" "aws " "no AWS call before the build inputs are checked"
ok

new_case refuses_until_both_lambda_handlers_exist no-handlers
TF_VAR_alarm_email=alerts@example.com deploy --dry-run </dev/null
[ "$CODE" -eq 2 ] || fail "exit code $CODE, want 2"
contains "$OUT" "workers/capture/src/lookout_capture/lambda_handler.py" "names the missing capture handler"
contains "$OUT" "workers/forecast/src/lookout_forecast/lambda_handler.py" "names the missing forecast handler"
lacks "$(calls)" "aws " "no AWS call before the build inputs are checked"
ok

new_case refuses_a_malformed_image_tag
TF_VAR_alarm_email=alerts@example.com deploy --dry-run --image-tag 'bad tag' </dev/null
[ "$CODE" -eq 2 ] || fail "exit code $CODE, want 2"
contains "$OUT" "invalid --image-tag" "message"
[ ! -s "$STUB_LOG" ] || fail "no external command may run; ran: $(calls)"
TF_VAR_alarm_email=alerts@example.com deploy --dry-run --image-tag '-rf' </dev/null
[ "$CODE" -eq 2 ] || fail "exit code $CODE, want 2 for a leading dash"
ok

new_case refuses_an_image_tag_that_is_not_in_ecr
TF_VAR_alarm_email=alerts@example.com deploy --image-tag abc123def456 <<<$'y\ny'
[ "$CODE" -eq 2 ] || fail "exit code $CODE, want 2; output: $OUT"
contains "$OUT" "image abc123def456 is not in the ECR repository lookout/lambda in us-east-1" "message"
contains "$OUT" "nothing was changed" "message"
lacks "$(calls)" "terraform" "no terraform"
ok

# ---- deploy.sh: region (a stack outside the region later commands look in is hard to find and manage) ----------

new_case refuses_when_the_cli_profile_points_at_another_region with-workers
DEPLOY_ENV="STUB_PROFILE_REGION=us-west-2" TF_VAR_alarm_email=alerts@example.com deploy <<<$'y\ny'
[ "$CODE" -eq 2 ] || fail "exit code $CODE, want 2; output: $OUT"
contains "$OUT" "the AWS CLI profile says us-west-2, but the stack deploys to us-east-1 (TF_VAR_region is unset)" "message"
contains "$OUT" "export TF_VAR_region=us-west-2" "how to deploy there on purpose"
log="$(calls)"
contains "$log" "aws configure get region" "reads the profile region (local config only)"
lacks "$log" "sts get-caller-identity" "no AWS call before the region is settled"
lacks "$log" "terraform" "no terraform"
ok

new_case refuses_when_aws_region_points_at_another_region with-workers
DEPLOY_ENV="AWS_REGION=us-west-2" TF_VAR_alarm_email=alerts@example.com deploy --dry-run </dev/null
[ "$CODE" -eq 2 ] || fail "exit code $CODE, want 2; output: $OUT"
contains "$OUT" "AWS_REGION says us-west-2" "message"
lacks "$(calls)" "sts get-caller-identity" "no AWS call"
ok

new_case matching_cli_region_is_accepted with-workers
DEPLOY_ENV="AWS_DEFAULT_REGION=us-east-1 STUB_PROFILE_REGION=us-west-2" TF_VAR_alarm_email=alerts@example.com deploy --dry-run </dev/null
[ "$CODE" -eq 0 ] || fail "exit code $CODE, want 0; output: $OUT"
contains "$OUT" "Region        us-east-1" "region"
lacks "$OUT" "WARNING" "no warning in us-east-1"
ok

new_case explicit_tf_var_region_is_used_everywhere with-workers
DEPLOY_ENV="TF_VAR_region=us-west-2 STUB_PROFILE_REGION=us-west-2" TF_VAR_alarm_email=alerts@example.com deploy <<<$'y\nn'
[ "$CODE" -eq 0 ] || fail "exit code $CODE; output: $OUT"
contains "$OUT" "Region        us-west-2" "region from TF_VAR_region"
contains "$OUT" "WARNING: NOAA's HRRR bucket is in us-east-1" "transfer-cost warning"
contains "$OUT" "Keep TF_VAR_region=us-west-2 exported" "reminder for later commands"
log="$(calls)"
contains "$log" "bootstrap apply -var region=us-west-2" "bootstrap in that region"
contains "$log" "lambda get-account-settings --region us-west-2" "quota checked in that region"
applies="$(grep -c 'infra/terraform apply' "$STUB_LOG" || true)"
[ "$applies" -eq 2 ] || fail "want 2 root applies, got $applies"
missing_region="$(grep 'infra/terraform apply' "$STUB_LOG" | grep -vc -- '-var region=us-west-2' || true)"
[ "$missing_region" -eq 0 ] || fail "every root apply must pass -var region=us-west-2; calls: $(calls)"
contains "$(cat "$CASE_DIR/repo/infra/terraform/backend.hcl")" 'region       = "us-west-2"' "state backend region"
contains "$OUT" "terraform -chdir=infra/terraform apply -var region=us-west-2 -var image_tag=$(sha12) -var capture_schedule_enabled=true" "the re-enable hint carries the region"
contains "$OUT" "aws scheduler get-schedule --region us-west-2" "verification commands carry the region"
ok

# ---- deploy.sh: the plan ------------------------------------------------------------------------------------------

new_case dry_run_prints_the_plan_and_changes_nothing with-workers
TF_VAR_alarm_email=alerts@example.com deploy --dry-run </dev/null
[ "$CODE" -eq 0 ] || fail "exit code $CODE, want 0; output: $OUT"
contains "$OUT" "Lookout deploy plan (nothing has changed yet)" "header"
contains "$OUT" "AWS account   123456789012" "account"
contains "$OUT" "Region        us-east-1" "default region"
contains "$OUT" "a***@example.com" "masked email"
lacks "$OUT" "alerts@example.com" "the full alarm address is never printed"
contains "$OUT" "lookout-tfstate-123456789012  (missing: step 1 creates it)" "state bucket"
contains "$OUT" "Image tag     $(sha12)  (step 4 builds and pushes it)" "image tag from git"
contains "$OUT" "-target=aws_budgets_budget.monthly" "phase 1 creates the budget"
contains "$OUT" "Dry run: stopping here." "dry run"
log="$(calls)"
lacks "$log" "terraform" "a dry run runs no terraform"
lacks "$log" "buildx build" "a dry run builds nothing"
lacks "$log" "ecr get-login-password" "a dry run does not log in"
[ ! -e "$CASE_DIR/repo/infra/terraform/backend.hcl" ] || fail "a dry run must not write backend.hcl"
ok

new_case declining_changes_nothing with-workers
TF_VAR_alarm_email=alerts@example.com deploy <<<"n"
[ "$CODE" -eq 0 ] || fail "exit code $CODE, want 0"
contains "$OUT" "Stopped. Nothing was changed." "message"
lacks "$(calls)" "terraform" "declining runs no terraform"
ok

# ---- deploy.sh: full runs against stubs ---------------------------------------------------------------------------

new_case full_run_bootstraps_then_deploys_in_order with-workers
TF_VAR_alarm_email=alerts@example.com deploy <<<$'y\ny'
[ "$CODE" -eq 0 ] || fail "exit code $CODE; output: $OUT"
tag="$(sha12)"
order="$(grep -n -E 'terraform .*(init|apply)|docker (login|buildx build)' "$STUB_LOG" | sed 's/^\([0-9]*\):.*/\1/' | tr '\n' ' ')"
boot_init="$(grep -n 'terraform -chdir=.*/bootstrap init' "$STUB_LOG" | cut -d: -f1)"
boot_apply="$(grep -n 'terraform -chdir=.*/bootstrap apply' "$STUB_LOG" | cut -d: -f1)"
root_init="$(grep -n 'terraform -chdir=.*/infra/terraform init -backend-config=backend.hcl' "$STUB_LOG" | cut -d: -f1)"
phase1="$(grep -n 'terraform -chdir=.*/infra/terraform apply .*-target=aws_budgets_budget.monthly' "$STUB_LOG" | cut -d: -f1)"
login="$(grep -n 'docker login --username AWS --password-stdin 123456789012.dkr.ecr.us-east-1.amazonaws.com' "$STUB_LOG" | cut -d: -f1)"
buildx="$(grep -n 'docker buildx build' "$STUB_LOG" | cut -d: -f1)"
phase2="$(grep -n "terraform -chdir=.*/infra/terraform apply -var region=us-east-1 -var image_tag=$tag\$" "$STUB_LOG" | cut -d: -f1)"
for v in boot_init boot_apply root_init phase1 login buildx phase2; do
  eval "val=\${$v}"
  [ -n "$val" ] || fail "step $v did not run; calls: $(calls)"
done
[ "$boot_init" -lt "$boot_apply" ] && [ "$boot_apply" -lt "$root_init" ] && [ "$root_init" -lt "$phase1" ] &&
  [ "$phase1" -lt "$login" ] && [ "$login" -lt "$buildx" ] && [ "$buildx" -lt "$phase2" ] ||
  fail "steps out of order (lines $order)"
log="$(calls)"
contains "$log" "-target=aws_sns_topic_subscription.alarm_email" "phase 1 subscribes the alarm email"
contains "$log" "-target=aws_ecr_lifecycle_policy.lambda" "phase 1 creates ECR"
contains "$log" "--platform linux/arm64 --provenance=false --sbom=false --file infra/lambda/Dockerfile --tag 123456789012.dkr.ecr.us-east-1.amazonaws.com/lookout/lambda:$tag --push ." "buildx arguments"
lacks "$log" "auto-approve" "terraform must always ask"
lacks "$log" "capture_schedule_enabled=false" "capture stays enabled when the pollers are stopped"
backend="$(cat "$CASE_DIR/repo/infra/terraform/backend.hcl")"
contains "$backend" 'bucket       = "lookout-tfstate-123456789012"' "backend bucket"
contains "$backend" 'key          = "lookout/terraform.tfstate"' "backend key"
contains "$backend" "use_lockfile = true" "S3-native locking"
lacks "$OUT" "alerts@example.com" "the full alarm address is never printed"
contains "$OUT" "aws scheduler get-schedule --region us-east-1 --group-name lookout --name lookout-capture" "verification commands"
contains "$log" "ecr describe-images --region us-east-1 --repository-name lookout/lambda --image-ids imageTag=$tag" "checks ECR for the tag before building"
ok

new_case rerun_on_the_same_commit_reuses_the_pushed_image with-workers
DEPLOY_ENV="STUB_IMAGE_EXISTS=1 STUB_BUCKET_EXISTS=1" TF_VAR_alarm_email=alerts@example.com deploy <<<$'y\ny'
[ "$CODE" -eq 0 ] || fail "exit code $CODE; output: $OUT"
contains "$OUT" "Image tag     $(sha12)  (already in ECR from an earlier run" "the plan says the image is reused"
log="$(calls)"
lacks "$log" "buildx build" "no rebuild: the repository's tags are immutable"
lacks "$log" "docker login" "no login needed"
contains "$log" "-var image_tag=$(sha12)" "deploys the existing tag"
ok

new_case local_poller_still_running_pauses_capture with-workers
TF_VAR_alarm_email=alerts@example.com deploy <<<$'y\nn'
[ "$CODE" -eq 0 ] || fail "exit code $CODE; output: $OUT"
contains "$(calls)" "-var image_tag=$(sha12) -var capture_schedule_enabled=false" "capture schedule created paused"
contains "$OUT" "The capture schedule will be created paused" "message"
ok

new_case existing_state_bucket_skips_bootstrap with-workers
STUB_BUCKET_EXISTS=1 TF_VAR_alarm_email=alerts@example.com deploy <<<$'y\ny'
[ "$CODE" -eq 0 ] || fail "exit code $CODE"
contains "$OUT" "Bootstrap: skipped, the state bucket exists." "plan says so"
lacks "$(calls)" "/bootstrap" "no bootstrap run"
ok

new_case image_tag_flag_deploys_without_building
DEPLOY_ENV="STUB_IMAGE_EXISTS=1" TF_VAR_alarm_email=alerts@example.com deploy --image-tag abc123def456 <<<$'y\ny'
[ "$CODE" -eq 0 ] || fail "exit code $CODE; output: $OUT"
log="$(calls)"
lacks "$log" "docker" "no Docker with --image-tag"
contains "$log" "ecr describe-images --region us-east-1 --repository-name lookout/lambda --image-ids imageTag=abc123def456" "checks the tag exists"
contains "$log" "-var image_tag=abc123def456" "deploys the given tag"
ok

# ---- build.sh -----------------------------------------------------------------------------------------------------

new_case build_refuses_until_the_workers_exist
build
[ "$CODE" -eq 3 ] || fail "exit code $CODE, want 3"
contains "$OUT" "missing: workers/common/pyproject.toml" "lists what is missing"
contains "$OUT" "viewpoints/manifests/*.json" "manifests are required"
[ ! -s "$STUB_LOG" ] || fail "no docker call when inputs are missing"
ok

# Terraform points the functions at these modules; an image without them fails every run (ImportModuleError).
new_case build_refuses_without_the_lambda_handlers no-handlers
build --check
[ "$CODE" -eq 3 ] || fail "exit code $CODE, want 3; output: $OUT"
contains "$OUT" "workers/capture/src/lookout_capture/lambda_handler.py" "names the capture handler"
contains "$OUT" "workers/forecast/src/lookout_forecast/lambda_handler.py" "names the forecast handler"
lacks "$OUT" "build inputs and Docker are ready" "must not report ready"
[ ! -s "$STUB_LOG" ] || fail "no docker call when a handler is missing"
printf 'def handler(event, context):\n    return {}\n' >"$CASE_DIR/repo/workers/capture/src/lookout_capture/lambda_handler.py"
build --check
[ "$CODE" -eq 3 ] || fail "with only the capture handler: exit code $CODE, want 3"
contains "$OUT" "workers/forecast/src/lookout_forecast/lambda_handler.py" "still names the forecast handler"
ok

new_case build_tag_is_the_commit_unless_image_inputs_changed with-workers
build --print-tag
[ "$CODE" -eq 0 ] || fail "exit code $CODE"
[ "$OUT" = "image_tag=$(sha12)" ] || fail "clean tree: got '$OUT'"
echo "more" >>"$CASE_DIR/repo/docs/notes.md"
build --print-tag
[ "$OUT" = "image_tag=$(sha12)" ] || fail "a docs change must not make the tag dirty: got '$OUT'"
echo "# change" >>"$CASE_DIR/repo/workers/capture/pyproject.toml"
build --print-tag
case "$OUT" in
  "image_tag=$(sha12)-dirty-"[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z) ;;
  *) fail "a worker change must give a dirty, timestamped tag: got '$OUT'" ;;
esac
ok

new_case build_runs_buildx_for_arm64_without_provenance with-workers
build
[ "$CODE" -eq 0 ] || fail "exit code $CODE; output: $OUT"
contains "$(calls)" "docker buildx build --platform linux/arm64 --provenance=false --sbom=false --file infra/lambda/Dockerfile --tag lookout-lambda:$(sha12) --load ." "local build"
last="$(printf '%s\n' "$OUT" | tail -n 1)"
[ "$last" = "image_tag=$(sha12)" ] || fail "last line must be image_tag=<tag>, got '$last'"
ok

new_case build_push_uses_the_repository_url with-workers
build --push 123456789012.dkr.ecr.us-east-1.amazonaws.com/lookout/lambda --tag v1
[ "$CODE" -eq 0 ] || fail "exit code $CODE"
contains "$(calls)" "--tag 123456789012.dkr.ecr.us-east-1.amazonaws.com/lookout/lambda:v1 --push ." "push target"
ok

new_case build_rejects_an_unsafe_tag with-workers
build --tag "bad tag"
[ "$CODE" -eq 2 ] || fail "exit code $CODE, want 2"
build --tag "-leading-dash"
[ "$CODE" -eq 2 ] || fail "exit code $CODE, want 2 for a leading dash"
ok

new_case build_reports_a_stopped_docker_daemon with-workers
set +e
OUT="$(cd "$CASE_DIR/repo" && env PATH="$PATH_WITH_STUBS" STUB_DOCKER_DOWN=1 infra/lambda/build.sh 2>&1)"
CODE=$?
set -e
[ "$CODE" -eq 4 ] || fail "exit code $CODE, want 4"
contains "$OUT" "Docker daemon is not running" "message"
lacks "$(calls)" "buildx build" "no build attempt"
ok

echo "infra/tests/test_scripts.sh: $PASSED passed, $FAILED failed"
