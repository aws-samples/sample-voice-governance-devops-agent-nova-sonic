#!/usr/bin/env bash
# 02-appsync-auth.sh — AppSync Events channel auth smoke test (Req 7.4, 7.8).
#
# Validates: Requirements 7.4 (Events_Channel requires a valid
# Cognito-issued authorization on connect) and 7.8 (missing/expired/
# invalid authorization is rejected and no events are delivered).
#
# Handshake-level checks only (no full aws-appsync-event-ws protocol
# implementation): performs raw HTTP/1.1 WebSocket upgrade requests with
# curl against the Events API realtime endpoint and asserts the HTTP
# status difference between unauthorized and authorized attempts.
#   - WITHOUT a token (bare `aws-appsync-event-ws` subprotocol, no
#     `header-` auth piece): expect rejection — any non-101 HTTP status
#     (401/403/4xx) or handshake failure.
#   - WITH a valid Cognito token (ID or access token via $COGNITO_TOKEN):
#     expect 101 Switching Protocols. Auth rides in the subprotocol list
#     as `header-<base64url({"Authorization": token, "host": <http-dns>})>`
#     per the AppSync Events realtime protocol.
#
# Obtaining a Cognito token interactively is OUT OF SCOPE here: sign in
# to the deployed portal and copy the ID token from the browser session,
# or use `aws cognito-idp initiate-auth` against a test user. Export it
# as COGNITO_TOKEN. Without it the authorized half is SKIPPED.
#
# Required inputs (env vars, or --outputs-json <terraform output -json file>):
#   APPSYNC_REALTIME_ENDPOINT (output: appsync_events_realtime_endpoint)
#   APPSYNC_HTTP_ENDPOINT     (output: appsync_events_http_endpoint;
#                              supplies the `host` field of the auth object)
#   COGNITO_TOKEN             (operator-supplied; authorized half only)
# Required tools: curl, jq. Optional: openssl (random Sec-WebSocket-Key;
# a fixed RFC 6455 sample key is used otherwise).
#
# Read-only: WebSocket handshakes only; nothing is published or mutated.
# The token is never echoed.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

load_config "$@"
require curl jq

banner "02 AppSync Events auth split (Req 7.4, 7.8)"

if [[ -z "$APPSYNC_REALTIME_ENDPOINT" ]]; then
  skip_all "APPSYNC_REALTIME_ENDPOINT not set — provide env vars or --outputs-json"
fi

# wss://<realtime-dns>/event/realtime → https:// URL for the raw upgrade.
REALTIME_HTTPS_URL=${APPSYNC_REALTIME_ENDPOINT/#wss:\/\//https://}

# Sec-WebSocket-Key: random when openssl is present, else the RFC 6455
# sample key (any valid 16-byte base64 value works for handshake tests).
if command -v openssl >/dev/null 2>&1; then
  WS_KEY=$(openssl rand -base64 16)
else
  WS_KEY="dGhlIHNhbXBsZSBub25jZQ=="
fi

# ws_handshake_status <subprotocol> — raw HTTP/1.1 WebSocket upgrade
# against the realtime endpoint; prints the HTTP status code ("000" on
# network-level failure). On a successful 101 the server holds the socket
# open, so --max-time bounds the probe; the status captured before the
# timeout is still reported.
ws_handshake_status() {
  local subprotocol=$1 headers_file status
  headers_file=$(mktemp)
  status=$(curl -s --include --no-buffer --http1.1 --max-time 15 \
    -o "$headers_file" -w '%{http_code}' \
    -H "Connection: Upgrade" \
    -H "Upgrade: websocket" \
    -H "Sec-WebSocket-Version: 13" \
    -H "Sec-WebSocket-Key: $WS_KEY" \
    -H "Sec-WebSocket-Protocol: $subprotocol" \
    "$REALTIME_HTTPS_URL" 2>/dev/null) || true
  # A timed-out-but-upgraded connection may report 000 via -w while the
  # 101 status line already arrived; fall back to the captured headers.
  if [[ "$status" == "000" ]] && grep -q " 101" "$headers_file" 2>/dev/null; then
    status="101"
  fi
  rm -f "$headers_file"
  printf '%s' "$status"
}

# --- Unauthorized: no auth subprotocol piece → must be rejected. -----------
status=$(ws_handshake_status "aws-appsync-event-ws")
if [[ "$status" == "101" ]]; then
  fail "unauthorized handshake was ACCEPTED (HTTP 101) — expected rejection"
else
  pass "unauthorized handshake rejected (HTTP $status, expected non-101)"
fi

# --- Authorized: Cognito token in the header- subprotocol → must upgrade. --
if [[ -z "$COGNITO_TOKEN" ]]; then
  skip_note "COGNITO_TOKEN not set — skipping the authorized-connection half (see header for how to obtain a token)"
else
  # host = DNS name of the Events API HTTP endpoint (strip scheme + path).
  http_dns=$(printf '%s' "$APPSYNC_HTTP_ENDPOINT" | sed -E 's#^[a-z]+://##; s#/.*$##')
  if [[ -z "$http_dns" ]]; then
    fail "cannot derive Events API host: APPSYNC_HTTP_ENDPOINT not set or malformed"
  else
    # base64url without padding (HTTP subprotocol tokens forbid '=', '+', '/').
    auth_b64=$(jq -cn --arg tok "$COGNITO_TOKEN" --arg host "$http_dns" \
      '{Authorization: $tok, host: $host}' \
      | base64 | tr -d '\n=' | tr '+/' '-_')
    status=$(ws_handshake_status "aws-appsync-event-ws, header-${auth_b64}")
    if [[ "$status" == "101" ]]; then
      pass "authorized handshake accepted (HTTP 101 Switching Protocols)"
    else
      fail "authorized handshake NOT accepted (HTTP $status) — token invalid/expired, or endpoint mismatch"
    fi
  fi
fi

report_summary
