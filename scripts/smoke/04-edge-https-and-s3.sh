#!/usr/bin/env bash
# 04-edge-https-and-s3.sh — edge HTTPS redirect + S3 OAC smoke test (Req 12.2, 13.7).
#
# Validates: Requirements 12.2 (CloudFront redirects HTTP requests to
# HTTPS) and 13.7 (the frontend bucket policy allows reads only to the
# CloudFront distribution via OAC and denies all other principals).
#
# Probes:
#   - GET http://$CLOUDFRONT_DOMAIN/ → expect a 301/308 redirect whose
#     Location is the https:// portal URL (viewer-protocol-policy
#     redirect-to-https).
#   - Anonymous GET of index.html via the S3 REST endpoint → expect 403
#     (OAC-only bucket policy denies anonymous reads). Uses the regional
#     endpoint when AWS_REGION is set (avoids the global endpoint's
#     307 propagation redirects for young buckets), else the global one.
#
# Required inputs (env vars, or --outputs-json <terraform output -json file>):
#   CLOUDFRONT_DOMAIN (output: cloudfront_domain_name)
#   FRONTEND_BUCKET   (output: frontend_bucket_name)
#   AWS_REGION        (output: aws_region; optional, improves the S3 probe)
# Required tools: curl.
#
# Read-only: two GETs, one of which is expected to be denied; no mutation.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

load_config "$@"
require curl

banner "04 edge HTTPS redirect and S3 OAC lockdown (Req 12.2, 13.7)"

if [[ -z "$CLOUDFRONT_DOMAIN" && -z "$FRONTEND_BUCKET" ]]; then
  skip_all "CLOUDFRONT_DOMAIN / FRONTEND_BUCKET not set — provide env vars or --outputs-json"
fi

# --- HTTP → HTTPS redirect at CloudFront (Req 12.2). -----------------------
if [[ -z "$CLOUDFRONT_DOMAIN" ]]; then
  skip_note "CLOUDFRONT_DOMAIN not set — skipping the HTTP→HTTPS redirect check"
else
  result=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' --max-time 15 \
    "http://$CLOUDFRONT_DOMAIN/" 2>/dev/null) || true
  status=${result%% *}
  redirect_url=${result#* }
  if [[ ("$status" == "301" || "$status" == "308") && "$redirect_url" == https://* ]]; then
    pass "HTTP request redirected to HTTPS (HTTP $status → $redirect_url)"
  else
    fail "HTTP request not redirected to HTTPS (HTTP ${status:-000}, Location: ${redirect_url:-<none>})"
  fi
fi

# --- Direct S3 read denied (Req 13.7). --------------------------------------
if [[ -z "$FRONTEND_BUCKET" ]]; then
  skip_note "FRONTEND_BUCKET not set — skipping the direct-S3 403 check"
else
  if [[ -n "$AWS_REGION" ]]; then
    s3_url="https://$FRONTEND_BUCKET.s3.$AWS_REGION.amazonaws.com/index.html"
  else
    s3_url="https://$FRONTEND_BUCKET.s3.amazonaws.com/index.html"
  fi
  status=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$s3_url" 2>/dev/null) || true
  if [[ "$status" == "403" ]]; then
    pass "anonymous S3 read denied (HTTP 403 at $s3_url)"
  else
    fail "anonymous S3 read not denied (HTTP ${status:-000} at $s3_url, expected 403)"
  fi
fi

report_summary
