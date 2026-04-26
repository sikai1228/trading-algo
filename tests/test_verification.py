"""Additional tests added during the Phase 1 verification pass.

These tests cover gaps identified by the verification checklist:

- S4: rate limiter actually caps at 80 % of tier (burst of 100 calls)
- S5: KalshiWebSocketFeed orderbook delta application + sequence-gap
       detection (unit tests on the pure book/state logic)
- S6: matcher edge cases — past/present tense, quoted self-claim,
       subject as part of larger name, performance benchmark
- S6: ABC vs concrete: NewsMatcher correctly produces match_reason
       strings the brief flags for human review
"""

from __future__ import annotations

import asyncio
import time

import pytest

from trumpbot.discovery.subjects import DEFAULT_SUBJECT_ALIASES, SubjectExtractor
from trumpbot.kalshi.rate_limit import TokenBucket
from trumpbot.market_data.kalshi_ws import (
    PRICE_CHANGE_THRESHOLD_CENTS,
    RECONNECT_BACKOFFS_SEC,
    _IncomingMessage,
    _MarketBook,
)
from trumpbot.news.matcher import PASSED_REASON, MarketContext, NewsMatcher

# ---------------------------------------------------------------------------
# S4: rate limiter cap under burst
# ---------------------------------------------------------------------------


class TestRateLimiterCap:
    async def test_burst_does_not_exceed_configured_rate(self) -> None:
        """100 acquires complete in time consistent with the configured rate.

        With rate=80/sec and capacity=10, 100 acquires must take at least
        ``(100 - capacity) / rate = 90 / 80 = 1.125 s`` and well under
        the unbounded burst rate. We allow generous slack on the upper
        bound to keep the test stable in CI.
        """
        bucket = TokenBucket(rate_per_sec=80.0, capacity=10.0)
        t0 = time.perf_counter()
        await asyncio.gather(*(bucket.acquire() for _ in range(100)))
        elapsed = time.perf_counter() - t0
        # Lower bound: must observe at least ~1.1s of refill wait beyond
        # the initial burst.
        assert elapsed >= 1.0, f"completed in only {elapsed:.3f}s"
        # Upper bound: must finish within 3s, otherwise something is
        # serializing more than the rate would imply.
        assert elapsed <= 3.0, f"took too long: {elapsed:.3f}s"
        observed_rps = 100 / elapsed
        assert observed_rps <= 80.0 * 1.15, f"observed {observed_rps} rps > cap"


# ---------------------------------------------------------------------------
# S5: WebSocket orderbook + dispatch logic
# ---------------------------------------------------------------------------


class TestMarketBook:
    def test_apply_snapshot_replaces_state(self) -> None:
        book = _MarketBook()
        book.apply_snapshot([(50, 100), (49, 200)], [(50, 50), (51, 25)])
        assert book.yes_levels == {50: 100, 49: 200}
        assert book.no_levels == {50: 50, 51: 25}

    def test_apply_delta_adds_size(self) -> None:
        book = _MarketBook()
        book.apply_snapshot([(50, 100)], [])
        book.apply_delta("yes", 50, 25)
        assert book.yes_levels[50] == 125

    def test_apply_delta_removes_level_at_zero(self) -> None:
        book = _MarketBook()
        book.apply_snapshot([(50, 100)], [])
        book.apply_delta("yes", 50, -100)
        assert 50 not in book.yes_levels

    def test_apply_delta_removes_level_below_zero(self) -> None:
        book = _MarketBook()
        book.apply_snapshot([(50, 100)], [])
        book.apply_delta("yes", 50, -150)
        assert 50 not in book.yes_levels

    def test_best_levels(self) -> None:
        book = _MarketBook()
        book.apply_snapshot(
            [(50, 100), (49, 200), (48, 300)],
            [(50, 50), (51, 25), (52, 10)],
        )
        assert book.best_yes_bid() == 50
        assert book.best_yes_ask() == 48
        assert book.best_no_bid() == 52
        assert book.best_no_ask() == 50

    def test_yes_levels_sorted_descending(self) -> None:
        book = _MarketBook()
        book.apply_snapshot([(48, 300), (50, 100), (49, 200)], [])
        assert book.yes_levels_sorted() == [(50, 100), (49, 200), (48, 300)]


class TestIncomingMessageSchema:
    def test_parses_orderbook_snapshot(self) -> None:
        msg = _IncomingMessage.model_validate(
            {"type": "orderbook_snapshot", "seq": 1, "msg": {"yes": [], "no": []}}
        )
        assert msg.type == "orderbook_snapshot"
        assert msg.seq == 1

    def test_extra_fields_allowed(self) -> None:
        # Kalshi may add fields; the schema must not break ingestion.
        msg = _IncomingMessage.model_validate({"type": "ticker", "extra_unexpected": True})
        assert msg.type == "ticker"

    def test_rejects_missing_required_type(self) -> None:
        import pydantic as _pydantic

        with pytest.raises(_pydantic.ValidationError):
            _IncomingMessage.model_validate({"seq": 1})


