# ALB module (Req 10.1, 12.4, 13.1, 13.5, 15.3): the internet-facing
# Application Load Balancer in front of the ECS Fargate voice service.
# The browser's wss:// connection terminates TLS at CloudFront on its
# default-domain certificate; because no custom-domain ACM certificate is
# available for the ALB, CloudFront forwards to the ALB over an HTTP origin
# (Req 12.4 — design: mixed-content constraint). Two controls keep that hop
# closed to the public internet:
#   1. the security group only admits :80 from the CloudFront origin-facing
#      managed prefix list, and
#   2. the listener only forwards requests whose x-origin-verify header
#      carries the secret CloudFront injects; everything else gets a fixed
#      403 — so traffic that did not come through the distribution never
#      reaches a task. Cognito JWT validation inside the Voice_Service
#      protects the payload itself (Req 12.5).
# Access logs go to the externally provided logging bucket, which is
# referenced by name and never created here (Req 13.5).

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

# AWS-managed prefix list of CloudFront origin-facing address ranges: the
# only sources allowed to open connections to the ALB.
data "aws_ec2_managed_prefix_list" "cloudfront_origin_facing" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_security_group" "alb" {
  name        = "${var.environment}-voice-alb"
  description = "Internet-facing voice ALB: HTTP from CloudFront origin-facing ranges only"
  vpc_id      = var.vpc_id

  tags = {
    Name = "${var.environment}-voice-alb"
  }
}

# Inbound :80 restricted to CloudFront's origin-facing ranges — direct
# clients cannot even complete a TCP handshake with the ALB.
resource "aws_vpc_security_group_ingress_rule" "http_from_cloudfront" {
  #checkov:skip=CKV_AWS_260: Port is restricted to cloudfront so its false positive
  security_group_id = aws_security_group.alb.id
  description       = "HTTP from CloudFront origin-facing address ranges"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
  prefix_list_id    = data.aws_ec2_managed_prefix_list.cloudfront_origin_facing.id
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.alb.id
  description       = "All egress (health checks and forwarding to ECS tasks)"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_lb" "this" {
  name               = "${var.environment}-voice-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnet_ids

  # Deletion protection is a non-negotiable hardening control (Req 13.1);
  # it is deliberately hardcoded rather than exposed as a variable.
  enable_deletion_protection = true

  # Long-lived voice WebSocket connections must survive quiet stretches
  # between audio frames without the ALB reaping them.
  idle_timeout = var.idle_timeout

  drop_invalid_header_fields = true

  # Access logs land in the externally provided bucket, referenced by name
  # only — this module never creates a logging bucket (Req 13.5). The
  # bucket owner grants log delivery out-of-band.
  access_logs {
    bucket  = var.access_logging_bucket_name
    prefix  = "${var.environment}-voice-alb"
    enabled = true
  }

  tags = {
    Name = "${var.environment}-voice-alb"
  }
}

# Target group for the Fargate tasks. Target type "ip" because awsvpc-mode
# tasks register their ENI addresses directly.
resource "aws_lb_target_group" "voice" {
  name        = "${var.environment}-voice-tg"
  port        = var.target_port
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  # Matches the ECS stopTimeout drain window (Req 10.5): a deregistering
  # task keeps serving established WebSocket sessions for up to 120 s.
  deregistration_delay = 120

  # Fast thresholds: /healthz flips to 503 when the task is draining or
  # task protection is unconfirmed (Req 10.7), and the ALB must stop
  # routing new sessions to it within ~20 s, not minutes.
  health_check {
    path                = "/healthz"
    protocol            = "HTTP"
    port                = "traffic-port"
    interval            = 10
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 2
    matcher             = "200"
  }

  tags = {
    Name = "${var.environment}-voice-tg"
  }
}

# HTTP :80 is the only listener: no ACM certificate is available for a
# custom ALB domain, so TLS terminates at CloudFront and the origin hop is
# plain HTTP by design (Req 12.4). The default action rejects everything —
# only requests carrying the CloudFront origin-verify secret are forwarded
# by the rule below.
# TLS terminates at CloudFront on its default-domain certificate; no
# custom-domain ACM certificate exists for this ALB, so the origin hop is
# HTTP by design (Req 12.4). Reachability is gated by the CloudFront
# origin-facing prefix list and the x-origin-verify header rule below, so
# this listener has no ssl_policy to harden — the default_action is a fixed
# 403, and only origin-verified requests are forwarded by the rule below.
resource "aws_lb_listener" "http" {
  #checkov:skip=CKV_AWS_2: As it a sample code we are not using HTTPS which require ACM certificated but it is recommended to use HTTPS.
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  # nosemgrep: terraform.aws.security.insecure-load-balancer-tls-version.insecure-load-balancer-tls-version
  protocol = "HTTP"

  default_action {
    type = "fixed-response"

    fixed_response {
      content_type = "text/plain"
      message_body = "Forbidden"
      status_code  = "403"
    }
  }
}

# Forward to the voice target group only when the request carries the
# x-origin-verify header value CloudFront injects, so traffic that bypasses
# the distribution never reaches the service.
resource "aws_lb_listener_rule" "origin_verified" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 1

  condition {
    http_header {
      http_header_name = "x-origin-verify"
      values           = [var.origin_verify_header_value]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.voice.arn
  }
}
