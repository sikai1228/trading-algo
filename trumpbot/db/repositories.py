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
    subject_full_name: str | None = None


def upsert_market(db: Database, row: MarketRow) -> None:
    """Insert or update a market by ticker.

    The discovery service must NOT call this for an existing market with
    a non-empty ``resolution_rules`` field — those rules are the
    contract for resolution and must be frozen at first write. Use
    :func:`update_market_market_data` for routine price/status updates,
    and gate ``upsert_market`` on
    :func:`market_resolution_unchanged` for safety.
    """
    sql = """
    INSERT INTO markets (
        ticker, series_ticker, event_ticker, title, subtitle,
        yes_sub_title, no_sub_title, subject, subject_full_name,
        resolution_rules, approved_sources, open_ts, close_ts,
        expected_expiration_ts, status, last_price_cents, volume,
        open_interest, raw_json, created_at, updated_at
    ) VALUES (
        :ticker, :series_ticker, :event_ticker, :title, :subtitle,
        :yes_sub_title, :no_sub_title, :subject, :subject_full_name,
        :resolution_rules, :approved_sources, :open_ts, :close_ts,
        :expected_expiration_ts, :status, :last_price_cents, :volume,
        :open_interest, :raw_json, :now, :now
    )
    ON CONFLICT(ticker) DO UPDATE SET
        series_ticker = excluded.series_ticker,
        event_ticker = excluded.event_ticker,
        title = excluded.title,
        subtitle = excluded.subtitle,
        yes_sub_title = excluded.yes_sub_title,
        no_sub_title = excluded.no_sub_title,
        subject = excluded.subject,
        subject_full_name = excluded.subject_full_name,
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
        "subject_full_name": row.subject_full_name,
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


def update_market_market_data(
    db: Database,
    *,
    ticker: str,
    last_price_cents: int | None,
    volume: int | None,
    open_interest: int | None,
    status: str | None,
) -> None:
    """Update only the price/status/volume fields on an existing market.

    Never touches ``title`` or ``resolution_rules`` — those are the
    contract for the market and are frozen at first insert.
    """
    fields = []
    params: dict[str, Any] = {"ticker": ticker, "now": utcnow_iso()}
    if last_price_cents is not None:
        fields.append("last_price_cents = :last_price_cents")
        params["last_price_cents"] = last_price_cents
    if volume is not None:
        fields.append("volume = :volume")
        params["volume"] = volume
    if open_interest is not None:
        fields.append("open_interest = :open_interest")
        params["open_interest"] = open_interest
    if status is not None:
        fields.append("status = :status")
        params["status"] = status
    if not fields:
        return
    fields.append("updated_at = :now")
    sql = f"UPDATE markets SET {', '.join(fields)} WHERE ticker = :ticker"
    with db.transaction() as conn:
        conn.execute(sql, params)


def market_resolution_unchanged(
    db: Database, *, ticker: str, title: str, resolution_rules: str
) -> bool | None:
    """Compare existing title + resolution_rules against incoming values.

    Returns:
        - ``True`` if the existing record matches both fields exactly.
        - ``False`` if the existing record exists but at least one
          field differs (the discovery service must NOT auto-update;
          fire a critical alert for human review).
        - ``None`` if no existing record (the caller is free to insert).
    """
    existing = get_market(db, ticker)
    if existing is None:
        return None
    return bool(existing["title"] == title and existing["resolution_rules"] == resolution_rules)


def list_active_markets(db: Database) -> list[sqlite3.Row]:
    conn = db.connect()
    return list(conn.execute("SELECT * FROM markets WHERE status = 'active' ORDER BY ticker"))


def list_markets_for_matching(db: Database) -> list[sqlite3.Row]:
    """Markets the matcher should evaluate news against.

    Distinct from :func:`list_active_markets` (which is the right query
    for trading): the matcher includes ``settled`` and ``finalized``
    markets too. During the observation period we want to know whether
    the matcher *would have* produced a signal for a market that has
    already resolved — that's how we calibrate matcher quality. The
    decision-engine layer in Phase 2 filters back down to ``active``
    before any trading.
    """
    conn = db.connect()
    return list(conn.execute("SELECT * FROM markets WHERE subject IS NOT NULL ORDER BY ticker"))


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
# Subjects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubjectRow:
    """One row in the ``subjects`` table.

    The discovery service writes ``auto_extracted=True``,
    ``llm_enriched=False``, ``reviewed=False`` for every new subject
    it discovers from a market title. Phase 2 will flip
    ``llm_enriched`` after running enrichment, and a human review pass
    flips ``reviewed``.
    """

    subject_key: str
    full_name: str
    aliases: list[str]
    ticker_suffix: str | None = None
    auto_extracted: bool = True
    llm_enriched: bool = False
    reviewed: bool = False


def upsert_subject(db: Database, row: SubjectRow) -> None:
    """Insert or merge a subject row.

    Conflict resolution preserves operator intent: ``llm_enriched``,
    ``reviewed``, and ``ticker_suffix`` are NOT overwritten by a
    subsequent auto-discovery; ``aliases`` is unioned with the existing
    list (so adding a fresh extraction never *removes* a manually-
    added alias).
    """
    sql = """
    INSERT INTO subjects (
        subject_key, full_name, aliases, ticker_suffix,
        auto_extracted, llm_enriched, reviewed, created_at, updated_at
    ) VALUES (
        :subject_key, :full_name, :aliases, :ticker_suffix,
        :auto_extracted, :llm_enriched, :reviewed, :now, :now
    )
    ON CONFLICT(subject_key) DO UPDATE SET
        full_name = excluded.full_name,
        aliases = (
            SELECT json_group_array(value)
            FROM (
                SELECT value FROM json_each(subjects.aliases)
                UNION
                SELECT value FROM json_each(excluded.aliases)
            )
        ),
        auto_extracted = subjects.auto_extracted OR excluded.auto_extracted,
        updated_at = excluded.updated_at
    """
    params = {
        "subject_key": row.subject_key,
        "full_name": row.full_name,
        "aliases": json.dumps(row.aliases),
        "ticker_suffix": row.ticker_suffix,
        "auto_extracted": int(row.auto_extracted),
        "llm_enriched": int(row.llm_enriched),
        "reviewed": int(row.reviewed),
        "now": utcnow_iso(),
    }
    with db.transaction() as conn:
        conn.execute(sql, params)


def get_subject(db: Database, subject_key: str) -> sqlite3.Row | None:
    conn = db.connect()
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM subjects WHERE subject_key = ?", (subject_key,)
    ).fetchone()
    return row


def list_subjects(db: Database) -> list[sqlite3.Row]:
    """All rows in the subjects table.

    Used by the matcher worker each batch to build a
    ``{subject_key: [aliases]}`` dict that mirrors the discovery
    service's view of who's currently in the markets. Cheap query
    against a small table (~22 rows in Phase 1).
    """
    conn = db.connect()
    return list(conn.execute("SELECT * FROM subjects ORDER BY subject_key"))


def subjects_alias_map(db: Database) -> dict[str, list[str]]:
    """Return ``{subject_key: aliases_list}`` ready to feed a SubjectExtractor."""
    out: dict[str, list[str]] = {}
    for row in list_subjects(db):
        try:
            aliases = json.loads(row["aliases"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(aliases, list):
            continue
        cleaned = [str(a) for a in aliases if isinstance(a, str) and a]
        if cleaned:
            out[row["subject_key"]] = cleaned
    return out


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


# ---------------------------------------------------------------------------
# Phase 2: trades, risk_decisions, telegram_approvals
# ---------------------------------------------------------------------------


def insert_risk_decision(
    db: Database,
    *,
    intent_type: str,
    intent_json: str,
    decision: str,
    rejection_reason: str | None,
    rule_fired: str | None,
    reasoning_text: str,
) -> int:
    sql = """
    INSERT INTO risk_decisions (
        intent_type, intent_json, decision, rejection_reason,
        rule_fired, reasoning_text, decided_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """
    with db.transaction() as conn:
        cur = conn.execute(
            sql,
            (
                intent_type,
                intent_json,
                decision,
                rejection_reason,
                rule_fired,
                reasoning_text,
                utcnow_iso(),
            ),
        )
        last_id: int | None = cur.lastrowid
        assert last_id is not None
        return last_id


def insert_telegram_approval(
    db: Database,
    *,
    intent_type: str,
    intent_json: str,
    message_text: str,
    chat_id: str | None,
    expires_at: str | None,
) -> int:
    sql = """
    INSERT INTO telegram_approvals (
        intent_type, intent_json, message_text,
        telegram_chat_id, expires_at, created_at
    ) VALUES (?, ?, ?, ?, ?, ?)
    """
    with db.transaction() as conn:
        cur = conn.execute(
            sql,
            (
                intent_type,
                intent_json,
                message_text,
                chat_id,
                expires_at,
                utcnow_iso(),
            ),
        )
        last_id: int | None = cur.lastrowid
        assert last_id is not None
        return last_id


def update_telegram_approval(
    db: Database,
    *,
    approval_id: int,
    decision: str,
    decision_source: str,
    telegram_message_id: int | None = None,
) -> None:
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE telegram_approvals
               SET decision = ?, decision_source = ?, decided_at = ?,
                   telegram_message_id = COALESCE(?, telegram_message_id)
             WHERE id = ?
            """,
            (decision, decision_source, utcnow_iso(), telegram_message_id, approval_id),
        )


