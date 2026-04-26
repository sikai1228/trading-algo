"""Truth Social scraper for @realDonaldTrump.

Truth Social runs Mastodon under the hood, exposing each profile's
posts as JSON at ``https://truthsocial.com/api/v1/accounts/{id}/statuses``.
The scraper resolves the account id once per process via the
``/api/v1/accounts/lookup`` endpoint, then polls the statuses endpoint
at the configured interval.

This is best-effort. The platform changes frequently; if scraping breaks
we log a system event and continue. Production use should consider
official API access if/when it becomes available.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

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

BASE = "https://truthsocial.com"
USER_AGENT = "trump-market-bot/1.0 research project"
COMPONENT = "truthsocial_scraper"
SOURCE_FAILURE_THRESHOLD = 5


class TruthSocialScraper(NewsMonitor):
    def __init__(
        self,
        *,
        handles: list[NewsSourceConfig],
        db: Database,
        event_bus: EventBus,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._sources = [s for s in handles if s.type == "truth_social"]
        self._db = db
        self._bus = event_bus
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(
            timeout=30,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._account_ids: dict[str, str] = {}
        self._last_seen: dict[str, str] = {}
        self._consecutive_failures: dict[str, int] = {}

    async def start(self) -> None:
        for source in self._sources:
            task = asyncio.create_task(
                self._poll_loop(source), name=f"truth_social:{source.handle}"
            )
            self._tasks.append(task)
        if self._sources:
            log.info("truth_social_scraper_started", handles=[s.handle for s in self._sources])

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
            log.error("truth_social_handle_missing", source=source.name)
            return
        while not self._stop.is_set():
            try:
                await self._poll_account(source)
                self._consecutive_failures[source.handle] = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._consecutive_failures[source.handle] = (
                    self._consecutive_failures.get(source.handle, 0) + 1
                )
                log.warning("truth_social_poll_failed", handle=source.handle, error=repr(exc))
                if self._consecutive_failures[source.handle] == SOURCE_FAILURE_THRESHOLD:
                    insert_system_event(
                        self._db,
                        event_type="source_failure",
                        severity="error",
                        component=COMPONENT,
                        message=f"truth_social:{source.handle} failed {SOURCE_FAILURE_THRESHOLD}x",
                    )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=source.poll_interval_sec)

    async def _poll_account(self, source: NewsSourceConfig) -> None:
        assert source.handle is not None  # mypy narrowing
        account_id = await self._resolve_account(source.handle)
        if account_id is None:
            return
        params: dict[str, Any] = {"limit": 20, "exclude_replies": "true"}
        since_id = self._last_seen.get(source.handle)
        if since_id:
            params["since_id"] = since_id
        response = await self._http.get(
            f"{BASE}/api/v1/accounts/{account_id}/statuses", params=params
        )
        response.raise_for_status()
        statuses = response.json()
        if not isinstance(statuses, list) or not statuses:
            return
        self._last_seen[source.handle] = str(statuses[0]["id"])
        for status in statuses:
            await self._persist(status, source)

    async def _resolve_account(self, handle: str) -> str | None:
        if handle in self._account_ids:
            return self._account_ids[handle]
        response = await self._http.get(f"{BASE}/api/v1/accounts/lookup", params={"acct": handle})
        if response.status_code != 200:
            log.warning("truth_social_lookup_failed", handle=handle, status=response.status_code)
            return None
        data = response.json()
        account_id = str(data.get("id")) if isinstance(data, dict) else None
        if account_id:
            self._account_ids[handle] = account_id
        return account_id

    async def _persist(self, status: dict[str, Any], source: NewsSourceConfig) -> None:
        content_html = status.get("content") or ""
        text = BeautifulSoup(content_html, "lxml").get_text(separator=" ", strip=True)
        url = status.get("url") or f"{BASE}/@{source.handle}/{status.get('id')}"
        canonical = canonicalize_url(url)
        media: list[dict[str, Any]] = status.get("media_attachments") or []
        has_photo = any(m.get("type") == "image" for m in media)
        has_video = any(m.get("type") in {"video", "gifv"} for m in media)
        published_iso = parse_iso_to_str(status.get("created_at"))
        row = NewsEventRow(
            source=source.name,
            is_kalshi_approved=source.is_kalshi_approved,
            headline=text[:280] or "(no text)",
            url=url,
            url_canonical=canonical,
            body_excerpt=text,
            author=f"@{source.handle}",
            raw_published_ts=published_iso,
            detected_ts=utcnow_iso(),
            has_photo=has_photo,
            has_video=has_video,
            raw_data={"status_id": str(status.get("id"))},
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
