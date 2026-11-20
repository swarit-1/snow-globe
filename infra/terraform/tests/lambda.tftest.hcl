# Plan-only tests against a mocked AWS provider: no credentials, no AWS calls, nothing created.
# Checks the two Lambda jobs against the M0.5 spec (PRD §11.2, §11.3) and their least-privilege roles.

mock_provider "aws" {
  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "123456789012" }
  }
  override_data {
    target = data.aws_partition.current
    values = { partition = "aws" }
  }
  # Computed attributes are unknown at plan time; pin the ones the assertions read.
  override_resource {
    target          = aws_ecr_repository.lambda
    override_during = plan
    values          = { repository_url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/lookout/lambda" }
  }
  override_resource {
    target          = aws_sns_topic.alarms
    override_during = plan
    values          = { arn = "arn:aws:sns:us-east-1:123456789012:lookout-alarms" }
  }
  # One ARN and id per role instance, so a test can tell which role each function and policy uses.
  override_resource {
    target          = aws_iam_role.job["capture"]
    override_during = plan
    values          = { arn = "arn:aws:iam::123456789012:role/lookout-capture-lambda", id = "lookout-capture-lambda" }
  }
  override_resource {
    target          = aws_iam_role.job["forecast"]
    override_during = plan
    values          = { arn = "arn:aws:iam::123456789012:role/lookout-forecast-lambda", id = "lookout-forecast-lambda" }
  }
  override_resource {
    target          = aws_iam_role.scheduler
    override_during = plan
    values          = { arn = "arn:aws:iam::123456789012:role/lookout-scheduler", id = "lookout-scheduler" }
  }
  override_resource {
    target          = aws_iam_role.budget_action
    override_during = plan
    values          = { arn = "arn:aws:iam::123456789012:role/lookout-budget-action", id = "lookout-budget-action" }
  }
  override_resource {
    target          = aws_iam_policy.budget_hard_stop
    override_during = plan
    values          = { arn = "arn:aws:iam::123456789012:policy/lookout-budget-hard-stop" }
  }
}

# Every variable is pinned to its default (alarm_email and image_tag, which have none, to test values), so the
# suite checks the shipped defaults whatever the shell exports (TF_VAR_*) or a *.auto.tfvars file in the module
# holds. infra/test.sh fails if a variable is missing here or a pin differs from its default.
variables {
  alarm_email                     = "alerts@example.com"
  image_tag                       = "0123456789ab"
  region                          = "us-east-1"
  project                         = "lookout"
  user_agent                      = "Lookout/0.1 (webcam research capture; fetch-on-change)"
  capture_schedule_expression     = "rate(5 minutes)"
  forecast_schedule_expression    = "rate(5 minutes)"
  capture_schedule_enabled        = true
  forecast_schedule_enabled       = true
  monthly_budget_usd              = 3
  budget_alert_thresholds_percent = [33, 67, 100]
  budget_hard_stop                = true
  budget_scope_to_project_tag     = false
  data_bucket_name                = ""
  data_bucket_force_destroy       = false
  data_noncurrent_version_days    = 30
  log_noncurrent_version_days     = 1
}

run "capture_function_matches_spec" {
  command = plan

  assert {
    condition     = aws_lambda_function.job["capture"].function_name == "lookout-capture"
    error_message = "capture function name"
  }
  assert {
    condition     = aws_lambda_function.job["capture"].package_type == "Image"
    error_message = "capture must be a container-image function"
  }
  assert {
    condition     = aws_lambda_function.job["capture"].architectures == tolist(["arm64"])
    error_message = "capture must run on arm64"
  }
  assert {
    condition     = aws_lambda_function.job["capture"].image_config[0].command == tolist(["lookout_capture.lambda_handler.handler"])
    error_message = "capture handler"
  }
  assert {
    condition     = aws_lambda_function.job["capture"].timeout == 120
    error_message = "capture timeout must be 120 s"
  }
  assert {
    condition     = aws_lambda_function.job["capture"].memory_size == 512
    error_message = "capture memory must be 512 MB"
  }
  assert {
    condition     = aws_lambda_function.job["capture"].reserved_concurrent_executions == 1
    error_message = "capture must be limited to one instance (never two NPS fetchers at once)"
  }
  assert {
    condition     = aws_lambda_function.job["capture"].ephemeral_storage[0].size == 512
    error_message = "capture ephemeral storage should stay at the free 512 MB"
  }
}

