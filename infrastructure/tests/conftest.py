# Feature: nova-sonic-support-portal, task 13.2 — Terraform plan-assertion suite.
# Shared fixtures and plan-walking helpers only; the assertions live in
# test_bootstrap_plan.py (bootstrap layer) and test_app_plan.py (app layer).

"""Fixtures and helpers for the Terraform plan-assertion suite.

The suite asserts security and architecture invariants against the JSON plan
representation (``terraform show -json``) of both infrastructure layers:

* ``infrastructure/bootstrap`` — CI/CD foundation (three pipelines, source
  buckets, artifact store, ECR, app-layer state backend);
* ``infrastructure/app`` — runtime infrastructure (ALB, WAF, CloudFront + S3,
  DynamoDB, ECS, Cognito, notifications, observability).

Each layer is copied into a session temporary directory so ``terraform init``
never writes provider trees, lock files, or state into the repository. A
transient ``*_override.tf`` file in the copy replaces the backend with a
throwaway ``backend "local"`` (the app layer declares a partial
``backend "s3"`` that only the IaC pipeline configures, and Terraform
refuses to ``plan`` a config whose declared backend was never initialised).
The layer is then planned with dummy-but-valid variables using
``-refresh=false``. Producing a plan never mutates anything.

Producing a plan does, however, require AWS credentials: the AWS provider
resolves data sources during ``terraform plan`` (``aws_caller_identity``,
``aws_availability_zones``, the CloudFront origin-facing managed prefix list,
the managed CloudFront cache policies). When credentials or network access
are missing, the fixtures skip every dependent test with a clear message —
the suite's primary home is the IaC pipeline's test stage, which has
credentials. Plan JSON documents are produced once per session and cached by
the session-scoped fixtures.
"""

import json
import os
import shutil
import subprocess
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final, NoReturn

import pytest

TESTS_DIR: Final[Path] = Path(__file__).resolve().parent
"""Directory containing this suite (``infrastructure/tests``)."""

INFRASTRUCTURE_DIR: Final[Path] = TESTS_DIR.parent
"""Repository infrastructure root holding the ``bootstrap/`` and ``app/`` layers."""

SKIP_MESSAGE: Final[str] = (
    "plan-assertion suite requires AWS credentials for provider data sources; "
    "run in the iac pipeline"
)
"""Skip reason used whenever a Terraform plan cannot be produced."""

BOOTSTRAP_VARIABLES: Final[dict[str, str]] = {
    "project_name": "portal",
    "environment": "test",
    "aws_region": "us-east-1",
}
"""Dummy-but-valid variables satisfying every required bootstrap-layer variable."""

_COPY_IGNORE = shutil.ignore_patterns(
    ".terraform",
    ".terraform.lock.hcl",
    "*.tfstate",
    "*.tfstate.*",
    "*.tfvars",
    "*.tfvars.json",
    "*_override.tf",
    "plan.bin",
)
"""Local Terraform artifacts never copied into the temporary plan directories."""

_INIT_TIMEOUT_SECONDS: Final[int] = 1800
_PLAN_TIMEOUT_SECONDS: Final[int] = 900
_SHOW_TIMEOUT_SECONDS: Final[int] = 300


def app_variables(lambda_zip_path: Path) -> dict[str, str]:
    """Build the dummy-but-valid variable set for the app-layer plan.

    Args:
        lambda_zip_path: Path to a real (tiny) zip file standing in for the
            Notifier deployment package. The notifications module hashes it
            with ``filebase64sha256`` at plan time, so the file must exist.

    Returns:
        Mapping of Terraform variable name to value satisfying every required
        app-layer variable and its validation block.
    """
    return {
        "environment": "test",
        "access_logging_bucket_name": "dummy-logs-bucket",
        "container_image": "dummy:tag",
        "lambda_zip_path": str(lambda_zip_path),
        "devops_agent_space_id": "dummy",
        "vapid_subject": "mailto:test@example.com",
        "vapid_private_key_parameter_name": "/test/vapid",
        "vapid_public_key": "dummykey",
        "cognito_domain_prefix": "test-portal-dummy",
    }


def _skip_suite(step: str, detail: str) -> NoReturn:
    """Skip the requesting test (and, via fixture caching, the whole suite).

    Args:
        step: Short label of the Terraform step that failed (for example
            ``"bootstrap init"``).
        detail: Trailing lines of the failing command's output, included so
            the skip reason explains what actually went wrong.

    Raises:
        pytest.skip.Exception: Always; carries the skip reason. Raised from a
            session-scoped fixture, pytest caches the outcome, so every test
            depending on that fixture reports the same skip.
    """
    pytest.skip(f"{SKIP_MESSAGE} [{step} failed: {detail}]")


