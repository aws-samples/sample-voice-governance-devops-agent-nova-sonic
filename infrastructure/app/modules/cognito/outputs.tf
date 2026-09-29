output "user_pool_id" {
  description = "ID of the Cognito user pool (Voice_Service COGNITO_USER_POOL_ID and frontend config.json cognito.userPoolId)."
  value       = aws_cognito_user_pool.this.id
}

output "user_pool_arn" {
  description = "ARN of the Cognito user pool."
  value       = aws_cognito_user_pool.this.arn
}

output "client_id" {
  description = "ID of the SPA app client (Voice_Service COGNITO_CLIENT_ID and frontend config.json cognito.clientId)."
  value       = aws_cognito_user_pool_client.spa.id
}

output "domain" {
  description = "Hosted UI domain prefix of the user pool."
  value       = aws_cognito_user_pool_domain.this.domain
}

output "hosted_ui_base_url" {
  description = "Base URL of the Cognito hosted UI (frontend config.json cognito.domain)."
  value       = "https://${aws_cognito_user_pool_domain.this.domain}.auth.${data.aws_region.current.region}.amazoncognito.com"
}

output "issuer_url" {
  description = "OIDC issuer URL of the user pool, validated by the Voice_Service JWT validator (Req 7.2)."
  value       = "https://${aws_cognito_user_pool.this.endpoint}"
}
