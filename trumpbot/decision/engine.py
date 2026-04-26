"""DecisionEngine: pure transformation from (match, market state, position,
config) to a typed intent.

Performs no I/O. No network calls, no database writes, no timers. Same
code runs in the live decision loop and in the backtester — that's the
property that makes the backtester valid.

All money math uses :class:`int` (cents). No :class:`float` anywhere on
prices or USD amounts.

Phase 3 Part 1 added the two-cap sizing system + order-book walking +
fee-aware total-cost reasoning. The locked Phase-3 strategy in
``CLAUDE.md`` is the spec. Any change to a numeric threshold must
update both the test suite and the rules section together.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from trumpbot.execution.fees import calculate_entry_fee_cents
from trumpbot.execution.slippage import (
    OrderbookWalkResult,
    walk_orderbook_for_buy,
)
from trumpbot.types.intents import (
    ReentryIntent,
    StopLossIntent,
    TradeIntent,
)

CapBinding = Literal["cap_one", "cap_two", "tie", "unknown"]


@dataclass(frozen=True)
class DecisionConfig:
    """Strategy parameters. Phase 3 rules section in CLAUDE.md is the
    spec for the two-cap + walk + FOK pipeline."""

    llm_confidence_threshold: float = 0.85
    max_buy_price_cents: int = 90
    """Phase 4 Part 2.5: raised from 80 to 90. See
    :class:`trumpbot.config.DecisionPhaseConfig` docstring for
    rationale."""
    position_size_base_pct: float = 0.08
    """Confidence-scaled target as a fraction of bankroll. Multiplied
    by ``match.confidence`` to set the dollar target before any cap
    applies (so a 1.0-confidence match wants 8 % of bankroll, scaling
    down with confidence)."""

    # ---- Two-cap system (Phase 3 Part 1) -------------------------
    position_size_hard_cap_cents: int = 2000
    """Cap one — hard fixed-dollar ceiling per trade, in USDCents.
    Default $20.00. Designed to be raised to $500-1000 once the
    strategy has live-traded data; configurable via the YAML field
    ``decision.position_size_hard_cap_usd``."""

    position_size_volume_pct: float = 0.05
    """Cap two — fraction of the market's total traded volume the
    bot is willing to take in a single trade. Default 5 %.

    .. note::
       ``markets.volume`` is captured from Kalshi as a count of
       contracts. We convert to a dollar-equivalent cap by treating
       one contract as $1 of notional (i.e.,
       ``cap_two_cents = volume * 100 * 0.05``). For a brand-new
       market with no recorded volume, cap two evaluates to $0 and
       trading on that ticker is effectively disabled until volume
       develops.
    """

    min_trade_size_contracts: int = 5
    """Skip the trade entirely if the walk fills fewer than this
    many contracts. Phase-3 spec default."""

    min_trade_value_cents: int = 200
    """Skip the trade entirely if the walk's total cost is below
    this. Default $2.00. Belt-and-suspenders alongside
    ``min_trade_size_contracts``."""
    # --------------------------------------------------------------

    # Phase 4 Part 2.3: ``total_exposure_cap_pct`` was REMOVED. The
    # engine no longer references aggregate exposure for sizing.
    # Per-trade caps (cap_one + cap_two) plus bankroll sufficiency
    # provide the per-trade ceiling; aggregate exposure is bounded by
    # the operator's Kalshi deposit. See CLAUDE.md.

    stop_loss_drop_cents: int = 50


@dataclass(frozen=True)
class MatchSnapshot:
    """The decision-relevant subset of a `news_market_matches` row joined
    with its market + source data. Built by the daemon (or backtester)
    immediately before passing to the engine."""

    match_id: int
    ticker: str
    confidence: float
    """LLM cascade confidence (0..1)."""

    interaction_occurred: bool
    """True only when the LLM cascade explicitly classified the article
    as proving a qualifying interaction. Pre-LLM rows are False."""

    source_name: str
    source_weight: float
    is_kalshi_approved: bool

    market_open_ts: str | None
    market_close_ts: str | None
    article_published_ts: str | None
    classified_at_ts: str
    """When the matcher / LLM classified this row."""


@dataclass(frozen=True)
class MarketState:
    """Snapshot of the orderbook + market metadata at evaluation time."""

    ticker: str
    yes_bid_cents: int | None
    yes_ask_cents: int | None
    yes_ask_size: int | None = None

    total_volume_traded_contracts: int = 0
    """Cumulative number of YES contracts traded over the market's
    lifetime, sourced from ``markets.volume``. Phase 3 Part 1 uses
    this to compute cap two = 5 % x volume x $1/contract."""


@dataclass(frozen=True)
class Position:
    """Open dry-run or live position in a market."""

    trade_id: int
    ticker: str
    entry_price_cents: int
    quantity: int
    cost_basis_usd_cents: int
    triggering_match_id: int


BankrollSource = Literal["config", "kalshi_synced", "kalshi_fallback"]
"""Provenance tag for a :class:`BankrollState`. Helps reasoning text
honestly disclose whether the engine sized off a real Kalshi balance
or a stale fallback.

