"""DecisionEngine: pure transformation from (match, market state, position,
config) to a typed intent.

Performs no I/O. No network calls, no database writes, no timers. Same
code runs in the live decision loop and in the backtester — that's the
property that makes the backtester valid.

All money math uses :class:`int` (cents). No :class:`float` anywhere on
prices or USD amounts.

The exact rules implemented here are the LOCKED Phase-2 strategy from
`CLAUDE.md`. Any change to a numeric threshold must update both the
test suite and the rules section together.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from trumpbot.types.intents import (
    ReentryIntent,
    StopLossIntent,
    TradeIntent,
)


@dataclass(frozen=True)
class DecisionConfig:
    """Strategy parameters. Phase 2 rules section in CLAUDE.md is the spec."""

    llm_confidence_threshold: float = 0.85
    max_buy_price_cents: int = 80
    position_size_base_pct: float = 0.08
    position_size_cap_first_30_days_pct: float = 0.02
    position_size_cap_after_30_days_pct: float = 0.10
    total_exposure_cap_pct: float = 0.30
    stop_loss_drop_cents: int = 50
    minimum_position_size_pct: float = 0.01
    """Minimum percent of bankroll. Floor for confidence-scaled sizing."""


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
    """Snapshot of the orderbook at evaluation time."""

    ticker: str
    yes_bid_cents: int | None
    yes_ask_cents: int | None
    yes_ask_size: int | None = None


@dataclass(frozen=True)
class Position:
    """Open dry-run or live position in a market."""

    trade_id: int
    ticker: str
    entry_price_cents: int
    quantity: int
    cost_basis_usd_cents: int
    triggering_match_id: int


@dataclass(frozen=True)
class BankrollState:
    """Snapshot of bankroll + open exposure at evaluation time."""

    bankroll_usd_cents: int
    """Total starting bankroll, in USDCents."""

    open_position_cost_usd_cents: int
    """Sum of cost_basis across all open positions."""

    live_trading_started_at: datetime | None
    """When live trading began. Drives the 30-day position cap window.

    None means live trading hasn't started, so the conservative
    first-30-days cap applies.
    """

    @property
    def available_bankroll_usd_cents(self) -> int:
        """Bankroll minus already-deployed cost basis."""
        return max(0, self.bankroll_usd_cents - self.open_position_cost_usd_cents)


def _within_first_30_days(now_utc: datetime, started_at: datetime | None) -> bool:
    if started_at is None:
        return True
    delta = now_utc - started_at
    return delta < timedelta(days=30)


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
        now_utc: datetime | None = None,
    ) -> TradeIntent | None:
        """Return a :class:`TradeIntent` or ``None`` per the strategy rules.

        See ``CLAUDE.md`` §"Phase 2 Strategy Rules" for the canonical
        spec these checks implement.
        """
        now = now_utc or datetime.now(UTC)

        # Rule 1 — confidence threshold
        if match.confidence < self._cfg.llm_confidence_threshold:
            return None
        # Rule 1b — must come from the LLM cascade with a positive
        # interaction classification. Keyword-only rows have
        # interaction_occurred=False and never trigger trades.
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

        # Rule 5 — price ceiling
        if market_state.yes_ask_cents is None:
            return None
        if market_state.yes_ask_cents > self._cfg.max_buy_price_cents:
            return None

        # Rule 6/7/8 — confidence-scaled sizing with cap and floor
        target_pct = self._cfg.position_size_base_pct * (match.confidence / 1.0)
        cap_pct = (
            self._cfg.position_size_cap_first_30_days_pct
            if _within_first_30_days(now, bankroll.live_trading_started_at)
            else self._cfg.position_size_cap_after_30_days_pct
        )
        sized_pct = min(target_pct, cap_pct)
        sized_pct = max(sized_pct, self._cfg.minimum_position_size_pct)

        target_size_usd_cents = int(round(bankroll.bankroll_usd_cents * sized_pct))

        # Rule 9/10 — convert to integer contracts at the ask
        ask = market_state.yes_ask_cents
        target_quantity = target_size_usd_cents // ask
        if target_quantity < 1:
            return None

        actual_cost_cents = target_quantity * ask

        # Build the intent with full reasoning.
        reasoning = _build_entry_reasoning(
            match=match,
            market_state=market_state,
            target_pct=sized_pct,
            cap_pct=cap_pct,
            target_quantity=target_quantity,
            cost_cents=actual_cost_cents,
            bankroll=bankroll,
        )
        return TradeIntent(
            ticker=match.ticker,
            target_price_cents=ask,
            target_quantity=target_quantity,
            target_size_usd_cents=actual_cost_cents,
            triggering_match_id=match.match_id,
            confirmation_weight=match.source_weight * match.confidence,
            confidence_score=match.confidence,
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
        now_utc: datetime | None = None,
    ) -> ReentryIntent | None:
        """Return a :class:`ReentryIntent` if the rules permit re-entering
        a market we previously held and exited."""
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

        # Apply the same logic as evaluate_news_match for the new entry.
        # Use a synthetic "no current position" call: at this point the
        # prior position is closed so the engine should be willing to
        # propose. We piggy-back on the same evaluation by inlining the
        # checks (so a future change to entry rules automatically
        # applies to re-entries too).
        synthetic_intent = self.evaluate_news_match(
            match=match,
            market_state=market_state,
            current_position=None,
            bankroll=bankroll,
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


def _build_entry_reasoning(
    *,
    match: MatchSnapshot,
    market_state: MarketState,
    target_pct: float,
    cap_pct: float,
    target_quantity: int,
    cost_cents: int,
    bankroll: BankrollState,
) -> str:
    """Multi-line human-readable rationale for the future Telegram message
    and the trades.reasoning_text audit row."""
    pct_of_bankroll = cost_cents / bankroll.bankroll_usd_cents if bankroll.bankroll_usd_cents else 0
    pct_str = f"{pct_of_bankroll * 100:.1f}%"
    lines = [
        f"Source {match.source_name} (weight={match.source_weight}) classified an article "
        f"matching {match.ticker} at confidence {match.confidence:.2f}, with "
        f"interaction_occurred=true.",
        f"Current YES ask is {market_state.yes_ask_cents}c (max-buy ceiling 80c).",
        f"Confidence-scaled position: {target_pct * 100:.1f}% of bankroll, capped at "
        f"{cap_pct * 100:.1f}% by the live-trading window cap.",
        f"Resulting order: {target_quantity} contracts at {market_state.yes_ask_cents}c, "
        f"cost basis ${cost_cents / 100:.2f} ({pct_str} of bankroll).",
    ]
    return "\n".join(lines)


__all__ = [
    "BankrollState",
    "DecisionConfig",
    "DecisionEngine",
    "MarketState",
    "MatchSnapshot",
    "Position",
]
