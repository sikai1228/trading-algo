"""Telegram message templates for entry / re-entry / stop-loss approvals.

Pure formatting — no I/O. The same string the user sees in Telegram is
also persisted to ``telegram_approvals.message_text`` so the audit
trail matches what was actually shown.
"""

from __future__ import annotations

from trumpbot.types.intents import (
    ReentryIntent,
    RiskApprovedOrder,
    StopLossIntent,
    TradeIntent,
)


def format_message(approved: RiskApprovedOrder) -> str:
    intent = approved.intent
    if isinstance(intent, StopLossIntent):
        return _format_stop_loss(intent)
    if isinstance(intent, ReentryIntent):
        return _format_reentry(intent, approved)
    return _format_entry(intent, approved)


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def _format_entry(intent: TradeIntent, approved: RiskApprovedOrder) -> str:
    """Phase 3 Part 1 — message includes the cap analysis, walk audit,
    fee estimate, and FOK warning."""
    qty = approved.adjusted_quantity or intent.target_quantity
    avg_fill = intent.target_avg_fill_price_cents
    body = [
        f"Triggered by match #{intent.triggering_match_id}",
        f"Confidence: {intent.confidence_score:.2f}  "
        f"Confirmation weight: {intent.confirmation_weight:.2f}",
        "",
        "Position sizing:",
        f"  Cap one (hard): ${intent.cap_one_value_cents / 100:.2f}",
        f"  Cap two (5% of volume): ${intent.cap_two_value_cents / 100:.2f}",
        f"  Binding: {intent.cap_binding}",
        "",
        f"Order book walk for ${intent.target_size_usd_cents / 100:.2f}:",
        f"  Filled: {qty} contracts at avg {avg_fill}c",
        f"  Slippage: {intent.slippage_cents}c from best ask",
        f"  Fee estimate: ${intent.estimated_fees_cents / 100:.2f}",
        f"  Total cost: ${intent.estimated_total_cost_cents / 100:.2f}",
        "",
        f"Action: BUY YES @ avg ~{avg_fill}c (FOK, target {qty} contracts)",
        "",
        "Reasoning:",
        intent.reasoning_text,
        "",
        "Approve within 3:00 to execute.",
        "If book moves unfavorably between approval and execution,",
        "order will be killed (no trade).",
        "[APPROVE] [REJECT] [DETAILS]",
    ]
    return _wrap(
        header="💰 TRADE PROPOSAL",
        ticker=intent.ticker,
        body=body,
    )


# ---------------------------------------------------------------------------
# Re-entry
# ---------------------------------------------------------------------------


def _format_reentry(intent: ReentryIntent, approved: RiskApprovedOrder) -> str:
    """Phase 3 Part 1 — same walk + cap fields as entry, plus the
    prior-trade audit context."""
    qty = approved.adjusted_quantity or intent.target_quantity
    avg_fill = intent.target_avg_fill_price_cents
    realized = intent.prior_trade_realized_pnl_usd_cents / 100
    body = [
        f"Prior trade #{intent.prior_trade_id} closed via " f"{intent.prior_trade_outcome}",
        f"Prior realized P&L: ${realized:+.2f}",
        "",
        f"Fresh signal: match #{intent.triggering_match_id} "
        f"(confidence {intent.confidence_score:.2f})",
        "",
        "Position sizing:",
        f"  Cap one (hard): ${intent.cap_one_value_cents / 100:.2f}",
        f"  Cap two (5% of volume): ${intent.cap_two_value_cents / 100:.2f}",
        f"  Binding: {intent.cap_binding}",
        "",
        f"Order book walk for ${intent.target_size_usd_cents / 100:.2f}:",
        f"  Filled: {qty} contracts at avg {avg_fill}c",
        f"  Slippage: {intent.slippage_cents}c from best ask",
        f"  Fee estimate: ${intent.estimated_fees_cents / 100:.2f}",
        f"  Total cost: ${intent.estimated_total_cost_cents / 100:.2f}",
        "",
        f"Action: BUY YES @ avg ~{avg_fill}c (FOK, target {qty} contracts)",
        "",
        "Reasoning:",
        intent.reasoning_text,
        "",
        "No timeout — respond when ready.",
        "If book moves unfavorably between approval and execution,",
        "order will be killed (no trade).",
        "[APPROVE] [REJECT] [DETAILS]",
    ]
    return _wrap(
        header="🔄 RE-ENTRY OPPORTUNITY",
        ticker=intent.ticker,
        body=body,
    )


# ---------------------------------------------------------------------------
# Stop-loss
# ---------------------------------------------------------------------------


def _format_stop_loss(intent: StopLossIntent) -> str:
    return _wrap(
        header="⚠️ STOP-LOSS TRIGGER",
        ticker=intent.ticker,
        body=[
            f"Trade #{intent.trade_id}",
            f"Entry: {intent.entry_price_cents}c   "
            f"Current bid: {intent.current_bid_cents}c   "
            f"Drop: {intent.drop_cents}c",
            "",
            f"Position: {intent.position_quantity} contracts",
            f"Cost basis: ${intent.cost_basis_usd_cents / 100:.2f}",
            f"Current value: ${intent.current_value_usd_cents / 100:.2f}",
            f"Unrealized P&L: ${intent.unrealized_pnl_usd_cents / 100:+.2f}",
            "",
            "Reasoning:",
            intent.reasoning_text,
            "",
            "No timeout — respond when ready.",
            "[APPROVE — exit at market] [REJECT — hold]",
        ],
    )


def _wrap(*, header: str, ticker: str, body: list[str]) -> str:
    lines = [header, "", f"Ticker: {ticker}", *body]
    return "\n".join(lines)


__all__ = ["format_message"]