run "forecast_function_matches_spec" {
  command = plan

  assert {
    condition     = aws_lambda_function.job["forecast"].function_name == "lookout-forecast"
    error_message = "forecast function name"
  }
  assert {
    condition     = aws_lambda_function.job["forecast"].package_type == "Image" && aws_lambda_function.job["forecast"].architectures == tolist(["arm64"])
    error_message = "forecast must be an arm64 container-image function"
  }
  assert {
    condition     = aws_lambda_function.job["forecast"].image_config[0].command == tolist(["lookout_forecast.lambda_handler.handler"])
    error_message = "forecast handler"
  }
  assert {
    condition     = aws_lambda_function.job["forecast"].timeout == 600
    error_message = "forecast timeout must be 600 s"
  }
  assert {
    condition     = aws_lambda_function.job["forecast"].memory_size == 2048
    error_message = "forecast memory must be 2048 MB"
  }
  assert {
    condition     = aws_lambda_function.job["forecast"].ephemeral_storage[0].size == 2048
    error_message = "forecast ephemeral storage must be 2 GB"
  }
  assert {
    condition     = aws_lambda_function.job["forecast"].reserved_concurrent_executions == 1
    error_message = "forecast must be limited to one instance"
  }
}

run "both_functions_share_one_image_and_environment" {
  command = plan

  assert {
    condition     = length(aws_lambda_function.job) == 2
    error_message = "exactly two jobs in M0.5"
  }
  assert {
    condition     = aws_lambda_function.job["capture"].image_uri == aws_lambda_function.job["forecast"].image_uri
    error_message = "both functions must run the same image"
  }
  assert {
    condition     = endswith(aws_lambda_function.job["capture"].image_uri, ":0123456789ab")
    error_message = "image_uri must use var.image_tag"
  }
  assert {
    condition = alltrue([for name in ["capture", "forecast"] : aws_lambda_function.job[name].environment[0].variables == tomap({
      LOOKOUT_STORAGE      = "s3"
      LOOKOUT_S3_BUCKET    = "lookout-data-123456789012"
      LOOKOUT_MANIFEST_DIR = "/var/task/viewpoints/manifests"
      LOOKOUT_USER_AGENT   = "Lookout/0.1 (webcam research capture; fetch-on-change)"
      LOOKOUT_DATA_DIR     = "/tmp/lookout-data"
    })])
    error_message = "environment variables differ from the spec"
  }
  assert {
    condition     = alltrue([for name in ["capture", "forecast"] : aws_lambda_function.job[name].logging_config[0].log_group == "/aws/lambda/lookout-${name}"])
    error_message = "each function must log to its own managed log group"
  }
}

# A function wired to the other job's role would get that job's bucket access.
run "each_function_runs_with_its_own_role" {
  command = plan

  assert {
    condition     = aws_lambda_function.job["capture"].role == "arn:aws:iam::123456789012:role/lookout-capture-lambda"
    error_message = "capture must run with the capture role"
  }
  assert {
    condition     = aws_lambda_function.job["forecast"].role == "arn:aws:iam::123456789012:role/lookout-forecast-lambda"
    error_message = "forecast must run with the forecast role"
  }
  assert {
    condition     = alltrue([for name in ["capture", "forecast"] : aws_iam_role.job[name].name == "lookout-${name}-lambda" && aws_iam_role_policy.job[name].role == "lookout-${name}-lambda"])
    error_message = "each job's policy must be attached to that job's role"
  }
}

