# Alarms and cost guard rail (PRD §11.2 "budget alarm on top", §17; Appendix B: the budget exists before
# anything else is deployed). Adapted from Oculus monitoring.tf: one SNS topic, an email subscription and
# AWS Budgets, plus a budget action that enforces the hosting cap. The ALB and Route 53 alarms are not carried
# over (there is no ALB).

resource "aws_sns_topic" "alarms" {
  name = "${var.project}-alarms"
}

# Only this project's CloudWatch alarms, in this account, may publish: the confused-deputy guard from the
# CloudWatch docs ("Notifying users on alarm changes"). AWS Budgets emails alarm_email directly, below.
# The topic stays unencrypted on purpose: CloudWatch can't publish to a topic under the default aws/sns key, and a
# customer-managed key costs $1 a month.
resource "aws_sns_topic_policy" "alarms" {
  arn = aws_sns_topic.alarms.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "CloudWatchAlarmsPublish"
      Effect    = "Allow"
      Principal = { Service = "cloudwatch.amazonaws.com" }
      Action    = "sns:Publish"
      Resource  = aws_sns_topic.alarms.arn
      Condition = {
        ArnLike      = { "aws:SourceArn" = "arn:${local.partition}:cloudwatch:${var.region}:${local.account_id}:alarm:${var.project}-*" }
        StringEquals = { "aws:SourceAccount" = local.account_id }
      }
    }]
  })
}

# AWS sends a confirmation email; alarms reach the inbox only after the link in it is clicked.
resource "aws_sns_topic_subscription" "alarm_email" {
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

# ---- per job: errors, and runs that stopped happening ----------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "job_errors" {
  for_each = local.jobs

  alarm_name          = "${local.function_name[each.key]}-errors"
  alarm_description   = "${local.function_name[each.key]} failed in 2 of the last 3 windows of ${local.error_alarm_period_s[each.key] / 60} min. Logs: ${local.log_group_name[each.key]}"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = local.function_name[each.key] }
  statistic           = "Sum"
  period              = local.error_alarm_period_s[each.key]
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "job_not_running" {
  for_each = local.jobs

  alarm_name          = "${local.function_name[each.key]}-not-running"
  alarm_description   = "No ${local.function_name[each.key]} invocation in ${2 * local.silence_alarm_period_s[each.key] / 60} min (schedule: ${each.value.schedule_expression}). Check the schedule, the image and the concurrency quota."
  namespace           = "AWS/Lambda"
  metric_name         = "Invocations"
  dimensions          = { FunctionName = local.function_name[each.key] }
  statistic           = "Sum"
  period              = local.silence_alarm_period_s[each.key]
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  # No datapoint at all means no invocation, which is exactly what this alarm is for.
  treat_missing_data = "breaching"
  # A paused schedule is not an outage.
  actions_enabled = each.value.schedule_enabled

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

# ---- AWS Budgets: PRD §17's $3-a-month hosting cap ---------------------------------------------------------
# Emails at 33 / 67 / 100 % of the limit in actual spend (about $1, $2 and $3), and when the month's forecast
# passes it. At 100 % of actual spend the hard stop below halts every scheduled job.

resource "aws_budgets_budget" "monthly" {
  name         = "${var.project}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Count costs before credits and refunds, so the emails and the hard stop see gross usage. AWS includes both by
  # default, and both arrive as negative amounts: the account's $100 of sign-up credits, and the $5,000 of AWS Activate
  # credits applied for on 21 Nov 2026 (two years), would hold a netted budget near $0, so no email and no hard stop
  # would ever fire. Every other cost type keeps AWS's default (counted; unblended rates). The Budgets API reference
  # marks CostTypes deprecated in favour of FilterExpression and Metrics (infra/README.md, "Hard stop at the cap").
  cost_types {
    include_credit = false
    include_refund = false
  }

  dynamic "cost_filter" {
    for_each = var.budget_scope_to_project_tag ? [format("user:project$%s", var.project)] : []

    content {
      name   = "TagKeyValue"
      values = [cost_filter.value]
    }
  }

  dynamic "notification" {
    for_each = toset(var.budget_alert_thresholds_percent)

    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.alarm_email]
    }
  }

  # An early warning: the month is forecast to pass the cap. AWS needs about five weeks of usage before it
  # forecasts, so a new account hears nothing from this one at first.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alarm_email]
  }
}

# ---- Hard stop at the cap (PRD §17: "Hard caps are enforced in code") ---------------------------------------
# At 100 % of the monthly budget in actual spend, AWS Budgets attaches local.budget_hard_stop_policy (a Deny on
# lambda:InvokeFunction for every job) to the scheduler role, so no scheduled job runs for the rest of the month.
# AWS resets IAM-policy actions at the start of each budget period; the owner can reverse it sooner
# (infra/README.md). Terraform leaves the attachment alone (the scheduler role does not manage its managed
# policies), so an apply during a stop does not undo it. While it holds, the not-running alarms email.
# Storage keeps costing a few cents a month: nothing is deleted. One action-enabled budget is free.

resource "aws_iam_policy" "budget_hard_stop" {
  count = var.budget_hard_stop ? 1 : 0

  name        = "${var.project}-budget-hard-stop"
  description = "Attached to ${var.project}-scheduler by AWS Budgets at 100 % of the monthly budget: stops every scheduled ${var.project} job"
  policy      = local.budget_hard_stop_policy
}

resource "aws_iam_role" "budget_action" {
  count = var.budget_hard_stop ? 1 : 0

  name               = "${var.project}-budget-action"
  description        = "Assumed by AWS Budgets to attach ${var.project}-budget-hard-stop to the ${var.project}-scheduler role"
  assume_role_policy = local.budget_action_assume_role_policy
}

# It may attach and detach that one policy, on that one role, and nothing else.
resource "aws_iam_role_policy" "budget_action" {
  count = var.budget_hard_stop ? 1 : 0

  name = "${var.project}-budget-action-attach"
  role = aws_iam_role.budget_action[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AttachHardStopToSchedulerRole"
      Effect    = "Allow"
      Action    = ["iam:AttachRolePolicy", "iam:DetachRolePolicy"]
      Resource  = aws_iam_role.scheduler.arn
      Condition = { ArnEquals = { "iam:PolicyARN" = aws_iam_policy.budget_hard_stop[0].arn } }
    }]
  })
}

resource "aws_budgets_budget_action" "hard_stop" {
  count = var.budget_hard_stop ? 1 : 0

  budget_name        = aws_budgets_budget.monthly.name
  action_type        = "APPLY_IAM_POLICY"
  approval_model     = "AUTOMATIC"
  notification_type  = "ACTUAL"
  execution_role_arn = aws_iam_role.budget_action[0].arn

  action_threshold {
    action_threshold_type  = "PERCENTAGE"
    action_threshold_value = 100
  }

  definition {
    iam_action_definition {
      policy_arn = aws_iam_policy.budget_hard_stop[0].arn
      roles      = [aws_iam_role.scheduler.name]
    }
  }

  subscriber {
    address           = var.alarm_email
    subscription_type = "EMAIL"
  }

  depends_on = [aws_iam_role_policy.budget_action]
}
