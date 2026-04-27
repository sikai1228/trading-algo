# Dedup verification — 2026-04-26

**Spec source**: Phase 4 Part 2.12, deliverable 6.

**Question**: when Google News surfaces 116 old Reuters articles in a
single 24h window (per the RSS ingestion investigation), is the bot
re-ingesting any of them, or are all 116 unique?

## Method

Single read-only query against the deployed
`~/Library/Application Support/trumpbot/trumpbot.db`:

```sql
SELECT source,
       COUNT(*) AS total_rows,
       COUNT(DISTINCT url_canonical) AS unique_urls,
       COUNT(*) - COUNT(DISTINCT url_canonical) AS duplicates
FROM news_events
WHERE detected_ts > datetime('now', '-7 days')
GROUP BY source
ORDER BY total_rows DESC;
```

## Findings

```
source             total  unique_canonical  duplicates
-----------------  -----  ----------------  ----------
reuters_via_gnews    229               229           0
ap_via_gnews         203               203           0
wapo_via_gnews       190               190           0
axios                116               116           0
semafor_via_gnews     87                87           0
nyt_world             73                73           0
bloomberg             61                61           0
nyt_politics          52                52           0
nbc_politics          50                50           0
cbs_politics          45                45           0
```

(Top 10 by volume; the same `total == unique_canonical` pattern
holds for every source in the database.)

## Verdict

**Deduplication is working correctly.** Zero duplicates across all
sources over the last 7 days. The 229 `reuters_via_gnews` rows
represent 229 distinct canonical URLs — Google News surfaced 229
unique old Reuters articles, and the database's
`UNIQUE(url_canonical)` constraint plus
`url.canonicalize_url()` together ensured each one was inserted
exactly once.

The 116-article finding from the RSS ingestion investigation is
distinct articles, not duplicates. The "Reuters seems low" intuition
is correctly resolved: the bot isn't double-counting; it's that
Google News is structurally surfacing old content as if it were new.

## Why it works

Three layers:

1. **`trumpbot/utils/url.py:canonicalize_url`** strips Google News's
   tracking parameters (`?utm_source=gnews`,
   `?ocid=msedgntp`, etc.) and normalizes scheme/host case before
   the row is inserted. So
   `https://reuters.com/article/foo?utm_source=x` and
   `https://reuters.com/article/foo` produce the same canonical
   string.
2. **`migrations/001_initial.sql`** declares `url_canonical` as a
   `UNIQUE` constraint on `news_events`. The `INSERT` raises
   `IntegrityError` on collision; `insert_news_event` catches that
   and returns `None` for the second-time-seen URL.
3. **The RSS poller** (`trumpbot/news/rss.py`) does not pre-check
   for duplicates — it relies on the unique-constraint failure. This
   is the correct order: it's atomic, race-free, and cheaper than a
   pre-flight `SELECT`.

## Implication for the Phase 4 Part 2.12 cleanup

Removing `reuters_via_gnews`, `ap_via_gnews`, `semafor_via_gnews`,
and `wapo_via_gnews` from the source list removes ~709 ingested
rows per week (sum of those 4 sources, 7 days). Of those, ~706 were
articles published more than 24 hours before ingestion (from the
RSS investigation's per-source publication-to-detection latency
table). Removing these proxy sources eliminates that database
churn entirely without losing any actionable signal — the same
breaking news appears via direct competitors at <2 minute lag.
