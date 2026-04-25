"""NewsMonitor: abstract contract for normalized news event streams."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any


class NewsMonitor(ABC):
    """Abstract source of normalized NewsEvent objects.

    Concrete implementations (RSSPoller, TwitterFirehoseListener,
    PaidNewsAPIClient) emit a unified stream of events; the decision engine
    consumes from the aggregator without knowing which underlying source
    produced any given event.
    """

    @abstractmethod
    async def start(self) -> None:
        """Begin polling or listening for news events."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the monitor and release resources."""

    @abstractmethod
    def events(self) -> AsyncIterator[Any]:
        """Async iterator yielding normalized NewsEvent objects."""
