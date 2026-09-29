"""Configuration loading for the Voice_Service.

Implements the design's Configuration Model for the Voice_Service process
(Req 14.1): non-sensitive settings come from environment variables set on
the ECS task definition, while sensitive values live in SSM Parameter Store
(SecureString) or Secrets Manager and are referenced from the environment
by *name* only (``ORIGIN_VERIFY_SECRET_NAME``), never by value.

Startup contract (Req 14.4, 14.5): the application entrypoint calls
``load_settings`` and then ``resolve_secrets`` before the server binds. A
missing required key or a failed secret retrieval raises
``ConfigurationError`` naming the offending key, which the entrypoint
converts into a non-zero process exit before any request is accepted.
Error messages reference configuration entries by key name only and never
contain a configuration or secret value (Req 14.6). Secret values are
carried in the ``Secret`` wrapper, whose ``str``, ``repr``, and ``format``
renderings all produce ``Secret(<key>)`` in place of the value, so a
resolved ``Settings`` instance is safe to log or ``repr``.

This module performs no I/O and imports no AWS SDK — the import-linter
contract confines ``boto3``/``aioboto3`` to ``app/adapters`` — so manifest
validation and secret redaction stay pure and property-testable. The real
SSM Parameter Store / Secrets Manager fetcher is injected into
``resolve_secrets`` by the application wiring; tests inject in-memory
fakes. The manifest here covers Voice_Service keys only; the Notifier
validates its own manifest (the VAPID private key, for example, belongs to
the Notifier, not to this service).
"""

import hmac
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Final, final

from app.exceptions import ConfigurationError

__all__ = [
    "REQUIRED_ENV_KEYS",
    "Secret",
    "SecretFetcher",
    "Settings",
    "load_settings",
    "resolve_secrets",
]

# Required-key manifest (Req 14.4): every key below must be present and
# non-blank for startup validation to pass. ``ORIGIN_VERIFY_SECRET_NAME``
# holds the *name* of the origin-verify secret, never its value.
REQUIRED_ENV_KEYS: Final[tuple[str, ...]] = (
    "AWS_REGION",
    "NOVA_SONIC_MODEL_ID",
    "SESSIONS_TABLE_NAME",
    "CHATS_TABLE_NAME",
    "SUBSCRIPTIONS_TABLE_NAME",
    "TRANSCRIPTS_TABLE_NAME",
    "GUARDRAIL_ID",
    "GUARDRAIL_VERSION",
    "APPSYNC_EVENTS_HTTP_ENDPOINT",
    "APPSYNC_EVENTS_REALTIME_ENDPOINT",
    "COGNITO_USER_POOL_ID",
    "COGNITO_CLIENT_ID",
    "DEVOPS_AGENT_SPACE_ID",
    "ORIGIN_VERIFY_SECRET_NAME",
)

# Design defaults for the optional numeric tunables, applied when the
# corresponding environment variable is absent or blank.
_DEFAULT_RETENTION_DAYS: Final = 30
_DEFAULT_SEGMENTATION_ROLLOVER_SECONDS: Final = 450.0
_DEFAULT_SEGMENTATION_WATCHDOG_SECONDS: Final = 10.0
_DEFAULT_AUDIO_BUFFER_MAX_SECONDS: Final = 30.0

# 120 s, not the original 60 s: the diagnostic prompt has the agent check
# several candidate causes for one question, and production runs showed
# every such call hitting the 60-second budget ("DevOps Agent response
# exceeded 60s budget") and being reported as a timeout even though the
# agent was still working. Still well inside the 450-second segmentation
# rollover, so a slow answer cannot outlive the stream carrying it.
# Override per environment with AGENT_RESPONSE_BUDGET_SECONDS.
_DEFAULT_AGENT_RESPONSE_BUDGET_SECONDS: Final = 120.0

# Engineer-silence budget before a live session is warned and then ended.
# Bounding a session by ACTIVITY rather than by the presented token's
# remaining lifetime is what stops a session dying arbitrarily early: a
# session inherits whatever is left of a 60-minute access token, so one
# started 54 minutes after sign-in used to be killed after 5 m 44 s.
# Sessions now start on a freshly refreshed token (frontend) and are
# reclaimed on idleness instead. Warning first, end 5 minutes later.
_DEFAULT_IDLE_WARNING_SECONDS: Final = 600.0
_DEFAULT_IDLE_GRACE_SECONDS: Final = 300.0

# Reason phrase for ConfigurationError on a failed secret retrieval
# (Req 14.5); completes the sentence "Configuration key <key> ...".
_SECRET_RETRIEVAL_REASON: Final = "could not be retrieved"


