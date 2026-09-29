# infrastructure/app — layer 2 root (Req 13.4, 13.6, 14.3, 15.5, 15.6, 15.7).
#
# Wires together all runtime infrastructure: network, ALB, WAF (both
# scopes), CloudFront + frontend S3, Cognito, DynamoDB, AppSync Events,
# Bedrock Guardrail, IAM, the ECS voice service, the notification plane,
# and observability. Per the layer conventions this file only instantiates
# modules — resource definitions live inside modules/ — with one deliberate
# exception, the origin-verify shared secret (see below), which is
# cross-module glue that belongs to no single module.
#
# Applied exclusively by the IaC_Pipeline; the operator never runs
# terraform against this layer directly (design: CI/CD and Two-Layer
# Terraform).

terraform {
  required_version = ">= 1.9"

  # Remote state in the bootstrap-created backend (Req 15.6): this block is
  # intentionally an EMPTY partial configuration because backend blocks
  # cannot reference variables or locals. The IaC pipeline buildspec
  # supplies bucket / key / region / dynamodb_table at
  # `terraform init -backend-config=...` time from the bootstrap layer's
  # outputs (exported to the build as TF_STATE_BUCKET / TF_LOCK_TABLE).
  # The bootstrap layer keeps its own separate local state; the two states
  # are never mixed (Req 15.6).
  backend "s3" {}

  required_providers {
    # Same AWS provider series as every app-layer module
    # (>= 6.9.0 for AppSync Events / Bedrock guardrail resource support,
    # < 7.0.0 to avoid unvetted major-version breaking changes).
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.9.0, < 7.0.0"
    }

    # Generates the origin-verify shared secret below.
    random = {
      source  = "hashicorp/random"
      version = ">= 3.6.0, < 4.0.0"
    }
  }
}

provider "aws" {
  # The design pins the stack to us-east-1 (Nova Sonic bidirectional
  # streaming and the CLOUDFRONT-scope WAF web ACL both live there); the
  # region is still an input variable, never hardcoded in resources
  # (Req 15.5).
  region = var.aws_region

  # var.tags reaches every resource via provider default_tags, so module
  # tag inputs stay at their defaults — modules only add per-resource
  # Name tags on top.
  default_tags {
    tags = var.tags
  }
}

# Account identity and partition, used only to derive the default
# Secrets Manager ARN prefix for IAM scoping when the operator does not
# supply one — account identifiers are never hardcoded (Req 14.2, 15.5).
data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  # SSM Parameter Store path prefix under which this environment's
  # voice-service configuration parameters live; the iam module scopes the
  # task role's ssm:GetParameter grant to it, and the origin-verify
  # parameter below is created under it.
  ssm_parameter_path_prefix = coalesce(
    var.ssm_parameter_path_prefix,
    "/${var.environment}/voice-service"
  )

  # Secrets Manager ARN prefix scoping the task role's secret reads; the
  # derived default covers secrets named "<environment>/..." in this
  # account and region (the iam module appends the trailing wildcard).
  secretsmanager_secret_arn_prefix = coalesce(
    var.secretsmanager_secret_arn_prefix,
    "arn:${data.aws_partition.current.partition}:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:${var.environment}/"
  )

  # The SPA's OAuth redirect/logout URI is the current page URI on the
  # CloudFront default domain (frontend/src/auth/cognito.js
  # currentPageUri()): "/" when served via the default root object, or
  # "/index.html" when addressed explicitly. Cognito requires an exact
  # match, so both spellings are registered.
  portal_urls = [
    "https://${module.cloudfront_s3.distribution_domain_name}/",
    "https://${module.cloudfront_s3.distribution_domain_name}/index.html",
  ]
}

# ---------------------------------------------------------------------------
# Origin-verify shared secret (design research finding 7) — the single
# root-level resource exception to "roots only wire modules". The secret is
# pure cross-module glue: CloudFront injects it into the x-origin-verify
# header on every ALB origin request (cloudfront_s3 module) and the ALB
# listener rule forwards only requests carrying it (alb module), so
# direct-to-ALB traffic is rejected (Req 12.2). Generating it here means no
# operator-managed secret exists to provision or leak; rotation is
# `terraform taint random_password.origin_verify` + apply, which updates
# CloudFront, the ALB rule, and the SSM parameter together.
# ---------------------------------------------------------------------------

resource "random_password" "origin_verify" {
  length = 32
  # Alphanumeric only: the value travels as an HTTP header and an ALB
  # listener-rule match, where special characters invite quoting trouble.
  special = false
}

