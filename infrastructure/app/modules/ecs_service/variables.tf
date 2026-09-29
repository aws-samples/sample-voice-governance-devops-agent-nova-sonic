variable "environment" {
  description = "Environment name (for example dev or prod) used as the prefix for the cluster, service, log group, and alarm names."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.environment))
    error_message = "environment must start with a lowercase letter and contain only lowercase letters, digits, and hyphens."
  }
}

variable "vpc_id" {
  description = "ID of the VPC (network module) hosting the task security group."
  type        = string

  validation {
    condition     = can(regex("^vpc-", var.vpc_id))
    error_message = "vpc_id must be a VPC ID (vpc-...)."
  }
}

variable "private_subnet_ids" {
  description = "IDs of the private subnets (network module) the Fargate tasks run in; at least two subnets in distinct Availability Zones so tasks spread across ≥2 AZs (Req 10.1)."
  type        = list(string)

  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "private_subnet_ids must contain at least 2 subnets so tasks span at least two Availability Zones (Req 10.1)."
  }
}

variable "container_image" {
  description = "Full image reference for the voice-service container (ECR repository URI plus tag or digest), supplied by the backend pipeline."
  type        = string

  validation {
    condition     = length(var.container_image) > 0
    error_message = "container_image is required."
  }
}

variable "container_port" {
  description = "Port the voice-service container listens on; must match the ALB target group port (alb module target_port)."
  type        = number
  default     = 8080

  validation {
    condition     = var.container_port >= 1 && var.container_port <= 65535
    error_message = "container_port must be a valid TCP port (1-65535)."
  }
}

variable "task_cpu" {
  description = "Fargate task CPU units (1024 = 1 vCPU); must be one of the values Fargate supports."
  type        = number
  default     = 1024

  validation {
    condition     = contains([256, 512, 1024, 2048, 4096, 8192, 16384], var.task_cpu)
    error_message = "task_cpu must be one of the Fargate-supported values: 256, 512, 1024, 2048, 4096, 8192, 16384."
  }
}

variable "task_memory" {
  description = "Fargate task memory in MiB; must be a value compatible with task_cpu per the Fargate size matrix."
  type        = number
  default     = 2048

  validation {
    condition     = var.task_memory >= 512 && var.task_memory <= 122880
    error_message = "task_memory must be between 512 and 122880 MiB and compatible with task_cpu per the Fargate size matrix."
  }
}

variable "desired_count" {
  description = "Baseline and minimum number of voice tasks; at least 2 at all times (Req 10.1). Also the autoscaling floor, so scale-in never goes below it (Req 10.6)."
  type        = number
  default     = 2

  validation {
    condition     = var.desired_count >= 2
    error_message = "desired_count must be at least 2 so the voice service always runs a minimum of 2 tasks (Req 10.1)."
  }
}

variable "max_capacity" {
  description = "Maximum number of voice tasks autoscaling may reach (Req 10.2 configured maximum task count)."
  type        = number

  validation {
    condition     = var.max_capacity >= var.desired_count
    error_message = "max_capacity must be greater than or equal to desired_count."
  }
}

variable "scale_out_threshold" {
  description = "ALB ActiveConnectionCount value above which the scale-out alarm fires and tasks are added (Req 10.2)."
  type        = number

  validation {
    condition     = var.scale_out_threshold > 0
    error_message = "scale_out_threshold must be greater than 0."
  }
}

variable "scale_in_threshold" {
  description = "ALB ActiveConnectionCount value below which, after the full evaluation window, one task is removed (Req 19.5); must be lower than scale_out_threshold so the policies never oscillate."
  type        = number

  validation {
    condition     = var.scale_in_threshold > 0 && var.scale_in_threshold < var.scale_out_threshold
    error_message = "scale_in_threshold must be greater than 0 and lower than scale_out_threshold."
  }
}

variable "scale_in_evaluation_periods" {
  description = "Number of consecutive 60-second periods the connection count must stay below scale_in_threshold before scale-in acts (Req 19.5 evaluation period); long by default so scale-in is conservative."
  type        = number
  default     = 15

  validation {
    condition     = var.scale_in_evaluation_periods >= 5
    error_message = "scale_in_evaluation_periods must be at least 5 (5 minutes) so scale-in stays conservative."
  }
}

variable "scale_out_step_size" {
  description = "Number of tasks added per scale-out event; sized to absorb incident-storm connection surges quickly (Req 10.2)."
  type        = number
  default     = 2

  validation {
    condition     = var.scale_out_step_size >= 1
    error_message = "scale_out_step_size must be at least 1."
  }
}

variable "target_group_arn" {
  description = "ARN of the ALB target group (alb module) the service registers its tasks with."
  type        = string

  validation {
    condition     = can(regex("^arn:", var.target_group_arn))
    error_message = "target_group_arn must be an ARN (arn:...)."
  }
}

variable "alb_security_group_id" {
  description = "ID of the ALB security group (alb module); the task security group admits the container port from it only."
  type        = string

  validation {
    condition     = can(regex("^sg-", var.alb_security_group_id))
    error_message = "alb_security_group_id must be a security group ID (sg-...)."
  }
}

variable "alb_arn_suffix" {
  description = "ARN suffix of the ALB (alb module), the LoadBalancer dimension of the ActiveConnectionCount alarms driving the step policies."
  type        = string

  validation {
    condition     = length(var.alb_arn_suffix) > 0
    error_message = "alb_arn_suffix is required."
  }
}

variable "execution_role_arn" {
  description = "ARN of the ECS task execution role (iam module): image pull, log delivery, and startup secret injection."
  type        = string

  validation {
    condition     = can(regex("^arn:", var.execution_role_arn))
    error_message = "execution_role_arn must be an ARN (arn:...)."
  }
}

variable "task_role_arn" {
  description = "ARN of the ECS task role (iam module) the voice-service application code runs as."
  type        = string

  validation {
    condition     = can(regex("^arn:", var.task_role_arn))
    error_message = "task_role_arn must be an ARN (arn:...)."
  }
}

variable "environment_variables" {
  description = "Non-sensitive container environment variables (Req 14.1): region, table names, guardrail id and version, AppSync endpoints, thresholds, retention days."
  type        = map(string)
  default     = {}
}

variable "secrets" {
  description = "Sensitive container configuration (Req 14.1): map of environment variable name to the SSM parameter or Secrets Manager ARN injected via valueFrom at container startup."
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for value_from in values(var.secrets) : length(value_from) > 0])
    error_message = "every secrets value must be a non-empty SSM parameter or Secrets Manager ARN."
  }
}

variable "log_retention_days" {
  description = "Retention in days for the service log group (Req 15.5); must be a value CloudWatch Logs supports."
  type        = number
  default     = 90

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.log_retention_days)
    error_message = "log_retention_days must be one of the retention values CloudWatch Logs supports (1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, ...)."
  }
}

variable "kms_key_arn" {
  description = "Optional customer managed KMS key ARN for the service log group; when null, CloudWatch Logs applies its default server-side encryption (Req 12.3)."
  type        = string
  default     = null

  validation {
    condition     = var.kms_key_arn == null || can(regex("^arn:aws[a-z-]*:kms:", var.kms_key_arn))
    error_message = "kms_key_arn must be a KMS key ARN (arn:aws...:kms:...) or null."
  }
}

variable "enable_execute_command" {
  description = "Whether ECS Exec is enabled on the service for interactive debugging; keep false outside troubleshooting sessions."
  type        = bool
  default     = false
}
