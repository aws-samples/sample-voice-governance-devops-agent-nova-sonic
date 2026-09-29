# infrastructure/bootstrap — layer 1 root (Req 12.3, 15.2, 15.6).
#
# Wires together the CI/CD foundation the operator applies locally, once,
# before any pipeline exists: three source buckets with EventBridge
# triggers, three CodePipeline V2 instances (frontend / backend / iac), the
# shared artifact bucket, the Voice_Service ECR repository, and the APP
# layer's remote state backend. Per the layer conventions this file only
# instantiates modules — resource definitions live inside modules/.

terraform {
  # No backend block on purpose: the bootstrap layer keeps its own separate
  # local state, applied by the operator (`terraform init/apply` from this
  # directory) — it cannot store state in the bucket it is itself creating.
  # Only the APP layer uses the remote backend provisioned here by
  # module.state_backend; the two states are never mixed (Req 15.6).
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0"
    }
    external = {
      source  = "hashicorp/external"
      version = ">= 2.3"
    }
  }
}

provider "aws" {
  region = var.aws_region

  # var.tags reaches every resource via provider default_tags, so module
  # tags inputs are left at their {} defaults — modules only add their
  # per-resource Name tags on top.
  default_tags {
    tags = var.tags
  }
}

locals {
  # The portal ships exactly these three source-driven pipelines (Req 16.1).
  pipeline_sources = ["frontend", "backend", "iac"]

  # Buildspecs ride IN each source archive (written by task 13.1 under ci/),
  # so stage logic is versioned with the code it builds. Paths are
  # overridable per pipeline and stage via var.buildspec_path_overrides.
  default_buildspec_paths = {
    for source in local.pipeline_sources :
    source => {
      scan   = "ci/${source}/scan.yml"
      test   = "ci/${source}/test.yml"
      build  = "ci/${source}/build.yml"
      deploy = "ci/${source}/deploy.yml"
    }
  }

  buildspec_paths = {
    for source in local.pipeline_sources :
    source => merge(local.default_buildspec_paths[source], lookup(var.buildspec_path_overrides, source, {}))
  }

  # Defaults MUST match the app layer's naming (infrastructure/app/
  # modules/ecs_service: cluster "${environment}-voice", service
  # "${environment}-voice-service"); override the variables only if the
  # app layer's names are overridden the same way.
  ecs_cluster_name = coalesce(var.ecs_cluster_name, "${var.environment}-voice")
  ecs_service_name = coalesce(var.ecs_service_name, "${var.environment}-voice-service")
}

# ---------------------------------------------------------------------------
# Shared CI/CD infrastructure: Voice_Service image registry (Req 12.3),
# app-layer Terraform state backend (Req 15.6), and the artifact bucket
# shared by all three pipelines.
# ---------------------------------------------------------------------------

module "ecr" {
  source = "./modules/ecr"

  project_name = var.project_name
  environment  = var.environment
}

# Web Push VAPID key (Req 14.1). Optional (gated by create_vapid_key):
# generates the key pair if absent, stores the PRIVATE key as an SSM
# SecureString, and returns ONLY the public key — the private key never
# enters Terraform state. The vapid_public_key / vapid_private_key_parameter_name
# outputs feed the app layer's tfvars, completing the app deploy without a
# manual key-generation step. Off by default so key creation stays an
# explicit, opt-in action.
module "vapid" {
  source = "./modules/vapid"
  count  = var.create_vapid_key ? 1 : 0

  environment             = var.environment
  region                  = var.aws_region
  subject                 = var.vapid_subject
  parameter_name_override = var.vapid_private_key_parameter_name
}

module "state_backend" {
  source = "./modules/state_backend"

  project_name = var.project_name
  environment  = var.environment
}

module "artifact_store" {
  source = "./modules/artifact_store"

  project_name = var.project_name
  environment  = var.environment
}

# Stage-specific CI permissions (Req 15.2, 16.10): the least-privilege
# policy documents each pipeline stage needs beyond the codebuild module's
# baseline (logs + artifact bucket) — Terraform state access and the
# read/apply surfaces for the iac stages, ECR push and ECS deploy for the
# backend stages, bucket sync + invalidation for the frontend deploy.
# Codified here so pipeline permissions are never attached to live roles by
# hand. Scan and UnitTest stages intentionally get nothing extra.
module "ci_policies" {
  source = "./modules/ci_policies"

  environment                = var.environment
  state_bucket_arn           = module.state_backend.state_bucket_arn
  lock_table_arn             = module.state_backend.lock_table_arn
  ecr_repository_arn         = module.ecr.repository_arn
  ecs_cluster_name           = local.ecs_cluster_name
  ecs_service_name           = local.ecs_service_name
  frontend_bucket_name       = var.frontend_bucket_name
  cloudfront_distribution_id = var.cloudfront_distribution_id
}

# ---------------------------------------------------------------------------
# Three instances of the reusable pipeline module (Req 15.2, 16.1). Each
# reads its source archive from the matching source bucket; the bucket
# names flow from module.source_buckets below while the pipeline ARNs flow
# back into it — resource-level dependencies stay acyclic (bucket ->
# pipeline -> EventBridge target).
# ---------------------------------------------------------------------------

module "pipeline_frontend" {
  source = "./modules/pipeline"

  name                 = "${var.project_name}-${var.environment}-frontend"
  source_bucket_name   = module.source_buckets.bucket_names["frontend"]
  source_object_key    = module.source_buckets.source_object_key
  artifact_bucket_name = module.artifact_store.bucket_name

