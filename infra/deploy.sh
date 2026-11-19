#!/usr/bin/env bash
# Deploy Lookout's serverless stack to AWS (PRD §11.3). For the owner only: it creates resources that cost money.
#
# Usage:
#   infra/deploy.sh                  check, print the plan, ask, then deploy
#   infra/deploy.sh --dry-run        check and print the plan; change nothing
#   infra/deploy.sh --image-tag TAG  deploy an image that is already in ECR (no build)
#
# It refuses to run without TF_VAR_alarm_email and working AWS credentials. Before it changes anything it prints
# every step and asks; Terraform then shows each plan and waits for "yes" again.
#
# Steps:
#   1. Bootstrap the Terraform state bucket if it does not exist yet (infra/terraform/bootstrap, local state).
#   2. terraform init with the S3 backend (S3-native lock file, no DynamoDB).
#   3. Phase 1: the budget, the alarm topic and its email subscription, and the ECR repository. No image needed,
#      so the budget exists before anything that can spend money (PRD Appendix B).
#   4. Build the arm64 image and push it to ECR (infra/lambda/build.sh), unless ECR already has this tag (a re-run
#      on the same commit: the repository's tags are immutable, so the push would be refused).
#   5. Phase 2: everything else (data bucket, IAM, log groups, the two functions, schedules, alarms, hard stop).
#   6. Print the commands that verify the schedules.
#
# Region: TF_VAR_region, or us-east-1 (next to NOAA's HRRR bucket) when it is unset. Every terraform and aws call
# here uses it. When TF_VAR_region is unset and the AWS CLI points elsewhere (AWS_REGION, AWS_DEFAULT_REGION or
# the profile's region), it refuses: later commands typed by hand would then look in the wrong region. To deploy
# outside us-east-1 on purpose, export TF_VAR_region and keep it exported for every later terraform command.
#
# Environment: TF_VAR_alarm_email (required); AWS credentials from `aws configure sso` / AWS_PROFILE, or keys in
# the environment; TF_VAR_region (optional, see above); LOOKOUT_TF_STATE_BUCKET to override the state bucket.
#
# Exit codes: 0 ok or declined, 1 a step failed, 2 a precondition failed (nothing was changed).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="$ROOT/infra/terraform"
BOOT_DIR="$TF_DIR/bootstrap"
BUILD="$ROOT/infra/lambda/build.sh"
PROJECT="${TF_VAR_project:-lookout}"
STATE_KEY="$PROJECT/terraform.tfstate"
DEFAULT_REGION="us-east-1"
MIN_LAMBDA_CONCURRENCY=102 # two functions reserve 1 each, and Lambda keeps 100 unreserved

say() { printf '%s\n' "$*"; }
err() { printf 'deploy.sh: %s\n' "$*" >&2; }
refuse() {
  err "$*"
  err "nothing was changed."
  exit 2
}
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"; }

ask() { # ask "question" -> 0 for yes
  local answer=""
  printf '%s [y/N] ' "$1"
  if ! read -r answer; then answer=""; fi
  case "$answer" in y | Y | yes | YES) return 0 ;; *) return 1 ;; esac
}

run() { # print, then run
  say "+ $*"
  "$@"
}

valid_tag() { # the rules infra/lambda/build.sh applies to a tag (ECR allows at most 128 characters)
  case "$1" in
    *[!A-Za-z0-9._-]* | "" | [.-]*) return 1 ;;
  esac
  [ "${#1}" -le 128 ]
}

dry_run=0
image_tag=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry_run=1; shift ;;
    --image-tag)
      [ $# -ge 2 ] || refuse "--image-tag needs a value"
      valid_tag "$2" || refuse "invalid --image-tag '$2': use up to 128 letters, digits, '.', '_' or '-', not starting with '.' or '-'"
      image_tag="$2"; shift 2 ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; refuse "unknown argument: $1" ;;
  esac
done

# ---- 0a. local checks: no AWS call happens until all of these pass -------------------------------------------
problems=""
add_problem() { problems="$problems
  - $*"; }

for tool in aws terraform git; do
  command -v "$tool" >/dev/null 2>&1 || add_problem "$tool is not installed (see infra/README.md)"
done
if [ -z "$image_tag" ]; then
  command -v docker >/dev/null 2>&1 || add_problem "docker is not installed (needed to build the image)"
fi

alarm_email="${TF_VAR_alarm_email:-}"
if [ -z "$alarm_email" ]; then
  add_problem "TF_VAR_alarm_email is not set: export TF_VAR_alarm_email=<address for alarms and budget emails>"
else
  case "$alarm_email" in
    *[[:space:]]* | *@*@* | @* | *@) add_problem "TF_VAR_alarm_email does not look like an email address" ;;
    *@*.*) ;;
    *) add_problem "TF_VAR_alarm_email does not look like an email address" ;;
  esac
