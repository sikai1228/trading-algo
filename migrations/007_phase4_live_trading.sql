-- 007_phase4_live_trading.sql
-- Phase 4 Part 1: live trading executor + reconciliation + shadow tracking.
--
-- Three things land here:
--
-- 1. trades.client_order_id and trades.kalshi_order_id columns. The
--    client_order_id is a UUIDv4 we generate locally and persist BEFORE
--    submitting the order to Kalshi. If the network call dies between
--    "send" and "ack", startup reconciliation can ask Kalshi by
--    client_order_id whether the order ever landed -- preventing
--    duplicate fills. Both are UNIQUE (partial index, since dry-run
--    rows leave them NULL).
--
-- 2. Expanded trades.status CHECK constraint to admit the new live
--    lifecycle states. Phase 2 statuses ('dry_run', '*_closed_*')
--    remain valid; Phase 4 adds:
--      pending                       — order submitted, no ack yet
--      live_closed_resolved_yes      — settled YES (paid 100c)
--      live_closed_resolved_no       — settled NO  (paid 0c)
--      killed_book_moved             — FOK refused; avg fill > target
--      killed_no_fill                — FOK refused; book too thin
--      error_validation              — Kalshi 4xx, code is buggy
--      error_transient               — Kalshi 5xx / network, retried
--      live_imported                 — reconciliation found an unknown
--                                       Kalshi position; tagged for review
--      reconcile_orphaned            — local trade had no Kalshi match
--
-- 3. shadow_decisions table — for every TRADE PROPOSAL the user sees,
--    we record the orderbook snapshot at message-send time AND at
--    decision time. Used by /shadow_report to compare "what would have
--    happened if we auto-approved" vs "what we actually got after the
--    human paused". This is data-only: auto-approve is hardcoded OFF
--    in v1; the table is the empirical foundation for the eventual
--    auto-approve decision.
--
-- SQLite cannot ALTER a CHECK constraint, so the trades table is
-- rebuilt: copy → drop → rename. Foreign keys (trade_news_links and
-- trades.prior_trade_id self-ref) are preserved by disabling the FK
-- pragma during the swap and re-enabling at the end.

BEGIN;

-- ---------------------------------------------------------------------
-- Add the two new ID columns first. Cheap ALTERs, no rebuild.
-- ---------------------------------------------------------------------
ALTER TABLE trades ADD COLUMN client_order_id TEXT;
ALTER TABLE trades ADD COLUMN kalshi_order_id TEXT;

-- ---------------------------------------------------------------------
-- Rebuild trades to widen the status CHECK constraint.
--
-- Foreign keys must be disabled across the rebuild because
-- trade_news_links.trade_id references trades(id); a naive DROP would
-- cascade-delete every link. We turn FK enforcement off, swap, then
-- re-enable. The data is preserved bit-for-bit.
-- ---------------------------------------------------------------------
PRAGMA foreign_keys = OFF;

