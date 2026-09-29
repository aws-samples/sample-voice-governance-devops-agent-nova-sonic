# Outputs of the reusable CodeBuild project module.

output "project_name" {
  description = "Name of the CodeBuild project, referenced by pipeline stage actions."
  value       = aws_codebuild_project.this.name
}

output "project_arn" {
  description = "ARN of the CodeBuild project."
  value       = aws_codebuild_project.this.arn
}

output "service_role_arn" {
  description = "ARN of the IAM service role attached to the project (created by this module or passed in by the caller)."
  value       = local.service_role_arn
}

output "log_group_name" {
  description = "Name of the CloudWatch log group receiving build logs."
  value       = aws_cloudwatch_log_group.this.name
}
