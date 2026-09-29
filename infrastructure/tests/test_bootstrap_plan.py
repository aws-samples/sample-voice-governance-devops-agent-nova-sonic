# Feature: nova-sonic-support-portal, task 13.2 — bootstrap-layer plan assertions.
# Validates: Requirements 16.1, 16.3, 16.6 (three pipelines, exact gated stage
# order, manual approval before deploy), 16.10 (no CodePipeline S3 deploy
# action anywhere), 16.2 (EventBridge rule per source bucket starting the
# matching pipeline), 12.3 (ECR scan-on-push + encryption, SSE on every
# bucket and the lock table), and the bootstrap analogues of 13.2/13.3
# (SecureTransport and TLS<1.2 deny statements on every created bucket).

"""Plan assertions for ``infrastructure/bootstrap`` (``terraform show -json``).

Asserts the CI/CD foundation's security and architecture invariants on the
bootstrap layer's plan: pipeline shape and gating, the ban on CodePipeline S3
deploy actions, the hardening baseline of every created S3 bucket, ECR
scanning and encryption, the app-layer state lock table's key schema, and the
EventBridge trigger wiring from each source bucket to its pipeline.
"""

import json
from typing import Any, Final

from conftest import (
    configuration_resources,
    expression_references,
    module_call_expressions,
    policy_document_has_deny_condition,
    resources_of_type,
    resources_of_type_by_module,
    single,
    values,
)

EXPECTED_STAGE_ORDER: Final[list[str]] = [
    "Source",
    "SecurityScan",
    "UnitTest",
    "BuildAndPlan",
    "ManualApproval",
    "Deploy",
]
"""Exact stage order every pipeline must plan (Req 16.1, 16.3, 16.6)."""

SOURCES: Final[tuple[str, str, str]] = ("frontend", "backend", "iac")
"""The three source-driven pipelines the portal ships (Req 16.1)."""

EXPECTED_PIPELINE_NAMES: Final[set[str]] = {f"portal-test-{source}" for source in SOURCES}
"""Pipeline names derived from the dummy project/environment variables."""

EXPECTED_BUCKET_PREFIXES: Final[dict[str, tuple[str, ...]]] = {
    "module.source_buckets": (
        "portal-test-frontend-source-",
        "portal-test-backend-source-",
        "portal-test-iac-source-",
    ),
    "module.artifact_store": ("portal-test-artifacts-",),
    "module.state_backend": ("portal-test-tf-state-",),
}
"""Every S3 bucket the bootstrap layer creates, by owning module (names end
with the account id and region, so assertions match on these prefixes)."""


def test_three_pipelines_with_exact_stage_order(bootstrap_plan: dict[str, Any]) -> None:
    """Exactly three pipelines plan the exact gated stage order (Req 16.1, 16.3, 16.6).

    Each of the frontend/backend/iac pipelines must carry the stages
    [Source, SecurityScan, UnitTest, BuildAndPlan, ManualApproval, Deploy] in
    exactly that order — CodePipeline stages are strictly sequential, so the
    order itself encodes the gating — and the ManualApproval stage must hold
    a single Approval/Manual action so no deploy happens unreviewed.

    Args:
        bootstrap_plan: Parsed plan JSON of the bootstrap layer.
    """
    pipelines = resources_of_type(bootstrap_plan, "aws_codepipeline")
    assert {values(pipeline)["name"] for pipeline in pipelines} == EXPECTED_PIPELINE_NAMES
    assert len(pipelines) == 3
    for pipeline in pipelines:
        stages = values(pipeline)["stage"]
        stage_names = [stage["name"] for stage in stages]
        assert stage_names == EXPECTED_STAGE_ORDER, (
            f"{values(pipeline)['name']} plans stage order {stage_names}"
        )
        approval_actions = stages[EXPECTED_STAGE_ORDER.index("ManualApproval")]["action"]
        assert len(approval_actions) == 1
        assert approval_actions[0]["category"] == "Approval"
        assert approval_actions[0]["provider"] == "Manual"


def test_no_codepipeline_s3_deploy_action_anywhere(bootstrap_plan: dict[str, Any]) -> None:
    """No pipeline action anywhere uses category=Deploy provider=S3 (Req 16.10).

    Deployment always runs inside the Deploy CodeBuild project; a CodePipeline
    S3 deploy action must never appear in any stage of any pipeline.

    Args:
        bootstrap_plan: Parsed plan JSON of the bootstrap layer.
    """
    offenders = [
        f"{values(pipeline)['name']}/{stage['name']}/{action['name']}"
        for pipeline in resources_of_type(bootstrap_plan, "aws_codepipeline")
        for stage in values(pipeline)["stage"]
        for action in stage["action"]
        if action["category"] == "Deploy" and action["provider"] == "S3"
    ]
    assert offenders == [], f"CodePipeline S3 deploy actions found: {offenders}"


