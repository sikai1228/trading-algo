"""Approval-flow message formatter.

Phase 3 Part 2 made this a thin facade over
:mod:`trumpbot.notifications.templates` so the single-source-of-truth
invariant holds: the actual message text lives in ``templates.py`` and
this module is just the adapter that turns a :class:`RiskApprovedOrder`
into the data dict its template expects.

The grep-test in CI checks that no Telegram-message text exists
outside ``notifications/templates.py``; this module passes the test
because every f-string here builds a *data value*, never the message
itself (the rendered text comes from :func:`render_template`).
"""

from __future__ import annotations

from trumpbot.notifications.templates import render_template
from trumpbot.types.intents import (
    ReentryIntent,
    RiskApprovedOrder,
    StopLossIntent,
    TradeIntent,
)


def format_message(approved: RiskApprovedOrder) -> str:
    """Render the approval-prompt text for a single risk-approved
    order. Routes by intent type to the right template, builds the
    appropriate data dict, returns the rendered string."""
    intent = approved.intent
    if isinstance(intent, StopLossIntent):
        return render_template(
            "trade_proposal_stop_loss",
            _stop_loss_data(intent),
        ).text
    if isinstance(intent, ReentryIntent):
        return render_template(
            "trade_proposal_reentry",
            _reentry_data(intent, approved),
        ).text
    return render_template(
        "trade_proposal_entry",
        _entry_data(intent, approved),
    ).text


# ---------------------------------------------------------------------------
# Data adapters: TradeIntent -> template data dict
# ---------------------------------------------------------------------------


def _entry_data(intent: TradeIntent, approved: RiskApprovedOrder) -> dict[str, object]:
    qty = approved.adjusted_quantity or intent.target_quantity
    return {
        "ticker": intent.ticker,
        "match_id": intent.triggering_match_id,
        "confidence": f"{intent.confidence_score:.2f}",
        **_proposal_body_data(intent, qty),
    }


def _reentry_data(intent: ReentryIntent, approved: RiskApprovedOrder) -> dict[str, object]:
    qty = approved.adjusted_quantity or intent.target_quantity
    realized = intent.prior_trade_realized_pnl_usd_cents / 100
    return {
        "ticker": intent.ticker,
        "match_id": intent.triggering_match_id,
        "confidence": f"{intent.confidence_score:.2f}",
        "prior_trade_id": intent.prior_trade_id,
        "prior_trade_outcome": intent.prior_trade_outcome,
        "prior_realized_dollars": f"${realized:+.2f}",
        **_proposal_body_data(intent, qty),
    }


def _stop_loss_data(intent: StopLossIntent) -> dict[str, object]:
    return {
        "ticker": intent.ticker,
        "trade_id": intent.trade_id,
        "entry_price": intent.entry_price_cents,
        "current_bid": intent.current_bid_cents,
        "drop": intent.drop_cents,
        "quantity": intent.position_quantity,
        "cost_basis_dollars": f"${intent.cost_basis_usd_cents / 100:.2f}",
        "current_value_dollars": f"${intent.current_value_usd_cents / 100:.2f}",
        "unrealized_dollars": f"${intent.unrealized_pnl_usd_cents / 100:+.2f}",
        "reasoning_text": intent.reasoning_text,
    }


def _proposal_body_data(intent: TradeIntent | ReentryIntent, qty: int) -> dict[str, object]:
    """Shared sub-template data for entry / re-entry templates.

    Phase 4 Part 2.6: cap_two now references the live orderbook
    instead of historical volume; the template surfaces both the
    contract count cap_two would allow and the total available
    contracts under the price ceiling.
    """
    # `available_contracts` isn't on the intent — derive it from the
    # walk's levels_consumed plus the cap-two contract count when we
    # can; fall back to "?" when the intent's `levels_consumed` was
    # synthesized in a test fixture (no walk depth recorded).
    available_contracts: int | str
    if intent.levels_consumed:
        available_contracts = sum(q for _p, q in intent.levels_consumed)
    else:
        available_contracts = "?"
    return {
        "cap_one_dollars": f"${intent.cap_one_value_cents / 100:.2f}",
        "cap_two_dollars": f"${intent.cap_two_value_cents / 100:.2f}",
        "cap_two_contracts": intent.cap_two_contracts,
        "available_contracts": available_contracts,
        "cap_binding": intent.cap_binding,
        "effective_cap_dollars": (f"${intent.target_size_usd_cents / 100:.2f}"),
        "quantity": qty,
        "avg_fill": intent.target_avg_fill_price_cents,
        "slippage": intent.slippage_cents,
        "fees_dollars": f"${intent.estimated_fees_cents / 100:.2f}",
        "total_cost_dollars": (f"${intent.estimated_total_cost_cents / 100:.2f}"),
        "reasoning_text": intent.reasoning_text,
    }


__all__ = ["format_message"]
