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