def test_every_bucket_versioned_encrypted_and_public_blocked(
    bootstrap_plan: dict[str, Any],
) -> None:
    """Every created bucket is versioned, SSE-encrypted, and publicly blocked (Req 12.3).

    The plan must create exactly the five known buckets (three source buckets,
    the artifact store, the state bucket), and every bucket-owning module must
    plan one versioning resource with status Enabled, one server-side
    encryption configuration, and one public access block with all four flags
    set, per bucket.

    Args:
        bootstrap_plan: Parsed plan JSON of the bootstrap layer.
    """
    buckets = resources_of_type_by_module(bootstrap_plan, "aws_s3_bucket")
    assert set(buckets) == set(EXPECTED_BUCKET_PREFIXES)
    for module_address, expected_prefixes in EXPECTED_BUCKET_PREFIXES.items():
        names = [values(bucket)["bucket"] for bucket in buckets[module_address]]
        assert len(names) == len(expected_prefixes)
        for prefix in expected_prefixes:
            assert any(name.startswith(prefix) for name in names), (
                f"no bucket named {prefix}* planned in {module_address}: {names}"
            )

    versioning = resources_of_type_by_module(bootstrap_plan, "aws_s3_bucket_versioning")
    encryption = resources_of_type_by_module(
        bootstrap_plan, "aws_s3_bucket_server_side_encryption_configuration"
    )
    public_access = resources_of_type_by_module(
        bootstrap_plan, "aws_s3_bucket_public_access_block"
    )
    for module_address, module_buckets in buckets.items():
        bucket_count = len(module_buckets)
        assert len(versioning.get(module_address, [])) == bucket_count
        for resource in versioning[module_address]:
            assert values(resource)["versioning_configuration"][0]["status"] == "Enabled"
        assert len(encryption.get(module_address, [])) == bucket_count
        for resource in encryption[module_address]:
            rule = values(resource)["rule"][0]
            assert rule["apply_server_side_encryption_by_default"][0]["sse_algorithm"]
        assert len(public_access.get(module_address, [])) == bucket_count
        for resource in public_access[module_address]:
            for flag in (
                "block_public_acls",
                "block_public_policy",
                "ignore_public_acls",
                "restrict_public_buckets",
            ):
                assert values(resource)[flag] is True, f"{resource['address']}: {flag}"


def test_every_bucket_policy_denies_insecure_transport_and_old_tls(
    bootstrap_plan: dict[str, Any],
) -> None:
    """Every bucket gets a policy with both TLS deny statements (Req 13.2/13.3 analogues).

    Each bucket-owning module must plan one ``aws_s3_bucket_policy`` per
    bucket. The rendered policy JSON is unknown at plan time (it references
    the bucket ARN), so the statement content is asserted on the
    configuration section: every bucket module carries a policy document with
    a Deny on ``aws:SecureTransport = false`` and a Deny on
    ``s3:TlsVersion < 1.2``.

    Args:
        bootstrap_plan: Parsed plan JSON of the bootstrap layer.
    """
    buckets = resources_of_type_by_module(bootstrap_plan, "aws_s3_bucket")
    policies = resources_of_type_by_module(bootstrap_plan, "aws_s3_bucket_policy")
    for module_address, module_buckets in buckets.items():
        assert len(policies.get(module_address, [])) == len(module_buckets), (
            f"{module_address} plans {len(module_buckets)} bucket(s) but "
            f"{len(policies.get(module_address, []))} bucket policy(ies)"
        )

    documents = configuration_resources(bootstrap_plan, "aws_iam_policy_document", mode="data")
    for module_address in buckets:
        module_documents = [
            document for address, document in documents if address == module_address
        ]
        assert any(
            policy_document_has_deny_condition(doc, "Bool", "aws:SecureTransport", "false")
            for doc in module_documents
        ), f"{module_address}: no aws:SecureTransport=false Deny statement"
        assert any(
            policy_document_has_deny_condition(doc, "NumericLessThan", "s3:TlsVersion", "1.2")
            for doc in module_documents
        ), f"{module_address}: no s3:TlsVersion<1.2 Deny statement"


