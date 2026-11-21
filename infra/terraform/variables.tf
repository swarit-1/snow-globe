# ---- required -----------------------------------------------------------------------------------------

variable "alarm_email" {
  description = "Address that receives CloudWatch alarms (through SNS) and AWS Budgets notifications. Required, with no default: set TF_VAR_alarm_email in your shell and never commit it."
  type        = string
  nullable    = false
  sensitive   = true

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alarm_email))
    error_message = "alarm_email must be an email address. Set TF_VAR_alarm_email (see infra/README.md)."
  }
}

# ---- set by infra/deploy.sh ---------------------------------------------------------------------------

variable "image_tag" {
  description = "Tag of the Lambda image in ECR, as printed by infra/lambda/build.sh. Leave null for phase 1 of a first deploy (budget, alarm topic and ECR, applied with -target); the Lambda functions refuse to plan without it."
  type        = string
  default     = null
}

# ---- optional -----------------------------------------------------------------------------------------

variable "region" {
  description = "AWS region. us-east-1 is where NOAA's HRRR bucket (noaa-hrrr-bdp-pds) lives, so the forecast worker's reads stay in-region and cost nothing in transfer."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Prefix for every resource name, and the value of the `project` tag."
  type        = string
  default     = "snow-globe"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.project))
    error_message = "project must be 2-21 lowercase letters, digits or hyphens, starting with a letter (it prefixes bucket and function names)."
  }
}

variable "user_agent" {
  description = "User-Agent the workers send (SNOW_GLOBE_USER_AGENT). Never put the owner's name, email or GitHub handle in it (CLAUDE.md privacy rule)."
  type        = string
  default     = "SnowGlobe/0.1 (webcam research capture; fetch-on-change)"

  validation {
    condition     = length(var.user_agent) > 0 && !strcontains(var.user_agent, "@")
    error_message = "user_agent must be non-empty and must not contain an email address."
  }
}

variable "capture_schedule_expression" {
  description = "How often the capture job runs. NPS cameras may be polled at most every 5 minutes (PRD §15.3), so only rate(N minutes) with 5 <= N <= 60, or rate(1 hour), is accepted."
  type        = string
  default     = "rate(5 minutes)"

  validation {
    condition = var.capture_schedule_expression == "rate(1 hour)" || (
      can(regex("^rate\\(([0-9]+) minutes\\)$", var.capture_schedule_expression)) &&
      try(tonumber(regex("^rate\\(([0-9]+) minutes\\)$", var.capture_schedule_expression)[0]) >= 5, false) &&
      try(tonumber(regex("^rate\\(([0-9]+) minutes\\)$", var.capture_schedule_expression)[0]) <= 60, false)
    )
    error_message = "capture_schedule_expression must be rate(N minutes) with 5 <= N <= 60, or rate(1 hour): NPS is polled at most every 5 minutes (PRD §15.3)."
  }
}

variable "forecast_schedule_expression" {
  description = "How often the forecast job runs. PRD §11.2: every 5 minutes, so an HRRR run is written within 5 minutes of reaching NOAA's bucket (most runs only find that nothing new is ready). Accepted forms: rate(1 minute), rate(N minutes) with 2 <= N <= 60, or rate(1 hour)."
  type        = string
  default     = "rate(5 minutes)"

  validation {
    condition = contains(["rate(1 minute)", "rate(1 hour)"], var.forecast_schedule_expression) || (
      can(regex("^rate\\(([0-9]+) minutes\\)$", var.forecast_schedule_expression)) &&
      try(tonumber(regex("^rate\\(([0-9]+) minutes\\)$", var.forecast_schedule_expression)[0]) >= 2, false) &&
      try(tonumber(regex("^rate\\(([0-9]+) minutes\\)$", var.forecast_schedule_expression)[0]) <= 60, false)
    )
    error_message = "forecast_schedule_expression must be rate(1 minute), rate(N minutes) with 2 <= N <= 60, or rate(1 hour)."
  }
}

