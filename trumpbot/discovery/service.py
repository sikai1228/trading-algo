"""MarketDiscoveryService — automated monthly KXTRUMPMEET discovery.

Runs as one of the daemon's asyncio tasks. On every cycle:

1. Compute the current month's event ticker and the next month's
   event ticker from today's UTC date.
2. Fetch each via :meth:`KalshiClient.get_event`. Empty events
   (no markets yet) are silently skipped — the next month's event
   typically isn't populated until a few days before its start.
3. For each market:
    - If new: extract the subject from the title, upsert into
      ``subjects`` and ``markets``, fire a ``market_discovered``
      event, and (if a brand-new event_ticker just appeared) send a
      Telegram notification.
    - If known: refresh price/status/volume only. The title and
      ``resolution_rules`` are the contract for resolution and must
      never be silently rewritten — if Kalshi changes either,
      :func:`market_resolution_unchanged` returns False, the service
      writes a critical ``resolution_rules_changed`` system event,
      sends a Telegram alert, and skips the update.
4. Write per-event snapshot artifacts to ``data/markets/`` (JSON +
   Markdown summary) when the event has at least one market.
5. From day-25 onward, additionally log whether next month's event
   has opened.

Errors during a fetch or persistence are caught per-event so one
broken event never blocks the others. The service uses an explicit
backoff (1m → 5m → 15m → 1h cap) on consecutive failures of the
same event so a repeated 5xx doesn't spam Kalshi.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    MarketRow,
    SubjectRow,
    insert_system_event,
    market_resolution_unchanged,
    update_market_market_data,
    upsert_market,
    upsert_subject,
)
from trumpbot.discovery.event_ticker import (
    DEFAULT_SERIES,
    EventTicker,
    current_event_ticker,
    is_late_month,
    next_month_event_ticker,
)
from trumpbot.discovery.snapshots import MarketSummaryRow, write_snapshots
from trumpbot.discovery.subject_extraction import (
    ExtractedSubject,
    ExtractionFailure,
    extract_subject,
)
from trumpbot.events.bus import Event, EventBus
from trumpbot.kalshi.client import KalshiClient
from trumpbot.kalshi.exceptions import KalshiError, StateError
from trumpbot.kalshi.schemas import KalshiEventResponse, KalshiMarket
from trumpbot.notifications.telegram import TelegramNotifier
from trumpbot.notifications.templates import render_template
from trumpbot.utils.logging import get_logger
from trumpbot.utils.timeutil import parse_iso_to_str, utcnow


def _render(name: str, data: dict) -> str:  # type: ignore[type-arg]
    """Tiny adapter so call sites stay tidy. Single source of truth
    for Telegram message text lives in
    :mod:`trumpbot.notifications.templates`."""
    return render_template(name, data).text


log = get_logger(__name__)

COMPONENT = "market_discovery"
DEFAULT_POLL_INTERVAL_SEC = 3600  # 1 hour
BACKOFF_SCHEDULE_SEC = (60, 300, 900, 3600)  # 1m, 5m, 15m, 1h


@dataclass(frozen=True)
class _PendingMarket:
    """Internal: one market parsed out of a Kalshi event response."""

    market: KalshiMarket
    extracted: ExtractedSubject


class MarketDiscoveryService:
    """Polls Kalshi for the current and next monthly events."""

    component = COMPONENT

    def __init__(
        self,
        *,
        client: KalshiClient,
        db: Database,
        event_bus: EventBus,
        telegram: TelegramNotifier | None = None,
        series: str = DEFAULT_SERIES,
        poll_interval_sec: int = DEFAULT_POLL_INTERVAL_SEC,
        snapshot_dir: Path | str = "data/markets",
        clock: Any = None,
    ) -> None:
        self._client = client
        self._db = db
        self._bus = event_bus
        self._telegram = telegram
        self._series = series
        self._poll_interval = poll_interval_sec
        self._snapshot_dir = Path(snapshot_dir)
        # ``clock`` lets tests inject a deterministic ``today`` source.
        self._clock = clock or _UtcDateClock()
        self._stop = asyncio.Event()
        # Per-event-ticker consecutive failure counter for backoff.
        self._failure_streak: dict[str, int] = {}
        # Event tickers we've already announced via Telegram.
        self._announced_events: set[str] = set()

    # -- lifecycle -----------------------------------------------------

    async def run(self) -> None:
        log.info("market_discovery_started", series=self._series)
        # Seed announcement cache from prior runs so we don't re-notify.
        self._announced_events = self._already_seen_event_tickers()
        while not self._stop.is_set():
            try:
                await self.poll_once()
            except StateError:
                # State errors halt the strategy; re-raise so the
                # daemon supervisor can take action.
                raise
            except Exception as exc:
                log.error("market_discovery_cycle_failed", error=repr(exc))
                insert_system_event(
                    self._db,
                    event_type="market_discovery_cycle_error",
                    severity="error",
                    component=self.component,
                    message=str(exc),
                )
            await self._sleep(self._poll_interval)
        log.info("market_discovery_stopped")

    def stop(self) -> None:
        self._stop.set()

    def stopped(self) -> bool:
        return self._stop.is_set()

    # -- main cycle ----------------------------------------------------

    async def poll_once(self) -> None:
        today = self._clock.today()
        current = current_event_ticker(today, self._series)
        next_month = next_month_event_ticker(today, self._series)
        # Fetch each in turn; failures isolated per ticker.
        await self._discover_event(current, today)
        await self._discover_event(next_month, today)
        if is_late_month(today):
            self._log_next_month_status(next_month)

    async def _discover_event(self, event_ticker: EventTicker, today: date) -> None:
        ticker_str = str(event_ticker)
        delay = self._backoff_for(ticker_str)
        if delay > 0:
            log.debug("market_discovery_backoff", event_ticker=ticker_str, wait_sec=delay)
            await self._sleep(delay)

        try:
            response = await self._client.get_event(ticker_str)
        except KalshiError as exc:
            self._record_failure(ticker_str, exc)
            return
        # Reset the failure counter on a successful fetch.
        self._failure_streak.pop(ticker_str, None)

        if not response.markets:
            log.info("market_discovery_event_empty", event_ticker=ticker_str)
            return

        await self._persist_event(event_ticker, response, today)

    async def _persist_event(
        self,
        event_ticker: EventTicker,
        response: KalshiEventResponse,
        today: date,
    ) -> None:
        ticker_str = str(event_ticker)
        # Parse subjects up front so a single broken title doesn't block
        # the others; we record the failure as a system event but keep
        # going for the rest of the markets in the same event.
        pending: list[_PendingMarket] = []
        for market in response.markets:
            try:
                extracted = extract_subject(market.title)
            except ExtractionFailure as exc:
                insert_system_event(
                    self._db,
                    event_type="subject_extraction_failed",
                    severity="warning",
                    component=self.component,
                    message=str(exc),
                    detail={"ticker": market.ticker, "title": market.title},
                )
                log.warning(
                    "subject_extraction_failed",
                    ticker=market.ticker,
                    title=market.title,
                    error=str(exc),
                )
                continue
            pending.append(_PendingMarket(market=market, extracted=extracted))

        # Track which markets were brand-new this cycle.
        new_market_count = 0
        for pm in pending:
            if await self._upsert_one(event_ticker, pm):
                new_market_count += 1

        # Snapshot artifacts (always for non-empty events).
        resolution_rules = self._best_resolution_rules(pending)
        if not resolution_rules:
            # Critical: every market should carry resolution_rules. If
            # none did, refuse to write the snapshot and alert.
            insert_system_event(
                self._db,
                event_type="resolution_rules_missing",
                severity="critical",
                component=self.component,
                message=f"event {ticker_str} returned markets with no resolution rules",
                detail={"event_ticker": ticker_str, "market_count": len(pending)},
            )
            await self._telegram_alert(
                _render(
                    "alert_warning_event_resolution_rules_missing",
                    {"event_ticker": ticker_str},
                )
            )
            return

        rows = [
            MarketSummaryRow(
                ticker=pm.market.ticker,
                subject_full_name=pm.extracted.full_name,
                title=pm.market.title,
            )
            for pm in pending
        ]
        try:
            json_path, md_path = write_snapshots(
                snapshot_dir=self._snapshot_dir,
                event_ticker=ticker_str,
                raw_response=response.model_dump(mode="json"),
                resolution_rules=resolution_rules,
                markets=rows,
            )
            log.info(
                "market_discovery_snapshot_written",
                event_ticker=ticker_str,
                json_path=str(json_path),
                md_path=str(md_path),
                market_count=len(rows),
                new_markets=new_market_count,
            )
        except OSError as exc:
            log.error("market_discovery_snapshot_failed", event_ticker=ticker_str, error=repr(exc))
            insert_system_event(
                self._db,
                event_type="snapshot_write_failed",
                severity="error",
                component=self.component,
                message=str(exc),
                detail={"event_ticker": ticker_str},
            )

        # Telegram notification for first sighting of this event ticker.
        if ticker_str not in self._announced_events and new_market_count > 0:
            self._announced_events.add(ticker_str)
            insert_system_event(
                self._db,
                event_type="new_event_detected",
                severity="info",
                component=self.component,
                message=f"new event ticker first seen: {ticker_str}",
                detail={
                    "event_ticker": ticker_str,
                    "market_count": len(rows),
                    "subjects": [r.subject_full_name for r in rows],
                },
            )
            subjects_preview = ", ".join(r.subject_full_name for r in rows[:6])
            md_basename = ticker_str.lower().replace("-", "_")
            preview_suffix = "..." if len(rows) > 6 else ""
            await self._telegram_alert(
                _render(
                    "alert_info_market_discovered",
                    {
                        "event_ticker": ticker_str,
                        "market_count": len(rows),
                        "new_subjects_summary": (f"Subjects: {subjects_preview}{preview_suffix}"),
                        "removed_subjects_summary": "",
                        "snapshot_path": f"{md_basename}_summary.md",
                    },
                )
            )
            await self._bus.publish(
                Event(
                    type="new_event_detected",
                    payload={"event_ticker": ticker_str, "market_count": len(rows)},
                )
            )

    async def _upsert_one(self, event_ticker: EventTicker, pm: _PendingMarket) -> bool:
        """Insert or refresh one market. Returns True if it was new."""
        market = pm.market
        extracted = pm.extracted
        rules = (market.rules_primary or "") + (
            "\n\n" + market.rules_secondary if market.rules_secondary else ""
        )
        rules = rules.strip()

        # Reject markets with no resolution rules — the brief calls these
        # "broken" and they must not be auto-inserted.
        if not rules:
            insert_system_event(
                self._db,
                event_type="resolution_rules_missing",
                severity="critical",
                component=self.component,
                message=f"market {market.ticker} returned with no resolution rules",
                detail={"ticker": market.ticker, "title": market.title},
            )
            await self._telegram_alert(
                _render(
                    "alert_warning_market_resolution_rules_missing",
                    {
                        "ticker": market.ticker,
                        "full_name": extracted.full_name,
                    },
                )
            )
            return False

        unchanged = market_resolution_unchanged(
            self._db,
            ticker=market.ticker,
            title=market.title,
            resolution_rules=rules,
        )
        if unchanged is False:
            # Existing record, but title or resolution_rules differ.
            insert_system_event(
                self._db,
                event_type="resolution_rules_changed",
                severity="critical",
                component=self.component,
                message=f"market {market.ticker} title or resolution_rules changed mid-event",
                detail={
                    "ticker": market.ticker,
                    "incoming_title": market.title,
                    "incoming_rules_excerpt": rules[:500],
                },
            )
            await self._telegram_alert(
                _render(
                    "alert_critical_resolution_rules_changed_midevent",
                    {"ticker": market.ticker},
                )
            )
            # Do still update price/status/volume — those are not the contract.
            update_market_market_data(
                self._db,
                ticker=market.ticker,
                last_price_cents=market.last_price,
                volume=market.volume,
                open_interest=market.open_interest,
                status=market.status,
            )
            return False

        if unchanged is True:
            update_market_market_data(
                self._db,
                ticker=market.ticker,
                last_price_cents=market.last_price,
                volume=market.volume,
                open_interest=market.open_interest,
                status=market.status,
            )
            return False

        # Brand-new market. Upsert subject first, then market.
        upsert_subject(
            self._db,
            SubjectRow(
                subject_key=extracted.subject_key,
                full_name=extracted.full_name,
                aliases=_initial_aliases(extracted),
                ticker_suffix=_ticker_suffix_from_market(market.ticker, str(event_ticker)),
                auto_extracted=True,
                llm_enriched=False,
                reviewed=False,
            ),
        )
        upsert_market(
            self._db,
            MarketRow(
                ticker=market.ticker,
                series_ticker=event_ticker.series,
                event_ticker=str(event_ticker),
                title=market.title,
                subtitle=market.subtitle,
                yes_sub_title=market.yes_sub_title,
                no_sub_title=market.no_sub_title,
                subject=extracted.subject_key,
                subject_full_name=extracted.full_name,
                resolution_rules=rules,
                approved_sources=None,
                open_ts=parse_iso_to_str(market.open_time) or "",
                close_ts=parse_iso_to_str(market.close_time),
                expected_expiration_ts=parse_iso_to_str(market.expected_expiration_time),
                status=market.status,
                last_price_cents=market.last_price,
                volume=market.volume,
                open_interest=market.open_interest,
                raw_json=market.model_dump(mode="json"),
            ),
        )
        await self._bus.publish(
            Event(
                type="market_discovered",
                payload={
                    "ticker": market.ticker,
                    "subject_key": extracted.subject_key,
                    "subject_full_name": extracted.full_name,
                    "event_ticker": str(event_ticker),
                },
            )
        )
        return True

    # -- helpers -------------------------------------------------------

    def _best_resolution_rules(self, pending: Iterable[_PendingMarket]) -> str:
        """All markets in a single Kalshi event share the same rules
        text. We pick the first non-empty one and use it as the
        canonical text for the event-level snapshot."""
        for pm in pending:
            primary = pm.market.rules_primary or ""
            secondary = pm.market.rules_secondary or ""
            combined = (primary + ("\n\n" + secondary if secondary else "")).strip()
            if combined:
                return combined
        return ""

    def _backoff_for(self, ticker_str: str) -> int:
        streak = self._failure_streak.get(ticker_str, 0)
        if streak == 0:
            return 0
        idx = min(streak - 1, len(BACKOFF_SCHEDULE_SEC) - 1)
        return BACKOFF_SCHEDULE_SEC[idx]

    def _record_failure(self, ticker_str: str, exc: Exception) -> None:
        self._failure_streak[ticker_str] = self._failure_streak.get(ticker_str, 0) + 1
        log.warning(
            "market_discovery_fetch_failed",
            event_ticker=ticker_str,
            error=repr(exc),
            consecutive_failures=self._failure_streak[ticker_str],
        )
        insert_system_event(
            self._db,
            event_type="event_fetch_failed",
            severity="warning",
            component=self.component,
            message=f"failed to fetch {ticker_str}: {exc!r}",
            detail={"event_ticker": ticker_str, "consecutive": self._failure_streak[ticker_str]},
        )

    def _log_next_month_status(self, next_event_ticker: EventTicker) -> None:
        ticker_str = str(next_event_ticker)
        if ticker_str in self._announced_events:
            event_type = "next_month_event_opened"
            message = f"next month's event {ticker_str} is open"
        else:
            event_type = "next_month_event_not_yet_open"
            message = f"next month's event {ticker_str} not yet open"
        insert_system_event(
            self._db,
            event_type=event_type,
            severity="info",
            component=self.component,
            message=message,
            detail={"event_ticker": ticker_str},
        )

    async def _telegram_alert(self, text: str) -> None:
        if self._telegram is None:
            return
        await self._telegram.send(text)

    async def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    def _already_seen_event_tickers(self) -> set[str]:
        conn = self._db.connect()
        rows = conn.execute(
            "SELECT DISTINCT event_ticker FROM markets WHERE event_ticker IS NOT NULL"
        ).fetchall()
        return {r["event_ticker"] for r in rows}

    # -- backfill -----------------------------------------------------

    async def backfill(self, *, months: int = 2) -> None:
        """One-shot fetch of the current month + ``months - 1`` prior months.

        Used by the daemon at startup so a fresh deployment has historical
        data immediately. Existing markets are left as-is (idempotent).
        """
        today = self._clock.today()
        for offset in range(months):
            target = today.replace(day=1) - timedelta(days=offset * 28)
            target = target.replace(day=1)
            event_ticker = EventTicker(series=self._series, year=target.year, month=target.month)
            await self._discover_event(event_ticker, today)
        # Always also probe the next month (it may already be open).
        await self._discover_event(next_month_event_ticker(today, self._series), today)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _initial_aliases(extracted: ExtractedSubject) -> list[str]:
    """Aliases stored on first discovery: full name + last name (deduped)."""
    out = [extracted.full_name]
    if extracted.last_name and extracted.last_name != extracted.full_name:
        out.append(extracted.last_name)
    return out


def _ticker_suffix_from_market(market_ticker: str, event_ticker: str) -> str | None:
    """Extract the per-person 4CHAR suffix from a market ticker.

    ``market_ticker`` looks like ``KXTRUMPMEET-26APR-VPUT``; the suffix
    is everything after the second dash (``VPUT``).
    """
    prefix = f"{event_ticker}-"
    if not market_ticker.startswith(prefix):
        return None
    return market_ticker[len(prefix) :] or None


class _UtcDateClock:
    """Default clock: today's date in UTC."""

    def today(self) -> date:
        return utcnow().date()