  scan_buildspec_path   = local.buildspec_paths["frontend"].scan
  test_buildspec_path   = local.buildspec_paths["frontend"].test
  build_buildspec_path  = local.buildspec_paths["frontend"].build
  deploy_buildspec_path = local.buildspec_paths["frontend"].deploy

  # Two-phase flow: both values are "" on the first bootstrap apply because
  # the app layer (which creates the frontend bucket and the CloudFront
  # distribution) has not run yet. After the app layer first applies, the
  # operator re-applies bootstrap with the real values; the frontend deploy
  # buildspec fails fast when either is empty (Req 16.9).
  environment_variables = {
    FRONTEND_BUCKET            = var.frontend_bucket_name
    CLOUDFRONT_DISTRIBUTION_ID = var.cloudfront_distribution_id
  }

  # Outputs-export contract (Req 14.3, 16.9): the iac pipeline's deploy
  # stage exports `terraform output -json` of the app layer to
  # s3://<state bucket>/app-outputs/latest.json (ci/iac/deploy.yml); the
  # frontend deploy stage reads that object to generate config.json from
  # Terraform outputs (ci/frontend/deploy.yml). Deploy-only on purpose —
  # no other frontend stage has any business with the state bucket.
  deploy_environment_variables = {
    TF_STATE_BUCKET = module.state_backend.state_bucket_name
  }

  # Deploy reads the exported app outputs, syncs the frontend bucket, and
  # invalidates the distribution (statements appear once the two-phase
  # wiring supplies real values). No other frontend stage needs anything.
  deploy_extra_policy_documents = [
    module.ci_policies.frontend_deploy_policy_json,
  ]

  approval_sns_topic_arn = var.approval_sns_topic_arn
  log_retention_days     = var.log_retention_days
}

module "pipeline_backend" {
  source = "./modules/pipeline"

  name                 = "${var.project_name}-${var.environment}-backend"
  source_bucket_name   = module.source_buckets.bucket_names["backend"]
  source_object_key    = module.source_buckets.source_object_key
  artifact_bucket_name = module.artifact_store.bucket_name

  scan_buildspec_path   = local.buildspec_paths["backend"].scan
  test_buildspec_path   = local.buildspec_paths["backend"].test
  build_buildspec_path  = local.buildspec_paths["backend"].build
  deploy_buildspec_path = local.buildspec_paths["backend"].deploy

  # The build stage runs `docker build`/`docker push` and therefore needs
  # the Docker daemon; the deploy stage only calls the ECS API, so it stays
  # unprivileged.
  build_privileged_mode  = true
  deploy_privileged_mode = false

  environment_variables = {
    ECR_REPOSITORY_URL = module.ecr.repository_url
    ECS_CLUSTER        = local.ecs_cluster_name
    ECS_SERVICE        = local.ecs_service_name
  }

  # BuildAndPlan pushes the image to ECR; Deploy registers the new task
  # definition, passes the runtime roles, and rolls the service.
  build_extra_policy_documents = [
    module.ci_policies.backend_build_policy_json,
  ]
  deploy_extra_policy_documents = [
    module.ci_policies.backend_deploy_policy_json,
  ]

  approval_sns_topic_arn = var.approval_sns_topic_arn
  log_retention_days     = var.log_retention_days
}

module "pipeline_iac" {
  source = "./modules/pipeline"

  name                 = "${var.project_name}-${var.environment}-iac"
  source_bucket_name   = module.source_buckets.bucket_names["iac"]
  source_object_key    = module.source_buckets.source_object_key
  artifact_bucket_name = module.artifact_store.bucket_name

  scan_buildspec_path   = local.buildspec_paths["iac"].scan
  test_buildspec_path   = local.buildspec_paths["iac"].test
  build_buildspec_path  = local.buildspec_paths["iac"].build
  deploy_buildspec_path = local.buildspec_paths["iac"].deploy

  # The iac buildspecs run `terraform init` for the APP layer against the
  # backend created above (Req 15.6). Environment-specific app-layer tfvars
  # ride in the iac source archive, not in this pipeline definition.
  environment_variables = {
    TF_STATE_BUCKET = module.state_backend.state_bucket_name
    TF_LOCK_TABLE   = module.state_backend.lock_table_name
  }

  # BuildAndPlan refreshes state read-only (control-plane reads, no
  # data-plane access); Deploy additionally carries the two-part apply
  # surface. Both stages get state-backend access.
  build_extra_policy_documents = [
    module.ci_policies.tf_state_policy_json,
    module.ci_policies.app_read_policy_json,
  ]
  deploy_extra_policy_documents = [
    module.ci_policies.tf_state_policy_json,
    module.ci_policies.app_read_policy_json,
    module.ci_policies.app_manage_core_policy_json,
    module.ci_policies.app_manage_platform_policy_json,
  ]

  approval_sns_topic_arn = var.approval_sns_topic_arn
  log_retention_days     = var.log_retention_days
}

# ---------------------------------------------------------------------------
# Source buckets + EventBridge trigger wiring (Req 16.1, 16.2): uploading
# source.zip to a bucket starts exactly the matching pipeline.
# ---------------------------------------------------------------------------

module "source_buckets" {
  source = "./modules/source_buckets"

  project_name = var.project_name
  environment  = var.environment

  pipelines = {
    frontend = {
      name = module.pipeline_frontend.pipeline_name
      arn  = module.pipeline_frontend.pipeline_arn
    }
    backend = {
      name = module.pipeline_backend.pipeline_name
      arn  = module.pipeline_backend.pipeline_arn
    }
    iac = {
      name = module.pipeline_iac.pipeline_name
      arn  = module.pipeline_iac.pipeline_arn
    }
  }
}
