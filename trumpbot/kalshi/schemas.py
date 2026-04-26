"""Pydantic v2 response schemas for Kalshi REST endpoints.

Every response is parsed against one of these models. Malformed
responses raise ``pydantic.ValidationError`` which the client wraps in
``ValidationError`` (no retry, fail closed).

Schemas are deliberately permissive on optional fields — Kalshi
occasionally adds new fields and we don't want a benign addition to
break ingestion. ``model_config`` allows extra keys.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _BaseModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=True)


class KalshiMarket(_BaseModel):
    """A single market record from /markets or /markets/{ticker}."""

    ticker: str
    series_ticker: str | None = None
    event_ticker: str | None = None
    title: str = ""
    subtitle: str | None = None
    yes_sub_title: str | None = None
    no_sub_title: str | None = None
    rules_primary: str | None = None
    rules_secondary: str | None = None
    open_time: str | None = None
    close_time: str | None = None
    expected_expiration_time: str | None = None
    status: str
    last_price: int | None = None
    yes_bid: int | None = None
    yes_ask: int | None = None
    no_bid: int | None = None
    no_ask: int | None = None
    volume: int | None = None
    open_interest: int | None = None
    category: str | None = None


class KalshiMarketListResponse(_BaseModel):
    markets: list[KalshiMarket] = Field(default_factory=list)
    cursor: str | None = None


class KalshiMarketResponse(_BaseModel):
    market: KalshiMarket


class KalshiOrderbookLevel(_BaseModel):
    """A single price level: [price_cents, size]."""

    price: int
    size: int


class KalshiOrderbook(_BaseModel):
    """Yes/No orderbook depth. Kalshi returns lists of [price, size] pairs."""

    yes: list[list[int]] = Field(default_factory=list)
    no: list[list[int]] = Field(default_factory=list)


class KalshiOrderbookResponse(_BaseModel):
    orderbook: KalshiOrderbook


class KalshiBalance(_BaseModel):
    """Account balance in cents."""

    balance: int


# ---------------------------------------------------------------------
# Phase 4: order placement, position queries, settlement notices.
# ---------------------------------------------------------------------


class KalshiOrder(_BaseModel):
    """A single order returned by /portfolio/orders or
    /portfolio/orders/{order_id}.

    Kalshi's order response shape is a moving target — we accept any
    extra fields and only pin the ones we actually rely on for
    reconciliation + lifecycle tracking. Specifically:

    - ``order_id``       opaque server-side id
    - ``client_order_id``  the UUIDv4 we provide at submission;
                           critical for idempotency.
    - ``ticker``         which market
    - ``status``         'resting' / 'canceled' / 'executed' / 'pending'
    - ``side``           'yes' / 'no'
    - ``action``         'buy' / 'sell'
    - ``type``           'limit' / 'market'
    - ``yes_price``/``no_price`` integer cents
    - ``count``          original requested count (contracts)
    - ``remaining_count`` contracts still resting (0 == fully filled)
    - ``filled_count``   contracts filled (best-effort; some endpoints
                          omit this and we recover it as count - remaining_count)
    - ``avg_fill_price`` integer cents — if Kalshi omits, code recovers
                          from per-fill records.
    """

    order_id: str
    client_order_id: str | None = None
    ticker: str | None = None
    status: str
    side: str | None = None
    action: str | None = None
    type: str | None = None
    yes_price: int | None = None
    no_price: int | None = None
    count: int | None = None
    remaining_count: int | None = None
    filled_count: int | None = None
    avg_fill_price: int | None = None
    created_time: str | None = None
    updated_time: str | None = None


class KalshiOrderResponse(_BaseModel):
    """Wrapper for /portfolio/orders/{order_id}."""

    order: KalshiOrder


class KalshiOrderListResponse(_BaseModel):
    """Wrapper for /portfolio/orders (list)."""

    orders: list[KalshiOrder] = Field(default_factory=list)
    cursor: str | None = None


class KalshiCreateOrderResponse(_BaseModel):
    """Wrapper for POST /portfolio/orders."""

    order: KalshiOrder


class KalshiPosition(_BaseModel):
    """A single position from /portfolio/positions.

    Kalshi positions are ticker-scoped. ``position`` is signed: positive
    is long YES, negative is long NO (Kalshi expresses NO holdings as
    negative YES). We hold the raw shape and let callers interpret.
    """

    ticker: str
    position: int = 0
    market_exposure: int | None = None
    realized_pnl: int | None = None
    fees_paid: int | None = None
    total_traded: int | None = None
    resting_orders_count: int | None = None
    last_updated_ts: str | None = None


class KalshiPositionListResponse(_BaseModel):
    """Wrapper for /portfolio/positions."""

    market_positions: list[KalshiPosition] = Field(default_factory=list)
    event_positions: list[dict[str, Any]] = Field(default_factory=list)
    cursor: str | None = None


class KalshiSettlement(_BaseModel):
    """A single settlement notice from /portfolio/settlements.

    Issued by Kalshi when a market resolves and the contract pays out.
    ``settled_at`` tells us when the trade lifecycle should transition
    to ``live_closed_resolved_yes`` or ``live_closed_resolved_no``.
    """

    ticker: str
    market_result: str | None = None  # 'yes' / 'no' / 'void'
    yes_count: int | None = None
    no_count: int | None = None
    yes_total_cost: int | None = None
    no_total_cost: int | None = None
    revenue: int | None = None
    settled_time: str | None = None


class KalshiSettlementListResponse(_BaseModel):
    """Wrapper for /portfolio/settlements."""

    settlements: list[KalshiSettlement] = Field(default_factory=list)
    cursor: str | None = None


class KalshiCancelOrderResponse(_BaseModel):
    """Wrapper for DELETE /portfolio/orders/{order_id}."""

    order: KalshiOrder
    reduced_by: int | None = None


class KalshiEvent(_BaseModel):
    event_ticker: str
    series_ticker: str | None = None
    title: str = ""
    sub_title: str | None = None
    category: str | None = None
    mutually_exclusive: bool | None = None


class KalshiEventResponse(_BaseModel):
    event: KalshiEvent
    markets: list[KalshiMarket] = Field(default_factory=list)


class KalshiCandlestick(_BaseModel):
    """A single candlestick. Kalshi nests price OHLC under ``price``."""

    end_period_ts: int | None = None
    open_interest: int | None = None
    volume: int | None = None
    price: dict[str, Any] | None = None


class KalshiCandlesticksResponse(_BaseModel):
    ticker: str | None = None
    candlesticks: list[KalshiCandlestick] = Field(default_factory=list)
