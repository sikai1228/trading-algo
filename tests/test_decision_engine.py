"""DecisionEngine unit tests — pure logic, no DB / network.

Pinned by the LOCKED Phase-2 strategy rules in CLAUDE.md. Each
numeric threshold or rule has at least one test that fails if the
threshold drifts.
"""

from __future__ import annotations

from trumpbot.decision.engine import (
    BankrollState,
    DecisionConfig,
    DecisionEngine,
    MarketState,
    MatchSnapshot,
    Position,
)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _match(
    *,
    confidence: float = 0.9,
    interaction_occurred: bool = True,
    is_kalshi_approved: bool = True,
    article_published_ts: str | None = "2026-04-15T12:00:00Z",
    market_open_ts: str | None = "2026-04-01T00:00:00Z",
    market_close_ts: str | None = "2026-04-30T23:59:59Z",
    source_name: str = "ap_via_gnews",
    source_weight: float = 1.0,
    ticker: str = "KXTRUMPMEET-26APR-VPUT",
    match_id: int = 1,
) -> MatchSnapshot:
    return MatchSnapshot(
        match_id=match_id,
        ticker=ticker,
        confidence=confidence,
        interaction_occurred=interaction_occurred,
        source_name=source_name,
        source_weight=source_weight,
        is_kalshi_approved=is_kalshi_approved,
        market_open_ts=market_open_ts,
        market_close_ts=market_close_ts,
        article_published_ts=article_published_ts,
        classified_at_ts="2026-04-15T12:00:01Z",
    )


def _bankroll(
    *,
    bankroll_usd_cents: int = 50000,
    open_position_cost_usd_cents: int = 0,
) -> BankrollState:
    return BankrollState(
        bankroll_usd_cents=bankroll_usd_cents,
        open_position_cost_usd_cents=open_position_cost_usd_cents,
    )


def _market(
    *,
    yes_bid: int | None = 50,
    yes_ask: int | None = 50,
    ticker: str = "KXTRUMPMEET-26APR-VPUT",
    volume: int = 100_000,
) -> MarketState:
    """Default market has 100 000 contracts of historical volume so
    cap two = 5 % x 100 000 x $1 = $5 000 — well above the $20 cap_one.
    Tests that want cap_two to bind override ``volume``."""
    return MarketState(
        ticker=ticker,
        yes_bid_cents=yes_bid,
        yes_ask_cents=yes_ask,
        total_volume_traded_contracts=volume,
    )


def _levels(price: int = 50, qty: int = 10_000) -> list[tuple[int, int]]:
    """Single-level deep book at ``price``. Phase-3 walker needs this
    to size every intent."""
    return [(price, qty)]


def _engine() -> DecisionEngine:
    return DecisionEngine(DecisionConfig())


# ---------------------------------------------------------------------------
# evaluate_news_match — entry rules
# ---------------------------------------------------------------------------


