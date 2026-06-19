"""Tests for cascaid_slack.events.send_slack_event + ensure_date_header.

Mirrors the 11 tests from automatic-charting/tests/test_slack_events.py,
adapted to the new shape: WebClient instead of raw requests, EventLogStorage
Protocol instead of DatabaseConnection.

Covered:
    1. Header formatting (bold separator, no leading zero on day)
    2. First post: header + event in order
    3. Same day: no fresh header, just event
    4. New day: fresh header before event
    5. Missing token: skip without hitting Slack
    6. Missing channel: same
    7. Explicit channel kwarg overrides notifier default
    8. Header post failure: event still posts (best-effort)
    9. Event post failure: returns False
   10. SlackApiError during event post: returns False
   11. Per-channel state isolation
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from unittest.mock import MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from cascaid_slack import EventLogState, NotificationService, send_slack_event
from cascaid_slack.events import _format_date_header, ensure_date_header


class FakeStorage:
    """In-memory EventLogStorage so tests don't need SQLAlchemy."""

    def __init__(self) -> None:
        """Start with no event-log state."""
        self.rows: dict[str, EventLogState] = {}

    def load_event_log(self, channel_id: str) -> Optional[EventLogState]:
        """Return the stored state for ``channel_id`` or None."""
        return self.rows.get(channel_id)

    def save_event_log(self, state: EventLogState) -> None:
        """Persist ``state`` -- overwrites any prior row."""
        self.rows[state.channel_id] = state


def _slack_error(code: str) -> SlackApiError:
    """Build a SlackApiError with a typical body."""
    return SlackApiError(message=code, response={"ok": False, "error": code})


@pytest.fixture
def fake_client():
    """WebClient stand-in. chat_postMessage returns ok by default."""
    client = MagicMock()
    client.chat_postMessage.return_value = {"ok": True, "ts": "1.0"}
    return client


@pytest.fixture
def notifier(fake_client) -> NotificationService:
    """Notifier wired to the fake client + default channel."""
    return NotificationService(
        bot_token="xoxb-test", channel_id="C-DEFAULT", client=fake_client
    )


@pytest.fixture
def storage() -> FakeStorage:
    """Fresh in-memory event-log storage."""
    return FakeStorage()


class TestDateHeaderFormatting:
    """The _format_date_header helper."""

    def test_header_is_bold_separator(self):
        """Output matches the *--- Weekday, Month Day, Year ---* pattern."""
        out = _format_date_header(datetime(2026, 6, 16).date())

        assert out.startswith("*--- ")
        assert out.endswith(" ---*")
        assert "Tuesday" in out
        assert "June" in out
        assert "2026" in out

    def test_header_drops_leading_zero_on_day(self):
        """Day-of-month uses %-d -- '4' not '04'."""
        out = _format_date_header(datetime(2026, 6, 4).date())

        assert "June 4," in out
        assert "June 04," not in out


class TestFirstPost:
    """No prior record for the channel."""

    def test_posts_header_then_event(self, notifier, storage, fake_client):
        """Cold start: chat.postMessage fires twice -- header first, then event."""
        send_slack_event(
            notifier,
            storage,
            text="Completed: 71 navigator tasks",
            now=datetime(2026, 6, 16, 10, 30),
        )

        assert fake_client.chat_postMessage.call_count == 2
        first_call = fake_client.chat_postMessage.call_args_list[0]
        second_call = fake_client.chat_postMessage.call_args_list[1]
        assert first_call.kwargs["text"].startswith("*--- ")  # the date header
        assert second_call.kwargs["text"] == "Completed: 71 navigator tasks"
        # State persisted with today's iso date
        assert storage.rows["C-DEFAULT"].last_header_date == "2026-06-16"


class TestSameDay:
    """A header already exists for today's date."""

    def test_no_header_when_date_matches(self, notifier, storage, fake_client):
        """Channel already has today's banner -> only the event posts."""
        storage.rows["C-DEFAULT"] = EventLogState(
            channel_id="C-DEFAULT",
            last_header_date="2026-06-16",
            last_header_ts=None,
        )

        send_slack_event(
            notifier, storage, text="event 2", now=datetime(2026, 6, 16, 14, 0)
        )

        # Only one post: the event itself.
        assert fake_client.chat_postMessage.call_count == 1
        assert fake_client.chat_postMessage.call_args.kwargs["text"] == "event 2"


class TestNewDay:
    """Existing record but for yesterday."""

    def test_new_day_posts_fresh_header(self, notifier, storage, fake_client):
        """Crossing midnight -> fresh header + event, storage updated."""
        storage.rows["C-DEFAULT"] = EventLogState(
            channel_id="C-DEFAULT",
            last_header_date="2026-06-15",
            last_header_ts=None,
        )

        send_slack_event(
            notifier, storage, text="next day", now=datetime(2026, 6, 16, 9, 0)
        )

        assert fake_client.chat_postMessage.call_count == 2
        assert storage.rows["C-DEFAULT"].last_header_date == "2026-06-16"


