"""MarketDiscoveryService: poll Kalshi REST, persist target markets.

Polls the configured target series every ``poll_interval_sec`` seconds.
For each market with status='open' or 'settled' (within the lookback
window for backfill), upserts into ``markets`` with the full resolution
rules text and an extracted subject. Records system events for
lifecycle transitions.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from datetime import timedelta

from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    MarketRow,
    get_market,
    insert_system_event,
    upsert_market,
)
from trumpbot.discovery.subjects import SubjectExtractor
from trumpbot.events.bus import Event, EventBus
from trumpbot.kalshi.client import KalshiClient
from trumpbot.kalshi.exceptions import KalshiError, StateError
from trumpbot.kalshi.schemas import KalshiMarket
from trumpbot.utils.logging import get_logger
from trumpbot.utils.timeutil import parse_iso, parse_iso_to_str, utcnow

log = get_logger(__name__)


# Patterns used to extract the approved-source list from Kalshi resolution
# rules text. Kalshi typically writes the source list inline in the rules,
# e.g. "as confirmed by reports from Reuters, AP, or Bloomberg".
_SOURCE_PATTERN = re.compile(
    r"\b(reuters|ap|associated press|bloomberg|nyt|new york times|"
    r"washington post|wapo|wall street journal|wsj|politico|axios|semafor|"
    r"the information|cnn|fox news|msnbc|nbc|abc|cbs|whitehouse\.gov|"
    r"state department|state\.gov|department of defense|defense\.gov|"
    r"truth social|@realdonaldtrump|@whitehouse|@presssec|@potus|"
    r"@secstate|@statedept|@deptofdefense)\b",
    re.IGNORECASE,
)


def _extract_approved_sources(rules_text: str) -> list[str]:
    """Best-effort extraction of source mentions from resolution rules."""
    found: list[str] = []
    seen: set[str] = set()
    for match in _SOURCE_PATTERN.findall(rules_text):
        normalized = match.lower()
        if normalized not in seen:
            seen.add(normalized)
            found.append(normalized)
    return found


def _resolution_text(market: KalshiMarket) -> str:
    parts = [market.rules_primary or "", market.rules_secondary or ""]
    return "\n\n".join(p for p in parts if p).strip()


def _to_market_row(
    market: KalshiMarket,
    *,
    extractor: SubjectExtractor,
    series_ticker_default: str | None = None,
) -> MarketRow:
    rules = _resolution_text(market)
    subject = extractor.extract(market.title, market.subtitle, market.yes_sub_title, rules)
    sources = _extract_approved_sources(rules) if rules else []
    open_ts = parse_iso_to_str(market.open_time)
    close_ts = parse_iso_to_str(market.close_time)
    expected_ts = parse_iso_to_str(market.expected_expiration_time)
    raw = market.model_dump(mode="json")
    return MarketRow(
        ticker=market.ticker,
        series_ticker=market.series_ticker or series_ticker_default or "",
        event_ticker=market.event_ticker,
        title=market.title,
        subtitle=market.subtitle,
        yes_sub_title=market.yes_sub_title,
        no_sub_title=market.no_sub_title,
        subject=subject,
        resolution_rules=rules,
        approved_sources=sources or None,
        open_ts=open_ts or "",
        close_ts=close_ts,
        expected_expiration_ts=expected_ts,
        status=market.status,
        last_price_cents=market.last_price,
        volume=market.volume,
        open_interest=market.open_interest,
        raw_json=raw,
    )


class MarketDiscoveryService:
    """Background poller that keeps the markets table in sync."""

    component = "market_discovery"

    def __init__(
        self,
        *,
        client: KalshiClient,
        db: Database,
        target_series: list[str],
        extractor: SubjectExtractor,
        event_bus: EventBus,
        poll_interval_sec: int = 300,
        backfill_days: int = 60,
    ) -> None:
        self._client = client
        self._db = db
        self._target_series = list(target_series)
        self._extractor = extractor
        self._event_bus = event_bus
        self._poll_interval = poll_interval_sec
        self._backfill_days = backfill_days
        self._stop = asyncio.Event()

    async def run(self) -> None:
        """Run the discovery loop until ``stop()`` is called."""
        log.info("market_discovery_started", target_series=self._target_series)
        await self.backfill()
        while not self._stop.is_set():
            try:
                await self.poll_once()
            except StateError:
                raise
            except KalshiError as exc:
                log.error("market_discovery_kalshi_error", error=str(exc))
                insert_system_event(
                    self._db,
                    event_type="kalshi_error",
                    severity="error",
                    component=self.component,
                    message=str(exc),
                )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
        log.info("market_discovery_stopped")

    def stop(self) -> None:
        self._stop.set()

    def stopped(self) -> bool:
        return self._stop.is_set()

    async def backfill(self) -> None:
        """One-shot scan over the past ``backfill_days`` for open + settled markets."""
        cutoff = utcnow() - timedelta(days=self._backfill_days)
        for series in self._target_series:
            for status in ("open", "settled"):
                markets = await self._client.iter_markets(series_ticker=series, status=status)
                for market in markets:
                    open_dt = parse_iso(market.open_time)
                    if status == "settled" and open_dt is not None and open_dt < cutoff:
                        continue
                    await self._upsert_and_emit(market, series)

    async def poll_once(self) -> None:
        """One pass over every target series for status='open'."""
        for series in self._target_series:
            markets = await self._client.iter_markets(series_ticker=series, status="open")
            for market in markets:
                await self._upsert_and_emit(market, series)

    async def _upsert_and_emit(self, market: KalshiMarket, series_ticker: str) -> None:
        existing = get_market(self._db, market.ticker)
        existing_status = existing["status"] if existing is not None else None
        row = _to_market_row(market, extractor=self._extractor, series_ticker_default=series_ticker)
        upsert_market(self._db, row)

        if existing_status is None:
            await self._event_bus.publish(
                Event(
                    type="market_discovered",
                    payload={"ticker": market.ticker, "subject": row.subject, "title": row.title},
                )
            )
        elif existing_status != row.status:
            insert_system_event(
                self._db,
                event_type="market_status_changed",
                severity="info",
                component=self.component,
                message=f"{market.ticker}: {existing_status} -> {row.status}",
                detail={"ticker": market.ticker, "from": existing_status, "to": row.status},
            )
            await self._event_bus.publish(
                Event(
                    type="market_status_changed",
                    payload={
                        "ticker": market.ticker,
                        "from": existing_status,
                        "to": row.status,
                    },
                )
            )


def export_market_extractor_state(extractor: SubjectExtractor) -> str:
    """Diagnostic helper: dump the alias dictionary as JSON."""
    return json.dumps(extractor.aliases, indent=2, sort_keys=True)
