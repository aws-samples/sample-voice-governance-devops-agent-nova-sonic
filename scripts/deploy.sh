#!/usr/bin/env bash
# scripts/deploy.sh — idempotent end-to-end deployment orchestrator.
#
# Drives the full Nova Sonic Support Portal deployment described in
# README.md, or any single stage of it, from one entry point. It is a thin
# orchestration layer over the tools the repository already ships:
#
#   * terraform            — applies the bootstrap layer locally
#   * scripts/push-source.sh — uploads source.zip to a pipeline's bucket,
#                              which is the "commit push" event that starts
#                              the matching CodePipeline
#   * aws codepipeline ...  — polls execution/stage state, approves the
#                              ManualApproval gate
#   * scripts/smoke/run-all.sh — post-deploy read-only smoke checks
#
# The portal is PIPELINE-DRIVEN: only the bootstrap layer is applied
# locally. The app layer, the container image, and the frontend bundle are
# all deployed by CodePipeline (Source -> SecurityScan -> UnitTest ->
# BuildAndPlan -> ManualApproval -> Deploy). This script pushes source,
# waits for each pipeline to reach its ManualApproval gate, surfaces the
# BuildAndPlan output for review, approves (interactively or with
# --auto-approve), then waits for Deploy.
#
# ---------------------------------------------------------------------------
# IDEMPOTENCY
# ---------------------------------------------------------------------------
# Running this script — or any single subcommand — repeatedly converges on
# the same deployed state and never creates duplicate or orphaned
# resources:
#
#   * bootstrap : `terraform apply` reconciles against Terraform state; a
#                 re-run with no config change is a no-op ("No changes").
#   * infra     : re-pushing the iac source starts a fresh pipeline run
#                 that re-plans and re-applies the app layer; Terraform
#                 state makes the apply converge (no duplicate resources).
#   * backend   : re-pushing builds and deploys the current image; ECS
#                 rolls the service only if the task definition changed.
#   * frontend  : re-pushing re-syncs the bundle (`aws s3 sync --delete`)
#                 and re-invalidates CloudFront — same bytes, same bucket.
#   * user      : `admin-create-user` is guarded by an existence check and
#                 is skipped when the account already exists (never
#                 overwrites a password or re-sends an invite).
#
# This script performs NO destructive operations (no delete/terminate/
# force). The only writes are `terraform apply` of the bootstrap layer,
# S3 source uploads, a CodePipeline approval, and an optional guarded
# Cognito user creation.
#
# ---------------------------------------------------------------------------
# USAGE
# ---------------------------------------------------------------------------
#   scripts/deploy.sh <command> [options]
#
# Commands (per-target; run any in isolation):
#   all            Run the whole flow: bootstrap -> infra -> backend ->
#                  wire-frontend -> frontend -> smoke.
#   bootstrap      Apply the bootstrap layer locally (Step 1). Idempotent.
#   vapid          Standalone (out-of-Terraform) generate-if-absent of the
#                  Web Push VAPID key pair: store the private key as an SSM
#                  SecureString and print the public key to paste into
#                  envs/<env>.tfvars. Idempotent — never overwrites an
#                  existing key (that would invalidate every live browser
#                  subscription). Prefer `bootstrap --with-vapid` for the
#                  Terraform-native path; use this when you want the key
#                  managed outside Terraform entirely.
#   infra          Push the iac source and drive the IaC pipeline that
#                  applies the app layer (Steps 2-3).
#   backend        Push the backend source and drive the backend pipeline
#                  (image -> ECR -> ECS) (Step 4).
#   wire-frontend  Re-apply the bootstrap layer with the app layer's
#                  frontend bucket + CloudFront id (two-phase wiring)
#                  (Step 5). Idempotent.
#   frontend       Push the frontend source and drive the frontend pipeline
#                  (sync -> invalidation) (Step 6).
#   create-user    Create a Cognito engineer account (guarded; idempotent).
#   smoke          Run the post-deploy read-only smoke checks (Step 7).
#   outputs        Fetch and print the app layer's exported outputs.
#
# Required options (or their environment-variable equivalents):
#   --project <name>        PROJECT   project_name for the bootstrap layer
#   --environment <env>     ENVIRONMENT  environment name (dev, prod, ...)
#   --region <region>       AWS_REGION   AWS region (default us-east-1)
#
# Optional:
#   --auto-approve          Approve each pipeline's ManualApproval gate
#                           automatically after BuildAndPlan succeeds.
#                           Without it, the script pauses and prompts.
#   --no-wait               Push source and return without polling the
#                           pipeline (fire-and-forget; not for `all`).
#   --username <email>      Engineer email for `create-user`.
#   --temp-password <pw>    Temporary password for `create-user` (else the
#                           script reads it interactively, never echoed).
#   --outputs-json <file>   For `smoke`: reuse a saved outputs capture.
#   --timeout <seconds>     Per-pipeline wait timeout (default 3600).
#   --vapid-param-name <n>  SSM SecureString parameter name for the VAPID
#                           private key (default /<env>/notifier/vapid-private-key).
#                           Must match vapid_private_key_parameter_name in
#                           envs/<env>.tfvars.
#   --vapid-subject <uri>   VAPID subject stored alongside the key note
#                           (mailto: or https:// URI); informational only —
#                           the deployed value comes from the tfvars.
#   --with-vapid            For `bootstrap`/`all`: drive the Terraform-native
#                           VAPID path (passes create_vapid_key=true to the
#                           bootstrap apply, so the vapid module generates &
#                           stores the key and exports the public key). Off
#                           by default so the key stays an explicit action.
#   -h | --help             Print this header.
#
# Environment knobs consumed by underlying tools:
#   SOURCE_OBJECT_KEY  overrides the source.zip object key (see
#                      scripts/push-source.sh); must match the object key
#                      the bootstrap layer was applied with.
#   TF_CLI_ARGS, etc.  standard Terraform environment variables pass through.
#
# Exit codes: 0 success; 2 usage error; 1 any deployment/verification
# failure (including a rejected or timed-out approval).

