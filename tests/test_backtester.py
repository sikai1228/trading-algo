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
    # A price snapshot at 50c before the match, then a high-confidence match.
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
    # Settled YES — payoff = 100c. Entry @ 50c — realized = +50c * qty.
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


def test_backtester_uses_same_risk_manager_class() -> None:
    """Pin: the backtester also runs the production RiskManager (no
    shadow logic for sizing caps / exposure caps), with db=None so the
    audit table isn't polluted from a backtest run."""
    from trumpbot.backtest.replay import Backtester as B
    from trumpbot.risk.manager import RiskManager as ProdRisk

    bt = B(db_path="/dev/null", starting_bankroll_usd=1.0)
    assert isinstance(bt._risk, ProdRisk)
    # Read-only mode: no DB handle.
    assert bt._risk._db is None  # pinning the contract


def test_backtester_skips_risk_rejected_intents(tmp_path: Path) -> None:
    """When the risk manager rejects (here: halted=True), the backtester
    does NOT record a trade and increments risk_rejections.

    Using ``halted`` is the cleanest way to force a risk-only rejection
    in test, because most other rejection causes (price ceiling, sizing
    floor) are also enforced inside the engine and would short-circuit
    before the risk gate ever runs.
    """
    from trumpbot.risk.manager import RiskConfig

    db_path = _seed(tmp_path)
    bt = Backtester(
        db_path=db_path,
        starting_bankroll_usd=500.0,
        risk_config=RiskConfig(halted=True),
    )
    result = bt.run(start_ts="2026-04-01T00:00:00Z", end_ts="2026-04-30T23:59:59Z")
    assert result.total_trades == 0
    assert result.risk_rejections == 1


def test_backtester_emits_sharpe_and_max_drawdown(tmp_path: Path) -> None:
    """BacktestResult exposes sharpe_ratio and max_drawdown_usd_cents
    fields (zero for the trivial single-trade fixture)."""
    db_path = _seed(tmp_path)
    bt = Backtester(db_path=db_path, starting_bankroll_usd=500.0)
    result = bt.run(start_ts="2026-04-01T00:00:00Z", end_ts="2026-04-30T23:59:59Z")
    # Single positive-P&L trade -> variance is zero -> Sharpe is 0.0.
    assert result.sharpe_ratio == 0.0
    # Single-trade equity curve only goes up -> max drawdown is 0.
    assert result.max_drawdown_usd_cents == 0


def test_backtester_populates_by_source_breakdown(tmp_path: Path) -> None:
    """by_source_breakdown is populated from news_events.source via the
    JOIN in _fetch_matches; counts and P&L sums match by_subject."""
    db_path = _seed(tmp_path)
    bt = Backtester(db_path=db_path, starting_bankroll_usd=500.0)
    result = bt.run(start_ts="2026-04-01T00:00:00Z", end_ts="2026-04-30T23:59:59Z")
    assert result.by_source  # not empty
    # The fixture's only news_event has source='ap_via_gnews'.
    assert "ap_via_gnews" in result.by_source
    assert result.by_source["ap_via_gnews"]["trades"] == 1
    # Sum across sources equals total_trades.
    assert sum(v["trades"] for v in result.by_source.values()) == result.total_trades
    # Sum of P&L across sources equals total realized.
    assert (
        sum(v["realized_pnl_usd_cents"] for v in result.by_source.values())
        == result.total_realized_pnl_usd_cents
    )


def test_max_drawdown_helper_handles_peak_and_trough() -> None:
    """Equity curve +100, +50, -200 -> peak 150, trough -50, max
    drawdown 200 from the running max."""
    from trumpbot.backtest.replay import BacktestTrade, _max_drawdown_usd_cents

    trades = [
        BacktestTrade(
            ticker="X",
            entered_at="2026-04-01T00:00:00Z",
            exit_at=f"2026-04-0{i+1}T00:00:00Z",
            entry_price_cents=50,
            exit_price_cents=50,
            quantity=1,
            realized_pnl_usd_cents=p,
        )
        for i, p in enumerate([100, 50, -200])
    ]
    # Cumulative: 100, 150, -50. Peak 150. Trough -50. Drawdown 200.
    assert _max_drawdown_usd_cents(trades) == 200


def test_sharpe_helper_handles_no_variance() -> None:
    """Sharpe is 0.0 when the daily P&L series has zero variance."""
    from trumpbot.backtest.replay import BacktestTrade, _annualized_sharpe

    same = [
        BacktestTrade(
            ticker="X",
            entered_at="2026-04-01T00:00:00Z",
            exit_at=f"2026-04-0{i+1}T00:00:00Z",
            entry_price_cents=50,
            exit_price_cents=60,
            quantity=1,
            realized_pnl_usd_cents=10,
        )
        for i in range(3)
    ]
    assert _annualized_sharpe(same) == 0.0
