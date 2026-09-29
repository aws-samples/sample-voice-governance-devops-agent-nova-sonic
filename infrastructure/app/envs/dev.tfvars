# Replace every REPLACE_ value before running scripts/deploy.sh infra.
# Non-secret values only. See the README runbook, steps 2 and 4.
environment                      = "dev"
access_logging_bucket_name       = "REPLACE_WITH_EXISTING_ALB_ACCESS_LOG_BUCKET"
container_image                  = "REPLACE_WITH_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/REPLACE_WITH_PROJECT-dev-voice-service:REPLACE_WITH_IMAGE_TAG"
devops_agent_space_id            = "REPLACE_WITH_AGENT_SPACE_ID"
cognito_domain_prefix            = "REPLACE_WITH_GLOBALLY_UNIQUE_PREFIX"
vapid_subject                    = "mailto:ops@example.com"
vapid_private_key_parameter_name = "/dev/notifier/vapid-private-key"
vapid_public_key                 = "REPLACE_WITH_VAPID_PUBLIC_KEY_FROM_BOOTSTRAP_OUTPUT"
