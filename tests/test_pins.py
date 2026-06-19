"""Tests for cascaid_slack.pins.upsert_pinned_message.

These mirror the original 18 tests from automatic-charting/tests/test_slack_pins.py,
adapted to the new shape: WebClient instead of raw requests, PinStateStorage
Protocol instead of DatabaseConnection.

Covered:
    1. First call ever: post + pins.add + storage insert
    2. Subsequent call, hash matches: skip API entirely
    3. Subsequent call, hash differs: chat.update only
    4. chat.update returns message_not_found: self-heal via fresh post
    5. chat.update returns unrecoverable error: bail with False
    6. Missing token/channel: skip without hitting Slack
    7. pins.add failure: post still counts as success
    8. Channel change (record's channel != desired): repost in new channel
    9. SLACK_PINS_CHANNEL_ID env var precedence
   10. PinKey.namespace helper
"""

from __future__ import annotations

from typing import Optional
from unittest.mock import MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from cascaid_slack import (
    NotificationService,
    PinKey,
    PinRecord,
    upsert_pinned_message,
)
from cascaid_slack.pins import _hash_text


class FakeStorage:
    """In-memory PinStateStorage so tests don't need SQLAlchemy or a real DB."""

    def __init__(self) -> None:
        """Start with no pin records."""
        self.rows: dict[str, PinRecord] = {}

    def load_pin(self, pin_key: str) -> Optional[PinRecord]:
        """Return the stored record for ``pin_key`` or None."""
        return self.rows.get(pin_key)

    def save_pin(self, record: PinRecord) -> None:
        """Persist ``record`` -- overwrites any prior row."""
        self.rows[record.pin_key] = record

    def delete_pin(self, pin_key: str) -> None:
        """Drop ``pin_key`` if present (no error if absent)."""
        self.rows.pop(pin_key, None)


def _slack_error(code: str) -> SlackApiError:
    """Build a SlackApiError with a typical body."""
    return SlackApiError(message=code, response={"ok": False, "error": code})


@pytest.fixture
def fake_client():
    """WebClient stand-in. Override side_effect / return_value per test."""
    client = MagicMock()
    client.chat_postMessage.return_value = {"ok": True, "ts": "1.0"}
    client.chat_update.return_value = {"ok": True}
    client.pins_add.return_value = {"ok": True}
    return client


@pytest.fixture
def notifier(fake_client) -> NotificationService:
    """Notifier configured for happy-path tests."""
    return NotificationService(
        bot_token="xoxb-test", channel_id="C-DEFAULT", client=fake_client
    )


@pytest.fixture
def storage() -> FakeStorage:
    """Fresh in-memory pin storage."""
    return FakeStorage()


class TestFirstPost:
    """No prior record -> post + pin + insert."""

    def test_post_then_pin_then_insert(self, notifier, storage, fake_client):
        """The three calls happen in order and the row lands in storage."""
        fake_client.chat_postMessage.return_value = {"ok": True, "ts": "9.0"}

        ok = upsert_pinned_message(notifier, storage, pin_key="status", text="hi")

        assert ok is True
        fake_client.chat_postMessage.assert_called_once_with(
            channel="C-DEFAULT", text="hi"
        )
        fake_client.pins_add.assert_called_once_with(channel="C-DEFAULT", timestamp="9.0")
        assert storage.rows["status"].message_ts == "9.0"
        assert storage.rows["status"].last_text_hash == _hash_text("hi")

    def test_pins_add_failure_doesnt_fail_the_publish(self, notifier, storage, fake_client):
        """Missing pins:write scope -> warning + still True."""
        fake_client.chat_postMessage.return_value = {"ok": True, "ts": "9.0"}
        fake_client.pins_add.side_effect = _slack_error("missing_scope")

        ok = upsert_pinned_message(notifier, storage, pin_key="status", text="hi")

        assert ok is True
        assert "status" in storage.rows

    def test_already_pinned_is_treated_as_success(self, notifier, storage, fake_client):
        """A previous run pinned it; pins.add says already_pinned; no big deal."""
        fake_client.chat_postMessage.return_value = {"ok": True, "ts": "9.0"}
        fake_client.pins_add.side_effect = _slack_error("already_pinned")

        ok = upsert_pinned_message(notifier, storage, pin_key="status", text="hi")

        assert ok is True

    def test_chat_post_failure_returns_false_and_no_row(self, notifier, storage, fake_client):
        """Post failed -> False and storage stays empty."""
        fake_client.chat_postMessage.side_effect = _slack_error("rate_limited")

        ok = upsert_pinned_message(notifier, storage, pin_key="status", text="hi")

        assert ok is False
        assert "status" not in storage.rows
        fake_client.pins_add.assert_not_called()


