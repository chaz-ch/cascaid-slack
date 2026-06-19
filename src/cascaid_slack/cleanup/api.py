"""Slack Web API reads needed for cleanup: history, pins, whoami.

All Slack reads in the cleanup pipeline live here so the runner can stay
focused on orchestration. Uses ``slack_sdk.WebClient`` which gives us free
``RateLimitErrorRetryHandler`` -- 429s on conversations.history pagination
will auto-back-off without us writing the loop.
"""

from __future__ import annotations

import logging
from typing import Iterator, Optional

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)


def fetch_all_messages(
    client: WebClient,
    channel: str,
    *,
    page_size: int = 200,
    log_every_n_pages: int = 5,
) -> Iterator[dict]:
    """Yield every message in ``channel`` via conversations.history pagination.

    Uses WebClient's built-in cursor handling. Returns raw Slack message dicts
    so callers have full access to text, blocks, attachments, reactions, files,
    thread refs, etc.

    Args:
        client: A configured ``slack_sdk.WebClient``.
        channel: Channel ID (e.g. ``C0AH7HJHG15``).
        page_size: Messages per page (Slack max is 200).
        log_every_n_pages: How often to print a progress line. 0 to silence.
    """
    cursor: Optional[str] = None
    page = 0
    while True:
        kwargs: dict = {"channel": channel, "limit": page_size}
        if cursor:
            kwargs["cursor"] = cursor

        try:
            resp = client.conversations_history(**kwargs)
        except SlackApiError as exc:
            logger.error(
                "[cascaid_slack.cleanup] conversations.history failed: %s",
                exc.response.get("error", "unknown"),
            )
            return

        for msg in resp.get("messages") or []:
            yield msg

        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        page += 1
        if log_every_n_pages and page % log_every_n_pages == 0:
            logger.info(
                "[cascaid_slack.cleanup] fetched ~%d messages so far",
                page * page_size,
            )
        if not cursor:
            return


def fetch_currently_pinned(client: WebClient, channel: str) -> set[str]:
    """Return ts of every message currently pinned in ``channel`` via pins.list.

    Secondary safety net for pinned messages your DB doesn't track (someone
    manually pinned an announcement, etc). The classifier protects these
    even when the consumer didn't list them in db_pinned_ts.

    On failure, returns an empty set + logs a warning. We prefer "no pins
    found, delete nothing extra" over "API hiccup, delete a pinned message
    by mistake."
    """
    try:
        resp = client.pins_list(channel=channel)
    except SlackApiError as exc:
        logger.warning(
            "[cascaid_slack.cleanup] pins.list failed (%s); treating channel "
            "as having no pins for safety",
            exc.response.get("error", "unknown"),
        )
        return set()

    return {
        item["message"]["ts"]
        for item in (resp.get("items") or [])
        if "message" in item
    }


def whoami(client: WebClient) -> dict:
    """Return Slack's auth.test response for the WebClient's token.

    Used at startup to log which identity will be doing the reads/deletes --
    critical context when juggling a bot token (xoxb-) for reads and a user
    token (xoxp-) for deletes. The returned dict has ``user``, ``user_id``,
    and ``bot_id`` (None for user tokens).
    """
    try:
        # ``client.auth_test()`` returns a SlackResponse, which iterates over
        # response *keys* rather than (k, v) tuples -- so the naive
        # ``dict(client.auth_test())`` raises ``ValueError: dictionary update
        # sequence element #0 has length 1; 2 is required``. Use ``.data`` to
        # get the raw dict the SDK already parsed from JSON.
        return dict(client.auth_test().data)
    except SlackApiError as exc:
        raise RuntimeError(
            f"auth.test failed: {exc.response.get('error', 'unknown')}"
        ) from exc
