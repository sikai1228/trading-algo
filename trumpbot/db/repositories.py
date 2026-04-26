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
    """Insert a news event. Returns lastrowid, or None on URL collision.

    Phase 4 Part 2.7: ``source_weight`` was REMOVED from this row.
    The ``news_events.source_weight`` column was dropped by migration
    010; all sources are now treated equally and the LLM cascade's
    confidence is the only signal that feeds into the entry rule.
    """
    sql = """
    INSERT INTO news_events (
        source, is_kalshi_approved, headline, url,
        url_canonical, body_excerpt, author, raw_published_ts,
        detected_ts, has_photo, has_video, raw_data
    ) VALUES (
        :source, :is_kalshi_approved, :headline, :url,
        :url_canonical, :body_excerpt, :author, :raw_published_ts,
        :detected_ts, :has_photo, :has_video, :raw_data
    )
    """
    params = {
        "source": row.source,
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


def insert_news_match_returning_id(db: Database, row: NewsMatchRow) -> int:
    """Insert one match row and return its id.

    Used by the Phase 4 Part 2.8 LLM cascade pipeline so the worker
    can patch the row after the LLM classifies it. Slightly more
    expensive than the bulk path; reserved for the per-row
    classifier loop."""
    sql = """
    INSERT INTO news_market_matches (
        news_event_id, ticker, confidence, matched_subject,
        matched_keywords, match_reason
    ) VALUES (
        :news_event_id, :ticker, :confidence, :matched_subject,
        :matched_keywords, :match_reason
    )
    """
    with db.transaction() as conn:
        cur = conn.execute(
            sql,
            {
                "news_event_id": row.news_event_id,
                "ticker": row.ticker,
                "confidence": row.confidence,
                "matched_subject": row.matched_subject,
                "matched_keywords": (
                    json.dumps(row.matched_keywords) if row.matched_keywords else None
                ),
                "match_reason": row.match_reason,
            },
        )
        return int(cur.lastrowid or 0)


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


def fetch_subject_aliases(db: Database, subject_key: str) -> tuple[bool, list[str]] | None:
    """Return ``(llm_enriched, aliases)`` for ``subject_key`` or
    ``None`` if the subject doesn't exist. Used by the alias enricher
    to skip subjects that have already been processed and to merge
    new aliases with the existing list."""
    conn = db.connect()
    row = conn.execute(
        "SELECT llm_enriched, aliases FROM subjects WHERE subject_key = ?",
        (subject_key,),
    ).fetchone()
    if row is None:
        return None
    try:
        aliases = json.loads(row["aliases"]) if row["aliases"] else []
    except (json.JSONDecodeError, TypeError):
        aliases = []
    if not isinstance(aliases, list):
        aliases = []
    return bool(row["llm_enriched"]), [str(a) for a in aliases]


def mark_subject_llm_enriched(db: Database, *, subject_key: str, aliases: list[str]) -> None:
    """Mark a subject as LLM-enriched and replace its alias list. The
    list is the union the caller has already computed (auto-extracted +
    LLM-suggested) so we don't redo the union here."""
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE subjects
               SET aliases = ?,
                   llm_enriched = 1,
                   updated_at = ?
             WHERE subject_key = ?
            """,
            (json.dumps(aliases), utcnow_iso(), subject_key),
        )


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
    cap_two_contracts: int | None = None
    """Phase 4 Part 2.6: contract-count representation of cap_two
    (``floor(available x 0.20)``). NULL on rows minted before
    migration 009 — those were sized under the old volume semantics
    and there's no way to reconstruct the live orderbook snapshot."""

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

    # Phase 4 Part 1 — live-trading idempotency columns (migration 007).
    client_order_id: str | None = None
    """UUIDv4 we generate locally and persist BEFORE the Kalshi API
    call. Kalshi treats it as the idempotency key; we use it during
    reconciliation to look up an order whose response was lost to a
    network failure."""

    kalshi_order_id: str | None = None
    """The opaque server-side order id Kalshi returns. Populated after
    a successful submission, or by reconciliation when we recover
    orphaned orders. NULL for dry-run rows."""


def insert_trade(db: Database, row: TradeInsertRow) -> int:
    """Insert a fresh trade row.

    Phase 4 Part 2.1: also populates the acquisition-side tax fields
    (``acquired_date`` and ``acquisition_cost_cents``) so the row is
    tax-trackable from the moment it lands. Disposal fields stay NULL
    until :func:`close_trade` fires.
    """
    sql = """
    INSERT INTO trades (
        ticker, side, action, status,
        entry_price_cents, quantity, cost_basis_usd_cents,
        triggering_match_id, triggering_intent_json,
        risk_decision_id, approval_id, is_reentry, prior_trade_id,
        reasoning_text, entered_at, created_at,
        cap_binding, cap_one_value_cents, cap_two_value_cents,
        cap_two_contracts,
        target_avg_fill_price_cents, actual_avg_fill_price_cents,
        slippage_cents, entry_fees_cents, levels_consumed_json,
        client_order_id, kalshi_order_id,
        acquired_date, acquisition_cost_cents
    ) VALUES (
        :ticker, 'yes', 'buy', :status,
        :entry_price_cents, :quantity, :cost_basis_usd_cents,
        :triggering_match_id, :triggering_intent_json,
        :risk_decision_id, :approval_id, :is_reentry, :prior_trade_id,
        :reasoning_text, :entered_at, :now,
        :cap_binding, :cap_one_value_cents, :cap_two_value_cents,
        :cap_two_contracts,
        :target_avg_fill_price_cents, :actual_avg_fill_price_cents,
        :slippage_cents, :entry_fees_cents, :levels_consumed_json,
        :client_order_id, :kalshi_order_id,
        :acquired_date, :acquisition_cost_cents
    )
    """
    # acquisition_cost_cents = price * qty + entry fees. cost_basis_usd_cents
    # already equals price * qty in production code paths, so this is the
    # closest-to-final acquisition cost we can record at fill time.
    acquired_date = row.entered_at[:10] if row.entered_at else None
    entry_fees = row.entry_fees_cents or 0
    acquisition_cost = row.cost_basis_usd_cents + entry_fees
    params = {
        **row.__dict__,
        "is_reentry": int(row.is_reentry),
        "now": utcnow_iso(),
        "acquired_date": acquired_date,
        "acquisition_cost_cents": acquisition_cost,
    }
    with db.transaction() as conn:
        cur = conn.execute(sql, params)
        last_id: int | None = cur.lastrowid
        assert last_id is not None
        return last_id


def update_trade_status_by_client_order_id(
    db: Database,
    *,
    client_order_id: str,
    new_status: str,
    kalshi_order_id: str | None = None,
    entry_price_cents: int | None = None,
    quantity: int | None = None,
    cost_basis_usd_cents: int | None = None,
    actual_avg_fill_price_cents: int | None = None,
) -> int | None:
    """Phase 4: update an existing trade row keyed by ``client_order_id``.

    Used by the live executor's two-phase write: insert with
    ``status='pending'`` BEFORE the API call, then update with the
    actual fill once Kalshi acks. Idempotent — the trade row is
    uniquely keyed on the UUIDv4 we minted locally.

    Returns the trade row id (or ``None`` if no matching row exists).
    """
    fields = ["status = :status"]
    params: dict[str, str | int | None] = {
        "client_order_id": client_order_id,
        "status": new_status,
    }
    if kalshi_order_id is not None:
        fields.append("kalshi_order_id = :kalshi_order_id")
        params["kalshi_order_id"] = kalshi_order_id
    if entry_price_cents is not None:
        fields.append("entry_price_cents = :entry_price_cents")
        params["entry_price_cents"] = entry_price_cents
    if quantity is not None:
        fields.append("quantity = :quantity")
        params["quantity"] = quantity
    if cost_basis_usd_cents is not None:
        fields.append("cost_basis_usd_cents = :cost_basis_usd_cents")
        params["cost_basis_usd_cents"] = cost_basis_usd_cents
    if actual_avg_fill_price_cents is not None:
        fields.append("actual_avg_fill_price_cents = :actual_avg_fill_price_cents")
        params["actual_avg_fill_price_cents"] = actual_avg_fill_price_cents
    sql = f"""
    UPDATE trades
       SET {", ".join(fields)}
     WHERE client_order_id = :client_order_id
    RETURNING id
    """
    with db.transaction() as conn:
        cur = conn.execute(sql, params)
        row = cur.fetchone()
    if row is None:
        return None
    return int(row[0])


def get_trade_by_client_order_id(db: Database, client_order_id: str) -> sqlite3.Row | None:
    """Phase 4: locate a trade row by its idempotency UUID. Used by
    reconciliation to recover from network failures."""
    conn = db.connect()
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM trades WHERE client_order_id = ? LIMIT 1",
        (client_order_id,),
    ).fetchone()
    return row


