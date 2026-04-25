"""Tests for the RSS poller. HTTP transport mocked via respx."""

from __future__ import annotations

import asyncio

import httpx
import respx

from trumpbot.db.connection import Database
from trumpbot.events.bus import EventBus
from trumpbot.news.rss import RSSPoller
from trumpbot.news.sources import NewsSourceConfig

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Sample</title>
    <link>https://example.com/</link>
    <description>Test feed</description>
    <item>
      <title>Trump and Putin spoke today</title>
      <link>https://example.com/article-1</link>
      <description>&lt;p&gt;Trump and Putin spoke today.&lt;/p&gt;</description>
      <pubDate>Mon, 25 Apr 2026 12:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Other news item</title>
      <link>https://example.com/article-2</link>
      <description>Generic news.</description>
      <pubDate>Mon, 25 Apr 2026 12:01:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


@respx.mock
async def test_poll_persists_items(tmp_db: Database) -> None:
    bus = EventBus()
    source = NewsSourceConfig(
        name="reuters",
        type="rss",
        url="https://example.com/feed.xml",
        poll_interval_sec=60,
        weight=1.0,
        is_kalshi_approved=True,
    )
    respx.get("https://example.com/feed.xml").mock(
        return_value=httpx.Response(
            200, text=SAMPLE_RSS, headers={"Content-Type": "application/rss+xml"}
        )
    )
    poller = RSSPoller(sources=[source], db=tmp_db, event_bus=bus)
    await poller._poll_source(source)
    rows = list(tmp_db.connect().execute("SELECT * FROM news_events ORDER BY id"))
    assert len(rows) == 2
    assert rows[0]["headline"] == "Trump and Putin spoke today"
    assert rows[0]["url"] == "https://example.com/article-1"
    assert rows[0]["source_weight"] == 1.0
    assert rows[0]["is_kalshi_approved"] == 1


@respx.mock
async def test_poll_dedups_on_repeat(tmp_db: Database) -> None:
    bus = EventBus()
    source = NewsSourceConfig(
        name="reuters",
        type="rss",
        url="https://example.com/feed.xml",
        poll_interval_sec=60,
        weight=1.0,
        is_kalshi_approved=True,
    )
    respx.get("https://example.com/feed.xml").mock(
        return_value=httpx.Response(
            200, text=SAMPLE_RSS, headers={"Content-Type": "application/rss+xml"}
        )
    )
    poller = RSSPoller(sources=[source], db=tmp_db, event_bus=bus)
    await poller._poll_source(source)
    await poller._poll_source(source)
    rows = list(tmp_db.connect().execute("SELECT * FROM news_events"))
    assert len(rows) == 2  # unchanged


@respx.mock
async def test_429_does_not_persist(tmp_db: Database) -> None:
    bus = EventBus()
    source = NewsSourceConfig(
        name="reuters",
        type="rss",
        url="https://example.com/feed.xml",
        poll_interval_sec=60,
        weight=1.0,
        is_kalshi_approved=True,
    )
    respx.get("https://example.com/feed.xml").mock(
        return_value=httpx.Response(429, text="rate limited")
    )
    poller = RSSPoller(sources=[source], db=tmp_db, event_bus=bus)
    # Replace asyncio.sleep so the test does not actually wait the full
    # rate-limit backoff (5 * poll_interval = 300s).
    orig = asyncio.sleep
    try:
        # Monkeypatch asyncio.sleep to avoid a real 5-minute 429 backoff;
        # the lambda signature differs from sleep's but mypy can't model
        # this short-lived patch.
        asyncio.sleep = lambda *_a, **_k: orig(0)  # type: ignore[assignment]
        await poller._poll_source(source)
    finally:
        asyncio.sleep = orig
    rows = list(tmp_db.connect().execute("SELECT * FROM news_events"))
    assert rows == []
