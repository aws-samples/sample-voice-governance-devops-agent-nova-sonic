"""SSM Parameter Store fetcher for startup secret resolution (Req 14.1, 14.5).

The Voice_Service references sensitive values from the environment by
*name* only; the values themselves live in SSM Parameter Store as
SecureString parameters (design Configuration Model, Req 14.1). This
adapter is the concrete ``app.config.SecretFetcher`` the composition root
(``app.main``) injects into ``resolve_secrets`` at startup: partially
applied to the region, :func:`fetch_ssm_parameter` maps one parameter
name to its decrypted value over the regional TLS endpoint (Req 12.7).

Scope is deliberately SSM-only: the design stores the Voice_Service's
sensitive values (currently the origin-verify header secret) in SSM
Parameter Store, so no Secrets Manager fallback exists here. Should a
future secret land in Secrets Manager instead, add a sibling fetcher in
this package rather than widening this one.

Failure contract: any retrieval fault — SDK error, missing parameter,
transport failure, or a response without a usable string value — aborts
startup. SDK exceptions propagate unchanged and a malformed response
raises ``ConfigurationError`` naming the parameter; either way
``app.config.resolve_secrets`` converts the failure into a
``ConfigurationError`` naming the secret key only, never a value
(Req 14.5, 14.6). This module lives in ``app.adapters`` because it
imports ``aioboto3`` — the only package zone permitted to import AWS
SDKs (Req 17.6, enforced by the import-linter contract).
"""

from typing import Final

import aioboto3

from app.exceptions import ConfigurationError

__all__ = ["fetch_ssm_parameter"]

_SSM_SERVICE: Final = "ssm"

# Reason phrase for ConfigurationError when the SSM response carries no
# usable string value; completes "Configuration key <key> ...".
_NO_VALUE_REASON: Final = "resolved to no usable value in SSM Parameter Store"


async def fetch_ssm_parameter(name: str, *, region: str) -> str:
    """Fetch the decrypted value of one SSM SecureString parameter.

    Issues ``GetParameter`` with ``WithDecryption=True`` against the
    regional SSM endpoint, using the SDK's default credential chain (the
    ECS task role in deployment). Called once per secret at startup, so
    the client is created per call and released before returning; no
    connection state outlives the fetch. Partially applied to ``region``
    this matches the ``app.config.SecretFetcher`` seam.

    Args:
        name: Name of the SSM parameter to retrieve — a configuration
            key such as ``Settings.origin_verify_secret_name``; safe to
            render in errors and logs (Req 14.6).
        region: AWS region hosting the parameter, from
            ``Settings.aws_region``.

    Returns:
        The parameter's decrypted value, exactly as stored.

    Raises:
        ConfigurationError: If the response carries no non-empty string
            value for the parameter, naming ``name`` and never a value
            (Req 14.6). SDK failures (missing parameter, access denied,
            transport errors) propagate as their botocore exception
            classes; ``resolve_secrets`` converts every failure into a
            ``ConfigurationError`` that aborts startup (Req 14.5).
    """
    session = aioboto3.Session()
    async with session.client(_SSM_SERVICE, region_name=region) as client:
        response = await client.get_parameter(Name=name, WithDecryption=True)
    parameter = response.get("Parameter") if isinstance(response, dict) else None
    value = parameter.get("Value") if isinstance(parameter, dict) else None
    if not isinstance(value, str) or not value:
        raise ConfigurationError(name, _NO_VALUE_REASON)
    return value
