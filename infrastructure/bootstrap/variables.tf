# Input variables for the bootstrap layer root.
# Every environment-specific value arrives here — nothing is hardcoded in
# resource definitions, and required variables carry no defaults so an unset
# value fails `terraform plan` with a clear error (Req 15.5, 15.7).

variable "project_name" {
  description = "Short project identifier used as the prefix for every bootstrap resource name (lowercase letters, digits, and hyphens; must start with a letter). Keep it short: it participates in S3 bucket names (63-character limit) and pipeline names (39-character limit)."
  type        = string

  validation {
    condition     = can(regex("^[a-z](?:[a-z0-9-]*[a-z0-9])?$", var.project_name))
    error_message = "project_name must start with a lowercase letter, contain only lowercase letters, digits, and hyphens, and end with a letter or digit."
  }
}

variable "environment" {
  description = "Environment name (for example dev, staging, prod) appended to every resource name; supplied by the operator, never hardcoded in resource definitions."
  type        = string

  validation {
    condition     = can(regex("^[a-z](?:[a-z0-9-]*[a-z0-9])?$", var.environment))
    error_message = "environment must start with a lowercase letter, contain only lowercase letters, digits, and hyphens, and end with a letter or digit."
  }

  validation {
    condition     = length("${var.project_name}-${var.environment}-frontend") <= 39
    error_message = "project_name and environment are too long together: the derived pipeline name \"<project_name>-<environment>-frontend\" must stay within the pipeline module's 39-character limit; shorten project_name or environment."
  }
}

variable "aws_region" {
  description = "AWS region the bootstrap layer deploys into (for example us-east-1). Supplied by the operator; the Voice_Service's Bedrock streaming additionally requires the app layer to run where Nova 2 Sonic is available."
  type        = string

  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]+$", var.aws_region))
    error_message = "aws_region must be a valid AWS region identifier such as us-east-1 or eu-west-2."
  }
}

variable "tags" {
  description = "Tags applied to every resource created by the bootstrap layer, via the provider's default_tags."
  type        = map(string)
  default     = {}
}

variable "approval_sns_topic_arn" {
  description = "Optional SNS topic ARN notified when any pipeline reaches its ManualApproval stage. When null (the default), no notification is sent and approvers must watch the CodePipeline console."
  type        = string
  default     = null

  validation {
    condition     = var.approval_sns_topic_arn == null || can(regex("^arn:aws[a-zA-Z-]*:sns:", var.approval_sns_topic_arn))
    error_message = "approval_sns_topic_arn must be null or an SNS topic ARN of the form arn:<partition>:sns:<region>:<account-id>:<topic-name>."
  }
}

variable "log_retention_days" {
  description = "Retention period in days for the CloudWatch log groups of every pipeline's CodeBuild projects."
  type        = number
  default     = 30

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.log_retention_days)
    error_message = "log_retention_days must be one of the retention values supported by CloudWatch Logs."
  }
}

variable "ecs_cluster_name" {
  description = "Name of the ECS cluster the backend pipeline's deploy stage updates. When null (the default), it is derived as \"<environment>-voice\" to match the app layer's ecs_service module; set it only if the app layer's cluster name is overridden the same way."
  type        = string
  default     = null

  validation {
    condition     = var.ecs_cluster_name == null || length(trimspace(coalesce(var.ecs_cluster_name, "unset"))) > 0
    error_message = "ecs_cluster_name must be null (derive from environment) or a non-empty cluster name."
  }
}

variable "ecs_service_name" {
  description = "Name of the ECS service the backend pipeline's deploy stage updates. When null (the default), it is derived as \"<environment>-voice-service\" to match the app layer's ecs_service module; set it only if the app layer's service name is overridden the same way."
  type        = string
  default     = null

  validation {
    condition     = var.ecs_service_name == null || length(trimspace(coalesce(var.ecs_service_name, "unset"))) > 0
    error_message = "ecs_service_name must be null (derive from environment) or a non-empty service name."
  }
}

variable "frontend_bucket_name" {
  description = "Name of the frontend hosting bucket the frontend pipeline's deploy stage syncs to. Empty (the default) on the first bootstrap apply — the bucket is created later by the app layer; re-apply bootstrap with the real name afterwards (two-phase flow). The frontend deploy buildspec fails fast while this is empty."
  type        = string
  default     = ""
}

variable "cloudfront_distribution_id" {
  description = "Id of the CloudFront distribution the frontend pipeline's deploy stage invalidates. Empty (the default) on the first bootstrap apply — the distribution is created later by the app layer; re-apply bootstrap with the real id afterwards (two-phase flow). The frontend deploy buildspec fails fast while this is empty."
  type        = string
  default     = ""
}

variable "buildspec_path_overrides" {
  description = "Optional per-pipeline buildspec path overrides, keyed by pipeline (frontend, backend, iac) then stage (scan, test, build, deploy); for example { backend = { deploy = \"ci/backend/deploy-blue-green.yml\" } }. Unlisted pipelines and stages keep the ci/<pipeline>/<stage>.yml defaults. Paths are resolved inside the source archive."
  type        = map(map(string))
  default     = {}

  validation {
    condition     = alltrue([for source in keys(var.buildspec_path_overrides) : contains(["frontend", "backend", "iac"], source)])
    error_message = "buildspec_path_overrides keys must be a subset of \"frontend\", \"backend\", and \"iac\"."
  }

  validation {
    condition     = alltrue([for source, stages in var.buildspec_path_overrides : alltrue([for stage in keys(stages) : contains(["scan", "test", "build", "deploy"], stage)])])
    error_message = "Each buildspec_path_overrides entry may only override the stages \"scan\", \"test\", \"build\", and \"deploy\"."
  }
}

# ---------------------------------------------------------------------------
# Web Push VAPID key management (optional, Req 14.1).
# ---------------------------------------------------------------------------

variable "create_vapid_key" {
  description = "When true, the bootstrap layer generates the Web Push VAPID key pair if it does not already exist, stores the private key as an SSM SecureString, and exports the public key (vapid_public_key) and parameter name for the app layer's tfvars. Create-if-absent: an existing key is never regenerated (that would invalidate live browser subscriptions). Off by default so key creation stays an explicit, opt-in action; when false the operator provisions the key manually (see the README VAPID section)."
  type        = bool
  default     = false
}

variable "vapid_subject" {
  description = "VAPID sub claim (a mailto: or https:// URI) recorded alongside the public key when create_vapid_key generates it; informational only. Ignored when create_vapid_key is false."
  type        = string
  default     = ""

  validation {
    condition     = var.vapid_subject == "" || can(regex("^(mailto:|https://)", var.vapid_subject))
    error_message = "vapid_subject must be empty or a mailto: or https:// URI."
  }
}

variable "vapid_private_key_parameter_name" {
  description = "Optional override for the SSM parameter name holding the VAPID private key. When null (the default), it is derived as /<environment>/notifier/vapid-private-key. Set it only if the app layer's vapid_private_key_parameter_name is overridden the same way. Must start with / and not end with /. Used only when create_vapid_key is true."
  type        = string
  default     = null

  validation {
    condition     = var.vapid_private_key_parameter_name == null || can(regex("^/.+[^/]$", var.vapid_private_key_parameter_name))
    error_message = "vapid_private_key_parameter_name must be null or start with / and not end with / (for example /dev/notifier/vapid-private-key)."
  }
}
