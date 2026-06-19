"""Tests for cascaid_slack.notifier.NotificationService.

Cover the contract:
    * Posts via WebClient.chat_postMessage when configured.
    * Returns False without hitting Slack when token/channel missing.
    * Returns False (not raises) on Slack-side errors -- notifications
      shouldn't crash callers.
    * Per-call ``channel=`` overrides the default.
    * files_upload_v2 path works the same way.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from cascaid_slack import NotificationService


@pytest.fixture
def fake_client():
    """A WebClient stand-in whose methods we can assert against."""
    client = MagicMock()
    client.chat_postMessage.return_value = {"ok": True, "ts": "1.0"}
    client.files_upload_v2.return_value = {"ok": True}
    return client


class TestSendSlack:
    """chat.postMessage path."""

    def test_happy_path_posts_to_default_channel(self, fake_client):
        """Configured notifier hits WebClient with the env-default channel."""
        n = NotificationService(
            bot_token="xoxb-test", channel_id="C-DEFAULT", client=fake_client
        )

        ok = n.send_slack("hello world")

        assert ok is True
        fake_client.chat_postMessage.assert_called_once_with(
            channel="C-DEFAULT", text="hello world"
        )

    def test_per_call_channel_overrides_default(self, fake_client):
        """The channel= kwarg routes one post elsewhere without sticky state."""
        n = NotificationService(
            bot_token="xoxb-test", channel_id="C-DEFAULT", client=fake_client
        )

        n.send_slack("hi", channel="C-OTHER")

        fake_client.chat_postMessage.assert_called_once_with(
            channel="C-OTHER", text="hi"
        )

    def test_username_and_icon_emoji_are_passed_through(self, fake_client):
        """Optional vanity args land in the WebClient kwargs when provided."""
        n = NotificationService(
            bot_token="xoxb-test", channel_id="C-DEFAULT", client=fake_client
        )

        n.send_slack("hi", username="Reporter", icon_emoji=":bar_chart:")

        fake_client.chat_postMessage.assert_called_once_with(
            channel="C-DEFAULT",
            text="hi",
            username="Reporter",
            icon_emoji=":bar_chart:",
        )

    def test_missing_token_returns_false_quietly(self, monkeypatch):
        """No token -> no API call, no exception, just False + log."""
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_CHANNEL_ID", raising=False)

        n = NotificationService()  # picks up empty env

        assert n.send_slack("ignored") is False
        assert n.client is None

    def test_missing_channel_returns_false_quietly(self, monkeypatch):
        """Token without channel and no override -> False."""
        # Clear leaked env so nothing fills in the channel for us.
        monkeypatch.delenv("SLACK_CHANNEL_ID", raising=False)
        n = NotificationService(bot_token="xoxb-test", channel_id=None)
        # client builds even without a channel; the guard fires inside send_slack
        assert n.send_slack("ignored") is False

    def test_slack_api_error_returns_false_not_raises(self, fake_client):
        """Slack-side failures must not crash the caller."""
        fake_client.chat_postMessage.side_effect = SlackApiError(
            message="bad",
            response={"ok": False, "error": "channel_not_found"},
        )
        n = NotificationService(
            bot_token="xoxb-test", channel_id="C-DEFAULT", client=fake_client
        )

        assert n.send_slack("hi") is False


class TestSendSlackFile:
    """files_upload_v2 path."""

    def test_happy_path_uploads(self, fake_client):
        """Pass-through content + filename + initial_comment to WebClient."""
        n = NotificationService(
            bot_token="xoxb-test", channel_id="C-DEFAULT", client=fake_client
        )

        ok = n.send_slack_file(
            content="row1\nrow2\n",
            filename="results.tsv",
            initial_comment="here you go",
        )

        assert ok is True
        fake_client.files_upload_v2.assert_called_once_with(
            channel="C-DEFAULT",
            content="row1\nrow2\n",
            filename="results.tsv",
            initial_comment="here you go",
        )

    def test_per_call_channel_id_overrides_default(self, fake_client):
        """channel_id= argument routes one upload elsewhere."""
        n = NotificationService(
            bot_token="xoxb-test", channel_id="C-DEFAULT", client=fake_client
        )

        n.send_slack_file(content="x", filename="x.txt", channel_id="C-OTHER")

        fake_client.files_upload_v2.assert_called_once()
        assert fake_client.files_upload_v2.call_args.kwargs["channel"] == "C-OTHER"

    def test_slack_api_error_returns_false(self, fake_client):
        """Upload errors are swallowed into a False return."""
        fake_client.files_upload_v2.side_effect = SlackApiError(
            message="bad", response={"ok": False, "error": "file_size_exceeded"}
        )
        n = NotificationService(
            bot_token="xoxb-test", channel_id="C-DEFAULT", client=fake_client
        )

        assert n.send_slack_file(content="x", filename="x.txt") is False

    def test_missing_config_returns_false(self, monkeypatch):
        """No token -> no upload attempted."""
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_CHANNEL_ID", raising=False)
        n = NotificationService()

        assert n.send_slack_file(content="x", filename="x.txt") is False
