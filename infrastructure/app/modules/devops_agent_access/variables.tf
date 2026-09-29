# Input variables for the DevOps Agent account-access module.

variable "environment" {
  description = "Environment name (for example dev or prod) used as the role name prefix."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.environment))
    error_message = "environment must start with a lowercase letter and contain only lowercase letters, digits, and hyphens."
  }
}

variable "devops_agent_service_principal" {
  description = "Service principal of the AWS DevOps Agent, the only principal allowed to assume the read-only inspection role (the agent's service-linked role is created for this same principal). Overridable because the service is new and its principal is not yet covered by a stable public reference."
  type        = string
  default     = "aidevops.amazonaws.com"

  validation {
    condition     = can(regex("^[a-z0-9.-]+\\.amazonaws\\.com$", var.devops_agent_service_principal))
    error_message = "devops_agent_service_principal must be an AWS service principal such as aidevops.amazonaws.com."
  }
}
