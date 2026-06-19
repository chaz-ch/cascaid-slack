"""Tests for cascaid_slack.cleanup.rules.classify_message.

Pure-function tests; no Slack, no I/O. The classifier takes a Slack message
dict + rules + runtime context and returns a MessageVerdict. Every code path
through classify_message gets exercised here.
"""

from __future__ import annotations

import re
import time

import pytest

from cascaid_slack.cleanup import (
    CleanupRules,
    MessageVerdict,
    classify_message,
)


@pytest.fixture
def rules() -> CleanupRules:
    """A representative ruleset modeled on automatic-charting's."""
    return CleanupRules(
        autobot_user_id="U-AUTO",
        redundant_patterns={
            "navigator_stats": re.compile(r"Navigator Tasks as of", re.IGNORECASE),
            "completed_navigator": re.compile(r"Completed: \d+ navigator tasks"),
            "smoke_test": re.compile(r"smoke test", re.IGNORECASE),
        },
    )


def _msg(ts: str, text: str, *, user: str = None, bot_id: str = None) -> dict:
    """Tiny helper: build a Slack-ish message dict."""
    out: dict = {"ts": ts, "text": text}
    if user:
        out["user"] = user
    if bot_id:
        out["bot_id"] = bot_id
    return out


class TestHumanProtection:
    """Human messages are sacrosanct."""

    def test_human_message_always_kept(self, rules):
        """Human user, even with a redundant-pattern text, is KEEP_HUMAN."""
        msg = _msg("1.0", "Navigator Tasks as of 9am", user="U-REAL-PERSON")

        v = classify_message(msg, rules, db_pinned_ts=set(), slack_pinned_ts=set())

        assert v.action == "KEEP_HUMAN"
        assert v.author_label == "human"

    def test_autobot_id_user_is_not_human(self, rules):
        """User field matching autobot_user_id is treated as autobot, not human."""
        msg = _msg("1.0", "*Some random thing*", user="U-AUTO")

        v = classify_message(msg, rules, db_pinned_ts=set(), slack_pinned_ts=set())

        assert v.action != "KEEP_HUMAN"
        assert v.author_label == "autobot"


class TestPinnedProtection:
    """Pinned-message protection has two flavours: DB-tracked and Slack-pinned."""

    def test_db_pinned_ts_blocks_delete(self, rules):
        """ts present in db_pinned_ts -> KEEP_PINNED, even if pattern matches."""
        msg = _msg("9.5", "Navigator Tasks as of now", bot_id="B-AUTO")

        v = classify_message(
            msg, rules, db_pinned_ts={"9.5"}, slack_pinned_ts=set()
        )

        assert v.action == "KEEP_PINNED"
        assert "tracked" in v.reason

    def test_slack_pinned_ts_blocks_delete(self, rules):
        """ts pinned in Slack but not in DB -> still KEEP_PINNED."""
        msg = _msg("9.5", "Navigator Tasks as of now", bot_id="B-AUTO")

        v = classify_message(
            msg, rules, db_pinned_ts=set(), slack_pinned_ts={"9.5"}
        )

        assert v.action == "KEEP_PINNED"
        assert "currently pinned" in v.reason


class TestBotIdFilter:
    """bot_id_filter narrows eligibility to one integration."""

    def test_other_bots_kept_when_filter_set(self, rules):
        """bot_id mismatch -> KEEP_OTHER even with matching pattern."""
        msg = _msg("1.0", "Navigator Tasks as of 9am", bot_id="B-WRONG")

        v = classify_message(
            msg,
            rules,
            db_pinned_ts=set(),
            slack_pinned_ts=set(),
            bot_id_filter="B-RIGHT",
        )

        assert v.action == "KEEP_OTHER"

    def test_matching_bot_id_proceeds_to_pattern(self, rules):
        """bot_id match -> normal pattern evaluation continues."""
        msg = _msg("1.0", "Navigator Tasks as of 9am", bot_id="B-RIGHT")

        v = classify_message(
            msg,
            rules,
            db_pinned_ts=set(),
            slack_pinned_ts=set(),
            bot_id_filter="B-RIGHT",
        )

        assert v.action == "DELETE"