class TestSkipUnconfigured:
    """Bail before touching the network when essentials are missing."""

    def test_missing_token_skips(self, storage, monkeypatch):
        """No bot token -> False, no API call, no state mutation."""
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_CHANNEL_ID", raising=False)
        n = NotificationService()

        ok = send_slack_event(n, storage, text="hi")

        assert ok is False
        assert storage.rows == {}

    def test_missing_channel_skips(self, storage, fake_client, monkeypatch):
        """Token present, channel missing -> False without API call."""
        monkeypatch.delenv("SLACK_CHANNEL_ID", raising=False)
        monkeypatch.delenv("SLACK_PINS_CHANNEL_ID", raising=False)
        n = NotificationService(bot_token="xoxb-test", channel_id=None, client=fake_client)

        ok = send_slack_event(n, storage, text="hi")

        assert ok is False
        fake_client.chat_postMessage.assert_not_called()


class TestChannelOverride:
    """Per-call channel argument routes one event elsewhere."""

    def test_explicit_channel_arg_overrides_notifier(self, notifier, storage, fake_client):
        """channel_id= keyword wins over notifier default."""
        send_slack_event(
            notifier,
            storage,
            text="hello",
            channel_id="C-OTHER",
            now=datetime(2026, 6, 16),
        )

        # Both posts (header + event) go to C-OTHER, not C-DEFAULT.
        for call in fake_client.chat_postMessage.call_args_list:
            assert call.kwargs["channel"] == "C-OTHER"
        assert "C-OTHER" in storage.rows
        assert "C-DEFAULT" not in storage.rows


class TestHeaderFailureStillPostsEvent:
    """Best-effort: header failure shouldn't drop the event."""

    def test_header_failure_still_posts_event(self, notifier, storage, fake_client):
        """If the header post fails, the event still goes through."""
        # First call (header) fails, second call (event) succeeds.
        fake_client.chat_postMessage.side_effect = [
            _slack_error("rate_limited"),
            {"ok": True, "ts": "2.0"},
        ]

        ok = send_slack_event(
            notifier, storage, text="event", now=datetime(2026, 6, 16)
        )

        assert ok is True
        assert fake_client.chat_postMessage.call_count == 2
        # Storage NOT updated because the header failed -- next call retries.
        assert "C-DEFAULT" not in storage.rows


class TestEventFailureReturnsFalse:
    """If the event itself fails, return False."""

    def test_event_failure_returns_false(self, notifier, storage, fake_client):
        """Header succeeds, event fails -> False."""
        fake_client.chat_postMessage.side_effect = [
            {"ok": True, "ts": "1.0"},  # header
            _slack_error("channel_not_found"),  # event
        ]

        ok = send_slack_event(
            notifier, storage, text="event", now=datetime(2026, 6, 16)
        )

        assert ok is False
        # But header state DID persist because header itself succeeded.
        assert storage.rows["C-DEFAULT"].last_header_date == "2026-06-16"

    def test_network_error_returns_false(self, notifier, storage, fake_client):
        """SlackApiError on the event-post call -> False."""
        # Same-day record so we skip the header attempt entirely.
        storage.rows["C-DEFAULT"] = EventLogState(
            channel_id="C-DEFAULT",
            last_header_date="2026-06-16",
            last_header_ts=None,
        )
        fake_client.chat_postMessage.side_effect = _slack_error("invalid_auth")

        ok = send_slack_event(
            notifier, storage, text="event", now=datetime(2026, 6, 16)
        )

        assert ok is False


class TestChannelIsolation:
    """Two channels can have independent header dates without interfering."""

    def test_channels_dont_share_date_state(self, notifier, storage, fake_client):
        """Posting to C-A doesn't affect what C-B thinks about its header."""
        # C-A already posted today
        storage.rows["C-A"] = EventLogState(
            channel_id="C-A",
            last_header_date="2026-06-16",
            last_header_ts=None,
        )

        # C-B has never posted -- should get its own header
        send_slack_event(
            notifier,
            storage,
            text="hi from B",
            channel_id="C-B",
            now=datetime(2026, 6, 16),
        )

        # B got header + event = 2 calls
        assert fake_client.chat_postMessage.call_count == 2
        assert storage.rows["C-A"].last_header_date == "2026-06-16"
        assert storage.rows["C-B"].last_header_date == "2026-06-16"


class TestEnsureDateHeaderStandalone:
    """ensure_date_header is usable on its own (for file-upload callers)."""

    def test_idempotent_within_same_day(self, notifier, storage, fake_client):
        """Two calls within the same day -> only one header posts."""
        ensure_date_header(notifier, storage, now=datetime(2026, 6, 16, 9, 0))
        ensure_date_header(notifier, storage, now=datetime(2026, 6, 16, 17, 0))

        assert fake_client.chat_postMessage.call_count == 1

    def test_returns_true_when_already_today(self, notifier, storage):
        """Existing today's record -> returns True without an API call."""
        storage.rows["C-DEFAULT"] = EventLogState(
            channel_id="C-DEFAULT",
            last_header_date="2026-06-16",
            last_header_ts=None,
        )

        assert (
            ensure_date_header(notifier, storage, now=datetime(2026, 6, 16, 9, 0))
            is True
        )
