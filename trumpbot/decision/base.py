"""DecisionEngine: pure transformation from market+news inputs to TradeIntents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from typing import Any


class DecisionEngine(ABC):
    """Pure-function decision engine.

    Performs no I/O: no network calls, no database writes, no timers.
    Given orderbook, news events, current positions, and risk config it
    deterministically produces zero or more TradeIntent objects.

    This purity is what makes the engine directly unit-testable and what
    makes backtesting trivial (replay historical data through the same
    engine).
    """

    @abstractmethod
    def evaluate(
        self,
        orderbook: Any,
        news_events: Sequence[Any],
        positions: Sequence[Any],
        risk_config: Any,
    ) -> Iterable[Any]:
        """Return zero or more TradeIntent objects for the given inputs."""
