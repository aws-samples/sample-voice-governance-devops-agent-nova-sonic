variable "bucket_id" {
  description = "Name (id) of the S3 bucket the TLS-hardening policy is attached to."
  type        = string

  validation {
    condition     = length(var.bucket_id) > 0
    error_message = "bucket_id is required."
  }
}

variable "bucket_arn" {
  description = "ARN of the S3 bucket; the deny statements target this ARN and every object under it."
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-z-]*:s3:::", var.bucket_arn))
    error_message = "bucket_arn must be an S3 bucket ARN (arn:aws...:s3:::...)."
  }
}

variable "additional_policy_json" {
  description = "Optional extra bucket policy JSON to merge with the TLS deny statements (S3 accepts only one policy per bucket); for example the frontend bucket's CloudFront OAC read allow (Req 13.7). When null, the policy carries only the two deny statements."
  type        = string
  default     = null

  validation {
    condition     = var.additional_policy_json == null || can(jsondecode(var.additional_policy_json))
    error_message = "additional_policy_json must be a valid JSON policy document or null."
  }
}
