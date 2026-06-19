"""High-level cleanup orchestration: classify all messages, optionally delete.

This is the consumer-facing API for cleanup. Wraps fetch + classify + delete
+ archive into a single ``run_cleanup`` call. CLIs (in consumer projects)
wrap this with argparse; tests can call it directly with FakeStorage and a
mocked WebClient.

Typical consumer flow::

    from cascaid_slack.cleanup import (
        CleanupRules, run_cleanup, write_audit_csv, build_delete_client,
    )

    rules = CleanupRules(
        autobot_user_id="U0AHGL30RGB",
        redundant_patterns={...},
    )
    db_pinned_ts = load_my_pinned_ts_from_db()   # consumer's choice

    result = run_cleanup(
        read_client=WebClient(token=os.environ["SLACK_BOT_TOKEN"]),
        delete_client=build_delete_client(os.environ.get("SLACK_USER_TOKEN") or ...),
        channel=os.environ["SLACK_CHANNEL_ID"],
        rules=rules,
        db_pinned_ts=db_pinned_ts,
        execute=args.execute,
        archive_path="cleanup_archive.jsonl",
        files_dir="cleanup_files/",
    )
    write_audit_csv(result.verdicts, "cleanup_preview.csv")
"""

from __future__ import annotations

import csv
import logging
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from slack_sdk import WebClient

from .api import fetch_all_messages, fetch_currently_pinned
from .archive import JsonlArchiveWriter, download_file
from .delete import delete_message
from .rules import CleanupRules, MessageVerdict, classify_message

logger = logging.getLogger(__name__)


@dataclass
class CleanupResult:
    """Everything the runner produces. Inspect from tests, log from CLIs.

    Attributes:
        verdicts: One MessageVerdict per channel message (in fetch order).
        action_summary: Counter of action -> count (DELETE / KEEP_*).
        attempted: Number of chat.delete calls actually made (0 in dry-run).
        succeeded: chat.delete calls that returned ok or already_gone.
        failed: chat.delete calls that returned a real error.
        archived: Records written to the JSONL archive.
        files_downloaded: File uploads successfully downloaded pre-delete.
        file_failures: File uploads that failed to download.
        failure_details: List of "ts: error_code" strings for the failures.
    """

    verdicts: list[MessageVerdict]
    action_summary: Counter
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    archived: int = 0
    files_downloaded: int = 0
    file_failures: int = 0
    failure_details: list[str] = field(default_factory=list)


