# CI/CD stage permissions (Req 15.2, 16.10): the least-privilege policy
# documents each pipeline stage needs beyond the codebuild module's
# baseline (logs + artifact bucket). Codified here — never attached to the
# live roles by hand — so `terraform apply` of the bootstrap layer is the
# single source of truth for what the pipelines may do.
#
# Design notes, distilled from a full end-to-end field deployment:
#
#   * Plan vs deploy separation: the iac BuildAndPlan stage gets a
#     READ-ONLY refresh surface (Describe/Get/List) that deliberately
#     EXCLUDES data-plane reads (s3:GetObject, dynamodb:GetItem,
#     secretsmanager:GetSecretValue, blanket ssm:GetParameter), so
#     planning can refresh state without gaining access to data. The one
#     exception is the Terraform-managed origin-verify SSM parameter,
#     which refresh must read for the saved plan to apply unchanged;
#     external secrets such as the VAPID private key stay unreadable.
#   * ec2:GetManagedPrefixListEntries is NOT covered by ec2:Describe*
#     (Get-prefixed) yet is required to refresh the CloudFront
#     origin-facing prefix list the ALB security group references.
#   * AppSync and Bedrock validate broader ARNs at apply time than their
#     documented resource formats suggest: scope to apis/* (which also
#     covers apis/*/channelNamespace/*) and guardrail/* rather than
#     name-exact ARNs.
#   * WAF logging to CloudWatch Logs rides the logs delivery control-plane
#     APIs (logs:CreateLogDelivery, logs:PutResourcePolicy, ...), which do
#     not support resource-level scoping — those actions carry "*" by
#     necessity, everything else is resource-scoped where the API allows.
#   * iam:PassRole is limited to the three runtime roles the app layer
#     creates ({env}-notifier, {env}-voice-task, {env}-voice-execution)
#     and conditioned on iam:PassedToService (ecs-tasks / lambda).
#   * The deploy surface includes lambda:UpdateFunctionCode and
#     lambda:UpdateFunctionConfiguration on {env}-* — every Notifier code
#     or configuration change flows through them.
#
# Managed-policy sizing: each document must stay within the 6,144-char
# managed-policy quota, which is why the apply surface is split into a
# core (network/compute/edge) and a platform (data/eventing/identity)
# document; the codebuild module attaches each as its own managed policy.

terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0"
    }
  }
}

data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  partition  = data.aws_partition.current.partition
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region

  # Must match the app layer's derived default (infrastructure/app/main.tf
  # ssm_parameter_path_prefix); override the variable only if the app
  # layer's prefix is overridden the same way. The prefix starts with "/",
  # so the ARN renders as parameter/<env>/... — never parameter//.
  ssm_parameter_path_prefix = coalesce(var.ssm_parameter_path_prefix, "/${var.environment}/voice-service")

  # The three runtime roles the app layer creates and CI must pass; names
  # are fixed in infrastructure/app/modules/iam and modules/notifications.
  runtime_role_arns = [
    "arn:${local.partition}:iam::${local.account_id}:role/${var.environment}-notifier",
    "arn:${local.partition}:iam::${local.account_id}:role/${var.environment}-voice-task",
    "arn:${local.partition}:iam::${local.account_id}:role/${var.environment}-voice-execution",
  ]

  ecs_service_arn = "arn:${local.partition}:ecs:${local.region}:${local.account_id}:service/${var.ecs_cluster_name}/${var.ecs_service_name}"
}

# ---------------------------------------------------------------------------
# Terraform state backend access (iac BuildAndPlan + Deploy): the app state
# object, the lock table, and nothing else in the bucket.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "tf_state" {
  statement {
    sid       = "ListStateBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation", "s3:GetBucketVersioning"]
    resources = [var.state_bucket_arn]
  }

  statement {
    sid       = "ReadWriteAppState"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${var.state_bucket_arn}/app/terraform.tfstate"]
  }

  statement {
    sid    = "StateLocking"
    effect = "Allow"
    actions = [
      "dynamodb:DescribeTable",
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:DeleteItem",
    ]
    resources = [var.lock_table_arn]
  }
}

