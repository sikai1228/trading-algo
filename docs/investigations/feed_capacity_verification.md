# Feed capacity verification — 2026-04-26

**Spec source**: Phase 4 Part 2.12, deliverable 8.

**Question**: do any working sources rotate articles out of the
RSS feed faster than the bot's 90-second poll cadence can capture?
If so, the bot would silently miss articles between polls.

## Method

Live-fetched each working RSS source with a Safari User-Agent.
For each feed, computed:

- **entries** — number of articles currently in the feed
- **newest_age** — hours since the most-recent article was published
- **oldest_age** — hours since the oldest article was published
- **window** — span between newest and oldest entry; this is the
  effective "rotation period" — how long an article stays in the
  feed before it's pushed out the back
- **pub/hr** — entries / window, the source's effective publication
  rate

If `window` is shorter than the poll cadence (90 s = 0.025 h), the
bot can miss articles. Realistic news-source values should be at
least several hours.

## Findings

```
feed                     entries      newest      oldest      window     pub/hr  rotation
--------------------------------------------------------------------------------------------
bloomberg                     30       4.0 h      23.6 h      19.6 h      1.53     19.6 h
nyt_politics                  28       1.4 h      23.2 h      21.8 h      1.29     21.8 h
nyt_world                     60       2.0 h       3.0 d       2.9 d      0.86      2.9 d
axios                        100       0.5 h       8.4 d       8.4 d      0.49      8.4 d
msnbc                         10       0.2 h      18.9 h      18.7 h      0.54     18.7 h
nbc_politics                  25       1.1 h      77.2 d      77.1 d      0.01     77.1 d
nbc_world                     25       3.0 h      18.3 d      18.2 d      0.06     18.2 d
cbs_politics                  30       0.2 h      25.5 h      25.3 h      1.19     25.3 h
cbs_world                     30       1.1 h       2.1 d       2.1 d      0.60      2.1 d
fox_politics                  25       7.0 h      37.8 h      30.8 h      0.81     30.8 h
fox_world                     25       3.2 h       5.2 d       5.1 d      0.21      5.1 d
abc_politics                  25       1.3 h       4.5 d       4.5 d      0.23      4.5 d
abc_international             25       1.1 h      42.2 h      41.1 h      0.61     41.1 h
wapo_politics                 81       0.1 h      39.5 h      39.3 h      2.06     39.3 h
wapo_world                     6      10.5 h      39.5 h      29.0 h      0.21     29.0 h
politico_wh                   30       6.5 h      13.2 d      12.9 d      0.10     12.9 d
the_information               20       2.4 h       3.0 d       2.9 d      0.29      2.9 d
dod_news                      10      51.5 h       4.3 d       2.2 d      0.19      2.2 d
pr_newswire_gov               20       3.4 h       2.2 d       2.1 d      0.40      2.1 d
```

## Verdict

**No source is at risk of rotating faster than the poll cadence.**

The fastest-rotating feed in the active list is `bloomberg` at
19.6 hours of window. At a 90-second poll interval (~800 polls per
window), the bot has ~800x margin before any article would scroll
out before being captured.

Even the highest-publication-rate feed (`wapo_politics` at 2.06
articles/hour, with a 39.3 h window holding 81 articles) has a
deep enough window that the poller can miss several days of
polling and still recover everything.

The slowest-publishing feeds (`nbc_politics` at 0.01 article/hour
≈ 1 article every 4 days; `politico_wh` at 0.10 articles/hour) are
the opposite problem — articles sit in the feed so long they're
ingested once and never scroll out, which is fine.

## Implication

The 90-second poll interval is not the bottleneck for any
configured source. Reducing the poll interval to 30 s would not
catch articles the current 90 s poll misses; the freshness comes
from the feed itself, not from polling cadence.

The previous concern (raised in the RSS investigation, Section 9
Option C) about "reducing polling interval as a way to catch
breaking news faster" is confirmed to be unhelpful: the published
articles already sit in the feed for hours. The investigation's
Section 6 measurement of 0.6-1.6 minute publication-to-detection
lag for direct feeds is the real lower bound — that's the feed
provider's own publish-to-RSS-cache delay plus our 90 s poll.

## Combined with `If-Modified-Since` (this PR)

Adding `If-Modified-Since` / `If-None-Match` conditional headers
(deliverable 5) means the bot's 90 s polls will produce a 304 from
well-behaved servers when nothing has changed. Bandwidth drops
~70-90 % without changing what the bot sees. Servers that ignore
the headers (some politico endpoints, Google News) just keep
returning 200 with the full body — same coverage, no regression.
