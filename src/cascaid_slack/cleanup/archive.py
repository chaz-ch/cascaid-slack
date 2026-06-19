"""JSONL archive writer + file-bytes downloader for the cleanup pipeline.

Two responsibilities, both about preserving data BEFORE chat.delete makes it
unrecoverable:

1. **JSONL archive** -- ``JsonlArchiveWriter`` appends the full raw Slack
   message dict (text, blocks, attachments, reactions, thread refs) to a
   file BEFORE each delete. Flush+fsync per line so a kill -9 mid-loop
   leaves the archive in sync with what Slack actually deleted. Restorable
   via chat.postMessage by anyone with the file.

2. **File-bytes download** -- Slack GCs orphaned files after the last
   referencing message is deleted. ``download_file`` grabs the bytes from
   ``url_private`` BEFORE chat.delete fires.

Why ``download_file`` uses ``urllib.request`` instead of ``slack_sdk`` or
``requests``:
    Slack's ``url_private`` is a CDN URL on files.slack.com, not a Web API
    endpoint. WebClient doesn't have a method for it. We don't want to take
    a hard dep on ``requests`` just for one HTTP GET with a custom
    Authorization header. ``urllib`` is in stdlib and Does The Thing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import IO, Optional

from .rules import MessageVerdict

logger = logging.getLogger(__name__)


class JsonlArchiveWriter:
    """Append-mode JSONL writer with per-line flush+fsync for crash safety.

    Use as a context manager::

        with JsonlArchiveWriter("cleanup_archive.jsonl") as archive:
            for verdict, raw_msg in to_delete:
                archive.write_record(channel="C123", verdict=verdict, raw=raw_msg)
                # ... then call chat.delete

    Each record on disk is one self-contained JSON object terminated by a
    newline, which makes the file processable with jq / Polars / standard
    JSONL tooling. Re-runs append (the consumer can de-dupe by ts if needed).
    """

    def __init__(self, path: str | os.PathLike):
        """Open ``path`` in append mode; nothing written until write_record."""
        self._path = str(path)
        self._fh: Optional[IO[str]] = None

    def __enter__(self) -> "JsonlArchiveWriter":
        """Open the underlying file handle in append mode."""
        self._fh = open(self._path, "a", encoding="utf-8")  # noqa: SIM115
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        """Close the file handle."""
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def write_record(
        self,
        *,
        channel: str,
        verdict: MessageVerdict,
        raw: dict,
        archived_files: Optional[list[dict]] = None,
    ) -> None:
        """Append one archive record and fsync immediately.

        Args:
            channel: The Slack channel ID the message lived in.
            verdict: The MessageVerdict that earmarked this message for delete.
            raw: The full raw message dict from conversations.history.
            archived_files: Optional list of download manifests from
                ``download_file``. Use [] (or omit) if the message had no
                file uploads.
        """
        if self._fh is None:
            raise RuntimeError("JsonlArchiveWriter used outside of a `with` block")

        record = {
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "channel": channel,
            "verdict": verdict.__dict__,
            "message": raw,
            "archived_files": archived_files or [],
        }
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())


# ---------------------------------------------------------------------------
# File downloads
# ---------------------------------------------------------------------------

# Sanitize filename to avoid path traversal / weird shell chars.
# Keep alnum + dot + underscore + dash; everything else becomes underscore.
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def _safe_filename(name: str) -> str:
    """Strip path-traversal chars from a Slack-provided filename."""
    return _UNSAFE_FILENAME_CHARS.sub("_", name)


def download_file(
    token: str, file_dict: dict, target_dir: str | os.PathLike
) -> Optional[dict]:
    """Download a Slack file to disk; return a manifest entry or None.

    ``file_dict`` is one element from a message's ``files`` list. Reads
    ``url_private_download`` (preferred) or ``url_private`` with the bot
    token in an Authorization header.

    Args:
        token: A Slack token (xoxb- with files:read scope is enough).
        file_dict: One Slack file dict.
        target_dir: Directory to write the bytes into. Created if missing.

    Returns:
        Manifest dict on success (file_id, name, local_path, bytes_downloaded,
        mimetype, url_private). Manifest dict with ``error`` key on download
        failure. None only when the file_dict has no usable URL.
    """
    file_id = file_dict.get("id", "unknown")
    name = file_dict.get("name") or f"{file_id}.bin"
    url = file_dict.get("url_private_download") or file_dict.get("url_private")
    if not url:
        return None

    os.makedirs(target_dir, exist_ok=True)
    target_path = os.path.join(target_dir, f"{file_id}_{_safe_filename(name)}")

    try:
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}"}
        )
        # nosec B310 -- URL comes from Slack API response, not user input;
        # the bearer-auth header makes it pointless for an attacker to
        # redirect us anyway (token isn't theirs).
        with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310
            with open(target_path, "wb") as out:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
        size = os.path.getsize(target_path)
        return {
            "file_id": file_id,
            "name": name,
            "local_path": target_path,
            "bytes_downloaded": size,
            "mimetype": file_dict.get("mimetype"),
            "url_private": file_dict.get("url_private"),
        }
    except (urllib.error.URLError, OSError) as exc:
        logger.warning(
            "[cascaid_slack.cleanup] file download failed for %s: %s", file_id, exc
        )
        return {
            "file_id": file_id,
            "name": name,
            "local_path": None,
            "bytes_downloaded": 0,
            "error": str(exc),
            "url_private": file_dict.get("url_private"),
        }