class TestEvaluateNewsMatch:
    def test_happy_path_produces_intent(self) -> None:
        intent = _engine().evaluate_news_match(
            _match(confidence=0.9),
            _market(),
            None,
            _bankroll(),
            yes_ask_levels=_levels(),
        )
        assert intent is not None
        assert intent.ticker == "KXTRUMPMEET-26APR-VPUT"
        assert intent.target_quantity >= 1
        # target_price_cents is the ceiling now (max-buy 90), not best ask.
        assert intent.target_price_cents == 90
        assert intent.target_avg_fill_price_cents == 50  # walked at 50c
        assert intent.cap_binding == "cap_one"  # $20 < $5000

    def test_below_confidence_threshold_returns_none(self) -> None:
        assert (
            _engine().evaluate_news_match(
                _match(confidence=0.84), _market(), None, _bankroll(), yes_ask_levels=_levels()
            )
            is None
        )

    def test_at_threshold_passes(self) -> None:
        out = _engine().evaluate_news_match(
            _match(confidence=0.85), _market(), None, _bankroll(), yes_ask_levels=_levels()
        )
        assert out is not None

    def test_interaction_occurred_false_returns_none(self) -> None:
        assert (
            _engine().evaluate_news_match(
                _match(interaction_occurred=False),
                _market(),
                None,
                _bankroll(),
                yes_ask_levels=_levels(),
            )
            is None
        )

    def test_non_approved_source_returns_none(self) -> None:
        assert (
            _engine().evaluate_news_match(
                _match(is_kalshi_approved=False),
                _market(),
                None,
                _bankroll(),
                yes_ask_levels=_levels(),
            )
            is None
        )

    def test_existing_open_position_blocks_entry(self) -> None:
        position = Position(
            trade_id=42,
            ticker="KXTRUMPMEET-26APR-VPUT",
            entry_price_cents=40,
            quantity=10,
            cost_basis_usd_cents=400,
            triggering_match_id=99,
        )
        assert (
            _engine().evaluate_news_match(
                _match(), _market(), position, _bankroll(), yes_ask_levels=_levels()
            )
            is None
        )

    def test_price_above_ceiling_returns_none(self) -> None:
        # Top-of-book 91c -> engine fast-fails before walking.
        # (Phase 4 Part 2.5: ceiling raised from 80c to 90c.)
        assert (
            _engine().evaluate_news_match(
                _match(),
                _market(yes_ask=91),
                None,
                _bankroll(),
                yes_ask_levels=_levels(price=91),
            )
            is None
        )

    def test_price_at_ceiling_passes(self) -> None:
        # 90c is the new ceiling (Phase 4 Part 2.5).
        assert (
            _engine().evaluate_news_match(
                _match(),
                _market(yes_ask=90),
                None,
                _bankroll(),
                yes_ask_levels=_levels(price=90),
            )
            is not None
        )

    def test_no_ask_returns_none(self) -> None:
        assert (
            _engine().evaluate_news_match(
                _match(), _market(yes_ask=None), None, _bankroll(), yes_ask_levels=[]
            )
            is None
        )

    # ---- Two-cap system (Phase 3 Part 1) ----

    def test_cap_one_binds_when_market_volume_is_huge(self) -> None:
        """Default $20 hard cap should bind on a high-volume market.
        $20 budget at 50c -> 40 contracts ($20.00)."""
        intent = _engine().evaluate_news_match(
            _match(confidence=0.9),
            _market(volume=100_000),  # cap_two = 5% x 100k x $1 = $5000
            None,
            _bankroll(),
            yes_ask_levels=_levels(qty=10_000),
        )
        assert intent is not None
        assert intent.cap_binding == "cap_one"
        assert intent.cap_one_value_cents == 2000
        assert intent.cap_two_value_cents == 500_000
        assert intent.target_size_usd_cents == 2000
        assert intent.target_quantity == 40

    def test_cap_two_binds_when_market_volume_is_thin(self) -> None:
        """Volume = 200 contracts -> cap_two = 5 % x 200 x $1 = $10.
        That's < $20 cap_one, so cap_two binds. $10 at 50c = 20 contracts."""
        intent = _engine().evaluate_news_match(
            _match(confidence=0.9),
            _market(volume=200),
            None,
            _bankroll(),
            yes_ask_levels=_levels(qty=10_000),
        )
        assert intent is not None
        assert intent.cap_binding == "cap_two"
        assert intent.cap_one_value_cents == 2000
        assert intent.cap_two_value_cents == 1000
        assert intent.target_size_usd_cents == 1000
        assert intent.target_quantity == 20

    def test_cap_two_zero_disables_trading_on_brand_new_market(self) -> None:
        """No volume at all -> cap_two = 0 -> effective cap = 0 -> drop."""
        out = _engine().evaluate_news_match(
            _match(),
            _market(volume=0),
            None,
            _bankroll(),
            yes_ask_levels=_levels(qty=10_000),
        )
        assert out is None

    def test_caps_equal_reports_tie_binding(self) -> None:
        """cap_one and cap_two equal -> binding = 'tie'.
        Volume = 400 -> cap_two = $20 = cap_one."""
        intent = _engine().evaluate_news_match(
            _match(confidence=0.9),
            _market(volume=400),
            None,
            _bankroll(),
            yes_ask_levels=_levels(qty=10_000),
        )
        assert intent is not None
        assert intent.cap_binding == "tie"
        assert intent.cap_one_value_cents == intent.cap_two_value_cents == 2000

    def test_cap_value_read_from_config(self) -> None:
        """Override the hard cap and confirm the engine respects it."""
        eng = DecisionEngine(DecisionConfig(position_size_hard_cap_cents=1000))
        intent = eng.evaluate_news_match(
            _match(confidence=0.95),
            _market(volume=100_000),
            None,
            _bankroll(),
            yes_ask_levels=_levels(qty=10_000),
        )
        assert intent is not None
        assert intent.target_size_usd_cents == 1000
        assert intent.target_quantity == 20

    def test_volume_pct_value_read_from_config(self) -> None:
        """Override cap_two pct and confirm the engine respects it.
        10 % x 200 contracts x $1 = $20 — same as cap_one -> tie."""
        eng = DecisionEngine(DecisionConfig(position_size_volume_pct=0.10))
        intent = eng.evaluate_news_match(
            _match(confidence=0.9),
            _market(volume=200),
            None,
            _bankroll(),
            yes_ask_levels=_levels(qty=10_000),
        )
        assert intent is not None
        assert intent.cap_two_value_cents == 2000

    # ---- Walker integration ----

    def test_intent_carries_walk_audit_fields(self) -> None:
        """Walking a multi-level book populates levels_consumed,
        slippage_cents, estimated_fees_cents."""
        intent = _engine().evaluate_news_match(
            _match(confidence=0.9),
            _market(volume=100_000),
            None,
            _bankroll(),
            yes_ask_levels=[(50, 5), (60, 5), (70, 100)],
        )
        assert intent is not None
        # Walked: 5 @ 50 ($2.50), 5 @ 60 ($3.00), then 22 @ 70 ($15.40)
        # for total $20.90 — wait, budget is $20 so we stop earlier:
        # 5 @ 50 = 250, 5 @ 60 = 300 (running 550), at 70 affordable =
        # (2000-550)//70 = 20, take 20 @ 70 = 1400. Total = 1950, qty = 30.
        assert intent.target_size_usd_cents == 1950
        assert intent.target_quantity == 30
        assert intent.levels_consumed == [(50, 5), (60, 5), (70, 20)]
        assert intent.slippage_cents > 0
        assert intent.estimated_fees_cents > 0
        assert intent.target_avg_fill_price_cents == 65  # 1950/30 = 65 exact

    def test_walk_with_no_acceptable_levels_returns_none(self) -> None:
        """Top-of-book passes the ceiling check (90c) but ALL levels
        are above it after merging — walker fills 0."""
        out = _engine().evaluate_news_match(
            _match(),
            _market(yes_ask=90),
            None,
            _bankroll(),
            yes_ask_levels=[(95, 1000)],  # no level <= 90c
        )
        assert out is None

    def test_min_trade_size_skips_when_walk_too_small(self) -> None:
        """Walker fills 2 contracts (under min 5) -> drop."""
        out = _engine().evaluate_news_match(
            _match(),
            _market(volume=100_000),
            None,
            _bankroll(),
            yes_ask_levels=[(50, 2)],  # only 2 contracts available
        )
        assert out is None

    def test_min_trade_value_skips_when_walk_too_cheap(self) -> None:
        """Override min_trade_size_contracts to allow few contracts but
        keep min_trade_value_cents at default $2.00. Walk: 5 @ 30c = $1.50 < $2.00."""
        eng = DecisionEngine(DecisionConfig(min_trade_size_contracts=1, min_trade_value_cents=200))
        out = eng.evaluate_news_match(
            _match(),
            _market(yes_ask=30, volume=100_000),
            None,
            _bankroll(),
            yes_ask_levels=[(30, 5)],
        )
        assert out is None

    # ---- Article-window + reasoning ----

    def test_article_outside_market_window_returns_none(self) -> None:
        m = _match(
            article_published_ts="2027-01-01T00:00:00Z",
            market_open_ts="2026-04-01T00:00:00Z",
            market_close_ts="2026-04-30T23:59:59Z",
        )
        assert (
            _engine().evaluate_news_match(m, _market(), None, _bankroll(), yes_ask_levels=_levels())
            is None
        )

    def test_article_undated_fails_closed(self) -> None:
        m = _match(article_published_ts=None)
        assert (
            _engine().evaluate_news_match(m, _market(), None, _bankroll(), yes_ask_levels=_levels())
            is None
        )

    def test_reasoning_text_cites_required_components(self) -> None:
        intent = _engine().evaluate_news_match(
            _match(confidence=0.9, source_name="reuters_via_gnews"),
            _market(yes_ask=42, volume=100_000),
            None,
            _bankroll(),
            yes_ask_levels=_levels(price=42, qty=10_000),
        )
        assert intent is not None
        text = intent.reasoning_text
        # Phase 3 reasoning text must include: source, confidence, ceiling,
        # cap analysis, walk depth + avg + slippage + fees, total cost,
        # YES-resolution P&L scenario.
        assert "reuters_via_gnews" in text
        assert "0.9" in text
        assert "90c" in text
        assert "Cap analysis" in text
        assert "cap_one" in text
        assert "cap_two" in text
        assert "Order-book walk" in text
        assert "slippage" in text
        assert "fees" in text
        assert "Total expected cost" in text
        assert "ROI" in text