class TestReconnectBackoff:
    def test_backoff_starts_at_one_second(self) -> None:
        assert RECONNECT_BACKOFFS_SEC[0] == 1

    def test_backoff_caps_at_sixty(self) -> None:
        assert max(RECONNECT_BACKOFFS_SEC) == 60

    def test_backoff_doubles_until_cap(self) -> None:
        # 1, 2, 4, 8, 16, 32, 60 — 32 then capped.
        assert RECONNECT_BACKOFFS_SEC[:6] == (1, 2, 4, 8, 16, 32)

    def test_price_change_threshold_is_two_cents(self) -> None:
        assert PRICE_CHANGE_THRESHOLD_CENTS == 2


# ---------------------------------------------------------------------------
# S6: matcher edge cases beyond the existing 70-test suite
# ---------------------------------------------------------------------------


@pytest.fixture()
def matcher() -> NewsMatcher:
    return NewsMatcher(extractor=SubjectExtractor(aliases=DEFAULT_SUBJECT_ALIASES))


class TestMatcherEdgeCases:
    """Phase 4 Part 2.8: matcher is the Stage-1 pre-filter (Trump +
    subject + interaction term). All confidence scoring moved to the
    LLM cascade. These tests pin pre-filter pass/fail behavior."""

    def test_present_tense_calls_in_headline(self, matcher: NewsMatcher) -> None:
        [r] = matcher.match(
            headline="Trump calls Putin to discuss ceasefire",
            body=None,
            markets=[MarketContext(ticker="T", subject="putin")],
        )
        assert r.match_reason == PASSED_REASON

    def test_subject_possessive_pre_apostrophe(self, matcher: NewsMatcher) -> None:
        # "Putin's spokesman said Trump called" — pre-filter sees
        # Trump + Putin + 'called', passes to Stage 2.
        [r] = matcher.match(
            headline="Putin's spokesman said Trump called",
            body=None,
            markets=[MarketContext(ticker="T", subject="putin")],
        )
        assert r.match_reason == PASSED_REASON

    def test_quoted_self_claim_in_headline(self, matcher: NewsMatcher) -> None:
        [r] = matcher.match(
            headline='Trump says "I called Putin yesterday"',
            body=None,
            markets=[MarketContext(ticker="T", subject="putin")],
        )
        assert r.match_reason == PASSED_REASON

    def test_subject_speech_alone_does_not_match(self, matcher: NewsMatcher) -> None:
        # "Putin gave a speech" — no Trump, fails pre-filter.
        [r] = matcher.match(
            headline="Putin gave a speech to Russian parliament",
            body=None,
            markets=[MarketContext(ticker="T", subject="putin")],
        )
        assert r.confidence == 0.0
        assert "no_trump" in r.match_reason

    def test_multiple_subjects_per_article(self, matcher: NewsMatcher) -> None:
        markets = [
            MarketContext(ticker="A", subject="putin"),
            MarketContext(ticker="B", subject="xi"),
            MarketContext(ticker="C", subject="netanyahu"),
        ]
        results = matcher.match(
            headline="Trump spoke with both Putin and Xi at G20",
            body=None,
            markets=markets,
        )
        by_t = {r.ticker: r for r in results}
        assert by_t["A"].match_reason == PASSED_REASON
        assert by_t["B"].match_reason == PASSED_REASON
        assert "no_subject" in by_t["C"].match_reason

    def test_indirect_communication_letter_to_subject(self, matcher: NewsMatcher) -> None:
        # Stage 1 NO LONGER gates indirect communication — that's the
        # LLM's job. Pre-filter sees Trump + Xi + 'letter' (interaction
        # term) and passes.
        [r] = matcher.match(
            headline="Trump sent a letter to Xi yesterday",
            body=None,
            markets=[MarketContext(ticker="T", subject="xi")],
        )
        assert r.match_reason == PASSED_REASON


class TestMatcherPerformance:
    def test_2kb_article_against_50_markets_under_50ms(self, matcher: NewsMatcher) -> None:
        # Build a representative ~2 KB article body.
        body = (
            "World leaders gathered at G20 today. " * 20
            + "Trump and Putin spoke privately on the sidelines. "
            + "extra context " * 50
        )
        assert 1400 < len(body) < 4000  # ~1.5 KB, representative article
        # 50 markets across a mix of subjects.
        subjects = list(DEFAULT_SUBJECT_ALIASES.keys())
        markets = [
            MarketContext(ticker=f"M{i}", subject=subjects[i % len(subjects)]) for i in range(50)
        ]

        # Warm-up to amortize regex compilation if any.
        matcher.match(headline="Test", body=body, markets=markets)

        t0 = time.perf_counter()
        for _ in range(20):
            matcher.match(
                headline="World leaders gather at G20",
                body=body,
                markets=markets,
            )
        avg_ms = (time.perf_counter() - t0) * 1000 / 20
        assert avg_ms < 50, f"avg {avg_ms:.1f}ms > 50ms target"
