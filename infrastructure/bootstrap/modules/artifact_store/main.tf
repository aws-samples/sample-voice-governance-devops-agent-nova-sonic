# artifact_store — the single S3 artifact bucket shared by all three
# CodePipeline instances (frontend / backend / iac) as their artifact store
# (Req 15.2). Versioned, SSE-encrypted, closed to public access, and
# TLS-enforcing, matching the security baseline of every bucket in this
# project. Pipeline and CodeBuild IAM roles are scoped to this bucket by
# the sibling pipeline/codebuild modules, which receive its name and derive
# its ARN.

terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0"
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  # Account id and region suffixes keep the globally unique S3 namespace
  # collision-free without hardcoding either value.
  bucket_name = "${var.project_name}-${var.environment}-artifacts-${data.aws_caller_identity.current.account_id}-${data.aws_region.current.region}"
}

# ---------------------------------------------------------------------------
# Artifact bucket: versioned, SSE-encrypted, and closed to all public
# access. CodePipeline objects are protected by the bucket's default
# server-side encryption.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "artifacts" {
  bucket = local.bucket_name

  tags = merge(var.tags, {
    Name = local.bucket_name
  })

  lifecycle {
    precondition {
      condition     = length(local.bucket_name) <= 63
      error_message = "Computed artifact bucket name \"${local.bucket_name}\" exceeds the 63-character S3 limit; shorten project_name or environment."
    }
  }
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------------------------------------------------------------------------
# Bucket policy: deny plaintext (aws:SecureTransport = false) and TLS < 1.2
# requests on the bucket and every object.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "artifact_bucket" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    resources = [
      aws_s3_bucket.artifacts.arn,
      "${aws_s3_bucket.artifacts.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid     = "DenyTlsBelow12"
    effect  = "Deny"
    actions = ["s3:*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    resources = [
      aws_s3_bucket.artifacts.arn,
      "${aws_s3_bucket.artifacts.arn}/*",
    ]

    condition {
      test     = "NumericLessThan"
      variable = "s3:TlsVersion"
      values   = ["1.2"]
    }
  }
}

resource "aws_s3_bucket_policy" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  policy = data.aws_iam_policy_document.artifact_bucket.json

  # Serialize against the public-access-block call on the same bucket to
  # avoid S3 conflicting-conditional-operation errors.
  depends_on = [aws_s3_bucket_public_access_block.artifacts]
}
