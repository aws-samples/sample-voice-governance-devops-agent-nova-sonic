#!/usr/bin/env bash
# 03-alb-direct-rejected.sh — direct-to-ALB rejection smoke test (Req 12.2, 7.5).
#
# Validates: Requirements 12.2/7.5 context — traffic that bypasses
# CloudFront never reaches the Voice_Service. The ALB listener's default
# action is a fixed 403; only requests carrying the x-origin-verify
# header value CloudFront injects are forwarded (design research
# finding 7). Defense in depth: the ALB security group only admits :80
# from the CloudFront origin-facing managed prefix list, so a direct
# probe may fail at the network level instead of receiving the 403 —
# both outcomes prove direct access is rejected and count as PASS.
#
# Probes (all expected rejected):
#   - GET http://$ALB_DNS_NAME/ws/voice with no origin-verify header
#   - GET http://$ALB_DNS_NAME/ws/voice with a BOGUS x-origin-verify value
#   - GET http://$ALB_DNS_NAME/healthz with no header (the fixed-response
#     default action gates everything, including health paths)
#
# Required inputs (env vars, or --outputs-json <terraform output -json file>):
#   ALB_DNS_NAME (output: alb_dns_name)
# Required tools: curl.
#
# Read-only: plain GETs that are expected to be rejected; no mutation.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

load_config "$@"
require curl

banner "03 direct ALB access rejected (Req 12.2, 7.5)"

if [[ -z "$ALB_DNS_NAME" ]]; then
  skip_all "ALB_DNS_NAME not set — provide env vars or --outputs-json"
fi

# probe <description> <url> [extra curl args...] — GET the URL and assert
# rejection: HTTP 403 (listener fixed response) or network-level failure
# (status 000: SG drop / timeout) both PASS; anything else FAILs.
probe() {
  local description=$1 url=$2 status
  shift 2
  status=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$@" "$url" 2>/dev/null) || true
  case $status in
    403)
      pass "$description → HTTP 403 (listener fixed-response)"
      ;;
    ''|000)
      pass "$description → no HTTP response (security group drop; network-level rejection)"
      ;;
    *)
      fail "$description → HTTP $status (expected 403 or network-level rejection)"
      ;;
  esac
}

probe "GET /ws/voice without origin-verify header" "http://$ALB_DNS_NAME/ws/voice"
probe "GET /ws/voice with bogus x-origin-verify value" "http://$ALB_DNS_NAME/ws/voice" \
  -H "x-origin-verify: smoke-test-bogus-value-0000000000000000"
probe "GET /healthz without origin-verify header" "http://$ALB_DNS_NAME/healthz"

report_summary
