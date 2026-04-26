"""KalshiExecutor — Phase 4 Part 1 live executor tests.

Mocks the KalshiClient (specifically ``place_order`` /
``get_order_by_client_id``) so the tests don't hit Kalshi. Idempotency
+ FOK + status-state-machine + error categorization are all covered
without touching the network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    MarketRow,
    NewsEventRow,
    get_trade_by_client_order_id,
    insert_news_event,
    upsert_market,
)
from trumpbot.decision.engine import BankrollState
from trumpbot.execution.dry_run import Quote
from trumpbot.execution.live_executor import HaltCallback, KalshiExecutor
from trumpbot.kalshi.exceptions import StateError, TransientError, ValidationError
from trumpbot.kalshi.schemas import KalshiOrder
from trumpbot.risk.manager import RiskConfig, RiskManager, RiskState
from trumpbot.types.intents import (
    RISK_APPROVAL_TOKEN,
    RiskApprovedOrder,
    StopLossIntent,
    TradeIntent,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "exec.db")
    db.connect()
    upsert_market(
        db,
        MarketRow(
            ticker="X",
            series_ticker="KXTRUMPMEET",
            event_ticker="KXTRUMPMEET-26APR",
            title="Trump and X meet before May 1, 2026?",
            subtitle=None,
            yes_sub_title=None,
            no_sub_title=None,
            subject="x",
            subject_full_name="X",
            resolution_rules="r",
            approved_sources=None,
            open_ts="2026-04-01T00:00:00Z",
            close_ts="2026-04-30T23:59:59Z",
            expected_expiration_ts=None,
            status="active",
            last_price_cents=50,
            volume=1000,
            open_interest=200,
            raw_json=None,
        ),
    )
    return db


def _seed_match(db: Database) -> int:
    eid = insert_news_event(
        db,
        NewsEventRow(
            source="reuters_via_gnews",
            source_weight=1.0,
            is_kalshi_approved=True,
            headline="h",
            url="https://e.com/1",
            url_canonical="https://e.com/1",
            body_excerpt=None,
            author=None,
            raw_published_ts=None,
            detected_ts="2026-04-15T12:00:00Z",
            has_photo=False,
            has_video=False,
            raw_data=None,
        ),
    )
    assert eid is not None
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO news_market_matches (news_event_id, ticker, confidence, "
            "matched_subject, match_reason) VALUES (?, 'X', 0.9, 'x', 'test')",
            (eid,),
        )
        return int(cur.lastrowid or 0)


def _approve(db: Database, intent: TradeIntent | StopLossIntent) -> RiskApprovedOrder:
    rm = RiskManager(db=db, config=RiskConfig())
    state = RiskState(
        bankroll=BankrollState(50000, 0),
        open_position_tickers=(
            frozenset({intent.ticker}) if isinstance(intent, StopLossIntent) else frozenset()
        ),
    )
    out = rm.evaluate(intent, state)
    assert isinstance(out, RiskApprovedOrder)
    return out


def _intent_with_walk(*, match_id: int, qty: int = 10, avg: int = 50) -> TradeIntent:
    return TradeIntent(
        ticker="X",
        target_price_cents=80,
        target_quantity=qty,
        target_size_usd_cents=avg * qty,
        triggering_match_id=match_id,
        confirmation_weight=0.9,
        confidence_score=0.9,
        target_avg_fill_price_cents=avg,
        target_max_fill_price_cents=avg,
        estimated_fees_cents=10,
        estimated_total_cost_cents=avg * qty + 10,
        cap_binding="cap_one",
        cap_one_value_cents=2000,
        cap_two_value_cents=500_000,
        slippage_cents=0,
        levels_consumed=[(avg, qty)],
        reasoning_text="phase-4 fixture",
    )


class _FakeKalshi:
    """Minimal stand-in for :class:`KalshiClient`. Tests configure the
    response shape per case; recordings let us assert idempotency."""

    def __init__(
        self,
        *,
        place_order_result: KalshiOrder | None = None,
        place_order_exception: Exception | None = None,
    ) -> None:
        self._result = place_order_result
        self._exc = place_order_exception
        self.place_order_calls: list[dict[str, Any]] = []
        self.get_order_by_client_id_calls: list[str] = []

    async def place_order(self, **kwargs: Any) -> KalshiOrder:
        self.place_order_calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result

    async def get_order_by_client_id(self, client_order_id: str) -> KalshiOrder | None:
        self.get_order_by_client_id_calls.append(client_order_id)
        return None


def _book(*, bid: int | None, ask: int | None) -> Quote:
    return Quote(yes_bid_cents=bid, yes_ask_cents=ask)


def _make_kalshi_order(
    *,
    order_id: str = "kalshi-1",
    client_order_id: str = "test-uuid-1",
    ticker: str = "X",
    status: str = "executed",
    count: int = 10,
    remaining_count: int = 0,
    avg_fill_price: int | None = 50,
) -> KalshiOrder:
    return KalshiOrder(
        order_id=order_id,
        client_order_id=client_order_id,
        ticker=ticker,
        status=status,
        side="yes",
        action="buy",
        type="limit",
        yes_price=avg_fill_price,
        count=count,
        remaining_count=remaining_count,
        filled_count=count - remaining_count,
        avg_fill_price=avg_fill_price,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIdempotencyAndStateMachine:
    async def test_pending_row_inserted_before_api_call(self, tmp_path: Path) -> None:
        """The trade row must hit the DB with status='pending' BEFORE
        the place_order coroutine is awaited. Verified by side-effecting
        the fake client to inspect the DB at call time."""
        db = _db(tmp_path)
        match_id = _seed_match(db)
        intent = _intent_with_walk(match_id=match_id)
        approved = _approve(db, intent)

        captured_status: list[str] = []

        async def _capture_then_succeed(**kwargs: Any) -> KalshiOrder:
            row = get_trade_by_client_order_id(db, kwargs["client_order_id"])
            assert row is not None
            captured_status.append(row["status"])
            return _make_kalshi_order(client_order_id=kwargs["client_order_id"])

        kalshi = _FakeKalshi(place_order_result=_make_kalshi_order())
        kalshi.place_order = _capture_then_succeed  # type: ignore[method-assign]

        executor = KalshiExecutor(
            db=db,
            kalshi_client=kalshi,  # type: ignore[arg-type]
            orderbook_fn=lambda _t: _book(bid=49, ask=50),
            depth_fn=lambda _t: [(50, 1000)],
            client_order_id_factory=lambda: "test-uuid-1",
        )
        result = await executor.submit(approved)
        assert captured_status == ["pending"]
        assert result.status == "filled"
        # After ack the row should be promoted to live.
        row = get_trade_by_client_order_id(db, "test-uuid-1")
        assert row is not None
        assert row["status"] == "live"
        assert row["kalshi_order_id"] == "kalshi-1"

    async def test_client_order_id_persisted(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        match_id = _seed_match(db)
        intent = _intent_with_walk(match_id=match_id)
        approved = _approve(db, intent)
        kalshi = _FakeKalshi(place_order_result=_make_kalshi_order())
        executor = KalshiExecutor(
            db=db,
            kalshi_client=kalshi,  # type: ignore[arg-type]
            orderbook_fn=lambda _t: _book(bid=49, ask=50),
            depth_fn=lambda _t: [(50, 1000)],
            client_order_id_factory=lambda: "test-uuid-1",
        )
        await executor.submit(approved)
        # The kwarg the executor passed must equal what's in the DB.
        assert kalshi.place_order_calls[0]["client_order_id"] == "test-uuid-1"
        row = get_trade_by_client_order_id(db, "test-uuid-1")
        assert row is not None

    async def test_kalshi_fok_kill_marks_killed_no_fill(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        match_id = _seed_match(db)
        intent = _intent_with_walk(match_id=match_id)
        approved = _approve(db, intent)
        # Kalshi reports order canceled with 0 filled (FOK rejected).
        kalshi = _FakeKalshi(
            place_order_result=_make_kalshi_order(status="canceled", count=10, remaining_count=10)
        )
        executor = KalshiExecutor(
            db=db,
            kalshi_client=kalshi,  # type: ignore[arg-type]
            orderbook_fn=lambda _t: _book(bid=49, ask=50),
            depth_fn=lambda _t: [(50, 1000)],
            client_order_id_factory=lambda: "test-uuid-1",
        )
        result = await executor.submit(approved)
        assert result.status == "rejected"
        row = get_trade_by_client_order_id(db, "test-uuid-1")
        assert row is not None
        assert row["status"] == "killed_no_fill"


class TestErrorCategorization:
    async def test_validation_error_marks_error_validation(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        match_id = _seed_match(db)
        intent = _intent_with_walk(match_id=match_id)
        approved = _approve(db, intent)
        exc = ValidationError("bad request", status_code=400)
        kalshi = _FakeKalshi(place_order_exception=exc)
        executor = KalshiExecutor(
            db=db,
            kalshi_client=kalshi,  # type: ignore[arg-type]
            orderbook_fn=lambda _t: _book(bid=49, ask=50),
            depth_fn=lambda _t: [(50, 1000)],
            client_order_id_factory=lambda: "test-uuid-1",
        )
        result = await executor.submit(approved)
        assert result.status == "rejected"
        row = get_trade_by_client_order_id(db, "test-uuid-1")
        assert row is not None
        assert row["status"] == "error_validation"

    async def test_transient_error_marks_error_transient(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        match_id = _seed_match(db)
        intent = _intent_with_walk(match_id=match_id)
        approved = _approve(db, intent)
        exc = TransientError("network timeout")
        kalshi = _FakeKalshi(place_order_exception=exc)
        executor = KalshiExecutor(
            db=db,
            kalshi_client=kalshi,  # type: ignore[arg-type]
            orderbook_fn=lambda _t: _book(bid=49, ask=50),
            depth_fn=lambda _t: [(50, 1000)],
            client_order_id_factory=lambda: "test-uuid-1",
        )
        result = await executor.submit(approved)
        assert result.status == "rejected"
        row = get_trade_by_client_order_id(db, "test-uuid-1")
        assert row is not None
        assert row["status"] == "error_transient"

    async def test_state_error_triggers_halt_callback(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        match_id = _seed_match(db)
        intent = _intent_with_walk(match_id=match_id)
        approved = _approve(db, intent)
        exc = StateError("insufficient_funds", status_code=400)
        halted: list[str] = []
        kalshi = _FakeKalshi(place_order_exception=exc)
        executor = KalshiExecutor(
            db=db,
            kalshi_client=kalshi,  # type: ignore[arg-type]
            orderbook_fn=lambda _t: _book(bid=49, ask=50),
            depth_fn=lambda _t: [(50, 1000)],
            halt_callback=HaltCallback(callback=lambda r: halted.append(r)),
            client_order_id_factory=lambda: "test-uuid-1",
        )
        result = await executor.submit(approved)
        assert result.status == "rejected"
        assert len(halted) == 1
        assert "insufficient_funds" in halted[0]

    async def test_duplicate_client_order_treated_as_landed(self, tmp_path: Path) -> None:
        """When Kalshi reports a duplicate client_order_id, the original
        submission landed; we promote optimistically and let
        reconciliation harden."""
        db = _db(tmp_path)
        match_id = _seed_match(db)
        intent = _intent_with_walk(match_id=match_id)
        approved = _approve(db, intent)
        exc = ValidationError(
            "Bad Request", status_code=400, response_body="duplicate_client_order_id"
        )
        kalshi = _FakeKalshi(place_order_exception=exc)
        executor = KalshiExecutor(
            db=db,
            kalshi_client=kalshi,  # type: ignore[arg-type]
            orderbook_fn=lambda _t: _book(bid=49, ask=50),
            depth_fn=lambda _t: [(50, 1000)],
            client_order_id_factory=lambda: "test-uuid-1",
        )
        result = await executor.submit(approved)
        # Treated as a successful landing.
        assert result.status == "filled"
        row = get_trade_by_client_order_id(db, "test-uuid-1")
        assert row is not None
        assert row["status"] == "live"


class TestFokGatePreservedInLive:
    async def test_killed_when_no_depth(self, tmp_path: Path) -> None:
        """No order-book depth at submit time -> the executor never even
        calls Kalshi. Same Phase 3 FOK semantics as DryRunExecutor."""
        db = _db(tmp_path)
        match_id = _seed_match(db)
        intent = _intent_with_walk(match_id=match_id)
        approved = _approve(db, intent)
        kalshi = _FakeKalshi(place_order_result=_make_kalshi_order())
        executor = KalshiExecutor(
            db=db,
            kalshi_client=kalshi,  # type: ignore[arg-type]
            orderbook_fn=lambda _t: _book(bid=None, ask=None),
            depth_fn=lambda _t: None,
            client_order_id_factory=lambda: "test-uuid-1",
        )
        result = await executor.submit(approved)
        assert result.status == "rejected"
        # No DB row, no Kalshi call.
        assert kalshi.place_order_calls == []
        row = get_trade_by_client_order_id(db, "test-uuid-1")
        assert row is None

    async def test_killed_when_book_moved(self, tmp_path: Path) -> None:
        """Re-walk avg above target avg -> FOK kill, no Kalshi call."""
        db = _db(tmp_path)
        match_id = _seed_match(db)
        # Engine sized for avg 50c; book moved to 70c.
        intent = _intent_with_walk(match_id=match_id, avg=50, qty=20)
        approved = _approve(db, intent)
        kalshi = _FakeKalshi(place_order_result=_make_kalshi_order())
        executor = KalshiExecutor(
            db=db,
            kalshi_client=kalshi,  # type: ignore[arg-type]
            orderbook_fn=lambda _t: _book(bid=68, ask=70),
            depth_fn=lambda _t: [(70, 1000)],
            client_order_id_factory=lambda: "test-uuid-1",
        )
        result = await executor.submit(approved)
        assert result.status == "rejected"
        assert kalshi.place_order_calls == []


class TestStopLoss:
    async def test_stop_loss_uses_sell_order(self, tmp_path: Path) -> None:
        """Stop-loss closes via sell-yes FOK, not buy."""
        db = _db(tmp_path)
        match_id = _seed_match(db)
        # Manually insert an open live trade row so the stop-loss has
        # something to close.
        with db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO risk_decisions (intent_type, intent_json, decision, "
                "rejection_reason, rule_fired, reasoning_text) "
                "VALUES ('entry', '{}', 'approved', NULL, NULL, 't')"
            )
            rd_id = cur.lastrowid
            cur = conn.execute(
                """
                INSERT INTO trades (
                    ticker, side, action, status,
                    entry_price_cents, quantity, cost_basis_usd_cents,
                    triggering_match_id, triggering_intent_json,
                    risk_decision_id, approval_id, is_reentry, prior_trade_id,
                    reasoning_text, entered_at, kalshi_order_id, client_order_id
                ) VALUES (
                    'X', 'yes', 'buy', 'live',
                    80, 10, 800,
                    ?, '{}',
                    ?, NULL, 0, NULL,
                    'r', '2026-04-15T00:00:00Z', 'kalshi-prior', 'uuid-prior'
                )
                """,
                (match_id, rd_id),
            )
            trade_id = cur.lastrowid
            assert trade_id is not None

        stop = StopLossIntent(
            ticker="X",
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
        approved_stop = RiskApprovedOrder(
            intent_type="stop_loss",
            intent=stop,
            risk_decision_id=rd_id,  # type: ignore[arg-type]
            approval_token=RISK_APPROVAL_TOKEN,
        )

        kalshi_order = _make_kalshi_order(
            order_id="kalshi-stop",
            client_order_id="uuid-stop",
            status="executed",
            count=10,
            remaining_count=0,
            avg_fill_price=20,
        )
        kalshi = _FakeKalshi(place_order_result=kalshi_order)
        executor = KalshiExecutor(
            db=db,
            kalshi_client=kalshi,  # type: ignore[arg-type]
            orderbook_fn=lambda _t: _book(bid=20, ask=22),
            depth_fn=lambda _t: [(20, 100)],
            client_order_id_factory=lambda: "uuid-stop",
        )
        result = await executor.submit(approved_stop)
        assert result.status == "filled"
        assert kalshi.place_order_calls[0]["action"] == "sell"
        # Trade row is closed with the live status.
        row = (
            db.connect()
            .execute("SELECT status, exit_price_cents FROM trades WHERE id = ?", (trade_id,))
            .fetchone()
        )
        assert row["status"] == "live_closed_stop"
        assert row["exit_price_cents"] == 20


class TestPositionMarking:
    async def test_marks_only_live_rows(self, tmp_path: Path) -> None:
        """update_position_marks must only touch live rows, not
        dry_run/pending/etc. — uses list_open_live_trades."""
        db = _db(tmp_path)
        match_id = _seed_match(db)
        # Insert one live row.
        with db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO risk_decisions (intent_type, intent_json, decision, "
                "rejection_reason, rule_fired, reasoning_text) "
                "VALUES ('entry', '{}', 'approved', NULL, NULL, 't')"
            )
            rd_id = cur.lastrowid
            cur = conn.execute(
                """
                INSERT INTO trades (
                    ticker, side, action, status,
                    entry_price_cents, quantity, cost_basis_usd_cents,
                    triggering_match_id, triggering_intent_json,
                    risk_decision_id, approval_id, is_reentry, prior_trade_id,
                    reasoning_text, entered_at, client_order_id
                ) VALUES (
                    'X', 'yes', 'buy', 'live',
                    50, 10, 500,
                    ?, '{}', ?, NULL, 0, NULL, 'r',
                    '2026-04-15T00:00:00Z', 'uuid-1'
                )
                """,
                (match_id, rd_id),
            )
            trade_id = cur.lastrowid
        kalshi = _FakeKalshi(place_order_result=_make_kalshi_order())
        executor = KalshiExecutor(
            db=db,
            kalshi_client=kalshi,  # type: ignore[arg-type]
            orderbook_fn=lambda _t: _book(bid=70, ask=72),
            depth_fn=lambda _t: [(72, 100)],
        )
        updated = executor.update_position_marks()
        assert updated == 1
        row = (
            db.connect()
            .execute("SELECT unrealized_pnl_usd_cents FROM trades WHERE id = ?", (trade_id,))
            .fetchone()
        )
        # bid 70c x 10 = 700c; cost 500c; unrealized +200.
        assert row["unrealized_pnl_usd_cents"] == 200


class TestCloseResolved:
    def test_yes_resolution_uses_live_status(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        match_id = _seed_match(db)
        with db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO risk_decisions (intent_type, intent_json, decision, "
                "rejection_reason, rule_fired, reasoning_text) "
                "VALUES ('entry', '{}', 'approved', NULL, NULL, 't')"
            )
            rd_id = cur.lastrowid
            cur = conn.execute(
                """
                INSERT INTO trades (
                    ticker, side, action, status,
                    entry_price_cents, quantity, cost_basis_usd_cents,
                    triggering_match_id, triggering_intent_json,
                    risk_decision_id, approval_id, is_reentry, prior_trade_id,
                    reasoning_text, entered_at, client_order_id
                ) VALUES (
                    'X', 'yes', 'buy', 'live',
                    50, 10, 500,
                    ?, '{}', ?, NULL, 0, NULL, 'r',
                    '2026-04-15T00:00:00Z', 'uuid-1'
                )
                """,
                (match_id, rd_id),
            )
            trade_id = cur.lastrowid
        kalshi = _FakeKalshi(place_order_result=_make_kalshi_order())
        executor = KalshiExecutor(
            db=db,
            kalshi_client=kalshi,  # type: ignore[arg-type]
            orderbook_fn=lambda _t: _book(bid=49, ask=50),
            depth_fn=lambda _t: [(50, 100)],
        )
        result = executor.close_resolved(ticker="X", resolution="settled_yes")
        assert result is not None
        row = db.connect().execute("SELECT status FROM trades WHERE id = ?", (trade_id,)).fetchone()
        assert row["status"] == "live_closed_resolved_yes"


# ---------------------------------------------------------------------------
# Direct module function tests
# ---------------------------------------------------------------------------


class TestErrorCategorizer:
    def test_validation(self) -> None:
        from trumpbot.kalshi.errors import categorize_order_error

        cat = categorize_order_error(ValidationError("bad", status_code=400))
        assert cat.name == "validation"
        assert cat.trade_status == "error_validation"
        assert cat.should_halt is False

    def test_transient(self) -> None:
        from trumpbot.kalshi.errors import categorize_order_error

        cat = categorize_order_error(TransientError("net"))
        assert cat.name == "transient"
        assert cat.trade_status == "error_transient"

    def test_state(self) -> None:
        from trumpbot.kalshi.errors import categorize_order_error

        cat = categorize_order_error(StateError("insufficient", status_code=400))
        assert cat.name == "state"
        assert cat.should_halt is True

    def test_duplicate(self) -> None:
        from trumpbot.kalshi.errors import categorize_order_error

        exc = ValidationError(
            "Bad Request", status_code=400, response_body="duplicate_client_order_id"
        )
        cat = categorize_order_error(exc)
        assert cat.name == "duplicate_client_order"
        assert cat.trade_status is None  # caller looks up the original

    def test_unknown(self) -> None:
        from trumpbot.kalshi.errors import categorize_order_error

        cat = categorize_order_error(RuntimeError("???"))
        assert cat.name == "unknown"


# ---------------------------------------------------------------------------
# Approval mode invariant
# ---------------------------------------------------------------------------


def test_approval_mode_hardcoded_human() -> None:
    """The single most important Phase 4 invariant: auto-approve is
    NOT reachable through config. A test failure here means somebody
    re-introduced a config knob; revert before merging."""
    from trumpbot.approval.gate import APPROVAL_MODE
    from trumpbot.config import ApprovalPhaseConfig

    assert APPROVAL_MODE == "human"
    # The config dataclass must NOT carry a `mode` field anymore.
    cfg = ApprovalPhaseConfig()
    with pytest.raises(AttributeError):
        _ = cfg.mode  # type: ignore[attr-defined]


__all__: list[str] = []
