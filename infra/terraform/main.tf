data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  tags = {
    project    = var.project
    managed_by = "terraform"
  }

  data_bucket     = var.data_bucket_name != "" ? var.data_bucket_name : "${var.project}-data-${local.account_id}"
  data_bucket_arn = "arn:${local.partition}:s3:::${local.data_bucket}"

  # Fixed rather than tunable. PRD §15.2 promises 30-day log retention. cache/ holds only deletable files (CLAUDE.md
  # data layout): in the bucket, capture's staged frames, which it deletes itself once their hour is decided
  # (storage.tf). Its expiry is a backstop that outlasts the longest hard stop (one budget month, monitoring.tf), so a
  # stopped capture job finds its undecided hours' frames when it runs again. ECR keeps the last 5 images.
  log_retention_days    = 30
  cache_expiration_days = 35
  ecr_keep_images       = 5

  # The daily JSONL logs in the data bucket (CLAUDE.md data layout); storage.tf expires their old versions sooner.
  log_prefixes = ["capture_log/", "forecast_log/"]

  # Paths inside the Lambda image (infra/lambda/Dockerfile). /tmp is the only writable path in Lambda.
  manifest_dir = "/var/task/viewpoints/manifests"
  scratch_dir  = "/tmp/snow-globe-data"

  # One entry per scheduled job (PRD §11.2). Both jobs run from the same image, and Terraform sets each one's
  # handler as the image CMD.
  #
  # Data-bucket access, as IAM key patterns (CLAUDE.md data layout). A job reads and writes only its own outputs,
  # its own state documents (state/<job>/) and its own locks (state/locks/<job>..., the snow_globe_common lock
  # names "<job>/..." or "<job>-..."). So neither job can overwrite or delete the other's state: the capture
  # job's NPS rate-limit history stays out of the forecast job's reach.
  #
  # Capture (lookout-capture 0.2.0, before D-080; PRD §8.7 as decided in D-028) also writes a 640 px thumbnail of every download
  # to thumbs/, and stages each download of the six hourly cameras under cache/staging/frames/ until its hour is
  # decided; the nearest frame is then copied to frames/ and every staged copy deleted (workers/capture/README.md,
  # "IAM"; snow_globe_capture.retention). Forecast keeps its GRIB subsets on the function's local disk
  # (snow_globe_common.paths.cache_dir(), /tmp on Lambda), never in the bucket, so it has no cache/ grant.
  #
  # s3_delete is narrower still: releasing a lock and removing capture's own staged copies are the only deletes a
  # worker makes (snow_globe_common.storage has no delete method; retention.delete_staged refuses any key outside
  # cache/staging/). No role may delete a frame, thumbnail, forecast or log, none has s3:DeleteObjectVersion, and
  # the bucket is versioned (storage.tf), so an overwrite is recoverable for var.data_noncurrent_version_days.
  jobs = {
    capture = {
      description          = "Snow Globe capture: polls NPS camera feeds, stores changed frames (PRD 11.2, 15.3)"
      handler              = "snow_globe_capture.lambda_handler.handler"
      timeout_s            = 120
      memory_mb            = 512
      ephemeral_storage_mb = 512
      schedule_expression  = var.capture_schedule_expression
      schedule_enabled     = var.capture_schedule_enabled
      s3_read_write        = ["frames/*", "thumbs/*", "cache/staging/*", "capture_log/*", "state/capture/*", "state/locks/capture*"]
      s3_delete            = ["cache/staging/*", "state/locks/capture*"]
    }
    forecast = {
      description          = "Snow Globe forecast: HRRR byte-range extraction per viewpoint, availability lag (PRD 8.3, 11.2)"
      handler              = "snow_globe_forecast.lambda_handler.handler"
      timeout_s            = 600
      memory_mb            = 2048
      ephemeral_storage_mb = 2048
      schedule_expression  = var.forecast_schedule_expression
      schedule_enabled     = var.forecast_schedule_enabled
      s3_read_write        = ["forecast/*", "forecast_log/*", "state/forecast/*", "state/locks/forecast*"]
      s3_delete            = ["state/locks/forecast*"]
    }
  }

  function_name  = { for name, _ in local.jobs : name => "${var.project}-${name}" }
  function_arn   = { for name, fn in local.function_name : name => "arn:${local.partition}:lambda:${var.region}:${local.account_id}:function:${fn}" }
  log_group_name = { for name, fn in local.function_name : name => "/aws/lambda/${fn}" }

  # Every job function, with and without a version or alias qualifier.
  job_invoke_arns = flatten([for name in sort(keys(local.jobs)) : [local.function_arn[name], "${local.function_arn[name]}:*"]])

  # Schedule interval in seconds, parsed from the rate() expressions that the variable validations allow.
  schedule_interval_s = {
    for name, job in local.jobs : name => (
      job.schedule_expression == "rate(1 hour)" ? 3600 : (
        job.schedule_expression == "rate(1 minute)" ? 60 :
        tonumber(regex("^rate\\(([0-9]+) minutes\\)$", job.schedule_expression)[0]) * 60
      )
    )
  }

  # A scheduled event that cannot start within one interval is dropped, not queued: the next tick replaces it.
  # Neither Lambda nor the Scheduler retries a failed run; the next tick retries, which keeps the NPS poll rate
  # at or below the schedule (PRD §15.3: back off on errors, never poll faster than every 5 minutes).
  max_event_age_s = { for name, s in local.schedule_interval_s : name => max(60, s - 60) }

  # Alarm windows follow the schedule: errors in 2 of 3 windows, or no invocation in 2 windows in a row.
  error_alarm_period_s   = { for name, s in local.schedule_interval_s : name => max(900, s) }
  silence_alarm_period_s = { for name, s in local.schedule_interval_s : name => max(900, 3 * s) }

  # ---- IAM policy documents (jsonencode keeps them deterministic, so tests/*.tftest.hcl can read them) ----

  lambda_assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  job_policy = {
    for name, job in local.jobs : name => jsonencode({
      Version = "2012-10-17"
      Statement = concat(
        [
          {
            Sid      = "WriteOwnLogs"
            Effect   = "Allow"
            Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
            Resource = "arn:${local.partition}:logs:${var.region}:${local.account_id}:log-group:${local.log_group_name[name]}:*"
          },
          {
            Sid      = "ListDataBucket"
            Effect   = "Allow"
            Action   = ["s3:ListBucket"]
            Resource = local.data_bucket_arn
          },
          {
            Sid      = "ReadWriteOwnKeys"
            Effect   = "Allow"
            Action   = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"]
            Resource = [for p in job.s3_read_write : "${local.data_bucket_arn}/${p}"]
          },
        ],
        [
          for _ in range(length(job.s3_delete) > 0 ? 1 : 0) : {
            Sid      = "DeleteOwnLocksAndScratch"
            Effect   = "Allow"
            Action   = ["s3:DeleteObject"]
            Resource = [for p in job.s3_delete : "${local.data_bucket_arn}/${p}"]
          }
        ],
      )
    })
  }

  scheduler_assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })

  scheduler_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "InvokeSnowGlobeJobs"
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = local.job_invoke_arns
    }]
  })

  # PRD §17 hosting hard cap (monitoring.tf): what AWS Budgets attaches to the scheduler role at 100 % of the
  # monthly budget. An explicit Deny beats the scheduler's Allow, so every scheduled run stops.
  budget_hard_stop_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "BudgetHardStop"
      Effect   = "Deny"
      Action   = ["lambda:InvokeFunction"]
      Resource = local.job_invoke_arns
    }]
  })

  # Trust policy for the role AWS Budgets assumes to run that action, with the confused-deputy guard from the AWS
  # Budgets docs ("Allow AWS Budgets to apply IAM policies and SCPs and target EC2 and RDS instances").
  budget_action_assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "budgets.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:${local.partition}:budgets::${local.account_id}:budget/*" }
      }
    }]
  })
}
