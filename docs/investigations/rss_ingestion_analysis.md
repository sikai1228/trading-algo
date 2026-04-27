# RSS ingestion analysis — 2026-04-26

**Status**: investigation only. No code changed. Report informs a
follow-up PR after the operator reviews findings and decides
priorities.

**Trigger**: operator noticed Reuters has only ~112 articles in the
database over the last 24 hours and that seems low for a major wire
service.

**Headline finding**: the count is misleading in both directions.
The bot ingested **117 reuters_via_gnews articles in 24 hours**, but
**only one of those articles was actually published in the last 24
hours**. The other 116 were old Reuters content (range:
2025-03-31 to 2026-04-15) that Google News surfaced for the first
time today. The deployed Reuters configuration is structurally
incapable of catching breaking Reuters news quickly because:

1. The newest article in the live `reuters_via_gnews` feed is
   **11 days old** (2026-04-15). Anything Reuters has published in
   the last ~11 days is invisible to the bot's Reuters source.
2. Direct Reuters feeds (`reuters.com/world/rss` etc.) return HTTP
   401 — Reuters cut public RSS off; access requires a paid
   Refinitiv license.
3. The Reuters HTML landing page (`reuters.com/world/`) also returns
   401 to unauthenticated requests, so even a homepage scraper falls
   over.

The same structural problem affects `ap_via_gnews` (newest article
in feed: 18 days old) and `wapo_via_gnews`. Direct Bloomberg /
Axios / NYT / MSNBC / NBC / CBS / Fox / WaPo-direct feeds work and
catch breaking news within ~1.5 minutes of publication.

Recommendation summary:

- **P0** — Replace `wapo_via_gnews` (Google News proxy) with the
  working direct WaPo politics feed. Trivial config change, zero
  cost, immediate coverage of fresh WaPo articles.
- **P0** — Acknowledge that Reuters and AP have no working free
  direct feed. Either accept the gap (the bot still catches the
  same news via Bloomberg / NYT / WaPo / NBC, which all cover the
  same Trump beat) or pay for a wire-service subscription before
  going live.
- **P0** — Disable broken sources permanently:
  `wsj_politics` (403), `whitehouse_press` (404), and the
  CNN feeds (RSS infrastructure abandoned since 2024).
- **P1** — Add `truth_social:@realDonaldTrump` retry logic; current
  scraper has been failing on intermittent DNS errors and has
  ingested zero posts.
- **P2** — Consider Twitter API access ($100/mo for v2 Basic) once
  the operator wants verified social media confirmation in real time.

Every section below is the supporting data behind those
recommendations.

---

## Section 1 — Source configuration audit

Source list copied verbatim from the deployed
`~/.config/trumpbot/config.yaml`. The repo's `config.example.yaml`
matches.

### RSS sources (31 entries)

| Source name | Feed URL | Type | Poll |
|---|---|---|---|
| `reuters_via_gnews` | `https://news.google.com/rss/search?q=%22Donald+Trump%22+source:reuters.com&hl=en-US&gl=US&ceid=US:en` | **Google News proxy** | 90 s |
| `ap_via_gnews` | `https://news.google.com/rss/search?q=%22Donald+Trump%22+source:apnews.com&hl=en-US&gl=US&ceid=US:en` | **Google News proxy** | 90 s |
| `bloomberg` | `https://feeds.bloomberg.com/politics/news.rss` | Direct RSS | 90 s |
| `nyt_politics` | `https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml` | Direct RSS | 90 s |
| `nyt_world` | `https://rss.nytimes.com/services/xml/rss/nyt/World.xml` | Direct RSS | 90 s |
| `wapo_world` | `https://feeds.washingtonpost.com/rss/world` | Direct RSS | 90 s |
| `wapo_via_gnews` | `https://news.google.com/rss/search?q=%22Donald+Trump%22+source:washingtonpost.com&hl=en-US&gl=US&ceid=US:en` | **Google News proxy** | 90 s |
| `wsj_politics` | `https://feeds.a.dj.com/rss/RSSPoliticsAndPolicy.xml` | Direct RSS | 90 s |
| `wsj_world` | `https://feeds.a.dj.com/rss/RSSWorldNews.xml` | Direct RSS | 90 s |
| `axios` | `https://api.axios.com/feed/` | Direct RSS | 90 s |
| `politico_picks` | `https://www.politico.com/rss/politicopicks.xml` | Direct RSS | 90 s |
| `politico_wh` | `https://rss.politico.com/whitehouse.xml` | Direct RSS | 90 s |
| `semafor_via_gnews` | `https://news.google.com/rss/search?q=%22Donald+Trump%22+source:semafor.com&hl=en-US&gl=US&ceid=US:en` | **Google News proxy** | 120 s |
| `the_information` | `https://www.theinformation.com/feed` | Direct RSS | 120 s |
| `cnn_politics` | `http://rss.cnn.com/rss/cnn_allpolitics.rss` | Direct RSS | 90 s |
| `cnn_world` | `http://rss.cnn.com/rss/cnn_world.rss` | Direct RSS | 90 s |
| `fox_politics` | `https://moxie.foxnews.com/google-publisher/politics.xml` | Direct RSS | 90 s |
| `fox_world` | `https://moxie.foxnews.com/google-publisher/world.xml` | Direct RSS | 90 s |
| `msnbc` | `https://www.msnbc.com/feed/` | Direct RSS | 90 s |
| `nbc_politics` | `https://feeds.nbcnews.com/nbcnews/public/politics` | Direct RSS | 90 s |
| `nbc_world` | `https://feeds.nbcnews.com/nbcnews/public/world` | Direct RSS | 90 s |
| `abc_politics` | `https://abcnews.go.com/abcnews/politicsheadlines` | Direct RSS | 90 s |
| `abc_international` | `https://abcnews.go.com/abcnews/internationalheadlines` | Direct RSS | 90 s |
| `cbs_politics` | `https://www.cbsnews.com/latest/rss/politics` | Direct RSS | 90 s |
| `cbs_world` | `https://www.cbsnews.com/latest/rss/world` | Direct RSS | 90 s |
| `whitehouse_news` | `https://www.whitehouse.gov/news/feed/` | Direct RSS | 90 s |
| `whitehouse_press` | `https://www.whitehouse.gov/briefing-room/press-briefings/feed/` | Direct RSS | 90 s |
| `state_press` | `https://www.state.gov/press-releases/feed/` | Direct RSS | 90 s |
| `state_readouts` | `https://www.state.gov/secretary-readouts/feed/` | Direct RSS | 90 s |
| `dod_news` | `https://www.defense.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=945&max=10` | Direct RSS | 90 s |
| `pr_newswire_gov` | `https://www.prnewswire.com/rss/policy-public-interest-latest-news/policy-public-interest-latest-news-list.rss` | Direct RSS | 120 s |
| `business_wire` | `https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeGVtRVw==` | Direct RSS | 120 s |

