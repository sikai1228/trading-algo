"""RiskManager — single chokepoint between intent and execution.

Every order flow goes through ``RiskManager.evaluate``. The
:class:`RiskApprovedOrder` it returns is the only thing
:class:`Executor.submit` accepts (enforced both by the executor's
type signature and by a Pydantic validator on
``RiskApprovedOrder`` itself that requires the
``RISK_APPROVAL_TOKEN`` instance held privately by the manager).

Persists every decision (approved or rejected) to ``risk_decisions``
so the audit trail is complete.
"""

from __future__ import annotations

from dataclasses import dataclass

from trumpbot.db.connection import Database
from trumpbot.decision.engine import BankrollState
from trumpbot.types.intents import (
    RISK_APPROVAL_TOKEN,
    AnyIntent,
    ReentryIntent,
    RiskApprovedOrder,
    RiskRejection,
    StopLossIntent,
    TradeIntent,
)


@dataclass(frozen=True)
class RiskConfig:
    """Caps + flags the RiskManager enforces.

    ``halted`` is the stub for the Phase-3 ``/halt`` command. Phase 2
    leaves it False; setting it True makes every check fail with
    ``trading_halted`` and is how Phase 3 will plug in the kill switch.

    Phase 3 Part 1 renamed ``position_size_cap_usd_cents`` to
    ``position_size_hard_cap_cents`` to match the engine and to make
    clear that this is "cap one" of the two-cap system. The walk-aware
    engine sizes intents below this value, so the risk-side check is a
    defense-in-depth guard rather than the primary enforcer.
    """

    enabled: bool = True
    max_buy_price_cents: int = 80
    total_exposure_cap_pct: float = 0.30
    position_size_hard_cap_cents: int = 2000
    halted: bool = False


@dataclass(frozen=True)
class RiskState:
    """Inputs the RiskManager needs beyond the intent itself."""

    bankroll: BankrollState
    open_position_tickers: frozenset[str]
    """Tickers we already hold open positions in. Used by the
    `position_not_open` guard for stop-loss intents."""


