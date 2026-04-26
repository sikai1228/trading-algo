"""NewsMatcher unit tests — the spec.

Phase 4 Part 2.8 simplified the matcher to a single 3-condition
pre-filter (Trump + subject alias + interaction term). All
positive-confidence behavior moved to the LLM classifier (Stage 2).

These tests pin the pre-filter:

- A passing article emits ``confidence=0.0`` with
  ``match_reason="passed_pre_filter"`` and the three matched keywords
  in ``matched_keywords``.
- A failing article emits ``confidence=0.0`` with
  ``match_reason="failed_pre_filter:<which>"`` listing the missing
  conditions.
- Word-boundary safety — short aliases (e.g. "xi", "lai", "orban")
  must not match inside other words ("axis", "lais", "orbanism").
"""

from __future__ import annotations

import pytest

from trumpbot.discovery.subjects import DEFAULT_SUBJECT_ALIASES, SubjectExtractor
from trumpbot.news.matcher import (
    PASSED_REASON,
    MarketContext,
    MatchResult,
    NewsMatcher,
)


@pytest.fixture()
def matcher() -> NewsMatcher:
    return NewsMatcher(extractor=SubjectExtractor(aliases=DEFAULT_SUBJECT_ALIASES))


def _one(
    matcher: NewsMatcher,
    headline: str,
    body: str | None = None,
    subject: str = "putin",
    ticker: str = "T",
) -> MatchResult:
    ctx = MarketContext(ticker=ticker, subject=subject)
    [r] = matcher.match(headline=headline, body=body, markets=[ctx])
    return r


# ---------------------------------------------------------------------------
# Pass: all three pre-filter conditions present
# ---------------------------------------------------------------------------


