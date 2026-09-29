# Reusable CI/CD pipeline (Req 15.2, 16.1, 16.3, 16.5-16.8, 16.10).
#
# One CodePipeline V2 definition instantiated once per pipeline (frontend,
# backend, iac) by the bootstrap root. Stage order:
#
#   Source (S3) -> SecurityScan -> UnitTest -> BuildAndPlan
#     -> ManualApproval -> Deploy
#
# CodePipeline stages are strictly sequential: a stage starts only after the
# previous stage succeeds, and a failing stage stops and fails the execution
# with no subsequent stage running (Req 16.3, 16.5).
#
# There is NO CodePipeline S3 deploy action anywhere in this definition
# (Req 16.10). Deployment always runs inside the Deploy CodeBuild project —
# the frontend deploy performs `aws s3 sync` plus a CloudFront invalidation
# from its buildspec (Req 16.9). The S3 deploy action could not order the
# sync before the invalidation, and keeping deploys in CodeBuild keeps all
# three pipelines on one reusable definition.

terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

data "aws_partition" "current" {}

locals {
  create_role       = var.pipeline_service_role_arn == null
  pipeline_role_arn = local.create_role ? aws_iam_role.pipeline[0].arn : var.pipeline_service_role_arn

  # Bucket ARNs are derived from the bucket names so callers pass plain
  # names; the partition comes from a data source, never hardcoded.
  source_bucket_arn   = "arn:${data.aws_partition.current.partition}:s3:::${var.source_bucket_name}"
  artifact_bucket_arn = "arn:${data.aws_partition.current.partition}:s3:::${var.artifact_bucket_name}"
}

# ---------------------------------------------------------------------------
# Per-stage CodeBuild projects. Buildspecs are read from the SOURCE artifact
# at the caller-supplied paths, so stage logic lives in the source repository.
# ---------------------------------------------------------------------------
module "security_scan" {
  source = "../codebuild"

  name                  = "${var.name}-security-scan"
  description           = "SecurityScan stage of pipeline ${var.name}: fails on high or critical findings (Req 16.4)"
  buildspec_path        = var.scan_buildspec_path
  image                 = var.codebuild_image
  compute_type          = var.codebuild_compute_type
  environment_variables = var.environment_variables
  service_role_arn      = var.codebuild_service_role_arn
  artifact_bucket_arn   = local.artifact_bucket_arn
  log_retention_days    = var.log_retention_days
  tags                  = var.tags
}

module "unit_test" {
  source = "../codebuild"

  name                  = "${var.name}-unit-test"
  description           = "UnitTest stage of pipeline ${var.name}"
  buildspec_path        = var.test_buildspec_path
  image                 = var.codebuild_image
  compute_type          = var.codebuild_compute_type
  environment_variables = var.environment_variables
  service_role_arn      = var.codebuild_service_role_arn
  artifact_bucket_arn   = local.artifact_bucket_arn
  log_retention_days    = var.log_retention_days
  tags                  = var.tags
}

module "build_and_plan" {
  source = "../codebuild"

  name                   = "${var.name}-build-and-plan"
  description            = "BuildAndPlan stage of pipeline ${var.name}"
  buildspec_path         = var.build_buildspec_path
  image                  = var.codebuild_image
  compute_type           = var.codebuild_compute_type
  privileged_mode        = var.build_privileged_mode
  environment_variables  = merge(var.environment_variables, var.build_environment_variables)
  service_role_arn       = var.codebuild_service_role_arn
  extra_policy_documents = var.build_extra_policy_documents
  artifact_bucket_arn    = local.artifact_bucket_arn
  log_retention_days     = var.log_retention_days
  tags                   = var.tags
}

module "deploy" {
  source = "../codebuild"

  name                   = "${var.name}-deploy"
  description            = "Deploy stage of pipeline ${var.name}: deployment runs inside CodeBuild, never via a CodePipeline S3 deploy action (Req 16.10)"
  buildspec_path         = var.deploy_buildspec_path
  image                  = var.codebuild_image
  compute_type           = var.codebuild_compute_type
  privileged_mode        = var.deploy_privileged_mode
  environment_variables  = merge(var.environment_variables, var.deploy_environment_variables)
  service_role_arn       = var.codebuild_service_role_arn
  extra_policy_documents = var.deploy_extra_policy_documents
  artifact_bucket_arn    = local.artifact_bucket_arn
  log_retention_days     = var.log_retention_days
  tags                   = var.tags
}

