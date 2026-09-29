output "cluster_name" {
  description = "Name of the voice ECS cluster (Voice_Service ECS_CLUSTER_NAME and observability alarm dimension)."
  value       = aws_ecs_cluster.this.name
}

output "cluster_arn" {
  description = "ARN of the voice ECS cluster, for scoping the task role's ecs:UpdateTaskProtection permission (iam module)."
  value       = aws_ecs_cluster.this.arn
}

output "service_name" {
  description = "Name of the voice ECS service (observability alarm dimension and backend-pipeline deploy target)."
  value       = aws_ecs_service.voice.name
}

output "task_definition_arn" {
  description = "ARN (with revision) of the voice task definition."
  value       = aws_ecs_task_definition.voice.arn
}

output "task_security_group_id" {
  description = "ID of the security group attached to the Fargate tasks."
  value       = aws_security_group.tasks.id
}

output "log_group_name" {
  description = "Name of the service CloudWatch log group, for error-rate metric filters (observability module)."
  value       = aws_cloudwatch_log_group.service.name
}
