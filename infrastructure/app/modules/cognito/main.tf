# Cognito module (Req 7.1, 15.3): user pool with closed enrollment and a
# strong password policy, an SPA app client restricted to the OAuth 2.0
# authorization-code + PKCE flow (no client secret, no implicit flow), and a
# hosted UI domain. The root layer wires the CloudFront domain into
# callback/logout URLs; outputs feed the frontend config.json (design:
# Configuration Model) and the Voice_Service environment
# (COGNITO_USER_POOL_ID / COGNITO_CLIENT_ID).

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

resource "aws_cognito_user_pool" "this" {
  name = "${var.environment}-support-portal"

  # The user pool is a stateful identity store; protect it against
  # accidental destroy the same way the DynamoDB tables are protected.
  deletion_protection = var.deletion_protection_enabled ? "ACTIVE" : "INACTIVE"

  # Engineers sign in with their e-mail address; addresses are verified so
  # account recovery below can rely on them.
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  # Closed enrollment (default): only administrators create engineer
  # accounts; self-service sign-up is disabled.
  admin_create_user_config {
    allow_admin_create_user_only = var.admin_create_only
  }

  # Password policy per current best practice: long minimum, all four
  # character classes, short-lived temporary passwords. Not
  # environment-specific, so fixed rather than variable-driven.
  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 7
  }

  mfa_configuration = var.mfa_configuration

  # Cognito requires at least one MFA method whenever MFA is ON or
  # OPTIONAL; TOTP authenticator apps need no SMS/SES wiring.
  dynamic "software_token_mfa_configuration" {
    for_each = var.mfa_configuration == "OFF" ? [] : [true]
    content {
      enabled = true
    }
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

# SPA app client: authorization-code grant with PKCE ONLY. Public browser
# clients cannot keep a secret, so generate_secret is false and PKCE
# protects the code exchange; the implicit flow is absent from
# allowed_oauth_flows. No password/SRP auth flow is exposed — sign-in goes
# through the hosted UI, and the client may only redeem refresh tokens.
resource "aws_cognito_user_pool_client" "spa" {
  name         = "${var.environment}-support-portal-spa"
  user_pool_id = aws_cognito_user_pool.this.id

  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = var.allowed_oauth_scopes
  callback_urls                        = var.callback_urls
  logout_urls                          = var.logout_urls
  supported_identity_providers         = ["COGNITO"]
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true

  # Short-lived access/id tokens: the Voice_Service closes a session when
  # the presented token expires mid-session (Req 7.6), and the frontend
  # refreshes ahead of expiry. The refresh token spans one on-call shift.
  access_token_validity  = 60
  id_token_validity      = 60
  refresh_token_validity = 8

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "hours"
  }
}

# Hosted UI domain: the frontend redirects unauthenticated engineers here
# for the PKCE sign-in flow (Req 7.7).
resource "aws_cognito_user_pool_domain" "this" {
  domain       = var.domain_prefix
  user_pool_id = aws_cognito_user_pool.this.id
}
