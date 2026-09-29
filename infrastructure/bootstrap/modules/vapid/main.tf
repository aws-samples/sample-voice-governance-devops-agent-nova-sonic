# vapid — Terraform-native, create-if-absent Web Push VAPID key management.
#
# An `external` data source runs vapid-ensure.sh, which (idempotently)
# generates a P-256 key pair only when the SSM parameter is absent, writes
# the PRIVATE key as an SSM SecureString itself, and returns ONLY the
# public key to Terraform. The private key therefore never enters Terraform
# state — honouring the design invariant that this layer carries the
# parameter name and public key only, never key material (Req 14.1).
#
# Why the bootstrap layer (not the app layer): the app layer is planned in
# one CodeBuild run and applied from that exact saved plan in a separate
# run, so a value produced during apply cannot feed the plan-time
# `vapid_public_key` variable, and a per-run-random generator would make
# the plan non-deterministic. The bootstrap layer is applied locally in a
# single step and keeps LOCAL state that is never exported to the frontend
# pipeline, so it is the correct home for a generate-and-store side effect.
# Its `vapid_public_key` output feeds the app layer's tfvars.
#
# Why `external` (not null_resource + local-exec): only a data source can
# return a value (the public key) back into Terraform to complete the app
# deploy. A null_resource/local-exec can run the side effect but cannot
# surface the public key, which is exactly what downstream needs.

terraform {
  required_version = ">= 1.9.0"

  required_providers {
    external = {
      source  = "hashicorp/external"
      version = ">= 2.3"
    }
  }
}

locals {
  # Default parameter name matches the tfvars convention documented in the
  # app layer (/<env>/notifier/vapid-private-key); overridable so it can be
  # kept in step with a non-default vapid_private_key_parameter_name.
  parameter_name = coalesce(var.parameter_name_override, "/${var.environment}/notifier/vapid-private-key")
}

# Create-if-absent: generates and stores the key on first apply, and is a
# read-only no-op on every subsequent apply (returns the recorded public
# key). The program never returns the private key, so nothing sensitive is
# recorded in state.
data "external" "vapid" {
  program = ["bash", "${path.module}/vapid-ensure.sh"]

  query = {
    parameter_name = local.parameter_name
    region         = var.region
    subject        = var.subject
  }
}
