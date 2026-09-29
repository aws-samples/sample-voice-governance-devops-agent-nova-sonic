# Feature: nova-sonic-support-portal, Property 13: TTL equals last update plus retention
"""Property test: TTL equals last update plus retention.

**Validates: Requirements 8.4**

For any record update time and any configured retention period (defaulting
to 30 days when unconfigured), the computed TTL attribute equals the
update time plus the retention period, expressed in epoch seconds
(Property 13).

Both entry points of ``app.domain.ttl`` are driven directly:

- :func:`~app.domain.ttl.compute_ttl_from_epoch` over a wide integer band
  reaching beyond the 32-bit range on both sides — the arithmetic is
  exact and total, so the TTL equation is asserted verbatim.
- :func:`~app.domain.ttl.compute_ttl` over aware datetimes built by
  rendering an epoch instant in arbitrary fixed whole-minute UTC offsets.
  Fixed offsets cover every offset a real-world zone can carry without
  depending on the tzdata package, and the instant band is held one day
  inside the representable calendar so no offset rendering can overflow
  ``datetime``.

The datetime path is checked for offset invariance (the same instant in
any offset yields the same TTL), sub-second flooring (injected
microseconds never move the TTL), agreement with an independent
``calendar.timegm`` oracle that shares no arithmetic with the
implementation, and the 30-day default retention. Naive update times —
including the degenerate case of a ``tzinfo`` whose ``utcoffset`` is
``None`` — have no defined epoch value and must be rejected with
``NaiveDatetimeError`` from the shared ``PortalError`` hierarchy rather
than silently interpreted in a guessed timezone.
"""

import calendar
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from shared.exceptions import PortalError

from app.domain.ttl import (
    DEFAULT_RETENTION_DAYS,
    SECONDS_PER_DAY,
    NaiveDatetimeError,
    compute_ttl,
    compute_ttl_from_epoch,
)

_DATETIME_MIN_EPOCH: Final = calendar.timegm(
    datetime.min.replace(tzinfo=UTC).utctimetuple()
)
"""Epoch seconds of ``datetime.min`` (0001-01-01T00:00:00) read as UTC."""

_DATETIME_MAX_EPOCH: Final = calendar.timegm(
    datetime.max.replace(tzinfo=UTC).utctimetuple()
)
"""Epoch seconds of ``datetime.max`` (9999-12-31T23:59:59) read as UTC."""

_MAX_OFFSET_MINUTES: Final = 24 * 60 - 1
"""Largest whole-minute UTC offset ``datetime.timezone`` accepts (23:59)."""

_wide_epochs: Final = st.integers(-(2**31), 2**33)
"""Update times in epoch seconds, well beyond the 32-bit range both ways."""

_retention_days: Final = st.integers(1, 3650)
"""Configured retention periods from one day to ten years."""

_aware_epochs: Final = st.integers(
    _DATETIME_MIN_EPOCH + SECONDS_PER_DAY,
    _DATETIME_MAX_EPOCH - SECONDS_PER_DAY,
)
"""Instants held one day inside the representable calendar, so rendering
them in any offset from ``_utc_offsets`` never overflows ``datetime``."""

_utc_offsets: Final = st.integers(-_MAX_OFFSET_MINUTES, _MAX_OFFSET_MINUTES).map(
    lambda minutes: timezone(timedelta(minutes=minutes))
)
"""Fixed timezones covering every whole-minute UTC offset, tzdata-free."""

_microseconds: Final = st.integers(0, 999_999)
"""Sub-second components injected to exercise flooring."""


