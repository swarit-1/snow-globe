# Plan-only tests against a mocked AWS provider: the private data bucket (PRD §8.7, §15.2) and ECR.

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
    values          = { repository_url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/snow-globe/lambda" }
  }
  override_resource {
    target          = aws_sns_topic.alarms
    override_during = plan
    values          = { arn = "arn:aws:sns:us-east-1:123456789012:snow-globe-alarms" }
  }
  override_resource {
    target          = aws_iam_role.job["capture"]
    override_during = plan
    values          = { arn = "arn:aws:iam::123456789012:role/snow-globe-capture-lambda", id = "snow-globe-capture-lambda" }
  }
  override_resource {
    target          = aws_iam_role.job["forecast"]
    override_during = plan
    values          = { arn = "arn:aws:iam::123456789012:role/snow-globe-forecast-lambda", id = "snow-globe-forecast-lambda" }
  }
  override_resource {
    target          = aws_iam_role.scheduler
    override_during = plan
    values          = { arn = "arn:aws:iam::123456789012:role/snow-globe-scheduler", id = "snow-globe-scheduler" }
  }
  override_resource {
    target          = aws_iam_role.budget_action
    override_during = plan
    values          = { arn = "arn:aws:iam::123456789012:role/snow-globe-budget-action", id = "snow-globe-budget-action" }
  }
  override_resource {
    target          = aws_iam_policy.budget_hard_stop
    override_during = plan
    values          = { arn = "arn:aws:iam::123456789012:policy/snow-globe-budget-hard-stop" }
  }
}

