# WAF module (Req 11.1-11.6): one aws_wafv2_web_acl, parameterized by scope
# so the app root instantiates it twice — once with scope = "CLOUDFRONT" for
# the distribution and once with scope = "REGIONAL" for the internet-facing
# ALB (Req 11.1).
#
# Scope/region note: a CLOUDFRONT-scope web ACL (and its logging
# destination) must live in us-east-1. The whole stack deploys to us-east-1
# per the design (Nova Sonic region), so the default provider already
# points there and no provider alias is needed.
#
# Association split:
#   - REGIONAL: attached to the ALB below via aws_wafv2_web_acl_association;
#   - CLOUDFRONT: CloudFront does not support the association resource —
#     the distribution references this ACL through its web_acl_id argument
#     (see the cloudfront_s3 module).

terraform {
  required_version = ">= 1.9"

  # All app-layer modules pin the same AWS provider series. The floor is
  # v6.9.0 because the AppSync Events resources (aws_appsync_api /
  # aws_appsync_channel_namespace) used by the appsync_events module first
  # shipped in that release.
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.9.0, < 7.0.0"
    }
  }
}

locals {
  # e.g. dev-cloudfront-web-acl / dev-regional-web-acl.
  acl_name = "${var.environment}-${lower(var.scope)}-web-acl"
}

resource "aws_wafv2_web_acl" "this" {
  name = local.acl_name
  # No parentheses: WAF web ACL descriptions only allow the characters
  # matched by ^[\w+=:#@/\-,\.][\w+=:#@/\-,\.\s]+$ — an out-of-pattern
  # description is rejected at apply time.
  description = "Web ACL at ${var.scope} scope for the ${var.environment} support portal."
  scope       = var.scope

  # Requests matching no rule pass through; the managed rule groups below
  # block matching requests (Req 11.4).
  default_action {
    allow {}
  }

  # Optional best-practice rate limit: blocks source IPs exceeding
  # var.rate_limit requests per 5-minute window. Disabled when
  # var.rate_limit is null.
  dynamic "rule" {
    for_each = var.rate_limit == null ? [] : [var.rate_limit]

    content {
      name     = "rate-limit"
      priority = 0

      action {
        block {}
      }

      statement {
        rate_based_statement {
          limit              = rule.value
          aggregate_key_type = "IP"
        }
      }

      visibility_config {
        cloudwatch_metrics_enabled = true
        metric_name                = "${local.acl_name}-rate-limit"
        sampled_requests_enabled   = true
      }
    }
  }

  # AWS managed core rule set (Req 11.2). override_action none means the
  # rule group's own rule actions apply — matching requests are BLOCKED,
  # not merely counted (Req 11.3).
  rule {
    name     = "aws-managed-common"
    priority = 1

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.acl_name}-common"
      sampled_requests_enabled   = true
    }
  }

  # AWS managed known bad inputs rule set (Req 11.2), likewise in BLOCK
  # mode via override_action none (Req 11.3).
  rule {
    name     = "aws-managed-known-bad-inputs"
    priority = 2

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.acl_name}-known-bad-inputs"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = local.acl_name
    sampled_requests_enabled   = true
  }
}

# Persistent WAF log destination (Req 11.5): a CloudWatch Logs log group
# whose name MUST start with "aws-waf-logs-" (WAF API requirement).
# CloudWatch Logs encrypts log data at rest by default; var.kms_key_arn
# optionally switches encryption to a customer managed key (Req 12.3).
resource "aws_cloudwatch_log_group" "waf" {
  name              = "aws-waf-logs-${var.environment}-${lower(var.scope)}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

# WAF logging (Req 11.6): every logged (including blocked) request carries
# the timestamp, source IP (httpRequest.clientIp), requested URI
# (httpRequest.uri), and the id of the rule that matched
# (terminatingRuleId) — no fields are redacted.
resource "aws_wafv2_web_acl_logging_configuration" "this" {
  log_destination_configs = [aws_cloudwatch_log_group.waf.arn]
  resource_arn            = aws_wafv2_web_acl.this.arn
}

# REGIONAL scope only: attach the web ACL to the internet-facing ALB
# (Req 11.1). CloudFront-scope ACLs are attached through the
# distribution's web_acl_id instead. The count depends only on var.scope
# (a plan-time literal) — a null-check on var.alb_arn would break planning
# whenever the caller wires the ARN from another module's output, because
# unknown values cannot drive count; alb_arn presence for REGIONAL scope
# is enforced by the variable's validation instead.
resource "aws_wafv2_web_acl_association" "alb" {
  count = var.scope == "REGIONAL" ? 1 : 0

  resource_arn = var.alb_arn
  web_acl_arn  = aws_wafv2_web_acl.this.arn
}
