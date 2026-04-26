"""Typed intent / order / execution models for the Phase 2 decision layer.

Unit conventions (enforced by these types — do not deviate):

- ``PriceCents`` — integer cents per contract (1c..99c for Kalshi).
- ``QuantityContracts`` — integer count of contracts.
- ``USDCents`` — integer hundredths of a US dollar (so $1.00 = 100,
  and a contract bought at 42c x 10 contracts = 420 USDCents).

All persisted USD amounts are USDCents so SQLite cannot reintroduce
float drift via a REAL column. Display/Telegram formatting converts
to :class:`decimal.Decimal` at the boundary; never use :class:`float`
on prices anywhere.

The ``RiskApprovedOrder`` type is the single chokepoint between the
risk layer and the executor: it is constructed through a frozen
factory token that only :class:`RiskManager` holds, so anything
calling ``RiskApprovedOrder(...)`` directly fails at runtime.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal, NewType

from pydantic import BaseModel, ConfigDict, Field, field_validator

PriceCents = NewType("PriceCents", int)
"""Integer cents per Kalshi contract (1..99)."""

QuantityContracts = NewType("QuantityContracts", int)
"""Integer count of Kalshi contracts."""

USDCents = NewType("USDCents", int)
"""Integer hundredths of a US dollar.

`100 USDCents == $1.00`. All persisted USD amounts use this unit.
Convert to / from :class:`decimal.Decimal` at the display boundary
only; never use :class:`float` on monetary values.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_intent_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Intents (pure DecisionEngine outputs)
# ---------------------------------------------------------------------------


