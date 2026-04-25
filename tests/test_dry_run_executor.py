"""DryRunExecutor tests."""

from __future__ import annotations

from pathlib import Path

from trumpbot.db.connection import Database
from trumpbot.db.repositories import MarketRow, NewsEventRow, insert_news_event, upsert_market
from trumpbot.decision.engine import BankrollState
from trumpbot.execution.dry_run import DryRunExecutor, Quote
from trumpbot.risk.manager import RiskConfig, RiskManager, RiskState
from trumpbot.types.intents import (
    RISK_APPROVAL_TOKEN,
    RiskApprovedOrder,
    StopLossIntent,
    TradeIntent,
)


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "exec.db")
    db.connect()
    # Seed market + news event so the FK references in `trades` resolve.
    upsert_market(
        db,
        MarketRow(
            ticker="X",
            series_ticker="KXTRUMPMEET",
            event_ticker="KXTRUMPMEET-26APR",
            title="Donald Trump and X Y meet before May 1, 2026?",
            subtitle=None,
            yes_sub_title=None,
            no_sub_title=None,
            subject="xy",
            subject_full_name="X Y",
            resolution_rules="r",
            approved_sources=None,
            open_ts="2026-04-01T00:00:00Z",
            close_ts="2026-04-30T23:59:59Z",
            expected_expiration_ts=None,
            status="active",
            last_price_cents=42,
            volume=100,
            open_interest=50,
            raw_json=None,
        ),
    )
    return db


def _seed_match(db: Database) -> int:
    eid = insert_news_event(
        db,
        NewsEventRow(
            source="reuters_via_gnews",
            source_weight=1.0,
            is_kalshi_approved=True,
            headline="h",
            url="https://e.com/1",
            url_canonical="https://e.com/1",
            body_excerpt=None,
            author=None,
            raw_published_ts=None,
            detected_ts="2026-04-15T12:00:00Z",
            has_photo=False,
            has_video=False,
            raw_data=None,
        ),
    )
    assert eid is not None
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO news_market_matches (news_event_id, ticker, confidence, "
            "matched_subject, match_reason) VALUES (?, 'X', 0.9, 'xy', 'test')",
            (eid,),
        )
        return int(cur.lastrowid or 0)


def _approve(db: Database, intent: TradeIntent | StopLossIntent) -> RiskApprovedOrder:
    rm = RiskManager(db=db, config=RiskConfig())
    state = RiskState(
        bankroll=BankrollState(50000, 0, None),
        open_position_tickers=(
            frozenset({intent.ticker}) if isinstance(intent, StopLossIntent) else frozenset()
        ),
    )
    out = rm.evaluate(intent, state)
    assert isinstance(out, RiskApprovedOrder)
    return out


def _book(*, bid: int | None, ask: int | None) -> Quote:
    return Quote(yes_bid_cents=bid, yes_ask_cents=ask)


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