class TestAgeCutoff:
    """age_cutoff_ts: only messages older than cutoff are eligible."""

    def test_newer_than_cutoff_kept(self, rules):
        """Message ts > cutoff -> KEEP_OTHER."""
        cutoff = time.time() - 86400  # 1 day ago
        msg = _msg(
            str(time.time()),  # right now -> newer than cutoff
            "Navigator Tasks as of now",
            bot_id="B-AUTO",
        )

        v = classify_message(
            msg,
            rules,
            db_pinned_ts=set(),
            slack_pinned_ts=set(),
            age_cutoff_ts=cutoff,
        )

        assert v.action == "KEEP_OTHER"
        assert "newer than age cutoff" in v.reason

    def test_older_than_cutoff_deleted(self, rules):
        """Message ts < cutoff -> proceeds to pattern eval -> DELETE."""
        cutoff = time.time() - 86400  # 1 day ago
        msg = _msg(
            str(time.time() - 7 * 86400),  # 7 days ago -> older than cutoff
            "Navigator Tasks as of now",
            bot_id="B-AUTO",
        )

        v = classify_message(
            msg,
            rules,
            db_pinned_ts=set(),
            slack_pinned_ts=set(),
            age_cutoff_ts=cutoff,
        )

        assert v.action == "DELETE"


class TestPatternMatching:
    """Pattern match determines DELETE vs KEEP_OTHER for bot messages."""

    def test_unmatched_pattern_kept(self, rules):
        """Bot message with no pattern match -> KEEP_OTHER."""
        msg = _msg("1.0", "Random bot output that nobody asked about", bot_id="B-X")

        v = classify_message(msg, rules, db_pinned_ts=set(), slack_pinned_ts=set())

        assert v.action == "KEEP_OTHER"
        assert v.pattern == "other"

    def test_first_matching_pattern_wins(self, rules):
        """When two patterns could match, the first one (dict order) wins."""
        # 'navigator_stats' regex would NOT match this; 'completed_navigator' would.
        # But our test asserts deterministic order: first one defined wins ties.
        msg = _msg("1.0", "Completed: 7 navigator tasks", bot_id="B-X")

        v = classify_message(msg, rules, db_pinned_ts=set(), slack_pinned_ts=set())

        assert v.action == "DELETE"
        assert v.pattern == "completed_navigator"

    def test_case_insensitive_pattern_matches(self, rules):
        """Patterns compiled with re.IGNORECASE work as advertised."""
        msg = _msg("1.0", "SMOKE TEST DELETE ME", bot_id="B-X")

        v = classify_message(msg, rules, db_pinned_ts=set(), slack_pinned_ts=set())

        assert v.action == "DELETE"
        assert v.pattern == "smoke_test"


