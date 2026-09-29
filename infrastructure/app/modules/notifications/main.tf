# Notifications module (Req 5.1, 5.11, 15.4): the notification plane's
# ingest and fan-out entry point — three EventBridge rules on the default
# bus (CloudWatch Alarm state changes, Incident Manager incidents, DevOps
# Agent findings) targeting the Notifier Lambda, the function itself with
# its scoped IAM role and explicit log group, and the optional SNS
# escalation topic. Environment variable names mirror the handler's
# required-key manifest (backend/notifier/src/handler.py) exactly;
# IAM statements grant exactly the operations the notifier source issues.
# Partition, region, and account always come from data sources or input
# variables — never hardcoded (Req 14.2, 15.5).

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
  function_name  = "${var.environment}-notifier"
  log_group_name = "/aws/lambda/${var.environment}-notifier"

  # The handler reads the VAPID private key from SSM Parameter Store
  # (aioboto3 get_parameter(WithDecryption=True) in
  # backend/notifier/src/handler.py), so the read permission targets the
  # parameter ARN derived from the same name variable that feeds the
  # VAPID_PRIVATE_KEY_SECRET_NAME environment entry — one variable, no
  # drift between the env value and the IAM scope. Parameter ARNs are
  # arn:<partition>:ssm:<region>:<account>:parameter/<name-without-slash>,
  # and the name variable is validated to start with "/".
  vapid_parameter_arn = "arn:${data.aws_partition.current.partition}:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter${var.vapid_private_key_parameter_name}"
}

# ---------------------------------------------------------------------------
# Optional SNS escalation topic (Req 5.11).
# ---------------------------------------------------------------------------

# Created only when escalation is configured; the SNS_ESCALATION_TOPIC_ARN
# environment entry and the role's sns:Publish statement exist exactly when
# this topic does, matching the handler's treatment of the absent key as
# escalation-not-configured. Server-side encryption uses the AWS managed
# SNS key (Req 12.3).
resource "aws_sns_topic" "escalation" {
  count = var.create_escalation_topic ? 1 : 0

  name              = "${var.environment}-incident-escalation"
  kms_master_key_id = "alias/aws/sns"
}

# ---------------------------------------------------------------------------
# Notifier log group (Req 19.4 delivery target, 15.5 retention).
# ---------------------------------------------------------------------------

# Created explicitly (rather than letting Lambda auto-create it on first
# write) so retention and encryption are always configured. CloudWatch Logs
# applies server-side encryption by default; a customer managed key can be
# supplied via var.kms_key_arn (Req 12.3).
resource "aws_cloudwatch_log_group" "notifier" {
  name              = local.log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

# ---------------------------------------------------------------------------
# Notifier execution role: exactly what the handler source touches.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    sid     = "LambdaAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "notifier" {
  name               = "${var.environment}-notifier"
  description        = "Notifier Lambda role: log delivery, AppSync Events publish, push-subscription fan-out reads and cleanup, VAPID key read, optional SNS escalation"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

data "aws_iam_policy_document" "notifier_permissions" {
  # Log delivery scoped to this function's explicit log group only — no
  # logs:CreateLogGroup, since the group is managed by Terraform above.
  statement {
    sid    = "LogDelivery"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.notifier.arn}:*"]
  }

  # Incident_Notification broadcast (Req 5.1): the handler publishes with
  # SigV4 POST /event (backend/notifier/src/channels/appsync_publisher.py)
  # to channel /incidents/all. The IAM action for publishing events to an
  # AppSync Events API is appsync:EventPublish, scoped to the incidents
  # channel namespace ARN (appsync_events module output) so the role can
  # publish on /incidents/* and nothing else.
  statement {
    sid       = "AppSyncEventPublish"
    effect    = "Allow"
    actions   = ["appsync:EventPublish"]
    resources = [var.channel_namespace_arn]
  }

  # Web Push fan-out (Req 6.2, 6.5): exactly the two operations the
  # subscription repository issues (backend/notifier/src/
  # subscription_repo.py) — the full-table Scan before a fan-out and the
  # unconditional DeleteItem cleanup after a push-service 404/410. The
  # table has no GSI the notifier reads, so no /index/* resource.
  statement {
    sid    = "PushSubscriptionFanOut"
    effect = "Allow"
    actions = [
      "dynamodb:Scan",
      "dynamodb:DeleteItem",
    ]
    resources = [var.subscriptions_table_arn]
  }

  # VAPID private key read at cold start (Req 14.1): the handler fetches
  # the SecureString from SSM Parameter Store, so the permission is
  # ssm:GetParameter on the one parameter — not
  # secretsmanager:GetSecretValue (verified against
  # backend/notifier/src/handler.py's _read_ssm_parameter). Decryption
  # with the AWS managed aws/ssm key is authorized through the key's
  # service policy; a customer managed parameter key would additionally
  # need kms:Decrypt.
  statement {
    sid       = "VapidPrivateKeyRead"
    effect    = "Allow"
    actions   = ["ssm:GetParameter"]
    resources = [local.vapid_parameter_arn]
  }

  # SNS escalation publish (Req 5.11), present exactly when the topic is
  # created — an unconfigured deployment carries no sns:* grant at all.
  dynamic "statement" {
    for_each = var.create_escalation_topic ? [1] : []

    content {
      sid       = "EscalationPublish"
      effect    = "Allow"
      actions   = ["sns:Publish"]
      resources = [aws_sns_topic.escalation[0].arn]
    }
  }
}