run "log_groups_keep_30_days" {
  command = plan

  assert {
    condition     = alltrue([for g in aws_cloudwatch_log_group.job : g.retention_in_days == 30])
    error_message = "PRD §15.2 promises 30-day log retention"
  }
  assert {
    condition     = toset([for g in aws_cloudwatch_log_group.job : g.name]) == toset(["/aws/lambda/lookout-capture", "/aws/lambda/lookout-forecast"])
    error_message = "log group names"
  }
}

run "scheduled_runs_are_not_retried" {
  command = plan

  assert {
    condition     = alltrue([for c in aws_lambda_function_event_invoke_config.job : c.maximum_retry_attempts == 0])
    error_message = "a failed run must not be retried (the next tick retries; polite collection)"
  }
  assert {
    condition     = aws_lambda_function_event_invoke_config.job["capture"].maximum_event_age_in_seconds == 240 && aws_lambda_function_event_invoke_config.job["forecast"].maximum_event_age_in_seconds == 240
    error_message = "events older than one 5-minute interval minus 60 s are dropped"
  }
}

run "roles_are_least_privilege" {
  command = plan

  # Only these actions, so no role can delete an object version, change the bucket or create log groups.
  assert {
    condition = alltrue(flatten([
      for p in aws_iam_role_policy.job : [
        for s in jsondecode(p.policy).Statement : [
          for a in flatten([s.Action]) : contains([
            "logs:CreateLogStream", "logs:PutLogEvents", "s3:ListBucket",
            "s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:DeleteObject",
          ], a)
        ]
      ]
    ]))
    error_message = "job policies may only use the listed actions (no wildcards, no s3:DeleteObjectVersion, no logs:CreateLogGroup)"
  }
  # The bucket itself or a key in it: a bare prefix match would also accept another bucket whose name starts with
  # this one's (lookout-data-123456789012-other).
  assert {
    condition = alltrue(flatten([
      for name, p in aws_iam_role_policy.job : [
        for s in jsondecode(p.policy).Statement : [
          for r in flatten([s.Resource]) :
          r == "arn:aws:s3:::lookout-data-123456789012" || startswith(r, "arn:aws:s3:::lookout-data-123456789012/") || r == "arn:aws:logs:us-east-1:123456789012:log-group:/aws/lambda/lookout-${name}:*"
        ]
      ]
    ]))
    error_message = "job policies may only touch the data bucket and their own log group"
  }
  assert {
    condition     = alltrue([for p in aws_iam_role_policy.job : alltrue([for s in jsondecode(p.policy).Statement : s.Effect == "Allow"])])
    error_message = "unexpected statement effect"
  }
  # Exactly these four statements, each only Sid, Effect, Action and Resource: no NotAction or NotResource (which
  # grant everything they do not name), no Principal, no Condition. each_job_owns_its_keys pins what they grant.
  assert {
    condition = alltrue([
      for p in aws_iam_role_policy.job : tolist([for s in jsondecode(p.policy).Statement : s.Sid]) == tolist(["WriteOwnLogs", "ListDataBucket", "ReadWriteOwnKeys", "DeleteOwnLocksAndScratch"])
    ])
    error_message = "each job policy must be exactly WriteOwnLogs, ListDataBucket, ReadWriteOwnKeys and DeleteOwnLocksAndScratch"
  }
  assert {
    condition = alltrue(flatten([
      for p in aws_iam_role_policy.job : [for s in jsondecode(p.policy).Statement : sort(keys(s)) == tolist(["Action", "Effect", "Resource", "Sid"])]
    ]))
    error_message = "a job policy statement may hold only Sid, Effect, Action and Resource"
  }
}

