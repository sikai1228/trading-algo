"""KalshiWebSocketFeed: live orderbook + ticker + trade + lifecycle stream.

Maintains an in-memory orderbook per subscribed ticker and reconciles
delta messages by sequence number. On any sequence gap or disconnect,
flushes local state and refetches a REST snapshot before resuming
delta processing.

Periodic price snapshots are written every 30 s; an additional snapshot
fires whenever the best bid or best ask moves by >= 2 cents.

Heartbeat: ping every 25 s, reconnect if the next pong is missed.
Reconnect with exponential backoff capped at 60 s.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pydantic
import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    OrderbookSnapshotRow,
    PriceSnapshotRow,
    insert_orderbook_snapshot,
    insert_price_snapshot,
    insert_system_event,
)
from trumpbot.events.bus import Event, EventBus
from trumpbot.kalshi.auth import WS_AUTH_PATH, build_auth_headers
from trumpbot.kalshi.client import KalshiClient
from trumpbot.market_data.base import MarketDataFeed
from trumpbot.utils.logging import get_logger
from trumpbot.utils.timeutil import utcnow_iso

log = get_logger(__name__)

# Production elections WebSocket. The path segment must match
# ``WS_AUTH_PATH`` in trumpbot.kalshi.auth so that the URL Kalshi
# receives and the path that gets signed are identical strings.
DEFAULT_WS_URL = f"wss://api.elections.kalshi.com{WS_AUTH_PATH}"
HEARTBEAT_INTERVAL_SEC = 25.0
HEARTBEAT_TIMEOUT_SEC = 10.0
RECONNECT_BACKOFFS_SEC = (1, 2, 4, 8, 16, 32, 60)
PRICE_SNAPSHOT_INTERVAL_SEC = 30
ORDERBOOK_SNAPSHOT_INTERVAL_SEC = 300
PRICE_CHANGE_THRESHOLD_CENTS = 2

CHANNELS = ("orderbook_delta", "ticker", "trade", "market_lifecycle")


@dataclass
class _MarketBook:
    """In-memory yes/no orderbook for one ticker."""

    yes_levels: dict[int, int] = field(default_factory=dict)
    no_levels: dict[int, int] = field(default_factory=dict)
    last_seq: int | None = None
    last_periodic_snapshot_at: float = 0.0
    last_orderbook_snapshot_at: float = 0.0
    last_yes_bid: int | None = None
    last_yes_ask: int | None = None
    last_no_bid: int | None = None
    last_no_ask: int | None = None
    last_trade_price_cents: int | None = None
    volume_24h: int | None = None

    def best_yes_bid(self) -> int | None:
        return max(self.yes_levels) if self.yes_levels else None

    def best_yes_ask(self) -> int | None:
        # In Kalshi-style books we keep both sides as price->size of resting
        # orders on the YES side; for asks we return the lowest entry that
        # represents a sell. Phase 1 stores both sides as separate maps.
        return min(self.yes_levels) if self.yes_levels else None

    def best_no_bid(self) -> int | None:
        return max(self.no_levels) if self.no_levels else None

    def best_no_ask(self) -> int | None:
        return min(self.no_levels) if self.no_levels else None

    def yes_levels_sorted(self) -> list[tuple[int, int]]:
        return sorted(self.yes_levels.items(), reverse=True)

    def no_levels_sorted(self) -> list[tuple[int, int]]:
        return sorted(self.no_levels.items(), reverse=True)

    def apply_snapshot(self, yes: list[tuple[int, int]], no: list[tuple[int, int]]) -> None:
        self.yes_levels = {p: s for p, s in yes if s > 0}
        self.no_levels = {p: s for p, s in no if s > 0}

    def apply_delta(self, side: str, price: int, delta: int) -> None:
        target = self.yes_levels if side == "yes" else self.no_levels
        existing = target.get(price, 0)
        new_size = existing + delta
        if new_size <= 0:
            target.pop(price, None)
        else:
            target[price] = new_size


class _IncomingMessage(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="allow")

    type: str
    seq: int | None = None
    msg: dict[str, Any] | None = None
    sid: int | None = None


class KalshiWebSocketFeed(MarketDataFeed):
    """Concrete MarketDataFeed backed by Kalshi's WS v2."""

    component = "kalshi_ws"

    def __init__(
        self,
        *,
        rest_client: KalshiClient,
        db: Database,
        event_bus: EventBus,
        api_key_id: str,
        ws_url: str = DEFAULT_WS_URL,
    ) -> None:
        self._rest = rest_client
        self._db = db
        self._bus = event_bus
        self._api_key_id = api_key_id
        self._ws_url = ws_url
        self._private_key = rest_client._private_key
        self._tickers: set[str] = set()
        self._books: dict[str, _MarketBook] = {}
        self._update_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=10000)
        self._stop = asyncio.Event()
        self._connected = asyncio.Event()
        self._next_cmd_id = 1

    # -- MarketDataFeed --------------------------------------------------

    async def subscribe(self, ticker: str) -> None:
        self._tickers.add(ticker)
        self._books.setdefault(ticker, _MarketBook())

    async def unsubscribe(self, ticker: str) -> None:
        self._tickers.discard(ticker)
        self._books.pop(ticker, None)

    async def get_orderbook(self, ticker: str) -> dict[str, list[tuple[int, int]]]:
        book = self._books.get(ticker)
        if book is None:
            ob = await self._rest.get_orderbook(ticker)
            return {
                "yes": [(p, s) for p, s in ob.yes],
                "no": [(p, s) for p, s in ob.no],
            }
        return {
            "yes": book.yes_levels_sorted(),
            "no": book.no_levels_sorted(),
        }

    async def get_price_history(self, ticker: str, lookback_minutes: int) -> list[dict[str, Any]]:
        # Phase 1 reads from local DB rather than Kalshi candlesticks to
        # avoid an extra REST call; downstream consumers can request
        # candlesticks directly via the REST client when needed.
        conn = self._db.connect()
        cur = conn.execute(
            """
            SELECT ts, yes_bid_cents, yes_ask_cents, last_trade_price_cents
            FROM price_snapshots
            WHERE ticker = ? AND ts >= datetime('now', ?)
            ORDER BY ts DESC
            """,
            (ticker, f"-{int(lookback_minutes)} minutes"),
        )
        return [dict(r) for r in cur.fetchall()]

    async def updates(self) -> AsyncIterator[dict[str, Any]]:
        while not self._stop.is_set():
            try:
                msg = await asyncio.wait_for(self._update_queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            yield msg

    # -- lifecycle -------------------------------------------------------

    async def run(self) -> None:
        """Main loop: connect, drain messages, reconnect with backoff on failure."""
        attempt = 0
        log.info("kalshi_ws_starting", url=self._ws_url, tickers=sorted(self._tickers))
        while not self._stop.is_set():
            try:
                await self._connect_and_run()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                wait = RECONNECT_BACKOFFS_SEC[min(attempt, len(RECONNECT_BACKOFFS_SEC) - 1)]
                attempt += 1
                log.warning(
                    "kalshi_ws_disconnected",
                    error=repr(exc),
                    next_attempt_in=wait,
                    attempt=attempt,
                )
                insert_system_event(
                    self._db,
                    event_type="ws_disconnect",
                    severity="warning",
                    component=self.component,
                    message=f"reconnect in {wait}s after error: {exc!r}",
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=wait)

    def stop(self) -> None:
        self._stop.set()

    def stopped(self) -> bool:
        return self._stop.is_set()

    # -- connection internals -------------------------------------------

    async def _connect_and_run(self) -> None:
        # WS handshake signs the WebSocket auth path. The constant lives
        # in trumpbot.kalshi.auth so REST and WS path strings cannot
        # drift apart; never inline the literal here.
        headers = build_auth_headers(
            api_key_id=self._api_key_id,
            private_key=self._private_key,
            method="GET",
            path=WS_AUTH_PATH,
        ).as_dict()

        async with websockets.connect(
            self._ws_url,
            additional_headers=headers,
            ping_interval=None,
        ) as ws:
            self._connected.set()
            await self._after_connect(ws)
            heartbeat_task = asyncio.create_task(self._heartbeat(ws))
            snapshot_task = asyncio.create_task(self._snapshot_loop())
            try:
                await self._read_loop(ws)
            finally:
                heartbeat_task.cancel()
                snapshot_task.cancel()
                with contextlib.suppress(BaseException):
                    await heartbeat_task
                with contextlib.suppress(BaseException):
                    await snapshot_task
                self._connected.clear()

    async def _after_connect(self, ws: ClientConnection) -> None:
        # Refresh REST snapshot for every subscribed ticker before resuming
        # delta processing — recovers any missed updates during downtime.
        for ticker in sorted(self._tickers):
            try:
                ob = await self._rest.get_orderbook(ticker)
            except Exception as exc:
                log.warning("kalshi_ws_rest_snapshot_failed", ticker=ticker, error=repr(exc))
                continue
            book = self._books.setdefault(ticker, _MarketBook())
            book.apply_snapshot(
                [(p, s) for p, s in ob.yes],
                [(p, s) for p, s in ob.no],
            )
            book.last_seq = None  # reset; deltas will rebuild

        if self._tickers:
            cmd = {
                "id": self._next_cmd_id,
                "cmd": "subscribe",
                "params": {
                    "channels": list(CHANNELS),
                    "market_tickers": sorted(self._tickers),
                },
            }
            self._next_cmd_id += 1
            await ws.send(json.dumps(cmd))
            log.info("kalshi_ws_subscribed", tickers=sorted(self._tickers))

    async def _read_loop(self, ws: ClientConnection) -> None:
        async for raw in ws:
            try:
                payload = json.loads(raw)
            except ValueError:
                log.warning("kalshi_ws_invalid_json", raw=str(raw)[:200])
                continue
            try:
                msg = _IncomingMessage.model_validate(payload)
            except pydantic.ValidationError as exc:
                log.warning("kalshi_ws_schema_mismatch", errors=exc.errors())
                continue
            await self._dispatch(msg, payload)

    async def _heartbeat(self, ws: ClientConnection) -> None:
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
                pong_waiter = await ws.ping()
                try:
                    await asyncio.wait_for(pong_waiter, timeout=HEARTBEAT_TIMEOUT_SEC)
                except TimeoutError as exc:
                    raise ConnectionClosed(None, None) from exc
        except ConnectionClosed:
            return

    async def _snapshot_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(1)
            now = loop.time()
            for ticker, book in self._books.items():
                if now - book.last_periodic_snapshot_at >= PRICE_SNAPSHOT_INTERVAL_SEC:
                    self._write_price_snapshot(ticker, book, reason="periodic")
                    book.last_periodic_snapshot_at = now
                if now - book.last_orderbook_snapshot_at >= ORDERBOOK_SNAPSHOT_INTERVAL_SEC:
                    self._write_orderbook_snapshot(ticker, book)
                    book.last_orderbook_snapshot_at = now

    # -- message dispatch ------------------------------------------------

    async def _dispatch(self, msg: _IncomingMessage, raw_payload: dict[str, Any]) -> None:
        if msg.type in {"subscribed", "ok", "ack"}:
            return
        if msg.type == "error":
            log.error("kalshi_ws_server_error", payload=raw_payload)
            return
        body = msg.msg or {}
        ticker = body.get("market_ticker") or body.get("ticker")
        book = self._books.setdefault(ticker, _MarketBook()) if isinstance(ticker, str) else None

        if msg.type == "orderbook_snapshot" and book is not None:
            book.apply_snapshot(
                [(int(p), int(s)) for p, s in body.get("yes", [])],
                [(int(p), int(s)) for p, s in body.get("no", [])],
            )
            book.last_seq = msg.seq
        elif msg.type == "orderbook_delta" and book is not None:
            if book.last_seq is not None and msg.seq is not None and msg.seq != book.last_seq + 1:
                log.warning(
                    "kalshi_ws_sequence_gap",
                    ticker=ticker,
                    expected=book.last_seq + 1,
                    actual=msg.seq,
                )
                insert_system_event(
                    self._db,
                    event_type="sequence_gap",
                    severity="warning",
                    component=self.component,
                    message=f"sequence gap on {ticker}",
                    detail={"expected": book.last_seq + 1, "actual": msg.seq},
                )
                book.yes_levels.clear()
                book.no_levels.clear()
                book.last_seq = None
                # Re-fetch snapshot via REST and resync.
                try:
                    ob = await self._rest.get_orderbook(ticker or "")
                    book.apply_snapshot(
                        [(p, s) for p, s in ob.yes],
                        [(p, s) for p, s in ob.no],
                    )
                except Exception as exc:
                    log.error("kalshi_ws_resync_failed", ticker=ticker, error=repr(exc))
                return
            side = body.get("side", "")
            price = int(body.get("price", 0))
            delta = int(body.get("delta", 0))
            if side in {"yes", "no"} and price > 0:
                book.apply_delta(side, price, delta)
                book.last_seq = msg.seq
                self._maybe_record_price_change(ticker or "", book)
        elif msg.type == "ticker" and book is not None:
            last_price = body.get("price")
            if isinstance(last_price, int):
                book.last_trade_price_cents = last_price
            volume = body.get("volume")
            if isinstance(volume, int):
                book.volume_24h = volume
        elif msg.type == "trade" and book is not None:
            trade_price = body.get("price")
            if isinstance(trade_price, int):
                book.last_trade_price_cents = trade_price
        elif msg.type == "market_lifecycle":
            insert_system_event(
                self._db,
                event_type="market_lifecycle",
                severity="info",
                component=self.component,
                message=f"lifecycle event for {ticker}",
                detail=body,
            )
            await self._bus.publish(Event(type="market_lifecycle", payload=body))

        try:
            self._update_queue.put_nowait({"type": msg.type, "msg": body})
        except asyncio.QueueFull:
            log.warning("kalshi_ws_queue_full")

    def _maybe_record_price_change(self, ticker: str, book: _MarketBook) -> None:
        new_yes_bid = book.best_yes_bid()
        new_yes_ask = book.best_yes_ask()
        moved = False
        if new_yes_bid is not None and book.last_yes_bid is not None:
            if abs(new_yes_bid - book.last_yes_bid) >= PRICE_CHANGE_THRESHOLD_CENTS:
                moved = True
        elif new_yes_bid != book.last_yes_bid:
            moved = True
        if new_yes_ask is not None and book.last_yes_ask is not None:
            if abs(new_yes_ask - book.last_yes_ask) >= PRICE_CHANGE_THRESHOLD_CENTS:
                moved = True
        elif new_yes_ask != book.last_yes_ask:
            moved = True
        if moved:
            self._write_price_snapshot(ticker, book, reason="price_change")
        book.last_yes_bid = new_yes_bid
        book.last_yes_ask = new_yes_ask
        book.last_no_bid = book.best_no_bid()
        book.last_no_ask = book.best_no_ask()

    def _write_price_snapshot(self, ticker: str, book: _MarketBook, *, reason: str) -> None:
        row = PriceSnapshotRow(
            ticker=ticker,
            yes_bid_cents=book.best_yes_bid(),
            yes_ask_cents=book.best_yes_ask(),
            no_bid_cents=book.best_no_bid(),
            no_ask_cents=book.best_no_ask(),
            yes_bid_size=book.yes_levels.get(book.best_yes_bid() or 0),
            yes_ask_size=book.yes_levels.get(book.best_yes_ask() or 0),
            no_bid_size=book.no_levels.get(book.best_no_bid() or 0),
            no_ask_size=book.no_levels.get(book.best_no_ask() or 0),
            last_trade_price_cents=book.last_trade_price_cents,
            volume_24h=book.volume_24h,
            ts=utcnow_iso(),
            snapshot_reason=reason,
        )
        try:
            insert_price_snapshot(self._db, row)
        except Exception as exc:
            log.error("kalshi_ws_snapshot_write_failed", ticker=ticker, error=repr(exc))

    def _write_orderbook_snapshot(self, ticker: str, book: _MarketBook) -> None:
        row = OrderbookSnapshotRow(
            ticker=ticker,
            yes_levels=book.yes_levels_sorted(),
            no_levels=book.no_levels_sorted(),
            ts=utcnow_iso(),
        )
        try:
            insert_orderbook_snapshot(self._db, row)
        except Exception as exc:
            log.error("kalshi_ws_orderbook_write_failed", ticker=ticker, error=repr(exc))
