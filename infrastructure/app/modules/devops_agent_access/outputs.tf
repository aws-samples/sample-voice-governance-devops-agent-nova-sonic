# Outputs consumed by the app-layer root, which re-exports the role ARN so
# the operator can complete the out-of-band AssociateService call.

output "assumable_role_arn" {
  description = "ARN of the read-only role the DevOps Agent assumes to inspect this account; supplied as AssociateService's configuration.aws.assumableRoleArn."
  value       = aws_iam_role.agent.arn
}

output "assumable_role_name" {
  description = "Name of the read-only role the DevOps Agent assumes, for console lookups and policy audits."
  value       = aws_iam_role.agent.name
}
