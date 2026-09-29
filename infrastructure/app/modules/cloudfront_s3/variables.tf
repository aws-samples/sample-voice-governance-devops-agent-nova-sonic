variable "environment" {
  description = "Environment name (for example dev or prod) used as the frontend bucket and OAC name prefix."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.environment))
    error_message = "environment must start with a lowercase letter and contain only lowercase letters, digits, and hyphens."
  }
}

variable "alb_dns_name" {
  description = "DNS name of the internet-facing ALB (alb module dns_name output), used as the custom origin domain for the /ws/* and /api/* behaviors."
  type        = string

  validation {
    condition     = length(var.alb_dns_name) > 0
    error_message = "alb_dns_name is required."
  }
}

variable "origin_verify_secret" {
  description = "Shared secret CloudFront injects on every ALB origin request in the origin-verify header; the ALB listener rule forwards only requests carrying it, preventing direct ALB access (design research finding 7). Sourced from SSM Parameter Store by the app root (Req 14.1)."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.origin_verify_secret) > 0
    error_message = "origin_verify_secret is required."
  }
}

variable "origin_verify_header_name" {
  description = "Name of the origin-verification header injected on the ALB origin; must match the header the ALB listener rule inspects."
  type        = string
  default     = "x-origin-verify"

  validation {
    condition     = can(regex("^[a-z0-9-]+$", var.origin_verify_header_name))
    error_message = "origin_verify_header_name must contain only lowercase letters, digits, and hyphens."
  }
}

variable "web_acl_arn" {
  description = "ARN of the CLOUDFRONT-scope WAF web ACL to associate with the distribution (Req 11.1); must live in us-east-1. Null skips the association, but the app root always passes it."
  type        = string
  default     = null

  validation {
    condition     = var.web_acl_arn == null || can(regex("^arn:aws[a-z-]*:wafv2:", var.web_acl_arn))
    error_message = "web_acl_arn must be a WAFv2 web ACL ARN (arn:aws...:wafv2:...) or null."
  }
}

variable "price_class" {
  description = "CloudFront price class controlling which edge locations serve the distribution."
  type        = string
  default     = "PriceClass_100"

  validation {
    condition     = contains(["PriceClass_100", "PriceClass_200", "PriceClass_All"], var.price_class)
    error_message = "price_class must be one of PriceClass_100, PriceClass_200, or PriceClass_All."
  }
}

variable "versioning_enabled" {
  description = "Whether versioning is enabled on the frontend bucket (rollback safety for deployed assets)."
  type        = bool
  default     = true
}

variable "kms_key_arn" {
  description = "Optional customer managed KMS key ARN for frontend bucket encryption; when null, SSE-S3 (AES256) is used (Req 12.3). When set, the key policy must also grant the CloudFront service principal decrypt for OAC reads."
  type        = string
  default     = null

  validation {
    condition     = var.kms_key_arn == null || can(regex("^arn:aws[a-z-]*:kms:", var.kms_key_arn))
    error_message = "kms_key_arn must be a KMS key ARN (arn:aws...:kms:...) or null."
  }
}

variable "cloudfront_logging_bucket_config" {
  description = "Optional CloudFront standard access logging configuration. When set, a logging_config block is emitted on the distribution; when null, access logging is disabled. bucket (required) is the S3 bucket domain name (bucket-name.s3.amazonaws.com) that receives the logs, include_cookies (default false) controls whether cookies are logged, and prefix (default cloudfront-logs/) is the object key prefix for delivered log files."
  type = object({
    bucket          = string
    include_cookies = optional(bool, false)
    prefix          = optional(string, "cloudfront-logs/")
  })
  default = null

  validation {
    condition     = var.cloudfront_logging_bucket_config == null || try(length(var.cloudfront_logging_bucket_config.bucket) > 0, false)
    error_message = "cloudfront_logging_bucket_config.bucket is required when cloudfront_logging_bucket_config is set."
  }
}