class _NoUtcOffsetZone(tzinfo):
    """A degenerate timezone whose UTC offset is unknown.

    ``datetime.tzinfo`` permits ``utcoffset`` to return ``None``; a
    datetime carrying such a zone is naive in effect because it has no
    defined epoch value, and TTL computation must reject it exactly like
    a datetime with no ``tzinfo`` at all.
    """

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        """Return ``None``: this zone yields no usable UTC offset.

        Args:
            dt: The datetime the offset is requested for (unused).

        Returns:
            Always ``None``.
        """
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        """Return ``None``: this zone declares no DST information.

        Args:
            dt: The datetime the DST offset is requested for (unused).

        Returns:
            Always ``None``.
        """
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        """Return ``None``: this zone declares no name.

        Args:
            dt: The datetime the name is requested for (unused).

        Returns:
            Always ``None``.
        """
        return None


def _moment_at(epoch: int, tz: timezone, microsecond: int = 0) -> datetime:
    """Render the instant ``epoch`` as an aware datetime in offset ``tz``.

    Args:
        epoch: The instant in whole epoch seconds.
        tz: The fixed UTC offset the wall clock is rendered in.
        microsecond: Sub-second component injected after rendering, moving
            the represented instant to ``epoch`` plus a fraction that the
            TTL computation must floor away.

    Returns:
        The aware datetime whose floored epoch seconds equal ``epoch``.
    """
    return datetime.fromtimestamp(epoch, tz=tz).replace(microsecond=microsecond)


@given(epoch=_wide_epochs, retention_days=_retention_days)
@settings(max_examples=100, deadline=None)
def test_epoch_ttl_equals_update_plus_retention(
    epoch: int, retention_days: int
) -> None:
    """The epoch entry point implements the TTL equation verbatim.

    For any update time and retention period, the computed attribute is
    exactly the update time plus the retention period in seconds; the day
    constant is pinned to 86,400 so a wrong unit in the implementation
    cannot cancel out against the same wrong unit in the test.

    Args:
        epoch: Update time in epoch seconds over a wide integer band.
        retention_days: Configured retention period in days.
    """
    assert SECONDS_PER_DAY == 86_400
    ttl = compute_ttl_from_epoch(epoch, retention_days)
    assert ttl == epoch + retention_days * 86_400
    assert ttl - epoch == retention_days * SECONDS_PER_DAY


@given(epoch=_wide_epochs, aware_epoch=_aware_epochs, tz=_utc_offsets)
@settings(max_examples=100, deadline=None)
def test_default_retention_is_thirty_days(
    epoch: int, aware_epoch: int, tz: timezone
) -> None:
    """Omitting the retention period applies the 30-day default on both paths.

    Calling either entry point without a retention period must equal both
    the explicit 30-day equation and an explicit call with
    ``DEFAULT_RETENTION_DAYS`` (Req 8.4: default 30 days when
    unconfigured).

    Args:
        epoch: Update time in epoch seconds for the epoch entry point.
        aware_epoch: Instant rendered as a datetime for the datetime
            entry point, held inside the representable calendar.
        tz: Fixed UTC offset the datetime is rendered in.
    """
    assert DEFAULT_RETENTION_DAYS == 30
    default_ttl = compute_ttl_from_epoch(epoch)
    assert default_ttl == epoch + 30 * SECONDS_PER_DAY
    assert default_ttl == compute_ttl_from_epoch(epoch, DEFAULT_RETENTION_DAYS)

    moment = _moment_at(aware_epoch, tz)
    assert compute_ttl(moment) == aware_epoch + 30 * SECONDS_PER_DAY
    assert compute_ttl(moment) == compute_ttl(moment, DEFAULT_RETENTION_DAYS)


