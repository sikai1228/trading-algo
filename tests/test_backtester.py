"""Backtester tests — replays a fixture DB and asserts deterministic output."""

from __future__ import annotations

from pathlib import Path

from trumpbot.backtest.replay import Backtester
from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    MarketRow,
    NewsEventRow,
    PriceSnapshotRow,
    insert_news_event,
    insert_price_snapshot,
    upsert_market,
)


def _seed(tmp_path: Path) -> Path:
    db_path = tmp_path / "bt.db"
    db = Database(db_path)
    db.connect()
    upsert_market(
        db,
        MarketRow(
            ticker="X",
            series_ticker="KXTRUMPMEET",
            event_ticker="KXTRUMPMEET-26APR",
            title="t",
            subtitle=None,
            yes_sub_title=None,
            no_sub_title=None,
            subject="putin",
            subject_full_name="V P",
            resolution_rules="r",
            approved_sources=None,
            open_ts="2026-04-01T00:00:00Z",
            close_ts="2026-04-30T23:59:59Z",
            expected_expiration_ts=None,
            status="settled_yes",
            last_price_cents=100,
            volume=100,
            open_interest=50,
            raw_json=None,
        ),
    )
    eid = insert_news_event(
        db,
        NewsEventRow(
            source="ap_via_gnews",
            source_weight=1.0,
            is_kalshi_approved=True,
            headline="h",
            url="https://e.com/a",
            url_canonical="https://e.com/a",
            body_excerpt=None,
            author=None,
            raw_published_ts="2026-04-15T12:00:00Z",
            detected_ts="2026-04-15T12:00:01Z",
            has_photo=False,
            has_video=False,
            raw_data=None,
        ),
    )
    assert eid is not None
    # A price snapshot at 50¢ before the match, then a high-confidence match.
    insert_price_snapshot(
        db,
        PriceSnapshotRow(
            ticker="X",
            yes_bid_cents=49,
            yes_ask_cents=50,
            no_bid_cents=49,
            no_ask_cents=50,
            yes_bid_size=10,
            yes_ask_size=10,
            no_bid_size=10,
            no_ask_size=10,
            last_trade_price_cents=50,
            volume_24h=100,
            ts="2026-04-15T11:59:00Z",
            snapshot_reason="periodic",
        ),
    )
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO news_market_matches (news_event_id, ticker, confidence, "
            "matched_subject, match_reason, created_at) "
            "VALUES (?, 'X', 0.95, 'putin', 'direct', '2026-04-15T12:00:01Z')",
            (eid,),
        )
    db.close()
    return db_path


def test_backtester_uses_engine_and_settles_yes(tmp_path: Path) -> None:
    db_path = _seed(tmp_path)
    bt = Backtester(db_path=db_path, starting_bankroll_usd=500.0)
    result = bt.run(start_ts="2026-04-01T00:00:00Z", end_ts="2026-04-30T23:59:59Z")
    assert result.total_trades == 1
    trade = result.trade_log[0]
    # Settled YES — payoff = 100¢. Entry @ 50¢ — realized = +50¢ * qty.
    assert trade.exit_price_cents == 100
    assert trade.realized_pnl_usd_cents > 0
    assert result.win_rate == 1.0


def test_backtester_csv_output(tmp_path: Path) -> None:
    db_path = _seed(tmp_path)
    bt = Backtester(db_path=db_path)
    result = bt.run(start_ts="2026-04-01T00:00:00Z", end_ts="2026-04-30T23:59:59Z")
    out = tmp_path / "out.csv"
    bt.write_csv(result, out)
    text = out.read_text()
    # Header line + 1 trade row.
    assert "ticker,entered_at" in text
    assert text.count("\n") >= 1


def test_backtester_uses_same_decision_engine_class() -> None:
    """Pin: the backtester instantiates the production DecisionEngine
    (no parallel implementation). The Backtester._engine attribute
    is the same class as the production import."""
    from trumpbot.backtest.replay import Backtester as B
    from trumpbot.decision.engine import DecisionEngine as ProdEngine

    bt = B(db_path="/dev/null", starting_bankroll_usd=1.0)
    assert isinstance(bt._engine, ProdEngine)