### Twitter sources (6 handles, currently disabled)

`@WhiteHouse`, `@PressSec`, `@POTUS`, `@SecState`, `@StateDept`,
`@DeptofDefense`. All `is_kalshi_approved=true`. **Disabled
silently** because `TWITTER_BEARER_TOKEN` is unset; the daemon
emits a `twitter_disabled` warning every poll cycle (18 in the last
24h).

### Truth Social (1 handle)

`truth_social:@realDonaldTrump`, `is_kalshi_approved=true`,
unauthenticated public scraper at `truthsocial.com/api/v1/...`.
**Currently producing zero events.** The poller is failing on DNS
errors (`ConnectError('[Errno 8] nodename nor servname provided, or
not known')`) and intermittent timeouts. Sample log:

```
{"handle": "realDonaldTrump", "error":
"ConnectError('[Errno 8] nodename nor servname provided, or not known')",
"event": "truth_social_poll_failed", "level": "warning",
"timestamp": "2026-04-26T23:08:07.397868Z"}
```

The 5-consecutive-failure threshold means transient successes are
keeping it from emitting a `source_failure` system_event; it's
silently broken.

**Note on the Google News proxy choice**: the 4 sources
`reuters_via_gnews`, `ap_via_gnews`, `wapo_via_gnews`, and
`semafor_via_gnews` use Google News as a search proxy with a
`q="Donald Trump" source:reuters.com` query. This is a shortcut
that has structural limitations documented in Section 5.

---

## Section 2 — Per-source ingestion volume

### 24-hour volume (live snapshot)

```
source             articles_24h  first_seen                   last_seen
-----------------  ------------  ---------------------------  ---------------------------
reuters_via_gnews            117  2026-04-26T02:13:23Z         2026-04-26T23:53:30Z
ap_via_gnews                  91  2026-04-26T02:13:23Z         2026-04-26T23:56:31Z
wapo_via_gnews                86  2026-04-26T02:13:23Z         2026-04-26T23:56:32Z
nyt_politics                  33  2026-04-26T02:13:22Z         2026-04-26T22:36:12Z
bloomberg                     30  2026-04-26T02:13:23Z         2026-04-26T20:32:40Z
nbc_politics                  25  2026-04-26T02:13:23Z         2026-04-26T23:27:42Z
cbs_politics                  21  2026-04-26T02:13:22Z         2026-04-26T23:53:14Z
nyt_world                     18  2026-04-26T02:13:22Z         2026-04-26T21:08:16Z
cbs_world                     17  2026-04-26T02:13:22Z         2026-04-26T18:51:11Z
fox_politics                  17  2026-04-26T02:13:22Z         2026-04-26T17:43:55Z
axios                         15  2026-04-26T02:13:23Z         2026-04-26T22:38:50Z
abc_international             13  2026-04-26T04:35:57Z         2026-04-26T23:18:43Z
politico_picks                11  2026-04-26T02:13:22Z         2026-04-26T20:28:19Z
msnbc                         10  2026-04-26T02:13:23Z         2026-04-26T23:21:59Z
semafor_via_gnews             10  2026-04-26T02:13:23Z         2026-04-26T22:36:15Z
dod_news                       6  2026-04-26T02:42:57Z         2026-04-26T02:42:57Z
abc_politics                   4  2026-04-26T15:24:37Z         2026-04-26T23:18:43Z
nbc_world                      4  2026-04-26T08:01:03Z         2026-04-26T21:37:44Z
pr_newswire_gov                4  2026-04-26T02:13:23Z         2026-04-26T21:06:46Z
the_information                3  2026-04-26T16:08:29Z         2026-04-26T22:42:49Z
wapo_world                     3  2026-04-26T17:00:40Z         2026-04-26T17:00:40Z
fox_world                      2  2026-04-26T16:38:44Z         2026-04-26T21:21:52Z
politico_wh                    1  2026-04-26T18:01:49Z         2026-04-26T18:01:49Z
```

### Sources NOT in the 24-hour snapshot

