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
    *, yes_bid: int | None = 50, yes_ask: int | None = 50, ticker: str = "KXTRUMPMEET-26APR-VPUT"
) -> MarketState:
    return MarketState(ticker=ticker, yes_bid_cents=yes_bid, yes_ask_cents=yes_ask)


def _engine() -> DecisionEngine:
    return DecisionEngine(DecisionConfig())


# ---------------------------------------------------------------------------
# evaluate_news_match — entry rules
# ---------------------------------------------------------------------------


class TestEvaluateNewsMatch:
    def test_happy_path_produces_intent(self) -> None:
        intent = _engine().evaluate_news_match(_match(confidence=0.9), _market(), None, _bankroll())
        assert intent is not None
        assert intent.ticker == "KXTRUMPMEET-26APR-VPUT"
        assert intent.target_quantity >= 1
        assert intent.target_price_cents == 50

    def test_below_confidence_threshold_returns_none(self) -> None:
        assert (
            _engine().evaluate_news_match(_match(confidence=0.84), _market(), None, _bankroll())
            is None
        )

    def test_at_threshold_passes(self) -> None:
        out = _engine().evaluate_news_match(_match(confidence=0.85), _market(), None, _bankroll())
        assert out is not None

    def test_interaction_occurred_false_returns_none(self) -> None:
        assert (
            _engine().evaluate_news_match(
                _match(interaction_occurred=False), _market(), None, _bankroll()
            )
            is None
        )

    def test_non_approved_source_returns_none(self) -> None:
        assert (
            _engine().evaluate_news_match(
                _match(is_kalshi_approved=False), _market(), None, _bankroll()
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
        assert _engine().evaluate_news_match(_match(), _market(), position, _bankroll()) is None

    def test_price_above_ceiling_returns_none(self) -> None:
        assert (
            _engine().evaluate_news_match(_match(), _market(yes_ask=81), None, _bankroll()) is None
        )

    def test_price_at_ceiling_passes(self) -> None:
        assert (
            _engine().evaluate_news_match(_match(), _market(yes_ask=80), None, _bankroll())
            is not None
        )

    def test_no_ask_returns_none(self) -> None:
        assert (
            _engine().evaluate_news_match(_match(), _market(yes_ask=None), None, _bankroll())
            is None
        )

    def test_fixed_cap_engages_when_target_above_20_dollars(self) -> None:
        """$500 bankroll x 8% x 0.95 = $38 unconstrained → cap to $20.
        At 50c/contract that's 40 contracts ($20.00) exactly."""
        bank = _bankroll(bankroll_usd_cents=50000)
        intent = _engine().evaluate_news_match(_match(confidence=0.95), _market(), None, bank)
        assert intent is not None
        assert intent.target_size_usd_cents == 2000
        assert intent.target_quantity == 40

    def test_fixed_cap_not_engaged_when_target_below_cap(self) -> None:
        """$200 bankroll x 8% x 0.5 = $8 unconstrained → cap NOT engaged.
        $8 / 50c = 16 contracts ($8.00).

        Note: confidence=0.5 is below the LLM threshold (0.85), so the
        engine would normally drop the match before sizing. We override
        the threshold here to isolate the sizing behavior."""
        from trumpbot.decision.engine import DecisionConfig

        eng = DecisionEngine(DecisionConfig(llm_confidence_threshold=0.5))
        bank = _bankroll(bankroll_usd_cents=20000)
        intent = eng.evaluate_news_match(_match(confidence=0.5), _market(), None, bank)
        assert intent is not None
        assert intent.target_size_usd_cents == 800
        assert intent.target_quantity == 16

    def test_cap_value_read_from_config(self) -> None:
        """Override `position_size_cap_usd_cents` and confirm the
        engine respects it. $500 x 8% x 0.95 = $38 → cap at $10 = 1000c
        → 20 contracts at 50c."""
        from trumpbot.decision.engine import DecisionConfig

        eng = DecisionEngine(DecisionConfig(position_size_cap_usd_cents=1000))
        bank = _bankroll(bankroll_usd_cents=50000)
        intent = eng.evaluate_news_match(_match(confidence=0.95), _market(), None, bank)
        assert intent is not None
        assert intent.target_size_usd_cents == 1000
        assert intent.target_quantity == 20

    def test_reasoning_text_says_cap_engaged_when_engaged(self) -> None:
        bank = _bankroll(bankroll_usd_cents=50000)
        intent = _engine().evaluate_news_match(_match(confidence=0.95), _market(), None, bank)
        assert intent is not None
        # When the cap is engaged the text cites the unconstrained
        # confidence-scaled target ($38) and the $20 cap.
        assert "capped at fixed $20.00" in intent.reasoning_text
        assert "$38" in intent.reasoning_text

    def test_reasoning_text_says_under_cap_when_not_engaged(self) -> None:
        from trumpbot.decision.engine import DecisionConfig

        eng = DecisionEngine(DecisionConfig(llm_confidence_threshold=0.5))
        bank = _bankroll(bankroll_usd_cents=20000)
        intent = eng.evaluate_news_match(_match(confidence=0.5), _market(), None, bank)
        assert intent is not None
        assert "under $20.00 cap" in intent.reasoning_text

    def test_below_one_contract_returns_none(self) -> None:
        # $5 bankroll cannot afford a single contract at 80¢.
        bank = _bankroll(bankroll_usd_cents=500)
        out = _engine().evaluate_news_match(_match(), _market(yes_ask=80), None, bank)
        assert out is None

    def test_article_outside_market_window_returns_none(self) -> None:
        m = _match(
            article_published_ts="2027-01-01T00:00:00Z",
            market_open_ts="2026-04-01T00:00:00Z",
            market_close_ts="2026-04-30T23:59:59Z",
        )
        assert _engine().evaluate_news_match(m, _market(), None, _bankroll()) is None

    def test_article_undated_fails_closed(self) -> None:
        m = _match(article_published_ts=None)
        assert _engine().evaluate_news_match(m, _market(), None, _bankroll()) is None

    def test_reasoning_text_cites_required_components(self) -> None:
        intent = _engine().evaluate_news_match(
            _match(confidence=0.9, source_name="reuters_via_gnews"),
            _market(yes_ask=42),
            None,
            _bankroll(),
        )
        assert intent is not None
        text = intent.reasoning_text
        # Source named, confidence stated, ceiling cited, sizing
        # rationale shown — these are the auditability requirements
        # from the strategy spec.
        assert "reuters_via_gnews" in text
        assert "0.9" in text
        assert "80c" in text
        # Sizing rationale always quotes the dollar cap.
        assert "$20.00" in text


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
        # Entry 80, bid 31 → drop 49, below 50 threshold.
        out = _engine().evaluate_stop_loss(
            self._pos(entry=80), MarketState(ticker="X", yes_bid_cents=31, yes_ask_cents=33)
        )
        assert out is None

    def test_drop_exactly_at_threshold_fires(self) -> None:
        # Entry 80, bid 30 → drop 50, exactly at threshold.
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
        )
        assert out is not None
        assert out.is_reentry is True
        assert out.prior_trade_id == 99
        assert out.prior_trade_outcome == "dry_run_closed_stop"
        assert out.prior_trade_realized_pnl_usd_cents == -100

    def test_fresh_match_after_resolution_proposes(self) -> None:
        out = _engine().evaluate_reentry(
            _match(match_id=43, confidence=0.9),
            _market(yes_ask=40),
            self._prior(),
            "dry_run_closed_resolved",
            +400,
            _bankroll(),
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
        _match(confidence=0.873), _market(yes_ask=42), None, _bankroll()
    )
    assert intent is not None
    assert isinstance(intent.target_price_cents, int)
    assert isinstance(intent.target_quantity, int)
    assert isinstance(intent.target_size_usd_cents, int)
