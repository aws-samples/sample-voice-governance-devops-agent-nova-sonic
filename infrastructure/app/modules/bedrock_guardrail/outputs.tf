output "guardrail_id" {
  description = "ID of the Bedrock guardrail (Voice_Service GUARDRAIL_ID)."
  value       = aws_bedrock_guardrail.this.guardrail_id
}

output "guardrail_arn" {
  description = "ARN of the Bedrock guardrail, for scoping the task role's bedrock:ApplyGuardrail permission."
  value       = aws_bedrock_guardrail.this.guardrail_arn
}

output "guardrail_version" {
  description = "Published numbered guardrail version the Voice_Service evaluates with ApplyGuardrail (Voice_Service GUARDRAIL_VERSION)."
  value       = aws_bedrock_guardrail_version.this.version
}

output "automated_reasoning_policy_arn" {
  description = "Pass-through of the Automated Reasoning read-only-operations policy ARN expected on this guardrail, or null when none is designated; attachment happens out-of-band until the AWS provider supports it (see variable of the same name)."
  value       = var.automated_reasoning_policy_arn
}
