# Bedrock Guardrail module (Req 4.1, 4.2, 15.3): the guardrail the
# Voice_Service evaluates with the standalone ApplyGuardrail API before any
# engineer request reaches the DevOps_Agent (design: Guardrail Evaluation
# Model). A DENY topic blocks Destructive_Operation requests — anything
# that creates, modifies, deletes, or terminates AWS resources or IAM
# entities (Req 4.2) — and content filters cover harmful content and
# prompt-attack attempts.
#
# Automated Reasoning policy (Req 4.1): the design attaches a
# read-only-operations Automated Reasoning policy to this guardrail, and
# AR findings are detect-mode — the fail-closed decision lives in the
# Voice_Service (app/domain/guardrail_policy.py), which BLOCKs on any
# non-VALID finding, on GUARDRAIL_INTERVENED, and on any evaluation error
# (Req 4.4, 4.6, 4.7). The AWS Terraform provider (through the 6.x series)
# has no resource or argument for Automated Reasoning policies or their
# attachment, so the policy cannot be expressed here yet. Until provider
# support lands, var.automated_reasoning_policy_arn documents the
# pre-built policy an operator attaches out-of-band (console/CLI:
# bedrock update-guardrail --automated-reasoning-policy-config); the DENY
# topic below independently enforces the destructive-operation block so
# the guardrail blocks Req 4.2's canonical utterances even before the AR
# policy is attached.
#
# No word policy is configured deliberately: word lists match literal
# strings, so words like "delete" or "terminate" would also block
# legitimate diagnostic questions ("why was my instance terminated?").
# The DENY topic evaluates intent semantically instead.

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

# Destructive_Operation DENY topic content (Req 4.2), kept in locals so the
# preconditions below can validate it against the Bedrock guardrail topic
# quotas at plan time. Examples are canonical utterances from the
# requirement and the post-deploy smoke tests; the quota allows at most 5,
# so the purge-queue utterance is not an example — the definition names
# "purging queues" explicitly and the smoke tests verify it is still
# blocked semantically.
locals {
  # Calibrated empirically against the live Bedrock topic classifier with
  # a 22-phrase probe matrix (12 read/diagnostic, 10 mutating). Two
  # properties of the wording matter, and both were measured:
  #
  #   * VERBS, NOT NOUNS. The previous definition named resource nouns
  #     ("launching or terminating instances, deleting or purging
  #     queues"), and the classifier keyed on the nouns: every read about
  #     EC2 ("List all EC2 instance ids", "Describe my EC2 instances")
  #     matched the DENY topic and was refused, which made the portal
  #     unable to answer the read questions it exists for.
  #   * AN EXPLICIT READ EXCLUSION. Dropping the nouns alone still
  #     blocked "List all EC2 instance ids"; appending "Excludes
  #     read-only questions." is what let all 12 read phrasings through
  #     while every mutating phrasing kept matching.
  #
  # The examples deliberately include IAM privilege escalation: with the
  # old noun-led wording, "Create an IAM role with administrator access"
  # PASSED (it is one of the old examples verbatim) — the exact
  # false-negative class a read-only portal can least afford.
  #
  # Topic matching stays probabilistic, so it is not the only line of
  # defence: some imperative mutations ("Scale the auto scaling group to
  # 10 instances", "Create an access key for my IAM user") still pass
  # this topic, and the Voice_Service blocks those deterministically
  # before the guardrail is even consulted
  # (backend/voice_service/app/domain/mutation_guard.py).
  destructive_operations_topic = {
    name       = "destructive-operations"
    definition = "Requests to perform a write or administrative action: create, launch, modify, update, delete, remove, terminate, stop, restart, purge, or grant permissions. Excludes read-only questions."
    examples = [
      "Terminate the EC2 instance i-0abc123.",
      "Delete the SQS queue orders-queue.",
      "Create an IAM role with administrator access.",
      "Attach the AdministratorAccess policy to my IAM user.",
      "Purge all messages from the payments queue.",
    ]
  }
}

resource "aws_bedrock_guardrail" "this" {
  name                      = "${var.environment}-read-only-operations"
  description               = "Restricts the support portal to read and diagnostic operations: blocks requests to create, modify, delete, or terminate AWS resources or IAM entities (Req 4.2)."
  blocked_input_messaging   = var.blocked_input_messaging
  blocked_outputs_messaging = var.blocked_outputs_messaging
  kms_key_arn               = var.kms_key_arn

  # Destructive_Operation DENY topic (Req 4.2); content in locals above.
  topic_policy_config {
    topics_config {
      name       = local.destructive_operations_topic.name
      type       = "DENY"
      definition = local.destructive_operations_topic.definition
      examples   = local.destructive_operations_topic.examples
    }
  }

  # Bedrock guardrail topic quotas, enforced at plan time so a violation
  # fails the IaC pipeline's BuildAndPlan stage with a clear message
  # instead of failing mid-apply (the API rejects: definition > 200
  # characters, more than 5 examples per topic, an example > 100
  # characters).
  lifecycle {
    precondition {
      condition     = length(local.destructive_operations_topic.definition) <= 200
      error_message = "Guardrail topic definition is ${length(local.destructive_operations_topic.definition)} characters; Bedrock allows at most 200."
    }

    precondition {
      condition     = length(local.destructive_operations_topic.examples) <= 5
      error_message = "Guardrail topic has ${length(local.destructive_operations_topic.examples)} examples; Bedrock allows at most 5 per topic."
    }

    precondition {
      condition     = alltrue([for e in local.destructive_operations_topic.examples : length(e) <= 100])
      error_message = "Every guardrail topic example must be at most 100 characters."
    }
  }

  # Harmful-content filters on both directions; the prompt-attack filter
  # guards the input side against jailbreak attempts to bypass the
  # read-only restriction (its output strength must be NONE — prompt
  # attacks are an input-only concept in the Bedrock API).
  content_policy_config {
    filters_config {
      type            = "HATE"
      input_strength  = "HIGH"
      output_strength = "HIGH"
    }

    filters_config {
      type            = "INSULTS"
      input_strength  = "HIGH"
      output_strength = "HIGH"
    }

    filters_config {
      type            = "SEXUAL"
      input_strength  = "HIGH"
      output_strength = "HIGH"
    }

    filters_config {
      type            = "VIOLENCE"
      input_strength  = "HIGH"
      output_strength = "HIGH"
    }

    filters_config {
      type            = "MISCONDUCT"
      input_strength  = "HIGH"
      output_strength = "HIGH"
    }

    filters_config {
      type            = "PROMPT_ATTACK"
      input_strength  = "HIGH"
      output_strength = "NONE"
    }
  }
}

# Numbered guardrail version the Voice_Service pins via GUARDRAIL_VERSION:
# ApplyGuardrail evaluates a published version, never the mutable DRAFT.
# replace_triggered_by publishes a fresh version whenever the guardrail
# definition changes, so the pinned version never drifts behind the
# configuration; skip_destroy retains superseded versions for audit.
resource "aws_bedrock_guardrail_version" "this" {
  guardrail_arn = aws_bedrock_guardrail.this.guardrail_arn
  description   = "Read-only-operations guardrail version for the ${var.environment} environment."
  skip_destroy  = true

  lifecycle {
    replace_triggered_by = [aws_bedrock_guardrail.this.updated_at]
  }
}
