# Pipeline order verification — 2026-04-26

**Spec source**: Phase 4 Part 2.12, deliverable 7.

**Question**: does the freshness check (24-hour cutoff) happen
BEFORE the LLM call, so old articles don't waste Anthropic budget?

## Method

Trace a news_event from ingestion → engine evaluation, reading the
production source paths in `trumpbot/`.

## Findings (pre-fix state)

The pre-fix pipeline order was:

1. **RSS poller** (`trumpbot/news/rss.py`) ingests an article →
   inserts a `news_events` row with `detected_ts` and
   `raw_published_ts`. **No freshness check.**

2. **MatcherWorker._process_batch** (`trumpbot/daemon.py`) picks up
   unmatched events and runs Stage 1 keyword pre-filter
   (`trumpbot/news/matcher.py`). Articles with at least one
   ``passed_pre_filter`` row land in `events_needing_llm`. **No
   freshness check.**

3. **MatcherWorker._classify_and_patch** calls the LLM cascade
   (`LLMClassifier.classify`) for every event in `events_needing_llm`.
   **No freshness check.**

4. **DecisionEngine.evaluate_news_match** rule 4 checks
   `_article_within_window(...)` — if the article is outside the
   market's open/close window, returns `None`. This is downstream
   of the LLM call and only blocks the trade, not the LLM cost.

The LLM was being called on every Stage 1 pass regardless of
article age. Stale articles (Google News-surfaced historical
content) burned LLM budget for no benefit.

## Cost impact (pre-fix)

Read-only query against the deployed DB (run 2026-04-26):

```sql
SELECT COUNT(*) AS classifications_total,
       SUM(CASE WHEN n.raw_published_ts < datetime('now','-24 hours')
             THEN 1 ELSE 0 END) AS classifications_on_stale,
       SUM(c.cost_micro_usd) / 10000 AS total_cost_cents,
       SUM(CASE WHEN n.raw_published_ts < datetime('now','-24 hours')
             THEN c.cost_micro_usd ELSE 0 END) / 10000 AS wasted_cost_cents
FROM llm_classifications c
JOIN news_events n ON n.id = c.news_event_id
WHERE c.classified_at > datetime('now', '-7 days');
```

Result: **0 rows.** No LLM calls had fired in the last 7 days yet
because the cascade was wired in PR #22 (Phase 4 Part 2.8) on
2026-04-26 and the daemon has been redeploying through several
PRs since. Most existing matches predate the cascade.

Measurable cost impact today: **$0**. Structural risk going
forward: real, especially while
`reuters_via_gnews` / `ap_via_gnews` / `wapo_via_gnews` /
`semafor_via_gnews` were active. The investigation showed those
four sources together ingested ~709 articles in 7 days, of which
~706 were >24h stale. Each would have triggered a Stage 1 pass on
the aggressive pre-filter, sending hundreds of stale-article LLM
calls per day to Anthropic.

## Fix shipped in this PR

Two complementary changes:

1. **Source-list cleanup** (deliverables 1-3) removed all four
   stale-content proxies. Direct feeds rarely surface articles
   older than ~48 hours, so the volume of stale articles
   reaching Stage 1 plummets.

2. **Freshness guard in `MatcherWorker._classify_and_patch`**
   (this deliverable) — added an explicit check at the top of the
   loop that classifies passed events:

   ```python
   if _article_is_stale(raw_published_ts, STALE_ARTICLE_HOURS):
       # Skip the LLM call; patch row to keyword_only with
       # match_reason='skipped_stale'.
       continue
   ```

   Constant: `STALE_ARTICLE_HOURS = 48`. Threshold chosen to be
   strictly greater than the engine's 24h article-window check so
   an article that *just* squeaks inside the engine's window still
   reaches Stage 2.

   `_article_is_stale` treats `None`, empty string, and
   unparseable timestamps as STALE (fail-closed) — better to skip
   the LLM than to spend budget on an article whose publish time
   we can't verify.

## Pinned by tests

`tests/test_rss_freshness_guard.py` (new):

- `test_constant_is_48_hours` — pins the threshold.
- `test_constant_is_above_engine_window` — pins
  `STALE_ARTICLE_HOURS > 24` so the freshness guard never skips
  an article the engine would have admitted.
- `test_*` over `_article_is_stale` for None / empty / unparseable
  / fresh / boundary / old / Z-suffix / TZ-offset / naive ISO.

## Post-fix pipeline order

```
RSS poller
   ↓ (insert news_events row)
MatcherWorker._process_batch
   ↓ (Stage 1 keyword pre-filter)
MatcherWorker._classify_and_patch
   ├─ FRESHNESS GUARD (new)  →  if stale: write keyword_only/skipped_stale row, continue
   └─ Stage 2 LLM cascade (only for articles ≤ 48 h old)
   ↓ (patch news_market_matches row)
DecisionEngine.evaluate_news_match
   ↓ rule 1: interaction_occurred (LLM gate)
   ↓ rule 4: article-window check (still in place; 24 h)
   ↓ rule 5-11: price ceiling, caps, walk, FOK
TradeIntent
```

The freshness guard is not on the hot path for direct feeds (their
articles are always ≤ a few hours old). It's a defense-in-depth
layer for the rare case a future source begins surfacing
historical content.
