"""Read-only query API for the future observability backend.

Per CLAUDE.md "Read-Only Query API": this module is the contract a future
FastAPI dashboard wraps over HTTP. It depends only on the SQLite schema
(via :mod:`sqlite3`) and Pydantic — no other ``trumpbot.*`` modules.

In Phase 1, the trade-related queries return empty results (the
``trades`` table is defined but unpopulated until Phase 4). Market and
news queries return real data once the daemon has been running.

Every function takes either a ``Database`` object or a SQLite path so the
backend can attach to the same database file (or a litestream replica)
without sharing the daemon's connection.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class _BaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Pydantic return types
# ---------------------------------------------------------------------------


class Position(_BaseModel):
    trade_id: int
    ticker: str
    side: str
    quantity: int
    entry_price_cents: int
    current_market_value_cents: int | None = None
    unrealized_pnl_cents: int | None = None
    submitted_at: str
    filled_at: str | None = None


class NewsEvidence(_BaseModel):
    news_event_id: int
    role: str  # 'trigger' | 'confirmation' | etc.
    source: str
    source_weight: float
    headline: str
    url: str | None
    detected_ts: str


class PriceSnapshot(_BaseModel):
    ts: str
    yes_bid_cents: int | None
    yes_ask_cents: int | None
    no_bid_cents: int | None
    no_ask_cents: int | None
    last_trade_price_cents: int | None
    snapshot_reason: str


class MarketHistory(_BaseModel):
    ticker: str
    start_ts: str
    end_ts: str
    snapshots: list[PriceSnapshot] = Field(default_factory=list)


class PnLBreakdown(_BaseModel):
    trade_id: int
    realized_pnl_cents: int
    unrealized_pnl_cents: int
    fill_price_cents: int | None
    current_market_value_cents: int | None


class DailyPnL(_BaseModel):
    day: str  # YYYY-MM-DD
    realized_pnl_cents: int
    unrealized_pnl_cents: int
    trade_count: int


class PerformanceMetrics(_BaseModel):
    start_ts: str
    end_ts: str
    total_trades: int
    win_count: int
    loss_count: int
    realized_pnl_cents: int
    unrealized_pnl_cents: int


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------


@contextmanager
def _open_readonly(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """Open the SQLite database in read-only mode.

    Uses the URI form (``mode=ro``) so the future backend can attach to a
    litestream replica without locking out the writer.
    """
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def get_open_positions(db_path: Path | str) -> list[Position]:
    """Trades currently open (status='open' or 'partially_filled')."""
    with _open_readonly(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, ticker, side, requested_quantity, target_price_cents,
                   current_market_value, unrealized_pnl_cents,
                   submitted_at, filled_at
            FROM trades
            WHERE status IN ('open', 'partially_filled')
            ORDER BY submitted_at DESC
            """
        ).fetchall()
    return [
        Position(
            trade_id=r["id"],
            ticker=r["ticker"],
            side=r["side"],
            quantity=r["requested_quantity"],
            entry_price_cents=r["target_price_cents"],
            current_market_value_cents=r["current_market_value"],
            unrealized_pnl_cents=r["unrealized_pnl_cents"],
            submitted_at=r["submitted_at"] or "",
            filled_at=r["filled_at"],
        )
        for r in rows
    ]


def get_position_pnl(db_path: Path | str, trade_id: int) -> PnLBreakdown | None:
    with _open_readonly(db_path) as conn:
        r = conn.execute(
            """
            SELECT id, realized_pnl_cents, unrealized_pnl_cents,
                   fill_price_cents, current_market_value
            FROM trades
            WHERE id = ?
            """,
            (trade_id,),
        ).fetchone()
    if r is None:
        return None
    return PnLBreakdown(
        trade_id=r["id"],
        realized_pnl_cents=r["realized_pnl_cents"] or 0,
        unrealized_pnl_cents=r["unrealized_pnl_cents"] or 0,
        fill_price_cents=r["fill_price_cents"],
        current_market_value_cents=r["current_market_value"],
    )


