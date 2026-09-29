output "lambda_function_name" {
  description = "Name of the Notifier Lambda function (observability module notifier-errors alarm dimension)."
  value       = aws_lambda_function.notifier.function_name
}

output "lambda_function_arn" {
  description = "ARN of the Notifier Lambda function."
  value       = aws_lambda_function.notifier.arn
}

output "event_rule_arns" {
  description = "ARNs of the three incident EventBridge rules on the default bus, keyed by source (cloudwatch_alarms, incident_manager, devops_agent_findings) (Req 5.1)."
  value = {
    cloudwatch_alarms     = aws_cloudwatch_event_rule.cloudwatch_alarms.arn
    incident_manager      = aws_cloudwatch_event_rule.incident_manager.arn
    devops_agent_findings = aws_cloudwatch_event_rule.devops_agent_findings.arn
  }
}

output "escalation_topic_arn" {
  description = "ARN of the SNS escalation topic (Req 5.11), or null when create_escalation_topic is false."
  value       = var.create_escalation_topic ? aws_sns_topic.escalation[0].arn : null
}
