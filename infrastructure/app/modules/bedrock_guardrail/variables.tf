variable "environment" {
  description = "Environment name (for example dev or prod) used as the guardrail name prefix."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.environment))
    error_message = "environment must start with a lowercase letter and contain only lowercase letters, digits, and hyphens."
  }
}

variable "blocked_input_messaging" {
  description = "Message returned when the guardrail blocks an engineer request; the Voice_Service relays it so Nova Sonic verbally informs the engineer that only read and diagnostic operations are supported (Req 4.3)."
  type        = string
  default     = "This request is not permitted. The support portal supports only read and diagnostic operations; it cannot create, modify, delete, or terminate AWS resources or IAM entities."

  validation {
    condition     = length(trimspace(var.blocked_input_messaging)) > 0
    error_message = "blocked_input_messaging must not be empty."
  }
}

variable "blocked_outputs_messaging" {
  description = "Message returned when the guardrail blocks a model response (Req 4.3)."
  type        = string
  default     = "The response was blocked. The support portal supports only read and diagnostic operations."

  validation {
    condition     = length(trimspace(var.blocked_outputs_messaging)) > 0
    error_message = "blocked_outputs_messaging must not be empty."
  }
}

variable "kms_key_arn" {
  description = "Optional customer managed KMS key ARN encrypting the guardrail at rest; when null, Bedrock uses its service-managed encryption."
  type        = string
  default     = null

  validation {
    condition     = var.kms_key_arn == null || can(regex("^arn:aws[a-z-]*:kms:", var.kms_key_arn))
    error_message = "kms_key_arn must be a KMS key ARN (arn:aws...:kms:...) or null."
  }
}

variable "automated_reasoning_policy_arn" {
  description = "ARN of the pre-built Automated Reasoning read-only-operations policy (Req 4.1). The AWS Terraform provider cannot yet attach Automated Reasoning policies to guardrails, so a non-null value documents the policy an operator attaches out-of-band (bedrock update-guardrail --automated-reasoning-policy-config) until provider support lands; the destructive-operations DENY topic enforces the block meanwhile."
  type        = string
  default     = null

  validation {
    condition     = var.automated_reasoning_policy_arn == null || can(regex("^arn:aws[a-z-]*:bedrock:", var.automated_reasoning_policy_arn))
    error_message = "automated_reasoning_policy_arn must be a Bedrock ARN (arn:aws...:bedrock:...) or null."
  }
}
