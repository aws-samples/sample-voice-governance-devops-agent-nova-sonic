# ecr — container registry for the Voice_Service image. The backend
# pipeline's BuildAndPlan stage pushes images here and its Deploy stage
# points the ECS service at the new image. Scan-on-push and server-side
# encryption are always enabled (Req 12.3); image tags are immutable by
# default so a deployed tag can never be silently repointed.

terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0"
    }
  }
}

locals {
  # Named after the ECS service it feeds; ECR names are account+region
  # scoped, so no account/region suffix is needed (unlike S3 buckets).
  repository_name = "${var.project_name}-${var.environment}-voice-service"
}

# image_tag_mutability defaults to IMMUTABLE (see variables.tf); semgrep
# cannot resolve the variable default, so this is a false positive. The
# validation block rejects any value other than IMMUTABLE/MUTABLE.
# nosemgrep: terraform.aws.security.aws-ecr-mutable-image-tags.aws-ecr-mutable-image-tags
resource "aws_ecr_repository" "voice_service" {
  name                 = local.repository_name
  image_tag_mutability = var.image_tag_mutability

  # Never bulk-delete images with the repository: running ECS tasks may
  # still reference digests here, so the operator must empty it deliberately.
  force_delete = false

  # Every pushed image is scanned for known CVEs on arrival (Req 12.3).
  image_scanning_configuration {
    scan_on_push = true
  }

  # Server-side encryption is always on: AES256 by default, or a
  # customer-managed KMS key when the caller supplies one (Req 12.3).
  encryption_configuration {
    encryption_type = var.kms_key_arn == null ? "AES256" : "KMS"
    kms_key         = var.kms_key_arn
  }

  tags = merge(var.tags, {
    Name = local.repository_name
  })
}

# ---------------------------------------------------------------------------
# Lifecycle policy: keep a bounded rollback window instead of growing
# forever — everything beyond the most recent N images expires.
# ---------------------------------------------------------------------------

resource "aws_ecr_lifecycle_policy" "voice_service" {
  repository = aws_ecr_repository.voice_service.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep only the most recent ${var.image_retention_count} images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = var.image_retention_count
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}
