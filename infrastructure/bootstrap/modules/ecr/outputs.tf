# Outputs consumed by the bootstrap root: the repository URL is injected
# into the backend pipeline's CodeBuild environment (docker push target)
# and surfaced as a root output for the app layer's task definition.

output "repository_url" {
  description = "Full registry/repository URL (account.dkr.ecr.region.amazonaws.com/name); the backend pipeline's build stage pushes the Voice_Service image here and the app layer's task definition pulls from it."
  value       = aws_ecr_repository.voice_service.repository_url
}

output "repository_arn" {
  description = "ARN of the repository, used in IAM policies granting the backend pipeline push access and the ECS execution role pull access."
  value       = aws_ecr_repository.voice_service.arn
}

output "repository_name" {
  description = "Name of the repository, used by aws ecr CLI calls in the backend pipeline's buildspecs."
  value       = aws_ecr_repository.voice_service.name
}
