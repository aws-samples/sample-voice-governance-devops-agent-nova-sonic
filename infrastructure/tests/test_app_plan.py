# Feature: nova-sonic-support-portal, task 13.2 — app-layer plan assertions.
# Validates: Requirements 8.1, 8.4 (Session_Store tables, keys, TTL), 10.1
# (>=2 tasks across >=2 AZs, 120 s drain stopTimeout), 11.1, 11.2, 11.3, 11.5,
# 11.6 (WAF at both scopes, managed rule groups in block mode, logging to
# aws-waf-logs-* groups), 12.3 (SSE + PITR), 13.1 (ALB deletion protection),
# 13.2, 13.3 (TLS deny statements on the frontend bucket), 13.5 (logging
# bucket referenced, never created), 13.7 (OAC-only frontend bucket policy),
# 19.2, 19.3 (four alarms wired to the ops SNS topic), plus the Cognito SPA
# client's code+PKCE-only posture (Req 7.7, 12.5 supporting control).

"""Plan assertions for ``infrastructure/app`` (``terraform show -json``).

Asserts the runtime layer's security and architecture invariants on the app
layer's plan: ALB hardening and external access-log bucket referencing, the
frontend bucket's OAC-only + TLS-deny policy, WAF coverage at both scopes
with logging, the Session_Store data model, voice-service high availability,
the observability alarm wiring, and the Cognito SPA client posture.
"""

import json
import re
from typing import Any, Final

from conftest import (
    INFRASTRUCTURE_DIR,
    configuration_resources,
    expression_references,
    module_call_expressions,
    policy_document_has_deny_condition,
    resources_of_type,
    resources_of_type_by_module,
    single,
    values,
)

ACCESS_LOGGING_BUCKET: Final[str] = "dummy-logs-bucket"
"""The externally provided access-logging bucket name passed to the plan."""

REQUIRED_MANAGED_RULE_GROUPS: Final[tuple[str, str]] = (
    "AWSManagedRulesCommonRuleSet",
    "AWSManagedRulesKnownBadInputsRuleSet",
)
"""Managed rule groups both web ACLs must include in block mode (Req 11.2, 11.3)."""

EXPECTED_TABLES: Final[dict[str, dict[str, Any]]] = {
    "test-voice-sessions": {"hash_key": "session_id", "range_key": None, "ttl": True},
    "test-agent-chats": {"hash_key": "session_id", "range_key": None, "ttl": True},
    "test-push-subscriptions": {
        "hash_key": "engineer_id",
        "range_key": "endpoint_hash",
        "ttl": False,
    },
    "test-transcripts": {"hash_key": "session_id", "range_key": "seq", "ttl": True},
}
"""The four Session_Store tables with their key schema and TTL posture (Req 8.1, 8.4)."""

EXPECTED_ALARM_NAMES: Final[set[str]] = {
    "test-running-task-count",
    "test-alb-unhealthy-targets",
    "test-voice-5xx",
    "test-notifier-errors",
}
"""The four observability alarms the design wires to the ops topic (Req 19.2)."""


def _managed_rule_group_name(rule: dict[str, Any]) -> str | None:
    """Extract the managed rule group name from one planned web ACL rule.

    Args:
        rule: One entry of a planned ``aws_wafv2_web_acl``'s ``rule`` list.

    Returns:
        The managed rule group's name, or ``None`` when the rule uses a
        different statement type (for example a rate-based statement).
    """
    for statement in rule.get("statement") or []:
        for managed in statement.get("managed_rule_group_statement") or []:
            return managed.get("name")
    return None


def test_alb_deletion_protection_enabled(app_plan: dict[str, Any]) -> None:
    """The internet-facing ALB plans deletion protection enabled (Req 13.1).

    Args:
        app_plan: Parsed plan JSON of the app layer.
    """
    load_balancer = single(resources_of_type(app_plan, "aws_lb"), "application load balancer")
    assert values(load_balancer)["enable_deletion_protection"] is True