@dataclass(frozen=True)
class TradeInsertRow:
    ticker: str
    status: str
    entry_price_cents: int
    quantity: int
    cost_basis_usd_cents: int
    triggering_match_id: int
    triggering_intent_json: str
    risk_decision_id: int
    approval_id: int | None
    is_reentry: bool
    prior_trade_id: int | None
    reasoning_text: str
    entered_at: str

    # Phase 3 Part 1 — order-book-walk + fees + FOK audit columns
    # (added by migration 005). All optional so existing call sites
    # that don't yet emit them keep working.
    cap_binding: str | None = None
    """One of 'cap_one' / 'cap_two' / 'tie' — which cap was active
    in the engine's sizing decision."""

    cap_one_value_cents: int | None = None
    cap_two_value_cents: int | None = None
    target_avg_fill_price_cents: int | None = None
    """The avg fill price the engine's walk predicted (decision time)."""

    actual_avg_fill_price_cents: int | None = None
    """The avg fill price the executor's re-walk produced
    (submission time). Equal to target when the book was stable."""

    slippage_cents: int | None = None
    entry_fees_cents: int | None = None
    levels_consumed_json: str | None = None
    """JSON-encoded list of [price_cents, qty] pairs the executor's
    re-walk actually consumed."""


def insert_trade(db: Database, row: TradeInsertRow) -> int:
    sql = """
    INSERT INTO trades (
        ticker, side, action, status,
        entry_price_cents, quantity, cost_basis_usd_cents,
        triggering_match_id, triggering_intent_json,
        risk_decision_id, approval_id, is_reentry, prior_trade_id,
        reasoning_text, entered_at, created_at,
        cap_binding, cap_one_value_cents, cap_two_value_cents,
        target_avg_fill_price_cents, actual_avg_fill_price_cents,
        slippage_cents, entry_fees_cents, levels_consumed_json
    ) VALUES (
        :ticker, 'yes', 'buy', :status,
        :entry_price_cents, :quantity, :cost_basis_usd_cents,
        :triggering_match_id, :triggering_intent_json,
        :risk_decision_id, :approval_id, :is_reentry, :prior_trade_id,
        :reasoning_text, :entered_at, :now,
        :cap_binding, :cap_one_value_cents, :cap_two_value_cents,
        :target_avg_fill_price_cents, :actual_avg_fill_price_cents,
        :slippage_cents, :entry_fees_cents, :levels_consumed_json
    )
    """
    params = {
        **row.__dict__,
        "is_reentry": int(row.is_reentry),
        "now": utcnow_iso(),
    }
    with db.transaction() as conn:
        cur = conn.execute(sql, params)
        last_id: int | None = cur.lastrowid
        assert last_id is not None
        return last_id


