# Input variables for the state_backend module.
# Every environment-specific value arrives here — nothing is hardcoded in main.tf.

variable "project_name" {
  description = "Short project identifier used as the prefix for the state bucket and lock table names (lowercase letters, digits, and hyphens; must start with a letter)."
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

variable "deletion_protection" {
  description = "Whether deletion protection is enabled on the DynamoDB lock table. Keep true (the default) outside of short-lived sandbox environments: losing the lock table breaks safe app-layer applies."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags applied to every resource created by this module, merged with per-resource Name tags."
  type        = map(string)
  default     = {}
}