def get_trade_by_kalshi_order_id(db: Database, kalshi_order_id: str) -> sqlite3.Row | None:
    """Phase 4: locate a trade row by Kalshi's server-side order id.
    Used by reconciliation when Kalshi reports an order our DB doesn't
    know about."""
    conn = db.connect()
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM trades WHERE kalshi_order_id = ? LIMIT 1",
        (kalshi_order_id,),
    ).fetchone()
    return row


def list_open_live_trades(db: Database) -> list[sqlite3.Row]:
    """Phase 4: open positions that need live-mode position marking,
    settlement detection, and stop-loss evaluation."""
    conn = db.connect()
    return list(conn.execute("SELECT * FROM trades WHERE status = 'live' ORDER BY entered_at"))


def list_pending_trades(db: Database) -> list[sqlite3.Row]:
    """Phase 4: trades stuck in ``pending`` (sent to Kalshi but no ack
    received). Reconciliation looks at these on startup and queries
    Kalshi by client_order_id to learn what really happened."""
    conn = db.connect()
    return list(conn.execute("SELECT * FROM trades WHERE status = 'pending' ORDER BY entered_at"))


def list_open_trades(db: Database) -> list[sqlite3.Row]:
    conn = db.connect()
    return list(
        conn.execute(
            "SELECT * FROM trades WHERE status IN ('dry_run', 'live', 'live_imported') "
            "ORDER BY entered_at"
        )
    )


