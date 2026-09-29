# Feature: nova-sonic-support-portal, Property 16: Secret values never appear in output
"""Property test: secret values never appear in any rendered output.

**Validates: Requirements 14.6**

For any secret value loaded through the ``Secret`` wrapper, the string
rendering, repr, and any log or error message referencing the configuration
entry contain the key name and never the secret value (Property 16).

Three output surfaces are exercised. First, the wrapper's own renderings:
``str``, ``repr``, f-string interpolation, and explicit format specs must
all produce exactly ``Secret(<key>)``, with ``reveal`` as the only value
accessor and a value-independent hash. Second, a ``Settings`` instance
carrying the secret — built through ``load_settings`` on a complete
placeholder environment and resolved through ``resolve_secrets`` with an
in-memory fetcher, the production wrapping path — must render (``str`` and
``repr``) with the redaction in place of the value, and the structured JSON
log line produced by :class:`shared.logging.JsonFormatter` for a record
referencing the secret must carry exactly the redaction in its field.
Third, the startup failure surface: a fetcher that fails with an error
embedding the secret value in its own message must yield a
``ConfigurationError`` whose message names the key and drops the value —
the chained ``__cause__`` is out of scope, being diagnostics rather than a
rendered message.

Generated secret values carry a distinctive ``SECRETVAL_`` prefix so a
leak assertion can never trip over portal-fixed text, and an ``assume``
guard discards the residual pathological draws where the value coincides
with text legitimately derived from the key itself: the raw
``Secret(<key>)`` rendering, the ``repr`` of the key (used by
``ConfigurationError`` messages and dataclass reprs), and the JSON-escaped
rendering emitted in structured log lines. ``hypothesis.given`` cannot
drive ``async def`` tests under pytest-asyncio, so the resolution paths run
their coroutines to completion with ``asyncio.run`` from synchronous test
bodies, giving every example a fresh event loop.
"""

import asyncio
import json
import logging
from dataclasses import replace
from typing import Final

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from shared.logging import JsonFormatter

from app.config import (
    REQUIRED_ENV_KEYS,
    Secret,
    Settings,
    load_settings,
    resolve_secrets,
)
from app.exceptions import ConfigurationError

_VALUE_PREFIX: Final = "SECRETVAL_"
"""Distinctive marker prefixing every generated secret value.

No portal-fixed output text (redaction template, error-message phrasing,
log field names, placeholder configuration values, timestamps, severity
names) contains this marker, so a positive ``value in output`` match can
only come from an actual leak or from text derived from the generated key,
which the ``_cannot_collide`` guard rules out.
"""

_KEYS: Final = st.text(min_size=1)
"""Arbitrary configuration key names, including exotic unicode."""

_VALUES: Final = st.text(min_size=8).map(lambda suffix: _VALUE_PREFIX + suffix)
"""Arbitrary secret values, marked with the distinctive prefix."""


class _SecretServiceError(Exception):
    """Stand-in for a raw SDK failure whose message embeds the secret value.

    Real SDK exceptions can carry sensitive request context in their
    messages; embedding the secret value here proves ``resolve_secrets``
    drops it when converting the failure into ``ConfigurationError``.
    """

    def __init__(self, name: str, value: str) -> None:
        """Initialize the fake failure with a deliberately leaking message.

        Args:
            name: Secret name whose retrieval failed.
            value: Secret value embedded verbatim in the message.
        """
        super().__init__(f"retrieval of {name} failed; last value was {value}")


