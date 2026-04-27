"""RSS-based concrete NewsMonitor.

One asyncio task per source, error-isolated: a failing Reuters poll
never affects Bloomberg. Each item is normalized into ``FetchedItem``,
deduplicated by canonical URL via the database UNIQUE index, and
inserted into ``news_events``.

Body extraction: the poller pulls the RSS-provided summary; if a body
is unavailable or empty, the article still lands in the database with
just the headline. We do not fetch the article HTML in Phase 1 — that
is a Phase 2 enhancement.

**Phase 4 Part 2.12** added two changes:

- User-Agent switched from the custom bot string to a Safari UA.
  The investigation found several sources (`politico_picks`,
  `truthsocial`) returning HTTP 403 to the bot UA but 200 to a
  real-browser UA. Same request body, only the UA differs. Don't
  revert without re-verifying.
- ``If-Modified-Since`` / ``If-None-Match`` conditional headers are
  sent on every poll after the first. On HTTP 304 the poller skips
  parsing entirely. Sources that don't honor the headers just
  return 200 with the full body (no harm done). Cuts real
  bandwidth ~70-90 % on well-behaved feeds.
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
    upsert_source_status,
)
from trumpbot.events.bus import Event, EventBus
from trumpbot.news.base import NewsMonitor
from trumpbot.news.sources import FetchedItem, NewsSourceConfig
from trumpbot.utils.logging import get_logger
from trumpbot.utils.timeutil import parse_iso_to_str, utcnow_iso
from trumpbot.utils.url import canonicalize_url

log = get_logger(__name__)

# Phase 4 Part 2.12 — switched from "trump-market-bot/1.0 research
# project" to a real Safari UA. Several sources (politico_picks,
# truthsocial, others) reject the bot UA with HTTP 403; the browser
# UA gets a clean 200. Don't revert without re-verifying.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4 Safari/605.1.15"
)
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
        # follow_redirects=True is essential — many feeds (MSNBC, ABC,
        # WaPo, ...) return 301 to canonical URLs. httpx defaults to
        # NOT following, which would silently turn every 301 feed into
        # zero ingested articles.
        self._http = http_client or httpx.AsyncClient(
            timeout=RSS_FETCH_TIMEOUT_SEC,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._consecutive_failures: dict[str, int] = {}
        # Phase 4 Part 2.12 — per-source conditional-request state.
        # The poller stashes the ``Last-Modified`` and ``ETag`` headers
        # the source returned on its last 200 response and sends them
        # back as ``If-Modified-Since`` / ``If-None-Match`` on the
        # next poll. On 304 (Not Modified) the poller skips parsing.
        # Resets on daemon restart; first poll after restart sends
        # no conditional header and does a full feed fetch.
        self._last_modified: dict[str, str] = {}
        self._etag: dict[str, str] = {}

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

        # Phase 4 Part 2.12 — send conditional-request headers when
        # we have prior ones from this source. RSS servers that
        # honor these return 304 with empty body when nothing has
        # changed.
        request_headers: dict[str, str] = {}
        if (lm := self._last_modified.get(source.name)) is not None:
            request_headers["If-Modified-Since"] = lm
        if (et := self._etag.get(source.name)) is not None:
            request_headers["If-None-Match"] = et

        # PR #30 follow-up — per-source UA override. The httpx client
        # is shared across all RSS sources and carries the global
        # Safari UA in its default headers; setting User-Agent on
        # this request overrides the client default for this one
        # call. Used for the_information, which serves 403 to the
        # Safari UA but 200 to a Chrome UA. See
        # docs/investigations/source_status_audit.md Section 4a.
        if source.user_agent_override is not None:
            request_headers["User-Agent"] = source.user_agent_override

        response = await self._http.get(source.url, headers=request_headers or None)
        if response.status_code == 304:
            # Nothing new since our last poll. Skip parsing entirely.
            # PR #33: a 304 still confirms the source is REACHABLE, so
            # update last_successful_poll. We deliberately leave
            # newest_feed_item_ts alone — 304 means "feed contents
            # unchanged since you last asked", so the prior value is
            # still correct. If a feed is genuinely paused, every
            # poll will return 304 and newest_feed_item_ts will age
            # out, which is the rotation_paused signal.
            log.debug("rss_not_modified", source=source.name)
            upsert_source_status(
                self._db,
                source_name=source.name,
                current_status="active",
                last_successful_poll=utcnow_iso(),
            )
            return
        if response.status_code == 429:
            # Honor 429 with extra backoff: pause this source for 5 intervals.
            log.warning("rss_rate_limited", source=source.name)
            await asyncio.sleep(source.poll_interval_sec * 5)
            return
        response.raise_for_status()

        # Stash the validators for the next poll, if the server gave
        # us any. Servers that don't set these headers just mean we
        # won't get 304s — no harm.
        if (lm := response.headers.get("last-modified")) is not None:
            self._last_modified[source.name] = lm
        if (et := response.headers.get("etag")) is not None:
            self._etag[source.name] = et

        feed = feedparser.parse(response.content)
        # PR #33 — compute the newest article timestamp from the parsed
        # entries (NOT the response headers, which are server-set and
        # don't reflect content freshness for many feeds). Used by
        # source_health_loop to detect rotation_paused feeds.
        newest_feed_item_ts = _newest_entry_iso(feed.entries)
        upsert_source_status(
            self._db,
            source_name=source.name,
            current_status="active",
            last_successful_poll=utcnow_iso(),
            newest_feed_item_ts=newest_feed_item_ts,
        )
        for entry in feed.entries:
            item = _entry_to_item(entry, source)
            if item is None:
                continue
            await self._persist(item)

    async def _persist(self, item: FetchedItem) -> None:
        canonical = canonicalize_url(item.url) if item.url else None
        row = NewsEventRow(
            source=item.source,
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


def _newest_entry_iso(entries: list[Any]) -> str | None:
    """PR #33 helper — pick the newest published/updated timestamp
    across a parsed feed's entries and return it as an ISO-8601
    string (UTC). Returns ``None`` when no entry carries a parseable
    timestamp.

    feedparser exposes ``published`` / ``updated`` strings on each
    entry. We re-use ``parse_iso_to_str`` (the same parser used at
    persistence time) so the returned format matches ``raw_published_ts``
    in news_events for free downstream comparability.

    A garbage timestamp on one entry must not crash the whole poll
    (real feeds occasionally have malformed dates), so the per-entry
    parse is wrapped in a swallow-and-skip.
    """
    newest: str | None = None
    for entry in entries:
        raw = entry.get("published") or entry.get("updated") or None
        if not raw:
            continue
        try:
            iso = parse_iso_to_str(raw)
        except (ValueError, TypeError, OverflowError):
            # Including dateutil's ParserError, which subclasses ValueError.
            continue
        if iso is None:
            continue
        if newest is None or iso > newest:
            newest = iso
    return newest
