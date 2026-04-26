-- 012_phase4_part_2_9_drop_snoozed_markets.sql
-- Phase 4 Part 2.9 cleanup — drop the per-ticker snooze table.
--
-- The /snooze and /unsnooze Telegram commands are removed in this
-- cleanup PR; /halt + /resume are sufficient as a global override.
-- The decision-loop check `is_market_snoozed(...)` is gone, so the
-- table is now dead weight.
--
-- Append-only rule: this migration drops the ``snoozed_markets``
-- table introduced by migration 006. The matching index goes with
-- it via DROP TABLE.

PRAGMA foreign_keys = ON;

BEGIN;

DROP INDEX IF EXISTS idx_snoozed_markets_until;
DROP TABLE IF EXISTS snoozed_markets;

COMMIT;
