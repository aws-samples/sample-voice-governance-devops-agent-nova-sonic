# IAM module (Req 15.3): the ECS task role and task execution role for the
# voice service. The task role is what the application code runs as — every
# statement is scoped to the narrowest resource the backend adapters
# actually touch (Bedrock Nova Sonic streaming, ApplyGuardrail on the one
# guardrail, the four Session_Store tables, task protection on this
# cluster's tasks, and the environment's configuration prefixes). The
# execution role is what the ECS agent uses before the container starts:
# image pull, log delivery, and startup secret injection. Partition,
# region, and account always come from data sources or input variables —
# never hardcoded (Req 14.2, 15.5).

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

data "aws_partition" "current" {}

data "aws_region" "current" {}

data "aws_caller_identity" "current" {}

locals {
  # Foundation-model ARNs carry no account id. The model region defaults to
  # the deployment region but is overridable because the design pins Nova
  # Sonic streaming to us-east-1 (design: Bedrock adapter) — set
  # bedrock_model_region when the stack itself deploys elsewhere.
  bedrock_model_region = coalesce(var.bedrock_model_region, data.aws_region.current.region)

  # Explicit ARN list wins when provided; otherwise the default pattern
  # arn:<partition>:bedrock:<region>::foundation-model/* narrowed by the
  # model-id variable.
  nova_sonic_model_arns = coalesce(
    var.nova_sonic_model_arns,
    ["arn:${data.aws_partition.current.partition}:bedrock:${local.bedrock_model_region}::foundation-model/${var.nova_sonic_model_id}"],
  )

  # UpdateTaskProtection acts on task ARNs (arn:...:task/{cluster}/{id});
  # derive the pattern for this cluster from its ARN.
  cluster_task_arn_pattern = "${replace(var.cluster_arn, ":cluster/", ":task/")}/*"

  # Startup and runtime configuration prefixes (Req 14.1): SSM parameters
  # under the environment's path, Secrets Manager secrets under the
  # environment's ARN prefix (secret ARNs end in a random suffix, so the
  # trailing wildcard is required even for a single secret).
  ssm_parameter_arn_prefix = "arn:${data.aws_partition.current.partition}:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter${var.ssm_parameter_path_prefix}/*"
  secret_arn_pattern       = "${var.secretsmanager_secret_arn_prefix}*"
}

data "aws_iam_policy_document" "ecs_tasks_assume_role" {
  statement {
    sid     = "EcsTasksAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# ---------------------------------------------------------------------------
# Task role: the identity of the running voice-service application.
# ---------------------------------------------------------------------------

resource "aws_iam_role" "task" {
  name               = "${var.environment}-voice-task"
  description        = "Voice service task role: Bedrock streaming, guardrail evaluation, DevOps Agent chat, Session_Store, task protection"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json
}

data "aws_iam_policy_document" "task_permissions" {
  # Bidirectional audio streaming to Nova Sonic (design: bedrock_stream
  # adapter), scoped to the configured foundation-model ARN(s).
  #
  # BOTH actions are required. InvokeModelWithBidirectionalStream alone yields
  # an AccessDeniedException (with an empty message) when the stream's initial
  # response is awaited; Bedrock also authorizes bedrock:InvokeModel for this
  # operation. Verified empirically against amazon.nova-2-sonic-v1:0 by
  # assuming a role with each policy in turn:
  #   InvokeModelWithBidirectionalStream only          -> AccessDeniedException
  #   InvokeModelWithBidirectionalStream + InvokeModel -> stream opens
  # Note that iam simulate-principal-policy reports "allowed" for the
  # single-action policy, so simulation does not catch this.
  statement {
    sid    = "NovaSonicBidirectionalStream"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModelWithBidirectionalStream",
      "bedrock:InvokeModel",
    ]
    resources = local.nova_sonic_model_arns
  }

  # Fail-closed guardrail gate (Req 4.1), scoped to the one guardrail the
  # bedrock_guardrail module created.
  statement {
    sid       = "ApplyGuardrail"
    effect    = "Allow"
    actions   = ["bedrock:ApplyGuardrail"]
    resources = [var.guardrail_arn]
  }

  # DevOps Agent chat (Req 3.2, 3.3). Resource is "*" because the DevOps
  # Agent service does not expose resource-scoped ARNs for its aidevops
  # actions — the action list itself is the narrowing.
  statement {
    sid    = "DevOpsAgentChat"
    effect = "Allow"
    actions = [
      "aidevops:CreateChat",
      "aidevops:SendMessage",
    ]
    resources = ["*"]
  }

  # Session_Store access (Req 8.1, 8.5): exactly the item operations the
  # dynamodb_store adapter issues — conditional single-item writes,
  # TransactWriteItems (with its condition checks), batch cleanup, and
  # Query over the tables and their indexes (by-engineer GSI reconnect
  # lookups).
  statement {
    sid    = "SessionStoreTables"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
      "dynamodb:TransactWriteItems",
      "dynamodb:BatchWriteItem",
      "dynamodb:ConditionCheckItem",
    ]
    resources = concat(
      var.dynamodb_table_arns,
      [for arn in var.dynamodb_table_arns : "${arn}/index/*"],
    )
  }

  # Scale-in protection while hosting live sessions (Req 10.3, 10.4),
  # scoped to this cluster's tasks via the task-ARN pattern plus the
  # ecs:cluster condition.
  statement {
    sid       = "TaskProtection"
    effect    = "Allow"
    actions   = ["ecs:UpdateTaskProtection"]
    resources = [local.cluster_task_arn_pattern]

    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [var.cluster_arn]
    }
  }

  # Runtime configuration reads at startup (Req 14.1, 14.5), scoped to the
  # environment's SSM path prefix.
  statement {
    sid       = "RuntimeParameterRead"
    effect    = "Allow"
    actions   = ["ssm:GetParameter"]
    resources = [local.ssm_parameter_arn_prefix]
  }

  # Runtime secret reads at startup (Req 14.1, 14.5), scoped to the
  # environment's Secrets Manager ARN prefix.
  statement {
    sid       = "RuntimeSecretRead"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [local.secret_arn_pattern]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "${var.environment}-voice-task"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_permissions.json
}

# ---------------------------------------------------------------------------
# Execution role: what the ECS agent uses before the container starts.
# ---------------------------------------------------------------------------

resource "aws_iam_role" "execution" {
  name               = "${var.environment}-voice-execution"
  description        = "Voice service execution role: ECR pull, CloudWatch Logs delivery, startup secret injection"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json
}

# ECR image pull and awslogs delivery via the AWS managed execution policy.
resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Startup injection of the task definition's secrets (valueFrom): the ECS
# agent resolves SSM parameters with GetParameters (plural) and Secrets
# Manager values with GetSecretValue, scoped to the same environment
# prefixes as the task role.
data "aws_iam_policy_document" "execution_secrets" {
  statement {
    sid       = "StartupParameterInjection"
    effect    = "Allow"
    actions   = ["ssm:GetParameters"]
    resources = [local.ssm_parameter_arn_prefix]
  }

  statement {
    sid       = "StartupSecretInjection"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [local.secret_arn_pattern]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "${var.environment}-voice-execution-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}
