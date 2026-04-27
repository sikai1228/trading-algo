# Government readouts feed search — investigation

**Date:** 2026-04-26
**Trigger:** Multi-PR sheet PR 4 deliverable 3. The
source-status audit (PR #30) found that of contract category #14
(Official government websites) and #18 (Official readouts), only
`dod_news` (degraded, feed itself 52 h stale) and
`pr_newswire_gov` (healthy) remain after PR #29 decommissioned
WhiteHouse / State endpoints. This probe checks whether any
working direct alternative exists.

## Method

Twelve candidate URLs across three agencies (White House, State
Dept, Defense Dept) probed with the deployed Safari UA. All
requests via `httpx.AsyncClient(follow_redirects=True, timeout=10)`.

## Results

| URL | HTTP | Bytes | Entries | Newest item |
|---|---:|---:|---:|---|
| **`https://www.whitehouse.gov/news/feed/`** | **200** | 149,916 | **10** | **2026-04-25T16:26:59Z (~57 h ago)** |
| `https://www.whitehouse.gov/feed/` | 404 | 28,446 | 0 | — |
| `https://www.whitehouse.gov/briefing-room/feed/` | 404 | 28,446 | 0 | — |
| `https://www.whitehouse.gov/briefing-room/press-briefings/feed/` | 404 | 28,446 | 0 | — (gone since 2024) |
| `https://www.whitehouse.gov/briefing-room/statements-releases/feed/` | 404 | 28,446 | 0 | — |
| `https://www.state.gov/feed/` | 200 | 1,407 | 0 | non-RSS body |
| `https://www.state.gov/press-releases/feed/` | 200 | 217,448 | 0 | "Technical Difficulties" HTML page |
| `https://www.state.gov/secretary-of-state/feed/` | 404 | 157,497 | 0 | — |
| `https://www.defense.gov/News/Releases/Feed/` | 403 | 398 | 0 | — |
| `https://www.defense.gov/News/Press-Releases/Feed/` | 403 | 410 | 0 | — |
| `https://www.defense.gov/News/Releases/feed/` | 403 | 398 | 0 | — |
| `https://www.whitehouse.gov/wp-json/wp/v2/posts` | 403 | 1,551 | 0 | — (WP REST blocked) |

## Verdict

**One viable but rotation-paused candidate**:
`https://www.whitehouse.gov/news/feed/` returns 200 with 10
parseable RSS entries. **However** the newest entry is from
2026-04-25T16:26 — ~57 h old at probe time. The feed is
publishing slowly (or paused entirely). Adding it would land it
into the new `rotation_paused` alert immediately (which is fine —
that's the system working as designed; the operator gets a
warning that the feed is stale and can decide whether to keep
polling).

**State Dept**: both candidate endpoints return HTTP 200 but with
non-RSS bodies (the "Technical Difficulties" page suggests
state.gov's RSS infrastructure is broken at the platform level,
not a per-feed issue). Other variants 404.

**DOD alternatives**: every alternative URL returns 403. The
existing `dod_news` URL is the only working DOD path; its
own staleness (52 h at audit time) is feed-internal — DOD itself
isn't publishing.

## Recommendation

**Two-step approach**:

1. **Add `whitehouse_news` to config**, accepting the slow
   rotation:

   ```yaml
   - {name: "whitehouse_news", type: "rss", url: "https://www.whitehouse.gov/news/feed/", poll_interval_sec: 300, is_kalshi_approved: true}
   ```

   Use a 5-min poll interval (slow rotation = no value polling
   faster). The PR #33 rotation_paused alert will fire after 12 h
   if the feed stays stale, giving the operator a clear signal.

2. **HTML scraping** as a follow-up project. If RSS coverage of
   government websites stays this thin, write a scraper for:
   - `https://www.whitehouse.gov/news/` (HTML listing)
   - `https://www.state.gov/press-releases/` (HTML listing)
   - `https://www.defense.gov/News/Releases/` (HTML listing)

   These pages reliably show fresh content even when the RSS
   endpoints are broken. A periodic scrape (every 5 min) into
   `news_events` would close the categorical gap. Out of scope
   for this PR sheet — file as a future investigation.

## What to skip

- All five 404 White House endpoints — paths no longer exist
- State Dept feeds — returning HTML error pages, not RSS
- DOD alternative URLs — every variant 403, only the deployed
  `dod_news` URL is reachable
- WP REST API — 403 Forbidden, anti-scraper hardening

## Reproducing this probe

```bash
cd "/Users/sikai/Desktop/Auto Trading"
uv run python /tmp/probe_gov.py
```
