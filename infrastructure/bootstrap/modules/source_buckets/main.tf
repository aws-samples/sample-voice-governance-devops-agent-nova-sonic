# source_buckets — three versioned S3 source buckets (frontend / backend / iac)
# acting as the pipeline source repositories (CodeCommit is closed to new
# customers), plus the EventBridge wiring that starts the matching CodePipeline
# when a new source archive is uploaded by scripts/push-source.sh.
# (Req 15.2, 16.1, 16.2)

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
  # The portal ships exactly three source-driven pipelines (Req 16.1); the
  # bucket set is structural, not environment-specific, so it is fixed here.
  # Environment-specific values (names, account, region) come from variables
  # and data sources only.
  source_names = toset(["frontend", "backend", "iac"])

  # Account id and region suffixes keep the globally unique S3 namespace
  # collision-free without hardcoding either value.
  bucket_names = {
    for source in local.source_names :
    source => "${var.project_name}-${var.environment}-${source}-source-${data.aws_caller_identity.current.account_id}-${data.aws_region.current.region}"
  }
}

# ---------------------------------------------------------------------------
# Source buckets: versioned (required by the CodePipeline S3 source action),
# SSE-encrypted, and closed to all public access.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "source" {
  for_each = local.source_names

  bucket = local.bucket_names[each.key]

  tags = merge(var.tags, {
    Name = local.bucket_names[each.key]
  })

  lifecycle {
    precondition {
      condition     = length(local.bucket_names[each.key]) <= 63
      error_message = "Computed source bucket name \"${local.bucket_names[each.key]}\" exceeds the 63-character S3 limit; shorten project_name or environment."
    }
  }
}

resource "aws_s3_bucket_versioning" "source" {
  for_each = local.source_names

  bucket = aws_s3_bucket.source[each.key].id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "source" {
  for_each = local.source_names

  bucket = aws_s3_bucket.source[each.key].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "source" {
  for_each = local.source_names

  bucket = aws_s3_bucket.source[each.key].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------------------------------------------------------------------------
# Bucket policies: deny plaintext (aws:SecureTransport = false) and TLS < 1.2
# requests on every bucket and object.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "source_bucket" {
  for_each = local.source_names

  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    resources = [
      aws_s3_bucket.source[each.key].arn,
      "${aws_s3_bucket.source[each.key].arn}/*",
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
      aws_s3_bucket.source[each.key].arn,
      "${aws_s3_bucket.source[each.key].arn}/*",
    ]

    condition {
      test     = "NumericLessThan"
      variable = "s3:TlsVersion"
      values   = ["1.2"]
    }
  }
}

resource "aws_s3_bucket_policy" "source" {
  for_each = local.source_names

  bucket = aws_s3_bucket.source[each.key].id
  policy = data.aws_iam_policy_document.source_bucket[each.key].json

  # Serialize against the public-access-block call on the same bucket to
  # avoid S3 conflicting-conditional-operation errors.
  depends_on = [aws_s3_bucket_public_access_block.source]
}

# ---------------------------------------------------------------------------
# EventBridge trigger wiring: bucket notifications to EventBridge, one rule
# per bucket matching an upload of the source archive, and a least-privilege
# role per rule that starts only the matching pipeline. (Req 16.2)
# ---------------------------------------------------------------------------

resource "aws_s3_bucket_notification" "source" {
  for_each = local.source_names

  bucket      = aws_s3_bucket.source[each.key].id
  eventbridge = true
}

data "aws_iam_policy_document" "eventbridge_assume_role" {
  statement {
    sid     = "AllowEventBridgeAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "start_pipeline" {
  for_each = local.source_names

  statement {
    sid       = "StartPipelineExecution"
    effect    = "Allow"
    actions   = ["codepipeline:StartPipelineExecution"]
    resources = [var.pipelines[each.key].arn]
  }
}

resource "aws_iam_role" "source_trigger" {
  for_each = local.source_names

  name               = "${var.project_name}-${var.environment}-${each.key}-source-trigger"
  assume_role_policy = data.aws_iam_policy_document.eventbridge_assume_role.json

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-${each.key}-source-trigger"
  })
}

resource "aws_iam_role_policy" "source_trigger" {
  for_each = local.source_names

  name   = "start-${each.key}-pipeline"
  role   = aws_iam_role.source_trigger[each.key].id
  policy = data.aws_iam_policy_document.start_pipeline[each.key].json
}

resource "aws_cloudwatch_event_rule" "source_upload" {
  for_each = local.source_names

  name        = "${var.project_name}-${var.environment}-${each.key}-source-upload"
  description = "Starts the ${each.key} pipeline when ${var.source_object_key} is uploaded to ${local.bucket_names[each.key]}."

  event_pattern = jsonencode({
    source        = ["aws.s3"]
    "detail-type" = ["Object Created"]
    detail = {
      bucket = {
        name = [aws_s3_bucket.source[each.key].bucket]
      }
      object = {
        key = [var.source_object_key]
      }
    }
  })

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-${each.key}-source-upload"
  })
}

resource "aws_cloudwatch_event_target" "start_pipeline" {
  for_each = local.source_names

  rule      = aws_cloudwatch_event_rule.source_upload[each.key].name
  target_id = "start-${each.key}-pipeline"
  arn       = var.pipelines[each.key].arn
  role_arn  = aws_iam_role.source_trigger[each.key].arn
}
