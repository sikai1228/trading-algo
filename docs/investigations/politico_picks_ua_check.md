# `politico_picks` UA matrix — investigation

**Date:** 2026-04-26
**Trigger:** Multi-PR sheet PR 4 deliverable 2. The
source-status audit (PR #30) decommissioned `politico_picks` for
returning HTTP 403 in the deployed environment. The audit also
demonstrated `the_information`'s 403 was UA-dependent, prompting
this re-test.

## Method

Five User-Agents tested against
`https://www.politico.com/rss/politicopicks.xml`:

1. Safari macOS (the deployed default since PR #29)
2. Chrome Linux (the_information's working UA)
3. The old bot UA (`trump-market-bot/1.0 research project`)
4. feedparser default (`feedparser/6.0.10`)
5. No UA header at all

All requests via `httpx.AsyncClient(follow_redirects=True, timeout=10)`.

## Results

| UA label | Status | Bytes | Entries | Newest item |
|---|---:|---:|---:|---|
| **safari_macos** | **200** | 728,146 | 36 | 2026-04-27T00:21:38Z |
| chrome_linux | 200 | 728,146 | 36 | 2026-04-27T00:21:38Z |
| old_bot_ua | 200 | 728,146 | 36 | 2026-04-27T00:21:38Z |
| feedparser_default | 200 | 728,146 | 36 | 2026-04-27T00:21:38Z |
| no_ua | 403 | 5,405 | — | — |

## Verdict

**Politico_picks works with the deployed Safari UA — the
decommission decision in PR #29 was based on a transient
condition, not a permanent block.**

The audit's source_failure log shows 6 occurrences of 403 for
politico_picks between deploy time and the PR #29 redeploy —
those were genuine errors at that moment, but the source has
since become reachable again (or the original 403 was
intermittent rate-limiting tied to higher-frequency polling
patterns). Today, all four real UAs return 200 with 36 fresh
entries; only an EMPTY UA gets 403.

## Recommendation

**Re-add `politico_picks` to `config.yaml` and
`config.example.yaml` in a follow-up PR.** Suggested entry:

```yaml
- {name: "politico_picks", type: "rss", url: "https://www.politico.com/rss/politicopicks.xml", poll_interval_sec: 120, is_kalshi_approved: true}
```

Use a 120 s poll interval (slightly slower than the default 90 s)
to reduce the risk of re-triggering whatever rate-limit hit the
source originally — the feed only rotates every ~30 min based on
the entry timestamps, so 120 s polls catch every update.

If the 403s recur after re-addition, the rotation_paused +
source_down monitoring (PR #33) will catch it cleanly. The
decommission notes in `config/config.example.yaml` should be
updated to remove the politico_picks line.

## Reproducing this probe

```bash
cd "/Users/sikai/Desktop/Auto Trading"
uv run python /tmp/probe_politico_picks.py
```

The probe script (transient — kept inline for audit):

```python
import asyncio, httpx, feedparser
UAS = {
    "safari_macos": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ...",
    "chrome_linux": "Mozilla/5.0 (X11; Linux x86_64) ...",
    "old_bot_ua": "trump-market-bot/1.0 research project",
    "feedparser_default": "feedparser/6.0.10",
    "no_ua": "",
}
URL = "https://www.politico.com/rss/politicopicks.xml"
# (probe with each UA, parse, report)
```