def list_open_trades(db: Database) -> list[sqlite3.Row]:
    conn = db.connect()
    return list(
        conn.execute("SELECT * FROM trades WHERE status IN ('dry_run', 'live') ORDER BY entered_at")
    )


def get_open_trade_for_ticker(db: Database, ticker: str) -> sqlite3.Row | None:
    conn = db.connect()
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM trades WHERE ticker = ? AND status IN ('dry_run', 'live') "
        "ORDER BY entered_at DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    return row


def get_last_closed_trade_for_ticker(db: Database, ticker: str) -> sqlite3.Row | None:
    conn = db.connect()
    row: sqlite3.Row | None = conn.execute(
        """
        SELECT * FROM trades
         WHERE ticker = ?
           AND status IN (
               'dry_run_closed_stop', 'dry_run_closed_resolved',
               'live_closed_stop', 'live_closed_resolved'
           )
         ORDER BY exited_at DESC
         LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    return row


def update_trade_marks(
    db: Database, *, trade_id: int, current_value_usd_cents: int, unrealized_pnl_usd_cents: int
) -> None:
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE trades
               SET unrealized_pnl_usd_cents = ?, last_marked_at = ?
             WHERE id = ?
            """,
            (unrealized_pnl_usd_cents, utcnow_iso(), trade_id),
        )


def close_trade(
    db: Database,
    *,
    trade_id: int,
    new_status: str,
    exit_price_cents: int,
    realized_pnl_usd_cents: int,
    exited_at: str,
) -> None:
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE trades
               SET status = ?, exit_price_cents = ?,
                   realized_pnl_usd_cents = ?, exited_at = ?
             WHERE id = ?
            """,
            (new_status, exit_price_cents, realized_pnl_usd_cents, exited_at, trade_id),
        )


def total_open_position_cost_cents(db: Database) -> int:
    conn = db.connect()
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_basis_usd_cents), 0) "
        "FROM trades WHERE status IN ('dry_run', 'live')"
    ).fetchone()
    return int(row[0])
