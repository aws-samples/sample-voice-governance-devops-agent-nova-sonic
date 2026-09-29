output "policy_json" {
  description = "Rendered bucket policy JSON (caller statements merged with the DenyInsecureTransport and DenyTlsBelow12 statements)."
  value       = data.aws_iam_policy_document.this.json
}

output "bucket_id" {
  description = "Name (id) of the bucket the policy is attached to; referencing it orders dependent resources after the policy attachment."
  value       = aws_s3_bucket_policy.this.bucket
}