set -euo pipefail

# ---------------------------------------------------------------------------
# Locations.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BOOTSTRAP_DIR="$REPO_ROOT/infrastructure/bootstrap"
PUSH_SOURCE="$SCRIPT_DIR/push-source.sh"
SMOKE_RUNNER="$SCRIPT_DIR/smoke/run-all.sh"

# ---------------------------------------------------------------------------
# Colored logging (only when stderr is a terminal). All logs go to stderr
# so command substitution of helper output stays clean.
# ---------------------------------------------------------------------------
if [[ -t 2 ]]; then
  _C_BLUE=$'\033[34m'; _C_GREEN=$'\033[32m'; _C_RED=$'\033[31m'
  _C_YELLOW=$'\033[33m'; _C_BOLD=$'\033[1m'; _C_RESET=$'\033[0m'
else
  _C_BLUE=''; _C_GREEN=''; _C_RED=''; _C_YELLOW=''; _C_BOLD=''; _C_RESET=''
fi

log()  { printf '%s[deploy]%s %s\n' "$_C_BLUE" "$_C_RESET" "$*" >&2; }
ok()   { printf '%s[ok]%s %s\n'    "$_C_GREEN" "$_C_RESET" "$*" >&2; }
warn() { printf '%s[warn]%s %s\n'  "$_C_YELLOW" "$_C_RESET" "$*" >&2; }
err()  { printf '%s[error]%s %s\n' "$_C_RED" "$_C_RESET" "$*" >&2; }
step() { printf '\n%s== %s ==%s\n' "$_C_BOLD" "$*" "$_C_RESET" >&2; }

die() { err "$*"; exit 1; }

usage() {
  # Print the header comment block as help (lines 2.. up to the first
  # non-comment line), stripping the leading "# ".
  sed -n '2,/^[^#]/{/^#/p;}' "$0" | sed 's/^# \{0,1\}//'
}

# ---------------------------------------------------------------------------
# Defaults and option parsing.
# ---------------------------------------------------------------------------
PROJECT="${PROJECT:-}"
ENVIRONMENT="${ENVIRONMENT:-}"
REGION="${AWS_REGION:-us-east-1}"
AUTO_APPROVE=0
NO_WAIT=0
USERNAME=""
TEMP_PASSWORD=""
OUTPUTS_JSON=""
PIPELINE_TIMEOUT=3600
VAPID_PARAM_NAME=""
VAPID_SUBJECT=""
WITH_VAPID=0

COMMAND="${1:-}"
if [[ -z "$COMMAND" ]]; then
  err "no command given"
  usage
  exit 2
fi
case "$COMMAND" in
  -h|--help) usage; exit 0 ;;