@final
class Secret:
    """A sensitive configuration value that never renders its content.

    Wraps a secret retrieved from SSM Parameter Store or Secrets Manager
    together with the key (parameter or secret name) it was loaded from.
    Every string rendering — ``str``, ``repr``, and ``format`` (hence any
    f-string or log interpolation) — produces ``Secret(<key>)``, so the
    value cannot leak into log output or error messages (Req 14.6).
    ``reveal`` is the single, greppable accessor for the wrapped value;
    call it only at the point of use and never embed the result in a
    message.
    """

    __slots__ = ("_key", "_value")

    def __init__(self, key: str, value: str) -> None:
        """Initialize the wrapper with a key name and its secret value.

        Args:
            key: Configuration key (SSM parameter or Secrets Manager
                secret name) the value was loaded from; safe to render.
            value: The sensitive value; never rendered by this class.
        """
        self._key = key
        self._value = value

    @property
    def key(self) -> str:
        """Configuration key the value was loaded from; safe to render."""
        return self._key

    def reveal(self) -> str:
        """Return the wrapped secret value.

        This is the only accessor for the sensitive content. Callers must
        never log, render, or embed the returned value in a message
        (Req 14.6).

        Returns:
            The sensitive value exactly as retrieved.
        """
        return self._value

    def __str__(self) -> str:
        """Render as ``Secret(<key>)`` without revealing the value.

        Returns:
            The redacted rendering containing the key name only.
        """
        return f"Secret({self._key})"

    def __repr__(self) -> str:
        """Render as ``Secret(<key>)`` without revealing the value.

        Returns:
            The redacted rendering containing the key name only.
        """
        return f"Secret({self._key})"

    def __format__(self, format_spec: str) -> str:
        """Format the redacted rendering, never the value.

        Defined so format specs (``f"{secret:>20}"``) apply to the
        redacted ``Secret(<key>)`` text instead of raising or leaking.

        Args:
            format_spec: Standard format specification to apply.

        Returns:
            The redacted rendering formatted per ``format_spec``.
        """
        return format(str(self), format_spec)

    def __eq__(self, other: object) -> bool:
        """Compare two secrets, matching values in constant time.

        Args:
            other: Object to compare against.

        Returns:
            True when ``other`` is a ``Secret`` with the same key and,
            per a timing-safe comparison, the same value; False otherwise
            (``NotImplemented`` for non-``Secret`` operands).
        """
        if not isinstance(other, Secret):
            return NotImplemented
        return self._key == other._key and hmac.compare_digest(
            self._value.encode("utf-8"), other._value.encode("utf-8")
        )

    def __hash__(self) -> int:
        """Compute a hash from the key name only, never the value.

        Returns:
            Hash derived from the class and key name, so the sensitive
            value does not influence (and cannot leak through) hashing.
        """
        return hash((type(self), self._key))