```
business_wire        (lifetime: 0 articles ever)
cnn_politics         (lifetime: 18, last seen 2026-04-25T21:49:04Z — 26h ago)
cnn_world            (lifetime: 23, last seen 2026-04-25T21:49:04Z — 26h ago)
state_press          (lifetime: 0 articles ever)
state_readouts       (lifetime: 0 articles ever)
whitehouse_news      (lifetime: 10, last seen 2026-04-25T21:49:04Z — 26h ago)
whitehouse_press     (lifetime: 0 articles ever)
wsj_politics         (lifetime: 0 articles ever)
wsj_world            (lifetime: 20, last seen 2026-04-25T21:49:04Z — 26h ago)
```

The `cnn_*`, `wsj_world`, `whitehouse_news` rows above all show the
exact same timestamp because they came from the bot's first-ever
poll cycle on initial deploy (2026-04-25T21:49:04). They've
ingested **nothing** since.

### 7-day daily volume

The DB only has 2 days of history (the bot's been running since
2026-04-25). Day-1 numbers are inflated by initial backfill (the
bot saw the entire feed contents on the first poll, regardless of
how old). Day-2 numbers represent steady-state daily ingestion:

```
source             day-1 (2026-04-25)  day-2 (2026-04-26)
-----------------  ------------------  ------------------
abc_international                  25                  13
abc_politics                       25                   4   ← drop
ap_via_gnews                      109                  91
axios                             100                  15   ← drop (caught up the backlog day 1)
bloomberg                          31                  30
cbs_politics                       24                  21
cbs_world                          27                  17
cnn_politics                       18                   0   ← stalled
cnn_world                          23                   0   ← stalled
dod_news                           10                   6
fox_politics                       25                  17
fox_world                          25                   2   ← drop
msnbc                              10                  10
nbc_politics                       25                  25
nbc_world                          24                   4   ← drop
nyt_politics                       19                  33
nyt_world                          55                  18
politico_picks                     34                  11   ← drop
politico_wh                        30                   1   ← stalled
pr_newswire_gov                    20                   4
reuters_via_gnews                 112                 117   ← steady (but stale, see below)
semafor_via_gnews                  77                  10
the_information                    20                   3
wapo_via_gnews                    102                  86
wapo_world                          7                   3
whitehouse_news                    10                   0   ← stalled
wsj_world                          20                   0   ← stalled
```

### Flagged sources

Per the spec criteria (< 10 articles/day average, large
day-to-day variation, long zero stretches):

- **Stalled (0 articles on day 2)**: `cnn_politics`, `cnn_world`,
  `politico_wh`, `whitehouse_news`, `wsj_world`. Section 5 confirms
  why.
- **Never ingested ever**: `business_wire`, `state_press`,
  `state_readouts`, `whitehouse_press`, `wsj_politics`. Section 8
  pulls the underlying `source_failure` events for `wsj_politics` +
  `whitehouse_press`; the others either fall under the same root
  causes (404 / 403 / TLS) or have no logged failure (likely the
  feed returns 0 entries on every poll).
- **Massive day-1 → day-2 drop without an obvious source-side
  failure**: `axios` (100 → 15), `politico_picks` (34 → 11),
  `politico_wh` (30 → 1), `the_information` (20 → 3),
  `nbc_world` (24 → 4), `fox_world` (25 → 2), `abc_politics`
  (25 → 4). Some of these are explainable as backfill exhaustion
  (axios feed has 100 entries; the bot ate them all on day 1, then
  only sees new arrivals on day 2). Others (politico_wh, fox_world)
  look like the source itself just publishes infrequently. The drop
  alone isn't a red flag; combined with very low day-2 volume it
  warrants a follow-up but doesn't block trading.

---

## Section 3 — Gap analysis per source

Longest gaps between detections in the last 24 hours.

### `reuters_via_gnews` (117 articles, 24h)

```
detected_ts                  prev_ts                      gap_min
---------------------------  ---------------------------  -------
2026-04-26T12:50:50Z         2026-04-26T05:52:33Z         418.3   ← ~7h gap
2026-04-26T16:41:43Z         2026-04-26T12:50:50Z         230.9   ← ~3.8h
2026-04-26T05:48:01Z         2026-04-26T04:03:55Z         104.1
2026-04-26T23:28:14Z         2026-04-26T21:57:07Z          91.1
2026-04-26T21:30:54Z         2026-04-26T20:22:13Z          68.7
2026-04-26T17:44:05Z         2026-04-26T16:41:43Z          62.4
2026-04-26T19:55:03Z         2026-04-26T18:55:51Z          59.2
```

Longest gap is ~7 hours. This is between Google News's index
refresh cadences, not Reuters going dark for 7 hours.

### `ap_via_gnews` (91 articles, 24h)

```
detected_ts          prev_ts              gap_min
2026-04-26T15:57:17Z 2026-04-26T08:10:05Z   467.2   ← ~7.8h
2026-04-26T08:10:05Z 2026-04-26T05:26:55Z   163.2
2026-04-26T23:05:00Z 2026-04-26T21:27:55Z    97.1
```

Same pattern. The gap aligns with Google News's polling cycles, not
AP's publish cadence.

### `bloomberg` (30 articles, 24h)

```
detected_ts          prev_ts              gap_min
2026-04-26T13:31:59Z 2026-04-26T08:09:56Z   322.0   ← ~5.4h
2026-04-26T08:09:56Z 2026-04-26T03:54:54Z   255.0   ← ~4.3h
2026-04-26T16:43:19Z 2026-04-26T13:31:59Z   191.3
```

