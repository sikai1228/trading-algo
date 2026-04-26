"""Twitter / X scraper for verified accounts.

Real-world caveat: scraping x.com without authentication has been
effectively impossible since 2023, and nitter mirrors are gone. For
production use, configure a Twitter API v2 bearer token via the
``TWITTER_BEARER_TOKEN`` environment variable; this module will use the
official API. Without a token, the scraper logs a warning and yields no
items — it does not fail the daemon.

If a bearer token is present we call ``GET /2/users/by/username`` to
resolve the user id, then ``GET /2/users/{id}/tweets`` for recent
tweets. Polling is rate-limited per handle (default 90 s).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

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

TWITTER_API_BASE = "https://api.twitter.com/2"
TWITTER_BEARER_ENV = "TWITTER_BEARER_TOKEN"
COMPONENT = "twitter_scraper"
USER_AGENT = "trump-market-bot/1.0 research project"


class TwitterScraper(NewsMonitor):
    """One asyncio task per tracked handle, polling at the source's interval."""

    def __init__(
        self,
        *,
        handles: list[NewsSourceConfig],
        db: Database,
        event_bus: EventBus,
        bearer_token: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._sources = [s for s in handles if s.type == "twitter"]
        self._db = db
        self._bus = event_bus
        self._bearer = bearer_token or os.environ.get(TWITTER_BEARER_ENV)
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(
            timeout=30,
            headers={"User-Agent": USER_AGENT},
        )
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._user_id_cache: dict[str, str] = {}
        self._last_seen: dict[str, str] = {}

    async def start(self) -> None:
        if not self._sources:
            return
        if not self._bearer:
            log.warning(
                "twitter_scraper_no_bearer",
                message=(
                    "TWITTER_BEARER_TOKEN unset — Twitter ingestion disabled. "
                    "Configure a Twitter API v2 token to enable."
                ),
            )
            insert_system_event(
                self._db,
                event_type="twitter_disabled",
                severity="warning",
                component=COMPONENT,
                message="TWITTER_BEARER_TOKEN unset; no tweets will be ingested",
            )
            return
        for source in self._sources:
            task = asyncio.create_task(self._poll_loop(source), name=f"twitter:{source.handle}")
            self._tasks.append(task)
        log.info("twitter_scraper_started", handles=[s.handle for s in self._sources])

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(BaseException):
                await task
        if self._owns_http:
            await self._http.aclose()

    async def events(self) -> AsyncIterator[FetchedItem]:
        if False:  # pragma: no cover
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
        if source.handle is None:
            log.error("twitter_handle_missing", source=source.name)
            return
        delay = source.poll_interval_sec
        while not self._stop.is_set():
            try:
                await self._poll_handle(source)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("twitter_poll_failed", handle=source.handle, error=repr(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)

    async def _poll_handle(self, source: NewsSourceConfig) -> None:
        assert source.handle is not None  # narrow for mypy
        user_id = await self._resolve_user_id(source.handle)
        if user_id is None:
            return
        params: dict[str, Any] = {
            "max_results": 25,
            "tweet.fields": "created_at,attachments,author_id,entities",
        }
        since_id = self._last_seen.get(source.handle)
        if since_id:
            params["since_id"] = since_id

        response = await self._authed_get(f"/users/{user_id}/tweets", params=params)
        if response.status_code == 429:
            log.warning("twitter_rate_limited", handle=source.handle)
            return
        response.raise_for_status()
        payload = response.json()
        tweets = payload.get("data") or []
        if not tweets:
            return
        self._last_seen[source.handle] = tweets[0]["id"]
        for tweet in tweets:
            await self._persist(tweet, source)

    async def _resolve_user_id(self, handle: str) -> str | None:
        if handle in self._user_id_cache:
            return self._user_id_cache[handle]
        response = await self._authed_get(f"/users/by/username/{handle}")
        if response.status_code != 200:
            log.warning(
                "twitter_user_lookup_failed",
                handle=handle,
                status=response.status_code,
                body=response.text[:200],
            )
            return None
        data = response.json().get("data") or {}
        user_id = data.get("id")
        if user_id:
            self._user_id_cache[handle] = user_id
        return user_id

    async def _authed_get(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._bearer}"}
        return await self._http.get(f"{TWITTER_API_BASE}{path}", params=params, headers=headers)

    async def _persist(self, tweet: dict[str, Any], source: NewsSourceConfig) -> None:
        text = tweet.get("text") or ""
        url = f"https://twitter.com/{source.handle}/status/{tweet.get('id')}"
        canonical = canonicalize_url(url)
        attachments = tweet.get("attachments") or {}
        media_keys = attachments.get("media_keys") or []
        has_photo = any("photo" in key for key in media_keys)
        has_video = any("video" in key or "animated_gif" in key for key in media_keys)
        published_iso = parse_iso_to_str(tweet.get("created_at"))
        row = NewsEventRow(
            source=source.name,
            source_weight=source.weight,
            is_kalshi_approved=source.is_kalshi_approved,
            headline=text[:280],
            url=url,
            url_canonical=canonical,
            body_excerpt=text,
            author=f"@{source.handle}",
            raw_published_ts=published_iso,
            detected_ts=utcnow_iso(),
            has_photo=has_photo,
            has_video=has_video,
            raw_data={"tweet_id": tweet.get("id"), "author_id": tweet.get("author_id")},
        )
        new_id = insert_news_event(self._db, row)
        if new_id is None:
            return
        await self._bus.publish(
            Event(
                type="news_event_ingested",
                payload={"news_event_id": new_id, "source": source.name, "url": url},
            )
        )
