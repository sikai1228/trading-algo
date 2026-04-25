"""ApprovalGate tests with a stub ApprovalRequester (no real Telegram)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from trumpbot.approval.gate import ApprovalGate, ApprovalGateConfig, ApprovalRequester
from trumpbot.db.connection import Database
from trumpbot.types.intents import (
    RISK_APPROVAL_TOKEN,
    ReentryIntent,
    RiskApprovedOrder,
    StopLossIntent,
    TradeIntent,
)


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "appr.db")
    db.connect()
    return db


def _entry_approved() -> RiskApprovedOrder:
    return RiskApprovedOrder(
        intent_type="entry",
        intent=TradeIntent(
            ticker="X",
            target_price_cents=50,
            target_quantity=10,
            target_size_usd_cents=500,
            triggering_match_id=1,
            confirmation_weight=0.9,
            confidence_score=0.9,
            reasoning_text="r",
        ),
        risk_decision_id=1,
        approval_token=RISK_APPROVAL_TOKEN,
    )


def _stop_approved() -> RiskApprovedOrder:
    return RiskApprovedOrder(
        intent_type="stop_loss",
        intent=StopLossIntent(
            ticker="X",
            trade_id=1,
            entry_price_cents=80,
            current_bid_cents=20,
            drop_cents=60,
            position_quantity=10,
            cost_basis_usd_cents=800,
            current_value_usd_cents=200,
            unrealized_pnl_usd_cents=-600,
            reasoning_text="s",
        ),
        risk_decision_id=2,
        approval_token=RISK_APPROVAL_TOKEN,
    )


def _reentry_approved() -> RiskApprovedOrder:
    return RiskApprovedOrder(
        intent_type="reentry",
        intent=ReentryIntent(
            ticker="X",
            target_price_cents=40,
            target_quantity=5,
            target_size_usd_cents=200,
            triggering_match_id=3,
            confirmation_weight=0.9,
            confidence_score=0.9,
            reasoning_text="r",
            prior_trade_id=1,
            prior_trade_outcome="dry_run_closed_stop",
            prior_trade_realized_pnl_usd_cents=-100,
        ),
        risk_decision_id=3,
        approval_token=RISK_APPROVAL_TOKEN,
    )


class _StubRequester(ApprovalRequester):
    """Records everything; resolves the stored response on demand."""

    def __init__(self, *, response: tuple[str, str], delay: float = 0.0) -> None:
        self._response = response
        self._delay = delay
        self.sent: list[tuple[str, str, str]] = []
        self.chat_id = "fake-chat"

    async def send_request(self, *, intent_id: str, intent_type: str, message_text: str) -> int:
        self.sent.append((intent_id, intent_type, message_text))
        return 999

    async def await_response(self, *, intent_id: str, timeout_sec: int | None) -> tuple[str, str]:
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._response


class _TimeoutRequester(ApprovalRequester):
    chat_id = None

    async def send_request(self, *, intent_id: str, intent_type: str, message_text: str) -> int:
        return 1

    async def await_response(self, *, intent_id: str, timeout_sec: int | None) -> tuple[str, str]:
        if timeout_sec is None:
            await asyncio.Future()  # would block forever
        await asyncio.sleep((timeout_sec or 0) + 5)  # caller times us out
        return ("approved", "telegram_button")


@pytest.fixture()
def gate_factory(tmp_path: Path):  # type: ignore[no-untyped-def]
    def _make(requester: ApprovalRequester, **cfg) -> tuple[ApprovalGate, Database]:  # type: ignore[no-untyped-def]
        db = _db(tmp_path)
        gate = ApprovalGate(
            db=db,
            config=ApprovalGateConfig(**cfg),
            requester=requester,
        )
        return gate, db

    return _make


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


class TestApprovalDecisions:
    async def test_approval_recorded(self, gate_factory) -> None:  # type: ignore[no-untyped-def]
        req = _StubRequester(response=("approved", "telegram_button"))
        gate, db = gate_factory(req)
        decision = await gate.request_approval(_entry_approved())
        assert decision.decision == "approved"
        assert decision.decision_source == "telegram_button"
        # Audit row written
        row = (
            db.connect()
            .execute("SELECT decision, decision_source FROM telegram_approvals")
            .fetchone()
        )
        assert row["decision"] == "approved"
        assert row["decision_source"] == "telegram_button"

    async def test_rejection_recorded(self, gate_factory) -> None:  # type: ignore[no-untyped-def]
        req = _StubRequester(response=("rejected", "telegram_button"))
        gate, db = gate_factory(req)
        decision = await gate.request_approval(_entry_approved())
        assert decision.decision == "rejected"
        assert decision.decision_source == "telegram_button"

    async def test_send_failure_logs_expired(self, gate_factory) -> None:  # type: ignore[no-untyped-def]
        class _BadSend(ApprovalRequester):
            chat_id = None

            async def send_request(self, **_kwargs) -> int:  # type: ignore[no-untyped-def]
                raise RuntimeError("telegram down")

            async def await_response(self, **_kwargs):  # type: ignore[no-untyped-def]
                raise NotImplementedError

        gate, db = gate_factory(_BadSend())
        decision = await gate.request_approval(_entry_approved())
        assert decision.decision == "expired"
        assert decision.decision_source == "timeout"


# ---------------------------------------------------------------------------
# Timeout per intent type
# ---------------------------------------------------------------------------


class TestTimeoutPerType:
    async def test_entry_uses_180_default(self, gate_factory) -> None:  # type: ignore[no-untyped-def]
        # Stub returns instantly; we verify that timeout was passed.
        captured: dict[str, int | None] = {}

        class _Capture(ApprovalRequester):
            chat_id = None

            async def send_request(self, **_kwargs) -> int:  # type: ignore[no-untyped-def]
                return 1

            async def await_response(
                self, *, intent_id: str, timeout_sec: int | None
            ) -> tuple[str, str]:
                captured["timeout"] = timeout_sec
                return ("approved", "telegram_button")

        gate, _ = gate_factory(_Capture())
        await gate.request_approval(_entry_approved())
        assert captured["timeout"] == 180

    async def test_stop_loss_no_timeout(self, gate_factory) -> None:  # type: ignore[no-untyped-def]
        captured: dict[str, int | None] = {}

        class _Capture(ApprovalRequester):
            chat_id = None

            async def send_request(self, **_kwargs) -> int:  # type: ignore[no-untyped-def]
                return 1

            async def await_response(
                self, *, intent_id: str, timeout_sec: int | None
            ) -> tuple[str, str]:
                captured["timeout"] = timeout_sec
                return ("approved", "telegram_button")

        gate, _ = gate_factory(_Capture())
        await gate.request_approval(_stop_approved())
        assert captured["timeout"] is None

    async def test_reentry_no_timeout(self, gate_factory) -> None:  # type: ignore[no-untyped-def]
        captured: dict[str, int | None] = {}

        class _Capture(ApprovalRequester):
            chat_id = None

            async def send_request(self, **_kwargs) -> int:  # type: ignore[no-untyped-def]
                return 1

            async def await_response(
                self, *, intent_id: str, timeout_sec: int | None
            ) -> tuple[str, str]:
                captured["timeout"] = timeout_sec
                return ("approved", "telegram_button")

        gate, _ = gate_factory(_Capture())
        await gate.request_approval(_reentry_approved())
        assert captured["timeout"] is None


# ---------------------------------------------------------------------------
# Message format
# ---------------------------------------------------------------------------


class TestMessageContents:
    async def test_entry_message_contains_required_fields(self, gate_factory) -> None:  # type: ignore[no-untyped-def]
        req = _StubRequester(response=("approved", "telegram_button"))
        gate, db = gate_factory(req)
        await gate.request_approval(_entry_approved())
        message = req.sent[0][2]
        assert "💰 TRADE PROPOSAL" in message
        assert "Ticker: X" in message
        assert "BUY YES" in message

    async def test_stop_loss_message_uses_warning_header(self, gate_factory) -> None:  # type: ignore[no-untyped-def]
        req = _StubRequester(response=("approved", "telegram_button"))
        gate, _ = gate_factory(req)
        await gate.request_approval(_stop_approved())
        assert "⚠️ STOP-LOSS TRIGGER" in req.sent[0][2]

    async def test_reentry_message_uses_reentry_header(self, gate_factory) -> None:  # type: ignore[no-untyped-def]
        req = _StubRequester(response=("approved", "telegram_button"))
        gate, _ = gate_factory(req)
        await gate.request_approval(_reentry_approved())
        assert "🔄 RE-ENTRY OPPORTUNITY" in req.sent[0][2]
