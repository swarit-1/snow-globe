output "data_bucket" {
  description = "Private data bucket (LOOKOUT_S3_BUCKET)."
  value       = aws_s3_bucket.data.bucket
}

output "ecr_repository_url" {
  description = "Push the Lambda image here: infra/lambda/build.sh --push <this>."
  value       = aws_ecr_repository.lambda.repository_url
}

output "image_tag" {
  description = "Image tag the functions run. Pass it again on later applies: -var image_tag=$(terraform output -raw image_tag)."
  value       = var.image_tag
}

output "functions" {
  description = "Lambda function name per job."
  value       = { for name, f in aws_lambda_function.job : name => f.function_name }
}

output "log_groups" {
  description = "CloudWatch log group per job (30-day retention)."
  value       = { for name, g in aws_cloudwatch_log_group.job : name => g.name }
}

output "schedules" {
  description = "EventBridge Scheduler schedule per job."
  value = {
    for name, s in aws_scheduler_schedule.job : name => {
      name       = s.name
      group      = s.group_name
      expression = s.schedule_expression
      state      = s.state
    }
  }
}

output "alarm_topic_arn" {
  description = "SNS topic for CloudWatch alarms; its email subscription must be confirmed from the inbox."
  value       = aws_sns_topic.alarms.arn
}

output "budget_name" {
  description = "AWS Budgets budget that emails alarm_email at each threshold."
  value       = aws_budgets_budget.monthly.name
}

output "budget_hard_stop_action_id" {
  description = "Id of the budget action that stops the schedules at the cap (null when budget_hard_stop = false). Reversing it early: infra/README.md, \"Hard stop\"."
  value       = one(aws_budgets_budget_action.hard_stop[*].action_id)
}