class TestUpdateExisting:
    """A record already exists for this pin_key."""

    def test_skip_when_hash_unchanged(self, notifier, storage, fake_client):
        """Same text + same channel -> no API call, returns True."""
        storage.rows["status"] = PinRecord(
            pin_key="status",
            channel_id="C-DEFAULT",
            message_ts="1.0",
            last_text_hash=_hash_text("hi"),
        )

        ok = upsert_pinned_message(notifier, storage, pin_key="status", text="hi")

        assert ok is True
        fake_client.chat_postMessage.assert_not_called()
        fake_client.chat_update.assert_not_called()

    def test_chat_update_when_hash_differs(self, notifier, storage, fake_client):
        """Body changed -> chat.update, hash refreshed, no new post."""
        storage.rows["status"] = PinRecord(
            pin_key="status",
            channel_id="C-DEFAULT",
            message_ts="1.0",
            last_text_hash=_hash_text("old"),
        )

        ok = upsert_pinned_message(notifier, storage, pin_key="status", text="new")

        assert ok is True
        fake_client.chat_update.assert_called_once_with(
            channel="C-DEFAULT", ts="1.0", text="new"
        )
        fake_client.chat_postMessage.assert_not_called()
        assert storage.rows["status"].last_text_hash == _hash_text("new")
        # ts unchanged -- we updated the existing message in place
        assert storage.rows["status"].message_ts == "1.0"

    def test_self_heal_when_message_not_found(self, notifier, storage, fake_client):
        """Slack lost the message -> drop record + post fresh."""
        storage.rows["status"] = PinRecord(
            pin_key="status",
            channel_id="C-DEFAULT",
            message_ts="1.0",
            last_text_hash=_hash_text("old"),
        )
        fake_client.chat_update.side_effect = _slack_error("message_not_found")
        fake_client.chat_postMessage.return_value = {"ok": True, "ts": "2.0"}

        ok = upsert_pinned_message(notifier, storage, pin_key="status", text="new")

        assert ok is True
        # New ts persisted; the old 1.0 is forgotten.
        assert storage.rows["status"].message_ts == "2.0"

    def test_self_heal_when_cant_update_message(self, notifier, storage, fake_client):
        """Old message past the edit window -> same self-heal path."""
        storage.rows["status"] = PinRecord(
            pin_key="status",
            channel_id="C-DEFAULT",
            message_ts="1.0",
            last_text_hash=_hash_text("old"),
        )
        fake_client.chat_update.side_effect = _slack_error("cant_update_message")
        fake_client.chat_postMessage.return_value = {"ok": True, "ts": "2.0"}

        ok = upsert_pinned_message(notifier, storage, pin_key="status", text="new")

        assert ok is True
        assert storage.rows["status"].message_ts == "2.0"

    def test_unrecoverable_update_error_returns_false(self, notifier, storage, fake_client):
        """Random Slack error we can't self-heal from -> False, no repost."""
        storage.rows["status"] = PinRecord(
            pin_key="status",
            channel_id="C-DEFAULT",
            message_ts="1.0",
            last_text_hash=_hash_text("old"),
        )
        fake_client.chat_update.side_effect = _slack_error("invalid_auth")

        ok = upsert_pinned_message(notifier, storage, pin_key="status", text="new")

        assert ok is False
        fake_client.chat_postMessage.assert_not_called()

    def test_channel_change_triggers_repost(self, notifier, storage, fake_client):
        """Same pin_key, different channel desired -> fresh post in new channel."""
        storage.rows["status"] = PinRecord(
            pin_key="status",
            channel_id="C-OLD",
            message_ts="1.0",
            last_text_hash=_hash_text("hi"),
        )
        fake_client.chat_postMessage.return_value = {"ok": True, "ts": "9.0"}

        ok = upsert_pinned_message(
            notifier, storage, pin_key="status", text="hi", channel_id="C-NEW"
        )

        assert ok is True
        # No chat.update -- we skip straight to post when channel doesn't match.
        fake_client.chat_update.assert_not_called()
        fake_client.chat_postMessage.assert_called_once_with(
            channel="C-NEW", text="hi"
        )
        assert storage.rows["status"].channel_id == "C-NEW"
        assert storage.rows["status"].message_ts == "9.0"


