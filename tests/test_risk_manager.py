"""RiskManager tests.

Each rejection reason has a dedicated test. Construction guard on
RiskApprovedOrder pinned. Size-cap engagement is exercised separately
from rejections (cap reduces quantity, doesn't reject)."""

from __future__ import annotations

from pathlib import Path

import pytest

from trumpbot.db.connection import Database
from trumpbot.decision.engine import BankrollState
from trumpbot.risk.manager import RiskConfig, RiskManager, RiskState
from trumpbot.types.intents import (
    RISK_APPROVAL_TOKEN,
    ReentryIntent,
    RiskApprovedOrder,
    RiskRejection,
    StopLossIntent,
    TradeIntent,
)


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "risk.db")
    db.connect()
    return db


def _bankroll(
    *,
    bankroll_usd_cents: int = 50000,
    open_position_cost_usd_cents: int = 0,
) -> BankrollState:
    return BankrollState(
        bankroll_usd_cents=bankroll_usd_cents,
        open_position_cost_usd_cents=open_position_cost_usd_cents,
    )


def _state(
    *,
    bankroll: BankrollState | None = None,
    open_tickers: frozenset[str] = frozenset(),
) -> RiskState:
    return RiskState(
        bankroll=bankroll or _bankroll(),
        open_position_tickers=open_tickers,
    )


def _intent(
    *,
    target_price_cents: int = 50,
    target_quantity: int = 10,
    ticker: str = "X",
) -> TradeIntent:
    return TradeIntent(
        ticker=ticker,
        target_price_cents=target_price_cents,
        target_quantity=target_quantity,
        target_size_usd_cents=target_price_cents * target_quantity,
        triggering_match_id=1,
        confirmation_weight=0.9,
        confidence_score=0.9,
        reasoning_text="for tests",
    )


def _stop(*, ticker: str = "X", trade_id: int = 1) -> StopLossIntent:
    return StopLossIntent(
        ticker=ticker,
        trade_id=trade_id,
        entry_price_cents=80,
        current_bid_cents=20,
        drop_cents=60,
        position_quantity=10,
        cost_basis_usd_cents=800,
        current_value_usd_cents=200,
        unrealized_pnl_usd_cents=-600,
        reasoning_text="stop",
    )


# ---------------------------------------------------------------------------
# Construction guard — non-negotiable
# ---------------------------------------------------------------------------


