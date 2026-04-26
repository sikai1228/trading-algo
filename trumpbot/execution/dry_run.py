"""DryRunExecutor — simulated order placement against the live orderbook.

Phase 3 Part 1 made the dry-run executor walk-aware and FOK-aware so
its behavior matches what we'll do in Phase 4 with real Fill-or-Kill
orders. The executor:

1. Re-fetches the order book at submission time (not stale from
   decision time). The book may have moved during the user's approval
   window.
2. Re-walks the book for the intent's target dollar budget.
3. **Fills** if the re-walk produces ``filled_quantity ==
   intent.target_quantity`` at average price ≤
   ``intent.target_avg_fill_price_cents`` — same trade, possibly
   slightly different actual avg.
4. **Kills** otherwise. No row is written. A ``fok_killed`` system
   event is logged; the gate's Telegram message is updated to
   "Order killed: book moved unfavorably".

Backtester goes through the same code path so historical replays
reflect realistic FOK behavior.

Phase 4 will add a sibling ``KalshiExecutor`` that places real FOK
orders. The interface (``Executor.submit``) is identical.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    TradeInsertRow,
    close_trade,
    get_open_trade_for_ticker,
    insert_system_event,
    insert_trade,
    list_open_trades,
    update_trade_marks,
)
from trumpbot.execution.fees import calculate_entry_fee_cents
from trumpbot.execution.slippage import walk_orderbook_for_buy
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
# top-of-book prices for stop-loss / position-marking. Daemon wiring
# passes a thin wrapper over the WS feed; tests pass a static dict.
OrderbookFn = Callable[[str], _Quote]

# Type alias: the order-book-depth callable the executor uses for the
# FOK re-walk on entry submissions. Returns the unified yes-ask side
# of the live book (NO bids already inverted), or ``None`` when the
# book is unavailable.
DepthFn = Callable[[str], Sequence[tuple[int, int]] | None]


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


class DryRunExecutor:
    """Phase 3 walk-aware, FOK-aware simulated executor.

    Honors the type-system chokepoint: ``submit`` accepts only
    :class:`RiskApprovedOrder`, never a raw intent.

    The ``depth_fn`` callable is required for entry / re-entry
    submissions — the executor re-walks the book at submission time
    and refuses to fill if the walk doesn't produce the target
    quantity at an acceptable average. ``orderbook_fn`` is still used
    for the stop-loss path (which only needs the best YES bid) and
    for ``update_position_marks``.
    """

    def __init__(
        self,
        *,
        db: Database,
        orderbook_fn: OrderbookFn,
        depth_fn: DepthFn | None = None,
    ) -> None:
        self._db = db
        self._quote_fn = orderbook_fn
        self._depth_fn = depth_fn

    # -- main API -----------------------------------------------------

    async def submit(self, approved: RiskApprovedOrder) -> ExecutionResult:
        """Simulate a fill and persist the resulting trade record.

        ``async`` so the call site can be uniform with
        :class:`KalshiExecutor.submit` (which awaits Kalshi's REST
        API). The dry-run path never actually awaits anything; the
        keyword is purely for interface symmetry."""
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
        # YES contract pays out 100c on YES resolution, 0c on NO.
        # Phase 4 Part 2.1: settlement payouts pay no exit fee (Kalshi's
        # fee formula returns 0 at price=0 or price=100 — see
        # trumpbot/execution/fees.py). Pass 0 explicitly for the audit
        # trail rather than leaving it None.
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
            exit_fees_cents=0,
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
        """Phase 3 FOK semantics. Re-walk the book at submission time;
        fill iff the re-walk produces the same target quantity at an
        average ≤ the original ``target_avg_fill_price_cents``."""
        # Path A: depth callable not configured. Fall back to the
        # Phase-2 simple-ask fill — used by older tests until they
        # migrate. Production daemon always wires depth_fn.
        if self._depth_fn is None:
            return self._submit_buy_legacy(intent, approved)

        levels = self._depth_fn(intent.ticker)
        if not levels:
            self._log_killed(
                ticker=intent.ticker,
                kind="fok_killed_insufficient_liquidity",
                reason="no order-book depth available at submission time",
            )
            return ExecutionResult(
                trade_id=-1,
                status="rejected",
                notes="FOK killed: no depth at submission",
            )

        target_qty = approved.adjusted_quantity or intent.target_quantity
        target_avg = intent.target_avg_fill_price_cents
        target_budget = intent.target_size_usd_cents
        # Re-walk for the same dollar budget the engine sized for.
        rewalk = walk_orderbook_for_buy(
            levels,
            target_dollars_cents=target_budget,
            max_price_cents=intent.target_price_cents,
            fee_calculator=calculate_entry_fee_cents,
        )

        # FOK kill conditions: filled less than target, OR avg fill
        # exceeds the original target avg. Either is "book moved
        # unfavorably between approval and submission".
        if rewalk.filled_quantity < target_qty:
            self._log_killed(
                ticker=intent.ticker,
                kind="fok_killed_insufficient_liquidity",
                reason=(
                    f"re-walk filled {rewalk.filled_quantity} < target " f"{target_qty} contracts"
                ),
            )
            return ExecutionResult(
                trade_id=-1,
                status="rejected",
                notes=(
                    f"FOK killed: re-walk filled {rewalk.filled_quantity} "
                    f"of {target_qty} contracts"
                ),
            )

        if target_avg > 0 and rewalk.average_fill_price_cents > target_avg:
            self._log_killed(
                ticker=intent.ticker,
                kind="fok_killed_book_moved",
                reason=(
                    f"re-walk avg {rewalk.average_fill_price_cents}c > " f"target avg {target_avg}c"
                ),
            )
            return ExecutionResult(
                trade_id=-1,
                status="rejected",
                notes=(
                    f"FOK killed: avg fill {rewalk.average_fill_price_cents}c "
                    f"> target {target_avg}c"
                ),
            )

        # Re-walk passed FOK gate. Use NEW (re-walked) numbers, not
        # stale decision-time numbers.
        actual_qty = rewalk.filled_quantity
        actual_avg = rewalk.average_fill_price_cents
        actual_cost = rewalk.total_cost_cents
        trade_id = insert_trade(
            self._db,
            TradeInsertRow(
                ticker=intent.ticker,
                status="dry_run",
                entry_price_cents=actual_avg,
                quantity=actual_qty,
                cost_basis_usd_cents=actual_cost,
                triggering_match_id=intent.triggering_match_id,
                triggering_intent_json=intent.model_dump_json(),
                risk_decision_id=approved.risk_decision_id,
                approval_id=None,
                is_reentry=isinstance(intent, ReentryIntent),
                prior_trade_id=getattr(intent, "prior_trade_id", None),
                reasoning_text=intent.reasoning_text,
                entered_at=_utcnow_iso(),
                # Phase 3 audit columns from the re-walk.
                cap_binding=intent.cap_binding,
                cap_one_value_cents=intent.cap_one_value_cents,
                cap_two_value_cents=intent.cap_two_value_cents,
                target_avg_fill_price_cents=intent.target_avg_fill_price_cents,
                actual_avg_fill_price_cents=actual_avg,
                slippage_cents=rewalk.slippage_cents,
                entry_fees_cents=rewalk.estimated_fees_cents,
                levels_consumed_json=json.dumps(rewalk.levels_consumed),
            ),
        )
        return ExecutionResult(
            trade_id=trade_id,
            status="filled",
            fill_price_cents=actual_avg,
            fill_quantity=actual_qty,
            notes=(
                f"FOK fill: {actual_qty} contracts at avg {actual_avg}c "
                f"(target avg {target_avg}c, slippage {rewalk.slippage_cents}c, "
                f"fees ${rewalk.estimated_fees_cents/100:.2f})"
            ),
        )

    def _submit_buy_legacy(
        self, intent: TradeIntent | ReentryIntent, approved: RiskApprovedOrder
    ) -> ExecutionResult:
        """Pre-Phase-3 fallback used when ``depth_fn`` isn't configured.
        Simulated fill at top-of-book ask; no FOK gate. Kept so older
        tests can still construct an executor without supplying depth."""
        quote = self._quote_fn(intent.ticker)
        if quote.yes_ask_cents is None:
            return ExecutionResult(
                trade_id=-1,
                status="rejected",
                notes="no ask available at submission time",
            )
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
            notes="dry-run entry (legacy top-of-book fill)",
        )

    def _log_killed(self, *, ticker: str, kind: str, reason: str) -> None:
        """Persist a system_event row + log so the audit trail
        captures every FOK-killed order."""
        insert_system_event(
            self._db,
            event_type=kind,
            severity="warning",
            component="dry_run_executor",
            message=f"FOK killed for {ticker}: {reason}",
            detail={"ticker": ticker, "reason": reason},
        )

    def _submit_stop_loss(
        self, intent: StopLossIntent, approved: RiskApprovedOrder
    ) -> ExecutionResult:
        from trumpbot.execution.fees import calculate_exit_fee_cents

        quote = self._quote_fn(intent.ticker)
        # We sell into the bid. If no bid is available (extremely rare),
        # fall back to the bid the engine observed when generating the
        # intent. Simulated fills only — Phase 4 reads the live book.
        bid = quote.yes_bid_cents if quote.yes_bid_cents is not None else intent.current_bid_cents
        # Phase 4 Part 2.1: exit fees enter the close lifecycle so the
        # disposal_proceeds_cents reflects net cash to the operator.
        exit_fees = calculate_exit_fee_cents(bid, intent.position_quantity)
        proceeds = bid * intent.position_quantity - exit_fees
        realized = proceeds - intent.cost_basis_usd_cents
        close_trade(
            self._db,
            trade_id=intent.trade_id,
            new_status="dry_run_closed_stop",
            exit_price_cents=bid,
            realized_pnl_usd_cents=realized,
            exited_at=_utcnow_iso(),
            exit_fees_cents=exit_fees,
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