def test_alb_logs_to_external_bucket_and_no_logging_bucket_created(
    app_plan: dict[str, Any],
) -> None:
    """Access logs reference the external bucket, which is never created (Req 13.5).

    The ALB's ``access_logs`` block must point at the externally provided
    bucket name (enabled), and the plan must create no S3 bucket named like
    it — the only bucket the app layer creates is the frontend bucket.

    Args:
        app_plan: Parsed plan JSON of the app layer.
    """
    load_balancer = single(resources_of_type(app_plan, "aws_lb"), "application load balancer")
    access_logs = values(load_balancer)["access_logs"]
    assert access_logs and access_logs[0]["enabled"] is True
    assert access_logs[0]["bucket"] == ACCESS_LOGGING_BUCKET

    bucket_names = [
        values(bucket)["bucket"] for bucket in resources_of_type(app_plan, "aws_s3_bucket")
    ]
    assert all(ACCESS_LOGGING_BUCKET not in name for name in bucket_names), (
        f"a bucket named like the access-logging bucket is planned: {bucket_names}"
    )
    frontend_bucket_name = single(
        resources_of_type(app_plan, "aws_s3_bucket"), "app-layer S3 bucket (frontend)"
    )
    assert values(frontend_bucket_name)["bucket"].startswith("test-frontend-")


def test_frontend_bucket_policy_is_oac_only_with_tls_denies(app_plan: dict[str, Any]) -> None:
    """The frontend bucket policy is OAC-only and carries the TLS denies (Req 13.2, 13.3, 13.7).

    The single planned bucket policy is composed by the s3_policies module:
    the rendered JSON is unknown at plan time (it references the bucket and
    distribution ARNs), so the statements are asserted on the configuration
    section — both TLS deny statements, plus an OAC read allow granting
    ``s3:GetObject`` to the CloudFront service principal conditioned on this
    distribution's ARN. Together with the bucket's all-flags public access
    block and the absence of any other Allow statement, every non-CloudFront
    principal is denied.

    Args:
        app_plan: Parsed plan JSON of the app layer.
    """
    policy = single(
        resources_of_type(app_plan, "aws_s3_bucket_policy"), "app-layer S3 bucket policy"
    )
    assert policy["address"].startswith("module.cloudfront_s3.module.frontend_bucket_policy.")

    documents = configuration_resources(app_plan, "aws_iam_policy_document", mode="data")
    tls_documents = [
        document
        for address, document in documents
        if address == "module.cloudfront_s3.module.frontend_bucket_policy"
    ]
    assert any(
        policy_document_has_deny_condition(doc, "Bool", "aws:SecureTransport", "false")
        for doc in tls_documents
    ), "frontend bucket policy lacks the aws:SecureTransport=false Deny statement"
    assert any(
        policy_document_has_deny_condition(doc, "NumericLessThan", "s3:TlsVersion", "1.2")
        for doc in tls_documents
    ), "frontend bucket policy lacks the s3:TlsVersion<1.2 Deny statement"

    oac_document = single(
        [
            document
            for address, document in documents
            if address == "module.cloudfront_s3" and document.get("name") == "oac_read"
        ],
        "OAC read policy document",
    )
    statement = (oac_document.get("expressions") or {})["statement"][0]
    assert (statement.get("effect") or {}).get("constant_value") == "Allow"
    assert (statement.get("actions") or {}).get("constant_value") == ["s3:GetObject"]
    principal = statement["principals"][0]
    assert (principal.get("type") or {}).get("constant_value") == "Service"
    assert (principal.get("identifiers") or {}).get("constant_value") == [
        "cloudfront.amazonaws.com"
    ]
    condition = statement["condition"][0]
    assert (condition.get("test") or {}).get("constant_value") == "StringEquals"
    assert (condition.get("variable") or {}).get("constant_value") == "AWS:SourceArn"
    assert any(
        reference.startswith("aws_cloudfront_distribution.this")
        for reference in expression_references(condition.get("values"))
    ), "OAC read allow is not conditioned on the distribution ARN"

    # The OAC allow is merged into the same policy the TLS denies ride in.
    additional_policy = module_call_expressions(
        app_plan, ("cloudfront_s3", "frontend_bucket_policy")
    ).get("additional_policy_json")
    assert any(
        reference.startswith("data.aws_iam_policy_document.oac_read")
        for reference in expression_references(additional_policy)
    )

    public_access = single(
        resources_of_type(app_plan, "aws_s3_bucket_public_access_block"),
        "frontend bucket public access block",
    )
    for flag in (
        "block_public_acls",
        "block_public_policy",
        "ignore_public_acls",
        "restrict_public_buckets",
    ):
        assert values(public_access)[flag] is True, flag


