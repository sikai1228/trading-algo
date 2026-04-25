"""Tests for the read-only queries module (future-backend integration)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    MarketRow,
    PriceSnapshotRow,
    insert_price_snapshot,
    upsert_market,
)
from trumpbot.queries import (
    DailyPnL,
    MarketHistory,
    PerformanceMetrics,
    get_daily_pnl,
    get_market_history,
    get_open_positions,
    get_position_pnl,
    get_strategy_performance,
    get_trade_evidence,
)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    db = Database(tmp_path / "queries.db")
    db.connect()
    db.close()
    return tmp_path / "queries.db"


@pytest.fixture()
def seeded_db(tmp_path: Path) -> Path:
    """Database with one market and a handful of price snapshots."""
    p = tmp_path / "seeded.db"
    db = Database(p)
    db.connect()
    upsert_market(
        db,
        MarketRow(
            ticker="T1",
            series_ticker="S",
            event_ticker=None,
            title="t",
            subtitle=None,
            yes_sub_title=None,
            no_sub_title=None,
            subject="putin",
            resolution_rules="r",
            approved_sources=None,
            open_ts="2026-01-01T00:00:00.000000Z",
            close_ts=None,
            expected_expiration_ts=None,
            status="active",
            last_price_cents=None,
            volume=None,
            open_interest=None,
            raw_json=None,
        ),
    )
    for i in range(5):
        insert_price_snapshot(
            db,
            PriceSnapshotRow(
                ticker="T1",
                yes_bid_cents=50 + i,
                yes_ask_cents=52 + i,
                no_bid_cents=48 - i,
                no_ask_cents=50 - i,
                yes_bid_size=10,
                yes_ask_size=10,
                no_bid_size=10,
                no_ask_size=10,
                last_trade_price_cents=51 + i,
                volume_24h=100,
                ts=f"2026-04-25T12:00:0{i}.000000Z",
                snapshot_reason="periodic",
            ),
        )
    db.close()
    return p


class TestPydanticReturns:
    def test_market_history_returns_pydantic(self, seeded_db: Path) -> None:
        out = get_market_history(seeded_db, "T1", "2026-04-25T00:00:00Z", "2026-04-25T23:59:59Z")
        assert isinstance(out, MarketHistory)
        assert len(out.snapshots) == 5
        assert out.snapshots[0].snapshot_reason == "periodic"

    def test_market_history_empty_window(self, seeded_db: Path) -> None:
        out = get_market_history(seeded_db, "T1", "2027-01-01T00:00:00Z", "2027-12-31T23:59:59Z")
        assert out.snapshots == []


class TestEmptyTrades:
    """Phase 1 has no trades. Trade-related queries must return empty results."""

    def test_open_positions_empty(self, db_path: Path) -> None:
        assert get_open_positions(db_path) == []

    def test_position_pnl_missing_returns_none(self, db_path: Path) -> None:
        assert get_position_pnl(db_path, trade_id=999) is None

    def test_trade_evidence_empty(self, db_path: Path) -> None:
        assert get_trade_evidence(db_path, trade_id=999) == []

    def test_daily_pnl_zero(self, db_path: Path) -> None:
        out = get_daily_pnl(db_path, date(2026, 4, 25))
        assert isinstance(out, DailyPnL)
        assert out.realized_pnl_cents == 0
        assert out.trade_count == 0

    def test_strategy_performance_zero(self, db_path: Path) -> None:
        out = get_strategy_performance(db_path, "2026-01-01T00:00:00Z", "2026-12-31T23:59:59Z")
        assert isinstance(out, PerformanceMetrics)
        assert out.total_trades == 0
        assert out.win_count == 0


class TestReadOnlyConcurrency:
    """The future backend reads from the same SQLite file while the daemon writes.

    With WAL mode + ``mode=ro`` URI, this should not block.
    """

    def test_concurrent_read_during_write(self, tmp_path: Path) -> None:
        p = tmp_path / "concurrent.db"
        writer = Database(p)
        writer_conn = writer.connect()
        # Insert via the writer
        upsert_market(
            writer,
            MarketRow(
                ticker="X",
                series_ticker="S",
                event_ticker=None,
                title="t",
                subtitle=None,
                yes_sub_title=None,
                no_sub_title=None,
                subject="putin",
                resolution_rules="r",
                approved_sources=None,
                open_ts="2026-01-01T00:00:00.000000Z",
                close_ts=None,
                expected_expiration_ts=None,
                status="active",
                last_price_cents=None,
                volume=None,
                open_interest=None,
                raw_json=None,
            ),
        )
        # Open a separate read-only connection (simulates future backend)
        out = get_market_history(p, "X", "2025-01-01Z", "2030-01-01Z")
        # The reader should see the database (no error). Snapshots are
        # empty (none inserted) but the call should complete.
        assert out.ticker == "X"
        # Ensure the writer is still healthy
        writer_conn.execute("SELECT 1").fetchone()
        writer.close()
