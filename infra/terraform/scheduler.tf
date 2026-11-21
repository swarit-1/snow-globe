# EventBridge Scheduler (PRD §11.2, §11.3): no GitHub Actions cron, which jitters and is disabled after 60 days
# without activity in a public repo. The free tier covers 14 M invocations a month; these schedules use ~17 k.

resource "aws_scheduler_schedule_group" "snow_globe" {
  name = var.project
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.project}-scheduler"
  description        = "Assumed by EventBridge Scheduler to invoke the ${var.project} jobs"
  assume_role_policy = local.scheduler_assume_role_policy
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "${var.project}-scheduler-invoke"
  role   = aws_iam_role.scheduler.id
  policy = local.scheduler_policy
}

resource "aws_scheduler_schedule" "job" {
  for_each = local.jobs

  name        = local.function_name[each.key]
  group_name  = aws_scheduler_schedule_group.snow_globe.name
  description = "Runs ${local.function_name[each.key]} on ${each.value.schedule_expression}"

  schedule_expression = each.value.schedule_expression
  state               = each.value.schedule_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.job[each.key].arn
    role_arn = aws_iam_role.scheduler.arn
    # The Scheduler fills in the <aws.scheduler.*> placeholders, so a worker can log how late each run started.
    # jsonencode() escapes "<" and ">" as < and >, and the Scheduler only replaces the literal
    # placeholder text, so the two characters are put back after encoding.
    input = replace(replace(jsonencode({
      source         = "snow-globe.scheduler"
      job            = each.key
      scheduled_time = "<aws.scheduler.scheduled-time>"
      execution_id   = "<aws.scheduler.execution-id>"
    }), "\\u003c", "<"), "\\u003e", ">")

    retry_policy {
      maximum_retry_attempts       = 0
      maximum_event_age_in_seconds = local.max_event_age_s[each.key]
    }
  }

  depends_on = [aws_iam_role_policy.scheduler]
}