class TestRiskApprovedOrderConstructionGuard:
    def test_cannot_construct_without_token(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            RiskApprovedOrder(
                intent_type="entry",
                intent=_intent(),
                risk_decision_id=1,
                approval_token=object(),  # type: ignore[arg-type]
            )

    def test_can_construct_with_real_token(self) -> None:
        ok = RiskApprovedOrder(
            intent_type="entry",
            intent=_intent(),
            risk_decision_id=1,
            approval_token=RISK_APPROVAL_TOKEN,
        )
        assert ok.risk_check_passed is True

    def test_frozen_no_mutation(self) -> None:
        from pydantic import ValidationError

        ok = RiskApprovedOrder(
            intent_type="entry",
            intent=_intent(),
            risk_decision_id=1,
            approval_token=RISK_APPROVAL_TOKEN,
        )
        with pytest.raises(ValidationError):
            ok.risk_decision_id = 2


# ---------------------------------------------------------------------------
# Per-rejection-reason coverage
# ---------------------------------------------------------------------------


class TestRejections:
    def test_disabled_rejects_everything(self, tmp_path: Path) -> None:
        rm = RiskManager(db=_db(tmp_path), config=RiskConfig(enabled=False))
        out = rm.evaluate(_intent(), _state())
        assert isinstance(out, RiskRejection)
        assert out.reason == "risk_disabled"

    def test_halted_rejects(self, tmp_path: Path) -> None:
        rm = RiskManager(db=_db(tmp_path), config=RiskConfig(halted=True))
        out = rm.evaluate(_intent(), _state())
        assert isinstance(out, RiskRejection)
        assert out.reason == "trading_halted"

    def test_price_above_ceiling(self, tmp_path: Path) -> None:
        rm = RiskManager(db=_db(tmp_path), config=RiskConfig())
        out = rm.evaluate(_intent(target_price_cents=81), _state())
        assert isinstance(out, RiskRejection)
        assert out.reason == "price_above_ceiling"

    def test_insufficient_bankroll(self, tmp_path: Path) -> None:
        rm = RiskManager(db=_db(tmp_path), config=RiskConfig())
        # Big trade vs tiny bankroll.
        out = rm.evaluate(
            _intent(target_price_cents=50, target_quantity=200),  # $100
            _state(bankroll=_bankroll(bankroll_usd_cents=500)),
        )
        assert isinstance(out, RiskRejection)
        assert out.reason == "insufficient_bankroll"

    def test_exposure_cap_exceeded(self, tmp_path: Path) -> None:
        rm = RiskManager(db=_db(tmp_path), config=RiskConfig())
        # Bankroll $500; 30% cap = $150. Already $140 deployed, new $20 -> busts.
        out = rm.evaluate(
            _intent(target_price_cents=50, target_quantity=40),  # $20
            _state(
                bankroll=_bankroll(
                    bankroll_usd_cents=50000,
                    open_position_cost_usd_cents=14000,
                ),
            ),
        )
        # 30% of $500 = $150. $140 + $20 = $160 > $150.
        assert isinstance(out, RiskRejection)
        assert out.reason == "exposure_cap_exceeded"

    def test_size_cap_engages_with_quantity_adjustment(self, tmp_path: Path) -> None:
        """Fixed $20 cap binds — risk APPROVES with adjusted_quantity.

        $30 intent (60 contracts at 50c) > $20 cap -> reduces qty to
        $20/50c = 40 contracts."""
        rm = RiskManager(db=_db(tmp_path), config=RiskConfig())
        out = rm.evaluate(
            _intent(target_price_cents=50, target_quantity=60),  # $30
            _state(bankroll=_bankroll(bankroll_usd_cents=50000)),  # $500
        )
        assert isinstance(out, RiskApprovedOrder)
        assert out.adjusted_quantity == 40

    def test_size_cap_below_one_contract_rejects(self, tmp_path: Path) -> None:
        """Cap so tight that even one contract doesn't fit. Use a custom
        config with a $0.50 cap and an 80c intent — 50/80 = 0 contracts
        -> reject."""
        rm = RiskManager(
            db=_db(tmp_path),
            config=RiskConfig(position_size_hard_cap_cents=50),  # $0.50 cap
        )
        out = rm.evaluate(
            _intent(target_price_cents=80, target_quantity=5),  # $4
            _state(bankroll=_bankroll(bankroll_usd_cents=50000)),
        )
        assert isinstance(out, RiskRejection)
        assert out.reason == "size_cap_below_one_contract"

    def test_size_cap_value_read_from_config(self, tmp_path: Path) -> None:
        """Override `position_size_hard_cap_cents` and confirm the
        adjustment math respects it. $50 cap, $30 intent (60 contracts
        at 50c) -> no adjustment because $30 < $50."""
        rm = RiskManager(
            db=_db(tmp_path),
            config=RiskConfig(position_size_hard_cap_cents=5000),  # $50 cap
        )
        out = rm.evaluate(
            _intent(target_price_cents=50, target_quantity=60),  # $30
            _state(bankroll=_bankroll(bankroll_usd_cents=50000)),
        )
        assert isinstance(out, RiskApprovedOrder)
        assert out.adjusted_quantity is None  # cap not engaged

    def test_stop_loss_position_not_open(self, tmp_path: Path) -> None:
        rm = RiskManager(db=_db(tmp_path), config=RiskConfig())
        out = rm.evaluate(_stop(ticker="X"), _state(open_tickers=frozenset({"Y"})))
        assert isinstance(out, RiskRejection)
        assert out.reason == "position_not_open"


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


class TestApprovals:
    def test_entry_happy_path(self, tmp_path: Path) -> None:
        rm = RiskManager(db=_db(tmp_path), config=RiskConfig())
        out = rm.evaluate(_intent(), _state())
        assert isinstance(out, RiskApprovedOrder)
        assert out.intent_type == "entry"
        assert out.adjusted_quantity is None  # no cap engaged

    def test_reentry_intent_routed_through_buy_path(self, tmp_path: Path) -> None:
        rm = RiskManager(db=_db(tmp_path), config=RiskConfig())
        reentry = ReentryIntent(
            ticker="X",
            target_price_cents=40,
            target_quantity=5,
            target_size_usd_cents=200,
            triggering_match_id=2,
            confirmation_weight=0.95,
            confidence_score=0.95,
            reasoning_text="reentry",
            prior_trade_id=99,
            prior_trade_outcome="dry_run_closed_stop",
            prior_trade_realized_pnl_usd_cents=-100,
        )
        out = rm.evaluate(reentry, _state())
        assert isinstance(out, RiskApprovedOrder)
        assert out.intent_type == "reentry"

    def test_stop_loss_with_open_position_approved(self, tmp_path: Path) -> None:
        rm = RiskManager(db=_db(tmp_path), config=RiskConfig())
        out = rm.evaluate(_stop(ticker="X"), _state(open_tickers=frozenset({"X"})))
        assert isinstance(out, RiskApprovedOrder)
        assert out.intent_type == "stop_loss"


# ---------------------------------------------------------------------------
# Decision audit log
# ---------------------------------------------------------------------------


def test_every_decision_writes_a_row(tmp_path: Path) -> None:
    db = _db(tmp_path)
    rm = RiskManager(db=db, config=RiskConfig())
    rm.evaluate(_intent(), _state())  # approved
    rm.evaluate(_intent(target_price_cents=99), _state())  # rejected
    rows = list(db.connect().execute("SELECT decision FROM risk_decisions ORDER BY id"))
    assert [r["decision"] for r in rows] == ["approved", "rejected"]
