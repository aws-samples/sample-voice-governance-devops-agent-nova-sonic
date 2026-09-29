# Outputs consumed by the bootstrap root: injected into the iac pipeline's
# CodeBuild environment (TF_STATE_BUCKET / TF_LOCK_TABLE) so the app layer's
# `terraform init` points at this backend, and surfaced as root outputs for
# the operator (Req 15.6).

output "state_bucket_name" {
  description = "Name of the S3 bucket holding the app layer's Terraform state; passed to the app layer's backend configuration (backend \"s3\" bucket argument)."
  value       = aws_s3_bucket.state.bucket
}

output "state_bucket_arn" {
  description = "ARN of the app-layer state bucket, used in IAM policies granting the iac pipeline's CodeBuild role state read/write access."
  value       = aws_s3_bucket.state.arn
}

output "lock_table_name" {
  description = "Name of the DynamoDB lock table serializing app-layer state operations; passed to the app layer's backend configuration (backend \"s3\" dynamodb_table argument)."
  value       = aws_dynamodb_table.lock.name
}

output "lock_table_arn" {
  description = "ARN of the DynamoDB lock table, used in IAM policies granting the iac pipeline's CodeBuild role lock access."
  value       = aws_dynamodb_table.lock.arn
}
