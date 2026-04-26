-- 013_phase4_part_2_11_auto_approval_and_article_context.sql
-- Phase 4 Part 2.11 — auto-approval mode + standardized trade
-- notifications.
--
-- Three schema changes:
--
-- 1. ``llm_classifications.parsed_key_quote`` — Haiku now returns a
--    verbatim quote from the article that supports its decision.
--    Used to render the "key quote" line in trade-proposal Telegram
--    messages.
--
-- 2. ``trades`` gains five article-context columns
--    (``triggering_article_url``, ``triggering_source``,
--    ``triggering_headline``, ``triggering_key_quote``,
--    ``triggering_published_ts``). Captured from the news_event +
--    llm_classification rows when the trade row is inserted so the
--    full audit trail lives on the trade row itself; downstream
--    queries don't need to re-join across three tables to render
--    the message that fired the trade.
--
-- 3. ``telegram_approvals.decision_source`` CHECK widened to admit
--    ``'auto_approval'``. Phase 4 Part 1 hardcoded the source list
--    to (telegram_button, telegram_command, timeout); the auto-
--    approval path inserts an audit row with source='auto_approval'
--    so the table can be queried for "every approval decision ever,
--    regardless of channel". SQLite cannot ALTER a CHECK constraint
--    so we rebuild the table; the index is recreated at the end.
--
-- Append-only — no edits to migrations 001-012.

PRAGMA foreign_keys = ON;

BEGIN;

-- ---------------------------------------------------------------------
-- 1. llm_classifications.parsed_key_quote
-- ---------------------------------------------------------------------
ALTER TABLE llm_classifications ADD COLUMN parsed_key_quote TEXT;

-- ---------------------------------------------------------------------
-- 2. trades — article-context columns
-- ---------------------------------------------------------------------
ALTER TABLE trades ADD COLUMN triggering_article_url   TEXT;
ALTER TABLE trades ADD COLUMN triggering_source        TEXT;
ALTER TABLE trades ADD COLUMN triggering_headline      TEXT;
ALTER TABLE trades ADD COLUMN triggering_key_quote     TEXT;
ALTER TABLE trades ADD COLUMN triggering_published_ts  TEXT;

-- ---------------------------------------------------------------------
-- 3. telegram_approvals.decision_source — widen CHECK to add
--    'auto_approval'. Rebuild required (SQLite limitation).
-- ---------------------------------------------------------------------
PRAGMA foreign_keys = OFF;

CREATE TABLE telegram_approvals_new (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_type           TEXT NOT NULL CHECK (intent_type IN ('entry', 'reentry', 'stop_loss')),
    intent_json           TEXT NOT NULL,
    message_text          TEXT NOT NULL,
    telegram_chat_id      TEXT,
    telegram_message_id   INTEGER,
    decision              TEXT CHECK (decision IS NULL OR decision IN ('approved', 'rejected', 'expired')),
    decision_source       TEXT CHECK (
        decision_source IS NULL OR decision_source IN (
            'telegram_button',
            'telegram_command',
            'timeout',
            'auto_approval'
        )
    ),
    decided_at            TEXT,
    expires_at            TEXT,
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

INSERT INTO telegram_approvals_new (
    id, intent_type, intent_json, message_text,
    telegram_chat_id, telegram_message_id,
    decision, decision_source, decided_at, expires_at, created_at
)
SELECT
    id, intent_type, intent_json, message_text,
    telegram_chat_id, telegram_message_id,
    decision, decision_source, decided_at, expires_at, created_at
FROM telegram_approvals;

DROP TABLE telegram_approvals;
ALTER TABLE telegram_approvals_new RENAME TO telegram_approvals;

CREATE INDEX idx_telegram_approvals_decision_expires ON telegram_approvals (decision, expires_at);
CREATE INDEX idx_telegram_approvals_created_at ON telegram_approvals (created_at DESC);

PRAGMA foreign_keys = ON;

COMMIT;
