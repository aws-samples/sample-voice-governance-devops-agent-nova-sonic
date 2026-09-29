# cloudfront_s3 module (Req 9.1, 12.1, 12.2, 13.2, 13.3, 13.7): the frontend
# S3 bucket and the single CloudFront distribution serving both planes on
# the CloudFront default domain:
#   - default behavior  -> S3 bucket via OAC (static SPA assets);
#   - /ws/* and /api/*  -> ALB custom origin (voice WebSocket + API). The
#     browser's wss:// connection rides this path: a page served over HTTPS
#     cannot open ws:// (prose, not code). # nosemgrep: javascript.lang.security.detect-insecure-websocket.detect-insecure-websocket
#     No custom-domain certificate exists for the
#     ALB, so CloudFront terminates TLS on its default certificate and
#     forwards to the ALB's HTTP listener (design research finding 7).
#
# Region note: the CLOUDFRONT-scope WAF web ACL referenced by web_acl_id
# must be created in us-east-1. The whole stack deploys to us-east-1 per
# the design (Nova Sonic region), so the default provider already points
# there and no provider alias is needed.

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

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  # Account id and region suffixes keep the globally unique S3 namespace
  # collision-free without hardcoding either value.
  bucket_name = "${var.environment}-frontend-${data.aws_caller_identity.current.account_id}-${data.aws_region.current.region}"

  s3_origin_id  = "frontend-s3"
  alb_origin_id = "voice-alb"

  # Voice_Service paths routed to the ALB origin; everything else falls
  # through to the S3 default behavior.
  alb_path_patterns = ["/ws/*", "/api/*"]
}

# ---------------------------------------------------------------------------
# Frontend bucket (Req 9.1): versioned, SSE-encrypted, closed to all public
# access. Readable only by this CloudFront distribution via OAC (Req 13.7).
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "frontend" {
  bucket = local.bucket_name

  lifecycle {
    precondition {
      condition     = length(local.bucket_name) <= 63
      error_message = "Computed frontend bucket name \"${local.bucket_name}\" exceeds the 63-character S3 limit; shorten environment."
    }
  }
}

resource "aws_s3_bucket_versioning" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  versioning_configuration {
    status = var.versioning_enabled ? "Enabled" : "Suspended"
  }
}

# SSE (Req 12.3): SSE-S3 (AES256) by default; a customer managed KMS key
# can be supplied via var.kms_key_arn.
resource "aws_s3_bucket_server_side_encryption_configuration" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = var.kms_key_arn == null ? "AES256" : "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }

    # S3 Bucket Keys only apply to SSE-KMS.
    bucket_key_enabled = var.kms_key_arn != null
  }
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# OAC-only read allow (Req 13.7): s3:GetObject for the CloudFront service
# principal, conditioned on AWS:SourceArn = this distribution — no OAI, no
# public read. "Deny all other principals" is implicit: the public access
# block above rejects any public grant and this policy contains no other
# Allow statement, so every non-CloudFront principal is denied by default.
data "aws_iam_policy_document" "oac_read" {
  statement {
    sid     = "AllowCloudFrontServicePrincipalReadOnly"
    effect  = "Allow"
    actions = ["s3:GetObject"]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    resources = ["${aws_s3_bucket.frontend.arn}/*"]

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.this.arn]
    }
  }
}

# Single bucket policy composing the OAC allow with the mandatory TLS
# hardening denies (aws:SecureTransport = false and TLS < 1.2) via the
# reusable s3_policies module (Req 13.2, 13.3, 13.7).
module "frontend_bucket_policy" {
  source = "../s3_policies"

  bucket_id              = aws_s3_bucket.frontend.id
  bucket_arn             = aws_s3_bucket.frontend.arn
  additional_policy_json = data.aws_iam_policy_document.oac_read.json

  # Serialize against the public-access-block call on the same bucket to
  # avoid S3 conflicting-conditional-operation errors.
  depends_on = [aws_s3_bucket_public_access_block.frontend]
}

# ---------------------------------------------------------------------------
# CloudFront distribution (Req 9.1, 12.1, 12.2)
# ---------------------------------------------------------------------------

