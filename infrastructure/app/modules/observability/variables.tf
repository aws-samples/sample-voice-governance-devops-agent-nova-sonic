variable "environment" {
  description = "Environment name (for example dev or prod) used as the prefix for the topic and alarm names."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.environment))
    error_message = "environment must start with a lowercase letter and contain only lowercase letters, digits, and hyphens."
  }
}

variable "ecs_cluster_name" {
  description = "Name of the voice ECS cluster (ecs_service module cluster_name output); the ClusterName dimension of the running-task-count alarm."
  type        = string

  validation {
    condition     = length(var.ecs_cluster_name) > 0
    error_message = "ecs_cluster_name is required."
  }
}

variable "ecs_service_name" {
  description = "Name of the voice ECS service (ecs_service module service_name output); the ServiceName dimension of the running-task-count alarm."
  type        = string

  validation {
    condition     = length(var.ecs_service_name) > 0
    error_message = "ecs_service_name is required."
  }
}

variable "alb_arn_suffix" {
  description = "ARN suffix of the voice ALB (alb module alb_arn_suffix output, app/{name}/{id}); the LoadBalancer dimension of the unhealthy-targets and voice-5xx alarms."
  type        = string

  validation {
    condition     = can(regex("^app/", var.alb_arn_suffix))
    error_message = "alb_arn_suffix must be an ALB ARN suffix (app/{name}/{id})."
  }
}

variable "target_group_arn_suffix" {
  description = "ARN suffix of the voice target group (alb module target_group_arn_suffix output, targetgroup/{name}/{id}); the TargetGroup dimension of the unhealthy-targets alarm."
  type        = string

  validation {
    condition     = can(regex("^targetgroup/", var.target_group_arn_suffix))
    error_message = "target_group_arn_suffix must be a target group ARN suffix (targetgroup/{name}/{id})."
  }
}

variable "notifier_function_name" {
  description = "Name of the Notifier Lambda function (notifications module lambda_function_name output); the FunctionName dimension of the notifier-errors alarm."
  type        = string

  validation {
    condition     = length(var.notifier_function_name) > 0
    error_message = "notifier_function_name is required."
  }
}

variable "alarm_email_subscriptions" {
  description = "Email addresses to subscribe to the operations SNS topic; each address must confirm the subscription before deliveries begin. Empty by default — subscriptions can also be attached outside Terraform."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for address in var.alarm_email_subscriptions : can(regex("^[^@\\s]+@[^@\\s]+$", address))])
    error_message = "alarm_email_subscriptions entries must be email addresses (user@domain)."
  }
}

variable "running_task_count_threshold" {
  description = "Running-task-count alarm threshold: the alarm fires when the Voice_Service task count falls below this value. Defaults to the design's high-availability floor of 2 tasks (Req 10.1, 19.2)."
  type        = number
  default     = 2

  validation {
    condition     = var.running_task_count_threshold >= 1
    error_message = "running_task_count_threshold must be at least 1."
  }
}

variable "voice_5xx_threshold" {
  description = "Voice-5xx alarm threshold: the alarm fires when the sum of target 5XX responses in one period exceeds this value (Req 19.2)."
  type        = number
  default     = 10

  validation {
    condition     = var.voice_5xx_threshold >= 0
    error_message = "voice_5xx_threshold must be zero or greater."
  }
}

variable "alarm_period_seconds" {
  description = "Metric aggregation period in seconds shared by the four alarms (Req 19.2); 10 or 30 (high resolution) or a multiple of 60."
  type        = number
  default     = 60

  validation {
    condition = (
      contains([10, 30], var.alarm_period_seconds) ||
      (var.alarm_period_seconds >= 60 && var.alarm_period_seconds % 60 == 0)
    )
    error_message = "alarm_period_seconds must be 10, 30, or a multiple of 60."
  }
}

variable "alarm_evaluation_periods" {
  description = "Number of consecutive periods a metric must breach before each alarm fires (Req 19.2); 1 by default so a single bad period pages inside the 60-second delivery requirement (Req 19.3)."
  type        = number
  default     = 1

  validation {
    condition     = var.alarm_evaluation_periods >= 1
    error_message = "alarm_evaluation_periods must be at least 1."
  }
}
