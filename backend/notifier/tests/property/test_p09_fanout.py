# Feature: nova-sonic-support-portal, Property 9: Push fan-out is complete and self-cleaning
"""Property test: push fan-out is complete and self-cleaning.

**Validates: Requirements 6.2, 6.5**

For any set of registered Web_Push_Subscriptions and any assignment of
per-subscription delivery outcomes — immediate success, permanently gone
rejection (HTTP 404/410), transient failures that recover within the
retry budget, or transient failures that exhaust it — the Web Push
channel attempts delivery of the notification payload, carrying the
incident summary and the executionId (Req 6.2), to every subscription;
one subscription's failure never prevents attempts to the rest; exactly
the subscriptions rejected as gone are removed from the store, without
retry, and receive no further deliveries (Req 6.5); and other failures
are retried within the bounded budget and then discarded for that one
subscription only (Property 9).

The suite drives
:meth:`src.channels.webpush_sender.WebPushSender.send_to_all` through
its injection seams: ``webpush_fn`` is a scripted fake that plays each
subscription's drawn outcome plan — raising real
``pywebpush.WebPushException`` values carrying fake responses, or
transport-level ``OSError`` — and the repository is an in-memory
subclass of :class:`src.subscription_repo.SubscriptionRepository` whose
AWS wiring is never initialized, so no AWS access can occur; the
injected ``sleep`` skips retry backoff. Expected attempt counts are
derived from each plan: one for success, the leading transient failures
plus one for gone, failures-then-success for recovery, and the full
``retries + 1`` budget for exhaustion. A second fan-out after the first
asserts removed subscriptions are never attempted again while every
survivor is.

``hypothesis.given`` cannot drive ``async def`` tests under
pytest-asyncio, so each example runs its coroutine to completion with
``asyncio.run`` from a synchronous test body — the same pattern as the
voice service property suites, giving every example a fresh event loop.
"""

import asyncio
import json
from collections import Counter
from dataclasses import dataclass
from typing import Final, Literal, cast

from hypothesis import given, settings
from hypothesis import strategies as st
from pywebpush import WebPushException

from src.channels.webpush_sender import WebPushSender
from src.normalizer import IncidentNotification
from src.subscription_repo import StoredSubscription, SubscriptionRepository

_VAPID_PRIVATE_KEY: Final = "test-vapid-private-key-material"
"""Placeholder VAPID key; the scripted fake never performs cryptography."""

_VAPID_SUBJECT: Final = "mailto:oncall@example.com"
"""VAPID ``sub`` claim handed to the sender under test."""

_NOTIFICATION: Final = IncidentNotification(
    notification_id="9c1b7a52-3f66-4a0e-8f1d-2b7c9d4e5a01",
    source="devops-agent-finding",
    summary="Checkout latency breached the paging threshold",
    severity="high",
    timestamp="2024-01-01T00:00:00+00:00",
    execution_id="exec-42",
    detail=None,
)
"""Fixed notification fanned out in every example; carries an executionId
so the payload contract of Req 6.2 is observable on every delivery."""

_EXPECTED_PAYLOAD: Final = _NOTIFICATION.to_payload()
"""The exact wire payload every delivery attempt must carry (Req 6.2)."""

_GONE_STATUSES: Final = (404, 410)
"""Push-service statuses marking a subscription permanently gone (Req 6.5)."""

_TRANSIENT_STATUSES: Final = (400, 401, 413, 429, 500, 502, 503)
"""Non-gone push-service statuses classified as transient failures."""

_ENGINEER_IDS: Final = ("eng-alpha", "eng-beta", "eng-gamma")
"""Small engineer pool so subscriptions share partition keys sometimes."""

_MAX_RETRIES: Final = 3
"""Largest retry budget drawn; matches the shared policy default."""

_MAX_SUBSCRIPTIONS: Final = 6
"""Largest fan-out population drawn per example."""

_SCRIPTED_MESSAGE: Final = "scripted push failure"
"""Message carried by every scripted delivery exception."""

