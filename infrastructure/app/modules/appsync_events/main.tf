# AppSync Events module (Req 5.1, 7.4, 7.8, 15.3): the Events_Channel the
# Notifier broadcasts Incident_Notifications on and every browser
# subscribes to. Authorization split per the design:
#   - connect + subscribe: Cognito user pool (default auth) — a missing,
#     expired, or invalid Cognito authorization is rejected by the API and
#     no events are delivered (Req 7.4, 7.8);
#   - publish: IAM — the Notifier Lambda signs POST /event with SigV4
#     (backend/notifier/src/channels/appsync_publisher.py).
# The "incidents" channel namespace makes the broadcast channel
# /incidents/all addressable.

terraform {
  required_version = ">= 1.9"

  # AppSync Events API support (aws_appsync_api with event_config and
  # aws_appsync_channel_namespace) first shipped in AWS provider v6.9.0
  # (August 2025) — earlier 5.x/6.x releases cannot express these
  # resources, so every app-layer module pins this same series.
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.9.0, < 7.0.0"
    }
  }
}

data "aws_region" "current" {}

resource "aws_appsync_api" "this" {
  name = "${var.environment}-incident-events"

  event_config {
    # Both providers the API accepts anywhere: Cognito for browsers,
    # IAM for the Notifier Lambda's SigV4 publishes.
    auth_provider {
      auth_type = "AMAZON_COGNITO_USER_POOLS"

      cognito_config {
        user_pool_id = var.user_pool_id
        aws_region   = data.aws_region.current.region
      }
    }

    auth_provider {
      auth_type = "AWS_IAM"
    }

    # Realtime WebSocket connections come only from browsers, so the
    # connection handshake requires Cognito (Req 7.4, 7.8).
    connection_auth_mode {
      auth_type = "AMAZON_COGNITO_USER_POOLS"
    }

    # Publishes come only from the Notifier Lambda over HTTP with SigV4.
    default_publish_auth_mode {
      auth_type = "AWS_IAM"
    }

    default_subscribe_auth_mode {
      auth_type = "AMAZON_COGNITO_USER_POOLS"
    }
  }
}

# Channel namespace "incidents": events publish and subscribe on channels
# under /incidents/*, including the portal-wide broadcast channel
# /incidents/all (Req 5.1). Auth modes are inherited from the API defaults
# above (Cognito subscribe, IAM publish).
resource "aws_appsync_channel_namespace" "incidents" {
  api_id = aws_appsync_api.this.api_id
  name   = var.channel_namespace
}
