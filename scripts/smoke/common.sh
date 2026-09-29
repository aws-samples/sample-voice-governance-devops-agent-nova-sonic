#!/usr/bin/env bash
# common.sh — shared helpers for the post-deploy smoke tests (spec task 13.4).
#
# Sourced (not executed) by every scripts/smoke/*.sh check. Provides:
#   - configuration loading from environment variables or a single
#     `--outputs-json <file>` argument pointing at the app layer's
#     `terraform output -json` capture,
#   - `require <cmd>...` guarding on CLI tool presence (jq, curl, aws),
#   - colored pass / fail / skip reporting with an exit-code aggregate.
#
# These smoke tests hit a LIVE deployed environment. Every probe is
# read-only: HTTP(S) GETs, WebSocket handshakes, and ApplyGuardrail
# evaluations. Nothing here mutates any AWS resource.
#
# Configuration keys (environment variable ← terraform output name):
#   GUARDRAIL_ID        ← guardrail_id
#   GUARDRAIL_VERSION   ← guardrail_version
#   APPSYNC_HTTP_ENDPOINT      ← appsync_events_http_endpoint
#   APPSYNC_REALTIME_ENDPOINT  ← appsync_events_realtime_endpoint
#   ALB_DNS_NAME        ← alb_dns_name
#   CLOUDFRONT_DOMAIN   ← cloudfront_domain_name
#   FRONTEND_BUCKET     ← frontend_bucket_name
#   AWS_REGION          ← aws_region
#   COGNITO_TOKEN       ← (never a terraform output; supplied by the
#                          operator for the authorized AppSync check)
#
# Environment variables always win over --outputs-json values, so single
# keys can be overridden without editing the outputs capture.
#
# Secrets hygiene: COGNITO_TOKEN (and any other sensitive value) is never
# echoed; reporting only ever names keys.

set -euo pipefail

# ---------------------------------------------------------------------------
# Colored reporting. Colors only when stdout is a terminal.
# ---------------------------------------------------------------------------

if [[ -t 1 ]]; then
  _C_GREEN=$'\033[32m'
  _C_RED=$'\033[31m'
  _C_YELLOW=$'\033[33m'
  _C_BOLD=$'\033[1m'
  _C_RESET=$'\033[0m'
else
  _C_GREEN=''
  _C_RED=''
  _C_YELLOW=''
  _C_BOLD=''
  _C_RESET=''
fi

# Aggregate counters. `report_summary` turns them into the exit code.
SMOKE_PASS_COUNT=0
SMOKE_FAIL_COUNT=0
SMOKE_SKIP_COUNT=0

# pass <message> — record and print a passing assertion.
pass() {
  SMOKE_PASS_COUNT=$((SMOKE_PASS_COUNT + 1))
  printf '%s[PASS]%s %s\n' "$_C_GREEN" "$_C_RESET" "$*"
}

# fail <message> — record and print a failing assertion (does not exit;
# the summary aggregates so one failure never hides the others).
fail() {
  SMOKE_FAIL_COUNT=$((SMOKE_FAIL_COUNT + 1))
  printf '%s[FAIL]%s %s\n' "$_C_RED" "$_C_RESET" "$*"
}

# skip_note <message> — print a SKIP line without terminating the script
# (for skipping one half of a check while the rest still runs).
skip_note() {
  SMOKE_SKIP_COUNT=$((SMOKE_SKIP_COUNT + 1))
  printf '%s[SKIP]%s %s\n' "$_C_YELLOW" "$_C_RESET" "$*"
}

# skip_all <message> — graceful whole-script skip: prints and exits 0 so
# a missing input never counts as a failure (task 13.4 mechanics).
skip_all() {
  printf '%s[SKIP]%s %s\n' "$_C_YELLOW" "$_C_RESET" "$*"
  exit 0
}

# banner <title> — section header for a check script.
banner() {
  printf '%s== %s ==%s\n' "$_C_BOLD" "$*" "$_C_RESET"
}

