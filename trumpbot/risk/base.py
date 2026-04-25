"""RiskManager: the single chokepoint that converts a TradeIntent into a RiskApprovedOrder."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class RiskManager(ABC):
    """Abstract risk checkpoint.

    The type system enforces that ``Executor.submit`` accepts only a
    ``RiskApprovedOrder``, and only ``RiskManager`` constructs that type.
    There is no path from the decision engine to the executor that bypasses
    risk checks. This is non-negotiable.

    Implementations enforce position-size cap, total-exposure cap,
    per-market frequency limit, bankroll sufficiency, price ceiling,
    stop-loss check, and post-loss cool-down.
    """

    @abstractmethod
    def evaluate(self, intent: Any) -> Any:
        """Return either a RiskApprovedOrder or a structured rejection."""
