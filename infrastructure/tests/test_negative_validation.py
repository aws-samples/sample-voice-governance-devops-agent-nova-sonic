# Negative Terraform validation tests for the app layer root.
# Feature: nova-sonic-support-portal, Task 13.3.
# Validates: Requirements 13.6 (empty access_logging_bucket_name fails
# validation with a clear "required" message) and 15.7 (a missing required
# input variable fails `terraform plan` with an error naming the variable,
# without creating or modifying any resources).
"""Negative Terraform validation tests (Requirements 13.6, 15.7).

Every test in this module runs the real ``terraform`` binary as a subprocess
against ``infrastructure/app`` and asserts that a *bad* input configuration
fails ``terraform plan`` with the expected diagnostic, without producing any
state or plan artifact:

* an empty ``access_logging_bucket_name`` fails its variable validation
  block (Req 13.6),
* omitting required variables fails with ``No value for required variable``
  naming each missing variable (Req 15.7),
* failing plans never materialize a state file, and at most leave
  Terraform's own non-appliable *errored* plan record (Req 15.7 "without
  creating or modifying any AWS resources"),
* ``scale_in_threshold >= scale_out_threshold`` fails the cross-variable
  validation guarding the autoscaling policies.

Root-module variable validations are evaluated by Terraform before any
provider configuration or data-source read, so these tests need no AWS
credentials and never touch AWS. The module is deliberately self-contained
(its own subprocess helpers and fixtures) so it does not depend on any
conftest.py fixtures that other infrastructure test suites may define.

Isolation: the app layer declares a partial ``backend "s3"`` block that only
the IaC pipeline configures, and Terraform refuses to ``plan`` a config
whose declared backend was never initialized (``init -backend=false`` is
enough for ``validate`` but not for ``plan``). The session fixture therefore
drops a transient, uniquely named ``*_override.tf`` file into the config
directory replacing the backend with ``backend "local"`` pointed at a
throwaway path — Terraform's documented override-file mechanism — runs
``terraform init`` once into a throwaway ``TF_DATA_DIR`` (so concurrent runs
never collide on ``.terraform/``), and on teardown removes the override
file, the data directory, and — if init created it — the
``.terraform.lock.hcl`` written next to the config, leaving the repo
pristine. No test ever touches S3 state or any AWS API.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

#: Root of the app layer Terraform configuration under test
#: (this file lives in infrastructure/tests/, the config in infrastructure/app/).
APP_DIR = Path(__file__).resolve().parents[1] / "app"

#: Seconds allowed for `terraform init` (may download providers on first run).
INIT_TIMEOUT = 900

#: Seconds allowed for each `terraform plan` (validation failures are fast).
PLAN_TIMEOUT = 300


def _run_terraform(
    args: list[str], tf_data_dir: str, timeout: int
) -> subprocess.CompletedProcess[str]:
    """Run ``terraform <args>`` in the app layer directory and capture output.

    The environment is inherited as-is (root variable validations run before
    any provider/credential work, so no AWS variables need stripping) with
    ``TF_DATA_DIR`` pointed at the session's throwaway directory and
    ``TF_IN_AUTOMATION`` set to quiet interactive hints.

    :param args: Terraform subcommand and flags, e.g. ``["plan", "-input=false"]``.
    :param tf_data_dir: Directory Terraform uses instead of ``.terraform/``.
    :param timeout: Maximum seconds to wait before failing the test run.
    :returns: The completed process with decoded stdout/stderr.
    """
    env = dict(os.environ)
    env["TF_DATA_DIR"] = tf_data_dir
    env["TF_IN_AUTOMATION"] = "1"
    return subprocess.run(  # noqa: S603 — fixed argv, no shell, trusted input
        ["terraform", *args],
        cwd=APP_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _normalized_output(result: subprocess.CompletedProcess[str]) -> str:
    """Collapse a plan's combined stdout+stderr onto single-spaced text.

    Terraform word-wraps diagnostic messages at terminal width, so a stable
    message fragment may be split across lines. Collapsing all whitespace
    runs to single spaces makes substring assertions robust against
    wrapping while keeping the assertion on the exact message text.

    :param result: Completed ``terraform`` process to normalize.
    :returns: The combined output with every whitespace run collapsed.
    """
    return " ".join((result.stdout + result.stderr).split())


def _base_vars(lambda_zip: Path) -> dict[str, str]:
    """Return a dummy-valid value for every required app-layer variable.

    These values satisfy every ``validation`` block in
    ``infrastructure/app/variables.tf`` so each negative test can override
    exactly one thing and be confident the resulting failure is scoped to
    the variable under test.

    :param lambda_zip: Path to a real (tiny) zip so ``lambda_zip_path``
        failures cannot mask the variable actually being exercised.
    :returns: Mapping of variable name to dummy-valid string value.
    """
    return {
        "environment": "test",
        "container_image": "dummy:tag",
        "lambda_zip_path": str(lambda_zip),
        "devops_agent_space_id": "dummy",
        "vapid_subject": "mailto:t@e.com",
        "vapid_private_key_parameter_name": "/t/v",
        "vapid_public_key": "dummykey",
        "cognito_domain_prefix": "test-neg-dummy",
        "access_logging_bucket_name": "dummy-logs",
    }


def _plan(
    tf_data_dir: str,
    variables: Mapping[str, str] | None,
    out_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``terraform plan`` with the given ``-var`` values.

    Uses ``-input=false`` (never prompt for missing variables),
    ``-refresh=false`` (no remote reads), and ``-no-color`` (clean text for
    assertions). Variables are passed as separate argv entries — no shell —
    so empty strings survive verbatim.

    :param tf_data_dir: The session's throwaway ``TF_DATA_DIR``.
    :param variables: Variable values to pass as ``-var name=value`` flags,
        or ``None`` to pass no variables at all (the missing-variable case).
    :param out_path: Optional ``-out`` plan-file target; used by the
        no-artifacts test to prove a failing plan writes nothing appliable.
    :returns: The completed ``terraform plan`` process.
    """
    args = ["plan", "-input=false", "-refresh=false", "-no-color"]
    if variables is not None:
        for name, value in variables.items():
            args += ["-var", f"{name}={value}"]
    if out_path is not None:
        args += [f"-out={out_path}"]
    return _run_terraform(args, tf_data_dir, timeout=PLAN_TIMEOUT)