def test_waf_web_acls_cover_both_scopes_with_managed_rules_in_block_mode(
    app_plan: dict[str, Any],
) -> None:
    """Two web ACLs (CLOUDFRONT + REGIONAL) block via managed rules (Req 11.1-11.3).

    Each scope's ACL must include AWSManagedRulesCommonRuleSet and
    AWSManagedRulesKnownBadInputsRuleSet with ``override_action none`` (block
    mode). The REGIONAL ACL associates with the ALB and the CLOUDFRONT ACL is
    wired into the distribution's ``web_acl_id`` — both associations are
    asserted through the configuration section because the ARNs are unknown
    at plan time.

    Args:
        app_plan: Parsed plan JSON of the app layer.
    """
    web_acls = resources_of_type(app_plan, "aws_wafv2_web_acl")
    assert {values(acl)["scope"] for acl in web_acls} == {"CLOUDFRONT", "REGIONAL"}
    assert len(web_acls) == 2
    for acl in web_acls:
        managed_rules = {
            name: rule
            for rule in values(acl)["rule"]
            if (name := _managed_rule_group_name(rule)) is not None
        }
        for required in REQUIRED_MANAGED_RULE_GROUPS:
            assert required in managed_rules, (
                f"{values(acl)['name']} lacks managed rule group {required}"
            )
            override_action = managed_rules[required].get("override_action") or []
            assert override_action and override_action[0].get("none") is not None
            assert not override_action[0].get("count"), (
                f"{values(acl)['name']}/{required} overrides to count — not block mode"
            )

    association = single(
        resources_of_type(app_plan, "aws_wafv2_web_acl_association"),
        "regional web ACL association",
    )
    assert association["address"].startswith("module.waf_regional.")
    association_configuration = single(
        [
            configuration
            for address, configuration in configuration_resources(
                app_plan, "aws_wafv2_web_acl_association"
            )
            if address == "module.waf_regional"
        ],
        "regional web ACL association configuration",
    )
    assert any(
        reference.startswith("var.alb_arn")
        for reference in expression_references(
            (association_configuration.get("expressions") or {}).get("resource_arn")
        )
    )

    distribution_configuration = single(
        [
            configuration
            for address, configuration in configuration_resources(
                app_plan, "aws_cloudfront_distribution"
            )
            if address == "module.cloudfront_s3"
        ],
        "CloudFront distribution configuration",
    )
    assert "var.web_acl_arn" in expression_references(
        (distribution_configuration.get("expressions") or {}).get("web_acl_id")
    )
    web_acl_wiring = module_call_expressions(app_plan, ("cloudfront_s3",)).get("web_acl_arn")
    assert any(
        reference.startswith("module.waf_cloudfront")
        for reference in expression_references(web_acl_wiring)
    ), "the distribution's web ACL is not wired to the CLOUDFRONT-scope module"


