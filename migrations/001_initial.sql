-- 001_initial.sql
-- Initial schema for the trumpbot trading system.
--
-- The SQLite schema is the contract between the trading bot and the future
-- observability backend. All timestamps are ISO 8601 UTC strings; foreign
-- keys are declared and must be enforced via PRAGMA foreign_keys = ON.

PRAGMA foreign_keys = ON;

BEGIN;

-- Markets observed on the exchange.
CREATE TABLE IF NOT EXISTS markets (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT NOT NULL UNIQUE,
    title               TEXT NOT NULL,
    subject             TEXT,
    resolution_rules    TEXT NOT NULL,
    open_ts             TEXT,
    close_ts            TEXT,
    status              TEXT NOT NULL,
    raw_json            TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_markets_status ON markets (status);
CREATE INDEX IF NOT EXISTS idx_markets_close_ts ON markets (close_ts);

-- Time-series of orderbook snapshots, indexed by (ticker, ts).
CREATE TABLE IF NOT EXISTS price_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT NOT NULL,
    ts                  TEXT NOT NULL,
    yes_bid_cents       INTEGER,
    yes_ask_cents       INTEGER,
    no_bid_cents        INTEGER,
    no_ask_cents        INTEGER,
    last_trade_cents    INTEGER,
    yes_bid_size        INTEGER,
    yes_ask_size        INTEGER,
    no_bid_size         INTEGER,
    no_ask_size         INTEGER,
    raw_orderbook       TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (ticker) REFERENCES markets (ticker) ON UPDATE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_price_snapshots_ticker_ts ON price_snapshots (ticker, ts);
CREATE INDEX IF NOT EXISTS idx_price_snapshots_ts ON price_snapshots (ts);

-- Every parsed news article from every source, regardless of whether it
-- triggered a trade. Foundation of the future observability backend's
-- evidence trail.
CREATE TABLE IF NOT EXISTS news_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source              TEXT NOT NULL,
    source_weight       REAL NOT NULL,
    headline            TEXT NOT NULL,
    body_excerpt        TEXT,
    url                 TEXT,
    published_ts        TEXT,
    detected_ts         TEXT NOT NULL,
    matched_subjects    TEXT,
    matched_ticker      TEXT,
    raw_data            TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (matched_ticker) REFERENCES markets (ticker) ON UPDATE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_news_events_matched_ticker_ts
    ON news_events (matched_ticker, detected_ts);
CREATE INDEX IF NOT EXISTS idx_news_events_detected_ts
    ON news_events (detected_ts);
CREATE INDEX IF NOT EXISTS idx_news_events_source
    ON news_events (source);

-- Every order submitted with full intent, risk decision, approval response,
-- and lifecycle data.
CREATE TABLE IF NOT EXISTS trades (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id          TEXT NOT NULL UNIQUE,
    ticker                   TEXT NOT NULL,
    side                     TEXT NOT NULL,
    action                   TEXT NOT NULL,
    target_price_cents       INTEGER NOT NULL,
    requested_quantity       INTEGER NOT NULL,
    trade_intent_json        TEXT NOT NULL,
    risk_decision_json       TEXT NOT NULL,
    approval_response_json   TEXT,
    kalshi_order_id          TEXT,
    fill_price_cents         INTEGER,
    fill_quantity            INTEGER,
    current_market_value     INTEGER,
    realized_pnl_cents       INTEGER,
    unrealized_pnl_cents     INTEGER,
    status                   TEXT NOT NULL,
    submitted_at             TEXT,
    filled_at                TEXT,
    closed_at                TEXT,
    created_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (ticker) REFERENCES markets (ticker) ON UPDATE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades (ticker);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades (status);
CREATE INDEX IF NOT EXISTS idx_trades_kalshi_order_id ON trades (kalshi_order_id);
CREATE INDEX IF NOT EXISTS idx_trades_submitted_at ON trades (submitted_at);

-- Many-to-many link between trades and the news events that triggered or
-- confirmed them.
CREATE TABLE IF NOT EXISTS trade_news_links (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id            INTEGER NOT NULL,
    news_event_id       INTEGER NOT NULL,
    role                TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (trade_id, news_event_id, role),
    FOREIGN KEY (trade_id) REFERENCES trades (id) ON DELETE CASCADE,
    FOREIGN KEY (news_event_id) REFERENCES news_events (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_trade_news_links_trade_id
    ON trade_news_links (trade_id);
CREATE INDEX IF NOT EXISTS idx_trade_news_links_news_event_id
    ON trade_news_links (news_event_id);

-- Every RiskManager decision including rejections.
CREATE TABLE IF NOT EXISTS risk_decisions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id            INTEGER,
    ticker              TEXT NOT NULL,
    intent_json         TEXT NOT NULL,
    decision            TEXT NOT NULL,
    rule_fired          TEXT,
    reasoning_text      TEXT,
    decided_at          TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (trade_id) REFERENCES trades (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_risk_decisions_ticker_decided_at
    ON risk_decisions (ticker, decided_at);
CREATE INDEX IF NOT EXISTS idx_risk_decisions_decision
    ON risk_decisions (decision);

-- Bot lifecycle and operational events for the audit log.
CREATE TABLE IF NOT EXISTS system_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type          TEXT NOT NULL,
    severity            TEXT NOT NULL,
    message             TEXT NOT NULL,
    payload_json        TEXT,
    occurred_at         TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_system_events_event_type_occurred_at
    ON system_events (event_type, occurred_at);
CREATE INDEX IF NOT EXISTS idx_system_events_severity
    ON system_events (severity);

COMMIT;