esac
shift || true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)       PROJECT="${2:?--project needs a value}"; shift 2 ;;
    --environment)   ENVIRONMENT="${2:?--environment needs a value}"; shift 2 ;;
    --region)        REGION="${2:?--region needs a value}"; shift 2 ;;
    --auto-approve)  AUTO_APPROVE=1; shift ;;
    --no-wait)       NO_WAIT=1; shift ;;
    --username)      USERNAME="${2:?--username needs a value}"; shift 2 ;;
    --temp-password) TEMP_PASSWORD="${2:?--temp-password needs a value}"; shift 2 ;;
    --outputs-json)  OUTPUTS_JSON="${2:?--outputs-json needs a value}"; shift 2 ;;
    --timeout)       PIPELINE_TIMEOUT="${2:?--timeout needs a value}"; shift 2 ;;
    --vapid-param-name) VAPID_PARAM_NAME="${2:?--vapid-param-name needs a value}"; shift 2 ;;
    --vapid-subject) VAPID_SUBJECT="${2:?--vapid-subject needs a value}"; shift 2 ;;
    --with-vapid)    WITH_VAPID=1; shift ;;
    -h|--help)       usage; exit 0 ;;
    *) err "unknown option: $1"; usage; exit 2 ;;
  esac
done

# ---------------------------------------------------------------------------
# Prerequisite checks.
# ---------------------------------------------------------------------------

# require_tools <cmd>... — fail with a clear message if any tool is absent.
require_tools() {
  local missing=0 cmd
  for cmd in "$@"; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      err "required tool not found on PATH: $cmd"
      missing=1
    fi
  done
  [[ "$missing" -eq 0 ]] || exit 2
}

# require_identity — verify credentials resolve, so a stage does not fail
# halfway through with an opaque auth error.
require_identity() {
  local who
  if ! who="$(aws sts get-caller-identity --query Arn --output text --region "$REGION" 2>/dev/null)"; then
    die "AWS credentials are not usable for region $REGION (aws sts get-caller-identity failed). Configure credentials first."
  fi
  log "AWS identity: $who"
}

# require_identity_context — the project/environment/region tuple that
# names every resource. Required by every command except help.
require_identity_context() {
  [[ -n "$PROJECT" ]]     || die "missing --project (or PROJECT env)"
  [[ -n "$ENVIRONMENT" ]] || die "missing --environment (or ENVIRONMENT env)"
  [[ -n "$REGION" ]]      || die "missing --region (or AWS_REGION env)"
}

# ---------------------------------------------------------------------------
# Bootstrap-layer helpers (local terraform).
# ---------------------------------------------------------------------------

# bootstrap_output <name> — read one output from the bootstrap layer's
# local state. Empty string when the output is absent (layer not applied).
bootstrap_output() {
  terraform -chdir="$BOOTSTRAP_DIR" output -raw "$1" 2>/dev/null || true
}

# bootstrap_output_json <name> — read one output as JSON (for maps).
bootstrap_output_json() {
  terraform -chdir="$BOOTSTRAP_DIR" output -json "$1" 2>/dev/null || true
}

# ensure_bootstrap_init — `terraform init` is itself idempotent; run it so
# a fresh checkout works, and re-run cheaply thereafter.
ensure_bootstrap_init() {
  log "terraform init (bootstrap layer)"
  terraform -chdir="$BOOTSTRAP_DIR" init -input=false >&2
}

# ---------------------------------------------------------------------------
# Pipeline helpers.
# ---------------------------------------------------------------------------

# pipeline_name <frontend|backend|iac> — resolve the concrete CodePipeline
# name from the bootstrap outputs (authoritative), falling back to the
# documented "<project>-<env>-<which>" naming if outputs are unavailable.
pipeline_name() {
  local which="$1" names
  names="$(bootstrap_output_json pipeline_names)"
  if [[ -n "$names" && "$names" != "null" ]]; then
    local resolved
    resolved="$(printf '%s' "$names" | jq -r --arg k "$which" '.[$k] // empty')"
    if [[ -n "$resolved" ]]; then printf '%s' "$resolved"; return 0; fi
  fi
  printf '%s-%s-%s' "$PROJECT" "$ENVIRONMENT" "$which"
}

# source_bucket <frontend|backend|iac> — resolve the source bucket name
# from the bootstrap outputs. Requires the bootstrap layer to be applied.
source_bucket() {
  local which="$1" names bucket
  names="$(bootstrap_output_json source_bucket_names)"
  [[ -n "$names" && "$names" != "null" ]] \
    || die "cannot resolve source buckets: apply the bootstrap layer first (scripts/deploy.sh bootstrap)"
  bucket="$(printf '%s' "$names" | jq -r --arg k "$which" '.[$k] // empty')"
  [[ -n "$bucket" ]] || die "no source bucket for '$which' in bootstrap outputs"
  printf '%s' "$bucket"
}