class TestPassedPreFilter:
    def test_classic_positive_headline(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump spoke with Putin about Ukraine")
        assert r.confidence == 0.0
        assert r.match_reason == PASSED_REASON
        assert "trump" in r.matched_keywords
        assert "putin" in r.matched_keywords
        assert "spoke" in r.matched_keywords

    def test_phone_call_passes(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump and Putin had a phone call")
        assert r.match_reason == PASSED_REASON

    def test_called_passes(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump called Putin Tuesday morning")
        assert r.match_reason == PASSED_REASON

    def test_summit_passes(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump-Putin summit underway in Vienna")
        assert r.match_reason == PASSED_REASON

    def test_held_talks_passes(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump and Putin held talks at G20")
        assert r.match_reason == PASSED_REASON

    def test_met_passes(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump met with Putin face to face")
        assert r.match_reason == PASSED_REASON

    def test_video_call_passes(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump and Putin held a video call today")
        assert r.match_reason == PASSED_REASON

    def test_dined_passes(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump dined with Putin at the embassy")
        assert r.match_reason == PASSED_REASON

    def test_subject_in_body_passes(self, matcher: NewsMatcher) -> None:
        r = _one(
            matcher,
            "Trump golfs at Mar-a-Lago",
            body="During the round he spoke with Putin briefly by phone.",
        )
        assert r.match_reason == PASSED_REASON

    def test_potus_alias_for_trump(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "POTUS spoke with Putin earlier today")
        assert r.match_reason == PASSED_REASON


# ---------------------------------------------------------------------------
# Fail: at least one condition missing
# ---------------------------------------------------------------------------


class TestFailedPreFilter:
    def test_no_trump_mention(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Putin gave a speech to Russian parliament")
        assert r.confidence == 0.0
        assert r.match_reason.startswith("failed_pre_filter:")
        assert "no_trump" in r.match_reason

    def test_no_subject_mention(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump spoke at the rally about taxes")
        assert "no_subject" in r.match_reason

    def test_no_interaction_term(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump and Putin appear in same Vogue spread")
        # Trump + Putin both present, but no interaction verb.
        assert "no_interaction_term" in r.match_reason

    def test_unknown_subject(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump met with someone", subject="not-a-real-subject")
        assert "unknown_subject" in r.match_reason

    def test_three_failures_listed_together(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Wholly unrelated weather report")
        # All three conditions missing.
        for token in ("no_trump", "no_subject", "no_interaction_term"):
            assert token in r.match_reason


# ---------------------------------------------------------------------------
# Stage-1 used to gate negation / future / indirect; the LLM does that now.
# These are the regression cases from the Reuters investigation: Stage 1
# now PASSES them so the LLM gets a chance to reject them.
# ---------------------------------------------------------------------------


class TestStage1NoLongerGatesPrecision:
    def test_negation_passes_to_stage_2(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump did not call Putin this week")
        assert r.match_reason == PASSED_REASON

    def test_future_tense_passes_to_stage_2(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump will meet with Putin next week")
        assert r.match_reason == PASSED_REASON

    def test_habitual_passes_to_stage_2(self, matcher: NewsMatcher) -> None:
        # The Reuters article that motivated this whole change.
        r = _one(matcher, "Trump says he speaks with Putin: Fox News")
        assert r.match_reason == PASSED_REASON

    def test_indirect_communication_passes_to_stage_2(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump sent a letter to Putin yesterday")
        assert r.match_reason == PASSED_REASON


# ---------------------------------------------------------------------------
# Subject alias variations
# ---------------------------------------------------------------------------


class TestSubjectAliases:
    def test_xi_jinping_full_name(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump and Xi Jinping spoke today", subject="xi")
        assert r.match_reason == PASSED_REASON

    def test_chinese_president_alias(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump phoned the Chinese President this morning", subject="xi")
        assert r.match_reason == PASSED_REASON

    def test_bibi_alias_for_netanyahu(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump and Bibi held a meeting", subject="netanyahu")
        assert r.match_reason == PASSED_REASON

    def test_zelenskyy_double_y(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump and Zelenskyy held talks", subject="zelensky")
        assert r.match_reason == PASSED_REASON


# ---------------------------------------------------------------------------
# Word-boundary safety (the matcher MUST NOT match inside other words)
# ---------------------------------------------------------------------------


class TestWordBoundarySafety:
    def test_lai_does_not_match_lais(self, matcher: NewsMatcher) -> None:
        r = _one(
            matcher,
            "Trump spoke at the lais convention center",
            subject="lai",
        )
        assert "no_subject" in r.match_reason

    def test_xi_inside_axis(self, matcher: NewsMatcher) -> None:
        r = _one(
            matcher,
            "Trump appears in axis-of-evil-themed sketch",
            subject="xi",
        )
        # 'xi' must not match inside 'axis'; with no Putin alias either,
        # this fails on no_subject.
        assert "no_subject" in r.match_reason

    def test_orban_does_not_match_orbanism(self, matcher: NewsMatcher) -> None:
        r = _one(
            matcher,
            "Trump comments on the orbanism political movement",
            subject="orban",
        )
        assert "no_subject" in r.match_reason


# ---------------------------------------------------------------------------
# Multi-market dispatch
# ---------------------------------------------------------------------------


class TestMultiMarket:
    def test_one_per_market(self, matcher: NewsMatcher) -> None:
        markets = [
            MarketContext(ticker="T1", subject="putin"),
            MarketContext(ticker="T2", subject="xi"),
            MarketContext(ticker="T3", subject="netanyahu"),
        ]
        results = matcher.match(
            headline="Trump spoke with Putin",
            body=None,
            markets=markets,
        )
        assert {r.ticker for r in results} == {"T1", "T2", "T3"}
        by_t = {r.ticker: r for r in results}
        assert by_t["T1"].match_reason == PASSED_REASON
        # Putin-only article — Xi and Netanyahu rows fail no_subject.
        assert "no_subject" in by_t["T2"].match_reason
        assert "no_subject" in by_t["T3"].match_reason

    def test_multiple_subjects_in_same_article(self, matcher: NewsMatcher) -> None:
        markets = [
            MarketContext(ticker="T1", subject="putin"),
            MarketContext(ticker="T2", subject="xi"),
        ]
        results = matcher.match(
            headline="Trump spoke with both Putin and Xi",
            body=None,
            markets=markets,
        )
        for r in results:
            assert r.match_reason == PASSED_REASON


# ---------------------------------------------------------------------------
# article_published_ts is accepted but ignored — back-compat with daemon
# ---------------------------------------------------------------------------


class TestArticleTimestampIgnored:
    def test_window_check_no_longer_in_matcher(self, matcher: NewsMatcher) -> None:
        # The article-window check moved to DecisionEngine. Stage 1
        # passes regardless of the timestamp.
        ctx = MarketContext(
            ticker="T",
            subject="putin",
            open_ts="2026-01-01T00:00:00Z",
            close_ts="2026-12-31T23:59:59Z",
        )
        [r] = matcher.match(
            headline="Trump spoke with Putin",
            body=None,
            markets=[ctx],
            article_published_ts="2025-12-01T00:00:00Z",  # before window
        )
        assert r.match_reason == PASSED_REASON
