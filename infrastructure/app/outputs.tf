# App-layer outputs (Req 14.3).
#
# The first group carries everything the frontend pipeline needs to
# generate frontend/config.json at deploy time (see
# frontend/config.example.json for the shape) — the built SPA artifact
# stays environment-independent. The second group feeds the frontend
# pipeline's deploy stage and the bootstrap layer's two-phase variables;
# the third group is operational reference data.

# ---------------------------------------------------------------------------
# Frontend config.json inputs (Req 14.3).
# ---------------------------------------------------------------------------

output "aws_region" {
  description = "Region the layer is deployed to (config.json region)."
  value       = var.aws_region
}

output "cognito_user_pool_id" {
  description = "ID of the Cognito user pool (config.json cognito.userPoolId; Voice_Service COGNITO_USER_POOL_ID)."
  value       = module.cognito.user_pool_id
}

output "cognito_client_id" {
  description = "ID of the Cognito SPA app client (config.json cognito.clientId; Voice_Service COGNITO_CLIENT_ID)."
  value       = module.cognito.client_id
}

output "cognito_hosted_ui_domain" {
  description = "Hosted UI domain prefix of the Cognito user pool."
  value       = module.cognito.domain
}

output "cognito_hosted_ui_url" {
  description = "Base URL of the Cognito hosted UI the SPA runs the PKCE flow against (config.json cognito.domain)."
  value       = module.cognito.hosted_ui_base_url
}

output "cognito_issuer_url" {
  description = "OIDC issuer URL of the user pool, validated by the Voice_Service JWT validator (Req 7.2)."
  value       = module.cognito.issuer_url
}

output "cloudfront_domain_name" {
  description = "CloudFront default domain name serving the portal."
  value       = module.cloudfront_s3.distribution_domain_name
}

output "portal_url" {
  description = "HTTPS URL of the portal on the CloudFront default domain (the Cognito callback/logout origin)."
  value       = "https://${module.cloudfront_s3.distribution_domain_name}"
}

output "voice_ws_url" {
  description = "WebSocket URL of the voice endpoint, riding through CloudFront to the ALB (config.json voiceWsUrl)."
  value       = "wss://${module.cloudfront_s3.distribution_domain_name}/ws/voice"
}

output "appsync_events_http_endpoint" {
  description = "HTTP publish endpoint of the AppSync Events API (config.json events.httpEndpoint)."
  value       = module.appsync_events.http_endpoint
}

output "appsync_events_realtime_endpoint" {
  description = "Realtime WebSocket endpoint of the AppSync Events API (config.json events.realtimeEndpoint)."
  value       = module.appsync_events.realtime_endpoint
}

output "vapid_public_key" {
  description = "VAPID public key browsers subscribe with (config.json vapidPublicKey); pass-through of the input variable, not sensitive."
  value       = var.vapid_public_key
}

# ---------------------------------------------------------------------------
# Frontend pipeline deploy targets and bootstrap two-phase inputs.
# ---------------------------------------------------------------------------

output "cloudfront_distribution_id" {
  description = "ID of the CloudFront distribution, for frontend-pipeline cache invalidations and the bootstrap layer's cloudfront_distribution_id variable (two-phase flow)."
  value       = module.cloudfront_s3.distribution_id
}

output "frontend_bucket_name" {
  description = "Name of the frontend bucket, the aws s3 sync target of the frontend pipeline and the bootstrap layer's frontend_bucket_name variable (two-phase flow)."
  value       = module.cloudfront_s3.frontend_bucket_name
}

# ---------------------------------------------------------------------------
# Operational reference.
# ---------------------------------------------------------------------------

output "alb_dns_name" {
  description = "DNS name of the voice ALB (the CloudFront origin; not directly reachable thanks to the origin-verify rule)."
  value       = module.alb.alb_dns_name
}

output "ecs_cluster_name" {
  description = "Name of the voice ECS cluster (backend-pipeline deploy target)."
  value       = module.ecs_service.cluster_name
}

output "ecs_service_name" {
  description = "Name of the voice ECS service (backend-pipeline deploy target)."
  value       = module.ecs_service.service_name
}

output "notifier_function_name" {
  description = "Name of the Notifier Lambda function."
  value       = module.notifications.lambda_function_name
}

output "guardrail_id" {
  description = "ID of the Bedrock guardrail the Voice_Service evaluates before every DevOps Agent call (Voice_Service GUARDRAIL_ID)."
  value       = module.bedrock_guardrail.guardrail_id
}

output "guardrail_version" {
  description = "Published guardrail version the Voice_Service evaluates with ApplyGuardrail (Voice_Service GUARDRAIL_VERSION)."
  value       = module.bedrock_guardrail.guardrail_version
}

output "devops_agent_assumable_role_arn" {
  description = "ARN of the read-only role the AWS DevOps Agent assumes to inspect this account. Supply it as configuration.aws.assumableRoleArn when registering the account with the agent space via AssociateService — an operator step, because the AWS provider has no devops-agent resources. Until that association exists the agent can read nothing and every diagnostic answer comes back empty."
  value       = module.devops_agent_access.assumable_role_arn
}

output "dynamodb_table_names" {
  description = "Names of the four Session_Store tables, keyed by purpose (voice_sessions, agent_chats, push_subscriptions, transcripts)."
  value = {
    voice_sessions     = module.dynamodb.voice_sessions_table_name
    agent_chats        = module.dynamodb.agent_chats_table_name
    push_subscriptions = module.dynamodb.push_subscriptions_table_name
    transcripts        = module.dynamodb.transcripts_table_name
  }
}
