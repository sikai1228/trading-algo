"""Phase 4 error categorization for live order placement.

The base exception hierarchy in :mod:`trumpbot.kalshi.exceptions`
splits Kalshi failures into three buckets at the HTTP layer:

    TransientError   network / 5xx / 429  → retry
    ValidationError  400 / 422            → bug, fail closed
    StateError       insufficient funds, etc. → halt bot

For order placement we need a stricter mapping because the *trade
lifecycle* has its own terminal statuses (``error_validation``,
``error_transient``, ``killed_book_moved``, etc.). This module:

1. Categorizes a raised exception into a ``OrderErrorCategory``.
2. Maps each category to the right ``trades.status`` value.
3. Maps each category to the right notification template name.

Centralizing these mappings here means the executor stays small and
the categorization logic is testable without wiring up a fake Kalshi.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from trumpbot.kalshi.exceptions import (
    KalshiError,
    StateError,
    TransientError,
    ValidationError,
)

# Kalshi error-body keywords that point to a duplicate-client-order-id
# response. When we see this, the original order DID land — caller must
# query by client_order_id rather than retry.
_DUPLICATE_CLIENT_ORDER_KEYWORDS: Final[tuple[str, ...]] = (
    "duplicate_client_order_id",
    "client_order_id already exists",
    "client order id already used",
)

OrderErrorCategoryName = Literal[
    "transient",
    "validation",
    "state",
    "duplicate_client_order",
    "unknown",
]


@dataclass(frozen=True)
class OrderErrorCategory:
    """The categorized outcome of a failed ``place_order`` call.

    Attributes:
        name: which bucket the exception falls into.
        trade_status: which value to write to ``trades.status`` so the
            row reflects the failure cleanly. ``None`` when the caller
            should NOT persist a final-state row (e.g. duplicate, where
            the truth is "go look up the original").
        template_name: which notification template to render to the
            user. ``None`` when no user-visible alert should fire.
        should_halt: whether the bot should set ``halt_flag=true`` and
            stop trading on this error. Currently only ``StateError``
            triggers this.
    """

    name: OrderErrorCategoryName
    trade_status: str | None
    template_name: str | None
    should_halt: bool
    detail: str


def _looks_like_duplicate_client_order(body: object) -> bool:
    """True when the response body strongly suggests the order is a
    duplicate of an earlier client_order_id we already submitted."""
    if body is None:
        return False
    text = str(body).lower()
    return any(kw in text for kw in _DUPLICATE_CLIENT_ORDER_KEYWORDS)


def categorize_order_error(exc: BaseException) -> OrderErrorCategory:
    """Map a raised exception to a structured category.

    Order placement specifically:

    - ``TransientError`` → ``"transient"``: the bot doesn't know
      whether the order landed; submission status is ``error_transient``
      and reconciliation must query by client_order_id.
    - ``ValidationError`` containing duplicate-client-order keywords →
      ``"duplicate_client_order"``: the original submission DID land;
      the caller must look up by client_order_id and treat as success.
    - ``ValidationError`` (other) → ``"validation"``: the request was
      malformed. Code bug. Mark the trade ``error_validation`` and
      surface to the user.
    - ``StateError`` → ``"state"``: insufficient funds, market closed,
      etc. Mark the trade ``error_validation`` (we don't have a
      separate bucket — same fail-closed handling) and HALT the bot.
    - Anything else → ``"unknown"``: defensive fallback. Treat like
      transient (preserve idempotency) but log loudly.
    """
    if isinstance(exc, ValidationError):
        if _looks_like_duplicate_client_order(exc.response_body):
            return OrderErrorCategory(
                name="duplicate_client_order",
                trade_status=None,
                template_name=None,
                should_halt=False,
                detail="Kalshi reported duplicate client_order_id; original submission landed.",
            )
        return OrderErrorCategory(
            name="validation",
            trade_status="error_validation",
            template_name="trade_error_validation",
            should_halt=False,
            detail=str(exc),
        )
    if isinstance(exc, StateError):
        return OrderErrorCategory(
            name="state",
            trade_status="error_validation",
            template_name="trade_error_validation",
            should_halt=True,
            detail=str(exc),
        )
    if isinstance(exc, TransientError):
        return OrderErrorCategory(
            name="transient",
            trade_status="error_transient",
            template_name="trade_error_transient",
            should_halt=False,
            detail=str(exc),
        )
    if isinstance(exc, KalshiError):
        return OrderErrorCategory(
            name="unknown",
            trade_status="error_transient",
            template_name="trade_error_transient",
            should_halt=False,
            detail=f"unknown Kalshi error: {exc!r}",
        )
    return OrderErrorCategory(
        name="unknown",
        trade_status="error_transient",
        template_name="trade_error_transient",
        should_halt=False,
        detail=f"unknown error: {exc!r}",
    )


__all__ = [
    "OrderErrorCategory",
    "OrderErrorCategoryName",
    "categorize_order_error",
]
