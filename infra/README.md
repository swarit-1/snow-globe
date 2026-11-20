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
| `lambda/smoke.sh`, `lambda/smoke_probe.py` | Local smoke test of a built image: an offline probe of both handlers, then the forecast handler under the Lambda Runtime Interface Emulator. It never invokes capture ([The Lambda image](#the-lambda-image)). |
| `deploy.sh` | The owner's one-command deploy. It checks, prints the plan and asks before changing anything. |
| `test.sh` | Every check that needs no AWS credentials. Offline once the AWS provider is in the plugin cache ([Checks](#checks)). |
| `tf_providers.sh` | How `test.sh` installs the provider: from the plugin cache with no network when it matches the lock file, else from the registry. `status` and `fetch` commands. |
| `cost/` | The hosting estimate: a simulation of the capture job's S3 requests and billed waiting, its output for the costed commit, and the arithmetic ([Monthly cost estimate](#monthly-cost-estimate)) |

## What gets created

| Resource | Name | Settings |
|---|---|---|
| State bucket (bootstrap) | `lookout-tfstate-<account>` | Versioned, SSE-S3, all public access blocked, ACLs off, TLS only; old versions expire after 90 days |
| Data bucket | `lookout-data-<account>` | Private, SSE-S3, all public access blocked, ACLs off, TLS only. **Versioned**: an overwritten or deleted object's previous version is kept 30 days (the daily logs' for 1 day, `cache/`'s for 1 day). `cache/` expires after 35 days; nothing else current expires |
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

**Bucket access by job.** Each job owns its outputs, its state documents and its locks, so neither can overwrite or delete the other's state (the capture job's NPS rate-limit history is out of the forecast job's reach). The only deletes a worker makes are releasing a lock and, for capture, removing its own staged copies under `cache/staging/`. No worker may delete a frame, thumbnail, forecast or log, and none may delete an object version. Overwrites are recoverable from the previous version (versioning, above).

| Job | Read and write | Delete |
|---|---|---|
| capture | `frames/`, `thumbs/`, `cache/staging/`, `capture_log/`, `state/capture/`, `state/locks/capture…` | `cache/staging/`, `state/locks/capture…` |
| forecast | `forecast/`, `forecast_log/`, `state/forecast/`, `state/locks/forecast…` | `state/locks/forecast…` |

- **Capture (lookout-capture 0.2.0, D-028).** Every download also gets a 640 px thumbnail in `thumbs/`. The six hourly cameras stage each download under `cache/staging/frames/` until its hour is decided; the nearest frame is then copied to `frames/` and every staged copy deleted (workers/capture/README.md, "IAM"). The bucket's 35-day `cache/` expiry is only a backstop for copies capture never deleted. It outlasts the longest hard stop (one budget month), because a stopped capture job decides its last hours only when it runs again.
- **Forecast** keeps its GRIB subsets on the function's own disk (`/tmp/lookout-data/cache/hrrr/`) and never writes `cache/` in the bucket, so it has no `cache/` grant.
- A worker's state keys must start with `state/<job>/` and its lock names with `<job>` (`lookout_common` stores lock `<name>` at `state/locks/<name>.lock`). Today the capture worker uses `state/capture/…` (the www.nps.gov hold is `state/capture/hosts/www.nps.gov.json`) and locks `capture/<viewpoint_id>` and `capture/hosts`. The forecast worker uses `state/forecast/hrrr/…` and locks `forecast-hrrr` and `forecast-lag-survey`. Anything else gets AccessDenied on Lambda.
- `terraform/tests/lambda.tftest.hcl` checks these grants against the keys the worker code builds (frames, thumbnails, staged copies, logs, state, locks, forecast files, markers). It also pins each role's whole policy: exactly four statements, and for every action the resources of every statement that holds it, so an added statement cannot widen a grant either.

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

The AWS provider is about 760 MB unpacked (764,426,784 bytes for 6.73.0 on darwin_arm64), and `~/Documents` syncs to iCloud.

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

To test the image before pushing it, build it locally with `infra/lambda/build.sh` and run `infra/lambda/smoke.sh lookout-lambda:<tag>` ([The Lambda image](#the-lambda-image)).

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

**A pause longer than 35 days** loses the full-size frames of the last 1–2 hours at the six hourly cameras. Those hours are decided only when capture runs again, and their staged copies under `cache/` expire after 35 days; the hours keep their thumbnails. No other current object expires.

**Emergency stop, without Terraform.** Zero concurrency throttles every run at once:

```bash
aws lambda put-function-concurrency --function-name lookout-capture --reserved-concurrent-executions 0
```

The next `terraform apply` puts it back to 1. While the function is stopped, its `not-running` alarm will email you.

### Hard stop at the cap

PRD §17 caps hosting at $3 a month and says hard caps are enforced in code. When actual spend passes 100 % of `monthly_budget_usd`, AWS Budgets attaches `lookout-budget-hard-stop`, a Deny on `lambda:InvokeFunction` for both functions, to the `lookout-scheduler` role. From then on:

- no scheduled job runs, so capture and forecast data stop;
- you get the budget's 100 % email, the action's own email, then the `not-running` alarms;
- S3 and ECR storage keep costing a few cents a month, and no current object is deleted: the staged frames of capture's last hours outlast the stop (`cache/` expires after 35 days) and are decided when capture runs again;
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

**These are estimates.** Nothing has been deployed, so nothing below was observed on AWS. This is the project's hosting estimate (PRD §17 and D-035 cite it). Updated 20 Nov 2026 for the code at commit `8246a8b` (capture 0.2.0); each input says where it comes from:

- **Prices** are for us-east-1, from AWS's public price-list files published 12–18 Nov 2026 and the pricing pages linked below, read on 19 Nov 2026 (not re-read on 20 Nov).
- **Run counts** follow from the schedules: capture 8,640 runs a month and forecast 8,640 (both every 5 minutes, PRD §11.2), 17,280 in all.
- **Capture is simulated**: the committed capture code, one Lambda-style pass every 5 minutes for 24 simulated hours, against a mock S3 bucket (moto) and the capture tests' fake NPS feeds changing at the measured cadences (Rainier about once a minute, the ARD cameras every 15 minutes). Stores reached 752 of the 768 the cap allows. The simulation is `cost/sim_capture_s3.py`, its output for `8246a8b` is `cost/sim_capture_8246a8b.json`, and `cost/estimate.py` turns that into every figure below; `tests/test_cost.sh` checks that this section quotes them.
  - **Requests.** One pass makes 50–216 S3 requests, 73 on average. Per day: 8,944 PUT, 174 LIST, 7,902 GET, 702 HEAD and 3,420 DELETE (DELETEs are free).
  - **Waiting, which Lambda bills.** Before any request or CPU time, a pass lasts 35.7 s on the simulation's clock on average (median 36.1 s, 95th percentile 56.0 s, longest 59.1 s): 32.8 s waiting for each camera's 5-minute slot and 2.9 s spacing requests 1 s apart. A camera is due 300 s plus a 1 s margin after its last poll, so each pass polls it about 1.05 s later than the one before. Its wait grows until it would pass 45 s (`wait_for_due_s`); the camera is then skipped once (48 `not_due` a day) and polled at the start of the next pass.
- **Forecast's S3 requests are counted from the code** (`process_pending`, `extract.run_covered` and `Extractor._write`). Every call reads the marker of each of the 13 runs in its 12 h lookback, HEADs each written run's 8 forecast files, and reads each pending run's failure record: 26 + 7 × W requests for W written runs. That is 110 with one run pending (W = 12), 103 while the previous run still waits for its last hours, and 117 if the newest run is complete within its own hour. Each extracted run adds 11 PUTs (8 files, the marker, the log, the lock) and 2 GETs (the marker re-read and the log).
- **Storage:**
  - frames and thumbnails come from D-028's measured sizes;
  - the image's size is measured;
  - forecast files at the deployed profile are extrapolated (below);
  - previous versions are simulated: what a simulated day's overwrites and deletes left behind.
- **Assumed:**
  - each capture pass's own requests and CPU, 5–20 s on top of its waiting. The laptop's capture loop spent 2.4 s a pass on NPS requests (measured over 41 passes on 20 Nov). A pass also makes about 73 S3 requests, and resizes and thumbnails its frames at 512 MB, about 0.29 vCPU;
  - forecast run times, and the CloudWatch log volume.

| Item | What drives it | Estimate per month | Source |
|---|---|---|---|
| EventBridge Scheduler | 17,280 runs. The free tier is 14,000,000 a month, then $1.00 per million. | **$0** | [EventBridge pricing](https://aws.amazon.com/eventbridge/pricing/) |
| Lambda requests | 17,280 at $0.20 per million | **$0.00** (about $0.003) | [Lambda pricing](https://aws.amazon.com/lambda/pricing/) |
| Lambda compute (Arm) | Capture 0.5 GB × 40.7–55.7 s × 8,640 (35.7 s of simulated waiting plus 5–20 s assumed): 175,824–240,624 GB-s. Forecast 2 GB: 7,920 calls that find nothing new (1–5 s; each still makes about 110 S3 requests) and 720 that extract an HRRR run (30–150 s): 59,040–295,200 GB-s. In all 234,864–535,824 GB-s, at $0.0000133334 per GB-s. The forecast durations are assumptions: the emulator runs on the laptop used a tiny profile and a 1 h lookback ([The Lambda image](#the-lambda-image)). | **$0–1.81** with the free tier (1 M requests and 400,000 GB-s a month; the AWS page sets no plan condition): the top of the range is 135,824 GB-s over it. Without it, $3.13–7.14. | [Lambda pricing](https://aws.amazon.com/lambda/pricing/) |
| Lambda `/tmp` above 512 MB | Forecast: 1.5 GB extra at $0.0000000309 per GB-s | < $0.01 | [Lambda pricing](https://aws.amazon.com/lambda/pricing/) |
| S3 storage, current objects | 317–330 MB a day, growing, at $0.023 per GB. Frames, thumbnails and sidecars: 262 MB (D-028, measured sizes, 4 downloads an hour per camera). Forecast files: 52–65 MB (below). Logs: about 3.5 MB (capture 2.45 MB in the simulation; forecast about 1 MB, assumed). That is 4.8–5.0 GB on average in month 1, 14.3–14.9 GB in month 2, 23.8–24.8 GB in month 3, and 28.5–29.7 GB on day 90. | **$0.11** (month 1), **$0.33–0.34** (month 2), **$0.55–0.57** (month 3), then $0.22–0.23 more each month | [S3 pricing](https://aws.amazon.com/s3/pricing/) |
| S3 storage, previous versions | Versioning keeps what each overwrite or delete replaced (simulated day): 353 MB of old `capture_log/` versions a day (one rewrite per pass), kept 1–2 days; 53 MB of old state documents a day, kept 30 days (1.6 GB); about 305 MB a day of deleted staged frames (D-028's every-frame sizes at the six hourly cameras), kept 1–2 days. That is 2.3–3.0 GB once steady, from about day 30. | **$0.03** (month 1), **$0.05–0.07** after | [S3 pricing](https://aws.amazon.com/s3/pricing/) |
| S3 requests | Capture (simulated): 273,540 PUT and LIST and 258,120 GET and HEAD, $1.47. Its PUTs a month: state documents 91,680 ($0.46), locks 69,120 ($0.35), thumbnails 45,120 ($0.23), staged copies 33,840 ($0.17), frames 19,920 ($0.10), logs 8,640 ($0.04), listings 5,220 ($0.03). Forecast (counted): 7,920 PUT and 891,360–1,012,320 GET and HEAD, $0.40–0.44; the steady state's lookback HEADs alone are 829,440 ($0.33). PUT and LIST are $0.005 per 1,000; GET and HEAD $0.0004 per 1,000. DELETE and lifecycle expirations are free. | **$1.87–1.92** | [S3 pricing](https://aws.amazon.com/s3/pricing/) |
| S3 ↔ Lambda, NOAA → Lambda transfer | Same region | **$0** | [S3 pricing](https://aws.amazon.com/s3/pricing/) |
| ECR | At most 5 images (the lifecycle rule) of 293.9 MB each: layers, config and manifest of `lookout-lambda:8246a8bd570b`, measured on 20 Nov. That is 1.47 GB at $0.10 per GB, $0.15. All five share the pinned AWS base (200.3 MB) until the pin is bumped. If ECR stores a shared layer once per repository (not checked offline), that is 0.67 GB, $0.07. AWS's page (19 Nov) gives new customers 500 MB a month free for 12 months; the totals leave it out. ECR → Lambda in the same region is free. | **$0.07–0.15** | [ECR pricing](https://aws.amazon.com/ecr/pricing/) |
| CloudWatch Logs | 17–86 MB a month (1–5 KB per run, assumed). Ingestion is $0.50 per GB, storage $0.03 per GB, 30-day retention. Free: 5 GB a month. | **$0** (≤ $0.04 without the free tier) | [CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/) |
| CloudWatch alarms | 4 standard alarms at $0.10 each. Free: 10 alarms. | **$0** ($0.40 without the free tier) | [CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/) |
| SNS email | A handful of alarm emails. The first 1,000 a month are free. | **$0** | AWS price list (AmazonSNS) |
| AWS Budgets | One budget with one action. Monitoring and notifications are free, and "your first two action-enabled budgets are free". | **$0** | [Budgets pricing](https://aws.amazon.com/aws-cost-management/aws-budgets/pricing/) |
| IAM | Roles and policies | **$0** | |
| **Total** | | With the free tiers: **$2.07–4.03** in month 1, **$2.32–4.29** in month 2, **$2.53–4.52** in month 3, then about $0.22 more each month. The bottom of the range passes $3 in month 6, the top from month 1. With none: $5.63–9.83 in month 1. | |

**Which capture code this costs.** The figures are for capture 0.2.0 at `8246a8b`, the code in `lookout-lambda:8246a8bd570b`. The working tree's capture 0.2.1, uncommitted when it was simulated at 08:18 UTC on 20 Nov, gives a camera a 2 ms margin instead of 1 s. In the same simulation its passes wait 14.6 s on average (12.2 s for slots, 2.5 s spacing requests), so capture's compute is 84,758–149,558 GB-s and the whole Lambda bill at most 44,758 GB-s over the free tier ($0.60). Its total with the free tiers is $2.07–2.81 in month 1 and $2.31–3.07 in month 2; with none, $4.41–8.61 in month 1. Re-run the simulation and `cost/estimate.py` on the commit you deploy.

**Forecast files at the deployed profile.** Terraform sets no `LOOKOUT_FORECAST_*` variable, so the deployed forecast job extracts all 22 fields and every hour. That is 453 MB of GRIB for an 18 h run and 1,202 MB for a 48 h run (workers/forecast/README.md, measured), which is free in-region. No file of that profile has been written yet. The estimate is extrapolated from the layout of a measured light file: 31.3 KB on average over 249 local files for 8 fields × 3 hours × 57 points. That file is about 5.7 B per value, 0.7 KB plus 0.1 KB per field for each hour, 0.65 KB per field and 13.8 KB fixed. At 5.7–8 B per value, a viewpoint's file would be 219–274 KB per 18 h run and 520–662 KB per 48 h run, 52–65 MB a day for 8 viewpoints. The local stand-in's light profile writes 6 MB a day.

**D-028's storage check.** 23.6 GB × $0.023 = $0.54 a month is right as the rate on day 90 for frames and thumbnails alone. Those cost less in the months before: $0.09, $0.27 and $0.45 on average. With forecast files, logs and previous versions, the storage rate on day 90 is $0.71–0.75.

How this compares with the PRD:

- **With the free tiers**, the total is above PRD §17's "≈ $0–1 a month" from the first month, and the range straddles its $3 cap from month 1. The bottom stays under it until month 6, as storage grows by 9.5–9.9 GB a month and adds $0.22–0.23 to each month's bill. The top, $4.03, is over it in month 1: the free 400,000 GB-s run out on day 22, spend passes $3 on about day 27 (`cost/estimate.py`), and the hard stop then halts the schedules for the rest of the month. The staged frames are kept (`cache/` expires after 35 days). The 67 % email (about $2) would go out in month 1 at any point of the range.
- **The range is wide because two assumptions are**: forecast run time (720 extractions × 2 GB × 30–150 s is 43,200–216,000 GB-s) and capture's own request and CPU time (21,600–86,400 GB-s). The simulated waiting is not small: 154,224 GB-s a month (0.5 GB × 35.7 s × 8,640), 39 % of the free tier. Capture 0.2.1's 2 ms margin cuts it to 63,158 GB-s.
- **S3 requests are the largest line at the bottom of the range, $1.87–1.92**; at the top, Lambda compute adds $1.81. Capture 0.2.0 already writes each pass's log in one rewrite, which removed the old per-record rewrites. Thumbnails and staging added about the same number of PUTs back, so the line is unchanged. The levers belong to the workers' owners:
  - Capture takes a lock and writes its state document at every poll, unchanged ones included: 5,360 PUTs a day, $0.80 a month.
  - Forecast re-checks every written run of its 12 h lookback with a HEAD per viewpoint every 5 minutes, $0.33 a month. Trusting the marker, which already lists each viewpoint's file, would remove that.
  - Old state versions are kept 30 days (1.6 GB, about $0.04 a month). A shorter rule for `state/` would make them negligible.
- **Without the Lambda free tier**, the whole range passes the $3 cap, so the hard stop would halt the schedules mid-month. The drivers are capture's billed waiting and forecast compute (2 GB × run time).
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

## The Lambda image

One arm64 image runs both jobs (PRD §11.3). Terraform picks each function's handler with `image_config.command`. The image's default command is the forecast handler, so a local `docker run` that forgets the command reads NOAA's public bucket and never becomes a second NPS fetcher.

**Build** with `infra/lambda/build.sh`. It loads `lookout-lambda:<tag>` into the local Docker; `--push REPO` pushes instead (step 6b.4). The build imports both handlers and fails if a module or a native library cannot load. It also fails if boto3 or botocore would come from the base image's own copy (1.43.127) instead of the locked one (1.44.6).

**Both base images are pinned by digest** in the Dockerfile, so one commit always builds the same image. AWS republishes `public.ecr.aws/lambda/python:3.13` often: two builds 12 minutes apart on 20 Nov got different digests. To take AWS's patches, bump the pin on purpose before a deploy:

```bash
docker buildx imagetools inspect public.ecr.aws/lambda/python:3.13 | sed -n 's/^Digest: *//p'
# put that digest in PYTHON_BASE in infra/lambda/Dockerfile, then build, smoke-test and commit
```

**Measured on 20 Nov 2026** (MacBook Air M4, Docker Desktop 29.5.3, BuildKit 0.30.0, native linux/arm64):

| What | Value |
|---|---|
| Cold build: both bases pulled, 28 wheels downloaded | 20.0 s |
| Rebuild after AWS republished the base: base pulled again, wheels from the uv cache | 15.8 s |
| Rebuild with nothing changed | 1.1 s |
| Rebuild of the clean commit `8246a8b` (capture and forecast code changed; base and wheels from the caches) | 12.2 s, tag `8246a8bd570b` |
| Compressed size: what ECR stores and Lambda pulls | 293.8 MB for `8246a8bd570b` (293,843,493 bytes of layers; 293.9 MB for the first build). The AWS base is 200.3 MB, byte-identical in both builds because it is pinned; `/var/task` 93.5 MB; manifests and schema 10 KB. |
| Unpacked size | 1.25 GB. `/var/task` is 319 MiB: ecCodes and eckit 183 MiB (eckit bundles PROJ, curl and OpenSSL 1.1.1k), numpy 64 MiB, botocore 31 MiB. |
| Runtime | Python 3.13.18, awslambdaric 4.2.2, Amazon Linux 2023 (glibc 2.34) |
| `/var/task`, exactly the two lock files | anyio 4.16.0, attrs 26.1.0, boto3 1.44.6, botocore 1.44.6, certifi 2026.10.23, cffi 2.1.1, eccodes 2.50.1 (ecCodes library 2.50.0), eccodeslib 2.50.0.34, eckitlib 2.5.0.34, findlibs 0.1.5, h11 0.16.0, httpcore 1.0.9, httpx 0.28.1, idna 3.20, jmespath 1.1.0, jsonschema 4.26.0, jsonschema-specifications 2025.9.1, numpy 2.5.5, pillow 13.0.0, pycparser 3.12, python-dateutil 2.9.0.post0, referencing 0.37.0, rpds-py 2026.9.1, s3transfer 0.19.3, six 1.17.0, typing_extensions 4.16.0, tzdata 2026.6, urllib3 2.8.0; lookout-common 0.1.0, lookout-capture 0.2.0, lookout-forecast 0.1.0 |
| Base image's own packages, behind `/var/task` on the runtime's `sys.path` | awslambdaric 4.2.2, boto3 1.43.127, botocore 1.43.127, jmespath 1.1.0, pip 26.2.2, python-dateutil 2.9.0.post0, s3transfer 0.19.3, simplejson 4.3.0, six 1.17.0, snapshot-restore-py 1.0.0, urllib3 2.8.0 |

**Smoke test:** `infra/lambda/smoke.sh lookout-lambda:<tag>`; `--offline` runs step 1 only. It builds, pushes and deploys nothing. It prints `key=value` results and the full package list, and exits 1 if a check fails.

1. **Offline probe.** First, the image's default command must not be the capture handler. The probe then runs with no network, a read-only root file system and a non-root user. `smoke_probe.py` puts `sys.path` in the runtime's order and resolves both handler strings with the runtime's own loader, as Lambda does at init. It checks that the pinned packages and `libeccodes.so` load from `/var/task`. It also builds what the capture handler builds before its first request: the 8 manifests, S3 storage for a made-up bucket, and the HTTP client with the project User-Agent. It uses none of them.
2. **Forecast invoke.** The forecast handler runs under the Runtime Interface Emulator that ships in the AWS base image, and is invoked twice. Storage is `LOOKOUT_STORAGE=local`, in a new temporary directory that the script deletes. The profile is tiny: `VIS:surface`, forecast hour 0, 10 MB a run, 1 h lookback. The container gets 2048 MB with no swap, 1.16 vCPU (Lambda gives one vCPU per 1,769 MB), a 2 GB `/tmp`, a read-only root and a non-root user. Checks:
   - the first call extracts a run;
   - the second call finds it already written;
   - no GRIB file is left behind;
   - the handler logs no error.

**No local test invokes the capture handler.** It polls nps.gov, and the capture loop is the only NPS fetcher. Capture is checked only by the import in the build and by the offline probe.

Three runs on 20 Nov. Run 1 (00:17Z) used the first build, on the base published before 00:12Z, and run 2 (00:26Z) that build's final image, through `smoke.sh`. Both extracted HRRR run 2026111923 f00, because run 2026112000 was not on S3 yet. Run 3 (06:59Z) is `smoke.sh` on `lookout-lambda:8246a8bd570b`, built from the clean commit `8246a8b`. Its 1 h lookback held two published runs, 2026112006 and 2026112005, so call 1 extracted both.

| | Run 1 | Run 2 | Run 3 (`8246a8bd570b`) |
|---|---|---|---|
| Runtime init (the emulator's `INIT REPORT`) | 267 ms | 299 ms | 232 ms |
| Call 1, handler (`INVOKE RTDONE`) | 1,278 ms | 1,222 ms | 2,696 ms (2 runs) |
| Call 1, bytes from NOAA | 3,588,572 | 3,588,572 | 7,254,750 |
| Call 1, files written | 11 files, 126,583 bytes | 11 files, 126,584 bytes | 20 files, 257,718 bytes |
| Call 2, handler | 187 ms | 209 ms | 38 ms |
| Call 2, bytes and files | 285 bytes; nothing written | the same | 0 bytes (both runs written; no listing); nothing written |
| Peak memory (container cgroup, emulator included) | 137.8 MiB | 138.4 MiB | 144.8 MiB |

- **Call 1's bytes** came from 3 listings (7,495 bytes), one `.idx` (9,000) and two ranged GETs (3,572,077). The ranged GETs fetch `VIS:surface` plus the `HGT:surface` terrain, which every run's first hour includes.
- **Call 1's files** were:
  - 8 forecast files of 15.2–15.5 KB each;
  - the day's `forecast_log` (2,860 bytes: 5 `lag` events and 1 `run` event);
  - the run's state marker (1,754 bytes);
  - the lock file (54 bytes).
  
  No GRIB file was left in `cache/hrrr/`.
- **Call 2** made one listing, of the run that was not on S3 yet. It skipped 2026111923 as written.
- **Run 3, call 1:** 4 listings (35,668 bytes), 2 `.idx` (17,998) and 4 ranged GETs (7,201,084). It wrote 16 forecast files, the day's `forecast_log`, 2 run markers and the lock file, and left no GRIB. The offline probe found the same Python and the same 31 distributions as the first build (lookout-capture 0.2.0), and no failure.

**Reading these numbers:**
- **The emulator's own `REPORT` line misleads.** It shows "Init Duration: 0.04 ms" and "Max Memory Used: 2048 MB". On the first call, its "Duration" also includes init. The script reports the `INIT REPORT` and `INVOKE RTDONE` lines and the cgroup's `memory.peak` instead.
- **These are laptop numbers.** NOAA was reached over the internet, not from inside us-east-1. The CPU was an M4 capped at 1.16 vCPU, not Graviton. A real cold start also loads the image, which the emulator skips.

**Capture's grants.** The capture code in this image writes `thumbs/` and `cache/staging/`, and deletes its staged copies (workers/capture/README.md, "IAM"). `local.jobs` in `terraform/main.tf` has granted both since 20 Nov ([Bucket access by job](#what-gets-created)). Without those grants the six hourly cameras would lose their frames on Lambda.

## Checks

```bash
infra/test.sh              # offline when it can be, else the registry (CI)
infra/test.sh --offline    # never the registry; fails at once if the provider is not cached
infra/tf_providers.sh status   # can init run offline here? Reads files only
infra/tf_providers.sh fetch    # the one-time network step: put the locked provider in the cache
```

It needs no credentials, and no plan or apply touches AWS. It runs:

- `terraform fmt -check -recursive`;
- static checks (`tests/test_static.sh`):
  - no email address anywhere in the Terraform code, the infra scripts or `cost/`, and in the tests and this README only addresses at the reserved `example.com` and `example.invalid` domains;
  - `alarm_email` stays required and sensitive;
  - both providers keep `default_tags`;
  - the `depends_on` ordering hints are still there;
  - every test file pins every variable to its default, which keeps the next step hermetic;
- `init -backend=false`, `validate` and `terraform test` for both modules. The tests are plan-only against a mocked AWS provider, so nothing is created and no AWS call is made. `TF_VAR_*` in your shell and `*.auto.tfvars` files do not change their result.
- the offline provider path (`tests/test_tf_providers.sh`): a fake provider in a fake cache, Terraform's own `h1:` hash as the oracle, a real offline `init`, and a stub `terraform` for each mode;
- shellcheck of every infra script, `lambda/smoke.sh` and `tf_providers.sh` included;
- the `deploy.sh` and `build.sh` tests, against stub `aws`, `terraform` and `docker`;
- a BuildKit check of exactly which files reach the image build. It skips without a running Docker.
- the cost estimate (`tests/test_cost.sh`): `cost/estimate.py` on the committed simulation output, checks on its arithmetic, and a check that [Monthly cost estimate](#monthly-cost-estimate) quotes its figures; then ruff on `cost/` (`cost/ruff.toml`, no cache written), which skips when ruff is not installed (as in CI's infra job);
- tflint, when installed.

**Network.** The only network step is installing the AWS provider (`hashicorp/aws` 6.73.0, about 760 MB unpacked) into `TF_PLUGIN_CACHE_DIR`, by default `$LOOKOUT_DATA_DIR/cache/terraform-plugins`, outside iCloud.

- **The first time,** with no cached copy for this machine's platform, a registry init fetches it: `infra/test.sh`, or just `infra/tf_providers.sh fetch`. That contacts:
  - registry.terraform.io, for service discovery, the version list and the download location;
  - releases.hashicorp.com, for the zip (estimated at about 160 MB by compressing the cached binary), its `SHA256SUMS` and the signature.
- **After that,** `tf_providers.sh` installs from the cache with `init -plugin-dir` and no network. It does so only when the cached package's `h1:` hash, computed as Terraform computes it, is in the module's `.terraform.lock.hcl`; Terraform makes the same check. Measured on 20 Nov: the whole of `infra/test.sh` passed under a macOS sandbox that denied every outbound connection except loopback and Unix sockets (`--offline` and the default mode, 22–24 s). Terraform runs with `CHECKPOINT_DISABLE=1`, so it never asks checkpoint-api.hashicorp.com about new releases.
- **The default mode falls back to the registry** when the cache cannot serve, or when the offline init fails. The fallback is unchanged: the same init as before, which also fills the cache. CI runs this way on linux_amd64. The committed lock files hold the `h1:` hash for darwin_arm64 only, so CI's restored cache never matches, and CI keeps using the registry. Adding the linux_amd64 hash would let CI run offline too: `terraform providers lock -platform=darwin_arm64 -platform=linux_amd64`, once per module, with network.
- **Still networked:** `tflint --init` downloads its AWS ruleset from GitHub the first time, when tflint is installed (it is not, here or in CI). `--offline` skips `tflint --init` and lints with the rulesets already installed; if one is missing, tflint fails and the message names the one-time command. The BuildKit check builds `FROM scratch` and pulls nothing.

`infra/lambda/smoke.sh` is not part of `test.sh`. It needs a built image, and its forecast step reads NOAA's bucket ([The Lambda image](#the-lambda-image)).

## Not built yet

- `TODO(M1)` CloudFront and the API Lambda for the PRD §11.1 routes. Nothing is public until NPS confirms (§15).
- `TODO(M1)` The prediction issuer (hourly), daily batch and cost report jobs (§11.2). Each is one more entry in `local.jobs` in `terraform/main.tf`.
- `TODO(M3)` The Tier 2 proxy and its server-side daily cap (§11.3).