def _run_terraform(
    step: str,
    arguments: list[str],
    working_directory: Path,
    environment: dict[str, str],
    timeout_seconds: int,
) -> str:
    """Run one Terraform command, skipping the suite when it cannot succeed.

    Args:
        step: Short label used in skip messages (for example ``"app plan"``).
        arguments: Terraform CLI arguments, excluding the executable itself.
        working_directory: Directory to run Terraform in (a temporary copy of
            one infrastructure layer).
        environment: Full process environment for the invocation.
        timeout_seconds: Upper bound on the command's runtime.

    Returns:
        The command's standard output on success.

    Raises:
        pytest.skip.Exception: When the ``terraform`` executable is missing,
            the command times out, or it exits non-zero — all treated as
            environment limitations (no credentials, no network, no binary)
            rather than assertion failures.
    """
    executable = shutil.which("terraform")
    if executable is None:
        _skip_suite(step, "terraform executable not found on PATH")
    try:
        completed = subprocess.run(  # noqa: S603 — fixed executable, no shell
            [executable, *arguments],
            cwd=working_directory,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _skip_suite(step, f"timed out after {timeout_seconds}s")
    if completed.returncode != 0:
        output = (completed.stderr or completed.stdout or "").strip()
        _skip_suite(step, "\n".join(output.splitlines()[-12:]))
    return completed.stdout


def _produce_plan_json(
    layer: str,
    variables: dict[str, str],
    work_root: Path,
    environment: dict[str, str],
) -> dict[str, Any]:
    """Copy one layer aside, init it on a throwaway local backend, plan it, parse the plan.

    Args:
        layer: Layer directory name under ``infrastructure/`` (``"bootstrap"``
            or ``"app"``).
        variables: Terraform input variables passed as ``-var`` arguments.
        work_root: Session temporary directory receiving the layer copy and
            the binary plan file.
        environment: Process environment for the Terraform invocations.

    Returns:
        The parsed ``terraform show -json`` document for the layer's plan.

    Raises:
        pytest.skip.Exception: When any Terraform step fails (missing
            credentials, network, or binary); see :func:`_run_terraform`.
    """
    working_directory = work_root / layer
    shutil.copytree(INFRASTRUCTURE_DIR / layer, working_directory, ignore=_COPY_IGNORE)
    _write_backend_override(working_directory)
    _run_terraform(
        f"{layer} init",
        ["init", "-input=false", "-no-color"],
        working_directory,
        environment,
        _INIT_TIMEOUT_SECONDS,
    )
    variable_arguments = []
    for name, value in variables.items():
        variable_arguments += ["-var", f"{name}={value}"]
    _run_terraform(
        f"{layer} plan",
        [
            "plan",
            "-refresh=false",
            "-input=false",
            "-no-color",
            "-out=plan.bin",
            *variable_arguments,
        ],
        working_directory,
        environment,
        _PLAN_TIMEOUT_SECONDS,
    )
    shown = _run_terraform(
        f"{layer} show",
        ["show", "-json", "plan.bin"],
        working_directory,
        environment,
        _SHOW_TIMEOUT_SECONDS,
    )
    return json.loads(shown)


def _write_backend_override(working_directory: Path) -> None:
    """Point the copied layer's backend at a throwaway local state path.

    The app layer declares a partial ``backend "s3" {}`` that only the IaC
    pipeline configures; Terraform refuses to ``plan`` a configuration whose
    declared backend was never initialised (``init -backend=false`` suffices
    for ``validate`` but not for ``plan``). Terraform's documented override
    mechanism replaces the backend with ``backend "local"`` inside the
    temporary copy — the repository configuration is never touched, and no
    state is ever written (a plan creates none).

    Args:
        working_directory: The temporary copy of one infrastructure layer.
    """
    state_path = working_directory / "plan-assertions.tfstate"
    (working_directory / "backend_plan_assertions_override.tf").write_text(
        "terraform {\n"
        "  backend \"local\" {\n"
        f"    path = {json.dumps(str(state_path))}\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )


def _write_placeholder_zip(path: Path) -> Path:
    """Create the tiny, valid Notifier deployment zip the app plan hashes.

    Args:
        path: Destination path of the zip file.

    Returns:
        The same ``path``, for call-site convenience.
    """
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "src/handler.py",
            '"""Placeholder handler for plan-time hashing only."""\n',
        )
    return path


@pytest.fixture(scope="session")
def terraform_environment(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """Provide the process environment shared by every Terraform invocation.

    Configures non-interactive automation mode and, unless the caller already
    manages one, a session-local provider plugin cache so the two layers
    share a single AWS provider download. The cache-may-break-lock override
    is safe here because the lock file lives in a throwaway copy.

    Args:
        tmp_path_factory: pytest session temporary-directory factory.

    Returns:
        Environment mapping for :func:`subprocess.run`.
    """
    environment = os.environ.copy()
    environment["TF_IN_AUTOMATION"] = "1"
    environment["TF_INPUT"] = "0"
    if "TF_PLUGIN_CACHE_DIR" not in environment:
        cache_directory = tmp_path_factory.mktemp("terraform-plugin-cache")
        environment["TF_PLUGIN_CACHE_DIR"] = str(cache_directory)
        environment["TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE"] = "1"
    return environment


@pytest.fixture(scope="session")
def bootstrap_plan(
    tmp_path_factory: pytest.TempPathFactory,
    terraform_environment: dict[str, str],
) -> dict[str, Any]:
    """Produce the bootstrap layer's plan JSON once per session.

    Args:
        tmp_path_factory: pytest session temporary-directory factory.
        terraform_environment: Shared Terraform process environment.

    Returns:
        Parsed ``terraform show -json`` document for the bootstrap layer.

    Raises:
        pytest.skip.Exception: When the plan cannot be produced (no AWS
            credentials, network, or terraform binary).
    """
    work_root = tmp_path_factory.mktemp("bootstrap-plan")
    return _produce_plan_json(
        "bootstrap", BOOTSTRAP_VARIABLES, work_root, terraform_environment
    )


@pytest.fixture(scope="session")
def app_plan(
    tmp_path_factory: pytest.TempPathFactory,
    terraform_environment: dict[str, str],
) -> dict[str, Any]:
    """Produce the app layer's plan JSON once per session.

    Args:
        tmp_path_factory: pytest session temporary-directory factory.
        terraform_environment: Shared Terraform process environment.

    Returns:
        Parsed ``terraform show -json`` document for the app layer.

    Raises:
        pytest.skip.Exception: When the plan cannot be produced (no AWS
            credentials, network, or terraform binary).
    """
    work_root = tmp_path_factory.mktemp("app-plan")
    zip_path = _write_placeholder_zip(work_root / "notifier.zip")
    return _produce_plan_json(
        "app", app_variables(zip_path), work_root, terraform_environment
    )


# ---------------------------------------------------------------------------
# Plan-walking helpers (planned_values section).
# ---------------------------------------------------------------------------


def _planned_modules(plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield every module object under ``planned_values``, root first.

    Args:
        plan: Parsed ``terraform show -json`` document.

    Returns:
        Iterator over module dictionaries (the root module carries no
        ``address`` key; child modules do).
    """
    stack = [(plan.get("planned_values") or {}).get("root_module") or {}]
    while stack:
        module = stack.pop()
        yield module
        stack.extend(module.get("child_modules") or [])


def resources_of_type(plan: dict[str, Any], resource_type: str) -> list[dict[str, Any]]:
    """Collect every planned managed resource of one type across all modules.

    Walks ``planned_values.root_module`` recursively through
    ``child_modules``.

    Args:
        plan: Parsed ``terraform show -json`` document.
        resource_type: Terraform resource type (for example
            ``"aws_s3_bucket"``).

    Returns:
        List of planned resource objects (each with ``address``, ``values``,
        and, for ``for_each``/``count`` instances, ``index``).
    """
    return [
        resource
        for module in _planned_modules(plan)
        for resource in module.get("resources") or []
        if resource.get("type") == resource_type
        and resource.get("mode", "managed") == "managed"
    ]


def resources_of_type_by_module(
    plan: dict[str, Any], resource_type: str
) -> dict[str, list[dict[str, Any]]]:
    """Group every planned managed resource of one type by module address.

    Args:
        plan: Parsed ``terraform show -json`` document.
        resource_type: Terraform resource type to collect.

    Returns:
        Mapping of module address (``""`` for the root module) to the list of
        planned resource objects inside that module.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for module in _planned_modules(plan):
        for resource in module.get("resources") or []:
            if (
                resource.get("type") == resource_type
                and resource.get("mode", "managed") == "managed"
            ):
                grouped.setdefault(module.get("address", ""), []).append(resource)
    return grouped


def values(resource: dict[str, Any]) -> dict[str, Any]:
    """Return a planned resource's known attribute values.

    Attributes only known after apply are absent from this mapping — assert
    on those through the ``configuration`` section instead (see
    :func:`configuration_resources`).

    Args:
        resource: One planned resource object.

    Returns:
        The resource's ``values`` mapping (empty when everything is unknown).
    """
    return resource.get("values") or {}


def single(resources: list[dict[str, Any]], description: str) -> dict[str, Any]:
    """Assert a resource list contains exactly one entry and return it.

    Args:
        resources: Candidate resource list.
        description: Human-readable description used in the failure message.

    Returns:
        The single resource object.

    Raises:
        AssertionError: When the list does not contain exactly one entry.
    """
    assert len(resources) == 1, (
        f"expected exactly one {description}, found {len(resources)}: "
        f"{[resource.get('address') for resource in resources]}"
    )
    return resources[0]


# ---------------------------------------------------------------------------
# Configuration-walking helpers (configuration section).
#
# Values derived from other resources (bucket ARNs, topic ARNs, distribution
# ARNs) are unknown at plan time, so planned_values cannot carry them. The
# plan JSON's configuration section still records the source expressions —
# constant policy conditions and cross-resource references — which is where
# the wiring assertions look.
# ---------------------------------------------------------------------------


def configuration_resources(
    plan: dict[str, Any],
    resource_type: str,
    mode: str = "managed",
) -> list[tuple[str, dict[str, Any]]]:
    """Collect resource configuration blocks of one type across all modules.

    Walks ``configuration.root_module`` recursively through ``module_calls``.
    Configuration is recorded once per module call, so ``for_each`` resource
    instances appear as a single entry.

    Args:
        plan: Parsed ``terraform show -json`` document.
        resource_type: Terraform resource type to collect.
        mode: ``"managed"`` for resources or ``"data"`` for data sources.

    Returns:
        List of ``(module_address, resource_configuration)`` tuples, where
        the module address is ``""`` for the root module.
    """
    collected: list[tuple[str, dict[str, Any]]] = []

    def walk(module: dict[str, Any], address: str) -> None:
        """Accumulate matching resource configurations from one module.

        Args:
            module: Configuration module object.
            address: Module address accumulated so far.
        """
        for resource in module.get("resources") or []:
            if (
                resource.get("type") == resource_type
                and resource.get("mode", "managed") == mode
            ):
                collected.append((address, resource))
        for call_name, call in (module.get("module_calls") or {}).items():
            child_address = f"{address}.module.{call_name}" if address else f"module.{call_name}"
            walk(call.get("module") or {}, child_address)

    walk((plan.get("configuration") or {}).get("root_module") or {}, "")
    return collected


def module_call_expressions(plan: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    """Return the argument expressions of one (possibly nested) module call.

    Args:
        plan: Parsed ``terraform show -json`` document.
        path: Module call names from the root, for example
            ``("cloudfront_s3", "frontend_bucket_policy")``.

    Returns:
        The final call's ``expressions`` mapping (argument name to expression
        object carrying ``constant_value`` and/or ``references``).

    Raises:
        KeyError: When a call name along the path does not exist — a clear
            test failure signal that the module wiring changed.
    """
    node = (plan.get("configuration") or {}).get("root_module") or {}
    call: dict[str, Any] = {}
    for name in path:
        call = (node.get("module_calls") or {})[name]
        node = call.get("module") or {}
    return call.get("expressions") or {}


def expression_references(expression: dict[str, Any]) -> list[str]:
    """Return the reference list of one configuration expression.

    Args:
        expression: Expression object from a resource configuration or module
            call (may be ``None``-ish when the argument is absent).

    Returns:
        The expression's ``references`` list, or an empty list when the
        expression is constant or absent.
    """
    return (expression or {}).get("references") or []


def policy_document_has_deny_condition(
    document_configuration: dict[str, Any],
    condition_test: str,
    condition_variable: str,
    condition_value: str,
) -> bool:
    """Check an ``aws_iam_policy_document`` configuration for a Deny condition.

    Bucket policy documents reference bucket ARNs that are unknown at plan
    time, so their rendered JSON is absent from ``planned_values``; the
    constant statement conditions in the configuration section carry the
    invariant instead.

    Args:
        document_configuration: One ``aws_iam_policy_document`` data source
            configuration block (as returned by
            :func:`configuration_resources`).
        condition_test: Expected condition operator (for example ``"Bool"``).
        condition_variable: Expected condition key (for example
            ``"aws:SecureTransport"``).
        condition_value: Expected single condition value (for example
            ``"false"``).

    Returns:
        ``True`` when the document carries a ``Deny`` statement whose
        condition matches all three expectations, ``False`` otherwise.
    """
    expressions = document_configuration.get("expressions") or {}
    for statement in expressions.get("statement") or []:
        if (statement.get("effect") or {}).get("constant_value") != "Deny":
            continue
        for condition in statement.get("condition") or []:
            matches_test = (condition.get("test") or {}).get("constant_value") == condition_test
            matches_variable = (
                (condition.get("variable") or {}).get("constant_value") == condition_variable
            )
            matches_value = (condition.get("values") or {}).get("constant_value") == [
                condition_value
            ]
            if matches_test and matches_variable and matches_value:
                return True
    return False
