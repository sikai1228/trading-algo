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
    def _make(  # type: ignore[no-untyped-def]
        requester: ApprovalRequester,
        *,
        depth_fn=None,
        **cfg,
    ) -> tuple[ApprovalGate, Database]:
        db = _db(tmp_path)
        gate = ApprovalGate(
            db=db,
            config=ApprovalGateConfig(**cfg),
            requester=requester,
            depth_fn=depth_fn,
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


# ---------------------------------------------------------------------------
# Phase 3 Part 1 — FOK re-walk on user approval
# ---------------------------------------------------------------------------


def _entry_approved_with_walk(
    *,
    target_avg: int = 50,
    target_qty: int = 40,
    target_budget: int = 2000,
) -> RiskApprovedOrder:
    """A RiskApprovedOrder whose intent carries Phase-3 walk fields,
    so the gate's FOK re-walk has something to compare against."""
    return RiskApprovedOrder(
        intent_type="entry",
        intent=TradeIntent(
            ticker="X",
            target_price_cents=80,
            target_quantity=target_qty,
            target_size_usd_cents=target_budget,
            triggering_match_id=1,
            confidence_score=0.9,
            target_avg_fill_price_cents=target_avg,
            target_max_fill_price_cents=target_avg,
            estimated_fees_cents=10,
            estimated_total_cost_cents=target_budget + 10,
            cap_binding="cap_one",
            cap_one_value_cents=2000,
            cap_two_value_cents=500_000,
            slippage_cents=0,
            levels_consumed=[(target_avg, target_qty)],
            reasoning_text="phase-3 fok test fixture",
        ),
        risk_decision_id=1,
        approval_token=RISK_APPROVAL_TOKEN,
    )


class TestFokRewalkOnApproval:
    async def test_book_unchanged_passes_through(self, gate_factory) -> None:  # type: ignore[no-untyped-def]
        """User approves; live book is the same as decision-time
        (single 50c level deep enough). Gate passes through approved."""
        req = _StubRequester(response=("approved", "telegram_button"))
        gate, db = gate_factory(req, depth_fn=lambda _t: [(50, 1000)])
        decision = await gate.request_approval(_entry_approved_with_walk())
        assert decision.decision == "approved"
        # No fok-killed event written.
        events = list(
            db.connect().execute(
                "SELECT * FROM system_events WHERE event_type = 'fok_killed_book_moved'"
            )
        )
        assert events == []

    async def test_book_moves_avg_drift_above_5c_kills(self, gate_factory) -> None:  # type: ignore[no-untyped-def]
        """User approves; live book filled the same 20 contracts but at
        70c avg vs the engine's 50c target — drift is 20c > 5c
        tolerance, so the gate downgrades approval to 'rejected' and
        writes a fok_killed_book_moved system event."""
        req = _StubRequester(response=("approved", "telegram_button"))
        gate, db = gate_factory(
            req,
            depth_fn=lambda _t: [(70, 1000)],
        )
        # target_qty=20 at $14 budget -> re-walk at 70c fills 20
        # contracts (1400/70=20 exact). Avg 70 vs target 50 -> drift 20c.
        decision = await gate.request_approval(
            _entry_approved_with_walk(target_avg=50, target_qty=20, target_budget=1400)
        )
        assert decision.decision == "rejected"
        events = list(
            db.connect().execute(
                "SELECT message FROM system_events WHERE event_type = 'fok_killed_book_moved'"
            )
        )
        assert len(events) == 1
        assert "Original avg fill: 50c" in events[0]["message"]
        assert "New: 70c" in events[0]["message"]

    async def test_book_moves_qty_drift_above_20pct_kills(self, gate_factory) -> None:  # type: ignore[no-untyped-def]
        """Avg-fill stays in tolerance but quantity drift > 20 % -> KILL.
        Engine targeted 100 contracts; book only has 50 -> 50 % qty
        drop (well above 20 % tolerance)."""
        req = _StubRequester(response=("approved", "telegram_button"))
        gate, db = gate_factory(
            req,
            depth_fn=lambda _t: [(50, 50)],  # only 50 contracts available
        )
        decision = await gate.request_approval(
            _entry_approved_with_walk(target_avg=50, target_qty=100, target_budget=5000)
        )
        assert decision.decision == "rejected"
        events = list(
            db.connect().execute(
                "SELECT * FROM system_events WHERE event_type = 'fok_killed_book_moved'"
            )
        )
        assert len(events) == 1

    async def test_no_depth_at_approval_time_kills(self, gate_factory) -> None:  # type: ignore[no-untyped-def]
        """depth_fn returns None / empty -> KILL with 'no order-book
        depth available'."""
        req = _StubRequester(response=("approved", "telegram_button"))
        gate, db = gate_factory(req, depth_fn=lambda _t: None)
        decision = await gate.request_approval(_entry_approved_with_walk())
        assert decision.decision == "rejected"

    async def test_user_rejection_skips_fok_check(self, gate_factory) -> None:  # type: ignore[no-untyped-def]
        """If the user REJECTS, the gate must not bother re-walking
        and must never log a kill event."""
        req = _StubRequester(response=("rejected", "telegram_button"))
        # depth_fn would FAIL the FOK check if invoked, but we
        # shouldn't get there.
        gate, db = gate_factory(
            req,
            depth_fn=lambda _t: [(99, 1000)],
        )
        decision = await gate.request_approval(_entry_approved_with_walk())
        assert decision.decision == "rejected"
        events = list(
            db.connect().execute(
                "SELECT * FROM system_events WHERE event_type = 'fok_killed_book_moved'"
            )
        )
        assert events == []

    async def test_stop_loss_skips_fok_check(self, gate_factory) -> None:  # type: ignore[no-untyped-def]
        """Stop-loss intents have no walk to compare; the gate passes
        the user's decision through unchanged."""
        req = _StubRequester(response=("approved", "telegram_button"))
        # depth_fn would NOT match a stop-loss intent; should be untouched.
        called: list[str] = []

        def _depth(t: str):  # type: ignore[no-untyped-def]
            called.append(t)
            return [(99, 1000)]

        gate, _ = gate_factory(req, depth_fn=_depth)
        decision = await gate.request_approval(_stop_approved())
        assert decision.decision == "approved"
        assert called == []  # depth_fn never invoked for stop-loss
