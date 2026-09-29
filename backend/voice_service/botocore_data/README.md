# Vendored botocore service models

Botocore data directory (loaded via `AWS_DATA_PATH`, set in the
Dockerfile) carrying service models newer than the botocore the service
resolves.

## devops-agent (2026-01-01)

Extracted verbatim from botocore 1.43.93 (`botocore/data/devops-agent/`).

Why vendored: the voice service pins `aioboto3~=13.2`, and every aioboto3
release to date (through 15.5) caps botocore below 1.41 via its exact
aiobotocore pin — but the `devops-agent` service model first shipped in
botocore 1.43.66. Without this directory, `boto3.client("devops-agent")`
raises `UnknownServiceError` and every `ask_devops_agent` tool call fails
before any request is made.

Remove this directory (and the `AWS_DATA_PATH` env in the Dockerfile) once
the aioboto3/aiobotocore chain resolves botocore >= 1.43.66.