# ---------------------------------------------------------------------------
# evaluate_stop_loss
# ---------------------------------------------------------------------------


class TestEvaluateStopLoss:
    def _pos(self, *, entry: int = 80, qty: int = 10) -> Position:
        return Position(
            trade_id=1,
            ticker="X",
            entry_price_cents=entry,
            quantity=qty,
            cost_basis_usd_cents=entry * qty,
            triggering_match_id=1,
        )

    def test_drop_below_threshold_returns_none(self) -> None:
        # Entry 80, bid 31 -> drop 49, below 50 threshold.
        out = _engine().evaluate_stop_loss(
            self._pos(entry=80), MarketState(ticker="X", yes_bid_cents=31, yes_ask_cents=33)
        )
        assert out is None

    def test_drop_exactly_at_threshold_fires(self) -> None:
        # Entry 80, bid 30 -> drop 50, exactly at threshold.
        out = _engine().evaluate_stop_loss(
            self._pos(entry=80), MarketState(ticker="X", yes_bid_cents=30, yes_ask_cents=32)
        )
        assert out is not None
        assert out.drop_cents == 50

    def test_drop_well_above_threshold_fires(self) -> None:
        out = _engine().evaluate_stop_loss(
            self._pos(entry=80), MarketState(ticker="X", yes_bid_cents=10, yes_ask_cents=12)
        )
        assert out is not None
        assert out.drop_cents == 70
        assert out.unrealized_pnl_usd_cents == 10 * 10 - 80 * 10

    def test_no_bid_returns_none(self) -> None:
        out = _engine().evaluate_stop_loss(
            self._pos(), MarketState(ticker="X", yes_bid_cents=None, yes_ask_cents=20)
        )
        assert out is None

    def test_intent_carries_position_metadata(self) -> None:
        out = _engine().evaluate_stop_loss(
            self._pos(entry=70, qty=5),
            MarketState(ticker="X", yes_bid_cents=15, yes_ask_cents=17),
        )
        assert out is not None
        assert out.entry_price_cents == 70
        assert out.position_quantity == 5
        assert out.cost_basis_usd_cents == 350
        assert out.current_value_usd_cents == 75
        assert out.unrealized_pnl_usd_cents == -275


