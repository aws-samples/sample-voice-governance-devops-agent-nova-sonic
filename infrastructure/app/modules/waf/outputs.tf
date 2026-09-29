output "web_acl_arn" {
  description = "ARN of the web ACL; the CLOUDFRONT-scope ARN feeds the distribution's web_acl_id, the REGIONAL-scope ARN is already associated with the ALB."
  value       = aws_wafv2_web_acl.this.arn
}

output "web_acl_id" {
  description = "ID of the web ACL."
  value       = aws_wafv2_web_acl.this.id
}

output "web_acl_name" {
  description = "Name of the web ACL."
  value       = aws_wafv2_web_acl.this.name
}

output "log_group_name" {
  description = "Name of the CloudWatch Logs log group receiving WAF logs (aws-waf-logs-* prefix, Req 11.5, 11.6)."
  value       = aws_cloudwatch_log_group.waf.name
}

output "log_group_arn" {
  description = "ARN of the WAF log group."
  value       = aws_cloudwatch_log_group.waf.arn
}
