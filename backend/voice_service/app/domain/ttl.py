"""TTL computation for Session_Store records (Req 8.4).

The Session_Store applies a DynamoDB time-to-live attribute ``ttl`` to
Voice_Session and transcript records (agent-chat records are aligned with
their owning session) so that expired records are removed automatically.
Per design Property 13, the attribute equals the record's last update time
plus a configurable retention period defaulting to
``DEFAULT_RETENTION_DAYS`` (30 days) when unconfigured, expressed in epoch
seconds.

This is a pure domain module: no I/O, no SDK imports, and no clock reads —
the update time is always injected by the caller, so identical inputs
always yield the identical TTL (Req 17.6). Two entry points cover the two
caller shapes:

- :func:`compute_ttl` takes a timezone-aware :class:`datetime.datetime`
  in any UTC offset; the arithmetic honors the offset exactly.
- :func:`compute_ttl_from_epoch` takes whole epoch seconds directly, for
  callers such as the DynamoDB store adapter that have already converted
  a record's ISO-8601 ``updated_at`` string.

Sub-second precision is floored: the update time is reduced to whole epoch
seconds via exact integer arithmetic on ``timedelta`` fields rather than
float ``datetime.timestamp()``, whose rounding can shift a second at
extreme dates. Whole seconds match DynamoDB's TTL format.

A naive datetime (one without a usable UTC offset) has no defined epoch
value, so :func:`compute_ttl` raises ``NaiveDatetimeError`` instead of
guessing a timezone. Like ``IllegalTransitionError`` in
``domain.session``, that error extends the shared ``PortalError``
hierarchy (Req 17.4) and is defined locally because it guards a contract
internal to this module; its message is built inside the class from typed
context, so call sites never format message strings.

The retention period is used as supplied: the configuration loader
validates the configured value as a positive integer at startup
(Req 14.4), and the arithmetic here is total over all integers.
"""

from datetime import UTC, datetime
from typing import Final

from shared.exceptions import PortalError

__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "SECONDS_PER_DAY",
    "NaiveDatetimeError",
    "compute_ttl",
    "compute_ttl_from_epoch",
]

SECONDS_PER_DAY: Final = 24 * 60 * 60
"""Seconds in one day (86,400), converting retention days to seconds."""

DEFAULT_RETENTION_DAYS: Final = 30
"""Retention period in days applied when none is configured (Req 8.4)."""

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
"""The Unix epoch as an aware UTC datetime, anchor of the exact conversion."""


class NaiveDatetimeError(PortalError):
    """A TTL computation received a naive datetime (Req 8.4).

    Raised by :func:`compute_ttl` when ``updated_at`` carries no usable
    UTC offset: a naive datetime has no defined epoch value, and guessing
    a timezone could silently shift a record's expiry. This class extends
    the shared ``PortalError`` hierarchy (Req 17.4) and is defined locally
    because it guards a contract internal to this domain module, mirroring
    ``IllegalTransitionError`` in ``domain.session``.

    Attributes:
        value: The naive datetime that was rejected.
    """

    def __init__(self, value: datetime) -> None:
        """Initialize the error with the rejected naive datetime.

        Args:
            value: The naive datetime that was rejected.
        """
        self.value = value
        super().__init__(
            f"TTL computation requires a timezone-aware datetime; "
            f"got naive {value.isoformat()!r}"
        )


def compute_ttl_from_epoch(
    updated_at_epoch: int,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> int:
    """Compute a record's TTL from its last update time in epoch seconds.

    Args:
        updated_at_epoch: The record's last update time as whole epoch
            seconds, for example the store adapter's conversion of the
            record's ISO-8601 ``updated_at`` string.
        retention_days: Configured retention period in days; defaults to
            ``DEFAULT_RETENTION_DAYS`` (30) when unconfigured (Req 8.4).

    Returns:
        The DynamoDB ``ttl`` attribute value: ``updated_at_epoch`` plus
        the retention period, in epoch seconds.
    """
    return updated_at_epoch + retention_days * SECONDS_PER_DAY


def compute_ttl(
    updated_at: datetime,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> int:
    """Compute a record's TTL from its last update time as a datetime.

    The update time may carry any UTC offset; subtracting the Unix epoch
    converts it exactly (integer ``timedelta`` fields), flooring
    sub-second precision to whole epoch seconds.

    Args:
        updated_at: Timezone-aware last update time of the record.
        retention_days: Configured retention period in days; defaults to
            ``DEFAULT_RETENTION_DAYS`` (30) when unconfigured (Req 8.4).

    Returns:
        The DynamoDB ``ttl`` attribute value: the update time plus the
        retention period, in epoch seconds.

    Raises:
        NaiveDatetimeError: If ``updated_at`` is naive (its ``tzinfo`` is
            missing or yields no UTC offset), because a naive value has
            no defined epoch seconds.
    """
    if updated_at.tzinfo is None or updated_at.tzinfo.utcoffset(updated_at) is None:
        raise NaiveDatetimeError(updated_at)
    since_epoch = updated_at - _EPOCH
    updated_at_epoch = since_epoch.days * SECONDS_PER_DAY + since_epoch.seconds
    return compute_ttl_from_epoch(updated_at_epoch, retention_days)
