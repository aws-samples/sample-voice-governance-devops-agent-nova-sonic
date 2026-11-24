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
# PERMISSION MODEL. The AWS managed AIDevOpsAgentAccessPolicy is the
# documented role policy for AWS DevOps Agent and the only permission set
# AWS has tested with the agent. The agent also applies a permission
# guardrail (a session policy) at assume-role time, so its effective
# permissions are the intersection of this role and that guardrail.
# On top of the managed policy:
#   * a small inline Allow adds two IAM key-age reads the managed policy
#     omits (both are inside the guardrail, which permits ReadOnlyAccess);
#   * an explicit Deny removes data-plane and secret reads, plus
#     support:CreateCase, the one write the managed policy grants that a
#     read-only portal does not need. The managed policy does not grant
#     the data-plane reads today; the Deny keeps that true if AWS widens
#     it, because an explicit Deny always wins.
# AWS DevOps Agent IAM docs:
# https://docs.aws.amazon.com/devopsagent/latest/userguide/aws-devops-agent-security-limiting-agent-access-in-an-aws-account.html
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
data "aws_region" "current" {}

locals {
  role_name = "${var.environment}-devops-agent-readonly"
}

# Trust policy: only the DevOps Agent service may assume the role, and only
# for an agent space in this account and Region. aws:SourceAccount and
# aws:SourceArn together defeat the confused-deputy case, where another
# customer's agent space names this role ARN in its own association. The
# ArnLike agentspace/* form follows the AWS DevOps Agent troubleshooting
# guidance for custom roles.
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

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${data.aws_partition.current.partition}:aidevops:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:agentspace/*"]
    }
  }
}

resource "aws_iam_role" "agent" {
  name               = local.role_name
  description        = "Read-only role the AWS DevOps Agent assumes to inspect this account for the ${var.environment} support portal; AIDevOpsAgentAccessPolicy with data-plane and secret reads explicitly denied."
  assume_role_policy = data.aws_iam_policy_document.assume_role.json
}

# The read surface: the AWS managed policy built for this agent, maintained
# by AWS as the agent gains capabilities.
resource "aws_iam_role_policy_attachment" "agent_access" {
  role       = aws_iam_role.agent.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AIDevOpsAgentAccessPolicy"
}

# Reads the managed policy omits but the portal's documented questions need
# ("which IAM users have access keys older than 90 days?"). Both actions are
# in ReadOnlyAccess, so the agent's permission guardrail allows them.
data "aws_iam_policy_document" "extra_reads" {
  statement {
    sid    = "AllowAccessKeyAgeReads"
    effect = "Allow"
    actions = [
      "iam:GetAccessKeyLastUsed",
      "iam:ListAccessKeys",
    ]
    resources = ["arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:user/*"]
  }
}

resource "aws_iam_role_policy" "extra_reads" {
  name   = "${local.role_name}-extra-reads"
  role   = aws_iam_role.agent.id
  policy = data.aws_iam_policy_document.extra_reads.json
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

  # Secret material: SecureString values (including this portal's own
  # origin-verify secret and the Notifier's VAPID private key) and secret
  # values. kms:Decrypt is the backstop for any other path to plaintext;
  # the guardrail allows it as an opt-in, so it is denied here outright.
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

  # The one write in AIDevOpsAgentAccessPolicy that a read-only portal does
  # not need. cloudtrail:StartQuery stays allowed: it runs a CloudTrail Lake
  # query, changes no resource, and the agent uses it to investigate.
  statement {
    sid       = "DenySupportCaseCreation"
    effect    = "Deny"
    actions   = ["support:CreateCase"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "deny_data_plane" {
  name   = "${local.role_name}-deny-data-plane"
  role   = aws_iam_role.agent.id
  policy = data.aws_iam_policy_document.deny_data_plane.json
}