# latest_execution_id <pipeline> — id of the most recent pipeline run.
latest_execution_id() {
  aws codepipeline list-pipeline-executions \
    --region "$REGION" --pipeline-name "$1" --max-results 1 \
    --query 'pipelineExecutionSummaries[0].pipelineExecutionId' \
    --output text 2>/dev/null || true
}

# execution_status <pipeline> <execution-id>
execution_status() {
  aws codepipeline get-pipeline-execution \
    --region "$REGION" --pipeline-name "$1" --pipeline-execution-id "$2" \
    --query 'pipelineExecution.status' --output text 2>/dev/null || true
}

# approval_token <pipeline> — if the ManualApproval action is currently
# waiting, print "<stageName> <actionName> <token>"; empty otherwise.
approval_token() {
  aws codepipeline get-pipeline-state \
    --region "$REGION" --name "$1" \
    --query "stageStates[?stageName=='ManualApproval'].actionStates[0].latestExecution.token | [0]" \
    --output text 2>/dev/null || true
}

# push_and_track <frontend|backend|iac> — push the source, then (unless
# --no-wait) capture the freshly started execution id.
#
# push-source.sh uploads source.zip, and the bootstrap EventBridge rule
# starts the pipeline. We snapshot the current execution id, push, then
# wait for a NEW execution id to appear so we track the run WE triggered
# rather than a stale one.
push_and_track() {
  local which="$1" bucket before after waited=0
  bucket="$(source_bucket "$which")"

  before="$(latest_execution_id "$(pipeline_name "$which")")"
  log "pushing $which source to s3://$bucket (starts the $which pipeline)"
  "$PUSH_SOURCE" "$which" "$bucket" >&2

  if [[ "$NO_WAIT" -eq 1 ]]; then
    ok "$which source pushed (--no-wait: not polling the pipeline)"
    return 0
  fi

  local pname; pname="$(pipeline_name "$which")"
  log "waiting for a new $pname execution to start"
  while [[ "$waited" -lt 120 ]]; do
    after="$(latest_execution_id "$pname")"
    if [[ -n "$after" && "$after" != "None" && "$after" != "$before" ]]; then
      printf '%s' "$after"
      return 0
    fi
    sleep 5; waited=$((waited + 5))
  done
  die "timed out waiting for the $pname pipeline to start after the source push"
}

# wait_for_pipeline <which> <execution-id> — poll until the execution
# reaches ManualApproval (handle it) and then terminal Deploy status.
# Returns 0 on Succeeded, 1 otherwise.
wait_for_pipeline() {
  local which="$1" exec_id="$2" pname status token approved=0 waited=0
  pname="$(pipeline_name "$which")"
  log "tracking $pname execution $exec_id (timeout ${PIPELINE_TIMEOUT}s)"

  while :; do
    status="$(execution_status "$pname" "$exec_id")"
    case "$status" in
      Succeeded) ok "$pname execution $exec_id Succeeded"; return 0 ;;
      Failed|Cancelled|Superseded|Stopped)
        err "$pname execution $exec_id ended: $status"
        err "inspect it in the CodePipeline console for the failing stage's logs"
        return 1 ;;
    esac

    # Handle the ManualApproval gate when the action is waiting.
    if [[ "$approved" -eq 0 ]]; then
      token="$(approval_token "$pname")"
      if [[ -n "$token" && "$token" != "None" ]]; then
        step "$pname is waiting at ManualApproval"
        log "review the BuildAndPlan output for $pname in the CodePipeline console before approving"
        if [[ "$AUTO_APPROVE" -eq 1 ]]; then
          log "--auto-approve: approving the gate"
          aws codepipeline put-approval-result \
            --region "$REGION" --pipeline-name "$pname" \
            --stage-name ManualApproval --action-name ManualApproval \
            --result 'summary=Approved by scripts/deploy.sh --auto-approve,status=Approved' \
            --token "$token" >&2
          approved=1
        else
          if prompt_yes_no "Approve the $pname deployment now?"; then
            aws codepipeline put-approval-result \
              --region "$REGION" --pipeline-name "$pname" \
              --stage-name ManualApproval --action-name ManualApproval \
              --result 'summary=Approved by scripts/deploy.sh,status=Approved' \
              --token "$token" >&2
            approved=1
          else
            aws codepipeline put-approval-result \
              --region "$REGION" --pipeline-name "$pname" \
              --stage-name ManualApproval --action-name ManualApproval \
              --result 'summary=Rejected by scripts/deploy.sh,status=Rejected' \
              --token "$token" >&2
            err "approval rejected; the $pname execution will stop before Deploy"
            return 1
          fi
        fi
      fi
    fi

    if [[ "$waited" -ge "$PIPELINE_TIMEOUT" ]]; then
      err "timed out after ${PIPELINE_TIMEOUT}s waiting for $pname execution $exec_id (last status: ${status:-unknown})"
      err "the pipeline may still be running; re-run with a larger --timeout or watch the console"
      return 1
    fi
    sleep 15; waited=$((waited + 15))
  done
}

