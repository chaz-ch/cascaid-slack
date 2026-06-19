r"""Classification rules and per-message verdict for the cleanup pipeline.

The original automatic-charting cleanup script hardcoded REDUNDANT_PATTERNS,
AUTOBOT_USER_ID, and the categorization rules into one big function. That
worked for one project, but every consumer of cascaid-slack has different
"messages I want to delete" and different "bot identities I want to keep."

So in the library, **all of those are passed in via a CleanupRules dataclass**.
The classification function is pure: it takes one Slack message dict + the
rules + the runtime context, and returns a MessageVerdict. No side effects,
no I/O, trivially unit-testable.

CleanupRules is the only thing each project needs to customize:

    rules = CleanupRules(
        autobot_user_id="U0AHGL30RGB",
        redundant_patterns={
            "navigator_stats_table": re.compile(r"Navigator Tasks as of", re.I),
            "completed_navigator":   re.compile(r"Completed: \\d+ navigator tasks"),
            # ...
        },
    )
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class CleanupRules:
    """Per-project knobs for what counts as deletable noise.

    Attributes:
        autobot_user_id: OAuth user_id of the current bot. Messages from this
            identity are usually recent + intended; the classifier protects
            them by default (configurable via keep_autobot_cutoff_ts at
            classify-time). Set to None if there's no "current bot" to
            protect -- all bot messages are then eligible.
        redundant_patterns: Mapping of human-readable name -> compiled regex.
            A message whose ``text`` matches ANY of these patterns is a
            deletion candidate. Names appear in the audit CSV's ``pattern``
            column and are the values accepted by --pattern in CLI wrappers.
    """

    autobot_user_id: Optional[str] = None
    redundant_patterns: dict[str, re.Pattern[str]] = field(default_factory=dict)

    def pattern_names(self) -> list[str]:
        """Sorted list of pattern names for CLI --choices wiring."""
        return sorted(self.redundant_patterns.keys())


@dataclass
class MessageVerdict:
    """One row in the audit CSV: what we'd do with a single Slack message.

    The classifier returns these; the runner aggregates them into the audit
    CSV. They're also what the runner iterates over when actually deleting.

    Attributes:
        ts: Slack message timestamp (also the message ID).
        posted_at: Human-readable UTC timestamp derived from ts.
        author_kind: 'user:<user_id>' or 'bot:<bot_id>' or 'unknown'.
        author_label: Short label for the audit CSV ('human', 'autobot', etc).
        pattern: Matched key from CleanupRules.redundant_patterns, or 'other'.
        action: 'DELETE', 'KEEP_PINNED', 'KEEP_HUMAN', 'KEEP_AUTOBOT', or 'KEEP_OTHER'.
        reason: Free-form explanation of the verdict.
        text_preview: First 120 chars of message text (for the audit CSV only).
    """

    ts: str
    posted_at: str
    author_kind: str
    author_label: str
    pattern: str
    action: str
    reason: str
    text_preview: str

    @classmethod
    def fieldnames(cls) -> list[str]:
        """CSV column order, stable for downstream consumers."""
        return [
            "ts",
            "posted_at",
            "author_kind",
            "author_label",
            "pattern",
            "action",
            "reason",
            "text_preview",
        ]


def _author_classification(
    msg: dict, autobot_user_id: Optional[str]
) -> tuple[str, str, bool]:
    """Return (author_kind, author_label, is_human) for one message."""
    user = msg.get("user")
    bot_id = msg.get("bot_id")

    if user and user != autobot_user_id and not bot_id:
        return f"user:{user}", "human", True

    if autobot_user_id and user == autobot_user_id:
        return f"user:{user}", "autobot", False

    if bot_id:
        return f"bot:{bot_id}", f"bot_{bot_id}", False

    return "unknown", "unknown", False


def _match_pattern(text: str, patterns: dict[str, re.Pattern[str]]) -> str:
    """Return the first matching pattern name, or 'other' if none match."""
    for name, regex in patterns.items():
        if regex.search(text):
            return name
    return "other"


def classify_message(  # noqa: C901 -- sequence of guards; splitting hides the rule list
    msg: dict,
    rules: CleanupRules,
    *,
    db_pinned_ts: set[str],
    slack_pinned_ts: set[str],
    keep_autobot: bool = True,
    keep_autobot_cutoff_ts: Optional[float] = None,
    bot_id_filter: Optional[str] = None,
    age_cutoff_ts: Optional[float] = None,
) -> MessageVerdict:
    """Apply the keep/delete rules to one message and produce a verdict.

    Args:
        msg: Raw Slack message dict (from conversations.history).
        rules: Per-project CleanupRules (autobot id + redundant patterns).
        db_pinned_ts: Set of ts values that must NOT be deleted because
            they're tracked in the consumer's pinned-message storage
            (active rolling summaries). Deleting one breaks the self-heal
            flow in ``cascaid_slack.pins.upsert_pinned_message``.
        slack_pinned_ts: Set of ts values currently pinned in Slack but
            not tracked by the consumer (e.g. someone manually pinned an
            announcement). Treated as protected.
        keep_autobot: When True (default), preserve the current bot's posts.
            When False, autobot's pattern-matched posts become eligible.
        keep_autobot_cutoff_ts: When set, protect autobot posts NEWER than
            the cutoff ts. Older ones fall through to DELETE. Used to sweep
            pre-migration leftovers while keeping recent flow safe.
        bot_id_filter: Restrict eligibility to messages from one specific
            bot integration's bot_id.
        age_cutoff_ts: Only messages OLDER than this UTC epoch ts are
            eligible. Used for "delete only messages older than N days."

    Returns:
        A MessageVerdict with action one of DELETE / KEEP_* and a reason.
        No I/O, no side effects.
    """
    ts = msg.get("ts", "")
    text = (msg.get("text") or "")[:120].replace("\n", " ")
    posted_at = (
        datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if ts
        else ""
    )

    author_kind, author_label, is_human = _author_classification(
        msg, rules.autobot_user_id
    )

    def verdict(action: str, reason: str, pattern: str = "n/a") -> MessageVerdict:
        """Local helper to avoid repeating the eight-arg constructor."""
        return MessageVerdict(
            ts=ts,
            posted_at=posted_at,
            author_kind=author_kind,
            author_label=author_label,
            pattern=pattern,
            action=action,
            reason=reason,
            text_preview=text,
        )

    # 1. Human messages are sacrosanct.
    if is_human:
        return verdict("KEEP_HUMAN", "message authored by a human user")

    # 2. Pinned-message protection (DB tracking is the primary signal).
    if ts in db_pinned_ts:
        return verdict(
            "KEEP_PINNED",
            "ts is tracked in pinned-message storage (active rolling summary)",
        )

    # 3. Pinned-but-not-in-our-DB protection (manual pins).
    if ts in slack_pinned_ts:
        return verdict(
            "KEEP_PINNED", "message is currently pinned in Slack (not in our DB)"
        )

    # 4. Bot-id allowlist (narrow eligibility to one integration).
    bot_id = msg.get("bot_id")
    if bot_id_filter and bot_id != bot_id_filter:
        return verdict(
            "KEEP_OTHER", f"bot_id {bot_id!r} doesn't match --bot-id filter"
        )

    # 5. Age cutoff (only delete things older than the threshold).
    if age_cutoff_ts is not None and ts and float(ts) > age_cutoff_ts:
        return verdict("KEEP_OTHER", "message is newer than age cutoff")

    # 6. Pattern match -- everything past this point depends on whether
    #    the text matches a known-redundant pattern.
    matched_pattern = _match_pattern(text, rules.redundant_patterns)

    if matched_pattern == "other":
        # Bot post that isn't a known redundant pattern -- preserve by default.
        # Could be a manual post, a Build status update, etc.
        return verdict(
            "KEEP_OTHER",
            "bot message doesn't match a known redundant pattern",
            pattern="other",
        )

    # 7. Autobot protection (last gate before DELETE).
    user = msg.get("user")
    if (
        rules.autobot_user_id
        and user == rules.autobot_user_id
        and keep_autobot
    ):
        # When a cutoff is set, only protect posts NEWER than it.
        # Older autobot posts fall through to DELETE below.
        if keep_autobot_cutoff_ts is None or (
            ts and float(ts) >= keep_autobot_cutoff_ts
        ):
            return verdict(
                "KEEP_AUTOBOT",
                "keep_autobot is set; preserving current bot's posts",
                pattern=matched_pattern,
            )

    return verdict(
        "DELETE",
        f"redundant {matched_pattern} from {author_label}",
        pattern=matched_pattern,
    )
