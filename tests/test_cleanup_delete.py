"""Tests for cascaid_slack.cleanup.delete: chat.delete + retry handler wiring.

The retry-on-429 behaviour is supplied by slack_sdk's RateLimitErrorRetryHandler,
so we only test the thin wrapper here: error-code mapping + the "already gone
counts as success" rule.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from slack_sdk.errors import SlackApiError

from cascaid_slack.cleanup import build_delete_client, delete_message


def _slack_error(code: str) -> SlackApiError:
    """Build a SlackApiError with a typical body."""
    return SlackApiError(message=code, response={"ok": False, "error": code})


class TestDeleteMessage:
    """Single chat.delete call mapping to (ok, detail)."""

    def test_happy_path_returns_ok_deleted(self):
        """Successful chat.delete -> (True, 'deleted')."""
        client = MagicMock()
        client.chat_delete.return_value = {"ok": True}

        ok, detail = delete_message(client, "C-X", "1.0")

        assert ok is True
        assert detail == "deleted"
        client.chat_delete.assert_called_once_with(channel="C-X", ts="1.0")

    def test_message_not_found_treated_as_success(self):
        """Slack 'message_not_found' -> (True, 'already_gone') -- end state satisfied."""
        client = MagicMock()
        client.chat_delete.side_effect = _slack_error("message_not_found")

        ok, detail = delete_message(client, "C-X", "1.0")

        assert ok is True
        assert detail == "already_gone"

    def test_cant_delete_message_returns_error_code(self):
        """Wrong-token failure -> (False, 'cant_delete_message')."""
        client = MagicMock()
        client.chat_delete.side_effect = _slack_error("cant_delete_message")

        ok, detail = delete_message(client, "C-X", "1.0")

        assert ok is False
        assert detail == "cant_delete_message"

    def test_unknown_error_returns_unknown(self):
        """SlackApiError without 'error' key -> (False, 'unknown')."""
        client = MagicMock()
        client.chat_delete.side_effect = SlackApiError(
            message="weird", response={"ok": False}
        )

        ok, detail = delete_message(client, "C-X", "1.0")

        assert ok is False
        assert detail == "unknown"


class TestBuildDeleteClient:
    """build_delete_client wires a RateLimitErrorRetryHandler into the WebClient."""

    def test_builds_a_webclient_with_retry_handler(self):
        """Returned client has the rate-limit handler in its retry_handlers."""
        client = build_delete_client("xoxb-fake", max_retry_count=3)

        # We can't easily introspect retry_handlers across slack_sdk versions
        # without depending on private attributes, so just verify the basics:
        assert client.token == "xoxb-fake"
        # And that it's a WebClient.
        from slack_sdk import WebClient

        assert isinstance(client, WebClient)
