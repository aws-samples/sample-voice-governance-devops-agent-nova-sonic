# Input variables for the CI/CD stage-permission policy module.

variable "environment" {
  description = "Environment name (for example dev or prod). Every app-layer resource name is prefixed with it, so it anchors the resource-level scoping of the deploy permissions (roles, tables, functions, topics, rules, buckets)."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.environment))
    error_message = "environment must start with a lowercase letter and contain only lowercase letters, digits, and hyphens."
  }
}

variable "state_bucket_arn" {
  description = "ARN of the app-layer Terraform state bucket (state_backend module output). The iac stages get object-scoped access to the app state key and the app-outputs export prefix; the frontend deploy stage gets read access to the exported outputs object."
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-z-]*:s3:::", var.state_bucket_arn))
    error_message = "state_bucket_arn must be an S3 bucket ARN (arn:aws...:s3:::<bucket>)."
  }
}

variable "lock_table_arn" {
  description = "ARN of the DynamoDB lock table serializing app-layer state operations (state_backend module output)."
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-z-]*:dynamodb:", var.lock_table_arn))
    error_message = "lock_table_arn must be a DynamoDB table ARN."
  }
}

variable "ecr_repository_arn" {
  description = "ARN of the Voice_Service ECR repository (ecr module output); the backend build stage's push permissions are scoped to it."
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-z-]*:ecr:", var.ecr_repository_arn))
    error_message = "ecr_repository_arn must be an ECR repository ARN."
  }
}

variable "ecs_cluster_name" {
  description = "Name of the ECS cluster the backend pipeline's deploy stage updates; scopes the deploy stage's ecs:DescribeServices / ecs:UpdateService grant."
  type        = string

  validation {
    condition     = length(trimspace(var.ecs_cluster_name)) > 0
    error_message = "ecs_cluster_name must not be empty."
  }
}

variable "ecs_service_name" {
  description = "Name of the ECS service the backend pipeline's deploy stage updates; scopes the deploy stage's ecs:DescribeServices / ecs:UpdateService grant."
  type        = string

  validation {
    condition     = length(trimspace(var.ecs_service_name)) > 0
    error_message = "ecs_service_name must not be empty."
  }
}

variable "ssm_parameter_path_prefix" {
  description = "SSM Parameter Store path prefix under which the app layer creates its parameters (must match the app layer's ssm_parameter_path_prefix). When null, it is derived as /<environment>/voice-service to match the app layer's default. Must start with / and not end with / (a trailing slash would render a broken parameter// ARN)."
  type        = string
  default     = null

  validation {
    condition     = var.ssm_parameter_path_prefix == null || can(regex("^/.+[^/]$", var.ssm_parameter_path_prefix))
    error_message = "ssm_parameter_path_prefix must be null or start with / and not end with / (for example /dev/voice-service)."
  }
}

variable "frontend_bucket_name" {
  description = "Name of the frontend hosting bucket the frontend pipeline's deploy stage syncs to. Empty (the default) on the first bootstrap apply — the bucket is created later by the app layer (two-phase flow); while empty, the frontend deploy policy simply omits the bucket-sync statements."
  type        = string
  default     = ""
}

variable "cloudfront_distribution_id" {
  description = "Id of the CloudFront distribution the frontend pipeline's deploy stage invalidates. Empty (the default) on the first bootstrap apply (two-phase flow); while empty, the frontend deploy policy simply omits the invalidation statement."
  type        = string
  default     = ""
}
