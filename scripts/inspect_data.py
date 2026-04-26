"""inspect_data.py — quick read-only summary of what the daemon has captured.

Run during the observation period to verify data quality without writing
SQL by hand.

Usage:
    uv run python scripts/inspect_data.py [--db PATH] [--n N]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

DEFAULT_DB = os.environ.get("TRUMPBOT_DB", "/var/lib/trumpbot/trumpbot.db")


def _open(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def cmd_markets(conn: sqlite3.Connection) -> None:
    print("\n=== ACTIVE MARKETS ===")
    cur = conn.execute(
        """
        SELECT m.ticker, m.subject, m.status, m.last_price_cents,
               m.volume, m.title
        FROM markets m
        WHERE m.status = 'active'
        ORDER BY m.ticker
        """
    )
    for row in cur:
        print(
            f"  {row['ticker']:30s} subj={row['subject']:>20s}  "
            f"last={row['last_price_cents']!s:>4}c  vol={row['volume']!s:>6}  "
            f"{row['title']}"
        )


def cmd_news(conn: sqlite3.Connection, limit: int) -> None:
    print(f"\n=== {limit} MOST RECENT NEWS EVENTS ===")
    cur = conn.execute(
        """
        SELECT n.id, n.detected_ts, n.source, n.headline,
               COUNT(m.id) AS match_count,
               MAX(m.confidence) AS max_confidence
        FROM news_events n
        LEFT JOIN news_market_matches m ON m.news_event_id = n.id
        GROUP BY n.id
        ORDER BY n.detected_ts DESC
        LIMIT ?
        """,
        (limit,),
    )
    for row in cur:
        max_c = f"{row['max_confidence']:.2f}" if row["max_confidence"] is not None else "    "
        print(
            f"  [{row['id']:>5}]  {row['detected_ts']}  src={row['source']:<25s}  "
            f"matches={row['match_count']!s:>2}  best={max_c}  {row['headline'][:80]}"
        )


def cmd_matches(conn: sqlite3.Connection, threshold: float, limit: int) -> None:
    print(f"\n=== HIGH-CONFIDENCE MATCHES (>= {threshold}) ===")
    cur = conn.execute(
        """
        SELECT m.created_at, m.ticker, m.confidence, m.matched_subject,
               m.match_reason, n.source, n.headline, n.url
        FROM news_market_matches m
        JOIN news_events n ON n.id = m.news_event_id
        WHERE m.confidence >= ?
        ORDER BY m.created_at DESC
        LIMIT ?
        """,
        (threshold, limit),
    )
    for row in cur:
        print(
            f"  {row['created_at']}  conf={row['confidence']:.2f}  "
            f"{row['ticker']} ({row['matched_subject']})\n"
            f"    src={row['source']}  reason={row['match_reason']}\n"
            f"    {row['headline']}\n"
            f"    {row['url']}\n"
        )


def cmd_stats(conn: sqlite3.Connection) -> None:
    print("\n=== STATS (last 24h) ===")
    today_total = conn.execute(
        "SELECT COUNT(*) FROM news_events WHERE detected_ts >= datetime('now', '-1 day')"
    ).fetchone()[0]
    by_source = conn.execute(
        """
        SELECT source, COUNT(*) AS n
        FROM news_events
        WHERE detected_ts >= datetime('now', '-1 day')
        GROUP BY source
        ORDER BY n DESC
        """
    )
    matched = conn.execute(
        """
        SELECT COUNT(DISTINCT n.id)
        FROM news_events n
        JOIN news_market_matches m ON m.news_event_id = n.id
        WHERE n.detected_ts >= datetime('now', '-1 day')
          AND m.confidence > 0
        """
    ).fetchone()[0]
    snapshots = conn.execute(
        "SELECT COUNT(*) FROM price_snapshots WHERE created_at >= datetime('now', '-1 day')"
    ).fetchone()[0]
    print(f"  news_events:    {today_total}")
    print(f"  matched (>0):   {matched}")
    print(f"  price_snapshots:{snapshots}")
    print("  by source:")
    for row in by_source:
        print(f"    {row['source']:<35s}  {row['n']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--n", type=int, default=20, help="rows per section")
    parser.add_argument("--match-threshold", type=float, default=0.5)
    args = parser.parse_args()

    if not Path(args.db).exists():
        raise SystemExit(f"database not found: {args.db}")
    conn = _open(args.db)
    cmd_markets(conn)
    cmd_news(conn, args.n)
    cmd_matches(conn, args.match_threshold, args.n)
    cmd_stats(conn)


if __name__ == "__main__":
    main()