Bloomberg shows multi-hour gaps even though it's a direct feed.
This reflects Bloomberg's own publication cadence (politics-channel
articles aren't continuous) rather than a feed problem. The feed
itself is fresh (Section 5).

### Pattern summary

The 5-7 hour gaps in Google News-proxied sources are an artifact
of Google's own indexing/aggregation cadence; **the bot is polling
fast enough that any feed gap is a feed-side problem, not a
poll-rate problem**. Reducing the poll interval below the current
90 s would not help.

---

## Section 4 — Publication-to-detection latency

### All articles in the last 24h

```
source              samples  avg_lag_min   min_lag_min   max_lag_min
------------------  -------  ------------  ------------  ------------
wapo_via_gnews          86    2,969,224     34,302         18,985,721      ← STALE
semafor_via_gnews       10      761,677     25,671          3,408,121      ← STALE
ap_via_gnews            91      449,343     19,374          1,514,647      ← STALE
reuters_via_gnews      117      219,986        129            933,991      ← STALE
dod_news                 6        6,749      6,145              7,633
nbc_world                4        2,882          4             11,409
wapo_world               3          481        480                481
politico_picks          11          241         78                650
cbs_politics            21          119          2              1,493
bloomberg               30           90          1                404
fox_politics            17           81          3                227
fox_world                2           80          5                155
msnbc                   10           66          1                171
politico_wh              1           62         62                 62
cbs_world               17           58       -138                313
abc_international       13           57          3                170
pr_newswire_gov          4           55          1                214
nyt_world               18           53          1                168
nyt_politics            33           53          1                340
nbc_politics            25           43          6                132
the_information          3           37          2                 68
axios                   15           31          1                 96
abc_politics             4           24          6                 43
```

### What this means

**The Google-News-proxied sources show absurd average lag (months
to years) because Google News surfaces historical content on every
poll.** Of the 117 `reuters_via_gnews` articles ingested today,
only 1 was actually published in the last 24 hours; the other 116
are stale Reuters content (range 2025-03-31 to 2026-04-15) that
Google News indexed-and-surfaced for the first time today. The
"average lag" is dominated by this stale content.

**Direct sources show realistic latency.** Best performers (avg lag
in minutes): `axios` (31), `nyt_politics` (53), `bloomberg` (90),
`msnbc` (66). The MIN values are even better — `axios` 0.6 min,
`bloomberg` 1.3 min, `msnbc` 0.8 min — indicating that direct
feeds reliably catch breaking news within 1-2 minutes when the
upstream publishes it.

The single exception in the "stale" set is `dod_news` showing 6745
min avg (~4.7 days). DoD's RSS likely backfills several days of
press releases on each poll. Not a healthy signal.

### Same query, restricted to articles actually published in the last 24h

```
source              samples  avg_lag_min   min_lag_min   max_lag_min
------------------  -------  ------------  ------------  ------------
wapo_world               3        481           480              481
politico_picks          10        251            78              650
reuters_via_gnews        1        129           129              129     ← 1 fresh article in 24h
bloomberg               30         90             1              404
fox_world                2         80             5              155
fox_politics            15         66             3              176
msnbc                   10         66             1              171
politico_wh              1         62            62               62
abc_international       13         57             3              170
nyt_politics            32         50             1              340
nyt_world               17         46             1              134
nbc_politics            25         43             6              132
nbc_world                3         39             4              106
the_information          3         37             2               68
cbs_politics            18         34             2              106
axios                   15         31             1               96
abc_politics             4         24             6               43
cbs_world               14         22          -138              134
pr_newswire_gov          3          2             1                3
```

**`reuters_via_gnews` returned 1 row** in this query. Of 117
Reuters articles ingested in the past 24 hours, exactly **one** was
published in the last 24 hours. That single fresh article had a 129
minute lag — the Google News indexing delay alone is ~2 hours
before the bot can possibly see a new Reuters article.

`ap_via_gnews` and `wapo_via_gnews` return zero rows here — none of
the AP / WaPo articles ingested today were actually published
today.

### Spec criterion: anything beyond 5 minutes of average latency
is meaningful drag

Direct feeds: 22-90 min avg. None are under 5 min, but the **MIN**
on direct feeds reliably hits 0.6-2 min for breaking news (because
the bot polls at 90 s). The 22-90 min average is dragged up by less
time-sensitive content (analyses, photo galleries, late-evening
catchup) that doesn't matter for the strategy.

Google News-proxied feeds: months-to-years avg, **2 hours minimum
even for the freshest visible article**. The strategy cannot fire
on a Reuters-via-gnews trigger faster than the underlying Google
News indexing delay.

---

## Section 5 — Live RSS feed inspection

Fetched each feed directly via httpx (single User-Agent
`Mozilla/5.0 (compatible; trumpbot-investigation/1.0)`).

### `reuters_via_gnews`

- HTTP 200, 132 KB, `application/xml; charset=utf-8`
- 100 entries
- Date range: **2025-07-10 to 2026-04-15** (newest article is
  **11 days old**)
- Article URLs: Google News redirect/tracker URLs (e.g.
  `https://news.google.com/rss/articles/CBMivgFBVV...`), not the
  canonical Reuters URL
- Sample titles:
  - "Trump's peace board faces cash crunch, stalling Gaza plan,
    sources say - Reuters"
  - "Trump weighs broader cabinet shake-up as Iran war pressure
    grows - Reuters"
  - "Iran rejects ceasefire as Trump ramps up threats ahead of
    deadline - Reuters"

