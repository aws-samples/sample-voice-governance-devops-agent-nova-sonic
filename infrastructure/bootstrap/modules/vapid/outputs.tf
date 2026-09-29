# Outputs of the vapid module. Both are non-sensitive: the public key is
# published to browsers, and the parameter name is just a reference. The
# private key is written to SSM by the external program and is never
# returned here, so it never appears in Terraform state or outputs.

output "public_key" {
  description = "VAPID public key (base64url, uncompressed P-256 point) matching the private key stored in SSM; feeds the app layer's vapid_public_key variable."
  value       = data.external.vapid.result.public_key
}

output "private_key_parameter_name" {
  description = "Name of the SSM SecureString holding the VAPID private key; feeds the app layer's vapid_private_key_parameter_name variable. Carries the name only — never key material (Req 14.1)."
  value       = local.parameter_name
}

output "created" {
  description = "\"true\" when this apply generated and stored a new key pair, \"false\" when an existing key was left untouched (idempotent no-op)."
  value       = data.external.vapid.result.created
}