# The Voice_Service's startup manifest (backend/voice_service/app/config.py)
# requires ORIGIN_VERIFY_SECRET_NAME — the *name* of the SSM SecureString it
# resolves at startup (Req 14.1, 14.5). This parameter is part of the same
# origin-verify glue as random_password above: it is how the generated
# value reaches the service, and no single module owns it. Stored under the
# task role's SSM read prefix (iam module) as a SecureString (encrypted with
# the AWS managed aws/ssm key unless var.kms_key_arn overrides — Req 12.3).
resource "aws_ssm_parameter" "origin_verify" {
  name        = "${local.ssm_parameter_path_prefix}/origin-verify"
  description = "Origin-verification shared secret injected by CloudFront and required by the ALB listener rule; resolved by the Voice_Service at startup."
  type        = "SecureString"
  key_id      = var.kms_key_arn
  value       = random_password.origin_verify.result
}

# ---------------------------------------------------------------------------
# Voice plane: VPC -> ALB -> ECS Fargate, fronted by CloudFront + WAF.
# ---------------------------------------------------------------------------

module "network" {
  source = "./modules/network"

  environment        = var.environment
  vpc_cidr           = var.vpc_cidr
  az_count           = var.az_count
  single_nat_gateway = var.single_nat_gateway
}

module "alb" {
  source = "./modules/alb"

  environment                = var.environment
  vpc_id                     = module.network.vpc_id
  public_subnet_ids          = module.network.public_subnet_ids
  access_logging_bucket_name = var.access_logging_bucket_name
  origin_verify_header_value = random_password.origin_verify.result
  target_port                = var.container_port

  # An internet-facing ALB cannot be created until the VPC's internet
  # gateway is attached; the subnet-id references above do not express
  # that edge (the subnets exist before the IGW attachment completes), so
  # a first apply raced and failed without this module-level dependency.
  depends_on = [module.network]
}

# Two web ACLs, one per scope (Req 11.1): the CLOUDFRONT-scope ACL attaches
# to the distribution via web_acl_arn (and must live in us-east-1, which
# the provider region is pinned to); the REGIONAL-scope ACL associates with
# the ALB inside the module.
module "waf_cloudfront" {
  source = "./modules/waf"

  environment        = var.environment
  scope              = "CLOUDFRONT"
  log_retention_days = var.waf_log_retention_days
  kms_key_arn        = var.kms_key_arn
}

module "waf_regional" {
  source = "./modules/waf"

  environment        = var.environment
  scope              = "REGIONAL"
  alb_arn            = module.alb.alb_arn
  log_retention_days = var.waf_log_retention_days
  kms_key_arn        = var.kms_key_arn
}

# Frontend bucket + dual-origin distribution (S3 default, ALB for /ws/* and
# /api/*). The reusable s3_policies module is NOT instantiated here: the
# cloudfront_s3 module already composes it into the frontend bucket policy
# (OAC-only read + TLS denies, Req 13.2, 13.3, 13.7), and no other app-layer
# bucket exists — the access-logging bucket is pre-existing and referenced
# by name only, never created by this layer (Req 13.5).
module "cloudfront_s3" {
  source = "./modules/cloudfront_s3"

  environment          = var.environment
  alb_dns_name         = module.alb.alb_dns_name
  origin_verify_secret = random_password.origin_verify.result
  web_acl_arn          = module.waf_cloudfront.web_acl_arn
  price_class          = var.price_class
  kms_key_arn          = var.kms_key_arn
}

# ---------------------------------------------------------------------------
# Shared services: identity, session store, events, guardrail.
# ---------------------------------------------------------------------------

# OAuth callback/logout URLs are built from the CloudFront domain — the
# resource graph stays acyclic because the distribution has no Cognito
# dependency.
module "cognito" {
  source = "./modules/cognito"

  environment   = var.environment
  domain_prefix = var.cognito_domain_prefix
  callback_urls = local.portal_urls
  logout_urls   = local.portal_urls
}

module "dynamodb" {
  source = "./modules/dynamodb"

  environment = var.environment
  kms_key_arn = var.kms_key_arn
}

module "appsync_events" {
  source = "./modules/appsync_events"

  environment  = var.environment
  user_pool_id = module.cognito.user_pool_id
}

module "bedrock_guardrail" {
  source = "./modules/bedrock_guardrail"

  environment = var.environment
  kms_key_arn = var.kms_key_arn
}

# The role the DevOps Agent assumes to inspect this account (Req 3.2): its
# own service-linked role carries no read access, so without this — and the
# out-of-band AssociateService call that registers it — every diagnostic
# question returns an empty answer. Read-only by construction: managed
# ReadOnlyAccess minus an explicit Deny on data-plane and secret reads, and
# no elevated role is ever registered (module header).
module "devops_agent_access" {
  source = "./modules/devops_agent_access"

  environment = var.environment
}

# ---------------------------------------------------------------------------
# IAM roles and the ECS voice service. The two modules reference each other
# at module level (iam needs the cluster ARN, ecs_service needs the role
# ARNs), but the resource graph is acyclic: the iam roles themselves have
# no cluster dependency — only the task role's policy scopes
# ecs:UpdateTaskProtection to the cluster — while the cluster depends on
# nothing from iam (only the task definition consumes the role ARNs).
# ---------------------------------------------------------------------------

