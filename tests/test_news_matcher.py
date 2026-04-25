"""NewsMatcher unit tests — the spec.

Per the Phase 1 brief these tests cover true positives, true negatives,
negation, future tense, indirect communication, alias variations,
partial-name matches, and ambiguous verbs. When matcher behavior
changes, these tests change with it.
"""

from __future__ import annotations

import pytest

from trumpbot.discovery.subjects import DEFAULT_SUBJECT_ALIASES, SubjectExtractor
from trumpbot.news.matcher import MarketContext, MatchResult, NewsMatcher


@pytest.fixture()
def matcher() -> NewsMatcher:
    return NewsMatcher(extractor=SubjectExtractor(aliases=DEFAULT_SUBJECT_ALIASES))


def _ctx(subject: str = "putin", ticker: str = "T") -> MarketContext:
    return MarketContext(ticker=ticker, subject=subject)


def _one(
    matcher: NewsMatcher,
    headline: str,
    body: str | None = None,
    subject: str = "putin",
    ticker: str = "T",
    article_published_ts: str | None = None,
    open_ts: str | None = None,
    close_ts: str | None = None,
) -> MatchResult:
    ctx = MarketContext(ticker=ticker, subject=subject, open_ts=open_ts, close_ts=close_ts)
    [r] = matcher.match(
        headline=headline,
        body=body,
        markets=[ctx],
        article_published_ts=article_published_ts,
    )
    return r


# ---------------------------------------------------------------------------
# Positive: direct verb in headline
# ---------------------------------------------------------------------------


