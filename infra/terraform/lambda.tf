# The scheduled jobs (PRD §11.2): one arm64 container image, one Lambda function per entry in local.jobs.
# Each function has its own role, scoped to its own log group and its own data-bucket prefixes.

resource "aws_iam_role" "job" {
  for_each = local.jobs

  name               = "${local.function_name[each.key]}-lambda"
  description        = "Execution role for ${local.function_name[each.key]}"
  assume_role_policy = local.lambda_assume_role_policy
}

resource "aws_iam_role_policy" "job" {
  for_each = local.jobs

  name   = "${local.function_name[each.key]}-least-privilege"
  role   = aws_iam_role.job[each.key].id
  policy = local.job_policy[each.key]
}

# Created before the functions so Lambda never creates a log group without retention (PRD §15.2: 30 days).
resource "aws_cloudwatch_log_group" "job" {
  for_each = local.jobs

  name              = local.log_group_name[each.key]
  retention_in_days = local.log_retention_days
}

resource "aws_lambda_function" "job" {
  for_each = local.jobs

  function_name = local.function_name[each.key]
  description   = each.value.description
  role          = aws_iam_role.job[each.key].arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.lambda.repository_url}:${coalesce(var.image_tag, "unset")}"
  architectures = ["arm64"]
  timeout       = each.value.timeout_s
  memory_size   = each.value.memory_mb
  # One instance at a time: never two NPS fetchers at once (CLAUDE.md), and no overlapping forecast runs.
  reserved_concurrent_executions = 1

  image_config {
    command = [each.value.handler]
  }

  ephemeral_storage {
    size = each.value.ephemeral_storage_mb
  }

  environment {
    variables = {
      LOOKOUT_STORAGE      = "s3"
      LOOKOUT_S3_BUCKET    = aws_s3_bucket.data.bucket
      LOOKOUT_MANIFEST_DIR = local.manifest_dir
      LOOKOUT_USER_AGENT   = var.user_agent
      LOOKOUT_DATA_DIR     = local.scratch_dir
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.job[each.key].name
  }

  lifecycle {
    precondition {
      condition     = var.image_tag != null && var.image_tag != ""
      error_message = "image_tag is not set. Build and push the image first (infra/lambda/build.sh --push), then pass -var image_tag=<tag>. infra/deploy.sh does both; see infra/README.md."
    }
  }

  depends_on = [aws_iam_role_policy.job]
}

# Scheduled runs are fire-and-forget: no retries (the next tick is the retry), and stale events are dropped.
resource "aws_lambda_function_event_invoke_config" "job" {
  for_each = local.jobs

  function_name                = aws_lambda_function.job[each.key].function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = local.max_event_age_s[each.key]
}
