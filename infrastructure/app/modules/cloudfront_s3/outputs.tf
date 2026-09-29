output "distribution_id" {
  description = "ID of the CloudFront distribution, for frontend-pipeline cache invalidations."
  value       = aws_cloudfront_distribution.this.id
}

output "distribution_arn" {
  description = "ARN of the CloudFront distribution."
  value       = aws_cloudfront_distribution.this.arn
}

output "distribution_domain_name" {
  description = "CloudFront default domain name serving the portal (frontend config.json portal URL and wss://{domain}/ws/voice endpoint)."
  value       = aws_cloudfront_distribution.this.domain_name
}

output "frontend_bucket_name" {
  description = "Name of the frontend bucket, the aws s3 sync target of the frontend pipeline."
  value       = aws_s3_bucket.frontend.id
}

output "frontend_bucket_arn" {
  description = "ARN of the frontend bucket, for frontend-pipeline deploy-role policy scoping."
  value       = aws_s3_bucket.frontend.arn
}
