-- 005_phase3_walk_fok.sql
-- Phase 3 Part 1: order-book walking, fees, FOK execution.
--
-- Adds the per-trade audit columns the new walk + FOK pipeline
-- produces. Append-only: all new columns are NULLABLE so existing
-- Phase-2 dry-run rows remain valid (their cap_binding et al. are
-- unknown and stay NULL forever — that's fine, the rows are still
-- queryable as historical Phase-2 trades).
--
-- New columns:
--   cap_binding             which of the two caps was active for the
--                           sizing decision: 'cap_one' (hard $20),
--                           'cap_two' (5 % of market volume), or
--                           'tie' (both equal).
--   cap_one_value_cents     dollar amount of cap one at decision time.
--   cap_two_value_cents     dollar amount of cap two at decision time.
--   target_avg_fill_price_cents  what the engine's order-book walk
--                           predicted the average fill would be.
--   actual_avg_fill_price_cents  what the executor's re-walk produced
--                           at submission time. Equal to target when
--                           the book was stable; differs when book
--                           moved between approval and submission.
--   slippage_cents          actual_avg_fill_price - best_ask_at_submit.
--   entry_fees_cents        Kalshi fee paid on the entry fill.
--   exit_fees_cents         Kalshi fee paid on the exit (NULL until
--                           the position closes).
--   levels_consumed_json    JSON array of [price_cents, qty] pairs
--                           showing exactly which levels the walk ate.

PRAGMA foreign_keys = ON;

BEGIN;

ALTER TABLE trades ADD COLUMN cap_binding                  TEXT;
ALTER TABLE trades ADD COLUMN cap_one_value_cents          INTEGER;
ALTER TABLE trades ADD COLUMN cap_two_value_cents          INTEGER;
ALTER TABLE trades ADD COLUMN target_avg_fill_price_cents  INTEGER;
ALTER TABLE trades ADD COLUMN actual_avg_fill_price_cents  INTEGER;
ALTER TABLE trades ADD COLUMN slippage_cents               INTEGER;
ALTER TABLE trades ADD COLUMN entry_fees_cents             INTEGER;
ALTER TABLE trades ADD COLUMN exit_fees_cents              INTEGER;
ALTER TABLE trades ADD COLUMN levels_consumed_json         TEXT;

-- Index for "which cap binds most often?" tuning queries.
CREATE INDEX IF NOT EXISTS idx_trades_cap_binding ON trades (cap_binding);

COMMIT;