class RiskManager:
    """The single chokepoint.

    ``db`` is optional. Pass ``None`` from the backtester (or any
    read-only context) to skip writing to ``risk_decisions``; the in-
    memory check chain still runs and the returned ``RiskApprovedOrder``
    still carries a sentinel ``risk_decision_id`` of ``0`` so the
    downstream type contract is unchanged.
    """

    def __init__(self, *, db: Database | None, config: RiskConfig) -> None:
        self._db = db
        self._cfg = config

    # -- public API ---------------------------------------------------

    def evaluate(self, intent: AnyIntent, state: RiskState) -> RiskApprovedOrder | RiskRejection:
        """Run the 6-check guard chain. First failure rejects."""
        # If the manager itself is disabled, every flow is rejected.
        if not self._cfg.enabled:
            return self._reject(intent, "risk_disabled", "RiskManager.enabled = False", "enabled")

        # 5. Active state check (kill-switch / halt)
        if self._cfg.halted:
            return self._reject(intent, "trading_halted", "kill switch engaged", "halted")

        if isinstance(intent, StopLossIntent):
            return self._evaluate_stop_loss(intent, state)
        return self._evaluate_buy(intent, state)

    # -- buy-side (entry + reentry) -----------------------------------

    def _evaluate_buy(
        self, intent: TradeIntent | ReentryIntent, state: RiskState
    ) -> RiskApprovedOrder | RiskRejection:
        # 4. Price ceiling
        if intent.target_price_cents > self._cfg.max_buy_price_cents:
            return self._reject(
                intent,
                "price_above_ceiling",
                f"target_price_cents={intent.target_price_cents} > "
                f"{self._cfg.max_buy_price_cents}c ceiling",
                "max_buy_price_cents",
            )

        # 1. Bankroll check
        if intent.target_size_usd_cents > state.bankroll.available_bankroll_usd_cents:
            return self._reject(
                intent,
                "insufficient_bankroll",
                f"target ${intent.target_size_usd_cents/100:.2f} > available "
                f"${state.bankroll.available_bankroll_usd_cents/100:.2f}",
                "available_bankroll",
            )

        # 2. Total exposure cap
        new_total = state.bankroll.open_position_cost_usd_cents + intent.target_size_usd_cents
        cap_cents = int(state.bankroll.bankroll_usd_cents * self._cfg.total_exposure_cap_pct)
        if new_total > cap_cents:
            return self._reject(
                intent,
                "exposure_cap_exceeded",
                f"new total ${new_total/100:.2f} > exposure cap "
                f"${cap_cents/100:.2f} "
                f"({self._cfg.total_exposure_cap_pct*100:.0f}% of bankroll)",
                "total_exposure_cap_pct",
            )

        # 3. Per-trade size cap (fixed dollar amount; engages adjustment,
        # does not reject unless the cap is too tight for one contract).
        adjusted_quantity: int | None = None
        adjustment_reason: str | None = None
        per_trade_cap_cents = self._cfg.position_size_hard_cap_cents
        if intent.target_size_usd_cents > per_trade_cap_cents:
            new_qty = per_trade_cap_cents // intent.target_price_cents
            if new_qty < 1:
                # Cap is so tight that we can't buy a single contract.
                return self._reject(
                    intent,
                    "size_cap_below_one_contract",
                    f"per-trade cap ${per_trade_cap_cents/100:.2f} less than one "
                    f"contract at {intent.target_price_cents}c",
                    "position_size_hard_cap_cents",
                )
            adjustment_reason = (
                f"fixed-dollar cap reduced quantity from {intent.target_quantity} to "
                f"{new_qty} (cap ${per_trade_cap_cents/100:.2f})"
            )
            adjusted_quantity = new_qty

        # All checks passed — log and emit RiskApprovedOrder.
        approval_reasoning = self._approval_reasoning(intent, adjustment_reason)
        decision_id = self._record_decision(
            intent_type=intent.intent_type,
            intent=intent,
            decision="approved",
            rejection_reason=None,
            rule_fired=None,
            reasoning_text=approval_reasoning,
        )
        return RiskApprovedOrder(
            intent_type=intent.intent_type,
            intent=intent,
            risk_decision_id=decision_id,
            adjusted_quantity=adjusted_quantity,
            approval_token=RISK_APPROVAL_TOKEN,
        )

    # -- stop-loss ----------------------------------------------------

    def _evaluate_stop_loss(
        self, intent: StopLossIntent, state: RiskState
    ) -> RiskApprovedOrder | RiskRejection:
        # 6. Position state check
        if intent.ticker not in state.open_position_tickers:
            return self._reject(
                intent,
                "position_not_open",
                f"no open position in {intent.ticker} for stop-loss",
                "open_position_tickers",
            )
        decision_id = self._record_decision(
            intent_type=intent.intent_type,
            intent=intent,
            decision="approved",
            rejection_reason=None,
            rule_fired=None,
            reasoning_text=intent.reasoning_text,
        )
        return RiskApprovedOrder(
            intent_type="stop_loss",
            intent=intent,
            risk_decision_id=decision_id,
            adjusted_quantity=None,
            approval_token=RISK_APPROVAL_TOKEN,
        )

    # -- helpers ------------------------------------------------------

    def _reject(
        self, intent: AnyIntent, reason: str, detail: str, rule_fired: str
    ) -> RiskRejection:
        text = f"REJECT [{reason}]: {detail}"
        decision_id = self._record_decision(
            intent_type=intent.intent_type,
            intent=intent,
            decision="rejected",
            rejection_reason=reason,
            rule_fired=rule_fired,
            reasoning_text=text,
        )
        return RiskRejection(
            intent=intent,
            reason=reason,
            detail=detail,
            rule_fired=rule_fired,
            risk_decision_id=decision_id,
        )

    def _approval_reasoning(
        self,
        intent: TradeIntent | ReentryIntent,
        adjustment_reason: str | None,
    ) -> str:
        base = (
            f"APPROVED [{intent.intent_type}]: {intent.target_quantity} contracts of "
            f"{intent.ticker} at {intent.target_price_cents}c "
            f"(${intent.target_size_usd_cents/100:.2f}). All risk checks passed."
        )
        if adjustment_reason:
            return f"{base} {adjustment_reason}."
        return base

    def _record_decision(
        self,
        *,
        intent_type: str,
        intent: AnyIntent,
        decision: str,
        rejection_reason: str | None,
        rule_fired: str | None,
        reasoning_text: str,
    ) -> int:
        # Read-only mode (backtester): skip persistence, return sentinel.
        if self._db is None:
            return 0
        from trumpbot.db.repositories import insert_risk_decision

        return insert_risk_decision(
            self._db,
            intent_type=intent_type,
            intent_json=intent.model_dump_json(),
            decision=decision,
            rejection_reason=rejection_reason,
            rule_fired=rule_fired,
            reasoning_text=reasoning_text,
        )


__all__ = ["RiskConfig", "RiskManager", "RiskState"]
