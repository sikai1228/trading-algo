"""KalshiExecutor — live order placement against Kalshi's REST API.

Phase 4 Part 1.

The interface mirrors :class:`DryRunExecutor` so the daemon swaps in
one or the other based on ``cfg.execution.mode``. Every behavioral
contract from Phase 3 is preserved:

- ``submit`` accepts only :class:`RiskApprovedOrder`. Risk gating is
  unchanged.
- FOK semantics: re-walk the book at submission time; refuse to
  submit if the walk doesn't fill the target quantity at acceptable
  prices.
- Status state machine: every trade row goes through
  ``pending → live → live_closed_*`` (or one of the terminal error /
  killed states defined in migration 007).

Live-mode additions:

1. **Idempotency** — every order carries a UUIDv4 ``client_order_id``
   we generate locally and persist BEFORE the API call. Kalshi treats
   it as a primary key; submitting the same value twice returns the
   original order. If the network dies between request and response,
   reconciliation looks up the order by client_order_id and recovers
   the correct lifecycle state.

2. **Two-phase write**: the trade row is inserted with
   ``status='pending'`` first. Only after Kalshi acks does the row
   move to ``live`` (or one of the error / killed states). On
   transient failure the row stays ``pending`` and reconciliation
   takes over.

3. **Error categorization** via :mod:`trumpbot.kalshi.errors`. Each
   exception bucket maps to a specific terminal status and
   notification template; ``StateError`` (insufficient funds, etc.)
   additionally HALTS the bot.

4. **No silent retries on POST**. The Kalshi client is configured
   with ``retry_on_transient=False`` for ``place_order`` so a 5xx or
   socket timeout surfaces immediately — we'd rather fail loud and
   reconcile than risk a duplicate.

The executor does NOT process settlements directly — that runs as a
separate daemon loop polling ``GET /portfolio/settlements`` every
five minutes. See :mod:`trumpbot.account.reconcile` for the
settlement detector.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    TradeInsertRow,
    close_trade,
    get_open_trade_for_ticker,
    insert_system_event,
    insert_trade,
    list_open_live_trades,
    update_trade_marks,
    update_trade_status_by_client_order_id,
)
from trumpbot.execution.dry_run import OrderbookFn, Quote
from trumpbot.execution.fees import calculate_entry_fee_cents
from trumpbot.execution.slippage import OrderbookWalkResult, walk_orderbook_for_buy
from trumpbot.kalshi.client import KalshiClient
from trumpbot.kalshi.errors import categorize_order_error
from trumpbot.kalshi.exceptions import KalshiError
from trumpbot.kalshi.schemas import KalshiOrder
from trumpbot.types.intents import (
    ExecutionResult,
    ReentryIntent,
    RiskApprovedOrder,
    StopLossIntent,
    TradeIntent,
)
from trumpbot.utils.logging import get_logger

log = get_logger(__name__)


# Reuse the depth-fn type from the dry-run executor.
DepthFnSync = Callable[[str], list[tuple[int, int]] | None]


def _utcnow_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _new_client_order_id() -> str:
    """Return a fresh UUIDv4 string. Public so tests can monkeypatch."""
    return str(uuid.uuid4())


@dataclass(frozen=True)
class HaltCallback:
    """Callable wrapper used by :class:`KalshiExecutor` when a
    :class:`StateError` (insufficient funds, market closed, etc.) is
    surfaced. Default implementation only logs; daemon wires a real
    halt callback that flips ``system_state.halt_flag`` and notifies
    the user."""

    callback: Callable[[str], None] | None = None

    def __call__(self, reason: str) -> None:
        if self.callback is not None:
            self.callback(reason)


class KalshiExecutor:
    """Phase 4 live-trading executor."""

    def __init__(
        self,
        *,
        db: Database,
        kalshi_client: KalshiClient,
        orderbook_fn: OrderbookFn,
        depth_fn: DepthFnSync,
        halt_callback: HaltCallback | None = None,
        client_order_id_factory: Callable[[], str] = _new_client_order_id,
    ) -> None:
        self._db = db
        self._client = kalshi_client
        self._quote_fn = orderbook_fn
        self._depth_fn = depth_fn
        self._halt = halt_callback or HaltCallback()
        self._mint_order_id = client_order_id_factory

    # -- main API -----------------------------------------------------

    async def submit(self, approved: RiskApprovedOrder) -> ExecutionResult:
        intent = approved.intent
        if isinstance(intent, StopLossIntent):
            return await self._submit_stop_loss(intent, approved)
        return await self._submit_buy(intent, approved)

    def update_position_marks(self) -> int:
        """Mirror of :meth:`DryRunExecutor.update_position_marks`. Walks
        every live position, refreshes ``unrealized_pnl_usd_cents``
        from the WS book. The settlement detector + stop-loss loop
        rely on this being current."""
        updated = 0
        for row in list_open_live_trades(self._db):
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

    def close_resolved(
        self,
        *,
        ticker: str,
        resolution: str,
    ) -> ExecutionResult | None:
        """Called by the settlement detector when Kalshi reports the
        market resolved. Closes the open live position at 100c (YES)
        or 0c (NO) and uses the live-specific terminal status."""
        row = get_open_trade_for_ticker(self._db, ticker)
        if row is None:
            return None
        if resolution == "settled_yes":
            payoff_cents = 100
            new_status = "live_closed_resolved_yes"
        elif resolution == "settled_no":
            payoff_cents = 0
            new_status = "live_closed_resolved_no"
        else:
            log.warning("close_resolved_unexpected_resolution", resolution=resolution)
            return None
        proceeds_cents = payoff_cents * row["quantity"]
        realized = proceeds_cents - row["cost_basis_usd_cents"]
        close_trade(
            self._db,
            trade_id=row["id"],
            new_status=new_status,
            exit_price_cents=payoff_cents,
            realized_pnl_usd_cents=realized,
            exited_at=_utcnow_iso(),
            exit_fees_cents=0,  # Kalshi fee formula returns 0 at p=0 / p=100
        )
        return ExecutionResult(
            trade_id=row["id"],
            status="filled",
            fill_price_cents=payoff_cents,
            fill_quantity=row["quantity"],
            realized_pnl_usd_cents=realized,
            notes=f"live market resolved {resolution}",
        )

    # -- internals: buy ------------------------------------------------

    async def _submit_buy(
        self,
        intent: TradeIntent | ReentryIntent,
        approved: RiskApprovedOrder,
    ) -> ExecutionResult:
        """Live FOK buy. Re-walks the book; if the walk passes, mints
        a UUID, persists ``status='pending'``, submits to Kalshi, then
        promotes to ``live`` on success."""
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
        rewalk = walk_orderbook_for_buy(
            levels,
            target_dollars_cents=target_budget,
            max_price_cents=intent.target_price_cents,
            fee_calculator=calculate_entry_fee_cents,
        )

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

        # Re-walk passed FOK gate. Persist the trade row as ``pending``
        # BEFORE the API call so reconciliation has something to look up
        # if the network dies mid-submit.
        client_order_id = self._mint_order_id()
        actual_qty = rewalk.filled_quantity
        actual_avg = rewalk.average_fill_price_cents
        actual_cost = rewalk.total_cost_cents
        max_price = rewalk.max_price_reached_cents or actual_avg
        trade_id = insert_trade(
            self._db,
            TradeInsertRow(
                ticker=intent.ticker,
                status="pending",
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
                cap_binding=intent.cap_binding,
                cap_one_value_cents=intent.cap_one_value_cents,
                cap_two_value_cents=intent.cap_two_value_cents,
                target_avg_fill_price_cents=intent.target_avg_fill_price_cents,
                actual_avg_fill_price_cents=actual_avg,
                slippage_cents=rewalk.slippage_cents,
                entry_fees_cents=rewalk.estimated_fees_cents,
                levels_consumed_json=json.dumps(rewalk.levels_consumed),
                client_order_id=client_order_id,
            ),
        )
        log.info(
            "kalshi_executor_submitting",
            trade_id=trade_id,
            ticker=intent.ticker,
            client_order_id=client_order_id,
            target_quantity=actual_qty,
            target_avg_fill_cents=actual_avg,
            max_price_cents=max_price,
        )

        # Submit to Kalshi. Use the highest level we'd consume as the
        # FOK limit price — Kalshi's matching engine fills at or below
        # this. The walk already verified the average is acceptable.
        try:
            order = await self._client.place_order(
                ticker=intent.ticker,
                client_order_id=client_order_id,
                action="buy",
                side="yes",
                count=actual_qty,
                order_type="limit",
                yes_price=max_price,
                time_in_force="FOK",
            )
        except Exception as exc:
            return self._handle_order_error(
                trade_id=trade_id,
                client_order_id=client_order_id,
                ticker=intent.ticker,
                actual_qty=actual_qty,
                actual_avg=actual_avg,
                actual_cost=actual_cost,
                exc=exc,
            )

        return self._finalize_buy_success(
            trade_id=trade_id,
            client_order_id=client_order_id,
            order=order,
            walk=rewalk,
            requested_qty=actual_qty,
        )

    def _finalize_buy_success(
        self,
        *,
        trade_id: int,
        client_order_id: str,
        order: KalshiOrder,
        walk: OrderbookWalkResult,
        requested_qty: int,
    ) -> ExecutionResult:
        """Promote a pending row to ``live`` (full fill) or
        ``killed_no_fill`` (FOK rejected by Kalshi).

        Pre-live fix #11 (Phase 4 Part 2.2): the trade row's
        ``entry_price_cents`` / ``quantity`` / ``cost_basis_usd_cents``
        prefer the values Kalshi REPORTED in its ack (the canonical
        fill record). Only fall back to the local re-walk numbers
        when Kalshi omits a field. Logs ``using_rewalk_fallback``
        as a system_event whenever any field falls back so it's
        visible in audit.
        """
        # Kalshi's order shape: count is requested, remaining_count
        # tells us how much is still resting. For FOK this is either
        # 0 (fully filled) or count (entirely killed).
        kalshi_filled: int | None = order.filled_count
        if kalshi_filled is None and order.count is not None:
            kalshi_filled = (order.count or 0) - (order.remaining_count or 0)
        # Prefer Kalshi-reported filled count; fall back to re-walk's
        # count when Kalshi gave us nothing usable.
        if kalshi_filled is None or kalshi_filled <= 0:
            filled = walk.filled_quantity
            filled_source = "rewalk"
        else:
            filled = kalshi_filled
            filled_source = "kalshi"

        if filled <= 0 or order.status not in {"executed", "filled"}:
            update_trade_status_by_client_order_id(
                self._db,
                client_order_id=client_order_id,
                new_status="killed_no_fill",
                kalshi_order_id=order.order_id,
            )
            self._log_killed(
                ticker=order.ticker or "",
                kind="kalshi_fok_killed",
                reason=(
                    f"Kalshi reported status={order.status} filled={filled}; "
                    "treating as FOK kill"
                ),
            )
            return ExecutionResult(
                trade_id=trade_id,
                status="rejected",
                notes=f"Kalshi FOK kill: status={order.status}",
            )

        # Prefer Kalshi-reported avg fill price; fall back to re-walk
        # only when Kalshi omits it.
        if order.avg_fill_price is not None and order.avg_fill_price > 0:
            actual_avg = order.avg_fill_price
            avg_source = "kalshi"
        else:
            actual_avg = walk.average_fill_price_cents
            avg_source = "rewalk"
        actual_cost = actual_avg * filled

        # Surface the source of each field in a system_event when ANY
        # falls back to the re-walk. Per Open Issue #11 — gives the
        # operator visibility into divergence between Kalshi truth and
        # our reproduction.
        if filled_source == "rewalk" or avg_source == "rewalk":
            insert_system_event(
                self._db,
                event_type="using_rewalk_fallback",
                severity="info",
                component="kalshi_executor",
                message=(
                    "Kalshi omitted fill details; using re-walk values "
                    f"for trade {trade_id} (filled_source={filled_source}, "
                    f"avg_source={avg_source})"
                ),
                detail={
                    "trade_id": trade_id,
                    "client_order_id": client_order_id,
                    "kalshi_order_id": order.order_id,
                    "filled_source": filled_source,
                    "avg_source": avg_source,
                    "kalshi_filled_count": order.filled_count,
                    "kalshi_avg_fill_price": order.avg_fill_price,
                    "rewalk_filled_quantity": walk.filled_quantity,
                    "rewalk_avg_fill_cents": walk.average_fill_price_cents,
                },
            )

        update_trade_status_by_client_order_id(
            self._db,
            client_order_id=client_order_id,
            new_status="live",
            kalshi_order_id=order.order_id,
            entry_price_cents=actual_avg,
            quantity=filled,
            cost_basis_usd_cents=actual_cost,
            actual_avg_fill_price_cents=actual_avg,
        )
        log.info(
            "kalshi_executor_filled",
            trade_id=trade_id,
            client_order_id=client_order_id,
            kalshi_order_id=order.order_id,
            filled_quantity=filled,
            filled_source=filled_source,
            actual_avg_fill_cents=actual_avg,
            avg_source=avg_source,
        )
        return ExecutionResult(
            trade_id=trade_id,
            status="filled",
            fill_price_cents=actual_avg,
            fill_quantity=filled,
            notes=(
                f"live fill: {filled} contracts at avg {actual_avg}c "
                f"(target avg {walk.average_fill_price_cents}c, "
                f"slippage {walk.slippage_cents}c, "
                f"qty_source={filled_source}, avg_source={avg_source})"
            ),
        )

    def _handle_order_error(
        self,
        *,
        trade_id: int,
        client_order_id: str,
        ticker: str,
        actual_qty: int,
        actual_avg: int,
        actual_cost: int,
        exc: BaseException,
    ) -> ExecutionResult:
        """Map a place_order exception to the right terminal status,
        log a system_event, and (for StateError) trigger the halt
        callback. Returns an ExecutionResult the gate can dispatch on."""
        category = categorize_order_error(exc)
        log.error(
            "kalshi_executor_order_error",
            trade_id=trade_id,
            ticker=ticker,
            client_order_id=client_order_id,
            category=category.name,
            detail=category.detail,
        )

        if category.name == "duplicate_client_order":
            # The original submission landed; we lost only the response.
            # Mark the row as live optimistically — startup
            # reconciliation will harden the data on next restart.
            update_trade_status_by_client_order_id(
                self._db,
                client_order_id=client_order_id,
                new_status="live",
            )
            insert_system_event(
                self._db,
                event_type="kalshi_duplicate_client_order",
                severity="info",
                component="kalshi_executor",
                message=(
                    f"Kalshi reported duplicate client_order_id={client_order_id}; "
                    "treating as already-landed."
                ),
                detail={"client_order_id": client_order_id, "ticker": ticker},
            )
            return ExecutionResult(
                trade_id=trade_id,
                status="filled",
                fill_price_cents=actual_avg,
                fill_quantity=actual_qty,
                notes="duplicate client_order_id; original submission landed",
            )

        # Persist the categorized terminal status.
        if category.trade_status is not None:
            update_trade_status_by_client_order_id(
                self._db,
                client_order_id=client_order_id,
                new_status=category.trade_status,
            )

        insert_system_event(
            self._db,
            event_type=f"kalshi_order_{category.name}",
            severity="warning" if category.name == "transient" else "error",
            component="kalshi_executor",
            message=f"order {category.name} for {ticker}: {category.detail[:240]}",
            detail={
                "client_order_id": client_order_id,
                "ticker": ticker,
                "category": category.name,
                "trade_id": trade_id,
            },
        )

        if category.should_halt:
            self._halt(category.detail)

        return ExecutionResult(
            trade_id=trade_id,
            status="rejected",
            notes=f"{category.name}: {category.detail[:200]}",
        )

    # -- internals: stop-loss ------------------------------------------

    async def _submit_stop_loss(
        self,
        intent: StopLossIntent,
        approved: RiskApprovedOrder,
    ) -> ExecutionResult:
        """Live stop-loss exit. Sells the position into the bid side
        via a Kalshi sell-yes FOK order."""
        quote = self._quote_fn(intent.ticker)
        bid = quote.yes_bid_cents if quote.yes_bid_cents is not None else intent.current_bid_cents
        client_order_id = self._mint_order_id()

        # We do NOT insert a new trade row for stop-loss exits; we
        # update the existing row's status. Persist the client_order_id
        # on the existing row so reconciliation can find it.
        # (close_trade overwrites status; reconciliation handles drift.)
        try:
            order = await self._client.place_order(
                ticker=intent.ticker,
                client_order_id=client_order_id,
                action="sell",
                side="yes",
                count=intent.position_quantity,
                order_type="limit",
                yes_price=bid,
                time_in_force="FOK",
            )
        except KalshiError as exc:
            category = categorize_order_error(exc)
            log.error(
                "kalshi_executor_stop_loss_error",
                trade_id=intent.trade_id,
                ticker=intent.ticker,
                category=category.name,
                detail=category.detail,
            )
            insert_system_event(
                self._db,
                event_type=f"kalshi_stop_loss_{category.name}",
                severity="error",
                component="kalshi_executor",
                message=f"stop-loss {category.name} for {intent.ticker}: {category.detail[:240]}",
                detail={
                    "client_order_id": client_order_id,
                    "ticker": intent.ticker,
                    "trade_id": intent.trade_id,
                },
            )
            if category.should_halt:
                self._halt(category.detail)
            return ExecutionResult(
                trade_id=intent.trade_id,
                status="rejected",
                notes=f"stop-loss {category.name}: {category.detail[:200]}",
            )

        # Pre-live fix #11: prefer Kalshi-reported filled count + avg
        # fill price; fall back to derived/bid only when Kalshi omits
        # them. Log a system_event when any field falls back.
        kalshi_filled: int | None = order.filled_count
        if kalshi_filled is None and order.count is not None:
            kalshi_filled = (order.count or 0) - (order.remaining_count or 0)
        if kalshi_filled is None or kalshi_filled <= 0:
            filled = intent.position_quantity
            filled_source = "intent"
        else:
            filled = kalshi_filled
            filled_source = "kalshi"

        if filled <= 0:
            self._log_killed(
                ticker=intent.ticker,
                kind="kalshi_stop_loss_fok_killed",
                reason=f"Kalshi stop-loss filled 0; status={order.status}",
            )
            return ExecutionResult(
                trade_id=intent.trade_id,
                status="rejected",
                notes=f"stop-loss FOK kill: status={order.status}",
            )

        from trumpbot.execution.fees import calculate_exit_fee_cents

        if order.avg_fill_price is not None and order.avg_fill_price > 0:
            actual_exit_price = order.avg_fill_price
            exit_price_source = "kalshi"
        else:
            actual_exit_price = bid
            exit_price_source = "bid_fallback"

        if filled_source != "kalshi" or exit_price_source != "kalshi":
            insert_system_event(
                self._db,
                event_type="using_rewalk_fallback",
                severity="info",
                component="kalshi_executor",
                message=(
                    "Kalshi omitted stop-loss fill details; using fallback "
                    f"values for trade {intent.trade_id} "
                    f"(filled_source={filled_source}, "
                    f"exit_price_source={exit_price_source})"
                ),
                detail={
                    "trade_id": intent.trade_id,
                    "client_order_id": client_order_id,
                    "kalshi_order_id": order.order_id,
                    "filled_source": filled_source,
                    "exit_price_source": exit_price_source,
                    "kalshi_filled_count": order.filled_count,
                    "kalshi_avg_fill_price": order.avg_fill_price,
                    "intent_quantity": intent.position_quantity,
                    "best_bid": bid,
                },
            )

        # Phase 4 Part 2.1: track exit fee on the close so the
        # disposal_proceeds_cents reflects net proceeds.
        exit_fees = calculate_exit_fee_cents(actual_exit_price, filled)
        proceeds = actual_exit_price * filled - exit_fees
        realized = proceeds - intent.cost_basis_usd_cents
        close_trade(
            self._db,
            trade_id=intent.trade_id,
            new_status="live_closed_stop",
            exit_price_cents=actual_exit_price,
            realized_pnl_usd_cents=realized,
            exited_at=_utcnow_iso(),
            exit_fees_cents=exit_fees,
        )
        log.info(
            "kalshi_executor_stop_loss_filled",
            trade_id=intent.trade_id,
            client_order_id=client_order_id,
            kalshi_order_id=order.order_id,
            filled_quantity=filled,
            exit_price_cents=actual_exit_price,
            realized_pnl_cents=realized,
        )
        return ExecutionResult(
            trade_id=intent.trade_id,
            status="filled",
            fill_price_cents=actual_exit_price,
            fill_quantity=filled,
            realized_pnl_usd_cents=realized,
            notes=f"live stop-loss exit at {actual_exit_price}c",
        )

    # -- helpers -------------------------------------------------------

    def _log_killed(self, *, ticker: str, kind: str, reason: str) -> None:
        insert_system_event(
            self._db,
            event_type=kind,
            severity="warning",
            component="kalshi_executor",
            message=f"FOK killed for {ticker}: {reason}",
            detail={"ticker": ticker, "reason": reason},
        )


__all__ = ["HaltCallback", "KalshiExecutor", "Quote"]