def test_ecr_repository_scans_on_push_and_encrypts(bootstrap_plan: dict[str, Any]) -> None:
    """The Voice_Service ECR repository scans on push and encrypts at rest (Req 12.3).

    Args:
        bootstrap_plan: Parsed plan JSON of the bootstrap layer.
    """
    repository = single(
        resources_of_type(bootstrap_plan, "aws_ecr_repository"), "ECR repository"
    )
    repository_values = values(repository)
    assert repository_values["image_scanning_configuration"][0]["scan_on_push"] is True
    encryption = repository_values["encryption_configuration"]
    assert encryption, "ECR repository plans no encryption_configuration"
    assert encryption[0]["encryption_type"] in {"AES256", "KMS"}


def test_state_lock_table_uses_lockid_hash_key(bootstrap_plan: dict[str, Any]) -> None:
    """The app-layer state lock table keys on the S3 backend's LockID schema.

    The S3 backend requires exactly a string hash key named ``LockID``; the
    table also carries SSE and point-in-time recovery per the security
    baseline (Req 12.3).

    Args:
        bootstrap_plan: Parsed plan JSON of the bootstrap layer.
    """
    table = single(
        resources_of_type(bootstrap_plan, "aws_dynamodb_table"), "DynamoDB lock table"
    )
    table_values = values(table)
    assert table_values["hash_key"] == "LockID"
    attributes = [
        {"name": attribute["name"], "type": attribute["type"]}
        for attribute in table_values["attribute"]
    ]
    assert {"name": "LockID", "type": "S"} in attributes
    assert table_values["server_side_encryption"][0]["enabled"] is True
    assert table_values["point_in_time_recovery"][0]["enabled"] is True


def test_eventbridge_rule_per_source_bucket_starts_matching_pipeline(
    bootstrap_plan: dict[str, Any],
) -> None:
    """Each source bucket has an EventBridge rule starting its pipeline (Req 16.2).

    For every source (frontend, backend, iac): a rule matches Object Created
    events for exactly that source bucket and the ``source.zip`` key, and an
    EventBridge target on that rule exists. The target ARN is unknown at plan
    time, so pipeline wiring is asserted through the configuration section:
    the target starts the pipeline passed for its source, and the root module
    wires all three pipeline modules into the source_buckets module.

    Args:
        bootstrap_plan: Parsed plan JSON of the bootstrap layer.
    """
    rules = {
        rule["index"]: rule
        for rule in resources_of_type(bootstrap_plan, "aws_cloudwatch_event_rule")
    }
    targets = {
        target["index"]: target
        for target in resources_of_type(bootstrap_plan, "aws_cloudwatch_event_target")
    }
    source_buckets = {
        bucket["index"]: bucket
        for bucket in resources_of_type(bootstrap_plan, "aws_s3_bucket")
        if bucket["address"].startswith("module.source_buckets.")
    }
    assert set(rules) == set(SOURCES)
    assert set(targets) == set(SOURCES)
    assert set(source_buckets) == set(SOURCES)

    for source in SOURCES:
        pattern = json.loads(values(rules[source])["event_pattern"])
        assert pattern["source"] == ["aws.s3"]
        assert pattern["detail-type"] == ["Object Created"]
        assert pattern["detail"]["bucket"]["name"] == [values(source_buckets[source])["bucket"]]
        assert pattern["detail"]["object"]["key"] == ["source.zip"]
        # The for_each key ties rule and target together: the target planned
        # for this source attaches to exactly this source's rule.
        assert values(targets[source])["rule"] == values(rules[source])["name"]

    target_configurations = [
        configuration
        for address, configuration in configuration_resources(
            bootstrap_plan, "aws_cloudwatch_event_target"
        )
        if address == "module.source_buckets"
    ]
    assert target_configurations, "no EventBridge target configured in module.source_buckets"
    for configuration in target_configurations:
        arn_references = expression_references(
            (configuration.get("expressions") or {}).get("arn")
        )
        assert any(reference.startswith("var.pipelines") for reference in arn_references)

    pipelines_expression = module_call_expressions(bootstrap_plan, ("source_buckets",)).get(
        "pipelines"
    )
    pipeline_references = expression_references(pipelines_expression)
    for module_name in ("pipeline_frontend", "pipeline_backend", "pipeline_iac"):
        assert any(
            reference.startswith(f"module.{module_name}") for reference in pipeline_references
        ), f"source_buckets is not wired to module.{module_name}"
