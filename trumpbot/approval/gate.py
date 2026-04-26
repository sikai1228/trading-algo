"""ApprovalGate — sits between RiskManager and Executor.

Phase 2 always runs in human-approval mode (auto-mode is wired in the
architecture but not enabled). Asks the user via Telegram to approve
each entry / re-entry / stop-loss intent, awaits the response within a
per-intent-type timeout, returns the decision.

Entry timeout: 180 s (configurable). Stop-loss / re-entry: no timeout
per the locked strategy rules.

Phase 3 Part 1 added an FOK pre-check: when the user approves, the
gate re-walks the live order book and **kills** the trade if the
rewalk shows materially-different numbers (>5 c avg-fill drift, OR
>20 % qty drift). On kill the gate marks the approval row
``decision='rejected'`` with ``decision_source='timeout'`` and
records a ``fok_killed_book_moved`` system event so the audit trail
captures the reason. The user effectively approves "trade this signal
under current rules", not "trade these specific contracts".

The gate does not talk to Telegram directly — it delegates to a
:class:`TelegramApprovalBot` (or a test double). This keeps the gate's
core logic pure-async and makes mocking trivial.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from trumpbot.approval.message_templates import format_message
from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    ShadowSnapshot,
    insert_shadow_decision_at_send,
    insert_system_event,
    insert_telegram_approval,
    update_shadow_decision_at_decision,
    update_telegram_approval,
)
from trumpbot.execution.fees import calculate_entry_fee_cents
from trumpbot.execution.slippage import walk_orderbook_for_buy
from trumpbot.types.intents import (
    ApprovalDecision,
    ReentryIntent,
    RiskApprovedOrder,
    StopLossIntent,
    TradeIntent,
)
from trumpbot.utils.logging import get_logger

log = get_logger(__name__)


# FOK tolerance for the re-walk on user approval. Chosen per the
# Phase 3 Part 1 spec: >5 c avg drift OR >20 % qty drift triggers
# an automatic kill.
FOK_AVG_DRIFT_TOLERANCE_CENTS = 5
FOK_QTY_DRIFT_TOLERANCE_PCT = 0.20


# Type alias: depth callable the gate uses for the FOK re-walk.
# Returns the unified yes-ask side of the live book (NO bids inverted),
# or ``None`` when the book is unavailable.
DepthFn = Callable[[str], Sequence[tuple[int, int]] | None]


APPROVAL_MODE: str = "human"
"""HARDCODED v1 approval mode. Auto-approve is not reachable through
config — it can only be enabled by editing this constant. The
``shadow_decisions`` table (Phase 4 Part 1) collects empirical
evidence to inform a future graduation."""


@dataclass(frozen=True)
class ApprovalGateConfig:
    entry_timeout_sec: int = 180
    stop_loss_timeout_sec: int | None = None
    reentry_timeout_sec: int | None = None

    @property
    def mode(self) -> str:
        """Always returns the module-level ``APPROVAL_MODE`` constant.
        Kept as a property for backwards compatibility with any
        snapshots / tests that read ``cfg.mode`` directly."""
        return APPROVAL_MODE


class ApprovalRequester:
    """Protocol-style hook for the actual Telegram I/O.

    Real implementation: :class:`TelegramApprovalBot`. Tests pass a
    stub that resolves the future themselves.
    """

    async def send_request(self, *, intent_id: str, intent_type: str, message_text: str) -> int:
        raise NotImplementedError

    async def await_response(self, *, intent_id: str, timeout_sec: int | None) -> tuple[str, str]:
        """Return (decision, decision_source) where decision is one of
        ``approved`` / ``rejected`` / ``expired`` and decision_source
        is one of the values defined on :class:`ApprovalDecision`."""
        raise NotImplementedError


class ApprovalGate:
    def __init__(
        self,
        *,
        db: Database,
        config: ApprovalGateConfig,
        requester: ApprovalRequester,
        depth_fn: DepthFn | None = None,
    ) -> None:
        self._db = db
        self._cfg = config
        self._requester = requester
        self._depth_fn = depth_fn
        """Optional. When supplied, the gate re-walks the live book
        on user approval and kills trades whose re-walk drifts
        materially from the engine's targets (Phase 3 Part 1 FOK).
        Stop-loss intents skip this check — they don't have a walk
        to compare against."""

    async def request_approval(self, approved: RiskApprovedOrder) -> ApprovalDecision:
        intent = approved.intent
        timeout_sec = self._timeout_for(approved)
        message_text = format_message(approved)
        expires_at = (
            (datetime.now(UTC) + timedelta(seconds=timeout_sec)).isoformat()
            if timeout_sec
            else None
        )
        chat_id = getattr(self._requester, "chat_id", None)
        record_id = insert_telegram_approval(
            self._db,
            intent_type=intent.intent_type,
            intent_json=intent.model_dump_json(),
            message_text=message_text,
            chat_id=chat_id,
            expires_at=expires_at,
        )

        # ---- Phase 4 Part 1: shadow capture at send time ----
        # Take a deterministic snapshot of the orderbook RIGHT BEFORE we
        # send the proposal. Pairs with a decision-time snapshot below
        # to back the /shadow_report query. Stop-loss intents are
        # excluded — they're emergency exits, not auto-approve
        # candidates, so the shadow comparison doesn't apply.
        send_at_iso = datetime.now(UTC).isoformat()
        if isinstance(intent, TradeIntent | ReentryIntent):
            self._capture_shadow_at_send(intent, send_at_iso)

        try:
            telegram_message_id = await self._requester.send_request(
                intent_id=intent.intent_id,
                intent_type=intent.intent_type,
                message_text=message_text,
            )
        except Exception as exc:
            log.error("approval_send_failed", intent_id=intent.intent_id, error=repr(exc))
            update_telegram_approval(
                self._db,
                approval_id=record_id,
                decision="expired",
                decision_source="timeout",
            )
            return ApprovalDecision(
                intent_id=intent.intent_id,
                decision="expired",
                decided_at=datetime.now(UTC),
                decision_source="timeout",
                rejected_reason=f"send_failed: {exc!r}",
                approval_record_id=record_id,
            )

        try:
            decision, source = await self._requester.await_response(
                intent_id=intent.intent_id, timeout_sec=timeout_sec
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("approval_await_failed", intent_id=intent.intent_id, error=repr(exc))
            decision, source = "expired", "timeout"

        # Phase 3 Part 1: on user approval of an entry / re-entry,
        # re-walk the live book and downgrade to "rejected" if the
        # numbers have drifted past the FOK tolerance. Stop-loss
        # intents skip this — they don't have a walk to compare.
        if (
            decision == "approved"
            and isinstance(intent, TradeIntent | ReentryIntent)
            and self._depth_fn is not None
        ):
            kill_reason = self._fok_kill_reason(intent)
            if kill_reason is not None:
                log.warning(
                    "fok_killed_book_moved",
                    intent_id=intent.intent_id,
                    reason=kill_reason,
                )
                insert_system_event(
                    self._db,
                    event_type="fok_killed_book_moved",
                    severity="warning",
                    component="approval_gate",
                    message=(
                        f"Order killed: orderbook moved between approval "
                        f"and submission. {kill_reason}. Re-trigger "
                        f"requires fresh news signal."
                    ),
                    detail={
                        "intent_id": intent.intent_id,
                        "ticker": intent.ticker,
                        "reason": kill_reason,
                    },
                )
                decision = "rejected"
                # source stays as 'telegram_button' — the user did
                # approve; the kill is a downstream automatic action.

        update_telegram_approval(
            self._db,
            approval_id=record_id,
            decision=decision,
            decision_source=source,
            telegram_message_id=telegram_message_id,
        )

        # ---- Phase 4 Part 1: shadow capture at decision time ----
        if isinstance(intent, TradeIntent | ReentryIntent):
            self._capture_shadow_at_decision(intent, decision)

        return ApprovalDecision(
            intent_id=intent.intent_id,
            decision=decision,  # type: ignore[arg-type]
            decided_at=datetime.now(UTC),
            decision_source=source,  # type: ignore[arg-type]
            approval_record_id=record_id,
        )

    def _capture_shadow_at_send(
        self,
        intent: TradeIntent | ReentryIntent,
        sent_at_iso: str,
    ) -> None:
        """Persist the orderbook snapshot at message-send time. The
        decision-time half is filled in by
        :meth:`_capture_shadow_at_decision` once the user responds.
        Logs and swallows errors — shadow tracking is best-effort and
        must never block an approval flow."""
        try:
            snapshot = self._build_shadow_snapshot(intent)
        except Exception as exc:
            log.warning(
                "shadow_snapshot_at_send_failed",
                intent_id=intent.intent_id,
                error=repr(exc),
            )
            return
        if snapshot is None:
            return
        try:
            insert_shadow_decision_at_send(
                self._db,
                intent_id=intent.intent_id,
                intent_type=intent.intent_type,
                ticker=intent.ticker,
                sent_at_iso=sent_at_iso,
                snapshot=snapshot,
            )
        except Exception as exc:
            log.warning(
                "shadow_insert_at_send_failed",
                intent_id=intent.intent_id,
                error=repr(exc),
            )

    def _capture_shadow_at_decision(
        self,
        intent: TradeIntent | ReentryIntent,
        decision: str,
    ) -> None:
        """Update the shadow row with the decision-time snapshot +
        derived diff fields. Best-effort; failures are logged."""
        try:
            snapshot = self._build_shadow_snapshot(intent)
        except Exception as exc:
            log.warning(
                "shadow_snapshot_at_decision_failed",
                intent_id=intent.intent_id,
                error=repr(exc),
            )
            snapshot = None
        try:
            update_shadow_decision_at_decision(
                self._db,
                intent_id=intent.intent_id,
                decision_made_at_iso=datetime.now(UTC).isoformat(),
                human_decision=decision,
                snapshot=snapshot,
            )
        except Exception as exc:
            log.warning(
                "shadow_update_at_decision_failed",
                intent_id=intent.intent_id,
                error=repr(exc),
            )

    def _build_shadow_snapshot(self, intent: TradeIntent | ReentryIntent) -> ShadowSnapshot | None:
        """Run the same walk the executor would, against the live book.
        Returns ``None`` when the depth_fn isn't configured or the book
        is empty — the caller skips the snapshot in that case."""
        if self._depth_fn is None:
            return None
        levels = self._depth_fn(intent.ticker)
        if not levels:
            return None
        rewalk = walk_orderbook_for_buy(
            levels,
            target_dollars_cents=intent.target_size_usd_cents,
            max_price_cents=intent.target_price_cents,
            fee_calculator=calculate_entry_fee_cents,
        )
        # The current best ask = the lowest price level present.
        best_ask = min((p for p, _q in levels), default=0)
        return ShadowSnapshot(
            yes_ask_cents=int(best_ask),
            avg_fill_cents=int(rewalk.average_fill_price_cents),
            filled_quantity=int(rewalk.filled_quantity),
            estimated_cost_cents=int(rewalk.total_cost_cents),
            orderbook_json=json.dumps(list(rewalk.levels_consumed)),
        )

    def _fok_kill_reason(self, intent: TradeIntent | ReentryIntent) -> str | None:
        """Return a human-readable reason string if the live re-walk
        differs materially from the engine's targets. ``None`` means
        the trade is OK to send to the executor.

        Tolerance: avg-fill drift > 5 c OR qty drift > 20 %.
        """
        assert self._depth_fn is not None  # guarded by caller
        levels = self._depth_fn(intent.ticker)
        if not levels:
            return "no order-book depth available at approval time"
        rewalk = walk_orderbook_for_buy(
            levels,
            target_dollars_cents=intent.target_size_usd_cents,
            max_price_cents=intent.target_price_cents,
            fee_calculator=calculate_entry_fee_cents,
        )
        target_qty = intent.target_quantity
        target_avg = intent.target_avg_fill_price_cents
        new_qty = rewalk.filled_quantity
        new_avg = rewalk.average_fill_price_cents

        if target_avg > 0:
            avg_drift = abs(new_avg - target_avg)
            if avg_drift > FOK_AVG_DRIFT_TOLERANCE_CENTS:
                return (
                    f"Original avg fill: {target_avg}c for "
                    f"{target_qty} contracts. New: {new_avg}c "
                    f"for {new_qty}"
                )
        if target_qty > 0:
            qty_drift_pct = abs(new_qty - target_qty) / target_qty
            if qty_drift_pct > FOK_QTY_DRIFT_TOLERANCE_PCT:
                return (
                    f"Original avg fill: {target_avg}c for "
                    f"{target_qty} contracts. New: {new_avg}c "
                    f"for {new_qty}"
                )
        return None

    def _timeout_for(self, approved: RiskApprovedOrder) -> int | None:
        if isinstance(approved.intent, StopLossIntent):
            return self._cfg.stop_loss_timeout_sec
        if approved.intent.intent_type == "reentry":
            return self._cfg.reentry_timeout_sec
        return self._cfg.entry_timeout_sec


__all__ = ["ApprovalGate", "ApprovalGateConfig", "ApprovalRequester"]