@pytest.fixture(scope="session")
def tf_data_dir() -> Iterator[str]:
    """Provide an initialized, throwaway ``TF_DATA_DIR`` for the session.

    The app layer declares a partial ``backend "s3"`` block, and Terraform
    will not ``plan`` a configuration whose declared backend was never
    initialized. This fixture writes a transient, uniquely named
    ``*_override.tf`` file (Terraform's override-file mechanism) replacing
    the backend with ``backend "local"`` whose state path lives inside the
    throwaway temp directory, then runs ``terraform init`` once — module and
    provider resolution plus trivial local-backend setup, no S3, no AWS.
    The unique temp ``TF_DATA_DIR`` and unique override filename keep
    parallel test sessions from colliding on a shared ``.terraform/``.
    Teardown removes the override file, the temp directory, and — if init
    created it — the ``.terraform.lock.hcl`` file Terraform writes next to
    the configuration, leaving the repo pristine.

    :yields: Absolute path of the initialized ``TF_DATA_DIR``.
    """
    lock_file = APP_DIR / ".terraform.lock.hcl"
    lock_existed_before = lock_file.exists()
    data_dir = Path(tempfile.mkdtemp(prefix="tf-negative-validation-"))
    override_file = APP_DIR / f"negtest_{uuid.uuid4().hex}_override.tf"
    try:
        override_file.write_text(
            "# Transient test-only override (written and removed by\n"
            "# infrastructure/tests/test_negative_validation.py): replaces the\n"
            "# pipeline-configured S3 backend with a throwaway local backend so\n"
            "# negative `terraform plan` runs need no backend config and no AWS.\n"
            "terraform {\n"
            "  backend \"local\" {\n"
            f'    path = "{(data_dir / "never-written.tfstate").as_posix()}"\n'
            "  }\n"
            "}\n"
        )
        init = _run_terraform(
            ["init", "-input=false", "-no-color"],
            str(data_dir),
            timeout=INIT_TIMEOUT,
        )
        if init.returncode != 0:
            pytest.fail(
                "terraform init (local-backend override) failed; cannot run "
                "negative validation tests.\n"
                f"stdout:\n{init.stdout}\nstderr:\n{init.stderr}"
            )
        yield str(data_dir)
    finally:
        override_file.unlink(missing_ok=True)
        shutil.rmtree(data_dir, ignore_errors=True)
        if not lock_existed_before:
            lock_file.unlink(missing_ok=True)


