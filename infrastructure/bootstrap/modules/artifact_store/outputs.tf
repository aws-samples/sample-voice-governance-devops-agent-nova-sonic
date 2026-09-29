# Outputs consumed by the bootstrap root, which passes the bucket name to
# every pipeline instance's artifact_bucket_name input.

output "bucket_name" {
  description = "Name of the shared pipeline artifact bucket; passed to each pipeline module instance, which derives the ARN for its artifact store and IAM scoping."
  value       = aws_s3_bucket.artifacts.bucket
}

output "bucket_arn" {
  description = "ARN of the shared pipeline artifact bucket, used in IAM policies granting the pipelines and their CodeBuild projects artifact read/write access."
  value       = aws_s3_bucket.artifacts.arn
}
