"""DryRunExecutor — simulated order placement against the live orderbook.

Phase 2's executor of record. Inserts a row into ``trades`` for every
"filled" entry/reentry, and updates the row to a closed state on
stop-loss exits or market resolution.

All money math is integer cents. The ``ExecutionResult`` returned to
the caller mirrors the row written so callers can act without an extra
DB round trip.

Phase 4 will add a sibling ``KalshiExecutor`` that talks to the
exchange. The interface (``Executor.submit``) is identical.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    TradeInsertRow,
    close_trade,
    get_open_trade_for_ticker,
    insert_trade,
    list_open_trades,
    update_trade_marks,
)
from trumpbot.types.intents import (
    ExecutionResult,
    ReentryIntent,
    RiskApprovedOrder,
    StopLossIntent,
    TradeIntent,
)


@dataclass(frozen=True)
class _Quote:
    yes_bid_cents: int | None
    yes_ask_cents: int | None


# Type alias: the orderbook callable the executor uses to read live
# prices. Daemon wiring passes a thin wrapper over the WS feed; tests
# pass a static dict.
OrderbookFn = Callable[[str], _Quote]


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


class DryRunExecutor:
    """Phase 2 simulated executor.

    Honors the type-system chokepoint: ``submit`` accepts only
    :class:`RiskApprovedOrder`, never a raw intent.
    """

    def __init__(self, *, db: Database, orderbook_fn: OrderbookFn) -> None:
        self._db = db
        self._quote_fn = orderbook_fn

    # -- main API -----------------------------------------------------

    def submit(self, approved: RiskApprovedOrder) -> ExecutionResult:
        """Simulate a fill and persist the resulting trade record."""
        intent = approved.intent
        if isinstance(intent, StopLossIntent):
            return self._submit_stop_loss(intent, approved)
        return self._submit_buy(intent, approved)

    def update_position_marks(self) -> int:
        """For every open dry-run trade, refresh `unrealized_pnl_usd_cents`
        from the current YES bid. Returns the number of rows updated.

        The daemon calls this every 60 s. Cheap query (small table).
        """
        updated = 0
        for row in list_open_trades(self._db):
            if row["status"] != "dry_run":
                continue
            quote = self._quote_fn(row["ticker"])
            if quote.yes_bid_cents is None:
                continue
            current_value = quote.yes_bid_cents * row["quantity"]
            unrealized = current_value - row["cost_basis_usd_cents"]
            update_trade_marks(
                self._db,
                trade_id=row["id"],
                current_value_usd_cents=current_value,
                unrealized_pnl_usd_cents=unrealized,
            )
            updated += 1
        return updated

    def close_resolved(self, *, ticker: str, resolution: str) -> ExecutionResult | None:
        """Called when a market settles (status moves to settled_yes /
        settled_no). Closes any open dry-run position at $1.00 (YES) or
        $0.00 (NO)."""
        row = get_open_trade_for_ticker(self._db, ticker)
        if row is None:
            return None
        # YES contract pays out 100¢ on YES resolution, 0¢ on NO.
        payoff_cents = 100 if resolution == "settled_yes" else 0
        proceeds_cents = payoff_cents * row["quantity"]
        realized = proceeds_cents - row["cost_basis_usd_cents"]
        close_trade(
            self._db,
            trade_id=row["id"],
            new_status="dry_run_closed_resolved",
            exit_price_cents=payoff_cents,
            realized_pnl_usd_cents=realized,
            exited_at=_utcnow_iso(),
        )
        return ExecutionResult(
            trade_id=row["id"],
            status="filled",
            fill_price_cents=payoff_cents,
            fill_quantity=row["quantity"],
            realized_pnl_usd_cents=realized,
            notes=f"market resolved {resolution}",
        )

    # -- internals ----------------------------------------------------

    def _submit_buy(
        self, intent: TradeIntent | ReentryIntent, approved: RiskApprovedOrder
    ) -> ExecutionResult:
        quote = self._quote_fn(intent.ticker)
        if quote.yes_ask_cents is None:
            return ExecutionResult(
                trade_id=-1,
                status="rejected",
                notes="no ask available at submission time",
            )
        # Simulated fill at the current ask. Slippage modeling is
        # Phase 3 — this overstates real fill quality but is consistent
        # for backtesting (same code path runs in both).
        fill_price = min(quote.yes_ask_cents, intent.target_price_cents)
        quantity = approved.adjusted_quantity or intent.target_quantity
        cost_basis = fill_price * quantity
        trade_id = insert_trade(
            self._db,
            TradeInsertRow(
                ticker=intent.ticker,
                status="dry_run",
                entry_price_cents=fill_price,
                quantity=quantity,
                cost_basis_usd_cents=cost_basis,
                triggering_match_id=intent.triggering_match_id,
                triggering_intent_json=intent.model_dump_json(),
                risk_decision_id=approved.risk_decision_id,
                approval_id=None,
                is_reentry=isinstance(intent, ReentryIntent),
                prior_trade_id=getattr(intent, "prior_trade_id", None),
                reasoning_text=intent.reasoning_text,
                entered_at=_utcnow_iso(),
            ),
        )
        return ExecutionResult(
            trade_id=trade_id,
            status="filled",
            fill_price_cents=fill_price,
            fill_quantity=quantity,
            notes="dry-run entry simulated at current ask",
        )

    def _submit_stop_loss(
        self, intent: StopLossIntent, approved: RiskApprovedOrder
    ) -> ExecutionResult:
        quote = self._quote_fn(intent.ticker)
        # We sell into the bid. If no bid is available (extremely rare),
        # fall back to the bid the engine observed when generating the
        # intent. Simulated fills only — Phase 4 reads the live book.
        bid = quote.yes_bid_cents if quote.yes_bid_cents is not None else intent.current_bid_cents
        proceeds = bid * intent.position_quantity
        realized = proceeds - intent.cost_basis_usd_cents
        close_trade(
            self._db,
            trade_id=intent.trade_id,
            new_status="dry_run_closed_stop",
            exit_price_cents=bid,
            realized_pnl_usd_cents=realized,
            exited_at=_utcnow_iso(),
        )
        # The risk-decision audit row is logged separately by the
        # RiskManager; we don't double-write here.
        return ExecutionResult(
            trade_id=intent.trade_id,
            status="filled",
            fill_price_cents=bid,
            fill_quantity=intent.position_quantity,
            realized_pnl_usd_cents=realized,
            notes=f"dry-run stop-loss exit at bid {bid}c",
        )


# Re-exported so callers don't have to dig.
Quote = _Quote


__all__ = ["DryRunExecutor", "OrderbookFn", "Quote"]
