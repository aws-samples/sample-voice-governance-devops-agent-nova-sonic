output "ops_topic_arn" {
  description = "ARN of the operations SNS topic every alarm publishes to on entering ALARM (Req 19.3)."
  value       = aws_sns_topic.ops.arn
}

output "alarm_names" {
  description = "Names of the four CloudWatch alarms, keyed by alarm (running_task_count, alb_unhealthy_targets, voice_5xx, notifier_errors) (Req 19.2)."
  value = {
    running_task_count    = aws_cloudwatch_metric_alarm.running_task_count.alarm_name
    alb_unhealthy_targets = aws_cloudwatch_metric_alarm.alb_unhealthy_targets.alarm_name
    voice_5xx             = aws_cloudwatch_metric_alarm.voice_5xx.alarm_name
    notifier_errors       = aws_cloudwatch_metric_alarm.notifier_errors.alarm_name
  }
}

output "alarm_arns" {
  description = "ARNs of the four CloudWatch alarms, keyed by alarm (running_task_count, alb_unhealthy_targets, voice_5xx, notifier_errors) (Req 19.2)."
  value = {
    running_task_count    = aws_cloudwatch_metric_alarm.running_task_count.arn
    alb_unhealthy_targets = aws_cloudwatch_metric_alarm.alb_unhealthy_targets.arn
    voice_5xx             = aws_cloudwatch_metric_alarm.voice_5xx.arn
    notifier_errors       = aws_cloudwatch_metric_alarm.notifier_errors.arn
  }
}