# ---------------------------------------------------------------------------
# evaluate_reentry
# ---------------------------------------------------------------------------


class TestEvaluateReentry:
    def _prior(self) -> Position:
        return Position(
            trade_id=99,
            ticker="KXTRUMPMEET-26APR-VPUT",
            entry_price_cents=60,
            quantity=10,
            cost_basis_usd_cents=600,
            triggering_match_id=42,
        )

    def test_no_prior_trade_returns_none(self) -> None:
        out = _engine().evaluate_reentry(
            _match(match_id=43), _market(), None, None, None, _bankroll()
        )
        assert out is None

    def test_prior_still_open_returns_none(self) -> None:
        # "still open" represented by passing a non-closed status.
        out = _engine().evaluate_reentry(
            _match(match_id=43),
            _market(),
            self._prior(),
            "dry_run",
            None,
            _bankroll(),
        )
        assert out is None

    def test_same_match_as_prior_returns_none(self) -> None:
        out = _engine().evaluate_reentry(
            _match(match_id=42),  # same as prior.triggering_match_id
            _market(),
            self._prior(),
            "dry_run_closed_stop",
            -100,
            _bankroll(),
        )
        assert out is None

    def test_fresh_match_after_stopped_out_proposes(self) -> None:
        out = _engine().evaluate_reentry(
            _match(match_id=43, confidence=0.9),
            _market(yes_ask=40),
            self._prior(),
            "dry_run_closed_stop",
            -100,
            _bankroll(),
            yes_ask_levels=_levels(price=40),
        )
        assert out is not None
        assert out.is_reentry is True
        assert out.prior_trade_id == 99
        assert out.prior_trade_outcome == "dry_run_closed_stop"
        assert out.prior_trade_realized_pnl_usd_cents == -100
        # Phase 3 audit fields propagate from the synthesized intent.
        assert out.target_avg_fill_price_cents == 40
        assert out.cap_binding == "cap_one"

    def test_fresh_match_after_resolution_proposes(self) -> None:
        out = _engine().evaluate_reentry(
            _match(match_id=43, confidence=0.9),
            _market(yes_ask=40),
            self._prior(),
            "dry_run_closed_resolved",
            +400,
            _bankroll(),
            yes_ask_levels=_levels(price=40),
        )
        assert out is not None
        assert out.prior_trade_outcome == "dry_run_closed_resolved"


# ---------------------------------------------------------------------------
# Type-system: prices are int
# ---------------------------------------------------------------------------


def test_no_float_in_intent() -> None:
    """A drift bug would silently turn target_price_cents into a float
    once we multiply by a confidence score. Pin int type."""
    intent = _engine().evaluate_news_match(
        _match(confidence=0.873),
        _market(yes_ask=42),
        None,
        _bankroll(),
        yes_ask_levels=_levels(price=42),
    )
    assert intent is not None
    assert isinstance(intent.target_price_cents, int)
    assert isinstance(intent.target_quantity, int)
    assert isinstance(intent.target_size_usd_cents, int)
