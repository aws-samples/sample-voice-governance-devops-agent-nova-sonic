# Input variables for the app layer root (Req 13.4, 13.6, 15.5, 15.7).
#
# Every environment-specific value is an input variable — zero hardcoded
# environment names, account identifiers, or endpoints in resource
# definitions (Req 15.5). Required variables carry no default, so an unset
# variable fails `terraform plan` with a clear error before any resource is
# created or modified (Req 15.7). Validations mirror the consuming modules'
# constraints so bad values fail at the root boundary with a clear message.

# ---------------------------------------------------------------------------
# Deployment identity.
# ---------------------------------------------------------------------------

variable "environment" {
  description = "Environment name (for example dev or prod); the prefix for every resource name in this layer. Required — never hardcoded (Req 15.5)."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.environment))
    error_message = "environment must start with a lowercase letter and contain only lowercase letters, digits, and hyphens."
  }
}

variable "aws_region" {
  description = "AWS region the layer deploys to. The design pins the stack to us-east-1 (Nova Sonic bidirectional streaming and the CLOUDFRONT-scope WAF web ACL both require it), so the default is that design constant; override only if the design's regional pinning changes."
  type        = string
  default     = "us-east-1"

  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-\\d$", var.aws_region))
    error_message = "aws_region must be an AWS region name (for example us-east-1)."
  }
}

variable "tags" {
  description = "Tags applied to every resource in the layer via provider default_tags."
  type        = map(string)
  default     = {}
}

# ---------------------------------------------------------------------------
# External dependencies this layer references but never creates.
# ---------------------------------------------------------------------------

variable "access_logging_bucket_name" {
  description = "Name of the pre-existing S3 bucket that receives ALB access logs (Req 13.4). The bucket is referenced, never created, by this layer (Req 13.5)."
  type        = string

  validation {
    condition     = length(trimspace(var.access_logging_bucket_name)) > 0
    error_message = "access_logging_bucket_name is required and must not be empty: provide the name of the pre-existing access-logging S3 bucket. This layer only references that bucket — it is never created by any layer (Req 13.5, 13.6)."
  }
}

variable "container_image" {
  description = "Full image reference for the voice-service container (the ECR repository URI plus tag or digest pushed by the backend pipeline)."
  type        = string

  validation {
    condition     = length(trimspace(var.container_image)) > 0
    error_message = "container_image is required: the ECR image URI (with tag or digest) of the voice-service container pushed by the backend pipeline."
  }
}

variable "lambda_zip_path" {
  description = "Filesystem path to the packaged Notifier deployment zip, built by the IaC pipeline buildspec from backend/notifier before terraform plan/apply runs."
  type        = string

  validation {
    condition     = length(trimspace(var.lambda_zip_path)) > 0
    error_message = "lambda_zip_path is required: the path to the Notifier deployment zip the IaC pipeline builds before plan/apply."
  }
}

variable "devops_agent_space_id" {
  description = "Identifier of the DevOps Agent agent space the Voice_Service creates chats in (Voice_Service DEVOPS_AGENT_SPACE_ID); account-specific, provisioned outside this layer."
  type        = string

  validation {
    condition     = length(trimspace(var.devops_agent_space_id)) > 0
    error_message = "devops_agent_space_id is required: the DevOps Agent agent-space identifier the Voice_Service creates chats in."
  }
}

# ---------------------------------------------------------------------------
# Web Push (VAPID). The private key is operator-provisioned key material in
# SSM Parameter Store and is referenced by name only (Req 14.1); the public
# key is not sensitive and feeds the frontend config.json output (Req 14.3).
# ---------------------------------------------------------------------------

variable "vapid_subject" {
  description = "VAPID sub claim identifying the push sender, a mailto: or https URI (Notifier VAPID_SUBJECT)."
  type        = string

  validation {
    condition     = can(regex("^(mailto:|https://)", var.vapid_subject))
    error_message = "vapid_subject must be a mailto: or https:// URI (the Web Push VAPID subject)."
  }
}

variable "vapid_private_key_parameter_name" {
  description = "Name of the operator-created SSM Parameter Store SecureString holding the VAPID private key (for example /dev/notifier/vapid-private-key). Carries the parameter name only — never key material (Req 14.1). Must start with / and not end with /."
  type        = string

  validation {
    condition     = can(regex("^/.+[^/]$", var.vapid_private_key_parameter_name))
    error_message = "vapid_private_key_parameter_name must start with / and must not end with / (for example /dev/notifier/vapid-private-key)."
  }
}