@pytest.fixture(scope="session")
def dummy_lambda_zip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Create a tiny valid zip file to stand in for the Notifier package.

    ``lambda_zip_path`` validation only requires a non-empty string, but a
    real file keeps any later expression (for example a file hash) from
    failing on a missing path, so each test's failure stays scoped to the
    variable it deliberately breaks.

    :param tmp_path_factory: pytest's session-scoped temp path factory.
    :returns: Path to the dummy zip file.
    """
    zip_path = tmp_path_factory.mktemp("negative-validation") / "dummy-lambda.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("placeholder.txt", "dummy notifier payload for tests\n")
    return zip_path


def test_empty_access_logging_bucket_name_fails_validation(
    tf_data_dir: str, dummy_lambda_zip: Path
) -> None:
    """An empty ``access_logging_bucket_name`` fails plan with a clear message.

    Validates Requirement 13.6: every other required variable carries a
    dummy-valid value, only the access-logging bucket name is empty, and the
    plan must exit non-zero with the variable's validation message stating
    the bucket name is required. Root variable validations run before any
    provider or data-source work, so this holds without AWS credentials
    (any incidental credential noise in the output is irrelevant to the
    assertion).
    """
    variables = _base_vars(dummy_lambda_zip)
    variables["access_logging_bucket_name"] = ""

    result = _plan(tf_data_dir, variables)

    assert result.returncode != 0, (
        "terraform plan must fail when access_logging_bucket_name is empty, "
        f"but it exited 0.\nstdout:\n{result.stdout}"
    )
    output = _normalized_output(result)
    assert "access_logging_bucket_name is required and must not be empty" in output, (
        "expected the access_logging_bucket_name validation message in the "
        f"plan diagnostics, got:\n{result.stdout}\n{result.stderr}"
    )


def test_missing_required_variables_fail_plan(tf_data_dir: str) -> None:
    """A plan given no variables fails naming every missing required variable.

    Validates Requirement 15.7: with ``-input=false`` and zero ``-var``
    flags, ``terraform plan`` must exit non-zero and report ``No value for
    required variable``, and the diagnostics must name (at minimum) the
    required variables checked below — proving required variables carry no
    defaults and an unset one aborts the plan.
    """
    result = _plan(tf_data_dir, variables=None)

    assert result.returncode != 0, (
        "terraform plan must fail when required variables are missing, "
        f"but it exited 0.\nstdout:\n{result.stdout}"
    )
    output = _normalized_output(result)
    assert "No value for required variable" in output, (
        "expected 'No value for required variable' diagnostics, got:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    for variable_name in (
        "access_logging_bucket_name",
        "container_image",
        "lambda_zip_path",
        "environment",
    ):
        assert variable_name in output, (
            f"expected missing-variable diagnostics to name {variable_name!r}, "
            f"got:\n{result.stdout}\n{result.stderr}"
        )


def test_validation_failure_creates_no_resources(
    tf_data_dir: str, dummy_lambda_zip: Path, tmp_path: Path
) -> None:
    """Failing plans leave no trace: no state, nothing appliable.

    Validates Requirements 13.6 and 15.7 ("without creating or modifying
    any AWS resources"): re-runs both failing plans from the tests above
    with an explicit ``-out`` target and asserts that (a) each still exits
    non-zero, (b) the missing-variables failure — which aborts before
    planning starts — writes no plan file at all, (c) the validation
    failure produces at most Terraform's deliberate *errored* plan record
    (Terraform >= 1.6 saves one for post-mortem inspection when ``-out`` is
    given), which Terraform itself marks ``errored``/non-``applyable`` and
    refuses to apply — so nothing appliable exists, and (d) no
    ``terraform.tfstate`` (or backup) materializes in the configuration
    directory.
    """
    empty_bucket_vars = _base_vars(dummy_lambda_zip)
    empty_bucket_vars["access_logging_bucket_name"] = ""
    empty_bucket_plan_file = tmp_path / "empty-bucket-errored.tfplan"
    missing_vars_plan_file = tmp_path / "missing-vars-never-written.tfplan"

    empty_bucket_result = _plan(
        tf_data_dir, empty_bucket_vars, out_path=empty_bucket_plan_file
    )
    missing_vars_result = _plan(
        tf_data_dir, variables=None, out_path=missing_vars_plan_file
    )

    assert empty_bucket_result.returncode != 0, (
        "empty access_logging_bucket_name plan unexpectedly succeeded.\n"
        f"stdout:\n{empty_bucket_result.stdout}"
    )
    assert missing_vars_result.returncode != 0, (
        "missing-required-variables plan unexpectedly succeeded.\n"
        f"stdout:\n{missing_vars_result.stdout}"
    )
    assert not missing_vars_plan_file.exists(), (
        "a plan aborted on missing required variables must not write a plan "
        f"file, but {missing_vars_plan_file} exists"
    )
    if empty_bucket_plan_file.exists():
        # Terraform >= 1.6 intentionally saves an errored plan for
        # inspection; it must be marked errored and refuse to apply.
        show = _run_terraform(
            ["show", "-json", str(empty_bucket_plan_file)],
            tf_data_dir,
            timeout=PLAN_TIMEOUT,
        )
        assert show.returncode == 0, (
            f"terraform show -json failed on the errored plan:\n{show.stderr}"
        )
        plan_json = json.loads(show.stdout)
        assert plan_json.get("errored") is True, (
            "a plan file saved by a failing plan must be marked errored, "
            f"got errored={plan_json.get('errored')!r}"
        )
        assert plan_json.get("applyable") is not True, (
            "a plan file saved by a failing plan must not be applyable — "
            "resources could be created or modified from it"
        )
    for state_name in ("terraform.tfstate", "terraform.tfstate.backup"):
        assert not (APP_DIR / state_name).exists(), (
            f"failing plans must never materialize {state_name} in "
            f"{APP_DIR} — resources may have been created or modified"
        )


def test_invalid_scale_thresholds_fail_validation(
    tf_data_dir: str, dummy_lambda_zip: Path
) -> None:
    """A scale-in threshold at or above the scale-out threshold fails plan.

    Exercises the cross-variable validation guarding the autoscaling
    policies against oscillation (companion to Requirement 15.7's
    fail-before-touching-resources contract): with every required variable
    dummy-valid, ``scale_in_threshold=100`` and ``scale_out_threshold=50``
    must exit non-zero with the message that scale_in_threshold has to stay
    lower than scale_out_threshold.
    """
    variables = _base_vars(dummy_lambda_zip)
    variables["scale_in_threshold"] = "100"
    variables["scale_out_threshold"] = "50"

    result = _plan(tf_data_dir, variables)

    assert result.returncode != 0, (
        "terraform plan must fail when scale_in_threshold >= "
        f"scale_out_threshold, but it exited 0.\nstdout:\n{result.stdout}"
    )
    output = _normalized_output(result)
    assert "lower than scale_out_threshold" in output, (
        "expected the scale_in_threshold validation message in the plan "
        f"diagnostics, got:\n{result.stdout}\n{result.stderr}"
    )
