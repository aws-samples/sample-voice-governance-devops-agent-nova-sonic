# DevOps Agent account access (Req 3.2, 4.2): the IAM role the AWS DevOps
# Agent assumes to inspect this account on the engineer's behalf.
#
# WHY THIS EXISTS. The agent's own service-linked role
# (AWSServiceRoleForAIDevOps) grants it almost nothing — CloudWatch metric
# publication for itself, VPC Lattice gateway management, and exactly three
# EC2 calls (DescribeVpcs, DescribeSubnets, DescribeSecurityGroups). It
# cannot call ec2:DescribeInstances, read IAM, query SSM, or read logs. An
# agent space with no account association therefore answers every
# diagnostic question with nothing, which is what the portal was doing:
# the engineer asked for instance ids and the agent returned an empty
# response, with no error anywhere to explain it.
#
# The service takes account access through AssociateService, whose AWS
# configuration carries an `assumableRoleArn` — "Role ARN to be assumed by
# AIDevOps to operate on behalf of customer". This module creates exactly
# that role. The association call itself is not expressible in Terraform
# (the AWS provider has no devops-agent resources), so it remains an
# operator step; `devops_agent_assumable_role_arn` is exported for it.
#
# PERMISSION MODEL. AWS managed ReadOnlyAccess supplies the broad
# describe/list/get surface a diagnostic assistant needs across every
# service, and an explicit Deny then removes the data-plane reads that
# would let the agent read customer or portal *content* rather than
# configuration. Explicit Deny always wins over the managed policy's
# Allow, so the boundary holds no matter how ReadOnlyAccess evolves.
#
# NOTE ON MUTATION. `agentElevatedRoleArn` — the optional second role the
# service assumes for state-changing "directed actions" — is deliberately
# NEVER configured, so the account association cannot mutate anything even
# if the guardrail and the mutation backstop were both bypassed. That is
# the third, IAM-level layer of Req 4.2's read-only guarantee.

terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.9.0, < 7.0.0"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  role_name = "${var.environment}-devops-agent-readonly"
}

# Trust policy: only the DevOps Agent service may assume the role, and only
# on behalf of this account (aws:SourceAccount defeats the cross-account
# confused-deputy case, where another customer's agent space names this
# role ARN in its own association).
data "aws_iam_policy_document" "assume_role" {
  statement {
    sid     = "DevOpsAgentAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = [var.devops_agent_service_principal]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "agent" {
  name               = local.role_name
  description        = "Read-only role the AWS DevOps Agent assumes to inspect this account for the ${var.environment} support portal; data-plane reads are explicitly denied."
  assume_role_policy = data.aws_iam_policy_document.assume_role.json
}

# The broad read surface: every Describe/List/Get the agent needs to reason
# about configuration, across services the portal cannot enumerate ahead of
# time (an incident can involve anything).
resource "aws_iam_role_policy_attachment" "read_only" {
  role       = aws_iam_role.agent.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/ReadOnlyAccess"
}

# The subtraction: configuration is fair game, stored CONTENT is not.
data "aws_iam_policy_document" "deny_data_plane" {
  # S3 and DynamoDB object/item reads — the two the portal's threat model
  # names explicitly. The agent can still see that a bucket or table
  # exists, its encryption, its policy, and its metrics, which is what
  # diagnosis actually needs.
  statement {
    sid    = "DenyObjectAndItemReads"
    effect = "Deny"
    actions = [
      "dynamodb:BatchGetItem",
      "dynamodb:GetItem",
      "dynamodb:GetRecords",
      "dynamodb:PartiQLSelect",
      "dynamodb:Query",
      "dynamodb:Scan",
      "s3:GetObject",
      "s3:GetObjectAttributes",
      "s3:GetObjectTagging",
      "s3:GetObjectVersion",
      "s3:GetObjectVersionTagging",
    ]
    resources = ["*"]
  }

  # Secret material. ReadOnlyAccess grants ssm:Get*, which would return
  # SecureString *values* — including this portal's own origin-verify
  # secret and the Notifier's VAPID private key. Denying the parameter and
  # secret value APIs, plus kms:Decrypt as the backstop for any other
  # path to plaintext, keeps the agent's view to configuration only.
  statement {
    sid    = "DenySecretMaterialReads"
    effect = "Deny"
    actions = [
      "kms:Decrypt",
      "secretsmanager:GetSecretValue",
      "ssm:GetParameter",
      "ssm:GetParameterHistory",
      "ssm:GetParameters",
      "ssm:GetParametersByPath",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "deny_data_plane" {
  name   = "${local.role_name}-deny-data-plane"
  role   = aws_iam_role.agent.id
  policy = data.aws_iam_policy_document.deny_data_plane.json
}
