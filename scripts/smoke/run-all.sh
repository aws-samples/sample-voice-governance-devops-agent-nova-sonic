#!/usr/bin/env bash
# run-all.sh — post-deploy smoke-test runner (spec task 13.4).
#
# Runs the numbered checks 01–05 in order against a DEPLOYED environment
# and aggregates their outcomes; exits non-zero if any check fails.
# Individual checks SKIP gracefully (exit 0 with a [SKIP] message) when
# their inputs are absent, so a partial configuration still runs
# everything it can.
#
# Checks:
#   01-guardrail-blocks-destructive.sh  (Req 4.2)
#   02-appsync-auth.sh                  (Req 7.4, 7.8)
#   03-alb-direct-rejected.sh           (Req 12.2, 7.5)
#   04-edge-https-and-s3.sh             (Req 12.2, 13.7)
#   05-waf-blocks-bad-input.sh          (Req 11.4)
#
# Usage:
#   ./run-all.sh [--outputs-json <file>]
#
# Configuration: export the env vars listed in common.sh, or capture the
# app layer's outputs once and pass them along:
#   terraform -chdir=infrastructure/app output -json > /tmp/outputs.json
#   ./run-all.sh --outputs-json /tmp/outputs.json
# COGNITO_TOKEN (for check 02's authorized half) is always env-only.
#
# All checks are read-only probes and ApplyGuardrail evaluations; nothing
# mutates AWS resources. Operator tooling — not wired into CI.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

# Validate the CLI surface (also implements --help) and let checks
# inherit any --outputs-json via pass-through of the original args.
load_config "$@"

CHECKS=(
  "01-guardrail-blocks-destructive.sh"
  "02-appsync-auth.sh"
  "03-alb-direct-rejected.sh"
  "04-edge-https-and-s3.sh"
  "05-waf-blocks-bad-input.sh"
)

failed_checks=()
for check in "${CHECKS[@]}"; do
  printf '\n'
  if "$SCRIPT_DIR/$check" "$@"; then
    : # exit 0 covers both pass and graceful skip
  else
    failed_checks+=("$check")
  fi
done

printf '\n'
banner "smoke-test summary"
if [[ ${#failed_checks[@]} -eq 0 ]]; then
  pass "all ${#CHECKS[@]} checks passed or skipped gracefully"
  exit 0
fi
for check in "${failed_checks[@]}"; do
  fail "$check"
done
printf '%d of %d checks failed\n' "${#failed_checks[@]}" "${#CHECKS[@]}" >&2
exit 1
