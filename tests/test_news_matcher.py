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
    TRUMP_AUTHOR_KEYWORD,
    TRUMP_AUTHOR_SOURCES,
    MarketContext,
    MatchResult,
    NewsMatcher,
    _is_trump_author,
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


# ---------------------------------------------------------------------------
# PR #32 — Trump-as-author rule for Truth Social posts
#
# Trump's Truth Social posts are first-person. They typically don't
# refer to "Trump" by name (he's writing them), so requiring the
# literal Trump alias in the body would silently reject every
# meeting/call announcement he posts. The matcher counts the source
# string as the implicit Trump-mention when it matches one of
# TRUMP_AUTHOR_SOURCES.
# ---------------------------------------------------------------------------


class TestTrumpAsAuthor:
    """Pin the Trump-as-author rule for Truth Social posts."""

    def test_truth_social_author_substitutes_for_body_trump(self, matcher: NewsMatcher) -> None:
        """The audit's canonical synthetic test: Trump posts about a
        phone call with Putin. Body has subject + verb but no "Trump"."""
        ctx = MarketContext(ticker="T", subject="putin")
        body = (
            "Just got off the phone with my friend Vladimir Putin. "
            "We had a great conversation about ending the war in Ukraine. "
            "Many things discussed!"
        )
        [r] = matcher.match(
            headline="(no text)",
            body=body,
            markets=[ctx],
            source="truth_social:@realDonaldTrump",
        )
        assert r.match_reason == PASSED_REASON
        # The author-implicit keyword is recorded for audit.
        assert TRUMP_AUTHOR_KEYWORD in r.matched_keywords
        assert "putin" in r.matched_keywords or "vladimir putin" in r.matched_keywords
        assert "phone" in r.matched_keywords

    def test_truth_social_without_subject_still_fails(self, matcher: NewsMatcher) -> None:
        """Author-implicit doesn't waive the subject requirement.
        A Truth Social rant about taxes still fails Stage 1."""
        ctx = MarketContext(ticker="T", subject="putin")
        body = "Today I will issue an Executive Order to lower taxes for all Americans."
        [r] = matcher.match(
            headline="(no text)",
            body=body,
            markets=[ctx],
            source="truth_social:@realDonaldTrump",
        )
        assert r.match_reason.startswith("failed_pre_filter:")
        assert "no_subject" in r.match_reason

    def test_truth_social_without_interaction_still_fails(self, matcher: NewsMatcher) -> None:
        """Author-implicit doesn't waive the interaction-term
        requirement. Mirrors the audit's real-world Hakeem case
        (Trump rant about a person, no meeting/call verb) using
        Putin since the default fixture aliases include Putin."""
        ctx = MarketContext(ticker="T", subject="putin")
        body = "Putin is weak and overrated. Russia is losing the war. " "America First, always."
        [r] = matcher.match(
            headline="(no text)",
            body=body,
            markets=[ctx],
            source="truth_social:@realDonaldTrump",
        )
        assert r.match_reason.startswith("failed_pre_filter:")
        assert "no_interaction_term" in r.match_reason

    def test_truth_social_with_explicit_trump_in_body_unchanged(self, matcher: NewsMatcher) -> None:
        """When Trump DOES say "Trump" in his post (rare but possible
        — e.g. quoting press), the literal-match path takes priority.
        TRUMP_AUTHOR_KEYWORD does not appear in matched_keywords."""
        ctx = MarketContext(ticker="T", subject="putin")
        body = "Trump met with Putin today, says the press."
        [r] = matcher.match(
            headline="(no text)",
            body=body,
            markets=[ctx],
            source="truth_social:@realDonaldTrump",
        )
        assert r.match_reason == PASSED_REASON
        assert "trump" in r.matched_keywords
        assert TRUMP_AUTHOR_KEYWORD not in r.matched_keywords

    def test_non_truth_social_source_still_requires_literal_trump(
        self, matcher: NewsMatcher
    ) -> None:
        """The author-implicit rule does not extend to other sources.
        A Reuters article with subject + verb but no "Trump" still
        fails Stage 1 — Reuters isn't Trump-authored content."""
        ctx = MarketContext(ticker="T", subject="putin")
        [r] = matcher.match(
            headline="Putin met with Lavrov in Moscow",
            body="The two discussed Ukraine.",
            markets=[ctx],
            source="reuters",
        )
        assert r.match_reason.startswith("failed_pre_filter:")
        assert "no_trump" in r.match_reason

    def test_no_source_argument_falls_back_to_literal_only(self, matcher: NewsMatcher) -> None:
        """Back-compat: callers that don't pass source get the
        pre-PR-#32 behavior (literal-match only). Pinned so the
        default doesn't accidentally become 'always implicit Trump'."""
        ctx = MarketContext(ticker="T", subject="putin")
        [r] = matcher.match(
            headline="Putin met with Lavrov",
            body="They spoke about Ukraine.",
            markets=[ctx],
        )
        assert r.match_reason.startswith("failed_pre_filter:")
        assert "no_trump" in r.match_reason

    def test_truth_social_with_explicit_trump_no_subject_no_verb(
        self, matcher: NewsMatcher
    ) -> None:
        """Sanity: a Truth Social post that fails on subject AND verb
        gets a clean 'no_subject+no_interaction_term' reason. The
        author-implicit rule satisfies condition A only."""
        ctx = MarketContext(ticker="T", subject="putin")
        [r] = matcher.match(
            headline="(no text)",
            body="Today is a great day for America!",
            markets=[ctx],
            source="truth_social:@realDonaldTrump",
        )
        assert r.match_reason == "failed_pre_filter:no_subject+no_interaction_term"


class TestIsTrumpAuthorHelper:
    """Pin the source-prefix helper directly."""

    def test_truth_social_realdonaldtrump_is_author(self) -> None:
        assert _is_trump_author("truth_social:@realDonaldTrump") is True

    def test_truth_social_other_handle_is_not(self) -> None:
        assert _is_trump_author("truth_social:@SomeoneElse") is False

    def test_truth_social_prefix_only_is_not(self) -> None:
        assert _is_trump_author("truth_social") is False

    def test_other_sources_are_not(self) -> None:
        assert _is_trump_author("reuters") is False
        assert _is_trump_author("bloomberg") is False
        assert _is_trump_author("twitter:@WhiteHouse") is False
        assert _is_trump_author("twitter:@realDonaldTrump") is False  # X is gone

    def test_constant_contains_truth_social_handle(self) -> None:
        # If you add a new Trump-authored source, append to
        # TRUMP_AUTHOR_SOURCES; this assertion guards the canonical entry.
        assert "truth_social:@realDonaldTrump" in TRUMP_AUTHOR_SOURCES
