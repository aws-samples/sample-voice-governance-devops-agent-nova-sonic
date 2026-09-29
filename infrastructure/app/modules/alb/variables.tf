variable "environment" {
  description = "Environment name (for example dev or prod) used as the prefix for the ALB, target group, and security group names."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.environment))
    error_message = "environment must start with a lowercase letter and contain only lowercase letters, digits, and hyphens."
  }
}

variable "vpc_id" {
  description = "ID of the VPC (network module) hosting the ALB security group and target group."
  type        = string

  validation {
    condition     = can(regex("^vpc-", var.vpc_id))
    error_message = "vpc_id must be a VPC ID (vpc-...)."
  }
}

variable "public_subnet_ids" {
  description = "IDs of the public subnets (network module) the internet-facing ALB spans; at least two subnets in distinct Availability Zones."
  type        = list(string)

  validation {
    condition     = length(var.public_subnet_ids) >= 2
    error_message = "public_subnet_ids must contain at least 2 subnets in distinct Availability Zones."
  }
}

variable "access_logging_bucket_name" {
  description = "Name of the existing S3 bucket that receives ALB access logs; the bucket is referenced, never created (Req 13.4, 13.5)."
  type        = string

  validation {
    condition     = length(trimspace(var.access_logging_bucket_name)) > 0
    error_message = "access_logging_bucket_name is required: provide the name of the existing access-logging bucket (Req 13.6)."
  }
}

variable "origin_verify_header_value" {
  description = "Secret value CloudFront injects into the x-origin-verify header on origin requests; the listener forwards to the voice target group only when the header matches, so direct-to-ALB traffic is rejected."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.origin_verify_header_value) >= 16
    error_message = "origin_verify_header_value must be at least 16 characters so the origin-verification secret is not guessable."
  }
}

variable "target_port" {
  description = "Port the voice-service container listens on; the target group forwards to it and health-checks /healthz on the same port."
  type        = number
  default     = 8080

  validation {
    condition     = var.target_port >= 1 && var.target_port <= 65535
    error_message = "target_port must be a valid TCP port (1-65535)."
  }
}

variable "idle_timeout" {
  description = "ALB idle timeout in seconds; sized for long-lived voice WebSocket connections that may pause between frames."
  type        = number
  default     = 300

  validation {
    condition     = var.idle_timeout >= 60 && var.idle_timeout <= 4000
    error_message = "idle_timeout must be between 60 and 4000 seconds."
  }
}
