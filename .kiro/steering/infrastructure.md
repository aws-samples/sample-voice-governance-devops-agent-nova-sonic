---
inclusion: fileMatch
fileMatchPattern: 'infrastructure/**'
---

# Infrastructure Conventions (infrastructure/)

Standards for all infrastructure code under `infrastructure/`. Terraform HCL only — no CloudFormation, no CDK, no console-created resources.

## Two Layers, Separate State

- `infrastructure/bootstrap/` (layer 1): source buckets, CodePipeline pipelines, CodeBuild projects, ECR, artifact store, CI/CD IAM roles, and the app-layer state backend (state bucket + DynamoDB lock table). Applied locally by the operator; keeps its own separate state.
- `infrastructure/app/` (layer 2): all runtime infrastructure (VPC, ALB, ECS, Cognito, DynamoDB, AppSync Events, Bedrock Guardrail, WAF, CloudFront + S3, EventBridge, notifier Lambda, CloudWatch alarms, SNS). Applied only by the IaC_Pipeline; its state lives in the bootstrap-created bucket with the DynamoDB lock table.
- Never mix state between layers. Each layer plans and applies independently of the other.

## Variables — Never Hardcode

- Every environment-specific value (environment name, account inputs, `access_logging_bucket_name`, scaling thresholds, retention days) is a Terraform input variable. Zero hardcoded account identifiers, environment names, or environment-specific endpoints in resource definitions.
- `access_logging_bucket_name` carries a `validation` block that rejects empty values with a clear message stating the bucket name is required.
- Required variables have no defaults, so an unset variable fails `terraform plan` with a clear error before any resource is created or modified.
- Every `variable` and `output` declares a `description`; every variable declares a `type`.

## Security Defaults (non-negotiable)

- Server-side encryption enabled on every S3 bucket, DynamoDB table, CloudWatch log group, and ECR repository.
- Every S3 bucket created by either layer gets a bucket policy denying requests with `aws:SecureTransport = false` and requests using a TLS version lower than 1.2 (apply the `s3_policies` module).
- ALB deletion protection enabled.
- Frontend bucket policy: read access only for the CloudFront distribution via OAC; deny all other principals.
- The access-logging bucket is referenced by the `access_logging_bucket_name` variable — it is never created by any layer.
- ECR repositories enable scan-on-push.
- WAF web ACLs at both scopes (CLOUDFRONT and REGIONAL) include AWSManagedRulesCommonRuleSet and AWSManagedRulesKnownBadInputsRuleSet in block mode, with WAF logging enabled to a persistent destination.
- DynamoDB tables enable TTL where the data model calls for it, plus SSE and point-in-time recovery.

## Module Structure

- Each module lives under `modules/<name>/` with `main.tf`, `variables.tf`, and `outputs.tf`.
- Layer roots (`bootstrap/`, `app/`) contain only `main.tf`, `variables.tf`, `outputs.tf` wiring module instances together — resource definitions belong inside modules.
- The pipeline module is reusable: Source(S3) → SecurityScan → UnitTest → Build+Plan → ManualApproval (7-day timeout) → Deploy, each stage gated on the previous one, with no CodePipeline S3 deploy action anywhere.

## Quality Gates (pipeline fails on any violation)

- `terraform fmt -check` and `terraform validate` on both layers (unit-test stage).
- `checkov --check-severity HIGH`, `gitleaks`, and the hardcoded-value grep gate (security-scan stage); high or critical findings fail the stage.
- Plan-JSON assertion tests (pytest against `terraform show -json`) verify the security defaults listed above before any apply.