fi

if [ -n "$problems" ]; then
  refuse "cannot deploy:$problems"
fi

# The image inputs must exist before phase 1, or the deploy would stop half-way.
if [ -z "$image_tag" ]; then
  if ! "$BUILD" --check >/dev/null; then
    refuse "the Lambda image cannot be built yet (see the build.sh message above), or pass --image-tag for an image already in ECR."
  fi
fi

# Region: TF_VAR_region or us-east-1, never silently whatever the CLI happens to point at. `aws configure get`
# only reads the local config; it makes no AWS call.
if [ -n "${TF_VAR_region:-}" ]; then
  region="$TF_VAR_region"
else
  region="$DEFAULT_REGION"
  cli_region="" cli_source=""
  if [ -n "${AWS_REGION:-}" ]; then
    cli_region="$AWS_REGION" cli_source="AWS_REGION"
  elif [ -n "${AWS_DEFAULT_REGION:-}" ]; then
    cli_region="$AWS_DEFAULT_REGION" cli_source="AWS_DEFAULT_REGION"
  else
    cli_region="$(aws configure get region 2>/dev/null || true)" cli_source="the AWS CLI profile"
  fi
  if [ -n "$cli_region" ] && [ "$cli_region" != "$region" ]; then
    refuse "$cli_source says $cli_region, but the stack deploys to $region (TF_VAR_region is unset). Lookout runs in $DEFAULT_REGION, next to NOAA's HRRR bucket: export AWS_REGION=$DEFAULT_REGION. To deploy to $cli_region on purpose, export TF_VAR_region=$cli_region and keep it exported for every later terraform command."
  fi
fi
case "$region" in
  *[!a-z0-9-]*) refuse "'$region' does not look like an AWS region (TF_VAR_region)." ;;
  [a-z][a-z]-[a-z]*-[0-9]) ;;
  *) refuse "'$region' does not look like an AWS region (TF_VAR_region)." ;;
esac
# Every aws call below uses this region, and every terraform call gets it as a variable as well as -var.
export AWS_REGION="$region" TF_VAR_region="$region"

# ---- 0b. read-only AWS checks -----------------------------------------------------------------------------------
if ! identity="$(aws sts get-caller-identity --query '[Account,Arn]' --output text 2>/dev/null)"; then
  refuse "no working AWS credentials: run 'aws configure sso' (or export AWS_PROFILE / keys), then 'aws sts get-caller-identity'."
fi
account_id="$(printf '%s\n' "$identity" | awk '{print $1}')"
caller_arn="$(printf '%s\n' "$identity" | awk '{print $2}')"
case "$account_id" in
  [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) ;;
  *) refuse "could not read the AWS account id from 'aws sts get-caller-identity' (got '$account_id')." ;;
esac

if ! lambda_limit="$(aws lambda get-account-settings --region "$region" --query 'AccountLimit.ConcurrentExecutions' --output text 2>/dev/null)"; then
  refuse "could not read the Lambda account settings in $region (aws lambda get-account-settings)."
fi
case "$lambda_limit" in
  '' | *[!0-9]*) refuse "unexpected Lambda concurrency limit '$lambda_limit'." ;;
esac
if [ "$lambda_limit" -lt "$MIN_LAMBDA_CONCURRENCY" ]; then
  refuse "this account's Lambda concurrency limit in $region is $lambda_limit; reserving 1 per function needs at least $MIN_LAMBDA_CONCURRENCY (Lambda keeps 100 unreserved). New accounts start low: request 'Concurrent executions' = 1000 in Service Quotas > AWS Lambda, then re-run."
fi

state_bucket="${LOOKOUT_TF_STATE_BUCKET:-$PROJECT-tfstate-$account_id}"
if head_err="$(aws s3api head-bucket --bucket "$state_bucket" 2>&1 >/dev/null)"; then
  bucket_state="exists"
