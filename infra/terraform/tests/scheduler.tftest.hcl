# Plan-only tests against a mocked AWS provider: EventBridge Scheduler (PRD §11.2, §11.3) and the polite
# collection guard on the capture cadence (PRD §15.3).

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

run "schedules_match_spec" {
  command = plan

  assert {
    condition     = aws_scheduler_schedule.job["capture"].schedule_expression == "rate(5 minutes)"
    error_message = "capture runs every 5 minutes (PRD §11.2)"
  }
  assert {
    condition     = aws_scheduler_schedule.job["forecast"].schedule_expression == "rate(5 minutes)"
    error_message = "the forecast worker runs every 5 minutes (PRD §11.2)"
  }
  assert {
    condition     = alltrue([for s in aws_scheduler_schedule.job : s.state == "ENABLED" && s.group_name == "snow-globe"])
    error_message = "schedules are enabled by default, in the snow-globe group"
  }
  assert {
    condition     = alltrue([for s in aws_scheduler_schedule.job : s.flexible_time_window[0].mode == "OFF"])
    error_message = "no flexible window: runs start on the tick"
  }
  assert {
    condition     = alltrue([for s in aws_scheduler_schedule.job : s.target[0].retry_policy[0].maximum_retry_attempts == 0])
    error_message = "the Scheduler must not retry (the next tick is the retry)"
  }
  assert {
    condition     = alltrue([for s in aws_scheduler_schedule.job : s.target[0].retry_policy[0].maximum_event_age_in_seconds == 240])
    error_message = "stale scheduled events must be dropped before the next tick"
  }
  assert {
    condition     = alltrue([for name, s in aws_scheduler_schedule.job : jsondecode(s.target[0].input).job == name])
    error_message = "the event names its job and stays valid JSON"
  }
  # Check the raw string, not the decoded one: the Scheduler substitutes only the literal placeholder text,
  # and jsonencode() would have escaped "<" and ">" (a decoded comparison cannot see that).
  assert {
    condition = alltrue([for s in aws_scheduler_schedule.job : (
      strcontains(s.target[0].input, "\"<aws.scheduler.scheduled-time>\"") &&
      strcontains(s.target[0].input, "\"<aws.scheduler.execution-id>\"") &&
      !strcontains(s.target[0].input, "\\u003c") && !strcontains(s.target[0].input, "\\u003e")
    )])
    error_message = "the input must carry the literal <aws.scheduler.*> placeholders, unescaped"
  }
}

run "scheduler_role_can_only_invoke_the_two_jobs" {
  command = plan

  assert {
    condition     = jsondecode(aws_iam_role.scheduler.assume_role_policy).Statement[0].Principal.Service == "scheduler.amazonaws.com"
    error_message = "the scheduler role is for EventBridge Scheduler"
  }
  assert {
    condition     = jsondecode(aws_iam_role.scheduler.assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "123456789012"
    error_message = "the trust policy must pin the source account (confused-deputy guard)"
  }
  assert {
    condition     = length(jsondecode(aws_iam_role_policy.scheduler.policy).Statement) == 1 && jsondecode(aws_iam_role_policy.scheduler.policy).Statement[0].Action == ["lambda:InvokeFunction"]
    error_message = "the scheduler may only invoke functions"
  }
  assert {
    condition = toset(jsondecode(aws_iam_role_policy.scheduler.policy).Statement[0].Resource) == toset([
      "arn:aws:lambda:us-east-1:123456789012:function:snow-globe-capture",
      "arn:aws:lambda:us-east-1:123456789012:function:snow-globe-capture:*",
      "arn:aws:lambda:us-east-1:123456789012:function:snow-globe-forecast",
      "arn:aws:lambda:us-east-1:123456789012:function:snow-globe-forecast:*",
    ])
    error_message = "the scheduler may only invoke the two Snow Globe jobs"
  }
  assert {
    condition     = aws_iam_role_policy.scheduler.role == "snow-globe-scheduler" && alltrue([for s in aws_scheduler_schedule.job : s.target[0].role_arn == "arn:aws:iam::123456789012:role/snow-globe-scheduler"])
    error_message = "the schedules run as the scheduler role, which holds the invoke policy"
  }
}

run "pausing_capture_disables_its_schedule_and_silence_alarm" {
  command = plan

  variables {
    capture_schedule_enabled = false
  }

  assert {
    condition     = aws_scheduler_schedule.job["capture"].state == "DISABLED" && aws_scheduler_schedule.job["forecast"].state == "ENABLED"
    error_message = "only the capture schedule pauses"
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.job_not_running["capture"].actions_enabled == false && aws_cloudwatch_metric_alarm.job_not_running["forecast"].actions_enabled == true
    error_message = "a paused job must not page as 'not running'"
  }
}

run "hourly_capture_is_allowed" {
  command = plan

  variables {
    capture_schedule_expression = "rate(1 hour)"
  }

  assert {
    condition     = aws_scheduler_schedule.job["capture"].schedule_expression == "rate(1 hour)"
    error_message = "rate(1 hour) is a valid capture cadence"
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.job_not_running["capture"].period == 10800 && aws_cloudwatch_metric_alarm.job_errors["capture"].period == 3600
    error_message = "alarm windows follow the schedule interval"
  }
  assert {
    condition     = aws_scheduler_schedule.job["capture"].target[0].retry_policy[0].maximum_event_age_in_seconds == 3540
    error_message = "the event age follows the schedule interval"
  }
}

run "slower_forecast_cadence_moves_its_windows" {
  command = plan

  variables {
    forecast_schedule_expression = "rate(10 minutes)"
  }

  assert {
    condition     = aws_lambda_function_event_invoke_config.job["forecast"].maximum_event_age_in_seconds == 540 && aws_cloudwatch_metric_alarm.job_not_running["forecast"].period == 1800
    error_message = "event age and the not-running window follow the forecast interval"
  }
}

run "capture_faster_than_5_minutes_is_rejected" {
  command = plan

  variables {
    capture_schedule_expression = "rate(4 minutes)"
  }

  expect_failures = [var.capture_schedule_expression]
}

run "capture_every_minute_is_rejected" {
  command = plan

  variables {
    capture_schedule_expression = "rate(1 minute)"
  }

  expect_failures = [var.capture_schedule_expression]
}

run "capture_cron_is_rejected" {
  command = plan

  variables {
    capture_schedule_expression = "cron(* * * * ? *)"
  }

  expect_failures = [var.capture_schedule_expression]
}

run "forecast_slower_than_hourly_is_rejected" {
  command = plan

  variables {
    forecast_schedule_expression = "rate(90 minutes)"
  }

  expect_failures = [var.forecast_schedule_expression]
}