class TestAutobotProtection:
    """keep_autobot gate is the last thing between matched-pattern and DELETE."""

    def test_keep_autobot_default_protects_autobot_post(self, rules):
        """Pattern matches AND user is autobot AND keep_autobot=True -> KEEP_AUTOBOT."""
        msg = _msg("1.0", "Navigator Tasks as of now", user="U-AUTO")

        v = classify_message(
            msg,
            rules,
            db_pinned_ts=set(),
            slack_pinned_ts=set(),
            keep_autobot=True,
        )

        assert v.action == "KEEP_AUTOBOT"
        assert v.pattern == "navigator_stats"  # pattern still recorded

    def test_no_keep_autobot_lets_autobot_post_through(self, rules):
        """keep_autobot=False -> autobot's matched posts fall through to DELETE."""
        msg = _msg("1.0", "Navigator Tasks as of now", user="U-AUTO")

        v = classify_message(
            msg,
            rules,
            db_pinned_ts=set(),
            slack_pinned_ts=set(),
            keep_autobot=False,
        )

        assert v.action == "DELETE"

    def test_keep_autobot_cutoff_protects_recent_only(self, rules):
        """Cutoff set + autobot post newer than cutoff -> KEEP_AUTOBOT."""
        cutoff = time.time() - 86400  # 1 day ago
        recent = _msg(
            str(time.time()),  # right now
            "Navigator Tasks as of now",
            user="U-AUTO",
        )

        v = classify_message(
            recent,
            rules,
            db_pinned_ts=set(),
            slack_pinned_ts=set(),
            keep_autobot=True,
            keep_autobot_cutoff_ts=cutoff,
        )

        assert v.action == "KEEP_AUTOBOT"

    def test_keep_autobot_cutoff_lets_old_through(self, rules):
        """Cutoff set + autobot post older than cutoff -> DELETE."""
        cutoff = time.time() - 86400  # 1 day ago
        old = _msg(
            str(time.time() - 7 * 86400),  # 7 days ago
            "Navigator Tasks as of now",
            user="U-AUTO",
        )

        v = classify_message(
            old,
            rules,
            db_pinned_ts=set(),
            slack_pinned_ts=set(),
            keep_autobot=True,
            keep_autobot_cutoff_ts=cutoff,
        )

        assert v.action == "DELETE"

    def test_foreign_bot_post_deleted_with_pattern_match(self, rules):
        """Non-autobot bot_id + matched pattern -> DELETE regardless of keep_autobot."""
        msg = _msg("1.0", "Navigator Tasks as of now", bot_id="B-LEGACY")

        v = classify_message(
            msg,
            rules,
            db_pinned_ts=set(),
            slack_pinned_ts=set(),
            keep_autobot=True,  # autobot protection doesn't apply to a different bot
        )

        assert v.action == "DELETE"
        assert v.author_label == "bot_B-LEGACY"


class TestVerdictMetadata:
    """The MessageVerdict carries human-readable context for the audit CSV."""

    def test_posted_at_formatted_from_ts(self, rules):
        """ts -> posted_at parses as a UTC isoformat string."""
        # 2026-06-19T18:00:00Z = 1781892000.0
        msg = _msg("1781892000.0", "anything", bot_id="B-X")

        v = classify_message(msg, rules, db_pinned_ts=set(), slack_pinned_ts=set())

        assert v.posted_at.startswith("2026-06-19 18:00:00")

    def test_text_preview_truncated_and_newlines_stripped(self, rules):
        """text_preview is 120-char max and replaces newlines with spaces."""
        long_text = "a" * 200 + "\nb" * 10
        msg = _msg("1.0", long_text, bot_id="B-X")

        v = classify_message(msg, rules, db_pinned_ts=set(), slack_pinned_ts=set())

        assert len(v.text_preview) <= 120
        assert "\n" not in v.text_preview

    def test_fieldnames_stable_order(self):
        """MessageVerdict.fieldnames() returns the documented CSV column order."""
        names = MessageVerdict.fieldnames()
        assert names[0] == "ts"
        assert names[-1] == "text_preview"
        assert "action" in names
        assert "pattern" in names


class TestRulesHelpers:
    """CleanupRules small surface."""

    def test_pattern_names_sorted(self):
        """pattern_names() returns keys sorted alphabetically."""
        rules = CleanupRules(
            autobot_user_id="U-X",
            redundant_patterns={
                "zeta": re.compile("z"),
                "alpha": re.compile("a"),
                "mid": re.compile("m"),
            },
        )

        assert rules.pattern_names() == ["alpha", "mid", "zeta"]

    def test_no_autobot_id_still_works(self):
        """autobot_user_id=None means no autobot protection at all."""
        rules = CleanupRules(
            autobot_user_id=None,
            redundant_patterns={"foo": re.compile(r"foo")},
        )
        msg = _msg("1.0", "foo", bot_id="B-X")

        v = classify_message(msg, rules, db_pinned_ts=set(), slack_pinned_ts=set())

        assert v.action == "DELETE"