def write_audit_csv(verdicts: list[MessageVerdict], path: str | os.PathLike) -> None:
    """Write verdicts to a CSV at ``path``. Stable column order from MessageVerdict.fieldnames()."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MessageVerdict.fieldnames())
        writer.writeheader()
        for v in verdicts:
            writer.writerow(v.__dict__)


def _classify_all(
    read_client: WebClient,
    channel: str,
    rules: CleanupRules,
    *,
    db_pinned_ts: set[str],
    slack_pinned_ts: set[str],
    keep_autobot: bool,
    keep_autobot_cutoff_ts: Optional[float],
    bot_id_filter: Optional[str],
    age_cutoff_ts: Optional[float],
) -> tuple[list[MessageVerdict], dict[str, dict]]:
    """Fetch + classify every channel message. Returns (verdicts, raw_by_ts).

    The raw_by_ts mapping lets the delete loop archive the full message dict
    (text + blocks + attachments + reactions + thread refs) rather than just
    the 120-char preview in MessageVerdict.text_preview.
    """
    verdicts: list[MessageVerdict] = []
    raw_by_ts: dict[str, dict] = {}
    for msg in fetch_all_messages(read_client, channel):
        verdicts.append(
            classify_message(
                msg,
                rules,
                db_pinned_ts=db_pinned_ts,
                slack_pinned_ts=slack_pinned_ts,
                keep_autobot=keep_autobot,
                keep_autobot_cutoff_ts=keep_autobot_cutoff_ts,
                bot_id_filter=bot_id_filter,
                age_cutoff_ts=age_cutoff_ts,
            )
        )
        ts = msg.get("ts")
        if ts:
            raw_by_ts[ts] = msg
    return verdicts, raw_by_ts


def _select_for_delete(
    verdicts: list[MessageVerdict],
    *,
    patterns: Optional[set[str]],
    limit: Optional[int],
) -> list[MessageVerdict]:
    """Filter DELETE verdicts down to the actual chat.delete targets."""
    to_delete = [v for v in verdicts if v.action == "DELETE"]
    if patterns:
        to_delete = [v for v in to_delete if v.pattern in patterns]
    if limit is not None:
        to_delete = to_delete[:limit]
    return to_delete


def _run_delete_loop(
    delete_client: WebClient,
    channel: str,
    to_delete: list[MessageVerdict],
    raw_by_ts: dict[str, dict],
    *,
    archive_path: str | os.PathLike,
    files_dir: str | os.PathLike,
    rate_seconds: float,
    file_download_token: Optional[str],
    progress_every: int,
) -> CleanupResult:
    """Inner delete loop. Archives + downloads files + deletes one msg at a time."""
    result = CleanupResult(verdicts=[], action_summary=Counter())
    result.attempted = len(to_delete)

    with JsonlArchiveWriter(archive_path) as archive:
        for i, v in enumerate(to_delete, start=1):
            raw = raw_by_ts.get(v.ts)
            archived_files: list[dict] = []

            if raw is not None:
                # Pre-download any file uploads; Slack GCs them after delete.
                if file_download_token:
                    for fd in raw.get("files") or []:
                        manifest = download_file(file_download_token, fd, files_dir)
                        if manifest is None:
                            continue
                        archived_files.append(manifest)
                        if manifest.get("local_path") and not manifest.get("error"):
                            result.files_downloaded += 1
                        else:
                            result.file_failures += 1

                # Write archive line BEFORE chat.delete so a crash leaves the
                # file in sync with what's actually been deleted.
                archive.write_record(
                    channel=channel,
                    verdict=v,
                    raw=raw,
                    archived_files=archived_files,
                )
                result.archived += 1

            ok, detail = delete_message(delete_client, channel, v.ts)
            if ok:
                result.succeeded += 1
            else:
                result.failed += 1
                result.failure_details.append(f"{v.ts}: {detail}")

            if progress_every and (i % progress_every == 0 or i == len(to_delete)):
                logger.info(
                    "[cascaid_slack.cleanup] progress: %d/%d "
                    "(success=%d, fail=%d, archived=%d, files=%d)",
                    i,
                    len(to_delete),
                    result.succeeded,
                    result.failed,
                    result.archived,
                    result.files_downloaded,
                )

            if i < len(to_delete):
                time.sleep(rate_seconds)

    return result


def run_cleanup(  # noqa: C901 -- thin orchestration with many knobs; splitting hides flow
    *,
    read_client: WebClient,
    delete_client: WebClient,
    channel: str,
    rules: CleanupRules,
    db_pinned_ts: Optional[set[str]] = None,
    execute: bool = False,
    keep_autobot: bool = True,
    keep_autobot_newer_than_days: Optional[int] = None,
    bot_id_filter: Optional[str] = None,
    older_than_days: Optional[int] = None,
    patterns: Optional[set[str]] = None,
    limit: Optional[int] = None,
    archive_path: str | os.PathLike = "slack_cleanup_archive.jsonl",
    files_dir: str | os.PathLike = "slack_cleanup_files",
    rate_seconds: float = 1.0,
    file_download_token: Optional[str] = None,
    progress_every: int = 25,
) -> CleanupResult:
    """Classify every message in ``channel`` and optionally delete the noise.

    Args:
        read_client: WebClient for conversations.history + pins.list (xoxb-).
        delete_client: WebClient for chat.delete. Can be the same as
            read_client (bot-only deletes) or a separate xoxp- user-token
            client for cross-bot deletes.
        channel: Channel ID to clean.
        rules: Per-project CleanupRules.
        db_pinned_ts: Set of ts values your pinned-message storage tracks.
            None means "no DB tracking" -- Slack's pins.list is the only
            protection. Pass ``set()`` explicitly if you mean "DB has zero
            tracked pins for this channel."
        execute: When False (default), dry-run -- no chat.delete calls.
        keep_autobot: When True (default), protect rules.autobot_user_id's posts.
        keep_autobot_newer_than_days: Narrow the autobot protection to
            posts from the last N days; older ones become eligible.
        bot_id_filter: Restrict eligibility to one bot integration.
        older_than_days: Only delete messages older than N days.
        patterns: When set, narrow DELETE candidates to these pattern names.
        limit: Cap deletes at N messages (for cautious first runs).
        archive_path: JSONL file appended to before each delete.
        files_dir: Directory for pre-delete file downloads.
        rate_seconds: Sleep between deletes. Belt-and-suspenders over the
            RateLimitErrorRetryHandler in build_delete_client.
        file_download_token: Token to use for url_private file downloads.
            Defaults to ``read_client.token`` since reads + file fetches
            share scopes. Pass None to skip downloads entirely (keep just
            the file metadata in the JSONL archive).
        progress_every: Log a progress line every N deletes. 0 to silence.

    Returns:
        A CleanupResult. Even in dry-run, .verdicts and .action_summary are
        populated; only the delete counters stay zero.
    """
    if db_pinned_ts is None:
        db_pinned_ts = set()

    slack_pinned_ts = fetch_currently_pinned(read_client, channel)
    logger.info(
        "[cascaid_slack.cleanup] %d ts in db_pinned_ts, %d currently pinned in Slack",
        len(db_pinned_ts),
        len(slack_pinned_ts),
    )

    # Convert day-based knobs to absolute UTC epoch cutoffs ONCE so the
    # classifier doesn't need to know about "days" at all.
    age_cutoff_ts: Optional[float] = None
    if older_than_days is not None:
        age_cutoff_ts = time.time() - older_than_days * 86400

    keep_autobot_cutoff_ts: Optional[float] = None
    if keep_autobot and keep_autobot_newer_than_days is not None:
        keep_autobot_cutoff_ts = time.time() - keep_autobot_newer_than_days * 86400

    verdicts, raw_by_ts = _classify_all(
        read_client,
        channel,
        rules,
        db_pinned_ts=db_pinned_ts,
        slack_pinned_ts=slack_pinned_ts,
        keep_autobot=keep_autobot,
        keep_autobot_cutoff_ts=keep_autobot_cutoff_ts,
        bot_id_filter=bot_id_filter,
        age_cutoff_ts=age_cutoff_ts,
    )

    summary = Counter(v.action for v in verdicts)
    logger.info(
        "[cascaid_slack.cleanup] classified %d messages: %s",
        len(verdicts),
        dict(summary),
    )

    if not execute:
        # Dry run -- return verdicts so the caller can write the CSV
        # without doing any deletes.
        return CleanupResult(verdicts=verdicts, action_summary=summary)

    to_delete = _select_for_delete(verdicts, patterns=patterns, limit=limit)
    if not to_delete:
        logger.info("[cascaid_slack.cleanup] nothing to delete after filters")
        return CleanupResult(
            verdicts=verdicts, action_summary=summary, attempted=0
        )

    logger.info(
        "[cascaid_slack.cleanup] EXECUTE: deleting %d messages at %.1fs intervals "
        "(estimated %.1f min)",
        len(to_delete),
        rate_seconds,
        len(to_delete) * rate_seconds / 60,
    )

    # Default file-download token to whatever the read client uses; if it's a
    # private attribute on WebClient, fall back to skipping downloads.
    if file_download_token is None:
        file_download_token = getattr(read_client, "token", None)

    result = _run_delete_loop(
        delete_client,
        channel,
        to_delete,
        raw_by_ts,
        archive_path=archive_path,
        files_dir=files_dir,
        rate_seconds=rate_seconds,
        file_download_token=file_download_token,
        progress_every=progress_every,
    )

    # Stitch the verdicts + summary back onto the loop result.
    result.verdicts = verdicts
    result.action_summary = summary

    logger.info(
        "[cascaid_slack.cleanup] DONE. Deleted %d, failed %d, archived %d, "
        "files_downloaded=%d, file_failures=%d",
        result.succeeded,
        result.failed,
        result.archived,
        result.files_downloaded,
        result.file_failures,
    )

    return result


__all__ = [
    "CleanupResult",
    "run_cleanup",
    "write_audit_csv",
]


def _epoch_to_iso(ts: float) -> str:  # pragma: no cover - tiny helper for repr
    """UTC ISO-8601 for an epoch second (used in log strings only)."""
    return datetime.fromtimestamp(ts).isoformat()
