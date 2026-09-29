# Input variables for the vapid module.

variable "environment" {
  description = "Environment name (for example dev, staging, prod); used to derive the default SSM parameter name /<environment>/notifier/vapid-private-key."
  type        = string

  validation {
    condition     = can(regex("^[a-z](?:[a-z0-9-]*[a-z0-9])?$", var.environment))
    error_message = "environment must start with a lowercase letter, contain only lowercase letters, digits, and hyphens, and end with a letter or digit."
  }
}

variable "region" {
  description = "AWS region the SSM SecureString is written to and read from; must match the region the app layer and Notifier run in."
  type        = string

  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]+$", var.region))
    error_message = "region must be a valid AWS region identifier such as us-east-1."
  }
}

variable "subject" {
  description = "VAPID sub claim (a mailto: or https:// URI) recorded alongside the public key in the parameter description; informational only — the deployed VAPID_SUBJECT comes from the app layer's vapid_subject variable."
  type        = string
  default     = ""

  validation {
    condition     = var.subject == "" || can(regex("^(mailto:|https://)", var.subject))
    error_message = "subject must be empty or a mailto: or https:// URI."
  }
}

variable "parameter_name_override" {
  description = "Optional SSM parameter name for the VAPID private key. When null (the default), it is derived as /<environment>/notifier/vapid-private-key. Set it only if the app layer's vapid_private_key_parameter_name is overridden the same way. Must start with / and not end with /."
  type        = string
  default     = null

  validation {
    condition     = var.parameter_name_override == null || can(regex("^/.+[^/]$", var.parameter_name_override))
    error_message = "parameter_name_override must be null or start with / and not end with / (for example /dev/notifier/vapid-private-key)."
  }
}
