# Snow Globe infrastructure

Renamed from Lookout on 21 Nov 2026 (D-080): every resource name, tag and bucket prefix is now `snow-globe` (`project = "snow-globe"`), the Lambda environment uses `SNOW_GLOBE_*` names (the workers still read the old `LOOKOUT_*` ones as a fallback), and the default User-Agent is `SnowGlobe/0.1 (webcam research capture; fetch-on-change)`. Nothing was ever applied, so no AWS resource carries the old name. The image sizes and the smoke runs below were measured before the rename, so they name the image `lookout-lambda:8246a8bd570b`, which is what that image is called in the local Docker. The cost simulation was re-run on the renamed code (`8fa6b6a`) on 21 Nov.

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
| `cost/` | The hosting estimate: a simulation of the capture job's S3 requests and billed waiting, the measured nps.gov request times it draws from (and the script that collects them), its output for the costed commit (several seeds, pooled by `sim_pool.py`), a count of the forecast job's S3 requests made by running its code, and the arithmetic ([Monthly cost estimate](#monthly-cost-estimate)) |
| `ops/migrate-to-snow-globe.sh` | The rename's live switch on the lead's Mac (D-080), run once right after the M0.5 gate closes: it refuses unless the data report shows the 72 h covered, rehearses the build before stopping anything, then moves `~/lookout-data` to `~/snow-globe-data` (with a symlink back) and restarts both local jobs on the new names. Tested by `tests/test_migrate.sh` against stub loops; not an AWS step. |

## What gets created

| Resource | Name | Settings |
|---|---|---|
| State bucket (bootstrap) | `snow-globe-tfstate-<account>` | Versioned, SSE-S3, all public access blocked, ACLs off, TLS only; old versions expire after 90 days |
| Data bucket | `snow-globe-data-<account>` | Private, SSE-S3, all public access blocked, ACLs off, TLS only. **Versioned**: an overwritten or deleted object's previous version is kept 30 days (the daily logs' for 1 day, `cache/`'s for 1 day). `cache/` expires after 35 days; nothing else current expires |
| ECR repository | `snow-globe/lambda` | Scan on push, immutable tags, keeps the last 5 images; only `snow-globe-*` functions may pull |
| Lambda `snow-globe-capture` | handler `snow_globe_capture.lambda_handler.handler` | arm64, 512 MB, 120 s, reserved concurrency 1 |
| Lambda `snow-globe-forecast` | handler `snow_globe_forecast.lambda_handler.handler` | arm64, 2048 MB, 600 s, 2 GB `/tmp`, reserved concurrency 1 |
| Function environment | both | `SNOW_GLOBE_STORAGE=s3`, `SNOW_GLOBE_S3_BUCKET`, `SNOW_GLOBE_MANIFEST_DIR=/var/task/viewpoints/manifests`, `SNOW_GLOBE_USER_AGENT`, `SNOW_GLOBE_DATA_DIR=/tmp/snow-globe-data` |
| IAM role per function | `snow-globe-<job>-lambda` | Writes to its own log group only. Reads and writes only its own bucket keys (below). |
| Log groups | `/aws/lambda/snow-globe-<job>` | 30-day retention (PRD §15.2) |
| Schedules | group `snow-globe` | capture and forecast both `rate(5 minutes)` (PRD §11.2); no flexible window, no retries |
| Scheduler role | `snow-globe-scheduler` | `lambda:InvokeFunction` on the two functions only |
| Alarms | `snow-globe-<job>-errors`, `snow-globe-<job>-not-running` | Errors in 2 of 3 windows; no invocation in 2 windows in a row |
| SNS topic | `snow-globe-alarms` | Email subscription to `alarm_email` |
| Budget | `snow-globe-monthly` | 3 USD a month, PRD §17's hosting hard cap. Emails at 33, 67 and 100 % of actual spend (about $1, $2, $3) and when the month is forecast to pass $3. It counts costs before credits and refunds ([What the budget counts](#hard-stop-at-the-cap)) |
| Hard stop | budget action, policy `snow-globe-budget-hard-stop`, role `snow-globe-budget-action` | At 100 % of actual spend, AWS Budgets attaches a deny-invoke policy to `snow-globe-scheduler`: no job runs until next month (see [Hard stop](#hard-stop-at-the-cap)) |

Every resource is tagged `project=snow-globe`.

**Bucket access by job.** Each job owns its outputs, its state documents and its locks, so neither can overwrite or delete the other's state (the capture job's NPS rate-limit history is out of the forecast job's reach). The only deletes a worker makes are releasing a lock and, for capture, removing its own staged copies under `cache/staging/`. No worker may delete a frame, thumbnail, forecast or log, and none may delete an object version. Overwrites are recoverable from the previous version (versioning, above).

| Job | Read and write | Delete |
|---|---|---|
| capture | `frames/`, `thumbs/`, `cache/staging/`, `capture_log/`, `state/capture/`, `state/locks/capture…` | `cache/staging/`, `state/locks/capture…` |
| forecast | `forecast/`, `forecast_log/`, `state/forecast/`, `state/locks/forecast…` | `state/locks/forecast…` |

- **Capture (0.2.0 and later, D-028).** Every download also gets a 640 px thumbnail in `thumbs/`. The six hourly cameras stage each download under `cache/staging/frames/` until its hour is decided; the nearest frame is then copied to `frames/` and every staged copy deleted (workers/capture/README.md, "IAM"). The bucket's 35-day `cache/` expiry is only a backstop for copies capture never deleted. It outlasts the longest hard stop (one budget month), because a stopped capture job decides its last hours only when it runs again.
- **Forecast** keeps its GRIB subsets on the function's own disk (`/tmp/snow-globe-data/cache/hrrr/`) and never writes `cache/` in the bucket, so it has no `cache/` grant.
- A worker's state keys must start with `state/<job>/` and its lock names with `<job>` (`snow_globe_common` stores lock `<name>` at `state/locks/<name>.lock`). Today the capture worker uses `state/capture/…` (the www.nps.gov hold is `state/capture/hosts/www.nps.gov.json`) and locks `capture/<viewpoint_id>` and `capture/hosts`. The forecast worker uses `state/forecast/hrrr/…` and locks `forecast-hrrr` and `forecast-lag-survey`. Anything else gets AccessDenied on Lambda.
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
export TF_PLUGIN_CACHE_DIR="$(bash -c '. infra/tf_providers.sh && tfp_cache_dir')"   # <data dir>/cache/terraform-plugins
mkdir -p "$TF_PLUGIN_CACHE_DIR"
```

`tfp_cache_dir` resolves the data dir as the workers do: `~/lookout-data` until the rename's live switch, `~/snow-globe-data` after it (D-080). Don't type `~/snow-globe-data` here before the switch: that would create a second data folder beside the running jobs' one.

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
`terraform -chdir=infra/terraform/bootstrap import aws_s3_bucket.tfstate snow-globe-tfstate-<account>`.

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
REPO=$(aws ecr describe-repositories --repository-names snow-globe/lambda --query 'repositories[0].repositoryUri' --output text)
aws ecr get-login-password | docker login --username AWS --password-stdin "${REPO%%/*}"
TAG=$(infra/lambda/build.sh --push "$REPO" | sed -n 's/^image_tag=//p')
echo "$TAG"
```

The tag is the git commit, plus `-dirty-<time>` when `workers/`, `viewpoints/` or `infra/lambda/` has uncommitted changes.

To test the image before pushing it, build it locally with `infra/lambda/build.sh` and run `infra/lambda/smoke.sh snow-globe-lambda:<tag>` ([The Lambda image](#the-lambda-image)).

**6b.5 Stop every local NPS fetcher** (see step 0).

**6b.6 Phase 2: everything else.**

```bash
terraform -chdir=infra/terraform plan  -var image_tag="$TAG"
terraform -chdir=infra/terraform apply -var image_tag="$TAG"
```

### 7. Verify the schedules

```bash
aws scheduler list-schedules --group-name snow-globe --query 'Schedules[].[Name,State]' --output table
aws scheduler get-schedule --group-name snow-globe --name snow-globe-capture --query '{state:State,expr:ScheduleExpression}'
aws logs tail /aws/lambda/snow-globe-capture --since 15m          # a run every 5 minutes
aws logs tail /aws/lambda/snow-globe-forecast --since 15m         # a run every 5 minutes
aws cloudwatch describe-alarms --alarm-name-prefix snow-globe- --query 'MetricAlarms[].[AlarmName,StateValue]' --output table
aws s3 ls "s3://$(terraform -chdir=infra/terraform output -raw data_bucket)/" --recursive --summarize | tail -n 3
```

Expect one ALARM email from each `not-running` alarm right after the first deploy, because the windows before the first run are empty. Each turns OK once a whole window contains a run: within about 15–30 min. After a day, measure what the cost estimate assumed (run durations, and the bucket's request counts and old-version bytes):

```bash
for f in snow-globe-capture snow-globe-forecast; do
  aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name Duration \
    --dimensions Name=FunctionName,Value=$f --statistics Average Maximum --period 86400 \
    --start-time "$(date -u -v-1d +%Y-%m-%dT%H:%M:%SZ)" --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
done
```

### 8. Deploy new code

Build and push a new tag (6b.4), then `terraform -chdir=infra/terraform apply -var image_tag="$TAG"`.

Every later apply needs the current tag. Reuse it with `-var image_tag="$(terraform -chdir=infra/terraform output -raw image_tag)"`. Or keep it in `infra/terraform/snow-globe.auto.tfvars` (gitignored) as `image_tag = "..."`. `infra/test.sh` is unaffected by that file and by any `TF_VAR_*` in your shell: the tests pin every variable.

The commands in steps 8–10 assume us-east-1. If you deployed elsewhere, keep `TF_VAR_region` exported (step 3) and add `--region` to the `aws` commands.

### 9. Pause and resume

**Pause.** Both schedules are disabled and their `not-running` alarms are silenced:

```bash
T="$(terraform -chdir=infra/terraform output -raw image_tag)"
terraform -chdir=infra/terraform apply -var image_tag="$T" -var capture_schedule_enabled=false -var forecast_schedule_enabled=false
```

Repeat the two `-var ...=false` flags on later applies, or put them in `snow-globe.auto.tfvars`. Otherwise the next apply re-enables the schedules.

**Resume:** apply again without the two flags.

**A pause longer than 35 days** loses the full-size frames of the last 1–2 hours at the six hourly cameras. Those hours are decided only when capture runs again, and their staged copies under `cache/` expire after 35 days; the hours keep their thumbnails. No other current object expires.

**Emergency stop, without Terraform.** Zero concurrency throttles every run at once:

```bash
aws lambda put-function-concurrency --function-name snow-globe-capture --reserved-concurrent-executions 0
```

The next `terraform apply` puts it back to 1. While the function is stopped, its `not-running` alarm will email you.

### Hard stop at the cap

PRD §17 caps hosting at $3 a month and says hard caps are enforced in code. When actual spend passes 100 % of `monthly_budget_usd`, AWS Budgets attaches `snow-globe-budget-hard-stop`, a Deny on `lambda:InvokeFunction` for both functions, to the `snow-globe-scheduler` role. From then on:

- no scheduled job runs, so capture and forecast data stop;
- you get the budget's 100 % email, the action's own email, then the `not-running` alarms;
- S3 and ECR storage keep costing a few cents a month, and no current object is deleted: the staged frames of capture's last hours outlast the stop (`cache/` expires after 35 days) and are decided when capture runs again;
- `terraform apply` does not undo it, because Terraform does not manage the scheduler role's attached policies.

AWS Budgets sees spend a few times a day, so the month can pass $3 by a little before the stop lands. The earlier emails, at about $1 and $2 and when the forecast passes $3, are the warning to act on. AWS needs about five weeks of usage before it forecasts.

**When it ends:** AWS resets IAM-policy actions at the start of each budget period, here the next month ([AWS announcement](https://aws.amazon.com/about-aws/whats-new/2020/10/announcing-aws-budgets-actions/)).

**To resume sooner:** raising the cap changes a PRD §17 decision, so record it in DECISIONS.md first. Then apply with a higher `-var monthly_budget_usd=...` and reverse the action:

```bash
aws budgets execute-budget-action --account-id "$(aws sts get-caller-identity --query Account --output text)" \
  --budget-name snow-globe-monthly --execution-type REVERSE_BUDGET_ACTION \
  --action-id "$(terraform -chdir=infra/terraform output -raw budget_hard_stop_action_id)"
```

**To keep emails only:** apply with `-var budget_hard_stop=false`.

**Not yet tried against AWS:** the action, its role and its policy have been checked only by the plan-only tests. If creating `aws_budgets_budget_action.hard_stop` fails during phase 2, apply again with `-var budget_hard_stop=false` so the rest deploys, and report the error.

**What the budget counts: costs before credits.** The budget's `cost_types` sets `include_credit = false` and `include_refund = false`. AWS counts both by default ([CostTypes](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_budgets_CostTypes.html)), and both arrive as negative amounts. Every other cost type keeps AWS's default: counted, at unblended rates.
- **Why it matters.** The account has $100 of sign-up credits, and on 21 Nov the owner applied for $5,000 of AWS Activate credits, which last two years. A budget that netted credits would read about $0 every month. No email would go out, and the hard stop would never fire.
- **What it means.** The $3 cap binds on gross usage, whatever pays the bill. Letting credits absorb more would mean raising `monthly_budget_usd`, a PRD §17 change (above).
- **The test.** `terraform/tests/monitoring.tftest.hcl` (`budget_counts_costs_before_credits_and_refunds`) fails if credits or refunds are counted again, or if any other cost type is excluded.
- **The budget counts tax; the estimate is before tax.** Tax is one of the cost types left at AWS's default (`include_tax`), so it is counted, and the test keeps it so. The [cost estimate](#monthly-cost-estimate) uses list prices before tax. Where AWS charges sales tax at a rate t, the hard stop therefore fires once spend before tax reaches $3 ÷ (1 + t): $2.78 at 8 %. Whether PRD §17's $3 is before or after tax is the owner's call. Setting `include_tax = false` would make the budget count before tax, like the estimate and AWS's spend limits (below). Not checked offline: when in the month AWS adds the tax to a budget's actual cost.
- **A deprecation to watch.** The Budgets API reference, read on 21 Nov, marks `CostTypes` (and `CostFilters`) deprecated. A budget takes either `FilterExpression` and `Metrics` or `CostFilters` and `CostTypes`, "not both" ([CreateBudget](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_budgets_CreateBudget.html)). The pinned provider still supports `cost_types`, and it refuses a mix at plan time.
  - If AWS ever stops honouring `CostTypes`, its defaults count credits again.
  - The way out is `filter_expression`, excluding credit and refund records and keeping the tag scope, with `metrics = ["UnblendedCost"]`. That is proposed, not built: the dimension values were not checked against AWS.

### Spend limits in AWS Settings (read 21 Nov 2026; not used)

AWS Settings offers spend limits on accounts made with the new sign-up, "Sign up for AWS (new)" ([announced 16 Sep 2026](https://aws.amazon.com/about-aws/whats-new/2026/09/New-AWS-Builder-Experience/); docs: [create](https://docs.aws.amazon.com/accounts/latest/reference/create-spend-limit.html), [update](https://docs.aws.amazon.com/accounts/latest/reference/update-spend-limit.html), [remove](https://docs.aws.amazon.com/accounts/latest/reference/remove-spend-limit.html)). The pages read show no date.

- **What it caps.** A project's monthly pre-tax costs. A project is one AWS account in an organization that AWS manages ([sign-up options](https://docs.aws.amazon.com/accounts/latest/reference/sign-up-for-aws.html)). It needs the Paid plan. The lowest limit is $20, or AWS's estimate of the month's spend if that is higher, so it cannot hold PRD §17's $3.
- **Before the limit.**
  - Emails go out at 50, 75 and 90 % of actual cost, and when the limit is about 10 days away.
  - Optional early controls act about 7, 5 and 4 days before the limit. They stop new resources (an AWS-managed SCP that denies `lambda:CreateFunction`, `scheduler:CreateSchedule` and more), then pause idle resources, then pause the top cost drivers, Lambda among them.
  - Near the limit, AWS applies an SCP that denies `lambda:InvokeFunction` and `lambda:PutFunctionConcurrency` ([SCPs for spend limits](https://docs.aws.amazon.com/accounts/latest/reference/scps-and-rcps-for-projects.html#scps-for-spend-limits)).
- **At the limit.** AWS pauses the project and stops all its resources. The data is kept, but AWS deletes the project's data if nothing is done "within 90 days". Raising the limit reactivates the project, and some resources may then need a manual restart. The announcement says a project at its limit is "paused for that month"; the docs say only that raising the limit reactivates it.
- **Credits.** Spend limits "exclude credits" and apply to pre-tax charges. A spend limit is a type of AWS Budgets, so this reads as gross usage, as in this module's budget: credits would not keep a limit from being reached. That reading is ours; the docs don't spell it out.
- **Terraform or an API: none.** A spend limit is set in the AWS Settings console only ([Billing and Cost Management guide](https://docs.aws.amazon.com/cost-management/latest/userguide/bcm-lite-use-budget.html)). The pinned AWS provider (6.73.0) has no resource for it, and the pages read name no API, CLI command or CloudFormation type.
- **Not with AWS Activate.** The sign-up comparison offers extra AWS Activate credits only with "Sign up for AWS (advanced)", and spend limits only with the new sign-up. Activating advanced features removes an account's spend limits, and activation cannot be reversed ([advanced features](https://docs.aws.amazon.com/accounts/latest/reference/activate-advanced-features.html)). So an account that receives Activate credits cannot have a spend limit.

**Proposal for the owner, with D-035 (not built).** Keep D-035's budget action as the hard stop. It fires at $3, far below the $20 floor, and it stops only the scheduler's invocations: storage, the image and the data stay. A spend limit could only be an outer ceiling, set by the owner in the console, and only on an account without Activate credits:
1. **The expected case.** The deployment account gets the Activate credits, so it has no spend limit. The budget action, counting costs before credits, is the only automatic stop.
2. **If the account is a new-sign-up project without Activate credits.** Set a $20 limit with every early control off, and keep this budget at $3. A normal month stays far below $20, so the limit acts only if the $3 stop fails, for example an action error or a cost that denying the scheduler does not stop.
   - Its pause stops everything, and its 90-day deletion clock then runs on the bucket. After a pause, reactivate within days, and copy the data out first (step 10.1).
   - In the sealed window (15 Dec – 14 Jan), D-035 recommends turning the hard stop off (the owner has not decided). If it is off, the limit's full pause would be the worse outcome, so keep the limit at the floor or remove it there.

### 10. Destroy

1. Copy the data out. The first 100 GB a month of egress is free ([S3 pricing](https://aws.amazon.com/s3/pricing/)):
   ```bash
   aws s3 sync "s3://$(terraform -chdir=infra/terraform output -raw data_bucket)" ~/lookout-data/s3-export/   # the data dir (D-080)
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

**These are estimates.** Nothing has been deployed, so nothing below was observed on AWS. This is the project's hosting estimate (PRD §17 and D-035 cite it). Updated 21 Nov 2026 for the code at commit `8fa6b6a` (capture 0.3.0); each input says where it comes from:

- **Prices** are for us-east-1, before tax, from AWS's public price-list files published 12–18 Nov 2026 and the pricing pages linked below, read on 19 Nov 2026 (not re-read since). Where AWS charges sales tax, the bill is higher by its rate, and so is what the budget counts ([Hard stop at the cap](#hard-stop-at-the-cap)).
- **Run counts** follow from the schedules: capture 8,640 runs a month and forecast 8,640 (both every 5 minutes, PRD §11.2), 17,280 in all.
- **Capture is simulated**: the committed capture code, one Lambda-style pass every 5 minutes, against a mock S3 bucket (moto) and the capture tests' fake NPS feeds changing at the measured cadences (Rainier about once a minute, the ARD cameras every 15 minutes).
  - **Each NPS request takes a measured time** on the simulation's clock. It is drawn, for its stage and connection state, from the 1,340 answered requests the live loop (capture 0.2.1) made from 20:01 to 07:00 UTC on 20–21 Nov (`cost/nps_request_times_0.2.1.json`, written by `cost/nps_request_times.py`). A request counts as cold (a new connection) when it is a pass's first, when it comes more than 5 s after the previous answer, or when it follows a failed request, whose connection httpcore closes.
  - **6 runs, seeds 1–6, pooled.** One run is one draw of the request times, and its waiting moves with the draw, so the figures below pool six runs that differ only in the seed (`cost/sim_pool.py`). Each run warms up for 7 simulated days and counts the next 3, because capture's state documents only reach their steady size after about 2.5 days (below).
  - Stores reached 748 a day of the 768 the cap allows.
  - The simulation is `cost/sim_capture_s3.py`. Its pooled output for `8fa6b6a` is `cost/sim_capture_8fa6b6a.json`, which keeps each run's own output under `replicates`, and `cost/estimate.py` turns that into every figure below. `tests/test_cost.sh` checks that this section quotes them, and runs a short simulation with the script's self-checks.
  - **Requests.** One pass makes 50–225 S3 requests, 88 on average. Per day: 11,195 PUT, 174 LIST, 9,929 GET, 705 HEAD and 3,426 DELETE (DELETEs are free).
  - **Waiting, which Lambda bills.** A pass waits 35.88 s on the simulation's clock on average over the 6 runs (each run's mean 34.90–37.15 s, median 37.0–39.9 s, 95th percentile 44.2–44.4 s; longest 53.3 s): 32.2 s for each camera's 5-minute slot and 3.6 s spacing requests 1 s apart. Its NPS requests take 2.23 s more, inside the 5–20 s assumed below.
  - **Why the waiting is this long.** Since 0.2.1 a camera is due 300 s plus 2 ms after its previous poll's *answer* (D-065), so each pass reaches it later, by that poll's request time. Its wait grows until it would pass 45 s (`wait_for_due_s`). The camera is then skipped once (3.85 `not_due` per camera per day; the runs, 3.21–4.25) and polled at the start of the next pass.
  - **Why the request times matter.** Without them, the same code waits 14.62 s a pass: the zero-latency figure D-066 set aside. Month 1 would then come to $2.48–3.19 (from a 24 h run that counted only the first day).
  - **S3 and CPU time are only added.** In the simulation S3 answers at once and the code's CPU work takes no time; the 5–20 s assumed below adds both as billed time. On Lambda they also delay each poll's send and answer, so they move the slots as request time does. With 20 ms for every S3 request (`--s3-latency-ms 20`, 1.76 s a pass; seeds 1–3, scratch runs, not committed), a pass waited 33.47 s on average (the runs: 30.92–35.08 s), against 35.64 s (34.90–36.19 s) for the same seeds without. The S3 time partly takes the place of waiting: a whole pass lasted 37.51 s against 37.87 s, so adding the assumed 5–20 s on top of the waiting errs high. Skips rose a little, to 3.96–4.33 a camera a day. CPU time is not simulated.
- **Forecast's S3 requests are counted from the code at `8fa6b6a`** (forecast 0.2.0: `process_pending`, `extract.run_covered`, `write_decisions`, `Extractor.extract` and `Extractor._write`). Running that code checks the count: `cost/count_forecast_s3.py` calls `process_pending` against the forecast tests' fake HRRR bucket with a storage that counts requests, and `tests/test_cost.sh` runs it when the forecast venv exists.
  - Every call reads the marker of each of the 13 runs in its 12 h lookback and HEADs each written run's 8 forecast files. For each run not yet written, it reads the marker again, HEADs the 8 files (`write_decisions`) and reads the run's failure record: 143 − 2 × W requests for W written runs. That is 119 with one run pending (W = 12), 121 while the previous run still waits for its last hours, and 117 if the newest run is complete within its own hour.
  - Each extracted run adds 11 PUTs (8 files, the marker, the log, the lock) and 19 GET-tier requests: `write_decisions` again under the lock (9), the marker re-read and 8 HEADs before the writes (9), and the log (1).
  - That is 90,000 more GET-tier requests a month than 8246a8b's code at the steady state, $0.04. D-060 and D-066 counted 110 a call and 2 an extraction.
- **Storage:**
  - frames and thumbnails come from D-028's measured sizes;
  - the image's size is measured;
  - forecast files at the deployed profile are extrapolated (below);
  - previous versions are simulated: what the counted days' overwrites and deletes left behind.
- **Assumed:**
  - each capture pass's own requests and CPU, 5–20 s on top of its waiting. Its NPS requests take 2.23 s at the laptop's measured times (Lambda's are unmeasured). A pass also makes about 88 S3 requests, and resizes and thumbnails its frames at 512 MB, about 0.29 vCPU;
  - forecast run times, and the CloudWatch log volume.

| Item | What drives it | Estimate per month | Source |
|---|---|---|---|
| EventBridge Scheduler | 17,280 runs. The free tier is 14,000,000 a month, then $1.00 per million. | **$0** | [EventBridge pricing](https://aws.amazon.com/eventbridge/pricing/) |
| Lambda requests | 17,280 at $0.20 per million | **$0.00** (about $0.003) | [Lambda pricing](https://aws.amazon.com/lambda/pricing/) |
| Lambda compute (Arm) | Capture 0.5 GB × 40.9–55.9 s × 8,640 (35.88 s of simulated waiting plus 5–20 s assumed): 176,602–241,402 GB-s. Forecast 2 GB: 7,920 calls that find nothing new (1–5 s; each still makes about 119 S3 requests) and 720 that extract an HRRR run (30–150 s): 59,040–295,200 GB-s. In all 235,642–536,602 GB-s, at $0.0000133334 per GB-s. The forecast durations are assumptions: the emulator runs on the laptop used a tiny profile and a 1 h lookback ([The Lambda image](#the-lambda-image)). | **$0–1.82** with the free tier (1 M requests and 400,000 GB-s a month; the AWS page sets no plan condition): the top of the range is 136,602 GB-s over it. Without it, $3.14–7.15. | [Lambda pricing](https://aws.amazon.com/lambda/pricing/) |
| Lambda `/tmp` above 512 MB | Forecast: 1.5 GB extra at $0.0000000309 per GB-s | < $0.01 | [Lambda pricing](https://aws.amazon.com/lambda/pricing/) |
| S3 storage, current objects | 317–331 MB a day, growing, at $0.023 per GB. Frames, thumbnails and sidecars: 262 MB (D-028, measured sizes, 4 downloads an hour per camera). Forecast files: 52–65 MB (below). Logs: about 3.7 MB (capture 2.72 MB in the simulation; forecast about 1 MB, assumed). That is 4.8–5.0 GB on average in month 1, 14.3–14.9 GB in month 2, 23.8–24.8 GB in month 3, and 28.6–29.8 GB on day 90. | **$0.11** (month 1), **$0.33–0.34** (month 2), **$0.55–0.57** (month 3), then $0.22–0.23 more each month | [S3 pricing](https://aws.amazon.com/s3/pricing/) |
| S3 storage, previous versions | Versioning keeps what each overwrite or delete replaced (simulated days 8–10 of each run): 391 MB of old `capture_log/` versions a day (one rewrite per pass), kept 1–2 days; 302 MB of old state documents a day, kept 30 days (9.1 GB); about 305 MB a day of deleted staged frames (D-028's every-frame sizes at the six hourly cameras), kept 1–2 days. That is 9.8–10.8 GB once steady, from about day 30. | **$0.11–0.12** (month 1), **$0.22–0.25** after | [S3 pricing](https://aws.amazon.com/s3/pricing/) |
| S3 requests | Capture (simulated): 341,078 PUT and LIST and 319,023 GET and HEAD, $1.83. Its PUTs a month: state documents 159,745 ($0.80), locks 69,120 ($0.35), thumbnails 44,857 ($0.22), staged copies 33,660 ($0.17), frames 19,837 ($0.10), logs 8,640 ($0.04), listings 5,220 ($0.03). Forecast (counted at `8fa6b6a`): 7,920 PUT and 1,024,560–1,059,120 GET and HEAD, $0.45–0.46; the steady state's lookback HEADs alone are 829,440 ($0.33). PUT and LIST are $0.005 per 1,000; GET and HEAD $0.0004 per 1,000. DELETE and lifecycle expirations are free. | **$2.28–2.30** | [S3 pricing](https://aws.amazon.com/s3/pricing/) |
| S3 ↔ Lambda, NOAA → Lambda transfer | Same region | **$0** | [S3 pricing](https://aws.amazon.com/s3/pricing/) |
| ECR | At most 5 images (the lifecycle rule) of 293.9 MB each: layers, config and manifest of `lookout-lambda:8246a8bd570b`, measured on 20 Nov. That is 1.47 GB at $0.10 per GB, $0.15. All five share the pinned AWS base (200.3 MB) until the pin is bumped. If ECR stores a shared layer once per repository (not checked offline), that is 0.67 GB, $0.07. AWS's page (19 Nov) gives new customers 500 MB a month free for 12 months; the totals leave it out. ECR → Lambda in the same region is free. | **$0.07–0.15** | [ECR pricing](https://aws.amazon.com/ecr/pricing/) |
| CloudWatch Logs | 17–86 MB a month (1–5 KB per run, assumed). Ingestion is $0.50 per GB, storage $0.03 per GB, 30-day retention. Free: 5 GB a month. | **$0** (≤ $0.04 without the free tier) | [CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/) |
| CloudWatch alarms | 4 standard alarms at $0.10 each. Free: 10 alarms. | **$0** ($0.40 without the free tier) | [CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/) |
| SNS email | A handful of alarm emails. The first 1,000 a month are free. | **$0** | AWS price list (AmazonSNS) |
| AWS Budgets | One budget with one action. Monitoring and notifications are free, and "your first two action-enabled budgets are free". | **$0** | [Budgets pricing](https://aws.amazon.com/aws-cost-management/aws-budgets/pricing/) |
| IAM | Roles and policies | **$0** | |
| **Total** | | With the free tiers: **$2.57–4.51** in month 1, **$2.90–4.86** in month 2, **$3.12–5.09** in month 3, then about $0.22 more each month. The bottom of the range passes $3 in month 3, the top from month 1. With none: $6.14–10.31 in month 1. Priced from each of the 6 runs alone, month 1 is $2.57–2.58 at the bottom and $4.46–4.58 at the top, as the waiting moves with the draw (the runs: 34.90–37.15 s). | |

**Which capture code this costs.** Capture 0.3.0 at `8fa6b6a`, HEAD on 21 Nov: 0.2.1's answer-time spacing (D-065), with a reservation write before each request (D-066), under the new names (D-080).
- **Its S3 calls are 0.2.1's.** With no request times, the simulation counts 11,048 PUT and 10,176 GET a day, D-066's figures for 0.2.1.
- **Against D-066** ($2.43–4.40 in month 1 with the free tiers, at about 36 s of waiting from the capture package's latency model): **$2.57–4.51**. The waiting is about D-066's (35.88 s a pass here), so compute is about the same. With D-066's other inputs (the first simulated day's S3 counts and old versions, and forecast counted at `8246a8b`), this waiting gives D-066's $2.43–4.40 to the cent. What moves it, in month 1:
  - **Old state versions**, $0.07 (about $0.14 a month once steady). A camera's state keeps the hashes of its last 240 stored images (`RECENT_SHA_LIMIT`), and that list is full only after about 2.5 days. So the overwritten versions average 57 KB on days 8–10, against 18 KB on day 1, and they come to 302 MB a day instead of 97 MB. D-066, like every earlier estimate here, took its S3 counts and old versions from the first simulated day.
  - **The steady state's request counts**, $0.02.
  - **Forecast 0.2.0's extra requests** (above), $0.05 at the bottom and $0.02 at the top.
- **One run is one draw.** The six runs' waiting spans 34.90–37.15 s, so one run alone would put month 1's top anywhere in $4.46–4.58. The totals use the pooled waiting.
- **The request times are the laptop's**, over a home connection, at Lambda's share of cold connections (20.7 % of the counted requests, a pass's first one among them). Lambda's own times to nps.gov are unmeasured until deployment.
  - Shorter ones would not make this cheaper. In sensitivity runs with every request a quarter as long (seeds 1–3; scratch, not committed), cameras skipped 0.75–0.92 times a day, against 3.67–4.17 for the same seeds at full length.
  - A pass then waited 38.01 s (the runs: 37.18–38.75 s), against 35.64 s (34.90–36.19 s), and month 1 came to $2.56–4.62, against $2.57–4.50.
- **The image figures are still those of `lookout-lambda:8246a8bd570b`.** The rename changed names, not dependencies, and no image was built at `8fa6b6a` for this estimate.
- **Re-run** the simulation (once per seed), `cost/sim_pool.py` and `cost/estimate.py` on the commit you deploy, and `cost/count_forecast_s3.py` for forecast.

**Forecast files at the deployed profile.** Terraform sets no `SNOW_GLOBE_FORECAST_*` variable, so the deployed forecast job extracts all 22 fields and every hour. That is 453 MB of GRIB for an 18 h run and 1,202 MB for a 48 h run (workers/forecast/README.md, measured), which is free in-region. No file of that profile has been written yet. The estimate is extrapolated from the layout of a measured light file: 31.3 KB on average over 249 local files for 8 fields × 3 hours × 57 points. That file is about 5.7 B per value, 0.7 KB plus 0.1 KB per field for each hour, 0.65 KB per field and 13.8 KB fixed. At 5.7–8 B per value, a viewpoint's file would be 219–274 KB per 18 h run and 520–662 KB per 48 h run, 52–65 MB a day for 8 viewpoints. The local stand-in's light profile writes 6 MB a day.

**D-028's storage check.** 23.6 GB × $0.023 = $0.54 a month is right as the rate on day 90 for frames and thumbnails alone. Those cost less in the months before: $0.09, $0.27 and $0.45 on average. With forecast files, logs and previous versions, the storage rate on day 90 is $0.88–0.93.

How this compares with the PRD:

- **With the free tiers**, the total is above PRD §17's "≈ $0–1 a month" from the first month, and the range straddles its $3 cap from month 1.
  - The bottom stays under the cap until month 3, as storage grows by 9.5–9.9 GB a month and adds $0.22–0.23 to each month's bill.
  - The top, $4.51, is over it in month 1: the free 400,000 GB-s run out on day 22, spend passes $3 on about day 25 (`cost/estimate.py`; day 25.2–25.5 across the runs), and the hard stop then halts the schedules for the rest of the month. The staged frames are kept (`cache/` expires after 35 days).
  - The 67 % email (about $2) would go out in month 1 at any point of the range.
  - These totals are before tax, and the budget counts tax. At a sales tax of 8 %, for example, month 2's bottom of $2.90 would reach the budget as $3.13, so the bottom would cross the cap a month sooner.
- **The range is wide because two assumptions are**: forecast run time (720 extractions × 2 GB × 30–150 s is 43,200–216,000 GB-s) and capture's own request and CPU time (21,600–86,400 GB-s). The simulated waiting is not small: 155,002 GB-s a month (0.5 GB × 35.88 s × 8,640), 39 % of the free tier.
- **S3 requests are the largest line at the bottom of the range, $2.28–2.30**; at the top, Lambda compute adds $1.82. The levers belong to the workers' owners, except the last:
  - **Capture's state and lock writes.** Capture takes a lock at every poll, and writes the camera's state document before each request (the reservation, D-065) and after it, unchanged ones included: 7,629 state and lock PUTs a day, $1.14 a month. D-066's lever, letting the takeover of an expired camera lock signal the crash, would drop the poll reservation: 2,273 PUTs a day, $0.34 a month.
  - **Forecast's lookback.** It re-checks every written run of its 12 h lookback with a HEAD per viewpoint every 5 minutes, $0.33 a month. Trusting the marker, which already lists each viewpoint's file, would remove that. Since 0.2.0 it also re-reads the marker and HEADs the 8 files of every run not yet written, every call, and twice more for each extraction: $0.04 a month.
  - **Old state versions** are kept 30 days: 9.1 GB once steady, about $0.21 a month. A shorter noncurrent rule for `state/` (infra's own) would cut it, for instance to $0.05 at 7 days, at the cost of a shorter undo window for state documents.
- **Without the Lambda free tier**, the whole range passes the $3 cap, so the hard stop would halt the schedules mid-month. The drivers are capture's billed waiting and forecast compute (2 GB × run time).
- **Measure before trusting either number.** Run durations come from step 7, and S3 request counts from S3 Storage Lens or the bill, after the first day.
- **The budget** is $3 a month with the hard stop on, counted before credits and with tax ([Hard stop at the cap](#hard-stop-at-the-cap)). It covers the whole account, so keep the account for Snow Globe only, or set `budget_scope_to_project_tag`.

## Variables

| Variable | Default | Notes |
|---|---|---|
| `alarm_email` | none (required) | `TF_VAR_alarm_email`; marked sensitive |
| `image_tag` | null | Required for phase 2; the functions refuse to plan without it |
| `region` | `us-east-1` | Set it with `TF_VAR_region` (step 3) |
| `monthly_budget_usd` | 3 | PRD §17's hosting hard cap |
| `budget_alert_thresholds_percent` | `[33, 67, 100]` | Actual spend; 1 to 4 values. A forecast alert at 100 % is always added. |
| `budget_hard_stop` | true | At 100 % of actual spend the schedules stop ([Hard stop](#hard-stop-at-the-cap)). false: emails only |
| `budget_scope_to_project_tag` | false | true counts only `project=snow-globe` costs. Activate that cost-allocation tag first. |
| `capture_schedule_expression` | `rate(5 minutes)` | Only `rate(N minutes)` with 5 ≤ N ≤ 60, or `rate(1 hour)` (PRD §15.3) |
| `forecast_schedule_expression` | `rate(5 minutes)` | PRD §11.2. `rate(1 minute)` to `rate(1 hour)` |
| `capture_schedule_enabled`, `forecast_schedule_enabled` | true | false pauses a job and silences its `not-running` alarm |
| `user_agent` | `SnowGlobe/0.1 (webcam research capture; fetch-on-change)` | Must not contain an email address |
| `data_bucket_name` | `snow-globe-data-<account>` | |
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
- **Budget.** Credits and refunds are excluded, so the budget sees costs before credits. The account's credits ($100 at sign-up, and the AWS Activate credits applied for on 21 Nov) would otherwise hide every cent of spend for as long as they last. The hard stop therefore acts on gross spend too, even while credits pay the bill ([Hard stop at the cap](#hard-stop-at-the-cap)).
- **Evidence is recoverable.** The data bucket is versioned and no worker role can delete a version. A buggy overwrite of a frame, forecast file or state document can be undone for 30 days, and of a log for 1 day: `aws s3api list-object-versions --bucket <data bucket> --prefix <key>`, then copy the old version back.
- **Takedowns delete every version.** PRD §15.5 removes a frame from storage within 7 days. On a versioned bucket a plain delete keeps the previous version for 30 days, so delete each version as the owner: `aws s3api list-object-versions` for the key, then `aws s3api delete-object --version-id <id>` for each one, delete markers included.

## The Lambda image

One arm64 image runs both jobs (PRD §11.3). Terraform picks each function's handler with `image_config.command`. The image's default command is the forecast handler, so a local `docker run` that forgets the command reads NOAA's public bucket and never becomes a second NPS fetcher.

**Build** with `infra/lambda/build.sh`. It loads `snow-globe-lambda:<tag>` into the local Docker; `--push REPO` pushes instead (step 6b.4). The build imports both handlers and fails if a module or a native library cannot load. It also fails if boto3 or botocore would come from the base image's own copy (1.43.127) instead of the locked one (1.44.6).

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

**Smoke test:** `infra/lambda/smoke.sh snow-globe-lambda:<tag>`; `--offline` runs step 1 only. It builds, pushes and deploys nothing. It prints `key=value` results and the full package list, and exits 1 if a check fails.

1. **Offline probe.** First, the image's default command must not be the capture handler. The probe then runs with no network, a read-only root file system and a non-root user. `smoke_probe.py` puts `sys.path` in the runtime's order and resolves both handler strings with the runtime's own loader, as Lambda does at init. It checks that the pinned packages and `libeccodes.so` load from `/var/task`. It also builds what the capture handler builds before its first request: the 8 manifests, S3 storage for a made-up bucket, and the HTTP client with the project User-Agent. It uses none of them.
2. **Forecast invoke.** The forecast handler runs under the Runtime Interface Emulator that ships in the AWS base image, and is invoked twice. Storage is `SNOW_GLOBE_STORAGE=local`, in a new temporary directory that the script deletes. The profile is tiny: `VIS:surface`, forecast hour 0, 10 MB a run, 1 h lookback. The container gets 2048 MB with no swap, 1.16 vCPU (Lambda gives one vCPU per 1,769 MB), a 2 GB `/tmp`, a read-only root and a non-root user. Checks:
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
- the cost estimate (`tests/test_cost.sh`): `cost/estimate.py` on the committed simulation output, checks on its arithmetic, a check that the simulation drew its request times from the committed measurements (by SHA-256), a check that it pools at least five seeds and that `cost/sim_pool.py` pools them into exactly that file, a test of `cost/nps_request_times.py` on a made-up log, forecast's S3 requests counted by running its code (`cost/count_forecast_s3.py`; skips without `workers/forecast`'s venv, as in CI's infra job), two short self-checked runs of `cost/sim_capture_s3.py` on this tree (skips without a venv that has moto; `make test-infra` syncs `workers/common`'s), and a check that [Monthly cost estimate](#monthly-cost-estimate) quotes its figures; then ruff on `cost/` (`cost/ruff.toml`, no cache written), which skips when ruff is not installed;
- tflint, when installed;
- the rename's live switch (`tests/test_migrate.sh`): `ops/migrate-to-snow-globe.sh` on a scratch HOME against stub loops, the gate refusal, the dry run, the full switch and its rollbacks. It skips off macOS.

**Network.** The only network step is installing the AWS provider (`hashicorp/aws` 6.73.0, about 760 MB unpacked) into `TF_PLUGIN_CACHE_DIR`, by default `<data dir>/cache/terraform-plugins` (`tfp_cache_dir`; `~/lookout-data` until the rename's live switch, D-080), outside iCloud.

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
