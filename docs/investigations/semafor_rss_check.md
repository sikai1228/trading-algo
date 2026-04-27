# Semafor direct RSS — investigation

**Date:** 2026-04-26
**Trigger:** Multi-PR sheet PR 4 deliverable 1. The
source-status audit (PR #30) decommissioned `semafor_via_gnews`
because the Google News proxy served stale content. This probe
checks whether Semafor publishes a working direct RSS feed.

## Method

Probed six candidate URLs with the deployed Safari UA, plus
inspected Semafor's homepage HTML for `<link rel="alternate"
type="application/rss+xml">` discovery hints. All requests went
through `httpx.AsyncClient(follow_redirects=True, timeout=10)`
with `Accept: application/rss+xml,application/xml,text/xml,*/*`.

## Results

| URL | HTTP | Bytes | Content-Type | Entries | Newest item |
|---|---:|---:|---|---:|---|
| `https://www.semafor.com/rss` | 404 | 2,804 | text/html | 0 | — |
| `https://www.semafor.com/feed` | 404 | 2,804 | text/html | 0 | — |
| **`https://www.semafor.com/rss.xml`** | **200** | **637,815** | **application/xml** | **205** | **2026-04-27T01:11:36Z (~25 min ago)** |
| `https://feeds.semafor.com/all` | DNS error | — | — | — | — (subdomain doesn't exist) |
| `https://www.semafor.com/rss/all` | 404 | 2,804 | text/html | 0 | — |
| `https://www.semafor.com/atom.xml` | 404 | 2,804 | text/html | 0 | — |

Homepage `<link rel="alternate">` discovery returned 0 hits (the
homepage doesn't advertise the feed; `rss.xml` is undiscoverable
from the markup).

## Verdict

**Working feed found:** `https://www.semafor.com/rss.xml`

- Returns 200 OK with `application/xml`
- 205 entries (large rolling window)
- Newest item ~25 min old at probe time → fresh
- Compatible with the existing `RSSPoller` infrastructure
- Safari UA (the deployed default) accepted

## Recommendation

**Add to `config.yaml` and `config.example.yaml` in a follow-up
PR.** Suggested entry:

```yaml
- {name: "semafor", type: "rss", url: "https://www.semafor.com/rss.xml", poll_interval_sec: 90, is_kalshi_approved: true}
```

This re-establishes Semafor coverage (one of the 21
contract-approved categories) that was dropped in PR #29 along
with the Google News proxy. Semafor publishes high-signal
foreign-policy reporting that doesn't fully overlap with
NYT/WaPo/Bloomberg — re-adding closes a real gap.

## Reproducing this probe

```bash
cd "/Users/sikai/Desktop/Auto Trading"
uv run python /tmp/probe_semafor.py
```

The probe script lives at `/tmp/probe_semafor.py` (transient —
kept inline below for the audit trail):

```python
import asyncio, httpx, feedparser
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
CANDIDATES = [
    "https://www.semafor.com/rss",
    "https://www.semafor.com/feed",
    "https://www.semafor.com/rss.xml",
    "https://feeds.semafor.com/all",
    "https://www.semafor.com/rss/all",
    "https://www.semafor.com/atom.xml",
]
# (probe each, parse with feedparser, report newest entry timestamp)
```
