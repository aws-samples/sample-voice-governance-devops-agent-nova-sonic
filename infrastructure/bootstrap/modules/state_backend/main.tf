# state_backend — remote Terraform state backend for the APP layer
# (infrastructure/app), created by the bootstrap layer so the two layers
# never mix state (Req 15.6): a versioned, SSE-encrypted S3 state bucket
# and a DynamoDB lock table. The bootstrap layer itself keeps its own
# separate local state and never stores state here.

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
  state_bucket_name = "${var.project_name}-${var.environment}-tf-state-${data.aws_caller_identity.current.account_id}-${data.aws_region.current.region}"

  # DynamoDB names are account+region scoped, so no suffix is needed.
  lock_table_name = "${var.project_name}-${var.environment}-tf-lock"
}

# ---------------------------------------------------------------------------
# State bucket: versioned (every state revision is recoverable),
# SSE-encrypted, and closed to all public access.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "state" {
  bucket = local.state_bucket_name

  tags = merge(var.tags, {
    Name = local.state_bucket_name
  })

  lifecycle {
    precondition {
      condition     = length(local.state_bucket_name) <= 63
      error_message = "Computed state bucket name \"${local.state_bucket_name}\" exceeds the 63-character S3 limit; shorten project_name or environment."
    }
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket = aws_s3_bucket.state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------------------------------------------------------------------------
# Bucket policy: deny plaintext (aws:SecureTransport = false) and TLS < 1.2
# requests on the bucket and every object.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "state_bucket" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    resources = [
      aws_s3_bucket.state.arn,
      "${aws_s3_bucket.state.arn}/*",
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
      aws_s3_bucket.state.arn,
      "${aws_s3_bucket.state.arn}/*",
    ]

    condition {
      test     = "NumericLessThan"
      variable = "s3:TlsVersion"
      values   = ["1.2"]
    }
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = data.aws_iam_policy_document.state_bucket.json

  # Serialize against the public-access-block call on the same bucket to
  # avoid S3 conflicting-conditional-operation errors.
  depends_on = [aws_s3_bucket_public_access_block.state]
}

# ---------------------------------------------------------------------------
# Lock table: serializes app-layer plans/applies via the S3 backend's
# dynamodb_table locking. SSE and point-in-time recovery are always on;
# deletion protection guards the lock table (and with it the app layer's
# ability to apply safely) against accidental destroy.
# ---------------------------------------------------------------------------

# server_side_encryption is enabled below with the AWS-owned DynamoDB key
# (Req 12.3). This table holds only Terraform state-lock records (a
# LockID and lock metadata) — no application data — so a customer-managed
# KMS key is not warranted; semgrep flags the absence of kms_key_arn.
# nosemgrep: terraform.aws.security.aws-dynamodb-table-unencrypted.aws-dynamodb-table-unencrypted
resource "aws_dynamodb_table" "lock" {
  name         = local.lock_table_name
  billing_mode = "PAY_PER_REQUEST"

  # The S3 backend requires exactly this key schema: a string hash key
  # named LockID.
  hash_key = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = true
  }

  deletion_protection_enabled = var.deletion_protection

  tags = merge(var.tags, {
    Name = local.lock_table_name
  })
}