run "each_job_owns_its_keys" {
  command = plan

  # The whole grant, action by action: for each action a job may use, the resources of every statement that holds
  # it, whatever the statement's Sid, are exactly these. So a statement added beside the four (s3:PutObject on
  # archive/*, s3:GetObject on every key) fails here, not only an edit to local.jobs.
  assert {
    condition = alltrue(flatten([
      for want in [
        {
          actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
          resources = ["arn:aws:logs:us-east-1:123456789012:log-group:/aws/lambda/lookout-capture:*"]
        },
        {
          actions   = ["s3:ListBucket"]
          resources = ["arn:aws:s3:::lookout-data-123456789012"]
        },
        {
          actions = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"]
          resources = [
            "arn:aws:s3:::lookout-data-123456789012/frames/*",
            "arn:aws:s3:::lookout-data-123456789012/thumbs/*",
            "arn:aws:s3:::lookout-data-123456789012/cache/staging/*",
            "arn:aws:s3:::lookout-data-123456789012/capture_log/*",
            "arn:aws:s3:::lookout-data-123456789012/state/capture/*",
            "arn:aws:s3:::lookout-data-123456789012/state/locks/capture*",
          ]
        },
        {
          actions = ["s3:DeleteObject"]
          resources = [
            "arn:aws:s3:::lookout-data-123456789012/cache/staging/*",
            "arn:aws:s3:::lookout-data-123456789012/state/locks/capture*",
          ]
        },
        ] : [
        for a in want.actions : toset(flatten([
          for s in jsondecode(aws_iam_role_policy.job["capture"].policy).Statement : flatten([s.Resource]) if contains(flatten([s.Action]), a)
        ])) == toset(want.resources)
      ]
    ]))
    error_message = "capture's grants differ from the spec (checked per action over every statement): ${aws_iam_role_policy.job["capture"].policy}"
  }
  assert {
    condition = alltrue(flatten([
      for want in [
        {
          actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
          resources = ["arn:aws:logs:us-east-1:123456789012:log-group:/aws/lambda/lookout-forecast:*"]
        },
        {
          actions   = ["s3:ListBucket"]
          resources = ["arn:aws:s3:::lookout-data-123456789012"]
        },
        {
          actions = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"]
          resources = [
            "arn:aws:s3:::lookout-data-123456789012/forecast/*",
            "arn:aws:s3:::lookout-data-123456789012/forecast_log/*",
            "arn:aws:s3:::lookout-data-123456789012/state/forecast/*",
            "arn:aws:s3:::lookout-data-123456789012/state/locks/forecast*",
          ]
        },
        {
          actions   = ["s3:DeleteObject"]
          resources = ["arn:aws:s3:::lookout-data-123456789012/state/locks/forecast*"]
        },
        ] : [
        for a in want.actions : toset(flatten([
          for s in jsondecode(aws_iam_role_policy.job["forecast"].policy).Statement : flatten([s.Resource]) if contains(flatten([s.Action]), a)
        ])) == toset(want.resources)
      ]
    ]))
    error_message = "forecast's grants differ from the spec (checked per action over every statement): ${aws_iam_role_policy.job["forecast"].policy}"
  }
  # Frames, thumbnails, forecasts and logs are evaluation evidence: a worker deletes only its own locks, and
  # capture also its own staged copies (D-028).
  assert {
    condition = toset(flatten([
      for s in jsondecode(aws_iam_role_policy.job["capture"].policy).Statement : flatten([s.Resource]) if contains(flatten([s.Action]), "s3:DeleteObject")
      ])) == toset([
      "arn:aws:s3:::lookout-data-123456789012/cache/staging/*",
      "arn:aws:s3:::lookout-data-123456789012/state/locks/capture*",
    ])
    error_message = "capture may delete only its staged copies and its own locks"
  }
  assert {
    condition = toset(flatten([
      for s in jsondecode(aws_iam_role_policy.job["forecast"].policy).Statement : flatten([s.Resource]) if contains(flatten([s.Action]), "s3:DeleteObject")
    ])) == toset(["arn:aws:s3:::lookout-data-123456789012/state/locks/forecast*"])
    error_message = "forecast may delete only its own locks"
  }
  # The capture job's persisted NPS politeness state (state/capture/..., its locks) is out of forecast's reach.
  assert {
    condition = !anytrue(flatten([
      for s in jsondecode(aws_iam_role_policy.job["forecast"].policy).Statement : [
        for r in flatten([s.Resource]) : (
          startswith("arn:aws:s3:::lookout-data-123456789012/state/capture/hosts/www.nps.gov.json", trimsuffix(r, "*")) ||
          startswith("arn:aws:s3:::lookout-data-123456789012/state/locks/capture/rainier.paradise-mountain.lock", trimsuffix(r, "*"))
        )
      ] if contains(flatten([s.Action]), "s3:PutObject") || contains(flatten([s.Action]), "s3:DeleteObject")
    ]))
    error_message = "the forecast role must not write or delete the capture job's state or locks"
  }
  assert {
    condition = !anytrue(flatten([
      for s in jsondecode(aws_iam_role_policy.job["capture"].policy).Statement : [
        for r in flatten([s.Resource]) : (
          startswith("arn:aws:s3:::lookout-data-123456789012/state/forecast/hrrr/2026111912z.json", trimsuffix(r, "*")) ||
          startswith("arn:aws:s3:::lookout-data-123456789012/state/locks/forecast-hrrr.lock", trimsuffix(r, "*"))
        )
      ] if contains(flatten([s.Action]), "s3:PutObject") || contains(flatten([s.Action]), "s3:DeleteObject")
    ]))
    error_message = "the capture role must not write or delete the forecast job's state or locks"
  }
  # No key a job may write or delete can be written or deleted by the other job: every pattern is a prefix
  # ending in its only "*", and no prefix of one job starts with a prefix of the other.
  assert {
    condition = alltrue(flatten([
      for a in flatten([for s in jsondecode(aws_iam_role_policy.job["capture"].policy).Statement : flatten([s.Resource]) if contains(flatten([s.Action]), "s3:PutObject") || contains(flatten([s.Action]), "s3:DeleteObject")]) : [
        for b in flatten([for s in jsondecode(aws_iam_role_policy.job["forecast"].policy).Statement : flatten([s.Resource]) if contains(flatten([s.Action]), "s3:PutObject") || contains(flatten([s.Action]), "s3:DeleteObject")]) :
        endswith(a, "*") && endswith(b, "*") && length(split("*", a)) == 2 && length(split("*", b)) == 2 &&
        !startswith(trimsuffix(a, "*"), trimsuffix(b, "*")) && !startswith(trimsuffix(b, "*"), trimsuffix(a, "*"))
      ]
    ]))
    error_message = "the two jobs' writable key spaces must not overlap"
  }
}