def get_open_trade_for_ticker(db: Database, ticker: str) -> sqlite3.Row | None:
    conn = db.connect()
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM trades WHERE ticker = ? AND status IN ('dry_run', 'live', 'live_imported') "
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
    exit_fees_cents: int | None = None,
) -> None:
    """Close a trade row and populate the Phase 4 Part 2.1 tax fields.

    ``disposed_date``, ``holding_period_days``, ``disposal_proceeds_cents``,
    ``realized_gain_loss_cents`` and ``tax_year`` are all derived from
    columns the row already carries (``acquired_date`` from
    :func:`insert_trade`; ``acquisition_cost_cents`` likewise; the
    new ``exit_price_cents`` / ``exit_fees_cents`` from the executor).

    Storing these alongside the lifecycle update keeps the per-trade
    tax record stable: exporters never have to recompute from raw data.
    """
    disposed_date = exited_at[:10] if exited_at else None
    fees = int(exit_fees_cents or 0)
    with db.transaction() as conn:
        # Pull the acquired_date so SQLite can compute holding period
        # in the same UPDATE without a Python round-trip.
        existing = conn.execute(
            "SELECT acquired_date, acquisition_cost_cents, quantity FROM trades WHERE id = ?",
            (trade_id,),
        ).fetchone()
        acquired_date = existing["acquired_date"] if existing is not None else None
        acquisition_cost = (
            int(existing["acquisition_cost_cents"])
            if existing is not None and existing["acquisition_cost_cents"] is not None
            else None
        )
        quantity = int(existing["quantity"]) if existing is not None else 0
        # disposal_proceeds depends on the terminal status:
        #   stop-loss -> exit_price * qty - exit_fees
        #   YES resolution -> 100 * qty (executor passes exit_price=100)
        #   NO resolution  -> 0          (executor passes exit_price=0)
        # All three patterns reduce to the same formula because the
        # executor always sets exit_price_cents to the right number.
        disposal_proceeds = exit_price_cents * quantity - fees
        if acquisition_cost is not None:
            realized_gain_loss = disposal_proceeds - acquisition_cost
        else:
            # Defensive: pre-Phase-4-Part-2.1 row missing acquisition cost.
            # Fall back to realized_pnl_usd_cents which already accounts
            # for cost_basis (less precise re fees but deterministic).
            realized_gain_loss = realized_pnl_usd_cents
        tax_year = int(exited_at[:4]) if exited_at and len(exited_at) >= 4 else None
        # holding period = (disposed - acquired) calendar days. Use SQLite
        # julianday() so the math matches migration 008's backfill exactly.
        if acquired_date is not None and disposed_date is not None:
            hp_row = conn.execute(
                "SELECT CAST(julianday(?) - julianday(?) AS INTEGER)",
                (disposed_date, acquired_date),
            ).fetchone()
            holding_period = int(hp_row[0]) if hp_row is not None else None
        else:
            holding_period = None

        conn.execute(
            """
            UPDATE trades
               SET status = ?, exit_price_cents = ?,
                   realized_pnl_usd_cents = ?, exited_at = ?,
                   exit_fees_cents = COALESCE(?, exit_fees_cents),
                   disposed_date = ?, holding_period_days = ?,
                   disposal_proceeds_cents = ?,
                   realized_gain_loss_cents = ?, tax_year = ?
             WHERE id = ?
            """,
            (
                new_status,
                exit_price_cents,
                realized_pnl_usd_cents,
                exited_at,
                exit_fees_cents,
                disposed_date,
                holding_period,
                disposal_proceeds,
                realized_gain_loss,
                tax_year,
                trade_id,
            ),
        )