# ---------------------------------------------------------------------------
# Pipeline IAM service role (created only when the caller does not pass one
# in). Least privilege: statements are scoped to this pipeline's source
# object, artifact bucket, four CodeBuild projects, and (when configured)
# approval topic — no wildcard resources.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "pipeline_assume_role" {
  count = local.create_role ? 1 : 0

  statement {
    sid     = "CodePipelineAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["codepipeline.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "pipeline_permissions" {
  count = local.create_role ? 1 : 0

  statement {
    sid    = "ReadSourceArchive"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
    ]
    resources = ["${local.source_bucket_arn}/${var.source_object_key}"]
  }

  statement {
    sid    = "ReadWriteArtifacts"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:PutObject",
    ]
    resources = ["${local.artifact_bucket_arn}/*"]
  }

  statement {
    sid    = "ListPipelineBuckets"
    effect = "Allow"
    actions = [
      "s3:GetBucketLocation",
      "s3:GetBucketVersioning",
      "s3:ListBucket",
    ]
    resources = [
      local.source_bucket_arn,
      local.artifact_bucket_arn,
    ]
  }

  statement {
    sid    = "RunStageBuilds"
    effect = "Allow"
    actions = [
      "codebuild:BatchGetBuilds",
      "codebuild:StartBuild",
      "codebuild:StopBuild",
    ]
    resources = [
      module.security_scan.project_arn,
      module.unit_test.project_arn,
      module.build_and_plan.project_arn,
      module.deploy.project_arn,
    ]
  }

  dynamic "statement" {
    for_each = var.approval_sns_topic_arn == null ? [] : [var.approval_sns_topic_arn]

    content {
      sid       = "NotifyApprovers"
      effect    = "Allow"
      actions   = ["sns:Publish"]
      resources = [statement.value]
    }
  }
}

resource "aws_iam_role" "pipeline" {
  count = local.create_role ? 1 : 0

  name               = "${var.name}-codepipeline"
  description        = "Service role for CodePipeline ${var.name}"
  assume_role_policy = data.aws_iam_policy_document.pipeline_assume_role[0].json
  tags               = var.tags
}

resource "aws_iam_role_policy" "pipeline" {
  count = local.create_role ? 1 : 0

  name   = "${var.name}-codepipeline"
  role   = aws_iam_role.pipeline[0].id
  policy = data.aws_iam_policy_document.pipeline_permissions[0].json
}

# ---------------------------------------------------------------------------
# The pipeline.
# ---------------------------------------------------------------------------
resource "aws_codepipeline" "this" {
  name          = var.name
  role_arn      = local.pipeline_role_arn
  pipeline_type = "V2"

  # No encryption_key block: artifacts are protected by the artifact
  # bucket's default server-side encryption.
  artifact_store {
    type     = "S3"
    location = var.artifact_bucket_name
  }

  stage {
    name = "Source"

    action {
      name             = "Source"
      category         = "Source"
      owner            = "AWS"
      provider         = "S3"
      version          = "1"
      output_artifacts = ["SourceOutput"]

      configuration = {
        S3Bucket    = var.source_bucket_name
        S3ObjectKey = var.source_object_key
        # An EventBridge rule on the source bucket starts the pipeline when
        # a new source version is uploaded (Req 16.2), so polling stays off.
        PollForSourceChanges = "false"
      }
    }
  }

  stage {
    name = "SecurityScan"

    action {
      name            = "SecurityScan"
      category        = "Test"
      owner           = "AWS"
      provider        = "CodeBuild"
      version         = "1"
      input_artifacts = ["SourceOutput"]

      configuration = {
        ProjectName = module.security_scan.project_name
      }
    }
  }

  stage {
    name = "UnitTest"

    action {
      name            = "UnitTest"
      category        = "Test"
      owner           = "AWS"
      provider        = "CodeBuild"
      version         = "1"
      input_artifacts = ["SourceOutput"]

      configuration = {
        ProjectName = module.unit_test.project_name
      }
    }
  }

  stage {
    name = "BuildAndPlan"

    action {
      name             = "BuildAndPlan"
      category         = "Build"
      owner            = "AWS"
      provider         = "CodeBuild"
      version          = "1"
      input_artifacts  = ["SourceOutput"]
      output_artifacts = ["BuildOutput"]

      configuration = {
        ProjectName = module.build_and_plan.project_name
      }
    }
  }

  stage {
    name = "ManualApproval"

    # A pending approval that is neither approved nor rejected fails after
    # CodePipeline's built-in 7-day approval timeout, stopping the execution
    # before Deploy (Req 16.6-16.8). The timeout is the service default and
    # is not configurable on the action.
    action {
      name     = "ManualApproval"
      category = "Approval"
      owner    = "AWS"
      provider = "Manual"
      version  = "1"

      configuration = merge(
        {
          CustomData = "Review the BuildAndPlan output for pipeline ${var.name} before approving deployment."
        },
        var.approval_sns_topic_arn == null ? {} : { NotificationArn = var.approval_sns_topic_arn }
      )
    }
  }

  stage {
    name = "Deploy"

    # Deployment runs INSIDE CodeBuild (category Build) — deliberately not a
    # CodePipeline S3 deploy action (Req 16.10). The frontend pipeline's
    # deploy buildspec runs `aws s3 sync` and then a CloudFront invalidation
    # (Req 16.9); backend and iac deploys likewise run their own tooling.
    # BuildOutput is passed as a secondary artifact so the deploy can ship
    # exactly what BuildAndPlan produced and the approval reviewed (built
    # assets, container image digest, terraform plan file); the buildspec
    # still resolves from the source artifact (PrimarySource).
    action {
      name            = "Deploy"
      category        = "Build"
      owner           = "AWS"
      provider        = "CodeBuild"
      version         = "1"
      input_artifacts = ["SourceOutput", "BuildOutput"]

      configuration = {
        ProjectName   = module.deploy.project_name
        PrimarySource = "SourceOutput"
      }
    }
  }

  tags = var.tags
}
