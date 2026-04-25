"""Typed intent / order / execution models for the Phase 2 decision layer.

Unit conventions (enforced by these types — do not deviate):

- ``PriceCents`` — integer cents per contract (1¢..99¢ for Kalshi).
- ``QuantityContracts`` — integer count of contracts.
- ``USDCents`` — integer hundredths of a US dollar (so $1.00 = 100,
  and a contract bought at 42¢ x 10 contracts = 420 USDCents).

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
    """Initial-entry signal proposed by the DecisionEngine."""

    intent_type: Literal["entry"] = "entry"
    ticker: str
    side: Literal["yes"] = "yes"
    action: Literal["buy"] = "buy"
    target_price_cents: int
    """Maximum price (cents) we'd pay per contract."""

    target_quantity: int
    """Number of contracts."""

    target_size_usd_cents: int
    """Total cost basis in USDCents at target price + quantity."""

    triggering_match_id: int
    confirmation_weight: float
    """source_weight x llm_confidence."""

    confidence_score: float
    """The matcher / LLM confidence (0..1)."""

    is_reentry: Literal[False] = False
    prior_trade_id: None = None


class ReentryIntent(_IntentBase):
    """Re-entry signal: a fresh match against a market where we previously
    held and closed a position."""

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
    """Stop-loss signal: the YES bid has dropped 50¢+ below our entry."""

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