def total_open_position_cost_cents(db: Database) -> int:
    conn = db.connect()
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_basis_usd_cents), 0) "
        "FROM trades WHERE status IN ('dry_run', 'live', 'live_imported')"
    ).fetchone()
    return int(row[0])


# ---------------------------------------------------------------------------
# Phase 3 Part 2: operational tables
# ---------------------------------------------------------------------------


# system_state — generic key/value bag (halt_flag etc.)


def get_system_state(db: Database, key: str) -> str | None:
    """Return the value for ``key`` from ``system_state`` or ``None``
    if the row does not exist."""
    conn = db.connect()
    row = conn.execute("SELECT value FROM system_state WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def set_system_state(db: Database, *, key: str, value: str) -> None:
    """Upsert ``key=value`` in ``system_state`` and bump ``updated_at``."""
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO system_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, utcnow_iso()),
        )


# snoozed_markets — per-ticker /snooze state


@dataclass(frozen=True)
class SnoozedMarketRow:
    ticker: str
    snoozed_until: str
    """ISO-8601 UTC timestamp at which the snooze expires."""

    snoozed_at: str
    reason: str | None


def upsert_snoozed_market(
    db: Database, *, ticker: str, snoozed_until: str, reason: str | None = None
) -> None:
    """Snooze (or extend the snooze on) ``ticker`` until ``snoozed_until``."""
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO snoozed_markets (ticker, snoozed_until, snoozed_at, reason)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                snoozed_until = excluded.snoozed_until,
                snoozed_at = excluded.snoozed_at,
                reason = excluded.reason
            """,
            (ticker, snoozed_until, utcnow_iso(), reason),
        )


def delete_snoozed_market(db: Database, *, ticker: str) -> bool:
    """Remove the snooze on ``ticker``. Returns True if a row was deleted."""
    with db.transaction() as conn:
        cur = conn.execute("DELETE FROM snoozed_markets WHERE ticker = ?", (ticker,))
        return cur.rowcount > 0


def is_market_snoozed(db: Database, ticker: str, *, now_iso: str | None = None) -> bool:
    """True iff ``ticker`` has a snooze row with ``snoozed_until > now``."""
    conn = db.connect()
    row = conn.execute(
        "SELECT snoozed_until FROM snoozed_markets WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    if row is None:
        return False
    now = now_iso or utcnow_iso()
    return str(row[0]) > now


def list_active_snoozed_markets(
    db: Database, *, now_iso: str | None = None
) -> list[SnoozedMarketRow]:
    """All snoozes whose ``snoozed_until`` is in the future."""
    conn = db.connect()
    now = now_iso or utcnow_iso()
    rows = conn.execute(
        """
        SELECT ticker, snoozed_until, snoozed_at, reason
        FROM snoozed_markets
        WHERE snoozed_until > ?
        ORDER BY snoozed_until ASC
        """,
        (now,),
    ).fetchall()
    return [
        SnoozedMarketRow(
            ticker=r["ticker"],
            snoozed_until=r["snoozed_until"],
            snoozed_at=r["snoozed_at"],
            reason=r["reason"],
        )
        for r in rows
    ]


# source_status — per-news-source health


@dataclass(frozen=True)
class SourceStatusRow:
    source_name: str
    current_status: str
    last_successful_poll: str | None
    last_alert_sent: str | None
    consecutive_failures: int
    updated_at: str


def upsert_source_status(
    db: Database,
    *,
    source_name: str,
    current_status: str,
    last_successful_poll: str | None = None,
    last_alert_sent: str | None = None,
    consecutive_failures: int = 0,
) -> None:
    """Upsert one row in ``source_status``. ``COALESCE`` on the
    optional fields preserves prior values if the caller passes
    ``None`` (i.e. a "just record a successful poll" call doesn't
    clobber ``last_alert_sent``)."""
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO source_status (
                source_name, current_status,
                last_successful_poll, last_alert_sent,
                consecutive_failures, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_name) DO UPDATE SET
                current_status = excluded.current_status,
                last_successful_poll = COALESCE(
                    excluded.last_successful_poll, last_successful_poll
                ),
                last_alert_sent = COALESCE(
                    excluded.last_alert_sent, last_alert_sent
                ),
                consecutive_failures = excluded.consecutive_failures,
                updated_at = excluded.updated_at
            """,
            (
                source_name,
                current_status,
                last_successful_poll,
                last_alert_sent,
                consecutive_failures,
                utcnow_iso(),
            ),
        )