def test_waf_logging_wired_to_waf_log_groups(app_plan: dict[str, Any]) -> None:
    """Both web ACLs log to persistent aws-waf-logs-* log groups (Req 11.5, 11.6).

    Each WAF module plans one logging configuration and one CloudWatch log
    group whose name carries the mandatory ``aws-waf-logs-`` prefix; the log
    group ARN is unknown at plan time, so the destination wiring is asserted
    through the configuration section.

    Args:
        app_plan: Parsed plan JSON of the app layer.
    """
    logging_by_module = resources_of_type_by_module(
        app_plan, "aws_wafv2_web_acl_logging_configuration"
    )
    groups_by_module = resources_of_type_by_module(app_plan, "aws_cloudwatch_log_group")
    logging_configurations = configuration_resources(
        app_plan, "aws_wafv2_web_acl_logging_configuration"
    )
    for module_address in ("module.waf_cloudfront", "module.waf_regional"):
        assert len(logging_by_module.get(module_address, [])) == 1, (
            f"{module_address} plans no WAF logging configuration"
        )
        log_group = single(
            groups_by_module.get(module_address, []), f"{module_address} log group"
        )
        assert values(log_group)["name"].startswith("aws-waf-logs-")

        configuration = single(
            [
                resource
                for address, resource in logging_configurations
                if address == module_address
            ],
            f"{module_address} logging configuration",
        )
        expressions = configuration.get("expressions") or {}
        assert any(
            reference.startswith("aws_cloudwatch_log_group.waf")
            for reference in expression_references(expressions.get("log_destination_configs"))
        )
        assert any(
            reference.startswith("aws_wafv2_web_acl.this")
            for reference in expression_references(expressions.get("resource_arn"))
        )


def test_dynamodb_tables_match_data_model(app_plan: dict[str, Any]) -> None:
    """Exactly four Session_Store tables with keys, TTL, SSE, PITR (Req 8.1, 8.4, 12.3).

    Key schema mirrors the backend adapter: voice-sessions keyed by
    session_id with the by-engineer GSI (engineer_id + created_at),
    agent-chats by session_id, push-subscriptions by engineer_id +
    endpoint_hash, transcripts by session_id + numeric seq. TTL is enabled on
    voice-sessions, agent-chats, and transcripts, and absent on
    push-subscriptions; every table plans SSE and point-in-time recovery.

    Args:
        app_plan: Parsed plan JSON of the app layer.
    """
    tables = {
        values(table)["name"]: values(table)
        for table in resources_of_type(app_plan, "aws_dynamodb_table")
    }
    assert set(tables) == set(EXPECTED_TABLES)
    for name, expected in EXPECTED_TABLES.items():
        table = tables[name]
        assert table["hash_key"] == expected["hash_key"], name
        assert table.get("range_key") == expected["range_key"], name
        ttl_entries = table.get("ttl") or []
        if expected["ttl"]:
            assert ttl_entries and ttl_entries[0]["enabled"] is True, f"{name}: TTL disabled"
            assert ttl_entries[0]["attribute_name"] == "ttl", name
        else:
            assert not any(entry.get("enabled") for entry in ttl_entries), (
                f"{name}: TTL unexpectedly enabled"
            )
        assert table["server_side_encryption"][0]["enabled"] is True, name
        assert table["point_in_time_recovery"][0]["enabled"] is True, name

    indexes = tables["test-voice-sessions"]["global_secondary_index"]
    assert len(indexes) == 1
    assert indexes[0]["name"] == "by-engineer"
    assert indexes[0]["hash_key"] == "engineer_id"
    assert indexes[0]["range_key"] == "created_at"

    transcript_attributes = [
        {"name": attribute["name"], "type": attribute["type"]}
        for attribute in tables["test-transcripts"]["attribute"]
    ]
    assert {"name": "seq", "type": "N"} in transcript_attributes


def test_voice_service_runs_two_tasks_across_two_azs(app_plan: dict[str, Any]) -> None:
    """The voice service keeps >=2 tasks in private subnets across >=2 AZs (Req 10.1).

    The service's subnet ids are unknown at plan time, so the AZ spread is
    asserted on the network module's planned private subnets (>=2 subnets in
    >=2 distinct AZs) plus the configuration-level wiring of the service's
    ``private_subnet_ids`` to the network module.

    Args:
        app_plan: Parsed plan JSON of the app layer.
    """
    service = single(resources_of_type(app_plan, "aws_ecs_service"), "ECS service")
    service_values = values(service)
    assert service_values["desired_count"] >= 2
    assert service_values["launch_type"] == "FARGATE"
    network_configuration = service_values["network_configuration"][0]
    assert network_configuration["assign_public_ip"] is False

    private_subnets = [
        subnet
        for subnet in resources_of_type(app_plan, "aws_subnet")
        if (values(subnet).get("tags") or {}).get("Tier") == "private"
    ]
    assert len(private_subnets) >= 2
    zones = {values(subnet).get("availability_zone") for subnet in private_subnets}
    assert None not in zones and len(zones) >= 2, (
        f"private subnets span availability zones {zones}"
    )

    subnet_wiring = module_call_expressions(app_plan, ("ecs_service",)).get(
        "private_subnet_ids"
    )
    assert any(
        reference.startswith("module.network")
        for reference in expression_references(subnet_wiring)
    ), "the ECS service is not wired to the network module's private subnets"