# ---------------------------------------------------------------------------
# App-layer read/refresh surface (iac BuildAndPlan + Deploy): control-plane
# Describe/Get/List only — no data-plane reads (header note).
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "app_read" {
  statement {
    sid    = "AppLayerControlPlaneRead"
    effect = "Allow"
    actions = [
      "application-autoscaling:Describe*",
      "application-autoscaling:ListTagsForResource",
      "appsync:Get*",
      "appsync:List*",
      "bedrock:Get*",
      "bedrock:List*",
      "cloudfront:Get*",
      "cloudfront:List*",
      "cloudwatch:Describe*",
      "cloudwatch:List*",
      "cognito-idp:Describe*",
      "cognito-idp:Get*",
      "cognito-idp:List*",
      "dynamodb:Describe*",
      "dynamodb:List*",
      "ec2:Describe*",
      "ec2:GetManagedPrefixListEntries",
      "ecs:Describe*",
      "ecs:List*",
      "elasticloadbalancing:Describe*",
      "events:Describe*",
      "events:List*",
      "iam:Get*",
      "iam:List*",
      "kms:Describe*",
      "kms:List*",
      "lambda:Get*",
      "lambda:List*",
      "logs:Describe*",
      "logs:List*",
      "sns:Get*",
      "sns:List*",
      "ssm:DescribeParameters",
      "wafv2:Get*",
      "wafv2:List*",
    ]
    resources = ["*"]
  }

  # Bucket-configuration reads only — deliberately no s3:GetObject.
  statement {
    sid    = "BucketConfigurationRead"
    effect = "Allow"
    actions = [
      "s3:GetAccelerateConfiguration",
      "s3:GetBucket*",
      "s3:GetEncryptionConfiguration",
      "s3:GetLifecycleConfiguration",
      "s3:GetReplicationConfiguration",
      "s3:ListAllMyBuckets",
      "s3:ListBucket",
    ]
    resources = ["*"]
  }

  # The one Terraform-MANAGED SecureString: refresh must read it so a
  # saved plan applies unchanged. External secrets (for example the VAPID
  # private key under /<env>/notifier/) remain unreadable.
  statement {
    sid    = "OriginVerifyParameterRead"
    effect = "Allow"
    actions = [
      "ssm:GetParameter",
      "ssm:ListTagsForResource",
    ]
    resources = ["arn:${local.partition}:ssm:${local.region}:${local.account_id}:parameter${local.ssm_parameter_path_prefix}/origin-verify"]
  }
}