def get_trade_evidence(db_path: Path | str, trade_id: int) -> list[NewsEvidence]:
    """News events linked to a trade via trade_news_links."""
    with _open_readonly(db_path) as conn:
        rows = conn.execute(
            """
            SELECT n.id AS news_event_id, l.role, n.source, n.source_weight,
                   n.headline, n.url, n.detected_ts
            FROM trade_news_links l
            JOIN news_events n ON n.id = l.news_event_id
            WHERE l.trade_id = ?
            ORDER BY n.detected_ts ASC
            """,
            (trade_id,),
        ).fetchall()
    return [
        NewsEvidence(
            news_event_id=r["news_event_id"],
            role=r["role"],
            source=r["source"],
            source_weight=r["source_weight"],
            headline=r["headline"],
            url=r["url"],
            detected_ts=r["detected_ts"],
        )
        for r in rows
    ]


def get_market_history(
    db_path: Path | str, ticker: str, start_ts: str, end_ts: str
) -> MarketHistory:
    """Price snapshots for ``ticker`` between ``start_ts`` and ``end_ts``."""
    with _open_readonly(db_path) as conn:
        rows = conn.execute(
            """
            SELECT ts, yes_bid_cents, yes_ask_cents, no_bid_cents, no_ask_cents,
                   last_trade_price_cents, snapshot_reason
            FROM price_snapshots
            WHERE ticker = ? AND ts >= ? AND ts <= ?
            ORDER BY ts ASC
            """,
            (ticker, start_ts, end_ts),
        ).fetchall()
    snapshots = [
        PriceSnapshot(
            ts=r["ts"],
            yes_bid_cents=r["yes_bid_cents"],
            yes_ask_cents=r["yes_ask_cents"],
            no_bid_cents=r["no_bid_cents"],
            no_ask_cents=r["no_ask_cents"],
            last_trade_price_cents=r["last_trade_price_cents"],
            snapshot_reason=r["snapshot_reason"],
        )
        for r in rows
    ]
    return MarketHistory(ticker=ticker, start_ts=start_ts, end_ts=end_ts, snapshots=snapshots)


def get_daily_pnl(db_path: Path | str, day: date) -> DailyPnL:
    """Aggregate realized + unrealized PnL for a single UTC day."""
    day_str = day.isoformat()
    with _open_readonly(db_path) as conn:
        r = conn.execute(
            """
            SELECT
              COALESCE(SUM(realized_pnl_cents), 0) AS realized,
              COALESCE(SUM(unrealized_pnl_cents), 0) AS unrealized,
              COUNT(*) AS trade_count
            FROM trades
            WHERE substr(submitted_at, 1, 10) = ?
            """,
            (day_str,),
        ).fetchone()
    return DailyPnL(
        day=day_str,
        realized_pnl_cents=r["realized"] or 0,
        unrealized_pnl_cents=r["unrealized"] or 0,
        trade_count=r["trade_count"] or 0,
    )


def get_strategy_performance(db_path: Path | str, start_ts: str, end_ts: str) -> PerformanceMetrics:
    """Aggregate performance over a window."""
    with _open_readonly(db_path) as conn:
        r = conn.execute(
            """
            SELECT
              COUNT(*) AS total_trades,
              COALESCE(SUM(CASE WHEN realized_pnl_cents > 0 THEN 1 ELSE 0 END), 0) AS wins,
              COALESCE(SUM(CASE WHEN realized_pnl_cents < 0 THEN 1 ELSE 0 END), 0) AS losses,
              COALESCE(SUM(realized_pnl_cents), 0) AS realized,
              COALESCE(SUM(unrealized_pnl_cents), 0) AS unrealized
            FROM trades
            WHERE submitted_at >= ? AND submitted_at <= ?
            """,
            (start_ts, end_ts),
        ).fetchone()
    return PerformanceMetrics(
        start_ts=start_ts,
        end_ts=end_ts,
        total_trades=r["total_trades"] or 0,
        win_count=r["wins"] or 0,
        loss_count=r["losses"] or 0,
        realized_pnl_cents=r["realized"] or 0,
        unrealized_pnl_cents=r["unrealized"] or 0,
    )


__all__ = [
    "DailyPnL",
    "MarketHistory",
    "NewsEvidence",
    "PerformanceMetrics",
    "PnLBreakdown",
    "Position",
    "PriceSnapshot",
    "get_daily_pnl",
    "get_market_history",
    "get_open_positions",
    "get_position_pnl",
    "get_strategy_performance",
    "get_trade_evidence",
]
