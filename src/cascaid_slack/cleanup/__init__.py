"""Bulk-delete tooling for Slack channels: pin-aware, archive-first, restorable.

Promotes ``debugging/cleanup_slack_channel.py`` from automatic-charting into
a reusable library. The main differences from the original:

* **Rules are explicit** -- patterns + autobot id come from a ``CleanupRules``
  dataclass instead of module-level constants. Each project defines its own.
* **Storage-agnostic** -- ``db_pinned_ts`` is a ``set[str]`` the consumer
  loads however it likes (SQL, JSON, hand-edited list).
* **No CLI** -- the lib provides ``run_cleanup()``; consumers wrap with
  argparse. Keeps the lib free of project-specific flag bikeshedding.
* **slack_sdk transport** -- WebClient + RateLimitErrorRetryHandler instead
  of hand-rolled requests + sleep loop.

Quick usage::

    from cascaid_slack.cleanup import (
        CleanupRules, run_cleanup, write_audit_csv, build_delete_client,
    )
    from slack_sdk import WebClient
    import os, re

    rules = CleanupRules(
        autobot_user_id="U0AHGL30RGB",
        redundant_patterns={
            "navigator_stats_table": re.compile(r"Navigator Tasks as of", re.I),
        },
    )

    read_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    delete_client = build_delete_client(
        os.environ.get("SLACK_USER_TOKEN") or os.environ["SLACK_BOT_TOKEN"]
    )

    result = run_cleanup(
        read_client=read_client,
        delete_client=delete_client,
        channel=os.environ["SLACK_CHANNEL_ID"],
        rules=rules,
        db_pinned_ts=load_pinned_ts_from_my_db(),
        execute=False,  # dry-run first
    )
    write_audit_csv(result.verdicts, "cleanup_preview.csv")

    print(result.action_summary)
    # Counter({'KEEP_HUMAN': 12, 'DELETE': 5043, 'KEEP_PINNED': 4, ...})
"""

from .archive import JsonlArchiveWriter, download_file
from .delete import build_delete_client, delete_message
from .rules import CleanupRules, MessageVerdict, classify_message
from .runner import CleanupResult, run_cleanup, write_audit_csv

__all__ = [
    "CleanupResult",
    "CleanupRules",
    "JsonlArchiveWriter",
    "MessageVerdict",
    "build_delete_client",
    "classify_message",
    "delete_message",
    "download_file",
    "run_cleanup",
    "write_audit_csv",
]
