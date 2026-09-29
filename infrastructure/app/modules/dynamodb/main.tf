# DynamoDB module (Req 8.1, 8.4, 12.3, 15.3): the four Session_Store tables
# of the design data model. Key and attribute names mirror the backend
# adapter (backend/voice_service/app/adapters/dynamodb_store.py) exactly:
# session_id / engineer_id / created_at / endpoint_hash / seq, TTL attribute
# "ttl" (epoch seconds). All tables use on-demand capacity, server-side
# encryption, and point-in-time recovery; TTL is enabled where the data
# model calls for it — the push-subscriptions table carries no TTL because
# subscription records are removed explicitly on unsubscribe or
# push-service rejection (Req 6.5).
#
# Key syntax: hash_key/range_key is used deliberately. Recent 6.x provider
# releases (>= ~6.29) deprecate it in favor of key_schema and emit a
# validate-time warning, but key_schema is newer than this module's
# provider floor (6.9.0) and has had GSI-related fixes land as recently as
# late 6.x, while hash_key/range_key stays fully supported for all of 6.x.

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

locals {
  # SSE: enabled = true with a null kms_key_arn selects the AWS managed
  # KMS key (aws/dynamodb); a customer managed key can be supplied via
  # var.kms_key_arn (Req 12.3).
  by_engineer_index_name = "by-engineer"
}

# Voice_Session state (Req 8.1, 8.2), keyed by session_id, with the
# by-engineer GSI (PK engineer_id, SK created_at) for reconnect lookups.
resource "aws_dynamodb_table" "voice_sessions" {
  name                        = "${var.environment}-voice-sessions"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "session_id"
  deletion_protection_enabled = var.deletion_protection_enabled

  attribute {
    name = "session_id"
    type = "S"
  }

  attribute {
    name = "engineer_id"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "S"
  }

  global_secondary_index {
    name            = local.by_engineer_index_name
    hash_key        = "engineer_id"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }

  point_in_time_recovery {
    enabled = true
  }
}

# DevOps_Agent chat/execution mapping (Req 3.2, 8.1): one chat per
# Voice_Session, keyed by session_id; TTL aligned with the owning session.
resource "aws_dynamodb_table" "agent_chats" {
  name                        = "${var.environment}-agent-chats"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "session_id"
  deletion_protection_enabled = var.deletion_protection_enabled

  attribute {
    name = "session_id"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }

  point_in_time_recovery {
    enabled = true
  }
}

# Web_Push_Subscriptions (Req 6.1, 8.1, 8.7), keyed by
# (engineer_id, endpoint_hash). No TTL: records are removed explicitly on
# unsubscribe or on push-service rejection 404/410 (Req 6.5).
resource "aws_dynamodb_table" "push_subscriptions" {
  name                        = "${var.environment}-push-subscriptions"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "engineer_id"
  range_key                   = "endpoint_hash"
  deletion_protection_enabled = var.deletion_protection_enabled

  attribute {
    name = "engineer_id"
    type = "S"
  }

  attribute {
    name = "endpoint_hash"
    type = "S"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }

  point_in_time_recovery {
    enabled = true
  }
}

# Conversation transcripts (Req 2.5, 8.1, 8.3), keyed by
# (session_id, seq) with the numeric monotonically increasing sort key.
resource "aws_dynamodb_table" "transcripts" {
  name                        = "${var.environment}-transcripts"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "session_id"
  range_key                   = "seq"
  deletion_protection_enabled = var.deletion_protection_enabled

  attribute {
    name = "session_id"
    type = "S"
  }

  attribute {
    name = "seq"
    type = "N"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }

  point_in_time_recovery {
    enabled = true
  }
}