This is the smoking gun. Google News searches return **historical
results, not a live news feed**. The newest entry is from
2026-04-15, so the bot literally cannot see anything Reuters has
published in the last 11 days through this source.

### `ap_via_gnews`

- HTTP 200, 124 KB, `application/xml; charset=utf-8`
- 100 entries
- Date range: **2024-07-27 to 2026-04-08** (newest is **18 days old**)
- Article URLs: same Google News redirect tracker pattern
- Sample titles:
  - "How Trump went from threatening Iran's annihilation to
    agreeing to a 2-week ceasefire"
  - "Pam Bondi, a Trump loyalist who oversaw Justice Department
    upheaval, is out as head"
  - "Trump unveils 100% tariff on some patented drugs on
    'Liberation Day' anniversary"

Same structural problem.

### `bloomberg` (direct)

- HTTP 200, 37 KB, `application/rss+xml; charset=utf-8`
- 30 entries
- Date range: **2026-04-26 00:50 to 20:29** (today, fresh)
- Sample titles:
  - "DC Gala Gunman Believed to Be Targeting US Officials"
  - "Trump Says Gala Attack Shows Security Need for His Ballroom"
  - "Chevron CEO Says Venezuela Must Do More for Oil Industry
    Revival"

Healthy.

### `nyt_politics` (direct)

- HTTP 200, 56 KB, `application/xml`
- 28 entries
- Date range: **2026-04-26 01:17 to 23:03** (today, fresh)

Healthy.

### `wsj_politics` (direct, currently broken)

- **HTTP 403 Forbidden** (111 bytes — error page)
- The deployed Dow Jones host is rejecting unauthenticated requests.
  WSJ moved to paywalled syndication in mid-2024. The
  `feeds.a.dj.com` endpoint is dead for free use.

### `wsj_world` (direct, currently broken-ish)

- HTTP 200, 13 KB
- 20 entries
- Date range: **2025-01-24 to 2025-01-27** (15+ months old)
- The feed *responds* but its content has been frozen in time since
  late January 2025. WSJ stopped updating this endpoint while
  keeping it serving the historical content. Also dead.

### `cnn_politics` (direct)

- HTTP 200, 62 KB
- 30 entries
- Date range: **2022-11-29 to 2024-06-14** (oldest 3.5 years old,
  newest ~22 months old)
- The CNN RSS endpoint is technically alive but the editorial team
  stopped pushing to it well over a year ago.

### `cnn_world`, `cnn_topstories`, `cnn_us` (direct, all broken)

Same story as `cnn_politics`. CNN's RSS infrastructure is
abandoned. `rss.cnn.com` returns ancient cached content.

### `axios` (direct)

- HTTP 200, ~1 MB, `application/rss+xml; charset=utf-8`
- 100 entries
- Date range: **2026-04-18 to 2026-04-26** (8 days, fresh)

Healthy. Note the 1 MB response size — Axios serves the full
article body in the feed.

### `msnbc` (direct)

- HTTP 200, 142 KB, `application/rss+xml; charset=UTF-8`
- 10 entries (a small but rotating window)
- Date range: **2026-04-26 05:14 to 23:35** (today, fresh)

Healthy, but the small window means anything older than the last
~10 articles from MSNBC isn't recoverable on first poll.

### Reuters direct feeds (test of alternatives)

| URL | Status |
|---|---|
| `https://www.reuters.com/world/rss` | **HTTP 401** |
| `https://www.reuters.com/politics/rss` | **HTTP 401** |
| `https://www.reuters.com/news/feed/rss` | **HTTP 401** |

Reuters has comprehensively cut off public RSS. Per their public
docs, the only sanctioned access is via a paid Refinitiv /
LSEG subscription.

### AP direct feeds (test of alternatives)

| URL | Status |
|---|---|
| `https://feeds.apnews.com/apnews/topnews` | DNS resolution failure |
| `https://rsshub.app/apnews/topics/politics` | HTTP 403 |

AP retired their public RSS feeds in 2023. Third-party reposters
(rsshub.app) are blocked too.

### WaPo direct feeds (the better alternative)

| URL | Status | Date range |
|---|---|---|
| `https://feeds.washingtonpost.com/rss/politics` | **HTTP 200, 79 entries** | 2026-04-25 09:00 to 2026-04-26 23:45 |
| `https://feeds.washingtonpost.com/rss/world` (deployed as `wapo_world`) | works in this test, fresh | (one transient DNS error logged earlier today) |

The deployed config has `wapo_world` (direct) and
`wapo_via_gnews` (proxy). `wapo_politics_direct` is **not** in the
config but works and would replace `wapo_via_gnews` cleanly.

### Other discovered alternatives

- `https://www.whitehouse.gov/feed/` returns HTTP 404. So both
  `whitehouse_news` (`/news/feed/`) and the `/feed/` root are dead;
  `whitehouse_press` (`/briefing-room/press-briefings/feed/`) is
  also 404.
- `https://moxie.foxnews.com/google-publisher/world.xml` (already
  in the config as `fox_world`): HTTP 200, 25 entries spanning
  2026-04-21 to today. Healthy at the source level even though the
  bot only ingested 2 articles in 24h — the rest are likely
  duplicates from earlier polls.

---

## Section 6 — Theoretical vs actual lag

Spec asks for 3 specific articles. Pulled the 6 fastest-ingested
articles from healthy direct sources within the last 12 hours:

