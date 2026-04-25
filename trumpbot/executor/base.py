"""Executor and ApprovalGate: order submission and human-in-the-loop approval."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Executor(ABC):
    """Abstract order executor.

    Concrete implementations (KalshiExecutor, DryRunExecutor,
    PaperTradingExecutor) submit approved orders to the exchange and
    report outcomes. Default deployment is DryRunExecutor until explicitly
    switched to live.

    ``submit`` accepts only RiskApprovedOrder, enforced by the type system.
    """

    @abstractmethod
    async def submit(self, order: Any) -> Any:
        """Submit a RiskApprovedOrder; return the resulting OrderResult."""

    @abstractmethod
    async def cancel(self, order_id: str) -> None:
        """Cancel an open order by exchange order id."""


class ApprovalGate(ABC):
    """Abstract approval gate sitting between RiskManager and Executor.

    Two modes: ``human`` (default) routes to Telegram with inline
    approve/reject buttons and waits for response within timeout;
    ``auto`` passes orders through immediately. Even in auto mode,
    tripwires (max triggers per window, kill switch) remain active.
    """

    @abstractmethod
    async def request_approval(self, order: Any) -> Any:
        """Return an ApprovalResult: approved, rejected, or expired."""
