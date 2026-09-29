# Feature: nova-sonic-support-portal, Property 10: WebSocket authentication accepts only fully valid tokens
"""Property test: WebSocket authentication accepts only fully valid tokens.

**Validates: Requirements 7.2, 7.3, 12.5, 12.6**

For any WebSocket handshake token — a validly signed unexpired Cognito
access JWT, or any mutation of one (altered signature, expired, wrong
issuer, wrong audience via the ``client_id`` claim, wrong ``token_use``,
unknown signing key, malformed, or absent) — the Voice_Service accepts
the connection and creates a Voice_Session if and only if the token is
fully valid, and on rejection creates no session and processes no audio
(design Property 10).

The suite drives the real handshake validation path:
:func:`app.auth.jwt_validator.extract_bearer_token` parses the
``Sec-WebSocket-Protocol`` offer, and
:class:`app.auth.jwt_validator.CognitoJwtValidator` verifies the token
against a JWKS served through ``httpx.MockTransport`` — the injection
seam the validator documents for tests. Acceptance means ``validate``
returns a ``ValidatedToken``; every mutation instead raises
``TokenExpiredError`` or ``TokenInvalidError``, so the connection
handler never receives a validated identity, rejects the handshake with
an authentication error, and creates no Voice_Session and processes no
audio (Req 7.3, 12.5, 12.6). The absent class asserts the composition
contract directly: an offer without a usable token extracts to ``None``,
so ``validate`` is never called at all — no token, no session.

Fixed rig, built once at module scope:

- One signer RSA keypair and one attacker keypair (2048 bits, matching
  Cognito's RSA keys). Keypair generation is the dominant cost of the
  suite, so it happens once per process rather than once per example.
- One JWKS document publishing only the signer's public key, served by
  an ``httpx.MockTransport`` handler.
- One shared ``CognitoJwtValidator``: the JWKS is constant across
  examples, so after the first fetch the key cache stays warm exactly
  like a live service; only unknown-``kid`` examples trigger a (mocked)
  refetch.

Expiry offsets are drawn at 120 seconds or more from the wall clock, in
either direction, so a token minted by an example can never flip between
valid and expired while the example runs (no clock-skew flake).
``hypothesis.given`` cannot drive ``async def`` tests under
pytest-asyncio, so each example runs its coroutine to completion with
``asyncio.run`` from a synchronous test body — the same pattern as the
Property 7 and Property 14 suites.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Final

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from hypothesis import given, settings
from hypothesis import strategies as st
from jwt.algorithms import RSAAlgorithm

from app.auth.jwt_validator import (
    BEARER_SUBPROTOCOL,
    CognitoJwtValidator,
    extract_bearer_token,
)
from app.exceptions import TokenExpiredError, TokenInvalidError

_REGION: Final = "us-east-1"
"""Region forming the issuer, standing in for ``Settings.aws_region``."""

_USER_POOL_ID: Final = "us-east-1_P10Pool"
"""User pool the signer key stands in for (``Settings.cognito_user_pool_id``)."""

_CLIENT_ID: Final = "p10-app-client-id"
"""App client every accepted token's ``client_id`` claim must equal."""

_SIGNER_KID: Final = "p10-signer-kid"
"""``kid`` under which the JWKS publishes the signer public key."""

_UNKNOWN_KID: Final = "missing-kid"
"""``kid`` no JWKS entry carries, for the unknown-signing-key mutation."""

_MIN_EXPIRY_OFFSET_SECONDS: Final = 120
"""Smallest distance of ``exp`` from now, either direction (no skew flake)."""

_EXPIRY_OFFSETS: Final = st.integers(_MIN_EXPIRY_OFFSET_SECONDS, 100_000)
"""Strategy for the distance of ``exp`` from the wall clock, in seconds."""

_SUBJECTS: Final = st.text(min_size=1)
"""Strategy for the ``sub`` claim: any non-empty engineer identity."""

_USERNAMES: Final = st.text(min_size=1)
"""Strategy for username-claim values, when a username claim is minted."""

# Module-scope key material (2048-bit generation once per process, not per
# example). The attacker key never appears in the JWKS: tokens it signs
# under the signer's kid are the altered-signature mutation.
_SIGNER_KEY: Final = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_ATTACKER_KEY: Final = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks_document() -> dict[str, object]:
    """Build the user pool JWKS document publishing the signer key.

    Returns:
        A JWKS mapping with a single RS256 verification entry for
        :data:`_SIGNER_KEY` under :data:`_SIGNER_KID`.
    """
    entry = RSAAlgorithm.to_jwk(_SIGNER_KEY.public_key(), as_dict=True)
    entry["kid"] = _SIGNER_KID
    entry["alg"] = "RS256"
    entry["use"] = "sig"
    return {"keys": [entry]}


_JWKS_DOCUMENT: Final = _jwks_document()
"""Constant JWKS body the mock transport serves for every fetch."""


