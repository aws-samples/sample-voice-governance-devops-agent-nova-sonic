# Feature: nova-sonic-support-portal, Property 15: Startup configuration validation is complete
"""Property test: startup configuration validation is complete.

**Validates: Requirements 14.4**

For any subset of required configuration keys removed from the environment,
Voice_Service startup validation (:func:`app.config.load_settings`) fails
with a ``ConfigurationError`` naming a missing key; with all required keys
present, startup validation passes (Property 15). The suite pins the
stronger contract the implementation documents: a key set to a blank value
counts as missing exactly like an absent key, the error names the *first*
missing key in manifest order (``REQUIRED_ENV_KEYS``), the error message
contains that key name (and, per Req 14.6, configuration values never
appear in it — only the key), and a passing validation yields a
``Settings`` whose required fields equal the environment values
(whitespace-stripped) with the design defaults applied to the optional
numeric tunables. A companion test covers the manifest's tunable branch:
an optional numeric tunable that is present but not a positive, finite
number fails validation naming that tunable's key even when every required
key is present.

``load_settings`` is a pure function over an environment mapping — no I/O,
no global state — so every hypothesis example builds its own plain ``dict``
environment and the tests need no fakes, no event loop, and no cleanup.
"""

from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.config import REQUIRED_ENV_KEYS, Settings, load_settings
from app.exceptions import ConfigurationError

_BLANK_VALUES: Final[tuple[str, ...]] = ("", " ", "\t", "\n", " \t ", "  ")
"""Blank renderings that must count as missing exactly like an absent key."""

_TUNABLE_KEYS: Final[tuple[str, ...]] = (
    "RETENTION_DAYS",
    "SEGMENTATION_ROLLOVER_SECONDS",
    "SEGMENTATION_WATCHDOG_SECONDS",
    "AGENT_RESPONSE_BUDGET_SECONDS",
    "AUDIO_BUFFER_MAX_SECONDS",
)
"""Optional numeric tunables validated only when present in the environment."""

_INVALID_TUNABLE_VALUES: Final[tuple[str, ...]] = ("abc", "-1", "0", "inf", "nan")
"""Values that are not positive, finite numbers for any numeric tunable.

``abc`` fails numeric parsing outright, ``-1`` and ``0`` violate the
positivity bound, and ``inf`` / ``nan`` fail integer parsing and violate
the float finiteness bound, so the pool is invalid for the integer tunable
(``RETENTION_DAYS``) and the float tunables alike.
"""

_NON_BLANK_VALUES: Final = st.text(min_size=1).filter(lambda s: s.strip())
"""Arbitrary environment values containing at least one non-whitespace char."""

_COMPLETE_ENVIRONMENTS: Final = st.fixed_dictionaries(
    {key: _NON_BLANK_VALUES for key in REQUIRED_ENV_KEYS}
)
"""Environments assigning every required key an arbitrary non-blank value."""

_REMOVAL_PLANS: Final = st.dictionaries(
    keys=st.sampled_from(REQUIRED_ENV_KEYS),
    values=st.none() | st.sampled_from(_BLANK_VALUES),
    min_size=1,
)
"""Non-empty subsets of required keys, each mapped to how it goes missing.

``None`` deletes the key from the environment; a string replaces the value
with that blank rendering. Both must count as missing (Req 14.4).
"""


def _expected_settings(env: dict[str, str]) -> Settings:
    """Build the ``Settings`` a complete environment must validate into.

    Mirrors the documented contract of :func:`app.config.load_settings`
    for an environment carrying only the required keys: each required
    field equals its environment value with surrounding whitespace
    stripped, and every optional numeric tunable takes its design default
    (via the ``Settings`` dataclass field defaults), with no secret
    resolved yet.

    Args:
        env: Complete environment mapping assigning every required key a
            non-blank value and containing no optional tunables.

    Returns:
        The ``Settings`` instance ``load_settings`` must return for
        ``env``.
    """
    return Settings(
        aws_region=env["AWS_REGION"].strip(),
        nova_sonic_model_id=env["NOVA_SONIC_MODEL_ID"].strip(),
        sessions_table_name=env["SESSIONS_TABLE_NAME"].strip(),
        chats_table_name=env["CHATS_TABLE_NAME"].strip(),
        subscriptions_table_name=env["SUBSCRIPTIONS_TABLE_NAME"].strip(),
        transcripts_table_name=env["TRANSCRIPTS_TABLE_NAME"].strip(),
        guardrail_id=env["GUARDRAIL_ID"].strip(),
        guardrail_version=env["GUARDRAIL_VERSION"].strip(),
        appsync_events_http_endpoint=env["APPSYNC_EVENTS_HTTP_ENDPOINT"].strip(),
        appsync_events_realtime_endpoint=env[
            "APPSYNC_EVENTS_REALTIME_ENDPOINT"
        ].strip(),
        cognito_user_pool_id=env["COGNITO_USER_POOL_ID"].strip(),
        cognito_client_id=env["COGNITO_CLIENT_ID"].strip(),
        devops_agent_space_id=env["DEVOPS_AGENT_SPACE_ID"].strip(),
        origin_verify_secret_name=env["ORIGIN_VERIFY_SECRET_NAME"].strip(),
    )


