"""Cognito JWT validation at the WebSocket handshake (Req 7.2, 7.3).

Browsers cannot set an ``Authorization`` header on a WebSocket, so the
Cognito **access token** travels in the ``Sec-WebSocket-Protocol`` header
as the subprotocol pair ``("bearer", "<jwt>")`` (design, WebSocket
Protocol). :func:`extract_bearer_token` parses that pair from the offered
subprotocols, and :meth:`CognitoJwtValidator.validate` verifies the token
**before** the connection is accepted. On any failure the connection
handler (task 6.16) closes with 4401 and creates no Voice_Session
(Req 7.3, 12.5, 12.6); on success it selects :data:`BEARER_SUBPROTOCOL`
as the negotiated subprotocol in its ``accept()`` response.

Validation profile for Cognito **access** tokens: RS256 signature against
the user pool's JWKS, unexpired ``exp``, ``iss`` equal to
``https://cognito-idp.{region}.amazonaws.com/{user_pool_id}``, the
``client_id`` claim equal to the configured app client (access tokens
carry ``client_id``, not ``aud``), and ``token_use == "access"``. ``sub``
is the engineer identity that sessions and audit entries attribute to.

JWKS handling is fully asynchronous: keys are fetched with ``httpx`` from
``{issuer}/.well-known/jwks.json`` (PyJWT's ``PyJWKClient`` blocks on
``urllib`` and is never used here), cached by ``kid`` under a TTL, and
refreshed exactly once when a token names an unknown ``kid`` (key
rotation). Any fetch or parse failure fails **closed** as
``TokenInvalidError("jwks-unavailable")`` — a token is never accepted
while the key set cannot be confirmed (Req 12.6).

Failure vocabulary: an expired token raises
``TokenExpiredError("handshake")``; every other failure raises
``TokenInvalidError`` with a short reason token — ``"malformed"``,
``"signature"``, ``"issuer"``, ``"client_id"``, ``"token_use"``,
``"kid"``, or ``"jwks-unavailable"``. Error messages never contain the
token value. The returned :class:`ValidatedToken` exposes ``expires_at``
(epoch seconds), the deadline the session manager's mid-session
token-expiry watchdog enforces (Req 7.6).

``app.auth`` is in the import-linter forbidden-source list: no AWS SDK
imports here — only ``httpx`` and ``PyJWT``.
"""

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import httpx
import jwt

from app.exceptions import TokenExpiredError, TokenInvalidError

__all__ = [
    "BEARER_SUBPROTOCOL",
    "CognitoJwtValidator",
    "ValidatedToken",
    "extract_bearer_token",
]

# Subprotocol name preceding the JWT in the client's Sec-WebSocket-Protocol
# offer; also the subprotocol the server selects in its accept() response.
BEARER_SUBPROTOCOL: Final = "bearer"

# Cognito signs user pool tokens with RS256 only; pinning the allowed list
# defeats algorithm-confusion attacks.
_SIGNING_ALGORITHMS: Final = ["RS256"]

_JWKS_PATH: Final = "/.well-known/jwks.json"
_ACCESS_TOKEN_USE: Final = "access"

# Per-request timeout for the lazily created JWKS client, so a hung fetch
# can never stall the WebSocket handshake indefinitely.
_JWKS_TIMEOUT_SECONDS: Final = 5.0

# TokenInvalidError reason vocabulary: short tokens naming the failed
# check; the JWT value itself never appears in an error message.
_REASON_MALFORMED: Final = "malformed"
_REASON_SIGNATURE: Final = "signature"
_REASON_ISSUER: Final = "issuer"
_REASON_CLIENT_ID: Final = "client_id"
_REASON_TOKEN_USE: Final = "token_use"
_REASON_KID: Final = "kid"
_REASON_JWKS_UNAVAILABLE: Final = "jwks-unavailable"

# Claims PyJWT must find in every accepted token. client_id and token_use
# presence is implied by the manual equality checks, which give those
# failures their own precise reasons.
_REQUIRED_CLAIMS: Final = ["exp", "iss", "sub"]

# The specific exception classes one JWKS fetch can raise: httpx.HTTPError
# covers transport failures, timeouts, and (via raise_for_status) non-2xx
# statuses; httpx.InvalidURL a malformed issuer URL; ValueError a response
# body that is not JSON. Never a bare Exception (Req 17.5).
_JWKS_FETCH_ERRORS: Final = (httpx.HTTPError, httpx.InvalidURL, ValueError)

