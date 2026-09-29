# Input variables for the ecr module.
# Every environment-specific value arrives here — nothing is hardcoded in main.tf.

variable "project_name" {
  description = "Short project identifier used as the prefix for the repository name (lowercase letters, digits, and hyphens; must start with a letter)."
  type        = string

  validation {
    condition     = can(regex("^[a-z](?:[a-z0-9-]*[a-z0-9])?$", var.project_name))
    error_message = "project_name must start with a lowercase letter, contain only lowercase letters, digits, and hyphens, and end with a letter or digit."
  }
}

variable "environment" {
  description = "Environment name (for example dev, staging, prod) appended to the repository name; supplied by the caller, never hardcoded in resource definitions."
  type        = string

  validation {
    condition     = can(regex("^[a-z](?:[a-z0-9-]*[a-z0-9])?$", var.environment))
    error_message = "environment must start with a lowercase letter, contain only lowercase letters, digits, and hyphens, and end with a letter or digit."
  }
}

variable "image_tag_mutability" {
  description = "Tag mutability setting for the repository. IMMUTABLE (the default) prevents a pushed tag from ever being overwritten, so what was deployed under a tag stays auditable; set MUTABLE only if the release process genuinely re-points tags."
  type        = string
  default     = "IMMUTABLE"

  validation {
    condition     = contains(["IMMUTABLE", "MUTABLE"], var.image_tag_mutability)
    error_message = "image_tag_mutability must be either IMMUTABLE or MUTABLE."
  }
}

variable "image_retention_count" {
  description = "Number of most-recent images the lifecycle policy keeps; older images expire. Sizes the rollback window while bounding storage growth."
  type        = number
  default     = 20

  validation {
    condition     = var.image_retention_count >= 1 && floor(var.image_retention_count) == var.image_retention_count
    error_message = "image_retention_count must be a whole number of at least 1."
  }
}

variable "kms_key_arn" {
  description = "Optional customer-managed KMS key ARN for repository encryption. When null (the default), images are encrypted at rest with AES256 — encryption is enabled either way (Req 12.3)."
  type        = string
  default     = null

  validation {
    condition     = var.kms_key_arn == null || can(regex("^arn:aws[a-zA-Z-]*:kms:", var.kms_key_arn))
    error_message = "kms_key_arn must be null or a KMS key ARN of the form arn:<partition>:kms:<region>:<account-id>:key/<key-id>."
  }
}

variable "tags" {
  description = "Tags applied to every resource created by this module, merged with per-resource Name tags."
  type        = map(string)
  default     = {}
}
