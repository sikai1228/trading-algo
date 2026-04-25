"""Top-level orchestrator: starts every Phase 1 task, manages shutdown.

Runs the Kalshi WS feed, market discovery, RSS poller, Twitter scraper,
Truth Social scraper, news matcher worker, heartbeat logger, and
healthcheck server concurrently. SIGTERM triggers graceful shutdown:
each task stops accepting new work, drains pending writes, and exits.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from prometheus_client import Counter, Gauge

from trumpbot.config import TrumpbotConfig, load_config
from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    NewsMatchRow,
    fetch_news_events_without_matches,
    insert_news_matches,
    insert_system_event,
    list_active_markets,
    recent_news_events,
)
from trumpbot.discovery.service import MarketDiscoveryService
from trumpbot.discovery.subjects import DEFAULT_SUBJECT_ALIASES, SubjectExtractor
from trumpbot.events.bus import Event, EventBus
from trumpbot.health.server import HealthcheckServer
from trumpbot.kalshi.auth import load_private_key
from trumpbot.kalshi.client import KalshiClient
from trumpbot.kalshi.exceptions import StateError
from trumpbot.market_data.kalshi_ws import KalshiWebSocketFeed
from trumpbot.news.matcher import MarketContext, NewsMatcher
from trumpbot.news.rss import RSSPoller
from trumpbot.news.truthsocial import TruthSocialScraper
from trumpbot.news.twitter import TwitterScraper
from trumpbot.utils.logging import configure_logging, get_logger
from trumpbot.utils.timeutil import utcnow_iso

log = get_logger(__name__)

NEWS_INGESTED = Counter(
    "trumpbot_news_events_ingested_total", "Total news events written to DB", ["source"]
)
MATCHES_WRITTEN = Counter(
    "trumpbot_news_matches_total", "Total matcher rows written", ["confidence_bucket"]
)
ACTIVE_MARKETS = Gauge("trumpbot_active_markets", "Markets currently in 'active' status")
WS_CONNECTED = Gauge("trumpbot_kalshi_ws_connected", "1 if WS connection is open")
TASK_HEALTHY = Gauge("trumpbot_task_healthy", "1 if task healthy", ["task"])


async def _amain(config_path: Path) -> int:
    cfg = load_config(config_path)
    configure_logging(cfg.logging.level)
    log.info("trumpbot_starting", config_path=str(config_path))

    db = Database(cfg.database.path)
    db.connect()
    insert_system_event(
        db,
        event_type="startup",
        severity="info",
        component="daemon",
        message="trumpbot daemon starting",
    )

    extractor = _load_extractor(cfg)
    private_key = load_private_key(
        cfg.kalshi.private_key_path,
        passphrase=(
            cfg.kalshi.private_key_passphrase.encode()
            if cfg.kalshi.private_key_passphrase
            else None
        ),
    )

    rest_client = KalshiClient(
        api_key_id=cfg.kalshi.api_key_id,
        private_key=private_key,
        base_url=cfg.kalshi.base_url,
        rate_per_sec=cfg.kalshi.rate_per_sec,
        burst=cfg.kalshi.rate_burst,
        rate_limit_pct=cfg.kalshi.rate_limit_pct,
    )
    bus = EventBus()
    discovery = MarketDiscoveryService(
        client=rest_client,
        db=db,
        target_series=cfg.kalshi.target_series,
        extractor=extractor,
        event_bus=bus,
        poll_interval_sec=cfg.kalshi.market_discovery_interval_sec,
        backfill_days=cfg.kalshi.backfill_days,
    )
    ws_feed = KalshiWebSocketFeed(
        rest_client=rest_client,
        db=db,
        event_bus=bus,
        api_key_id=cfg.kalshi.api_key_id,
        ws_url=cfg.kalshi.ws_url,
    )
    rss_poller = RSSPoller(sources=cfg.news.sources, db=db, event_bus=bus)
    twitter_scraper = TwitterScraper(handles=cfg.news.sources, db=db, event_bus=bus)
    truth_scraper = TruthSocialScraper(handles=cfg.news.sources, db=db, event_bus=bus)
    matcher_worker = MatcherWorker(
        db=db,
        matcher=NewsMatcher(extractor=extractor),
        event_bus=bus,
        poll_interval_sec=cfg.matcher.poll_interval_sec,
        batch_size=cfg.matcher.batch_size,
    )
    heartbeat = HeartbeatLogger(db=db, interval_sec=cfg.daemon.heartbeat_interval_sec)

    bus.subscribe("news_event_ingested", _make_news_metric_handler())
    bus.subscribe("market_discovered", _make_market_metric_handler(db))
    bus.subscribe("market_status_changed", _make_market_metric_handler(db))

    health = HealthcheckServer(
        health_check=_make_health_check(ws_feed, discovery, matcher_worker),
        host=cfg.health.host,
        port=cfg.health.port,
    )
    await health.start()

    # Subscribe WS feed to every active market discovered so far.
    for row in list_active_markets(db):
        await ws_feed.subscribe(row["ticker"])

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    await rss_poller.start()
    await twitter_scraper.start()
    await truth_scraper.start()

    tasks: dict[str, asyncio.Task[None]] = {
        "discovery": asyncio.create_task(_supervised(discovery.run, "discovery", critical=True)),
        "kalshi_ws": asyncio.create_task(_supervised(ws_feed.run, "kalshi_ws", critical=True)),
        "matcher": asyncio.create_task(_supervised(matcher_worker.run, "matcher", critical=False)),
        "heartbeat": asyncio.create_task(_supervised(heartbeat.run, "heartbeat", critical=False)),
    }

    log.info("trumpbot_started", task_count=len(tasks))
    exit_code = 0
    try:
        done, _ = await asyncio.wait(
            [*tasks.values(), asyncio.create_task(stop_event.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            exc = task.exception() if not task.cancelled() else None
            if exc is not None:
                log.error("task_terminated_with_error", error=repr(exc))
                exit_code = 1
    finally:
        log.info("trumpbot_shutting_down")
        discovery.stop()
        ws_feed.stop()
        matcher_worker.stop()
        heartbeat.stop()
        for task in tasks.values():
            task.cancel()
        for task in tasks.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await rss_poller.stop()
        await twitter_scraper.stop()
        await truth_scraper.stop()
        await rest_client.aclose()
        await health.stop()
        insert_system_event(
            db,
            event_type="shutdown",
            severity="info",
            component="daemon",
            message="trumpbot daemon stopped",
        )
        db.close()
    return exit_code


async def _supervised(
    coro_factory: Callable[[], Awaitable[None]], name: str, *, critical: bool
) -> None:
    """Run ``coro_factory()`` and surface its outcome via metrics + logs."""
    TASK_HEALTHY.labels(task=name).set(1)
    try:
        await coro_factory()
    except asyncio.CancelledError:
        raise
    except StateError:
        TASK_HEALTHY.labels(task=name).set(0)
        log.error("task_state_error_halt", task=name)
        raise
    except Exception as exc:
        TASK_HEALTHY.labels(task=name).set(0)
        log.error("task_failed", task=name, critical=critical, error=repr(exc))
        if critical:
            raise


def _make_news_metric_handler() -> Callable[[Event], Awaitable[None]]:
    async def handle(event: Event) -> None:
        source = str(event.payload.get("source", "unknown"))
        NEWS_INGESTED.labels(source=source).inc()

    return handle


def _make_market_metric_handler(db: Database) -> Callable[[Event], Awaitable[None]]:
    async def handle(_: Event) -> None:
        ACTIVE_MARKETS.set(len(list_active_markets(db)))

    return handle


def _make_health_check(
    ws_feed: KalshiWebSocketFeed,
    discovery: MarketDiscoveryService,
    matcher_worker: MatcherWorker,
) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return all(
            (
                not ws_feed.stopped(),
                not discovery.stopped(),
                not matcher_worker.stopped(),
            )
        )

    return check


def _load_extractor(cfg: TrumpbotConfig) -> SubjectExtractor:
    if cfg.matcher.subject_aliases_path:
        path = Path(cfg.matcher.subject_aliases_path)
        if path.exists():
            import yaml as _yaml

            data = _yaml.safe_load(path.read_text()) or {}
            if not isinstance(data, dict):
                raise ValueError(f"{path} must be a YAML mapping of subject -> aliases")
            return SubjectExtractor(aliases=data)
    return SubjectExtractor(aliases=DEFAULT_SUBJECT_ALIASES)


# ---------------------------------------------------------------------------
# Background workers (defined here for one-file orchestration clarity)
# ---------------------------------------------------------------------------


class MatcherWorker:
    """Consumes new news events, runs the matcher, writes news_market_matches."""

    component = "matcher_worker"

    def __init__(
        self,
        *,
        db: Database,
        matcher: NewsMatcher,
        event_bus: EventBus,
        poll_interval_sec: int = 5,
        batch_size: int = 100,
    ) -> None:
        self._db = db
        self._matcher = matcher
        self._bus = event_bus
        self._poll_interval = poll_interval_sec
        self._batch_size = batch_size
        self._stop = asyncio.Event()

    async def run(self) -> None:
        log.info("matcher_worker_started")
        while not self._stop.is_set():
            try:
                processed = await self._process_batch()
                if processed == 0:
                    await self._sleep(self._poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("matcher_worker_error", error=repr(exc))
                insert_system_event(
                    self._db,
                    event_type="matcher_error",
                    severity="error",
                    component=self.component,
                    message=str(exc),
                )
                await self._sleep(self._poll_interval)
        log.info("matcher_worker_stopped")

    def stop(self) -> None:
        self._stop.set()

    def stopped(self) -> bool:
        return self._stop.is_set()

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            return

    async def _process_batch(self) -> int:
        events = fetch_news_events_without_matches(self._db, limit=self._batch_size)
        if not events:
            return 0
        markets_rows = list_active_markets(self._db)
        contexts = [
            MarketContext(
                ticker=row["ticker"],
                subject=row["subject"] or "",
                open_ts=row["open_ts"],
                close_ts=row["close_ts"],
            )
            for row in markets_rows
            if row["subject"]
        ]
        if not contexts:
            return len(events)
        rows: list[NewsMatchRow] = []
        for evt in events:
            results = self._matcher.match(
                headline=evt["headline"],
                body=evt["body_excerpt"],
                markets=contexts,
                article_published_ts=evt["raw_published_ts"],
            )
            for r in results:
                rows.append(
                    NewsMatchRow(
                        news_event_id=evt["id"],
                        ticker=r.ticker,
                        confidence=r.confidence,
                        matched_subject=r.matched_subject,
                        matched_keywords=r.matched_keywords or None,
                        match_reason=r.match_reason,
                    )
                )
                bucket = _bucket(r.confidence)
                MATCHES_WRITTEN.labels(confidence_bucket=bucket).inc()
        insert_news_matches(self._db, rows)
        return len(events)


def _bucket(confidence: float) -> str:
    if confidence >= 0.8:
        return "high"
    if confidence >= 0.5:
        return "medium"
    if confidence > 0:
        return "low"
    return "zero"


class HeartbeatLogger:
    component = "heartbeat"

    def __init__(self, *, db: Database, interval_sec: int = 60) -> None:
        self._db = db
        self._interval = interval_sec
        self._stop = asyncio.Event()
        self._started_at = time.time()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._emit()
            except Exception as exc:
                log.error("heartbeat_error", error=repr(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue

    def _emit(self) -> None:
        active = list_active_markets(self._db)
        recent = recent_news_events(self._db, limit=1)
        last_news_ts = recent[0]["detected_ts"] if recent else None
        log.info(
            "heartbeat",
            uptime_sec=int(time.time() - self._started_at),
            active_markets=len(active),
            last_news_ts=last_news_ts,
            ts=utcnow_iso(),
        )

    def stop(self) -> None:
        self._stop.set()

    def stopped(self) -> bool:
        return self._stop.is_set()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def run() -> None:
    parser = argparse.ArgumentParser(prog="trumpbot")
    parser.add_argument(
        "--config",
        default=os.environ.get("TRUMPBOT_CONFIG", "/etc/trumpbot/config.yaml"),
        help="Path to YAML config (env: TRUMPBOT_CONFIG)",
    )
    args = parser.parse_args()
    config_path = Path(args.config).expanduser()
    if not config_path.is_file():
        print(f"config not found: {config_path}", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(_amain(config_path)))


__all__ = ["HeartbeatLogger", "MatcherWorker", "run"]
