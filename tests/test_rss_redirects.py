"""Regression: RSSPoller HTTP client must follow redirects.

Many RSS feeds (MSNBC, ABC, WaPo, ...) return 301 to canonical URLs.
httpx defaults to NOT following redirects, which would silently turn
every 301 feed into zero ingested articles. The poller must override
the default with ``follow_redirects=True``. Pinning behavior here so
this can never silently regress.
"""

from __future__ import annotations

import httpx
import respx
from pytest import MonkeyPatch  # noqa: F401  (kept for typing visibility)

from trumpbot.db.connection import Database
from trumpbot.events.bus import EventBus
from trumpbot.news.rss import RSSPoller
from trumpbot.news.sources import NewsSourceConfig

REDIRECT_TARGET = "https://canonical.example.com/feed.xml"
RSS_BODY = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Test</title>
    <item>
      <title>Trump did something</title>
      <link>https://canonical.example.com/article-1</link>
      <description>News</description>
      <pubDate>Mon, 25 Apr 2026 12:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


def test_default_client_follows_redirects() -> None:
    """The poller's auto-constructed http client follows redirects."""
    poller = RSSPoller(sources=[], db=None, event_bus=EventBus())  # type: ignore[arg-type]
    # The httpx default is False; the poller must override.
    assert poller._http.follow_redirects is True


@respx.mock
async def test_redirected_feed_actually_ingested(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Full path: 301 → 200 → 1 article persisted."""
    db = Database(tmp_path / "rss.db")
    db.connect()
    bus = EventBus()
    source = NewsSourceConfig(
        name="redirecting_source",
        type="rss",
        url="http://old.example.com/feed",
        poll_interval_sec=60,
        is_kalshi_approved=True,
    )
    respx.get("http://old.example.com/feed").mock(
        return_value=httpx.Response(301, headers={"Location": REDIRECT_TARGET})
    )
    respx.get(REDIRECT_TARGET).mock(
        return_value=httpx.Response(
            200, text=RSS_BODY, headers={"Content-Type": "application/rss+xml"}
        )
    )
    poller = RSSPoller(sources=[source], db=db, event_bus=bus)
    await poller._poll_source(source)
    rows = list(db.connect().execute("SELECT source, headline FROM news_events"))
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}: {rows}"
    assert rows[0]["source"] == "redirecting_source"
    assert rows[0]["headline"] == "Trump did something"
    db.close()
