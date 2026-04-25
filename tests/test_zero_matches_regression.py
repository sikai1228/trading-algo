"""Regression tests for the "528 articles, 0 matches" smoke-test failure.

Three root causes — pin behavior so they cannot silently regress:

1. Subject-key bridge: discovery-side keys (``vladimirputin``) must
   resolve via the subjects table, not the matcher's hardcoded
   ``DEFAULT_SUBJECT_ALIASES`` (which uses short keys like ``putin``).
   Pinned in ``test_matcher_subjects_bridge.py``.

2. Case-sensitive verb proximity: ``_within_distance`` must lowercase
   both operands so DB-loaded aliases (mixed case) match the
   pre-lowercased text. Pinned in ``test_matcher_subjects_bridge.py``
   too.

3. **This file** pins:
   a. The verb list covers the contract-relevant interaction verbs the
      operator's diagnostic identified — ``briefed``, ``dined``,
      ``lunch with``, ``dinner with`` — without which articles like
      "Powell briefed Trump" silently scored 0.
   b. The matcher worker queries via ``list_markets_for_matching``
      (all markets with subject) rather than ``list_active_markets``
      (only ``status='active'``), so news mentioning settled-market
      subjects still produces match rows during the observation period.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trumpbot.daemon import MatcherWorker
from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    MarketRow,
    NewsEventRow,
    SubjectRow,
    insert_news_event,
    list_markets_for_matching,
    upsert_market,
    upsert_subject,
)
from trumpbot.discovery.subjects import DEFAULT_SUBJECT_ALIASES, SubjectExtractor
from trumpbot.events.bus import EventBus
from trumpbot.news.matcher import DIRECT_VERBS, MarketContext, NewsMatcher

_NULL = SubjectExtractor(aliases={"_unused": ["unused"]})


def _seed(db: Database, ticker: str, subject_key: str, full_name: str, status: str) -> None:
    upsert_subject(
        db,
        SubjectRow(
            subject_key=subject_key,
            full_name=full_name,
            aliases=[full_name, full_name.split()[-1]],
        ),
    )
    upsert_market(
        db,
        MarketRow(
            ticker=ticker,
            series_ticker="KXTRUMPMEET",
            event_ticker="KXTRUMPMEET-26APR",
            title=f"Donald Trump and {full_name} meet before May 1, 2026?",
            subtitle=full_name,
            yes_sub_title="Yes",
            no_sub_title="No",
            subject=subject_key,
            subject_full_name=full_name,
            resolution_rules="If they meet, resolves YES.",
            approved_sources=None,
            open_ts="2026-04-01T00:00:00.000000Z",
            close_ts="2026-04-30T23:59:59.000000Z",
            expected_expiration_ts=None,
            status=status,
            last_price_cents=42,
            volume=100,
            open_interest=50,
            raw_json=None,
        ),
    )


# ---------------------------------------------------------------------------
# 3a — verb list completeness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "verb",
    [
        "briefed",
        "briefing",
        "dined",
        "dined with",
        "dinner with",
        "had dinner with",
        "lunch with",
        "lunched",
        "lunched with",
        "had lunch with",
        "breakfast with",
        "had breakfast with",
    ],
)
def test_contract_relevant_verb_present(verb: str) -> None:
    """The Phase-1 diagnostic identified these verbs as missing.
    Adding each enables real news to actually produce matches."""
    assert verb in DIRECT_VERBS, f"{verb!r} must remain in DIRECT_VERBS"


@pytest.fixture()
def matcher_with_subjects() -> NewsMatcher:
    """Matcher whose extractor knows the discovery-style subject_keys."""
    aliases = {
        **DEFAULT_SUBJECT_ALIASES,
        "jeromepowell": ["Jerome Powell", "Powell", "Jay Powell"],
        "chuckschumer": ["Chuck Schumer", "Schumer"],
        "tigerwoods": ["Tiger Woods", "Woods"],
        "johnthune": ["John Thune", "Thune"],
        "vladimirputin": ["Vladimir Putin", "Putin"],
        "benjaminnetanyahu": ["Benjamin Netanyahu", "Netanyahu", "Bibi"],
        "jensenhuang": ["Jensen Huang", "Huang"],
    }
    return NewsMatcher(extractor=SubjectExtractor(aliases=aliases))


class TestUserSpecifiedRegressionCases:
    """The five cases the operator explicitly required to score >= 0.7."""

    def test_thune_meeting(self, matcher_with_subjects: NewsMatcher) -> None:
        ctx = MarketContext(ticker="T", subject="johnthune")
        [r] = matcher_with_subjects.match(
            headline="Trump met with Senator Thune at the White House yesterday",
            body=None,
            markets=[ctx],
        )
        assert r.confidence >= 0.7

    def test_putin_phone_call(self, matcher_with_subjects: NewsMatcher) -> None:
        ctx = MarketContext(ticker="T", subject="vladimirputin")
        [r] = matcher_with_subjects.match(
            headline="Trump and Putin held a 90-minute phone call this morning",
            body=None,
            markets=[ctx],
        )
        assert r.confidence >= 0.7

    def test_powell_briefed(self, matcher_with_subjects: NewsMatcher) -> None:
        """`briefed` was missing from DIRECT_VERBS pre-fix — this scored 0."""
        ctx = MarketContext(ticker="T", subject="jeromepowell")
        [r] = matcher_with_subjects.match(
            headline="Powell briefed Trump on rate decision during private meeting",
            body=None,
            markets=[ctx],
        )
        assert r.confidence >= 0.7, f"got {r.confidence} reason={r.match_reason}"

    def test_netanyahu_called(self, matcher_with_subjects: NewsMatcher) -> None:
        ctx = MarketContext(ticker="T", subject="benjaminnetanyahu")
        [r] = matcher_with_subjects.match(
            headline="Trump called Netanyahu, sources confirm",
            body=None,
            markets=[ctx],
        )
        assert r.confidence >= 0.7

    def test_schumer_meeting(self, matcher_with_subjects: NewsMatcher) -> None:
        ctx = MarketContext(ticker="T", subject="chuckschumer")
        [r] = matcher_with_subjects.match(
            headline="Schumer met with Trump to discuss the budget",
            body=None,
            markets=[ctx],
        )
        assert r.confidence >= 0.7


class TestMealsAndBriefings:
    """Contract explicitly says 'Working dinners, lunches, or other meal
    meetings' qualify. Added these verbs so they actually do."""

    def test_dined_with(self, matcher_with_subjects: NewsMatcher) -> None:
        ctx = MarketContext(ticker="T", subject="chuckschumer")
        [r] = matcher_with_subjects.match(
            headline="Schumer dined with Trump at Mar-a-Lago last night",
            body=None,
            markets=[ctx],
        )
        assert r.confidence >= 0.7

    def test_had_lunch_with(self, matcher_with_subjects: NewsMatcher) -> None:
        ctx = MarketContext(ticker="T", subject="tigerwoods")
        [r] = matcher_with_subjects.match(
            headline="Trump and Tiger Woods had lunch at Mar-a-Lago yesterday",
            body=None,
            markets=[ctx],
        )
        assert r.confidence >= 0.7

    def test_briefing_alias(self, matcher_with_subjects: NewsMatcher) -> None:
        ctx = MarketContext(ticker="T", subject="jeromepowell")
        [r] = matcher_with_subjects.match(
            headline="Trump received Fed briefing from Powell",
            body=None,
            markets=[ctx],
        )
        assert r.confidence >= 0.5


