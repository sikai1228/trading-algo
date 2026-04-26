"""Re-run the matcher against existing news_events.

Used after a matcher fix (e.g., new verbs added, status filter widened)
to retroactively classify articles already ingested. Without this, a
real-money observation period stays poisoned by the pre-fix
``news_market_matches`` rows.

Usage:
    uv run python scripts/reprocess_matches.py [--db PATH] [--hours 24]
                                                [--dry-run]

Behavior:
    1. Computes the cutoff timestamp from --hours back.
    2. Selects all news_events with detected_ts >= cutoff.
    3. Deletes their existing news_market_matches rows.
    4. Re-runs the matcher (with current verb list, current
       subjects table, current market list).
    5. Inserts fresh news_market_matches.

Idempotent: re-running it is safe; you'll just get the same outcome
again. ``--dry-run`` reports the deltas without writing.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Repo-root sys.path tweak so this script works from any cwd, mirroring
# the smoke_test pattern.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from trumpbot.db.connection import Database  # noqa: E402
from trumpbot.db.repositories import (  # noqa: E402
    NewsMatchRow,
    insert_news_matches,
    list_markets_for_matching,
    subjects_alias_map,
)
from trumpbot.discovery.subjects import DEFAULT_SUBJECT_ALIASES, SubjectExtractor  # noqa: E402
from trumpbot.news.matcher import MarketContext, NewsMatcher  # noqa: E402
from trumpbot.platform_paths import current_platform_paths  # noqa: E402

DEFAULT_HOURS = 24


def reprocess(*, db_path: Path, hours: int, dry_run: bool) -> dict[str, int]:
    db = Database(db_path)
    db.connect()
    conn = db.connect()

    # 1. Select target events.
    events = list(
        conn.execute(
            """
            SELECT id, headline, body_excerpt, raw_published_ts
            FROM news_events
            WHERE detected_ts >= datetime('now', ? )
            ORDER BY id
            """,
            (f"-{int(hours)} hours",),
        )
    )
    if not events:
        return {"events": 0, "matches_deleted": 0, "matches_written": 0}

    event_ids = [e["id"] for e in events]
    placeholders = ",".join("?" for _ in event_ids)

    # 2. Count existing matches we'd delete.
    existing_count = conn.execute(
        f"SELECT COUNT(*) FROM news_market_matches WHERE news_event_id IN ({placeholders})",
        event_ids,
    ).fetchone()[0]

    # 3. Build the matcher (same logic as MatcherWorker).
    merged = {**DEFAULT_SUBJECT_ALIASES, **subjects_alias_map(db)}
    matcher = NewsMatcher(extractor=SubjectExtractor(aliases=merged))

    contexts = [
        MarketContext(
            ticker=row["ticker"],
            subject=row["subject"],
            open_ts=row["open_ts"],
            close_ts=row["close_ts"],
        )
        for row in list_markets_for_matching(db)
        if row["subject"]
    ]

    # 4. Run matcher across every event x context.
    new_rows: list[NewsMatchRow] = []
    passed = 0  # Phase 4 Part 2.8: count pre-filter passes (LLM-eligible)
    for evt in events:
        results = matcher.match(
            headline=evt["headline"],
            body=evt["body_excerpt"],
            markets=contexts,
            article_published_ts=evt["raw_published_ts"],
        )
        for r in results:
            new_rows.append(
                NewsMatchRow(
                    news_event_id=evt["id"],
                    ticker=r.ticker,
                    confidence=r.confidence,
                    matched_subject=r.matched_subject,
                    matched_keywords=r.matched_keywords or None,
                    match_reason=r.match_reason,
                )
            )
            if r.match_reason == "passed_pre_filter":
                passed += 1

    if dry_run:
        return {
            "events": len(events),
            "matches_deleted": existing_count,
            "matches_written": len(new_rows),
            "passed_pre_filter": passed,
            "dry_run": 1,
        }

    # 5. Atomic swap.
    with db.transaction() as tx:
        tx.execute(
            f"DELETE FROM news_market_matches WHERE news_event_id IN ({placeholders})",
            event_ids,
        )
    insert_news_matches(db, new_rows)
    db.close()

    return {
        "events": len(events),
        "matches_deleted": existing_count,
        "matches_written": len(new_rows),
        "passed_pre_filter": passed,
    }


def _default_db_path() -> Path:
    env = os.environ.get("TRUMPBOT_DB")
    if env:
        return Path(env).expanduser()
    return current_platform_paths().database_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="Path to the SQLite db (default: platform default)")
    parser.add_argument(
        "--hours",
        type=int,
        default=DEFAULT_HOURS,
        help=f"Reprocess events from the last N hours (default: {DEFAULT_HOURS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report counts without writing.",
    )
    args = parser.parse_args()

    db_path = Path(args.db).expanduser() if args.db else _default_db_path()
    if not db_path.exists():
        print(f"database not found at {db_path}", file=sys.stderr)
        return 2

    print(f"[reprocess] db={db_path}  hours={args.hours}  dry_run={args.dry_run}")
    summary = reprocess(db_path=db_path, hours=args.hours, dry_run=args.dry_run)
    width = max(len(k) for k in summary)
    print()
    for k, v in summary.items():
        print(f"  {k.ljust(width)} : {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