def list_source_status(db: Database) -> list[SourceStatusRow]:
    conn = db.connect()
    rows = conn.execute("SELECT * FROM source_status ORDER BY source_name").fetchall()
    return [
        SourceStatusRow(
            source_name=r["source_name"],
            current_status=r["current_status"],
            last_successful_poll=r["last_successful_poll"],
            last_alert_sent=r["last_alert_sent"],
            consecutive_failures=r["consecutive_failures"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


def get_source_status(db: Database, source_name: str) -> SourceStatusRow | None:
    conn = db.connect()
    row = conn.execute(
        "SELECT * FROM source_status WHERE source_name = ?", (source_name,)
    ).fetchone()
    if row is None:
        return None
    return SourceStatusRow(
        source_name=row["source_name"],
        current_status=row["current_status"],
        last_successful_poll=row["last_successful_poll"],
        last_alert_sent=row["last_alert_sent"],
        consecutive_failures=row["consecutive_failures"],
        updated_at=row["updated_at"],
    )


# alert_dedup — short-window dedup for the categorized-alert system


def claim_alert_send(
    db: Database,
    *,
    dedup_key: str,
    category: str,
    window_seconds: int,
    now_iso_str: str | None = None,
) -> bool:
    """Atomically check + record an alert send. Returns True if the
    alert SHOULD be sent (no recent send under this key); False if a
    duplicate within ``window_seconds`` is suppressed.

    Updates the row in either case so the most-recent send timestamp
    is current — that way the dedup window slides forward on every
    duplicate attempt rather than allowing a flood at the boundary.
    """
    now = now_iso_str or utcnow_iso()
    with db.transaction() as conn:
        prior = conn.execute(
            "SELECT last_sent_at FROM alert_dedup WHERE dedup_key = ? AND category = ?",
            (dedup_key, category),
        ).fetchone()
        # Decide first, then upsert. The transaction holds the row
        # lock until the upsert completes so a second caller within
        # the same transaction window sees the updated timestamp.
        send_it = True
        if prior is not None:
            from datetime import datetime

            last = datetime.fromisoformat(str(prior[0]).replace("Z", "+00:00"))
            current = datetime.fromisoformat(now.replace("Z", "+00:00"))
            if (current - last).total_seconds() < window_seconds:
                send_it = False
        conn.execute(
            """
            INSERT INTO alert_dedup (dedup_key, category, last_sent_at)
            VALUES (?, ?, ?)
            ON CONFLICT(dedup_key, category) DO UPDATE SET
                last_sent_at = excluded.last_sent_at
            """,
            (dedup_key, category, now),
        )
        return send_it


# llm_spend_log — Anthropic API spend tracker


def insert_llm_spend(
    db: Database,
    *,
    component: str,
    model: str,
    cost_usd_cents: int,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO llm_spend_log (
                component, model, cost_usd_cents,
                input_tokens, output_tokens, occurred_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (component, model, cost_usd_cents, input_tokens, output_tokens, utcnow_iso()),
        )


def llm_spend_since_cents(db: Database, *, since_iso: str) -> int:
    """Sum of ``cost_usd_cents`` from ``llm_spend_log`` since
    ``since_iso``. Used by the cost guard and the ``/spend``
    command."""
    conn = db.connect()
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd_cents), 0) FROM llm_spend_log WHERE occurred_at >= ?",
        (since_iso,),
    ).fetchone()
    return int(row[0])


def llm_spend_count_since(db: Database, *, since_iso: str) -> int:
    """Number of LLM calls since ``since_iso``. Used to compute
    average-per-call for the ``/spend`` command."""
    conn = db.connect()
    row = conn.execute(
        "SELECT COUNT(*) FROM llm_spend_log WHERE occurred_at >= ?",
        (since_iso,),
    ).fetchone()
    return int(row[0])


# ---------------------------------------------------------------------------
# Phase 4 Part 1 — shadow_decisions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShadowSnapshot:
    """Walk result snapshot at a single point in time. Used at
    message-send-time and at human-decision-time to compare outcomes."""

    yes_ask_cents: int
    avg_fill_cents: int
    filled_quantity: int
    estimated_cost_cents: int
    orderbook_json: str
    """JSON-encoded yes-side levels actually consumed at this snapshot."""


def insert_shadow_decision_at_send(
    db: Database,
    *,
    intent_id: str,
    intent_type: str,
    ticker: str,
    sent_at_iso: str,
    snapshot: ShadowSnapshot,
) -> int:
    """Phase 4: record the orderbook snapshot at TRADE PROPOSAL send
    time. The decision-time snapshot is filled in later by
    :func:`update_shadow_decision_at_decision`.

    Returns the row id so the approval flow can update the same row
    when the human responds (or the timeout fires)."""
    sql = """
    INSERT INTO shadow_decisions (
        intent_id, intent_type, ticker,
        message_sent_at, human_decision,
        shadow_yes_ask_at_send_cents,
        shadow_orderbook_at_send_json,
        shadow_avg_fill_at_send_cents,
        shadow_filled_quantity_at_send,
        shadow_estimated_cost_at_send_cents
    ) VALUES (
        :intent_id, :intent_type, :ticker,
        :sent_at, 'pending',
        :yes_ask, :orderbook_json,
        :avg_fill, :filled_qty, :est_cost
    )
    """
    params = {
        "intent_id": intent_id,
        "intent_type": intent_type,
        "ticker": ticker,
        "sent_at": sent_at_iso,
        "yes_ask": snapshot.yes_ask_cents,
        "orderbook_json": snapshot.orderbook_json,
        "avg_fill": snapshot.avg_fill_cents,
        "filled_qty": snapshot.filled_quantity,
        "est_cost": snapshot.estimated_cost_cents,
    }
    with db.transaction() as conn:
        cur = conn.execute(sql, params)
        last_id: int | None = cur.lastrowid
        assert last_id is not None
        return last_id


def update_shadow_decision_at_decision(
    db: Database,
    *,
    intent_id: str,
    decision_made_at_iso: str,
    human_decision: str,
    snapshot: ShadowSnapshot | None,
) -> None:
    """Phase 4: write the decision-time half of the shadow record.

    ``snapshot`` is ``None`` when the orderbook was unavailable at
    decision time (e.g. the user took so long the WS reconnected with
    no fresh snapshot). In that case derived diff fields stay NULL.
    """
    fields = [
        "decision_made_at = :decision_made_at",
        "human_decision = :human_decision",
    ]
    params: dict[str, str | int | None] = {
        "intent_id": intent_id,
        "decision_made_at": decision_made_at_iso,
        "human_decision": human_decision,
    }
    if snapshot is not None:
        fields.extend(
            [
                "actual_yes_ask_at_decision_cents = :yes_ask",
                "actual_avg_fill_at_decision_cents = :avg_fill",
                "actual_filled_quantity_at_decision = :filled_qty",
                "price_movement_cents = " "  COALESCE(:yes_ask, 0) - shadow_yes_ask_at_send_cents",
                "decision_lag_seconds = CAST(("
                "  julianday(:decision_made_at) - julianday(message_sent_at)"
                ") * 86400 AS INTEGER)",
                "hypothetical_pnl_difference_cents = "
                "  (shadow_estimated_cost_at_send_cents - COALESCE(:est_cost, 0))",
            ]
        )
        params["yes_ask"] = snapshot.yes_ask_cents
        params["avg_fill"] = snapshot.avg_fill_cents
        params["filled_qty"] = snapshot.filled_quantity
        params["est_cost"] = snapshot.estimated_cost_cents
    sql = f"""
    UPDATE shadow_decisions
       SET {", ".join(fields)}
     WHERE intent_id = :intent_id
       AND human_decision = 'pending'
    """
    with db.transaction() as conn:
        conn.execute(sql, params)


def shadow_report_summary(db: Database, *, since_iso: str) -> dict[str, int | float]:
    """Aggregate stats for the ``/shadow_report`` command.

    Returns a dict with:
    - total_proposals
    - approved_count
    - rejected_count
    - expired_count
    - avg_decision_lag_seconds
    - avg_price_movement_cents
    - sum_hypothetical_pnl_diff_cents

    All numeric fields fall back to 0 when no rows match.
    """
    conn = db.connect()
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total_proposals,
            SUM(CASE WHEN human_decision = 'approved' THEN 1 ELSE 0 END) AS approved_count,
            SUM(CASE WHEN human_decision = 'rejected' THEN 1 ELSE 0 END) AS rejected_count,
            SUM(CASE WHEN human_decision = 'expired'  THEN 1 ELSE 0 END) AS expired_count,
            COALESCE(AVG(decision_lag_seconds), 0) AS avg_decision_lag_seconds,
            COALESCE(AVG(price_movement_cents), 0) AS avg_price_movement_cents,
            COALESCE(SUM(hypothetical_pnl_difference_cents), 0) AS sum_hypothetical_pnl_diff_cents
          FROM shadow_decisions
         WHERE message_sent_at >= ?
        """,
        (since_iso,),
    ).fetchone()
    if row is None:
        return {
            "total_proposals": 0,
            "approved_count": 0,
            "rejected_count": 0,
            "expired_count": 0,
            "avg_decision_lag_seconds": 0.0,
            "avg_price_movement_cents": 0.0,
            "sum_hypothetical_pnl_diff_cents": 0,
        }
    return {
        "total_proposals": int(row["total_proposals"] or 0),
        "approved_count": int(row["approved_count"] or 0),
        "rejected_count": int(row["rejected_count"] or 0),
        "expired_count": int(row["expired_count"] or 0),
        "avg_decision_lag_seconds": float(row["avg_decision_lag_seconds"] or 0.0),
        "avg_price_movement_cents": float(row["avg_price_movement_cents"] or 0.0),
        "sum_hypothetical_pnl_diff_cents": int(row["sum_hypothetical_pnl_diff_cents"] or 0),
    }


# ---------------------------------------------------------------------------
# Phase 4 Part 2.8 — LLM cascade tables
# ---------------------------------------------------------------------------


def insert_llm_classification(
    *,
    db: Database,
    news_event_id: int,
    prompt_version: str,
    contract_hash: str,
    model: str,
    request_payload: str,
    response_text: str | None,
    parsed: Any,  # ClassificationResult | None — typed Any to avoid module cycle
    input_tokens: int | None,
    output_tokens: int | None,
    cost_micro_usd: int | None,
    error: str | None,
) -> int:
    """Insert one ``llm_classifications`` row. Returns the new id.

    ``parsed`` is the ClassificationResult Pydantic instance from
    :mod:`trumpbot.news.llm_classifier`, or ``None`` on failure. We
    take it as ``Any`` so this module doesn't import the classifier
    (the dependency arrow points the other way).
    """
    if parsed is None:
        parsed_subject = None
        parsed_response = None
        parsed_interaction = None
        parsed_type = None
        parsed_tense = None
        parsed_negated = None
        parsed_indirect = None
        parsed_confidence = None
        parsed_reasoning = None
    else:
        parsed_subject = getattr(parsed, "subject", None)
        parsed_response = json.dumps(parsed.model_dump())
        parsed_interaction = 1 if parsed.interaction_occurred else 0
        parsed_type = parsed.interaction_type
        parsed_tense = parsed.tense
        parsed_negated = 1 if parsed.negated else 0
        parsed_indirect = 1 if parsed.indirect_only else 0
        parsed_confidence = float(parsed.confidence)
        parsed_reasoning = parsed.reasoning

    with db.transaction() as conn:
        cur = conn.execute(
            """
            INSERT INTO llm_classifications (
                news_event_id, prompt_version, contract_hash, model,
                request_payload, response_text, parsed_response,
                parsed_subject, parsed_interaction_occurred,
                parsed_interaction_type, parsed_tense, parsed_negated,
                parsed_indirect_only, parsed_confidence, parsed_reasoning,
                input_tokens, output_tokens, cost_micro_usd, error,
                classified_at
            ) VALUES (
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?, ?
            )
            """,
            (
                news_event_id,
                prompt_version,
                contract_hash,
                model,
                request_payload,
                response_text,
                parsed_response,
                parsed_subject,
                parsed_interaction,
                parsed_type,
                parsed_tense,
                parsed_negated,
                parsed_indirect,
                parsed_confidence,
                parsed_reasoning,
                input_tokens,
                output_tokens,
                cost_micro_usd,
                error,
                utcnow_iso(),
            ),
        )
        return int(cur.lastrowid or 0)


def fetch_llm_classification(db: Database, classification_id: int) -> sqlite3.Row | None:
    conn = db.connect()
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM llm_classifications WHERE id = ?", (classification_id,)
    ).fetchone()
    return row


def fetch_llm_classification_for_event(db: Database, news_event_id: int) -> sqlite3.Row | None:
    """The most recent classification for a given news event (if any)."""
    conn = db.connect()
    row: sqlite3.Row | None = conn.execute(
        """
        SELECT * FROM llm_classifications
         WHERE news_event_id = ?
         ORDER BY classified_at DESC
         LIMIT 1
        """,
        (news_event_id,),
    ).fetchone()
    return row


@dataclass(frozen=True)
class LLMMatchUpdate:
    """The fields :func:`update_match_with_classification` overwrites."""

    classifier_type: str  # 'llm_cascade' or 'keyword_only'
    confidence: float
    matched_subject: str | None
    match_reason: str
    llm_classification_id: int | None


def update_match_with_classification(
    db: Database,
    *,
    match_id: int,
    update: LLMMatchUpdate,
) -> None:
    """Patch an existing ``news_market_matches`` row in place.

    Called by the matcher worker after the LLM classifies a Stage-1
    pre-filter pass: the keyword row is upgraded to a Stage-2 row
    (or stays keyword-only on cap-hit / failure)."""
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE news_market_matches
               SET classifier_type = ?,
                   confidence = ?,
                   matched_subject = ?,
                   match_reason = ?,
                   llm_classification_id = ?
             WHERE id = ?
            """,
            (
                update.classifier_type,
                update.confidence,
                update.matched_subject,
                update.match_reason,
                update.llm_classification_id,
                match_id,
            ),
        )