- ``config``: dry-run mode; engine used ``cfg.bankroll.starting_amount_usd``.
- ``kalshi_synced``: live mode; ``system_state['bankroll_usd_cents']``
  was populated by the bankroll sync loop and is what we used.
- ``kalshi_fallback``: live mode but the cache was empty (daemon just
  started OR sync has never succeeded); engine fell back to
  ``cfg.bankroll.starting_amount_usd``.
"""


@dataclass(frozen=True)
class BankrollState:
    """Snapshot of bankroll + open exposure at evaluation time.

    Phase 4 Part 2.2 (pre-live fix #1): added :attr:`source` and
    :attr:`last_synced_at` so reasoning text can disclose the
    provenance of the bankroll number. The engine and risk gate
    still consult :attr:`available_usd_cents` for sizing decisions.
    """

    bankroll_usd_cents: int
    """Total bankroll at evaluation time, in USDCents.

    In dry-run mode this is the configured starting amount. In live
    mode it's the cached Kalshi balance (or the configured fallback
    if the cache is empty)."""

    open_position_cost_usd_cents: int
    """Sum of cost_basis across all open positions."""

    source: BankrollSource = "config"
    """Where this number came from. Surfaced in reasoning text."""

    last_synced_at: datetime | None = None
    """When the cache was last refreshed from Kalshi (UTC). ``None``
    in dry-run mode and on the first cycle of a live-mode startup."""

    @property
    def available_usd_cents(self) -> int:
        """Bankroll minus already-deployed cost basis. The risk gate
        and engine size off this, NOT off ``bankroll_usd_cents``.
        Clamped at 0 — never returns negative."""
        return max(0, self.bankroll_usd_cents - self.open_position_cost_usd_cents)

    # Back-compat alias. Older tests / external consumers reference
    # the verbose name; keep both pointing at the same number so we
    # don't break callsites unnecessarily.
    @property
    def available_bankroll_usd_cents(self) -> int:
        return self.available_usd_cents


class DecisionEngine:
    """Pure-function evaluator.

    Returns either an intent (proposing a trade) or ``None`` (do
    nothing). Never raises on bad inputs — invalid inputs return
    ``None`` after writing the rejection-style ``reasoning`` into the
    skipped-reason channel via the caller's logging.
    """

    def __init__(self, config: DecisionConfig) -> None:
        self._cfg = config

    # -- entry --------------------------------------------------------

    def evaluate_news_match(
        self,
        match: MatchSnapshot,
        market_state: MarketState,
        current_position: Position | None,
        bankroll: BankrollState,
        *,
        yes_ask_levels: Sequence[tuple[int, int]] = (),
        now_utc: datetime | None = None,
    ) -> TradeIntent | None:
        """Return a :class:`TradeIntent` or ``None`` per the locked
        Phase 3 Part 1 strategy.

        Logic chain (all integer-cents arithmetic):

        1. Confidence ≥ 0.85, else None.
        2. ``interaction_occurred`` true, else None.
        3. Kalshi-approved source, else None.
        4. No open position, else None.
        5. Article inside the market's open/close window, else None.
        6. Top-of-book ask ≤ ``max_buy_price_cents`` (90 c, raised
           from 80 c in Phase 4 Part 2.5), else None — fast guard
           before the walker.
        7. ``cap_one = config.position_size_hard_cap_cents`` ($20).
        8. ``cap_two = floor(market.volume_traded x 5)`` — 5 % of
           market volume treating one contract as $1 of notional.
        9. ``effective_cap = min(cap_one, cap_two)``.
        10. Walk the order book for the effective cap with the 90 c
            ceiling and the Kalshi fee calculator.
        11. Skip if walk filled fewer than ``min_trade_size_contracts``
            or below ``min_trade_value_cents``.
        12. Build :class:`TradeIntent` with the full walk audit
            (avg fill, max fill, slippage, fees, levels consumed,
            cap binding).

        ``yes_ask_levels`` is the merged YES-ask side of the book
        (NO bids already inverted via
        :func:`merge_to_yes_asks`). Pass an empty sequence to skip
        the walker — used only by legacy fixtures; production code
        always passes the live book.
        """
        del now_utc  # unused — retained for caller compatibility

        # Rule 1 — confidence threshold
        if match.confidence < self._cfg.llm_confidence_threshold:
            return None
        # Rule 1b — must come from the LLM cascade with a positive
        # interaction classification.
        if not match.interaction_occurred:
            return None

        # Rule 2 — Kalshi-approved source
        if not match.is_kalshi_approved:
            return None

        # Rule 3 — one entry per cycle
        if current_position is not None:
            return None

        # Rule 4 — article inside market resolution window
        if not _article_within_window(
            match.article_published_ts,
            match.market_open_ts,
            match.market_close_ts,
        ):
            return None

        # Rule 5 — price ceiling (top-of-book pre-check; the walker
        # also enforces this per-level)
        if market_state.yes_ask_cents is None:
            return None
        if market_state.yes_ask_cents > self._cfg.max_buy_price_cents:
            return None

        # ---- Rule 7/8/9 — two-cap system ----
        cap_one_cents = self._cfg.position_size_hard_cap_cents
        # Cap two: 5 % of market volume. ``markets.volume`` is captured
        # from Kalshi as a contract count; we treat 1 contract ≈ $1 of
        # notional, so cap_two_cents = volume x 100 x 0.05 = volume x 5.
        # See DecisionConfig.position_size_volume_pct for the rationale.
        volume_dollars_cents = market_state.total_volume_traded_contracts * 100
        cap_two_cents = int(volume_dollars_cents * self._cfg.position_size_volume_pct)
        effective_cap_cents = min(cap_one_cents, cap_two_cents)
        cap_binding = _which_cap_binds(cap_one_cents, cap_two_cents)

        # Cap-two-zero (brand-new market): trading is effectively
        # disabled until volume develops. Drop with reasoning logged.
        if effective_cap_cents <= 0:
            return None

        # ---- Rule 10 — walk the book ----
        walk = walk_orderbook_for_buy(
            yes_ask_levels,
            target_dollars_cents=effective_cap_cents,
            max_price_cents=self._cfg.max_buy_price_cents,
            fee_calculator=calculate_entry_fee_cents,
        )

        # ---- Rule 11/12 — minimum-trade-size guard ----
        if walk.filled_quantity < self._cfg.min_trade_size_contracts:
            return None
        if walk.total_cost_cents < self._cfg.min_trade_value_cents:
            return None

        reasoning = _build_entry_reasoning(
            match=match,
            market_state=market_state,
            cap_one_cents=cap_one_cents,
            cap_two_cents=cap_two_cents,
            cap_binding=cap_binding,
            effective_cap_cents=effective_cap_cents,
            walk=walk,
            bankroll=bankroll,
        )

        return TradeIntent(
            ticker=match.ticker,
            target_price_cents=self._cfg.max_buy_price_cents,
            target_quantity=walk.filled_quantity,
            target_size_usd_cents=walk.total_cost_cents,
            triggering_match_id=match.match_id,
            confirmation_weight=match.source_weight * match.confidence,
            confidence_score=match.confidence,
            target_avg_fill_price_cents=walk.average_fill_price_cents,
            target_max_fill_price_cents=walk.max_price_reached_cents,
            estimated_fees_cents=walk.estimated_fees_cents,
            estimated_total_cost_cents=walk.total_cost_with_fees_cents,
            cap_binding=cap_binding,
            cap_one_value_cents=cap_one_cents,
            cap_two_value_cents=cap_two_cents,
            slippage_cents=walk.slippage_cents,
            levels_consumed=list(walk.levels_consumed),
            reasoning_text=reasoning,
        )

    # -- stop-loss ----------------------------------------------------

    def evaluate_stop_loss(
        self, position: Position, market_state: MarketState
    ) -> StopLossIntent | None:
        """Return a :class:`StopLossIntent` if the YES bid has fallen
        ``stop_loss_drop_cents`` or more below the entry price."""
        if market_state.yes_bid_cents is None:
            return None
        drop = position.entry_price_cents - market_state.yes_bid_cents
        if drop < self._cfg.stop_loss_drop_cents:
            return None

        bid = market_state.yes_bid_cents
        current_value = bid * position.quantity
        unrealized = current_value - position.cost_basis_usd_cents
        reasoning = (
            f"Stop-loss for {position.ticker}: entry {position.entry_price_cents}c, "
            f"current bid {bid}c, drop {drop}c (>= {self._cfg.stop_loss_drop_cents}c "
            "threshold). "
            f"{position.quantity} contracts at cost basis "
            f"${position.cost_basis_usd_cents/100:.2f}; current value "
            f"${current_value/100:.2f}; unrealized P&L "
            f"${unrealized/100:+.2f}."
        )
        return StopLossIntent(
            ticker=position.ticker,
            trade_id=position.trade_id,
            entry_price_cents=position.entry_price_cents,
            current_bid_cents=bid,
            drop_cents=drop,
            position_quantity=position.quantity,
            cost_basis_usd_cents=position.cost_basis_usd_cents,
            current_value_usd_cents=current_value,
            unrealized_pnl_usd_cents=unrealized,
            reasoning_text=reasoning,
        )

    # -- re-entry -----------------------------------------------------

    def evaluate_reentry(
        self,
        match: MatchSnapshot,
        market_state: MarketState,
        prior_trade: Position | None,
        prior_trade_outcome: str | None,
        prior_trade_realized_pnl_cents: int | None,
        bankroll: BankrollState,
        *,
        yes_ask_levels: Sequence[tuple[int, int]] = (),
        now_utc: datetime | None = None,
    ) -> ReentryIntent | None:
        """Return a :class:`ReentryIntent` if the rules permit re-entering
        a market we previously held and exited.

        Re-entry runs the SAME ``evaluate_news_match`` pipeline (so any
        change to entry rules — two-cap, walker, fee model — applies
        identically to re-entries) and re-packages the resulting
        :class:`TradeIntent` as a :class:`ReentryIntent` with the
        prior-trade audit fields attached. The walk fields
        (``target_avg_fill_price_cents``, etc.) are forwarded so the
        executor's FOK logic treats both intent types uniformly.
        """
        # Rule 1 — must have a prior trade record to "re-enter from".
        if prior_trade is None or prior_trade_outcome is None:
            return None

        # Rule 2 — prior trade must be closed.
        closed_statuses = {
            "dry_run_closed_stop",
            "dry_run_closed_resolved",
            "live_closed_stop",
            "live_closed_resolved",
        }
        if prior_trade_outcome not in closed_statuses:
            return None

        # Rule 3 — must be a fresh signal (different match id).
        if match.match_id == prior_trade.triggering_match_id:
            return None

        synthetic_intent = self.evaluate_news_match(
            match=match,
            market_state=market_state,
            current_position=None,
            bankroll=bankroll,
            yes_ask_levels=yes_ask_levels,
            now_utc=now_utc,
        )
        if synthetic_intent is None:
            return None

        return ReentryIntent(
            ticker=synthetic_intent.ticker,
            target_price_cents=synthetic_intent.target_price_cents,
            target_quantity=synthetic_intent.target_quantity,
            target_size_usd_cents=synthetic_intent.target_size_usd_cents,
            triggering_match_id=synthetic_intent.triggering_match_id,
            confirmation_weight=synthetic_intent.confirmation_weight,
            confidence_score=synthetic_intent.confidence_score,
            target_avg_fill_price_cents=synthetic_intent.target_avg_fill_price_cents,
            target_max_fill_price_cents=synthetic_intent.target_max_fill_price_cents,
            estimated_fees_cents=synthetic_intent.estimated_fees_cents,
            estimated_total_cost_cents=synthetic_intent.estimated_total_cost_cents,
            cap_binding=synthetic_intent.cap_binding,
            cap_one_value_cents=synthetic_intent.cap_one_value_cents,
            cap_two_value_cents=synthetic_intent.cap_two_value_cents,
            slippage_cents=synthetic_intent.slippage_cents,
            levels_consumed=list(synthetic_intent.levels_consumed),
            reasoning_text=(
                f"Re-entry into {match.ticker}. "
                f"Prior trade #{prior_trade.trade_id} closed via "
                f"{prior_trade_outcome} with realized P&L "
                f"${(prior_trade_realized_pnl_cents or 0)/100:+.2f}. "
                f"Fresh signal: " + synthetic_intent.reasoning_text
            ),
            prior_trade_id=prior_trade.trade_id,
            prior_trade_outcome=prior_trade_outcome,  # type: ignore[arg-type]
            prior_trade_realized_pnl_usd_cents=int(prior_trade_realized_pnl_cents or 0),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _article_within_window(
    article_ts: str | None, open_ts: str | None, close_ts: str | None
) -> bool:
    """The matcher can be lenient if the article ts is missing; the
    engine must be strict — only accept articles inside the window when
    timestamps are present and parseable."""
    if open_ts is None and close_ts is None:
        # Market without a known window: accept (defensive — the
        # discovery service shouldn't write a market without these).
        return True
    if article_ts is None:
        # Fail closed: don't fire on undated articles.
        return False
    try:
        article = datetime.fromisoformat(article_ts.replace("Z", "+00:00"))
        if article.tzinfo is None:
            article = article.replace(tzinfo=UTC)
    except ValueError:
        return False
    if open_ts is not None:
        try:
            opens = datetime.fromisoformat(open_ts.replace("Z", "+00:00"))
            if opens.tzinfo is None:
                opens = opens.replace(tzinfo=UTC)
            if article < opens:
                return False
        except ValueError:
            pass
    if close_ts is not None:
        try:
            closes = datetime.fromisoformat(close_ts.replace("Z", "+00:00"))
            if closes.tzinfo is None:
                closes = closes.replace(tzinfo=UTC)
            if article > closes:
                return False
        except ValueError:
            pass
    return True


def _which_cap_binds(cap_one_cents: int, cap_two_cents: int) -> CapBinding:
    """Return 'cap_one' / 'cap_two' / 'tie'. Used both in the engine's
    pipeline and in the reasoning-text builder so the labels stay
    consistent."""
    if cap_one_cents == cap_two_cents:
        return "tie"
    return "cap_one" if cap_one_cents < cap_two_cents else "cap_two"


def _build_entry_reasoning(
    *,
    match: MatchSnapshot,
    market_state: MarketState,
    cap_one_cents: int,
    cap_two_cents: int,
    cap_binding: CapBinding,
    effective_cap_cents: int,
    walk: OrderbookWalkResult,
    bankroll: BankrollState,
) -> str:
    """Phase 3 reasoning text — multi-paragraph audit log.

    The format is the contract documented in CLAUDE.md §"Phase 3 Part
    1 — reasoning text". The Telegram message and the
    ``trades.reasoning_text`` row use the same string. Each section
    cites integer cents in dollars-formatted strings; nothing here
    feeds back into the engine's arithmetic."""
    best_ask = market_state.yes_ask_cents or 0
    volume = market_state.total_volume_traded_contracts
    cap_one_dollars = f"${cap_one_cents / 100:.2f}"
    cap_two_dollars = f"${cap_two_cents / 100:.2f}"
    binding_label = {
        "cap_one": "Cap_one (hard $20)",
        "cap_two": "Cap_two (5 % of volume)",
        "tie": "Tie — both caps equal",
    }.get(cap_binding, cap_binding)

    cap_para = (
        f"Cap analysis: cap_one={cap_one_dollars}, "
        f"cap_two={cap_two_dollars} (5 % of {volume} contracts of market "
        f"volume). Binding: {binding_label}, sizing target "
        f"${effective_cap_cents / 100:.2f}."
    )

    levels_str = ", ".join(f"{q} @ {p}c" for p, q in walk.levels_consumed) or "none"
    walk_para = (
        f"Order-book walk for ${effective_cap_cents / 100:.2f}: "
        f"{walk.filled_quantity} contracts filled across {len(walk.levels_consumed)} "
        f"levels [{levels_str}] at avg "
        f"{walk.average_fill_price_cents}c (best ask: {best_ask}c, "
        f"slippage: {walk.slippage_cents}c). "
        f"Estimated Kalshi entry fees: ${walk.estimated_fees_cents / 100:.2f}."
    )

    cost_para = (
        f"Total expected cost: ${walk.total_cost_cents / 100:.2f} "
        f"(entry) + ${walk.estimated_fees_cents / 100:.2f} (fees) = "
        f"${walk.total_cost_with_fees_cents / 100:.2f}."
    )

    # Hypothetical YES-resolution P&L: payoff = 100c x qty, gross = payoff
    # - total_cost (incl. fees). ROI = gross / total_cost_with_fees.
    payoff_cents = 100 * walk.filled_quantity
    gross_pnl_cents = payoff_cents - walk.total_cost_with_fees_cents
    roi_pct = (
        (gross_pnl_cents / walk.total_cost_with_fees_cents * 100)
        if walk.total_cost_with_fees_cents > 0
        else 0.0
    )
    pnl_para = (
        f"If resolves YES at $1.00, gross P&L = "
        f"${payoff_cents / 100:.2f} - ${walk.total_cost_with_fees_cents / 100:.2f} "
        f"= ${gross_pnl_cents / 100:+.2f}, ROI = {roi_pct:.0f}%."
    )

    header = (
        f"Source {match.source_name} (weight={match.source_weight}) "
        f"classified an article matching {match.ticker} at confidence "
        f"{match.confidence:.2f}, with interaction_occurred=true."
    )
    ceiling = f"Current YES ask is {best_ask}c (max-buy ceiling 90c)."

    # Phase 4 Part 2.2 (pre-live fix #1): disclose where the bankroll
    # number came from so the operator can spot a stale-fallback
    # situation in the reasoning text itself.
    bankroll_label = {
        "config": "configured starting amount (dry-run)",
        "kalshi_synced": "Kalshi-synced balance",
        "kalshi_fallback": "configured starting amount (Kalshi sync hasn't run yet)",
    }.get(bankroll.source, bankroll.source)
    sync_age = ""
    if bankroll.last_synced_at is not None:
        from datetime import UTC

        age_secs = int((datetime.now(UTC) - bankroll.last_synced_at).total_seconds())
        sync_age = (
            f", last synced {age_secs // 60}m ago"
            if age_secs >= 60
            else f", last synced {age_secs}s ago"
        )
    bankroll_para = (
        f"Bankroll: ${bankroll.bankroll_usd_cents / 100:.2f} ({bankroll_label}{sync_age}); "
        f"available ${bankroll.available_usd_cents / 100:.2f} after open positions."
    )

    return "\n\n".join([header, ceiling, bankroll_para, cap_para, walk_para, cost_para, pnl_para])


__all__ = [
    "BankrollState",
    "DecisionConfig",
    "DecisionEngine",
    "MarketState",
    "MatchSnapshot",
    "Position",
]
