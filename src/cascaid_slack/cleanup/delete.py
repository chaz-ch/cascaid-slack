"""Slack message deletion with retry-on-429.

slack_sdk's WebClient ships with ``RateLimitErrorRetryHandler`` baked in --
when configured, any 429 response triggers a sleep-and-retry using Slack's
Retry-After header. No need to hand-roll the retry loop the original cleanup
script had.

Token semantics (carry over from the original):
    * xoxb- (bot)  -- can only delete the bot's own posts.
    * xoxp- (user) -- can delete any message the user has perms to delete;
                      workspace owners/admins can nuke anything in-channel.

The lib doesn't enforce this -- the consumer constructs the WebClient with
whichever token is appropriate. ``cant_delete_message`` from chat.delete is
the symptom of wrong-token-for-message and is returned as the detail string.
"""

from __future__ import annotations

import logging

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

logger = logging.getLogger(__name__)


def build_delete_client(token: str, *, max_retry_count: int = 5) -> WebClient:
    """Construct a WebClient pre-wired with RateLimitErrorRetryHandler.

    Use this in your cleanup runner instead of bare ``WebClient(token=...)``
    so 429s on chat.delete auto-retry without you writing the loop.

    Args:
        token: Slack token (xoxb- or xoxp-).
        max_retry_count: How many 429-retry-after sleeps before giving up.
    """
    return WebClient(
        token=token,
        retry_handlers=[RateLimitErrorRetryHandler(max_retry_count=max_retry_count)],
    )


def delete_message(client: WebClient, channel: str, ts: str) -> tuple[bool, str]:
    """Delete one message via chat.delete.

    Returns:
        ``(ok, detail)`` -- ``detail`` is ``"deleted"`` on success,
        ``"already_gone"`` if the message was missing, or the Slack error
        code on failure.

    ``message_not_found`` is treated as success because the desired end-state
    (message no longer in channel) is satisfied.
    """
    try:
        client.chat_delete(channel=channel, ts=ts)
        return True, "deleted"
    except SlackApiError as exc:
        error = exc.response.get("error", "unknown")
        if error == "message_not_found":
            return True, "already_gone"
        return False, error
