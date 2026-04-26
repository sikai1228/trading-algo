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
    ticker: str = "KXTRUMPMEET-26APR-VPUT",
    match_id: int = 1,
) -> MatchSnapshot:
    return MatchSnapshot(
        match_id=match_id,
        ticker=ticker,
        confidence=confidence,
        interaction_occurred=interaction_occurred,
        source_name=source_name,
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

    def test_low_confidence_with_interaction_true_still_produces_intent(self) -> None:
        """Phase 4 Part 2.9 removed the ``llm_confidence_threshold``
        engine gate. A match with ``interaction_occurred=True`` and
        ``confidence=0.51`` (well below the old 0.85 cut) must produce
        a TradeIntent — proves the threshold is fully gone."""
        out = _engine().evaluate_news_match(
            _match(confidence=0.51, interaction_occurred=True),
            _market(),
            None,
            _bankroll(),
            yes_ask_levels=_levels(),
        )
        assert out is not None
        assert out.confidence_score == 0.51

    def test_high_confidence_but_interaction_false_returns_none(self) -> None:
        """Phase 4 Part 2.9: ``interaction_occurred=False`` with
        ``confidence=0.99`` must return None — the boolean is the
        sole gate, the confidence float does not override it."""
        assert (
            _engine().evaluate_news_match(
                _match(confidence=0.99, interaction_occurred=False),
                _market(),
                None,
                _bankroll(),
                yes_ask_levels=_levels(),
            )
            is None
        )

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

    # ---- Two-cap system (Phase 3 Part 1, redefined Phase 4 Part 2.6) ----
    #
    # Cap two now reflects LIVE orderbook depth: 20 % of YES contracts
    # available at prices ≤ max_buy_price_cents (90c). Total historical
    # volume on the market is no longer involved.

    def test_cap_one_binds_when_book_is_deep(self) -> None:
        """Default $20 hard cap should bind when the live book has
        deep acceptable inventory. 10 000 contracts at 50c → cap_two
        ≈ 20 % x 10 000 x 50c = $1000. Cap_one ($20) is much smaller
        → cap_one binds. $20 budget at 50c → 40 contracts."""
        intent = _engine().evaluate_news_match(
            _match(confidence=0.9),
            _market(),
            None,
            _bankroll(),
            yes_ask_levels=_levels(qty=10_000),
        )
        assert intent is not None
        assert intent.cap_binding == "cap_one"
        assert intent.cap_one_value_cents == 2000
        # cap_two = floor(10000 x 0.20) x 50c = 2000 contracts x 50 = 100 000c
        assert intent.cap_two_contracts == 2000
        assert intent.cap_two_value_cents == 100_000
        assert intent.target_size_usd_cents == 2000
        assert intent.target_quantity == 40

    def test_cap_two_binds_when_book_is_thin(self) -> None:
        """Thin book: only 50 contracts available at 50c. cap_two =
        floor(50 x 0.20) = 10 contracts x 50c = $5.00, well under
        cap_one ($20.00). cap_two binds. Walker fills $5.00 → 10
        contracts at 50c."""
        intent = _engine().evaluate_news_match(
            _match(confidence=0.9),
            _market(),
            None,
            _bankroll(),
            yes_ask_levels=[(50, 50)],
        )
        assert intent is not None
        assert intent.cap_binding == "cap_two"
        assert intent.cap_one_value_cents == 2000
        assert intent.cap_two_contracts == 10
        assert intent.cap_two_value_cents == 500
        assert intent.target_size_usd_cents == 500
        assert intent.target_quantity == 10

    def test_empty_book_skips_trade(self) -> None:
        """Empty live book → no acceptable depth → engine returns
        None (no_acceptable_liquidity scenario)."""
        out = _engine().evaluate_news_match(
            _match(),
            _market(),
            None,
            _bankroll(),
            yes_ask_levels=[],
        )
        assert out is None

    def test_book_above_ceiling_skips_trade(self) -> None:
        """Every level above the 90c ceiling → no acceptable depth →
        engine returns None. Top-of-book pre-check actually catches
        this earlier (best_ask > 90), but this still pins the
        cap-two computation when somehow we reach it (e.g. matcher
        finds a stale market)."""
        out = _engine().evaluate_news_match(
            _match(),
            _market(yes_ask=95),
            None,
            _bankroll(),
            yes_ask_levels=[(95, 1000)],
        )
        assert out is None

    def test_book_too_thin_to_meet_min_trade_size_skips(self) -> None:
        """Available = 20 contracts under ceiling. cap_two_contracts =
        floor(20 x 0.20) = 4 < min_trade_size_contracts (5) → engine
        skips with the below_minimum_after_orderbook_cap scenario."""
        out = _engine().evaluate_news_match(
            _match(),
            _market(),
            None,
            _bankroll(),
            yes_ask_levels=[(50, 20)],
        )
        assert out is None

    def test_caps_equal_reports_tie_binding(self) -> None:
        """cap_one and cap_two equal → binding = 'tie'.

        For 200 contracts at 50c: cap_two_contracts = 40, cap_two =
        40 x 50 = 2000c = $20 = cap_one → tie.
        """
        intent = _engine().evaluate_news_match(
            _match(confidence=0.9),
            _market(),
            None,
            _bankroll(),
            yes_ask_levels=[(50, 200)],
        )
        assert intent is not None
        assert intent.cap_binding == "tie"
        assert intent.cap_one_value_cents == intent.cap_two_value_cents == 2000

    def test_cap_value_read_from_config(self) -> None:
        """Override the hard cap and confirm the engine respects it."""
        eng = DecisionEngine(DecisionConfig(position_size_hard_cap_cents=1000))
        intent = eng.evaluate_news_match(
            _match(confidence=0.95),
            _market(),
            None,
            _bankroll(),
            yes_ask_levels=_levels(qty=10_000),
        )
        assert intent is not None
        assert intent.target_size_usd_cents == 1000
        assert intent.target_quantity == 20

    def test_orderbook_pct_value_read_from_config(self) -> None:
        """Override cap_two pct and confirm the engine respects it.
        Bumping to 40 % of the same thin book doubles cap_two."""
        eng = DecisionEngine(DecisionConfig(position_size_orderbook_pct=0.40))
        intent = eng.evaluate_news_match(
            _match(confidence=0.9),
            _market(),
            None,
            _bankroll(),
            yes_ask_levels=[(50, 50)],
        )
        assert intent is not None
        # 40 % x 50 contracts = 20 contracts at 50c = $10.00
        assert intent.cap_two_contracts == 20
        assert intent.cap_two_value_cents == 1000

    def test_volume_weighted_avg_used_for_cap_two_dollar_value(self) -> None:
        """When acceptable levels span multiple prices, cap_two_cents
        uses the volume-weighted average so the dollar number is
        comparable to cap_one. Levels [(50, 100), (80, 100)] →
        available 200, cap_two_contracts = 40, avg = (5000+8000)/200
        = 65c, cap_two = 40 x 65 = 2600c. Cap_one ($20) binds."""
        intent = _engine().evaluate_news_match(
            _match(confidence=0.9),
            _market(),
            None,
            _bankroll(),
            yes_ask_levels=[(50, 100), (80, 100)],
        )
        assert intent is not None
        assert intent.cap_two_contracts == 40
        assert intent.cap_two_value_cents == 2600
        assert intent.cap_binding == "cap_one"  # 2000 < 2600

    # ---- Walker integration ----

    def test_intent_carries_walk_audit_fields(self) -> None:
        """Walking a multi-level book populates levels_consumed,
        slippage_cents, estimated_fees_cents.

        Phase 4 Part 2.6: book extended to 1010 contracts under the
        ceiling so cap_two doesn't bind below cap_one. cap_two =
        floor(1010 x 0.20) x avg ≈ 202 contracts at avg 70c = ~$141,
        well above the $20 cap_one. cap_one binds → walker walks $20
        (same as before)."""
        intent = _engine().evaluate_news_match(
            _match(confidence=0.9),
            _market(),
            None,
            _bankroll(),
            yes_ask_levels=[(50, 5), (60, 5), (70, 1000)],
        )
        assert intent is not None
        # Walked: 5 @ 50 = 250, 5 @ 60 = 300 (running 550), at 70 affordable =
        # (2000-550)//70 = 20, take 20 @ 70 = 1400. Total = 1950, qty = 30.
        assert intent.cap_binding == "cap_one"
        assert intent.target_size_usd_cents == 1950
        assert intent.target_quantity == 30
        assert intent.levels_consumed == [(50, 5), (60, 5), (70, 20)]
        assert intent.slippage_cents > 0
        assert intent.estimated_fees_cents > 0
        assert intent.target_avg_fill_price_cents == 65  # 1950/30 = 65 exact
        # Phase 4 Part 2.6: cap_two_contracts populated from the
        # 20%-of-orderbook-depth computation.
        assert intent.cap_two_contracts == 202  # floor(1010 x 0.20)

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


# ---------------------------------------------------------------------------
# _compute_cap_two_pure helper (Phase 4 Part 2.6)
# ---------------------------------------------------------------------------


class TestComputeCapTwoPure:
    """Pure-function unit tests for the cap_two computation. These pin
    the math independently of the full decision pipeline so a math
    bug surfaces with a tighter test name."""

    def test_empty_levels_returns_zero(self) -> None:
        from trumpbot.decision.engine import _compute_cap_two_pure

        assert _compute_cap_two_pure([], max_price_cents=90, orderbook_pct=0.20) == (0, 0)

    def test_all_levels_above_ceiling_returns_zero(self) -> None:
        from trumpbot.decision.engine import _compute_cap_two_pure

        assert _compute_cap_two_pure(
            [(95, 100), (99, 50)], max_price_cents=90, orderbook_pct=0.20
        ) == (0, 0)

    def test_single_level_under_ceiling(self) -> None:
        """100 contracts at 50c → 20 contracts x 50c = 1000c."""
        from trumpbot.decision.engine import _compute_cap_two_pure

        contracts, value = _compute_cap_two_pure(
            [(50, 100)], max_price_cents=90, orderbook_pct=0.20
        )
        assert contracts == 20
        assert value == 1000

    def test_volume_weighted_average_across_levels(self) -> None:
        """[(50, 100), (80, 100)] available=200, cap_contracts=40,
        avg=(5000+8000)/200=65, value=40*65=2600."""
        from trumpbot.decision.engine import _compute_cap_two_pure

        contracts, value = _compute_cap_two_pure(
            [(50, 100), (80, 100)], max_price_cents=90, orderbook_pct=0.20
        )
        assert contracts == 40
        assert value == 2600

    def test_filter_drops_above_ceiling(self) -> None:
        """100 @ 50c kept; 100 @ 95c dropped. Result same as
        single-level test."""
        from trumpbot.decision.engine import _compute_cap_two_pure

        contracts, value = _compute_cap_two_pure(
            [(50, 100), (95, 100)], max_price_cents=90, orderbook_pct=0.20
        )
        assert contracts == 20
        assert value == 1000

    def test_zero_quantity_levels_ignored(self) -> None:
        from trumpbot.decision.engine import _compute_cap_two_pure

        contracts, value = _compute_cap_two_pure(
            [(50, 0), (60, 100)], max_price_cents=90, orderbook_pct=0.20
        )
        # Only 60c level counts → 100 available → 20 x 60 = 1200
        assert contracts == 20
        assert value == 1200

    def test_tiny_book_floors_to_zero_contracts(self) -> None:
        """4 contracts x 0.20 = 0.8 → floor to 0 → returns zero/zero."""
        from trumpbot.decision.engine import _compute_cap_two_pure

        contracts, value = _compute_cap_two_pure([(50, 4)], max_price_cents=90, orderbook_pct=0.20)
        assert contracts == 0
        assert value == 0


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
