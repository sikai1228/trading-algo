"""Approval-flow message formatter.

Phase 3 Part 2 made this a thin facade over
:mod:`trumpbot.notifications.templates` so the single-source-of-truth
invariant holds: the actual message text lives in ``templates.py`` and
this module is just the adapter that turns a :class:`RiskApprovedOrder`
into the data dict its template expects.

Phase 4 Part 2.11 expanded the data dicts with the standardized
trade-notification fields (timestamp, market subject, P&L breakdown,
article context). All formatting / math goes through
:mod:`trumpbot.notifications.trade_render`.

The grep-test in CI checks that no Telegram-message text exists
outside ``notifications/templates.py``; this module passes the test
because every f-string here builds a *data value*, never the message
itself (the rendered text comes from :func:`render_template`).
"""

from __future__ import annotations

from trumpbot.notifications.templates import render_template
from trumpbot.notifications.trade_render import (
    article_link_markdown,
    compute_potential_loss_cents,
    compute_settlement_pnl,
    dollars,
    dollars_signed,
    format_et_short,
    humanize_age_since,
    now_et_long,
    percent_from_bps,
    render_key_quote,
)
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
    base = _proposal_body_data(intent, qty)
    return {
        "intent_id_short": intent.intent_id.split("-")[0],
        **base,
    }


def _reentry_data(intent: ReentryIntent, approved: RiskApprovedOrder) -> dict[str, object]:
    qty = approved.adjusted_quantity or intent.target_quantity
    realized = intent.prior_trade_realized_pnl_usd_cents / 100
    base = _proposal_body_data(intent, qty)
    return {
        "intent_id_short": intent.intent_id.split("-")[0],
        "prior_trade_id": intent.prior_trade_id,
        "prior_trade_outcome": intent.prior_trade_outcome,
        "prior_realized_dollars": f"${realized:+.2f}",
        # Phase 4 Part 2.11 — best-effort age. We don't carry the
        # prior-trade close timestamp on the intent; show a placeholder
        # the operator can replace with /history if they need exact
        # timing.
        "prior_closed_age": "unknown",
        **base,
    }


def _stop_loss_data(intent: StopLossIntent) -> dict[str, object]:
    return {
        "ticker": intent.ticker,
        "trade_id": intent.trade_id,
        "timestamp_et": now_et_long(),
        # Phase 4 Part 2.11 — synthesized fields. The StopLossIntent
        # doesn't carry the originating market metadata (subject,
        # full title) or recent-news context; surface placeholders
        # so the template renders cleanly.
        "market_title": f"(see {intent.ticker})",
        "subject_full_name": "—",
        "entry_price": intent.entry_price_cents,
        "current_bid": intent.current_bid_cents,
        "drop": intent.drop_cents,
        "quantity": intent.position_quantity,
        "cost_basis_dollars": f"${intent.cost_basis_usd_cents / 100:.2f}",
        "current_value_dollars": f"${intent.current_value_usd_cents / 100:.2f}",
        "unrealized_dollars": f"${intent.unrealized_pnl_usd_cents / 100:+.2f}",
        "time_held": "(see /why)",
        "news_context": "(no recent matches indexed)",
        "reasoning_text": intent.reasoning_text,
    }


def _proposal_body_data(intent: TradeIntent | ReentryIntent, qty: int) -> dict[str, object]:
    """Phase 4 Part 2.11 — build the standardized info dict the entry
    + re-entry templates share. All cents math; format helpers handle
    dollar conversion."""
    avg_fill = intent.target_avg_fill_price_cents or 0
    cost_basis_cents = qty * avg_fill if avg_fill > 0 else intent.target_size_usd_cents
    entry_fees_cents = intent.estimated_fees_cents
    total_cost_cents = cost_basis_cents + entry_fees_cents
    best_ask_cents = max(0, avg_fill - intent.slippage_cents) if avg_fill > 0 else 0

    settlement_cents, _exit_fees, profit_cents, roi_bps = compute_settlement_pnl(
        quantity=qty,
        cost_basis_cents=cost_basis_cents,
        entry_fees_cents=entry_fees_cents,
    )
    # Worst-case potential-loss estimate when we don't have a live
    # bid book at template render time.
    potential_loss_cents = compute_potential_loss_cents(
        quantity=qty,
        cost_basis_cents=cost_basis_cents,
        entry_fees_cents=entry_fees_cents,
        entry_price_cents=avg_fill,
        yes_bid_levels=None,
    )

    if intent.levels_consumed:
        available_contracts: int | str = sum(q for _p, q in intent.levels_consumed)
    else:
        available_contracts = "?"

    published_ts = intent.triggering_published_ts or ""
    age = humanize_age_since(published_ts) if published_ts else "unknown"
    article_age_note = (
        f" (published {age} ago)" if published_ts and age not in {"unknown", "just now"} else ""
    )

    return {
        "ticker": intent.ticker,
        "match_id": intent.triggering_match_id,
        "timestamp_et": now_et_long(),
        "market_title": intent.triggering_headline or f"(market {intent.ticker})",
        "subject_full_name": intent.triggering_source or "—",
        "avg_fill_price": avg_fill,
        "target_quantity": qty,
        "total_cost": dollars(cost_basis_cents),
        "total_fees": dollars(entry_fees_cents),
        "slippage": intent.slippage_cents,
        "best_ask": best_ask_cents,
        "total_commitment": dollars(total_cost_cents),
        "settlement_value": dollars(settlement_cents),
        "potential_profit": dollars_signed(profit_cents),
        "potential_roi": percent_from_bps(roi_bps),
        "potential_loss": dollars(potential_loss_cents),
        "source": intent.triggering_source or "(unknown)",
        "published_time_et": format_et_short(published_ts) if published_ts else "unknown",
        "article_age_note": article_age_note,
        "headline": intent.triggering_headline or "(no headline)",
        "key_quote": render_key_quote(intent.triggering_key_quote),
        "article_url": article_link_markdown(intent.triggering_article_url),
        # Cap audit (still useful for the engineering-minded operator).
        "cap_one_dollars": dollars(intent.cap_one_value_cents),
        "cap_two_dollars": dollars(intent.cap_two_value_cents),
        "cap_two_contracts": intent.cap_two_contracts,
        "available_contracts": available_contracts,
        "cap_binding": intent.cap_binding,
        "reasoning_text": intent.reasoning_text,
    }


__all__ = ["format_message"]
