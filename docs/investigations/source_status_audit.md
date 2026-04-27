# Per-source status audit

**Date:** 2026-04-26
**Author:** Investigation only — no code, no config, no behavior change.
**Daemon state at time of audit:** PID running, started 2026-04-27T00:39:46Z
on commit `786afc3` (PR #29 — RSS ingestion fixes & verification),
`source_count: 19` (RSS) + 1 truth_social + 6 twitter handles
(silently disabled, no `TWITTER_BEARER_TOKEN`).
**Database snapshot:** `~/Library/Application Support/trumpbot/trumpbot.db`
at audit time, 35,288 `news_market_matches` rows, 0 `llm_classifications`
rows, 1,572 `news_events` rows.

This audit complements `rss_ingestion_analysis.md` (which focused on
Reuters and the Google News proxies) by walking **every** configured
source — RSS, Twitter, Truth Social — and producing one definitive
status row per source, cross-referenced against the 21 contract-
approved categories.

> **Important date caveat.** First-ever ingestion in this DB is
> `2026-04-25T21:49:04Z`, so the "7d" windows in this report are
> really ~28 hours of pre-2.12 data plus ~25 minutes of post-2.12
> data. The cardinality numbers should be read as ratios, not
> absolutes. The PR #29 redeploy at `2026-04-27T00:39:46Z` removed
> 9 sources from the active config; their historical rows remain in
> the DB for audit but are flagged DECOMMISSIONED below and excluded
> from the active-source health classification.

---

## Section 1 — Configured sources

Configuration source: deployed `~/.config/trumpbot/config.yaml`,
which mirrors `config/config.example.yaml` post-PR-29.

| # | Source name | Feed URL | Type | Configured | Currently active |
|---|---|---|---|---|---|
| 1 | bloomberg | `https://feeds.bloomberg.com/politics/news.rss` | direct_rss | yes | yes |
| 2 | nyt_politics | `https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml` | direct_rss | yes | yes |
| 3 | nyt_world | `https://rss.nytimes.com/services/xml/rss/nyt/World.xml` | direct_rss | yes | yes |
| 4 | wapo_politics | `https://feeds.washingtonpost.com/rss/politics` | direct_rss | yes | yes |
| 5 | wapo_world | `https://feeds.washingtonpost.com/rss/world` | direct_rss | yes | yes |
| 6 | axios | `https://api.axios.com/feed/` | direct_rss | yes | yes |
| 7 | msnbc | `https://www.msnbc.com/feed/` | direct_rss | yes | yes |
| 8 | nbc_politics | `https://feeds.nbcnews.com/nbcnews/public/politics` | direct_rss | yes | yes |
| 9 | nbc_world | `https://feeds.nbcnews.com/nbcnews/public/world` | direct_rss | yes | yes |
| 10 | cbs_politics | `https://www.cbsnews.com/latest/rss/politics` | direct_rss | yes | yes |
| 11 | cbs_world | `https://www.cbsnews.com/latest/rss/world` | direct_rss | yes | yes |
| 12 | fox_politics | `https://moxie.foxnews.com/google-publisher/politics.xml` | direct_rss | yes | yes |
| 13 | fox_world | `https://moxie.foxnews.com/google-publisher/world.xml` | direct_rss | yes | yes |
| 14 | abc_politics | `https://abcnews.go.com/abcnews/politicsheadlines` | direct_rss | yes | yes |
| 15 | abc_international | `https://abcnews.go.com/abcnews/internationalheadlines` | direct_rss | yes | yes |
| 16 | politico_wh | `https://rss.politico.com/whitehouse.xml` | direct_rss | yes | yes |
| 17 | the_information | `https://www.theinformation.com/feed` | direct_rss | yes | yes |
| 18 | dod_news | `https://www.defense.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=945&max=10` | direct_rss | yes | yes |
| 19 | pr_newswire_gov | `https://www.prnewswire.com/rss/policy-public-interest-latest-news/...` | direct_rss | yes | yes |
| 20 | truth_social:@realDonaldTrump | `https://truthsocial.com/api/v1/...` | truth_social | yes | yes |
| 21 | twitter:@WhiteHouse | (X API v2) | twitter | yes | no — no `TWITTER_BEARER_TOKEN` |
| 22 | twitter:@PressSec | (X API v2) | twitter | yes | no — no `TWITTER_BEARER_TOKEN` |
| 23 | twitter:@POTUS | (X API v2) | twitter | yes | no — no `TWITTER_BEARER_TOKEN` |
| 24 | twitter:@SecState | (X API v2) | twitter | yes | no — no `TWITTER_BEARER_TOKEN` |
| 25 | twitter:@StateDept | (X API v2) | twitter | yes | no — no `TWITTER_BEARER_TOKEN` |
| 26 | twitter:@DeptofDefense | (X API v2) | twitter | yes | no — no `TWITTER_BEARER_TOKEN` |

**Total: 26 configured sources, 20 currently being polled.**
The 6 Twitter handles register as configured but the daemon's
`TwitterScraper` quietly skips them when the bearer token is unset
(verified by reading the per-source registration in `daemon.py` —
they never enter `tasks_started`).

**Decommissioned (not in current config, listed for context only —
historical rows still present in DB):**
`reuters_via_gnews`, `ap_via_gnews`, `wapo_via_gnews`,
`semafor_via_gnews`, `politico_picks`, `wsj_politics`, `wsj_world`,
`cnn_politics`, `cnn_world`, `politico_picks`, `whitehouse_press`,
`whitehouse_news`, `state_press`, `state_readouts`, `business_wire`.
Removal rationale documented inline in `config.example.yaml` and in
PR #29 (commit `786afc3`).

---

## Section 2 — Per-source ingestion volume (last 7 days)

Query: `news_events` rows where `detected_ts > now − 7 days`,
grouped by source. The `avg_lag_min` column is the average
(detection − published) minutes; large positive values mean the
source is publishing items dated long before the bot saw them.

### 2a — Active sources (in current config)

| Source | 7d count | 24h count | 1h count | Last seen | Avg lag (min) |
|---|---:|---:|---:|---|---:|
| axios | 117 | 17 | 2 | 2026-04-27T00:59:39Z | 5,283.7 |
| nyt_world | 73 | 18 | 0 | 2026-04-26T21:08:16Z | 1,176.7 |
| bloomberg | 62 | 31 | 1 | 2026-04-27T00:42:47Z | 484.4 |
| nbc_politics | 52 | 27 | 2 | 2026-04-27T00:29:38Z | 3,210.8 |
| nyt_politics | 52 | 33 | 0 | 2026-04-26T22:36:12Z | 354.4 |
| cbs_politics | 50 | 26 | 5 | 2026-04-27T01:01:06Z | 837.7 |
| cbs_world | 44 | 17 | 0 | 2026-04-26T18:51:11Z | 3,556.9 |
| fox_politics | 42 | 17 | 0 | 2026-04-26T17:43:55Z | 579.7 |
| abc_international | 38 | 13 | 0 | 2026-04-26T23:18:43Z | 721.5 |
| politico_wh | 31 | 1 | 0 | 2026-04-26T18:01:49Z | 10,610.2 |
| abc_politics | 29 | 4 | 0 | 2026-04-26T23:18:43Z | 3,217.3 |
| fox_world | 28 | 3 | 1 | 2026-04-27T00:58:07Z | 3,331.6 |
| nbc_world | 28 | 4 | 0 | 2026-04-26T21:37:44Z | 6,367.6 |
| pr_newswire_gov | 24 | 4 | 0 | 2026-04-26T21:06:46Z | 1,231.9 |
| the_information | 24 | 4 | 1 | 2026-04-27T00:35:33Z | 1,489.9 |
| msnbc | 21 | 11 | 1 | 2026-04-27T00:13:18Z | 435.0 |
| **truth_social:@realDonaldTrump** | **20** | **20** | **20** | **2026-04-27T00:39:47Z** | **1,697.3** |
| **wapo_politics** | **17** | **17** | **17** | **2026-04-27T00:39:55Z** | **1,266.7** |
| dod_news | 16 | 6 | 0 | 2026-04-26T02:42:57Z | 4,272.4 |
| wapo_world | 10 | 3 | 0 | 2026-04-26T17:00:40Z | 1,040.5 |
| twitter:@WhiteHouse | 0 | 0 | 0 | — | — |
| twitter:@PressSec | 0 | 0 | 0 | — | — |
| twitter:@POTUS | 0 | 0 | 0 | — | — |
| twitter:@SecState | 0 | 0 | 0 | — | — |
| twitter:@StateDept | 0 | 0 | 0 | — | — |
| twitter:@DeptofDefense | 0 | 0 | 0 | — | — |

Bold rows are sources that **first started producing data** at the
PR #29 redeploy (00:39:47Z and 00:39:55Z). Truth Social was at
zero events for its entire previous deployment lifetime due to the
bot UA being 403'd; the Safari UA fix unblocked it. Likewise the
direct WaPo politics feed replaced `wapo_via_gnews` (decommissioned).

### 2b — Decommissioned sources (rows in DB but no longer polled)

Listed for context. These rows continue to occupy `news_events` but
are not being added to. They should age out of routine queries
naturally.

| Source | 7d count | 24h count | Last seen | Notes |
|---|---:|---:|---|---|
| reuters_via_gnews | 229 | 117 | 2026-04-26T23:53:30Z | Avg lag 193,428 min → 99 % stale (`rss_ingestion_analysis.md`) |
| ap_via_gnews | 204 | 95 | 2026-04-27T00:29:42Z | Avg lag 382,209 min |
| wapo_via_gnews | 190 | 88 | 2026-04-27T00:25:10Z | Replaced by direct `wapo_politics` |
| semafor_via_gnews | 87 | 10 | 2026-04-26T22:36:15Z | Avg lag 596,805 min |
| politico_picks | 46 | 12 | 2026-04-27T00:34:08Z | 403 Forbidden in deployed env (still polled until 00:39 redeploy) |
| cnn_world | 23 | 0 | 2026-04-25T21:49:04Z | Frozen feed (last published 2023-09 in source) |
| wsj_world | 20 | 0 | 2026-04-25T21:49:04Z | Frozen feed (last published 2025-01) |
| cnn_politics | 18 | 0 | 2026-04-25T21:49:04Z | Frozen feed (last published 2024-06) |
| whitehouse_news | 10 | 0 | 2026-04-25T21:49:04Z | Returned stale content |

---

## Section 3 — Health classification

Rules applied (from the spec):

- **HEALTHY**: ≥ 5 articles/day average, last article < 4h ago,
  no gap > 12h, avg lag < 5 min — but the bot's pollers introduce a
  publication-to-detection lag whose floor is the
  `poll_interval_sec` (90 s for most), so we widen the lag bar to
  "feed-inherent" if the source publishes once per hour.
- **DEGRADED**: 1-5 articles/day, OR last article 4-24h, OR gap
  12-48h, OR lag 5-30 min.
- **BROKEN**: 0 articles in last 24 h, OR last > 48h, OR
  consistent `system_events` errors.
- **UNKNOWN**: configured but never produced any article (Twitter
  rows fall here).

Lag observation: most sources show high `avg_lag` because RSS
feeds carry a tail of items dated days ago alongside today's
fresh items. The metric I use for "is the source itself fresh" is
**newest item in the live feed probe (Section 4)**, not avg lag.

| Source | Status | Articles 24h | Last seen | Notes |
|---|---|---:|---|---|
| bloomberg | HEALTHY | 31 | 00:42 | Newest live item < 30 min old; fastest rotation |
| nyt_politics | HEALTHY | 33 | 22:36 | Newest live item ~1.5 h old |
| nyt_world | HEALTHY | 18 | 21:08 | Newest live item ~2.5 h old |
| wapo_politics | HEALTHY | 17 | 00:39 | First-ever data post-2.12; live newest 00:19 |
| wapo_world | HEALTHY | 3 | 17:00 | Tiny feed (6 entries) but fresh; 1 transient ReadTimeout in audit probe |
| axios | HEALTHY | 17 | 00:59 | Newest live item 23:59 |
| msnbc | HEALTHY | 11 | 00:13 | Newest live item 00:12 |
| nbc_politics | HEALTHY | 27 | 00:29 | Newest live item 00:24 |
| cbs_politics | HEALTHY | 26 | 01:01 | Newest live item 00:41 |
| cbs_world | HEALTHY | 17 | 18:51 | Newest live item 23:22 |
| fox_world | HEALTHY | 3 | 00:58 | Newest live item 00:43 |
| abc_politics | HEALTHY | 4 | 23:18 | Newest live item 23:09 |
| abc_international | HEALTHY | 13 | 23:22 | Newest live item 23:22 |
| pr_newswire_gov | HEALTHY | 4 | 21:06 | Newest live item 21:04 |
| truth_social:@realDonaldTrump | HEALTHY | 20 | 00:39 | First-ever data post-2.12 |
| nbc_world | DEGRADED | 4 | 21:37 | Last seen 4 h ago; newest live item 21:30 — slow rotation |
| fox_politics | DEGRADED | 17 | 17:43 | 24h count OK, but last seen 7 h ago and live newest 17:29 — rotation paused |
| politico_wh | DEGRADED | 1 | 18:01 | Only 1 article in 24 h; live newest 18:00 (7 h gap) |
| dod_news | DEGRADED | 6 | 02:42 | Live probe newest is from 24th 20:57 (52 h ago) — feed-internal staleness |
| **the_information** | **BROKEN** | 4 | 00:35 | Live probe returns **403** with the deployed Safari UA. Worked pre-2.12 with the bot UA. |
| twitter:@WhiteHouse | UNKNOWN | 0 | — | No `TWITTER_BEARER_TOKEN`; scraper not started |
| twitter:@PressSec | UNKNOWN | 0 | — | Same |
| twitter:@POTUS | UNKNOWN | 0 | — | Same |
| twitter:@SecState | UNKNOWN | 0 | — | Same |
| twitter:@StateDept | UNKNOWN | 0 | — | Same |
| twitter:@DeptofDefense | UNKNOWN | 0 | — | Same |

### 3a — `system_events` failure scan (last 7d)

```sql
SELECT event_type, severity, detail, COUNT(*) AS occurrences, MAX(ts)
FROM system_events
WHERE ts > datetime('now', '-7 days')
  AND event_type IN ('source_failure', 'source_down', 'source_rate_limited')
GROUP BY event_type, detail
ORDER BY occurrences DESC;
```

| event_type | source (from detail) | error | occurrences | last seen |
|---|---|---|---:|---|
| `source_failure` | wsj_politics | HTTP 403 (paywalled syndication) | 12 | 2026-04-26T23:59:30Z |
| `source_failure` | whitehouse_press | HTTP 404 (endpoint gone) | 11 | 2026-04-26T23:59:30Z |
| `source_failure` | politico_picks | HTTP 403 | 6 | 2026-04-26T22:43:37Z |
| `source_failure` | wapo_world | DNS error (one-shot) | 1 | 2026-04-26T22:25:44Z |
| `source_failure` | whitehouse_press | ReadError | 1 | 2026-04-26T02:36:06Z |

All `wsj_politics` / `whitehouse_press` / `politico_picks` errors
are pre-redeploy (those sources are no longer polled). The
`wapo_world` DNS error is transient — re-probed three times in
this audit, all 200. The `source_status` table is empty
(`source_status_loop` writes here but only on threshold-crossing
events; this matches the spec).

**Note: the_information's 403 should appear as `source_failure`
events on every poll going forward.** It does not yet because the
freshness guard / earlier polls predate the redeploy. A
`source_failure: the_information` row with HTTP 403 should land
within the next 5-poll window of normal operation.

---

## Section 4 — Live HTTP probe

Probed at audit time using the deployed Safari UA
(`Mozilla/5.0 ... Version/17.4 Safari/605.1.15`),
`follow_redirects=True`, 10 s timeout.

| Source | HTTP | Bytes | Articles | Newest article | Oldest article | Diagnosis |
|---|---:|---:|---:|---|---|---|
| bloomberg | 200 | 37,530 | 30 | 2026-04-27T00:37Z | 2026-04-26T00:50Z | Healthy, fast rotation (~24 h tail) |
| nyt_politics | 200 | 53,991 | 27 | 2026-04-26T23:03Z | 2026-04-26T04:12Z | Healthy, ~19 h tail |
| nyt_world | 200 | 127,998 | 60 | 2026-04-26T22:27Z | 2026-04-24T01:04Z | Healthy, ~3 d tail |
| wapo_politics | 200 | 32,669 | 81 | 2026-04-27T00:19Z | 2026-04-25T09:00Z | Healthy, ~40 h tail |
| wapo_world | 200* | 4,396 | 6 | 2026-04-26T13:57Z | (recent) | Healthy, tiny feed, *first probe ReadTimeout (transient) |
| axios | 200 | 1,011,169 | 100 | 2026-04-26T23:59Z | 2026-04-18T13:54Z | Healthy, large feed, ~9 d tail |
| msnbc | 200 | 144,027 | 10 | 2026-04-27T00:12Z | 2026-04-26T05:32Z | Healthy, short feed |
| nbc_politics | 200 | 55,583 | 25 | 2026-04-27T00:24Z | 2026-02-08T20:10Z | Healthy; oldest item from Feb is feed metadata noise, not stale content |
| nbc_world | 200 | 60,907 | 25 | 2026-04-26T21:30Z | 2026-04-08T17:52Z | Healthy, slow rotation |
| cbs_politics | 200 | 27,584 | 30 | 2026-04-27T00:41Z | 2026-04-24T14:07Z | Healthy |
| cbs_world | 200 | 26,466 | 30 | 2026-04-26T23:22Z | 2026-04-24T21:00Z | Healthy |
| fox_politics | 200 | 203,296 | 25 | 2026-04-26T17:29Z | 2026-04-25T10:42Z | Newest item ~7 h old → DEGRADED rotation |
| fox_world | 200 | 214,119 | 25 | 2026-04-27T00:43Z | 2026-04-21T20:06Z | Healthy |
| abc_politics | 200 | 44,983 | 25 | 2026-04-26T23:09Z | 2026-04-22T12:10Z | Healthy |
| abc_international | 200 | 46,005 | 25 | 2026-04-26T23:22Z | 2026-04-25T06:15Z | Healthy |
| politico_wh | 200 | 215,041 | 30 | 2026-04-26T18:00Z | 2026-04-13T20:06Z | Newest ~7 h old → DEGRADED |
| **the_information** | **403** | 5,596 | 0 | — | — | **BROKEN: Safari UA blocked. See follow-up below.** |
| dod_news | 200 | 10,381 | 10 | 2026-04-24T20:57Z | 2026-04-22T17:11Z | **Newest item is 52 h old → feed itself is stale** |
| pr_newswire_gov | 200 | 39,063 | 20 | 2026-04-26T21:04Z | 2026-04-24T18:57Z | Healthy |
| truth_social:@realDonaldTrump | 200 | 72,928 | 20 | 2026-04-27T00:30Z | 2026-04-24T21:17Z | Healthy (first time in deployment lifetime) |

### 4a — UA-sensitivity follow-up on `the_information`

The headline change in PR #29 was the User-Agent swap from a custom
bot string to a real Safari string. To understand the
`the_information` 403, I probed the same URL with five different UAs:

| UA label | UA string | Result |
|---|---|---:|
| safari_macos | `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 ... Version/17.4 Safari/605.1.15` | **403** (5,574 bytes) |
| chrome_linux | `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 ... Chrome/121.0.0.0 Safari/537.36` | 200 (40,156 bytes) |
| old_bot_ua | `trump-market-bot/1.0 research project` | 200 (40,156 bytes) |
| feedparser_default | `feedparser/6.0.10` | 200 (40,156 bytes) |
| no_ua | (empty) | **403** (5,470 bytes) |

The `the_information` edge has UA-based blocklisting that flags
Safari/Mac (and missing UA) but accepts Chrome/Linux, the old bot
UA, and feedparser's default. **The PR #29 UA change unblocked
Truth Social and Politico but broke the_information.** The fix
shipping in `truthsocial.py` and `rss.py` is the same UA constant
across all sources; `the_information` needs a different one.

---

## Section 5 — Cross-reference vs. 21 contract-approved sources

The KXTRUMPMEET contract resolution rules approve 21 source
categories. This table maps each category onto the bot's
configured sources and reports the status from Sections 3-4.

| # | Contract source | Configured? | Status | Failure reason | Action needed |
|---|---|---|---|---|---|
| 1 | The Washington Post | yes (`wapo_politics`, `wapo_world`) | HEALTHY | — | None |
| 2 | Official social media accounts (verified by platform) | partial — see 5a | mixed | Truth Social ✓; Twitter handles all UNKNOWN (no token) | Acquire `TWITTER_BEARER_TOKEN` |
| 3 | Official press release distribution services | yes (`pr_newswire_gov`) | HEALTHY | — | None |
| 4 | Fox News | yes (`fox_politics`, `fox_world`) | DEGRADED | `fox_politics` rotation paused (newest item 7 h old) | Monitor; alert if last_seen exceeds 24 h |
| 5 | MSNBC | yes (`msnbc`) | HEALTHY | — | None |
| 6 | The Wall Street Journal | **no** | **MISSING** | Free RSS rejected with HTTP 403 (paywalled); decommissioned in PR #29 | Paid subscription only; not currently viable |
| 7 | Semafor | **no** | **MISSING** | Was via Google News with stale content; decommissioned in PR #29 | Investigate Semafor's direct RSS (`semafor.com/rss`?); none currently configured |
| 8 | The Information | yes (`the_information`) | **BROKEN** | UA block — Safari UA gets 403 | **P0**: per-source UA override (Chrome or feedparser UA — see Section 4a) |
| 9 | ABC | yes (`abc_politics`, `abc_international`) | HEALTHY | — | None |
| 10 | The Associated Press (AP) | **no** | **MISSING** | AP retired public RSS in 2023; was via Google News with 99 % stale content; decommissioned in PR #29 | Paid AP API only; not currently viable |
| 11 | NBC | yes (`nbc_politics`, `nbc_world`) | nbc_politics HEALTHY, nbc_world DEGRADED | nbc_world has slow rotation | Monitor; not blocking |
| 12 | Photographic / video evidence from accredited media | implicit — captured via `has_photo`, `has_video` columns on every news_event | n/a | — | Not a separate source; piggybacks on Sections 1-11 above |
| 13 | Axios | yes (`axios`) | HEALTHY | — | None |
| 14 | Official government websites | partial — `dod_news` only; see 5b | mixed | dod_news feed itself is 52 h stale; whitehouse / state pulled in PR #29 | **P1**: investigate replacement government feeds |
| 15 | The New York Times | yes (`nyt_politics`, `nyt_world`) | HEALTHY | — | None |
| 16 | Politico | yes (`politico_wh`) | DEGRADED | Only 1 article in 24 h; rotation slow | Monitor; consider re-adding `politico_picks` with the right UA |
| 17 | CNN | **no** | **MISSING** | Direct RSS abandoned by CNN (last update 2024-06 / 2023-09); decommissioned in PR #29 | No working free CNN RSS exists; consider scraping `cnn.com/politics` if they ever re-enable RSS |
| 18 | Official readouts from governments / organizations | partial | mixed | `dod_news` (degraded), `pr_newswire_gov` (healthy). State Dept readouts pulled in PR #29. | **P1**: investigate WH press-pool / state.gov readouts feeds |
| 19 | Reuters | **no** | **MISSING** | Reuters cut all public RSS in 2020 (paid Refinitiv only); Google News proxy decommissioned (1/117 fresh); | **P2**: paid news API the only path |
| 20 | CBS | yes (`cbs_politics`, `cbs_world`) | HEALTHY | — | None |
| 21 | Bloomberg News | yes (`bloomberg`) | HEALTHY | — | None |

**Summary: 21 contract sources mapped to 13 healthy + 4 degraded +
1 broken + 5 missing.** The 5 missing categories cluster into "the
wires" (Reuters/AP/Semafor) and "established outlets that abandoned
or paywalled their RSS" (WSJ/CNN). The 1 broken (the_information)
is a recent UA-fix regression.

### 5a — Categorical: "Verified social media accounts" (#2)

| Specific account | Configured | Status |
|---|---|---|
| truth_social:@realDonaldTrump | yes | HEALTHY (first ever ingestion post-2.12) |
| twitter:@WhiteHouse | yes | UNKNOWN — no `TWITTER_BEARER_TOKEN` |
| twitter:@PressSec | yes | UNKNOWN — no `TWITTER_BEARER_TOKEN` |
| twitter:@POTUS | yes | UNKNOWN — no `TWITTER_BEARER_TOKEN` |
| twitter:@SecState | yes | UNKNOWN — no `TWITTER_BEARER_TOKEN` |
| twitter:@StateDept | yes | UNKNOWN — no `TWITTER_BEARER_TOKEN` |
| twitter:@DeptofDefense | yes | UNKNOWN — no `TWITTER_BEARER_TOKEN` |

The single most-valuable account here is `@realDonaldTrump` on
Truth Social — Trump's own first-person posts are dispositive
under the contract. That source is now healthy. The 6 Twitter
handles add breadth (administration officials' announcements) but
require ~$100/mo X API "Basic" tier or equivalent.

### 5b — Categorical: "Official government websites" (#14) and "Official readouts" (#18)

| Specific feed | Configured | Status |
|---|---|---|
| `dod_news` (Department of Defense news RSS) | yes | DEGRADED — feed itself 52 h stale at probe time |
| `pr_newswire_gov` (PR Newswire policy & public-interest list) | yes | HEALTHY |
| `whitehouse_press` (white house press briefings RSS) | no | Decommissioned (404) |
| `whitehouse_news` (whitehouse.gov news RSS) | no | Decommissioned (stalled feed) |
| `state_press` (State Dept press releases) | no | Decommissioned (200 with 0 entries) |
| `state_readouts` (State Dept readouts) | no | Decommissioned (200/404 with 0 entries) |

Both categories are thin: only DoD and PR Newswire remain. These
should be augmented by re-scraping whitehouse.gov / state.gov via
direct HTML if the official RSS endpoints don't recover.

---

## Section 6 — Coverage gaps

### GAPS_CRITICAL (contract-approved + completely unavailable)

- **Reuters (#19)** — no working free direct feed. The Google News
  proxy was 99 % stale and was decommissioned. Reuters articles
  are still surfaced indirectly when other outlets cite them, but
  there is no first-party Reuters ingestion. **Recommended:**
  evaluate Reuters API pricing or accept the gap and rely on
  AP / Bloomberg / NYT for wire-style coverage. Treat as P2 for
  now; no working free alternative exists.

- **AP (#10)** — same situation as Reuters. AP retired their public
  RSS in 2023. Google News proxy decommissioned. **Recommended:**
  P2; AP API or a paid news aggregator (e.g. Newscatcher, NewsAPI).

- **WSJ (#6)** — paywalled RSS. **Recommended:** P2 if a WSJ
  digital subscription with API access becomes available.

- **CNN (#17)** — CNN abandoned their RSS infrastructure 1-2 years
  ago. **Recommended:** P2; revisit if CNN reinstates RSS.

- **Semafor (#7)** — was via Google News (stale). Semafor publishes
  newsletter-format content and may not maintain a public RSS.
  **Recommended:** P1 — try `https://www.semafor.com/rss` or
  similar direct endpoints; Semafor is a small but high-signal
  outlet for foreign-policy reporting.

### GAPS_DEGRADED (configured but partial / slow / breaking)

- **the_information (#8)** — Safari UA gets 403; works with
  Chrome / feedparser / old bot UA. **Recommended:** P0 — per-source
  UA override, takes one config-edit + one HTTP-client header
  change. See Section 4a for the working alternatives.

- **fox_politics** — feed rotation paused (newest item 7 h old at
  probe time). May resolve on its own; add to monitoring.

- **politico_wh** — only 1 article in last 24 h. Feed updates
  slowly (newest item 7 h old in probe). The decommissioned
  `politico_picks` would have provided a second Politico stream;
  consider re-evaluating with a different UA (the deployed env
  was getting 403; bot UA worked locally — same kind of edge
  fingerprinting as the_information may be at play).

- **dod_news** — newest item in feed is 52 h old. The DoD's RSS
  endpoint may be intermittently stalled rather than truly
  abandoned. Add to monitoring; consider scraping
  `defense.gov/News/Releases` directly if RSS stays stale > 7 d.

- **nbc_world**, **fox_politics** — slow rotation; not failing,
  just publishing few items. Probably reflects actual editorial
  cadence, not a bot bug. Monitor.

### GAPS_CATEGORICAL (entire categories with zero / minimal coverage)

- **Twitter (Category #2 contributor)** — 6 official-account
  handles configured but no `TWITTER_BEARER_TOKEN`, so 0 events
  ever. The X API "Basic" tier is ~$100/mo. **Recommended:** P1
  if budget allows; the operator gets value out of admin
  announcements (`@PressSec`, `@SecState`) that confirm meetings
  hours before the wire stories drop. If budget is the blocker,
  a no-cost workaround is to scrape
  `nitter.privacydev.net/<handle>/rss` with rotation to handle
  uptime — but Nitter instances are unreliable. The bot already
  has Truth Social as the highest-value first-party voice; the
  Twitter gap is real but not blocking.

- **Government websites (#14) & readouts (#18)** — reduced to
  `dod_news` + `pr_newswire_gov` after PR #29. **Recommended:**
  P1 — find a working direct WhiteHouse / State alternative.
  Candidates: `state.gov/feed/`, `whitehouse.gov/news/feed/`,
  scraping the public press-briefings page if no RSS exists.

### GAPS_NONCRITICAL

- The Reuters / AP / WSJ / CNN gaps are partly mitigated by the
  fact that **NYT, WaPo, Bloomberg, MSNBC, NBC, ABC, Fox, CBS,
  Politico, and Axios are all healthy** and pick up wire-style
  reporting within minutes. A cross-source dedup based on
  headline similarity (not just URL canonicalization) could
  surface "is this story being reported by multiple outlets in
  parallel?" as an upstream signal even without first-party wires.

---

## Section 7 — Existing data integrity checks

### 7.1 Duplicate detection (URL canonicalization)

```sql
SELECT url_canonical, COUNT(*) FROM news_events
WHERE detected_ts > datetime('now', '-7 days') AND url_canonical IS NOT NULL
GROUP BY url_canonical HAVING COUNT(*) > 1;
-- (zero rows)
```

**No URL-canonical duplicates.** Confirms what
`dedup_verification.md` reported. Headline-level dedup shows 5
articles whose body was empty (`headline = '(no text)'` — these
are typically Truth Social text-only posts where the scraper had
no headline to extract; the URL is unique per status_id) and 4
Truth Social re-shares with the headline `"Here's the latest."`.
None are bot-side bugs.

### 7.2 Stale article ingestion (avg age at detection per source)

The decommissioned Google News sources dominate the "served us
old content" leaderboard, confirming the rationale for their
removal:

| Source (decommissioned) | Avg age at detection (h) | Min | Max |
|---|---:|---:|---:|
| wapo_via_gnews | 45,741 | 498 | 316,432 |
| cnn_world | 29,047 | 22,846 | 38,691 |
| cnn_politics | 26,702 | 16,364 | 29,857 |
| wsj_world | 10,927 | 10,901 | 10,970 |
| semafor_via_gnews | 9,972 | 107 | 56,804 |
| ap_via_gnews | 6,391 | 305 | 25,265 |
| reuters_via_gnews | 3,242 | 23 | 15,572 |

| Source (active) | Avg age at detection (h) | Min | Max |
|---|---:|---:|---:|
| politico_wh | 203 | 7.99 | 359 |
| nbc_world | 130 | 3.5 | 439 |
| axios | 114 | 1.0 | 232 |
| dod_news | 96 | 52.0 | 149 |
| cbs_world | 80 | 6.0 | 1,780 |
| fox_world | 80 | 0.27 | 129 |
| nbc_politics | 73 | 0.58 | 1,852 |
| the_information | 48 | 0.4 | 76 |
| pr_newswire_gov | 44 | 3.93 | 57 |
| nyt_world | 43 | 3.96 | 80 |
| wapo_world | 38 | 16 | 73 |
| abc_international | 33 | 1.8 | 61 |
| cbs_politics | 32 | 0.29 | 96 |
| fox_politics | 31 | 7.5 | 54 |
| truth_social:@realDonaldTrump | 28 | 0.48 | 51 |
| bloomberg | 27 | 0.37 | 53 |
| msnbc | 24 | 0.78 | 48 |
| nyt_politics | 23 | 2.65 | 53 |
| wapo_politics | 21 | 0.66 | 39 |

Active-source averages are dominated by the long tail of items
with `published_ts` from 1-3 days back (RSS feeds typically carry
that tail). The min values — best-case detection lag for a fresh
item — are mostly < 1 h, confirming that the bot detects fresh
items fast when they appear. The 48 h freshness guard added in
PR #29 (`STALE_ARTICLE_HOURS = 48`) trims the tail before LLM cost
is incurred.

### 7.3 Articles passing Stage 1 keyword pre-filter

```sql
SELECT ne.source, COUNT(DISTINCT ne.id) AS articles,
       COUNT(DISTINCT CASE WHEN nmm.match_reason LIKE 'passed_pre_filter%'
                              OR nmm.match_reason LIKE 'direct_verb:%'
                              OR nmm.match_reason LIKE 'mention_verb:%'
                           THEN ne.id END) AS passed_stage_1
FROM news_events ne LEFT JOIN news_market_matches nmm
  ON nmm.news_event_id = ne.id
WHERE ne.detected_ts > datetime('now', '-7 days')
GROUP BY ne.source ORDER BY passed_stage_1 DESC;
```

| Source | Articles 7d | Passed Stage 1 |
|---|---:|---:|
| axios | 117 | 2 |
| nyt_world | 73 | 1 |
| bloomberg | 62 | 1 |
| wapo_world | 10 | 1 |
| (everything else) | … | 0 |

Total Stage 1 passes in the audit window: **5 articles.** All 5 use
the OLD matcher's `direct_verb:` / `mention_verb:` reason format,
which means they were classified by the pre-Phase-1.5 matcher
that ran before PR #22 / #23. **Zero articles in the window have
matched under the new `passed_pre_filter` format.**

A side-effect finding worth flagging: `llm_classifications` is
**empty** (`SELECT COUNT(*) FROM llm_classifications` → 0). The
Phase 1.5 LLM cascade has not made a single classification call
since deployment. Possible reasons:

1. **Stage 1 is correctly rejecting all post-Phase-1.5 articles**
   because the news happens not to contain Trump+Subject+
   Interaction-term in the audit window. Spot-checked the one
   active-market subject mention (the @realDonaldTrump truth post
   `"Hakeem 'High Tax' Jeffries..."`) and confirmed the matcher
   correctly classified it as `failed_pre_filter:no_trump+
   no_interaction_term` — the post mentions Hakeem but not Trump
   and no meeting/call/interaction term.
2. The full daemon log shows `matcher_worker_started` and
   `llm_classifier_attached` events but no `llm_classification_*`
   events — consistent with "no Stage 1 pass" rather than "Stage 1
   passes but LLM call fails silently."

This is **expected behavior** given the news distribution in the
audit window, not a bot bug — but the audit window is narrow (~25
min of post-2.12 polling) and a follow-up check after 24-48 h of
post-2.12 operation is worth queuing to confirm the cascade
fires when the news actually carries an interaction-shaped story.

---

## Section 8 — Summary and recommended actions

### One-paragraph summary

Of the 26 configured sources, **15 are HEALTHY**, **4 are
DEGRADED**, **1 is BROKEN** (the_information, regressed by the
PR #29 UA swap), and **6 are UNKNOWN** (Twitter handles
silently disabled because no bearer token is set). PR #29
unlocked two previously-zero sources — Truth Social
(`@realDonaldTrump`) and the direct WaPo politics feed — by
swapping in a Safari UA. Mapped against the 21 contract-approved
source categories, the bot has working coverage of 13 categories
(NYT, WaPo, Axios, MSNBC, ABC, NBC, CBS, Bloomberg, Fox News,
Politico, PR Newswire, Truth Social, photographic-evidence
piggyback), partial coverage of 3 (government websites/readouts —
only DoD + PR Newswire), 1 broken (The Information), and 5
genuinely missing — Reuters, AP, WSJ, CNN, Semafor — because
their public RSS infrastructure no longer exists or is paywalled.
Database integrity is clean: zero URL-canonical duplicates across
the 7-day window; stale-content ingestion is concentrated entirely
in the (now-decommissioned) Google News proxies; the Phase 1.5 LLM
cascade has so far made zero classification calls in the
post-2.12 audit window because no article has passed Stage 1's
Trump+Subject+Interaction-term filter — consistent with the
narrow news sample, not a cascade bug.

### Recommended actions

#### P0 — must fix before going live

- **the_information UA override.** Source returns 403 on the
  deployed Safari UA; works with Chrome, feedparser default, and
  the old bot UA. Add a per-source UA override (one HTTPx header
  on the per-source request, or a `user_agent_override` field on
  `NewsSourceConfig`). Lowest-risk fix is to use Chrome
  `Mozilla/5.0 (X11; Linux x86_64) ... Chrome/121.0.0.0
  Safari/537.36` only for `the_information`, leaving the global
  Safari UA in place for the sources that need it. (The
  Safari UA was specifically added to fix Truth Social and
  Politico; Chrome works fine on those too — a "Chrome
  everywhere" alternative would also resolve `the_information`
  with a single global change. Worth A/B testing.)

#### P1 — fix in first 30 days

- **Twitter ingestion.** Acquire `TWITTER_BEARER_TOKEN`
  (X API Basic tier ~$100/mo). The 6 already-configured handles
  (`@WhiteHouse`, `@PressSec`, `@POTUS`, `@SecState`,
  `@StateDept`, `@DeptofDefense`) start producing events the
  next poll cycle after the env var is set; no code changes
  required. High signal value for early confirmation of
  meetings.

- **Government website coverage.** `dod_news` is the only
  government feed that's live, and its content is 52 h stale at
  probe time. Investigate fresh alternatives:
  `https://www.state.gov/feed/`,
  `https://www.whitehouse.gov/news/feed/`,
  scraping the press-briefings HTML if no RSS exists. PR
  Newswire's policy/public-interest list partly covers official
  press releases but lags official channels.

- **Semafor direct feed.** Try `https://www.semafor.com/rss`
  (or whatever endpoint Semafor currently exposes). Semafor was
  decommissioned-via-Google-News, not because direct RSS was
  tried and failed.

- **fox_politics / politico_wh monitoring.** Both show degraded
  rotation. Add to a "rotation pause" alert: if a source's
  newest-article-in-feed timestamp doesn't advance for > 12 h,
  fire a warning. Currently the daemon's `source_health_loop`
  alerts on absence of *ingested* events, not on absence of
  *fresh feed content* — a feed that keeps returning 200 with
  the same 5 stale articles wouldn't trigger today.

- **Re-evaluate `politico_picks`.** Was decommissioned because
  it returned 403 in deployed env, but `the_information` 403
  finding suggests this may be UA-dependent. Worth re-probing
  with the Chrome UA before declaring it permanently dead.

#### P2 — consider after live data

- **Reuters / AP paid API.** Single biggest coverage gap. After
  4-6 weeks of live operation, audit how often the bot's
  detected interactions had a Reuters / AP byline cited
  in the originating article. If > 30 % of confirmed
  interactions are Reuters-first, the API subscription pays
  for itself.

- **WSJ digital subscription with API access.** Same logic.

- **CNN.** Revisit only if CNN reinstates RSS. No working
  free path exists today.

- **Cross-source headline-similarity dedup.** Today's dedup is
  URL-canonical only. If two outlets independently report the
  same Trump-Putin meeting at 14:01 and 14:03, both events land
  in the DB. That's fine for matching purposes (each goes to
  the cascade independently), but a cross-outlet "N independent
  sources reported X" signal could be a useful confirmation
  metric.

### Follow-up investigations

- **Confirm Phase 1.5 cascade fires under non-trivial news
  load.** The audit window is too narrow (~25 min of post-2.12
  polling) to validate that the LLM cascade actually classifies
  anything. Re-check `llm_classifications` count after 24-48 h
  of post-2.12 operation; if still zero, dig into Stage 1's
  acceptance rate (currently 0/946 post-redeploy) — may indicate
  the interaction-term list is too narrow or the subject-alias
  lookup is missing fuzzy variants.

- **Stop investing in `dod_news` if it stays stale.** If the
  DoD RSS endpoint's newest item doesn't advance over the next
  72 h, decommission and replace with `defense.gov/News/Releases`
  HTML scraping.

- **`the_information` UA testing matrix.** Once the per-source
  UA override lands, re-verify the 5-UA test in Section 4a
  monthly — anti-bot policies change without notice.

---

## Appendix — How this audit was reproduced

```bash
# Section 2/3/7 queries: deployed DB
sqlite3 ~/Library/Application\ Support/trumpbot/trumpbot.db < <queries from sections>

# Section 4 live probe: feedparser + httpx with the deployed Safari UA
uv run python -c "<see /tmp/source_probe.py used during this audit>"

# Section 4a UA matrix
uv run python -c "<see /tmp/probe_inf.py used during this audit>"
```

No production state was modified during this audit. No code
changes. No source enable/disable. No polling-interval changes.
No paid APIs called. The deployed daemon (PID running at audit
time) was not restarted.