# JWK entries PyJWT cannot turn into a verification key are skipped
# individually; ValueError covers malformed base64url key material.
_JWK_PARSE_ERRORS: Final = (jwt.PyJWKError, jwt.InvalidKeyError, ValueError)


def extract_bearer_token(subprotocols: Sequence[str]) -> str | None:
    """Extract the JWT from a Sec-WebSocket-Protocol subprotocol offer.

    Parses the design's ``("bearer", "<jwt>")`` subprotocol pair: the
    token is the entry immediately following the first ``"bearer"`` entry.

    Args:
        subprotocols: Subprotocol names offered by the client, in order,
            as the WebSocket implementation parsed them from the
            ``Sec-WebSocket-Protocol`` header.

    Returns:
        The presented token, or ``None`` when the offer carries no
        ``"bearer"`` entry, ``"bearer"`` is the last entry, or the entry
        following it is empty. Rejecting the handshake on ``None`` — close
        4401, no Voice_Session — is the caller's job (Req 7.3).
    """
    for position, name in enumerate(subprotocols):
        if name != BEARER_SUBPROTOCOL:
            continue
        if position + 1 >= len(subprotocols):
            return None
        return subprotocols[position + 1] or None
    return None


@dataclass(frozen=True, slots=True)
class ValidatedToken:
    """Successfully validated Cognito access token, ready for session use.

    Attributes:
        sub: Stable Cognito subject identifier — the engineer identity
            that Voice_Sessions and audit log entries attribute to.
        username: Human-readable user name, resolved from the
            ``cognito:username`` claim when present, else the ``username``
            claim, else ``sub``.
        expires_at: Token expiry instant in epoch seconds — the deadline
            the mid-session token-expiry watchdog enforces (Req 7.6).
        claims: The complete verified claim set, for callers needing
            further claims; treat as read-only.
    """

    sub: str
    username: str
    expires_at: float
    claims: Mapping[str, object]


