# Reusable CodeBuild project for pipeline stages (Req 15.2, 16.3).
#
# Each pipeline stage (SecurityScan, UnitTest, BuildAndPlan, Deploy) is one
# instance of this module. The buildspec is read from the pipeline SOURCE
# ARTIFACT (var.buildspec_path, e.g. ci/backend/scan.yml), so build logic
# lives in the source repository and changes without touching this layer.

terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

locals {
  # Use the caller-supplied role when given, otherwise the role created below.
  create_role      = var.service_role_arn == null
  service_role_arn = local.create_role ? aws_iam_role.this[0].arn : var.service_role_arn
  log_group_name   = "/aws/codebuild/${var.name}"
}

data "aws_partition" "current" {}

# ---------------------------------------------------------------------------
# CloudWatch log group.
# Created explicitly (rather than letting CodeBuild auto-create it) so that
# retention is bounded. CloudWatch Logs applies server-side encryption at
# rest to every log group by default.
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "this" {
  # nosemgrep: terraform.aws.security.aws-cloudwatch-log-group-unencrypted.aws-cloudwatch-log-group-unencrypted
  # CloudWatch Logs encrypts every log group at rest with an AWS-managed
  # key by default (Req 12.3). This group holds only pipeline build logs
  # (Terraform plan output, scan results) — a customer-managed KMS key is
  # not warranted for this bootstrap CI/CD group.
  name              = local.log_group_name
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

# ---------------------------------------------------------------------------
# Per-project IAM service role (created only when the caller does not pass
# one in). Least privilege: logs are scoped to this project's log group and
# artifact access is scoped to the pipeline artifact bucket — no wildcard
# resources.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "assume_role" {
  count = local.create_role ? 1 : 0

  statement {
    sid     = "CodeBuildAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["codebuild.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "permissions" {
  count = local.create_role ? 1 : 0

  statement {
    sid    = "WriteBuildLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      aws_cloudwatch_log_group.this.arn,
      "${aws_cloudwatch_log_group.this.arn}:*",
    ]
  }

  statement {
    sid    = "AccessPipelineArtifacts"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:PutObject",
    ]
    resources = ["${var.artifact_bucket_arn}/*"]
  }

  statement {
    sid       = "ListArtifactBucket"
    effect    = "Allow"
    actions   = ["s3:GetBucketLocation", "s3:ListBucket"]
    resources = [var.artifact_bucket_arn]
  }
}

resource "aws_iam_role" "this" {
  count = local.create_role ? 1 : 0

  name               = "${var.name}-codebuild"
  description        = "Service role for CodeBuild project ${var.name}"
  assume_role_policy = data.aws_iam_policy_document.assume_role[0].json
  tags               = var.tags
}

resource "aws_iam_role_policy" "this" {
  count = local.create_role ? 1 : 0

  name   = "${var.name}-codebuild"
  role   = aws_iam_role.this[0].id
  policy = data.aws_iam_policy_document.permissions[0].json
}

# Additional stage-specific permissions (for example the iac deploy
# stage's app-layer apply surface, or the backend build stage's ECR push).
# Customer-managed policies rather than inline: a role's inline policies
# share a single 10,240-character quota, which the iac deploy surface
# alone would exhaust, while each managed policy gets its own 6,144-char
# quota. Attached only to the module-created role — callers passing their
# own service_role_arn own that role's policies entirely.
resource "aws_iam_policy" "extra" {
  count = local.create_role ? length(var.extra_policy_documents) : 0

  name        = "${var.name}-codebuild-extra-${count.index}"
  description = "Stage-specific permissions ${count.index + 1}/${length(var.extra_policy_documents)} for CodeBuild project ${var.name}"
  policy      = var.extra_policy_documents[count.index]
  tags        = var.tags
}

resource "aws_iam_role_policy_attachment" "extra" {
  count = local.create_role ? length(var.extra_policy_documents) : 0

  role       = aws_iam_role.this[0].name
  policy_arn = aws_iam_policy.extra[count.index].arn
}

# ---------------------------------------------------------------------------
# CodeBuild project.
# ---------------------------------------------------------------------------
# CodeBuild encrypts build artifacts and cache at rest with the
# AWS-managed S3 key by default (Req 12.3). Artifacts flow only between
# pipeline stages inside the account; a customer-managed KMS key is not
# warranted for this bootstrap CI/CD project.
# nosemgrep: terraform.aws.security.aws-codebuild-project-unencrypted.aws-codebuild-project-unencrypted
resource "aws_codebuild_project" "this" {
  name          = var.name
  description   = var.description
  service_role  = local.service_role_arn
  build_timeout = var.build_timeout_minutes

  # Input and output artifacts are managed by the owning pipeline.
  artifacts {
    type = "CODEPIPELINE"
  }

  source {
    type = "CODEPIPELINE"
    # Path of the buildspec INSIDE the source artifact — not inline build
    # logic. CodeBuild resolves this path against the input artifact root.
    buildspec = var.buildspec_path
  }

  environment {
    image                       = var.image
    type                        = var.environment_type
    compute_type                = var.compute_type
    privileged_mode             = var.privileged_mode
    image_pull_credentials_type = "CODEBUILD"

    dynamic "environment_variable" {
      for_each = var.environment_variables

      content {
        name  = environment_variable.key
        value = environment_variable.value
        type  = "PLAINTEXT"
      }
    }
  }

  logs_config {
    cloudwatch_logs {
      status     = "ENABLED"
      group_name = aws_cloudwatch_log_group.this.name
    }
  }

  tags = var.tags
}