variable "vapid_public_key" {
  description = "VAPID public key (URL-safe base64) matching the private key named by vapid_private_key_parameter_name; not sensitive — published to browsers through the frontend config.json output (Req 14.3)."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_-]+$", var.vapid_public_key))
    error_message = "vapid_public_key is required and must be the URL-safe base64 VAPID public key (letters, digits, hyphen, underscore)."
  }
}

# ---------------------------------------------------------------------------
# Identity and networking.
# ---------------------------------------------------------------------------

variable "cognito_domain_prefix" {
  description = "Cognito hosted UI domain prefix (the <prefix> in https://<prefix>.auth.<region>.amazoncognito.com); must be globally unique within the region, so it is environment-specific and required."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", var.cognito_domain_prefix))
    error_message = "cognito_domain_prefix must be 1-63 characters of lowercase letters, digits, and hyphens, and must not start or end with a hyphen."
  }
}

variable "vpc_cidr" {
  description = "IPv4 CIDR block for the voice-plane VPC (network module)."
  type        = string
  default     = "10.0.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "vpc_cidr must be a valid IPv4 CIDR block (for example 10.0.0.0/16)."
  }
}

variable "az_count" {
  description = "Number of Availability Zones the VPC spans with one public and one private subnet each; at least 2 so the voice service keeps tasks in ≥2 AZs (Req 10.1)."
  type        = number
  default     = 2

  validation {
    condition     = var.az_count >= 2
    error_message = "az_count must be at least 2 so the voice service spans at least two Availability Zones (Req 10.1)."
  }
}

variable "single_nat_gateway" {
  description = "When true, all private subnets share one NAT gateway (cost control for non-production); when false, each AZ gets its own NAT gateway."
  type        = bool
  default     = false
}

variable "container_port" {
  description = "Port the voice-service container listens on; wired to both the ALB target group (alb module target_port) and the task definition (ecs_service module container_port) so the two can never drift apart."
  type        = number
  default     = 8080

  validation {
    condition     = var.container_port >= 1 && var.container_port <= 65535
    error_message = "container_port must be a valid TCP port (1-65535)."
  }
}

# ---------------------------------------------------------------------------
# Bedrock model and IAM scoping.
# ---------------------------------------------------------------------------

variable "nova_sonic_model_id" {
  description = "Bedrock foundation-model identifier of Nova 2 Sonic; the Voice_Service invokes it over InvokeModelWithBidirectionalStream (NOVA_SONIC_MODEL_ID) and the iam module scopes the task role's invoke permission to it. A design constant rather than an environment value, hence the default."
  type        = string
  default     = "amazon.nova-2-sonic-v1:0"

  validation {
    condition     = length(trimspace(var.nova_sonic_model_id)) > 0
    error_message = "nova_sonic_model_id is required."
  }
}

variable "ssm_parameter_path_prefix" {
  description = "SSM Parameter Store path prefix under which this environment's voice-service parameters live (task-role read scope and the origin-verify parameter location). Null derives /<environment>/voice-service. Must start with / and not end with / when set."
  type        = string
  default     = null

  validation {
    condition     = var.ssm_parameter_path_prefix == null || can(regex("^/.+[^/]$", var.ssm_parameter_path_prefix))
    error_message = "ssm_parameter_path_prefix must be null or start with / and not end with / (for example /dev/voice-service)."
  }
}

variable "secretsmanager_secret_arn_prefix" {
  description = "Secrets Manager ARN prefix scoping the task role's secret reads. Null derives arn:<partition>:secretsmanager:<region>:<account>:secret:<environment>/ for the deployment account, keeping account identifiers out of source (Req 14.2)."
  type        = string
  default     = null

  validation {
    condition     = var.secretsmanager_secret_arn_prefix == null || can(regex("^arn:aws[a-z-]*:secretsmanager:", var.secretsmanager_secret_arn_prefix))
    error_message = "secretsmanager_secret_arn_prefix must be null or a Secrets Manager ARN prefix (arn:aws...:secretsmanager:...)."
  }
}

# ---------------------------------------------------------------------------
# Scaling and retention (Req 15.5: environment-tunable, never hardcoded).
# ---------------------------------------------------------------------------

variable "max_capacity" {
  description = "Maximum number of voice tasks autoscaling may reach (Req 10.2); the floor stays at the ecs_service module's minimum of 2 tasks (Req 10.1)."
  type        = number
  default     = 10

  validation {
    condition     = var.max_capacity >= 2
    error_message = "max_capacity must be at least 2, the voice service's minimum task count (Req 10.1)."
  }
}

variable "scale_out_threshold" {
  description = "ALB ActiveConnectionCount value above which the scale-out alarm fires and tasks are added (Req 10.2)."
  type        = number
  default     = 80

  validation {
    condition     = var.scale_out_threshold > 0
    error_message = "scale_out_threshold must be greater than 0."
  }
}