# The keys below are what the worker code builds (printed on 20 Nov from lookout_common.paths frame_key,
# sidecar_key, thumb_key, capture_log_key, forecast_key and forecast_log_key; lookout_common.storage.lock_key;
# lookout_capture.retention's staged key, state.camera_state_key and state.HOST_STATE_KEY; lookout_forecast's
# marker, failure and lag-survey keys). A pattern matches a key as IAM does: a trailing "*" matches any rest of the
# key, "/" included, and a pattern without "*" (the bucket ARN, for s3:ListBucket) matches only itself.
#
# Capture 0.2.0 (D-028) stores a thumbnail of every download in thumbs/ and stages the six hourly cameras' downloads
# under cache/staging/frames/. Without those grants every download there would fail to store, and its frame be lost.
run "capture_can_use_every_key_it_writes" {
  command = plan

  assert {
    condition = alltrue([
      for k in [
        "frames/grand-canyon.yavapai/2026-11-20/20261120T121500Z_0123456789ab.jpg",
        "frames/grand-canyon.yavapai/2026-11-20/20261120T121500Z_0123456789ab.json",
        "thumbs/grand-canyon.yavapai/2026-11-20/20261120T121500Z_0123456789ab.jpg",
        "thumbs/grand-canyon.yavapai/2026-11-20/20261120T121500Z_0123456789ab.json",
        "cache/staging/frames/grand-canyon.yavapai/2026-11-20/20261120T121500Z_0123456789ab.jpg",
        "cache/staging/frames/grand-canyon.yavapai/2026-11-20/20261120T121500Z_0123456789ab.json",
        "capture_log/2026-11-20.jsonl",
        "state/capture/rainier.paradise-mountain.json",
        "state/capture/hosts/www.nps.gov.json",
        "state/locks/capture/rainier.paradise-mountain.lock",
        "state/locks/capture/hosts.lock",
        ] : anytrue([
          for r in flatten([
            for s in jsondecode(aws_iam_role_policy.job["capture"].policy).Statement : flatten([s.Resource])
            if contains(flatten([s.Action]), "s3:GetObject") && contains(flatten([s.Action]), "s3:PutObject")
          ]) : endswith(r, "*") ? startswith("arn:aws:s3:::lookout-data-123456789012/${k}", trimsuffix(r, "*")) : "arn:aws:s3:::lookout-data-123456789012/${k}" == r
      ])
    ])
    error_message = "capture must read and write frames/, thumbs/, cache/staging/ (D-028), capture_log/, its state (camera state, the www.nps.gov hold) and its locks"
  }
  # retention.delete_staged removes the staged copies once the hour is decided, and every lock is released.
  assert {
    condition = alltrue([
      for k in [
        "cache/staging/frames/grand-canyon.yavapai/2026-11-20/20261120T121500Z_0123456789ab.jpg",
        "cache/staging/frames/grand-canyon.yavapai/2026-11-20/20261120T121500Z_0123456789ab.json",
        "state/locks/capture/rainier.paradise-mountain.lock",
        "state/locks/capture/hosts.lock",
        ] : anytrue([
          for r in flatten([
            for s in jsondecode(aws_iam_role_policy.job["capture"].policy).Statement : flatten([s.Resource]) if contains(flatten([s.Action]), "s3:DeleteObject")
          ]) : endswith(r, "*") ? startswith("arn:aws:s3:::lookout-data-123456789012/${k}", trimsuffix(r, "*")) : "arn:aws:s3:::lookout-data-123456789012/${k}" == r
      ])
    ])
    error_message = "capture must delete its staged copies under cache/staging/ and release its locks"
  }
}

