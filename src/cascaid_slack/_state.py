"""Storage protocols for cascaid_slack stateful helpers.

The library's stateful helpers (rolling pins, date-grouped event logs) need to
persist a small amount of bookkeeping between calls. We don't want to lock
consumers into any particular DB -- automatic-charting uses Postgres,
minimal_reporting uses SQLite, a future project might use Redis or even a JSON
file. So storage is a Protocol the consumer implements.

Two ready-made implementations live in ``cascaid_slack.storage`` for the
common cases.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class PinRecord:
    """Bookkeeping for one rolling pinned message.

    Attributes:
        pin_key: Stable identifier provided by the consumer (e.g. "weekly_revenue").
        channel_id: Slack channel where the message currently lives.
        message_ts: Slack timestamp acting as the message ID.
        last_text_hash: Truncated SHA-256 of the last rendered body. Used for
            skip-if-unchanged short-circuiting on subsequent calls.
    """

    pin_key: str
    channel_id: str
    message_ts: str
    last_text_hash: Optional[str]


class PinStateStorage(Protocol):
    """Persistence for ``upsert_pinned_message`` rolling-pin state.

    Three methods, no transactions assumed. Implementations should be safe to
    call from a single thread; concurrent callers for the same pin_key are not
    supported (last write wins).
    """

    def load_pin(self, pin_key: str) -> Optional[PinRecord]:
        """Return the stored record for ``pin_key`` or None if never persisted."""
        ...

    def save_pin(self, record: PinRecord) -> None:
        """Persist ``record``, overwriting any prior row for the same pin_key."""
        ...

    def delete_pin(self, pin_key: str) -> None:
        """Drop the row for ``pin_key``. Used by the self-heal path when Slack returns

        message_not_found / channel_not_found from chat.update -- we forget the
        stale ts and fall back to a fresh post on the next call.
        """
        ...


@dataclass
class EventLogState:
    """Bookkeeping for one date-grouped event log channel.

    Attributes:
        channel_id: Slack channel hosting the log.
        last_header_date: ISO date string ('YYYY-MM-DD') of the most recently
            posted day-header message. Used to decide whether the next event
            needs a new date header.
        last_header_ts: Slack ts of the day-header message itself, so events
            can be threaded under it (when consumers want threading) or
            chained as siblings (when they don't).
    """

    channel_id: str
    last_header_date: Optional[str]
    last_header_ts: Optional[str]


class EventLogStorage(Protocol):
    """Persistence for ``send_slack_event``'s date-grouping state."""

    def load_event_log(self, channel_id: str) -> Optional[EventLogState]:
        """Return the stored state for ``channel_id`` or None on first use."""
        ...

    def save_event_log(self, state: EventLogState) -> None:
        """Persist ``state``, overwriting any prior row for the same channel_id."""
        ...