# prompt_yes_no <question> — interactive y/N; defaults to No. Returns 0 for
# yes. Auto-answers No when stdin is not a terminal (safe default).
prompt_yes_no() {
  local reply
  if [[ ! -t 0 ]]; then
    warn "non-interactive stdin; declining: $1 (use --auto-approve to approve)"
    return 1
  fi
  printf '%s%s [y/N]%s ' "$_C_BOLD" "$1" "$_C_RESET" >&2
  read -r reply
  [[ "$reply" =~ ^[Yy]$ || "$reply" =~ ^[Yy][Ee][Ss]$ ]]
}

# drive_pipeline <which> — push source and, unless --no-wait, wait through
# approval and Deploy. Fails the command on a failed/rejected run.
drive_pipeline() {
  local which="$1" exec_id
  exec_id="$(push_and_track "$which")"
  [[ "$NO_WAIT" -eq 1 ]] && return 0
  wait_for_pipeline "$which" "$exec_id" \
    || die "$which pipeline did not complete successfully"
}

# ---------------------------------------------------------------------------
# App-layer outputs.
# ---------------------------------------------------------------------------

# state_bucket — the app-layer state bucket from bootstrap outputs.
state_bucket() {
  local b; b="$(bootstrap_output state_bucket_name)"
  [[ -n "$b" ]] || die "cannot resolve the state bucket: apply the bootstrap layer first"
  printf '%s' "$b"
}

# fetch_app_outputs <dest-file> — download the app layer's exported
# outputs (written by the IaC pipeline's deploy stage). Fails clearly when
# the object does not exist yet (app layer never applied).
fetch_app_outputs() {
  local dest="$1" bucket; bucket="$(state_bucket)"
  if ! aws s3 cp "s3://$bucket/app-outputs/latest.json" "$dest" --region "$REGION" >&2; then
    die "app outputs not found at s3://$bucket/app-outputs/latest.json — run 'infra' first (the IaC pipeline exports them after apply)"
  fi
}

# app_output <name> — one value from the exported app outputs.
app_output() {
  local tmp; tmp="$(mktemp)"
  fetch_app_outputs "$tmp"
  local val; val="$(jq -r --arg k "$1" '.[$k].value // empty' "$tmp")"
  rm -f "$tmp"
  printf '%s' "$val"
}

# ---------------------------------------------------------------------------
# Commands.
# ---------------------------------------------------------------------------

# vapid_param_name — the SSM parameter name for the VAPID private key:
# the --vapid-param-name override, else the documented default that matches
# the tfvars convention (/<env>/notifier/vapid-private-key).
vapid_param_name() {
  if [[ -n "$VAPID_PARAM_NAME" ]]; then
    printf '%s' "$VAPID_PARAM_NAME"
  else
    printf '/%s/notifier/vapid-private-key' "$ENVIRONMENT"
  fi
}

# ssm_parameter_exists <name> — 0 if the SSM parameter exists, non-zero
# otherwise. Read-only; the existence check is what makes generation
# create-if-absent (idempotent).
ssm_parameter_exists() {
  aws ssm get-parameter --region "$REGION" --name "$1" >/dev/null 2>&1
}

# ssm_public_key_note <name> — best-effort read of the public key stashed
# in the parameter's Description on first creation (see cmd_vapid). Empty
# when absent. Never reads the SecureString Value.
ssm_public_key_note() {
  aws ssm get-parameter --region "$REGION" --name "$1" \
    --query 'Parameter.Description' --output text 2>/dev/null \
    | sed -n 's/^vapid_public_key=//p'
}

# b64url — read stdin bytes, emit URL-safe base64 without padding.
b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

