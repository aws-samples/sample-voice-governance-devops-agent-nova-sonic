#!/usr/bin/env bash
# infrastructure/bootstrap/modules/vapid/vapid-ensure.sh
#
# Terraform `external` data source program (create-if-absent) for the Web
# Push VAPID key pair. Terraform passes a JSON object on stdin; this script
# MUST print a single flat JSON object of string→string on stdout and
# nothing else (the external-provider protocol). Any diagnostic goes to
# stderr; a non-zero exit fails the plan/apply.
#
# Contract
# --------
# stdin  (from the data source `query`):
#   {
#     "parameter_name": "/<env>/notifier/vapid-private-key",
#     "region":         "us-east-1",
#     "subject":        "mailto:ops@example.com"   # optional, informational
#   }
#
# stdout (consumed as the data source `result`):
#   {
#     "parameter_name": "<echoed name>",
#     "public_key":     "<base64url uncompressed P-256 point, 87 chars>",
#     "created":        "true" | "false"
#   }
#
# Behaviour
# ---------
#   * If the SSM parameter already exists, the key is LEFT UNTOUCHED
#     ("created":"false") and the public key is read back from the
#     parameter's Description (recorded on first creation). Regenerating a
#     VAPID key invalidates every live browser subscription, so rotation is
#     never automatic — it must be a deliberate, separate operation.
#   * If absent, a P-256 pair is generated; the private key (32-byte raw
#     scalar, base64url) is written as an SSM SecureString and the public
#     key (65-byte uncompressed point, base64url) is recorded in the
#     parameter Description and returned. The private key is NEVER printed,
#     logged, or returned to Terraform, so it never enters Terraform state.
#
# Idempotency & safety
# --------------------
#   * `put-parameter` is called WITHOUT --overwrite, so a parameter created
#     between the existence check and the write cannot be clobbered (the
#     write fails, the script re-reads the now-existing public key).
#   * The PEM lives only in a mode-700 temp dir created with umask 077 and
#     removed on exit.
#
# Requirements on PATH: bash, openssl, xxd, jq, aws.

set -euo pipefail

fail() { echo "vapid-ensure: $*" >&2; exit 1; }

for tool in openssl xxd jq aws; do
  command -v "$tool" >/dev/null 2>&1 || fail "required tool not found on PATH: $tool"
done

# --- Read and validate the query object from stdin. ------------------------
input="$(cat)"
parameter_name="$(printf '%s' "$input" | jq -r '.parameter_name // empty')"
region="$(printf '%s' "$input" | jq -r '.region // empty')"
subject="$(printf '%s' "$input" | jq -r '.subject // empty')"

[ -n "$parameter_name" ] || fail "query.parameter_name is required"
[ -n "$region" ]         || fail "query.region is required"
case "$parameter_name" in
  /*[!/]) : ;;  # starts with / and does not end with /
  *) fail "parameter_name must start with / and not end with / (got: $parameter_name)" ;;
esac

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

# emit <public_key> <created> — print the strict JSON result and exit 0.
emit() {
  jq -n \
    --arg parameter_name "$parameter_name" \
    --arg public_key "$1" \
    --arg created "$2" \
    '{parameter_name: $parameter_name, public_key: $public_key, created: $created}'
  exit 0
}

# read_recorded_public_key — echo the public key stored in the parameter's
# Description on first creation ("vapid_public_key=<key>[ subject=...]"),
# or empty if unavailable. Never reads the SecureString Value.
read_recorded_public_key() {
  aws ssm get-parameter --region "$region" --name "$parameter_name" \
    --query 'Parameter.Description' --output text 2>/dev/null \
    | sed -n 's/^vapid_public_key=\([A-Za-z0-9_-]*\).*/\1/p'
}

# --- Create-if-absent. -----------------------------------------------------
if aws ssm get-parameter --region "$region" --name "$parameter_name" >/dev/null 2>&1; then
  existing_pub="$(read_recorded_public_key || true)"
  [ -n "$existing_pub" ] || fail \
    "parameter $parameter_name exists but records no public key in its description; \
its public half is unknown to this automation. Set vapid_public_key manually, or \
rotate the key pair deliberately (this invalidates existing subscriptions)."
  emit "$existing_pub" "false"
fi

# Generate a fresh P-256 pair in an isolated, private temp dir.
keydir="$(mktemp -d)"; chmod 700 "$keydir"
trap 'rm -rf "$keydir"' EXIT
pem="$keydir/vapid.pem"
( umask 077; openssl ecparam -name prime256v1 -genkey -noout -out "$pem" 2>/dev/null ) \
  || fail "openssl key generation failed"

# Public: uncompressed EC point (0x04 || X || Y) — the last 65 DER bytes.
pub_hex="$(openssl ec -in "$pem" -pubout -conv_form uncompressed -outform DER 2>/dev/null \
  | tail -c 65 | xxd -p -c 200)"
[ "$(( ${#pub_hex} / 2 ))" -eq 65 ] && [ "${pub_hex:0:2}" = "04" ] \
  || fail "public key derivation failed (expected a 65-byte uncompressed point)"

# Private: the 32-byte scalar OCTET STRING in the EC private-key DER.
priv_hex="$(openssl ec -in "$pem" -outform DER 2>/dev/null \
  | openssl asn1parse -inform DER 2>/dev/null \
  | awk '/OCTET STRING/ {sub(/.*\[HEX DUMP\]:/, ""); print; exit}')"
[ "$(( ${#priv_hex} / 2 ))" -eq 32 ] || fail "private key derivation failed (expected a 32-byte scalar)"

public_key="$(printf '%s' "$pub_hex"  | xxd -r -p | b64url)"
private_key="$(printf '%s' "$priv_hex" | xxd -r -p | b64url)"

# Description carries the PUBLIC key only (never sensitive) so subsequent
# runs can read it back without touching the SecureString Value.
description="vapid_public_key=${public_key}"
[ -n "$subject" ] && description="${description} subject=${subject}"

# Write WITHOUT --overwrite: if the name was created concurrently, this
# fails and we fall back to reading the now-existing public key rather than
# clobbering someone else's key.
if aws ssm put-parameter \
     --region "$region" \
     --name "$parameter_name" \
     --type SecureString \
     --description "$description" \
     --value "$private_key" >/dev/null 2>&1; then
  # Scrub the private key from the shell as soon as it is persisted.
  private_key=""; unset private_key
  emit "$public_key" "true"
else
  private_key=""; unset private_key
  raced_pub="$(read_recorded_public_key || true)"
  [ -n "$raced_pub" ] || fail \
    "put-parameter for $parameter_name failed and no existing public key could be read \
(check credentials/permissions for ssm:PutParameter and ssm:GetParameter)"
  emit "$raced_pub" "false"
fi