# The capture job never deletes anything under frames/, archive/ or thumbs/ (D-028), nor a log, a state document or
# anything of the forecast job's.
run "capture_never_deletes_evidence" {
  command = plan

  assert {
    condition = !anytrue(flatten([
      for k in [
        "frames/grand-canyon.yavapai/2026-11-20/20261120T121500Z_0123456789ab.jpg",
        "frames/grand-canyon.yavapai/2026-11-20/20261120T121500Z_0123456789ab.json",
        "thumbs/grand-canyon.yavapai/2026-11-20/20261120T121500Z_0123456789ab.jpg",
        "thumbs/grand-canyon.yavapai/2026-11-20/20261120T121500Z_0123456789ab.json",
        "archive/point-reyes.beach/2026-05-31/20260531T120000Z_0123abcd.jpg",
        "capture_log/2026-11-20.jsonl",
        "state/capture/rainier.paradise-mountain.json",
        "state/capture/hosts/www.nps.gov.json",
        "forecast/hrrr/rainier.paradise-mountain/2026112012z.json",
        "state/locks/forecast-hrrr.lock",
        ] : [
        for r in flatten([
          for s in jsondecode(aws_iam_role_policy.job["capture"].policy).Statement : flatten([s.Resource]) if contains(flatten([s.Action]), "s3:DeleteObject")
        ]) : endswith(r, "*") ? startswith("arn:aws:s3:::lookout-data-123456789012/${k}", trimsuffix(r, "*")) : "arn:aws:s3:::lookout-data-123456789012/${k}" == r
      ]
    ]))
    error_message = "capture may delete only cache/staging/ copies and its own locks: never a frame, thumbnail, archive frame, log, state document or forecast key"
  }
}

