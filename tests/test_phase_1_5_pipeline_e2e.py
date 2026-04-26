"""End-to-end pipeline test for Phase 1.5 LLM cascade.

The CRITICAL test: a synthetic clear-positive article must flow
through MatcherWorker (Stage 1) -> LLMClassifier (Stage 2) ->
news_market_matches (with classifier_type='llm_cascade') ->
DecisionEngine -> a TradeIntent.

Pre-Phase-4-Part-2.8 this was impossible: interaction_occurred was
hardcoded False, so the engine always returned None. After 2.8 the
LLM's parsed_interaction_occurred flows through.

A second test pins the Reuters habitual-self-claim case (the article
that motivated the investigation): same pipeline, but the LLM
returns interaction_occurred=False, confidence below 0.85, so the
engine correctly returns None.
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
    fetch_match_with_classification,
    insert_news_event,
    upsert_market,
    upsert_subject,
)
from trumpbot.decision.engine import (
    BankrollState,
    DecisionConfig,
    DecisionEngine,
    MarketState,
    MatchSnapshot,
)
from trumpbot.discovery.subjects import SubjectExtractor
from trumpbot.events.bus import EventBus
from trumpbot.news.llm_classifier import (
    LLMClassifier,
    LLMClassifierConfig,
)
from trumpbot.news.matcher import NewsMatcher
from trumpbot.notifications.llm_cost import LLMCostGuard, LLMCostGuardConfig

_ALIASES = {"vladimirputin": ["Vladimir Putin", "Putin"]}


def _seed_market(db: Database, ticker: str = "KXTRUMPMEET-26APR-VPUT") -> str:
    upsert_subject(
        db,
        SubjectRow(
            subject_key="vladimirputin",
            full_name="Vladimir Putin",
            aliases=["Vladimir Putin", "Putin"],
        ),
    )
    upsert_market(
        db,
        MarketRow(
            ticker=ticker,
            series_ticker="KXTRUMPMEET",
            event_ticker="KXTRUMPMEET-26APR",
            title="Donald Trump and Vladimir Putin meet before May 1, 2026?",
            subtitle="Vladimir Putin",
            yes_sub_title="Yes",
            no_sub_title="No",
            subject="vladimirputin",
            subject_full_name="Vladimir Putin",
            resolution_rules="If they meet, resolves YES.",
            approved_sources=None,
            open_ts="2026-04-01T00:00:00.000000Z",
            close_ts="2026-04-30T23:59:59.000000Z",
            expected_expiration_ts=None,
            status="active",
            last_price_cents=42,
            volume=100,
            open_interest=50,
            raw_json=None,
        ),
    )
    return ticker


def _seed_event(db: Database, headline: str, body: str) -> int:
    return (
        insert_news_event(
            db,
            NewsEventRow(
                source="reuters",
                is_kalshi_approved=True,
                headline=headline,
                url=f"https://example.com/{abs(hash(headline))}",
                url_canonical=f"https://example.com/{abs(hash(headline))}",
                body_excerpt=body,
                author=None,
                raw_published_ts="2026-04-25T12:00:00Z",
                detected_ts="2026-04-25T12:00:01Z",
                has_photo=False,
                has_video=False,
                raw_data=None,
            ),
        )
        or 0
    )


def _make_classifier(
    db: Database,
    *,
    contract_file: Path,
    prompt_file: Path,
    response_text: str,
) -> LLMClassifier:
    cost_guard = LLMCostGuard(
        db=db,
        config=LLMCostGuardConfig(monthly_cap_usd_cents=10_000),
    )

    async def stub_call(system: str, user: str) -> tuple[int, int, str]:
        return (200, 60, response_text)

    return LLMClassifier(
        db=db,
        cost_guard=cost_guard,
        alerts=None,
        config=LLMClassifierConfig(
            enabled=True,
            prompt_path=str(prompt_file),
            contract_path=str(contract_file),
        ),
        llm_call=stub_call,
    )


@pytest.fixture()
def contract_file(tmp_path: Path) -> Path:
    p = tmp_path / "rules.txt"
    p.write_text(
        "If Donald Trump and [name] meet (including phone calls)\n"
        "before the deadline, then the market resolves YES.\n"
    )
    return p


@pytest.fixture()
def prompt_file(tmp_path: Path) -> Path:
    p = tmp_path / "prompt.txt"
    p.write_text(
        "{contract_rules_verbatim}\n{subject_list}\n" "{article_headline}\n{article_body}\n"
    )
    return p


# ---------------------------------------------------------------------------
# Test 1: synthetic clear positive -> TradeIntent
# ---------------------------------------------------------------------------


class TestSyntheticClearPositiveProducesTradeIntent:
    """The CRITICAL test from the user spec.

    A clear-positive article ("Trump and Putin held 90-min call,
    Kremlin confirms") must flow through the full pipeline and end
    in a TradeIntent. Pre-Phase-4-Part-2.8 this was impossible
    because interaction_occurred was hardcoded False."""

    async def test_full_pipeline_produces_intent(
        self,
        tmp_path: Path,
        contract_file: Path,
        prompt_file: Path,
    ) -> None:
        db = Database(tmp_path / "e2e.db")
        db.connect()
        ticker = _seed_market(db)
        evt_id = _seed_event(
            db,
            headline="Trump and Putin held 90-minute phone call, Kremlin confirms",
            body=(
                "President Donald Trump and Russian President Vladimir Putin "
                "held a 90-minute phone call early Tuesday, according to a "
                "Kremlin readout."
            ),
        )

        classifier = _make_classifier(
            db,
            contract_file=contract_file,
            prompt_file=prompt_file,
            response_text=(
                '{"subject": "vladimirputin", "interaction_occurred": true, '
                '"interaction_type": "phone", "tense": "past", '
                '"negated": false, "indirect_only": false, '
                '"confidence": 0.92, "reasoning": "Past-tense phone call '
                'confirmed by Kremlin readout per contract clause."}'
            ),
        )

        worker = MatcherWorker(
            db=db,
            matcher=NewsMatcher(extractor=SubjectExtractor(aliases=_ALIASES)),
            event_bus=EventBus(),
            classifier=classifier,
        )
        await worker._process_batch()

        # Pull the upgraded match row.
        row = (
            db.connect()
            .execute(
                "SELECT id FROM news_market_matches WHERE news_event_id = ?",
                (evt_id,),
            )
            .fetchone()
        )
        assert row is not None
        merged = fetch_match_with_classification(db, match_id=row["id"])
        assert merged is not None
        assert merged["classifier_type"] == "llm_cascade"
        assert merged["confidence"] >= 0.85
        assert merged["matched_subject"] == "vladimirputin"
        assert merged["parsed_interaction_occurred"] == 1

        # Now run it through the engine.
        snap = MatchSnapshot(
            match_id=int(merged["id"]),
            ticker=ticker,
            confidence=float(merged["confidence"]),
            interaction_occurred=bool(merged["parsed_interaction_occurred"]),
            source_name="reuters",
            is_kalshi_approved=True,
            market_open_ts="2026-04-01T00:00:00Z",
            market_close_ts="2026-04-30T23:59:59Z",
            article_published_ts="2026-04-25T12:00:00Z",
            classified_at_ts="2026-04-25T12:00:02Z",
        )
        engine = DecisionEngine(DecisionConfig())
        intent = engine.evaluate_news_match(
            match=snap,
            market_state=MarketState(
                ticker=ticker,
                yes_ask_cents=60,
                yes_bid_cents=58,
            ),
            current_position=None,
            bankroll=BankrollState(
                bankroll_usd_cents=50_000,
                open_position_cost_usd_cents=0,
            ),
            yes_ask_levels=[(60, 100), (61, 100)],
        )
        # The pipeline is functional!
        assert intent is not None
        assert intent.ticker == ticker
        assert intent.target_quantity > 0
        db.close()


# ---------------------------------------------------------------------------
# Test 2: Reuters habitual self-claim -> NO TradeIntent
# ---------------------------------------------------------------------------


class TestReutersHabitualSelfClaimRejected:
    """The article that motivated the investigation: 'Trump says he
    speaks with Putin: Fox News'. It passes Stage 1 (the new aggressive
    pre-filter), but the LLM correctly identifies it as a habitual
    self-claim and returns interaction_occurred=False with low
    confidence. The engine then returns None — no trade fires."""

    async def test_reuters_article_does_not_trigger_trade(
        self,
        tmp_path: Path,
        contract_file: Path,
        prompt_file: Path,
    ) -> None:
        db = Database(tmp_path / "reuters.db")
        db.connect()
        ticker = _seed_market(db)
        evt_id = _seed_event(
            db,
            headline="Trump says he speaks with Putin and Zelenskiy: Fox News",
            body=(
                "President Donald Trump told Fox News in an interview that "
                "he speaks with Russian President Vladimir Putin and Ukrainian "
                "President Volodymyr Zelenskiy regularly about the conflict. "
                "Trump did not specify when the most recent calls occurred."
            ),
        )

        classifier = _make_classifier(
            db,
            contract_file=contract_file,
            prompt_file=prompt_file,
            response_text=(
                '{"subject": "vladimirputin", "interaction_occurred": false, '
                '"interaction_type": null, "tense": "ongoing", '
                '"negated": false, "indirect_only": false, '
                '"confidence": 0.15, "reasoning": "Habitual self-claim with '
                'no specific dated event; no third-party confirmation."}'
            ),
        )

        worker = MatcherWorker(
            db=db,
            matcher=NewsMatcher(extractor=SubjectExtractor(aliases=_ALIASES)),
            event_bus=EventBus(),
            classifier=classifier,
        )
        await worker._process_batch()

        row = (
            db.connect()
            .execute(
                "SELECT id FROM news_market_matches WHERE news_event_id = ?",
                (evt_id,),
            )
            .fetchone()
        )
        assert row is not None
        merged = fetch_match_with_classification(db, match_id=row["id"])
        assert merged is not None
        assert merged["classifier_type"] == "llm_cascade"
        assert merged["confidence"] < 0.85
        assert merged["parsed_interaction_occurred"] == 0

        # Engine: with confidence < 0.85 OR interaction_occurred False,
        # returns None. We assert by passing the snapshot through.
        snap = MatchSnapshot(
            match_id=int(merged["id"]),
            ticker=ticker,
            confidence=float(merged["confidence"]),
            interaction_occurred=bool(merged["parsed_interaction_occurred"]),
            source_name="reuters",
            is_kalshi_approved=True,
            market_open_ts="2026-04-01T00:00:00Z",
            market_close_ts="2026-04-30T23:59:59Z",
            article_published_ts="2026-04-25T12:00:00Z",
            classified_at_ts="2026-04-25T12:00:02Z",
        )
        engine = DecisionEngine(DecisionConfig())
        intent = engine.evaluate_news_match(
            match=snap,
            market_state=MarketState(
                ticker=ticker,
                yes_ask_cents=60,
                yes_bid_cents=58,
            ),
            current_position=None,
            bankroll=BankrollState(
                bankroll_usd_cents=50_000,
                open_position_cost_usd_cents=0,
            ),
            yes_ask_levels=[(60, 100), (61, 100)],
        )
        assert intent is None
        db.close()