def _serve_jwks(request: httpx.Request) -> httpx.Response:
    """Answer every JWKS fetch with the fixed key set.

    Args:
        request: The JWKS GET request issued by the validator.

    Returns:
        A 200 response carrying :data:`_JWKS_DOCUMENT` as JSON.
    """
    return httpx.Response(200, json=_JWKS_DOCUMENT)


_VALIDATOR: Final = CognitoJwtValidator(
    _USER_POOL_ID,
    _CLIENT_ID,
    _REGION,
    http_client=httpx.AsyncClient(transport=httpx.MockTransport(_serve_jwks)),
)
"""Shared validator under test; see the module docstring for the rationale."""

_WRONG_ISSUERS: Final = st.text().filter(lambda issuer: issuer != _VALIDATOR.issuer)
"""Strategy for ``iss`` values naming anything but the configured pool."""

_WRONG_CLIENT_IDS: Final = st.text().filter(lambda client: client != _CLIENT_ID)
"""Strategy for ``client_id`` values naming anything but the app client."""

_MUTATIONS: Final = st.sampled_from(
    (
        "valid",
        "signature",
        "expired",
        "issuer",
        "client_id",
        "token_use",
        "malformed",
        "unknown_kid",
        "absent",
    )
)
"""Design Property 10 mutation space, plus the unknown-``kid`` rotation case."""

_ABSENT_OFFERS: Final = st.sampled_from(
    ((), (BEARER_SUBPROTOCOL,), (BEARER_SUBPROTOCOL, ""))
)
"""Offers carrying no usable token: empty, trailing bearer, empty entry."""


@dataclass(frozen=True, slots=True)
class _HandshakeCase:
    """One generated handshake: the offer, the mutation, and the verdict.

    Attributes:
        kind: Mutation label; ``"valid"`` is the single accepting class.
        offer: ``Sec-WebSocket-Protocol`` entries presented at the
            handshake.
        token: The token carried by ``offer``, or ``None`` for the absent
            class.
        expected_sub: For the valid class, the ``sub`` the result must
            expose.
        expected_username: For the valid class, the resolved username the
            result must expose.
        expected_exp: For the valid class, the ``exp`` instant the result
            must expose as ``expires_at``.
        expected_reason: For ``TokenInvalidError`` classes, the exact
            failure reason the validator must name.
    """

    kind: str
    offer: tuple[str, ...]
    token: str | None = None
    expected_sub: str | None = None
    expected_username: str | None = None
    expected_exp: float | None = None
    expected_reason: str | None = None


def _mint(claims: dict[str, object], *, key: rsa.RSAPrivateKey, kid: str) -> str:
    """Mint a compact RS256-signed JWT.

    Args:
        claims: Claim set to encode.
        key: RSA private key signing the token.
        kid: ``kid`` header naming the JWKS entry the token claims to
            match.

    Returns:
        The compact-serialized signed token.
    """
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def _access_claims(sub: str, exp: int) -> dict[str, object]:
    """Build the claim set of a fully valid Cognito access token.

    Args:
        sub: Subject (engineer identity) claim value.
        exp: Expiry instant in epoch seconds.

    Returns:
        Claims carrying the configured issuer and app client, and
        ``token_use == "access"``.
    """
    return {
        "iss": _VALIDATOR.issuer,
        "client_id": _CLIENT_ID,
        "token_use": "access",
        "sub": sub,
        "exp": exp,
    }


@st.composite
def _handshake_cases(draw: st.DrawFn) -> _HandshakeCase:
    """Draw one handshake case across the full mutation space.

    Args:
        draw: Hypothesis draw function supplied by ``st.composite``.

    Returns:
        The generated case: the subprotocol offer plus the exact outcome
        the validator must produce for it.
    """
    kind = draw(_MUTATIONS)
    if kind == "absent":
        return _HandshakeCase(kind=kind, offer=draw(_ABSENT_OFFERS))
    if kind == "malformed":
        # Arbitrary text virtually never forms a parseable JWT; PyJWT
        # rejects it while reading the header, mapped to "malformed".
        # min_size=1 because the extractor already normalizes an empty
        # entry to None — the empty string belongs to the absent class.
        token = draw(st.text(min_size=1))
        return _HandshakeCase(
            kind=kind,
            offer=(BEARER_SUBPROTOCOL, token),
            token=token,
            expected_reason="malformed",
        )
    sub = draw(_SUBJECTS)
    offset = draw(_EXPIRY_OFFSETS)
    now = int(time.time())
    exp = now - offset if kind == "expired" else now + offset
    claims = _access_claims(sub, exp)
    key = _SIGNER_KEY
    kid = _SIGNER_KID
    expected_reason: str | None = None
    if kind == "signature":
        key = _ATTACKER_KEY
        expected_reason = "signature"
    elif kind == "issuer":
        claims["iss"] = draw(_WRONG_ISSUERS)
        expected_reason = "issuer"
    elif kind == "client_id":
        claims["client_id"] = draw(_WRONG_CLIENT_IDS)
        expected_reason = "client_id"
    elif kind == "token_use":
        claims["token_use"] = "id"
        expected_reason = "token_use"
    elif kind == "unknown_kid":
        kid = _UNKNOWN_KID
        expected_reason = "kid"
    expected_username = sub
    if kind == "valid":
        username_claim = draw(st.sampled_from(("none", "cognito:username", "username")))
        if username_claim != "none":
            name = draw(_USERNAMES)
            claims[username_claim] = name
            expected_username = name
    token = _mint(claims, key=key, kid=kid)
    if kind == "valid":
        return _HandshakeCase(
            kind=kind,
            offer=(BEARER_SUBPROTOCOL, token),
            token=token,
            expected_sub=sub,
            expected_username=expected_username,
            expected_exp=float(exp),
        )
    return _HandshakeCase(
        kind=kind,
        offer=(BEARER_SUBPROTOCOL, token),
        token=token,
        expected_reason=expected_reason,
    )