# report_summary — print the aggregate and exit non-zero on any failure.
# Call as the last statement of every check script.
report_summary() {
  printf '%s-- %d passed, %d failed, %d skipped --%s\n' \
    "$_C_BOLD" "$SMOKE_PASS_COUNT" "$SMOKE_FAIL_COUNT" "$SMOKE_SKIP_COUNT" "$_C_RESET"
  if [[ "$SMOKE_FAIL_COUNT" -gt 0 ]]; then
    exit 1
  fi
  exit 0
}

# ---------------------------------------------------------------------------
# Tool presence.
# ---------------------------------------------------------------------------

# require <cmd>... — verify every named CLI tool is on PATH; exits 2 with
# a clear message otherwise (a missing tool is an operator-setup error,
# not a smoke-test failure or skip).
require() {
  local missing=0 cmd
  for cmd in "$@"; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      printf '%s[ERROR]%s required tool not found on PATH: %s\n' \
        "$_C_RED" "$_C_RESET" "$cmd" >&2
      missing=1
    fi
  done
  if [[ "$missing" -ne 0 ]]; then
    exit 2
  fi
}

# ---------------------------------------------------------------------------
# Configuration loading.
# ---------------------------------------------------------------------------

# _load_outputs_json <file> — populate the standard config variables from a
# `terraform output -json` capture, without overriding values already set
# in the environment. Requires jq.
_load_outputs_json() {
  local file=$1
  if [[ ! -r "$file" ]]; then
    printf '%s[ERROR]%s outputs JSON file not readable: %s\n' \
      "$_C_RED" "$_C_RESET" "$file" >&2
    exit 2
  fi
  require jq

  # _tf_out <output-name> — extract one output's .value ('' when absent).
  _tf_out() {
    jq -r --arg k "$1" '.[$k].value // empty' "$file"
  }

  : "${GUARDRAIL_ID:=$(_tf_out guardrail_id)}"
  : "${GUARDRAIL_VERSION:=$(_tf_out guardrail_version)}"
  : "${APPSYNC_HTTP_ENDPOINT:=$(_tf_out appsync_events_http_endpoint)}"
  : "${APPSYNC_REALTIME_ENDPOINT:=$(_tf_out appsync_events_realtime_endpoint)}"
  : "${ALB_DNS_NAME:=$(_tf_out alb_dns_name)}"
  : "${CLOUDFRONT_DOMAIN:=$(_tf_out cloudfront_domain_name)}"
  : "${FRONTEND_BUCKET:=$(_tf_out frontend_bucket_name)}"
  : "${AWS_REGION:=$(_tf_out aws_region)}"
}

# load_config "$@" — parse the standard smoke-test CLI surface:
#   [--outputs-json <file>]   load config from a terraform output -json file
#   [-h|--help]               print the calling script's header comment
# Everything can also arrive purely via environment variables. This
# function never fails on MISSING config — each check decides whether a
# missing key is a skip (skip_all) so partial configs still run the
# checks they can.
load_config() {
  while [[ $# -gt 0 ]]; do
    case $1 in
      --outputs-json)
        if [[ $# -lt 2 ]]; then
          printf '%s[ERROR]%s --outputs-json requires a file argument\n' \
            "$_C_RED" "$_C_RESET" >&2
          exit 2
        fi
        _load_outputs_json "$2"
        shift 2
        ;;
      -h|--help)
        # Print the header comment block of the calling script as usage.
        sed -n '2,/^[^#]/{/^#/p;}' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
      *)
        printf '%s[ERROR]%s unknown argument: %s (supported: --outputs-json <file>, --help)\n' \
          "$_C_RED" "$_C_RESET" "$1" >&2
        exit 2
        ;;
    esac
  done

  # Defaults for optional keys so `set -u` scripts can test them safely.
  : "${GUARDRAIL_ID:=}"
  : "${GUARDRAIL_VERSION:=}"
  : "${APPSYNC_HTTP_ENDPOINT:=}"
  : "${APPSYNC_REALTIME_ENDPOINT:=}"
  : "${ALB_DNS_NAME:=}"
  : "${CLOUDFRONT_DOMAIN:=}"
  : "${FRONTEND_BUCKET:=}"
  : "${AWS_REGION:=}"
  : "${COGNITO_TOKEN:=}"
}