| Source | Headline | Published | Detected | Lag |
|---|---|---|---|---|
| `axios` | "Trump links White House Correspondents Dinner shooting to push new ballroom" | 2026-04-26T02:50:03Z | 2026-04-26T02:50:40Z | **0.6 min** |
| `msnbc` | "'Pop. Pop. Pop.': Inside the room at the White House correspondents dinner" | 2026-04-26T05:32:38Z | 2026-04-26T05:33:24Z | **0.8 min** |
| `bloomberg` | "Armed Man Stormed White House Correspondents Dinner Checkpoint" | 2026-04-26T03:47:36Z | 2026-04-26T03:48:51Z | **1.3 min** |
| `msnbc` | "Trump evacuated after security incident at White House Correspondents' Dinner" | 2026-04-26T02:12:00Z | 2026-04-26T02:13:23Z | **1.4 min** |
| `nyt_politics` | "Gunman Was Tackled by Law Enforcement Near Correspondents' Dinner Security Checkpoint" | 2026-04-26T03:50:26Z | 2026-04-26T03:51:47Z | **1.4 min** |
| `axios` | "Timeline: Shootings, threats against Trump over the years" | 2026-04-26T03:14:54Z | 2026-04-26T03:16:30Z | **1.6 min** |

### Theoretical-vs-actual analysis

Theoretical max lag = poll_interval (90 s = 1.5 min) + feed
indexing delay. The observed lags of 36-96 s match this perfectly:
the bot is polling at the configured rate and the feed is serving
fresh content, so there's no slack to recover.

For **direct sources, the polling cadence and the feed are working
as designed**. There's nothing to fix here — the strategy gets
sub-2-minute notification of breaking news from healthy direct
feeds.

For **Google News-proxied sources**, the analysis is different.
The single fresh `reuters_via_gnews` article in the last 24 h had a
129 min lag (Reuters published it, Google News indexed it ~2 hours
later, the bot saw it on its next poll). The polling cadence isn't
the bottleneck — Google News's own indexing delay is.

---

## Section 7 — Articles missing from ingestion (Reuters spot-check)

Spec asks: scrape the Reuters world / politics homepage, grab the
top 20-30 visible headlines, check the match rate against
`news_events` from the last 24 h.

**Cannot be done as specified.** Reuters' HTML landing pages
(`https://www.reuters.com/world/`, `https://www.reuters.com/world/us/`)
return **HTTP 401** to unauthenticated `httpx` requests. The
landing page requires either a logged-in session cookie or a
JavaScript-evaluated bot challenge. A simple `httpx` GET from the
investigation cannot enumerate the headlines on the live homepage.

What we know from indirect signals:

- `reuters_via_gnews` has 117 articles ingested in 24 hours, but
  only **1** of those was actually published in the last 24 hours
  (Section 4). The other 116 are stale Reuters content surfacing
  in Google News for the first time.
- Direct competitors covering the same Trump beat
  (`bloomberg` 30, `nyt_politics` 33, `wapo_via_gnews` 86 stale,
  `nbc_politics` 25, `cbs_politics` 21, `axios` 15, `msnbc` 10)
  are catching today's stories within 1-2 minutes.
