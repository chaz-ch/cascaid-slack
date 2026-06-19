"""Tests for cascaid_slack.storage.JsonFileEventLogStorage.

Mirror of test_storage_json.py for the EventLog flavour. We don't re-test the
shared _JsonFileBackend plumbing (atomic write, parent dir creation) -- the
pin tests cover that. Here we just verify the EventLog-shaped API maps right.
"""

from __future__ import annotations

from pathlib import Path

from cascaid_slack import EventLogState
from cascaid_slack.storage import JsonFileEventLogStorage


class TestJsonFileEventLogStorage:
    """File-backed event-log storage round-trip."""

    def test_load_returns_none_when_file_absent(self, tmp_path: Path):
        """Cold start: no file yet, load returns None."""
        storage = JsonFileEventLogStorage(tmp_path / "events.json")

        assert storage.load_event_log("C-NEVER-USED") is None

    def test_save_then_load_round_trip(self, tmp_path: Path):
        """Saved state comes back identical via load."""
        storage = JsonFileEventLogStorage(tmp_path / "events.json")
        state = EventLogState(
            channel_id="C-OPS",
            last_header_date="2026-06-16",
            last_header_ts="9.5",
        )

        storage.save_event_log(state)
        got = storage.load_event_log("C-OPS")

        assert got == state

    def test_save_overwrites_prior_state(self, tmp_path: Path):
        """Same channel_id written twice -> second wins."""
        storage = JsonFileEventLogStorage(tmp_path / "events.json")
        storage.save_event_log(
            EventLogState(channel_id="C", last_header_date="2026-06-15", last_header_ts=None)
        )
        storage.save_event_log(
            EventLogState(channel_id="C", last_header_date="2026-06-16", last_header_ts="2.0")
        )

        got = storage.load_event_log("C")
        assert got.last_header_date == "2026-06-16"
        assert got.last_header_ts == "2.0"

    def test_multiple_channels_coexist(self, tmp_path: Path):
        """Storage handles many channel_ids in the same file independently."""
        storage = JsonFileEventLogStorage(tmp_path / "events.json")

        for i in range(3):
            storage.save_event_log(
                EventLogState(
                    channel_id=f"C-{i}",
                    last_header_date=f"2026-06-{16 + i}",
                    last_header_ts=None,
                )
            )

        for i in range(3):
            got = storage.load_event_log(f"C-{i}")
            assert got.last_header_date == f"2026-06-{16 + i}"
