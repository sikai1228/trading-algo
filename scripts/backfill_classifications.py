"""backfill_classifications.py — re-run the LLM cascade against existing
news_events.

After Phase 4 Part 2.8 lands, the database has news_events from before
the cascade was wired up: their news_market_matches rows are
``classifier_type='keyword_only'`` (or NULL on legacy rows). This
script:

1. Selects news_events from the last N hours / days.
2. For each, checks whether the new Stage-1 pre-filter passes.
3. For passes, calls the LLM classifier (subject to the cost guard).
4. Writes llm_classifications rows + patches news_market_matches in
   place to ``classifier_type='llm_cascade'``.

Cost: roughly $0.001-0.005 per classified article (Haiku 4.5
pricing). A 7-day backfill of a few thousand events typically lands
under $1.

Usage:

    uv run python scripts/backfill_classifications.py [--db PATH]
                                                       [--hours 168]
                                                       [--config PATH]
                                                       [--dry-run]

``--dry-run`` reports counts without calling the LLM. Idempotent:
re-running it is safe; rows already at ``classifier_type='llm_cascade'``
are skipped.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from trumpbot.config import load_config  # noqa: E402
from trumpbot.db.connection import Database  # noqa: E402
from trumpbot.db.repositories import (  # noqa: E402
    LLMMatchUpdate,
    list_markets_for_matching,
    subjects_alias_map,
    update_match_with_classification,
)
from trumpbot.discovery.subjects import DEFAULT_SUBJECT_ALIASES, SubjectExtractor  # noqa: E402
from trumpbot.news.llm_classifier import (  # noqa: E402
    AnthropicAuthError,
    LLMClassifier,
    LLMClassifierConfig,
)
from trumpbot.news.matcher import PASSED_REASON, MarketContext, NewsMatcher  # noqa: E402
from trumpbot.notifications.llm_cost import LLMCostGuard, LLMCostGuardConfig  # noqa: E402
from trumpbot.platform_paths import current_platform_paths  # noqa: E402


def _default_db_path() -> Path:
    env = os.environ.get("TRUMPBOT_DB")
    if env:
        return Path(env).expanduser()
    return current_platform_paths().database_path


def _make_llm_call(model: str, max_tokens: int):  # type: ignore[no-untyped-def]
    from anthropic import AsyncAnthropic
    from anthropic._exceptions import AuthenticationError

    async def _llm_call(system: str, user: str) -> tuple[int, int, str]:
        client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        try:
            msg = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except AuthenticationError as exc:
            raise AnthropicAuthError(str(exc)) from exc
        text = ""
        for block in msg.content:
            if hasattr(block, "text"):
                text = block.text
                break
        return (msg.usage.input_tokens, msg.usage.output_tokens, text)

    return _llm_call


async def _backfill(
    *,
    db_path: Path,
    hours: int,
    config_path: Path,
    dry_run: bool,
) -> dict[str, int]:
    cfg = load_config(config_path)
    db = Database(db_path)
    db.connect()
    conn = db.connect()

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
        return {"events": 0, "passed_pre_filter": 0, "classified": 0, "skipped": 0}

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
    if not contexts:
        return {"events": len(events), "passed_pre_filter": 0, "classified": 0, "skipped": 0}

    cost_guard = LLMCostGuard(
        db=db,
        config=LLMCostGuardConfig(
            monthly_cap_usd_cents=cfg.alias_enrichment.monthly_cap_usd_cents,
        ),
    )
    classifier = LLMClassifier(
        db=db,
        cost_guard=cost_guard,
        alerts=None,
        config=LLMClassifierConfig(
            enabled=cfg.llm_classifier.enabled,
            model=cfg.llm_classifier.model,
            max_input_tokens=cfg.llm_classifier.max_input_tokens,
            max_output_tokens=cfg.llm_classifier.max_output_tokens,
            timeout_sec=cfg.llm_classifier.timeout_sec,
            prompt_path=cfg.llm_classifier.prompt_path,
            prompt_version=cfg.llm_classifier.prompt_version,
            contract_path=cfg.llm_classifier.contract_path,
        ),
        llm_call=_make_llm_call(
            cfg.llm_classifier.model,
            cfg.llm_classifier.max_output_tokens,
        ),
    )

    counters = {"events": len(events), "passed_pre_filter": 0, "classified": 0, "skipped": 0}

    for evt in events:
        results = matcher.match(
            headline=evt["headline"],
            body=evt["body_excerpt"],
            markets=contexts,
            article_published_ts=evt["raw_published_ts"],
        )
        passed = [r for r in results if r.match_reason == PASSED_REASON]
        if not passed:
            continue
        counters["passed_pre_filter"] += 1

        # Skip events whose pre-existing match rows are already
        # llm_cascade — idempotent re-run.
        existing = list(
            conn.execute(
                "SELECT id, ticker, classifier_type FROM news_market_matches "
                "WHERE news_event_id = ?",
                (evt["id"],),
            )
        )
        already_done = {r["ticker"] for r in existing if r["classifier_type"] == "llm_cascade"}
        not_yet = [r for r in passed if r.ticker not in already_done]
        if not not_yet:
            counters["skipped"] += 1
            continue

        if dry_run:
            continue

        candidates = {
            r.matched_subject: merged.get(r.matched_subject, [])
            for r in not_yet
            if r.matched_subject is not None
        }
        if not candidates:
            continue
        try:
            classified = await classifier.classify(
                news_event_id=int(evt["id"]),
                headline=evt["headline"],
                body=evt["body_excerpt"],
                subject_candidates=candidates,
            )
        except AnthropicAuthError:
            print("[backfill] Anthropic 401 — aborting backfill", file=sys.stderr)
            break
        if classified is None:
            continue
        result, classification_id = classified
        counters["classified"] += 1

        # Patch the matching match rows in place.
        ticker_to_row_id = {r["ticker"]: r["id"] for r in existing}
        picked = result.subject
        for r in not_yet:
            row_id = ticker_to_row_id.get(r.ticker)
            if row_id is None:
                continue
            if picked is not None and r.matched_subject == picked:
                update_match_with_classification(
                    db,
                    match_id=int(row_id),
                    update=LLMMatchUpdate(
                        classifier_type="llm_cascade",
                        confidence=float(result.confidence),
                        matched_subject=picked,
                        match_reason=(
                            f"llm_cascade:interaction={result.interaction_occurred}"
                            f"|tense={result.tense}|negated={result.negated}"
                            f"|indirect={result.indirect_only}"
                        ),
                        llm_classification_id=classification_id,
                    ),
                )
            else:
                update_match_with_classification(
                    db,
                    match_id=int(row_id),
                    update=LLMMatchUpdate(
                        classifier_type="llm_cascade",
                        confidence=0.0,
                        matched_subject=r.matched_subject,
                        match_reason="llm_cascade:not_picked_subject",
                        llm_classification_id=classification_id,
                    ),
                )

    return counters


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="Path to the SQLite db (default: platform default)")
    parser.add_argument("--hours", type=int, default=168, help="Backfill window in hours")
    parser.add_argument(
        "--config",
        default=str(_REPO_ROOT / "config" / "config.example.yaml"),
        help="Config YAML path (env: TRUMPBOT_CONFIG)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count what would be classified without calling the LLM",
    )
    args = parser.parse_args()

    db_path = Path(args.db).expanduser() if args.db else _default_db_path()
    if not db_path.exists():
        print(f"database not found at {db_path}", file=sys.stderr)
        return 2

    config_path = Path(os.environ.get("TRUMPBOT_CONFIG", args.config)).expanduser()

    print(
        f"[backfill] db={db_path}  hours={args.hours}  "
        f"config={config_path}  dry_run={args.dry_run}"
    )
    summary = asyncio.run(
        _backfill(
            db_path=db_path,
            hours=args.hours,
            config_path=config_path,
            dry_run=args.dry_run,
        )
    )
    width = max(len(k) for k in summary)
    print()
    for k, v in summary.items():
        print(f"  {k.ljust(width)} : {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
