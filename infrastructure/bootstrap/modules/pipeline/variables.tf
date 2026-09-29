# Input variables for the reusable CI/CD pipeline module.

variable "name" {
  description = "Name of the pipeline. Also used as the prefix for the per-stage CodeBuild project names and derived IAM role names."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9_-]{1,38}$", var.name))
    error_message = "name must be 2-39 characters, start with a letter or digit, and contain only letters, digits, hyphens, and underscores. The 39-character cap keeps derived names (for example <name>-build-and-plan-codebuild) within the 64-character IAM role name limit."
  }
}

variable "source_bucket_name" {
  description = "Name of the versioned S3 bucket holding this pipeline's source archive (created by the source_buckets module)."
  type        = string

  validation {
    condition     = length(trimspace(var.source_bucket_name)) > 0
    error_message = "source_bucket_name must not be empty."
  }
}

variable "source_object_key" {
  description = "Object key of the source archive inside the source bucket (for example source.zip). The pipeline tracks new versions of this key."
  type        = string

  validation {
    condition     = length(trimspace(var.source_object_key)) > 0
    error_message = "source_object_key must not be empty."
  }
}

variable "artifact_bucket_name" {
  description = "Name of the S3 bucket used as the pipeline artifact store (created outside this module). Objects are protected by the bucket's default server-side encryption."
  type        = string

  validation {
    condition     = length(trimspace(var.artifact_bucket_name)) > 0
    error_message = "artifact_bucket_name must not be empty."
  }
}

variable "scan_buildspec_path" {
  description = "Path, inside the source artifact, of the buildspec for the SecurityScan stage (for example ci/backend/scan.yml)."
  type        = string

  validation {
    condition     = length(trimspace(var.scan_buildspec_path)) > 0
    error_message = "scan_buildspec_path must not be empty."
  }
}

variable "test_buildspec_path" {
  description = "Path, inside the source artifact, of the buildspec for the UnitTest stage."
  type        = string

  validation {
    condition     = length(trimspace(var.test_buildspec_path)) > 0
    error_message = "test_buildspec_path must not be empty."
  }
}

variable "build_buildspec_path" {
  description = "Path, inside the source artifact, of the buildspec for the BuildAndPlan stage."
  type        = string

  validation {
    condition     = length(trimspace(var.build_buildspec_path)) > 0
    error_message = "build_buildspec_path must not be empty."
  }
}

variable "deploy_buildspec_path" {
  description = "Path, inside the source artifact, of the buildspec for the Deploy stage."
  type        = string

  validation {
    condition     = length(trimspace(var.deploy_buildspec_path)) > 0
    error_message = "deploy_buildspec_path must not be empty."
  }
}

variable "environment_variables" {
  description = "Plaintext environment variables (name => value) exposed to every CodeBuild stage of this pipeline."
  type        = map(string)
  default     = {}
}

variable "build_environment_variables" {
  description = "Additional environment variables for the BuildAndPlan stage only, merged over environment_variables."
  type        = map(string)
  default     = {}
}

variable "deploy_environment_variables" {
  description = "Additional environment variables for the Deploy stage only, merged over environment_variables."
  type        = map(string)
  default     = {}
}

variable "build_extra_policy_documents" {
  description = "Additional IAM policy documents (JSON strings) attached to the BuildAndPlan stage's module-created CodeBuild role, granting exactly what that stage's buildspec needs beyond logs and artifact access (for example Terraform state read and the app-layer plan/refresh surface for the iac pipeline, or ECR push for the backend pipeline). Ignored when codebuild_service_role_arn is supplied."
  type        = list(string)
  default     = []
}

variable "deploy_extra_policy_documents" {
  description = "Additional IAM policy documents (JSON strings) attached to the Deploy stage's module-created CodeBuild role, granting exactly what that stage's buildspec needs beyond logs and artifact access (for example the app-layer apply surface for the iac pipeline, or ECS service update for the backend pipeline). Ignored when codebuild_service_role_arn is supplied."
  type        = list(string)
  default     = []
}

variable "build_privileged_mode" {
  description = "Run the BuildAndPlan CodeBuild container in privileged mode. Enable only when the build needs the Docker daemon (container image builds)."
  type        = bool
  default     = false
}

variable "deploy_privileged_mode" {
  description = "Run the Deploy CodeBuild container in privileged mode. Enable only when the deploy needs the Docker daemon."
  type        = bool
  default     = false
}

variable "codebuild_image" {
  description = "Container image used by every CodeBuild stage of this pipeline."
  type        = string
  default     = "aws/codebuild/standard:7.0"
}

variable "codebuild_compute_type" {
  description = "Compute type used by every CodeBuild stage of this pipeline."
  type        = string
  default     = "BUILD_GENERAL1_SMALL"
}

variable "pipeline_service_role_arn" {
  description = "ARN of an existing IAM service role for CodePipeline. When null, the module creates a least-privilege role scoped to this pipeline's buckets and CodeBuild projects."
  type        = string
  default     = null
}

variable "codebuild_service_role_arn" {
  description = "ARN of an existing IAM service role shared by all CodeBuild projects of this pipeline. When null, each project creates its own least-privilege role."
  type        = string
  default     = null
}

variable "approval_sns_topic_arn" {
  description = "Optional SNS topic ARN notified when the pipeline reaches the ManualApproval stage. When null, no notification is sent and approvers must watch the console."
  type        = string
  default     = null
}

variable "log_retention_days" {
  description = "Retention period in days for the CloudWatch log groups of this pipeline's CodeBuild projects."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Tags applied to every resource created by this module."
  type        = map(string)
  default     = {}
}
