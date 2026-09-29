# Outputs of the bootstrap layer root, consumed by the operator and the
# README's deployment steps: where to push source archives, where images
# land, and which backend the app layer's terraform init points at.

output "source_bucket_names" {
  description = "Source bucket names keyed by pipeline (frontend, backend, iac); scripts/push-source.sh uploads each source archive here to start the matching pipeline."
  value       = module.source_buckets.bucket_names
}

output "source_object_key" {
  description = "Object key every source archive must be uploaded under (the EventBridge rules and S3 source actions match exactly this key)."
  value       = module.source_buckets.source_object_key
}

output "pipeline_names" {
  description = "CodePipeline names keyed by pipeline (frontend, backend, iac), for console lookup and aws codepipeline CLI calls."
  value = {
    frontend = module.pipeline_frontend.pipeline_name
    backend  = module.pipeline_backend.pipeline_name
    iac      = module.pipeline_iac.pipeline_name
  }
}

output "ecr_repository_url" {
  description = "URL of the Voice_Service ECR repository; the backend pipeline pushes images here and the app layer's task definition pulls from it."
  value       = module.ecr.repository_url
}

output "artifact_bucket_name" {
  description = "Name of the S3 bucket all three pipelines share as their artifact store."
  value       = module.artifact_store.bucket_name
}

output "state_bucket_name" {
  description = "Name of the S3 bucket holding the app layer's Terraform state (backend \"s3\" bucket argument); the bootstrap layer's own state stays local (Req 15.6)."
  value       = module.state_backend.state_bucket_name
}

output "lock_table_name" {
  description = "Name of the DynamoDB table serializing app-layer state operations (backend \"s3\" dynamodb_table argument)."
  value       = module.state_backend.lock_table_name
}

# ---------------------------------------------------------------------------
# Web Push VAPID key (present only when create_vapid_key = true). Both are
# non-sensitive: the private key is written to SSM by the vapid module's
# external program and never enters Terraform state. Feed these into the
# app layer's envs/<env>.tfvars (vapid_public_key and
# vapid_private_key_parameter_name) to complete the app deploy without a
# manual key-generation step. Null when create_vapid_key is false.
# ---------------------------------------------------------------------------

output "vapid_public_key" {
  description = "VAPID public key (base64url) matching the private key stored in SSM; set the app layer's vapid_public_key to this. Null when create_vapid_key is false."
  value       = one(module.vapid[*].public_key)
}

output "vapid_private_key_parameter_name" {
  description = "Name of the SSM SecureString holding the VAPID private key; set the app layer's vapid_private_key_parameter_name to this. Null when create_vapid_key is false."
  value       = one(module.vapid[*].private_key_parameter_name)
}
