# Outputs consumed by the sibling pipeline module (S3 source action location)
# and by scripts/push-source.sh (upload destination).

output "bucket_names" {
  description = "Source bucket names keyed by source name (frontend, backend, iac); used by the pipeline module's S3 source action and by scripts/push-source.sh as the upload destination."
  value       = { for source, bucket in aws_s3_bucket.source : source => bucket.bucket }
}

output "bucket_arns" {
  description = "Source bucket ARNs keyed by source name (frontend, backend, iac); used for IAM policies granting the pipelines read access to their source archives."
  value       = { for source, bucket in aws_s3_bucket.source : source => bucket.arn }
}

output "source_object_key" {
  description = "Object key of the source archive within every source bucket; the pipeline module's S3 source action and scripts/push-source.sh must use this exact key."
  value       = var.source_object_key
}

output "event_rule_arns" {
  description = "EventBridge rule ARNs keyed by source name (frontend, backend, iac); each rule starts the matching pipeline when the source archive is uploaded."
  value       = { for source, rule in aws_cloudwatch_event_rule.source_upload : source => rule.arn }
}

output "trigger_role_arns" {
  description = "IAM role ARNs keyed by source name (frontend, backend, iac); each role is assumed by EventBridge to call codepipeline:StartPipelineExecution on the matching pipeline."
  value       = { for source, role in aws_iam_role.source_trigger : source => role.arn }
}
