# ECS service module (Req 10.1, 10.2, 10.5, 10.6, 15.3, 19.5): the Fargate
# cluster, task definition, service, and autoscaling for the Voice_Service.
# The service keeps a minimum of 2 tasks (Req 10.1) in private subnets that
# span ≥2 Availability Zones — with no placement constraints, the Fargate
# scheduler spreads tasks across the subnets' AZs naturally, so the AZ
# spread follows from the network module's ≥2-AZ private subnets.
# Autoscaling uses step policies on the ALB ActiveConnectionCount metric:
# the scale-out alarm evaluates a single 60-second period so capacity is
# added well inside the 5-minute bound of Req 10.2, while scale-in is
# conservative (long evaluation, single-task steps, Req 19.5) — ECS task
# scale-in protection, acquired by the tasks themselves while they host
# live sessions, prevents draining live calls (Req 10.6, 19.6, 19.7).

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

data "aws_region" "current" {}

locals {
  container_name = "voice-service"
  service_name   = "${var.environment}-voice-service"
}

resource "aws_ecs_cluster" "this" {
  name = "${var.environment}-voice"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

# Service log group. CloudWatch Logs applies server-side encryption by
# default; a customer managed key can be supplied via var.kms_key_arn
# (Req 12.3). Retention is environment-tunable (Req 15.5).
resource "aws_cloudwatch_log_group" "service" {
  name              = "/ecs/${local.service_name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

# Task security group: only the ALB may reach the container port; all
# egress is open because the tasks call Bedrock, the DevOps Agent,
# DynamoDB, AppSync, SSM, and Secrets Manager over TLS through NAT.
resource "aws_security_group" "tasks" {
  name        = "${local.service_name}-tasks"
  description = "Voice service Fargate tasks: container port from the ALB only"
  vpc_id      = var.vpc_id

  tags = {
    Name = "${local.service_name}-tasks"
  }
}

resource "aws_vpc_security_group_ingress_rule" "from_alb" {
  security_group_id            = aws_security_group.tasks.id
  description                  = "Container port from the voice ALB"
  from_port                    = var.container_port
  to_port                      = var.container_port
  ip_protocol                  = "tcp"
  referenced_security_group_id = var.alb_security_group_id
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.tasks.id
  description       = "All egress (AWS service calls over TLS via NAT)"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_ecs_task_definition" "voice" {
  family                   = local.service_name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.task_cpu)
  memory                   = tostring(var.task_memory)
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.task_role_arn

  container_definitions = jsonencode([
    {
      name      = local.container_name
      image     = var.container_image
      essential = true

      portMappings = [
        {
          containerPort = var.container_port
          protocol      = "tcp"
        }
      ]

      # Drain contract (Req 10.5, design drain_manager): SIGTERM starts the
      # drain — the task rejects new WebSocket upgrades and keeps serving
      # existing sessions for up to these 120 seconds before SIGKILL;
      # sessions still live at expiry get a session.terminating frame.
      stopTimeout = 120

      # Non-sensitive configuration only (Req 14.1); sensitive values are
      # injected below via secrets valueFrom, never as plain environment.
      environment = [
        for name, value in var.environment_variables : {
          name  = name
          value = value
        }
      ]

      # valueFrom points at SSM parameter or Secrets Manager ARNs; the
      # execution role reads them at container startup (Req 14.1).
      secrets = [
        for name, value_from in var.secrets : {
          name      = name
          valueFrom = value_from
        }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.service.name
          "awslogs-region"        = data.aws_region.current.region
          "awslogs-stream-prefix" = local.container_name
        }
      }
    }
  ])
}

resource "aws_ecs_service" "voice" {
  name            = local.service_name
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.voice.arn
  launch_type     = "FARGATE"

  # Minimum 2 tasks at all times (Req 10.1); the autoscaling target below
  # uses the same value as its floor so scale-in never goes under it.
  desired_count = var.desired_count

  # Private subnets across ≥2 AZs (network module): the Fargate scheduler
  # spreads tasks over the subnets' AZs, satisfying the ≥2-AZ distribution
  # of Req 10.1 without explicit placement configuration.
  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = var.target_group_arn
    container_name   = local.container_name
    container_port   = var.container_port
  }

  # Failed deployments stop and roll back automatically instead of cycling
  # broken tasks behind the ALB.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  enable_execute_command = var.enable_execute_command

  # Application Auto Scaling owns the task count after creation; without
  # this, every apply would reset the service to desired_count and undo
  # scale-out.
  lifecycle {
    ignore_changes = [desired_count]
  }
}

