# Policy documents consumed by the bootstrap root, which threads them onto
# the matching pipeline stage roles via the pipeline module's
# build_extra_policy_documents / deploy_extra_policy_documents inputs.

output "tf_state_policy_json" {
  description = "Terraform state-backend access (state object read/write, lock table) for the iac pipeline's BuildAndPlan and Deploy stages."
  value       = data.aws_iam_policy_document.tf_state.json
}

output "app_read_policy_json" {
  description = "Read-only app-layer refresh surface (control-plane Describe/Get/List, no data-plane reads) for the iac pipeline's BuildAndPlan and Deploy stages."
  value       = data.aws_iam_policy_document.app_read.json
}

output "app_manage_core_policy_json" {
  description = "App-layer apply surface part 1 (network, load balancing, ECS, auto scaling, Cognito, WAF, CloudFront, frontend bucket) for the iac pipeline's Deploy stage only."
  value       = data.aws_iam_policy_document.app_manage_core.json
}

output "app_manage_platform_policy_json" {
  description = "App-layer apply surface part 2 (DynamoDB, AppSync Events, Bedrock guardrail, EventBridge, Lambda, SNS, alarms, log groups, IAM runtime roles, SSM, outputs export) for the iac pipeline's Deploy stage only."
  value       = data.aws_iam_policy_document.app_manage_platform.json
}

output "backend_build_policy_json" {
  description = "ECR auth + push, scoped to the Voice_Service repository, for the backend pipeline's BuildAndPlan stage."
  value       = data.aws_iam_policy_document.backend_build.json
}

output "backend_deploy_policy_json" {
  description = "ECS task-definition registration, service update scoped to the voice service, and iam:PassRole on the task/execution roles, for the backend pipeline's Deploy stage."
  value       = data.aws_iam_policy_document.backend_deploy.json
}

output "frontend_deploy_policy_json" {
  description = "Exported-outputs read plus (once two-phase wiring supplies real values) frontend bucket sync and CloudFront invalidation, for the frontend pipeline's Deploy stage."
  value       = data.aws_iam_policy_document.frontend_deploy.json
}