# Every variable is pinned to its default (alarm_email and image_tag, which have none, to test values), so the
# suite checks the shipped defaults whatever the shell exports (TF_VAR_*) or a *.auto.tfvars file in the module
# holds. infra/test.sh fails if a variable is missing here or a pin differs from its default.
variables {
  alarm_email                     = "alerts@example.com"
  image_tag                       = "0123456789ab"
  region                          = "us-east-1"
  project                         = "snow-globe"
  user_agent                      = "SnowGlobe/0.1 (webcam research capture; fetch-on-change)"
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

run "data_bucket_is_private_and_encrypted" {
  command = plan

  assert {
    condition     = aws_s3_bucket.data.bucket == "snow-globe-data-123456789012"
    error_message = "data bucket name"
  }
  assert {
    condition = (
      aws_s3_bucket_public_access_block.data.block_public_acls &&
      aws_s3_bucket_public_access_block.data.block_public_policy &&
      aws_s3_bucket_public_access_block.data.ignore_public_acls &&
      aws_s3_bucket_public_access_block.data.restrict_public_buckets
    )
    error_message = "all four public-access blocks must be on"
  }
  assert {
    condition     = one([for r in aws_s3_bucket_server_side_encryption_configuration.data.rule : r.apply_server_side_encryption_by_default[0].sse_algorithm]) == "AES256"
    error_message = "data bucket must use SSE-S3"
  }
  assert {
    condition     = aws_s3_bucket_ownership_controls.data.rule[0].object_ownership == "BucketOwnerEnforced"
    error_message = "ACLs must be disabled"
  }
  assert {
    condition     = aws_s3_bucket.data.force_destroy == false
    error_message = "the data bucket must not be force-destroyed by default"
  }
}

run "data_bucket_denies_plain_http" {
  command = plan

  assert {
    condition     = jsondecode(aws_s3_bucket_policy.data.policy).Statement[0].Effect == "Deny"
    error_message = "the only bucket-policy statement must be a Deny"
  }
  assert {
    condition     = jsondecode(aws_s3_bucket_policy.data.policy).Statement[0].Condition.Bool["aws:SecureTransport"] == "false"
    error_message = "the deny must target non-TLS requests"
  }
  assert {
    condition     = length(jsondecode(aws_s3_bucket_policy.data.policy).Statement) == 1
    error_message = "the bucket policy must not grant anything"
  }
}

# Evaluation evidence survives a buggy overwrite or a truncated log (PRD §6.2).
run "data_bucket_keeps_previous_versions" {
  command = plan

  assert {
    condition     = aws_s3_bucket_versioning.data.versioning_configuration[0].status == "Enabled"
    error_message = "the data bucket must be versioned"
  }
  assert {
    condition     = one([for r in aws_s3_bucket_lifecycle_configuration.data.rule : r.noncurrent_version_expiration[0].noncurrent_days if r.id == "expire-noncurrent-versions"]) == 30
    error_message = "previous versions are kept for 30 days"
  }
  assert {
    condition     = one([for r in aws_s3_bucket_lifecycle_configuration.data.rule : r.expiration[0].expired_object_delete_marker if r.id == "expire-noncurrent-versions"]) == true
    error_message = "delete markers left without versions are removed"
  }
}

# Each logged record rewrites the whole day's log, so the logs' old versions are kept 1 day, not 30 (cost).
run "old_log_versions_go_after_a_day" {
  command = plan

  assert {
    condition = toset([
      for r in aws_s3_bucket_lifecycle_configuration.data.rule : "${r.filter[0].prefix}=${r.noncurrent_version_expiration[0].noncurrent_days}" if startswith(r.id, "expire-old-log-versions-")
    ]) == toset(["capture_log/=1", "forecast_log/=1"])
    error_message = "capture_log/ and forecast_log/ keep previous versions for 1 day"
  }
  assert {
    condition     = alltrue([for r in aws_s3_bucket_lifecycle_configuration.data.rule : length(r.expiration) == 0 && r.status == "Enabled" if startswith(r.id, "expire-old-log-versions-")])
    error_message = "the log rules touch old versions only, never the current log"
  }
}

run "noncurrent_retention_is_a_variable" {
  command = plan

  variables {
    data_noncurrent_version_days = 7
    log_noncurrent_version_days  = 2
  }

  assert {
    condition     = one([for r in aws_s3_bucket_lifecycle_configuration.data.rule : r.noncurrent_version_expiration[0].noncurrent_days if r.id == "expire-noncurrent-versions"]) == 7
    error_message = "data_noncurrent_version_days must reach the lifecycle rule"
  }
  assert {
    condition     = alltrue([for r in aws_s3_bucket_lifecycle_configuration.data.rule : r.noncurrent_version_expiration[0].noncurrent_days == 2 if startswith(r.id, "expire-old-log-versions-")])
    error_message = "log_noncurrent_version_days must reach the log rules"
  }
}

run "noncurrent_retention_must_be_whole_days" {
  command = plan

  variables {
    data_noncurrent_version_days = 0
  }

  expect_failures = [var.data_noncurrent_version_days]
}

# cache/ holds capture's staged frames (D-028) until their hour is decided, and the hours of a stopped capture job
# are decided when it runs again. So the backstop expiry outlasts the longest hard stop (AWS Budgets lifts it at the
# next budget month, at most 31 days on) with room for the first passes after it; at 7 days a stop that began before
# about day 23 of a month cost the undecided hours their full frames.
run "cache_prefix_expires_after_35_days" {
  command = plan

  assert {
    condition     = one([for r in aws_s3_bucket_lifecycle_configuration.data.rule : r.filter[0].prefix if r.id == "expire-cache"]) == "cache/"
    error_message = "the expiring rule must be scoped to cache/"
  }
  assert {
    condition     = one([for r in aws_s3_bucket_lifecycle_configuration.data.rule : r.expiration[0].days if r.id == "expire-cache"]) == 35
    error_message = "cache/ must expire after 35 days, longer than a hard stop can last (31 days)"
  }
  assert {
    condition     = one([for r in aws_s3_bucket_lifecycle_configuration.data.rule : r.noncurrent_version_expiration[0].noncurrent_days if r.id == "expire-cache"]) == 1
    error_message = "expired cache versions go after a day"
  }
  # Nothing current outside cache/ may expire: frames and forecasts are evaluation evidence. The other rule
  # with an expiration block only removes delete markers (S3 refuses Days next to ExpiredObjectDeleteMarker).
  assert {
    condition     = toset([for r in aws_s3_bucket_lifecycle_configuration.data.rule : r.id if length(r.expiration) > 0]) == toset(["expire-cache", "expire-noncurrent-versions"])
    error_message = "only expire-cache may expire current objects"
  }
  assert {
    condition     = one([for r in aws_s3_bucket_lifecycle_configuration.data.rule : r.abort_incomplete_multipart_upload[0].days_after_initiation if r.id == "abort-incomplete-multipart-uploads"]) == 1
    error_message = "incomplete multipart uploads should be aborted after a day"
  }
}

run "ecr_scans_on_push_and_keeps_5_images" {
  command = plan

  assert {
    condition     = aws_ecr_repository.lambda.name == "snow-globe/lambda"
    error_message = "ECR repository name"
  }
  assert {
    condition     = aws_ecr_repository.lambda.image_scanning_configuration[0].scan_on_push
    error_message = "scan on push must be on"
  }
  assert {
    condition     = aws_ecr_repository.lambda.image_tag_mutability == "IMMUTABLE"
    error_message = "tags must be immutable (Lambda pins digests; a re-pointed tag would silently not deploy)"
  }
  assert {
    condition     = jsondecode(aws_ecr_lifecycle_policy.lambda.policy).rules[0].selection.countNumber == 5
    error_message = "keep the last 5 images"
  }
  assert {
    condition     = jsondecode(aws_ecr_lifecycle_policy.lambda.policy).rules[0].selection.countType == "imageCountMoreThan" && jsondecode(aws_ecr_lifecycle_policy.lambda.policy).rules[0].selection.tagStatus == "any"
    error_message = "the lifecycle rule must count every image"
  }
}

run "ecr_pull_is_limited_to_this_projects_functions" {
  command = plan

  assert {
    condition     = jsondecode(aws_ecr_repository_policy.lambda.policy).Statement[0].Principal.Service == "lambda.amazonaws.com"
    error_message = "only the Lambda service may pull"
  }
  assert {
    condition     = jsondecode(aws_ecr_repository_policy.lambda.policy).Statement[0].Condition.StringLike["aws:sourceArn"] == "arn:aws:lambda:us-east-1:123456789012:function:snow-globe-*"
    error_message = "pulls must be limited to this project's functions"
  }
}

# The provider block itself (default_tags = local.tags) is checked statically by infra/test.sh: a mocked provider
# never merges default_tags into tags_all.
run "default_tags_name_the_project" {
  command = plan

  assert {
    condition     = local.tags.project == "snow-globe"
    error_message = "every resource carries project=snow-globe through the provider's default_tags"
  }
  assert {
    condition     = var.region == "us-east-1"
    error_message = "us-east-1 is NOAA's HRRR region (no transfer cost)"
  }
}

run "bucket_name_can_be_overridden" {
  command = plan

  variables {
    data_bucket_name = "my-snow-globe-bucket"
  }

  assert {
    condition     = aws_s3_bucket.data.bucket == "my-snow-globe-bucket" && aws_lambda_function.job["capture"].environment[0].variables["SNOW_GLOBE_S3_BUCKET"] == "my-snow-globe-bucket"
    error_message = "an explicit bucket name must reach the bucket and the workers"
  }
}
