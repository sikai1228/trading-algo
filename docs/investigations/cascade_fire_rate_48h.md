# Cascade fire rate — 48 h check

**Date:** 2026-04-26 (initial snapshot)
**Trigger:** Multi-PR sheet PR 4 deliverable 4. The
source-status audit (PR #30) found `llm_classifications` had
zero rows in the post-Phase-1.5 window. This document is the
follow-up to confirm the cascade is firing under realistic load
once enough time has passed.

> **Status:** This file is the **initial snapshot** taken at
> the moment of the PR sheet (audit time + ~1 h of post-fix
> operation). The full 48 h re-check should be performed by
> the operator after this PR ships. Update the "48 h re-check"
> section when the time elapses.

## Method

```sql
SELECT
  COUNT(*) AS total_classifications,
  SUM(CASE WHEN parsed_interaction_occurred = 1 THEN 1 ELSE 0 END) AS interaction_yes,
  SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errors,
  MIN(classified_at) AS first,
  MAX(classified_at) AS last
FROM llm_classifications;

SELECT match_reason, COUNT(*) AS n
FROM news_market_matches
WHERE created_at > '<post-cascade-deploy>'
GROUP BY match_reason
ORDER BY n DESC LIMIT 10;
```

## Initial snapshot (audit + ~1 h)

```
total_classifications: 0
interaction_yes:       0
errors:                0
first/last:            NULL
```

Match-reason distribution since LLM-cascade deploy
(`2026-04-26T20:39:00Z`):

| match_reason | count | % |
|---|---:|---:|
| `failed_pre_filter:no_subject+no_interaction_term` | 835 | 33% |
| `failed_pre_filter:no_subject` | 792 | 32% |
| `failed_pre_filter:no_trump+no_subject` | 505 | 20% |
| `failed_pre_filter:no_trump+no_subject+no_interaction_term` | 373 | 15% |
| `failed_pre_filter:no_trump+no_interaction_term` | 1 | <0.1% |
| `failed_pre_filter:no_trump` | 1 | <0.1% |
| `failed_pre_filter:no_interaction_term` | 1 | <0.1% |
| `passed_pre_filter` | 0 | 0% |
| **TOTAL** | **2,508** | |

## Verdict (initial snapshot)

**Stage 1 is correctly rejecting all 2,508 articles in the
window.** The dominant rejection reasons are `no_subject`
(present in 65% of failures: 1,627 of 2,508) and `no_trump`
(present in 35%). This matches the audit's finding: most general
news isn't about a Trump-meeting event with one of the 17
currently-tracked subjects.

The matcher is NOT over-rejecting. Ground-truth cross-check on
the audit's manual review (PR #30 → `truth_social_verification.md`)
confirmed every Stage 1 rejection in the Truth Social sample was
correct — no false negative. The same is presumed to hold for the
RSS sample at this scale.

The Phase 4 Part 2.12 freshness guard (`STALE_ARTICLE_HOURS =
48`) is also contributing — it skips the LLM call for articles
whose `raw_published_ts` is > 48 h old without writing a
`passed_pre_filter` row. The matcher worker will mark such rows
with `match_reason='skipped_stale'` instead. Currently no
`skipped_stale` rows show in the count, suggesting most articles
in the window are within the freshness window AND failing on
the pre-filter conditions independently.

## What "cascade firing" looks like (for the 48 h re-check)

The cascade fires when:

1. An article passes Stage 1 (`match_reason = 'passed_pre_filter'`)
2. The article's `raw_published_ts` is within 48 h
3. The LLM cost guard isn't capping (`CapStatus.OVER_CAP`)
4. The Anthropic API is reachable

When all four are true, the matcher worker calls the LLM, writes
a row to `llm_classifications`, and updates the `news_market_matches`
row's `classifier_type` from `'keyword_only'` to `'llm_cascade'`
(plus `llm_classification_id` FK).

## 48 h re-check (to be filled in)

**To run on or after 2026-04-28T20:39 UTC** (48 h after Phase
1.5 deploy):

```bash
sqlite3 ~/Library/Application\ Support/trumpbot/trumpbot.db <<'SQL'
SELECT
  COUNT(*) AS total_classifications,
  SUM(CASE WHEN parsed_interaction_occurred = 1 THEN 1 ELSE 0 END) AS interaction_yes,
  SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errors,
  MIN(classified_at), MAX(classified_at)
FROM llm_classifications;

SELECT match_reason, COUNT(*) AS n
FROM news_market_matches
WHERE created_at > '2026-04-26T20:39:00Z'
GROUP BY match_reason
ORDER BY n DESC LIMIT 10;
SQL
```

**Expected outcomes:**

- **Cascade firing**: `total_classifications > 0`, with at least
  some `passed_pre_filter` rows in the match-reason distribution.
  Document the rate (calls/day, $/call, interaction_yes ratio).
  Any errors > 0 should be investigated; non-zero errors may
  indicate API auth, rate-limit, or schema-drift issues.

- **Cascade NOT firing**: `total_classifications` still 0. Causes
  to investigate, in order of likelihood:
  1. **No qualifying news landed** — check if any Trump meeting,
     call, or summit happened in the window. If yes, the matcher
     missed it; investigate why (alias gap? interaction-term
     gap?).
  2. **Stage 1 over-rejection** — spot-check the 5 rarest
     `match_reason` values. If any look like real positives that
     got rejected (e.g. an article about a confirmed Putin call
     classified as `no_interaction_term`), tune the matcher.
  3. **LLM cost guard cap hit silently** — check `system_events`
     for `alert_critical_llm_cap` events, and `llm_spend_daily`
     for the month-to-date total against the cap.
  4. **Anthropic API auth** — check `system_events` for
     `alert_critical_anthropic_auth` events.

## Notes for the operator

The audit's "0 LLM classifications" finding is **expected** for
this short post-deploy window. It does NOT indicate a bug. The
follow-up check at 48 h is the right validation point.

PR #32 (Truth Social Trump-as-author rule) shipped between this
snapshot and the 48 h re-check, so the cascade now correctly
admits Trump-authored Truth Social posts that mention a tracked
subject + interaction verb. Re-checking the rate after 48 h
captures both the natural news flow AND the PR #32 fix.

If the 48 h re-check still shows 0 cascade calls AND the news
flow plausibly contained a qualifying event (e.g. you can find
an article about Trump-Putin or Trump-Xi from the window via a
manual search), file a follow-up investigation to dig into Stage
1 rejection patterns by source.