@given(env=_COMPLETE_ENVIRONMENTS)
@settings(max_examples=100, deadline=None)
def test_all_required_keys_present_passes_validation(env: dict[str, str]) -> None:
    """With all required keys present, startup validation passes.

    For any environment assigning every key in ``REQUIRED_ENV_KEYS`` an
    arbitrary non-blank value (the empty removed subset), ``load_settings``
    returns a ``Settings`` whose required fields equal the environment
    values with surrounding whitespace stripped, whose optional numeric
    tunables carry the design defaults, and whose secret field is not yet
    resolved.

    Args:
        env: Environment mapping every required key to a non-blank value.
    """
    loaded = load_settings(env)

    assert loaded == _expected_settings(env)
    assert loaded.origin_verify_secret is None


@given(env=_COMPLETE_ENVIRONMENTS, removal_plan=_REMOVAL_PLANS)
@settings(max_examples=100, deadline=None)
def test_missing_keys_fail_naming_first_missing_in_manifest_order(
    env: dict[str, str],
    removal_plan: dict[str, str | None],
) -> None:
    """Any removed subset fails validation naming a removed key.

    For any non-empty subset of required keys removed from a complete
    environment — each either deleted outright or blanked to a
    whitespace-only value — ``load_settings`` raises ``ConfigurationError``
    before a ``Settings`` is ever produced. The error's ``key`` is one of
    the removed keys and, per the implementation's documented contract,
    exactly the first removed key in ``REQUIRED_ENV_KEYS`` manifest order;
    the message contains that key name.

    Args:
        env: Complete environment mapping every required key to a
            non-blank value, before removal.
        removal_plan: Non-empty mapping of the keys to remove; ``None``
            deletes the key, a blank string overwrites its value.
    """
    for key, blank in removal_plan.items():
        if blank is None:
            del env[key]
        else:
            env[key] = blank
    first_missing = next(key for key in REQUIRED_ENV_KEYS if key in removal_plan)

    with pytest.raises(ConfigurationError) as excinfo:
        load_settings(env)

    assert excinfo.value.key in removal_plan
    assert excinfo.value.key == first_missing
    assert first_missing in str(excinfo.value)


@given(
    env=_COMPLETE_ENVIRONMENTS,
    tunable_key=st.sampled_from(_TUNABLE_KEYS),
    invalid_value=st.sampled_from(_INVALID_TUNABLE_VALUES),
)
@settings(max_examples=100, deadline=None)
def test_invalid_numeric_tunable_fails_naming_that_key(
    env: dict[str, str],
    tunable_key: str,
    invalid_value: str,
) -> None:
    """A present-but-invalid numeric tunable fails validation naming it.

    For any complete environment (every required key present and
    non-blank) where one optional numeric tunable carries a value that is
    not a positive, finite number, ``load_settings`` raises
    ``ConfigurationError`` whose ``key`` is that tunable's key and whose
    message contains the key name — startup validation covers the full
    manifest, not just the required keys.

    Args:
        env: Complete environment mapping every required key to a
            non-blank value.
        tunable_key: The optional numeric tunable to poison.
        invalid_value: A value that is not a positive, finite number.
    """
    env[tunable_key] = invalid_value

    with pytest.raises(ConfigurationError) as excinfo:
        load_settings(env)

    assert excinfo.value.key == tunable_key
    assert tunable_key in str(excinfo.value)
