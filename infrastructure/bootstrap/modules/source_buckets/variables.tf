# Input variables for the source_buckets module.
# Every environment-specific value arrives here — nothing is hardcoded in main.tf.

variable "project_name" {
  description = "Short project identifier used as the prefix for bucket, EventBridge rule, and IAM role names (lowercase letters, digits, and hyphens; must start with a letter)."
  type        = string

  validation {
    condition     = can(regex("^[a-z](?:[a-z0-9-]*[a-z0-9])?$", var.project_name))
    error_message = "project_name must start with a lowercase letter, contain only lowercase letters, digits, and hyphens, and end with a letter or digit."
  }
}

variable "environment" {
  description = "Environment name (for example dev, staging, prod) appended to resource names; supplied by the caller, never hardcoded in resource definitions."
  type        = string

  validation {
    condition     = can(regex("^[a-z](?:[a-z0-9-]*[a-z0-9])?$", var.environment))
    error_message = "environment must start with a lowercase letter, contain only lowercase letters, digits, and hyphens, and end with a letter or digit."
  }
}

variable "pipelines" {
  description = "CodePipeline each source bucket triggers, keyed by source name (frontend, backend, iac). Name and ARN come from the sibling pipeline module; this module wires the EventBridge target and the start-execution IAM policy to these ARNs."
  type = map(object({
    name = string
    arn  = string
  }))

  validation {
    condition     = toset(keys(var.pipelines)) == toset(["frontend", "backend", "iac"])
    error_message = "pipelines must contain exactly the keys \"frontend\", \"backend\", and \"iac\" — one entry per source bucket."
  }

  validation {
    condition     = alltrue([for pipeline in values(var.pipelines) : can(regex("^arn:aws[a-zA-Z-]*:codepipeline:", pipeline.arn))])
    error_message = "Each pipelines entry must carry a CodePipeline ARN of the form arn:<partition>:codepipeline:<region>:<account-id>:<pipeline-name>."
  }

  validation {
    condition     = alltrue([for pipeline in values(var.pipelines) : length(pipeline.name) > 0])
    error_message = "Each pipelines entry must carry a non-empty pipeline name."
  }
}

variable "source_object_key" {
  description = "Object key of the source archive uploaded by scripts/push-source.sh. The EventBridge rules match this exact key and the CodePipeline S3 source action consumes it, so it must be a ZIP archive."
  type        = string
  default     = "source.zip"

  validation {
    condition     = can(regex("^.+\\.zip$", var.source_object_key))
    error_message = "source_object_key must be a non-empty object key ending in .zip because the CodePipeline S3 source action requires a ZIP archive."
  }
}

variable "tags" {
  description = "Tags applied to every resource created by this module, merged with per-resource Name tags."
  type        = map(string)
  default     = {}
}
