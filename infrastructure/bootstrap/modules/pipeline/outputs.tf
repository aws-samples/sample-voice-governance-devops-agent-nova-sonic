# Outputs of the reusable CI/CD pipeline module.

output "pipeline_name" {
  description = "Name of the CodePipeline, used by EventBridge rules that start executions on source upload."
  value       = aws_codepipeline.this.name
}

output "pipeline_arn" {
  description = "ARN of the CodePipeline, used as the target of EventBridge rules and in IAM policies."
  value       = aws_codepipeline.this.arn
}

output "pipeline_role_arn" {
  description = "ARN of the IAM service role the pipeline runs as (created by this module or passed in by the caller)."
  value       = local.pipeline_role_arn
}

output "codebuild_project_names" {
  description = "Names of the per-stage CodeBuild projects, keyed by stage."
  value = {
    security_scan  = module.security_scan.project_name
    unit_test      = module.unit_test.project_name
    build_and_plan = module.build_and_plan.project_name
    deploy         = module.deploy.project_name
  }
}

output "codebuild_project_arns" {
  description = "ARNs of the per-stage CodeBuild projects, keyed by stage."
  value = {
    security_scan  = module.security_scan.project_arn
    unit_test      = module.unit_test.project_arn
    build_and_plan = module.build_and_plan.project_arn
    deploy         = module.deploy.project_arn
  }
}
