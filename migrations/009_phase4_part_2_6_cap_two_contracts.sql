-- 009_phase4_part_2_6_cap_two_contracts.sql
-- Phase 4 Part 2.6: cap_two semantic swap.
--
-- Cap two used to mean "5 % of historical traded volume" (a poor
-- proxy for current liquidity). It now means "20 % of YES contracts
-- AVAILABLE at prices ≤ max_buy_price_cents". The dollar
-- representation (``cap_two_value_cents``) is still meaningful — it
-- expresses cap_two as the volume-weighted dollar value of the
-- selected contracts, comparable to cap_one.
--
-- This migration adds the contract-count alongside so the operator
-- can see both representations after the trade closes.
--
--   cap_two_contracts = floor(available_acceptable_contracts × 0.20)
--   cap_two_value_cents = cap_two_contracts × volume_weighted_avg_price
--
-- All pre-Phase-4-Part-2.6 trade rows leave the new column NULL —
-- they were sized under the old semantics and there's no way to
-- reconstruct what live-orderbook depth looked like at decision
-- time. Going forward, every new trade row populates it.

PRAGMA foreign_keys = ON;

BEGIN;

ALTER TABLE trades ADD COLUMN cap_two_contracts INTEGER;

COMMIT;