variable "capture_schedule_enabled" {
  description = "false pauses the capture schedule and silences its 'not running' alarm. Pause it while any local NPS fetcher runs: two NPS fetchers must never run at once (CLAUDE.md)."
  type        = bool
  default     = true
}

variable "forecast_schedule_enabled" {
  description = "false pauses the forecast schedule and silences its 'not running' alarm."
  type        = bool
  default     = true
}

variable "monthly_budget_usd" {
  description = "Monthly AWS Budgets limit in USD. The default is PRD §17's hosting hard cap, $3. AWS Budgets emails at budget_alert_thresholds_percent of it (actual spend) and when the month's forecast passes it; at 100 % of actual spend, budget_hard_stop stops the schedules."
  type        = number
  default     = 3

  validation {
    condition     = var.monthly_budget_usd > 0
    error_message = "monthly_budget_usd must be greater than 0."
  }
}

variable "budget_alert_thresholds_percent" {
  description = "Percentages of monthly_budget_usd at which AWS Budgets emails alarm_email about actual spend. The default emails at about $1, $2 and $3 of the $3 cap. At most 4 values: a budget holds 5 notifications, and one is the forecast alert."
  type        = list(number)
  default     = [33, 67, 100]

  validation {
    condition     = length(var.budget_alert_thresholds_percent) > 0 && length(var.budget_alert_thresholds_percent) <= 4 && alltrue([for t in var.budget_alert_thresholds_percent : t > 0 && t <= 1000])
    error_message = "budget_alert_thresholds_percent needs 1 to 4 values, each in (0, 1000]."
  }
}

variable "budget_hard_stop" {
  description = "true (default; PRD §17: hard caps are enforced in code): at 100 % of monthly_budget_usd in actual spend, AWS Budgets attaches a deny policy to the scheduler role, so no job runs until AWS resets the action at the start of the next month or you reverse it (infra/README.md). Storage keeps costing a few cents a month; nothing is deleted. false: emails only."
  type        = bool
  default     = true
}

variable "data_noncurrent_version_days" {
  description = "Days the versioned data bucket keeps the previous version of an overwritten or deleted object, so a buggy run cannot destroy evaluation evidence (PRD §6.2: scoring reads the forecast file the app showed). The logs (log_noncurrent_version_days) and cache/ (1 day) keep theirs for less."
  type        = number
  default     = 30

  validation {
    condition     = var.data_noncurrent_version_days >= 1 && floor(var.data_noncurrent_version_days) == var.data_noncurrent_version_days
    error_message = "data_noncurrent_version_days must be a whole number of days, at least 1."
  }
}

variable "log_noncurrent_version_days" {
  description = "Days the previous versions of the daily logs (capture_log/, forecast_log/) are kept. S3 has no append, so every logged record rewrites the whole day's log and leaves a near-copy behind: about 1-5 GB of old versions a day at 8 cameras (infra/README.md, cost estimate). 1 day is enough to undo a bad write noticed the same day."
  type        = number
  default     = 1

  validation {
    condition     = var.log_noncurrent_version_days >= 1 && floor(var.log_noncurrent_version_days) == var.log_noncurrent_version_days
    error_message = "log_noncurrent_version_days must be a whole number of days, at least 1."
  }
}

variable "budget_scope_to_project_tag" {
  description = "false (default): the budget counts every charge in the account, the safe choice for an account used only by Snow Globe. true: only charges tagged project=<project> count. That needs the `project` cost-allocation tag activated in the Billing console first, and untagged charges then go unseen."
  type        = bool
  default     = false
}

variable "data_bucket_name" {
  description = "Name of the private data bucket. Empty means <project>-data-<account id>."
  type        = string
  default     = ""
}

variable "data_bucket_force_destroy" {
  description = "Allow `terraform destroy` to delete the data bucket while it still holds frames and forecasts. Leave false; set true only for the final teardown, after copying the data out."
  type        = bool
  default     = false
}