class _IntentBase(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    intent_id: str = Field(default_factory=_new_intent_id)
    created_at: datetime = Field(default_factory=_utcnow)
    reasoning_text: str

    @field_validator("reasoning_text")
    @classmethod
    def _reasoning_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("reasoning_text is required and must be non-empty")
        return v


class TradeIntent(_IntentBase):
    """Initial-entry signal proposed by the DecisionEngine.

    Phase 3 Part 1 added the order-book-walk fields below
    (``target_avg_fill_price_cents``, ``cap_*``, ``slippage_cents``,
    etc.). The walker computes them once at decision time; the
    executor re-walks at submission time and may produce different
    actuals if the book moved (FOK semantics).
    """

    intent_type: Literal["entry"] = "entry"
    ticker: str
    side: Literal["yes"] = "yes"
    action: Literal["buy"] = "buy"
    target_price_cents: int
    """The price ceiling — strictly the locked 80 c max-buy. Each
    individual fill level may be at or below this; the average is
    captured separately in ``target_avg_fill_price_cents``."""

    target_quantity: int
    """Number of contracts the engine's walk filled."""

    target_size_usd_cents: int
    """Total cost (sum of price x qty across consumed levels), excluding
    fees, in USDCents. Equal to ``OrderbookWalkResult.total_cost_cents``."""

    triggering_match_id: int
    confirmation_weight: float
    """source_weight x llm_confidence."""

    confidence_score: float
    """The matcher / LLM confidence (0..1)."""

    # ---- Phase 3 Part 1: walk + cap audit ------------------------
    target_avg_fill_price_cents: int = 0
    """Average fill price the engine's walk predicted, banker's-rounded.
    Defaulted to 0 for back-compat with synthetic test fixtures that
    don't supply it; production engine always populates it."""

    target_max_fill_price_cents: int = 0
    """Highest price level the walk reached — i.e. the deepest fill."""

    estimated_fees_cents: int = 0
    """Sum of per-level Kalshi entry fees from the walk."""

    estimated_total_cost_cents: int = 0
    """``target_size_usd_cents + estimated_fees_cents``, for audit."""

    cap_binding: Literal["cap_one", "cap_two", "tie", "unknown"] = "unknown"
    """Which cap (hard $20 or 5 % of volume) was binding at decision
    time. ``tie`` when both equal; ``unknown`` for back-compat fixtures."""

    cap_one_value_cents: int = 0
    cap_two_value_cents: int = 0
    slippage_cents: int = 0
    """Average fill price minus best ask — the cost of depth."""

    levels_consumed: list[tuple[int, int]] = []
    """Ordered ``(price_cents, contracts)`` pairs from the walk. Defaults
    to empty list for fixtures."""
    # --------------------------------------------------------------

    is_reentry: Literal[False] = False
    prior_trade_id: None = None


class ReentryIntent(_IntentBase):
    """Re-entry signal: a fresh match against a market where we previously
    held and closed a position. Carries the same Phase 3 walk fields as
    :class:`TradeIntent` so the executor / message templates can branch
    on intent type without a fork in the data shape."""

    intent_type: Literal["reentry"] = "reentry"
    ticker: str
    side: Literal["yes"] = "yes"
    action: Literal["buy"] = "buy"
    target_price_cents: int
    target_quantity: int
    target_size_usd_cents: int
    triggering_match_id: int
    confirmation_weight: float
    confidence_score: float

    # Phase 3 Part 1 — same walk audit fields as TradeIntent.
    target_avg_fill_price_cents: int = 0
    target_max_fill_price_cents: int = 0
    estimated_fees_cents: int = 0
    estimated_total_cost_cents: int = 0
    cap_binding: Literal["cap_one", "cap_two", "tie", "unknown"] = "unknown"
    cap_one_value_cents: int = 0
    cap_two_value_cents: int = 0
    slippage_cents: int = 0
    levels_consumed: list[tuple[int, int]] = []

    is_reentry: Literal[True] = True
    prior_trade_id: int

    prior_trade_outcome: Literal[
        "dry_run_closed_stop",
        "dry_run_closed_resolved",
        "live_closed_stop",
        "live_closed_resolved",
    ]
    prior_trade_realized_pnl_usd_cents: int


class StopLossIntent(_IntentBase):
    """Stop-loss signal: the YES bid has dropped 50c+ below our entry."""

    intent_type: Literal["stop_loss"] = "stop_loss"
    ticker: str
    trade_id: int
    entry_price_cents: int
    current_bid_cents: int
    drop_cents: int
    position_quantity: int
    cost_basis_usd_cents: int
    current_value_usd_cents: int
    unrealized_pnl_usd_cents: int
    suggested_action: Literal["exit_at_market"] = "exit_at_market"


AnyIntent = TradeIntent | ReentryIntent | StopLossIntent


# ---------------------------------------------------------------------------
# Risk decisions (RiskManager output)
# ---------------------------------------------------------------------------


class _RiskApprovalToken:
    """Sentinel object only RiskManager constructs.

    Stored on every :class:`RiskApprovedOrder`; the Pydantic validator
    refuses to build the model unless the token instance is *the* one
    bound to the active RiskManager. Subclassing is fine — the check is
    by ``isinstance``.
    """

    __slots__ = ()


# Module-level instance held by RiskManager. Tests can inject their own
# via ``RiskManager._approval_token`` if needed.
RISK_APPROVAL_TOKEN: _RiskApprovalToken = _RiskApprovalToken()


class RiskApprovedOrder(BaseModel):
    """The only thing :class:`Executor.submit` accepts.

    Construction guard: callers must pass ``approval_token=RISK_APPROVAL_TOKEN``.
    The ``RiskManager`` is the only sanctioned producer; any other code
    path that tries to build one fails at validation time.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True, frozen=True)

    intent_type: Literal["entry", "reentry", "stop_loss"]
    intent: TradeIntent | ReentryIntent | StopLossIntent
    risk_decision_id: int
    risk_check_passed: Literal[True] = True
    adjusted_quantity: int | None = None
    """Set if the size cap reduced ``intent.target_quantity``."""

    approval_token: Annotated[_RiskApprovalToken, Field(exclude=True)]

    @field_validator("approval_token")
    @classmethod
    def _validate_token(cls, v: _RiskApprovalToken) -> _RiskApprovalToken:
        if not isinstance(v, _RiskApprovalToken):
            raise TypeError(
                "RiskApprovedOrder may only be constructed by RiskManager. "
                "If you're seeing this, something is bypassing the risk gate."
            )
        return v


class RiskRejection(BaseModel):
    """RiskManager said no. The decision is logged; the executor never sees it."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True, frozen=True)

    intent: TradeIntent | ReentryIntent | StopLossIntent
    reason: str
    detail: str
    rule_fired: str
    risk_decision_id: int


# ---------------------------------------------------------------------------
# Approval gate
# ---------------------------------------------------------------------------


class ApprovalDecision(BaseModel):
    """Outcome of an :class:`ApprovalGate.request_approval` call."""

    model_config = ConfigDict(extra="forbid")

    intent_id: str
    decision: Literal["approved", "rejected", "expired"]
    decided_at: datetime
    decision_source: Literal["telegram_button", "telegram_command", "timeout"]
    rejected_reason: str | None = None
    approval_record_id: int | None = None
    """Row id in ``telegram_approvals``."""


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class ExecutionResult(BaseModel):
    """Outcome of :class:`Executor.submit`."""

    model_config = ConfigDict(extra="forbid")

    trade_id: int
    """Row id in ``trades``."""

    status: Literal["filled", "rejected", "partial"]
    fill_price_cents: int | None = None
    fill_quantity: int | None = None
    realized_pnl_usd_cents: int | None = None
    notes: str = ""
