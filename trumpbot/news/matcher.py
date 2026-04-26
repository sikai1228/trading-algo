"""NewsMatcher: Stage 1 keyword pre-filter (aggressively inclusive).

Phase 4 Part 2.8 (April 2026) replaced the proximity-based, verb-
classified matcher with a single 3-condition pre-filter. Stage 2
(:mod:`trumpbot.news.llm_classifier`) is the precision filter; Stage
1's only job is to ensure no obvious positive ever falls off the
conveyor before the LLM sees it.

Pre-filter (all word-boundary, case-insensitive, anywhere in
``headline + body``):

A. ``"trump"`` (or another alias in :data:`TRUMP_ALIASES`)
B. At least one alias of the market's subject (loaded from the
   ``subjects`` table via :class:`SubjectExtractor`)
C. At least one term from
   :data:`trumpbot.news.interaction_terms.INTERACTION_TERMS`

If all three are present, the matcher emits ``confidence=0.0`` with
``match_reason="passed_pre_filter"``. The LLM cascade picks up
"passed_pre_filter" rows, classifies them against the verbatim
contract rules, and overwrites confidence + ``classifier_type`` +
``llm_classification_id`` on the same row. Until the cascade has
classified a match, the decision engine sees
``interaction_occurred=False`` and skips it (see
``trumpbot.decision.loops._row_to_snapshot``).

If a condition is missing, the matcher emits ``confidence=0.0`` with
``match_reason="failed_pre_filter:<which conditions failed>"`` and
the LLM is **not** called — that's the cost guard.

What was removed (and why):

- Proximity windows (subject must be within N chars of verb)
- Verb-class hierarchy (DIRECT vs MENTION vs INDIRECT vs FUTURE)
- Negation pattern detection
- Future-tense pattern detection
- Tier-based confidence scoring (1.0/0.8/0.5/0.2)
- Article-window check (the decision engine still enforces this in
  Rule 4)

These rules were brittle: real headlines like "Trump says he speaks
with Putin Zelenskiy: Fox News" passed proximity checks at high
confidence even though they describe a habitual self-claim, and
others like "Powell briefed Trump" needed bespoke verb-list patches
to score at all. Stage 2 is the right layer for those calls because
it reads the article body against the contract rules.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final

from trumpbot.discovery.subjects import SubjectExtractor
from trumpbot.news.interaction_terms import INTERACTION_TERMS

# Words that signal "Trump is the actor."
TRUMP_ALIASES: Final[tuple[str, ...]] = (
    "trump",
    "donald trump",
    "president trump",
    "the president",
    "potus",
    "@potus",
    "@realdonaldtrump",
)

PRE_FILTER_CONFIDENCE: Final[float] = 0.0
"""Stage 1 always emits confidence=0.0 — the LLM writes the real value."""

PASSED_REASON: Final[str] = "passed_pre_filter"
"""Match-row reason when all three conditions are satisfied."""


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

    The ``article_published_ts`` parameter is accepted for back-
    compatibility but ignored — the article-window check moved to the
    decision engine in Phase 4 Part 2.8.
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
        del article_published_ts  # unused; window check lives in DecisionEngine

        text = f"{headline} {body or ''}".lower()
        trump_match = _first_word_match(TRUMP_ALIASES, text)
        interaction_match = _first_word_match(INTERACTION_TERMS, text)

        return [
            self._score_one(
                market=market,
                text=text,
                trump_match=trump_match,
                interaction_match=interaction_match,
            )
            for market in markets
        ]

    def _score_one(
        self,
        *,
        market: MarketContext,
        text: str,
        trump_match: str | None,
        interaction_match: str | None,
    ) -> MatchResult:
        aliases = self.extractor.aliases.get(market.subject)
        if not aliases:
            return MatchResult(
                ticker=market.ticker,
                confidence=PRE_FILTER_CONFIDENCE,
                matched_subject=None,
                matched_keywords=[],
                match_reason=f"failed_pre_filter:unknown_subject:{market.subject!r}",
            )

        subject_match = _first_word_match(aliases, text)

        missing: list[str] = []
        if trump_match is None:
            missing.append("no_trump")
        if subject_match is None:
            missing.append("no_subject")
        if interaction_match is None:
            missing.append("no_interaction_term")

        keywords = [k for k in (trump_match, subject_match, interaction_match) if k]

        if missing:
            return MatchResult(
                ticker=market.ticker,
                confidence=PRE_FILTER_CONFIDENCE,
                matched_subject=market.subject,
                matched_keywords=keywords,
                match_reason="failed_pre_filter:" + "+".join(missing),
            )

        return MatchResult(
            ticker=market.ticker,
            confidence=PRE_FILTER_CONFIDENCE,
            matched_subject=market.subject,
            matched_keywords=keywords,
            match_reason=PASSED_REASON,
        )


def _first_word_match(needles: Iterable[str], text: str) -> str | None:
    """Return the first needle that occurs in ``text`` as a whole word.

    ``text`` must be pre-lowercased upstream. Each needle is lowercased
    here so callers can pass aliases in their natural casing
    ("Vladimir Putin", "Bibi"). Multi-word needles work because ``\\b``
    only constrains the outer edges.
    """
    for n in needles:
        nl = n.lower()
        if re.search(rf"\b{re.escape(nl)}\b", text):
            return n
    return None