# ---------------------------------------------------------------------------
# 3b — matcher worker considers settled markets too
# ---------------------------------------------------------------------------


class TestMatcherWorkerIncludesSettledMarkets:
    """Pre-fix: ``list_active_markets`` filtered settled markets out of
    the matcher's candidate set entirely. "Trump called Netanyahu"
    against a ``status='finalized'`` Netanyahu market produced 0 not
    because the matcher failed but because the market was invisible.
    Now ``list_markets_for_matching`` includes them."""

    def test_list_markets_for_matching_includes_finalized(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "lm.db")
        db.connect()
        _seed(db, "KXTRUMPMEET-26APR-AAA", "subjecta", "Subject A", status="active")
        _seed(db, "KXTRUMPMEET-26APR-BBB", "subjectb", "Subject B", status="finalized")
        _seed(db, "KXTRUMPMEET-26APR-CCC", "subjectc", "Subject C", status="settled")
        rows = list_markets_for_matching(db)
        statuses = sorted(r["status"] for r in rows)
        assert statuses == ["active", "finalized", "settled"]
        db.close()

    async def test_worker_matches_against_finalized_market(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "wf.db")
        db.connect()
        _seed(
            db,
            "KXTRUMPMEET-26APR-BNET",
            "benjaminnetanyahu",
            "Benjamin Netanyahu",
            status="finalized",
        )
        insert_news_event(
            db,
            NewsEventRow(
                source="reuters",
                source_weight=1.0,
                is_kalshi_approved=True,
                headline="Trump called Netanyahu, sources confirm",
                url="https://example.com/n",
                url_canonical="https://example.com/n",
                body_excerpt=None,
                author=None,
                raw_published_ts=None,
                detected_ts="2026-04-25T12:00:00Z",
                has_photo=False,
                has_video=False,
                raw_data=None,
            ),
        )
        worker = MatcherWorker(
            db=db,
            matcher=NewsMatcher(extractor=_NULL),
            event_bus=EventBus(),
        )
        await worker._process_batch()
        rows = list(
            db.connect().execute(
                "SELECT confidence, matched_subject, match_reason FROM news_market_matches"
            )
        )
        assert len(rows) == 1
        # The finalized market is now visible to the matcher and the
        # call+subject combination scores 1.0.
        assert rows[0]["confidence"] == 1.0
        assert rows[0]["matched_subject"] == "benjaminnetanyahu"
        db.close()
