"""Date-grouped event log for Slack: append events under a daily date banner.

Companion to ``cascaid_slack.pins``. Where ``pins`` keeps ONE rolling
message per "report kind" (and chat.updates it), this module keeps an
append-only event log -- but inserts a bold

    *--- Tuesday, June 16, 2026 ---*

separator the first time anything posts on a given calendar day. The
result is a channel that reads like a chronological journal: a date
banner, then events under it, then tomorrow's banner, etc.

Single entry point::

    send_slack_event(notifier, storage, text="Completed 71 navigator tasks")

State machine per channel_id:

    (no record)                        -> post header + event, save today
    (record, last_header_date == today) -> just post event
    (record, last_header_date != today) -> post header + event, save today
    (header post fails)                -> still try to post the event;
                                          don't save state (next call retries
                                          the header)

Date arithmetic uses host-local time (``datetime.now()``). Pass ``now=``
for tests or a timezone-fixed wall clock if business-day boundaries ever
matter for compliance.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

from slack_sdk.errors import SlackApiError

from ._state import EventLogState, EventLogStorage
from .notifier import NotificationService

logger = logging.getLogger(__name__)


def _format_date_header(today: date) -> str:
    """Render the bold date separator. Example: ``*--- Tuesday, June 16, 2026 ---*``.

    Uses ``%-d`` (no leading zero) -- POSIX-only but matches the original
    behaviour. If we ever need Windows support, swap to ``str(today.day)``.
    """
    return f"*--- {today.strftime('%A, %B %-d, %Y')} ---*"


def _post(notifier: NotificationService, channel: str, text: str) -> bool:
    """Single chat.postMessage that returns True/False instead of raising."""
    client = notifier.client
    if client is None:
        return False
    try:
        client.chat_postMessage(channel=channel, text=text)
        return True
    except SlackApiError as exc:
        logger.error(
            "[cascaid_slack.events] chat.postMessage failed: %s",
            exc.response.get("error", "unknown"),
        )
        return False


def ensure_date_header(
    notifier: NotificationService,
    storage: EventLogStorage,
    *,
    channel_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> bool:
    """Post today's date banner row if it isn't already on the channel.

    Idempotent within a calendar day. Pull this out of ``send_slack_event``
    so callers that post their own content (e.g. ``send_slack_file`` with an
    initial_comment) can still get the date-banner treatment without
    double-posting.

    Args:
        notifier: ``NotificationService`` -- provides the WebClient + default channel.
        storage: ``EventLogStorage`` for the per-channel last-header bookkeeping.
        channel_id: Override the notifier's default channel for this one call.
        now: Test override for "right now".

    Returns:
        True when today's banner is already on the channel or we just posted
        one. False when Slack is unconfigured, the storage read failed, or
        the banner post itself failed.
    """
    channel = channel_id or notifier.slack_channel_id
    if notifier.client is None or not channel:
        return False

    today = (now or datetime.now()).date()
    today_iso = today.isoformat()

    try:
        state = storage.load_event_log(channel)
    except Exception:  # noqa: BLE001 -- storage hiccup must not break the caller
        logger.warning(
            "[cascaid_slack.events] could not read event-log state", exc_info=True
        )
        return False

    if state and state.last_header_date == today_iso:
        return True

    if not _post(notifier, channel, _format_date_header(today)):
        # Don't persist the date if the post failed -- next call retries.
        logger.warning(
            "[cascaid_slack.events] date header post failed for %s on %s",
            channel,
            today_iso,
        )
        return False

    storage.save_event_log(
        EventLogState(
            channel_id=channel,
            last_header_date=today_iso,
            last_header_ts=None,  # we don't capture ts for the header today
        )
    )
    return True


def send_slack_event(
    notifier: NotificationService,
    storage: EventLogStorage,
    *,
    text: str,
    channel_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> bool:
    """Post ``text`` to Slack, prepending a date-header if today's banner is missing.

    Args:
        notifier: ``NotificationService`` with the WebClient + default channel.
        storage: ``EventLogStorage`` persisting last-header state per channel.
        text: Event body (e.g. ``"Completed: 71 navigator tasks created"``).
        channel_id: Override the notifier's default channel.
        now: Test override for "right now".

    Returns:
        True if the event message was delivered. The date-header step is
        best-effort: if the header post fails, we still try the event,
        because losing one banner is better than losing the event itself.
        Returns False only if the event post fails or Slack is unconfigured.
    """
    channel = channel_id or notifier.slack_channel_id
    if notifier.client is None or not channel:
        logger.info(
            "[cascaid_slack.events] SLACK_BOT_TOKEN / channel not configured -- "
            "skipping event post"
        )
        return False

    # Best-effort header. Return value ignored: if it failed, we still want
    # the event text to land.
    ensure_date_header(notifier, storage, channel_id=channel, now=now)

    return _post(notifier, channel, text)
