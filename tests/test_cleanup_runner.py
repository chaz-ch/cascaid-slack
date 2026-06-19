"""Tests for cascaid_slack.cleanup.runner.run_cleanup + write_audit_csv.

Integration-flavored: a MagicMock WebClient stands in for Slack, but the
classifier, archive writer, and CSV writer are all real. Verifies the
end-to-end flow without hitting the network.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cascaid_slack.cleanup import (
    CleanupResult,
    CleanupRules,
    run_cleanup,
    write_audit_csv,
)


@pytest.fixture
def rules() -> CleanupRules:
    """A minimal ruleset modeled on the original automatic-charting one."""
    return CleanupRules(
        autobot_user_id="U-AUTO",
        redundant_patterns={
            "navigator_stats": re.compile(r"Navigator Tasks as of", re.IGNORECASE),
            "smoke_test": re.compile(r"smoke test", re.IGNORECASE),
        },
    )


def _msg(ts: str, text: str, *, user: str = None, bot_id: str = None) -> dict:
    """Build a Slack-ish message dict for the fake history response."""
    out: dict = {"ts": ts, "text": text}
    if user:
        out["user"] = user
    if bot_id:
        out["bot_id"] = bot_id
    return out


@pytest.fixture
def read_client():
    """Mock WebClient for reads. Override conversations_history per test."""
    client = MagicMock()
    client.pins_list.return_value = {"ok": True, "items": []}
    client.conversations_history.return_value = {
        "ok": True,
        "messages": [],
        "response_metadata": {"next_cursor": ""},
    }
    return client


@pytest.fixture
def delete_client():
    """Mock WebClient for deletes. Default: chat.delete always succeeds."""
    client = MagicMock()
    client.chat_delete.return_value = {"ok": True}
    return client


class TestDryRun:
    """execute=False -> classify only, no delete calls."""

    def test_dry_run_returns_verdicts_only(
        self, read_client, delete_client, rules
    ):
        """Dry-run populates verdicts + summary, leaves attempt/succeed/fail at 0."""
        read_client.conversations_history.return_value = {
            "ok": True,
            "messages": [
                _msg("1.0", "Navigator Tasks as of 9am", bot_id="B-LEGACY"),
                _msg("2.0", "Real human message", user="U-REAL"),
                _msg("3.0", "Random output", bot_id="B-OTHER"),
            ],
            "response_metadata": {"next_cursor": ""},
        }

        result = run_cleanup(
            read_client=read_client,
            delete_client=delete_client,
            channel="C-X",
            rules=rules,
            execute=False,
        )

        assert len(result.verdicts) == 3
        assert result.action_summary["DELETE"] == 1
        assert result.action_summary["KEEP_HUMAN"] == 1
        assert result.action_summary["KEEP_OTHER"] == 1
        assert result.attempted == 0
        assert result.succeeded == 0
        delete_client.chat_delete.assert_not_called()


class TestExecuteMode:
    """execute=True -> classify + delete + archive."""

    def test_execute_deletes_matched_messages(
        self, tmp_path: Path, read_client, delete_client, rules
    ):
        """DELETE verdicts trigger chat.delete; archives are written."""
        read_client.conversations_history.return_value = {
            "ok": True,
            "messages": [
                _msg("1.0", "Navigator Tasks as of 9am", bot_id="B-LEGACY"),
                _msg("2.0", "smoke test bla", bot_id="B-LEGACY"),
                _msg("3.0", "human msg", user="U-REAL"),
            ],
            "response_metadata": {"next_cursor": ""},
        }
        archive_path = tmp_path / "archive.jsonl"

        result = run_cleanup(
            read_client=read_client,
            delete_client=delete_client,
            channel="C-X",
            rules=rules,
            execute=True,
            archive_path=archive_path,
            files_dir=tmp_path / "files",
            rate_seconds=0.0,  # tests shouldn't sleep
            progress_every=0,
        )

        assert result.attempted == 2
        assert result.succeeded == 2
        assert result.failed == 0
        assert result.archived == 2
        assert delete_client.chat_delete.call_count == 2

        # Archive has both deleted messages
        lines = archive_path.read_text().strip().split("\n")
        assert len(lines) == 2
        recs = [json.loads(line) for line in lines]
        assert {r["message"]["ts"] for r in recs} == {"1.0", "2.0"}

    def test_pin_protection_blocks_delete(
        self, tmp_path: Path, read_client, delete_client, rules
    ):
        """Pinned ts in db_pinned_ts -> KEEP_PINNED, no chat.delete."""
        read_client.conversations_history.return_value = {
            "ok": True,
            "messages": [
                _msg("1.0", "Navigator Tasks as of 9am", bot_id="B-LEGACY"),
            ],
            "response_metadata": {"next_cursor": ""},
        }

        result = run_cleanup(
            read_client=read_client,
            delete_client=delete_client,
            channel="C-X",
            rules=rules,
            db_pinned_ts={"1.0"},  # protect this ts
            execute=True,
            archive_path=tmp_path / "archive.jsonl",
            files_dir=tmp_path / "files",
            rate_seconds=0.0,
        )

        assert result.attempted == 0
        delete_client.chat_delete.assert_not_called()
        assert result.action_summary["KEEP_PINNED"] == 1

    def test_pattern_filter_narrows_deletes(
        self, tmp_path: Path, read_client, delete_client, rules
    ):
        """patterns={'smoke_test'} -> only smoke_test verdicts get deleted."""
        read_client.conversations_history.return_value = {
            "ok": True,
            "messages": [
                _msg("1.0", "Navigator Tasks as of 9am", bot_id="B-LEGACY"),
                _msg("2.0", "smoke test bla", bot_id="B-LEGACY"),
            ],
            "response_metadata": {"next_cursor": ""},
        }

        result = run_cleanup(
            read_client=read_client,
            delete_client=delete_client,
            channel="C-X",
            rules=rules,
            execute=True,
            patterns={"smoke_test"},
            archive_path=tmp_path / "archive.jsonl",
            files_dir=tmp_path / "files",
            rate_seconds=0.0,
        )

        assert result.attempted == 1
        # The smoke_test message is the one that got deleted.
        delete_client.chat_delete.assert_called_once_with(channel="C-X", ts="2.0")

    def test_limit_caps_deletes(
        self, tmp_path: Path, read_client, delete_client, rules
    ):
        """limit=N -> only first N DELETEs go through."""
        read_client.conversations_history.return_value = {
            "ok": True,
            "messages": [
                _msg(f"{i}.0", "Navigator Tasks as of x", bot_id="B-LEGACY")
                for i in range(1, 6)  # 5 deletable messages
            ],
            "response_metadata": {"next_cursor": ""},
        }

        result = run_cleanup(
            read_client=read_client,
            delete_client=delete_client,
            channel="C-X",
            rules=rules,
            execute=True,
            limit=2,
            archive_path=tmp_path / "archive.jsonl",
            files_dir=tmp_path / "files",
            rate_seconds=0.0,
        )

        assert result.attempted == 2
        assert delete_client.chat_delete.call_count == 2

    def test_delete_failures_recorded(
        self, tmp_path: Path, read_client, delete_client, rules
    ):
        """chat.delete failing -> failed counter + failure_details captured."""
        from slack_sdk.errors import SlackApiError

        read_client.conversations_history.return_value = {
            "ok": True,
            "messages": [
                _msg("1.0", "Navigator Tasks as of 9am", bot_id="B-LEGACY"),
            ],
            "response_metadata": {"next_cursor": ""},
        }
        delete_client.chat_delete.side_effect = SlackApiError(
            message="x", response={"ok": False, "error": "cant_delete_message"}
        )

        result = run_cleanup(
            read_client=read_client,
            delete_client=delete_client,
            channel="C-X",
            rules=rules,
            execute=True,
            archive_path=tmp_path / "archive.jsonl",
            files_dir=tmp_path / "files",
            rate_seconds=0.0,
        )

        assert result.attempted == 1
        assert result.succeeded == 0
        assert result.failed == 1
        assert "cant_delete_message" in result.failure_details[0]
        # Archive STILL written even though delete failed (the archive
        # represents our INTENT, so we keep the row for forensic purposes).
        assert result.archived == 1


class TestWriteAuditCsv:
    """write_audit_csv produces a parseable CSV with stable columns."""

    def test_writes_one_row_per_verdict(self, tmp_path: Path, rules):
        """Two verdicts -> two CSV rows with the documented column order."""
        from cascaid_slack.cleanup import MessageVerdict

        verdicts = [
            MessageVerdict(
                ts="1.0",
                posted_at="2026-06-19 18:00:00",
                author_kind="user:U-REAL",
                author_label="human",
                pattern="n/a",
                action="KEEP_HUMAN",
                reason="human user",
                text_preview="hi",
            ),
            MessageVerdict(
                ts="2.0",
                posted_at="2026-06-19 19:00:00",
                author_kind="bot:B-X",
                author_label="bot_B-X",
                pattern="navigator_stats",
                action="DELETE",
                reason="redundant",
                text_preview="Navigator Tasks...",
            ),
        ]

        path = tmp_path / "preview.csv"
        write_audit_csv(verdicts, path)

        with path.open() as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        assert len(rows) == 2
        assert rows[0]["action"] == "KEEP_HUMAN"
        assert rows[1]["pattern"] == "navigator_stats"


class TestCleanupResult:
    """CleanupResult defaults sanity check."""

    def test_default_counters_zero(self):
        """A bare CleanupResult has all numeric counters at 0."""
        from collections import Counter

        r = CleanupResult(verdicts=[], action_summary=Counter())

        assert r.attempted == 0
        assert r.succeeded == 0
        assert r.failed == 0
        assert r.archived == 0
        assert r.files_downloaded == 0
        assert r.file_failures == 0
        assert r.failure_details == []