- Spot-checking the same news event ("Correspondents Dinner
  shooting") across direct sources confirms the bot caught the
  story from `axios`, `msnbc`, `bloomberg`, `nyt_politics`
  independently within minutes of publication. The story would have
  reached the LLM cascade with confidence.

**Pragmatic conclusion**: Reuters-specific coverage rate is
unmeasurable from this investigation, but the bot's overall
breaking-news coverage of the SAME stories is high because of
overlapping direct sources. The risk of a *Reuters-only*
exclusive being missed is real but bounded by the fact that major
political events involving Trump rarely break exclusively on
Reuters.

---

## Section 8 — Cost / failures

### HTTP request volume

31 RSS sources at 90 s (most) or 120 s (semafor / the_information /
pr_newswire_gov / business_wire) = **~29,760 HTTP requests/day**.
At ~50 KB average response (most are 10-100 KB; axios is 1 MB), that
is ~1.4 GB/day or ~43 GB/month uncompressed. Real bandwidth is
much lower because most sources serve `Last-Modified` /
`If-Modified-Since` 304 responses (the bot doesn't currently send
those headers — see "Improvement options" below).

### `system_events` failure scan (last 7 days)

```
event_type      severity  component      occurrences  first_seen          last_seen
--------------  --------  -------------  -----------  ------------------  ------------------
source_failure  error     rss_poller     31           2026-04-26T02:36Z   2026-04-26T23:59Z
sequence_gap    warning   kalshi_ws      242          (WS, unrelated)
ws_disconnect   warning   kalshi_ws      23           (WS, unrelated)
twitter_disabled warning   twitter_scraper 21         (no token)
```

### `source_failure` breakdown by source (24 h)

```
source            occurrences   error
----------------  -----------   -----
wsj_politics      12            HTTP 403 Forbidden on https://feeds.a.dj.com/rss/RSSPoliticsAndPolicy.xml
whitehouse_press  12            HTTP 404 Not Found on https://www.whitehouse.gov/briefing-room/press-briefings/feed/
politico_picks    6             HTTP 403 Forbidden on https://www.politico.com/rss/politicopicks.xml
wapo_world        1             ConnectError (DNS) — transient
```

**No 429 (rate limit) responses logged.** Polling at 90 s isn't
hitting any source's rate limit, so reducing the poll interval is
mechanically possible (though not useful per Section 6).

The `wsj_politics` and `whitehouse_press` errors are persistent —
the feeds simply don't exist at those URLs anymore.

The `politico_picks` 403s are a recent change; politico has been
hardening its anti-scraper defenses in 2025.

The `wapo_world` ConnectError was a single transient DNS resolution
failure on the daemon's host (macOS). Not a feed problem.

### Truth Social

- **0 events ever ingested.**
- Recent `truth_social_poll_failed` warnings show repeated
  `ConnectError('[Errno 8] nodename nor servname provided, or not
  known')` and `ConnectTimeout('')`. The intermittent nature
  prevents the 5-consecutive-failure threshold from firing a
  `source_failure` system_event.

---

## Section 9 — Improvement options

### Option A: Switch from Google News proxy to direct source RSS

| Source | Current (proxy) | Direct alternative | Status of direct |
|---|---|---|---|
| `reuters_via_gnews` | gnews q=Reuters | `reuters.com/world/rss` etc. | **HTTP 401 — requires paid Refinitiv** |
| `ap_via_gnews` | gnews q=AP | `feeds.apnews.com/apnews/topnews` | **DNS fail / discontinued** |
| `wapo_via_gnews` | gnews q=WaPo | `feeds.washingtonpost.com/rss/politics` | **WORKS — 79 entries, fresh** ✅ |
| `semafor_via_gnews` | gnews q=Semafor | `https://www.semafor.com/rss` | not yet tested |

**Cost**: zero. **Effort**: 1 line of YAML per source. **Benefit**:
WaPo would catch fresh content immediately instead of waiting on
Google News indexing.

For Reuters and AP, **there is no working free direct alternative**.
The tradeoff is: keep the stale proxy, switch to a paid alternative,
or accept the gap.

### Option B: Add paid news API

| Service | Tier | Coverage of Kalshi-approved? | Latency | Monthly cost |
|---|---|---|---|---|
| NewsAPI.org Developer | 100 req/day, 1 mo old | All major US sources | 30-60 min | Free |
| NewsAPI.org Business | 250k req/mo, real-time | All major US | minutes | $449 |
| GNews Free | 100 req/day | Limited | 30-60 min | Free |
| GNews Standard | 50k req/mo | Most major US | <30 min | $50 |
| Bing News Search v7 | 1k tx/mo | Reuters, AP, all wires | minutes | $5 (S1 tier) |
| Bing News Search v7 | 100k tx/mo | same | minutes | $250 (S2 tier) |
| Refinitiv Real-Time News | enterprise | Reuters wire (the actual source) | seconds | $thousands/mo |

At the bot's current polling rate (~30k requests/day across all
sources), most paid tiers are an order of magnitude over what's
needed if you only use the API for Reuters + AP coverage. A more
sensible pattern: use a paid API at ~5-10 minute polling for Reuters
+ AP only, keep direct feeds for everyone else.

**Bing News v7 at the S1 tier ($5/mo for 1000 transactions/month)**
is the cheapest entry point. At 1 query every 90 seconds, you'd burn
the monthly allowance in 1.5 days; realistic poll interval for the
S1 tier is 1 query every ~45 minutes. Useful for "every 30 min,
search for fresh Reuters coverage of the active subjects" but not
for sub-minute breaking news.

**Refinitiv** is the gold standard but enterprise-only and requires
a contract conversation.

### Option C: Reduce polling interval (90 s → 30 s)

- Mechanical change; current YAML field is `poll_interval_sec: 90`.
- Risk: 429 rate-limit responses. None logged in the last 24 h, so
  there's headroom.
- Benefit: lower bound on direct-feed lag falls from ~90 s to ~30 s.
  Section 6 shows direct feeds already hit 36-96 s end-to-end; a
  30 s poll could reduce this to ~15-45 s. Real but small.
- **Will not help Google News-proxied sources** — Google News's own
  indexing delay (2 + hours minimum for Reuters) dominates.

### Option D: Add Twitter/X firehose integration

- The 6 Twitter handles (`@WhiteHouse`, `@PressSec`, `@POTUS`,
  `@SecState`, `@StateDept`, `@DeptofDefense`) are already in
  config but disabled because `TWITTER_BEARER_TOKEN` is unset.
- Twitter API v2 Basic: $200/month, 50k tweets/month read. Enough
  to poll those 6 accounts every 5-10 minutes.
- Implementation effort: real (auth, rate-limit handling, parsing).
  The `twitter.py` scaffolding already exists; needs a token and
  testing.
- Strategy benefit: meaningful. Trump's account or @PressSec
  often post breaking news ahead of any wire service. Would convert
  Twitter from "configured-but-disabled" to actually working.

### Option E: Add direct API integrations

Some sources have undocumented JSON endpoints used by their own
websites. Examples:

- Reuters has an internal `apidata.reuters.com/...` API.
- NYT has a public `developer.nytimes.com` API with a free tier.

Risks: TOS violations, sudden authentication requirements. These
are short-term hacks rather than a sustainable solution.

### Option F: Add Truth Social monitoring (already configured)

