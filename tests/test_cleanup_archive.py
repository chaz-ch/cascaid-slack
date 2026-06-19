"""Tests for cascaid_slack.cleanup.archive: JSONL writer + file downloader.

Uses real files (tmp_path) so we get true filesystem behaviour: atomic
append, fsync, parent-dir creation. File downloads are exercised via
urllib mocking so we don't hit the network.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from cascaid_slack.cleanup import JsonlArchiveWriter, MessageVerdict, download_file


@pytest.fixture
def verdict() -> MessageVerdict:
    """A representative DELETE verdict for archive tests."""
    return MessageVerdict(
        ts="1.0",
        posted_at="2026-06-19 18:00:00",
        author_kind="bot:B-AUTO",
        author_label="bot_B-AUTO",
        pattern="navigator_stats",
        action="DELETE",
        reason="redundant navigator_stats from bot_B-AUTO",
        text_preview="Navigator Tasks as of...",
    )


class TestJsonlArchiveWriter:
    """JSONL append + fsync round-trip."""

    def test_records_round_trip(self, tmp_path: Path, verdict):
        """Two records written -> file has two parseable JSON lines."""
        path = tmp_path / "archive.jsonl"
        with JsonlArchiveWriter(path) as archive:
            archive.write_record(
                channel="C-X",
                verdict=verdict,
                raw={"ts": "1.0", "text": "hello"},
            )
            archive.write_record(
                channel="C-X",
                verdict=verdict,
                raw={"ts": "2.0", "text": "world"},
            )

        lines = path.read_text().strip().split("\n")
        assert len(lines) == 2
        rec0 = json.loads(lines[0])
        assert rec0["channel"] == "C-X"
        assert rec0["message"]["text"] == "hello"
        assert rec0["verdict"]["action"] == "DELETE"
        assert "archived_at" in rec0
        assert rec0["archived_files"] == []

    def test_append_mode_preserves_prior_runs(self, tmp_path: Path, verdict):
        """Re-opening the archive appends; doesn't truncate."""
        path = tmp_path / "archive.jsonl"

        with JsonlArchiveWriter(path) as a1:
            a1.write_record(channel="C-X", verdict=verdict, raw={"ts": "1.0"})

        with JsonlArchiveWriter(path) as a2:
            a2.write_record(channel="C-X", verdict=verdict, raw={"ts": "2.0"})

        lines = path.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_write_outside_context_raises(self, tmp_path: Path, verdict):
        """Using the writer without entering the context manager is an error."""
        archive = JsonlArchiveWriter(tmp_path / "x.jsonl")

        with pytest.raises(RuntimeError, match="outside of a `with` block"):
            archive.write_record(channel="C", verdict=verdict, raw={})

    def test_archived_files_persisted(self, tmp_path: Path, verdict):
        """archived_files parameter lands on disk in the record."""
        path = tmp_path / "archive.jsonl"

        with JsonlArchiveWriter(path) as archive:
            archive.write_record(
                channel="C-X",
                verdict=verdict,
                raw={"ts": "1.0"},
                archived_files=[
                    {"file_id": "F1", "local_path": "/tmp/F1_x.bin"}  # nosec B108
                ],
            )

        rec = json.loads(path.read_text().strip())
        assert rec["archived_files"][0]["file_id"] == "F1"


class TestDownloadFile:
    """download_file: write bytes from url_private to a sanitized filename."""

    def test_happy_path_writes_bytes(self, tmp_path: Path):
        """Successful download -> manifest with local_path + bytes_downloaded."""
        file_dict = {
            "id": "F123",
            "name": "report.tsv",
            "url_private_download": "https://files.slack.com/F123/report.tsv",
            "mimetype": "text/tab-separated-values",
        }
        fake_body = b"row1\trow2\nrow3\trow4\n"

        # Mock urlopen to return our bytes via a context-manager-like object.
        class FakeResp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        with patch(
            "cascaid_slack.cleanup.archive.urllib.request.urlopen",
            return_value=FakeResp(fake_body),
        ):
            manifest = download_file("xoxb-test", file_dict, tmp_path)

        assert manifest is not None
        assert manifest["bytes_downloaded"] == len(fake_body)
        assert manifest["local_path"].endswith("F123_report.tsv")
        assert Path(manifest["local_path"]).read_bytes() == fake_body

    def test_no_url_returns_none(self, tmp_path: Path):
        """File dict with no usable URL -> None (caller skips it)."""
        file_dict = {"id": "F1", "name": "x.bin"}  # no url_private/_download

        out = download_file("xoxb-test", file_dict, tmp_path)

        assert out is None

    def test_filename_sanitization(self, tmp_path: Path):
        """Filenames with path-traversal chars get scrubbed."""
        file_dict = {
            "id": "F-X",
            "name": "../../etc/passwd",
            "url_private": "https://files.slack.com/F-X/passwd",
        }

        class FakeResp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        with patch(
            "cascaid_slack.cleanup.archive.urllib.request.urlopen",
            return_value=FakeResp(b"x"),
        ):
            manifest = download_file("xoxb-test", file_dict, tmp_path)

        # Resulting path stays inside tmp_path (no traversal). The '..' chars
        # may remain in the filename, but '/' is scrubbed -- which is what
        # actually prevents path traversal. The crucial property is that
        # local.parent equals tmp_path: we did NOT escape the target dir.
        local = Path(manifest["local_path"])
        assert local.parent == tmp_path
        assert "/" not in local.name

    def test_download_failure_returns_manifest_with_error(self, tmp_path: Path):
        """URLError -> manifest with error key, no local_path, no crash."""
        import urllib.error

        file_dict = {
            "id": "F-X",
            "name": "x.bin",
            "url_private_download": "https://files.slack.com/F-X/x.bin",
        }

        with patch(
            "cascaid_slack.cleanup.archive.urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            manifest = download_file("xoxb-test", file_dict, tmp_path)

        assert manifest is not None
        assert manifest["local_path"] is None
        assert "error" in manifest
        assert manifest["bytes_downloaded"] == 0
