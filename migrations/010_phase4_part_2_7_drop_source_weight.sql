-- 010_phase4_part_2_7_drop_source_weight.sql
-- Phase 4 Part 2.7: drop news_events.source_weight column.
--
-- All news sources are now treated equally. The per-source ``weight``
-- knob in config.yaml has been removed and the engine no longer
-- multiplies confidence by source weight to derive a confirmation
-- score — the LLM cascade's confidence is the only signal that feeds
-- into the entry rule.
--
-- The on-disk column was REAL NOT NULL, so it can't be quietly
-- ignored — we drop it. SQLite 3.35+ supports ``ALTER TABLE ...
-- DROP COLUMN`` natively (the runtime ships 3.50.4).
--
-- Existing rows lose the value silently (it was never read after the
-- column was dropped from the engine path). No data migration needed.

PRAGMA foreign_keys = ON;

BEGIN;

ALTER TABLE news_events DROP COLUMN source_weight;

COMMIT;