class TestDirectVerbHighConfidence:
    def test_spoke_in_headline(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump spoke with Putin about Ukraine")
        assert r.confidence == 1.0
        assert "putin" in (r.matched_keywords or [])

    def test_phone_call_in_headline(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump and Putin had a phone call")
        assert r.confidence == 1.0

    def test_called_in_headline(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump called Putin Tuesday morning")
        assert r.confidence == 1.0

    def test_summit_in_headline(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump-Putin summit underway in Vienna")
        assert r.confidence == 1.0

    def test_held_talks_in_headline(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump and Putin held talks at G20")
        assert r.confidence == 1.0

    def test_met_in_headline(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump met with Putin face to face")
        assert r.confidence == 1.0

    def test_video_call_in_headline(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump and Putin held a video call today")
        assert r.confidence == 1.0

    def test_readout_in_headline(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "White House releases Trump-Putin readout")
        assert r.confidence == 1.0

    def test_bilateral_in_headline(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump-Putin bilateral on sidelines of UNGA")
        assert r.confidence == 1.0

    def test_direct_in_body_not_headline(self, matcher: NewsMatcher) -> None:
        r = _one(
            matcher,
            "World leaders convene in Vienna",
            body="Trump and Putin spoke privately on the sidelines today.",
        )
        assert r.confidence == 0.8
        assert "body" in r.match_reason


# ---------------------------------------------------------------------------
# Subject alias variations
# ---------------------------------------------------------------------------


class TestSubjectAliases:
    def test_xi_jinping_full_name(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump and Xi Jinping spoke today", subject="xi")
        assert r.confidence == 1.0

    def test_chinese_president_alias(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump phoned the Chinese President this morning", subject="xi")
        assert r.confidence == 1.0

    def test_bibi_alias_for_netanyahu(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump and Bibi held a meeting", subject="netanyahu")
        assert r.confidence == 1.0

    def test_kim_jong_un_with_dash(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump met with Kim Jong-un at the DMZ", subject="kim jong un")
        assert r.confidence == 1.0

    def test_uk_pm_alias(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump phoned the UK PM today", subject="starmer")
        assert r.confidence == 1.0

    def test_zelenskyy_double_y(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump and Zelenskyy held talks", subject="zelensky")
        assert r.confidence == 1.0

    def test_subject_only_in_body_alias(self, matcher: NewsMatcher) -> None:
        r = _one(
            matcher,
            "Trump speaks at NATO summit",
            body="In private remarks, Trump spoke with the French President for an hour.",
            subject="macron",
        )
        assert r.confidence == 0.8


# ---------------------------------------------------------------------------
# Negation defeats a match
# ---------------------------------------------------------------------------


class TestNegation:
    def test_did_not_call(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump did not call Putin this week")
        assert r.confidence == 0.0
        assert "negation" in r.match_reason

    def test_didnt_speak(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump didn't speak with Putin at the summit")
        assert r.confidence == 0.0

    def test_no_call_between(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "No call between Trump and Putin this month", subject="putin")
        assert r.confidence == 0.0

    def test_declined_to_speak(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump declined to speak with Putin")
        assert r.confidence == 0.0

    def test_refused_to_meet(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump refused to meet with Putin in Helsinki")
        assert r.confidence == 0.0

    def test_denied_calling(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump denied calling Putin yesterday")
        assert r.confidence == 0.0

    def test_no_plans_to_call(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump has no plans to call Putin")
        assert r.confidence == 0.0

    def test_ruled_out_meeting(self, matcher: NewsMatcher) -> None:
        r = _one(
            matcher,
            "Trump ruled out a meeting with Putin",
            body="The administration confirmed no plans to meet Putin.",
        )
        assert r.confidence == 0.0

    def test_has_not_spoken(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump has not spoken with Putin since taking office")
        assert r.confidence == 0.0

    def test_negation_far_from_subject_does_not_trigger(self, matcher: NewsMatcher) -> None:
        # negation is far away in the text from the subject mention; should
        # NOT defeat the match.
        body = "Trump spoke with Putin today. " + "x " * 200 + "He did not call Xi this week."
        r = _one(matcher, "Trump and Putin spoke", body=body)
        assert r.confidence == 1.0


# ---------------------------------------------------------------------------
# Future tense / scheduled
# ---------------------------------------------------------------------------


class TestFutureTense:
    def test_will_meet(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump will meet with Putin next week")
        assert r.confidence == 0.2
        assert "future_tense" in r.match_reason

    def test_expected_to_call(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump expected to call Putin Wednesday")
        assert r.confidence == 0.2

    def test_scheduled_a_call(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump scheduled a call with Putin for Friday")
        assert r.confidence == 0.2

    def test_plans_to_speak(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump plans to speak with Putin this week")
        assert r.confidence == 0.2

    def test_may_call(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump may call Putin to discuss Ukraine")
        assert r.confidence == 0.2

    def test_could_meet(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump could meet Putin at G20")
        assert r.confidence == 0.2

    def test_set_to_meet(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump set to meet Putin in October")
        assert r.confidence == 0.2

    def test_agreed_to_call(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump and Putin agreed to call next month")
        assert r.confidence == 0.2

    def test_future_loses_to_direct(self, matcher: NewsMatcher) -> None:
        r = _one(
            matcher,
            "Trump and Putin spoke today; agreed to meet again",
        )
        # direct verb wins over future-tense planning
        assert r.confidence == 1.0


# ---------------------------------------------------------------------------
# Indirect communication
# ---------------------------------------------------------------------------


class TestIndirectCommunication:
    def test_sent_a_letter(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump sent a letter to Putin yesterday")
        assert r.confidence == 0.5
        assert "indirect_communication" in r.match_reason

    def test_via_envoy(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump messaged Putin via envoy", body="The envoy delivered the message.")
        assert r.confidence == 0.5

    def test_through_intermediary(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump reached Putin through an intermediary")
        assert r.confidence == 0.5

    def test_exchanged_messages(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump and Putin exchanged messages today")
        assert r.confidence == 0.5

    def test_passed_a_message(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump passed a message to Putin via Lavrov")
        assert r.confidence == 0.5

    def test_indirect_overrides_direct_when_both_present(self, matcher: NewsMatcher) -> None:
        # If "sent a letter" appears near subject, treat as indirect even
        # if a direct verb fires elsewhere — the more specific signal wins.
        r = _one(
            matcher,
            "Trump sent a letter to Putin with talks expected later",
        )
        assert r.confidence == 0.5
        assert "indirect" in r.match_reason


# ---------------------------------------------------------------------------
# Mention verbs
# ---------------------------------------------------------------------------


class TestMentionVerbs:
    def test_mentioned(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump mentioned Putin in his speech today")
        assert r.confidence == 0.5
        assert "mention_verb" in r.match_reason

    def test_addressed(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump addressed Putin's recent statement")
        assert r.confidence == 0.5

    def test_referenced(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump referenced Putin during the rally")
        assert r.confidence == 0.5

    def test_criticized(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump criticized Putin's nuclear posture")
        assert r.confidence == 0.5

    def test_praised(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump praised Putin's negotiation style")
        assert r.confidence == 0.5

    def test_mention_loses_to_direct(self, matcher: NewsMatcher) -> None:
        r = _one(
            matcher,
            "Trump mentioned and then spoke with Putin afterwards",
        )
        assert r.confidence == 1.0


# ---------------------------------------------------------------------------
# Required preconditions
# ---------------------------------------------------------------------------


class TestPreconditions:
    def test_no_subject_mention(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump golfs at Mar-a-Lago")
        assert r.confidence == 0.0
        assert r.match_reason == "no_subject_mention"

    def test_no_trump_mention(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Putin addresses Russian parliament")
        assert r.confidence == 0.0
        assert r.match_reason == "no_trump_mention"

    def test_unknown_subject(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump met with someone", subject="not-a-real-subject")
        assert r.confidence == 0.0
        assert "unknown_subject" in r.match_reason

    def test_subject_and_trump_no_relevant_verb(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "Trump and Putin appear in same documentary")
        assert r.confidence == 0.0
        assert "no_relevant_verb" in r.match_reason

    def test_potus_alias_for_trump(self, matcher: NewsMatcher) -> None:
        r = _one(matcher, "POTUS spoke with Putin earlier today")
        assert r.confidence == 1.0


# ---------------------------------------------------------------------------
# Article window
# ---------------------------------------------------------------------------


class TestArticleWindow:
    def test_before_market_open(self, matcher: NewsMatcher) -> None:
        r = _one(
            matcher,
            "Trump spoke with Putin",
            article_published_ts="2025-12-01T00:00:00Z",
            open_ts="2026-01-01T00:00:00Z",
            close_ts="2026-12-31T23:59:59Z",
        )
        assert r.confidence == 0.0
        assert r.match_reason == "out_of_window"

    def test_after_market_close(self, matcher: NewsMatcher) -> None:
        r = _one(
            matcher,
            "Trump spoke with Putin",
            article_published_ts="2027-02-01T00:00:00Z",
            open_ts="2026-01-01T00:00:00Z",
            close_ts="2026-12-31T23:59:59Z",
        )
        assert r.confidence == 0.0
        assert r.match_reason == "out_of_window"

    def test_within_window(self, matcher: NewsMatcher) -> None:
        r = _one(
            matcher,
            "Trump spoke with Putin",
            article_published_ts="2026-04-25T12:00:00Z",
            open_ts="2026-01-01T00:00:00Z",
            close_ts="2026-12-31T23:59:59Z",
        )
        assert r.confidence == 1.0

    def test_window_only_open_ts(self, matcher: NewsMatcher) -> None:
        r = _one(
            matcher,
            "Trump spoke with Putin",
            article_published_ts="2026-04-25T12:00:00Z",
            open_ts="2026-01-01T00:00:00Z",
        )
        assert r.confidence == 1.0


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
        results = matcher.match(headline="Trump spoke with Putin", body=None, markets=markets)
        assert {r.ticker for r in results} == {"T1", "T2", "T3"}
        by_t = {r.ticker: r for r in results}
        assert by_t["T1"].confidence == 1.0
        assert by_t["T2"].confidence == 0.0
        assert by_t["T3"].confidence == 0.0

    def test_multiple_subjects_in_same_article(self, matcher: NewsMatcher) -> None:
        markets = [
            MarketContext(ticker="T1", subject="putin"),
            MarketContext(ticker="T2", subject="xi"),
        ]
        results = matcher.match(
            headline="Trump spoke with both Putin and Xi", body=None, markets=markets
        )
        for r in results:
            assert r.confidence == 1.0


# ---------------------------------------------------------------------------
# Partial-name false positives we want to AVOID
# ---------------------------------------------------------------------------


class TestPartialNameSafety:
    def test_lai_does_not_false_match_on_lais(self, matcher: NewsMatcher) -> None:
        # "lais" should not satisfy the "lai" alias due to whole-word matching.
        r = _one(
            matcher,
            "Trump spoke at the lais convention center",
            subject="lai",
        )
        assert r.confidence == 0.0

    def test_xi_inside_word_does_not_match(self, matcher: NewsMatcher) -> None:
        r = _one(
            matcher,
            "Trump appears in axis-of-evil-themed sketch",
            subject="xi",
        )
        # "xi" should not match inside "axis"
        assert r.confidence == 0.0

    def test_orban_does_not_match_inside_orbans(self, matcher: NewsMatcher) -> None:
        r = _one(
            matcher,
            "Trump comments on the orbanism political movement",
            subject="orban",
        )
        assert r.confidence == 0.0


# ---------------------------------------------------------------------------
# Body window (>500 chars)
# ---------------------------------------------------------------------------


class TestBodyWindow:
    def test_subject_beyond_first_500_chars(self, matcher: NewsMatcher) -> None:
        body = "filler " * 100 + "Trump spoke with Putin in private."
        r = _one(matcher, "World leaders gather in Davos", body=body)
        # Subject beyond the first 500 chars => no_subject_mention
        assert r.confidence == 0.0
        assert "no_subject_mention" in r.match_reason

    def test_subject_in_first_500_chars(self, matcher: NewsMatcher) -> None:
        body = "Trump spoke with Putin in private. " + "extra context " * 100
        r = _one(matcher, "World leaders gather in Davos", body=body)
        assert r.confidence == 0.8