variable "scale_in_threshold" {
  description = "ALB ActiveConnectionCount value below which, after the full evaluation window, one task is removed (Req 19.5); must stay below scale_out_threshold so the policies never oscillate."
  type        = number
  default     = 20

  validation {
    condition     = var.scale_in_threshold > 0 && var.scale_in_threshold < var.scale_out_threshold
    error_message = "scale_in_threshold must be greater than 0 and lower than scale_out_threshold."
  }
}

variable "session_retention_days" {
  description = "Session_Store TTL retention period in days (Voice_Service RETENTION_DAYS): records expire this many days after their last update (Req 8.4; design default 30)."
  type        = number
  default     = 30

  validation {
    condition     = var.session_retention_days > 0
    error_message = "session_retention_days must be greater than 0."
  }
}

variable "log_retention_days" {
  description = "Retention in days for the voice-service and Notifier CloudWatch log groups (Req 15.5); must be a value CloudWatch Logs supports."
  type        = number
  default     = 90

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.log_retention_days)
    error_message = "log_retention_days must be one of the retention values CloudWatch Logs supports (1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, ...)."
  }
}

variable "waf_log_retention_days" {
  description = "Retention in days for the two WAF log groups (Req 11.5 persistent destination); 0 keeps logs forever."
  type        = number
  default     = 365

  validation {
    condition     = contains([0, 1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.waf_log_retention_days)
    error_message = "waf_log_retention_days must be 0 (never expire) or one of the retention values CloudWatch Logs supports."
  }
}

# ---------------------------------------------------------------------------
# Alarms and notifications.
# ---------------------------------------------------------------------------

variable "running_task_count_threshold" {
  description = "Running-task-count alarm threshold: fires when the Voice_Service task count falls below this value (Req 19.2); defaults to the high-availability floor of 2 (Req 10.1)."
  type        = number
  default     = 2

  validation {
    condition     = var.running_task_count_threshold >= 1
    error_message = "running_task_count_threshold must be at least 1."
  }
}

variable "voice_5xx_threshold" {
  description = "Voice-5xx alarm threshold: fires when the sum of target 5XX responses in one period exceeds this value (Req 19.2)."
  type        = number
  default     = 10

  validation {
    condition     = var.voice_5xx_threshold >= 0
    error_message = "voice_5xx_threshold must be zero or greater."
  }
}

variable "alarm_email_subscriptions" {
  description = "Email addresses subscribed to the operations SNS topic; each address must confirm before deliveries begin. Empty by default — subscriptions can also be attached outside Terraform."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for address in var.alarm_email_subscriptions : can(regex("^[^@\\s]+@[^@\\s]+$", address))])
    error_message = "alarm_email_subscriptions entries must be email addresses (user@domain)."
  }
}

variable "devops_agent_event_source" {
  description = "EventBridge source string of DevOps Agent finding events; defined by the DevOps Agent service, so configurable rather than fixed."
  type        = string
  default     = "aws.aidevops"

  validation {
    condition     = length(var.devops_agent_event_source) > 0
    error_message = "devops_agent_event_source is required (the EventBridge source string of DevOps Agent finding events)."
  }
}

variable "create_escalation_topic" {
  description = "Whether to create the SNS escalation topic the Notifier publishes to when escalation is configured (Req 5.11)."
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# Delivery and encryption knobs.
# ---------------------------------------------------------------------------

variable "price_class" {
  description = "CloudFront price class controlling which edge locations serve the distribution."
  type        = string
  default     = "PriceClass_100"

  validation {
    condition     = contains(["PriceClass_100", "PriceClass_200", "PriceClass_All"], var.price_class)
    error_message = "price_class must be one of PriceClass_100, PriceClass_200, or PriceClass_All."
  }
}

variable "secrets" {
  description = "Additional sensitive voice-service configuration (Req 14.1): map of container environment variable name to the SSM parameter or Secrets Manager ARN injected via valueFrom at startup. Values are ARNs, never secret material."
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for value_from in values(var.secrets) : length(value_from) > 0])
    error_message = "every secrets value must be a non-empty SSM parameter or Secrets Manager ARN."
  }
}

variable "kms_key_arn" {
  description = "Optional customer managed KMS key ARN applied to the layer's encrypted-at-rest resources (DynamoDB tables, log groups, frontend bucket, guardrail, origin-verify parameter); when null, each service's AWS managed encryption applies (Req 12.3)."
  type        = string
  default     = null

  validation {
    condition     = var.kms_key_arn == null || can(regex("^arn:aws[a-z-]*:kms:", var.kms_key_arn))
    error_message = "kms_key_arn must be a KMS key ARN (arn:aws...:kms:...) or null."
  }
}
