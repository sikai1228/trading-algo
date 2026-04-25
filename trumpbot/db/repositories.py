"""CRUD repositories for every table populated in Phase 1.

Each repository is a thin layer over parameterized SQL — no ORM, no
query builder. The schema in migrations/001_initial.sql is the source
of truth; these functions only insert/update/select.

All writes go through ``Database.transaction`` so callers can batch
multiple operations atomically. Single-write helpers wrap themselves
in their own transaction for convenience.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from trumpbot.db.connection import Database
from trumpbot.utils.timeutil import utcnow_iso

# ---------------------------------------------------------------------------
# Markets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketRow:
    ticker: str
    series_ticker: str
    event_ticker: str | None
    title: str
    subtitle: str | None
    yes_sub_title: str | None
    no_sub_title: str | None
    subject: str | None
    resolution_rules: str
    approved_sources: list[str] | None
    open_ts: str
    close_ts: str | None
    expected_expiration_ts: str | None
    status: str
    last_price_cents: int | None
    volume: int | None
    open_interest: int | None
    raw_json: dict[str, Any] | None = None


def upsert_market(db: Database, row: MarketRow) -> None:
    """Insert or update a market by ticker."""
    sql = """
    INSERT INTO markets (
        ticker, series_ticker, event_ticker, title, subtitle,
        yes_sub_title, no_sub_title, subject, resolution_rules,
        approved_sources, open_ts, close_ts, expected_expiration_ts,
        status, last_price_cents, volume, open_interest, raw_json,
        created_at, updated_at
    ) VALUES (
        :ticker, :series_ticker, :event_ticker, :title, :subtitle,
        :yes_sub_title, :no_sub_title, :subject, :resolution_rules,
        :approved_sources, :open_ts, :close_ts, :expected_expiration_ts,
        :status, :last_price_cents, :volume, :open_interest, :raw_json,
        :now, :now
    )
    ON CONFLICT(ticker) DO UPDATE SET
        series_ticker = excluded.series_ticker,
        event_ticker = excluded.event_ticker,
        title = excluded.title,
        subtitle = excluded.subtitle,
        yes_sub_title = excluded.yes_sub_title,
        no_sub_title = excluded.no_sub_title,
        subject = excluded.subject,
        resolution_rules = excluded.resolution_rules,
        approved_sources = excluded.approved_sources,
        open_ts = excluded.open_ts,
        close_ts = excluded.close_ts,
        expected_expiration_ts = excluded.expected_expiration_ts,
        status = excluded.status,
        last_price_cents = excluded.last_price_cents,
        volume = excluded.volume,
        open_interest = excluded.open_interest,
        raw_json = excluded.raw_json,
        updated_at = excluded.updated_at
    """
    params = {
        "ticker": row.ticker,
        "series_ticker": row.series_ticker,
        "event_ticker": row.event_ticker,
        "title": row.title,
        "subtitle": row.subtitle,
        "yes_sub_title": row.yes_sub_title,
        "no_sub_title": row.no_sub_title,
        "subject": row.subject,
        "resolution_rules": row.resolution_rules,
        "approved_sources": json.dumps(row.approved_sources) if row.approved_sources else None,
        "open_ts": row.open_ts,
        "close_ts": row.close_ts,
        "expected_expiration_ts": row.expected_expiration_ts,
        "status": row.status,
        "last_price_cents": row.last_price_cents,
        "volume": row.volume,
        "open_interest": row.open_interest,
        "raw_json": json.dumps(row.raw_json) if row.raw_json else None,
        "now": utcnow_iso(),
    }
    with db.transaction() as conn:
        conn.execute(sql, params)


def list_active_markets(db: Database) -> list[sqlite3.Row]:
    conn = db.connect()
    return list(conn.execute("SELECT * FROM markets WHERE status = 'active' ORDER BY ticker"))


def get_market(db: Database, ticker: str) -> sqlite3.Row | None:
    conn = db.connect()
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM markets WHERE ticker = ?", (ticker,)
    ).fetchone()
    return row


def update_market_status(db: Database, ticker: str, status: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE markets SET status = ?, updated_at = ? WHERE ticker = ?",
            (status, utcnow_iso(), ticker),
        )


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PriceSnapshotRow:
    ticker: str
    yes_bid_cents: int | None
    yes_ask_cents: int | None
    no_bid_cents: int | None
    no_ask_cents: int | None
    yes_bid_size: int | None
    yes_ask_size: int | None
    no_bid_size: int | None
    no_ask_size: int | None
    last_trade_price_cents: int | None
    volume_24h: int | None
    ts: str
    snapshot_reason: str  # 'periodic' | 'price_change' | 'reconnect'


def insert_price_snapshot(db: Database, row: PriceSnapshotRow) -> None:
    sql = """
    INSERT INTO price_snapshots (
        ticker, yes_bid_cents, yes_ask_cents, no_bid_cents, no_ask_cents,
        yes_bid_size, yes_ask_size, no_bid_size, no_ask_size,
        last_trade_price_cents, volume_24h, ts, snapshot_reason
    ) VALUES (
        :ticker, :yes_bid_cents, :yes_ask_cents, :no_bid_cents, :no_ask_cents,
        :yes_bid_size, :yes_ask_size, :no_bid_size, :no_ask_size,
        :last_trade_price_cents, :volume_24h, :ts, :snapshot_reason
    )
    """
    with db.transaction() as conn:
        conn.execute(sql, row.__dict__)


@dataclass(frozen=True)
class OrderbookSnapshotRow:
    ticker: str
    yes_levels: list[tuple[int, int]]
    no_levels: list[tuple[int, int]]
    ts: str


def insert_orderbook_snapshot(db: Database, row: OrderbookSnapshotRow) -> None:
    sql = """
    INSERT INTO orderbook_snapshots (ticker, yes_levels, no_levels, ts)
    VALUES (?, ?, ?, ?)
    """
    with db.transaction() as conn:
        conn.execute(
            sql,
            (
                row.ticker,
                json.dumps(row.yes_levels),
                json.dumps(row.no_levels),
                row.ts,
            ),
        )


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NewsEventRow:
    source: str
    source_weight: float
    is_kalshi_approved: bool
    headline: str
    url: str | None
    url_canonical: str | None
    body_excerpt: str | None
    author: str | None
    raw_published_ts: str | None
    detected_ts: str
    has_photo: bool
    has_video: bool
    raw_data: dict[str, Any] | None


def insert_news_event(db: Database, row: NewsEventRow) -> int | None:
    """Insert a news event. Returns lastrowid, or None on URL collision."""
    sql = """
    INSERT INTO news_events (
        source, source_weight, is_kalshi_approved, headline, url,
        url_canonical, body_excerpt, author, raw_published_ts,
        detected_ts, has_photo, has_video, raw_data
    ) VALUES (
        :source, :source_weight, :is_kalshi_approved, :headline, :url,
        :url_canonical, :body_excerpt, :author, :raw_published_ts,
        :detected_ts, :has_photo, :has_video, :raw_data
    )
    """
    params = {
        "source": row.source,
        "source_weight": row.source_weight,
        "is_kalshi_approved": int(row.is_kalshi_approved),
        "headline": row.headline,
        "url": row.url,
        "url_canonical": row.url_canonical,
        "body_excerpt": row.body_excerpt,
        "author": row.author,
        "raw_published_ts": row.raw_published_ts,
        "detected_ts": row.detected_ts,
        "has_photo": int(row.has_photo),
        "has_video": int(row.has_video),
        "raw_data": json.dumps(row.raw_data) if row.raw_data else None,
    }
    # The unique index on url_canonical is partial (only enforced when
    # url_canonical IS NOT NULL); SQLite cannot resolve ON CONFLICT
    # against a partial index, so we catch IntegrityError ourselves.
    try:
        with db.transaction() as conn:
            cur = conn.execute(sql, params)
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def fetch_news_event(db: Database, news_event_id: int) -> sqlite3.Row | None:
    conn = db.connect()
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM news_events WHERE id = ?", (news_event_id,)
    ).fetchone()
    return row


def fetch_news_events_without_matches(db: Database, limit: int = 100) -> list[sqlite3.Row]:
    """News events that have not yet been processed by the matcher."""
    conn = db.connect()
    return list(
        conn.execute(
            """
            SELECT n.*
            FROM news_events n
            LEFT JOIN news_market_matches m ON m.news_event_id = n.id
            WHERE m.id IS NULL
            ORDER BY n.id ASC
            LIMIT ?
            """,
            (limit,),
        )
    )


def recent_news_events(db: Database, limit: int = 50) -> list[sqlite3.Row]:
    conn = db.connect()
    return list(
        conn.execute(
            "SELECT * FROM news_events ORDER BY detected_ts DESC LIMIT ?",
            (limit,),
        )
    )


@dataclass(frozen=True)
class NewsMatchRow:
    news_event_id: int
    ticker: str
    confidence: float
    matched_subject: str | None
    matched_keywords: list[str] | None
    match_reason: str | None


def insert_news_matches(db: Database, rows: Iterable[NewsMatchRow]) -> None:
    """Insert a batch of matcher outputs in a single transaction."""
    sql = """
    INSERT INTO news_market_matches (
        news_event_id, ticker, confidence, matched_subject,
        matched_keywords, match_reason
    ) VALUES (
        :news_event_id, :ticker, :confidence, :matched_subject,
        :matched_keywords, :match_reason
    )
    """
    payload = [
        {
            "news_event_id": r.news_event_id,
            "ticker": r.ticker,
            "confidence": r.confidence,
            "matched_subject": r.matched_subject,
            "matched_keywords": json.dumps(r.matched_keywords) if r.matched_keywords else None,
            "match_reason": r.match_reason,
        }
        for r in rows
    ]
    if not payload:
        return
    with db.transaction() as conn:
        conn.executemany(sql, payload)


def recent_high_confidence_matches(
    db: Database, *, min_confidence: float = 0.5, limit: int = 50
) -> list[sqlite3.Row]:
    conn = db.connect()
    return list(
        conn.execute(
            """
            SELECT m.*, n.headline, n.source, n.url
            FROM news_market_matches m
            JOIN news_events n ON n.id = m.news_event_id
            WHERE m.confidence >= ?
            ORDER BY m.created_at DESC
            LIMIT ?
            """,
            (min_confidence, limit),
        )
    )


# ---------------------------------------------------------------------------
# System events
# ---------------------------------------------------------------------------


def insert_system_event(
    db: Database,
    *,
    event_type: str,
    severity: str,
    component: str,
    message: str,
    detail: dict[str, Any] | None = None,
) -> None:
    sql = """
    INSERT INTO system_events (event_type, severity, component, message, detail, ts)
    VALUES (?, ?, ?, ?, ?, ?)
    """
    with db.transaction() as conn:
        conn.execute(
            sql,
            (
                event_type,
                severity,
                component,
                message,
                json.dumps(detail) if detail else None,
                utcnow_iso(),
            ),
        )
