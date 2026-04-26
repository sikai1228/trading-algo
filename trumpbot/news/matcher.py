"""NewsMatcher: keyword-based mapping from articles to active markets.

This module implements the v1 matcher per CLAUDE.md and the Phase 1
spec. It is *deliberately conservative*:

- Negation explicitly defeats a match.
- Future tense / scheduled language scores 0.2 (not a confirmation).
- Indirect communication (letter, envoy) scores at most 0.5 with a
  ``match_reason`` flagging the indirection.
- "Mention" verbs (mentioned, addressed, referenced) score 0.5.
- "Direct" verbs (spoke, called, met, summit, bilateral, readout)
  with subject + Trump in headline score 1.0.

The interface is pure: takes (headline, body, market_context) and
returns ``MatchResult``. No I/O, fully unit-testable. The 50+ tests
are the spec — when behavior changes, the tests change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from trumpbot.discovery.subjects import SubjectExtractor
from trumpbot.utils.timeutil import parse_iso

# Words that MUST appear (case-insensitive) for the article to even be
# considered as Trump-related.
TRUMP_ALIASES: Final[tuple[str, ...]] = (
    "trump",
    "president trump",
    "donald trump",
    "the president",
    "potus",
    "@potus",
    "@realdonaldtrump",
)

# Verbs that indicate a *direct* conversation took place (past tense).
DIRECT_VERBS: Final[tuple[str, ...]] = (
    "spoke",
    "spoke with",
    "spoke to",
    "talked",
    "talked with",
    "talked to",
    "met",
    "met with",
    "meeting with",
    "phone call",
    "phone-call",
    "called",
    "calls",
    "phoned",
    "discussed",
    "conversation",
    "talks",
    "summit",
    "bilateral",
    "readout",
    "conferred",
    "consulted",
    "consulted with",
    "had a call",
    "held a call",
    "held talks",
    "held a meeting",
    "sit down",
    "sit-down",
    "sat down",
    "sat-down",
    "video call",
    "video-call",
    "videoconference",
    "video conference",
)

# Verbs that indicate a *mention* but not necessarily a conversation.
MENTION_VERBS: Final[tuple[str, ...]] = (
    "mentioned",
    "addressed",
    "referenced",
    "named",
    "praised",
    "criticized",
    "attacked",
    "responded to",
    "reacted to",
)

# Indicators of indirect communication (does NOT qualify as "talked to").
INDIRECT_VERBS: Final[tuple[str, ...]] = (
    "sent a letter",
    "wrote a letter",
    "sent letter",
    "sent an envoy",
    "sent envoy",
    "exchanged messages",
    "exchanged letters",
    "communicated via",
    "communicated through",
    "reached out via",
    "made contact",
    "passed a message",
    "via intermediary",
    "via intermediaries",
    "through intermediary",
    "through an intermediary",
    "through intermediaries",
    "through envoy",
    "through an envoy",
    "via envoy",
)

# Future-tense or planning indicators.
FUTURE_PHRASES: Final[tuple[str, ...]] = (
    "will meet",
    "will speak",
    "will call",
    "will hold talks",
    "expected to meet",
    "expected to speak",
    "expected to call",
    "scheduled to meet",
    "scheduled to speak",
    "scheduled to call",
    "scheduled a call",
    "plans to meet",
    "plans to speak",
    "plans to call",
    "planning to meet",
    "may meet",
    "may speak",
    "may call",
    "could meet",
    "could speak",
    "could call",
    "to meet",
    "to speak with",
    "to hold talks",
    "set to meet",
    "set to speak",
    "set to call",
    "agreed to meet",
    "agreed to speak",
    "agreed to call",
)

# Negation phrases that defeat a match outright.
NEGATION_PHRASES: Final[tuple[str, ...]] = (
    "did not call",
    "did not speak",
    "did not meet",
    "did not talk",
    "didn't call",
    "didn't speak",
    "didn't meet",
    "didn't talk",
    "no call",
    "no meeting",
    "no conversation",
    "no contact",
    "declined to speak",
    "declined to call",
    "declined to meet",
    "refused to speak",
    "refused to call",
    "refused to meet",
    "denied",
    "denied calling",
    "denied speaking",
    "denied meeting",
    "denied a call",
    "denied any call",
    "ruled out",
    "no plans",
    "no plans to call",
    "no plans to meet",
    "no plans to speak",
    "has not spoken",
    "has not called",
    "has not met",
    "have not spoken",
    "have not called",
    "have not met",
    "without speaking",
    "without meeting",
)

BODY_HEADLINE_WINDOW = 500
VERB_PROXIMITY_WINDOW = 200


@dataclass(frozen=True)
class MarketContext:
    """The minimal market info the matcher needs to score an article."""

    ticker: str
    subject: str
    open_ts: str | None = None
    close_ts: str | None = None


@dataclass(frozen=True)
class MatchResult:
    """One per (article, market) pair, including 0-confidence results."""

    ticker: str
    confidence: float
    matched_subject: str | None
    matched_keywords: list[str] = field(default_factory=list)
    match_reason: str = ""


@dataclass
class NewsMatcher:
    """Score a (headline, body) against each market context.

    Construct with:
    - ``extractor``: a SubjectExtractor (the same one feeding discovery)
    - ``article_published_ts``: optional ISO timestamp; if a market's
      window does not contain it, confidence is forced to 0 with reason
      ``out_of_window``.
    """

    extractor: SubjectExtractor

    def match(
        self,
        *,
        headline: str,
        body: str | None,
        markets: list[MarketContext],
        article_published_ts: str | None = None,
    ) -> list[MatchResult]:
        """Return one MatchResult per market context (always populated)."""
        results: list[MatchResult] = []
        body_text = body or ""
        published_dt = parse_iso(article_published_ts) if article_published_ts else None

        # Pre-compute lowered text once.
        h_lower = headline.lower()
        b_lower = body_text.lower()
        first_500 = b_lower[:BODY_HEADLINE_WINDOW]
        full_lower = f"{h_lower} {b_lower}"

        trump_in_headline = self._any_in(TRUMP_ALIASES, h_lower)
        trump_in_first_500 = self._any_in(TRUMP_ALIASES, first_500)
        has_trump = trump_in_headline or trump_in_first_500

        for market in markets:
            results.append(
                self._score_one(
                    market=market,
                    headline_lower=h_lower,
                    body_lower=b_lower,
                    first_500_lower=first_500,
                    full_lower=full_lower,
                    has_trump=has_trump,
                    trump_in_headline=trump_in_headline,
                    published_dt=published_dt,
                )
            )
        return results

    # -- per-market scoring ---------------------------------------------

    def _score_one(
        self,
        *,
        market: MarketContext,
        headline_lower: str,
        body_lower: str,
        first_500_lower: str,
        full_lower: str,
        has_trump: bool,
        trump_in_headline: bool,
        published_dt: datetime | None,
    ) -> MatchResult:
        subject_aliases = self.extractor.aliases.get(market.subject)
        if not subject_aliases:
            return MatchResult(
                ticker=market.ticker,
                confidence=0.0,
                matched_subject=None,
                matched_keywords=[],
                match_reason=f"unknown_subject:{market.subject!r}",
            )

        subject_in_headline = _first_alias_in(subject_aliases, headline_lower)
        subject_in_first_500 = _first_alias_in(subject_aliases, first_500_lower)
        subject_alias = subject_in_headline or subject_in_first_500
        if subject_alias is None:
            return MatchResult(
                ticker=market.ticker,
                confidence=0.0,
                matched_subject=market.subject,
                matched_keywords=[],
                match_reason="no_subject_mention",
            )

        if not has_trump:
            return MatchResult(
                ticker=market.ticker,
                confidence=0.0,
                matched_subject=market.subject,
                matched_keywords=[subject_alias],
                match_reason="no_trump_mention",
            )

        # Negation check first — overrides everything below.
        neg_phrase = self._negation_near_subject(full_lower, subject_aliases)
        if neg_phrase is not None:
            return MatchResult(
                ticker=market.ticker,
                confidence=0.0,
                matched_subject=market.subject,
                matched_keywords=[subject_alias, neg_phrase],
                match_reason=f"negation_detected:{neg_phrase!r}",
            )

        # Article-window check.
        if published_dt is not None and not _within_window(published_dt, market):
            return MatchResult(
                ticker=market.ticker,
                confidence=0.0,
                matched_subject=market.subject,
                matched_keywords=[subject_alias],
                match_reason="out_of_window",
            )

        # Verb classification, in priority order:
        # 1. direct verb near subject -> high confidence
        # 2. indirect verb near subject -> 0.5, flagged
        # 3. future-tense phrase -> 0.2
        # 4. mention verb near subject -> 0.5

        direct_verb = self._direct_verb_near_subject(full_lower, subject_aliases)
        indirect_verb = self._indirect_verb_near_subject(full_lower, subject_aliases)
        future_phrase = self._future_phrase_near_subject(full_lower, subject_aliases)
        mention_verb = self._mention_verb_near_subject(full_lower, subject_aliases)

        keywords = [k for k in (subject_alias,) if k]
        # Indirect language is *more specific* than a generic direct verb
        # match; if both fire, treat the article as ambiguous (0.5) with
        # an indirect-communication flag.
        if indirect_verb is not None:
            keywords.append(indirect_verb)
            return MatchResult(
                ticker=market.ticker,
                confidence=0.5,
                matched_subject=market.subject,
                matched_keywords=keywords,
                match_reason=f"indirect_communication:{indirect_verb!r}",
            )
        if future_phrase is not None and direct_verb is None:
            keywords.append(future_phrase)
            return MatchResult(
                ticker=market.ticker,
                confidence=0.2,
                matched_subject=market.subject,
                matched_keywords=keywords,
                match_reason=f"future_tense:{future_phrase!r}",
            )
        if direct_verb is not None:
            keywords.append(direct_verb)
            confidence = 1.0 if subject_in_headline else 0.8
            location = "headline" if subject_in_headline else "body"
            return MatchResult(
                ticker=market.ticker,
                confidence=confidence,
                matched_subject=market.subject,
                matched_keywords=keywords,
                match_reason=f"direct_verb:{direct_verb!r} (subject_in_{location})",
            )
        if mention_verb is not None:
            keywords.append(mention_verb)
            return MatchResult(
                ticker=market.ticker,
                confidence=0.5,
                matched_subject=market.subject,
                matched_keywords=keywords,
                match_reason=f"mention_verb:{mention_verb!r}",
            )
        return MatchResult(
            ticker=market.ticker,
            confidence=0.0,
            matched_subject=market.subject,
            matched_keywords=keywords,
            match_reason="subject_and_trump_present_no_relevant_verb",
        )

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _any_in(needles: tuple[str, ...], haystack: str) -> bool:
        return any(re.search(rf"\b{re.escape(n)}\b", haystack) for n in needles)

    def _negation_near_subject(self, text: str, aliases: list[str]) -> str | None:
        """Return the negation phrase if it occurs within 80 chars of any alias."""
        for alias in aliases:
            for phrase in NEGATION_PHRASES:
                if self._within_distance(text, phrase, alias, distance=80):
                    return phrase
        return None

    def _direct_verb_near_subject(self, text: str, aliases: list[str]) -> str | None:
        return self._any_verb_near_subject(text, aliases, DIRECT_VERBS)

    def _indirect_verb_near_subject(self, text: str, aliases: list[str]) -> str | None:
        return self._any_verb_near_subject(text, aliases, INDIRECT_VERBS)

    def _mention_verb_near_subject(self, text: str, aliases: list[str]) -> str | None:
        return self._any_verb_near_subject(text, aliases, MENTION_VERBS)

    def _future_phrase_near_subject(self, text: str, aliases: list[str]) -> str | None:
        return self._any_verb_near_subject(text, aliases, FUTURE_PHRASES)

    def _any_verb_near_subject(
        self, text: str, aliases: list[str], verbs: tuple[str, ...]
    ) -> str | None:
        for alias in aliases:
            for verb in verbs:
                if self._within_distance(text, verb, alias, distance=VERB_PROXIMITY_WINDOW):
                    return verb
        return None

    @staticmethod
    def _within_distance(text: str, phrase_a: str, phrase_b: str, *, distance: int) -> bool:
        """True if `phrase_a` and `phrase_b` both occur in text within ``distance`` chars."""
        a_re = re.compile(rf"\b{re.escape(phrase_a)}\b")
        b_re = re.compile(rf"\b{re.escape(phrase_b)}\b")
        a_positions = [m.start() for m in a_re.finditer(text)]
        if not a_positions:
            return False
        b_positions = [m.start() for m in b_re.finditer(text)]
        if not b_positions:
            return False
        for a_pos in a_positions:
            for b_pos in b_positions:
                if abs(a_pos - b_pos) <= distance:
                    return True
        return False


def _first_alias_in(aliases: list[str], text: str) -> str | None:
    """Return the first alias (longest-first) that occurs in ``text`` as a whole word."""
    for alias in sorted(aliases, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias.lower())}\b", text):
            return alias
    return None


def _within_window(article_dt: datetime, market: MarketContext) -> bool:
    open_dt = parse_iso(market.open_ts) if market.open_ts else None
    close_dt = parse_iso(market.close_ts) if market.close_ts else None
    if open_dt and article_dt < open_dt:
        return False
    return not (close_dt and article_dt > close_dt)