resource "aws_iam_role_policy" "notifier" {
  name   = "${var.environment}-notifier"
  role   = aws_iam_role.notifier.id
  policy = data.aws_iam_policy_document.notifier_permissions.json
}

# ---------------------------------------------------------------------------
# Notifier Lambda function (Req 5.1).
# ---------------------------------------------------------------------------

resource "aws_lambda_function" "notifier" {
  function_name = local.function_name
  description   = "Fans one EventBridge incident event out to AppSync Events, Web Push, and optional SNS escalation (Req 5.1)"
  role          = aws_iam_role.notifier.arn

  # Handler module path: the deployment zip carries the notifier package
  # root (backend/notifier contents), so src/handler.py resolves as module
  # src.handler with entrypoint function handler — verified against
  # backend/notifier/src/handler.py (`def handler(event, context)`), whose
  # own imports (`from src...`, `from shared...`) require exactly this
  # zip-root layout.
  handler = "src.handler.handler"
  runtime = "python3.14"

  # The zip is built by the IaC pipeline buildspec from backend/notifier
  # before terraform plan/apply runs, so the path always exists when this
  # expression is evaluated; the hash redeploys the function whenever the
  # packaged code changes.
  filename         = var.lambda_zip_path
  source_code_hash = filebase64sha256(var.lambda_zip_path)

  timeout       = var.lambda_timeout_seconds
  memory_size   = var.lambda_memory_mb
  architectures = var.lambda_architectures

  # Active X-Ray tracing (Req 12.3): end-to-end traces across the fan-out
  # to AppSync Events, Web Push, and optional SNS make latency and failure
  # attribution possible without instrumenting the handler by hand. The
  # execution role needs no extra grant — the Lambda service attaches the
  # X-Ray daemon permissions when tracing is Active.
  tracing_config {
    mode = "Active"
  }

  # nosemgrep: terraform.aws.security.aws-lambda-environment-unencrypted.aws-lambda-environment-unencrypted
  # Environment variables are encrypted at rest with the AWS-managed Lambda
  # key by default (Req 12.3). No key material is stored here — the VAPID
  # private key lives in SSM and only its parameter *name* is passed
  # (VAPID_PRIVATE_KEY_SECRET_NAME), so a customer-managed KMS key is not
  # warranted for these non-secret configuration values.
  environment {
    # Exactly the handler's environment manifest
    # (backend/notifier/src/handler.py _REQUIRED_ENV_KEYS plus the
    # optional escalation key). AWS_REGION is also required by the
    # handler but is a reserved key the Lambda runtime sets itself —
    # never set it here. VAPID_PRIVATE_KEY_SECRET_NAME carries the SSM
    # parameter *name*, never key material (Req 14.1).
    variables = merge(
      {
        APPSYNC_EVENTS_HTTP_ENDPOINT  = var.appsync_events_http_endpoint
        SUBSCRIPTIONS_TABLE_NAME      = var.subscriptions_table_name
        VAPID_SUBJECT                 = var.vapid_subject
        VAPID_PRIVATE_KEY_SECRET_NAME = var.vapid_private_key_parameter_name
      },
      # Present exactly when the escalation topic exists (Req 5.11); the
      # handler treats the absent key as escalation-not-configured.
      var.create_escalation_topic ? { SNS_ESCALATION_TOPIC_ARN = aws_sns_topic.escalation[0].arn } : {},
    )
  }

  # The log group and the role policy must exist before the first
  # invocation so early logs land in the retention-managed group and the
  # first fan-out already holds its permissions.
  depends_on = [
    aws_cloudwatch_log_group.notifier,
    aws_iam_role_policy.notifier,
  ]
}

