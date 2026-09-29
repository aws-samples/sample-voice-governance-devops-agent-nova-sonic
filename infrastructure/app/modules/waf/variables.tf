variable "environment" {
  description = "Environment name (for example dev or prod) used as the web ACL and log group name prefix."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.environment))
    error_message = "environment must start with a lowercase letter and contain only lowercase letters, digits, and hyphens."
  }
}

variable "scope" {
  description = "WAF scope of the web ACL: CLOUDFRONT (attach via the distribution's web_acl_id, must be created in us-east-1) or REGIONAL (attached to the ALB in this module). The app root instantiates this module once per scope (Req 11.1)."
  type        = string

  validation {
    condition     = contains(["CLOUDFRONT", "REGIONAL"], var.scope)
    error_message = "scope must be exactly CLOUDFRONT or REGIONAL."
  }
}

variable "alb_arn" {
  description = "ARN of the internet-facing ALB the REGIONAL web ACL is associated with (Req 11.1); required when scope is REGIONAL. Must be null for CLOUDFRONT scope, whose ACL attaches via the distribution's web_acl_id."
  type        = string
  default     = null

  validation {
    condition     = var.alb_arn == null || can(regex("^arn:aws[a-z-]*:elasticloadbalancing:", var.alb_arn))
    error_message = "alb_arn must be an Elastic Load Balancing ARN (arn:aws...:elasticloadbalancing:...) or null."
  }

  validation {
    condition     = var.scope == "REGIONAL" || var.alb_arn == null
    error_message = "alb_arn can only be set when scope is REGIONAL; CLOUDFRONT-scope ACLs attach via the distribution's web_acl_id."
  }

  validation {
    condition     = var.scope != "REGIONAL" || var.alb_arn != null
    error_message = "alb_arn is required when scope is REGIONAL: the REGIONAL web ACL must be associated with the ALB (Req 11.1)."
  }
}

variable "rate_limit" {
  description = "Best-practice rate limit: maximum requests per source IP per 5-minute window before requests are blocked. Set to null to omit the rate-based rule."
  type        = number
  default     = 2000

  validation {
    condition     = var.rate_limit == null || (var.rate_limit >= 10 && var.rate_limit <= 2000000000)
    error_message = "rate_limit must be between 10 and 2,000,000,000 (WAF rate-based statement bounds) or null."
  }
}

variable "log_retention_days" {
  description = "Retention in days for the WAF CloudWatch Logs log group (Req 11.5 persistent destination); 0 keeps logs forever."
  type        = number
  default     = 365

  validation {
    condition     = contains([0, 1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.log_retention_days)
    error_message = "log_retention_days must be one of the retention values CloudWatch Logs supports (0, 1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653)."
  }
}

variable "kms_key_arn" {
  description = "Optional customer managed KMS key ARN for the WAF log group; when null, CloudWatch Logs uses its default at-rest encryption (Req 12.3)."
  type        = string
  default     = null

  validation {
    condition     = var.kms_key_arn == null || can(regex("^arn:aws[a-z-]*:kms:", var.kms_key_arn))
    error_message = "kms_key_arn must be a KMS key ARN (arn:aws...:kms:...) or null."
  }
}
