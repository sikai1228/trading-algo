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
    qty = approved.adjusted_quantity or intent.target_quantity
    cost = qty * intent.target_price_cents
    return _wrap(
        header="💰 TRADE PROPOSAL",
        ticker=intent.ticker,
        body=[
            f"Triggered by match #{intent.triggering_match_id}",
            f"Confidence: {intent.confidence_score:.2f}  "
            f"Confirmation weight: {intent.confirmation_weight:.2f}",
            "",
            f"Action: BUY YES @ {intent.target_price_cents}¢",
            f"Size: {qty} contracts (${cost / 100:.2f})",
            "",
            "Reasoning:",
            intent.reasoning_text,
            "",
            "Approve within 3:00 to execute.",
            "[APPROVE] [REJECT] [DETAILS]",
        ],
    )


# ---------------------------------------------------------------------------
# Re-entry
# ---------------------------------------------------------------------------


def _format_reentry(intent: ReentryIntent, approved: RiskApprovedOrder) -> str:
    qty = approved.adjusted_quantity or intent.target_quantity
    cost = qty * intent.target_price_cents
    realized = intent.prior_trade_realized_pnl_usd_cents / 100
    return _wrap(
        header="🔄 RE-ENTRY OPPORTUNITY",
        ticker=intent.ticker,
        body=[
            f"Prior trade #{intent.prior_trade_id} closed via " f"{intent.prior_trade_outcome}",
            f"Prior realized P&L: ${realized:+.2f}",
            "",
            f"Fresh signal: match #{intent.triggering_match_id} "
            f"(confidence {intent.confidence_score:.2f})",
            "",
            f"Action: BUY YES @ {intent.target_price_cents}¢",
            f"Size: {qty} contracts (${cost / 100:.2f})",
            "",
            "Reasoning:",
            intent.reasoning_text,
            "",
            "No timeout — respond when ready.",
            "[APPROVE] [REJECT] [DETAILS]",
        ],
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
            f"Entry: {intent.entry_price_cents}¢   "
            f"Current bid: {intent.current_bid_cents}¢   "
            f"Drop: {intent.drop_cents}¢",
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
