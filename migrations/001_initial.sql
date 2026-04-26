-- 001_initial.sql
-- Phase 1 schema for the trumpbot trading system. Implements every table
-- in CLAUDE.md "Database Schema as the Source of Truth", including
-- tables defined for completeness but not actively populated until
-- Phase 2 or later (trades, trade_news_links, risk_decisions,
-- telegram_approvals).
--
-- All timestamps are ISO 8601 UTC strings. Foreign keys are declared and
-- must be enforced via PRAGMA foreign_keys = ON on every connection.

PRAGMA foreign_keys = ON;

BEGIN;

-- ---------------------------------------------------------------------
-- markets: every Kalshi market we observe in a target series.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS markets (
    ticker                       TEXT PRIMARY KEY,
    series_ticker                TEXT NOT NULL,
    event_ticker                 TEXT,
    title                        TEXT NOT NULL,
    subtitle                     TEXT,
    yes_sub_title                TEXT,
    no_sub_title                 TEXT,
    subject                      TEXT,
    resolution_rules             TEXT NOT NULL,
    approved_sources             TEXT,
    open_ts                      TEXT NOT NULL,
    close_ts                     TEXT,
    expected_expiration_ts       TEXT,
    status                       TEXT NOT NULL,
    last_price_cents             INTEGER,
    volume                       INTEGER,
    open_interest                INTEGER,
    raw_json                     TEXT,
    created_at                   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at                   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_markets_status ON markets (status);
CREATE INDEX IF NOT EXISTS idx_markets_series ON markets (series_ticker);
CREATE INDEX IF NOT EXISTS idx_markets_subject ON markets (subject);
CREATE INDEX IF NOT EXISTS idx_markets_close_ts ON markets (close_ts);

-- ---------------------------------------------------------------------
-- price_snapshots: best bid/ask + volume sampled periodically and on
-- significant moves (>=2c shift on best bid or ask).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS price_snapshots (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker                       TEXT NOT NULL,
    yes_bid_cents                INTEGER,
    yes_ask_cents                INTEGER,
    no_bid_cents                 INTEGER,
    no_ask_cents                 INTEGER,
    yes_bid_size                 INTEGER,
    yes_ask_size                 INTEGER,
    no_bid_size                  INTEGER,
    no_ask_size                  INTEGER,
    last_trade_price_cents       INTEGER,
    volume_24h                   INTEGER,
    ts                           TEXT NOT NULL,
    snapshot_reason              TEXT NOT NULL,
    created_at                   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (ticker) REFERENCES markets (ticker) ON UPDATE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_price_snapshots_ticker_ts
    ON price_snapshots (ticker, ts DESC);

-- ---------------------------------------------------------------------
-- orderbook_snapshots: full depth, sampled less often than price
-- snapshots (every 5 minutes plus on major moves).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker                       TEXT NOT NULL,
    yes_levels                   TEXT NOT NULL,
    no_levels                    TEXT NOT NULL,
    ts                           TEXT NOT NULL,
    created_at                   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (ticker) REFERENCES markets (ticker) ON UPDATE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_orderbook_snapshots_ticker_ts
    ON orderbook_snapshots (ticker, ts DESC);

-- ---------------------------------------------------------------------
-- news_events: every parsed article from every source. Foundation of
-- the future observability backend's evidence trail.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS news_events (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    source                       TEXT NOT NULL,
    source_weight                REAL NOT NULL,
    is_kalshi_approved           INTEGER NOT NULL DEFAULT 0,
    headline                     TEXT NOT NULL,
    url                          TEXT,
    url_canonical                TEXT,
    body_excerpt                 TEXT,
    author                       TEXT,
    raw_published_ts             TEXT,
    detected_ts                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    has_photo                    INTEGER NOT NULL DEFAULT 0,
    has_video                    INTEGER NOT NULL DEFAULT 0,
    raw_data                     TEXT,
    created_at                   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_news_events_detected_ts
    ON news_events (detected_ts DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_news_events_url_canonical
    ON news_events (url_canonical) WHERE url_canonical IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_news_events_source
    ON news_events (source);

-- ---------------------------------------------------------------------
-- news_market_matches: matcher output (positive AND negative).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS news_market_matches (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    news_event_id                INTEGER NOT NULL,
    ticker                       TEXT NOT NULL,
    confidence                   REAL NOT NULL,
    matched_subject              TEXT,
    matched_keywords             TEXT,
    match_reason                 TEXT,
    created_at                   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (news_event_id) REFERENCES news_events (id) ON DELETE CASCADE,
    FOREIGN KEY (ticker) REFERENCES markets (ticker) ON UPDATE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_news_market_matches_ticker_conf
    ON news_market_matches (ticker, confidence DESC);
CREATE INDEX IF NOT EXISTS idx_news_market_matches_news_event_id
    ON news_market_matches (news_event_id);

-- ---------------------------------------------------------------------
-- system_events: bot lifecycle and operational events for the audit log.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS system_events (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type                   TEXT NOT NULL,
    severity                     TEXT NOT NULL,
    component                    TEXT NOT NULL,
    message                      TEXT NOT NULL,
    detail                       TEXT,
    ts                           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_at                   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_system_events_event_type_ts
    ON system_events (event_type, ts DESC);
CREATE INDEX IF NOT EXISTS idx_system_events_severity
    ON system_events (severity);
CREATE INDEX IF NOT EXISTS idx_system_events_component
    ON system_events (component);

-- ---------------------------------------------------------------------
-- trades, trade_news_links, risk_decisions, telegram_approvals: defined
-- here for completeness; not populated until Phase 2+.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trades (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id              TEXT NOT NULL UNIQUE,
    ticker                       TEXT NOT NULL,
    side                         TEXT NOT NULL,
    action                       TEXT NOT NULL,
    target_price_cents           INTEGER NOT NULL,
    requested_quantity           INTEGER NOT NULL,
    trade_intent_json            TEXT NOT NULL,
    risk_decision_json           TEXT NOT NULL,
    approval_response_json       TEXT,
    kalshi_order_id              TEXT,
    fill_price_cents             INTEGER,
    fill_quantity                INTEGER,
    current_market_value         INTEGER,
    realized_pnl_cents           INTEGER,
    unrealized_pnl_cents         INTEGER,
    status                       TEXT NOT NULL,
    submitted_at                 TEXT,
    filled_at                    TEXT,
    closed_at                    TEXT,
    created_at                   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at                   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (ticker) REFERENCES markets (ticker) ON UPDATE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades (ticker);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades (status);
CREATE INDEX IF NOT EXISTS idx_trades_kalshi_order_id ON trades (kalshi_order_id);

CREATE TABLE IF NOT EXISTS trade_news_links (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id                     INTEGER NOT NULL,
    news_event_id                INTEGER NOT NULL,
    role                         TEXT NOT NULL,
    created_at                   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (trade_id, news_event_id, role),
    FOREIGN KEY (trade_id) REFERENCES trades (id) ON DELETE CASCADE,
    FOREIGN KEY (news_event_id) REFERENCES news_events (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_trade_news_links_trade_id
    ON trade_news_links (trade_id);
CREATE INDEX IF NOT EXISTS idx_trade_news_links_news_event_id
    ON trade_news_links (news_event_id);

CREATE TABLE IF NOT EXISTS risk_decisions (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id                     INTEGER,
    ticker                       TEXT NOT NULL,
    intent_json                  TEXT NOT NULL,
    decision                     TEXT NOT NULL,
    rule_fired                   TEXT,
    reasoning_text               TEXT,
    decided_at                   TEXT NOT NULL,
    created_at                   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (trade_id) REFERENCES trades (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_risk_decisions_ticker_decided_at
    ON risk_decisions (ticker, decided_at);
CREATE INDEX IF NOT EXISTS idx_risk_decisions_decision
    ON risk_decisions (decision);

CREATE TABLE IF NOT EXISTS telegram_approvals (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id                     INTEGER,
    chat_id                      TEXT NOT NULL,
    message_id                   TEXT,
    request_payload_json         TEXT NOT NULL,
    response                     TEXT,
    response_received_at         TEXT,
    expired                      INTEGER NOT NULL DEFAULT 0,
    created_at                   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (trade_id) REFERENCES trades (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_telegram_approvals_trade_id
    ON telegram_approvals (trade_id);

COMMIT;
