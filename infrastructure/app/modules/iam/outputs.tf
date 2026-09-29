output "task_role_arn" {
  description = "ARN of the voice-service task role, for the ecs_service module's task definition."
  value       = aws_iam_role.task.arn
}

output "task_role_name" {
  description = "Name of the voice-service task role, for attaching additional environment-specific policies if ever needed."
  value       = aws_iam_role.task.name
}

output "execution_role_arn" {
  description = "ARN of the voice-service task execution role, for the ecs_service module's task definition."
  value       = aws_iam_role.execution.arn
}

output "execution_role_name" {
  description = "Name of the voice-service task execution role."
  value       = aws_iam_role.execution.name
}