# ---------------------------------------------------------------------------
# EventBridge rules on the default bus (Req 5.1): the three incident
# sources, each with a Lambda target and an invoke permission scoped to
# that one rule.
# ---------------------------------------------------------------------------

# (1) CloudWatch Alarm state changes: the natively documented pattern for
# alarm events, narrowed to transitions INTO the ALARM state so OK and
# INSUFFICIENT_DATA transitions never page anyone.
resource "aws_cloudwatch_event_rule" "cloudwatch_alarms" {
  name        = "${var.environment}-notify-cloudwatch-alarms"
  description = "Routes CloudWatch Alarm transitions into ALARM state to the Notifier (Req 5.1)"

  event_pattern = jsonencode({
    source        = ["aws.cloudwatch"]
    "detail-type" = ["CloudWatch Alarm State Change"]
    detail = {
      state = {
        value = ["ALARM"]
      }
    }
  })
}

resource "aws_cloudwatch_event_target" "cloudwatch_alarms" {
  rule      = aws_cloudwatch_event_rule.cloudwatch_alarms.name
  target_id = "notifier-lambda"
  arn       = aws_lambda_function.notifier.arn
}

resource "aws_lambda_permission" "cloudwatch_alarms" {
  statement_id  = "AllowEventBridgeCloudWatchAlarms"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.notifier.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.cloudwatch_alarms.arn
}

# (2) Incident Manager incidents: per the EventBridge service reference
# (events-ref-ssm-incidents), Incident Manager delivers its events to
# EventBridge via AWS CloudTrail, so the documented pattern is detail-type
# "AWS API Call via CloudTrail" narrowed by detail.eventSource. The
# eventName narrows further to StartIncident — the API call that opens
# every incident (manual, CloudWatch-alarm, or EventBridge-created) — so
# the Notifier fires once per new incident rather than on every Incident
# Manager write. The normalizer classifies by top-level source
# ("aws.ssm-incidents" → incident-manager) and degrades missing fields
# safely, so the CloudTrail detail shape is handled (Req 5.5).
resource "aws_cloudwatch_event_rule" "incident_manager" {
  name        = "${var.environment}-notify-incident-manager"
  description = "Routes Incident Manager incident-opening events (via CloudTrail) to the Notifier (Req 5.1)"

  event_pattern = jsonencode({
    source        = ["aws.ssm-incidents"]
    "detail-type" = ["AWS API Call via CloudTrail"]
    detail = {
      eventSource = ["ssm-incidents.amazonaws.com"]
      eventName   = ["StartIncident"]
    }
  })
}

resource "aws_cloudwatch_event_target" "incident_manager" {
  rule      = aws_cloudwatch_event_rule.incident_manager.name
  target_id = "notifier-lambda"
  arn       = aws_lambda_function.notifier.arn
}

resource "aws_lambda_permission" "incident_manager" {
  statement_id  = "AllowEventBridgeIncidentManager"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.notifier.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.incident_manager.arn
}

# (3) DevOps Agent findings: the finding event source is defined by the
# DevOps Agent service and carries no publicly documented detail-type, so
# the rule matches on source alone and the source string is configurable
# (var.devops_agent_event_source, default "aws.aidevops" — the service's
# IAM/action namespace). The normalizer's catch-all maps this shape to
# devops-agent-finding with lenient field extraction (Req 5.5, 5.6).
resource "aws_cloudwatch_event_rule" "devops_agent_findings" {
  name        = "${var.environment}-notify-devops-agent-findings"
  description = "Routes DevOps Agent finding events to the Notifier (Req 5.1)"

  event_pattern = jsonencode({
    source = [var.devops_agent_event_source]
  })
}

resource "aws_cloudwatch_event_target" "devops_agent_findings" {
  rule      = aws_cloudwatch_event_rule.devops_agent_findings.name
  target_id = "notifier-lambda"
  arn       = aws_lambda_function.notifier.arn
}

resource "aws_lambda_permission" "devops_agent_findings" {
  statement_id  = "AllowEventBridgeDevOpsAgentFindings"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.notifier.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.devops_agent_findings.arn
}