module "iam" {
  source = "./modules/iam"

  environment                      = var.environment
  nova_sonic_model_id              = var.nova_sonic_model_id
  guardrail_arn                    = module.bedrock_guardrail.guardrail_arn
  cluster_arn                      = module.ecs_service.cluster_arn
  ssm_parameter_path_prefix        = local.ssm_parameter_path_prefix
  secretsmanager_secret_arn_prefix = local.secretsmanager_secret_arn_prefix

  dynamodb_table_arns = [
    module.dynamodb.voice_sessions_table_arn,
    module.dynamodb.voice_sessions_by_engineer_gsi_arn,
    module.dynamodb.agent_chats_table_arn,
    module.dynamodb.push_subscriptions_table_arn,
    module.dynamodb.transcripts_table_arn,
  ]
}

locals {
  # Non-sensitive Voice_Service configuration (Req 14.1), matching the
  # startup required-key manifest in backend/voice_service/app/config.py
  # (REQUIRED_ENV_KEYS) plus the optional RETENTION_DAYS tunable (Req 8.4).
  # ORIGIN_VERIFY_SECRET_NAME carries the SSM parameter *name* — never the
  # value — which the service resolves itself at startup (Req 14.5).
  voice_service_environment = {
    AWS_REGION                       = var.aws_region
    NOVA_SONIC_MODEL_ID              = var.nova_sonic_model_id
    SESSIONS_TABLE_NAME              = module.dynamodb.voice_sessions_table_name
    CHATS_TABLE_NAME                 = module.dynamodb.agent_chats_table_name
    SUBSCRIPTIONS_TABLE_NAME         = module.dynamodb.push_subscriptions_table_name
    TRANSCRIPTS_TABLE_NAME           = module.dynamodb.transcripts_table_name
    GUARDRAIL_ID                     = module.bedrock_guardrail.guardrail_id
    GUARDRAIL_VERSION                = module.bedrock_guardrail.guardrail_version
    APPSYNC_EVENTS_HTTP_ENDPOINT     = module.appsync_events.http_endpoint
    APPSYNC_EVENTS_REALTIME_ENDPOINT = module.appsync_events.realtime_endpoint
    COGNITO_USER_POOL_ID             = module.cognito.user_pool_id
    COGNITO_CLIENT_ID                = module.cognito.client_id
    DEVOPS_AGENT_SPACE_ID            = var.devops_agent_space_id
    ORIGIN_VERIFY_SECRET_NAME        = aws_ssm_parameter.origin_verify.name
    RETENTION_DAYS                   = tostring(var.session_retention_days)
  }
}

module "ecs_service" {
  source = "./modules/ecs_service"

  environment        = var.environment
  vpc_id             = module.network.vpc_id
  private_subnet_ids = module.network.private_subnet_ids
  container_image    = var.container_image
  container_port     = var.container_port

  target_group_arn      = module.alb.target_group_arn
  alb_security_group_id = module.alb.security_group_id
  alb_arn_suffix        = module.alb.alb_arn_suffix

  execution_role_arn = module.iam.execution_role_arn
  task_role_arn      = module.iam.task_role_arn

  max_capacity        = var.max_capacity
  scale_out_threshold = var.scale_out_threshold
  scale_in_threshold  = var.scale_in_threshold

  environment_variables = local.voice_service_environment
  secrets               = var.secrets

  log_retention_days = var.log_retention_days
  kms_key_arn        = var.kms_key_arn
}

# ---------------------------------------------------------------------------
# Notification plane and observability.
# ---------------------------------------------------------------------------

module "notifications" {
  source = "./modules/notifications"

  environment     = var.environment
  lambda_zip_path = var.lambda_zip_path

  appsync_events_http_endpoint = module.appsync_events.http_endpoint
  channel_namespace_arn        = module.appsync_events.channel_namespace_arn
  subscriptions_table_name     = module.dynamodb.push_subscriptions_table_name
  subscriptions_table_arn      = module.dynamodb.push_subscriptions_table_arn

  vapid_subject                    = var.vapid_subject
  vapid_private_key_parameter_name = var.vapid_private_key_parameter_name

  devops_agent_event_source = var.devops_agent_event_source
  create_escalation_topic   = var.create_escalation_topic

  log_retention_days = var.log_retention_days
  kms_key_arn        = var.kms_key_arn
}

module "observability" {
  source = "./modules/observability"

  environment             = var.environment
  ecs_cluster_name        = module.ecs_service.cluster_name
  ecs_service_name        = module.ecs_service.service_name
  alb_arn_suffix          = module.alb.alb_arn_suffix
  target_group_arn_suffix = module.alb.target_group_arn_suffix
  notifier_function_name  = module.notifications.lambda_function_name

  running_task_count_threshold = var.running_task_count_threshold
  voice_5xx_threshold          = var.voice_5xx_threshold
  alarm_email_subscriptions    = var.alarm_email_subscriptions
}