else
  case "$head_err" in
    *404* | *"Not Found"* | *NoSuchBucket*) bucket_state="missing" ;;
    *403* | *Forbidden*) refuse "state bucket $state_bucket exists but this account cannot access it (owned by another account?). Set LOOKOUT_TF_STATE_BUCKET." ;;
    *) refuse "could not check state bucket $state_bucket: $head_err" ;;
  esac
fi

# image_in_ecr TAG: 0 if ECR holds TAG, 1 if it does not (or the repository does not exist yet); refuses on any
# other error. Sets pushed_at.
pushed_at=""
image_in_ecr() {
  local out
  if out="$(aws ecr describe-images --region "$region" --repository-name "$PROJECT/lambda" --image-ids "imageTag=$1" --query 'imageDetails[0].imagePushedAt' --output text 2>&1)"; then
    pushed_at="$out"
    return 0
  fi
  case "$out" in
    *ImageNotFoundException* | *RepositoryNotFoundException*) return 1 ;;
    *) refuse "could not check ECR for image $1: $out" ;;
  esac
}

reuse_note=""
if [ -z "$image_tag" ]; then
  image_tag="$("$BUILD" --print-tag | sed -n 's/^image_tag=//p' | tail -n 1)"
  [ -n "$image_tag" ] || refuse "could not derive the image tag (infra/lambda/build.sh --print-tag)."
  if image_in_ecr "$image_tag"; then
    build_new=0 # same commit as an earlier run: ECR refuses to push an existing tag, so reuse it
    reuse_note="already in ECR from an earlier run, pushed $pushed_at: no build"
  else
    build_new=1
  fi
else
  build_new=0
  image_in_ecr "$image_tag" ||
    refuse "image $image_tag is not in the ECR repository $PROJECT/lambda in $region: build and push it first (infra/lambda/build.sh --push), or run deploy.sh without --image-tag."
  reuse_note="already in ECR, pushed $pushed_at: no build"
fi

masked_email="$(printf '%s' "${alarm_email%@*}" | cut -c1)***@${alarm_email#*@}"
phase1_targets="-target=aws_budgets_budget.monthly -target=aws_sns_topic_subscription.alarm_email -target=aws_sns_topic_policy.alarms -target=aws_ecr_lifecycle_policy.lambda -target=aws_ecr_repository_policy.lambda"

# ---- 0c. the plan, printed before anything changes --------------------------------------------------------------
say ""
say "Lookout deploy plan (nothing has changed yet)"
say ""
say "  AWS account   $account_id  ($caller_arn)"
say "  Region        $region"
if [ "$region" != "$DEFAULT_REGION" ]; then
  say "                WARNING: NOAA's HRRR bucket is in $DEFAULT_REGION; reading it from $region costs transfer fees."
  say "                Keep TF_VAR_region=$region exported for every later terraform command."
fi
say "  Alarm email   $masked_email  (TF_VAR_alarm_email)"
if [ "$bucket_state" = "exists" ]; then
  say "  State bucket  $state_bucket  (exists)"
else
  say "  State bucket  $state_bucket  (missing: step 1 creates it)"
fi
if [ "$build_new" -eq 1 ]; then
  say "  Image tag     $image_tag  (step 4 builds and pushes it)"
else
  say "  Image tag     $image_tag  ($reuse_note)"
fi
say "  Lambda quota  $lambda_limit concurrent executions (needs >= $MIN_LAMBDA_CONCURRENCY)"
say ""
if [ "$bucket_state" = "exists" ]; then
  say "  1. Bootstrap: skipped, the state bucket exists."
else
  say "  1. terraform -chdir=infra/terraform/bootstrap init, then apply -var region=$region"
fi
say "  2. Write infra/terraform/backend.hcl, then terraform -chdir=infra/terraform init -backend-config=backend.hcl"
say "  3. terraform -chdir=infra/terraform apply -var region=$region $phase1_targets"
say "     Then confirm the 'AWS Notification - Subscription Confirmation' email; alarms arrive only after that."
if [ "$build_new" -eq 1 ]; then
  say "  4. aws ecr get-login-password | docker login; infra/lambda/build.sh --push <ECR repository> --tag $image_tag"
else
  say "  4. Use $image_tag from ECR (no build)."
