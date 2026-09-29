variable "environment" {
  description = "Environment name (for example dev or prod) used as the table name prefix ({env}-voice-sessions, {env}-agent-chats, {env}-push-subscriptions, {env}-transcripts)."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.environment))
    error_message = "environment must start with a lowercase letter and contain only lowercase letters, digits, and hyphens."
  }
}

variable "kms_key_arn" {
  description = "Optional customer managed KMS key ARN for table encryption; when null, server-side encryption uses the AWS managed key aws/dynamodb (Req 12.3)."
  type        = string
  default     = null

  validation {
    condition     = var.kms_key_arn == null || can(regex("^arn:aws[a-z-]*:kms:", var.kms_key_arn))
    error_message = "kms_key_arn must be a KMS key ARN (arn:aws...:kms:...) or null."
  }
}

variable "deletion_protection_enabled" {
  description = "Whether deletion protection is enabled on every Session_Store table, guarding session state, transcripts, and subscriptions against accidental destroy."
  type        = bool
  default     = true
}