# generate_vapid_keypair <priv-out-var> <pub-out-var> — generate a P-256
# key pair and set the named variables to the base64url private scalar
# (32 bytes) and public point (uncompressed, 65 bytes) — exactly the VAPID
# formats pywebpush/web-push expect. Writes the PEM to a mode-600 temp file
# that is removed before returning; the private material never touches
# stdout and is never logged.
generate_vapid_keypair() {
  local __priv_ref="$1" __pub_ref="$2"
  local kd pem priv_hex pub_hex priv_b64 pub_b64
  kd="$(mktemp -d)"; chmod 700 "$kd"
  pem="$kd/vapid.pem"
  ( umask 077; openssl ecparam -name prime256v1 -genkey -noout -out "$pem" 2>/dev/null )

  # Public: uncompressed EC point (0x04 || X || Y), last 65 DER bytes.
  pub_hex="$(openssl ec -in "$pem" -pubout -conv_form uncompressed -outform DER 2>/dev/null \
    | tail -c 65 | xxd -p -c 200)"
  # Private: the 32-byte scalar OCTET STRING in the EC private-key DER.
  priv_hex="$(openssl ec -in "$pem" -outform DER 2>/dev/null \
    | openssl asn1parse -inform DER 2>/dev/null \
    | awk '/OCTET STRING/ {sub(/.*\[HEX DUMP\]:/, ""); print; exit}')"

  rm -rf "$kd"

  if [[ "$(( ${#pub_hex} / 2 ))" -ne 65 || "${pub_hex:0:2}" != "04" ]]; then
    die "VAPID public key derivation failed (expected a 65-byte uncompressed point)"
  fi
  if [[ "$(( ${#priv_hex} / 2 ))" -ne 32 ]]; then
    die "VAPID private key derivation failed (expected a 32-byte scalar)"
  fi

  pub_b64="$(printf '%s' "$pub_hex" | xxd -r -p | b64url)"
  priv_b64="$(printf '%s' "$priv_hex" | xxd -r -p | b64url)"
  printf -v "$__priv_ref" '%s' "$priv_b64"
  printf -v "$__pub_ref"  '%s' "$pub_b64"
}

cmd_vapid() {
  step "VAPID key pair — generate-if-absent, store private key as SSM SecureString"
  require_tools aws openssl xxd
  require_identity_context
  require_identity

  local param; param="$(vapid_param_name)"
  log "VAPID private-key parameter: $param (region $REGION)"

  # Idempotency + safety guard: an existing key is NEVER regenerated or
  # overwritten. Rotating the key invalidates every live browser
  # subscription (the public key browsers subscribed with would no longer
  # match), so rotation must be a deliberate, separate operation.
  if ssm_parameter_exists "$param"; then
    ok "VAPID private key already exists at $param — leaving it untouched (idempotent)"
    local note; note="$(ssm_public_key_note "$param")"
    if [[ -n "$note" && "$note" != "None" ]]; then
      log "recorded public key (set vapid_public_key to this in envs/${ENVIRONMENT}.tfvars):"
      printf '  vapid_public_key = "%s"\n' "$note" >&2
    else
      warn "no public key was recorded on this parameter; if you no longer have"
      warn "the public half, you must rotate the key pair deliberately (this"
      warn "invalidates existing subscriptions) — see the README VAPID section."
    fi
    return 0
  fi

  local priv_b64="" pub_b64=""
  generate_vapid_keypair priv_b64 pub_b64
  log "generated a new P-256 VAPID key pair"

  # Store the private key as a SecureString. --description carries the
  # PUBLIC key only (never sensitive) so a later run can re-print it for
  # the tfvars. Overwrite is intentionally NOT passed: put-parameter fails
  # if the name was created between the check and here, so a race can never
  # clobber an existing key.
  local subject_tag=""
  [[ -n "$VAPID_SUBJECT" ]] && subject_tag=" subject=${VAPID_SUBJECT}"
  aws ssm put-parameter \
    --region "$REGION" \
    --name "$param" \
    --type SecureString \
    --description "vapid_public_key=${pub_b64}${subject_tag}" \
    --value "$priv_b64" >/dev/null
  unset priv_b64
  ok "stored VAPID private key (SecureString) at $param"

  step "ACTION REQUIRED — record the public key in your tfvars"
  log "The private key is in SSM. Set these in infrastructure/app/envs/${ENVIRONMENT}.tfvars"
  log "(commit before running 'infra' — the iac archive is HEAD-only):"
  printf '\n  vapid_private_key_parameter_name = "%s"\n' "$param" >&2
  printf   '  vapid_public_key                 = "%s"\n\n' "$pub_b64" >&2
  warn "the public key is printed once here and recorded in the parameter's"
  warn "description; the private key is never printed."
}