type _FailureKind = Literal["status", "no-response", "transport"]
"""How a scripted attempt fails: a push-service HTTP status, a
``WebPushException`` without any response, or a transport-level error."""


@dataclass(frozen=True, slots=True)
class _FakeResponse:
    """Minimal stand-in for the requests response pywebpush attaches.

    Exposes the two attributes the code under test and the exception's
    own rendering may read.

    Attributes:
        status_code: HTTP status the fake push service answered with.
        text: Response body text, present so stringifying the exception
            never fails.
    """

    status_code: int
    text: str = "scripted response body"


@dataclass(frozen=True, slots=True)
class _FailureSpec:
    """One scripted failing delivery attempt.

    Attributes:
        kind: How the attempt fails — with a push-service HTTP status,
            with a response-less ``WebPushException``, or with a
            transport-level ``OSError``.
        status: The HTTP status for the ``status`` kind; ``None`` for
            the response-less and transport kinds.
    """

    kind: _FailureKind
    status: int | None

    def to_exception(self) -> Exception:
        """Build a fresh exception for one scripted failing attempt.

        Returns:
            A transport-level ``ConnectionError`` for the ``transport``
            kind, otherwise a real ``pywebpush.WebPushException`` — with
            a fake response carrying ``status`` for the ``status`` kind,
            or without any response for the ``no-response`` kind.
        """
        if self.kind == "transport":
            return ConnectionError(_SCRIPTED_MESSAGE)
        if self.kind == "no-response":
            return cast("Exception", WebPushException(_SCRIPTED_MESSAGE))
        return cast(
            "Exception",
            WebPushException(
                _SCRIPTED_MESSAGE,
                response=_FakeResponse(status_code=cast("int", self.status)),
            ),
        )


@dataclass(frozen=True, slots=True)
class _SubscriptionCase:
    """One stored subscription with its scripted outcome and expectations.

    Attributes:
        subscription: The stored Web_Push_Subscription handed to the
            fake repository.
        endpoint: The subscription's push endpoint URL, the key the
            scripted delivery fake identifies calls by.
        failures: Scripted failing attempts, in order; the attempt after
            the last scripted failure succeeds.
        expected_attempts: Exact number of delivery attempts the sender
            must make to this subscription during the first fan-out.
        expect_deleted: Whether the subscription must be removed from
            the store (true exactly for gone outcomes, Req 6.5).
    """

    subscription: StoredSubscription
    endpoint: str
    failures: tuple[_FailureSpec, ...]
    expected_attempts: int
    expect_deleted: bool


@dataclass(frozen=True, slots=True)
class _FanoutCase:
    """One drawn fan-out scenario.

    Attributes:
        retries: Retry budget configured on the sender under test.
        cases: The subscriptions with their scripted outcomes.
    """

    retries: int
    cases: tuple[_SubscriptionCase, ...]


@dataclass(frozen=True, slots=True)
class _RecordedCall:
    """One delivery attempt observed by the scripted webpush fake.

    Attributes:
        endpoint: Push endpoint URL the attempt targeted.
        data: The serialized payload handed to the delivery callable.
        subscription_info: The subscription mapping handed to the
            delivery callable.
    """

    endpoint: str
    data: str
    subscription_info: dict[str, object]