class TestEntrySubmission:
    def test_simulated_fill_at_current_ask(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        match_id = _seed_match(db)
        intent = TradeIntent(
            ticker="X",
            target_price_cents=50,
            target_quantity=10,
            target_size_usd_cents=500,
            triggering_match_id=match_id,
            confirmation_weight=0.9,
            confidence_score=0.9,
            reasoning_text="r",
        )
        approved = _approve(db, intent)
        executor = DryRunExecutor(db=db, orderbook_fn=lambda _t: _book(bid=49, ask=50))
        result = executor.submit(approved)
        assert result.status == "filled"
        assert result.fill_price_cents == 50
        assert result.fill_quantity == 10
        rows = list(db.connect().execute("SELECT * FROM trades"))
        assert len(rows) == 1
        assert rows[0]["status"] == "dry_run"
        assert rows[0]["entry_price_cents"] == 50
        assert rows[0]["cost_basis_usd_cents"] == 500

    def test_no_ask_rejects(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        match_id = _seed_match(db)
        intent = TradeIntent(
            ticker="X",
            target_price_cents=50,
            target_quantity=10,
            target_size_usd_cents=500,
            triggering_match_id=match_id,
            confirmation_weight=0.9,
            confidence_score=0.9,
            reasoning_text="r",
        )
        approved = _approve(db, intent)
        executor = DryRunExecutor(db=db, orderbook_fn=lambda _t: _book(bid=None, ask=None))
        result = executor.submit(approved)
        assert result.status == "rejected"

    def test_adjusted_quantity_honored(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        match_id = _seed_match(db)
        # Construct an intent that would normally buy 100 contracts but
        # the risk gate caps to 4. Confirm executor uses 4.
        intent = TradeIntent(
            ticker="X",
            target_price_cents=50,
            target_quantity=100,
            target_size_usd_cents=5000,
            triggering_match_id=match_id,
            confirmation_weight=0.9,
            confidence_score=0.9,
            reasoning_text="r",
        )
        # Construct directly with a manual adjusted_quantity (since we
        # want to test the executor in isolation from risk-cap logic).
        approved = RiskApprovedOrder(
            intent_type="entry",
            intent=intent,
            risk_decision_id=1,
            adjusted_quantity=4,
            approval_token=RISK_APPROVAL_TOKEN,
        )
        # Pre-insert a risk_decisions row so the FK is satisfied.
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO risk_decisions (intent_type, intent_json, decision, "
                "rejection_reason, rule_fired, reasoning_text) "
                "VALUES ('entry', '{}', 'approved', NULL, NULL, 't')"
            )
        executor = DryRunExecutor(db=db, orderbook_fn=lambda _t: _book(bid=49, ask=50))
        result = executor.submit(approved)
        assert result.fill_quantity == 4
        assert result.fill_price_cents == 50


# ---------------------------------------------------------------------------
# Stop-loss
# ---------------------------------------------------------------------------


class TestStopLossSubmission:
    def test_closes_at_current_bid(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        match_id = _seed_match(db)
        # First, create an open trade.
        intent = TradeIntent(
            ticker="X",
            target_price_cents=80,
            target_quantity=10,
            target_size_usd_cents=800,
            triggering_match_id=match_id,
            confirmation_weight=1.0,
            confidence_score=1.0,
            reasoning_text="r",
        )
        approved = _approve(db, intent)
        executor = DryRunExecutor(db=db, orderbook_fn=lambda _t: _book(bid=80, ask=80))
        entry_result = executor.submit(approved)
        assert entry_result.fill_quantity is not None

        # Now stop-loss it: bid drops to 20¢.
        stop = StopLossIntent(
            ticker="X",
            trade_id=entry_result.trade_id,
            entry_price_cents=80,
            current_bid_cents=20,
            drop_cents=60,
            position_quantity=entry_result.fill_quantity,
            cost_basis_usd_cents=80 * entry_result.fill_quantity,
            current_value_usd_cents=20 * entry_result.fill_quantity,
            unrealized_pnl_usd_cents=(20 - 80) * entry_result.fill_quantity,
            reasoning_text="stop",
        )
        approved_stop = _approve(db, stop)
        executor2 = DryRunExecutor(db=db, orderbook_fn=lambda _t: _book(bid=20, ask=22))
        stop_result = executor2.submit(approved_stop)
        assert stop_result.fill_price_cents == 20
        assert stop_result.realized_pnl_usd_cents == (20 - 80) * entry_result.fill_quantity
        # Status updated to closed.
        row = (
            db.connect()
            .execute(
                "SELECT status, exit_price_cents, realized_pnl_usd_cents FROM trades WHERE id = ?",
                (entry_result.trade_id,),
            )
            .fetchone()
        )
        assert row["status"] == "dry_run_closed_stop"


# ---------------------------------------------------------------------------
# Position marking
# ---------------------------------------------------------------------------


class TestUpdatePositionMarks:
    def test_marks_open_dry_run_trades_only(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        match_id = _seed_match(db)
        intent = TradeIntent(
            ticker="X",
            target_price_cents=50,
            target_quantity=10,
            target_size_usd_cents=500,
            triggering_match_id=match_id,
            confirmation_weight=0.9,
            confidence_score=0.9,
            reasoning_text="r",
        )
        approved = _approve(db, intent)
        executor = DryRunExecutor(db=db, orderbook_fn=lambda _t: _book(bid=50, ask=50))
        executor.submit(approved)

        # Now bid moved to 70¢ — unrealized P&L should be +200¢.
        executor2 = DryRunExecutor(db=db, orderbook_fn=lambda _t: _book(bid=70, ask=72))
        updated = executor2.update_position_marks()
        assert updated == 1
        row = (
            db.connect()
            .execute("SELECT unrealized_pnl_usd_cents FROM trades WHERE status = 'dry_run'")
            .fetchone()
        )
        assert row["unrealized_pnl_usd_cents"] == (70 - 50) * 10


# ---------------------------------------------------------------------------
# Resolution close
# ---------------------------------------------------------------------------


class TestCloseResolved:
    def test_yes_resolution_pays_full_dollar(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        match_id = _seed_match(db)
        intent = TradeIntent(
            ticker="X",
            target_price_cents=42,
            target_quantity=10,
            target_size_usd_cents=420,
            triggering_match_id=match_id,
            confirmation_weight=0.9,
            confidence_score=0.9,
            reasoning_text="r",
        )
        approved = _approve(db, intent)
        executor = DryRunExecutor(db=db, orderbook_fn=lambda _t: _book(bid=42, ask=42))
        executor.submit(approved)
        result = executor.close_resolved(ticker="X", resolution="settled_yes")
        assert result is not None
        assert result.fill_price_cents == 100
        assert result.realized_pnl_usd_cents == (100 - 42) * 10

    def test_no_resolution_pays_zero(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        match_id = _seed_match(db)
        intent = TradeIntent(
            ticker="X",
            target_price_cents=42,
            target_quantity=10,
            target_size_usd_cents=420,
            triggering_match_id=match_id,
            confirmation_weight=0.9,
            confidence_score=0.9,
            reasoning_text="r",
        )
        approved = _approve(db, intent)
        executor = DryRunExecutor(db=db, orderbook_fn=lambda _t: _book(bid=42, ask=42))
        executor.submit(approved)
        result = executor.close_resolved(ticker="X", resolution="settled_no")
        assert result is not None
        assert result.fill_price_cents == 0
        assert result.realized_pnl_usd_cents == -420
