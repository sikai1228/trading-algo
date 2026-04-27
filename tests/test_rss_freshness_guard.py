"""Phase 4 Part 2.12 — freshness guard pinned tests.

The freshness guard in ``MatcherWorker._classify_and_patch`` skips
the LLM call for articles older than ``STALE_ARTICLE_HOURS``
(currently 48). This file pins:

- The threshold constant value (48).
- ``_article_is_stale`` returns True for ``None``, empty string,
  unparseable input, and timestamps older than the threshold.
- ``_article_is_stale`` returns False for fresh timestamps.
- The constant respects the engine's 24 h window: 48 h provides a
  buffer so an article that *just* squeaks inside the engine's
  window still reaches Stage 2.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trumpbot.daemon import STALE_ARTICLE_HOURS, _article_is_stale


class TestStaleArticleConstant:
    def test_constant_is_48_hours(self) -> None:
        # 48 h provides a buffer over the engine's 24 h window check.
        # If this number changes, update CLAUDE.md and the
        # docs/investigations/pipeline_order_verification.md doc.
        assert STALE_ARTICLE_HOURS == 48

    def test_constant_is_above_engine_window(self) -> None:
        """The freshness guard's threshold must be > the engine's
        article-window check (24 h, see DecisionEngine rule 4).
        Otherwise we'd skip the LLM call for articles the engine
        would have admitted."""
        from trumpbot.decision.engine import _article_within_window

        assert STALE_ARTICLE_HOURS > 24
        # And as a smoke check, _article_within_window is a real
        # callable.
        assert callable(_article_within_window)


class TestArticleIsStale:
    def test_none_is_stale(self) -> None:
        assert _article_is_stale(None, 48) is True

    def test_empty_string_is_stale(self) -> None:
        assert _article_is_stale("", 48) is True

    def test_unparseable_is_stale(self) -> None:
        assert _article_is_stale("not-a-timestamp", 48) is True

    def test_fresh_within_threshold(self) -> None:
        recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        assert _article_is_stale(recent, 48) is False

    def test_at_threshold_boundary(self) -> None:
        # Exactly 47 hours old → fresh.
        recent = (datetime.now(UTC) - timedelta(hours=47)).isoformat()
        assert _article_is_stale(recent, 48) is False
        # Exactly 49 hours old → stale.
        old = (datetime.now(UTC) - timedelta(hours=49)).isoformat()
        assert _article_is_stale(old, 48) is True

    def test_old_article_is_stale(self) -> None:
        # 7 days old.
        old = (datetime.now(UTC) - timedelta(days=7)).isoformat()
        assert _article_is_stale(old, 48) is True

    def test_iso_with_z_suffix(self) -> None:
        recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        assert _article_is_stale(recent, 48) is False

    def test_iso_with_tz_offset(self) -> None:
        recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        assert _article_is_stale(recent, 48) is False

    def test_naive_iso_treated_as_utc(self) -> None:
        # Bare ISO without timezone — we treat it as UTC.
        recent = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        assert _article_is_stale(recent, 48) is False
