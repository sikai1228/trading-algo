-- 002_subjects.sql
-- Adds the `subjects` table (one row per person Trump might meet) and the
-- `subject_full_name` column on `markets` so the discovery service can
-- record both the verbatim name extracted from the title and a normalized
-- key suitable for joining.
--
-- The `markets.subject` column from migration 001 is reused as the
-- `subject_key` (lowercased + non-alpha stripped form). No FK constraint
-- is declared between `markets.subject` and `subjects.subject_key`
-- because adding a foreign key to an existing table in SQLite requires
-- recreating the table; the discovery service maintains both atomically.

PRAGMA foreign_keys = ON;

BEGIN;

CREATE TABLE IF NOT EXISTS subjects (
    subject_key      TEXT PRIMARY KEY,
    full_name        TEXT NOT NULL,
    aliases          TEXT NOT NULL DEFAULT '[]',  -- JSON array of strings
    ticker_suffix    TEXT,                        -- e.g. 'VPUT' for Putin
    auto_extracted   INTEGER NOT NULL DEFAULT 1,
    llm_enriched     INTEGER NOT NULL DEFAULT 0,
    reviewed         INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_subjects_full_name ON subjects (full_name);
CREATE INDEX IF NOT EXISTS idx_subjects_reviewed ON subjects (reviewed);

ALTER TABLE markets ADD COLUMN subject_full_name TEXT;

CREATE INDEX IF NOT EXISTS idx_markets_subject_full_name ON markets (subject_full_name);

COMMIT;