async def _check_handshake(case: _HandshakeCase) -> None:
    """Run one handshake case against the shared validator.

    Args:
        case: The generated offer and its required outcome.

    Raises:
        AssertionError: If extraction or validation deviates from the
            case's required outcome — acceptance of any mutation, a wrong
            failure classification, or a wrong validated identity.
    """
    token = extract_bearer_token(case.offer)
    if case.kind == "absent":
        # No usable token in the offer: validate() is never called, the
        # handler rejects the handshake, and no Voice_Session exists —
        # the composition contract of Req 7.3 / 12.5 / 12.6.
        assert token is None
        return
    assert token is not None
    assert token == case.token
    if case.kind == "valid":
        validated = await _VALIDATOR.validate(token)
        assert validated.sub == case.expected_sub
        assert validated.username == case.expected_username
        assert validated.expires_at == pytest.approx(case.expected_exp)
        assert validated.claims["client_id"] == _CLIENT_ID
        assert validated.claims["token_use"] == "access"
        return
    if case.kind == "expired":
        with pytest.raises(TokenExpiredError) as expired:
            await _VALIDATOR.validate(token)
        assert expired.value.phase == "handshake"
        return
    with pytest.raises(TokenInvalidError) as invalid:
        await _VALIDATOR.validate(token)
    assert invalid.value.reason == case.expected_reason


@given(case=_handshake_cases())
@settings(max_examples=100, deadline=None)
def test_handshake_accepts_only_fully_valid_tokens(case: _HandshakeCase) -> None:
    """A handshake token is accepted iff it is fully valid.

    For every generated offer: a validly signed, unexpired token for the
    configured pool and client validates to a ``ValidatedToken`` exposing
    the engineer identity (``sub``), the resolved username
    (``cognito:username``, then ``username``, then ``sub``), and the
    token's ``exp`` as the watchdog deadline (Req 7.2). Every mutation —
    altered signature, expired, wrong issuer, wrong ``client_id``
    audience, wrong ``token_use``, unknown signing key, malformed, or
    absent — produces no ``ValidatedToken``: the validator raises
    ``TokenExpiredError`` (phase ``handshake``) for expiry and otherwise
    ``TokenInvalidError`` naming the precise failed check, so the
    connection handler rejects the handshake without creating a
    Voice_Session or processing audio (Req 7.3, 12.5, 12.6).

    Args:
        case: Generated handshake offer plus its required outcome.
    """
    asyncio.run(_check_handshake(case))


@given(
    prefix=st.lists(
        st.text().filter(lambda name: name != BEARER_SUBPROTOCOL), max_size=4
    ),
    token=st.text(min_size=1),
    suffix=st.lists(st.text(), max_size=4),
)
@settings(max_examples=100, deadline=None)
def test_extract_returns_token_following_first_bearer(
    prefix: list[str], token: str, suffix: list[str]
) -> None:
    """The extractor yields the entry following the first ``bearer``.

    The prefix strategy is filtered to carry no ``bearer`` entry so the
    injected pair is the first match — the extractor's documented
    first-match semantics; entries after the pair never matter.

    Args:
        prefix: Offer entries before the pair, none equal to ``bearer``.
        token: Non-empty token entry following the ``bearer`` entry.
        suffix: Arbitrary offer entries after the pair.
    """
    offer = [*prefix, BEARER_SUBPROTOCOL, token, *suffix]
    assert extract_bearer_token(offer) == token


@given(
    offer=st.lists(
        st.text().filter(lambda name: name != BEARER_SUBPROTOCOL), max_size=6
    )
)
@settings(max_examples=100, deadline=None)
def test_extract_without_bearer_entry_is_none(offer: list[str]) -> None:
    """An offer with no ``bearer`` entry never yields a token (Req 7.3).

    Without a token there is nothing to validate: the handler rejects
    the handshake and creates no Voice_Session.

    Args:
        offer: Subprotocol entries, none equal to ``bearer``.
    """
    assert extract_bearer_token(offer) is None
