variable "environment" {
  description = "Environment name (for example dev or prod) used as the prefix for VPC, subnet, and gateway names."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.environment))
    error_message = "environment must start with a lowercase letter and contain only lowercase letters, digits, and hyphens."
  }
}

variable "vpc_cidr" {
  description = "IPv4 CIDR block for the VPC; public and private subnets are carved from it with subnet_newbits."
  type        = string
  default     = "10.0.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "vpc_cidr must be a valid IPv4 CIDR block (for example 10.0.0.0/16)."
  }
}

variable "az_count" {
  description = "Number of Availability Zones to span with one public and one private subnet each; at least 2 so the ECS service keeps tasks in ≥2 AZs (Req 10.1)."
  type        = number
  default     = 2

  validation {
    condition     = var.az_count >= 2
    error_message = "az_count must be at least 2 so the voice service spans at least two Availability Zones (Req 10.1)."
  }
}

variable "subnet_newbits" {
  description = "Additional bits added to the VPC CIDR prefix for each subnet (cidrsubnet newbits); 8 turns a /16 VPC into /24 subnets."
  type        = number
  default     = 8

  validation {
    condition     = var.subnet_newbits >= 1 && var.subnet_newbits <= 12
    error_message = "subnet_newbits must be between 1 and 12."
  }
}

variable "single_nat_gateway" {
  description = "When true, all private subnets share one NAT gateway (cost control for non-production); when false, each AZ gets its own NAT gateway so an AZ loss never severs egress for tasks in the surviving AZs."
  type        = bool
  default     = false
}