cmd_bootstrap() {
  step "Step 1 — apply the bootstrap layer (local terraform, idempotent)"
  require_tools terraform aws jq
  require_identity_context
  require_identity
  ensure_bootstrap_init

  # Assemble the bootstrap variables. --with-vapid drives the
  # TERRAFORM-NATIVE VAPID path: the bootstrap layer's vapid module (an
  # `external` data source, create-if-absent) generates the key pair if
  # absent, stores the private key as an SSM SecureString, and exports the
  # public key — all within `terraform apply`, with no key material in
  # state. Off by default so key creation stays opt-in.
  local -a tf_vars=(
    -var "project_name=$PROJECT"
    -var "environment=$ENVIRONMENT"
    -var "aws_region=$REGION"
  )
  if [[ "$WITH_VAPID" -eq 1 ]]; then
    require_tools openssl xxd
    log "--with-vapid: enabling the Terraform-native VAPID module (create_vapid_key=true)"
    tf_vars+=(-var "create_vapid_key=true")
    [[ -n "$VAPID_SUBJECT" ]]    && tf_vars+=(-var "vapid_subject=$VAPID_SUBJECT")
    [[ -n "$VAPID_PARAM_NAME" ]] && tf_vars+=(-var "vapid_private_key_parameter_name=$VAPID_PARAM_NAME")
  fi

  log "terraform apply (bootstrap) — converges to declared state; a re-run with no drift is a no-op"
  terraform -chdir="$BOOTSTRAP_DIR" apply "${tf_vars[@]}" >&2
  ok "bootstrap layer applied"
  log "source buckets: $(bootstrap_output_json source_bucket_names)"
  log "pipelines: $(bootstrap_output_json pipeline_names)"

  if [[ "$WITH_VAPID" -eq 1 ]]; then
    local vpub vname
    vpub="$(bootstrap_output vapid_public_key)"
    vname="$(bootstrap_output vapid_private_key_parameter_name)"
    step "VAPID key ready — record it in your app tfvars"
    log "Set these in infrastructure/app/envs/${ENVIRONMENT}.tfvars (commit before 'infra'):"
    printf '\n  vapid_private_key_parameter_name = "%s"\n' "$vname" >&2
    printf   '  vapid_public_key                 = "%s"\n\n' "$vpub" >&2
    warn "the private key was stored in SSM by Terraform and never entered Terraform state"
  fi
}

cmd_infra() {
  step "Steps 2-3 — push iac source and drive the IaC pipeline (applies the app layer)"
  require_tools terraform aws jq
  require_identity_context
  require_identity
  warn "the app-layer tfvars must be committed at infrastructure/app/envs/$ENVIRONMENT.tfvars"
  warn "(the archive is HEAD-only in a git tree — commit it before running this)"
  drive_pipeline iac
  ok "app layer applied by the IaC pipeline; outputs exported to s3://$(state_bucket)/app-outputs/latest.json"
}

cmd_backend() {
  step "Step 4 — push backend source and drive the backend pipeline (image -> ECR -> ECS)"
  require_tools terraform aws jq
  require_identity_context
  require_identity
  drive_pipeline backend
  ok "backend image built and rolled onto ECS"
  warn "copy the deployed image URI into container_image in infrastructure/app/envs/$ENVIRONMENT.tfvars"
  warn "(and commit) so the next IaC apply does not roll the service back to a stale image"
}

cmd_wire_frontend() {
  step "Step 5 — re-apply bootstrap with the frontend bucket + CloudFront id (two-phase, idempotent)"
  require_tools terraform aws jq
  require_identity_context
  require_identity
  ensure_bootstrap_init

  local fe_bucket cf_id
  fe_bucket="$(app_output frontend_bucket_name)"
  cf_id="$(app_output cloudfront_distribution_id)"
  [[ -n "$fe_bucket" ]] || die "frontend_bucket_name missing from app outputs — run 'infra' first"
  [[ -n "$cf_id" ]]     || die "cloudfront_distribution_id missing from app outputs — run 'infra' first"
  log "wiring frontend_bucket_name=$fe_bucket cloudfront_distribution_id=$cf_id"

  terraform -chdir="$BOOTSTRAP_DIR" apply \
    -var "project_name=$PROJECT" \
    -var "environment=$ENVIRONMENT" \
    -var "aws_region=$REGION" \
    -var "frontend_bucket_name=$fe_bucket" \
    -var "cloudfront_distribution_id=$cf_id" >&2
  ok "bootstrap re-applied; frontend pipeline deploy stage now has its targets"
}

