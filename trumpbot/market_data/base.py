"""MarketDataFeed: abstract contract for real-time market state and historical prices."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any


class MarketDataFeed(ABC):
    """Abstract real-time market data feed.

    Concrete implementations (KalshiWebSocketFeed, MockFeed) plug in
    behind this interface so the rest of the system never depends on a
    specific exchange.
    """

    @abstractmethod
    async def subscribe(self, ticker: str) -> None: ...

    @abstractmethod
    async def unsubscribe(self, ticker: str) -> None: ...

    @abstractmethod
    async def get_orderbook(self, ticker: str) -> Any: ...

    @abstractmethod
    async def get_price_history(self, ticker: str, lookback_minutes: int) -> Any: ...

    @abstractmethod
    def updates(self) -> AsyncIterator[Any]: ...
