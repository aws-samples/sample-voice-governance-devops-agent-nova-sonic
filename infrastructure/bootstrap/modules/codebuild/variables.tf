# Input variables for the reusable CodeBuild project module.

variable "name" {
  description = "Name of the CodeBuild project. Also used to derive the CloudWatch log group name and, when no service role is supplied, the IAM service role name."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9_-]{1,58}$", var.name))
    error_message = "name must be 2-59 characters, start with a letter or digit, and contain only letters, digits, hyphens, and underscores (keeps the derived IAM role name within the 64-character limit)."
  }
}

variable "description" {
  description = "Human-readable description of the CodeBuild project."
  type        = string
  default     = null
}

variable "buildspec_path" {
  description = "Path to the buildspec file inside the pipeline source artifact (for example ci/backend/scan.yml). The buildspec is read from the source artifact at build time, so build logic is versioned with the source code rather than baked into infrastructure."
  type        = string

  validation {
    condition     = length(trimspace(var.buildspec_path)) > 0
    error_message = "buildspec_path must not be empty; provide a path inside the source artifact such as ci/backend/scan.yml."
  }
}

variable "image" {
  description = "Container image for the build environment."
  type        = string
  default     = "aws/codebuild/standard:7.0"
}

variable "compute_type" {
  description = "Compute type for the build environment."
  type        = string
  default     = "BUILD_GENERAL1_SMALL"
}

variable "environment_type" {
  description = "Build environment type (for example LINUX_CONTAINER or ARM_CONTAINER)."
  type        = string
  default     = "LINUX_CONTAINER"
}

variable "privileged_mode" {
  description = "Whether to run the build container in privileged mode. Enable only for builds that need the Docker daemon (building or pushing container images)."
  type        = bool
  default     = false
}

variable "environment_variables" {
  description = "Map of plaintext environment variables (name => value) exposed to the build. Do not put secret values here; have the buildspec resolve secrets from SSM Parameter Store or Secrets Manager at build time instead."
  type        = map(string)
  default     = {}
}

variable "service_role_arn" {
  description = "ARN of an existing IAM service role for the project. When null, the module creates a least-privilege role scoped to this project's CloudWatch log group and the pipeline artifact bucket."
  type        = string
  default     = null
}

variable "artifact_bucket_arn" {
  description = "ARN of the pipeline artifact store bucket. Required when service_role_arn is null so the created role can read input artifacts and write output artifacts."
  type        = string
  default     = null

  validation {
    condition     = var.service_role_arn != null || var.artifact_bucket_arn != null
    error_message = "artifact_bucket_arn is required when service_role_arn is null, so the created service role can be scoped to the pipeline artifact bucket instead of a wildcard resource."
  }
}

variable "build_timeout_minutes" {
  description = "Build timeout in minutes, after which CodeBuild stops the build and marks it failed."
  type        = number
  default     = 60

  validation {
    condition     = var.build_timeout_minutes >= 5 && var.build_timeout_minutes <= 2160
    error_message = "build_timeout_minutes must be between 5 and 2160."
  }
}

variable "log_retention_days" {
  description = "Retention period in days for the project's CloudWatch log group."
  type        = number
  default     = 30

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.log_retention_days)
    error_message = "log_retention_days must be one of the retention values supported by CloudWatch Logs."
  }
}

variable "tags" {
  description = "Tags applied to every resource created by this module."
  type        = map(string)
  default     = {}
}

variable "extra_policy_documents" {
  description = "Additional IAM policy documents (JSON strings) attached to the module-created service role as customer-managed policies, granting the stage-specific permissions the buildspec needs (for example Terraform state access and the app-layer apply surface for the iac deploy stage, or ECR push for the backend build stage). Ignored when service_role_arn is supplied — the caller then owns the role's policies. Each document must stay within the 6,144-character managed-policy quota."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for doc in var.extra_policy_documents : can(jsondecode(doc))])
    error_message = "Every extra_policy_documents entry must be a valid JSON policy document."
  }
}
