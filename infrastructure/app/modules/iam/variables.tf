variable "environment" {
  description = "Environment name (for example dev or prod) used as the prefix for the role and policy names."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.environment))
    error_message = "environment must start with a lowercase letter and contain only lowercase letters, digits, and hyphens."
  }
}

variable "nova_sonic_model_id" {
  description = "Bedrock foundation-model identifier that narrows the task role's bedrock:InvokeModelWithBidirectionalStream resource (arn:<partition>:bedrock:<region>::foundation-model/<this value>); may contain a trailing wildcard to cover model revisions."
  type        = string
  default     = "amazon.nova-2-sonic-*"

  validation {
    condition     = length(var.nova_sonic_model_id) > 0
    error_message = "nova_sonic_model_id is required."
  }
}

variable "nova_sonic_model_arns" {
  description = "Optional explicit list of foundation-model ARNs for bedrock:InvokeModelWithBidirectionalStream; when null, the ARN is built from nova_sonic_model_id and bedrock_model_region."
  type        = list(string)
  default     = null

  validation {
    condition = var.nova_sonic_model_arns == null || (
      length(coalesce(var.nova_sonic_model_arns, [])) > 0 &&
      alltrue([for arn in coalesce(var.nova_sonic_model_arns, []) : can(regex("^arn:", arn))])
    )
    error_message = "nova_sonic_model_arns must be null or a non-empty list of ARNs (arn:...)."
  }
}

variable "bedrock_model_region" {
  description = "Region segment of the constructed foundation-model ARN; defaults to the deployment region when null. The design pins Nova Sonic streaming to us-east-1, so set this when the stack deploys to another region."
  type        = string
  default     = null

  validation {
    condition     = var.bedrock_model_region == null || can(regex("^[a-z]{2}(-[a-z]+)+-\\d$", var.bedrock_model_region))
    error_message = "bedrock_model_region must be an AWS region name (for example us-east-1) or null."
  }
}

variable "guardrail_arn" {
  description = "ARN of the Bedrock guardrail (bedrock_guardrail module) the task role may evaluate with bedrock:ApplyGuardrail (Req 4.1)."
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-z-]*:bedrock:", var.guardrail_arn))
    error_message = "guardrail_arn must be a Bedrock guardrail ARN (arn:aws...:bedrock:...)."
  }
}

variable "dynamodb_table_arns" {
  description = "ARNs of the four Session_Store tables (dynamodb module: voice-sessions, agent-chats, push-subscriptions, transcripts); the task role's DynamoDB statement covers each table and its indexes."
  type        = list(string)

  validation {
    condition = (
      length(var.dynamodb_table_arns) > 0 &&
      alltrue([for arn in var.dynamodb_table_arns : can(regex("^arn:aws[a-z-]*:dynamodb:", arn))])
    )
    error_message = "dynamodb_table_arns must be a non-empty list of DynamoDB table ARNs (arn:aws...:dynamodb:...)."
  }
}

variable "cluster_arn" {
  description = "ARN of the voice ECS cluster (ecs_service module); ecs:UpdateTaskProtection is scoped to this cluster's tasks via resource pattern and the ecs:cluster condition (Req 10.3)."
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-z-]*:ecs:.*:cluster/", var.cluster_arn))
    error_message = "cluster_arn must be an ECS cluster ARN (arn:aws...:ecs:...:cluster/...)."
  }
}

variable "ssm_parameter_path_prefix" {
  description = "SSM Parameter Store path prefix (for example /dev/voice-service) under which the environment's configuration parameters live; reads are scoped to parameters below it. Must start with / and not end with /."
  type        = string

  validation {
    condition     = can(regex("^/.+[^/]$", var.ssm_parameter_path_prefix))
    error_message = "ssm_parameter_path_prefix must start with / and must not end with / (for example /dev/voice-service)."
  }
}

variable "secretsmanager_secret_arn_prefix" {
  description = "Secrets Manager ARN prefix (for example arn:aws:secretsmanager:<region>:<account>:secret:dev/voice) covering the environment's secrets; reads are scoped to ARNs matching this prefix."
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-z-]*:secretsmanager:", var.secretsmanager_secret_arn_prefix))
    error_message = "secretsmanager_secret_arn_prefix must be a Secrets Manager ARN prefix (arn:aws...:secretsmanager:...)."
  }
}
