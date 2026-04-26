-- 011_phase4_part_2_8_llm_cascade.sql
-- Phase 4 Part 2.8 — Stage 2 LLM cascade goes from "documented" to
-- "deployed."
--
-- Three changes:
--
-- 1. New table ``llm_classifications`` — one row per LLM call
--    (success OR failure). The ``parsed_*`` columns mirror the
--    ``ClassificationResult`` Pydantic model in
--    ``trumpbot/news/llm_classifier.py``. ``error`` is non-NULL on
--    failures so we can audit timeouts / parse errors / 401s without
--    losing them.
--
-- 2. ``news_market_matches`` gains two columns:
--      - ``classifier_type``      — 'keyword_only' (Stage 1 only,
--                                    pre-filter pass or fail) OR
--                                    'llm_cascade' (LLM has
--                                    classified this row).
--      - ``llm_classification_id`` — FK to the row in
--                                    ``llm_classifications`` that
--                                    produced this row's confidence /
--                                    matched_subject.
--    Both nullable; existing rows backfill with default
--    'keyword_only' / NULL.
--
-- 3. New table ``llm_spend_daily`` — fast rollup of per-day spend.
--    The existing ``llm_spend_log`` table (migration 006) remains the
--    authoritative per-call audit trail; ``llm_spend_daily`` is the
--    denormalized aggregate the cost guard reads on every call to
--    keep p99 latency low. Both tables are written together by
--    ``LLMCostGuard.record_spend`` so they never drift.
--
-- All FOREIGN KEY references match the existing parent tables.
-- Append-only — no edits to migrations 001-010.

PRAGMA foreign_keys = ON;

BEGIN;

-- ---------------------------------------------------------------------
-- 1. llm_classifications — per-LLM-call audit trail
-- ---------------------------------------------------------------------
CREATE TABLE llm_classifications (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    news_event_id                   INTEGER NOT NULL,
    prompt_version                  TEXT NOT NULL,
    contract_hash                   TEXT NOT NULL,
    model                           TEXT NOT NULL,
    request_payload                 TEXT NOT NULL,
    response_text                   TEXT,
    parsed_response                 TEXT,
    parsed_subject                  TEXT,
    parsed_interaction_occurred     INTEGER,  -- BOOLEAN as 0/1; SQLite has no real bool type
    parsed_interaction_type         TEXT,
    parsed_tense                    TEXT,
    parsed_negated                  INTEGER,
    parsed_indirect_only            INTEGER,
    parsed_confidence               REAL,
    parsed_reasoning                TEXT,
    input_tokens                    INTEGER,
    output_tokens                   INTEGER,
    cost_micro_usd                  INTEGER,
    error                           TEXT,
    classified_at                   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (news_event_id) REFERENCES news_events(id) ON DELETE CASCADE
);

CREATE INDEX idx_llm_classifications_news_event
    ON llm_classifications(news_event_id);

CREATE INDEX idx_llm_classifications_classified_at
    ON llm_classifications(classified_at DESC);

-- ---------------------------------------------------------------------
-- 2. news_market_matches gains classifier_type + llm_classification_id
-- ---------------------------------------------------------------------
ALTER TABLE news_market_matches
    ADD COLUMN classifier_type TEXT DEFAULT 'keyword_only';

ALTER TABLE news_market_matches
    ADD COLUMN llm_classification_id INTEGER
    REFERENCES llm_classifications(id);

CREATE INDEX IF NOT EXISTS idx_news_market_matches_classifier_type
    ON news_market_matches(classifier_type);

-- ---------------------------------------------------------------------
-- 3. llm_spend_daily — denormalized rollup
-- ---------------------------------------------------------------------
-- One row per UTC day. Updated in lockstep with ``llm_spend_log``
-- by LLMCostGuard.record_spend(). Used by the new CapStatus API to
-- compute "spend percentage of monthly cap" without scanning the
-- per-call log.
CREATE TABLE llm_spend_daily (
    date                    TEXT PRIMARY KEY,  -- 'YYYY-MM-DD' UTC
    total_calls             INTEGER NOT NULL DEFAULT 0,
    cache_hits              INTEGER NOT NULL DEFAULT 0,
    total_input_tokens      INTEGER NOT NULL DEFAULT 0,
    total_output_tokens     INTEGER NOT NULL DEFAULT 0,
    total_cost_micro_usd    INTEGER NOT NULL DEFAULT 0,
    updated_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

COMMIT;
