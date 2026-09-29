variable "environment" {
  description = "Environment name (for example dev or prod) used as the Events API name prefix."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.environment))
    error_message = "environment must start with a lowercase letter and contain only lowercase letters, digits, and hyphens."
  }
}

variable "user_pool_id" {
  description = "ID of the Cognito user pool that authorizes Events API connections and subscriptions (Req 7.4)."
  type        = string

  validation {
    condition     = length(var.user_pool_id) > 0
    error_message = "user_pool_id is required."
  }
}

variable "channel_namespace" {
  description = "Name of the channel namespace for incident broadcasts; the Notifier publishes to /<namespace>/all (design: /incidents/all)."
  type        = string
  default     = "incidents"

  validation {
    condition     = can(regex("^[A-Za-z0-9](?:[A-Za-z0-9-]{0,48}[A-Za-z0-9])?$", var.channel_namespace))
    error_message = "channel_namespace must be 1-50 alphanumeric or hyphen characters and must not start or end with a hyphen."
  }
}
