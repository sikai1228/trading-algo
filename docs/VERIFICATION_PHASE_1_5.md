# Phase 1.5 source verification (RSS lockdown)

Operator-reported issue (2026-04-25): six Kalshi-approved sources
were "missing from the active RSS poller" — i.e. configured but
producing zero articles. Audit, fix, and live-confirm below.

## TL;DR

| # | Source     | Old URL status                                 | Final URL                                                                                                                              | Live ingest |
|---|------------|------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------|-------------|
| 1 | Reuters    | 404 (RSS shut down 2020)                       | `https://news.google.com/rss/search?q=%22Donald+Trump%22+source:reuters.com&hl=en-US&gl=US&ceid=US:en`                                | **112 / 3 min** |
| 2 | AP         | 404 (returns HTML 404 page)                    | `https://news.google.com/rss/search?q=%22Donald+Trump%22+source:apnews.com&hl=en-US&gl=US&ceid=US:en`                                 | **109 / 3 min** |
| 3 | WaPo       | `/politics` 200-OK-then-hangs, `/world` thin   | `https://feeds.washingtonpost.com/rss/world` (kept) + `https://news.google.com/rss/search?q=%22Donald+Trump%22+source:washingtonpost.com&hl=en-US&gl=US&ceid=US:en` | **102 + 7 / 3 min** |
| 4 | MSNBC      | `/feeds/latest` returns the homepage HTML      | `https://www.msnbc.com/feed/`                                                                                                          | **10 / 3 min** |
| 5 | ABC News   | 301 → silently dropped by httpx default        | `https://abcnews.go.com/abcnews/politicsheadlines` + `/internationalheadlines` (URLs were already correct)                            | **25 + 25 / 3 min** |
| 6 | Semafor    | `/feed.xml` 404                                | `https://news.google.com/rss/search?q=%22Donald+Trump%22+source:semafor.com&hl=en-US&gl=US&ceid=US:en`                                | **77 / 3 min** |

Plus an ancillary unlock: `dod_news` (10 articles in 3 min) — its
URL was already correct but it returns 301 too, so the underlying
redirect-fix recovered it as well.

## Root causes

### Causes 1-2-6: outlets shut down their RSS

Reuters discontinued public RSS feeds in 2020 (now requires a paid
"Reuters Connect" partner agreement). AP killed `apnews.com/hub/*/feed`
in favor of an undocumented internal API. Semafor never had a
working public RSS endpoint despite the URL pattern that's
circulating in old wikis.

**Fix**: use Google News' RSS-search endpoint with a `source:` filter
that returns articles from those outlets matching `"Donald Trump"`.
The articles' `<source>` element confirms the underlying outlet, so
`is_kalshi_approved: true` remains accurate. The article URL is a
Google redirector (`news.google.com/rss/articles/...`) rather than the
outlet's canonical URL, but the matcher only needs the headline + body,
both of which are present verbatim. Naming convention `<outlet>_via_gnews`
makes the discovery mechanism explicit so a future maintainer doesn't
mistake these for direct feeds.

### Cause 3: WaPo politics endpoint dead, world endpoint thin

`https://feeds.washingtonpost.com/rss/politics` returns an `HTTP/1.1
200 OK` header but never delivers a body within 10 s — the connection
hangs. `/world` works but only ships 7-8 items at a time. We dropped
the politics feed and supplement with the Google News fallback.

### Causes 4-5 (and the latent unlock for DoD): **httpx wasn't following redirects**

The actual bug, surfaced by the audit. MSNBC's `https://www.msnbc.com/feed/`
returns a 301 to its CDN host. ABC's `politicsheadlines` and
`internationalheadlines` likewise return 301. `httpx.AsyncClient`'s
**default is `follow_redirects=False`** — it just returned the 301
response with a tiny redirect-page body, and feedparser saw zero
entries. This was silently affecting:

- MSNBC (every poll, 0 articles ever ingested before this fix)
- ABC News (both feeds, 0 articles ever)
- DoD News (sporadically; same root cause)

**Fix**: set `follow_redirects=True` on the auto-constructed
`httpx.AsyncClient` in `RSSPoller.__init__`. Pinned by
`tests/test_rss_redirects.py`:

- `test_default_client_follows_redirects` — guards the constructor
  default.
- `test_redirected_feed_actually_ingested` — full end-to-end with a
  respx-mocked 301 → 200 chain that asserts an article actually
  lands in `news_events`.

## Live confirmation

Two consecutive smoke runs (5 min and 2 min) of `scripts/smoke_test.py`
against the user's real config. Per-source counts from the most
recent 3-minute window:

```
reuters_via_gnews   112    abc_international    25
ap_via_gnews        109    abc_politics         25
wapo_via_gnews      102    fox_politics         25
axios               100    fox_world            25
semafor_via_gnews    77    nbc_politics         25
nyt_world            55    cbs_politics         24
politico_picks       34    nbc_world            24
bloomberg            31    cnn_world            23
politico_wh          30    pr_newswire_gov      20
cbs_world            27    the_information      20
                           wsj_world            20
                           nyt_politics         19
                           cnn_politics         18
                           dod_news             10
                           msnbc                10
                           whitehouse_news      10
                           wapo_world            7
```

Every previously-missing source is delivering. No critical
system_events during either run.

## Acceptance

- `tests/test_rss_redirects.py` (2 tests) pins the constructor
  default + an end-to-end follow-redirect ingest.
- All existing RSS poller tests (`tests/test_rss_poller.py`) still pass.
- `config/config.example.yaml` and `~/.config/trumpbot/config.yaml`
  both updated to the verified URLs.

## Operator next steps

1. The deployed config in `~/.config/trumpbot/config.yaml` was edited
   in place during this work; no further action needed for the
   running daemon to pick up the changes (it'll re-read on the next
   restart).
2. Restart the daemon:
   `launchctl unload ~/Library/LaunchAgents/com.trumpbot.daemon.plist; launchctl load ~/Library/LaunchAgents/com.trumpbot.daemon.plist`
3. After ~10 minutes, re-run `inspect_data.py` and confirm the
   per-source counts include every name in the table above.
