"""Rolling pinned message per "report kind" instead of channel flooding.

Operational summaries (status reports, daily metrics, queue depth) tend to
flood channels when posted-fresh-every-time. This module keeps ONE Slack
message per ``pin_key`` and ``chat.update``s its text on each refresh, so
subscribers see a single pinned message that always reflects the latest state.

State machine per pin_key:

    (no record)                    first call    -> post + pins.add + insert
    (record, hash matches)         no-op         -> skip API entirely
    (record, hash differs)         update        -> chat.update
    (record, channel mismatch)     channel-move  -> post fresh in new channel
    (record, update 404s)          self-heal     -> drop record, re-anchor

Slack scopes required (in addition to chat:write already used for plain posts):
    pins:write    -- to call pins.add on first publish

If the workspace lacks pins:write, pins.add returns ``missing_scope`` and we
log a warning. The post itself still lands and chat.update continues to work
on the post we already made -- you just don't get the Slack-side pin badge
until the scope is added.

Env vars:
    SLACK_PINS_CHANNEL_ID  -- optional. When set, every upsert_pinned_message
                              call routes pins to this channel instead of the
                              notifier's default channel. Lets you keep the
                              rolling dashboard at the top of its own channel
                              while noisier ops messages live elsewhere.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Optional

from slack_sdk.errors import SlackApiError

from ._state import PinRecord, PinStateStorage
from .notifier import NotificationService

logger = logging.getLogger(__name__)

# Pinned-summary destination resolution order:
#   explicit channel_id= kwarg > SLACK_PINS_CHANNEL_ID > notifier.slack_channel_id
_PINS_CHANNEL_ENV_VAR = "SLACK_PINS_CHANNEL_ID"

# Errors from chat.update that mean "the underlying message is gone, just
# repost from scratch". Slack also returns ``cant_update_message`` when the
# message is too old (>1hr) for some integrations -- treating it as a
# self-heal trigger is fine: we just re-anchor with a fresh post.
_REPOST_ON_UPDATE_ERRORS = frozenset(
    {"message_not_found", "channel_not_found", "cant_update_message"}
)


class PinKey:
    """Helper for constructing namespaced pin keys.

    Pin keys are just strings -- but typo-safety matters when one wrong
    character means an orphaned Slack message you have to clean up manually.
    Use ``PinKey.namespace(prefix)`` to get a callable that prepends your
    project's prefix to every key.

    Example::

        weekly = PinKey.namespace("weekly")
        upsert_pinned_message(..., pin_key=weekly("revenue"), text=...)
        # -> pin_key = "weekly:revenue"
    """

    @staticmethod
    def namespace(prefix: str):
        """Return a callable that prepends ``{prefix}:`` to each pin key."""

        def make_key(suffix: str) -> str:
            return f"{prefix}:{suffix}"

        return make_key


def _hash_text(text: str) -> str:
    """Stable digest used for skip-if-unchanged. Truncate so the column stays small."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _resolve_channel(
    notifier: NotificationService, explicit: Optional[str]
) -> Optional[str]:
    """Pick the destination channel honoring the documented precedence."""
    return explicit or os.getenv(_PINS_CHANNEL_ENV_VAR) or notifier.slack_channel_id


def _post_and_pin(
    notifier: NotificationService,
    storage: PinStateStorage,
    *,
    pin_key: str,
    channel: str,
    text: str,
    text_hash: str,
) -> bool:
    """Post a fresh message, pin it, persist the mapping. True on success."""
    client = notifier.client
    if client is None:
        return False

    try:
        post_resp = client.chat_postMessage(channel=channel, text=text)
    except SlackApiError as exc:
        logger.error(
            "[cascaid_slack.pins] chat.postMessage failed for %s: %s",
            pin_key,
            exc.response.get("error", "unknown"),
        )
        return False

    ts = post_resp["ts"]

    # pins.add is best-effort. If the workspace lacks pins:write, the message
    # is still posted -- we just don't get the Slack-side pin badge.
    try:
        client.pins_add(channel=channel, timestamp=ts)
    except SlackApiError as exc:
        error = exc.response.get("error")
        # already_pinned is fine: a prior run pinned it and we self-healed here.
        if error != "already_pinned":
            logger.warning(
                "[cascaid_slack.pins] pins.add failed for %s (continuing): %s",
                pin_key,
                error,
            )

    storage.save_pin(
        PinRecord(
            pin_key=pin_key, channel_id=channel, message_ts=ts, last_text_hash=text_hash
        )
    )
    return True


def upsert_pinned_message(
    notifier: NotificationService,
    storage: PinStateStorage,
    *,
    pin_key: str,
    text: str,
    channel_id: Optional[str] = None,
) -> bool:
    """Post-or-update the single pinned message for ``pin_key``.

    Args:
        notifier: ``NotificationService`` providing the WebClient and default channel.
        storage: ``PinStateStorage`` implementation persisting (pin_key, channel, ts, hash).
        pin_key: Stable identifier (use ``PinKey.namespace`` to keep them tidy).
        text: Full message body. Hashed for skip-if-unchanged.
        channel_id: Explicit override for the destination channel.

    Destination resolution (first non-empty wins):
        1. ``channel_id=`` kwarg                  (explicit caller override)
        2. ``$SLACK_PINS_CHANNEL_ID`` env var     (split pins -> own channel)
        3. ``notifier.slack_channel_id``          (default ops channel)

    Returns:
        True when Slack acknowledged the post or update; False on any failure
        (missing config, network error, unrecoverable Slack error).
    """
    client = notifier.client
    channel = _resolve_channel(notifier, channel_id)

    if client is None or not channel:
        logger.info(
            "[cascaid_slack.pins] SLACK_BOT_TOKEN / channel not configured -- "
            "skipping pin update for %s",
            pin_key,
        )
        return False

    text_hash = _hash_text(text)
    record = storage.load_pin(pin_key)

    # Skip-if-unchanged short circuit: if the body is byte-identical to last
    # time AND we're targeting the same channel, don't waste an API call.
    if record and record.last_text_hash == text_hash and record.channel_id == channel:
        logger.debug("[cascaid_slack.pins] %s unchanged; skipping Slack update", pin_key)
        return True

    # If we have an existing message in the desired channel, try to update it.
    if record and record.channel_id == channel:
        try:
            client.chat_update(
                channel=record.channel_id, ts=record.message_ts, text=text
            )
        except SlackApiError as exc:
            error = exc.response.get("error")
            if error in _REPOST_ON_UPDATE_ERRORS:
                logger.info(
                    "[cascaid_slack.pins] %s: chat.update returned %s -- self-healing",
                    pin_key,
                    error,
                )
                storage.delete_pin(pin_key)
                return _post_and_pin(
                    notifier,
                    storage,
                    pin_key=pin_key,
                    channel=channel,
                    text=text,
                    text_hash=text_hash,
                )
            logger.error(
                "[cascaid_slack.pins] %s: chat.update failed unrecoverably: %s",
                pin_key,
                error,
            )
            return False

        # Update succeeded -- bump the stored hash without changing channel/ts.
        storage.save_pin(
            PinRecord(
                pin_key=pin_key,
                channel_id=channel,
                message_ts=record.message_ts,
                last_text_hash=text_hash,
            )
        )
        return True

    # Either no record yet, or the desired channel changed -- post fresh.
    return _post_and_pin(
        notifier,
        storage,
        pin_key=pin_key,
        channel=channel,
        text=text,
        text_hash=text_hash,
    )