def test_container_stop_timeout_is_120_seconds(app_plan: dict[str, Any]) -> None:
    """The voice container plans the 120 s drain stopTimeout (Req 10.1, 10.5 drain window).

    ``container_definitions`` is a ``jsonencode`` carrying apply-time values
    (guardrail id, AppSync endpoints, Cognito ids), so the rendered attribute
    is usually unknown at plan time and absent from ``planned_values``. When
    the plan carries it, the JSON is asserted directly; otherwise the
    constant is asserted in the ecs_service module source, which the plan was
    produced from.

    Args:
        app_plan: Parsed plan JSON of the app layer.
    """
    task_definition = single(
        resources_of_type(app_plan, "aws_ecs_task_definition"), "ECS task definition"
    )
    container_definitions = values(task_definition).get("container_definitions")
    if container_definitions:
        containers = json.loads(container_definitions)
        assert containers[0]["stopTimeout"] == 120
    else:
        module_source = (
            INFRASTRUCTURE_DIR / "app" / "modules" / "ecs_service" / "main.tf"
        ).read_text(encoding="utf-8")
        assert re.search(r"stopTimeout\s*=\s*120\b", module_source), (
            "ecs_service module no longer sets stopTimeout = 120"
        )


def test_four_alarms_notify_ops_topic(app_plan: dict[str, Any]) -> None:
    """The four observability alarms all notify the ops SNS topic (Req 19.2, 19.3).

    The observability module plans exactly the four design alarms, each with
    a monitored metric, a threshold, and an evaluation period. The topic ARN
    is unknown at plan time, so the ``alarm_actions`` wiring is asserted
    through the configuration section: every alarm references
    ``aws_sns_topic.ops``.

    Args:
        app_plan: Parsed plan JSON of the app layer.
    """
    alarms = [
        alarm
        for alarm in resources_of_type(app_plan, "aws_cloudwatch_metric_alarm")
        if alarm["address"].startswith("module.observability.")
    ]
    assert {values(alarm)["alarm_name"] for alarm in alarms} == EXPECTED_ALARM_NAMES
    assert len(alarms) == 4
    for alarm in alarms:
        alarm_values = values(alarm)
        assert alarm_values["namespace"] and alarm_values["metric_name"]
        assert alarm_values["threshold"] is not None
        assert alarm_values["period"] >= 1
        assert alarm_values["evaluation_periods"] >= 1

    topic = single(
        [
            topic
            for topic in resources_of_type(app_plan, "aws_sns_topic")
            if topic["address"].startswith("module.observability.")
        ],
        "operations SNS topic",
    )
    assert values(topic)["name"] == "test-ops-alarms"

    alarm_configurations = [
        configuration
        for address, configuration in configuration_resources(
            app_plan, "aws_cloudwatch_metric_alarm"
        )
        if address == "module.observability"
    ]
    assert len(alarm_configurations) == 4
    for configuration in alarm_configurations:
        references = expression_references(
            (configuration.get("expressions") or {}).get("alarm_actions")
        )
        assert any(reference.startswith("aws_sns_topic.ops") for reference in references), (
            f"{configuration.get('name')} does not notify the ops topic"
        )


def test_cognito_spa_client_uses_code_flow_without_secret(app_plan: dict[str, Any]) -> None:
    """The SPA app client allows only the code grant and holds no secret (Req 7.7).

    Args:
        app_plan: Parsed plan JSON of the app layer.
    """
    client = single(
        resources_of_type(app_plan, "aws_cognito_user_pool_client"), "Cognito app client"
    )
    client_values = values(client)
    assert client_values["allowed_oauth_flows"] == ["code"]
    assert client_values["generate_secret"] is False