CREATE TABLE trades_new (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker                      TEXT NOT NULL,
    side                        TEXT NOT NULL DEFAULT 'yes' CHECK (side IN ('yes', 'no')),
    action                      TEXT NOT NULL DEFAULT 'buy' CHECK (action IN ('buy', 'sell')),
    status                      TEXT NOT NULL CHECK (status IN (
        -- Phase 2 lifecycle (preserved verbatim).
        'dry_run',
        'dry_run_closed_stop',
        'dry_run_closed_resolved',
        'live',
        'live_closed_stop',
        'live_closed_resolved',
        -- Phase 4 lifecycle (new).
        'pending',
        'live_closed_resolved_yes',
        'live_closed_resolved_no',
        'killed_book_moved',
        'killed_no_fill',
        'error_validation',
        'error_transient',
        'live_imported',
        'reconcile_orphaned'
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
    -- Phase 3 Part 1 walk + cap audit columns.
    cap_binding                 TEXT,
    cap_one_value_cents         INTEGER,
    cap_two_value_cents         INTEGER,
    target_avg_fill_price_cents INTEGER,
    actual_avg_fill_price_cents INTEGER,
    slippage_cents              INTEGER,
    entry_fees_cents            INTEGER,
    exit_fees_cents             INTEGER,
    levels_consumed_json        TEXT,
    -- Phase 4 Part 1 idempotency columns.
    client_order_id             TEXT,
    kalshi_order_id             TEXT,
    FOREIGN KEY (ticker) REFERENCES markets (ticker) ON UPDATE CASCADE,
    FOREIGN KEY (triggering_match_id) REFERENCES news_market_matches (id),
    FOREIGN KEY (risk_decision_id) REFERENCES risk_decisions (id),
    FOREIGN KEY (approval_id) REFERENCES telegram_approvals (id),
    FOREIGN KEY (prior_trade_id) REFERENCES trades (id)
);

-- Copy every row, column for column. Order matches trades_new exactly.
INSERT INTO trades_new (
    id, ticker, side, action, status,
    entry_price_cents, exit_price_cents, quantity, cost_basis_usd_cents,
    realized_pnl_usd_cents, unrealized_pnl_usd_cents,
    triggering_match_id, triggering_intent_json,
    risk_decision_id, approval_id, is_reentry, prior_trade_id,
    reasoning_text, entered_at, exited_at, last_marked_at, created_at,
    cap_binding, cap_one_value_cents, cap_two_value_cents,
    target_avg_fill_price_cents, actual_avg_fill_price_cents,
    slippage_cents, entry_fees_cents, exit_fees_cents, levels_consumed_json,
    client_order_id, kalshi_order_id
)
SELECT
    id, ticker, side, action, status,
    entry_price_cents, exit_price_cents, quantity, cost_basis_usd_cents,
    realized_pnl_usd_cents, unrealized_pnl_usd_cents,
    triggering_match_id, triggering_intent_json,
    risk_decision_id, approval_id, is_reentry, prior_trade_id,
    reasoning_text, entered_at, exited_at, last_marked_at, created_at,
    cap_binding, cap_one_value_cents, cap_two_value_cents,
    target_avg_fill_price_cents, actual_avg_fill_price_cents,
    slippage_cents, entry_fees_cents, exit_fees_cents, levels_consumed_json,
    client_order_id, kalshi_order_id
FROM trades;

DROP TABLE trades;
ALTER TABLE trades_new RENAME TO trades;

-- Recreate the indexes from migrations 004 + 005.
CREATE INDEX idx_trades_status ON trades (status);
CREATE INDEX idx_trades_ticker_status ON trades (ticker, status);
CREATE INDEX idx_trades_entered_at ON trades (entered_at DESC);
CREATE INDEX idx_trades_triggering_match ON trades (triggering_match_id);
CREATE INDEX idx_trades_prior_trade ON trades (prior_trade_id);
CREATE INDEX idx_trades_cap_binding ON trades (cap_binding);

-- Phase 4 unique partial indexes — UUID idempotency only applies to live
-- trades; dry-run rows leave both order IDs NULL forever.
CREATE UNIQUE INDEX idx_trades_client_order_id
    ON trades (client_order_id)
    WHERE client_order_id IS NOT NULL;
CREATE UNIQUE INDEX idx_trades_kalshi_order_id
    ON trades (kalshi_order_id)
    WHERE kalshi_order_id IS NOT NULL;

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------
-- shadow_decisions — every TRADE PROPOSAL captures the book at
--   message-send-time and at human-decision-time. Used by
--   /shadow_report to compare actual vs counterfactual fills.
--
-- The table is data-only. Auto-approve is HARDCODED OFF in v1; this
-- table simply collects empirical evidence about whether the human's
-- pause helped or hurt across many proposals.
-- ---------------------------------------------------------------------
CREATE TABLE shadow_decisions (
    id                                      INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id                               TEXT NOT NULL,
    intent_type                             TEXT NOT NULL CHECK (
        intent_type IN ('entry', 'reentry', 'stop_loss')
    ),
    ticker                                  TEXT NOT NULL,
    message_sent_at                         TEXT NOT NULL,
    decision_made_at                        TEXT,
    human_decision                          TEXT NOT NULL CHECK (
        human_decision IN ('approved', 'rejected', 'expired', 'pending')
    ),
    -- Snapshot at message-send time (counterfactual auto-approve baseline).
    shadow_yes_ask_at_send_cents            INTEGER NOT NULL,
    shadow_orderbook_at_send_json           TEXT NOT NULL,
    shadow_avg_fill_at_send_cents           INTEGER NOT NULL,
    shadow_filled_quantity_at_send          INTEGER NOT NULL,
    shadow_estimated_cost_at_send_cents     INTEGER NOT NULL,
    -- Snapshot at human-decision time (what the human actually got).
    actual_yes_ask_at_decision_cents        INTEGER,
    actual_avg_fill_at_decision_cents       INTEGER,
    actual_filled_quantity_at_decision      INTEGER,
    -- Derived diffs, computed when both snapshots are present.
    price_movement_cents                    INTEGER,
    decision_lag_seconds                    INTEGER,
    hypothetical_pnl_difference_cents       INTEGER,
    created_at                              TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    )
);

CREATE INDEX idx_shadow_decisions_intent ON shadow_decisions (intent_id);
CREATE INDEX idx_shadow_decisions_ticker ON shadow_decisions (ticker);
CREATE INDEX idx_shadow_decisions_created_at ON shadow_decisions (created_at DESC);

COMMIT;
