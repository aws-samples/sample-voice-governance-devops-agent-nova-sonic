# Security

If you discover a potential security issue in this project, we ask that you notify AWS Security via our [vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public issue.

## Reporting Security Issues

**Please do not report security vulnerabilities through public issue trackers.**

Instead, report them to the AWS Security team:

1. Visit the [AWS Vulnerability Reporting page](https://aws.amazon.com/security/vulnerability-reporting/)
2. Follow the instructions to submit your report

Please include as much of the following as you can:

* Type of issue (e.g. authentication bypass, injection, SSRF, guardrail evasion)
* Full paths of source file(s) related to the manifestation of the issue
* The location of the affected source code (tag/branch/commit or direct URL)
* Any special configuration required to reproduce the issue
* Step-by-step instructions to reproduce the issue
* Proof-of-concept or exploit code (if possible)
* Impact of the issue, including how an attacker might exploit it

This information helps us triage your report more quickly.

## Security scanning in the pipelines

Each of the three pipelines runs a **SecurityScan** stage before its UnitTest, BuildAndPlan, ManualApproval, and Deploy stages. Every gate below fails the build on findings. The buildspecs are the source of truth (`ci/<pipeline>/scan.yml`).

| Pipeline | Gates (all blocking) |
|---|---|
| Backend (`ci/backend/scan.yml`) | `gitleaks detect --no-git --redact`; `bandit -ll -r backend/` (fails on MEDIUM+); `pip-audit` over the resolved runtime dependency set of **both** `backend/voice_service` and `backend/notifier` (any known advisory fails — OSV advisories are not uniformly scored, so no severity filter is applied) |
| Frontend (`ci/frontend/scan.yml`) | `gitleaks`; `npm audit --audit-level=high`; `npx eslint .` |
| IaC (`ci/iac/scan.yml`) | `gitleaks`; `checkov --directory infrastructure --hard-fail-on HIGH --hard-fail-on CRITICAL`; a hardcoded-value grep gate |

gitleaks is version-pinned (8.21.2) and always runs **first**, before any tool writes into the source tree, with `--redact` so candidate values never reach build logs. Documented false positives are allowlisted in `.gitleaks.toml`; no real credential is suppressed.

The IaC grep gate enforces two deny rules: no 12-digit AWS-account-id-like values in any source file (`envs/` and `package-lock.json` excluded), and no hardcoded `us-east-1` in Terraform outside `variables.tf` and comments — the region is a variable.

> **Note on ASH.** The `ash_output/` directory holds results from a manual [Automated Security Helper](https://github.com/awslabs/automated-security-helper) run. ASH is **not** wired into any pipeline stage; it is an out-of-band review artifact. The blocking gates are the ones tabulated above.

## Implemented controls

### Edge and network

- **Two WAF web ACLs** (`infrastructure/app/modules/waf`) — one at `CLOUDFRONT` scope on the distribution, one at `REGIONAL` scope attached to the ALB. Both carry a rate-limit rule plus `AWSManagedRulesCommonRuleSet` and `AWSManagedRulesKnownBadInputsRuleSet` in **block** mode (`override_action { none }`), with logging to CloudWatch Logs and redacted fields.
- **S3 is reachable only through CloudFront** via Origin Access Control; the frontend bucket blocks all public access, is encrypted (AES256, or KMS when `kms_key_arn` is set), and is versioned.
- **Bucket policies deny insecure transport** — `aws:SecureTransport = false` and `s3:TlsVersion < 1.2` are both denied (`infrastructure/app/modules/s3_policies`).
- **CloudFront redirects all viewers to HTTPS** (`viewer_protocol_policy = "redirect-to-https"`). See the TLS caveat below.
- **Direct-to-ALB traffic is rejected.** CloudFront injects a 32-character origin-verify header; the ALB listener rule forwards only requests carrying it, and everything else gets a fixed-response. The ALB additionally sets `drop_invalid_header_fields = true`, enables deletion protection, and writes access logs to the operator-supplied bucket.

### Identity and authorization

- **Cognito user pool** (`infrastructure/app/modules/cognito`): 12-character minimum password with all four character classes, 7-day temporary-password validity, `prevent_user_existence_errors = ENABLED`, token revocation enabled, 60-minute access and ID tokens with an 8-hour refresh token (one on-call shift), configurable MFA, and enrollment restricted to admin-create-only (`admin_create_only`). Sign-in uses OAuth 2.0 authorization code + PKCE against the hosted UI.
- **JWT validation is fail-closed** at the WebSocket handshake (`backend/voice_service/app/auth/jwt_validator.py`): RS256 signatures are verified against the pool JWKS, `token_use` must be `access`, and any doubt rejects the connection. The token travels as a `Sec-WebSocket-Protocol` subprotocol pair, never in a URL or query string.

### Data protection

- **DynamoDB**: server-side encryption on all four tables (AWS-managed key, or a CMK via `kms_key_arn`), point-in-time recovery, TTL-based expiry, and deletion protection.
- **ECR**: `scan_on_push = true`, immutable tags by default, and server-side encryption (AES256 or KMS).
- **Secrets never live in source.** The VAPID private key and the origin-verify value are SSM SecureStrings; only their *parameter names* appear in source and tfvars. The backend wraps sensitive config in a `Secret` type whose `str`/`repr` renders `Secret(<key>)`, so values cannot be logged accidentally.

### AI safety — the fail-closed guardrail

This is the headline control. Every `ask_devops_agent` tool call is gated before it can reach the DevOps Agent, by **two independent layers**:

1. **A deterministic mutation guard** (`backend/voice_service/app/domain/mutation_guard.py`) blocks mutating requests before the guardrail is even consulted. Bedrock topic matching is probabilistic, so this layer catches imperative mutations that the topic classifier lets through.
2. **The Bedrock Guardrail** (`infrastructure/app/modules/bedrock_guardrail`) carries a `destructive-operations` DENY topic plus content filters for harmful content and prompt attacks. The decision function (`backend/voice_service/app/domain/guardrail_policy.py`) **BLOCKs on any non-`VALID` finding, on `GUARDRAIL_INTERVENED`, and on any evaluation error** — a guardrail that cannot be reached is a guardrail that blocks.

The DENY topic wording is verb-led with an explicit read-only exclusion, calibrated against a 22-phrase probe matrix. Noun-led wording caused read questions about EC2 to be refused while `Create an IAM role with administrator access` passed — the exact false-negative class a read-only portal can least afford. Do not "simplify" that definition without re-running the probe matrix.

### Observability

Four CloudWatch alarms (running task count, ALB unhealthy targets, voice 5XX rate, Notifier errors) publish to an ops SNS topic. WAF logs, ECS logs, and Notifier logs all honour a configurable retention period.

## Operator responsibilities

The code cannot do these for you.

1. **Attach the Automated Reasoning policy out-of-band.** The AWS Terraform provider (through the 6.x series) has no resource or argument for Automated Reasoning policies or their attachment, so `infrastructure/app/modules/bedrock_guardrail` cannot express it. `var.automated_reasoning_policy_arn` documents the pre-built policy; attach it with `aws bedrock update-guardrail --automated-reasoning-policy-config`. The DENY topic and the mutation guard enforce the destructive-operation block in the meantime.
2. **Attach a custom ACM certificate if you need a real TLS 1.2 floor** — see the section below.
3. **Never regenerate the VAPID key pair** once browsers have subscribed; a new pair invalidates every existing push subscription. All three provisioning paths are deliberately create-if-absent.
4. **Provide a compliant S3 access-logging bucket.** The stack references it and never creates it, so its policy and retention are yours to own.
5. **Enable Bedrock model access** to Nova 2 Sonic in `us-east-1`, and keep enrollment admin-create-only unless you have a reason to open it.
6. **Rotate the origin-verify secret** with `terraform taint random_password.origin_verify` followed by an apply, which updates CloudFront, the ALB rule, and the SSM parameter together. Note that because Terraform generates this value, it is present in Terraform state — protect the state bucket accordingly.
7. **Review IAM before production.** Roles are resource-scoped by design; re-verify them against your own least-privilege bar.

## Use a custom ACM certificate for TLS enforcement

`infrastructure/app/modules/cloudfront_s3/main.tf` sets `minimum_protocol_version = "TLSv1.2_2021"`, but it also sets `cloudfront_default_certificate = true`. **That security policy only takes effect when a custom ACM certificate is attached.** On the default `*.cloudfront.net` domain, CloudFront ignores the setting and permits older TLS versions (TLSv1.0/1.1), which have known weaknesses.

This Terraform does not currently expose a custom domain or certificate — there are no `aliases` or `acm_certificate_arn` variables — so enabling one is a **code change**, not a configuration value:

1. **Request or import a certificate** in ACM. It must be in `us-east-1` for CloudFront:
   ```bash
   aws acm request-certificate \
     --domain-name portal.example.com \
     --validation-method DNS \
     --region us-east-1
   ```

2. **Validate it** by adding the DNS CNAME records ACM provides.

3. **Add the variables and wire them through.** Introduce `acm_certificate_arn` and `domain_names` in `infrastructure/app/modules/cloudfront_s3/variables.tf`, surface them in `infrastructure/app/variables.tf`, then replace the `viewer_certificate` block and add `aliases` to `aws_cloudfront_distribution`:
   ```hcl
   aliases = var.domain_names

   viewer_certificate {
     acm_certificate_arn      = var.acm_certificate_arn
     ssl_support_method       = "sni-only"
     minimum_protocol_version = "TLSv1.2_2021"
   }
   ```

4. **Set the values** in `infrastructure/app/envs/<env>.tfvars`, commit, and re-run the IaC pipeline.

5. **Create a DNS alias** (Route 53 or your provider) pointing the domain at the distribution.

## Known gaps and accepted risks

Read [SECURITY_EXCEPTIONS.md](SECURITY_EXCEPTIONS.md) for the full analysis. In summary:

- **One accepted container CVE**: CVE-2026-85091 in zlib. No Debian suite carries a fix, `libz` cannot be removed (CPython links it), and the vulnerable `gzFile` API is not reachable from this service. Re-check before each release; it closes with a rebuild once Debian ships a fix.
- **The backend pipeline does not scan the image it builds.** gitleaks, bandit, and pip-audit inspect source and Python dependencies, not the built image's OS packages — which is why a High in `perl-base` was found post-deploy by Inspector rather than at build time. Adding grype or trivy with `--only-fixed` against the pushed image would close this; it needs an allowlist for the zlib exception above, which is High and unfixable.
- **No CloudFront response headers policy.** There is currently no managed or custom response-headers policy on the distribution, so CSP, HSTS, `X-Frame-Options`, and `Referrer-Policy` are not set at the edge. Adding one is a straightforward hardening step for production use.

## General practices for this codebase

1. **Never commit credentials** — use SSM Parameter Store or Secrets Manager and reference parameters by name.
2. **Keep secrets out of `config.json`** — it is generated at deploy time from Terraform outputs and served to browsers. Only non-sensitive values (the VAPID *public* key, endpoints, Cognito ids) belong there.
3. **Keep dependencies current** — `pip-audit`, `npm audit`, and the Dockerfile's `apt-get upgrade` in both stages are what keep advisory counts down.
4. **Act on scan findings rather than suppressing them.** If a suppression is genuinely necessary, document it in `.gitleaks.toml` or `SECURITY_EXCEPTIONS.md` with the reasoning, as the existing entries do.
5. **Run the smoke checks after deploying** (`scripts/smoke/run-all.sh`) — they verify that the guardrail blocks destructive requests, that direct-to-ALB access is rejected, that AppSync requires auth, and that WAF blocks known-bad input.

## Preferred Languages

We prefer all communications to be in English.