fi
say "  5. terraform -chdir=infra/terraform apply -var region=$region -var image_tag=$image_tag"
say "     Before it, you are asked whether every local NPS fetcher is stopped (the Rainier stopgap poller, any local"
say "     capture loop). If not, the capture schedule is created paused: two NPS fetchers must never run at once."
say "  6. Print the commands that verify the schedules."
say ""
say "Terraform shows each plan and waits for 'yes' before it changes anything."
say ""

if [ "$dry_run" -eq 1 ]; then
  say "Dry run: stopping here."
  exit 0
fi
if ! ask "Proceed?"; then
  say "Stopped. Nothing was changed."
  exit 0
fi

# ---- 1. bootstrap -------------------------------------------------------------------------------------------------
if [ "$bucket_state" = "missing" ]; then
  run terraform -chdir="$BOOT_DIR" init -input=false
  run terraform -chdir="$BOOT_DIR" apply -var "region=$region" -var "state_bucket_name=$state_bucket" -var "state_key=$STATE_KEY"
fi

# ---- 2. init with the S3 backend ----------------------------------------------------------------------------------
cat >"$TF_DIR/backend.hcl" <<EOF
bucket       = "$state_bucket"
key          = "$STATE_KEY"
region       = "$region"
encrypt      = true
use_lockfile = true
EOF
say "wrote infra/terraform/backend.hcl (gitignored)"
run terraform -chdir="$TF_DIR" init -backend-config=backend.hcl

# ---- 3. phase 1: budget, alarm email, ECR -------------------------------------------------------------------------
# shellcheck disable=SC2086 # phase1_targets is a list of separate -target flags
run terraform -chdir="$TF_DIR" apply -var "region=$region" $phase1_targets
say ""
say "Check the inbox of $masked_email and click the SNS confirmation link now (alarms need it; budget emails don't)."

# ---- 4. image -----------------------------------------------------------------------------------------------------
# Read back from ECR (read-only) rather than from Terraform outputs, which a -target apply may not refresh.
if ! repo_url="$(aws ecr describe-repositories --region "$region" --repository-names "$PROJECT/lambda" --query 'repositories[0].repositoryUri' --output text)"; then
  err "phase 1 did not leave an ECR repository $PROJECT/lambda; stopping before the build."
  exit 1
fi
if [ "$build_new" -eq 1 ]; then
  registry="${repo_url%%/*}"
  say "+ aws ecr get-login-password --region $region | docker login --username AWS --password-stdin $registry"
  aws ecr get-login-password --region "$region" | docker login --username AWS --password-stdin "$registry"
  run "$BUILD" --push "$repo_url" --tag "$image_tag"
else
  say "Using $image_tag from $repo_url ($reuse_note)."
fi

# ---- 5. phase 2: everything else ------------------------------------------------------------------------------------
capture_flag=""
if ! ask "Is every local NPS fetcher stopped (the Rainier stopgap poller, any local capture loop)?"; then
  capture_flag="-var capture_schedule_enabled=false"
  say "The capture schedule will be created paused. Enable it later with:"
  say "  terraform -chdir=infra/terraform apply -var region=$region -var image_tag=$image_tag -var capture_schedule_enabled=true"
fi
# shellcheck disable=SC2086 # capture_flag is empty or one -var flag
run terraform -chdir="$TF_DIR" apply -var "region=$region" -var "image_tag=$image_tag" $capture_flag

# ---- 6. verification commands ------------------------------------------------------------------------------------
data_bucket="$(terraform -chdir="$TF_DIR" output -raw data_bucket)"
say ""
say "Deployed image $image_tag in $region. Verify:"
say "  aws scheduler get-schedule --region $region --group-name $PROJECT --name $PROJECT-capture --query '{state:State,expr:ScheduleExpression}'"
say "  aws scheduler get-schedule --region $region --group-name $PROJECT --name $PROJECT-forecast --query '{state:State,expr:ScheduleExpression}'"
say "  aws logs tail /aws/lambda/$PROJECT-capture --region $region --since 15m"
say "  aws logs tail /aws/lambda/$PROJECT-forecast --region $region --since 15m"
say "  aws cloudwatch describe-alarms --region $region --alarm-name-prefix $PROJECT- --query 'MetricAlarms[].[AlarmName,StateValue]' --output table"
say "  aws s3 ls s3://$data_bucket/ --region $region --recursive --summarize | tail -n 3"
say "Later terraform commands: pass -var region=$region (or keep TF_VAR_region=$region exported) and -var image_tag=$image_tag."