# ---------------------------------------------------------------------------
# App-layer apply surface, part 1 of 2 (iac Deploy only): network, load
# balancing, compute, auth, and edge. VPC/ELB/ECS control-plane actions
# largely predate resource-level scoping, so they carry "*"; everything
# nameable is scoped to the environment prefix.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "app_manage_core" {
  statement {
    sid    = "NetworkManage"
    effect = "Allow"
    actions = [
      "ec2:AllocateAddress",
      "ec2:AssociateRouteTable",
      "ec2:AttachInternetGateway",
      "ec2:AuthorizeSecurityGroupEgress",
      "ec2:AuthorizeSecurityGroupIngress",
      "ec2:CreateInternetGateway",
      "ec2:CreateNatGateway",
      "ec2:CreateRoute",
      "ec2:CreateRouteTable",
      "ec2:CreateSecurityGroup",
      "ec2:CreateSubnet",
      "ec2:CreateTags",
      "ec2:CreateVpc",
      "ec2:DeleteInternetGateway",
      "ec2:DeleteNatGateway",
      "ec2:DeleteRoute",
      "ec2:DeleteRouteTable",
      "ec2:DeleteSecurityGroup",
      "ec2:DeleteSubnet",
      "ec2:DeleteTags",
      "ec2:DeleteVpc",
      "ec2:DetachInternetGateway",
      "ec2:DisassociateAddress",
      "ec2:DisassociateRouteTable",
      "ec2:ModifySecurityGroupRules",
      "ec2:ModifySubnetAttribute",
      "ec2:ModifyVpcAttribute",
      "ec2:ReleaseAddress",
      "ec2:ReplaceRoute",
      "ec2:RevokeSecurityGroupEgress",
      "ec2:RevokeSecurityGroupIngress",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "LoadBalancerManage"
    effect = "Allow"
    actions = [
      "elasticloadbalancing:Add*",
      "elasticloadbalancing:Create*",
      "elasticloadbalancing:Delete*",
      "elasticloadbalancing:Deregister*",
      "elasticloadbalancing:Modify*",
      "elasticloadbalancing:Register*",
      "elasticloadbalancing:Remove*",
      "elasticloadbalancing:Set*",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "EcsManage"
    effect = "Allow"
    actions = [
      "ecs:CreateCluster",
      "ecs:CreateService",
      "ecs:DeleteCluster",
      "ecs:DeleteService",
      "ecs:DeregisterTaskDefinition",
      "ecs:RegisterTaskDefinition",
      "ecs:TagResource",
      "ecs:UntagResource",
      "ecs:UpdateCluster",
      "ecs:UpdateClusterSettings",
      "ecs:UpdateService",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "ServiceAutoScalingManage"
    effect = "Allow"
    actions = [
      "application-autoscaling:DeleteScalingPolicy",
      "application-autoscaling:DeregisterScalableTarget",
      "application-autoscaling:PutScalingPolicy",
      "application-autoscaling:RegisterScalableTarget",
      "application-autoscaling:TagResource",
      "application-autoscaling:UntagResource",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "CognitoManage"
    effect = "Allow"
    actions = [
      "cognito-idp:CreateUserPool",
      "cognito-idp:CreateUserPoolClient",
      "cognito-idp:CreateUserPoolDomain",
      "cognito-idp:DeleteUserPool",
      "cognito-idp:DeleteUserPoolClient",
      "cognito-idp:DeleteUserPoolDomain",
      "cognito-idp:SetUserPoolMfaConfig",
      "cognito-idp:TagResource",
      "cognito-idp:UntagResource",
      "cognito-idp:UpdateUserPool",
      "cognito-idp:UpdateUserPoolClient",
      "cognito-idp:UpdateUserPoolDomain",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "WafManage"
    effect = "Allow"
    actions = [
      "wafv2:AssociateWebACL",
      "wafv2:CreateWebACL",
      "wafv2:DeleteLoggingConfiguration",
      "wafv2:DeleteWebACL",
      "wafv2:DisassociateWebACL",
      "wafv2:PutLoggingConfiguration",
      "wafv2:TagResource",
      "wafv2:UntagResource",
      "wafv2:UpdateWebACL",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "CloudFrontManage"
    effect = "Allow"
    actions = [
      "cloudfront:CreateDistribution",
      "cloudfront:CreateDistributionWithTags",
      "cloudfront:CreateOriginAccessControl",
      "cloudfront:DeleteDistribution",
      "cloudfront:DeleteOriginAccessControl",
      "cloudfront:TagResource",
      "cloudfront:UntagResource",
      "cloudfront:UpdateDistribution",
      "cloudfront:UpdateOriginAccessControl",
    ]
    resources = ["*"]
  }

  # The frontend hosting bucket (name fixed by the cloudfront_s3 module:
  # <env>-frontend-<account>-<region>). Bucket configuration only — the
  # frontend PIPELINE syncs objects, never this role.
  statement {
    sid    = "FrontendBucketManage"
    effect = "Allow"
    actions = [
      "s3:CreateBucket",
      "s3:DeleteBucket",
      "s3:DeleteBucketPolicy",
      "s3:PutBucket*",
      "s3:PutEncryptionConfiguration",
      "s3:PutLifecycleConfiguration",
    ]
    resources = ["arn:${local.partition}:s3:::${var.environment}-frontend-${local.account_id}-${local.region}"]
  }
}

# ---------------------------------------------------------------------------
# App-layer apply surface, part 2 of 2 (iac Deploy only): data stores,
# eventing, guardrail, identity, and the outputs export.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "app_manage_platform" {
  statement {
    sid    = "DynamoDbManage"
    effect = "Allow"
    actions = [
      "dynamodb:CreateTable",
      "dynamodb:DeleteTable",
      "dynamodb:TagResource",
      "dynamodb:UntagResource",
      "dynamodb:UpdateContinuousBackups",
      "dynamodb:UpdateTable",
      "dynamodb:UpdateTimeToLive",
    ]
    resources = [
      "arn:${local.partition}:dynamodb:${local.region}:${local.account_id}:table/${var.environment}-*",
    ]
  }

  # apis/* also covers apis/<id>/channelNamespace/<name>; AppSync
  # validates these broader ARNs at apply time (header note).
  statement {
    sid    = "AppSyncEventsManage"
    effect = "Allow"
    actions = [
      "appsync:CreateApi",
      "appsync:CreateChannelNamespace",
      "appsync:DeleteApi",
      "appsync:DeleteChannelNamespace",
      "appsync:TagResource",
      "appsync:UntagResource",
      "appsync:UpdateApi",
      "appsync:UpdateChannelNamespace",
    ]
    resources = ["arn:${local.partition}:appsync:${local.region}:${local.account_id}:apis/*"]
  }

  # guardrail/* rather than a name-exact ARN (header note).
  statement {
    sid    = "BedrockGuardrailManage"
    effect = "Allow"
    actions = [
      "bedrock:CreateGuardrail",
      "bedrock:CreateGuardrailVersion",
      "bedrock:DeleteGuardrail",
      "bedrock:TagResource",
      "bedrock:UntagResource",
      "bedrock:UpdateGuardrail",
    ]
    resources = ["arn:${local.partition}:bedrock:${local.region}:${local.account_id}:guardrail/*"]
  }

  statement {
    sid    = "EventBridgeManage"
    effect = "Allow"
    actions = [
      "events:DeleteRule",
      "events:PutRule",
      "events:PutTargets",
      "events:RemoveTargets",
      "events:TagResource",
      "events:UntagResource",
    ]
    resources = ["arn:${local.partition}:events:${local.region}:${local.account_id}:rule/${var.environment}-*"]
  }

  # Includes lambda:UpdateFunctionCode / lambda:UpdateFunctionConfiguration:
  # every Notifier code or configuration change flows through them.
  statement {
    sid    = "LambdaManage"
    effect = "Allow"
    actions = [
      "lambda:AddPermission",
      "lambda:CreateFunction",
      "lambda:DeleteFunction",
      "lambda:RemovePermission",
      "lambda:TagResource",
      "lambda:UntagResource",
      "lambda:UpdateFunctionCode",
      "lambda:UpdateFunctionConfiguration",
    ]
    resources = ["arn:${local.partition}:lambda:${local.region}:${local.account_id}:function:${var.environment}-*"]
  }

  # Topic-prefixed resources also cover subscription ARNs
  # (<topic-arn>:<uuid>), so Subscribe/Unsubscribe stay env-scoped.
  statement {
    sid    = "SnsManage"
    effect = "Allow"
    actions = [
      "sns:CreateTopic",
      "sns:DeleteTopic",
      "sns:SetTopicAttributes",
      "sns:Subscribe",
      "sns:TagResource",
      "sns:UntagResource",
      "sns:Unsubscribe",
    ]
    resources = ["arn:${local.partition}:sns:${local.region}:${local.account_id}:${var.environment}-*"]
  }

  statement {
    sid    = "CloudWatchAlarmManage"
    effect = "Allow"
    actions = [
      "cloudwatch:DeleteAlarms",
      "cloudwatch:PutMetricAlarm",
      "cloudwatch:TagResource",
      "cloudwatch:UntagResource",
    ]
    resources = ["arn:${local.partition}:cloudwatch:${local.region}:${local.account_id}:alarm:${var.environment}-*"]
  }

  # Log-group names span three conventions (/aws/lambda/..., /ecs/...,
  # aws-waf-logs-...), so group management carries "*" within the account.
  statement {
    sid    = "LogGroupManage"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:DeleteLogGroup",
      "logs:PutRetentionPolicy",
      "logs:TagResource",
      "logs:UntagResource",
    ]
    resources = ["arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:*"]
  }

  # WAF -> CloudWatch Logs delivery control plane; these actions do not
  # support resource-level scoping (header note).
  statement {
    sid    = "WafLogsDelivery"
    effect = "Allow"
    actions = [
      "logs:CreateLogDelivery",
      "logs:DeleteLogDelivery",
      "logs:DeleteResourcePolicy",
      "logs:ListLogDeliveries",
      "logs:PutResourcePolicy",
      "logs:UpdateLogDelivery",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "RuntimeRoleManage"
    effect = "Allow"
    actions = [
      "iam:AttachRolePolicy",
      "iam:CreateRole",
      "iam:DeleteRole",
      "iam:DeleteRolePolicy",
      "iam:DetachRolePolicy",
      "iam:PutRolePolicy",
      "iam:TagRole",
      "iam:UntagRole",
      "iam:UpdateAssumeRolePolicy",
      "iam:UpdateRole",
      "iam:UpdateRoleDescription",
    ]
    resources = ["arn:${local.partition}:iam::${local.account_id}:role/${var.environment}-*"]
  }

  # Exactly the three runtime roles, only to the two services that assume
  # them (header note).
  statement {
    sid       = "PassRuntimeRoles"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = local.runtime_role_arns

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com", "lambda.amazonaws.com"]
    }
  }

  # First-apply conveniences: ECS, ELB, and ECS service auto scaling
  # create service-linked roles on first use in an account.
  statement {
    sid       = "ServiceLinkedRoles"
    effect    = "Allow"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["arn:${local.partition}:iam::${local.account_id}:role/aws-service-role/*"]

    condition {
      test     = "StringEquals"
      variable = "iam:AWSServiceName"
      values = [
        "ecs.amazonaws.com",
        "ecs.application-autoscaling.amazonaws.com",
        "elasticloadbalancing.amazonaws.com",
      ]
    }
  }

  # The origin-verify SecureString is the only parameter Terraform manages.
  statement {
    sid    = "SsmParameterManage"
    effect = "Allow"
    actions = [
      "ssm:AddTagsToResource",
      "ssm:DeleteParameter",
      "ssm:PutParameter",
      "ssm:RemoveTagsFromResource",
    ]
    resources = ["arn:${local.partition}:ssm:${local.region}:${local.account_id}:parameter${local.ssm_parameter_path_prefix}/*"]
  }

  # Outputs-export contract (ci/iac/deploy.yml): the apply ends by
  # uploading `terraform output -json` for the frontend pipeline to read.
  statement {
    sid       = "ExportAppOutputs"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${var.state_bucket_arn}/app-outputs/*"]
  }
}

# ---------------------------------------------------------------------------
# Backend pipeline: BuildAndPlan pushes the image, Deploy rolls ECS.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "backend_build" {
  # GetAuthorizationToken is account-scoped and does not support
  # resource-level permissions.
  statement {
    sid       = "EcrAuth"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPush"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [var.ecr_repository_arn]
  }
}

data "aws_iam_policy_document" "backend_deploy" {
  # Task-definition actions do not support resource-level scoping.
  statement {
    sid    = "TaskDefinitionManage"
    effect = "Allow"
    actions = [
      "ecs:DescribeTaskDefinition",
      "ecs:RegisterTaskDefinition",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "ServiceDeploy"
    effect = "Allow"
    actions = [
      "ecs:DescribeServices",
      "ecs:UpdateService",
    ]
    resources = [local.ecs_service_arn]
  }

  # RegisterTaskDefinition carries the task + execution role ARNs.
  statement {
    sid     = "PassTaskRoles"
    effect  = "Allow"
    actions = ["iam:PassRole"]
    resources = [
      "arn:${local.partition}:iam::${local.account_id}:role/${var.environment}-voice-task",
      "arn:${local.partition}:iam::${local.account_id}:role/${var.environment}-voice-execution",
    ]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# ---------------------------------------------------------------------------
# Frontend pipeline Deploy: read the exported app outputs, sync the bundle,
# invalidate the distribution. The bucket and distribution statements
# appear only once the two-phase wiring has supplied real values.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "frontend_deploy" {
  statement {
    sid       = "ReadExportedAppOutputs"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${var.state_bucket_arn}/app-outputs/latest.json"]
  }

  dynamic "statement" {
    for_each = var.frontend_bucket_name == "" ? [] : [var.frontend_bucket_name]

    content {
      sid       = "ListFrontendBucket"
      effect    = "Allow"
      actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
      resources = ["arn:${local.partition}:s3:::${statement.value}"]
    }
  }

  dynamic "statement" {
    for_each = var.frontend_bucket_name == "" ? [] : [var.frontend_bucket_name]

    content {
      sid    = "SyncFrontendBundle"
      effect = "Allow"
      actions = [
        "s3:DeleteObject",
        "s3:GetObject",
        "s3:PutObject",
      ]
      resources = ["arn:${local.partition}:s3:::${statement.value}/*"]
    }
  }

  dynamic "statement" {
    for_each = var.cloudfront_distribution_id == "" ? [] : [var.cloudfront_distribution_id]

    content {
      sid    = "InvalidateDistribution"
      effect = "Allow"
      actions = [
        "cloudfront:CreateInvalidation",
        "cloudfront:GetInvalidation",
      ]
      resources = ["arn:${local.partition}:cloudfront::${local.account_id}:distribution/${statement.value}"]
    }
  }
}
