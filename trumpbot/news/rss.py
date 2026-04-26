"""RSS-based concrete NewsMonitor.

One asyncio task per source, error-isolated: a failing Reuters poll
never affects Bloomberg. Each item is normalized into ``FetchedItem``,
deduplicated by canonical URL via the database UNIQUE index, and
inserted into ``news_events``.

Body extraction: the poller pulls the RSS-provided summary; if a body
is unavailable or empty, the article still lands in the database with
just the headline. We do not fetch the article HTML in Phase 1 — that
is a Phase 2 enhancement.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import feedparser
import httpx
from bs4 import BeautifulSoup

from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    NewsEventRow,
    insert_news_event,
    insert_system_event,
)
from trumpbot.events.bus import Event, EventBus
from trumpbot.news.base import NewsMonitor
from trumpbot.news.sources import FetchedItem, NewsSourceConfig
from trumpbot.utils.logging import get_logger
from trumpbot.utils.timeutil import parse_iso_to_str, utcnow_iso
from trumpbot.utils.url import canonicalize_url

log = get_logger(__name__)

USER_AGENT = "trump-market-bot/1.0 research project"
BODY_EXCERPT_CHARS = 1500
RSS_FETCH_TIMEOUT_SEC = 30
SOURCE_FAILURE_THRESHOLD = 5
COMPONENT = "rss_poller"


class RSSPoller(NewsMonitor):
    """Concurrent multi-source RSS poller."""

    def __init__(
        self,
        *,
        sources: list[NewsSourceConfig],
        db: Database,
        event_bus: EventBus,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._sources = [s for s in sources if s.type == "rss"]
        self._db = db
        self._bus = event_bus
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(
            timeout=RSS_FETCH_TIMEOUT_SEC,
            headers={"User-Agent": USER_AGENT},
        )
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._consecutive_failures: dict[str, int] = {}

    async def start(self) -> None:
        for source in self._sources:
            task = asyncio.create_task(self._poll_loop(source), name=f"rss:{source.name}")
            self._tasks.append(task)
        log.info("rss_poller_started", source_count=len(self._sources))

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(BaseException):
                await task
        if self._owns_http:
            await self._http.aclose()
        log.info("rss_poller_stopped")

    async def events(self) -> AsyncIterator[FetchedItem]:
        # The RSS poller writes directly to the database rather than a
        # shared queue (downstream consumers read from the DB), but the
        # interface contract requires this method to exist.
        if False:  # pragma: no cover — contractual no-op for this monitor
            yield FetchedItem(
                source="",
                source_weight=0.0,
                is_kalshi_approved=False,
                headline="",
                url=None,
                body_excerpt=None,
                author=None,
                published_ts=None,
            )

    # -- internals --------------------------------------------------------

    async def _poll_loop(self, source: NewsSourceConfig) -> None:
        delay = source.poll_interval_sec
        while not self._stop.is_set():
            try:
                await self._poll_source(source)
                self._consecutive_failures[source.name] = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._consecutive_failures[source.name] = (
                    self._consecutive_failures.get(source.name, 0) + 1
                )
                log.warning(
                    "rss_poll_failed",
                    source=source.name,
                    error=repr(exc),
                    consecutive_failures=self._consecutive_failures[source.name],
                )
                if self._consecutive_failures[source.name] == SOURCE_FAILURE_THRESHOLD:
                    msg = f"{source.name} failed {SOURCE_FAILURE_THRESHOLD} consecutive polls"
                    insert_system_event(
                        self._db,
                        event_type="source_failure",
                        severity="error",
                        component=COMPONENT,
                        message=msg,
                        detail={"source": source.name, "last_error": repr(exc)},
                    )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)

    async def _poll_source(self, source: NewsSourceConfig) -> None:
        if not source.url:
            raise ValueError(f"RSS source {source.name!r} has no url")
        log.debug("rss_poll", source=source.name)
        response = await self._http.get(source.url)
        if response.status_code == 429:
            # Honor 429 with extra backoff: pause this source for 5 intervals.
            log.warning("rss_rate_limited", source=source.name)
            await asyncio.sleep(source.poll_interval_sec * 5)
            return
        response.raise_for_status()

        feed = feedparser.parse(response.content)
        for entry in feed.entries:
            item = _entry_to_item(entry, source)
            if item is None:
                continue
            await self._persist(item)

    async def _persist(self, item: FetchedItem) -> None:
        canonical = canonicalize_url(item.url) if item.url else None
        row = NewsEventRow(
            source=item.source,
            source_weight=item.source_weight,
            is_kalshi_approved=item.is_kalshi_approved,
            headline=item.headline,
            url=item.url,
            url_canonical=canonical,
            body_excerpt=item.body_excerpt,
            author=item.author,
            raw_published_ts=item.published_ts,
            detected_ts=utcnow_iso(),
            has_photo=item.has_photo,
            has_video=item.has_video,
            raw_data=dict(item.raw_data) if item.raw_data else None,
        )
        new_id = insert_news_event(self._db, row)
        if new_id is None:
            return
        await self._bus.publish(
            Event(
                type="news_event_ingested",
                payload={"news_event_id": new_id, "source": item.source, "url": item.url},
            )
        )


def _entry_to_item(entry: Any, source: NewsSourceConfig) -> FetchedItem | None:
    """Map a feedparser entry to our normalized FetchedItem."""
    headline = (entry.get("title") or "").strip()
    if not headline:
        return None
    url = entry.get("link") or None
    summary_html = entry.get("summary") or entry.get("description") or ""
    body_text = _clean_html(summary_html)[:BODY_EXCERPT_CHARS] if summary_html else None
    author = entry.get("author") or None
    published = entry.get("published") or entry.get("updated") or None
    published_iso = parse_iso_to_str(published)

    has_photo = False
    has_video = False
    if "media_content" in entry:
        for media in entry["media_content"]:
            mtype = (media.get("medium") or media.get("type") or "").lower()
            if "image" in mtype:
                has_photo = True
            if "video" in mtype:
                has_video = True
    if not has_photo and body_text and "photo" in body_text.lower():
        has_photo = True
    if not has_video and body_text and "video" in body_text.lower():
        has_video = True

    raw: dict[str, str | int | float | None] = {
        "id": entry.get("id"),
        "guid": entry.get("guid"),
        "link": url,
    }

    return FetchedItem(
        source=source.name,
        source_weight=source.weight,
        is_kalshi_approved=source.is_kalshi_approved,
        headline=headline,
        url=url,
        body_excerpt=body_text,
        author=author,
        published_ts=published_iso,
        has_photo=has_photo,
        has_video=has_video,
        raw_data=raw,
    )


def _clean_html(html: str) -> str:
    """Strip tags, collapse whitespace."""
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(separator=" ", strip=True)
    return " ".join(text.split())