class _CapturingHandler(logging.Handler):
    """Collects each formatted log line instead of writing it anywhere."""

    def __init__(self) -> None:
        """Initialize the handler with an empty line collection."""
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Format the record and store the resulting JSON line.

        Args:
            record: The log record passing through the handler.
        """
        self.lines.append(self.format(record))


def _make_logger() -> tuple[logging.Logger, _CapturingHandler]:
    """Build a fresh, isolated logger rendering entries as JSON lines.

    The logger is a dedicated named logger reset on every call — handlers
    replaced, propagation disabled — so each hypothesis example observes
    only its own output and the root logger configuration is never
    touched.

    Returns:
        The logger and its capturing handler.
    """
    handler = _CapturingHandler()
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("tests.property.p16")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger, handler


def _complete_env() -> dict[str, str]:
    """Build an environment mapping satisfying the required-key manifest.

    Returns:
        A mapping with a benign, prefix-free placeholder value for every
        key in ``REQUIRED_ENV_KEYS``.
    """
    return {key: f"cfg-{key.lower().replace('_', '-')}" for key in REQUIRED_ENV_KEYS}


def _cannot_collide(key: str, value: str) -> bool:
    """Whether ``value`` cannot coincide with key-derived output text.

    The redaction assertions check that the value appears nowhere in
    rendered output, so draws where the value happens to be a substring of
    text legitimately derived from the key must be discarded: the raw
    ``Secret(<key>)`` rendering, the ``repr`` of the key (embedded by
    ``ConfigurationError`` messages via ``key!r`` and by dataclass reprs of
    string fields), and the JSON-escaped rendering emitted in structured
    log lines.

    Args:
        key: Generated configuration key name.
        value: Generated secret value.

    Returns:
        True when the value overlaps none of the key-derived renderings.
    """
    redacted = f"Secret({key})"
    return (
        value not in redacted
        and value not in repr(key)
        and value not in json.dumps(redacted)
    )


async def _resolve_settings(key: str, value: str) -> Settings:
    """Resolve a ``Settings`` instance carrying ``Secret(key, value)``.

    Loads a complete placeholder environment, points the secret manifest at
    ``key``, and resolves it through an in-memory fetcher returning
    ``value`` — the production wrapping path of ``resolve_secrets``.

    Args:
        key: Secret name to configure and fetch.
        value: Secret value the fetcher returns.

    Returns:
        Settings whose ``origin_verify_secret`` wraps ``value`` under
        ``key``.
    """

    async def fetch(name: str) -> str:
        """Return the secret value for the configured name.

        Args:
            name: Secret name requested by ``resolve_secrets``.

        Returns:
            The generated secret value.
        """
        assert name == key
        return value

    base = load_settings(_complete_env())
    return await resolve_secrets(replace(base, origin_verify_secret_name=key), fetch)


async def _resolve_failing(key: str, value: str) -> None:
    """Attempt secret resolution with a fetcher that fails, leaking the value.

    Args:
        key: Secret name to configure and fetch.
        value: Secret value embedded in the fake SDK failure message.

    Raises:
        ConfigurationError: Always — raised by ``resolve_secrets`` for the
            failed retrieval, expected to name ``key`` only.
    """

    async def failing_fetch(name: str) -> str:
        """Raise a fake SDK error embedding the secret value.

        Args:
            name: Secret name requested by ``resolve_secrets``.

        Raises:
            _SecretServiceError: Always, with the value in its message.
        """
        raise _SecretServiceError(name, value)

    base = load_settings(_complete_env())
    await resolve_secrets(replace(base, origin_verify_secret_name=key), failing_fetch)


@given(key=_KEYS, value=_VALUES)
@settings(max_examples=100, deadline=None)
def test_secret_renderings_contain_key_and_never_value(
    key: str, value: str
) -> None:
    """Every rendering of ``Secret`` is exactly ``Secret(<key>)``.

    ``str``, ``repr``, f-string interpolation, and format-spec rendering
    all produce the redaction — containing the key name and never the
    value; ``reveal`` remains the only accessor returning the value; and
    the hash is value-independent, so the value cannot leak through
    hashing.

    Args:
        key: Generated configuration key name.
        value: Generated secret value with the distinctive prefix.
    """
    assume(_cannot_collide(key, value))
    secret = Secret(key, value)
    redacted = f"Secret({key})"

    assert str(secret) == redacted
    assert repr(secret) == redacted
    assert f"{secret}" == redacted
    assert format(secret, ">40") == format(redacted, ">40")
    for rendering in (str(secret), repr(secret), f"{secret}", format(secret, ">40")):
        assert key in rendering
        assert value not in rendering

    assert secret.key == key
    assert secret.reveal() == value
    # Documented contract: hashing derives from the key only, so two
    # secrets under the same key with different values hash equal.
    assert hash(secret) == hash(Secret(key, value + "X"))


@given(key=_KEYS, value=_VALUES)
@settings(max_examples=100, deadline=None)
def test_resolved_settings_and_log_entries_redact_value(
    key: str, value: str
) -> None:
    """Settings renderings and JSON log lines name the key, never the value.

    A ``Settings`` instance resolved through ``resolve_secrets`` renders
    (``str`` and ``repr``) with the redaction in place of the value, and a
    structured JSON log entry referencing the secret carries exactly the
    redaction in its field while the raw line contains the key rendering
    and never the value.

    Args:
        key: Generated configuration key name.
        value: Generated secret value with the distinctive prefix.
    """
    assume(_cannot_collide(key, value))
    resolved = asyncio.run(_resolve_settings(key, value))
    redacted = f"Secret({key})"

    assert resolved.origin_verify_secret is not None
    assert resolved.origin_verify_secret.reveal() == value
    for rendering in (str(resolved), repr(resolved)):
        assert redacted in rendering
        assert value not in rendering

    logger, handler = _make_logger()
    logger.info(
        "resolved origin-verify secret",
        extra={"secret": resolved.origin_verify_secret},
    )

    assert len(handler.lines) == 1
    line = handler.lines[0]
    entry = json.loads(line)
    assert entry["secret"] == redacted
    assert json.dumps(redacted) in line
    assert value not in line


@given(key=_KEYS, value=_VALUES)
@settings(max_examples=100, deadline=None)
def test_failed_retrieval_error_names_key_and_never_value(
    key: str, value: str
) -> None:
    """A failed secret retrieval produces an error naming the key only.

    When the fetcher fails with an error embedding the secret value in its
    own message, the resulting ``ConfigurationError`` message contains the
    key name and never the value. The chained ``__cause__`` is diagnostics
    rather than a rendered message and stays out of scope.

    Args:
        key: Generated configuration key name.
        value: Generated secret value with the distinctive prefix.
    """
    assume(_cannot_collide(key, value))

    with pytest.raises(ConfigurationError) as excinfo:
        asyncio.run(_resolve_failing(key, value))

    message = str(excinfo.value)
    assert excinfo.value.key == key
    assert repr(key) in message
    assert value not in message