resource "aws_appautoscaling_target" "voice" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.voice.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.desired_count
  max_capacity       = var.max_capacity
}

# Scale-out: add tasks as soon as the high alarm fires so capacity arrives
# well within 5 minutes of the threshold breach (Req 10.2).
resource "aws_appautoscaling_policy" "scale_out" {
  name               = "${local.service_name}-scale-out"
  policy_type        = "StepScaling"
  service_namespace  = aws_appautoscaling_target.voice.service_namespace
  resource_id        = aws_appautoscaling_target.voice.resource_id
  scalable_dimension = aws_appautoscaling_target.voice.scalable_dimension

  step_scaling_policy_configuration {
    adjustment_type         = "ChangeInCapacity"
    metric_aggregation_type = "Average"
    cooldown                = 60

    step_adjustment {
      metric_interval_lower_bound = 0
      scaling_adjustment          = var.scale_out_step_size
    }
  }
}

# Scale-in: conservative single-task steps with a long cooldown (Req 19.5).
# Tasks hosting live Voice_Sessions carry ECS scale-in protection, so a
# scale-in action never drains a live call (Req 10.6, 19.6), and the
# autoscaling target's min_capacity keeps the count at or above the
# 2-task floor.
resource "aws_appautoscaling_policy" "scale_in" {
  name               = "${local.service_name}-scale-in"
  policy_type        = "StepScaling"
  service_namespace  = aws_appautoscaling_target.voice.service_namespace
  resource_id        = aws_appautoscaling_target.voice.resource_id
  scalable_dimension = aws_appautoscaling_target.voice.scalable_dimension

  step_scaling_policy_configuration {
    adjustment_type         = "ChangeInCapacity"
    metric_aggregation_type = "Average"
    cooldown                = 300

    step_adjustment {
      metric_interval_upper_bound = 0
      scaling_adjustment          = -1
    }
  }
}

# High alarm: a single 60-second period above the scale-out threshold
# triggers the step policy, keeping reaction time far inside the 5-minute
# bound of Req 10.2. ActiveConnectionCount is a load-balancer-scoped
# metric, so its only dimension is the ALB ARN suffix.
resource "aws_cloudwatch_metric_alarm" "active_connections_high" {
  alarm_name          = "${local.service_name}-active-connections-high"
  alarm_description   = "ALB active connections above the scale-out threshold: add voice tasks (Req 10.2)"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "ActiveConnectionCount"
  statistic           = "Average"
  period              = 60
  evaluation_periods  = 1
  threshold           = var.scale_out_threshold
  comparison_operator = "GreaterThanThreshold"

  dimensions = {
    LoadBalancer = var.alb_arn_suffix
  }

  alarm_actions = [aws_appautoscaling_policy.scale_out.arn]
}

# Low alarm: the connection count must stay below the scale-in threshold
# for the whole (long) evaluation window before a single task is removed
# (Req 19.5) — transient lulls between incident calls never shrink the
# fleet.
resource "aws_cloudwatch_metric_alarm" "active_connections_low" {
  alarm_name          = "${local.service_name}-active-connections-low"
  alarm_description   = "ALB active connections below the scale-in threshold for the full evaluation window: remove one voice task (Req 19.5)"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "ActiveConnectionCount"
  statistic           = "Average"
  period              = 60
  evaluation_periods  = var.scale_in_evaluation_periods
  threshold           = var.scale_in_threshold
  comparison_operator = "LessThanThreshold"

  dimensions = {
    LoadBalancer = var.alb_arn_suffix
  }

  alarm_actions = [aws_appautoscaling_policy.scale_in.arn]
}