resource "aws_cloudfront_origin_access_control" "frontend" {
  name                              = "${var.environment}-frontend-oac"
  description                       = "OAC for the ${var.environment} frontend bucket (SigV4-signed S3 origin requests)."
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# AWS managed policies looked up by their well-known names instead of
# hardcoded ids.
data "aws_cloudfront_cache_policy" "caching_optimized" {
  name = "Managed-CachingOptimized"
}

data "aws_cloudfront_cache_policy" "caching_disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_viewer_except_host" {
  name = "Managed-AllViewerExceptHostHeader"
}
#checkov:skip=CKV_AWS_310: Origin failover is not needed here as it is hosting S3 with static content.
#checkov:skip=CKV_AWS_374: No geo restriction needed as the content is sample and might need to support all locations
resource "aws_cloudfront_distribution" "this" {
  enabled             = true
  is_ipv6_enabled     = true
  comment             = "${var.environment} support portal (frontend + voice/API paths)"
  default_root_object = "index.html"
  price_class         = var.price_class
  # CLOUDFRONT-scope WAF web ACL (Req 11.1); created in us-east-1 by the
  # waf module (see region note in the header). Null skips the
  # association, but the app root always passes it.
  web_acl_id = var.web_acl_arn

  origin {
    origin_id                = local.s3_origin_id
    domain_name              = aws_s3_bucket.frontend.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend.id
  }

  origin {
    origin_id   = local.alb_origin_id
    domain_name = var.alb_dns_name

    # http-only: the ALB has no custom-domain certificate (Req 12.4), so
    # CloudFront terminates viewer TLS on its default certificate
    # (Req 12.2 note) and reaches the ALB over its HTTP listener. The
    # x-origin-verify header below gates that hop — the ALB listener only
    # forwards requests carrying the shared secret, so the plaintext path
    # cannot be reached directly — and Cognito JWT validation at the
    # WebSocket handshake protects the connection itself (Req 12.5).
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }

    # Origin-verification shared secret injected on every request to the
    # ALB; the ALB listener rule requires it (design research finding 7).
    custom_header {
      name  = var.origin_verify_header_name
      value = var.origin_verify_secret
    }
  }

  dynamic "logging_config" {
    for_each = var.cloudfront_logging_bucket_config == null ? [] : [var.cloudfront_logging_bucket_config]

    content {
      include_cookies = logging_config.value.include_cookies
      bucket          = logging_config.value.bucket
      prefix          = logging_config.value.prefix
    }
  }

  # SPA assets from S3: HTTPS enforced by redirecting plain-HTTP viewers
  # (Req 12.2), long-lived optimized caching, compression on.
  default_cache_behavior {
    target_origin_id       = local.s3_origin_id
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    cache_policy_id        = data.aws_cloudfront_cache_policy.caching_optimized.id
  }

  # Voice WebSocket and API paths to the ALB. redirect-to-https also covers
  # the WebSocket handshake (wss:// arrives as an https request). Caching
  # is disabled and AllViewerExceptHostHeader forwards all headers (incl.
  # the WebSocket upgrade pair), query strings, and cookies to the origin
  # while letting CloudFront set the Host the ALB expects.
  dynamic "ordered_cache_behavior" {
    for_each = local.alb_path_patterns

    content {
      path_pattern             = ordered_cache_behavior.value
      target_origin_id         = local.alb_origin_id
      viewer_protocol_policy   = "redirect-to-https"
      allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
      cached_methods           = ["GET", "HEAD"]
      cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled.id
      origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id
    }
  }

  # HTTPS on the CloudFront default domain certificate (Req 9.1, 12.1).
  viewer_certificate {
    cloudfront_default_certificate = true
    minimum_protocol_version       = "TLSv1.2_2021"
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  # SPA routing: deep links hit S3 keys that do not exist, which the OAC
  # origin surfaces as 403 (no s3:ListBucket grant) or 404 — rewrite both
  # to index.html so the router takes over client-side.
  custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 10
  }

  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 10
  }
}