# Forecast keeps its GRIB subsets on the function's local disk (cache_dir(), /tmp on Lambda), so it needs no cache/
# grant; without one it also cannot touch capture's staged frames.
run "forecast_uses_only_its_own_keys" {
  command = plan

  assert {
    condition = alltrue([
      for k in [
        "forecast/hrrr/rainier.paradise-mountain/2026112012z.json",
        "forecast_log/2026-11-20.jsonl",
        "forecast_log/lag_survey/2026-11-20.jsonl",
        "state/forecast/hrrr/2026112012z.json",
        "state/forecast/hrrr/2026112012z.failure.json",
        "state/locks/forecast-hrrr.lock",
        "state/locks/forecast-lag-survey.lock",
        ] : anytrue([
          for r in flatten([
            for s in jsondecode(aws_iam_role_policy.job["forecast"].policy).Statement : flatten([s.Resource])
            if contains(flatten([s.Action]), "s3:GetObject") && contains(flatten([s.Action]), "s3:PutObject")
          ]) : endswith(r, "*") ? startswith("arn:aws:s3:::lookout-data-123456789012/${k}", trimsuffix(r, "*")) : "arn:aws:s3:::lookout-data-123456789012/${k}" == r
      ])
    ])
    error_message = "forecast must read and write its forecast files, its logs, its run markers and failure records, and its locks"
  }
  # No forecast statement, of any action, reaches capture's keys or any cache/ key.
  assert {
    condition = !anytrue(flatten([
      for k in [
        "cache/staging/frames/grand-canyon.yavapai/2026-11-20/20261120T121500Z_0123456789ab.jpg",
        "cache/hrrr/2026112012/hrrr.t12z.wrfsfcf00.grib2",
        "thumbs/grand-canyon.yavapai/2026-11-20/20261120T121500Z_0123456789ab.jpg",
        "frames/grand-canyon.yavapai/2026-11-20/20261120T121500Z_0123456789ab.jpg",
        "capture_log/2026-11-20.jsonl",
        ] : [
        for s in jsondecode(aws_iam_role_policy.job["forecast"].policy).Statement : [
          for r in flatten([s.Resource]) : endswith(r, "*") ? startswith("arn:aws:s3:::lookout-data-123456789012/${k}", trimsuffix(r, "*")) : "arn:aws:s3:::lookout-data-123456789012/${k}" == r
        ]
      ]
    ]))
    error_message = "the forecast role must reach no cache/ key and none of capture's frames, thumbnails or logs"
  }
  assert {
    condition = alltrue([
      for k in ["state/locks/forecast-hrrr.lock", "state/locks/forecast-lag-survey.lock"] : anytrue([
        for r in flatten([
          for s in jsondecode(aws_iam_role_policy.job["forecast"].policy).Statement : flatten([s.Resource]) if contains(flatten([s.Action]), "s3:DeleteObject")
        ]) : endswith(r, "*") ? startswith("arn:aws:s3:::lookout-data-123456789012/${k}", trimsuffix(r, "*")) : "arn:aws:s3:::lookout-data-123456789012/${k}" == r
      ])
    ])
    error_message = "forecast must be able to release its locks"
  }
}

run "lambda_trust_policy_is_lambda_only" {
  command = plan

  assert {
    condition     = alltrue([for r in aws_iam_role.job : jsondecode(r.assume_role_policy).Statement[0].Principal.Service == "lambda.amazonaws.com"])
    error_message = "execution roles must be assumable by Lambda only"
  }
}

run "lambda_refuses_to_plan_without_an_image_tag" {
  command = plan

  variables {
    image_tag = null
  }

  expect_failures = [aws_lambda_function.job["capture"], aws_lambda_function.job["forecast"]]
}

run "lambda_refuses_an_empty_image_tag" {
  command = plan

  variables {
    image_tag = ""
  }

  expect_failures = [aws_lambda_function.job["capture"], aws_lambda_function.job["forecast"]]
}

run "user_agent_rejects_an_email_address" {
  command = plan

  variables {
    user_agent = "Lookout/0.1 (contact someone@example.com)"
  }

  expect_failures = [var.user_agent]
}
