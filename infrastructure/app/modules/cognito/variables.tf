variable "environment" {
  description = "Environment name (for example dev or prod) used as the resource name prefix."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.environment))
    error_message = "environment must start with a lowercase letter and contain only lowercase letters, digits, and hyphens."
  }
}

variable "domain_prefix" {
  description = "Cognito hosted UI domain prefix (the <prefix> in https://<prefix>.auth.<region>.amazoncognito.com); must be globally unique within the region."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", var.domain_prefix))
    error_message = "domain_prefix must be 1-63 characters of lowercase letters, digits, and hyphens, and must not start or end with a hyphen."
  }
}

variable "callback_urls" {
  description = "Allowed OAuth callback (redirect) URLs for the SPA client; the root layer wires the CloudFront distribution domain here."
  type        = list(string)

  validation {
    condition     = length(var.callback_urls) > 0
    error_message = "At least one callback URL is required."
  }
}

variable "logout_urls" {
  description = "Allowed OAuth sign-out redirect URLs for the SPA client; the root layer wires the CloudFront distribution domain here."
  type        = list(string)

  validation {
    condition     = length(var.logout_urls) > 0
    error_message = "At least one logout URL is required."
  }
}

variable "allowed_oauth_scopes" {
  description = "OAuth scopes the SPA client may request in the authorization-code + PKCE flow."
  type        = list(string)
  default     = ["openid", "email", "profile"]

  validation {
    condition     = length(var.allowed_oauth_scopes) > 0
    error_message = "At least one OAuth scope is required."
  }
}

variable "mfa_configuration" {
  description = "Multi-factor authentication mode for the user pool: OFF, OPTIONAL (each engineer may enroll a TOTP authenticator), or ON (required for every sign-in)."
  type        = string
  default     = "OPTIONAL"

  validation {
    condition     = contains(["OFF", "OPTIONAL", "ON"], var.mfa_configuration)
    error_message = "mfa_configuration must be one of OFF, OPTIONAL, or ON."
  }
}

variable "admin_create_only" {
  description = "Whether enrollment is closed: when true only administrators can create engineer accounts and self-service sign-up is disabled."
  type        = bool
  default     = true
}

variable "deletion_protection_enabled" {
  description = "Whether deletion protection is active on the user pool, guarding the identity store against accidental destroy."
  type        = bool
  default     = true
}