class _FakeSubscriptionRepository(SubscriptionRepository):
    """In-memory stand-in for the DynamoDB push-subscriptions repository.

    Overrides every operation the sender uses with dict-backed versions
    and deliberately never calls the base initializer, so the adapter's
    aioboto3 wiring is never created and no AWS access can occur.
    """

    def __init__(self, subscriptions: list[StoredSubscription]) -> None:
        """Seed the store with the drawn subscriptions.

        Args:
            subscriptions: The registered Web_Push_Subscriptions the
                fan-out must reach.
        """
        self._items: dict[tuple[str, str], StoredSubscription] = {
            (sub.engineer_id, sub.endpoint_hash): sub for sub in subscriptions
        }
        self.deleted: list[tuple[str, str]] = []

    async def list_all(self) -> list[StoredSubscription]:
        """Return every currently stored subscription.

        Returns:
            The stored subscriptions, as a fresh list.
        """
        return list(self._items.values())

    async def delete(self, engineer_id: str, endpoint_hash: str) -> None:
        """Remove one subscription record, recording the deletion.

        Args:
            engineer_id: Partition key of the record to remove.
            endpoint_hash: Sort key of the record to remove.
        """
        self.deleted.append((engineer_id, endpoint_hash))
        self._items.pop((engineer_id, endpoint_hash), None)

    def remaining_keys(self) -> set[tuple[str, str]]:
        """Return the keys of the subscriptions still stored.

        Returns:
            The ``(engineer_id, endpoint_hash)`` pairs still present.
        """
        return set(self._items)


class _FakeWebPush:
    """Scripted stand-in for ``pywebpush.webpush``.

    Called by the sender through ``asyncio.to_thread`` with the seam's
    keyword arguments. Each subscription (identified by the endpoint in
    ``subscription_info``) plays its scripted failures in order; every
    attempt past the script succeeds. Per-subscription attempts are
    sequential inside the sender's retry loop, so the per-endpoint
    counters are safe under the concurrent fan-out.
    """

    def __init__(self, scripts: dict[str, tuple[_FailureSpec, ...]]) -> None:
        """Initialize the fake with one failure script per endpoint.

        Args:
            scripts: Scripted failing attempts keyed by push endpoint
                URL; an absent or exhausted script means success.
        """
        self._scripts = scripts
        self._attempt_counts: dict[str, int] = {}
        self.calls: list[_RecordedCall] = []

    def __call__(
        self,
        *,
        subscription_info: dict[str, object],
        data: str,
        vapid_private_key: str,
        vapid_claims: dict[str, str],
    ) -> None:
        """Record one delivery attempt and play the scripted outcome.

        Args:
            subscription_info: The stored subscription mapping; its
                ``endpoint`` identifies whose script applies.
            data: The serialized notification payload being delivered.
            vapid_private_key: VAPID key material; accepted, unused.
            vapid_claims: VAPID claims dict; accepted, unused.

        Raises:
            Exception: The scripted exception for this attempt — a
                ``WebPushException`` or a transport-level ``OSError`` —
                when the subscription's script has not been exhausted.
        """
        endpoint = subscription_info["endpoint"]
        assert isinstance(endpoint, str)
        made = self._attempt_counts.get(endpoint, 0)
        self._attempt_counts[endpoint] = made + 1
        self.calls.append(
            _RecordedCall(
                endpoint=endpoint, data=data, subscription_info=subscription_info
            )
        )
        failures = self._scripts.get(endpoint, ())
        if made < len(failures):
            raise failures[made].to_exception()

    def attempt_counts(self) -> Counter[str]:
        """Tally the recorded delivery attempts per endpoint.

        Returns:
            Counter mapping each attempted endpoint to its attempt count.
        """
        return Counter(call.endpoint for call in self.calls)

    def reset_to_all_success(self) -> None:
        """Clear recorded calls and scripts so future deliveries succeed."""
        self._scripts = {}
        self._attempt_counts.clear()
        self.calls.clear()


async def _instant_sleep(_delay: float) -> None:
    """Skip retry backoff so scripted retries complete instantly.

    Args:
        _delay: Requested backoff delay in seconds; ignored.
    """


_transient_specs: Final = st.one_of(
    st.sampled_from(_TRANSIENT_STATUSES).map(
        lambda status: _FailureSpec(kind="status", status=status)
    ),
    st.just(_FailureSpec(kind="no-response", status=None)),
    st.just(_FailureSpec(kind="transport", status=None)),
)
"""One transient failing attempt: a non-gone status, a response-less
``WebPushException``, or a transport-level error."""

