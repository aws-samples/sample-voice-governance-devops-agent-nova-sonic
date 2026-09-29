variable "environment" {
  description = "Environment name (for example dev or prod) used as the prefix for the function, role, rule, and topic names."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.environment))
    error_message = "environment must start with a lowercase letter and contain only lowercase letters, digits, and hyphens."
  }
}

variable "lambda_zip_path" {
  # Required — no default: the IaC pipeline buildspec packages
  # backend/notifier into this zip before terraform plan/apply runs, so
  # the path always exists when filebase64sha256 is evaluated; an unset
  # variable fails the plan before any resource is touched (Req 15.7).
  description = "Filesystem path to the packaged Notifier deployment zip (zip root carries src/ and shared/), built by the IaC pipeline buildspec before plan/apply."
  type        = string

  validation {
    condition     = length(var.lambda_zip_path) > 0
    error_message = "lambda_zip_path is required: the path to the Notifier deployment zip built before plan/apply."
  }
}

variable "lambda_timeout_seconds" {
  description = "Notifier function timeout in seconds; must comfortably cover one fan-out (AppSync publish with retries, Web Push fan-out, optional SNS publish) while staying far below the 5-second delivery target's alerting horizon (Req 5.1)."
  type        = number
  default     = 30

  validation {
    condition     = var.lambda_timeout_seconds >= 1 && var.lambda_timeout_seconds <= 900
    error_message = "lambda_timeout_seconds must be between 1 and 900 (the Lambda maximum)."
  }
}

variable "lambda_memory_mb" {
  description = "Notifier function memory size in MB; also scales the CPU share driving the concurrent channel fan-out."
  type        = number
  default     = 256

  validation {
    condition     = var.lambda_memory_mb >= 128 && var.lambda_memory_mb <= 10240
    error_message = "lambda_memory_mb must be between 128 and 10240 (the Lambda limits)."
  }
}

variable "lambda_architectures" {
  description = "Instruction set architecture for the Notifier function as a single-element list, x86_64 or arm64; must match the platform the pipeline builds the deployment zip's native dependencies (cryptography for VAPID) for."
  type        = list(string)
  default     = ["x86_64"]

  validation {
    condition = (
      length(var.lambda_architectures) == 1 &&
      contains(["x86_64", "arm64"], var.lambda_architectures[0])
    )
    error_message = "lambda_architectures must be a single-element list containing x86_64 or arm64."
  }
}

variable "log_retention_days" {
  description = "Retention in days for the Notifier log group (Req 15.5); must be a value CloudWatch Logs supports."
  type        = number
  default     = 90

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.log_retention_days)
    error_message = "log_retention_days must be one of the retention values CloudWatch Logs supports (1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, ...)."
  }
}

variable "kms_key_arn" {
  description = "Optional customer managed KMS key ARN for the Notifier log group; when null, CloudWatch Logs applies its default server-side encryption (Req 12.3)."
  type        = string
  default     = null

  validation {
    condition     = var.kms_key_arn == null || can(regex("^arn:aws[a-z-]*:kms:", var.kms_key_arn))
    error_message = "kms_key_arn must be a KMS key ARN (arn:aws...:kms:...) or null."
  }
}

variable "appsync_events_http_endpoint" {
  description = "HTTP publish endpoint of the AppSync Events API (appsync_events module http_endpoint output); the Notifier's APPSYNC_EVENTS_HTTP_ENDPOINT environment value."
  type        = string

  validation {
    condition     = can(regex("^https://", var.appsync_events_http_endpoint))
    error_message = "appsync_events_http_endpoint must be an https:// URL (the Events API HTTP publish endpoint)."
  }
}

variable "channel_namespace_arn" {
  description = "ARN of the incidents channel namespace (appsync_events module channel_namespace_arn output), scoping the role's appsync:EventPublish grant to /incidents/* (Req 5.1)."
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-z-]*:appsync:", var.channel_namespace_arn))
    error_message = "channel_namespace_arn must be an AppSync ARN (arn:aws...:appsync:...)."
  }
}

variable "subscriptions_table_name" {
  description = "Name of the push-subscriptions table (dynamodb module push_subscriptions_table_name output); the Notifier's SUBSCRIPTIONS_TABLE_NAME environment value."
  type        = string

  validation {
    condition     = length(var.subscriptions_table_name) > 0
    error_message = "subscriptions_table_name is required."
  }
}

variable "subscriptions_table_arn" {
  description = "ARN of the push-subscriptions table (dynamodb module push_subscriptions_table_arn output), scoping the role's dynamodb:Scan and dynamodb:DeleteItem grants (Req 6.2, 6.5)."
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-z-]*:dynamodb:", var.subscriptions_table_arn))
    error_message = "subscriptions_table_arn must be a DynamoDB table ARN (arn:aws...:dynamodb:...)."
  }
}

variable "vapid_subject" {
  description = "VAPID sub claim identifying the push sender, a mailto: or https URI; the Notifier's VAPID_SUBJECT environment value."
  type        = string

  validation {
    condition     = can(regex("^(mailto:|https://)", var.vapid_subject))
    error_message = "vapid_subject must be a mailto: or https:// URI (the Web Push VAPID subject)."
  }
}

variable "vapid_private_key_parameter_name" {
  description = "Name of the SSM Parameter Store SecureString holding the VAPID private key (for example /dev/notifier/vapid-private-key); the Notifier's VAPID_PRIVATE_KEY_SECRET_NAME environment value and the resource of the role's ssm:GetParameter grant. Carries the parameter name only — never key material (Req 14.1). Must start with / and not end with /."
  type        = string

  validation {
    condition     = can(regex("^/.+[^/]$", var.vapid_private_key_parameter_name))
    error_message = "vapid_private_key_parameter_name must start with / and must not end with / (for example /dev/notifier/vapid-private-key)."
  }
}

variable "devops_agent_event_source" {
  description = "EventBridge source string of DevOps Agent finding events. The value is defined by the DevOps Agent service, so it is configurable rather than fixed; the default is the service's aidevops namespace."
  type        = string
  default     = "aws.aidevops"

  validation {
    condition     = length(var.devops_agent_event_source) > 0
    error_message = "devops_agent_event_source is required (the EventBridge source string of DevOps Agent finding events)."
  }
}

variable "create_escalation_topic" {
  description = "Whether to create the SNS escalation topic (Req 5.11). When true, the topic is created and the Notifier receives SNS_ESCALATION_TOPIC_ARN plus a matching sns:Publish grant; when false, no topic, environment entry, or grant exists and the Notifier's SNS channel is its documented no-op."
  type        = bool
  default     = false
}