# Async seam for secret retrieval: maps a parameter or secret name to its
# value. The application wiring injects the real SSM Parameter Store /
# Secrets Manager adapter; tests inject in-memory fakes.
type SecretFetcher = Callable[[str], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable, validated Voice_Service configuration.

    Built by ``load_settings`` from an environment mapping and completed
    by ``resolve_secrets``, which fills the ``Secret`` fields from SSM
    Parameter Store / Secrets Manager. Instances are safe to log: the
    only sensitive field is wrapped in ``Secret`` and renders redacted.

    Attributes:
        aws_region: AWS region hosting the service's AWS dependencies.
        nova_sonic_model_id: Bedrock model identifier of the Nova Sonic
            model invoked over ``InvokeModelWithBidirectionalStream``
            (Req 2.1); consumed by the Bedrock stream adapter factory.
        sessions_table_name: DynamoDB table holding Voice_Session state.
        chats_table_name: DynamoDB table mapping Voice_Sessions to DevOps
            Agent chat identifiers.
        subscriptions_table_name: DynamoDB table holding
            Web_Push_Subscription records.
        transcripts_table_name: DynamoDB table holding transcript entries.
        guardrail_id: Identifier of the Bedrock Guardrail evaluated before
            every DevOps Agent call.
        guardrail_version: Version of the Bedrock Guardrail to evaluate.
        appsync_events_http_endpoint: AppSync Events HTTP endpoint used to
            publish events.
        appsync_events_realtime_endpoint: AppSync Events realtime endpoint
            used by subscribers.
        cognito_user_pool_id: Cognito user pool whose JWTs the WebSocket
            handshake validates.
        cognito_client_id: Cognito app client identifier expected in
            presented JWTs.
        devops_agent_space_id: DevOps Agent agent-space identifier used
            when creating chats.
        origin_verify_secret_name: Name (never the value) of the
            origin-verify header secret in SSM Parameter Store / Secrets
            Manager.
        retention_days: Session_Store TTL retention period in days
            (Req 8.4; design default 30).
        segmentation_rollover_seconds: Bedrock_Stream age that triggers a
            segmentation rollover (design default 7 m 30 s; Req 2.2).
        segmentation_watchdog_seconds: Budget for a segmentation rollover
            to complete before it is failed (design default 10 s;
            Req 2.6).
        agent_response_budget_seconds: Budget for consuming a DevOps
            Agent streamed response (default 120 s; Req 3.8).
        audio_buffer_max_seconds: Upper bound of the segmentation audio
            FIFO buffer (design default 30 s; Req 2.4).
        idle_warning_seconds: Engineer silence tolerated before the
            ``idle_warning`` notice is sent (default 600 s).
        idle_grace_seconds: Further silence tolerated after the warning
            before the session is ended (default 300 s).
        origin_verify_secret: Resolved origin-verify header secret;
            ``None`` until ``resolve_secrets`` runs.
    """

    aws_region: str
    nova_sonic_model_id: str
    sessions_table_name: str
    chats_table_name: str
    subscriptions_table_name: str
    transcripts_table_name: str
    guardrail_id: str
    guardrail_version: str
    appsync_events_http_endpoint: str
    appsync_events_realtime_endpoint: str
    cognito_user_pool_id: str
    cognito_client_id: str
    devops_agent_space_id: str
    origin_verify_secret_name: str
    retention_days: int = _DEFAULT_RETENTION_DAYS
    segmentation_rollover_seconds: float = _DEFAULT_SEGMENTATION_ROLLOVER_SECONDS
    segmentation_watchdog_seconds: float = _DEFAULT_SEGMENTATION_WATCHDOG_SECONDS
    agent_response_budget_seconds: float = _DEFAULT_AGENT_RESPONSE_BUDGET_SECONDS
    audio_buffer_max_seconds: float = _DEFAULT_AUDIO_BUFFER_MAX_SECONDS
    idle_warning_seconds: float = _DEFAULT_IDLE_WARNING_SECONDS
    idle_grace_seconds: float = _DEFAULT_IDLE_GRACE_SECONDS
    origin_verify_secret: Secret | None = None


def load_settings(environ: Mapping[str, str]) -> Settings:
    """Load and validate Voice_Service settings from an environment mapping.

    Checks the required-key manifest (``REQUIRED_ENV_KEYS``) and parses
    the optional numeric tunables, applying design defaults where a
    tunable is unset. The entrypoint calls this before the server binds,
    so a validation failure terminates startup before any request is
    accepted (Req 14.4).

    Args:
        environ: Environment mapping to read, typically ``os.environ``;
            tests pass plain dictionaries.

    Returns:
        A validated ``Settings`` whose secret fields are not yet resolved
        (``origin_verify_secret`` is ``None`` until ``resolve_secrets``).

    Raises:
        ConfigurationError: If a required key is absent or blank, naming
            the first such key in manifest order, or if a numeric tunable
            is present but not a positive, finite number, naming that key.
            The message never contains a configuration value.
    """
    values = {key: _require(environ, key) for key in REQUIRED_ENV_KEYS}
    return Settings(
        aws_region=values["AWS_REGION"],
        nova_sonic_model_id=values["NOVA_SONIC_MODEL_ID"],
        sessions_table_name=values["SESSIONS_TABLE_NAME"],
        chats_table_name=values["CHATS_TABLE_NAME"],
        subscriptions_table_name=values["SUBSCRIPTIONS_TABLE_NAME"],
        transcripts_table_name=values["TRANSCRIPTS_TABLE_NAME"],
        guardrail_id=values["GUARDRAIL_ID"],
        guardrail_version=values["GUARDRAIL_VERSION"],
        appsync_events_http_endpoint=values["APPSYNC_EVENTS_HTTP_ENDPOINT"],
        appsync_events_realtime_endpoint=values["APPSYNC_EVENTS_REALTIME_ENDPOINT"],
        cognito_user_pool_id=values["COGNITO_USER_POOL_ID"],
        cognito_client_id=values["COGNITO_CLIENT_ID"],
        devops_agent_space_id=values["DEVOPS_AGENT_SPACE_ID"],
        origin_verify_secret_name=values["ORIGIN_VERIFY_SECRET_NAME"],
        retention_days=_optional_int(
            environ, "RETENTION_DAYS", _DEFAULT_RETENTION_DAYS
        ),
        segmentation_rollover_seconds=_optional_float(
            environ,
            "SEGMENTATION_ROLLOVER_SECONDS",
            _DEFAULT_SEGMENTATION_ROLLOVER_SECONDS,
        ),
        segmentation_watchdog_seconds=_optional_float(
            environ,
            "SEGMENTATION_WATCHDOG_SECONDS",
            _DEFAULT_SEGMENTATION_WATCHDOG_SECONDS,
        ),
        agent_response_budget_seconds=_optional_float(
            environ,
            "AGENT_RESPONSE_BUDGET_SECONDS",
            _DEFAULT_AGENT_RESPONSE_BUDGET_SECONDS,
        ),
        audio_buffer_max_seconds=_optional_float(
            environ,
            "AUDIO_BUFFER_MAX_SECONDS",
            _DEFAULT_AUDIO_BUFFER_MAX_SECONDS,
        ),
        idle_warning_seconds=_optional_float(
            environ,
            "IDLE_WARNING_SECONDS",
            _DEFAULT_IDLE_WARNING_SECONDS,
        ),
        idle_grace_seconds=_optional_float(
            environ,
            "IDLE_GRACE_SECONDS",
            _DEFAULT_IDLE_GRACE_SECONDS,
        ),
    )


async def resolve_secrets(
    settings: Settings,
    secret_fetcher: SecretFetcher,
) -> Settings:
    """Resolve the sensitive values referenced by name in ``settings``.

    Fetches each secret in the Voice_Service secret manifest — currently
    the origin-verify header secret — through the injected fetcher and
    returns a copy of ``settings`` with the corresponding ``Secret``
    fields populated. The entrypoint awaits this before the server binds,
    so a retrieval failure terminates startup before any request is
    accepted (Req 14.5).

    Args:
        settings: Validated settings whose ``*_secret_name`` fields name
            the secrets to fetch.
        secret_fetcher: Coroutine function mapping a parameter or secret
            name to its value; the application wiring injects the real
            SSM Parameter Store / Secrets Manager adapter, tests inject
            fakes.

    Returns:
        A copy of ``settings`` with ``origin_verify_secret`` populated;
        the input ``settings`` is not modified.

    Raises:
        ConfigurationError: If a secret retrieval fails, naming the secret
            key only — never a value (Req 14.5, 14.6).
    """
    origin_verify_secret = await _fetch_secret(
        secret_fetcher, settings.origin_verify_secret_name
    )
    return replace(settings, origin_verify_secret=origin_verify_secret)


async def _fetch_secret(secret_fetcher: SecretFetcher, secret_name: str) -> Secret:
    """Fetch one secret by name, converting any failure to name the key.

    Args:
        secret_fetcher: Coroutine function mapping a parameter or secret
            name to its value.
        secret_name: Key of the secret to retrieve.

    Returns:
        The retrieved value wrapped in ``Secret`` under ``secret_name``.

    Raises:
        ConfigurationError: If the fetcher raises any exception, naming
            ``secret_name`` with reason ``"could not be retrieved"`` and
            never including a value (Req 14.5, 14.6). Cancellation is not
            swallowed: ``asyncio.CancelledError`` derives from
            ``BaseException`` and propagates unchanged.
    """
    try:
        value = await secret_fetcher(secret_name)
    except Exception as exc:
        # Startup boundary (Req 14.5): any retrieval failure — SDK error,
        # timeout, transport fault — must abort startup as a
        # ConfigurationError naming the secret key only. The original
        # exception stays chained for diagnostics.
        raise ConfigurationError(secret_name, _SECRET_RETRIEVAL_REASON) from exc
    return Secret(secret_name, value)


def _require(environ: Mapping[str, str], key: str) -> str:
    """Return the value of a required key, rejecting absent or blank ones.

    Args:
        environ: Environment mapping to read.
        key: Required environment variable name.

    Returns:
        The value with surrounding whitespace stripped.

    Raises:
        ConfigurationError: If ``key`` is absent or blank (Req 14.4).
    """
    value = environ.get(key, "").strip()
    if not value:
        raise ConfigurationError(key)
    return value


def _optional_int(environ: Mapping[str, str], key: str, default: int) -> int:
    """Parse an optional positive-integer tunable, defaulting when unset.

    Args:
        environ: Environment mapping to read.
        key: Environment variable name of the tunable.
        default: Design default applied when ``key`` is absent or blank.

    Returns:
        The parsed value, or ``default`` when ``key`` is absent or blank.

    Raises:
        ConfigurationError: If the value is present but not a positive
            integer, naming ``key``.
    """
    raw = environ.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(key) from exc
    if value <= 0:
        raise ConfigurationError(key)
    return value


def _optional_float(environ: Mapping[str, str], key: str, default: float) -> float:
    """Parse an optional positive-number tunable, defaulting when unset.

    Args:
        environ: Environment mapping to read.
        key: Environment variable name of the tunable.
        default: Design default applied when ``key`` is absent or blank.

    Returns:
        The parsed value, or ``default`` when ``key`` is absent or blank.

    Raises:
        ConfigurationError: If the value is present but not a positive,
            finite number, naming ``key``.
    """
    raw = environ.get(key, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(key) from exc
    if not math.isfinite(value) or value <= 0.0:
        raise ConfigurationError(key)
    return value
