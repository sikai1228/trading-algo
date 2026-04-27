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
    assert rows[0]["is_kalshi_approved"] == 1


@respx.mock
async def test_poll_dedups_on_repeat(tmp_db: Database) -> None:
    bus = EventBus()
    source = NewsSourceConfig(
        name="reuters",
        type="rss",
        url="https://example.com/feed.xml",
        poll_interval_sec=60,
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


# ---------------------------------------------------------------------------
# Phase 4 Part 2.12 — If-Modified-Since / If-None-Match
# ---------------------------------------------------------------------------


@respx.mock
async def test_conditional_request_sent_on_second_poll(tmp_db: Database) -> None:
    """First poll lands a 200 with Last-Modified + ETag. Second
    poll should send those values back as If-Modified-Since /
    If-None-Match. We check both the request headers AND that the
    bot honors a 304 by skipping insertion."""
    bus = EventBus()
    source = NewsSourceConfig(
        name="reuters",
        type="rss",
        url="https://example.com/feed.xml",
        poll_interval_sec=60,
        is_kalshi_approved=True,
    )
    last_modified = "Sun, 26 Apr 2026 12:00:00 GMT"
    etag = '"abc123"'
    respx.get("https://example.com/feed.xml").mock(
        return_value=httpx.Response(
            200,
            text=SAMPLE_RSS,
            headers={
                "Content-Type": "application/rss+xml",
                "Last-Modified": last_modified,
                "ETag": etag,
            },
        )
    )

    poller = RSSPoller(sources=[source], db=tmp_db, event_bus=bus)
    await poller._poll_source(source)

    # First poll: no conditional headers sent (we had no prior values).
    first_call = respx.calls[0].request
    assert first_call.headers.get("if-modified-since") is None
    assert first_call.headers.get("if-none-match") is None

    # Second poll: conditional headers should be present.
    await poller._poll_source(source)
    second_call = respx.calls[1].request
    assert second_call.headers.get("if-modified-since") == last_modified
    assert second_call.headers.get("if-none-match") == etag


@respx.mock
async def test_304_response_skips_parsing(tmp_db: Database) -> None:
    """When the source returns 304 Not Modified, the poller skips
    inserting any rows for that poll cycle. Verifies bandwidth
    savings translate into work savings."""
    bus = EventBus()
    source = NewsSourceConfig(
        name="reuters",
        type="rss",
        url="https://example.com/feed.xml",
        poll_interval_sec=60,
        is_kalshi_approved=True,
    )
    # First poll returns 200; second poll returns 304.
    route = respx.get("https://example.com/feed.xml")
    route.side_effect = [
        httpx.Response(
            200,
            text=SAMPLE_RSS,
            headers={
                "Content-Type": "application/rss+xml",
                "Last-Modified": "Sun, 26 Apr 2026 12:00:00 GMT",
                "ETag": '"abc123"',
            },
        ),
        httpx.Response(304),
    ]

    poller = RSSPoller(sources=[source], db=tmp_db, event_bus=bus)
    await poller._poll_source(source)
    rows_after_first = list(tmp_db.connect().execute("SELECT id FROM news_events"))
    assert len(rows_after_first) == 2

    await poller._poll_source(source)
    rows_after_second = list(tmp_db.connect().execute("SELECT id FROM news_events"))
    # No new rows on 304 — the existing two are unchanged.
    assert len(rows_after_second) == 2


# ---------------------------------------------------------------------------
# PR #30 follow-up — per-source User-Agent override
# ---------------------------------------------------------------------------


def test_news_source_config_accepts_user_agent_override() -> None:
    """Pydantic model validates with the override set."""
    s = NewsSourceConfig(
        name="the_information",
        type="rss",
        url="https://www.theinformation.com/feed",
        poll_interval_sec=120,
        is_kalshi_approved=True,
        user_agent_override="Mozilla/5.0 ... Chrome/121.0.0.0 Safari/537.36",
    )
    assert s.user_agent_override == "Mozilla/5.0 ... Chrome/121.0.0.0 Safari/537.36"


def test_news_source_config_default_user_agent_override_is_none() -> None:
    """Existing source entries without the field default to None,
    so the global UA continues to apply for them."""
    s = NewsSourceConfig(
        name="bloomberg",
        type="rss",
        url="https://feeds.bloomberg.com/politics/news.rss",
        poll_interval_sec=90,
        is_kalshi_approved=True,
    )
    assert s.user_agent_override is None


@respx.mock
async def test_user_agent_override_sent_when_set(tmp_db: Database) -> None:
    """When a source has user_agent_override set, the per-request
    User-Agent header takes precedence over the client-default
    Safari UA. Pins the the_information fix."""
    bus = EventBus()
    chrome_ua = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    )
    source = NewsSourceConfig(
        name="the_information",
        type="rss",
        url="https://example.com/feed.xml",
        poll_interval_sec=120,
        is_kalshi_approved=True,
        user_agent_override=chrome_ua,
    )
    respx.get("https://example.com/feed.xml").mock(
        return_value=httpx.Response(
            200, text=SAMPLE_RSS, headers={"Content-Type": "application/rss+xml"}
        )
    )

    poller = RSSPoller(sources=[source], db=tmp_db, event_bus=bus)
    await poller._poll_source(source)

    sent_ua = respx.calls[0].request.headers.get("user-agent")
    assert sent_ua == chrome_ua


@respx.mock
async def test_global_user_agent_used_when_override_unset(tmp_db: Database) -> None:
    """When user_agent_override is None (the common case), the
    client's default Safari UA is sent. Pins the no-regression
    side of the fix."""
    from trumpbot.news.rss import USER_AGENT as GLOBAL_UA

    bus = EventBus()
    source = NewsSourceConfig(
        name="bloomberg",
        type="rss",
        url="https://example.com/feed.xml",
        poll_interval_sec=90,
        is_kalshi_approved=True,
    )
    respx.get("https://example.com/feed.xml").mock(
        return_value=httpx.Response(
            200, text=SAMPLE_RSS, headers={"Content-Type": "application/rss+xml"}
        )
    )

    poller = RSSPoller(sources=[source], db=tmp_db, event_bus=bus)
    await poller._poll_source(source)

    sent_ua = respx.calls[0].request.headers.get("user-agent")
    assert sent_ua == GLOBAL_UA