class TestSkipUnconfigured:
    """Bail before touching the network when essentials are missing."""

    def test_no_token_skips_quietly(self, storage, monkeypatch):
        """No bot token -> False, no API call."""
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_CHANNEL_ID", raising=False)
        monkeypatch.delenv("SLACK_PINS_CHANNEL_ID", raising=False)
        n = NotificationService()

        ok = upsert_pinned_message(n, storage, pin_key="status", text="hi")

        assert ok is False

    def test_no_channel_skips_quietly(self, storage, fake_client, monkeypatch):
        """Token but no channel + no override -> False."""
        # Clear any leaked env so the only channel source is the constructor.
        monkeypatch.delenv("SLACK_CHANNEL_ID", raising=False)
        monkeypatch.delenv("SLACK_PINS_CHANNEL_ID", raising=False)
        n = NotificationService(bot_token="xoxb-test", channel_id=None, client=fake_client)

        ok = upsert_pinned_message(n, storage, pin_key="status", text="hi")

        assert ok is False
        fake_client.chat_postMessage.assert_not_called()


class TestPinsChannelEnvVar:
    """SLACK_PINS_CHANNEL_ID precedence."""

    def test_env_var_overrides_notifier_default(
        self, notifier, storage, fake_client, monkeypatch
    ):
        """With env set, post goes to that channel even though notifier has another default."""
        monkeypatch.setenv("SLACK_PINS_CHANNEL_ID", "C-PINS")
        fake_client.chat_postMessage.return_value = {"ok": True, "ts": "9.0"}

        upsert_pinned_message(notifier, storage, pin_key="status", text="hi")

        assert fake_client.chat_postMessage.call_args.kwargs["channel"] == "C-PINS"
        assert storage.rows["status"].channel_id == "C-PINS"

    def test_explicit_kwarg_beats_env_var(
        self, notifier, storage, fake_client, monkeypatch
    ):
        """channel_id= wins over SLACK_PINS_CHANNEL_ID."""
        monkeypatch.setenv("SLACK_PINS_CHANNEL_ID", "C-ENV")
        fake_client.chat_postMessage.return_value = {"ok": True, "ts": "9.0"}

        upsert_pinned_message(
            notifier, storage, pin_key="status", text="hi", channel_id="C-KWARG"
        )

        assert fake_client.chat_postMessage.call_args.kwargs["channel"] == "C-KWARG"

    def test_unset_env_falls_back_to_notifier(
        self, notifier, storage, fake_client, monkeypatch
    ):
        """No env, no kwarg -> notifier default."""
        monkeypatch.delenv("SLACK_PINS_CHANNEL_ID", raising=False)
        fake_client.chat_postMessage.return_value = {"ok": True, "ts": "9.0"}

        upsert_pinned_message(notifier, storage, pin_key="status", text="hi")

        assert fake_client.chat_postMessage.call_args.kwargs["channel"] == "C-DEFAULT"

    def test_empty_env_string_treated_as_unset(
        self, notifier, storage, fake_client, monkeypatch
    ):
        """SLACK_PINS_CHANNEL_ID='' shouldn't hijack routing to empty string.

        os.getenv returns '' for blank .env lines (not None), and that would
        silently route every pin to channel='' which 400s. The ``or`` chain
        in pins.py coerces falsy values to the next fallback.
        """
        monkeypatch.setenv("SLACK_PINS_CHANNEL_ID", "")
        fake_client.chat_postMessage.return_value = {"ok": True, "ts": "9.0"}

        upsert_pinned_message(notifier, storage, pin_key="status", text="hi")

        assert fake_client.chat_postMessage.call_args.kwargs["channel"] == "C-DEFAULT"


class TestPinKeyNamespace:
    """PinKey.namespace builds prefixed keys without typos."""

    def test_namespace_prepends_prefix_with_colon(self):
        """namespace('weekly')('revenue') -> 'weekly:revenue'."""
        weekly = PinKey.namespace("weekly")

        assert weekly("revenue") == "weekly:revenue"
        assert weekly("users") == "weekly:users"

    def test_namespaces_dont_collide(self):
        """Two namespaces produce non-overlapping keys for the same suffix."""
        weekly = PinKey.namespace("weekly")
        monthly = PinKey.namespace("monthly")

        assert weekly("metrics") != monthly("metrics")
