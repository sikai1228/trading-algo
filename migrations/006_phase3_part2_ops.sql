-- 006_phase3_part2_ops.sql
-- Phase 3 Part 2: operational layer.
--
-- Five small tables backing the new operational features. All
-- append-only: this migration adds tables and seed rows; no existing
-- column or table changes.
--
-- 1. snoozed_markets    — per-ticker /snooze [duration] state. The
--                          decision loop checks before processing matches.
-- 2. system_state       — generic key/value bag. Used today for
--                          'halt_flag' (the /halt and /resume commands).
-- 3. source_status      — per-news-source health tracker. Drives the
--                          source_health_loop's "down >30 min" warning
--                          and the recovery info alert.
-- 4. alert_dedup        — suppresses repeated alerts within a window.
--                          E.g. source_down doesn't re-alert every poll.
-- 5. llm_spend_log      — every Anthropic API call's cost (in
--                          USDCents) recorded for /spend and the
--                          alert_critical_llm_cap guard.

PRAGMA foreign_keys = ON;

BEGIN;

-- ---------------------------------------------------------------------
-- snoozed_markets
-- ---------------------------------------------------------------------
CREATE TABLE snoozed_markets (
    ticker          TEXT PRIMARY KEY,
    snoozed_until   TEXT NOT NULL,
    snoozed_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    reason          TEXT,
    FOREIGN KEY (ticker) REFERENCES markets(ticker) ON UPDATE CASCADE
);

CREATE INDEX idx_snoozed_markets_until ON snoozed_markets(snoozed_until);

-- ---------------------------------------------------------------------
-- system_state — generic key/value bag.
-- Seeded with halt_flag = 'false' so the decision loop's lookup is safe
-- even before anyone has ever issued /halt.
-- ---------------------------------------------------------------------
CREATE TABLE system_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

INSERT INTO system_state (key, value) VALUES ('halt_flag', 'false');

-- ---------------------------------------------------------------------
-- source_status — per-news-source health.
-- ---------------------------------------------------------------------
CREATE TABLE source_status (
    source_name             TEXT PRIMARY KEY,
    current_status          TEXT NOT NULL CHECK (
        current_status IN ('active', 'down', 'recovering', 'unknown')
    ),
    last_successful_poll    TEXT,
    last_alert_sent         TEXT,
    consecutive_failures    INTEGER NOT NULL DEFAULT 0,
    updated_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ---------------------------------------------------------------------
-- alert_dedup — short-window dedup of alert sends.
-- The categorized-alert system writes here on send when a dedup_key is
-- supplied; subsequent sends with the same key inside the dedup window
-- are dropped (and logged as info-level system events).
-- ---------------------------------------------------------------------
CREATE TABLE alert_dedup (
    dedup_key       TEXT NOT NULL,
    category        TEXT NOT NULL,
    last_sent_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (dedup_key, category)
);

-- ---------------------------------------------------------------------
-- llm_spend_log — Anthropic API spend tracker.
-- ---------------------------------------------------------------------
CREATE TABLE llm_spend_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    component       TEXT NOT NULL,            -- 'alias_enrichment', 'classifier', etc.
    model           TEXT NOT NULL,            -- 'claude-haiku-4-5'
    cost_usd_cents  INTEGER NOT NULL,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    occurred_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_llm_spend_log_occurred_at ON llm_spend_log(occurred_at DESC);
CREATE INDEX idx_llm_spend_log_component ON llm_spend_log(component);

COMMIT;
