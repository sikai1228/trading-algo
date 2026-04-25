-- 004_phase2_trades.sql
-- Phase 2 decision layer: full trade lifecycle, risk decisions audit,
-- telegram approvals.
--
-- The Phase-1 schema (001_initial.sql) created a `trades` table with a
-- different shape (kalshi_order_id, target_price_cents/requested_quantity
-- pre-fill terminology). Phase 2 needs a richer model with separate
-- entry/exit pricing, cost basis, P&L, reentry self-references, and
-- explicit FK back to the triggering match.
--
-- Strategy: drop and recreate the empty Phase-1 trades table (no rows
-- yet — we're still in observation), and likewise rebuild risk_decisions
-- and telegram_approvals to match the Phase-2 contract.
--
-- All USD amounts are stored as INTEGER cents to avoid float drift.

PRAGMA foreign_keys = ON;

BEGIN;

-- Phase-1 trades has no rows yet (verified via schema_migrations and
-- the fact that no Phase-1 code wrote to it). Drop and recreate.
DROP TABLE IF EXISTS trade_news_links;  -- depended on Phase-1 trades
DROP TABLE IF EXISTS trades;
DROP TABLE IF EXISTS risk_decisions;
DROP TABLE IF EXISTS telegram_approvals;

-- ---------------------------------------------------------------------
-- risk_decisions — every RiskManager.evaluate() outcome.
-- ---------------------------------------------------------------------
CREATE TABLE risk_decisions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_type           TEXT NOT NULL CHECK (intent_type IN ('entry', 'reentry', 'stop_loss')),
    intent_json           TEXT NOT NULL,
    decision              TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    rejection_reason      TEXT,
    rule_fired            TEXT,
    reasoning_text        TEXT NOT NULL,
    decided_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_risk_decisions_decision_time ON risk_decisions (decision, decided_at DESC);
CREATE INDEX idx_risk_decisions_intent_type ON risk_decisions (intent_type);

-- ---------------------------------------------------------------------
-- telegram_approvals — every approval request + the human's response.
-- ---------------------------------------------------------------------
CREATE TABLE telegram_approvals (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_type           TEXT NOT NULL CHECK (intent_type IN ('entry', 'reentry', 'stop_loss')),
    intent_json           TEXT NOT NULL,
    message_text          TEXT NOT NULL,
    telegram_chat_id      TEXT,
    telegram_message_id   INTEGER,
    decision              TEXT CHECK (decision IS NULL OR decision IN ('approved', 'rejected', 'expired')),
    decision_source       TEXT CHECK (decision_source IS NULL OR decision_source IN ('telegram_button', 'telegram_command', 'timeout')),
    decided_at            TEXT,
    expires_at            TEXT,  -- NULL for unlimited (stop-loss / reentry)
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_telegram_approvals_decision_expires ON telegram_approvals (decision, expires_at);
CREATE INDEX idx_telegram_approvals_created_at ON telegram_approvals (created_at DESC);

-- ---------------------------------------------------------------------
-- trades — Phase-2 lifecycle table.
-- All USD amounts stored as INTEGER cents; never as REAL.
-- ---------------------------------------------------------------------
CREATE TABLE trades (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker                      TEXT NOT NULL,
    side                        TEXT NOT NULL DEFAULT 'yes' CHECK (side IN ('yes', 'no')),
    action                      TEXT NOT NULL DEFAULT 'buy' CHECK (action IN ('buy', 'sell')),
    status                      TEXT NOT NULL CHECK (status IN (
        'dry_run',
        'dry_run_closed_stop',
        'dry_run_closed_resolved',
        'live',
        'live_closed_stop',
        'live_closed_resolved'
    )),
    entry_price_cents           INTEGER NOT NULL,
    exit_price_cents            INTEGER,
    quantity                    INTEGER NOT NULL,
    cost_basis_usd_cents        INTEGER NOT NULL,
    realized_pnl_usd_cents      INTEGER,
    unrealized_pnl_usd_cents    INTEGER,
    triggering_match_id         INTEGER NOT NULL,
    triggering_intent_json      TEXT NOT NULL,
    risk_decision_id            INTEGER NOT NULL,
    approval_id                 INTEGER,
    is_reentry                  INTEGER NOT NULL DEFAULT 0,
    prior_trade_id              INTEGER,
    reasoning_text              TEXT NOT NULL,
    entered_at                  TEXT NOT NULL,
    exited_at                   TEXT,
    last_marked_at              TEXT,
    created_at                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (ticker) REFERENCES markets (ticker) ON UPDATE CASCADE,
    FOREIGN KEY (triggering_match_id) REFERENCES news_market_matches (id),
    FOREIGN KEY (risk_decision_id) REFERENCES risk_decisions (id),
    FOREIGN KEY (approval_id) REFERENCES telegram_approvals (id),
    FOREIGN KEY (prior_trade_id) REFERENCES trades (id)
);

CREATE INDEX idx_trades_status ON trades (status);
CREATE INDEX idx_trades_ticker_status ON trades (ticker, status);
CREATE INDEX idx_trades_entered_at ON trades (entered_at DESC);
CREATE INDEX idx_trades_triggering_match ON trades (triggering_match_id);
CREATE INDEX idx_trades_prior_trade ON trades (prior_trade_id);

-- ---------------------------------------------------------------------
-- Recreate trade_news_links (referenced trades(id) in 001).
-- ---------------------------------------------------------------------
CREATE TABLE trade_news_links (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id            INTEGER NOT NULL,
    news_event_id       INTEGER NOT NULL,
    role                TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (trade_id, news_event_id, role),
    FOREIGN KEY (trade_id) REFERENCES trades (id) ON DELETE CASCADE,
    FOREIGN KEY (news_event_id) REFERENCES news_events (id) ON DELETE CASCADE
);

CREATE INDEX idx_trade_news_links_trade_id ON trade_news_links (trade_id);
CREATE INDEX idx_trade_news_links_news_event_id ON trade_news_links (news_event_id);

COMMIT;
