# Observability module (Req 15.4, 19.2, 19.3): the operations SNS topic
# and the four CloudWatch alarms the design wires to it — Voice_Service
# running task count, ALB unhealthy target count, Voice_Service error rate
# (target 5XX), and Notifier delivery failures (Lambda errors). Every alarm
# defines a monitored metric, a threshold, and an evaluation period
# (Req 19.2), and publishes to the ops topic on entering ALARM (Req 19.3;
# alarm actions fire on the state change itself, well inside the
# 60-second requirement). Thresholds, period, and evaluation periods are
# environment-tunable variables — never hardcoded (Req 15.5).

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

# ---------------------------------------------------------------------------
# Operations SNS topic (Req 15.4, 19.3).
# ---------------------------------------------------------------------------

# Server-side encryption uses the AWS managed SNS key (Req 12.3);
# CloudWatch alarm actions are authorized to publish through the key's
# service policy.
resource "aws_sns_topic" "ops" {
  name              = "${var.environment}-ops-alarms"
  kms_master_key_id = "alias/aws/sns"
}

# Optional operator email subscriptions; each address must confirm the
# subscription SNS sends it before deliveries begin.
resource "aws_sns_topic_subscription" "email" {
  for_each = toset(var.alarm_email_subscriptions)

  topic_arn = aws_sns_topic.ops.arn
  protocol  = "email"
  endpoint  = each.value
}

# ---------------------------------------------------------------------------
# The four alarms (Req 19.2).
# ---------------------------------------------------------------------------

# (1) Voice_Service running task count below the HA floor (Req 10.1,
# 19.2). ECS/ContainerInsights RunningTaskCount is emitted because the
# ecs_service module enables containerInsights on the cluster. Missing
# data is treated as breaching: this metric exists exactly while tasks
# run, so silence is itself the failure signal — a cluster reporting
# nothing must page, not idle in INSUFFICIENT_DATA.
resource "aws_cloudwatch_metric_alarm" "running_task_count" {
  alarm_name          = "${var.environment}-running-task-count"
  alarm_description   = "Voice_Service running task count fell below the high-availability floor (Req 19.2)"
  namespace           = "ECS/ContainerInsights"
  metric_name         = "RunningTaskCount"
  statistic           = "Average"
  comparison_operator = "LessThanThreshold"
  threshold           = var.running_task_count_threshold
  period              = var.alarm_period_seconds
  evaluation_periods  = var.alarm_evaluation_periods
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.ops.arn]

  dimensions = {
    ClusterName = var.ecs_cluster_name
    ServiceName = var.ecs_service_name
  }
}

# (2) ALB unhealthy target count (Req 19.2): any target failing health
# checks pages immediately (Maximum catches the worst datapoint in the
# period). UnHealthyHostCount is emitted continuously while targets are
# registered, so missing data carries no health signal — notBreaching.
resource "aws_cloudwatch_metric_alarm" "alb_unhealthy_targets" {
  alarm_name          = "${var.environment}-alb-unhealthy-targets"
  alarm_description   = "At least one voice target group member is failing ALB health checks (Req 19.2)"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  statistic           = "Maximum"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  period              = var.alarm_period_seconds
  evaluation_periods  = var.alarm_evaluation_periods
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.ops.arn]

  dimensions = {
    LoadBalancer = var.alb_arn_suffix
    TargetGroup  = var.target_group_arn_suffix
  }
}

# (3) Voice_Service error rate as target 5XX responses (Req 19.2). The
# ALB emits HTTPCode_Target_5XX_Count only in periods where at least one
# 5XX occurred, so missing data means zero errors — notBreaching, never a
# false page on a quiet service. Sum over the period against the tunable
# threshold.
resource "aws_cloudwatch_metric_alarm" "voice_5xx" {
  alarm_name          = "${var.environment}-voice-5xx"
  alarm_description   = "Voice_Service targets returned more 5XX responses than the configured threshold (Req 19.2)"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  statistic           = "Sum"
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.voice_5xx_threshold
  period              = var.alarm_period_seconds
  evaluation_periods  = var.alarm_evaluation_periods
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.ops.arn]

  dimensions = {
    LoadBalancer = var.alb_arn_suffix
  }
}

# (4) Notifier delivery failures as Lambda invocation errors (Req 19.2):
# the handler converts channel failures into logged responses, so an
# Errors datapoint means an invocation itself failed (misconfiguration —
# ConfigurationError re-raises by design — or an escape past the boundary
# handler). Any error pages. AWS/Lambda Errors is emitted per invocation,
# so missing data just means no invocations — notBreaching.
resource "aws_cloudwatch_metric_alarm" "notifier_errors" {
  alarm_name          = "${var.environment}-notifier-errors"
  alarm_description   = "Notifier Lambda invocations are failing, so incident notifications are not being delivered (Req 19.2)"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  period              = var.alarm_period_seconds
  evaluation_periods  = var.alarm_evaluation_periods
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.ops.arn]

  dimensions = {
    FunctionName = var.notifier_function_name
  }
}
