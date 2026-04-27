-- 014_pr_33_rotation_paused.sql
-- PR #33 — Rotation-paused detection for sources that return 200
-- with no new content for > 12 hours.
--
-- Two schema changes:
--
-- 1. ``source_status.newest_feed_item_ts`` — the timestamp of the
--    newest article in the most-recently parsed feed for this
--    source. Updated by the RSS poller on every successful fetch
--    (200 OR 304 — a 304 means "no new content, last_modified
--    unchanged" so newest_feed_item_ts also stays put). The
--    source_health_loop reads this column to detect feeds that
--    are technically reachable (no source_failure events) but
--    whose publisher has stopped emitting new items — the
--    audit's ``fox_politics`` (newest item 7 h old) and
--    ``dod_news`` (newest item 52 h old) cases.
--
-- 2. ``source_status.current_status`` CHECK widened to admit
--    ``'rotation_paused'``. The pre-existing constraint allowed
--    only ('active', 'down', 'recovering', 'unknown'). SQLite
--    cannot ALTER a CHECK constraint in place, so the table is
--    rebuilt — existing rows are preserved bit-for-bit. Foreign
--    keys are temporarily disabled across the swap (no FKs
--    actually point at source_status, but the rebuild dance is
--    the standard one).

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

CREATE TABLE source_status_new (
    source_name             TEXT PRIMARY KEY,
    current_status          TEXT NOT NULL CHECK (
        current_status IN (
            'active', 'down', 'recovering', 'unknown', 'rotation_paused'
        )
    ),
    last_successful_poll    TEXT,
    last_alert_sent         TEXT,
    consecutive_failures    INTEGER NOT NULL DEFAULT 0,
    updated_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    newest_feed_item_ts     TEXT
);

INSERT INTO source_status_new (
    source_name, current_status, last_successful_poll, last_alert_sent,
    consecutive_failures, updated_at, newest_feed_item_ts
)
SELECT
    source_name, current_status, last_successful_poll, last_alert_sent,
    consecutive_failures, updated_at, NULL
FROM source_status;

DROP TABLE source_status;
ALTER TABLE source_status_new RENAME TO source_status;

COMMIT;

PRAGMA foreign_keys = ON;
