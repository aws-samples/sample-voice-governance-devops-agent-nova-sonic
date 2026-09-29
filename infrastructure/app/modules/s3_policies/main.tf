# s3_policies module (Req 13.2, 13.3): reusable TLS-hardening bucket policy
# applied to every S3 bucket the Application_Layer creates. It attaches one
# bucket policy carrying two Deny statements — plaintext requests
# (aws:SecureTransport = false) and TLS below 1.2 (s3:TlsVersion < 1.2) —
# on the bucket and every object, for all principals.
#
# S3 allows a single policy document per bucket, so callers that need
# bucket-specific Allow statements (for example the frontend bucket's
# CloudFront OAC read, Req 13.7) merge them in through
# var.additional_policy_json; the deny statements below are always appended.

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

data "aws_iam_policy_document" "this" {
  # Caller-supplied statements (if any) are merged ahead of the two deny
  # statements below; statement sids must not collide with the ones here.
  source_policy_documents = var.additional_policy_json == null ? [] : [var.additional_policy_json]

  # Deny every request that does not arrive over TLS (Req 13.2).
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    resources = [
      var.bucket_arn,
      "${var.bucket_arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  # Deny every request negotiated with a TLS version below 1.2 (Req 13.3).
  statement {
    sid     = "DenyTlsBelow12"
    effect  = "Deny"
    actions = ["s3:*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    resources = [
      var.bucket_arn,
      "${var.bucket_arn}/*",
    ]

    condition {
      test     = "NumericLessThan"
      variable = "s3:TlsVersion"
      values   = ["1.2"]
    }
  }
}

resource "aws_s3_bucket_policy" "this" {
  bucket = var.bucket_id
  policy = data.aws_iam_policy_document.this.json
}