_gone_specs: Final = st.sampled_from(_GONE_STATUSES).map(
    lambda status: _FailureSpec(kind="status", status=status)
)
"""One permanently-gone rejection: HTTP 404 or 410 (Req 6.5)."""


@st.composite
def _fanout_cases(draw: st.DrawFn) -> _FanoutCase:
    """Draw one fan-out scenario with mixed per-subscription outcomes.

    Draws a retry budget and a population of subscriptions, assigning
    each an outcome: immediate success; permanently gone (optionally
    after leading transient failures that stay within the budget);
    transient failures that recover within the budget (only drawable
    when the budget allows a retry); or transient failures exhausting
    the whole budget. Expected attempt counts and deletions are derived
    alongside each plan.

    Args:
        draw: Hypothesis draw function supplied by ``st.composite``.

    Returns:
        The drawn scenario with its per-subscription expectations.
    """
    retries = draw(st.integers(min_value=0, max_value=_MAX_RETRIES))
    outcomes = ("success", "gone", "exhaust") + (("recover",) if retries else ())
    population = draw(st.integers(min_value=0, max_value=_MAX_SUBSCRIPTIONS))
    cases: list[_SubscriptionCase] = []
    for index in range(population):
        endpoint_hash = f"hash-{index:02d}"
        endpoint = f"https://push.example.test/{endpoint_hash}"
        subscription = StoredSubscription(
            engineer_id=draw(st.sampled_from(_ENGINEER_IDS)),
            endpoint_hash=endpoint_hash,
            subscription={
                "endpoint": endpoint,
                "keys": {"p256dh": f"p256-{index:02d}", "auth": f"auth-{index:02d}"},
            },
        )
        outcome = draw(st.sampled_from(outcomes))
        if outcome == "success":
            failures: tuple[_FailureSpec, ...] = ()
            expected_attempts = 1
        elif outcome == "gone":
            leading = draw(st.integers(min_value=0, max_value=retries))
            failures = tuple(
                [draw(_transient_specs) for _ in range(leading)] + [draw(_gone_specs)]
            )
            expected_attempts = leading + 1
        elif outcome == "recover":
            failing = draw(st.integers(min_value=1, max_value=retries))
            failures = tuple(draw(_transient_specs) for _ in range(failing))
            expected_attempts = failing + 1
        else:  # exhaust: every allowed attempt fails transiently
            failures = tuple(draw(_transient_specs) for _ in range(retries + 1))
            expected_attempts = retries + 1
        cases.append(
            _SubscriptionCase(
                subscription=subscription,
                endpoint=endpoint,
                failures=failures,
                expected_attempts=expected_attempts,
                expect_deleted=outcome == "gone",
            )
        )
    return _FanoutCase(retries=retries, cases=tuple(cases))


def _build(
    case: _FanoutCase,
) -> tuple[_FakeSubscriptionRepository, _FakeWebPush, WebPushSender]:
    """Wire a sender under test to fakes seeded from the drawn scenario.

    Args:
        case: The drawn fan-out scenario.

    Returns:
        The fake repository, the scripted delivery fake, and the sender.
    """
    repo = _FakeSubscriptionRepository([sub.subscription for sub in case.cases])
    push = _FakeWebPush({sub.endpoint: sub.failures for sub in case.cases})
    sender = WebPushSender(
        _VAPID_PRIVATE_KEY,
        _VAPID_SUBJECT,
        repo,
        retries=case.retries,
        webpush_fn=push,
        sleep=_instant_sleep,
    )
    return repo, push, sender