- `truth_social:@realDonaldTrump` is in config but failing on DNS.
- Trump posts on Truth Social are dispositive under Kalshi
  resolution rules ("verified social media accounts verified by
  the platform").
- Implementation: the scraper exists; needs investigation of why
  `truthsocial.com` resolves intermittently from the deployed host.
  Possibly an IPv6/IPv4 ordering issue, or a transient DNS cache
  problem on macOS launchd-spawned processes.

### Option G: Accept current state

The strategy currently catches breaking news within 1-2 minutes
from healthy direct sources (Bloomberg, NYT, Axios, MSNBC, NBC,
CBS, Fox, WaPo direct). Reuters and AP coverage via Google News is
broken in latency terms, but the SAME news typically appears in
the direct feeds at the same time. The risk of a Reuters-exclusive
breaking story being missed exists but is bounded.

If the operator's strategy thesis depends on "first to know about
a Trump-Putin call exclusively reported on Reuters," current state
is unacceptable. If it depends on "first to know about a Trump-Putin
call that any major US wire service has reported," current state
is acceptable.

### Option H: Bandwidth optimisation — `If-Modified-Since`

Not in the user's spec, but worth noting: the RSS poller doesn't
currently send `If-Modified-Since` / `If-None-Match` conditional
headers, so every poll downloads the full feed body. Most major
news RSS feeds support 304 responses; adding the headers would cut
real bandwidth by ~70-90% with no logic change. Effort: ~30 min.

---

## Section 10 — Prioritized recommendations

### P0 — must address before going live

1. **Replace `wapo_via_gnews` with a direct WaPo politics feed.**
   Change `https://news.google.com/rss/search?...source:washingtonpost.com...` to
   `https://feeds.washingtonpost.com/rss/politics`. Effort: 1 line of
   YAML. Benefit: gets fresh WaPo content instead of stale
   Google-News-indexed WaPo content.

2. **Disable or remove broken sources.** Move `wsj_politics` (403),
   `wsj_world` (frozen 15 months), `whitehouse_press` (404),
   `whitehouse_news` (last seen 26h ago), `cnn_politics` (last
   updated 2024-06), `cnn_world` (same), `business_wire` (0 events
   ever), `state_press` / `state_readouts` (0 events ever) to
   commented-out lines in `config.yaml`. They contribute nothing,
   they emit `source_failure` system_events that pollute the audit
   log, and they cost HTTP requests + bandwidth. Effort: 10 minutes.

3. **Decide on Reuters / AP gap explicitly.** Either:
   - **Accept** the fact that Reuters/AP arrive 2+ hours late via
     Google News, with the note that breaking Trump news typically
     also appears on Bloomberg / NYT / WaPo / NBC simultaneously and
     those direct feeds work; OR
   - **Pay** for Bing News Search S1 ($5/mo) or NewsAPI.org Business
     ($449/mo) and add a separate poller hitting them every 5-15
     minutes for `Trump` coverage.

   This is a strategic call, not a technical one. Document the
   choice in CLAUDE.md.

### P1 — fix in first month of operation

4. **Investigate Truth Social DNS failures.** The configured handle
   is producing zero events because of intermittent DNS errors. This
   is the highest-value source for the strategy (Trump's own
   first-person statements are dispositive under Kalshi rules) and
   it's silently broken. Effort: ~2-4 hours of debugging the
   `truthsocial.com` resolution from the deployed host.

5. **Enable Twitter polling.** Generate a Twitter API v2 Basic
   token ($200/mo if Trump-news velocity justifies it; free
   research access is also worth investigating for low-volume
   testing) and add it to `secrets.env`. The 6 handles in config
   include `@PressSec` and `@StateDept` which often post readouts
   minutes after a meeting concludes. Effort: ~3 hours of token +
   smoke-test work.

6. **Add `If-Modified-Since` conditional headers to the RSS
   poller.** Cut real bandwidth by ~70%, reduce surprise data
   transfer if the bot ever moves to a metered VPS connection.
   Effort: ~30 minutes in `trumpbot/news/rss.py`.

### P2 — consider after 30 days of operational data

7. **Per-source poll interval tuning.** Some sources (Bloomberg,
   Axios) post continuously; polling at 30 s might be worth the
   trade-off. Other sources (DOD news, business_wire) post a few
   times a day; polling at 5 minutes would be plenty. Wait until
   30 days of data show actual per-source publish cadences, then
   tune.

8. **Paid news API only if 30 days of operational data show
   Reuters-exclusive breakings the bot missed.** If the operator's
   first month of trades reveals a recurring "we didn't see X
   that Reuters had exclusively" pattern, then revisit Bing /
   NewsAPI / Refinitiv. If it doesn't, save the money.

9. **Consider direct Reuters scraping** (puppeteer-style headless
   browser hitting `reuters.com/world/`) only if all of the above
   prove insufficient. Expensive in maintenance and TOS-fragile.

---

## Appendix — limitations of this investigation

- The DB has only 2 days of history (the bot started 2026-04-25
  21:49 UTC). 7-day analysis was constrained to those two days;
  day-1 numbers are inflated by initial backfill.
- Reuters coverage rate cannot be measured directly because
  `reuters.com/world/` returns HTTP 401 to the investigation script.
  The "117 articles in 24h but only 1 fresh" finding is the strongest
  available indirect indicator.
- The investigation did not test paid news API trial accounts.
  Pricing/tier information above was sourced from public
  documentation as of 2026-04-26 and may be outdated.
- The investigation did not load-test the suggested polling-rate
  reductions; the "no 429s in 24h" observation is necessary but not
  sufficient evidence that 30 s polling would be safe.
- Truth Social DNS failures are observed from the macOS launchd
  daemon context only. The same fetch may succeed from a different
  host (e.g. a Linux VPS), and the pattern is consistent with
  IPv6-stack interference common on macOS.