cmd_frontend() {
  step "Step 6 — push frontend source and drive the frontend pipeline (sync -> invalidation)"
  require_tools terraform aws jq
  require_identity_context
  require_identity
  drive_pipeline frontend
  local url; url="$(app_output portal_url)"
  ok "frontend deployed"
  [[ -n "$url" ]] && log "portal URL: $url"
}

cmd_create_user() {
  step "Create a Cognito engineer account (guarded; idempotent)"
  require_tools aws jq
  require_identity_context
  require_identity
  [[ -n "$USERNAME" ]] || die "create-user needs --username <engineer@example.com>"

  local pool_id; pool_id="$(app_output cognito_user_pool_id)"
  [[ -n "$pool_id" ]] || die "cognito_user_pool_id missing from app outputs — run 'infra' first"

  # Idempotency guard: skip when the account already exists so a re-run
  # never overwrites a password or re-sends an invite.
  if aws cognito-idp admin-get-user \
      --region "$REGION" --user-pool-id "$pool_id" --username "$USERNAME" \
      >/dev/null 2>&1; then
    ok "user $USERNAME already exists in pool $pool_id — skipping (idempotent)"
    return 0
  fi

  # Read the temp password without echoing when not supplied on the CLI.
  if [[ -z "$TEMP_PASSWORD" ]]; then
    if [[ -t 0 ]]; then
      printf '%sTemporary password for %s (12+ chars, all 4 classes): %s' \
        "$_C_BOLD" "$USERNAME" "$_C_RESET" >&2
      read -rs TEMP_PASSWORD; printf '\n' >&2
    else
      die "no --temp-password and stdin is not a terminal"
    fi
  fi
  [[ -n "$TEMP_PASSWORD" ]] || die "temporary password must not be empty"

  aws cognito-idp admin-create-user \
    --region "$REGION" --user-pool-id "$pool_id" \
    --username "$USERNAME" \
    --user-attributes "Name=email,Value=$USERNAME" "Name=email_verified,Value=true" \
    --temporary-password "$TEMP_PASSWORD" >/dev/null
  ok "created user $USERNAME (hosted UI forces a password change on first sign-in)"
}

cmd_smoke() {
  step "Step 7 — post-deploy smoke tests (read-only)"
  require_tools aws jq
  require_identity_context
  [[ -x "$SMOKE_RUNNER" ]] || die "smoke runner not found or not executable: $SMOKE_RUNNER"

  local tmp="" outputs="$OUTPUTS_JSON"
  if [[ -z "$outputs" ]]; then
    require_identity
    tmp="$(mktemp)"; fetch_app_outputs "$tmp"; outputs="$tmp"
  fi
  local rc=0
  "$SMOKE_RUNNER" --outputs-json "$outputs" >&2 || rc=$?
  [[ -n "$tmp" ]] && rm -f "$tmp"
  if [[ "$rc" -eq 0 ]]; then ok "smoke checks passed or skipped gracefully"; else
    die "smoke checks reported failures (exit $rc)"; fi
}

cmd_outputs() {
  require_tools aws jq
  require_identity_context
  require_identity
  local tmp; tmp="$(mktemp)"
  fetch_app_outputs "$tmp"
  jq . "$tmp"
  rm -f "$tmp"
}

cmd_all() {
  if [[ "$NO_WAIT" -eq 1 ]]; then
    die "--no-wait cannot be combined with 'all' (each stage depends on the previous one completing)"
  fi
  step "Full deployment: bootstrap -> infra -> backend -> wire-frontend -> frontend -> smoke"
  cmd_bootstrap
  cmd_infra
  cmd_backend
  cmd_wire_frontend
  cmd_frontend
  if [[ -n "$USERNAME" ]]; then cmd_create_user; fi
  cmd_smoke
  ok "full deployment complete"
  local url; url="$(app_output portal_url 2>/dev/null || true)"
  [[ -n "$url" ]] && log "portal URL: $url"
}

# ---------------------------------------------------------------------------
# Dispatch.
# ---------------------------------------------------------------------------
case "$COMMAND" in
  all)            cmd_all ;;
  bootstrap)      cmd_bootstrap ;;
  vapid)          cmd_vapid ;;
  infra)          cmd_infra ;;
  backend)        cmd_backend ;;
  wire-frontend)  cmd_wire_frontend ;;
  frontend)       cmd_frontend ;;
  create-user)    cmd_create_user ;;
  smoke)          cmd_smoke ;;
  outputs)        cmd_outputs ;;
  *)
    err "unknown command: $COMMAND"
    usage
    exit 2
    ;;
esac
