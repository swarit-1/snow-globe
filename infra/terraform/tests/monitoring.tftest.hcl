# Plan-only tests against a mocked AWS provider: alarms, the email subscription, AWS Budgets and the hard stop
# that enforces PRD §17's $3-a-month hosting cap.

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

run "alarm_email_is_subscribed" {
  command = plan

  assert {
    condition     = aws_sns_topic_subscription.alarm_email.protocol == "email" && aws_sns_topic_subscription.alarm_email.endpoint == "alerts@example.com"
    error_message = "alarm_email must be subscribed to the alarm topic"
  }
  assert {
    condition     = length(jsondecode(aws_sns_topic_policy.alarms.policy).Statement) == 1 && jsondecode(aws_sns_topic_policy.alarms.policy).Statement[0].Principal.Service == "cloudwatch.amazonaws.com"
    error_message = "only CloudWatch may publish to the alarm topic"
  }
  assert {
    condition     = jsondecode(aws_sns_topic_policy.alarms.policy).Statement[0].Condition.ArnLike["aws:SourceArn"] == "arn:aws:cloudwatch:us-east-1:123456789012:alarm:snow-globe-*"
    error_message = "only this project's alarms may publish (confused-deputy guard)"
  }
  assert {
    condition     = jsondecode(aws_sns_topic_policy.alarms.policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "123456789012"
    error_message = "publishing must be pinned to this account"
  }
  # Every alarm name must match the topic policy's alarm:<project>-* pattern, or its emails would be refused.
  assert {
    condition     = alltrue([for a in concat(values(aws_cloudwatch_metric_alarm.job_errors), values(aws_cloudwatch_metric_alarm.job_not_running)) : startswith(a.alarm_name, "snow-globe-")])
    error_message = "alarm names must start with the project prefix"
  }
}

# The repo is public: plans and logs must never print the owner's address. (That the variable has no default,
# so no address sits in the code, is checked statically by infra/test.sh.)
run "alarm_email_stays_sensitive" {
  command = plan

  assert {
    condition     = issensitive(var.alarm_email)
    error_message = "alarm_email must be declared sensitive"
  }
  assert {
    condition     = issensitive(aws_sns_topic_subscription.alarm_email.endpoint)
    error_message = "the subscribed address must stay sensitive in the plan"
  }
}

run "error_alarms_match_spec" {
  command = plan

  assert {
    condition     = alltrue([for name, a in aws_cloudwatch_metric_alarm.job_errors : a.namespace == "AWS/Lambda" && a.metric_name == "Errors" && a.dimensions["FunctionName"] == "snow-globe-${name}"])
    error_message = "one Errors alarm per function"
  }
  assert {
    condition     = alltrue([for a in aws_cloudwatch_metric_alarm.job_errors : a.comparison_operator == "GreaterThanOrEqualToThreshold" && a.threshold == 1 && a.treat_missing_data == "notBreaching"])
    error_message = "errors alarm on any error; no invocations is not an error"
  }
  assert {
    condition     = alltrue([for a in aws_cloudwatch_metric_alarm.job_errors : a.period == 900 && a.evaluation_periods == 3 && a.datapoints_to_alarm == 2])
    error_message = "errors in 2 of the last 3 fifteen-minute windows"
  }
  assert {
    condition     = alltrue([for a in concat(values(aws_cloudwatch_metric_alarm.job_errors), values(aws_cloudwatch_metric_alarm.job_not_running)) : a.alarm_actions == toset(["arn:aws:sns:us-east-1:123456789012:snow-globe-alarms"]) && a.ok_actions == toset(["arn:aws:sns:us-east-1:123456789012:snow-globe-alarms"])])
    error_message = "every alarm notifies the alarm topic, on alarm and on recovery"
  }
}

run "missing_invocation_alarms_match_spec" {
  command = plan

  assert {
    condition     = alltrue([for name, a in aws_cloudwatch_metric_alarm.job_not_running : a.metric_name == "Invocations" && a.dimensions["FunctionName"] == "snow-globe-${name}"])
    error_message = "one Invocations alarm per function"
  }
  assert {
    condition     = alltrue([for a in aws_cloudwatch_metric_alarm.job_not_running : a.comparison_operator == "LessThanThreshold" && a.threshold == 1 && a.treat_missing_data == "breaching"])
    error_message = "no invocation (no datapoint) must breach"
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.job_not_running["capture"].period == 900 && aws_cloudwatch_metric_alarm.job_not_running["forecast"].period == 900
    error_message = "each window holds three scheduled runs"
  }
  assert {
    condition     = alltrue([for a in aws_cloudwatch_metric_alarm.job_not_running : a.evaluation_periods == 2 && a.actions_enabled])
    error_message = "two empty windows in a row page, while the schedule is enabled"
  }
}

run "budget_matches_spec" {
  command = plan

  assert {
    condition     = aws_budgets_budget.monthly.limit_amount == "3" && aws_budgets_budget.monthly.limit_unit == "USD"
    error_message = "the default monthly limit is PRD §17's $3 hosting cap"
  }
  assert {
    condition     = aws_budgets_budget.monthly.time_unit == "MONTHLY" && aws_budgets_budget.monthly.budget_type == "COST"
    error_message = "monthly cost budget"
  }
  assert {
    condition     = toset([for n in aws_budgets_budget.monthly.notification : n.threshold if n.notification_type == "ACTUAL"]) == toset([33, 67, 100])
    error_message = "actual-spend emails at 33, 67 and 100 percent (about $1, $2 and $3)"
  }
  assert {
    condition     = toset([for n in aws_budgets_budget.monthly.notification : n.threshold if n.notification_type == "FORECASTED"]) == toset([100])
    error_message = "one email when the month is forecast to pass the cap"
  }
  assert {
    condition     = length(aws_budgets_budget.monthly.notification) <= 5
    error_message = "AWS Budgets allows at most 5 notifications per budget"
  }
  assert {
    condition     = alltrue([for n in aws_budgets_budget.monthly.notification : n.threshold_type == "PERCENTAGE" && n.comparison_operator == "GREATER_THAN"])
    error_message = "notifications as a percentage of the limit"
  }
  assert {
    condition     = alltrue([for n in aws_budgets_budget.monthly.notification : n.subscriber_email_addresses == toset(["alerts@example.com"])])
    error_message = "budget notifications go to alarm_email"
  }
  assert {
    condition     = aws_budgets_budget.monthly.cost_types[0].include_credit == false
    error_message = "credits must not hide real spend"
  }
  assert {
    condition     = length(aws_budgets_budget.monthly.cost_filter) == 0
    error_message = "by default the budget covers the whole account"
  }
}

# PRD §17: hosting has a hard cap of $3 a month. The owner must hear before the cap, and at it.
run "budget_never_lets_hosting_pass_the_prd_cap_unannounced" {
  command = plan

  assert {
    condition     = tonumber(aws_budgets_budget.monthly.limit_amount) <= 3
    error_message = "the monthly limit must not exceed PRD §17's $3 hosting cap"
  }
  assert {
    condition     = min([for n in aws_budgets_budget.monthly.notification : tonumber(aws_budgets_budget.monthly.limit_amount) * n.threshold / 100 if n.notification_type == "ACTUAL"]...) < 3
    error_message = "the first actual-spend email must come before $3"
  }
  assert {
    condition     = contains([for n in aws_budgets_budget.monthly.notification : n.threshold if n.notification_type == "ACTUAL"], 100)
    error_message = "an email must go out at the cap itself"
  }
}

run "hard_stop_matches_spec" {
  command = plan

  assert {
    condition     = length(aws_budgets_budget_action.hard_stop) == 1
    error_message = "the hard stop is on by default (PRD §17: hard caps are enforced in code)"
  }
  assert {
    condition = (
      aws_budgets_budget_action.hard_stop[0].budget_name == "snow-globe-monthly" &&
      aws_budgets_budget_action.hard_stop[0].action_type == "APPLY_IAM_POLICY" &&
      aws_budgets_budget_action.hard_stop[0].approval_model == "AUTOMATIC" &&
      aws_budgets_budget_action.hard_stop[0].notification_type == "ACTUAL"
    )
    error_message = "an automatic IAM-policy action on the monthly budget's actual spend"
  }
  assert {
    condition     = aws_budgets_budget_action.hard_stop[0].action_threshold[0].action_threshold_type == "PERCENTAGE" && aws_budgets_budget_action.hard_stop[0].action_threshold[0].action_threshold_value == 100
    error_message = "the stop fires at 100 % of the limit"
  }
  assert {
    condition = (
      aws_budgets_budget_action.hard_stop[0].definition[0].iam_action_definition[0].policy_arn == "arn:aws:iam::123456789012:policy/snow-globe-budget-hard-stop" &&
      aws_budgets_budget_action.hard_stop[0].definition[0].iam_action_definition[0].roles == toset(["snow-globe-scheduler"]) &&
      length(coalesce(aws_budgets_budget_action.hard_stop[0].definition[0].iam_action_definition[0].users, [])) == 0 &&
      length(coalesce(aws_budgets_budget_action.hard_stop[0].definition[0].iam_action_definition[0].groups, [])) == 0
    )
    error_message = "the stop attaches the hard-stop policy to the scheduler role only"
  }
  assert {
    condition     = aws_budgets_budget_action.hard_stop[0].execution_role_arn == "arn:aws:iam::123456789012:role/snow-globe-budget-action"
    error_message = "AWS Budgets runs the action as the budget-action role"
  }
  assert {
    condition     = [for s in aws_budgets_budget_action.hard_stop[0].subscriber : s.address] == ["alerts@example.com"] && alltrue([for s in aws_budgets_budget_action.hard_stop[0].subscriber : s.subscription_type == "EMAIL"])
    error_message = "the owner is emailed when the stop runs"
  }
  # The policy denies invoking every job, so the schedules stop.
  assert {
    condition = (
      jsondecode(aws_iam_policy.budget_hard_stop[0].policy).Statement[0].Effect == "Deny" &&
      jsondecode(aws_iam_policy.budget_hard_stop[0].policy).Statement[0].Action == ["lambda:InvokeFunction"] &&
      toset(jsondecode(aws_iam_policy.budget_hard_stop[0].policy).Statement[0].Resource) == toset(jsondecode(aws_iam_role_policy.scheduler.policy).Statement[0].Resource)
    )
    error_message = "the hard-stop policy must deny invoking every function the scheduler may invoke"
  }
  assert {
    condition     = length(jsondecode(aws_iam_policy.budget_hard_stop[0].policy).Statement) == 1
    error_message = "the hard-stop policy only denies"
  }
}

run "budget_action_role_is_least_privilege" {
  command = plan

  assert {
    condition = (
      jsondecode(aws_iam_role.budget_action[0].assume_role_policy).Statement[0].Principal.Service == "budgets.amazonaws.com" &&
      jsondecode(aws_iam_role.budget_action[0].assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "123456789012" &&
      jsondecode(aws_iam_role.budget_action[0].assume_role_policy).Statement[0].Condition.ArnLike["aws:SourceArn"] == "arn:aws:budgets::123456789012:budget/*"
    )
    error_message = "only AWS Budgets, for this account's budgets, may assume the action role (confused-deputy guard)"
  }
  assert {
    condition = (
      length(jsondecode(aws_iam_role_policy.budget_action[0].policy).Statement) == 1 &&
      toset(jsondecode(aws_iam_role_policy.budget_action[0].policy).Statement[0].Action) == toset(["iam:AttachRolePolicy", "iam:DetachRolePolicy"]) &&
      jsondecode(aws_iam_role_policy.budget_action[0].policy).Statement[0].Resource == "arn:aws:iam::123456789012:role/snow-globe-scheduler" &&
      jsondecode(aws_iam_role_policy.budget_action[0].policy).Statement[0].Condition.ArnEquals["iam:PolicyARN"] == "arn:aws:iam::123456789012:policy/snow-globe-budget-hard-stop"
    )
    error_message = "the action role may only attach or detach the hard-stop policy on the scheduler role"
  }
  assert {
    condition     = aws_iam_role_policy.budget_action[0].role == "snow-globe-budget-action"
    error_message = "the attach permission belongs to the budget-action role"
  }
}

run "hard_stop_can_be_turned_off" {
  command = plan

  variables {
    budget_hard_stop = false
  }

  assert {
    condition     = length(aws_budgets_budget_action.hard_stop) == 0 && length(aws_iam_policy.budget_hard_stop) == 0 && length(aws_iam_role.budget_action) == 0 && length(aws_iam_role_policy.budget_action) == 0
    error_message = "budget_hard_stop = false leaves emails only"
  }
  assert {
    condition     = aws_budgets_budget.monthly.limit_amount == "3"
    error_message = "the budget and its emails stay"
  }
}

run "budget_limit_is_a_variable" {
  command = plan

  variables {
    monthly_budget_usd = 5
  }

  assert {
    condition     = aws_budgets_budget.monthly.limit_amount == "5"
    error_message = "monthly_budget_usd must reach the budget"
  }
}

run "budget_can_be_scoped_to_the_project_tag" {
  command = plan

  variables {
    budget_scope_to_project_tag = true
  }

  assert {
    condition     = one([for f in aws_budgets_budget.monthly.cost_filter : f.name]) == "TagKeyValue"
    error_message = "tag-scoped budget uses a TagKeyValue filter"
  }
  assert {
    condition     = one([for f in aws_budgets_budget.monthly.cost_filter : f.values]) == tolist(["user:project$snow-globe"])
    error_message = "tag filter value must be user:project$snow-globe"
  }
}

run "too_many_budget_thresholds_are_rejected" {
  command = plan

  variables {
    budget_alert_thresholds_percent = [20, 40, 60, 80, 100]
  }

  expect_failures = [var.budget_alert_thresholds_percent]
}

run "phase_one_plans_without_an_image" {
  command = plan

  variables {
    image_tag = null
  }

  # What infra/deploy.sh applies before any image exists: the budget, the alarm email and the ECR repository.
  plan_options {
    target = [
      aws_budgets_budget.monthly,
      aws_sns_topic_subscription.alarm_email,
      aws_sns_topic_policy.alarms,
      aws_ecr_lifecycle_policy.lambda,
      aws_ecr_repository_policy.lambda,
    ]
  }

  assert {
    condition     = aws_budgets_budget.monthly.limit_amount == "3"
    error_message = "the budget is part of phase one"
  }
}

run "alarm_email_must_be_an_address" {
  command = plan

  variables {
    alarm_email = "not-an-address"
  }

  expect_failures = [var.alarm_email]
}

run "alarm_email_must_not_be_empty" {
  command = plan

  variables {
    alarm_email = ""
  }

  expect_failures = [var.alarm_email]
}
