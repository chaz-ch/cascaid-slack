"""Tests for cascaid_slack.storage.JsonFilePinStateStorage.

SqlAlchemy storage isn't tested here because it'd pull SQLAlchemy into the
test deps -- and the consumers that use it (automatic-charting) already have
their own integration coverage via the migration tests.
"""

from __future__ import annotations

from pathlib import Path

from cascaid_slack import PinRecord
from cascaid_slack.storage import JsonFilePinStateStorage


class TestJsonFilePinStateStorage:
    """File-backed storage round-trip."""

    def test_load_returns_none_when_file_absent(self, tmp_path: Path):
        """Cold start: no file yet, load returns None."""
        storage = JsonFilePinStateStorage(tmp_path / "pins.json")

        assert storage.load_pin("anything") is None

    def test_save_then_load_round_trip(self, tmp_path: Path):
        """Saved record comes back identical via load."""
        storage = JsonFilePinStateStorage(tmp_path / "pins.json")
        rec = PinRecord(
            pin_key="weekly:revenue",
            channel_id="C-PINS",
            message_ts="1.5",
            last_text_hash="abc123",
        )

        storage.save_pin(rec)
        got = storage.load_pin("weekly:revenue")

        assert got == rec

    def test_save_overwrites_prior_record(self, tmp_path: Path):
        """Same pin_key written twice -> the second wins (last-write-wins)."""
        storage = JsonFilePinStateStorage(tmp_path / "pins.json")
        storage.save_pin(
            PinRecord(pin_key="k", channel_id="C-OLD", message_ts="1.0", last_text_hash="h1")
        )
        storage.save_pin(
            PinRecord(pin_key="k", channel_id="C-NEW", message_ts="2.0", last_text_hash="h2")
        )

        got = storage.load_pin("k")
        assert got.channel_id == "C-NEW"
        assert got.message_ts == "2.0"
        assert got.last_text_hash == "h2"

    def test_delete_removes_record(self, tmp_path: Path):
        """delete_pin clears the row; load_pin then returns None."""
        storage = JsonFilePinStateStorage(tmp_path / "pins.json")
        storage.save_pin(
            PinRecord(pin_key="k", channel_id="C", message_ts="1.0", last_text_hash="h")
        )

        storage.delete_pin("k")

        assert storage.load_pin("k") is None

    def test_delete_missing_key_is_noop(self, tmp_path: Path):
        """delete_pin on a key that was never saved doesn't raise."""
        storage = JsonFilePinStateStorage(tmp_path / "pins.json")

        # Just shouldn't raise.
        storage.delete_pin("never-saved")

    def test_multiple_records_coexist(self, tmp_path: Path):
        """Storage handles many pin_keys in the same file independently."""
        storage = JsonFilePinStateStorage(tmp_path / "pins.json")

        for i in range(5):
            storage.save_pin(
                PinRecord(
                    pin_key=f"key_{i}",
                    channel_id=f"C-{i}",
                    message_ts=f"{i}.0",
                    last_text_hash=f"h{i}",
                )
            )

        for i in range(5):
            got = storage.load_pin(f"key_{i}")
            assert got.channel_id == f"C-{i}"

    def test_atomic_write_creates_parent_dirs(self, tmp_path: Path):
        """Storage at a/b/c/pins.json works even if a/b/c doesn't exist yet."""
        nested = tmp_path / "a" / "b" / "c" / "pins.json"
        storage = JsonFilePinStateStorage(nested)

        storage.save_pin(
            PinRecord(pin_key="k", channel_id="C", message_ts="1.0", last_text_hash="h")
        )

        assert nested.exists()
        assert storage.load_pin("k").channel_id == "C"
