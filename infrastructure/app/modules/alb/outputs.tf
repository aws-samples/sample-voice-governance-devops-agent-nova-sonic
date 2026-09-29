output "alb_arn" {
  description = "ARN of the internet-facing voice ALB, for attaching the REGIONAL WAF web ACL (waf module)."
  value       = aws_lb.this.arn
}

output "alb_arn_suffix" {
  description = "ARN suffix of the ALB (app/{name}/{id}), the LoadBalancer dimension for CloudWatch metrics driving autoscaling and alarms."
  value       = aws_lb.this.arn_suffix
}

output "alb_dns_name" {
  description = "DNS name of the ALB, the origin domain for the CloudFront distribution's /ws/* and /api/* behaviors (cloudfront_s3 module)."
  value       = aws_lb.this.dns_name
}

output "alb_zone_id" {
  description = "Route 53 hosted zone ID of the ALB, for alias records if a custom domain is ever attached."
  value       = aws_lb.this.zone_id
}

output "target_group_arn" {
  description = "ARN of the voice target group, for the ECS service load_balancer attachment (ecs_service module)."
  value       = aws_lb_target_group.voice.arn
}

output "target_group_arn_suffix" {
  description = "ARN suffix of the voice target group (targetgroup/{name}/{id}), the TargetGroup dimension for CloudWatch target-health alarms (observability module)."
  value       = aws_lb_target_group.voice.arn_suffix
}

output "security_group_id" {
  description = "ID of the ALB security group, referenced by the ECS task security group to admit traffic only from the ALB."
  value       = aws_security_group.alb.id
}

output "listener_arn" {
  description = "ARN of the HTTP :80 listener carrying the origin-verify forwarding rule."
  value       = aws_lb_listener.http.arn
}
