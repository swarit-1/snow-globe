# Lookout infrastructure

AWS, serverless only (PRD §11.3, §17). Two scheduled Lambda jobs run from one arm64 container image. They write to one private S3 bucket. Alarms and a budget email you.

Nothing here has been applied. Only the owner applies it: `infra/deploy.sh` or the manual steps below. Agents never apply.

Carried over from Oculus's Terraform: the state bootstrap, the S3 and ECR patterns, the SNS alarm topic and the AWS Budgets alarm. **Not** carried over: the ALB, ECS/Fargate, ElastiCache and NAT gateway, which Oculus's own PRD puts at about $60 a month.

| Path | What |
|---|---|
| `terraform/bootstrap/` | The S3 bucket for Terraform state (versioned, encrypted, private). It keeps its own state locally. |
| `terraform/` | Everything else. State is in S3 with S3-native locking (`use_lockfile`, no DynamoDB). |
| `lambda/Dockerfile`, `lambda/Dockerfile.dockerignore` | The image. The build context is the repo root, but only `workers/{common,capture,forecast}` and `viewpoints/{manifests,schema}` get in. |
| `lambda/build.sh` | `docker buildx build --platform linux/arm64 --provenance=false` (Lambda rejects provenance attestations). |
| `deploy.sh` | The owner's one-command deploy. It checks, prints the plan and asks before changing anything. |
| `test.sh` | Every check that needs no AWS credentials. |

## What gets created