def _token_key_id(token: str) -> str:
    """Read the signing key id from an unverified JWT header.

    Args:
        token: The presented compact-serialized JWT.

    Returns:
        The ``kid`` header value naming the JWKS key that signed the
        token.

    Raises:
        TokenInvalidError: With reason ``"malformed"`` when the value is
            not a parseable JWT, or ``"kid"`` when the header carries no
            usable key id.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise TokenInvalidError(_REASON_MALFORMED) from exc
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise TokenInvalidError(_REASON_KID)
    return kid


def _decode_claims(
    token: str, signing_key: jwt.PyJWK, issuer: str
) -> dict[str, object]:
    """Verify signature, expiry, issuer, and claim presence via PyJWT.

    Args:
        token: The presented compact-serialized JWT.
        signing_key: JWKS verification key matching the token's ``kid``.
        issuer: Expected ``iss`` value for the configured user pool.

    Returns:
        The verified claim set.

    Raises:
        TokenExpiredError: Phase ``"handshake"`` when ``exp`` has passed.
        TokenInvalidError: Reason ``"signature"`` on a signature mismatch,
            ``"issuer"`` on an ``iss`` mismatch, and ``"malformed"`` for
            every other verification failure (missing required claims,
            malformed segments, a disallowed algorithm, and so on).
    """
    try:
        claims: dict[str, object] = jwt.decode(
            token,
            key=signing_key.key,
            algorithms=_SIGNING_ALGORITHMS,
            issuer=issuer,
            # Cognito access tokens carry client_id instead of aud, so
            # audience verification is off; client_id is compared by the
            # caller. The literal stays inline so it typechecks against
            # both the dict-typed (2.10) and TypedDict-typed (2.13)
            # signatures of jwt.decode.
            options={"require": _REQUIRED_CLAIMS, "verify_aud": False},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError("handshake") from exc
    except jwt.InvalidSignatureError as exc:
        raise TokenInvalidError(_REASON_SIGNATURE) from exc
    except jwt.InvalidIssuerError as exc:
        raise TokenInvalidError(_REASON_ISSUER) from exc
    except jwt.InvalidTokenError as exc:
        raise TokenInvalidError(_REASON_MALFORMED) from exc
    return claims


def _parse_jwks(payload: object) -> dict[str, jwt.PyJWK]:
    """Convert a fetched JWKS document into verification keys by key id.

    Individual entries that cannot be converted are skipped, so one
    malformed entry never disables the remaining keys; a document without
    the JWKS shape is rejected outright.

    Args:
        payload: Decoded JSON body of the JWKS endpoint response.

    Returns:
        Mapping of ``kid`` to the parsed verification key for every
        usable entry (possibly empty).

    Raises:
        TokenInvalidError: With reason ``"jwks-unavailable"`` when the
            document is not an object carrying a ``"keys"`` array —
            validation fails closed on an unusable key set (Req 12.6).
    """
    if not isinstance(payload, dict):
        raise TokenInvalidError(_REASON_JWKS_UNAVAILABLE)
    entries = payload.get("keys")
    if not isinstance(entries, list):
        raise TokenInvalidError(_REASON_JWKS_UNAVAILABLE)
    keys: dict[str, jwt.PyJWK] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            key = jwt.PyJWK(entry)
        except _JWK_PARSE_ERRORS:
            continue
        kid = key.key_id
        if isinstance(kid, str) and kid:
            keys[kid] = key
    return keys


def _validated_token(claims: Mapping[str, object]) -> ValidatedToken:
    """Build the ``ValidatedToken`` result from a verified claim set.

    Args:
        claims: Claim set that already passed signature, expiry, issuer,
            client, and token-use verification.

    Returns:
        The immutable validation result, with ``username`` resolved from
        ``cognito:username``, then ``username``, then ``sub``.

    Raises:
        TokenInvalidError: With reason ``"malformed"`` when ``sub`` is not
            a non-empty string or ``exp`` is not numeric.
    """
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise TokenInvalidError(_REASON_MALFORMED)
    expiry = claims.get("exp")
    if not isinstance(expiry, int | float):
        raise TokenInvalidError(_REASON_MALFORMED)
    username = claims.get("cognito:username")
    if not isinstance(username, str) or not username:
        username = claims.get("username")
    resolved = username if isinstance(username, str) and username else sub
    return ValidatedToken(
        sub=sub,
        username=resolved,
        expires_at=float(expiry),
        claims=dict(claims),
    )


class CognitoJwtValidator:
    """Cognito access-token validation against the user pool JWKS.

    Verifies every token presented at the WebSocket handshake before the
    connection is accepted (Req 7.2): RS256 signature via the pool's
    JWKS, expiry, issuer, the ``client_id`` claim, and
    ``token_use == "access"``. Validation is fail-closed: any doubt —
    including an unreachable JWKS endpoint — raises, and the caller
    rejects the handshake without creating a Voice_Session (Req 7.3,
    12.5, 12.6).

    The JWKS is fetched asynchronously with ``httpx`` and cached by
    ``kid`` under a TTL; an unknown ``kid`` (key rotation) triggers
    exactly one refresh before the token is rejected. Transport uses a
    lazily created ``httpx.AsyncClient`` unless one is injected — the
    seam tests use to supply an ``httpx.MockTransport``. Call
    :meth:`aclose` at shutdown to release an owned client. Instances are
    safe for concurrent use: refreshes serialize behind an
    ``asyncio.Lock``.
    """

    def __init__(
        self,
        user_pool_id: str,
        client_id: str,
        region: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        jwks_ttl_seconds: float = 3600.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Initialize the validator for one user pool and app client.

        Args:
            user_pool_id: Cognito user pool whose JWKS signs accepted
                tokens (``Settings.cognito_user_pool_id``).
            client_id: App client identifier the ``client_id`` claim of
                every accepted token must equal
                (``Settings.cognito_client_id``).
            region: AWS region hosting the user pool, forming the issuer
                ``https://cognito-idp.{region}.amazonaws.com/{pool}``
                (``Settings.aws_region``).
            http_client: Optional preconfigured ``httpx.AsyncClient`` for
                JWKS fetches. An injected client's lifecycle belongs to
                its owner; :meth:`aclose` leaves it untouched.
            jwks_ttl_seconds: Age at which the cached key set counts as
                stale and is refetched before further validations.
            clock: Time source for cache-staleness bookkeeping only
                (defaults to ``time.monotonic``); token expiry itself is
                checked by PyJWT against the wall clock.
        """
        self._issuer = (
            f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
        )
        self._jwks_url = self._issuer + _JWKS_PATH
        self._client_id = client_id
        self._jwks_ttl_seconds = jwks_ttl_seconds
        self._clock: Callable[[], float] = (
            clock if clock is not None else time.monotonic
        )
        self._client = http_client
        self._owns_client = http_client is None
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._refresh_lock = asyncio.Lock()

    @property
    def issuer(self) -> str:
        """The ``iss`` value required of every accepted token."""
        return self._issuer

    @property
    def jwks_url(self) -> str:
        """The URL of the user pool's JWKS document."""
        return self._jwks_url

    async def validate(self, token: str) -> ValidatedToken:
        """Validate one presented access token, failing closed on doubt.

        Verifies, in order: token shape and ``kid``; RS256 signature
        against the JWKS key for that ``kid`` (fetching or refreshing the
        cached key set as needed); ``exp``, ``iss``, and required-claim
        presence; then the Cognito access-token profile — ``client_id``
        equal to the configured app client and ``token_use == "access"``.

        Args:
            token: The compact-serialized JWT presented in the ``bearer``
                subprotocol pair.

        Returns:
            The validation result; its ``expires_at`` is the deadline for
            the mid-session token-expiry watchdog (Req 7.6).

        Raises:
            TokenExpiredError: Phase ``"handshake"`` when the token is
                already expired (Req 7.3).
            TokenInvalidError: For every other failure, with reason
                ``"malformed"``, ``"signature"``, ``"issuer"``,
                ``"client_id"``, ``"token_use"``, ``"kid"``, or
                ``"jwks-unavailable"``; JWKS unavailability never admits
                a token (Req 12.6).
        """
        kid = _token_key_id(token)
        signing_key = await self._signing_key(kid)
        claims = _decode_claims(token, signing_key, self._issuer)
        if claims.get("client_id") != self._client_id:
            raise TokenInvalidError(_REASON_CLIENT_ID)
        if claims.get("token_use") != _ACCESS_TOKEN_USE:
            raise TokenInvalidError(_REASON_TOKEN_USE)
        return _validated_token(claims)

    async def aclose(self) -> None:
        """Close the lazily created HTTP client, if one exists.

        Closes only a client this validator created itself; an injected
        client's lifecycle belongs to its owner and is left untouched.
        Safe to call repeatedly — a later validation recreates the owned
        client on demand.
        """
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _signing_key(self, kid: str) -> jwt.PyJWK:
        """Return the verification key for ``kid``, refreshing at most once.

        Serves from the cached key set while it is fresh; otherwise —
        first use, TTL expiry, or an unknown ``kid`` after key rotation —
        performs one JWKS refresh under the lock and retries the lookup.

        Args:
            kid: Signing key id taken from the token header.

        Returns:
            The verification key for ``kid``.

        Raises:
            TokenInvalidError: Reason ``"kid"`` when the freshly refreshed
                key set still has no such key, or ``"jwks-unavailable"``
                when the refresh itself fails — a token is never checked
                against a stale or unconfirmed key set (Req 12.6).
        """
        key = self._cached_key(kid)
        if key is not None:
            return key
        async with self._refresh_lock:
            # Another task may have refreshed while this one awaited the
            # lock; re-check before fetching again.
            key = self._cached_key(kid)
            if key is None:
                await self._refresh_jwks()
                key = self._keys.get(kid)
        if key is None:
            raise TokenInvalidError(_REASON_KID)
        return key

    def _cached_key(self, kid: str) -> jwt.PyJWK | None:
        """Look up ``kid`` in the cached key set, honoring the TTL.

        Args:
            kid: Signing key id taken from the token header.

        Returns:
            The cached verification key, or ``None`` when the cache has
            never been filled, is older than the TTL, or has no such key.
        """
        if self._fetched_at is None:
            return None
        if self._clock() - self._fetched_at >= self._jwks_ttl_seconds:
            return None
        return self._keys.get(kid)

    async def _refresh_jwks(self) -> None:
        """Fetch the JWKS document and replace the cached key set.

        Raises:
            TokenInvalidError: With reason ``"jwks-unavailable"`` when the
                endpoint cannot be reached, answers non-2xx, or returns a
                body that is not a JWKS document; the previous cache is
                not reused, so validation fails closed (Req 12.6).
        """
        client = self._http_client()
        try:
            response = await client.get(self._jwks_url)
            response.raise_for_status()
            payload: object = response.json()
        except _JWKS_FETCH_ERRORS as exc:
            raise TokenInvalidError(_REASON_JWKS_UNAVAILABLE) from exc
        self._keys = _parse_jwks(payload)
        self._fetched_at = self._clock()

    def _http_client(self) -> httpx.AsyncClient:
        """Return the HTTP client, creating the owned one on first use.

        Returns:
            The injected client when one was provided, otherwise a lazily
            created ``httpx.AsyncClient`` bound to the JWKS fetch timeout.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(_JWKS_TIMEOUT_SECONDS)
            )
        return self._client
