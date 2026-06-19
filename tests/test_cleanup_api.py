"""Tests for cascaid_slack.cleanup.api: fetch helpers + whoami.

Mocks the WebClient -- no real Slack calls. Verifies pagination, error
handling, and the "treat failures as empty for safety" policy on pins.list.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from cascaid_slack.cleanup.api import (
    fetch_all_messages,
    fetch_currently_pinned,
    whoami,
)


def _slack_error(code: str) -> SlackApiError:
    """Build a SlackApiError with a typical body."""
    return SlackApiError(message=code, response={"ok": False, "error": code})


@pytest.fixture
def client():
    """Fresh MagicMock WebClient. Override per test."""
    return MagicMock()


class TestFetchAllMessages:
    """conversations.history pagination."""

    def test_single_page_yields_all_messages(self, client):
        """One page with next_cursor=None -> yield messages then stop."""
        client.conversations_history.return_value = {
            "ok": True,
            "messages": [{"ts": "1.0"}, {"ts": "2.0"}, {"ts": "3.0"}],
            "response_metadata": {"next_cursor": ""},
        }

        out = list(fetch_all_messages(client, "C-X", log_every_n_pages=0))

        assert [m["ts"] for m in out] == ["1.0", "2.0", "3.0"]
        client.conversations_history.assert_called_once_with(channel="C-X", limit=200)

    def test_multi_page_pagination(self, client):
        """Two pages: cursor passed to second call, both yielded in order."""
        client.conversations_history.side_effect = [
            {
                "ok": True,
                "messages": [{"ts": "1.0"}, {"ts": "2.0"}],
                "response_metadata": {"next_cursor": "cur-a"},
            },
            {
                "ok": True,
                "messages": [{"ts": "3.0"}],
                "response_metadata": {"next_cursor": ""},
            },
        ]

        out = list(fetch_all_messages(client, "C-X", log_every_n_pages=0))

        assert [m["ts"] for m in out] == ["1.0", "2.0", "3.0"]
        # Second call had cursor=cur-a
        second_call = client.conversations_history.call_args_list[1]
        assert second_call.kwargs["cursor"] == "cur-a"

    def test_slack_api_error_stops_iteration(self, client):
        """If a page request errors, we stop yielding (don't crash the caller)."""
        client.conversations_history.side_effect = _slack_error("rate_limited")

        out = list(fetch_all_messages(client, "C-X", log_every_n_pages=0))

        assert out == []


class TestFetchCurrentlyPinned:
    """pins.list with safety-first error handling."""

    def test_returns_set_of_ts_from_pins(self, client):
        """Happy path: pins.list returns ts of each pinned message."""
        client.pins_list.return_value = {
            "ok": True,
            "items": [
                {"message": {"ts": "1.0"}},
                {"message": {"ts": "2.0"}},
                {"file": {"id": "F-X"}},  # not a message; ignored
            ],
        }

        out = fetch_currently_pinned(client, "C-X")

        assert out == {"1.0", "2.0"}

    def test_api_error_returns_empty_set_not_raises(self, client):
        """pins.list failure -> empty set so the caller defaults to no-pins."""
        client.pins_list.side_effect = _slack_error("missing_scope")

        out = fetch_currently_pinned(client, "C-X")

        assert out == set()

    def test_empty_pins_list(self, client):
        """No items in pins -> empty set, no error."""
        client.pins_list.return_value = {"ok": True, "items": []}

        out = fetch_currently_pinned(client, "C-X")

        assert out == set()


class TestWhoami:
    """auth.test wrapping."""

    def test_returns_auth_test_dict(self, client):
        """Happy path: forward the auth.test response as a plain dict."""
        client.auth_test.return_value = {
            "ok": True,
            "user": "autochart-bot",
            "user_id": "U-AUTO",
            "bot_id": "B-AUTO",
        }

        out = whoami(client)

        assert out["user_id"] == "U-AUTO"
        assert out["bot_id"] == "B-AUTO"

    def test_auth_test_failure_raises_runtime_error(self, client):
        """Slack-side failure -> RuntimeError so callers can fail loudly at startup."""
        client.auth_test.side_effect = _slack_error("invalid_auth")

        with pytest.raises(RuntimeError, match="invalid_auth"):
            whoami(client)