def fetch_match_with_classification(db: Database, *, match_id: int) -> sqlite3.Row | None:
    """Read a match row joined with its (optional) LLM classification.

    Used by ``decision/loops.py:_row_to_snapshot`` so the decision
    engine sees ``parsed_interaction_occurred`` instead of inferring
    it from a missing column."""
    conn = db.connect()
    row: sqlite3.Row | None = conn.execute(
        """
        SELECT m.id AS id,
               m.news_event_id AS news_event_id,
               m.ticker AS ticker,
               m.confidence AS confidence,
               m.matched_subject AS matched_subject,
               m.matched_keywords AS matched_keywords,
               m.match_reason AS match_reason,
               m.classifier_type AS classifier_type,
               m.llm_classification_id AS llm_classification_id,
               c.parsed_interaction_occurred AS parsed_interaction_occurred,
               c.parsed_subject AS parsed_subject,
               c.parsed_confidence AS parsed_confidence
          FROM news_market_matches m
          LEFT JOIN llm_classifications c ON c.id = m.llm_classification_id
         WHERE m.id = ?
        """,
        (match_id,),
    ).fetchone()
    return row


# ---- llm_spend_daily upsert ------------------------------------------------


def upsert_llm_spend_daily(
    db: Database,
    *,
    day_iso: str,  # 'YYYY-MM-DD'
    cost_micro_usd: int,
    input_tokens: int,
    output_tokens: int,
    cache_hit: bool = False,
) -> None:
    """Add one call's cost+tokens to the day's rollup row.

    Idempotent only at the call-grain — calling it twice for the
    same call double-counts. The cost guard wraps a single
    ``record_spend`` per call so this stays consistent with
    ``llm_spend_log``."""
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO llm_spend_daily (
                date, total_calls, cache_hits,
                total_input_tokens, total_output_tokens,
                total_cost_micro_usd, updated_at
            ) VALUES (?, 1, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                total_calls = total_calls + 1,
                cache_hits = cache_hits + excluded.cache_hits,
                total_input_tokens = total_input_tokens + excluded.total_input_tokens,
                total_output_tokens = total_output_tokens + excluded.total_output_tokens,
                total_cost_micro_usd = total_cost_micro_usd + excluded.total_cost_micro_usd,
                updated_at = excluded.updated_at
            """,
            (
                day_iso,
                1 if cache_hit else 0,
                input_tokens,
                output_tokens,
                cost_micro_usd,
                utcnow_iso(),
            ),
        )


def llm_spend_daily_total_cents_since(db: Database, *, day_iso: str) -> int:
    """Sum of ``total_cost_micro_usd`` (converted back to USDCents) since
    ``day_iso`` (inclusive). Used by the cap-status query."""
    conn = db.connect()
    row = conn.execute(
        """
        SELECT COALESCE(SUM(total_cost_micro_usd), 0) AS micro
          FROM llm_spend_daily
         WHERE date >= ?
        """,
        (day_iso,),
    ).fetchone()
    micro = int(row["micro"] or 0)
    # micro USD -> cents: 1 cent = 10_000 micro USD
    return micro // 10_000
