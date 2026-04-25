"""MarketDataFeed: abstract contract for real-time market state and historical prices."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any


class MarketDataFeed(ABC):
    """Abstract real-time market data feed.

    Concrete implementations (KalshiWebSocketFeed, KalshiRESTBackfill, MockFeed)
    plug in behind this interface so the rest of the system never depends on a
    specific exchange.
    """

    @abstractmethod
    async def subscribe(self, ticker: str) -> None:
        """Subscribe to live updates for a market ticker."""

    @abstractmethod
    async def unsubscribe(self, ticker: str) -> None:
        """Stop receiving updates for a ticker."""

    @abstractmethod
    async def get_orderbook(self, ticker: str) -> Any:
        """Return the current orderbook snapshot for a ticker."""

    @abstractmethod
    async def get_price_history(self, ticker: str, lookback_minutes: int) -> Any:
        """Return historical price snapshots over the given lookback window."""

    @abstractmethod
    def updates(self) -> AsyncIterator[Any]:
        """Async iterator yielding live market update events."""
