"""replay_news_match.py — re-run the matcher on a single stored news event.

Useful for debugging matcher behavior on a specific article without
restarting the daemon.

Usage:
    uv run python scripts/replay_news_match.py <news_event_id> [--db PATH]
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from trumpbot.db.connection import Database
from trumpbot.db.repositories import fetch_news_event, list_active_markets
from trumpbot.discovery.subjects import DEFAULT_SUBJECT_ALIASES, SubjectExtractor
from trumpbot.news.matcher import MarketContext, NewsMatcher

DEFAULT_DB = os.environ.get("TRUMPBOT_DB", "/var/lib/trumpbot/trumpbot.db")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("news_event_id", type=int)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--aliases-yaml", default=None)
    args = parser.parse_args()

    if not Path(args.db).exists():
        raise SystemExit(f"database not found: {args.db}")
    db = Database(args.db)
    db.connect()

    event = fetch_news_event(db, args.news_event_id)
    if event is None:
        raise SystemExit(f"no news event with id={args.news_event_id}")

    aliases = DEFAULT_SUBJECT_ALIASES
    if args.aliases_yaml:
        import yaml

        aliases = yaml.safe_load(Path(args.aliases_yaml).read_text())
    extractor = SubjectExtractor(aliases=aliases)
    matcher = NewsMatcher(extractor=extractor)

    market_rows = list_active_markets(db)
    contexts = [
        MarketContext(
            ticker=row["ticker"],
            subject=row["subject"] or "",
            open_ts=row["open_ts"],
            close_ts=row["close_ts"],
        )
        for row in market_rows
        if row["subject"]
    ]
    if not contexts:
        raise SystemExit("no active markets with extracted subject — nothing to match against")

    print(f"\nNews event {event['id']}:")
    print(f"  source:   {event['source']}")
    print(f"  ts:       {event['detected_ts']}")
    print(f"  headline: {event['headline']}")
    print(f"  url:      {event['url']}")
    print()

    results = matcher.match(
        headline=event["headline"],
        body=event["body_excerpt"],
        markets=contexts,
        article_published_ts=event["raw_published_ts"],
    )
    # Phase 4 Part 2.8: matcher writes confidence=0.0 for everything;
    # interesting rows are the ones whose match_reason is
    # "passed_pre_filter" (the LLM cascade picks them up).
    passed = [r for r in results if r.match_reason == "passed_pre_filter"]
    print(
        f"Matched against {len(contexts)} markets — {len(passed)} passed Stage 1 "
        "(pre-filter); remaining failed_pre_filter rows omitted from this view.\n"
    )
    for r in passed:
        print(
            f"  PASSED  {r.ticker} (subject={r.matched_subject})\n"
            f"    keywords={r.matched_keywords}\n"
            f"    reason={r.match_reason}\n"
        )


if __name__ == "__main__":
    main()