| Resource | Name | Settings |
|---|---|---|
| State bucket (bootstrap) | `lookout-tfstate-<account>` | Versioned, SSE-S3, all public access blocked, ACLs off, TLS only; old versions expire after 90 days |
| Data bucket | `lookout-data-<account>` | Private, SSE-S3, all public access blocked, ACLs off, TLS only. **Versioned**: an overwritten or deleted object's previous version is kept 30 days (the daily logs' for 1 day, `cache/`'s for 1 day). `cache/` expires after 7 days; nothing else current expires |
| ECR repository | `lookout/lambda` | Scan on push, immutable tags, keeps the last 5 images; only `lookout-*` functions may pull |
| Lambda `lookout-capture` | handler `lookout_capture.lambda_handler.handler` | arm64, 512 MB, 120 s, reserved concurrency 1 |
| Lambda `lookout-forecast` | handler `lookout_forecast.lambda_handler.handler` | arm64, 2048 MB, 600 s, 2 GB `/tmp`, reserved concurrency 1 |
| Function environment | both | `LOOKOUT_STORAGE=s3`, `LOOKOUT_S3_BUCKET`, `LOOKOUT_MANIFEST_DIR=/var/task/viewpoints/manifests`, `LOOKOUT_USER_AGENT`, `LOOKOUT_DATA_DIR=/tmp/lookout-data` |
| IAM role per function | `lookout-<job>-lambda` | Writes to its own log group only. Reads and writes only its own bucket keys (below). |
| Log groups | `/aws/lambda/lookout-<job>` | 30-day retention (PRD §15.2) |
| Schedules | group `lookout` | capture and forecast both `rate(5 minutes)` (PRD §11.2); no flexible window, no retries |
| Scheduler role | `lookout-scheduler` | `lambda:InvokeFunction` on the two functions only |
| Alarms | `lookout-<job>-errors`, `lookout-<job>-not-running` | Errors in 2 of 3 windows; no invocation in 2 windows in a row |
| SNS topic | `lookout-alarms` | Email subscription to `alarm_email` |
| Budget | `lookout-monthly` | 3 USD a month, PRD §17's hosting hard cap. Emails at 33, 67 and 100 % of actual spend (about $1, $2, $3) and when the month is forecast to pass $3; credits excluded |
| Hard stop | budget action, policy `lookout-budget-hard-stop`, role `lookout-budget-action` | At 100 % of actual spend, AWS Budgets attaches a deny-invoke policy to `lookout-scheduler`: no job runs until next month (see [Hard stop](#hard-stop-at-the-cap)) |

Every resource is tagged `project=lookout`.

**Bucket access by job.** Each job owns its outputs, its state documents and its locks, so neither can overwrite or delete the other's state (the capture job's NPS rate-limit history is out of the forecast job's reach). Releasing a lock is the only delete a worker makes; no worker may delete a frame, forecast or log, and none may delete an object version. Overwrites are recoverable from the previous version (versioning, above).

| Job | Read and write | Delete |
|---|---|---|
| capture | `frames/`, `capture_log/`, `state/capture/`, `state/locks/capture…` | `state/locks/capture…` |
| forecast | `forecast/`, `forecast_log/`, `cache/`, `state/forecast/`, `state/locks/forecast…` | `cache/`, `state/locks/forecast…` |

A worker's state keys must start with `state/<job>/` and its lock names with `<job>` (`lookout_common` stores lock `<name>` at `state/locks/<name>.lock`). Today the capture worker uses `state/capture/…` and locks `capture/<viewpoint_id>` and `capture/hosts`; the forecast worker uses `state/forecast/hrrr/…` and lock `forecast-hrrr`. Anything else gets AccessDenied on Lambda.

Neither job is granted `archive/`. The NPS archive backfill runs on demand (PRD §11.2) and is not a scheduled Lambda job. If it ever moves to Lambda, give that job `archive/*`, `state/archive/*` and `state/locks/archive*` in `local.jobs` in `terraform/main.tf`.

## Owner steps

### 0. Before you start

- **Use an AWS account on the Paid plan.** It still receives the sign-up credits. An account on the new Free plan closes "6 months after you open it or when your credits run out, whichever comes first" ([AWS Free Tier](https://aws.amazon.com/free/)). That falls before the end of this project's data retention (PRD §15.2).
- **Lambda concurrency quota.** Each function reserves 1, and Lambda keeps 100 unreserved, so the account needs a limit of at least 102 ([AWS docs](https://docs.aws.amazon.com/lambda/latest/dg/configuration-concurrency.html)). New accounts start lower ([quotas](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html)). Check it after step 2:
  `aws lambda get-account-settings --query AccountLimit.ConcurrentExecutions`.
  If it is under 102, request "Concurrent executions" = 1000 in Service Quotas → AWS Lambda. `deploy.sh` refuses to run until it is.
- **Only one NPS fetcher.** Stop the Rainier stopgap poller, and any local capture loop, before the capture schedule is enabled. Two NPS fetchers must never run at once.

### 1. Install the AWS CLI

```bash
brew install awscli
aws --version
terraform version   # 1.11 or newer (this machine has 1.16)
```

### 2. Sign in

IAM Identity Center (SSO) is recommended:

```bash
aws configure sso          # pick the account and an admin permission set; note the profile name
export AWS_PROFILE=<profile>
aws sso login
```

Or put access keys in the environment only, never in a file inside the repo:

```bash
export AWS_ACCESS_KEY_ID=...  AWS_SECRET_ACCESS_KEY=...  # plus AWS_SESSION_TOKEN for temporary keys
```

Then check: `aws sts get-caller-identity`.

### 3. Region

```bash
export AWS_REGION=us-east-1
```

Keep us-east-1. NOAA's HRRR bucket is there, so the forecast reads cost no transfer.

The stack's region is `TF_VAR_region`, or us-east-1 when that is unset. `deploy.sh` refuses to run if `TF_VAR_region` is unset and the AWS CLI points at another region (through `AWS_REGION`, `AWS_DEFAULT_REGION` or the profile), because the commands below would then look in the wrong place. To deploy elsewhere on purpose, `export TF_VAR_region=<region>` and keep it exported for every later `terraform` command.

### 4. Alarm email

```bash
export TF_VAR_alarm_email='you@example.com'
```

It has no default, and the code holds no address. Never commit it.

### 5. Keep the provider out of iCloud (recommended)

The AWS provider is about 735 MB, and `~/Documents` syncs to iCloud.

```bash
export TF_PLUGIN_CACHE_DIR="$HOME/lookout-data/cache/terraform-plugins"
mkdir -p "$TF_PLUGIN_CACHE_DIR"
```

### 6a. One command

```bash
infra/deploy.sh --dry-run   # checks and prints the plan; changes nothing
infra/deploy.sh             # the same, then asks; Terraform asks again before each apply
```

It runs steps 6b.1 to 6b.6 below, in order. It refuses to run without `TF_VAR_alarm_email`, working credentials, the concurrency quota, a consistent region (step 3), or the worker packages and both Lambda handler modules it needs to build the image. Before the last apply it asks whether every local NPS fetcher is stopped. If you answer no, the capture schedule is created paused.

Re-running it on the same commit is safe: if ECR already has that image tag (the repository's tags are immutable, so a second push would be refused), it skips the build and deploys the existing image. `--image-tag TAG` deploys an image already in ECR; it refuses, before changing anything, if the tag is not there.

### 6b. The same steps by hand

**6b.1 Bootstrap the state bucket (once per account).**

```bash
terraform -chdir=infra/terraform/bootstrap init
terraform -chdir=infra/terraform/bootstrap apply
terraform -chdir=infra/terraform/bootstrap output -raw backend_hcl > infra/terraform/backend.hcl
```

Keep `infra/terraform/bootstrap/terraform.tfstate`. It is gitignored. If it is ever lost, run
`terraform -chdir=infra/terraform/bootstrap import aws_s3_bucket.tfstate lookout-tfstate-<account>`.

**6b.2 Initialise the main module with the S3 backend.**

```bash
terraform -chdir=infra/terraform init -backend-config=backend.hcl
```

**6b.3 Phase 1: budget, alarm email and ECR.** No image is needed yet, so the budget exists before anything that can spend money (PRD Appendix B).

```bash
P1='-target=aws_budgets_budget.monthly -target=aws_sns_topic_subscription.alarm_email -target=aws_sns_topic_policy.alarms -target=aws_ecr_lifecycle_policy.lambda -target=aws_ecr_repository_policy.lambda'
terraform -chdir=infra/terraform plan  $P1
terraform -chdir=infra/terraform apply $P1
```

Terraform warns that "resource targeting is in effect". That is expected here.

Click the link in the "AWS Notification - Subscription Confirmation" email. Alarms only arrive after that. Budget emails don't need it.

**6b.4 Build and push the image.**

```bash
REPO=$(aws ecr describe-repositories --repository-names lookout/lambda --query 'repositories[0].repositoryUri' --output text)
aws ecr get-login-password | docker login --username AWS --password-stdin "${REPO%%/*}"
TAG=$(infra/lambda/build.sh --push "$REPO" | sed -n 's/^image_tag=//p')
echo "$TAG"
```

The tag is the git commit, plus `-dirty-<time>` when `workers/`, `viewpoints/` or `infra/lambda/` has uncommitted changes.

**6b.5 Stop every local NPS fetcher** (see step 0).

**6b.6 Phase 2: everything else.**

```bash
terraform -chdir=infra/terraform plan  -var image_tag="$TAG"
terraform -chdir=infra/terraform apply -var image_tag="$TAG"
```

### 7. Verify the schedules

```bash
aws scheduler list-schedules --group-name lookout --query 'Schedules[].[Name,State]' --output table
aws scheduler get-schedule --group-name lookout --name lookout-capture --query '{state:State,expr:ScheduleExpression}'
aws logs tail /aws/lambda/lookout-capture --since 15m          # a run every 5 minutes
aws logs tail /aws/lambda/lookout-forecast --since 15m         # a run every 5 minutes
aws cloudwatch describe-alarms --alarm-name-prefix lookout- --query 'MetricAlarms[].[AlarmName,StateValue]' --output table
aws s3 ls "s3://$(terraform -chdir=infra/terraform output -raw data_bucket)/" --recursive --summarize | tail -n 3
```

Expect one ALARM email from each `not-running` alarm right after the first deploy, because the windows before the first run are empty. Each turns OK once a whole window contains a run: within about 15–30 min. After a day, measure what the cost estimate assumed (run durations, and the bucket's request counts and old-version bytes):

```bash
for f in lookout-capture lookout-forecast; do
  aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name Duration \
    --dimensions Name=FunctionName,Value=$f --statistics Average Maximum --period 86400 \
    --start-time "$(date -u -v-1d +%Y-%m-%dT%H:%M:%SZ)" --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
done
```

### 8. Deploy new code

Build and push a new tag (6b.4), then `terraform -chdir=infra/terraform apply -var image_tag="$TAG"`.

Every later apply needs the current tag. Reuse it with `-var image_tag="$(terraform -chdir=infra/terraform output -raw image_tag)"`. Or keep it in `infra/terraform/lookout.auto.tfvars` (gitignored) as `image_tag = "..."`. `infra/test.sh` is unaffected by that file and by any `TF_VAR_*` in your shell: the tests pin every variable.

The commands in steps 8–10 assume us-east-1. If you deployed elsewhere, keep `TF_VAR_region` exported (step 3) and add `--region` to the `aws` commands.

### 9. Pause and resume

**Pause.** Both schedules are disabled and their `not-running` alarms are silenced:

```bash
T="$(terraform -chdir=infra/terraform output -raw image_tag)"
terraform -chdir=infra/terraform apply -var image_tag="$T" -var capture_schedule_enabled=false -var forecast_schedule_enabled=false
```

Repeat the two `-var ...=false` flags on later applies, or put them in `lookout.auto.tfvars`. Otherwise the next apply re-enables the schedules.

**Resume:** apply again without the two flags.

**Emergency stop, without Terraform.** Zero concurrency throttles every run at once:

```bash
aws lambda put-function-concurrency --function-name lookout-capture --reserved-concurrent-executions 0
```

The next `terraform apply` puts it back to 1. While the function is stopped, its `not-running` alarm will email you.

### Hard stop at the cap

PRD §17 caps hosting at $3 a month and says hard caps are enforced in code. When actual spend passes 100 % of `monthly_budget_usd`, AWS Budgets attaches `lookout-budget-hard-stop`, a Deny on `lambda:InvokeFunction` for both functions, to the `lookout-scheduler` role. From then on:

- no scheduled job runs, so capture and forecast data stop;
- you get the budget's 100 % email, the action's own email, then the `not-running` alarms;
- S3 and ECR storage keep costing a few cents a month, and nothing is deleted;
- `terraform apply` does not undo it, because Terraform does not manage the scheduler role's attached policies.

AWS Budgets sees spend a few times a day, so the month can pass $3 by a little before the stop lands. The earlier emails, at about $1 and $2 and when the forecast passes $3, are the warning to act on. AWS needs about five weeks of usage before it forecasts.

**When it ends:** AWS resets IAM-policy actions at the start of each budget period, here the next month ([AWS announcement](https://aws.amazon.com/about-aws/whats-new/2020/10/announcing-aws-budgets-actions/)).

**To resume sooner:** raising the cap changes a PRD §17 decision, so record it in DECISIONS.md first. Then apply with a higher `-var monthly_budget_usd=...` and reverse the action:

```bash
aws budgets execute-budget-action --account-id "$(aws sts get-caller-identity --query Account --output text)" \
  --budget-name lookout-monthly --execution-type REVERSE_BUDGET_ACTION \
  --action-id "$(terraform -chdir=infra/terraform output -raw budget_hard_stop_action_id)"
```

**To keep emails only:** apply with `-var budget_hard_stop=false`.

**Not yet tried against AWS:** the action, its role and its policy have been checked only by the plan-only tests. If creating `aws_budgets_budget_action.hard_stop` fails during phase 2, apply again with `-var budget_hard_stop=false` so the rest deploys, and report the error.

### 10. Destroy

1. Copy the data out. The first 100 GB a month of egress is free ([S3 pricing](https://aws.amazon.com/s3/pricing/)):
   ```bash
   aws s3 sync "s3://$(terraform -chdir=infra/terraform output -raw data_bucket)" ~/lookout-data/s3-export/
   ```
2. Allow deleting the non-empty bucket, then destroy:
   ```bash
   T="$(terraform -chdir=infra/terraform output -raw image_tag)"
   terraform -chdir=infra/terraform apply   -var image_tag="$T" -var data_bucket_force_destroy=true
   terraform -chdir=infra/terraform destroy -var image_tag="$T" -var data_bucket_force_destroy=true
   ```
   This removes the budget and alarms too.
3. Optional, last of all: the state bucket.
   ```bash
   terraform -chdir=infra/terraform/bootstrap apply   -var force_destroy=true
   terraform -chdir=infra/terraform/bootstrap destroy -var force_destroy=true
   ```

## Monthly cost estimate

**These are estimates, not measurements.** Nothing has been deployed, so no duration, request count or byte count below has been observed.

- Prices are for us-east-1, from AWS's public price-list files published 12–18 Nov 2026, and from the pricing pages linked below, read on 19 Nov 2026.
- Run counts follow from the schedules: capture 8,640 runs a month, forecast 8,640 (both every 5 minutes, PRD §11.2), 17,280 in total.
- **S3 request counts come from reading the worker code** in `workers/capture` and `workers/forecast` on 19 Nov (both still in progress):
  - Each camera poll is a lock PUT, a state GET and PUT, and a host-state GET.
  - Each logged record is a GET and a PUT, because S3 has no append and the whole day's log is rewritten. That is 1–2 records per poll plus one per pass.
  - Each stored frame is a JPEG and a sidecar PUT, at most 4 an hour per camera.
  - Each forecast run is a lock PUT and 13 marker GETs, plus 8 files, a marker and a log rewrite per HRRR run.
- Run durations are assumptions.

| Item | What drives it | Estimate per month | Source |
|---|---|---|---|
| EventBridge Scheduler | 17,280 runs. The free tier is 14,000,000 a month, then $1.00 per million. | **$0** | [EventBridge pricing](https://aws.amazon.com/eventbridge/pricing/) |
| Lambda requests | 17,280 at $0.20 per million | **$0.00** (about $0.003) | [Lambda pricing](https://aws.amazon.com/lambda/pricing/) |
| Lambda compute (Arm) | Capture 0.5 GB × 5–20 s × 8,640. Forecast 2 GB: 7,920 runs that only find nothing new is ready (1–5 s, assumed), and 720 that extract an HRRR run (30–150 s, assumed). That is 80,640–381,600 GB-s, at $0.0000133334 per GB-s. | **$0** if the free tier (1 M requests and 400,000 GB-s a month; the AWS page sets no plan condition) applies. Without it, $1.08–5.09. | [Lambda pricing](https://aws.amazon.com/lambda/pricing/) |
| Lambda `/tmp` above 512 MB | Forecast: 1.5 GB extra at $0.0000000309 per GB-s | < $0.01 | [Lambda pricing](https://aws.amazon.com/lambda/pricing/) |
| S3 storage, current objects | ~135 MB a day for 8 cameras (PRD §8.7): about 2 GB in month 1 and ~12 GB by month 3, at $0.023 per GB | **$0.05–0.28** | [S3 pricing](https://aws.amazon.com/s3/pricing/) |
| S3 storage, previous versions | Versioning. Every logged record rewrites the day's log, so 2,600–4,900 records a day at 250–400 bytes (250 is the mean of the stopgap's local log, 19 Nov) leave 0.8–4.8 GB of old log versions a day. They are kept 1–2 days (`log_noncurrent_version_days` = 1). State and other overwrites add 0.1–0.4 GB at 30 days. At a 30-day log setting this line would be $0.58–3.31. | **$0.02–0.23** | [S3 pricing](https://aws.amazon.com/s3/pricing/) |
| S3 requests | 278k–347k PUT at $0.005 per 1,000, and 329k–398k GET at $0.0004 per 1,000 (counted from the code, as above). DELETE and lifecycle expirations are free. | **$1.52–1.89** | [S3 pricing](https://aws.amazon.com/s3/pricing/) |
| S3 ↔ Lambda, NOAA → Lambda transfer | Same region | **$0** | [S3 pricing](https://aws.amazon.com/s3/pricing/) |
| ECR | Up to 5 images, 0.5–4 GB stored (image size not yet measured), at $0.10 per GB. New customers get 500 MB a month free for 12 months. ECR → Lambda in the same region is free. | **$0–0.40** | [ECR pricing](https://aws.amazon.com/ecr/pricing/) |
| CloudWatch Logs | 17–86 MB a month (1–5 KB per run, assumed). Ingestion is $0.50 per GB, storage $0.03 per GB, 30-day retention. Free: 5 GB a month. | **$0** (≤ $0.05 without the free tier) | [CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/) |
| CloudWatch alarms | 4 standard alarms at $0.10 each. Free: 10 alarms. | **$0** ($0.40 without the free tier) | [CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/) |
| SNS email | A handful of alarm emails. The first 1,000 a month are free. | **$0** | AWS price list (AmazonSNS) |
| AWS Budgets | One budget with one action. Monitoring and notifications are free, and "your first two action-enabled budgets are free". | **$0** | [Budgets pricing](https://aws.amazon.com/aws-cost-management/aws-budgets/pricing/) |
| IAM | Roles and policies | **$0** | |
| **Total** | | **≈ $1.60–2.81** with the free tiers; **≈ $3.13–8.35** with none | |

How this compares with the PRD:

- **With the free tiers**, the total is above PRD §17's "≈ $0–1 a month" estimate, though under its $3 cap. The 67 % email (about $2) would likely go out at the top of the range. The largest line is S3 requests, mostly from the capture worker's per-record log rewrites and per-camera lock and state writes.
  - Batching each pass's log records into one append would save about 2,300–4,600 GET+PUT pairs a day, or $0.37–0.75 a month.
  - It would also make the old log versions negligible.
- **Without the Lambda free tier**, the whole range passes the $3 cap, so the hard stop would halt the schedules mid-month. The driver is forecast compute (2 GB × run time).
- **Measure before trusting either number.** Run durations come from step 7, and S3 request counts from S3 Storage Lens or the bill, after the first day.
- **The budget** is $3 a month with the hard stop on. It covers the whole account, so keep the account for Lookout only, or set `budget_scope_to_project_tag`.

## Variables

| Variable | Default | Notes |
|---|---|---|
| `alarm_email` | none (required) | `TF_VAR_alarm_email`; marked sensitive |
| `image_tag` | null | Required for phase 2; the functions refuse to plan without it |
| `region` | `us-east-1` | Set it with `TF_VAR_region` (step 3) |
| `monthly_budget_usd` | 3 | PRD §17's hosting hard cap |
| `budget_alert_thresholds_percent` | `[33, 67, 100]` | Actual spend; 1 to 4 values. A forecast alert at 100 % is always added. |
| `budget_hard_stop` | true | At 100 % of actual spend the schedules stop ([Hard stop](#hard-stop-at-the-cap)). false: emails only |
| `budget_scope_to_project_tag` | false | true counts only `project=lookout` costs. Activate that cost-allocation tag first. |
| `capture_schedule_expression` | `rate(5 minutes)` | Only `rate(N minutes)` with 5 ≤ N ≤ 60, or `rate(1 hour)` (PRD §15.3) |
| `forecast_schedule_expression` | `rate(5 minutes)` | PRD §11.2. `rate(1 minute)` to `rate(1 hour)` |
| `capture_schedule_enabled`, `forecast_schedule_enabled` | true | false pauses a job and silences its `not-running` alarm |
| `user_agent` | `Lookout/0.1 (webcam research capture; fetch-on-change)` | Must not contain an email address |
| `data_bucket_name` | `lookout-data-<account>` | |
| `data_bucket_force_destroy` | false | true only for the final teardown |
| `data_noncurrent_version_days` | 30 | How long an overwritten or deleted object's previous version is kept |
| `log_noncurrent_version_days` | 1 | The same for `capture_log/` and `forecast_log/`, whose every record rewrites the day's log |

## How it behaves

- **One run at a time.** Reserved concurrency 1 per function: never two NPS fetchers, and never overlapping forecast runs.
- **No retries.** Neither the Scheduler nor Lambda retries a failed run; the next tick does. A run that can't start within one interval minus 60 s (240 s for both jobs) is dropped rather than queued. This keeps NPS polling at or below the schedule, as PRD §15.3 asks ("back off on errors"). A forecast run can last up to 600 s, longer than its 5-minute interval; the tick that finds it still running waits up to 240 s and is then dropped (reserved concurrency 1).
- **Scheduled time in the event.** Each event carries `{"job": ..., "scheduled_time": "<aws.scheduler.scheduled-time>", "execution_id": ...}`, filled in by the Scheduler. A worker can log how late each run started.
- **Alarm windows follow the schedule.**
  - Errors alarm: errors in 2 of the last 3 windows of max(15 min, interval).
  - Not-running alarm: no invocation in 2 windows of max(15 min, 3 × interval) in a row.
- **Budget.** Promotional credits are excluded, so the budget sees gross spend. New accounts get up to $200 in credits, which would otherwise hide it. The hard stop therefore acts on gross spend too, even while credits pay the bill.
- **Evidence is recoverable.** The data bucket is versioned and no worker role can delete a version. A buggy overwrite of a frame, forecast file or state document can be undone for 30 days, and of a log for 1 day: `aws s3api list-object-versions --bucket <data bucket> --prefix <key>`, then copy the old version back.
- **Takedowns delete every version.** PRD §15.5 removes a frame from storage within 7 days. On a versioned bucket a plain delete keeps the previous version for 30 days, so delete each version as the owner: `aws s3api list-object-versions` for the key, then `aws s3api delete-object --version-id <id>` for each one, delete markers included.

## Checks

```bash
infra/test.sh
```

It needs no credentials, and no plan or apply touches AWS. It runs:

- `terraform fmt -check -recursive`;
- static checks (`tests/test_static.sh`):
  - no email address anywhere in the Terraform code or the infra scripts;
  - `alarm_email` stays required and sensitive;
  - both providers keep `default_tags`;
  - the `depends_on` ordering hints are still there;
  - every test file pins every variable to its default, which keeps the next step hermetic;
- `init -backend=false`, `validate` and `terraform test` for both modules. The tests are plan-only against a mocked AWS provider, so nothing is created and no AWS call is made. `TF_VAR_*` in your shell and `*.auto.tfvars` files do not change their result.
- shellcheck;
- the `deploy.sh` and `build.sh` tests, against stub `aws`, `terraform` and `docker`;
- a BuildKit check of exactly which files reach the image build. It skips without a running Docker.
- tflint, when installed.

The first `terraform init` downloads the AWS provider from the Terraform registry.

## Not built yet

- `TODO(M1)` CloudFront and the API Lambda for the PRD §11.1 routes. Nothing is public until NPS confirms (§15).
- `TODO(M1)` The prediction issuer (hourly), daily batch and cost report jobs (§11.2). Each is one more entry in `local.jobs` in `terraform/main.tf`.
- `TODO(M3)` The Tier 2 proxy and its server-side daily cap (§11.3).
