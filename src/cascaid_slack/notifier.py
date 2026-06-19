"""Slack text + file sending built on slack_sdk.WebClient.

This module is the official transport layer for cascaid_slack. Anything that
posts to Slack should go through ``NotificationService`` so we have one place
to add cross-cutting concerns (logging, metrics, retry policy tweaks, etc.).

Why a wrapper over WebClient directly:
    * Env-var resolution (SLACK_BOT_TOKEN, SLACK_CHANNEL_ID) with sensible
      defaults so callers don't pass them every time.
    * Consistent error handling -- WebClient raises SlackApiError; we coerce
      to a True/False return so notification failures don't crash sync jobs.
    * File-upload convenience: WebClient's files_upload_v2 wants either a
      file path or bytes; we accept a str and handle the encoding.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

logger = logging.getLogger(__name__)


class NotificationService:
    """Post text + upload files to Slack via Bot Token.

    Construct with explicit args for tests; in production let it pick up
    SLACK_BOT_TOKEN and SLACK_CHANNEL_ID from the environment.
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        channel_id: Optional[str] = None,
        client: Optional[WebClient] = None,
    ):
        """Initialize the notifier.

        Args:
            bot_token: xoxb- token. Defaults to ``$SLACK_BOT_TOKEN``.
            channel_id: Default destination channel. Defaults to ``$SLACK_CHANNEL_ID``.
            client: Pre-built WebClient. Mostly for tests; production code
                should let __init__ build one from the bot_token.
        """
        self.slack_bot_token = bot_token or os.getenv("SLACK_BOT_TOKEN")
        self.slack_channel_id = channel_id or os.getenv("SLACK_CHANNEL_ID")
        # Only build a WebClient if we have a token -- otherwise defer until
        # send time so unconfigured environments don't blow up at import.
        self._client = client
        if self._client is None and self.slack_bot_token:
            self._client = WebClient(token=self.slack_bot_token)

    @property
    def client(self) -> Optional[WebClient]:
        """The underlying WebClient, or None if no token was configured."""
        return self._client

    def send_slack(
        self,
        message: str,
        *,
        channel: Optional[str] = None,
        username: Optional[str] = None,
        icon_emoji: Optional[str] = None,
    ) -> bool:
        """Post a plain-text message via chat.postMessage.

        Args:
            message: Text body. Slack's ``mrkdwn`` formatting works inside.
            channel: Override the default channel for this one post.
            username: Bot display name (requires ``chat:write.customize`` scope).
            icon_emoji: Avatar emoji (requires ``chat:write.customize`` scope).

        Returns:
            True if Slack acknowledged ok=true; False on any failure
            (missing config, network error, Slack-side error). Failures are
            logged but never raised -- notifications are operational
            ergonomics, not core flow.
        """
        target_channel = channel or self.slack_channel_id
        if not self._client or not target_channel:
            logger.info(
                "[cascaid_slack] SLACK_BOT_TOKEN / SLACK_CHANNEL_ID not configured -- "
                "skipping Slack notification."
            )
            return False

        kwargs: dict = {"channel": target_channel, "text": message}
        if username:
            kwargs["username"] = username
        if icon_emoji:
            kwargs["icon_emoji"] = icon_emoji

        try:
            self._client.chat_postMessage(**kwargs)
            return True
        except SlackApiError as exc:
            logger.error(
                "[cascaid_slack] chat.postMessage failed: %s",
                exc.response.get("error", "unknown"),
            )
            return False

    def send_slack_file(
        self,
        content: str,
        filename: str,
        initial_comment: str = "",
        channel_id: Optional[str] = None,
    ) -> bool:
        """Upload a text file via the modern files_upload_v2 flow.

        slack_sdk's files_upload_v2 wraps the 3-step
        getUploadURLExternal / PUT / completeUploadExternal dance Slack
        requires post-2025. We accept a str so callers don't have to
        encode upfront.

        Args:
            content: UTF-8 file body.
            filename: Filename shown in Slack.
            initial_comment: Message posted alongside the upload.
            channel_id: Override default channel for this one upload.

        Returns:
            True on success, False on failure.
        """
        channel = channel_id or self.slack_channel_id
        if not self._client or not channel:
            logger.info(
                "[cascaid_slack] SLACK_BOT_TOKEN / SLACK_CHANNEL_ID not configured -- "
                "skipping Slack file upload."
            )
            return False

        try:
            self._client.files_upload_v2(
                channel=channel,
                content=content,
                filename=filename,
                initial_comment=initial_comment,
            )
            return True
        except SlackApiError as exc:
            logger.error(
                "[cascaid_slack] files_upload_v2 failed: %s",
                exc.response.get("error", "unknown"),
            )
            return False