@given(
    epoch=_aware_epochs,
    retention_days=_retention_days,
    first_tz=_utc_offsets,
    second_tz=_utc_offsets,
)
@settings(max_examples=100, deadline=None)
def test_datetime_ttl_is_offset_invariant(
    epoch: int, retention_days: int, first_tz: timezone, second_tz: timezone
) -> None:
    """Rendering the same instant in any UTC offset yields the same TTL.

    The TTL depends only on the instant the update time denotes, never on
    the offset it happens to be expressed in: UTC and two arbitrary fixed
    offsets all produce the update time plus the retention period.

    Args:
        epoch: The instant in whole epoch seconds.
        retention_days: Configured retention period in days.
        first_tz: One fixed UTC offset rendering the instant.
        second_tz: Another fixed UTC offset rendering the same instant.
    """
    expected = epoch + retention_days * SECONDS_PER_DAY
    assert compute_ttl(_moment_at(epoch, UTC), retention_days) == expected
    assert compute_ttl(_moment_at(epoch, first_tz), retention_days) == expected
    assert compute_ttl(_moment_at(epoch, second_tz), retention_days) == expected


@given(
    epoch=_aware_epochs,
    retention_days=_retention_days,
    tz=_utc_offsets,
    microsecond=_microseconds,
)
@settings(max_examples=100, deadline=None)
def test_subsecond_precision_is_floored(
    epoch: int, retention_days: int, tz: timezone, microsecond: int
) -> None:
    """Sub-second precision on the update time never moves the TTL.

    An update time of ``epoch`` plus any microsecond fraction floors to
    ``epoch``, so the computed TTL equals the whole-second equation
    regardless of the fraction.

    Args:
        epoch: The instant in whole epoch seconds.
        retention_days: Configured retention period in days.
        tz: Fixed UTC offset the wall clock is rendered in.
        microsecond: Sub-second component injected into the update time.
    """
    moment = _moment_at(epoch, tz, microsecond)
    expected = epoch + retention_days * SECONDS_PER_DAY
    assert compute_ttl(moment, retention_days) == expected


@given(
    epoch=_aware_epochs,
    retention_days=_retention_days,
    tz=_utc_offsets,
    microsecond=_microseconds,
)
@settings(max_examples=100, deadline=None)
def test_datetime_and_epoch_entry_points_agree(
    epoch: int, retention_days: int, tz: timezone, microsecond: int
) -> None:
    """Both entry points agree through an independent epoch oracle.

    The datetime path must equal the epoch path applied to the update
    time's floored epoch seconds as derived by ``calendar.timegm`` — an
    oracle sharing no arithmetic with the implementation — and that
    oracle must recover exactly the instant the datetime was built from.

    Args:
        epoch: The instant in whole epoch seconds.
        retention_days: Configured retention period in days.
        tz: Fixed UTC offset the wall clock is rendered in.
        microsecond: Sub-second component exercising the flooring path.
    """
    moment = _moment_at(epoch, tz, microsecond)
    oracle_epoch = calendar.timegm(moment.utctimetuple())
    assert oracle_epoch == epoch
    assert compute_ttl(moment, retention_days) == compute_ttl_from_epoch(
        oracle_epoch, retention_days
    )


@given(
    naive=st.datetimes(),
    retention_days=_retention_days,
    degenerate_zone=st.booleans(),
)
@settings(max_examples=100, deadline=None)
def test_naive_update_time_is_rejected(
    naive: datetime, retention_days: int, degenerate_zone: bool
) -> None:
    """Update times without a usable UTC offset raise ``NaiveDatetimeError``.

    A naive datetime has no defined epoch value, so instead of guessing a
    timezone the computation raises the specific ``PortalError`` subclass
    carrying the rejected value — both when ``tzinfo`` is absent entirely
    and for the degenerate zone whose ``utcoffset`` is ``None``.

    Args:
        naive: An arbitrary naive datetime.
        retention_days: Configured retention period in days.
        degenerate_zone: Whether to attach the zone with no UTC offset
            instead of leaving ``tzinfo`` absent.
    """
    value = naive.replace(tzinfo=_NoUtcOffsetZone()) if degenerate_zone else naive
    with pytest.raises(NaiveDatetimeError) as excinfo:
        compute_ttl(value, retention_days)
    assert isinstance(excinfo.value, PortalError)
    assert excinfo.value.value is value
