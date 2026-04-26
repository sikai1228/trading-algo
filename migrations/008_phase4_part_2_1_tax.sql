-- 008_phase4_part_2_1_tax.sql
-- Phase 4 Part 2.1: tax tracking and exports.
--
-- Captures tax-relevant data on every trade so year-end exports
-- (CSV / Form 8949 / Kalshi 1099-B reconciliation) can be generated
-- without recomputing anything from raw lifecycle rows.
--
-- Design choice: capture EVERYTHING at trade time. Don't try to
-- figure out at filing time what the user will need; let the
-- exporter filter from a stable per-trade row.
--
-- All monetary amounts continue to live as INTEGER cents. Dollar
-- formatting happens at the export / display boundary only — no
-- floats anywhere in storage or in the SQL math below.
--
-- New columns on trades:
--   acquired_date              ISO date when the position opened
--                              (date portion of entered_at)
--   disposed_date              ISO date when the position closed
--                              (date portion of exited_at)
--   holding_period_days        (disposed_date - acquired_date).days
--   acquisition_cost_cents     entry_price x quantity + entry_fees
--   disposal_proceeds_cents    settlement payout / stop-exit value,
--                              less exit fees
--   realized_gain_loss_cents   disposal_proceeds_cents -
--                              acquisition_cost_cents
--   tax_year                   year of disposed_date
--
-- Backfill strategy (in this migration):
--   - acquired_date     = date portion of entered_at
--   - disposed_date     = date portion of exited_at (NULL for open)
--   - holding_period    = julianday diff for closed rows
--   - acquisition_cost  = cost_basis_usd_cents + COALESCE(entry_fees,0)
--   - disposal_proceeds:
--       * dry_run_closed_resolved / live_closed_resolved_yes: 100 * qty
--       * live_closed_resolved_no:                            0
--       * *_closed_stop:           exit_price_cents * qty - fees
--   - realized_gain_loss = proceeds - cost
--   - tax_year           = strftime('%Y', disposed_date)
--
-- All open trades (and never-filled rows) leave the tax fields NULL
-- — they're only meaningful once the position closes.

PRAGMA foreign_keys = ON;

BEGIN;

-- ---------------------------------------------------------------------
-- 1. Add new columns. Cheap ALTER TABLEs; no rebuild required.
-- ---------------------------------------------------------------------
ALTER TABLE trades ADD COLUMN acquired_date            TEXT;
ALTER TABLE trades ADD COLUMN disposed_date            TEXT;
ALTER TABLE trades ADD COLUMN holding_period_days      INTEGER;
ALTER TABLE trades ADD COLUMN acquisition_cost_cents   INTEGER;
ALTER TABLE trades ADD COLUMN disposal_proceeds_cents  INTEGER;
ALTER TABLE trades ADD COLUMN realized_gain_loss_cents INTEGER;
ALTER TABLE trades ADD COLUMN tax_year                 INTEGER;

-- Index for /tax_summary and /tax_export queries.
CREATE INDEX IF NOT EXISTS idx_trades_tax_year ON trades (tax_year);
CREATE INDEX IF NOT EXISTS idx_trades_disposed_date ON trades (disposed_date);

-- ---------------------------------------------------------------------
-- 2. Acquisition data — applies to every row (open or closed) the
--    moment a fill is recorded. For pre-existing rows we use what we
--    have: cost_basis_usd_cents already includes the price * qty;
--    entry_fees_cents is nullable for Phase-2 rows (defaulted to 0).
-- ---------------------------------------------------------------------
UPDATE trades
   SET acquired_date          = substr(entered_at, 1, 10),
       acquisition_cost_cents = cost_basis_usd_cents + COALESCE(entry_fees_cents, 0)
 WHERE acquired_date IS NULL
   AND entered_at IS NOT NULL;

-- ---------------------------------------------------------------------
-- 3. Disposal data — only meaningful for closed rows. Stop-loss
--    rows already have exit_price_cents and (sometimes) exit_fees_cents.
--    Resolved rows infer the payoff from the terminal status.
--
--    Note: SQLite julianday is days-since-noon-UTC-of-Nov-24-4714-BC.
--    Subtracting two date strings via julianday() yields a real number
--    of days; CAST to INTEGER drops the fractional part. For midnight-
--    aligned dates that's exact; for closed-mid-day rows it under-
--    counts by 0–1 day. Acceptable for a holding-period field
--    (tax law cares about calendar days, not timestamp precision).
-- ---------------------------------------------------------------------

-- Stop-loss exits.
UPDATE trades
   SET disposed_date           = substr(exited_at, 1, 10),
       disposal_proceeds_cents =
           exit_price_cents * quantity - COALESCE(exit_fees_cents, 0),
       holding_period_days     = CAST(
           julianday(substr(exited_at, 1, 10))
         - julianday(substr(entered_at, 1, 10))
         AS INTEGER),
       tax_year = CAST(strftime('%Y', exited_at) AS INTEGER)
 WHERE disposed_date IS NULL
   AND exited_at IS NOT NULL
   AND status IN ('dry_run_closed_stop', 'live_closed_stop');

-- YES resolution (Phase 2 dry-run + Phase 4 live).
UPDATE trades
   SET disposed_date           = substr(exited_at, 1, 10),
       disposal_proceeds_cents = 100 * quantity,
       holding_period_days     = CAST(
           julianday(substr(exited_at, 1, 10))
         - julianday(substr(entered_at, 1, 10))
         AS INTEGER),
       tax_year = CAST(strftime('%Y', exited_at) AS INTEGER)
 WHERE disposed_date IS NULL
   AND exited_at IS NOT NULL
   AND status IN ('dry_run_closed_resolved', 'live_closed_resolved',
                  'live_closed_resolved_yes');

-- NO resolution (Phase 4 explicit; Phase 2 dry-run defaults to 0
-- when exit_price_cents is 0 — handled below as the catch-all).
UPDATE trades
   SET disposed_date           = substr(exited_at, 1, 10),
       disposal_proceeds_cents = 0,
       holding_period_days     = CAST(
           julianday(substr(exited_at, 1, 10))
         - julianday(substr(entered_at, 1, 10))
         AS INTEGER),
       tax_year = CAST(strftime('%Y', exited_at) AS INTEGER)
 WHERE disposed_date IS NULL
   AND exited_at IS NOT NULL
   AND status = 'live_closed_resolved_no';

-- ---------------------------------------------------------------------
-- 4. Realized gain/loss — once cost + proceeds are populated, it's
--    just subtraction. Done as a separate pass so it works for every
--    row whose disposal data was filled in above.
-- ---------------------------------------------------------------------
UPDATE trades
   SET realized_gain_loss_cents = disposal_proceeds_cents - acquisition_cost_cents
 WHERE realized_gain_loss_cents IS NULL
   AND acquisition_cost_cents IS NOT NULL
   AND disposal_proceeds_cents IS NOT NULL;

COMMIT;