async def _check_complete_fanout(case: _FanoutCase) -> None:
    """Assert every subscription is attempted exactly per its outcome.

    Runs one fan-out and asserts: the attempted endpoints are exactly
    the registered ones — one subscription's failure never suppresses
    another's attempt; each subscription's attempt count matches its
    plan (one for success, no retry after a gone rejection, bounded
    retries otherwise); and every single attempt carried the exact wire
    payload with the incident summary and the executionId (Req 6.2).

    Args:
        case: The drawn fan-out scenario.

    Raises:
        AssertionError: If any facet of the complete-fan-out contract is
            violated for this scenario.
    """
    _, push, sender = _build(case)

    await sender.send_to_all(_NOTIFICATION)

    observed = push.attempt_counts()
    assert set(observed) == {sub.endpoint for sub in case.cases}
    expected = Counter({sub.endpoint: sub.expected_attempts for sub in case.cases})
    assert observed == expected

    infos = {sub.endpoint: dict(sub.subscription.subscription) for sub in case.cases}
    for call in push.calls:
        payload = json.loads(call.data)
        assert payload == _EXPECTED_PAYLOAD
        assert payload["summary"] == _NOTIFICATION.summary
        assert payload["executionId"] == _NOTIFICATION.execution_id
        assert call.subscription_info == infos[call.endpoint]


async def _check_self_cleaning(case: _FanoutCase) -> None:
    """Assert exactly the gone subscriptions are removed, then skipped.

    Runs one fan-out and asserts the store lost exactly the
    subscriptions the push service rejected as gone — each deleted once,
    none of the successful or transiently failing ones (Req 6.5). A
    second fan-out (with every remaining delivery succeeding) then
    asserts removed subscriptions receive no further deliveries while
    every survivor is attempted exactly once.

    Args:
        case: The drawn fan-out scenario.

    Raises:
        AssertionError: If a deletion is missing or excessive, or a
            removed subscription is attempted again.
    """
    repo, push, sender = _build(case)
    all_keys = {
        (sub.subscription.engineer_id, sub.subscription.endpoint_hash)
        for sub in case.cases
    }
    gone_keys = {
        (sub.subscription.engineer_id, sub.subscription.endpoint_hash)
        for sub in case.cases
        if sub.expect_deleted
    }

    await sender.send_to_all(_NOTIFICATION)

    assert set(repo.deleted) == gone_keys
    assert len(repo.deleted) == len(gone_keys)
    assert repo.remaining_keys() == all_keys - gone_keys

    push.reset_to_all_success()
    await sender.send_to_all(_NOTIFICATION)

    survivors = Counter(
        {sub.endpoint: 1 for sub in case.cases if not sub.expect_deleted}
    )
    assert push.attempt_counts() == survivors
    assert set(repo.deleted) == gone_keys
    assert repo.remaining_keys() == all_keys - gone_keys


@given(case=_fanout_cases())
@settings(max_examples=100, deadline=None)
def test_fanout_attempts_every_subscription_exactly_per_outcome(
    case: _FanoutCase,
) -> None:
    """Fan-out reaches every subscription with the full payload.

    For any subscription population and outcome mix, the sender attempts
    delivery to every registered subscription — one subscription's
    failure never blocks the rest — making exactly one attempt on
    success, exactly one non-retried attempt for a gone rejection after
    its leading transient failures, and at most ``retries + 1`` bounded
    attempts otherwise, while every attempt carries the payload with the
    incident summary and the executionId (Req 6.2, Property 9).

    Args:
        case: Drawn fan-out scenario with mixed delivery outcomes.
    """
    asyncio.run(_check_complete_fanout(case))


@given(case=_fanout_cases())
@settings(max_examples=100, deadline=None)
def test_fanout_removes_exactly_gone_subscriptions_and_stops_delivering(
    case: _FanoutCase,
) -> None:
    """Exactly the gone subscriptions are removed and never re-attempted.

    For any outcome mix, subscriptions the push service rejects with
    HTTP 404 or 410 are deleted from the store — each exactly once, and
    no successful or transiently failing subscription with them — and a
    subsequent fan-out attempts every surviving subscription exactly
    once while the removed ones receive no further deliveries
    (Req 6.5, Property 9).

    Args:
        case: Drawn fan-out scenario with mixed delivery outcomes.
    """
    asyncio.run(_check_self_cleaning(case))
